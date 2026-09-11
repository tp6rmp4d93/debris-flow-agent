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
from google import genai
from google.genai import types
from google.genai.errors import APIError
from google.api_core.exceptions import ResourceExhausted

# 導入雨量 AI Agent 核心模組
from agent_core import DebrisRainfallAgentCore

# -------------------------------------------------------------
# 1. 頁面配置與行動裝置響應式樣式
# -------------------------------------------------------------
st.set_page_config(
    page_title="土石流潛勢溪流調查與雨量決策Agent",
    page_icon="⛰️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 隱藏側邊欄與極簡樣式排版
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
        margin-bottom: 2px;
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
        return str(st.secrets[key]).strip()
    return os.getenv(key, default)

TURSO_URL = get_secret("TURSO_DATABASE_URL")
TURSO_TOKEN = get_secret("TURSO_AUTH_TOKEN")
GEMINI_API_KEY = get_secret("GEMINI_API_KEY")

# 初始化雨量 AI 專家引擎 (快取化)
@st.cache_resource
def load_rainfall_agent():
    return DebrisRainfallAgentCore()

rain_agent = load_rainfall_agent()

# -------------------------------------------------------------
# 3. R2 智慧年份分群與預簽名安全下載
# -------------------------------------------------------------
r2_clients_cache = {}

def determine_storage_group(file_name, storage_group) -> str:
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
    if not file_name or pd.isna(file_name):
        return "", "檔名為空"
    fn = str(file_name).strip()
    if not fn or fn.lower() in ["none", "nan", "null"]:
        return "", "無效檔名"

    grp = determine_storage_group(fn, storage_group)
    account_id, access_key, secret_key, bucket_name = None, None, None, None

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

    if not all([account_id, access_key, secret_key, bucket_name]):
        account_id = account_id or get_secret(f"{grp}_ACCOUNT_ID") or get_secret("R2_ACCOUNT_ID")
        access_key = access_key or get_secret(f"{grp}_ACCESS_KEY") or get_secret("R2_ACCESS_KEY")
        secret_key = secret_key or get_secret(f"{grp}_SECRET_KEY") or get_secret("R2_SECRET_KEY")
        bucket_name = bucket_name or get_secret(f"{grp}_BUCKET") or get_secret("R2_BUCKET")

    if not bucket_name:
        bucket_name = "debris-reports-2011-2015" if grp == "R2_GRP_3" else "debris-reports-2007"

    if not all([account_id, access_key, secret_key, bucket_name]):
        return "", f"Secrets 缺少群組【{grp}】設定"

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
            Params={"Bucket": str(bucket_name).strip(), "Key": fn, "ResponseContentDisposition": disposition},
            ExpiresIn=900
        )
        return url, ""
    except Exception as e:
        return "", f"R2 簽名失敗: {str(e)}"

# -------------------------------------------------------------
# 4. Turso 資料庫載入與快取
# -------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="正在自雲端資料庫載入潛勢溪流清冊...")
def load_all_streams_data():
    if not TURSO_URL or not TURSO_TOKEN:
        st.error("❌ 缺少 Turso 資料庫連線設定")
        return pd.DataFrame()

    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN.strip()}", "Content-Type": "application/json"}
    sql = "SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group, risk_history FROM streams;"
    payload = {"requests": [{"type": "execute", "stmt": {"sql": sql}}, {"type": "close"}]}
    
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()["results"][0]["response"]["result"]
        cols = [c["name"] for c in result.get("cols", [])]
        rows = [[c.get("value") for c in r] for r in result.get("rows", [])]
        if not rows:
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame(rows, columns=cols)
        for col in ["stream_id", "county", "township", "villages", "disaster_history", "demarcation_adjustments", "file_name", "storage_group", "risk_history"]:
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str)
        return df
    except Exception as e:
        st.error(f"❌ Turso 資料庫連線失敗: {e}")
        return pd.DataFrame()

