import os
import json
import re
from collections import defaultdict
from urllib.parse import quote
import requests
import boto3
from botocore.config import Config
from fastapi import FastAPI, Request, HTTPException, Header, Response, status
from fastapi.responses import RedirectResponse
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

# 導入雨量與空間回退 AI Agent 核心
from agent_core import DebrisRainfallAgentCore

# -------------------------------------------------------------
# 1. 服務初始化與環境變數設定
# -------------------------------------------------------------
app = FastAPI(title="Debris Flow LineBot Agent with Rainfall")

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://debris-flow-linebot.onrender.com")
ADMIN_LINE_USER_ID = os.getenv("ADMIN_LINE_USER_ID", "")  # 維護人員 LINE ID

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("⚠️ 警告: 請設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET 環境變數。")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
r2_clients_cache = {}

# 初始化雨量 Agent 核心
rain_agent = DebrisRainfallAgentCore()

# 快取 1753 條溪流基本屬性 (若本機存在 Excel 則載入加速反查)
debris_meta_cache = {}
excel_file = "Debris_1753條_屬性資料庫_20251230.xlsx"
if os.path.exists(excel_file):
    try:
        import pandas as pd
        df_meta = pd.read_excel(excel_file, sheet_name=0)
        for _, row in df_meta.iterrows():
            d_no = str(row["Debrisno"]).strip()
            d_old = str(row["Dbno_old"]).strip() if pd.notna(row["Dbno_old"]) else ""
            c = str(row["County01"]).strip() if pd.notna(row["County01"]) else ""
            t = str(row["Town01"]).strip() if pd.notna(row["Town01"]) else ""
            v = str(row["Vill01"]).strip() if pd.notna(row["Vill01"]) else ""
            info = {"debrisno": d_no, "dbno_old": d_old, "county": c, "town": t, "village": v}
            debris_meta_cache[d_no] = info
            if d_old:
                debris_meta_cache[d_old] = info
        print(f"✅ 成功預載入 {len(df_meta)} 筆溪流屬性對照庫")
    except Exception as e:
        print(f"ℹ️ 讀取屬性 Excel 失敗，將純以雨量 Agent 動態反查: {e}")

# -------------------------------------------------------------
# 2. R2 智慧年份分群預簽名與短網址重新導向
# -------------------------------------------------------------
def parse_report_year(file_name: str) -> int:
    match = re.search(r"^(19\d\d|20\d\d)", str(file_name or ""))
    return int(match.group(1)) if match else 0

def determine_storage_group(file_name: str, storage_group: str) -> str:
    year = parse_report_year(file_name)
    if year > 0:
        if year <= 2007: return "R2_GRP_1"
        elif 2008 <= year <= 2010: return "R2_GRP_2"
        elif 2011 <= year <= 2015: return "R2_GRP_3"
        elif 2016 <= year <= 2020: return "R2_GRP_4"
        else: return "R2_GRP_5"
    if storage_group and storage_group.strip():
        s = storage_group.strip()
        if s.lower() not in ["none", "nan", "null"]: return s
    return "R2_GRP_3"

def get_r2_download_url(file_name: str, storage_group: str) -> str:
    if not file_name: return ""
    fn = str(file_name).strip()
    grp = determine_storage_group(fn, storage_group)
    account_id = os.getenv(f"{grp}_ACCOUNT_ID") or os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv(f"{grp}_ACCESS_KEY") or os.getenv("R2_ACCESS_KEY")
    secret_key = os.getenv(f"{grp}_SECRET_KEY") or os.getenv("R2_SECRET_KEY")
    bucket_name = os.getenv(f"{grp}_BUCKET") or os.getenv("R2_BUCKET")
    if not all([account_id, access_key, secret_key, bucket_name]): return ""
    try:
        cache_key = f"{grp}_{account_id}"
        if cache_key not in r2_clients_cache:
            r2_clients_cache[cache_key] = boto3.client(
                "s3", endpoint_url=f"https://{str(account_id).strip()}.r2.cloudflarestorage.com",
                aws_access_key_id=str(access_key).strip(), aws_secret_access_key=str(secret_key).strip(),
                region_name="auto", config=Config(signature_version="s3v4")
            )
        s3 = r2_clients_cache[cache_key]
        encoded_fn = quote(fn)
        disposition = f"attachment; filename*=UTF-8''{encoded_fn}"
        return s3.generate_presigned_url(ClientMethod="get_object", Params={"Bucket": str(bucket_name).strip(), "Key": fn, "ResponseContentDisposition": disposition}, ExpiresIn=900)
    except Exception as e:
        print(f"❌ R2 預簽名生成異常 ({fn}): {e}")
        return ""

