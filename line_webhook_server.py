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

# -------------------------------------------------------------
# 1. 服務初始化與環境變數設定
# -------------------------------------------------------------
app = FastAPI(title="Debris Flow LineBot Agent")

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("⚠️ 警告: 請設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET 環境變數。")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
r2_clients_cache = {}

# -------------------------------------------------------------
# 2. 解析年份與 R2 智慧年份分群預簽名
# -------------------------------------------------------------
def parse_report_year(file_name: str) -> int:
    """從檔名解析 4 位數年份 (如 2014_...)"""
    match = re.search(r"^(19\d\d|20\d\d)", str(file_name or ""))
    return int(match.group(1)) if match else 0

def determine_storage_group(file_name: str, storage_group: str) -> str:
    """依年份自動對應所屬 R2 儲存群組"""
    year = parse_report_year(file_name)
    if year > 0:
        if year <= 2007: return "R2_GRP_1"
        elif 2008 <= year <= 2010: return "R2_GRP_2"
        elif 2011 <= year <= 2015: return "R2_GRP_3"
        elif 2016 <= year <= 2020: return "R2_GRP_4"
        else: return "R2_GRP_5"

    if storage_group and storage_group.strip():
        s = storage_group.strip()
        if s.lower() not in ["none", "nan", "null"]:
            return s

    return "R2_GRP_3"

def get_r2_download_url(file_name: str, storage_group: str) -> str:
    """生成 15 分鐘有效的 R2 預簽名下載 URL (支援中文檔名編碼)"""
    if not file_name:
        return ""

    fn = str(file_name).strip()
    grp = determine_storage_group(fn, storage_group)

    account_id = os.getenv(f"{grp}_ACCOUNT_ID") or os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv(f"{grp}_ACCESS_KEY") or os.getenv("R2_ACCESS_KEY")
    secret_key = os.getenv(f"{grp}_SECRET_KEY") or os.getenv("R2_SECRET_KEY")
    bucket_name = os.getenv(f"{grp}_BUCKET") or os.getenv("R2_BUCKET")

    if not all([account_id, access_key, secret_key, bucket_name]):
        return ""

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

        return s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": str(bucket_name).strip(),
                "Key": fn,
                "ResponseContentDisposition": disposition
            },
            ExpiresIn=900
        )
    except Exception as e:
        print(f"❌ R2 預簽名生成異常 ({fn}): {e}")
        return ""

# -------------------------------------------------------------
# 3. Turso 資料庫查詢 (納入 risk_history 欄位)
# -------------------------------------------------------------
def query_turso_db(keyword: str):
    """查詢相符的溪流調查紀錄 (最多撈取 15 筆做聚合)"""
    if not TURSO_URL or not TURSO_TOKEN or not keyword:
        return []

    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN.strip()}",
        "Content-Type": "application/json"
    }
    pat = f"%{keyword.strip()}%"
    
    sql = """
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group, risk_history
        FROM streams 
        WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ? OR file_name LIKE ?
        ORDER BY file_name DESC
        LIMIT 15
    """
    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": [{"type": "text", "value": pat}] * 5
                }
            },
            {"type": "close"}
        ]
    }
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=8)
        resp.raise_for_status()
        res = resp.json()["results"][0]["response"]["result"]
        return [[col.get("value") for col in r] for r in res.get("rows", [])]
    except Exception as e:
        print(f"❌ Turso 資料庫查詢失敗: {e}")
        return []

# -------------------------------------------------------------
# 4. 解析與建構風險等級異動歷程 (垂直分行，新至舊)
# -------------------------------------------------------------
def build_risk_history_boxes(risk_history_raw: str):
    """
    將 risk_history JSON 解析為 Flex Message 垂直結構元件
    """
    if not risk_history_raw or not str(risk_history_raw).strip().startswith("["):
        return [{
            "type": "text",
            "text": "• 尚無 2010～2026 公告風險等級紀錄",
            "size": "xs",
            "color": "#9CA3AF"
        }]

    try:
        r_list = json.loads(risk_history_raw)
        if not r_list:
            return [{
                "type": "text",
                "text": "• 尚無 2010～2026 公告風險等級紀錄",
                "size": "xs",
                "color": "#9CA3AF"
            }]

        # 1. 先按年份由小到大正序掃描，找出首次劃設與異動點
        sorted_asc = sorted(r_list, key=lambda x: x.get("year", 0))
        change_records = []
        prev_risk = None

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

        # 2. 轉為「由新至舊」倒序
        sorted_desc = sorted(change_records, key=lambda x: x["year"], reverse=True)

        risk_boxes = []
        for idx, item in enumerate(sorted_desc):
            y = item["year"]
            r_name = item["risk"]
            status = item["status"]

            # 色彩配置
            if "高" in r_name:
                bg_color, text_color = "#FEE2E2", "#991B1B"
            elif "中" in r_name:
                bg_color, text_color = "#FEF3C7", "#92400E"
            elif "低" in r_name:
                bg_color, text_color = "#DCFCE7", "#166534"
            else:
                bg_color, text_color = "#F3F4F6", "#374151"

            prefix_tag = "🔸 [現況] " if idx == 0 and len(sorted_desc) > 1 else "🔹 "

            row_box = {
                "type": "box",
                "layout": "horizontal",
                "alignItems": "center",
                "margin": "xs",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{prefix_tag}{y}年",
                        "size": "xs",
                        "weight": "bold",
                        "color": "#1F2937",
                        "flex": 3
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "backgroundColor": bg_color,
                        "cornerRadius": "sm",
                        "paddingStart": "6px",
                        "paddingEnd": "6px",
                        "paddingTop": "2px",
                        "paddingBottom": "2px",
                        "alignItems": "center",
                        "contents": [
                            {
                                "type": "text",
                                "text": r_name,
                                "size": "xxs",
                                "weight": "bold",
                                "color": text_color
                            }
                        ]
                    },
                    {
                        "type": "text",
                        "text": f"（{status}）",
                        "size": "xxs",
                        "color": "#6B7280",
                        "margin": "sm",
                        "flex": 3
                    }
                ]
            }
            risk_boxes.append(row_box)

        return risk_boxes
    except Exception as e:
        return [{
            "type": "text",
            "text": f"• 風險等級資料解析異常: {e}",
            "size": "xs",
            "color": "#9CA3AF"
        }]

