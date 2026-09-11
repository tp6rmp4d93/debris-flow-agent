import os
import json
import re
from collections import defaultdict
from urllib.parse import quote
import requests
import boto3
from botocore.config import Config
from fastapi import FastAPI, Request, HTTPException, Header
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

# 導入共用的雨量與空間回退 AI Agent 核心
from agent_core import DebrisRainfallAgentCore

# -------------------------------------------------------------
# 1. 服務初始化與環境變數設定
# -------------------------------------------------------------
app = FastAPI(title="Debris Flow LineBot Agent with Rainfall")

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("⚠️ 警告: 請設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET 環境變數。")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
r2_clients_cache = {}

# 初始化雨量 Agent 核心（自動掃描 wra_rain_db/excel_data/ 中的新舊事件檔案）
rain_agent = DebrisRainfallAgentCore()

# -------------------------------------------------------------
# 2. R2 智慧年份分群預簽名 (PDF 報告下載)
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
        return ""

# -------------------------------------------------------------
# 3. Turso 資料庫查詢 (歷年報告卡片)
# -------------------------------------------------------------
def query_turso_db(keyword: str):
    if not TURSO_URL or not TURSO_TOKEN or not keyword: return []
    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN.strip()}", "Content-Type": "application/json"}
    pat = f"%{keyword.strip()}%"
    sql = "SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group, risk_history FROM streams WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ? OR file_name LIKE ? ORDER BY file_name DESC LIMIT 15"
    payload = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": [{"type": "text", "value": pat}] * 5}}, {"type": "close"}]}
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=8)
        resp.raise_for_status()
        return [[col.get("value") for col in r] for r in resp.json()["results"][0]["response"]["result"].get("rows", [])]
    except: return []

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
            except: pass
    return [{"type": "text", "text": "• 尚無風險等級紀錄", "size": "xs", "color": "#9CA3AF"}]

def build_stream_flex_bubble(stream_id: str, group_records: list):
    latest_rec = group_records[0]
    cty, twn = latest_rec.get("county") or "", latest_rec.get("township") or ""
    villages = set()
    for rec in group_records:
        for v in rec.get("villages", []): villages.add(v)
    v_str = "、".join(sorted(villages)) if villages else "未標記村里"
    adj = latest_rec.get("adjustments") or "無調整紀錄"
    
    report_buttons = []
    for rec in group_records:
        yr, fname, sgrp = rec.get("year"), rec.get("file_name"), rec.get("storage_group")
        dl_url = get_r2_download_url(fname, sgrp)
        if dl_url:
            report_buttons.append({"type": "button", "action": {"type": "uri", "label": f"📄 下載 {yr}年報告" if yr > 0 else "📄 下載報告", "uri": dl_url}, "style": "primary", "color": "#2563EB" if yr >= 2016 else "#4B5563", "height": "sm", "margin": "xs"})

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1E3A8A", "paddingAll": "14px", "contents": [
            {"type": "text", "text": stream_id, "weight": "bold", "size": "lg", "color": "#FFFFFF"},
            {"type": "text", "text": f"📍 {cty} {twn}（{v_str}）", "size": "xs", "color": "#E0E7FF", "margin": "xs"}
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
        bubble["footer"] = {"type": "box", "layout": "vertical", "paddingAll": "12px", "contents": [{"type": "text", "text": "📚 調查報告下載", "weight": "bold", "size": "xs", "color": "#2563EB"}, *report_buttons]}
    return bubble

