# -*- coding: utf-8 -*-
"""
EV充電器設置工事 規制区域自動判定ツール v3.0

v3.0の変更点:
- A10-15（平成27年版）全国統合データに対応
- gzip圧縮されたGeoJSONを直接読み込み
- レイヤ重ね合わせ方式による地種区分判定
  - 11: 自然公園地域（全体）
  - 12: 特別地域
  - 13: 特別保護地区
"""

import os
import io
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


# カラム候補
PARK_CLASS_COL_CANDIDATES = [
    "A10_003", "A10-10_003", "A10_15_003", "A10-15_003",
    "naturalParkClassCode",
]
PARK_NAME_COL_CANDIDATES = [
    "A10_005", "A10-10_005", "A10_15_005", "A10-15_005",
    "naturalParkNameCode",
]
LAYER_CD_COL_CANDIDATES = ["layer_cd"]
PREF_CD_COL_CANDIDATES = ["pref_cd", "A10_001", "A10_15_001"]

LANDSCAPE_ORG_COL_CANDIDATES = [
    "A35a_003", "A35b_003", "A35c_003",
    "A35d_003", "A35e_003", "A35f_003",
]
LANDSCAPE_STATUS_COL_CANDIDATES = [
    "A35a_007", "A35b_007", "A35d_007", "A35e_007", "A35f_007",
]


def pick_first_value(row, candidates):
    for col in candidates:
        if col in row.index:
            v = row[col]
            if pd.notna(v) and str(v).strip() not in ("", "nan", "None"):
                return str(v).strip(), col
    return "", None


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


@st.cache_data(show_spinner=False)
def load_natural_park_gdf(data_dir: str):
    """A10-15 全国統合GeoJSON（gzipまたは非圧縮）を読み込む"""
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

    # 重複除去（順序保持）
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    path = ordered[0]

    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                data_bytes = f.read()
            gdf = gpd.read_file(io.BytesIO(data_bytes))
        else:
            try:
                gdf = gpd.read_file(path, encoding="cp932")
            except Exception:
                gdf = gpd.read_file(path)
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
    if not HAS_GIS or not os.path.isdir(data_dir):
        return None
    patterns = [
        "*A35a*.shp", "*A35b*.shp", "*A35d*.shp", "*A35e*.shp", "*A35f*.shp",
        "*A35*.shp", "*Landscape*.shp", "*景観*.shp",
    ]
    candidates = []
    for pat in patterns:
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
            candidates.append((shp, gdf))
    if not candidates:
        return None
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    gdf = candidates[0][1]
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


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