# -------------------------------------------------------------
# 5. LINE Flex Message 視覺卡片構建
# -------------------------------------------------------------
def build_stream_flex_bubble(stream_id: str, group_records: list):
    """
    建立聚合單一溪流的卡片：
    - 頂部：溪流編號、位置與歷年調查年度清單
    - 內容 1：歷年風險等級異動歷程
    - 內容 2：劃設調整沿革
    - 內容 3：歷年重大災害情勢
    - 底部：各年度報告之專屬 PDF 下載按鈕 (由新至舊)
    """
    latest_rec = group_records[0]
    cty = latest_rec.get("county") or ""
    twn = latest_rec.get("township") or ""
    
    # 整合涵蓋村里
    villages = set()
    for rec in group_records:
        for v in rec.get("villages", []):
            villages.add(v)
    v_str = "、".join(sorted(villages)) if villages else "未標記村里"

    # 最長/最新沿革
    adj = latest_rec.get("adjustments") or "無調整紀錄"
    for rec in group_records:
        curr_adj = rec.get("adjustments") or ""
        if len(curr_adj) > len(adj):
            adj = curr_adj

    # 提取所有調查年度
    report_years = []
    for rec in group_records:
        yr = rec.get("year", 0)
        if yr > 0:
            report_years.append(f"{yr}年")
    years_summary = "、".join(report_years) if report_years else "無紀錄"

    # 1. 歷年風險等級異動元件
    risk_history_boxes = build_risk_history_boxes(latest_rec.get("risk_history", ""))

    # 2. 歷年重大災害情勢元件 (去重)
    all_disasters = []
    seen_events = set()
    for rec in group_records:
        for d in rec.get("disaster_history", []):
            event_key = f"{d.get('year')}_{d.get('scale_and_damage') or d.get('description')}"
            if event_key not in seen_events:
                seen_events.add(event_key)
                all_disasters.append(d)

    disaster_boxes = []
    if all_disasters:
        for d in all_disasters[:6]:  # 最多顯示 6 筆
            yr = d.get("year", "歷史事件")
            rf = d.get("rainfall_info", "")
            dmg = d.get("scale_and_damage") or d.get("description", "無詳細災情紀錄")
            
            event_box = {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F9FAFB",
                "cornerRadius": "md",
                "paddingAll": "8px",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": f"🚨 {yr}",
                        "weight": "bold",
                        "size": "xs",
                        "color": "#DC2626"
                    }
                ]
            }
            if rf and rf != "未載明":
                event_box["contents"].append({
                    "type": "text",
                    "text": f"🌧️ 雨量：{rf}",
                    "size": "xxs",
                    "color": "#2563EB",
                    "margin": "xs"
                })
            event_box["contents"].append({
                "type": "text",
                "text": dmg,
                "size": "xs",
                "color": "#374151",
                "wrap": True,
                "margin": "xs"
            })
            disaster_boxes.append(event_box)
    else:
        disaster_boxes.append({
            "type": "text",
            "text": "• 報告內無重大歷史災害紀錄",
            "size": "xs",
            "color": "#9CA3AF"
        })

    # 3. 歷年報告下載按鈕 (由新到舊排列)
    report_buttons = []
    for rec in group_records:
        yr = rec.get("year")
        yr_label = f"{yr} 年報告" if yr > 0 else "調查報告"
        fname = rec.get("file_name")
        sgrp = rec.get("storage_group")
        
        dl_url = get_r2_download_url(fname, sgrp)
        if dl_url:
            report_buttons.append({
                "type": "button",
                "action": {
                    "type": "uri",
                    "label": f"📄 下載 {yr_label}",
                    "uri": dl_url
                },
                "style": "primary",
                "color": "#2563EB" if yr >= 2016 else "#4B5563",
                "height": "sm",
                "margin": "xs"
            })

    # 卡片結構組裝
    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1E3A8A",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "text",
                    "text": stream_id if stream_id else "土石流潛勢溪流",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#FFFFFF"
                },
                {
                    "type": "text",
                    "text": f"📍 {cty} {twn}（{v_str}）",
                    "size": "xs",
                    "color": "#E0E7FF",
                    "margin": "xs"
                },
                {
                    "type": "text",
                    "text": f"📅 調查年度：{years_summary}（共 {len(group_records)} 份）",
                    "size": "xxs",
                    "color": "#CBD5E1",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "contents": [
                # 區塊 1: 風險歷程
                {
                    "type": "text",
                    "text": "📊 歷年風險等級異動歷程",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#111827"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F8FAFC",
                    "cornerRadius": "md",
                    "paddingAll": "8px",
                    "margin": "xs",
                    "contents": risk_history_boxes
                },
                {"type": "separator", "margin": "md"},

                # 區塊 2: 劃設調整沿革
                {
                    "type": "text",
                    "text": "📐 劃設調整沿革",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#111827",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": adj,
                    "size": "xs",
                    "color": "#4B5563",
                    "wrap": True,
                    "margin": "xs"
                },
                {"type": "separator", "margin": "md"},

                # 區塊 3: 歷年重大災害
                {
                    "type": "text",
                    "text": "🕒 歷年重大災害情勢",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#111827",
                    "margin": "md"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": disaster_boxes,
                    "margin": "xs"
                }
            ]
        }
    }

    # 裝配底部下載按鈕區
    if report_buttons:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "text",
                    "text": "📚 歷年調查報告下載",
                    "weight": "bold",
                    "size": "xs",
                    "color": "#2563EB",
                    "margin": "none"
                },
                *report_buttons,
                {
                    "type": "text",
                    "text": "⚡ 連結有效期限 15 分鐘",
                    "size": "xxs",
                    "color": "#94A3B8",
                    "align": "center",
                    "margin": "sm"
                }
            ]
        }

    return bubble

