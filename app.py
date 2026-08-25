import os
import io
import re
import json
import zipfile
import tempfile
from urllib.parse import quote

import streamlit as st
import pandas as pd
import geopandas as gpd
import folium
from folium.plugins import Fullscreen, MeasureControl
from streamlit_folium import st_folium
import requests
import boto3
from botocore.config import Config
import google.generativeai as genai


# -------------------------------------------------------------
# 1. 頁面配置與 Secrets
# -------------------------------------------------------------
st.set_page_config(page_title="土石流潛勢溪流 Agent", page_icon="⛰️", initial_sidebar_state="expanded")
# 自訂 CSS 提升介面質感
st.markdown("""
<style>
    .main-title { font-size: 26px; font-weight: 700; color: #1E3A8A; margin-bottom: 5px; }
    .sub-title { font-size: 14px; color: #4B5563; margin-bottom: 20px; }
    .metric-card { background-color: #F8FAFC; border-left: 4px solid #2563EB; padding: 12px; border-radius: 6px; }
    .stButton>button { width: 100%; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)

# -------------------------------------------------------------
# 2. 讀取 Secrets 與初始化
# -------------------------------------------------------------
def get_secret(key: str, default: str = "") -> str:
    if key in st.secrets:
        return st.secrets[key]
    return os.getenv(key, default)

TURSO_URL = get_secret("TURSO_DATABASE_URL")
TURSO_TOKEN = get_secret("TURSO_AUTH_TOKEN")
GEMINI_API_KEY = get_secret("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# -------------------------------------------------------------
# 3. R2 智慧年份分群與預簽名下載 URL
# -------------------------------------------------------------
r2_clients_cache = {}

def determine_storage_group(file_name: str, storage_group: str) -> str:
    if storage_group and storage_group.strip():
        return storage_group.strip()
    year_match = re.search(r"^(19\d\d|20\d\d)", file_name)
    if year_match:
        year = int(year_match.group(1))
        if year <= 2007: return "R2_GRP_1"
        elif 2008 <= year <= 2010: return "R2_GRP_2"
        elif 2011 <= year <= 2015: return "R2_GRP_3"
        elif 2016 <= year <= 2020: return "R2_GRP_4"
        else: return "R2_GRP_5"
    return "R2_GRP_3"

def get_r2_download_url(file_name: str, storage_group: str) -> str:
    if not file_name:
        return ""
    grp = determine_storage_group(file_name, storage_group)

    # 兼容 [R2_GRP_X] 巢狀與平鋪環境變數
    sec_dict = st.secrets.get(grp, {}) if isinstance(st.secrets.get(grp), dict) else {}
    account_id = sec_dict.get("ACCOUNT_ID") or get_secret(f"{grp}_ACCOUNT_ID") or get_secret("R2_ACCOUNT_ID")
    access_key = sec_dict.get("ACCESS_KEY") or get_secret(f"{grp}_ACCESS_KEY") or get_secret("R2_ACCESS_KEY")
    secret_key = sec_dict.get("SECRET_KEY") or get_secret(f"{grp}_SECRET_KEY") or get_secret("R2_SECRET_KEY")
    bucket_name = sec_dict.get("BUCKET") or get_secret(f"{grp}_BUCKET") or get_secret("R2_BUCKET")

    if not all([account_id, access_key, secret_key, bucket_name]):
        return ""

    try:
        if grp not in r2_clients_cache:
            r2_clients_cache[grp] = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id.strip()}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key.strip(),
                aws_secret_access_key=secret_key.strip(),
                config=Config(signature_version="s3v4")
            )
        s3 = r2_clients_cache[grp]
        encoded_fn = quote(file_name)
        disposition = f"attachment; filename*=UTF-8''{encoded_fn}"

        return s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket_name.strip(), "Key": file_name, "ResponseContentDisposition": disposition},
            ExpiresIn=900
        )
    except Exception:
        return ""

# -------------------------------------------------------------
# 4. Turso 資料庫查詢與快取
# -------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_all_streams_from_turso():
    if not TURSO_URL or not TURSO_TOKEN:
        return pd.DataFrame()

    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN.strip()}", "Content-Type": "application/json"}
    sql = "SELECT file_name, stream_id, county, township, villages, disaster_history, demarcation_adjustments, storage_group FROM streams;"
    
    payload = {"requests": [{"type": "execute", "stmt": {"sql": sql}}, {"type": "close"}]}
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=12)
        res = resp.json()["results"][0]["response"]["result"]
        cols = [c["name"] for c in res["cols"]]
        rows = [[c.get("value") for c in r] for r in res.get("rows", [])]
        df = pd.DataFrame(rows, columns=cols)
        return df
    except Exception as e:
        st.error(f"Turso 資料庫連線失敗: {e}")
        return pd.DataFrame()

# -------------------------------------------------------------
# 5. 空間圖資讀取 (支援 SHP zip 與 GeoJSON)
# -------------------------------------------------------------
def load_spatial_file(uploaded_file) -> gpd.GeoDataFrame:
    try:
        fname = uploaded_file.name.lower()
        if fname.endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = os.path.join(tmpdir, "upload.zip")
                with open(zip_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(tmpdir)
                
                shp_files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.lower().endswith(".shp")]
                if not shp_files:
                    st.warning("⚠️ 上傳的 ZIP 檔內未找到 .shp 檔案！")
                    return None
                gdf = gpd.read_file(shp_files[0])
        elif fname.endswith((".geojson", ".json")):
            gdf = gpd.read_file(uploaded_file)
        else:
            st.error("❌ 不支援的格式，請上傳 .zip (包含 Shapefile) 或 .geojson 檔案。")
            return None

        # 強制轉為 WGS84 經緯度座標系統 (EPSG:4326)
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        elif gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
            
        return gdf
    except Exception as e:
        st.error(f"圖資讀取解析異常: {e}")
        return None

# -------------------------------------------------------------
# 6. 主頁面佈局
# -------------------------------------------------------------
st.markdown('<div class="main-title">⛰️ 土石流潛勢溪流歷史調查與圖資決策平台</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">整合 Turso 雲端調查庫、Gemini AI 災情沿革萃取、Cloudflare R2 調查報告下載與 GIS 空間圖資</div>', unsafe_allow_html=True)

# 側邊欄：圖資上傳與篩選過濾
with st.sidebar:
    st.header("📂 空間圖層上傳")
    uploaded_gis = st.file_uploader(
        "上傳潛勢溪流圖層 (ZIP / GeoJSON)",
        type=["zip", "geojson", "json"],
        help="ZIP 需包含 .shp, .shx, .dbf 等完整 Shapefile 檔案"
    )
    
    st.markdown("---")
    st.header("🔍 條件篩選與檢索")

# 載入 Turso 資料
df_turso = load_all_streams_from_turso()

if df_turso.empty:
    st.info("💡 目前資料庫尚無資料，請先執行 `ai_batch_extractor.py` 進行資料萃取。")
    st.stop()

# 側邊欄篩選組件
all_counties = ["全部縣市"] + sorted([c for c in df_turso["county"].dropna().unique() if c])
sel_county = st.sidebar.selectbox("所屬縣市", all_counties)

if sel_county != "全部縣市":
    filtered_df = df_turso[df_turso["county"] == sel_county]
    townships = ["全部鄉鎮"] + sorted([t for t in filtered_df["township"].dropna().unique() if t])
else:
    filtered_df = df_turso
    townships = ["全部鄉鎮"] + sorted([t for t in df_turso["township"].dropna().unique() if t])

sel_township = st.sidebar.selectbox("鄉鎮市區", townships)
if sel_township != "全部鄉鎮":
    filtered_df = filtered_df[filtered_df["township"] == sel_township]

search_kw = st.sidebar.text_input("關鍵字搜尋 (編號 / 村里 / 檔名)", placeholder="如：投縣DF135、秀林里")
if search_kw:
    pat = search_kw.strip()
    filtered_df = filtered_df[
        filtered_df["stream_id"].str.contains(pat, case=False, na=False) |
        filtered_df["villages"].str.contains(pat, case=False, na=False) |
        filtered_df["file_name"].str.contains(pat, case=False, na=False)
    ]

# 頂部統計指標
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("資料庫報告總數", f"{len(df_turso):,} 筆")
with c2:
    st.metric("篩選後筆數", f"{len(filtered_df):,} 筆")
with c3:
    st.metric("涵蓋縣市數", f"{df_turso['county'].nunique()} 個")
with c4:
    has_disaster = df_turso["disaster_history"].apply(lambda x: len(json.loads(x)) if x and x.startswith("[") else 0)
    st.metric("具災害紀錄溪流", f"{(has_disaster > 0).sum():,} 條")

# -------------------------------------------------------------
# 7. 主分頁規劃：[空間地圖圖台] / [結構化清冊] / [AI 智慧摘要分析]
# -------------------------------------------------------------
tab_map, tab_list, tab_ai = st.tabs(["🗺️ GIS 空間決策圖台", "📋 調查報告結構化清冊", "🤖 Gemini AI 決策綜整"])

# --- TAB 1: GIS 空間地圖圖台 ---
with tab_map:
    gdf = None
    if uploaded_gis is not None:
        gdf = load_spatial_file(uploaded_gis)
        if gdf is not None:
            st.success(f"✅ 成功載入圖資！共有 {len(gdf)} 筆空間幾何要素。")
            
            # 尋找圖層中的溪流編號欄位
            id_col_candidates = [c for c in gdf.columns if any(k in c.upper() for k in ["NO", "ID", "編號", "STREAM", "NAME"])]
            id_col = st.selectbox("請指定圖層中對應「溪流編號」的屬性欄位：", id_col_candidates) if id_col_candidates else None
    
    # 建立 Folium 地圖 (中心預設台灣中部)
    m = folium.Map(
        location=[23.973875, 120.982024],
        zoom_start=8,
        tiles="CartoDB positron",
        control_scale=True
    )
    Fullscreen().add_to(m)
    MeasureControl(position="topleft").add_to(m)

    # 若有圖資，進行屬性關聯並疊合至地圖
    if gdf is not None and not gdf.empty:
        # 圖層融合 Turso 屬性
        for idx, row in gdf.iterrows():
            geom = row.geometry
            if geom is None:
                continue

            stream_key = str(row[id_col]).strip() if id_col else ""
            db_match = df_turso[df_turso["stream_id"] == stream_key]
            
            if not db_match.empty:
                rec = db_match.iloc[0]
                c_name = rec["county"] or ""
                t_name = rec["township"] or ""
                adj_text = rec["demarcation_adjustments"] or "無調整紀錄"
                fname = rec["file_name"]
                s_grp = rec["storage_group"]
                
                # 取得 15 分鐘下載 URL
                dl_url = get_r2_download_url(fname, s_grp)
                dl_btn = f'<a href="{dl_url}" target="_blank" style="display:inline-block;padding:5px 10px;background-color:#2563EB;color:white;text-decoration:none;border-radius:4px;font-size:12px;margin-top:6px;">📄 下載原始調查報告 (PDF)</a>' if dl_url else '<span style="color:#9CA3AF;font-size:12px;">⚠️ 無法生成下載連結</span>'

                popup_html = f"""
                <div style="font-family:sans-serif; width:260px;">
                    <h4 style="margin:0 0 5px 0; color:#1E3A8A;">📌 {stream_key}</h4>
                    <p style="margin:0; font-size:12px; color:#4B5563;"><b>行政區：</b>{c_name} {t_name}</p>
                    <p style="margin:4px 0; font-size:12px; color:#4B5563;"><b>劃設沿革：</b>{adj_text[:60]}...</p>
                    {dl_btn}
                </div>
                """
                color = "#DC2626"  # 資料庫比對成功顯示紅色
            else:
                popup_html = f"<b>{stream_key}</b><br><span style='color:#6B7280;'>（資料庫尚無調查報告）</span>"
                color = "#3B82F6"  # 一般未比對顯示藍色

            # 依幾何型態繪製
            if geom.geom_type in ["LineString", "MultiLineString"]:
                folium.GeoJson(
                    geom,
                    style_function=lambda x, col=color: {"color": col, "weight": 4, "opacity": 0.8},
                    tooltip=f"溪流編號：{stream_key}",
                    popup=folium.Popup(popup_html, max_width=300)
                ).add_to(m)
            elif geom.geom_type in ["Polygon", "MultiPolygon"]:
                folium.GeoJson(
                    geom,
                    style_function=lambda x, col=color: {"fillColor": col, "color": col, "weight": 2, "fillOpacity": 0.4},
                    tooltip=f"影響範圍：{stream_key}",
                    popup=folium.Popup(popup_html, max_width=300)
                ).add_to(m)
            elif geom.geom_type in ["Point", "MultiPoint"]:
                folium.CircleMarker(
                    location=[geom.y, geom.x],
                    radius=6,
                    color=color,
                    fill=True,
                    fillColor=color,
                    popup=folium.Popup(popup_html, max_width=300)
                ).add_to(m)

        # 自動縮放至圖層邊界
        bounds = gdf.total_bounds
        m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

    st_folium(m, width="100%", height=620, returned_objects=[])

# --- TAB 2: 調查報告結構化清冊 ---
with tab_list:
    st.subheader(f"📋 溪流調查紀錄列表（共 {len(filtered_df)} 筆）")
    
    for idx, r in filtered_df.iterrows():
        with st.expander(f"📌 【{r['stream_id'] or '未編號'}】 {r['county']}{r['township']} - {r['file_name']}"):
            col_l, col_r = st.columns([2, 1])
            
            with col_l:
                v_list = json.loads(r["villages"]) if r["villages"] and r["villages"].startswith("[") else []
                st.markdown(f"**📍 涵蓋村里**：{'、'.join(v_list) if v_list else '未標明'}")
                st.markdown(f"**📐 劃設沿革與等級調整**：\n{r['demarcation_adjustments'] or '無紀錄'}")
                
                # 歷年災害清單
                h_list = json.loads(r["disaster_history"]) if r["disaster_history"] and r["disaster_history"].startswith("[") else []
                if h_list:
                    st.markdown("**🕒 歷年重大土石流災害**：")
                    for d in h_list:
                        yr = d.get("year", "歷史事件")
                        desc = d.get("scale_and_damage") or d.get("description", "")
                        rf = d.get("rainfall_info", "")
                        st.markdown(f"- **{yr}**：{desc} " + (f"*(雨量: {rf})*" if rf else ""))
                else:
                    st.markdown("**🕒 歷年重大土石流災害**：無歷史災情紀錄")
                    
            with col_r:
                st.markdown("**📄 報告檔案下載**")
                dl_link = get_r2_download_url(r["file_name"], r["storage_group"])
                if dl_link:
                    st.link_button("⬇️ 下載原始調查報告 (PDF)", dl_link, type="primary")
                    st.caption("⚡ 連結具備安全時效（15分鐘內有效）")
                else:
                    st.warning("無法取得下載網址，請確認 R2 環境變數")

# --- TAB 3: Gemini AI 決策綜整 ---
with tab_ai:
    st.subheader("🤖 調查資料 AI 智慧決策摘要")
    st.write("利用 Gemini 3.6 Flash 對當前篩選的溪流調查紀錄進行防救災與劃設沿革綜整分析。")
    
    if st.button("✨ 生成當前篩選溪流之 AI 綜整分析報告", type="primary"):
        if not GEMINI_API_KEY:
            st.error("❌ 未設定 GEMINI_API_KEY，請至 secrets.toml 設定！")
        else:
            with st.spinner("AI 正在彙整分析中..."):
                sample_data = filtered_df[["stream_id", "county", "township", "villages", "disaster_history", "demarcation_adjustments"]].head(20).to_dict(orient="records")
                
                prompt = f"""
                你是一名資深的土石流防災與水土保持工程專家。請根據以下 {len(sample_data)} 筆潛勢溪流的歷年調查紀錄，進行專業的統整分析報告：
                
                資料內容：
                {json.dumps(sample_data, ensure_ascii=False, indent=2)}
                
                請依下列架構輸出繁體中文 Markdown 報告：
                1. **歷年災害情勢與高風險區域特徵分析**（統整誘發雨量、重複致災點）
                2. **劃設等級調整歷史趨勢**（新增、等級提升或保全對象變更主要原因）
                3. **後續治理與巡勘精進建議**
                """
                
                try:
                    model = genai.GenerativeModel("gemini-3.6-flash")
                    response = model.generate_content(prompt)
                    st.markdown(response.text)
                except Exception as e:
                    st.error(f"AI 生成失敗: {e}")
三、相依套件清單 (requirements.txt)
為確保本地與 Streamlit Community Cloud 部署時具備完整空間圖資處理與地圖元件，請確認 requirements.txt 內容如下：

Plaintext
streamlit
pandas
geopandas
folium
streamlit-folium
shapely
fiona
requests
boto3
pydantic
toml
google-generativeai
四、密鑰設定檔 (.streamlit/secrets.toml)
在專案根目錄下的 .streamlit/secrets.toml（或 Streamlit Cloud 後台 Secrets 區塊），填入以下設定：

Ini, TOML
GEMINI_API_KEY = "你的_Gemini_API_Key"
TURSO_DATABASE_URL = "libsql://debris-db-xxx.turso.io"
TURSO_AUTH_TOKEN = "eyJhbGciOi..."

# 5 組 R2 分群設定 (對應 3,200 份 PDF 的年份區間)
[R2_GRP_1]
ACCOUNT_ID = "你的_Cloudflare_Account_ID"
ACCESS_KEY = "你的_R2_Access_Key"
SECRET_KEY = "你的_R2_Secret_Key"
BUCKET = "debris-reports-2007"

[R2_GRP_2]
ACCOUNT_ID = "你的_Cloudflare_Account_ID"
ACCESS_KEY = "你的_R2_Access_Key"
SECRET_KEY = "你的_R2_Secret_Key"
BUCKET = "debris-reports-2008-2010"

[R2_GRP_3]
ACCOUNT_ID = "你的_Cloudflare_Account_ID"
ACCESS_KEY = "你的_R2_Access_Key"
SECRET_KEY = "你的_R2_Secret_Key"
BUCKET = "debris-reports-2011-2015"

[R2_GRP_4]
ACCOUNT_ID = "你的_Cloudflare_Account_ID"
ACCESS_KEY = "你的_R2_Access_Key"
SECRET_KEY = "你的_R2_Secret_Key"
BUCKET = "debris-reports-2016-2020"

[R2_GRP_5]
ACCOUNT_ID = "你的_Cloudflare_Account_ID"
ACCESS_KEY = "你的_R2_Access_Key"
SECRET_KEY = "你的_R2_Secret_Key"
BUCKET = "debris-reports-2021-recent"