@app.get("/download")
def redirect_to_r2(file: str, group: str = "R2_GRP_3"):
    dl_url = get_r2_download_url(file, group)
    if dl_url:
        return RedirectResponse(url=dl_url)
    return {"error": "File not found or expired"}, 404

# -------------------------------------------------------------
# 3. 行政區改制反查與 Turso 資料庫查詢邏輯
# -------------------------------------------------------------
def parse_user_input(user_input: str):
    tokens = [t.strip() for t in re.split(r"[\s,]+", user_input.strip()) if t.strip()]
    if not tokens:
        return "", None

    id_pattern = r"([A-Za-z\u4e00-\u9fa5]{1,4}[A-Za-z]{1,2}\d{2,4}(?:-\d+)?)"
    main_term = tokens[0]
    event_kw = " ".join(tokens[1:]) if len(tokens) > 1 else None

    for i, t in enumerate(tokens):
        if re.search(id_pattern, t):
            main_term = t
            other_tokens = [tokens[j] for j in range(len(tokens)) if j != i]
            event_kw = " ".join(other_tokens) if other_tokens else None
            break

    return main_term, event_kw

def convert_stream_id_prefix(stream_id: str) -> str:
    conversions = {
        "南市": "南縣", "中市": "中縣", "高市": "高縣", "新北": "北縣", "桃市": "桃縣",
        "南縣": "南市", "中縣": "中市", "高縣": "高市", "北縣": "新北", "桃縣": "桃市"
    }
    for k, v in conversions.items():
        if stream_id.startswith(k):
            return stream_id.replace(k, v, 1)
    return ""

def extract_location_from_text(text: str):
    if not text:
        return None, None, None
    pattern = r"((?:臺|台)?(?:北市|中市|南市|高市|新北|桃園|新竹|苗栗|彰化|南投|雲林|嘉義|屏東|宜蘭|花蓮|臺東|台東|澎湖|金門|連江|臺北|台北|臺中|台中|臺南|台南|高雄|基隆)[縣市]?)\s*([\u4e00-\u9fa5]{1,4}?(?:鄉|鎮|市|區))\s*([\u4e00-\u9fa5]{1,4}?(?:村|里))?"
    m = re.search(pattern, text)
    if m:
        return m.group(1), m.group(2), m.group(3) or ""
    return None, None, None

def query_turso_db(keyword: str):
    if not TURSO_URL or not TURSO_TOKEN or not keyword: return []
    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN.strip()}", "Content-Type": "application/json"}
    pat = f"%{keyword.strip()}%"
    sql = """
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group, risk_history, dbno_old 
        FROM streams 
        WHERE stream_id LIKE ? OR dbno_old LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ? OR file_name LIKE ? 
        ORDER BY file_name DESC LIMIT 15
    """
    payload = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": [{"type": "text", "value": pat}] * 6}}, {"type": "close"}]}
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=8)
        resp.raise_for_status()
        return [[col.get("value") for col in r] for r in resp.json()["results"][0]["response"]["result"].get("rows", [])]
    except Exception as e:
        print(f"❌ Turso 資料庫查詢失敗: {e}")
        return []

def query_turso_by_admin(town_base: str, village_base: str = ""):
    if not TURSO_URL or not TURSO_TOKEN or not town_base: return []
    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN.strip()}", "Content-Type": "application/json"}
    
    if village_base:
        sql = """
            SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group, risk_history, dbno_old 
            FROM streams 
            WHERE township LIKE ? AND (villages LIKE ? OR file_name LIKE ?)
            ORDER BY file_name DESC LIMIT 15
        """
        args = [
            {"type": "text", "value": f"%{town_base}%"},
            {"type": "text", "value": f"%{village_base}%"},
            {"type": "text", "value": f"%{village_base}%"}
        ]
    else:
        sql = """
            SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group, risk_history, dbno_old 
            FROM streams 
            WHERE township LIKE ?
            ORDER BY file_name DESC LIMIT 15
        """
        args = [{"type": "text", "value": f"%{town_base}%"}]

    payload = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": args}}, {"type": "close"}]}
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=8)
        resp.raise_for_status()
        return [[col.get("value") for col in r] for r in resp.json()["results"][0]["response"]["result"].get("rows", [])]
    except Exception as e:
        print(f"❌ Turso 二階段歷史行政區查詢失敗: {e}")
        return []

