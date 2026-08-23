import os
import json
import math
from urllib.parse import quote
from fastapi import FastAPI, Request, HTTPException
import boto3
from botocore.config import Config
import libsql_experimental as libsql

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, LocationMessageContent

app = FastAPI()

# -------------------------------------------------------------
# 1. 環境變數設定
# -------------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "你的_LINE_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "你的_LINE_SECRET")

TURSO_URL = os.getenv("TURSO_DATABASE_URL", "libsql://debris-db-yourusername.turso.io")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "你的_TURSO_TOKEN")

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = "debris-flow-reports"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# -------------------------------------------------------------
# 2. 資料庫與 R2 下載網址工具
# -------------------------------------------------------------
def get_secure_pdf_url(file_name: str) -> str:
    """產生 15 分鐘有效的繁體中文檔名安全下載 URL"""
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4")
    )
    encoded = quote(file_name)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": file_name, "ResponseContentDisposition": f"attachment; filename*=UTF-8''{encoded}"},
        ExpiresIn=900
    )

def query_streams_by_text(keyword: str):
    """關鍵字模糊查詢"""
    conn = libsql.connect("turso_cache.db", sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.sync()
    cursor = conn.cursor()
    pat = f"%{keyword.strip()}%"
    cursor.execute("""
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, risk_level
        FROM streams
        WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ?
        LIMIT 5
    """, (pat, pat, pat, pat))
    rows = cursor.fetchall()
    conn.close()
    return rows

# -------------------------------------------------------------
# 3. 建立 LINE Flex Message 卡片排版
# -------------------------------------------------------------
def create_stream_flex_card(stream_data) -> dict:
    stream_id, county, township, v_raw, h_raw, adjustments, file_name, risk = stream_data
    villages = json.loads(v_raw) if v_raw else []
    history = json.loads(h_raw) if h_raw else []
    
    # 歷年災害簡述
    history_text = "\n".join([f"• {h.get('year')}: {h.get('description')[:40]}..." for h in history[:2]]) if history else "無重大紀錄"
    download_url = get_secure_pdf_url(file_name)
    
    flex_dict = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1E3A8A",
            "contents": [
                {"type": "text", "text": f"⛰️ {stream_id}", "weight": "bold", "color": "#FFFFFF", "size": "lg"},
                {"type": "text", "text": f"{county}{township} ({', '.join(villages)})", "color": "#CBD5E1", "size": "sm"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "🕒 歷年重大災害：", "weight": "bold", "size": "sm", "color": "#334155"},
                        {"type": "text", "text": history_text, "size": "xs", "color": "#64748B", "wrap": True}
                    ]
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "📐 劃設沿革：", "weight": "bold", "size": "sm", "color": "#334155"},
                        {"type": "text", "text": (adjustments[:60] + "...") if adjustments else "無調整紀錄", "size": "xs", "color": "#64748B", "wrap": True}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#2563EB",
                    "action": {
                        "type": "uri",
                        "label": "📄 下載調查報告 PDF",
                        "uri": download_url
                    }
                }
            ]
        }
    }
    return flex_dict

# -------------------------------------------------------------
# 4. Webhook 接收與事件處理
# -------------------------------------------------------------
@app.post("/webhook")
async def line_webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    records = query_streams_by_text(user_text)
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if not records:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"查無「{user_text}」相關的土石流溪流。請輸入溪流編號（如 DF001）或村里名稱。")]
                )
            )
            return

        # 產生 Flex Message 輪播卡片 (最多 5 筆)
        bubbles = [create_stream_flex_card(r) for r in records]
        carousel = {"type": "carousel", "contents": bubbles} if len(bubbles) > 1 else bubbles[0]
        
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[FlexMessage(alt_text=f"土石流調查報告查詢結果 ({user_text})", contents=FlexContainer.from_dict(carousel))]
            )
        )