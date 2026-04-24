# -*- coding: utf-8 -*-
"""
EV充電器設置工事 規制区域自動判定ツール（v2.1 デバッグ機能付き）

v2.0からの変更点:
- サイドバーに「属性カラム確認」パネルを追加
  → 読み込んだShapefileの実際のカラム名と値サンプルを表示
- カラム名の探索候補を大幅拡充（A10-10_003 等のハイフン入り形式にも対応）
- 判定結果に生コード情報を含めるオプションを追加
"""

import os
import io
import glob
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
    translate_natural_park_area,
    translate_natural_park_name,
    translate_landscape_plan_status,
    get_landscape_ordinance,
    A10_ATTRIBUTE_LABELS,
    A35A_ATTRIBUTE_LABELS,
)


# =============================================================================
# カラム名候補リスト（属性バリエーション吸収）
# =============================================================================
PARK_CLASS_COL_CANDIDATES = [
    "A10_003", "A10-10_003", "A10_15_003", "A10-15_003",
    "naturalParkClassCode", "NaturalParkClassCode",
    "parkClass", "park_class", "kubun",
]
PARK_AREA_COL_CANDIDATES = [
    "A10_004", "A10-10_004", "A10_15_004", "A10-15_004",
    "naturalParkCode", "naturalParkCode2", "NaturalParkCode",
    "parkArea", "park_area", "chishu",
]
PARK_NAME_COL_CANDIDATES = [
    "A10_005", "A10-10_005", "A10_15_005", "A10-15_005",
    "naturalParkNameCode", "NaturalParkNameCode",
    "parkName", "park_name", "name_code",
]

LANDSCAPE_ORG_COL_CANDIDATES = [
    "A35a_003", "A35b_003", "A35c_003",
    "A35d_003", "A35e_003", "A35f_003",
    "A35a-14_003", "A35b-14_003", "A35d-14_003", "A35e-14_003", "A35f-14_003",
    "organizationName", "orgName", "dantai",
]
LANDSCAPE_STATUS_COL_CANDIDATES = [
    "A35a_007", "A35b_007", "A35d_007", "A35e_007", "A35f_007",
    "A35a-14_007", "A35b-14_007",
    "planStatus",
]


def pick_first_value(row, candidates):
    """rowから候補カラム名のうち最初に見つかった非空値を返す"""
    for col in candidates:
        if col in row.index:
            v = row[col]
            if pd.notna(v) and str(v).strip() not in ("", "nan", "None"):
                return str(v).strip(), col
    return "", None


def pick_first_col(df_columns, candidates):
    """df.columnsのうち候補リストで最初にマッチするカラム名を返す"""
    for col in candidates:
        if col in df_columns:
            return col
    return None


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
# GISデータ読み込み
# =============================================================================
@st.cache_data(show_spinner=False)
def find_shapefile(data_dir: str, keyword_patterns: list):
    if not os.path.isdir(data_dir):
        return None
    for pat in keyword_patterns:
        matches = glob.glob(os.path.join(data_dir, "**", pat), recursive=True)
        if matches:
            return matches[0]
    return None


@st.cache_data(show_spinner=False)
def load_natural_park_gdf(data_dir: str):
    if not HAS_GIS:
        return None
    patterns = [
        "*A10*NaturalPark*.shp",
        "*A10-10*.shp",
        "*A10-15*.shp",
        "*A10*.shp",
        "*NaturalPark*.shp",
        "*自然公園*.shp",
    ]
    shp = find_shapefile(data_dir, patterns)
    if shp is None:
        return None
    try:
        gdf = gpd.read_file(shp, encoding="cp932")
    except Exception:
        try:
            gdf = gpd.read_file(shp)
        except Exception as e:
            st.error(f"自然公園データ読み込みエラー: {e}")
            return None
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    gdf.attrs["source_file"] = shp
    return gdf