# -------------------------------------------------------------
# 5. Gemini AI 智慧決策摘要
# -------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def generate_ai_summary(stream_data_json_str: str) -> str:
    if not GEMINI_API_KEY:
        return "⚠️ 未設定 GEMINI_API_KEY。"
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
你是一名資深土石流防災與水土保持工程專家。請根據以下土石流潛勢溪流調查數據，產出一份專業決策綜整報告：
{stream_data_json_str}
請依 Markdown 結構輸出（繁體中文）：
### 📌 溪流基本特性與風險態勢演變
### 📐 劃設調整與現地評估沿革分析
### 🚨 歷史致災情勢與降雨臨界關聯
### 💡 後續防減災與巡勘治理具體建議
"""
    models_to_try = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.0-flash"]
    for m in models_to_try:
        try:
            response = client.models.generate_content(model=m, contents=prompt, config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2500))
            return response.text
        except:
            continue
    return "⏳ API 伺服器忙碌中，請稍後再試。"

# -------------------------------------------------------------
# 6. 主頁面與頂部條件篩選
# -------------------------------------------------------------
st.markdown('<div class="main-title">⛰️ 土石流潛勢溪流調查與雨量決策平台</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">歷史報告｜劃設沿革｜歷年風險等級｜🌧️ 歷年雨量分析｜AI 決策綜整</div>', unsafe_allow_html=True)

df_turso = load_all_streams_data()
if df_turso.empty:
    st.info("💡 資料庫目前無資料。")
    st.stop()

with st.container():
    st.markdown('<div class="filter-box">', unsafe_allow_html=True)
    c_county, c_township, c_search = st.columns([1, 1, 2])
    
    all_counties = ["選擇縣市"] + sorted([c for c in df_turso["county"].dropna().unique() if c])
    with c_county:
        sel_county = st.selectbox("所屬縣市", all_counties, label_visibility="collapsed")
    
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
            mask = (
                filtered_df["stream_id"].astype(str).str.contains(pat, case=False, na=False) |
                filtered_df["file_name"].astype(str).str.contains(pat, case=False, na=False) |
                filtered_df["county"].astype(str).str.contains(pat, case=False, na=False) |
                filtered_df["township"].astype(str).str.contains(pat, case=False, na=False) |
                filtered_df["villages"].astype(str).str.contains(pat, case=False, na=False)
            )
            filtered_df = filtered_df[mask]
    st.markdown('</div>', unsafe_allow_html=True)

if has_filter:
    st.caption(f"📊 篩選結果：共 **{len(filtered_df):,}** 筆調查紀錄")
else:
    st.caption(f"📊 資料庫就緒（全台共 {len(df_turso):,} 筆紀錄），請設定上方條件開始檢索。")

grouped_streams = {}
if has_filter and not filtered_df.empty:
    for idx, r in filtered_df.iterrows():
        sid = str(r["stream_id"]).strip() if pd.notna(r["stream_id"]) and str(r["stream_id"]).strip() else f"{r.get('county','')}{r.get('township','')}未編號"
        if sid not in grouped_streams:
            v_list = json.loads(r["villages"]) if r.get("villages") and str(r["villages"]).startswith("[") else []
            grouped_streams[sid] = {
                "stream_id": sid, "county": r.get("county") or "", "township": r.get("township") or "",
                "villages": set(v_list), "adjustments": r.get("demarcation_adjustments") or "無調整紀錄",
                "risk_history": [], "disasters": [], "seen_disaster_keys": set(), "report_count": 0
            }
        else:
            if r.get("villages") and str(r["villages"]).startswith("["):
                grouped_streams[sid]["villages"].update(json.loads(r["villages"]))
            curr_adj = r.get("demarcation_adjustments") or ""
            if len(curr_adj) > len(grouped_streams[sid]["adjustments"]):
                grouped_streams[sid]["adjustments"] = curr_adj
        grouped_streams[sid]["report_count"] += 1

        if not grouped_streams[sid]["risk_history"] and r.get("risk_history"):
            try:
                if str(r["risk_history"]).startswith("["):
                    grouped_streams[sid]["risk_history"] = json.loads(r["risk_history"])
            except: pass

        if r.get("disaster_history") and str(r["disaster_history"]).startswith("["):
            try:
                for d in json.loads(r["disaster_history"]):
                    d_key = f"{d.get('year')}_{d.get('scale_and_damage') or d.get('description')}"
                    if d_key not in grouped_streams[sid]["seen_disaster_keys"]:
                        grouped_streams[sid]["seen_disaster_keys"].add(d_key)
                        grouped_streams[sid]["disasters"].append(d)
            except: pass

# -------------------------------------------------------------
# 7. 四大功能分頁 (新增 Tab 3 歷史雨量檢索)
# -------------------------------------------------------------
tab1, tab2, tab3, tab4 = st.tabs([
    "📋 調查資料 (沿革與災情)",
    "📄 調查報告 (歷年 PDF)",
    "🌧️ 歷史雨量分析",
    "🤖 AI 智慧決策摘要"
])

# =============================================================
# TAB 1: 調查資料 (完整保留原有沿革與災情，並擴充內嵌雨量資訊)
# =============================================================
with tab1:
    if not has_filter:
        st.markdown('<div class="empty-state"><h4>🔍 尚未選擇查詢條件</h4><p>請於上方選擇縣市、鄉鎮或輸入溪流編號。</p></div>', unsafe_allow_html=True)
    elif filtered_df.empty:
        st.warning("⚠️ 查無符合條件之溪流調查資料。")
    else:
        st.caption(f"📌 共涵蓋 **{len(grouped_streams)}** 條土石流潛勢溪流")
        for sid, info in grouped_streams.items():
            cty, twn = info["county"], info["township"]
            v_str = "、".join(sorted(info["villages"])) if info["villages"] else "未載明村里"
            with st.expander(f"📌 【{sid}】 {cty} {twn}（{v_str}） ｜ 歷年報告：{info['report_count']} 份", expanded=(len(grouped_streams) == 1)):
                # 1. 劃設調整沿革
                st.markdown(f"**📐 劃設調整沿革**：\n\n{info['adjustments']}")
                st.markdown("<hr style='margin:10px 0; border:0; border-top:1px dashed #CBD5E1;'>", unsafe_allow_html=True)
                
                # 2. 歷年風險等級異動歷程
                st.markdown("**📊 歷年風險評估等級異動歷程**：")
                if info["risk_history"]:
                    sorted_asc = sorted(info["risk_history"], key=lambda x: x.get("year", 0))
                    change_records, prev_risk = [], None
                    for item in sorted_asc:
                        y = item.get("year")
                        r_val = str(item.get("risk", "")).strip()
                        if r_val and r_val.lower() not in ['nan', 'none', 'null', '']:
                            if prev_risk is None:
                                change_records.append({"year": y, "risk": r_val, "status": "首次公告劃設"})
                                prev_risk = r_val
                            elif r_val != prev_risk:
                                change_records.append({"year": y, "risk": r_val, "status": "等級調整"})
                                prev_risk = r_val
                    sorted_changes = sorted(change_records, key=lambda x: x["year"], reverse=True)
                    line_items = []
                    for idx, item in enumerate(sorted_changes):
                        y, r_name, status = item["year"], item["risk"], item["status"]
                        prefix = "🔸 <b>最近一次調整</b>：" if (idx == 0 and len(sorted_changes) > 1) else "🔹 "
                        line_items.append(f"<div style='margin-bottom: 6px;'>{prefix}<b>[{y}年]</b> <span style='background:#E2E8F0; padding:2px 6px; border-radius:4px;'>{r_name}</span> （{status}）</div>")
                    st.markdown(f"<div style='background:#F8FAFC; border:1px solid #E2E8F0; padding:10px 14px; border-radius:6px; font-size:13px;'>{''.join(line_items)}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<span style='color:#94A3B8; font-size:13px;'>• 尚無公告風險等級紀錄</span>", unsafe_allow_html=True)

                # 3. 歷年重大災害情勢
                if info["disasters"]:
                    st.markdown("<br>**🕒 歷年重大災害情勢**：", unsafe_allow_html=True)
                    for d in info["disasters"]:
                        yr = d.get("year", "歷史災害")
                        rf = d.get("rainfall_info", "")
                        dmg = d.get("scale_and_damage") or d.get("description", "無詳細說明")
                        rf_badge = f"<span style='color:#2563EB;font-size:12px;margin-left:8px;'>🌧️ 雨量：{rf}</span>" if (rf and rf != "未載明") else ""
                        st.markdown(f'<div class="disaster-badge"><b>🚨 {yr}</b>{rf_badge}<br><span style="color:#334155;">{dmg}</span></div>', unsafe_allow_html=True)

                # 4. 【新增】將歷史雨量資訊直接內嵌至查詢結果中
                st.markdown("<hr style='margin:12px 0; border:0; border-top:1px solid #CBD5E1;'>", unsafe_allow_html=True)
                st.markdown("**🌧️ 水利署歷史極端雨量與空間回退統計**：")
                try:
                    rain_md = rain_agent.execute_query(sid)
                    
                    # 透過 Streamlit 原生 container 呈現，外觀乾淨且 100% 完美解析 Markdown
                    with st.container(border=True):
                        st.markdown(rain_md)
                except Exception as e:
                    st.caption(f"*(目前無對應的雨量站數據: {e})*")

# =============================================================
# TAB 2: 調查報告 (歷年 PDF)
# =============================================================
with tab2:
    if not has_filter or filtered_df.empty:
        st.markdown('<div class="empty-state"><h4>📄 尚未選擇查詢條件</h4><p>請於上方設定條件以檢索 PDF 報告。</p></div>', unsafe_allow_html=True)
    else:
        def parse_report_year(fn):
            m = re.search(r"^(19\d\d|20\d\d)", str(fn))
            return int(m.group(1)) if m else 0
        df_reports = filtered_df.copy()
        df_reports["report_year"] = df_reports["file_name"].apply(parse_report_year)
        df_reports = df_reports.sort_values(by=["report_year", "stream_id"], ascending=[False, True])
        for idx, r in df_reports.iterrows():
            yr = r["report_year"]
            fname, sid, s_grp = r["file_name"], r["stream_id"] or "未知", r["storage_group"]
            dl_url, err_msg = get_r2_download_url(fname, s_grp)
            c_info, c_btn = st.columns([3, 1])
            with c_info:
                st.markdown(f'<span class="year-tag">📅 {yr if yr>0 else "未標明"}</span> <b style="color:#1E293B;">【{sid}】</b> <span style="color:#64748B;font-size:13px;">{fname}</span>', unsafe_allow_html=True)
            with c_btn:
                if dl_url: st.link_button("⬇️ 下載 PDF", dl_url, type="primary")
                else: st.button("⚠️ 無連結", disabled=True, key=f"btn_dis_{idx}", help=err_msg)
            st.markdown("<hr style='margin:8px 0; border:0; border-top:1px dashed #E2E8F0;'>", unsafe_allow_html=True)

# =============================================================
# TAB 3: 歷史雨量分析 (智慧聯想帶入篩選溪流)
# =============================================================
with tab3:
    st.markdown("#### 🌧️ 土石流潛勢溪流歷年極端降雨與事件雨量智慧檢索")
    st.markdown("系統會**自動依上方設定的縣市、鄉鎮或關鍵字篩選條件**帶入對應的潛勢溪流清單。只需直接選擇溪流並選填歷史事件即可快速檢索[cite: 3, 4]。")
    
    # 自動抓取當前篩選條件下的溪流編號清單（若未篩選則預設全台清單）
    if grouped_streams:
        available_streams = sorted(list(grouped_streams.keys()))
        selection_hint = f"（已自動依上方篩選條件鎖定 {len(available_streams)} 條溪流）"
    else:
        available_streams = sorted(rain_agent.df_debris['DebrisNO'].dropna().unique().tolist())
        selection_hint = "（目前為全台潛勢溪流清單，建議可透過上方篩選縮小範圍）"

    st.caption(f"📌 可選溪流範圍 {selection_hint}")

    r_col1, r_col2, r_col3 = st.columns([2, 1, 1])
    with r_col1:
        # 自動帶入目前篩選出的第一條溪流作為預設值
        selected_stream = st.selectbox("選擇潛勢溪流編號", available_streams, key="rain_stream_select")
    with r_col2:
        event_keyword_input = st.text_input("選填歷史事件關鍵字", placeholder="例如: 莫拉克、山陀兒、T2418", key="rain_event_input")
    with r_col3:
        st.markdown("<br>", unsafe_allow_html=True)
        rain_submit = st.button("🔍 執行雨量檢索", type="primary", key="rain_btn")

    # 當使用者點擊檢索或切換時自動帶入執行
    if rain_submit and selected_stream:
        with st.spinner(f"正在對應 `{selected_stream}` 之參考雨量站與水利署歷史雨量資料庫中..."):
            e_kw = event_keyword_input.strip() if event_keyword_input else None
            
            # 執行雨量與空間回退查詢
            df_rain_result = rain_agent.execute_query(selected_stream, e_kw)
            
            if isinstance(df_rain_result, str):
                st.error(df_rain_result)
            else:
                st.markdown(f"### 📍 查詢結果：`{selected_stream}`")
                st.dataframe(df_rain_result, use_container_width=True, hide_index=True)

# =============================================================
# TAB 4: AI 智慧決策摘要
# =============================================================
with tab4:
    if not has_filter or filtered_df.empty:
        st.markdown('<div class="empty-state"><h4>🤖 尚未選擇分析對象</h4><p>請於上方設定條件篩選溪流。</p></div>', unsafe_allow_html=True)
    else:
        st.markdown("#### 🧠 當前範圍之土石流潛勢溪流 AI 專家決策綜整")
        summary_payload = []
        for sid, info in grouped_streams.items():
            summary_payload.append({
                "溪流編號": sid, "行政區": f"{info['county']}{info['township']}", "涵蓋村里": list(info["villages"]),
                "劃設調整沿革": info["adjustments"], "歷年風險等級歷程(2010-2026)": info.get("risk_history", []),
                "歷年重大災害紀錄": info["disasters"]
            })
        payload_json_str = json.dumps(summary_payload, ensure_ascii=False)
        if st.button("✨ 立即生成專家決策綜整報告", type="primary"):
            with st.spinner("🚀 Gemini 3.7 Flash 正在比對歷年沿革、風險等級與致災歷史..."):
                report_markdown = generate_ai_summary(payload_json_str)
                st.markdown(report_markdown)

# -------------------------------------------------------------
# 8. 頁尾極簡狀態列
# -------------------------------------------------------------
st.markdown("<br><hr style='margin: 24px 0 12px 0; border:0; border-top:1px solid #E2E8F0;'>", unsafe_allow_html=True)
st.markdown("""
<div style="text-align: center; color: #64748B; font-size: 12px; line-height: 1.8;">
    <div>資料來源：農業部農村發展及水土保持署 ｜ 水利署歷史雨量資料庫</div>
    <div>協力單位：財團法人中興工程顧問社</div>
</div>
""", unsafe_allow_html=True)
