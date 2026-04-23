# -*- coding: utf-8 -*-
"""
EV充電器設置工事 規制区域自動判定ツール（改良版）

改良内容（前版からの差分）:
1. 自然公園データの判定結果に、公園名と地域種別（普通地域/第1種特別地域/
   第2種特別地域/第3種特別地域/特別保護地区）を日本語で表示
2. 景観計画データの判定結果に、該当する行政（自治体名）と条例名を表示
3. 属性コード（A10_002等）を日本語に自動変換
"""

import os
import io
import glob
import time
import requests
from pathlib import Path

import streamlit as st
import pandas as pd

# GISライブラリ（任意）
try:
    import geopandas as gpd
    from shapely.geometry import Point
    HAS_GIS = True
except ImportError:
    HAS_GIS = False

# 地図（任意）
try:
    import folium
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

# コード変換辞書
from ksj_codes import (
    translate_natural_park_class,
    translate_natural_park_area,
    translate_natural_park_name,
    translate_prefecture,
    translate_development_bureau,
    translate_element_class,
    translate_landscape_plan_status,
    translate_attribute_name,
    get_landscape_ordinance,
    A10_ATTRIBUTE_LABELS,
    A35A_ATTRIBUTE_LABELS,
)


# =============================================================================
# ページ設定
# =============================================================================
st.set_page_config(
    page_title="EV充電器設置工事 規制区域自動判定ツール",
    page_icon="⚡",
    layout="wide",
)