@st.cache_data(show_spinner=False)
def load_landscape_gdf(data_dir: str):
    if not HAS_GIS:
        return None
    patterns = [
        "*A35a*.shp", "*A35b*.shp",
        "*A35d*.shp", "*A35e*.shp", "*A35f*.shp",
        "*A35*.shp",
        "*Landscape*.shp", "*景観*.shp",
    ]
    if not os.path.isdir(data_dir):
        return None
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
    return candidates[0][1]


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
        "地域種別": "", "詳細": "", "生コード": "",
    }
    if park_gdf is None or lat is None or lng is None:
        return result
    if not HAS_GIS:
        return result

    try:
        point = Point(lng, lat)
        mask = park_gdf.geometry.contains(point) | park_gdf.geometry.intersects(point)
        hits = park_gdf[mask]
    except Exception:
        return result

    if hits.empty:
        return result

    severity = {"1": 1, "2": 2, "3": 3, "4": 4, "8": 5, "6": 6, "5": 7, "7": 8}

    best_idx = None
    best_severity = 999
    for idx, row in hits.iterrows():
        area_code, _ = pick_first_value(row, PARK_AREA_COL_CANDIDATES)
        s = severity.get(area_code, 99)
        if s < best_severity:
            best_severity = s
            best_idx = idx

    if best_idx is None:
        best_idx = hits.index[0]

    row = hits.loc[best_idx]
    name_code, name_col = pick_first_value(row, PARK_NAME_COL_CANDIDATES)
    class_code, class_col = pick_first_value(row, PARK_CLASS_COL_CANDIDATES)
    area_code, area_col = pick_first_value(row, PARK_AREA_COL_CANDIDATES)

    park_name = translate_natural_park_name(name_code)
    park_class = translate_natural_park_class(class_code)
    area_name = translate_natural_park_area(area_code)

    if not park_name and park_class:
        park_name = park_class
    if not area_name:
        area_name = "区分不明"

    result["該当"] = True
    result["公園名"] = park_name
    result["公園区分"] = park_class
    result["地域種別"] = area_name
    result["生コード"] = f"区分={class_code}({class_col}), 地種={area_code}({area_col}), 公園={name_code}({name_col})"
    if len(hits) > 1:
        result["詳細"] = f"{len(hits)}件のポリゴンに該当（最厳区分を表示）"
    return result


def lookup_landscape(lat, lng, landscape_gdf):
    result = {"該当": False, "行政団体": "", "条例名": "", "策定状況": "", "詳細": ""}
    if landscape_gdf is None or lat is None or lng is None:
        return result
    if not HAS_GIS:
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
    data_dir = None
    for c in candidates:
        if os.path.isdir(c):
            data_dir = c
            break
    if data_dir is None:
        data_dir = "data"
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
            st.success(f"✅ 自然公園: {len(park_gdf)}件")
            if "source_file" in park_gdf.attrs:
                st.caption(f"📄 {os.path.basename(park_gdf.attrs['source_file'])}")
        else:
            st.warning("⚠️ 自然公園: データ未配置")

        with st.spinner("景観計画データを読み込み中..."):
            landscape_gdf = load_landscape_gdf(data_dir)
        if landscape_gdf is not None:
            st.success(f"✅ 景観計画: {len(landscape_gdf)}件")
            if "source_file" in landscape_gdf.attrs:
                st.caption(f"📄 {os.path.basename(landscape_gdf.attrs['source_file'])}")
        else:
            st.warning("⚠️ 景観計画: データ未配置")

    st.markdown("---")

    # 🔍 デバッグパネル
    with st.expander("🔍 属性カラム確認（デバッグ）", expanded=True):
        st.caption("「区分不明」が出る原因を特定するために、Shapefileの実際のカラム名と値サンプルを確認できます。")

        if park_gdf is not None:
            st.markdown("**🏞️ 自然公園データの属性**")
            st.write(f"カラム一覧:")
            st.code(str(list(park_gdf.columns)))
            sample = park_gdf.drop(columns=["geometry"], errors="ignore").head(3)
            st.dataframe(sample, use_container_width=True)

            st.markdown("**認識状況:**")
            for label, cands in [
                ("自然公園区分", PARK_CLASS_COL_CANDIDATES),
                ("地域区分（地種区分）", PARK_AREA_COL_CANDIDATES),
                ("自然公園名称", PARK_NAME_COL_CANDIDATES),
            ]:
                found = pick_first_col(park_gdf.columns, cands)
                if found:
                    uniq = park_gdf[found].dropna().astype(str).str.strip()
                    uniq = uniq[uniq != ""].unique()
                    uniq_preview = ", ".join(list(uniq)[:15])
                    st.success(f"{label}: `{found}` → 値: {uniq_preview}")
                else:
                    st.error(f"{label}: 該当カラムなし")

        if landscape_gdf is not None:
            st.markdown("**🎨 景観計画データの属性**")
            st.write(f"カラム一覧:")
            st.code(str(list(landscape_gdf.columns)))
            sample = landscape_gdf.drop(columns=["geometry"], errors="ignore").head(3)
            st.dataframe(sample, use_container_width=True)

            st.markdown("**認識状況:**")
            for label, cands in [
                ("団体名", LANDSCAPE_ORG_COL_CANDIDATES),
                ("策定状況", LANDSCAPE_STATUS_COL_CANDIDATES),
            ]:
                found = pick_first_col(landscape_gdf.columns, cands)
                if found:
                    uniq = landscape_gdf[found].dropna().astype(str).str.strip()
                    uniq = uniq[uniq != ""].unique()
                    uniq_preview = ", ".join(list(uniq)[:5])
                    st.success(f"{label}: `{found}` → 値例: {uniq_preview}")
                else:
                    st.error(f"{label}: 該当カラムなし")

    st.markdown("---")
    geocode_delay = st.slider(
        "API呼び出し間隔（秒）",
        min_value=0.3, max_value=3.0, value=1.0, step=0.1,
    )