# -------------------------------------------------------------
# 6. FastAPI 路由與 Webhook 處理
# -------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "service": "Debris Flow LineBot Multi-Year Server"}

@app.post("/callback")
async def handle_callback(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    body = await request.body()
    body_text = body.decode("utf-8")

    try:
        events = parser.parse(body_text, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature.")

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                user_text = event.message.text.strip()
                
                # 查詢 Turso 資料庫 (包含 risk_history)
                raw_records = query_turso_db(user_text)

                if not raw_records:
                    reply_msg = TextMessage(
                        text=f"🔍 查詢關鍵字：「{user_text}」\n\n"
                             "⚠️ 查無相符的土石流潛勢溪流紀錄。\n"
                             "💡 建議輸入：\n"
                             "• 溪流編號 (如：中市DF004、投縣DF135)\n"
                             "• 鄉鎮村里 (如：和平區、達觀里、竹山鎮)"
                    )
                else:
                    grouped_streams = defaultdict(list)
                    for r in raw_records:
                        sid, cty, twn, v_raw, h_raw, adj, fname, s_grp, r_hist = r
                        v_list = json.loads(v_raw) if v_raw and v_raw.startswith("[") else []
                        h_list = json.loads(h_raw) if h_raw and h_raw.startswith("[") else []
                        yr = parse_report_year(fname)
                        
                        stream_key = sid.strip() if sid else f"{cty}{twn}未編號"
                        grouped_streams[stream_key].append({
                            "stream_id": sid,
                            "county": cty,
                            "township": twn,
                            "villages": v_list,
                            "disaster_history": h_list,
                            "adjustments": adj,
                            "file_name": fname,
                            "storage_group": s_grp,
                            "risk_history": r_hist,
                            "year": yr
                        })

                    bubbles = []
                    for sid_key, recs in list(grouped_streams.items())[:5]:  # 最多 5 筆輪播
                        recs.sort(key=lambda x: x["year"], reverse=True)
                        bubble = build_stream_flex_bubble(sid_key, recs)
                        bubbles.append(bubble)

                    if len(bubbles) > 1:
                        flex_payload = {"type": "carousel", "contents": bubbles}
                    else:
                        flex_payload = bubbles[0]

                    reply_msg = FlexMessage(
                        alt_text=f"⛰️ 找到 {len(grouped_streams)} 條「{user_text}」相關潛勢溪流調查資料",
                        contents=FlexContainer.from_json(json.dumps(flex_payload))
                    )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[reply_msg]
                    )
                )

    return "OK"
