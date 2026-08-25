import os
import json
import re
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

app = FastAPI()

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
r2_clients_cache = {}

# -------------------------------------------------------------
# 智慧判定所屬 R2 群組 (優先讀取 DB，若無則依年份規則路由)
# -------------------------------------------------------------
def determine_storage_group(file_name: str, storage_group: str) -> str:
    if storage_group and storage_group.strip():
        return storage_group.strip()

    # 從檔名解析 4 位數年份 (如 2014_...)
    year_match = re.search(r"^(19\d\d|20\d\d)", file_name)
    if year_match:
        year = int(year_match.group(1))
        if year <= 2007:
            return "R2_GRP_1"
        elif 2008 <= year <= 2010:
            return "R2_GRP_2"
        elif 2011 <= year <= 2015:
            return "R2_GRP_3"
        elif 2016 <= year <= 2020:
            return "R2_GRP_4"
        else:
            return "R2_GRP_5"

    return "R2_GRP_3"  # 預設落點

def get_r2_download_url(file_name: str, storage_group: str) -> str:
    """生成 15 分鐘有效的 R2 預簽名下載 URL"""
    if not file_name:
        return ""

    grp = determine_storage_group(file_name, storage_group)

    # 讀取對應群組的環境變數
    account_id = os.getenv(f"{grp}_ACCOUNT_ID")
    access_key = os.getenv(f"{grp}_ACCESS_KEY")
    secret_key = os.getenv(f"{grp}_SECRET_KEY")
    bucket_name = os.getenv(f"{grp}_BUCKET")

    if not all([account_id, access_key, secret_key, bucket_name]):
        print(f"❌ 找不到群組【{grp}】的完整環境變數設定")
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
            Params={
                "Bucket": bucket_name.strip(),
                "Key": file_name,
                "ResponseContentDisposition": disposition
            },
            ExpiresIn=900  # 15 分鐘
        )
    except Exception as e:
        print(f"R2 預簽名生成異常 ({file_name}): {e}")
        return ""

# -------------------------------------------------------------
# Turso 查詢與 LINE 訊息封裝
# -------------------------------------------------------------
def query_turso_db(keyword: str):
    if not TURSO_URL or not TURSO_TOKEN:
        return []

    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"}
    pat = f"%{keyword.strip()}%"
    
    sql = """
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group
        FROM streams 
        WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ? OR file_name LIKE ?
        LIMIT 5
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
        res = resp.json()["results"][0]["response"]["result"]
        return [[col.get("value") for col in r] for r in res.get("rows", [])]
    except Exception as e:
        print(f"Turso 查詢失敗: {e}")
        return []

def build_stream_flex_bubble(stream_id, county, township, villages, disaster_history, adjustments, file_name, download_url):
    v_str = "、".join(villages) if villages else "未標記村里"
    
    disaster_boxes = []
    if disaster_history:
        for d in disaster_history[:2]:
            yr = d.get("year", "歷史")
            desc = d.get("description", "")
            disaster_boxes.append({
                "type": "text",
                "text": f"• {yr}：{desc[:28]}{'...' if len(desc) > 28 else ''}",
                "size": "xs",
                "color": "#666666",
                "wrap": True
            })
    else:
        disaster_boxes.append({
            "type": "text",
            "text": "• 無重大災害紀錄",
            "size": "xs",
            "color": "#888888"
        })

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1E3A8A",
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
                    "text": f"📍 {county} {township}（{v_str}）",
                    "size": "xs",
                    "color": "#93C5FD",
                    "margin": "sm"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "📐 劃設調整沿革",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#111827"
                },
                {
                    "type": "text",
                    "text": adjustments if adjustments else "無調整紀錄",
                    "size": "xs",
                    "color": "#4B5563",
                    "wrap": True,
                    "margin": "xs"
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "🕒 歷年重大災害",
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

    if download_url:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "uri",
                        "label": "📄 下載調查報告 (PDF)",
                        "uri": download_url
                    },
                    "style": "primary",
                    "color": "#2563EB",
                    "height": "sm"
                },
                {
                    "type": "text",
                    "text": "⚡ 連結有效期限 15 分鐘",
                    "size": "xxs",
                    "color": "#9CA3AF",
                    "align": "center",
                    "margin": "xs"
                }
            ]
        }

    return bubble

@app.get("/")
def health_check():
    return {"status": "ok", "service": "Debris Flow LineBot Flex"}

@app.post("/callback")
async def handle_callback(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing signature")

    body = await request.body()
    body_text = body.decode("utf-8")

    try:
        events = parser.parse(body_text, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                user_text = event.message.text.strip()
                records = query_turso_db(user_text)

                if not records:
                    reply_msg = TextMessage(
                        text=f"🔍 查詢關鍵字：「{user_text}」\n\n⚠️ 查無相符的土石流潛勢溪流紀錄。\n💡 請嘗試輸入完整編號（如：投縣DF135）或村里名稱。"
                    )
                else:
                    bubbles = []
                    for r in records:
                        sid, cty, twn, v_raw, h_raw, adj, fname, s_grp = r
                        v_list = json.loads(v_raw) if v_raw else []
                        h_list = json.loads(h_raw) if h_raw else []
                        
                        dl_url = get_r2_download_url(fname, s_grp)
                        bubble = build_stream_flex_bubble(sid, cty, twn, v_list, h_list, adj, fname, dl_url)
                        bubbles.append(bubble)

                    flex_payload = {
                        "type": "carousel" if len(bubbles) > 1 else "bubble",
                    }
                    if len(bubbles) > 1:
                        flex_payload["contents"] = bubbles
                    else:
                        flex_payload = bubbles[0]

                    reply_msg = FlexMessage(
                        alt_text=f"⛰️ 找到 {len(records)} 筆「{user_text}」相關調查紀錄",
                        contents=FlexContainer.from_json(json.dumps(flex_payload))
                    )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[reply_msg]
                    )
                )

    return "OK"