# =============================================================================
# メイン
# =============================================================================
st.markdown('<div class="main-header">⚡ EV充電器設置工事 規制区域自動判定ツール</div>', unsafe_allow_html=True)

st.info("🔍 **デバッグ版**：「区分不明」が多く出る場合は、サイドバーの「🔍 属性カラム確認」を展開して、Shapefileの実際のカラム名を確認し、その結果をお知らせください。")

st.markdown("""
住所リスト（Excel/CSV）をアップロードすると、各住所が以下の規制区域に該当するかを自動判定します。
- **自然公園区域**（国立公園・国定公園・都道府県立自然公園）
- **景観計画区域**（環境色対応が必要な地域）
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

    show_raw_code = st.checkbox("判定結果に生コード情報を含める（デバッグ用）", value=False)

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
                result_row["自然公園_備考"] = np_result["詳細"]
                if show_raw_code:
                    result_row["自然公園_生コード"] = np_result["生コード"]

                result_row["景観計画_該当"] = "該当" if ls_result["該当"] else "非該当"
                result_row["景観計画_行政団体"] = ls_result["行政団体"]
                result_row["景観計画_条例名"] = ls_result["条例名"]
                result_row["景観計画_策定状況"] = ls_result["策定状況"]
                result_row["景観計画_備考"] = ls_result["詳細"]

                if np_result["該当"]:
                    if "特別保護地区" in np_result["地域種別"]:
                        overall = "設置不可（特別保護地区）"
                    elif "第1種特別地域" in np_result["地域種別"]:
                        overall = "要許可（第1種特別地域）"
                    elif "特別地域" in np_result["地域種別"]:
                        overall = "要許可（特別地域）"
                    elif "普通地域" in np_result["地域種別"]:
                        overall = "要届出（普通地域）"
                    else:
                        overall = f"要確認（{np_result['地域種別']}）"
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
        "自然公園_該当", "自然公園_公園名", "自然公園_地域種別", "自然公園_公園区分", "自然公園_備考",
        "自然公園_生コード",
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
    'EV充電器設置工事 規制区域自動判定ツール v2.1 (Debug) | '
    'GISデータ出典: 国土数値情報（国土交通省）| '
    'ジオコーディング: 国土地理院'
    '</p>',
    unsafe_allow_html=True,
)
