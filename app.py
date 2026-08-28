import os
import io
import re
import json
import time
from urllib.parse import quote

import streamlit as st
import pandas as pd
import folium
from folium.plugins import Fullscreen, MeasureControl
from streamlit_folium import st_folium
import requests
import boto3
from botocore.config import Config
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

# -------------------------------------------------------------
# 1. 頁面配置與行動裝置響應式樣式
# -------------------------------------------------------------
st.set_page_config(
    page_title="土石流潛勢溪流調查Agent",
    page_icon="⛰️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 隱藏側邊欄與行動裝置極簡排版
st.markdown("""
<style>
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {
        display: none !important;
    }
    .main .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2.5rem;
        padding-left: 1rem;
        padding-right: 1rem;
        max-width: 1100px;
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
    }
    .filter-box {
        background: #F8FAFC;
        border: 1px solid #E2E8F0;
        padding: 12px 14px 4px 14px;
        border-radius: 10px;
        margin-bottom: 12px;
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
    .empty-state {
        text-align: center;
        padding: 40px 20px;
        background: #F8FAFC;
        border: 1px dashed #CBD5E1;
        border-radius: 8px;
        color: #64748B;
        margin-top: 10px;
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
# 3. R2 智慧年份分群與預簽名安全下載 (強健解析 + 即時診斷)
# -------------------------------------------------------------
r2_clients_cache = {}

def determine_storage_group(file_name, storage_group) -> str:
    """優先以檔名年份精準導向對應的 R2 群組 (2014 -> R2_GRP_3)"""
    fn = str(file_name or "").strip()
    year_match = re.search(r"^(19\d\d|20\d\d)", fn)
    if year_match:
        year = int(year_match.group(1))
        if year <= 2007: return "R2_GRP_1"
        elif 2008 <= year <= 2010: return "R2_GRP_2"
        elif 2011 <= year <= 2015: return "R2_GRP_3"
        elif 2016 <= year <= 2020: return "R2_GRP_4"
        else: return "R2_GRP_5"

    if storage_group and not pd.isna(storage_group):
        s_grp = str(storage_group).strip()
        if s_grp.lower() not in ["none", "nan", "null", ""]:
            return s_grp

    return "R2_GRP_3"

def get_r2_download_url(file_name, storage_group) -> tuple[str, str]:
    """
    生成 15 分鐘有效的 R2 預簽名下載 URL
    回傳值：(download_url, error_message)
    """
    if not file_name or pd.isna(file_name):
        return "", "檔名為空"
        
    fn = str(file_name).strip()
    if not fn or fn.lower() in ["none", "nan", "null"]:
        return "", "無效檔名"

    grp = determine_storage_group(fn, storage_group)

    # 1. 深度掃描 st.secrets (不分大小寫、支援區塊與平鋪)
    account_id, access_key, secret_key, bucket_name = None, None, None, None

    # (A) 嘗試從巢狀區塊讀取 (如 [R2_GRP_3] 或 [r2_grp_3])
    for g_key in [grp, grp.lower(), grp.upper()]:
        if g_key in st.secrets:
            sec_dict = st.secrets[g_key]
            if isinstance(sec_dict, dict) or hasattr(sec_dict, "items"):
                norm = {str(k).upper(): str(v).strip() for k, v in sec_dict.items()}
                account_id = norm.get("ACCOUNT_ID")
                access_key = norm.get("ACCESS_KEY") or norm.get("ACCESS_KEY_ID")
                secret_key = norm.get("SECRET_KEY") or norm.get("SECRET_ACCESS_KEY")
                bucket_name = norm.get("BUCKET") or norm.get("BUCKET_NAME")
                if account_id and access_key and secret_key and bucket_name:
                    break

    # (B) 嘗試從平鋪變數讀取 (如 R2_GRP_3_ACCOUNT_ID 或 R2_ACCOUNT_ID)
    if not all([account_id, access_key, secret_key, bucket_name]):
        account_id = account_id or get_secret(f"{grp}_ACCOUNT_ID") or get_secret("R2_ACCOUNT_ID")
        access_key = access_key or get_secret(f"{grp}_ACCESS_KEY") or get_secret("R2_ACCESS_KEY")
        secret_key = secret_key or get_secret(f"{grp}_SECRET_KEY") or get_secret("R2_SECRET_KEY")
        bucket_name = bucket_name or get_secret(f"{grp}_BUCKET") or get_secret("R2_BUCKET")

    # (C) 備援：若仍缺少，嘗試抓取全域任何一組有效的 R2 金鑰
    if not all([account_id, access_key, secret_key]):
        for fallback_grp in ["R2_GRP_1", "R2_GRP_2", "R2_GRP_4", "R2_GRP_5"]:
            if fallback_grp in st.secrets:
                fb_sec = st.secrets[fallback_grp]
                if isinstance(fb_sec, dict) or hasattr(fb_sec, "items"):
                    norm_fb = {str(k).upper(): str(v).strip() for k, v in fb_sec.items()}
                    account_id = account_id or norm_fb.get("ACCOUNT_ID")
                    access_key = access_key or norm_fb.get("ACCESS_KEY")
                    secret_key = secret_key or norm_fb.get("SECRET_KEY")

    # 預設 Bucket
    if not bucket_name:
        bucket_name = "debris-reports-2011-2015" if grp == "R2_GRP_3" else "debris-reports-2007"

    # 若依然缺少必要金鑰，回報精確錯誤
    if not all([account_id, access_key, secret_key, bucket_name]):
        missing = []
        if not account_id: missing.append("ACCOUNT_ID")
        if not access_key: missing.append("ACCESS_KEY")
        if not secret_key: missing.append("SECRET_KEY")
        return "", f"Secrets 缺少群組【{grp}】的設定: {', '.join(missing)}"

    try:
        cache_key = f"{grp}_{account_id}"
        if cache_key not in r2_clients_cache:
            r2_clients_cache[cache_key] = boto3.client(
                "s3",
                endpoint_url=f"https://{str(account_id).strip()}.r2.cloudflarestorage.com",
                aws_access_key_id=str(access_key).strip(),
                aws_secret_access_key=str(secret_key).strip(),
                region_name="auto",
                config=Config(signature_version="s3v4")
            )
        s3 = r2_clients_cache[cache_key]
        encoded_fn = quote(fn)
        disposition = f"attachment; filename*=UTF-8''{encoded_fn}"

        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": str(bucket_name).strip(),
                "Key": fn,
                "ResponseContentDisposition": disposition
            },
            ExpiresIn=900
        )
        return url, ""
    except Exception as e:
        return "", f"R2 簽名失敗: {str(e)}"
        
# -------------------------------------------------------------
# 4. Turso 資料庫載入與快取
# -------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_all_streams_from_turso():
    if not TURSO_URL or not TURSO_TOKEN:
        return pd.DataFrame()

    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN.strip()}", "Content-Type": "application/json"}
# -------------------------------------------------------------
# 1. 確保 SQL 查詢包含所有 9 個標準欄位
# -------------------------------------------------------------
sql = """
    SELECT 
    stream_id, 
    county, 
    township, 
    villages, 
    disaster_history, 
    demarcation_adjustments, 
    file_name, 
    storage_group, 
    risk_history
    FROM streams;
"""

# -------------------------------------------------------------
# 2. 確保 DataFrame 欄位名稱清單完全一一對齊 (共 9 個)
# -------------------------------------------------------------
columns = [
    "stream_id",
    "county",
    "township",
    "villages",
    "disaster_history",
    "demarcation_adjustments",
    "file_name",
    "storage_group",
    "risk_history"
]

# 建立 DataFrame 並將字串欄位填補空字串，避免 None 導致 .str 操作報錯
df = pd.DataFrame(rows, columns=columns)
df["file_name"] = df["file_name"].fillna("").astype(str)
df["stream_id"] = df["stream_id"].fillna("").astype(str)
df["county"] = df["county"].fillna("").astype(str)
df["township"] = df["township"].fillna("").astype(str)
df["villages"] = df["villages"].fillna("").astype(str)
df["demarcation_adjustments"] = df["demarcation_adjustments"].fillna("").astype(str)
df["risk_history"] = df["risk_history"].fillna("").astype(str)
df["disaster_history"] = df["disaster_history"].fillna("").astype(str)    


payload = {"requests": [{"type": "execute", "stmt": {"sql": sql}}, {"type": "close"}]}
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=12)
        res = resp.json()["results"][0]["response"]["result"]
        cols = [c["name"] for c in res["cols"]]
        rows = [[c.get("value") for c in r] for r in res.get("rows", [])]
        return pd.DataFrame(rows, columns=cols)  # 👈 這行往內縮排（與上方指令對齊）
    except Exception as e:
        st.error(f"Turso 資料庫連線失敗: {e}")
        return pd.DataFrame()  # 👈 這行往內縮排（與 st.error 對齊）

# -------------------------------------------------------------
# 5. Gemini AI 智慧決策摘要 (輕量 3.1 Flash-Lite + 快取保護)
# -------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def generate_ai_summary_cached(stream_ids_tuple, sample_data_json, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3.7-flash")
    
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
# 6. 主頁面與頂部條件篩選
# -------------------------------------------------------------
st.markdown('<div class="main-title">⛰️ 土石流潛勢溪流調查決策平台</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">歷史調查報告檢索 ｜ 劃設沿革與重大災情 ｜ AI 智慧決策綜整</div>', unsafe_allow_html=True)

df_turso = load_all_streams_from_turso()
if df_turso.empty:
    st.info("💡 資料庫目前無資料，請先執行資料匯入。")
    st.stop()

# 檢索條件輸入區
# --- 頂部條件篩選區 (已移除多餘的 filter-box div) ---
with st.container():
    c_county, c_township, c_search = st.columns([1, 1, 2])
    
    all_counties = ["選擇縣市"] + sorted([c for c in df_turso["county"].dropna().unique() if c])
    with c_county:
        sel_county = st.selectbox("所屬縣市", all_counties, label_visibility="collapsed")
    
    # 判斷是否啟動篩選
    has_filter = False
    
    if sel_county != "選擇縣市":
        has_filter = True
        filtered_df = df_turso[df_turso["county"] == sel_county]
        townships = ["全部鄉鎮"] + sorted([t for t in filtered_df["township"].dropna().unique() if t])
    else:
        filtered_df = df_turso
        townships = ["全部鄉鎮"] + sorted([t for t in df_turso["township"].dropna().unique() if t])

    with c_township:
        sel_township = st.selectbox("鄉鎮市區", townships, label_visibility="collapsed")
        if sel_township != "全部鄉鎮":
            has_filter = True
            filtered_df = filtered_df[filtered_df["township"] == sel_township]

    with c_search:
        search_kw = st.text_input("關鍵字搜尋", placeholder="輸入溪流編號 (如: 投縣DF135) 或村里名稱", label_visibility="collapsed")
        if search_kw.strip():
            has_filter = True
            pat = search_kw.strip()
            filtered_df = filtered_df[
                filtered_df["stream_id"].str.contains(pat, case=False, na=False) |
                filtered_df["villages"].str.contains(pat, case=False, na=False) |
                filtered_df["file_name"].str.contains(pat, case=False, na=False)
            ]

    # 關鍵字篩選安全防護寫法
    if keyword:
        pat = keyword.strip()
        mask = (
            filtered_df["stream_id"].astype(str).str.contains(pat, case=False, na=False) |
            filtered_df["file_name"].astype(str).str.contains(pat, case=False, na=False) |
            filtered_df["county"].astype(str).str.contains(pat, case=False, na=False) |
            filtered_df["township"].astype(str).str.contains(pat, case=False, na=False) |
            filtered_df["villages"].astype(str).str.contains(pat, case=False, na=False)
        )
        filtered_df = filtered_df[mask]
    
    st.markdown('</div>', unsafe_allow_html=True)

# 狀態提示列
if has_filter:
    st.caption(f"📊 篩選結果：共 **{len(filtered_df):,}** 筆調查紀錄（全台資料庫總計 {len(df_turso):,} 筆）")
else:
    st.caption(f"📊 資料庫就緒（全台共 {len(df_turso):,} 筆紀錄），請設定上方條件開始檢索。")

# -------------------------------------------------------------
# 7. 三大功能分頁
# -------------------------------------------------------------
tab1, tab2, tab3 = st.tabs([
    "📋 調查資料 (沿革與災情)",
    "📄 調查報告 (歷年 PDF)",
    "🤖 AI 智慧決策摘要"
])

# =============================================================
# TAB 1: 調查資料 (依溪流整併聚合 + 歷年風險等級歷程)
# =============================================================
with tab1:
    if not has_filter:
        st.markdown("""
        <div class="empty-state">
            <h4 style="margin:0 0 8px 0; color:#475569;">🔍 尚未選擇查詢條件</h4>
            <p style="margin:0; font-size:14px;">請於上方選擇縣市、鄉鎮，或直接輸入溪流編號／村里名稱以調閱歷史調查資料。</p>
        </div>
        """, unsafe_allow_html=True)
    elif filtered_df.empty:
        st.warning("⚠️ 查無符合條件之溪流調查資料。")
    else:
        # 依 stream_id 進行整併聚合
        grouped_streams = {}
        for idx, r in filtered_df.iterrows():
            sid = str(r["stream_id"]).strip() if pd.notna(r["stream_id"]) and str(r["stream_id"]).strip() else f"{r.get('county','')}{r.get('township','')}未編號"
            
            if sid not in grouped_streams:
                v_list = json.loads(r["villages"]) if r.get("villages") and str(r["villages"]).startswith("[") else []
                grouped_streams[sid] = {
                    "stream_id": sid,
                    "county": r.get("county") or "",
                    "township": r.get("township") or "",
                    "villages": set(v_list),
                    "adjustments": r.get("demarcation_adjustments") or "無調整紀錄",
                    "risk_history": [],
                    "disasters": [],
                    "seen_disaster_keys": set(),
                    "report_count": 0
                }
            else:
                if r.get("villages") and str(r["villages"]).startswith("["):
                    grouped_streams[sid]["villages"].update(json.loads(r["villages"]))
                curr_adj = r.get("demarcation_adjustments") or ""
                if len(curr_adj) > len(grouped_streams[sid]["adjustments"]):
                    grouped_streams[sid]["adjustments"] = curr_adj

            grouped_streams[sid]["report_count"] += 1

            # 讀取風險等級歷程
            if not grouped_streams[sid]["risk_history"] and r.get("risk_history"):
                try:
                    if str(r["risk_history"]).startswith("["):
                        grouped_streams[sid]["risk_history"] = json.loads(r["risk_history"])
                except Exception:
                    pass

            # 跨年度重大災害事件自動去重彙整
            if r.get("disaster_history") and str(r["disaster_history"]).startswith("["):
                try:
                    h_list = json.loads(r["disaster_history"])
                    for d in h_list:
                        d_key = f"{d.get('year')}_{d.get('scale_and_damage') or d.get('description')}"
                        if d_key not in grouped_streams[sid]["seen_disaster_keys"]:
                            grouped_streams[sid]["seen_disaster_keys"].add(d_key)
                            grouped_streams[sid]["disasters"].append(d)
                except Exception:
                    pass

        # 呈現溪流卡片清單
        st.caption(f"📌 共涵蓋 **{len(grouped_streams)}** 條土石流潛勢溪流")
        for sid, info in grouped_streams.items():
            cty = info["county"]
            twn = info["township"]
            v_str = "、".join(sorted(info["villages"])) if info["villages"] else "未載明村里"
            adj = info["adjustments"]
            r_history = info["risk_history"]
            h_list = info["disasters"]
            rep_cnt = info["report_count"]
            
            with st.expander(f"📌 【{sid}】 {cty} {twn}（{v_str}） ｜ 歷年報告：{rep_cnt} 份", expanded=(len(grouped_streams) == 1)):
                # 1. 劃設調整沿革
                st.markdown(f"**📐 劃設調整沿革**：\n\n{adj}")
                
                st.markdown("<hr style='margin:10px 0; border:0; border-top:1px dashed #CBD5E1;'>", unsafe_allow_html=True)
                
                # 2. 歷年風險評估等級歷程 (置於劃設調整沿革正下方，由最新至最舊依序條列)
                st.markdown("**📊 歷年風險評估等級歷程**：")
                if r_history:
                    # 依年份由新至舊 (降冪) 排序
                    sorted_r_history = sorted(r_history, key=lambda x: x.get("year", 0), reverse=True)
                    
                    # 建立色彩標籤
                    def get_risk_badge(r_val):
                        if "高" in r_val:
                            return f"<span style='background-color:#FEE2E2; color:#991B1B; font-weight:bold; padding:2px 8px; border-radius:4px;'>{r_val}</span>"
                        elif "中" in r_val:
                            return f"<span style='background-color:#FEF3C7; color:#92400E; font-weight:bold; padding:2px 8px; border-radius:4px;'>{r_val}</span>"
                        elif "低" in r_val:
                            return f"<span style='background-color:#DCFCE7; color:#166534; font-weight:bold; padding:2px 8px; border-radius:4px;'>{r_val}</span>"
                        else:
                            return f"<span style='background-color:#F1F5F9; color:#475569; padding:2px 8px; border-radius:4px;'>{r_val}</span>"

                    risk_items_html = " &nbsp; | &nbsp; ".join([
                        f"<b>[{item.get('year')}]</b> {get_risk_badge(item.get('risk'))}" 
                        for item in sorted_r_history
                    ])
                    st.markdown(f"""
                    <div style="background-color:#F8FAFC; border:1px solid #E2E8F0; padding:10px 14px; border-radius:6px; line-height:2.0; font-size:13px;">
                        {risk_items_html}
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.markdown("<span style='color:#94A3B8; font-size:13px;'>• 尚無 2010～2026 公告風險等級紀錄</span>", unsafe_allow_html=True)

                st.markdown("<hr style='margin:10px 0; border:0; border-top:1px dashed #CBD5E1;'>", unsafe_allow_html=True)

                # 3. 歷年重大災害情勢
                if h_list:
                    st.markdown("**🕒 歷年重大災害情勢（彙整歷年調查紀錄）**：")
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
# TAB 2: 調查報告 (依年度新至舊排序 + 15分鐘安全 PDF 下載)
# =============================================================
with tab2:
    if not has_filter:
        st.markdown("""
        <div class="empty-state">
            <h4 style="margin:0 0 8px 0; color:#475569;">📄 尚未選擇查詢條件</h4>
            <p style="margin:0; font-size:14px;">請於上方設定條件，系統將依年度由新至舊列出調查報告 PDF 與下載連結。</p>
        </div>
        """, unsafe_allow_html=True)
    elif filtered_df.empty:
        st.warning("⚠️ 查無符合條件之調查報告。")
    else:
        # 解析報告年份
        def parse_report_year(fn):
            m = re.search(r"^(19\d\d|20\d\d)", str(fn))
            return int(m.group(1)) if m else 0

        df_reports = filtered_df.copy()
        df_reports["report_year"] = df_reports["file_name"].apply(parse_report_year)
        # 依年度新至舊 (降冪)、溪流編號 (升冪) 排序
        df_reports = df_reports.sort_values(by=["report_year", "stream_id"], ascending=[False, True])

        st.caption(f"📚 共找到 **{len(df_reports)}** 份相關調查報告（依年度由新至舊排列）")

        for idx, r in df_reports.iterrows():
            yr = r["report_year"]
            yr_display = f"{yr} 年" if yr > 0 else "調查年份未標明"
            fname = r["file_name"]
            sid = r["stream_id"] or "未知編號"
            s_grp = r["storage_group"]
            
            # 取得 R2 下載網址與錯誤診斷
            dl_url, err_msg = get_r2_download_url(fname, s_grp)
            
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
                    st.button("⚠️ 無連結", disabled=True, key=f"btn_dis_{idx}", help=err_msg)
                    if err_msg:
                        st.caption(f"<span style='color:#DC2626;font-size:11px;'>{err_msg}</span>", unsafe_allow_html=True)
            st.markdown("<hr style='margin:8px 0; border:0; border-top:1px dashed #E2E8F0;'>", unsafe_allow_html=True)
            
# =============================================================
# TAB 3: AI 智慧決策摘要
# =============================================================
with tab3:
    if not has_filter:
        st.markdown("""
        <div class="empty-state">
            <h4 style="margin:0 0 8px 0; color:#475569;">🤖 尚未鎖定分析對象</h4>
            <p style="margin:0; font-size:14px;">請於上方篩選特定區域或溪流後，點擊按鈕即可調用 Gemini AI 進行防救災與治理決策綜整。</p>
        </div>
        """, unsafe_allow_html=True)
    elif filtered_df.empty:
        st.warning("⚠️ 目前條件下無任何溪流資料可供分析。")
    else:
        st.markdown(f"##### 🤖 當前篩選範圍（共 {len(filtered_df)} 筆）之 AI 決策綜整")
        if st.button("✨ 產生防救災與治理建議綜整報告", type="primary", use_container_width=True):
            if not GEMINI_API_KEY:
                st.error("❌ 未設定 GEMINI_API_KEY，請至 secrets.toml 設定。")
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
# 8. 頁尾極簡狀態列 (不起眼顯示於最下方)
# -------------------------------------------------------------
st.markdown("<br><hr style='margin: 20px 0 8px 0; border:0; border-top:1px solid #F1F5F9;'>", unsafe_allow_html=True)
st.caption("資料來源：農業部農村發展及水土保持署 ｜ 協力單位：財團法人中興工程顧問社")