# -------------------------------------------------------------
# 4. Flex Message 視覺卡片構建
# -------------------------------------------------------------
def build_risk_history_boxes(group_records: list):
    for rec in group_records:
        raw = rec.get("risk_history")
        if raw and str(raw).strip().startswith("[") and str(raw).strip() != "[]":
            try:
                r_list = json.loads(str(raw).strip())
                sorted_asc = sorted(r_list, key=lambda x: x.get("year", 0))
                change_records, prev_risk = [], None
                for item in sorted_asc:
                    y, r_val = item.get("year"), str(item.get("risk", "")).strip()
                    if r_val and r_val.lower() not in ['nan', 'none', 'null', '']:
                        if prev_risk is None:
                            change_records.append({"year": y, "risk": r_val, "status": "首次公告劃設"})
                            prev_risk = r_val
                        elif r_val != prev_risk:
                            change_records.append({"year": y, "risk": r_val, "status": "等級調整"})
                            prev_risk = r_val
                sorted_desc = sorted(change_records, key=lambda x: x["year"], reverse=True)
                risk_boxes = []
                for idx, item in enumerate(sorted_desc):
                    y, r_name, status = item["year"], item["risk"], item["status"]
                    bg_color, text_color = ("#FEE2E2", "#991B1B") if "高" in r_name else ("#FEF3C7", "#92400E") if "中" in r_name else ("#DCFCE7", "#166534") if "低" in r_name else ("#F3F4F6", "#374151")
                    prefix = "🔸 [現況] " if idx == 0 and len(sorted_desc) > 1 else "🔹 "
                    risk_boxes.append({
                        "type": "box", "layout": "horizontal", "alignItems": "center", "margin": "xs",
                        "contents": [
                            {"type": "text", "text": f"{prefix}{y}年", "size": "xs", "weight": "bold", "color": "#1F2937", "flex": 4},
                            {"type": "box", "layout": "vertical", "backgroundColor": bg_color, "cornerRadius": "sm", "paddingStart": "6px", "paddingEnd": "6px", "paddingTop": "2px", "paddingBottom": "2px", "contents": [{"type": "text", "text": r_name, "size": "xxs", "weight": "bold", "color": text_color}]},
                            {"type": "text", "text": f"（{status}）", "size": "xxs", "color": "#6B7280", "margin": "sm", "flex": 4}
                        ]
                    })
                return risk_boxes
            except Exception:
                pass
    return [{"type": "text", "text": "• 尚無 2010～2026 公告風險等級紀錄", "size": "xs", "color": "#9CA3AF"}]

def build_stream_flex_bubble(stream_id: str, group_records: list):
    latest_rec = group_records[0]
    cty, twn = latest_rec.get("county") or "", latest_rec.get("township") or ""
    db_old = latest_rec.get("dbno_old") or ""
    villages = set()
    for rec in group_records:
        for v in rec.get("villages", []): villages.add(v)
    v_str = "、".join(sorted(villages)) if villages else "未標記村里"
    adj = latest_rec.get("adjustments") or "無調整紀錄"
    
    sub_title = f"📍 {cty} {twn}（{v_str}）"
    header_title = stream_id
    if db_old:
        header_title += f" (舊: {db_old})"

    report_buttons = []
    for rec in group_records:
        yr, fname, sgrp = rec.get("year"), rec.get("file_name"), rec.get("storage_group")
        if fname:
            encoded_fn = quote(str(fname))
            encoded_sgrp = quote(determine_storage_group(str(fname), sgrp))
            short_download_url = f"{BASE_URL}/download?file={encoded_fn}&group={encoded_sgrp}"
            
            report_buttons.append({
                "type": "button", 
                "action": {
                    "type": "uri", 
                    "label": f"📄 下載 {yr}年報告" if yr > 0 else "📄 下載報告", 
                    "uri": short_download_url
                }, 
                "style": "primary", 
                "color": "#2563EB" if yr >= 2016 else "#4B5563", 
                "height": "sm", 
                "margin": "xs"
            })

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1E3A8A", "paddingAll": "14px", "contents": [
            {"type": "text", "text": header_title, "weight": "bold", "size": "lg", "color": "#FFFFFF"},
            {"type": "text", "text": sub_title, "size": "xs", "color": "#E0E7FF", "margin": "xs", "wrap": True}
        ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "contents": [
            {"type": "text", "text": "📊 歷年風險等級異動歷程", "weight": "bold", "size": "sm", "color": "#111827"},
            {"type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "cornerRadius": "md", "paddingAll": "8px", "margin": "xs", "contents": build_risk_history_boxes(group_records)},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": "📐 劃設調整沿革", "weight": "bold", "size": "sm", "color": "#111827", "margin": "md"},
            {"type": "text", "text": adj, "size": "xs", "color": "#4B5563", "wrap": True, "margin": "xs"}
        ]}
    }
    if report_buttons:
        bubble["footer"] = {"type": "box", "layout": "vertical", "paddingAll": "12px", "contents": [{"type": "text", "text": "📚 調查報告下載 (15分鐘有效)", "weight": "bold", "size": "xs", "color": "#2563EB"}, *report_buttons]}
    return bubble

