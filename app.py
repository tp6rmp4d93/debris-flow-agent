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
# 1. 頁面配置與行動裝置響應式樣式
# -------------------------------------------------------------
st.set_page_config(
    page_title="土石流潛勢溪流調查Agent",
    page_icon="⛰️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 隱藏側邊欄與行動裝置優化 CSS
st.markdown("""
<style>
    /* 完全隱藏側邊欄與開關按鈕 */
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {
        display: none !important;
    }
    .main .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2.5rem;
        padding-left: 1rem;
        padding-right: 1rem;
        max-width: 1200px;
    }
    .main-title {
        font-size: 22px;
        font-weight: 800;
        color: #1E3A8A;
        margin-bottom: 2px;
    }
    .sub-title {
        font-size: 13px;
        color: #64748B;
        margin-bottom: 15px;
    }
    .filter-box {
        background: #F8FAFC;
        border: 1px solid #E2E8F0;
        padding: 12px 14px 4px 14px;
        border-radius: 10px;
        margin-bottom: 15px;
    }
    .card-item {
        background: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 12px;
        margin-bottom: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }
    .disaster-badge {
        background-color: #FEF2F2;
        border-left: 3px solid #EF4444;
        padding: 6px 10px;
        border-radius: 4px;
        margin-top: 5px;
        margin-bottom: 5px;
        font-size: 13px;
    }
    .year-tag {
        background: #EFF6FF;
        color: #1D4ED8;
        padding: 2px 8px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 12px;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 15px;
        font-weight: 600;
        padding: 8px 14px;
    }
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

# -------------------------------------------------------------
# 3. R2 智慧年份分群與預簽名安全下載
# -------------------------------------------------------------
r2_clients_cache = {}

def determine_storage_group(file_name, storage_group) -> str:
    if storage_group is not None and not pd.isna(storage_group):
        s_grp = str(storage_group).strip()
        if s_grp and s_grp.lower() not in ["none", "nan", "null"]:
            return s_grp

    if not file_name or pd.isna(file_name):
        return "R2_GRP_3"

    fn = str(file_name).strip()
    year_match = re.search(r"^(19\d\d|20\d\d)", fn)
    if year_match:
        year = int(year_match.group(1))
        if year <= 2007: return "R2_GRP_1"
        elif 2008 <= year <= 2010: return "R2_GRP_2"
        elif 2011 <= year <= 2015: return "R2_GRP_3"
        elif 2016 <= year <= 2020: return "R2_GRP_4"
        else: return "R2_GRP_5"
        
    return "R2_GRP_3"

def get_r2_download_url(file_name, storage_group) -> str:
    if not file_name or pd.isna(file_name):
        return ""
        
    fn = str(file_name).strip()
    if not fn or fn.lower() in ["none", "nan", "null"]:
        return ""

    grp = determine_storage_group(fn, storage_group)

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
                endpoint_url=f"https://{str(account_id).strip()}.r2.cloudflarestorage.com",
                aws_access_key_id=str(access_key).strip(),
                aws_secret_access_key=str(secret_key).strip(),
                config=Config(signature_version="s3v4")
            )
        s3 = r2_clients_cache[grp]
        encoded_fn = quote(fn)
        disposition = f"attachment; filename*=UTF-8''{encoded_fn}"

        return s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": str(bucket_name).strip(), "Key": fn, "ResponseContentDisposition": disposition},
            ExpiresIn=900
        )
    except Exception:
        return ""

# -------------------------------------------------------------
# 4. Turso 資料庫載入與快取
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
# 5. 空間圖資讀取 (GitHub data/ 目錄自動探測)
# -------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_default_gis_layer() -> gpd.GeoDataFrame:
    possible_paths = [
        "data/debris_streams.geojson",
        "debris_streams.geojson",
        "data/debris_streams.json",
        "data/debris_streams.zip",
        "debris_streams.zip"
    ]
    for path in possible_paths:
        if os.path.exists(path):
            try:
                if path.endswith(".zip"):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with zipfile.ZipFile(path, "r") as zip_ref:
                            zip_ref.extractall(tmpdir)
                        shp_files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.lower().endswith(".shp")]
                        if shp_files:
                            gdf = gpd.read_file(shp_files[0])
                        else:
                            continue
                else:
                    gdf = gpd.read_file(path)

                if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs(epsg=4326)
                elif gdf.crs is None:
                    gdf.set_crs(epsg=4326, inplace=True)
                return gdf
            except Exception:
                continue
    return None

# -------------------------------------------------------------
# 6. AI 決策綜整分析 (Gemini 3.1 Flash-Lite + 快取保護)
# -------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def generate_ai_summary_cached(stream_ids_tuple, sample_data_json, api_key):
    genai.configure(api_key=api_key)
    # 使用輕量快速且配額寬裕的 3.1 Flash-Lite 模型
    model = genai.GenerativeModel("gemini-3.1-flash-lite")
    
    prompt = f"""
    你是一名資深的土石流防災與水土保持工程專家。請根據以下潛勢溪流的歷年調查紀錄，進行精準專業的統整分析：
    
    資料內容：
    {sample_data_json}
    
    請依下列架構輸出繁體中文 Markdown 報告：
    1. **歷年災害情勢與致災熱點分析**（統整重複致災溪流、誘發雨量特徵與保全受損）
    2. **劃設等級調整歷程趨勢**（探討等級提升、範圍調整或新增溪流的主因）
    3. **後續巡勘與工程治理建議**（提供具體防減災對策）
    """
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            return response.text
        except ResourceExhausted:
            if attempt < 2:
                time.sleep(10)
                continue
            raise
        except Exception as e:
            raise e

# -------------------------------------------------------------
# 7. 主頁面佈局開始
# -------------------------------------------------------------
st.markdown('<div class="main-title">⛰️ 土石流潛勢溪流調查Agent</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">歷史調查報告檢索 ｜ 劃設沿革與重大災情 ｜ 空間圖資與 AI 決策分析</div>', unsafe_allow_html=True)

df_turso = load_all_streams_from_turso()
if df_turso.empty:
    st.info("💡 目前資料庫尚無資料，請先執行 `ai_batch_extractor.py` 進行資料萃取。")
    st.stop()

# --- 頂部篩選器 (支援手機自動響應) ---
with st.container():
    st.markdown('<div class="filter-box">', unsafe_allow_html=True)
    c_county, c_township, c_search = st.columns([1, 1, 2])
    
    all_counties = ["全部縣市"] + sorted([c for c in df_turso["county"].dropna().unique() if c])
    with c_county:
        sel_county = st.selectbox("所屬縣市", all_counties, label_visibility="collapsed")
    
    if sel_county != "全部縣市":
        filtered_df = df_turso[df_turso["county"] == sel_county]
        townships = ["全部鄉鎮"] + sorted([t for t in filtered_df["township"].dropna().unique() if t])
    else:
        filtered_df = df_turso
        townships = ["全部鄉鎮"] + sorted([t for t in df_turso["township"].dropna().unique() if t])

    with c_township:
        sel_township = st.selectbox("鄉鎮市區", townships, label_visibility="collapsed")
        if sel_township != "全部鄉鎮":
            filtered_df = filtered_df[filtered_df["township"] == sel_township]

    with c_search:
        search_kw = st.text_input("關鍵字搜尋", placeholder="輸入溪流編號 (如: 投縣DF135) 或村里名稱", label_visibility="collapsed")
        if search_kw:
            pat = search_kw.strip()
            filtered_df = filtered_df[
                filtered_df["stream_id"].str.contains(pat, case=False, na=False) |
                filtered_df["villages"].str.contains(pat, case=False, na=False) |
                filtered_df["file_name"].str.contains(pat, case=False, na=False)
            ]
    st.markdown('</div>', unsafe_allow_html=True)

# 簡明統計列
st.caption(f"📊 篩選結果：共 **{len(filtered_df):,}** 筆調查紀錄（全台資料庫總計 {len(df_turso):,} 筆）")

# -------------------------------------------------------------
# 8. 三大主功能分頁
# -------------------------------------------------------------
tab1, tab2, tab3 = st.tabs([
    "📋 調查資料 (沿革與災情)",
    "📄 歷年調查報告 (溪流報告)",
    "🗺️ 空間圖台與 AI 決策"
])

# =============================================================
# TAB 1: 調查資料 (詳細沿革與歷年重大災害)
# =============================================================
with tab1:
    if filtered_df.empty:
        st.warning("查無符合條件之溪流調查資料。")
    else:
        for idx, r in filtered_df.iterrows():
            sid = r["stream_id"] or "未編號溪流"
            cty = r["county"] or ""
            twn = r["township"] or ""
            v_list = json.loads(r["villages"]) if r["villages"] and r["villages"].startswith("[") else []
            v_str = "、".join(v_list) if v_list else "未載明村里"
            adj = r["demarcation_adjustments"] or "無調整紀錄"
            
            with st.expander(f"📌 【{sid}】 {cty} {twn}（{v_str}）", expanded=(len(filtered_df) == 1)):
                st.markdown(f"**📐 劃設調整沿革**：\n{adj}")
                
                h_list = json.loads(r["disaster_history"]) if r["disaster_history"] and r["disaster_history"].startswith("[") else []
                if h_list:
                    st.markdown("**🕒 歷年重大災害情勢**：")
                    for d in h_list:
                        yr = d.get("year", "歷史災害")
                        rf = d.get("rainfall_info", "")
                        dmg = d.get("scale_and_damage") or d.get("description", "無詳細說明")
                        
                        rf_badge = f"<span style='color:#2563EB;font-size:12px;margin-left:8px;'>🌧️ 雨量：{rf}</span>" if (rf and rf != "未載明") else ""
                        st.markdown(f"""
                        <div class="disaster-badge">
                            <b>🚨 {yr}</b>{rf_badge}<br>
                            <span style="color:#334155;">{dmg}</span>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.markdown("<span style='color:#94A3B8;font-size:13px;'>• 報告內無重大歷史災害紀錄</span>", unsafe_allow_html=True)

# =============================================================
# TAB 2: 調查報告 (依年度由新至舊排列 + 15分鐘 PDF 下載)
# =============================================================
with tab2:
    if filtered_df.empty:
        st.warning("查無符合條件之調查報告。")
    else:
        # 解析年份並依年度由新到舊排序
        def parse_report_year(fn):
            m = re.search(r"^(19\d\d|20\d\d)", str(fn))
            return int(m.group(1)) if m else 0

        df_reports = filtered_df.copy()
        df_reports["report_year"] = df_reports["file_name"].apply(parse_report_year)
        # 依年度新至舊 (降冪)、溪流編號 (升冪) 排序
        df_reports = df_reports.sort_values(by=["report_year", "stream_id"], ascending=[False, True])

        for idx, r in df_reports.iterrows():
            yr = r["report_year"]
            yr_display = f"{yr} 年" if yr > 0 else "調查年份未標明"
            fname = r["file_name"]
            sid = r["stream_id"] or "未知編號"
            s_grp = r["storage_group"]
            
            dl_url = get_r2_download_url(fname, s_grp)
            
            c_info, c_btn = st.columns([3, 1])
            with c_info:
                st.markdown(f"""
                <div style="padding-top:4px;">
                    <span class="year-tag">📅 {yr_display}</span>
                    <b style="font-size:15px; margin-left:6px; color:#1E293B;">【{sid}】</b> 
                    <span style="color:#64748B; font-size:13px;">{fname}</span>
                </div>
                """, unsafe_allow_html=True)
            with c_btn:
                if dl_url:
                    st.link_button("⬇️ 下載 PDF", dl_url, type="primary")
                else:
                    st.button("⚠️ 無連結", disabled=True, key=f"btn_dis_{idx}")
            st.markdown("<hr style='margin:8px 0; border:0; border-top:1px dashed #E2E8F0;'>", unsafe_allow_html=True)

# =============================================================
# TAB 3: 空間圖台與 AI 決策 (地圖 + Gemini 決策綜整)
# =============================================================
with tab3:
    gdf = load_default_gis_layer()
    
    st.markdown("##### 🗺️ 潛勢溪流空間圖台")
    m = folium.Map(location=[23.973875, 120.982024], zoom_start=8, tiles="CartoDB positron", control_scale=True)
    Fullscreen().add_to(m)
    MeasureControl(position="topleft").add_to(m)

    if gdf is not None and not gdf.empty:
        id_col = next((c for c in gdf.columns if any(k in c.upper() for k in ["NO", "ID", "編號", "STREAM", "NAME"])), gdf.columns[0])
        target_ids = set(filtered_df["stream_id"].dropna().unique())
        
        for idx, row in gdf.iterrows():
            geom = row.geometry
            if geom is None: continue

            stream_key = str(row[id_col]).strip()
            if target_ids and stream_key not in target_ids and len(target_ids) < len(df_turso):
                continue

            db_match = df_turso[df_turso["stream_id"] == stream_key]
            if not db_match.empty:
                rec = db_match.iloc[0]
                c_name = rec["county"] or ""
                t_name = rec["township"] or ""
                adj_text = rec["demarcation_adjustments"] or "無調整紀錄"
                fname = rec["file_name"]
                s_grp = rec["storage_group"]
                
                dl_url = get_r2_download_url(fname, s_grp)
                dl_btn = f'<a href="{dl_url}" target="_blank" style="display:inline-block;padding:4px 8px;background-color:#2563EB;color:white;text-decoration:none;border-radius:4px;font-size:11px;margin-top:6px;">📄 下載調查報告</a>' if dl_url else ''

                popup_html = f"""
                <div style="font-family:sans-serif; width:220px;">
                    <h5 style="margin:0 0 4px 0; color:#1E3A8A;">📌 {stream_key}</h5>
                    <div style="font-size:12px; color:#475569;"><b>行政區：</b>{c_name} {t_name}</div>
                    <div style="font-size:11px; color:#64748B; margin-top:2px;"><b>沿革：</b>{adj_text[:40]}...</div>
                    {dl_btn}
                </div>
                """
                color = "#DC2626"
            else:
                popup_html = f"<b>{stream_key}</b>"
                color = "#3B82F6"

            if geom.geom_type in ["LineString", "MultiLineString"]:
                folium.GeoJson(geom, style_function=lambda x, col=color: {"color": col, "weight": 4, "opacity": 0.85}, tooltip=stream_key, popup=folium.Popup(popup_html, max_width=260)).add_to(m)
            elif geom.geom_type in ["Polygon", "MultiPolygon"]:
                folium.GeoJson(geom, style_function=lambda x, col=color: {"fillColor": col, "color": col, "weight": 2, "fillOpacity": 0.4}, tooltip=stream_key, popup=folium.Popup(popup_html, max_width=260)).add_to(m)
            elif geom.geom_type in ["Point", "MultiPoint"]:
                folium.CircleMarker(location=[geom.y, geom.x], radius=6, color=color, fill=True, fillColor=color, popup=folium.Popup(popup_html, max_width=260)).add_to(m)

        try:
            bounds = gdf.total_bounds
            m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
        except Exception:
            pass

    st_folium(m, width="100%", height=450, returned_objects=[])

    st.markdown("---")
    st.markdown("##### 🤖 Gemini AI 智慧決策摘要")
    if st.button("✨ 生成當前篩選溪流之防救災與治理綜整報告", type="primary", use_container_width=True):
        if not GEMINI_API_KEY:
            st.error("❌ 未設定 GEMINI_API_KEY，請至 secrets.toml 設定。")
        elif filtered_df.empty:
            st.warning("⚠️ 目前條件下無任何溪流資料可供分析。")
        else:
            with st.spinner("Gemini 正在彙整分析歷年調查紀錄..."):
                target_records = filtered_df[["stream_id", "county", "township", "villages", "disaster_history", "demarcation_adjustments"]].head(15).to_dict(orient="records")
                stream_ids = tuple(filtered_df["stream_id"].head(15).tolist())
                data_json_str = json.dumps(target_records, ensure_ascii=False)
                
                try:
                    result_text = generate_ai_summary_cached(stream_ids, data_json_str, GEMINI_API_KEY)
                    st.markdown(result_text)
                except ResourceExhausted:
                    st.error("⏳ API 呼叫頻率已達免費上限，請稍候 1 分鐘後重試。")
                except Exception as e:
                    st.error(f"AI 生成失敗: {e}")

# -------------------------------------------------------------
# 9. 頁尾極簡狀態列 (不起眼顯示於最下方)
# -------------------------------------------------------------
st.markdown("<br><hr style='margin: 20px 0 8px 0; border:0; border-top:1px solid #F1F5F9;'>", unsafe_allow_html=True)
layer_status = f"已載入 GitHub 圖資 ({len(gdf):,} 筆)" if gdf is not None else "未檢測到空間圖層"
st.caption(f"🌐 系統狀態：{layer_status} ｜ 資料庫：Turso 連線正常 ｜ 儲存庫：Cloudflare R2 5組分群就緒")
