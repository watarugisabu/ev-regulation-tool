# -*- coding: utf-8 -*-
"""
EV充電器設置工事 規制区域自動判定ツール v3.3

v3.3の変更点:
- 景観計画データを全国版（A35a_ALL_Japan.geojson.gz）に対応
- gzip圧縮された景観計画データの読み込みに対応
"""

import os
import io
import re
import glob
import gzip
import time
import requests

import streamlit as st
import pandas as pd

try:
    import geopandas as gpd
    from shapely.geometry import Point
    HAS_GIS = True
except ImportError:
    HAS_GIS = False

try:
    import folium
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

from ksj_codes import (
    translate_natural_park_class,
    translate_natural_park_name,
    translate_layer_type,
    translate_prefecture,
    translate_landscape_plan_status,
    get_landscape_ordinance,
    determine_area_type_by_layers,
)


# =============================================================================
# 確認用URL生成関数
# =============================================================================
EADAS_TOP_URL = "https://eadas.env.go.jp/eiadb/ebidbs/"


def build_gsi_map_url(lat, lng, zoom=16):
    if lat is None or lng is None:
        return ""
    return f"https://maps.gsi.go.jp/#{zoom}/{lat:.6f}/{lng:.6f}/&base=std&ls=std"


def build_google_map_url(lat, lng):
    if lat is None or lng is None:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"


def build_eadas_url():
    return EADAS_TOP_URL


# =============================================================================
# カラム候補
# =============================================================================
PARK_CLASS_COL_CANDIDATES = [
    "A10_003", "A10-10_003", "A10_15_003", "A10-15_003",
    "naturalParkClassCode",
]
PARK_NAME_COL_CANDIDATES = [
    "OBJ_NAME",
    "A10_005", "A10-10_005", "A10_15_005", "A10-15_005",
    "naturalParkNameCode",
]
LAYER_CD_COL_CANDIDATES = ["layer_cd", "LAYER_NO"]
PREF_CD_COL_CANDIDATES = ["pref_cd", "PREFEC_CD", "A10_001", "A10_15_001"]
CTV_NAME_COL_CANDIDATES = ["CTV_NAME"]

LANDSCAPE_ORG_COL_CANDIDATES = [
    "A35a_003", "A35b_003", "A35c_003",
    "A35d_003", "A35e_003", "A35f_003",
    "A35a-14_003", "A35b-14_003", "A35d-14_003", "A35e-14_003", "A35f-14_003",
]
LANDSCAPE_STATUS_COL_CANDIDATES = [
    "A35a_007", "A35b_007", "A35d_007", "A35e_007", "A35f_007",
    "A35a-14_007", "A35b-14_007",
]
LANDSCAPE_PREF_COL_CANDIDATES = [
    "A35a_002", "A35b_002", "A35d_002", "A35e_002", "A35f_002",
    "pref_cd",
]


def pick_first_value(row, candidates):
    for col in candidates:
        if col in row.index:
            v = row[col]
            if pd.notna(v) and str(v).strip() not in ("", "nan", "None"):
                return str(v).strip(), col
    return "", None


# =============================================================================
# 番地検出
# =============================================================================
def has_banchi(address: str) -> bool:
    if not address or not isinstance(address, str):
        return False
    addr = address.strip()
    if not addr:
        return False
    if re.search(r"\d+\s*(丁目|番地|番|号)", addr):
        return True
    last_part = addr[-15:]
    if re.search(r"\d+(-\d+)*\s*$", last_part) or re.search(r"\d+(-\d+)+", last_part):
        return True
    if re.search(r"\d+[\-ー]\d+", addr):
        return True
    return False