# -------------------------------------------------------------
# 5. FastAPI 路由與 Webhook 處理 (雙軌容錯反查 + 防休眠 + 告警)
# -------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    """供 Render 與 UptimeRobot 定時 Ping 喚醒端點 (支援 GET 與 HEAD 杜絕 405)"""
    return {"status": "ok", "service": "Debris Flow LineBot Agent with Rainfall (Active)"}

@app.post("/uptime-alert")
async def uptime_alert(request: Request):
    """接收 UptimeRobot 斷線與復原告警並轉發 LINE Push Message"""
    try:
        data = await request.json()
        monitor_name = data.get("monitorFriendlyName", "土石流 LineBot 服務")
        alert_type = data.get("alertTypeFriendlyName", "狀態警示")
        details = data.get("alertDetails", "")
        
        status_icon = "🚨 【伺服器斷線告警】" if alert_type == "Down" else "✅ 【伺服器已恢復正常】"
        msg_text = f"{status_icon}\n\n📌 監控服務：{monitor_name}\n⚠️ 當前狀態：{alert_type}\n🕒 詳情：{details}\n\n請維護人員留意系統狀態。"

        if ADMIN_LINE_USER_ID and CHANNEL_ACCESS_TOKEN:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=ADMIN_LINE_USER_ID,
                        messages=[TextMessage(text=msg_text)]
                    )
                )
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ 告警推播發送失敗: {e}")
        return {"status": "error"}