# CSS
st.markdown("""
<style>
.main-header {
    font-size: 1.8rem;
    font-weight: 700;
    color: #1a365d;
    padding: 0.6rem 0;
    border-bottom: 3px solid #3182ce;
    margin-bottom: 1rem;
}
.result-ok { color: #22543d; background: #c6f6d5; padding: 2px 8px; border-radius: 4px; }
.result-ng { color: #742a2a; background: #fed7d7; padding: 2px 8px; border-radius: 4px; }
.result-caution { color: #744210; background: #fefcbf; padding: 2px 8px; border-radius: 4px; }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# GISデータ読み込み（キャッシュ）
# =============================================================================
@st.cache_data(show_spinner=False)
def find_shapefile(data_dir: str, keyword_patterns: list) -> str | None:
    """指定フォルダ内で、パターンにマッチする.shpファイルを探す"""
    if not os.path.isdir(data_dir):
        return None
    for pat in keyword_patterns:
        matches = glob.glob(os.path.join(data_dir, "**", pat), recursive=True)
        if matches:
            return matches[0]
    return None


@st.cache_data(show_spinner=False)
def load_natural_park_gdf(data_dir: str):
    """自然公園地域データ（A10）を読み込む"""
    if not HAS_GIS:
        return None
    patterns = [
        "*A10*NaturalPark*.shp",
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

    # CRS統一（WGS84）
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


@st.cache_data(show_spinner=False)
def load_landscape_gdf(data_dir: str):
    """景観計画区域データ（A35）を読み込む"""
    if not HAS_GIS:
        return None
    patterns = [
        "*A35a*.shp",
        "*A35b*.shp",
        "*A35d*.shp",
        "*A35e*.shp",
        "*A35f*.shp",
        "*A35*.shp",
        "*Landscape*.shp",
        "*景観*.shp",
    ]
    # 面データ優先で順次検索し、最も要素数が多いものを採用する
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
            # ポリゴン（面）のみ対象
            try:
                geom_types = gdf.geometry.geom_type.unique()
                if not any(gt in ("Polygon", "MultiPolygon") for gt in geom_types):
                    continue
            except Exception:
                continue
            candidates.append((shp, gdf))

    if not candidates:
        return None

    # 件数が最も多いものを採用
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    gdf = candidates[0][1]

    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


# =============================================================================
# ジオコーディング（国土地理院API）
# =============================================================================
def geocode_address_gsi(address: str) -> tuple[float | None, float | None]:
    """国土地理院 住所検索APIで緯度経度を取得"""
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
# 判定関数（改良版）
# =============================================================================
def lookup_natural_park(lat: float, lng: float, park_gdf) -> dict:
    """自然公園地域判定。該当する場合は公園名・地種区分を日本語で返す"""
    result = {
        "該当": False,
        "公園名": "",
        "公園区分": "",
        "地域種別": "",
        "詳細": "",
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

    # 複数ヒット時は最も厳しい地種区分（特別保護地区＞第1種＞...）を採用
    # 地種区分の厳しさ順位: 1=特別保護 > 2=第1種 > 3=第2種 > 4=第3種 > 8=特別地域 > 6=海域公園 > 5=普通地域 > 7=その他
    severity = {"1": 1, "2": 2, "3": 3, "4": 4, "8": 5, "6": 6, "5": 7, "7": 8}

    def get_area_code_col(row):
        # カラム名候補
        for col in ["A10_004", "naturalParkCode", "naturalParkCode2"]:
            if col in hits.columns:
                v = row.get(col)
                if v is not None and str(v).strip() not in ("", "nan", "None"):
                    return str(v).strip()
        return ""

    def get_name_code_col(row):
        for col in ["A10_005", "naturalParkNameCode"]:
            if col in hits.columns:
                v = row.get(col)
                if v is not None and str(v).strip() not in ("", "nan", "None"):
                    return str(v).strip()
        return ""

    def get_class_code_col(row):
        for col in ["A10_003", "naturalParkClassCode"]:
            if col in hits.columns:
                v = row.get(col)
                if v is not None and str(v).strip() not in ("", "nan", "None"):
                    return str(v).strip()
        return ""

    # 最も厳しい地種区分を持つ行を抽出
    best_idx = None
    best_severity = 999
    for idx, row in hits.iterrows():
        area_code = get_area_code_col(row)
        s = severity.get(area_code, 99)
        if s < best_severity:
            best_severity = s
            best_idx = idx

    if best_idx is None:
        best_idx = hits.index[0]

    row = hits.loc[best_idx]
    name_code = get_name_code_col(row)
    class_code = get_class_code_col(row)
    area_code = get_area_code_col(row)

    park_name = translate_natural_park_name(name_code)
    park_class = translate_natural_park_class(class_code)
    area_name = translate_natural_park_area(area_code)

    # 公園名が空なら公園区分だけ表示
    if not park_name and park_class:
        park_name = park_class
    # 地域種別が空ならデフォルト
    if not area_name:
        area_name = "区分不明"

    result["該当"] = True
    result["公園名"] = park_name
    result["公園区分"] = park_class
    result["地域種別"] = area_name
    # 複数ヒット数の注記
    if len(hits) > 1:
        result["詳細"] = f"{len(hits)}件のポリゴンに該当（最も厳しい区分を表示）"
    else:
        result["詳細"] = ""
    return result


def lookup_landscape(lat: float, lng: float, landscape_gdf) -> dict:
    """景観計画区域判定。該当する場合は行政団体名・条例名を返す"""
    result = {
        "該当": False,
        "行政団体": "",
        "条例名": "",
        "策定状況": "",
        "詳細": "",
    }
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

    # 先頭行を採用
    row = hits.iloc[0]

    # 団体名を取得（A35a_003 / A35b_003 / A35d_003 等）
    org_name = ""
    for col in ["A35a_003", "A35b_003", "A35c_003", "A35d_003", "A35e_003", "A35f_003"]:
        if col in hits.columns:
            v = row.get(col)
            if v is not None and str(v).strip() not in ("", "nan", "None"):
                org_name = str(v).strip()
                break

    # 策定状況（A35x_007）
    status_code = ""
    for col in ["A35a_007", "A35b_007", "A35d_007", "A35e_007", "A35f_007"]:
        if col in hits.columns:
            v = row.get(col)
            if v is not None and str(v).strip() not in ("", "nan", "None"):
                status_code = str(v).strip()
                break

    result["該当"] = True
    result["行政団体"] = org_name if org_name else "（団体名不明）"
    result["条例名"] = get_landscape_ordinance(org_name)
    result["策定状況"] = translate_landscape_plan_status(status_code) if status_code else ""
    if len(hits) > 1:
        result["詳細"] = f"{len(hits)}件の区域に該当"
    return result


# =============================================================================
# サイドバー
# =============================================================================
with st.sidebar:
    st.markdown("### ⚙️ 設定")

    # dataフォルダの自動検出（ローカル/クラウド両対応）
    candidates = ["data", "./data", "/mount/src/ev-regulation-tool/data", "../data"]
    data_dir = None
    for c in candidates:
        if os.path.isdir(c):
            data_dir = c
            break
    if data_dir is None:
        data_dir = "data"

    st.markdown(f"**データフォルダ:** `{data_dir}`")

    # データ読み込み状況
    st.markdown("#### 📊 データ読み込み状況")
    if not HAS_GIS:
        st.error("geopandasが未インストール")
        park_gdf = None
        landscape_gdf = None
    else:
        with st.spinner("自然公園データを読み込み中..."):
            park_gdf = load_natural_park_gdf(data_dir)
        if park_gdf is not None:
            st.success(f"✅ 自然公園: {len(park_gdf)}件のポリゴン")
        else:
            st.warning("⚠️ 自然公園: データ未配置")

        with st.spinner("景観計画データを読み込み中..."):
            landscape_gdf = load_landscape_gdf(data_dir)
        if landscape_gdf is not None:
            st.success(f"✅ 景観計画区域: {len(landscape_gdf)}件のポリゴン")
        else:
            st.warning("⚠️ 景観計画区域: データ未配置")

    st.markdown("---")
    geocode_delay = st.slider(
        "API呼び出し間隔（秒）",
        min_value=0.3, max_value=3.0, value=1.0, step=0.1,
        help="国土地理院APIへの負荷軽減のため"
    )

    st.markdown("---")
    with st.expander("📋 属性コード早見表"):
        st.markdown("**自然公園地域（A10）**")
        st.code("\n".join([f"{k}: {v}" for k, v in A10_ATTRIBUTE_LABELS.items() if k.startswith("A10")]))
        st.markdown("**景観計画区域（A35a）**")
        st.code("\n".join([f"{k}: {v}" for k, v in A35A_ATTRIBUTE_LABELS.items() if k.startswith("A35a")]))


# =============================================================================
# メインコンテンツ
# =============================================================================
st.markdown('<div class="main-header">⚡ EV充電器設置工事 規制区域自動判定ツール</div>', unsafe_allow_html=True)

st.markdown("""
住所リスト（Excel/CSV）をアップロードすると、各住所が以下の規制区域に該当するかを自動判定します。
- **自然公園区域**（国立公園・国定公園・都道府県立自然公園）→ 公園名・地種区分を日本語表示
- **景観計画区域** → 該当する行政団体・条例名を表示
""")

# ファイルアップロード
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


# 判定実行
if df_input is not None:
    # 住所列の選択
    address_cols = [c for c in df_input.columns if "住所" in str(c) or "address" in str(c).lower()]
    default_idx = df_input.columns.get_loc(address_cols[0]) if address_cols else 0
    addr_col = st.selectbox(
        "住所列を選択してください",
        options=list(df_input.columns),
        index=default_idx,
    )

    if st.button("🚀 判定を実行", type="primary"):
        if park_gdf is None and landscape_gdf is None:
            st.error("GISデータが読み込まれていません。data/ フォルダにShapefileを配置してください。")
        else:
            results = []
            progress = st.progress(0)
            status = st.empty()

            for i, row in df_input.iterrows():
                addr = str(row[addr_col]) if pd.notna(row[addr_col]) else ""
                status.text(f"判定中 ({i+1}/{len(df_input)}): {addr[:40]}")

                # ジオコーディング
                lat, lng = geocode_address_gsi(addr)
                time.sleep(geocode_delay)

                # 自然公園判定
                np_result = lookup_natural_park(lat, lng, park_gdf)
                # 景観計画判定
                ls_result = lookup_landscape(lat, lng, landscape_gdf)

                result_row = dict(row)
                result_row["緯度"] = lat
                result_row["経度"] = lng

                # 自然公園 - 日本語表示
                result_row["自然公園_該当"] = "該当" if np_result["該当"] else "非該当"
                result_row["自然公園_公園名"] = np_result["公園名"]
                result_row["自然公園_公園区分"] = np_result["公園区分"]
                result_row["自然公園_地域種別"] = np_result["地域種別"]
                if np_result["詳細"]:
                    result_row["自然公園_備考"] = np_result["詳細"]
                else:
                    result_row["自然公園_備考"] = ""

                # 景観計画 - 日本語表示
                result_row["景観計画_該当"] = "該当" if ls_result["該当"] else "非該当"
                result_row["景観計画_行政団体"] = ls_result["行政団体"]
                result_row["景観計画_条例名"] = ls_result["条例名"]
                result_row["景観計画_策定状況"] = ls_result["策定状況"]
                if ls_result["詳細"]:
                    result_row["景観計画_備考"] = ls_result["詳細"]
                else:
                    result_row["景観計画_備考"] = ""

                # 総合判定
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

            df_result = pd.DataFrame(results)
            st.session_state["df_result"] = df_result
            st.success("✅ 判定完了")


# =============================================================================
# 結果表示
# =============================================================================
if "df_result" in st.session_state:
    df_result = st.session_state["df_result"]

    st.markdown("### 📊 判定結果")

    # サマリー
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("総件数", len(df_result))
    col2.metric("自然公園該当", int((df_result["自然公園_該当"] == "該当").sum()))
    col3.metric("景観計画該当", int((df_result["景観計画_該当"] == "該当").sum()))
    not_found = int(((df_result["自然公園_該当"] == "非該当") & (df_result["景観計画_該当"] == "非該当")).sum())
    col4.metric("規制区域外", not_found)

    # 表示順序を整理
    preferred_cols = [
        "総合判定",
        "自然公園_該当", "自然公園_公園名", "自然公園_地域種別", "自然公園_公園区分", "自然公園_備考",
        "景観計画_該当", "景観計画_行政団体", "景観計画_条例名", "景観計画_策定状況", "景観計画_備考",
        "緯度", "経度",
    ]
    show_cols = []
    # 元の入力列を先頭に
    for c in df_result.columns:
        if c not in preferred_cols and c not in show_cols:
            show_cols.append(c)
    # 次に結果列
    for c in preferred_cols:
        if c in df_result.columns:
            show_cols.append(c)

    st.dataframe(df_result[show_cols], use_container_width=True)

    # 地図表示
    if HAS_FOLIUM:
        st.markdown("### 🗺️ 地図表示")
        try:
            valid = df_result.dropna(subset=["緯度", "経度"])
            if not valid.empty:
                center_lat = valid["緯度"].mean()
                center_lng = valid["経度"].mean()
                m = folium.Map(location=[center_lat, center_lng], zoom_start=6)

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
                        location=[row["緯度"], row["経度"]],
                        popup=folium.Popup(popup_html, max_width=350),
                        icon=folium.Icon(color=color, icon="info-sign"),
                    ).add_to(m)

                st_folium(m, width=1200, height=500)
        except Exception as e:
            st.warning(f"地図表示でエラー: {e}")

    # ダウンロード
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


# フッター
st.markdown("---")
st.markdown(
    '<p style="text-align:center; color:#a0aec0; font-size:0.85rem;">'
    'EV充電器設置工事 規制区域自動判定ツール v2.0 | '
    'GISデータ出典: 国土数値情報（国土交通省）| '
    'ジオコーディング: 国土地理院'
    '</p>',
    unsafe_allow_html=True,
)