# -------------------------------------------------------------
# 4. FastAPI 路由與 Webhook 處理 (雙模組智慧判斷)
# -------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "service": "Debris Flow LineBot Agent with Rainfall"}

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

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                user_text = event.message.text.strip()
                parts = user_text.split()
                
                # 決定用來查 Turso 報告的關鍵字：若有輸入編號，取第一部分；否則用全部
                turso_keyword = parts[0] if len(parts) > 0 and ("DF" in parts[0] or "縣" in parts[0] or "市" in parts[0] or "鄉" in parts[0]) else user_text
                
                messages_to_reply = []
                try:
                    # 1. 查詢 Turso 資料庫 (取得原本完整的報告卡片與 PDF 下載按鈕)
                    raw_records = query_turso_db(turso_keyword)

                    if raw_records:
                        grouped_streams = defaultdict(list)
                        for r in raw_records:
                            sid, cty, twn, v_raw, h_raw, adj, fname, s_grp, r_hist = r
                            v_list = json.loads(v_raw) if v_raw and str(v_raw).startswith("[") else []
                            h_list = json.loads(h_raw) if h_raw and str(h_raw).startswith("[") else []
                            yr = parse_report_year(fname)
                            stream_key = sid.strip() if sid else f"{cty}{twn}未編號"
                            grouped_streams[stream_key].append({
                                "stream_id": sid, "county": cty, "township": twn, "villages": v_list,
                                "disaster_history": h_list, "adjustments": adj, "file_name": fname,
                                "storage_group": s_grp, "risk_history": r_hist, "year": yr
                            })

                        bubbles = []
                        target_stream_id = None
                        for sid_key, recs in list(grouped_streams.items())[:3]: # 限制最多 3 個 bubble 避免超過 LINE 限制
                            recs.sort(key=lambda x: x["year"], reverse=True)
                            bubbles.append(build_stream_flex_bubble(sid_key, recs))
                            if not target_stream_id:
                                target_stream_id = sid_key

                        flex_payload = {"type": "carousel", "contents": bubbles} if len(bubbles) > 1 else bubbles[0]
                        
                        # 加入原本的 Flex 視覺卡片（含沿革、風險歷程、PDF 下載按鈕）
                        messages_to_reply.append(
                            FlexMessage(
                                alt_text=f"⛰️ 找到 {len(grouped_streams)} 條相關潛勢溪流調查資料",
                                contents=FlexContainer.from_json(json.dumps(flex_payload))
                            )
                        )

                        # 如果抓到的目標溪流編號有效，同步附加歷史雨量統計
                        if target_stream_id and ("DF" in target_stream_id or len(target_stream_id) >= 5):
                            event_kw = parts[1] if len(parts) > 1 else None
                            rain_result_md = rain_agent.execute_query(target_stream_id, event_kw)
                            messages_to_reply.append(TextMessage(text=rain_result_md))

                    # 如果 Turso 查無結果，但開頭是溪流編號，至少回傳雨量查詢結果
                    if not messages_to_reply and len(parts) > 0 and "DF" in parts[0]:
                        debris_no = parts[0]
                        event_kw = parts[1] if len(parts) > 1 else None
                        rain_result_md = rain_agent.execute_query(debris_no, event_kw)
                        messages_to_reply.append(TextMessage(text=rain_result_md))

                    # 若兩者皆無結果，回傳提示訊息
                    if not messages_to_reply:
                        reply_text = (
                            f"🔍 查詢關鍵字：「{user_text}」\n\n"
                            "⚠️ 查無相符的潛勢溪流調查或雨量紀錄。\n"
                            "💡 建議輸入：\n"
                            "• 溪流編號與事件 (如：屏縣DF022 莫拉克)\n"
                            "• 鄉鎮村里 (如：和平區、達觀里)"
                        )
                        messages_to_reply.append(TextMessage(text=reply_text))

                except Exception as inner_e:
                    print(f"❌ [LineBot 處理查詢發生錯誤]: {inner_e}")
                    messages_to_reply = [TextMessage(text=f"⚠️ 系統處理發生錯誤：{str(inner_e)}")]

                try:
                    # LINE 最多一次回發 5 則訊息
                    line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages_to_reply[:5]))
                except Exception as reply_e:
                    print(f"❌ [LineBot 回覆發送失敗]: {reply_e}")

    return "OK"