@app.post("/callback")
async def handle_callback(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    body = await request.body()
    body_str = body.decode("utf-8")
    
    try:
        events = parser.parse(body_str, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # LINE Verify 測試快速通道 (避免 Timeout)
    if not events:
        return Response(content="OK", status_code=status.HTTP_200_OK)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                user_text = event.message.text.strip()
                main_term, event_kw = parse_user_input(user_text)
                messages_to_reply = []

                try:
                    # 步驟 1: 調查報告庫以輸入字串直查
                    raw_records = query_turso_db(main_term)
                    if not raw_records and main_term != user_text:
                        raw_records = query_turso_db(user_text)

                    # 步驟 2: 若未找到，嘗試前綴改制代碼反查 (例如 南市DF038 -> 南縣DF038)
                    if not raw_records:
                        alt_id = convert_stream_id_prefix(main_term)
                        if alt_id:
                            raw_records = query_turso_db(alt_id)

                    # 步驟 3: 調用雨量 Agent (獲取雨量分析並提取地理位置以供二階段歷史反查)
                    rain_result_md = None
                    debris_no_candidate = main_term
                    if main_term in debris_meta_cache:
                        debris_no_candidate = debris_meta_cache[main_term]["debrisno"]

                    try:
                        rain_result_md = rain_agent.execute_query(debris_no_candidate, event_kw)
                    except Exception as rain_err:
                        print(f"⚠️ 雨量 Agent 執行異常: {rain_err}")

                    # 步驟 4: 若仍無報告，透過雨量結果所得行政區進行改制二階段反查
                    if not raw_records:
                        city, district, village = None, None, None
                        
                        # 優先從屬性快取取得標準行政區
                        if main_term in debris_meta_cache:
                            m_info = debris_meta_cache[main_term]
                            city, district, village = m_info["county"], m_info["town"], m_info["village"]
                        elif rain_result_md:
                            city, district, village = extract_location_from_text(rain_result_md)

                        if district:
                            town_base = re.sub(r"[鄉鎮市區]$", "", district)
                            village_base = re.sub(r"[村里]$", "", village) if village else ""
                            # 以鄉鎮與村里模糊檢索歷史報告
                            raw_records = query_turso_by_admin(town_base, village_base)

                    # 步驟 5: 彙整調查報告回覆
                    if raw_records:
                        grouped_streams = defaultdict(list)
                        for r in raw_records:
                            sid, cty, twn, v_raw, h_raw, adj, fname, s_grp, r_hist, db_old = r if len(r) >= 10 else (*r, "")
                            v_list = json.loads(v_raw) if v_raw and str(v_raw).startswith("[") else []
                            h_list = json.loads(h_raw) if h_raw and str(h_raw).startswith("[") else []
                            yr = parse_report_year(fname)
                            stream_key = sid.strip() if sid else f"{cty}{twn}未編號"
                            grouped_streams[stream_key].append({
                                "stream_id": sid, "county": cty, "township": twn, "villages": v_list,
                                "disaster_history": h_list, "adjustments": adj, "file_name": fname,
                                "storage_group": s_grp, "risk_history": r_hist, "year": yr, "dbno_old": db_old
                            })

                        # 組裝 Flex Message (附純文字自動降級)
                        try:
                            bubbles = []
                            for sid_key, recs in list(grouped_streams.items())[:3]:
                                recs.sort(key=lambda x: x["year"], reverse=True)
                                bubbles.append(build_stream_flex_bubble(sid_key, recs))

                            flex_payload = {"type": "carousel", "contents": bubbles} if len(bubbles) > 1 else bubbles[0]
                            messages_to_reply.append(
                                FlexMessage(
                                    alt_text=f"⛰️ 找到 {len(grouped_streams)} 條相關潛勢溪流調查資料",
                                    contents=FlexContainer.from_json(json.dumps(flex_payload))
                                )
                            )
                        except Exception as flex_err:
                            print(f"⚠️ Flex Message 封裝異常，啟動降級純文字: {flex_err}")
                            fallback_lines = [f"⛰️ 查詢「{user_text}」成果（共 {len(grouped_streams)} 條）：\n"]
                            for s_k, r_list in list(grouped_streams.items())[:3]:
                                first_r = r_list[0]
                                db_old_txt = f" (舊: {first_r.get('dbno_old')})" if first_r.get('dbno_old') else ""
                                fallback_lines.append(f"📌 【{s_k}】{db_old_txt} {first_r['county']}{first_r['township']}")
                                fallback_lines.append(f"• 歷年報告：共 {len(r_list)} 份")
                                fallback_lines.append(f"• 劃設沿革：{first_r['adjustments'][:60]}...\n")
                            messages_to_reply.append(TextMessage(text="\n".join(fallback_lines)))
                    else:
                        # 查無任何調查報告時，依指示輸出明確提示訊息
                        messages_to_reply.append(
                            TextMessage(text="暫無找到所輸入溪流編號，請改以鄉鎮村里查詢!!")
                        )

                    # 步驟 6: 若有查得雨量數據，一併附加回傳
                    if rain_result_md and rain_result_md.strip():
                        messages_to_reply.append(TextMessage(text=rain_result_md))

                except Exception as inner_e:
                    print(f"❌ [LineBot 處理查詢發生錯誤]: {inner_e}")
                    messages_to_reply = [TextMessage(text=f"⚠️ 系統處理發生錯誤：{str(inner_e)}")]

                # 步驟 7: 執行回覆發送
                try:
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=messages_to_reply[:5]
                        )
                    )
                except Exception as reply_e:
                    print(f"❌ [LineBot 回覆發送失敗，嘗試純文字緊急降級]: {reply_e}")
                    try:
                        line_bot_api.reply_message(
                            ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text="⚠️ 查詢成果卡片生成受限，請改以鄉鎮村里或至決策平台查閱。")]
                            )
                        )
                    except Exception as fatal_e:
                        print(f"❌ [緊急降級發送仍失敗]: {fatal_e}")

    return Response(content="OK", status_code=status.HTTP_200_OK)