def lookup_natural_park(lat, lng, park_gdf):
    result = {
        "該当": False, "公園名": "", "公園区分": "",
        "地域種別": "", "都道府県": "", "詳細": "",
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

    layer_codes, park_names, park_classes, pref_codes = set(), set(), set(), set()

    for _, row in hits.iterrows():
        lcd, _ = pick_first_value(row, LAYER_CD_COL_CANDIDATES)
        if lcd:
            layer_codes.add(lcd)
        name_code, _ = pick_first_value(row, PARK_NAME_COL_CANDIDATES)
        if name_code:
            park_names.add(name_code)
        class_code, _ = pick_first_value(row, PARK_CLASS_COL_CANDIDATES)
        if class_code:
            park_classes.add(class_code)
        pref_code, _ = pick_first_value(row, PREF_CD_COL_CANDIDATES)
        if pref_code:
            pref_codes.add(pref_code)

    area_type = determine_area_type_by_layers(layer_codes) or "区分不明"

    park_name_labels = [translate_natural_park_name(c) for c in park_names]
    park_name_display = " / ".join(sorted(set([n for n in park_name_labels if n])))

    park_class_labels = [translate_natural_park_class(c) for c in park_classes]
    park_class_display = " / ".join(sorted(set([c for c in park_class_labels if c])))

    pref_labels = [translate_prefecture(c) for c in pref_codes]
    pref_display = " / ".join(sorted(set([p for p in pref_labels if p])))

    if not park_name_display and park_class_display:
        park_name_display = park_class_display

    result["該当"] = True
    result["公園名"] = park_name_display
    result["公園区分"] = park_class_display
    result["地域種別"] = area_type
    result["都道府県"] = pref_display

    layer_labels = [translate_layer_type(c) for c in sorted(layer_codes)]
    result["詳細"] = f"ヒット: {', '.join(layer_labels)}" if layer_labels else ""
    return result


def lookup_landscape(lat, lng, landscape_gdf):
    result = {"該当": False, "行政団体": "", "条例名": "", "策定状況": "", "詳細": ""}
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

    row = hits.iloc[0]
    org_name, _ = pick_first_value(row, LANDSCAPE_ORG_COL_CANDIDATES)
    status_code, _ = pick_first_value(row, LANDSCAPE_STATUS_COL_CANDIDATES)

    result["該当"] = True
    result["行政団体"] = org_name if org_name else "（団体名不明）"
    result["条例名"] = get_landscape_ordinance(org_name) if org_name else ""
    result["策定状況"] = translate_landscape_plan_status(status_code) if status_code else ""
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

    st.markdown("---")
    geocode_delay = st.slider(
        "API呼び出し間隔（秒）",
        min_value=0.3, max_value=3.0, value=1.0, step=0.1,
    )


st.markdown('<div class="main-header">⚡ EV充電器設置工事 規制区域自動判定ツール</div>', unsafe_allow_html=True)

st.success("🆕 **v3.0**：A10-15（平成27年版）全国データに更新。レイヤ重ね合わせ方式で地種区分を正確に判定します。")

st.markdown("""
住所リスト（Excel/CSV）をアップロードすると、各住所が以下の規制区域に該当するかを自動判定します。
- **自然公園区域**（国立公園・国定公園）
- **景観計画区域**
""")

st.markdown("### 📂 住所リストのアップロード")
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
    address_cols = [c for c in df_input.columns if "住所" in str(c) or "address" in str(c).lower()]
    default_idx = df_input.columns.get_loc(address_cols[0]) if address_cols else 0
    addr_col = st.selectbox(
        "住所列を選択してください",
        options=list(df_input.columns),
        index=default_idx,
    )

    if st.button("🚀 判定を実行", type="primary"):
        if park_gdf is None and landscape_gdf is None:
            st.error("GISデータが読み込まれていません。")
        else:
            results = []
            progress = st.progress(0)
            status = st.empty()

            for i, row in df_input.iterrows():
                addr = str(row[addr_col]) if pd.notna(row[addr_col]) else ""
                status.text(f"判定中 ({i+1}/{len(df_input)}): {addr[:40]}")

                lat, lng = geocode_address_gsi(addr)
                time.sleep(geocode_delay)

                np_result = lookup_natural_park(lat, lng, park_gdf)
                ls_result = lookup_landscape(lat, lng, landscape_gdf)

                result_row = dict(row)
                result_row["緯度"] = lat
                result_row["経度"] = lng

                result_row["自然公園_該当"] = "該当" if np_result["該当"] else "非該当"
                result_row["自然公園_公園名"] = np_result["公園名"]
                result_row["自然公園_公園区分"] = np_result["公園区分"]
                result_row["自然公園_地域種別"] = np_result["地域種別"]
                result_row["自然公園_都道府県"] = np_result["都道府県"]
                result_row["自然公園_備考"] = np_result["詳細"]

                result_row["景観計画_該当"] = "該当" if ls_result["該当"] else "非該当"
                result_row["景観計画_行政団体"] = ls_result["行政団体"]
                result_row["景観計画_条例名"] = ls_result["条例名"]
                result_row["景観計画_策定状況"] = ls_result["策定状況"]
                result_row["景観計画_備考"] = ls_result["詳細"]

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
                result_row["総合判定"] = overall

                results.append(result_row)
                progress.progress((i + 1) / len(df_input))

            status.empty()
            progress.empty()
            st.session_state["df_result"] = pd.DataFrame(results)
            st.success("✅ 判定完了")


if "df_result" in st.session_state:
    df_result = st.session_state["df_result"]
    st.markdown("### 📊 判定結果")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("総件数", len(df_result))
    col2.metric("自然公園該当", int((df_result["自然公園_該当"] == "該当").sum()))
    col3.metric("景観計画該当", int((df_result["景観計画_該当"] == "該当").sum()))
    not_found = int(((df_result["自然公園_該当"] == "非該当") & (df_result["景観計画_該当"] == "非該当")).sum())
    col4.metric("規制区域外", not_found)

    preferred = [
        "総合判定",
        "自然公園_該当", "自然公園_公園名", "自然公園_地域種別", "自然公園_公園区分",
        "自然公園_都道府県", "自然公園_備考",
        "景観計画_該当", "景観計画_行政団体", "景観計画_条例名", "景観計画_策定状況", "景観計画_備考",
        "緯度", "経度",
    ]
    show_cols = []
    for c in df_result.columns:
        if c not in preferred and c not in show_cols:
            show_cols.append(c)
    for c in preferred:
        if c in df_result.columns:
            show_cols.append(c)

    st.dataframe(df_result[show_cols], use_container_width=True)

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
                    <b>景観行政団体:</b> {row.get('景観計画_行政団体', '')}<br>
                    <b>条例:</b> {row.get('景観計画_条例名', '')}
                    """
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
        df_result[show_cols].to_excel(writer, sheet_name="判定結果", index=False)
    buf.seek(0)
    st.download_button(
        "📥 Excelでダウンロード",
        data=buf,
        file_name="ev_regulation_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


st.markdown("---")
st.markdown(
    '<p style="text-align:center; color:#a0aec0; font-size:0.85rem;">'
    'EV充電器設置工事 規制区域自動判定ツール v3.0 | '
    'GISデータ出典: 国土数値情報（国土交通省）A10-15・A35 | '
    'ジオコーディング: 国土地理院'
    '</p>',
    unsafe_allow_html=True,
)
