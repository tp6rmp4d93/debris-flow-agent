import os
import json
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
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

app = FastAPI()

# -------------------------------------------------------------
# 1. 讀取環境變數
# -------------------------------------------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

# 預設 R2 下載配置 (若有設定)
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# -------------------------------------------------------------
# 2. Turso 資料庫查詢邏輯
# -------------------------------------------------------------
def query_turso_db(keyword: str):
    """查詢 Turso 資料庫中符合條件的溪流"""
    if not TURSO_URL or not TURSO_TOKEN:
        return []

    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"}
    pat = f"%{keyword.strip()}%"
    
    sql = """
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group
        FROM streams 
        WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ? OR file_name LIKE ?
        LIMIT 3
    """
    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": [
                        {"type": "text", "value": pat},
                        {"type": "text", "value": pat},
                        {"type": "text", "value": pat},
                        {"type": "text", "value": pat},
                        {"type": "text", "value": pat}
                    ]
                }
            },
            {"type": "close"}
        ]
    }
    try:
        resp = requests.post(http_url, headers=headers, json=payload, timeout=8)
        res = resp.json()["results"][0]["response"]["result"]
        rows = []
        for r in res.get("rows", []):
            rows.append([col.get("value") for col in r])
        return rows
    except Exception as e:
        print(f"Turso 查詢失敗: {e}")
        return []

# -------------------------------------------------------------
# 3. 訊息格式化工具
# -------------------------------------------------------------
def format_stream_response(records: list, keyword: str) -> str:
    """將資料庫查詢結果排版為易讀的 LINE 文字訊息"""
    if not records:
        return (
            f"🔍 查詢關鍵字：「{keyword}」\n\n"
            "⚠️ 查無相符的土石流潛勢溪流紀錄。\n"
            "💡 建議可輸入：\n"
            "• 完整編號 (如：投縣DF135、DF001)\n"
            "• 鄉鎮村里 (如：國姓鄉、長福村、大同鄉)"
        )

    output = [f"⛰️ 找到 {len(records)} 筆「{keyword}」相關調查紀錄：\n"]

    for r in records:
        stream_id, county, township, v_raw, h_raw, adj, fname, s_grp = r
        v_list = json.loads(v_raw) if v_raw else []
        h_list = json.loads(h_raw) if h_raw else []

        card = [
            f"📌 【{stream_id}】",
            f"📍 位置：{county}{township} ({'、'.join(v_list) if v_list else '未標明村里'})",
            f"📐 劃設沿革：{adj if adj else '無調整紀錄'}"
        ]

        if h_list:
            card.append("🕒 歷年重大災害：")
            for h in h_list[:3]:  # 最多顯示 3 筆歷史災情
                yr = h.get("year", "未標明")
                desc = h.get("description", "")
                card.append(f"  • {yr}：{desc[:40]}{'...' if len(desc) > 40 else ''}")
        else:
            card.append("🕒 歷年重大災害：無歷史災情紀錄")

        card.append(f"📄 原始報告：{fname}")
        output.append("\n".join(card))
        output.append("────────────────")

    return "\n\n".join(output)

# -------------------------------------------------------------
# 4. Webhook 路由端點
# -------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "service": "Debris Flow Agent LineBot"}

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
                
                # 執行資料庫查詢
                records = query_turso_db(user_text)
                
                # 格式化回覆內容
                reply_content = format_stream_response(records, user_text)

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_content)]
                    )
                )

    return "OK"
