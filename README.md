# ⚡ EV充電器設置工事 規制区域自動判定ツール

## 概要

EV充電器の設置工事にあたって必要な行政申請（自然公園法、景観条例など）の要否を、住所リストから自動判定するWebアプリです。

**できること:**
- Excel/CSVの住所リストをアップロードするだけで判定
- 自然公園区域（国立公園・国定公園・都道府県立自然公園）の該当判定
- 景観計画区域（環境色対応地域）の該当判定
- 判定結果を地図上に可視化
- 結果をExcelファイルでダウンロード

---

## クイックスタート（最短5分）

### 1. Pythonのインストール
https://www.python.org/downloads/ からPython 3.10以上をインストール

> ⚠️ **Windows**: インストール時に「Add Python to PATH」に必ずチェック

### 2. ライブラリのインストール
```bash
pip install streamlit pandas openpyxl requests geopandas shapely folium streamlit-folium
```

### 3. アプリの起動
```bash
streamlit run app.py
```

ブラウザが自動で開きます。開かない場合は http://localhost:8501 にアクセス。

---

## GISデータの準備

### 自然公園地域データ（推奨）
1. [国土数値情報 自然公園地域データ](https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A10-v3_1.html) にアクセス
2. 必要な都道府県のデータをダウンロード（Shapefile形式）
3. ZIPを解凍し、`data/` フォルダに配置

### 景観計画区域データ（推奨）
1. [国土数値情報](https://nlftp.mlit.go.jp/ksj/) で「景観計画区域」を検索
2. ダウンロードして `data/` フォルダに配置

> 💡 GISデータがなくても、住所→緯度経度変換（ジオコーディング）だけで動作します。

---

## フォルダ構成

```
ev-regulation-tool/
├── app.py                  ← メインアプリ
├── README.md               ← このファイル
├── SETUP_GUIDE.py          ← 詳細セットアップガイド
├── sample_input.xlsx       ← テスト用サンプルデータ
└── data/                   ← GISデータ格納先
    ├── A10-xx_xxxx.shp     ← 自然公園データ
    ├── A10-xx_xxxx.shx
    ├── A10-xx_xxxx.dbf
    ├── A10-xx_xxxx.prj
    └── ...
```

---

## 社内メンバーへの共有

同じネットワーク内のPCからアクセスできるようにする方法:

```bash
streamlit run app.py --server.address 0.0.0.0
```

他のメンバーはブラウザで `http://あなたのIPアドレス:8501` にアクセス。

---

## 使用技術・データ出典

- **ジオコーディング**: [国土地理院 住所検索API](https://msearch.gsi.go.jp/address-search/AddressSearch)（無料）
- **GISデータ**: [国土数値情報](https://nlftp.mlit.go.jp/ksj/)（国土交通省）
- **フレームワーク**: [Streamlit](https://streamlit.io/)（Python）