def parse_lat_lng(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            f = float(value)
            if f != f:
                return None
            return f
        s = str(value).strip()
        if s == "" or s.lower() in ("nan", "none"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def is_valid_japan_coords(lat, lng):
    if lat is None or lng is None:
        return False
    if not (20 <= lat <= 46):
        return False
    if not (122 <= lng <= 154):
        return False
    return True


# =============================================================================
# ページ設定
# =============================================================================
st.set_page_config(
    page_title="EV充電器設置工事 規制区域自動判定ツール",
    page_icon="⚡",
    layout="wide",
)

st.markdown("""
<style>
.main-header {
    font-size: 1.8rem; font-weight: 700; color: #1a365d;
    padding: 0.6rem 0; border-bottom: 3px solid #3182ce;
    margin-bottom: 1rem;
}
</style>
""", unsafe_allow_html=True)


# =============================================================================
# データ読み込み
# =============================================================================
def _read_geojson_or_shp(path):
    """GeoJSON / gzip GeoJSON / Shapefileを統一的に読み込む"""
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            data_bytes = f.read()
        return gpd.read_file(io.BytesIO(data_bytes))
    else:
        try:
            return gpd.read_file(path, encoding="cp932")
        except Exception:
            return gpd.read_file(path)


@st.cache_data(show_spinner=False)
def load_natural_park_gdf(data_dir: str):
    if not HAS_GIS or not os.path.isdir(data_dir):
        return None
    candidates = []
    for pat in ["*A10*park*optimized*.geojson.gz", "*A10*park*.geojson.gz", "*A10*.geojson.gz"]:
        candidates.extend(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    for pat in ["*A10*park*optimized*.geojson", "*A10*park*.geojson",
                "*A10-15*.geojson", "*A10*.geojson"]:
        candidates.extend(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    for pat in ["*A10-15*.shp", "*A10*NaturalPark*.shp", "*A10*.shp", "*自然公園*.shp"]:
        candidates.extend(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))

    if not candidates:
        return None
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    path = ordered[0]

    try:
        gdf = _read_geojson_or_shp(path)
    except Exception as e:
        st.error(f"自然公園データ読み込みエラー: {e}")
        return None

    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    gdf.attrs["source_file"] = path
    return gdf


@st.cache_data(show_spinner=False)
def load_landscape_gdf(data_dir: str):
    """A35a 全国統合GeoJSON（gzip）または個別Shapefileを読み込む"""
    if not HAS_GIS or not os.path.isdir(data_dir):
        return None

    # 優先順: 全国統合gzip > 全国統合GeoJSON > 個別Shapefile
    candidates = []
    for pat in ["*A35a*ALL*.geojson.gz", "*A35*ALL*.geojson.gz",
                "*A35*Japan*.geojson.gz", "*A35*all*.geojson.gz"]:
        candidates.extend(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    for pat in ["*A35a*ALL*.geojson", "*A35*ALL*.geojson",
                "*A35*Japan*.geojson", "*A35*all*.geojson"]:
        candidates.extend(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    # gzipの個別ファイルもサポート
    for pat in ["*A35*.geojson.gz", "*A35*.geojson"]:
        candidates.extend(glob.glob(os.path.join(data_dir, "**", pat), recursive=True))
    # フォールバック: 個別Shapefile
    fallback_patterns = [
        "*A35a*.shp", "*A35b*.shp", "*A35d*.shp", "*A35e*.shp", "*A35f*.shp",
        "*A35*.shp", "*Landscape*.shp", "*景観*.shp",
    ]

    # 統合GeoJSON系がある場合はそれを優先
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)

    if ordered:
        path = ordered[0]
        try:
            gdf = _read_geojson_or_shp(path)
            if gdf is not None and not gdf.empty:
                if gdf.crs is None:
                    gdf.set_crs(epsg=4326, inplace=True)
                elif gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs(epsg=4326)
                gdf.attrs["source_file"] = path
                return gdf
        except Exception as e:
            st.warning(f"景観計画データ（統合版）の読み込みに失敗、個別ファイルにフォールバック: {e}")

    # フォールバック: 個別Shapefileから最大件数のものを選ぶ
    shp_candidates = []
    for pat in fallback_patterns:
        for shp in glob.glob(os.path.join(data_dir, "**", pat), recursive=True):
            try:
                gdf = gpd.read_file(shp, encoding="cp932")
            except Exception:
                try:
                    gdf = gpd.read_file(shp)
                except Exception:
                    continue
            if gdf is None or gdf.empty:
                continue
            try:
                geom_types = gdf.geometry.geom_type.unique()
                if not any(gt in ("Polygon", "MultiPolygon") for gt in geom_types):
                    continue
            except Exception:
                continue
            gdf.attrs["source_file"] = shp
            shp_candidates.append((shp, gdf))

    if not shp_candidates:
        return None
    shp_candidates.sort(key=lambda x: len(x[1]), reverse=True)
    gdf = shp_candidates[0][1]
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


# =============================================================================
# ジオコーディング
# =============================================================================
def geocode_address_gsi(address: str):
    if not address or not isinstance(address, str):
        return None, None
    url = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    try:
        r = requests.get(url, params={"q": address.strip()}, timeout=10)
        r.raise_for_status()
        results = r.json()
        if not results:
            return None, None
        lng, lat = results[0]["geometry"]["coordinates"]
        return float(lat), float(lng)
    except Exception:
        return None, None


# =============================================================================
# 判定関数
# =============================================================================
def lookup_natural_park(lat, lng, park_gdf):
    result = {
        "該当": False, "公園名": "", "公園区分": "",
        "地域種別": "", "都道府県": "", "市町村": "", "詳細": "",
    }
    if park_gdf is None or lat is None or lng is None or not HAS_GIS:
        return result

    try:
        point = Point(lng, lat)
        mask = park_gdf.geometry.contains(point) | park_gdf.geometry.intersects(point)
        hits = park_gdf[mask]
    except Exception:
        return result

    if hits.empty:
        return result

    layer_codes, park_names_obj, park_names_code, park_classes, pref_codes, ctv_names = (
        set(), set(), set(), set(), set(), set()
    )

    for _, row in hits.iterrows():
        lcd, _ = pick_first_value(row, LAYER_CD_COL_CANDIDATES)
        if lcd:
            if lcd in ("21", "22", "23"):
                lcd = lcd.replace("2", "1", 1)
            layer_codes.add(lcd)

        if "OBJ_NAME" in row.index:
            v = row["OBJ_NAME"]
            if pd.notna(v) and str(v).strip() not in ("", "nan", "None"):
                park_names_obj.add(str(v).strip())

        for cand in ["A10_005", "A10_15_005", "naturalParkNameCode"]:
            if cand in row.index:
                v = row[cand]
                if pd.notna(v) and str(v).strip() not in ("", "nan", "None"):
                    park_names_code.add(str(v).strip())
                    break

        class_code, _ = pick_first_value(row, PARK_CLASS_COL_CANDIDATES)
        if class_code:
            park_classes.add(class_code)
        pref_code, _ = pick_first_value(row, PREF_CD_COL_CANDIDATES)
        if pref_code:
            pref_codes.add(pref_code)
        ctv, _ = pick_first_value(row, CTV_NAME_COL_CANDIDATES)
        if ctv:
            ctv_names.add(ctv)

    area_type = determine_area_type_by_layers(layer_codes) or "区分不明"

    if park_names_obj:
        park_name_display = " / ".join(sorted(park_names_obj))
    else:
        park_name_labels = [translate_natural_park_name(c) for c in park_names_code]
        park_name_display = " / ".join(sorted(set([n for n in park_name_labels if n])))

    park_class_labels = [translate_natural_park_class(c) for c in park_classes]
    park_class_display = " / ".join(sorted(set([c for c in park_class_labels if c])))

    pref_labels = [translate_prefecture(c) for c in pref_codes]
    pref_display = " / ".join(sorted(set([p for p in pref_labels if p])))

    ctv_display = " / ".join(sorted(ctv_names))

    if not park_name_display and park_class_display:
        park_name_display = park_class_display

    result["該当"] = True
    result["公園名"] = park_name_display
    result["公園区分"] = park_class_display
    result["地域種別"] = area_type
    result["都道府県"] = pref_display
    result["市町村"] = ctv_display

    layer_labels = [translate_layer_type(c) for c in sorted(layer_codes)]
    result["詳細"] = f"ヒット: {', '.join(layer_labels)}" if layer_labels else ""
    return result


def lookup_landscape(lat, lng, landscape_gdf):
    result = {"該当": False, "行政団体": "", "条例名": "", "策定状況": "",
              "都道府県": "", "詳細": ""}
    if landscape_gdf is None or lat is None or lng is None or not HAS_GIS:
        return result
    try:
        point = Point(lng, lat)
        mask = landscape_gdf.geometry.contains(point) | landscape_gdf.geometry.intersects(point)
        hits = landscape_gdf[mask]
    except Exception:
        return result
    if hits.empty:
        return result

    # 複数ヒット時はすべての団体名を集約
    org_names = set()
    pref_codes = set()
    statuses = set()

    for _, row in hits.iterrows():
        org, _ = pick_first_value(row, LANDSCAPE_ORG_COL_CANDIDATES)
        if org:
            org_names.add(org)
        pref, _ = pick_first_value(row, LANDSCAPE_PREF_COL_CANDIDATES)
        if pref:
            pref_codes.add(pref)
        status, _ = pick_first_value(row, LANDSCAPE_STATUS_COL_CANDIDATES)
        if status:
            statuses.add(status)

    org_display = " / ".join(sorted(org_names)) if org_names else "（団体名不明）"
    pref_labels = [translate_prefecture(c) for c in pref_codes]
    pref_display = " / ".join(sorted(set([p for p in pref_labels if p])))

    # 複数の条例を集約
    ordinances = set()
    for org in org_names:
        ordinance = get_landscape_ordinance(org)
        if ordinance:
            ordinances.add(ordinance)
    ordinance_display = " / ".join(sorted(ordinances))

    # 策定状況: 「策定済み」が含まれていれば優先
    status_display = ""
    if statuses:
        status_labels = [translate_landscape_plan_status(s) for s in statuses]
        status_labels = [s for s in status_labels if s]
        if "景観計画策定済み" in status_labels:
            status_display = "景観計画策定済み"
        elif status_labels:
            status_display = " / ".join(sorted(set(status_labels)))

    result["該当"] = True
    result["行政団体"] = org_display
    result["条例名"] = ordinance_display
    result["策定状況"] = status_display
    result["都道府県"] = pref_display
    if len(hits) > 1:
        result["詳細"] = f"{len(hits)}件の区域に該当"
    return result


# =============================================================================
# サイドバー
# =============================================================================
with st.sidebar:
    st.markdown("### ⚙️ 設定")
    candidates = ["data", "./data", "/mount/src/ev-regulation-tool/data", "../data"]
    data_dir = next((c for c in candidates if os.path.isdir(c)), "data")
    st.markdown(f"**データフォルダ:** `{data_dir}`")

    st.markdown("#### 📊 データ読み込み状況")
    if not HAS_GIS:
        st.error("geopandasが未インストール")
        park_gdf = None
        landscape_gdf = None
    else:
        with st.spinner("自然公園データを読み込み中..."):
            park_gdf = load_natural_park_gdf(data_dir)
        if park_gdf is not None:
            st.success(f"✅ 自然公園: {len(park_gdf):,}件")
            if "source_file" in park_gdf.attrs:
                st.caption(f"📄 {os.path.basename(park_gdf.attrs['source_file'])}")
        else:
            st.warning("⚠️ 自然公園: データ未配置")

        with st.spinner("景観計画データを読み込み中..."):
            landscape_gdf = load_landscape_gdf(data_dir)
        if landscape_gdf is not None:
            st.success(f"✅ 景観計画: {len(landscape_gdf):,}件")
            if "source_file" in landscape_gdf.attrs:
                st.caption(f"📄 {os.path.basename(landscape_gdf.attrs['source_file'])}")
        else:
            st.warning("⚠️ 景観計画: データ未配置")

    st.markdown("---")

    with st.expander("🔍 属性カラム確認（デバッグ）", expanded=False):
        if park_gdf is not None:
            st.markdown("**🏞️ 自然公園データ**")
            st.code(str(list(park_gdf.columns)))
            if "layer_cd" in park_gdf.columns:
                lc = park_gdf["layer_cd"].value_counts().sort_index()
                st.markdown("**レイヤ別件数:**")
                for code, n in lc.items():
                    label = translate_layer_type(code)
                    st.caption(f"  {code} {label}: {n:,}件")
        if landscape_gdf is not None:
            st.markdown("**🎨 景観計画データ**")
            st.code(str(list(landscape_gdf.columns)))
            if "pref_cd" in landscape_gdf.columns:
                pc = landscape_gdf["pref_cd"].value_counts().sort_index()
                st.markdown("**都道府県別件数（上位10）:**")
                for code, n in list(pc.items())[:10]:
                    label = translate_prefecture(code)
                    st.caption(f"  {code} {label}: {n:,}件")

    st.markdown("---")
    st.markdown("#### 🔗 確認URL設定")
    show_eadas = st.checkbox("EADASリンクを含める", value=True)
    show_gsi = st.checkbox("地理院地図リンクを含める", value=True)
    show_gmap = st.checkbox("Googleマップリンクを含める", value=True)
    only_hit = st.checkbox("該当ありの案件のみリンク表示", value=False)

    st.markdown("---")
    geocode_delay = st.slider(
        "API呼び出し間隔（秒）",
        min_value=0.3, max_value=3.0, value=1.0, step=0.1,
    )


# =============================================================================
# メイン
# =============================================================================
st.markdown('<div class="main-header">⚡ EV充電器設置工事 規制区域自動判定ツール</div>', unsafe_allow_html=True)

st.success("🆕 **v3.3**：景観計画データを全国版に更新（A35a全47都道府県）")

st.markdown("""
住所リスト（Excel/CSV）をアップロードすると、各住所が以下の規制区域に該当するかを自動判定します。
- **自然公園区域**（A10-15、全国）
- **景観計画区域**（A35a、全国）
- **確認用URL**：地理院地図/Googleマップ/EADASへのリンク自動生成
""")

st.markdown("### 📂 住所リストのアップロード")
st.caption("📌 ヒント：列名に「緯度」「経度」を含む列があれば自動でその列を使用します")

uploaded_file = st.file_uploader(
    "Excel(.xlsx)またはCSV(.csv)ファイルを選択",
    type=["xlsx", "csv"],
)

df_input = None
if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith(".csv"):
            try:
                df_input = pd.read_csv(uploaded_file, encoding="utf-8")
            except UnicodeDecodeError:
                uploaded_file.seek(0)
                df_input = pd.read_csv(uploaded_file, encoding="cp932")
        else:
            df_input = pd.read_excel(uploaded_file)
        st.success(f"✅ {len(df_input)}件のデータを読み込みました")
        st.dataframe(df_input.head(10), use_container_width=True)
    except Exception as e:
        st.error(f"ファイル読み込みエラー: {e}")


if df_input is not None:
    cols = list(df_input.columns)
    lat_col_candidates = [c for c in cols if any(kw in str(c) for kw in ["緯度", "lat", "Lat", "LAT", "Y座標", "y座標"])]
    lng_col_candidates = [c for c in cols if any(kw in str(c) for kw in ["経度", "lng", "lon", "Lng", "Lon", "LNG", "LON", "X座標", "x座標"])]
    addr_col_candidates = [c for c in cols if any(kw in str(c) for kw in ["住所", "address", "Address", "所在地"])]

    has_latlng = bool(lat_col_candidates) and bool(lng_col_candidates)
    has_addr = bool(addr_col_candidates)

    st.markdown("### 🎯 入力モード")
    if has_latlng and has_addr:
        st.info("🔵 **緯度経度モード** + **住所モード（補助）**")
    elif has_latlng:
        st.info("🔵 **緯度経度モード**")
    elif has_addr:
        st.info("🟢 **住所モード**")
    else:
        st.warning("⚠️ 緯度経度列も住所列も自動認識できませんでした。手動で列を指定してください。")

    col1, col2, col3 = st.columns(3)
    with col1:
        addr_col = st.selectbox(
            "住所列",
            options=["（使わない）"] + cols,
            index=(cols.index(addr_col_candidates[0]) + 1) if addr_col_candidates else 0,
        )
    with col2:
        lat_col = st.selectbox(
            "緯度列",
            options=["（使わない）"] + cols,
            index=(cols.index(lat_col_candidates[0]) + 1) if lat_col_candidates else 0,
        )
    with col3:
        lng_col = st.selectbox(
            "経度列",
            options=["（使わない）"] + cols,
            index=(cols.index(lng_col_candidates[0]) + 1) if lng_col_candidates else 0,
        )

    if st.button("🚀 判定を実行", type="primary"):
        if park_gdf is None and landscape_gdf is None:
            st.error("GISデータが読み込まれていません。")
        elif addr_col == "（使わない）" and (lat_col == "（使わない）" or lng_col == "（使わない）"):
            st.error("住所列、または緯度・経度列の少なくとも一方を指定してください。")
        else:
            results = []
            progress = st.progress(0)
            status = st.empty()

            for i, row in df_input.iterrows():
                lat, lng = None, None
                coord_source = ""
                banchi_warning = ""

                if lat_col != "（使わない）" and lng_col != "（使わない）":
                    lat_val = parse_lat_lng(row.get(lat_col))
                    lng_val = parse_lat_lng(row.get(lng_col))
                    if is_valid_japan_coords(lat_val, lng_val):
                        lat, lng = lat_val, lng_val
                        coord_source = "緯度経度入力"

                addr = ""
                if (lat is None or lng is None) and addr_col != "（使わない）":
                    addr = str(row.get(addr_col, "")).strip() if pd.notna(row.get(addr_col)) else ""
                    if addr:
                        status.text(f"判定中 ({i+1}/{len(df_input)}): {addr[:40]}")
                        if not has_banchi(addr):
                            banchi_warning = "番地未入力"
                        lat, lng = geocode_address_gsi(addr)
                        time.sleep(geocode_delay)
                        coord_source = "住所→ジオコーディング"
                    else:
                        coord_source = "住所空欄"

                if banchi_warning == "" and addr_col != "（使わない）":
                    addr_check = str(row.get(addr_col, "")).strip() if pd.notna(row.get(addr_col)) else ""
                    if addr_check and not has_banchi(addr_check):
                        banchi_warning = "番地未入力"

                if lat is None or lng is None:
                    coord_source = coord_source or "座標取得失敗"

                if i % 5 == 0:
                    status.text(f"判定中 ({i+1}/{len(df_input)})")

                np_result = lookup_natural_park(lat, lng, park_gdf)
                ls_result = lookup_landscape(lat, lng, landscape_gdf)

                result_row = dict(row)
                result_row["緯度"] = lat
                result_row["経度"] = lng
                result_row["座標取得元"] = coord_source

                if banchi_warning:
                    result_row["住所精度"] = "⚠️ 番地未入力"
                else:
                    result_row["住所精度"] = "OK" if coord_source else ""

                result_row["自然公園_該当"] = "該当" if np_result["該当"] else "非該当"
                result_row["自然公園_公園名"] = np_result["公園名"]
                result_row["自然公園_公園区分"] = np_result["公園区分"]
                result_row["自然公園_地域種別"] = np_result["地域種別"]
                result_row["自然公園_都道府県"] = np_result["都道府県"]
                result_row["自然公園_市町村"] = np_result["市町村"]
                result_row["自然公園_備考"] = np_result["詳細"]

                result_row["景観計画_該当"] = "該当" if ls_result["該当"] else "非該当"
                result_row["景観計画_行政団体"] = ls_result["行政団体"]
                result_row["景観計画_条例名"] = ls_result["条例名"]
                result_row["景観計画_策定状況"] = ls_result["策定状況"]
                result_row["景観計画_都道府県"] = ls_result["都道府県"]
                result_row["景観計画_備考"] = ls_result["詳細"]

                if lat is None or lng is None:
                    overall = "判定不可（座標取得失敗）"
                else:
                    if np_result["該当"]:
                        area = np_result["地域種別"]
                        if "特別保護地区" in area:
                            overall = "設置不可（特別保護地区）"
                        elif "特別地域" in area:
                            overall = "要許可（特別地域）"
                        elif "普通地域" in area:
                            overall = "要届出（普通地域）"
                        else:
                            overall = f"要確認（{area}）"
                    elif ls_result["該当"]:
                        overall = "要届出（景観計画区域）"
                    else:
                        overall = "規制区域外"
                    if banchi_warning:
                        overall = f"{overall} ⚠️要確認（番地未入力）"
                result_row["総合判定"] = overall

                is_hit = np_result["該当"] or ls_result["該当"]
                show_link = (not only_hit) or is_hit

                if lat is not None and lng is not None and show_link:
                    if show_gsi:
                        result_row["地理院地図"] = build_gsi_map_url(lat, lng)
                    if show_gmap:
                        result_row["Googleマップ"] = build_google_map_url(lat, lng)
                    if show_eadas:
                        result_row["EADAS"] = build_eadas_url()
                else:
                    if show_gsi:
                        result_row["地理院地図"] = ""
                    if show_gmap:
                        result_row["Googleマップ"] = ""
                    if show_eadas:
                        result_row["EADAS"] = ""

                results.append(result_row)
                progress.progress((i + 1) / len(df_input))

            status.empty()
            progress.empty()
            st.session_state["df_result"] = pd.DataFrame(results)
            st.success("✅ 判定完了")


if "df_result" in st.session_state:
    df_result = st.session_state["df_result"]
    st.markdown("### 📊 判定結果")

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("総件数", len(df_result))
    col2.metric("自然公園該当", int((df_result["自然公園_該当"] == "該当").sum()))
    col3.metric("景観計画該当", int((df_result["景観計画_該当"] == "該当").sum()))
    not_found = int(((df_result["自然公園_該当"] == "非該当") & (df_result["景観計画_該当"] == "非該当")).sum())
    col4.metric("規制区域外", not_found)
    if "住所精度" in df_result.columns:
        warn_count = int((df_result["住所精度"] == "⚠️ 番地未入力").sum())
        col5.metric("番地未入力", warn_count)

    preferred = [
        "総合判定", "住所精度", "座標取得元",
        "自然公園_該当", "自然公園_公園名", "自然公園_地域種別", "自然公園_公園区分",
        "自然公園_都道府県", "自然公園_市町村", "自然公園_備考",
        "景観計画_該当", "景観計画_行政団体", "景観計画_条例名", "景観計画_策定状況",
        "景観計画_都道府県", "景観計画_備考",
        "緯度", "経度",
        "地理院地図", "Googleマップ", "EADAS",
    ]
    show_cols = []
    for c in df_result.columns:
        if c not in preferred and c not in show_cols:
            show_cols.append(c)
    for c in preferred:
        if c in df_result.columns:
            show_cols.append(c)

    df_display = df_result[show_cols].copy()

    column_config = {}
    if "地理院地図" in df_display.columns:
        column_config["地理院地図"] = st.column_config.LinkColumn(
            "地理院地図", display_text="🗺️ 開く"
        )
    if "Googleマップ" in df_display.columns:
        column_config["Googleマップ"] = st.column_config.LinkColumn(
            "Googleマップ", display_text="📍 開く"
        )
    if "EADAS" in df_display.columns:
        column_config["EADAS"] = st.column_config.LinkColumn(
            "EADAS", display_text="🌐 開く"
        )

    st.dataframe(df_display, use_container_width=True, column_config=column_config)

    if HAS_FOLIUM:
        st.markdown("### 🗺️ 地図表示")
        try:
            valid = df_result.dropna(subset=["緯度", "経度"])
            if not valid.empty:
                m = folium.Map(location=[valid["緯度"].mean(), valid["経度"].mean()], zoom_start=6)
                for _, row in valid.iterrows():
                    overall = str(row.get("総合判定", ""))
                    if "不可" in overall:
                        color = "red"
                    elif "要確認" in overall and "番地未入力" in overall:
                        color = "purple"
                    elif "許可" in overall:
                        color = "orange"
                    elif "届出" in overall:
                        color = "blue"
                    else:
                        color = "green"
                    popup_html = f"""
                    <b>総合判定:</b> {overall}<br>
                    <b>公園名:</b> {row.get('自然公園_公園名', '')}<br>
                    <b>地域種別:</b> {row.get('自然公園_地域種別', '')}<br>
                    <b>市町村:</b> {row.get('自然公園_市町村', '')}<br>
                    <b>景観行政団体:</b> {row.get('景観計画_行政団体', '')}<br>
                    """
                    if row.get("地理院地図"):
                        popup_html += f'<a href="{row["地理院地図"]}" target="_blank">🗺️ 地理院地図で開く</a><br>'
                    if row.get("EADAS"):
                        popup_html += f'<a href="{row["EADAS"]}" target="_blank">🌐 EADASで開く</a>'
                    folium.Marker(
                        [row["緯度"], row["経度"]],
                        popup=folium.Popup(popup_html, max_width=350),
                        icon=folium.Icon(color=color, icon="info-sign"),
                    ).add_to(m)
                st_folium(m, width=1200, height=500)
        except Exception as e:
            st.warning(f"地図表示でエラー: {e}")

    st.markdown("### 💾 結果のダウンロード")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_excel = df_result[show_cols].copy()
        for url_col, label in [
            ("地理院地図", "🗺️ 地理院地図"),
            ("Googleマップ", "📍 Googleマップ"),
            ("EADAS", "🌐 EADAS"),
        ]:
            if url_col in df_excel.columns:
                df_excel[url_col] = df_excel[url_col].apply(
                    lambda u: f'=HYPERLINK("{u}","{label}")' if u and isinstance(u, str) and u.startswith("http") else ""
                )
        df_excel.to_excel(writer, sheet_name="判定結果", index=False)

        ws = writer.sheets["判定結果"]
        for col_idx, col_name in enumerate(df_excel.columns, start=1):
            try:
                col_letter = ws.cell(row=1, column=col_idx).column_letter
                if col_name in ("地理院地図", "Googleマップ", "EADAS"):
                    ws.column_dimensions[col_letter].width = 18
                elif "備考" in str(col_name) or "条例" in str(col_name):
                    ws.column_dimensions[col_letter].width = 35
                else:
                    ws.column_dimensions[col_letter].width = 15
            except Exception:
                pass

    buf.seek(0)
    st.download_button(
        "📥 Excelでダウンロード（リンク埋め込み済み）",
        data=buf,
        file_name="ev_regulation_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


st.markdown("---")
st.markdown(
    '<p style="text-align:center; color:#a0aec0; font-size:0.85rem;">'
    'EV充電器設置工事 規制区域自動判定ツール v3.3 | '
    'GISデータ出典: 国土数値情報（国土交通省）A10-15・A35a | '
    'ジオコーディング: 国土地理院'
    '</p>',
    unsafe_allow_html=True,
)
