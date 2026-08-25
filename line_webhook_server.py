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
# 1. 讀取環境變數與初始化
# -------------------------------------------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# 緩存 R2 Client 以重複利用連線池
r2_clients_cache = {}

# -------------------------------------------------------------
# 2. Cloudflare R2 預簽名安全下載連結生成
# -------------------------------------------------------------
def get_r2_download_url(file_name: str, storage_group: str) -> str:
    """生成 15 分鐘有效的 R2 預簽名下載 URL (支援繁體中文檔名)"""
    if not file_name:
        return ""

    grp = storage_group if storage_group else "R2_GRP_1"
    account_id = os.getenv(f"{grp}_ACCOUNT_ID", os.getenv("R2_ACCOUNT_ID", ""))
    access_key = os.getenv(f"{grp}_ACCESS_KEY", os.getenv("R2_ACCESS_KEY", ""))
    secret_key = os.getenv(f"{grp}_SECRET_KEY", os.getenv("R2_SECRET_KEY", ""))
    bucket_name = os.getenv(f"{grp}_BUCKET", os.getenv("R2_BUCKET", ""))

    if not all([account_id, access_key, secret_key, bucket_name]):
        return ""

    try:
        if grp not in r2_clients_cache:
            r2_clients_cache[grp] = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4")
            )
        s3 = r2_clients_cache[grp]

        # 處理中文檔名在瀏覽器下載時的編碼
        encoded_filename = quote(file_name)
        disposition = f"attachment; filename*=UTF-8''{encoded_filename}"

        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": bucket_name,
                "Key": file_name,
                "ResponseContentDisposition": disposition
            },
            ExpiresIn=900  # 15 分鐘有效
        )
        return url
    except Exception as e:
        print(f"R2 預簽名 URL 生成失敗 ({file_name}): {e}")
        return ""

# -------------------------------------------------------------
# 3. Turso 資料庫查詢邏輯
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
        LIMIT 3
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

# -------------------------------------------------------------
# 4. 訊息格式化 (整合安全下載連結)
# -------------------------------------------------------------
def format_stream_response(records: list, keyword: str) -> str:
    if not records:
        return (
            f"🔍 查詢關鍵字：「{keyword}」\n\n"
            "⚠️ 查無相符的土石流潛勢溪流紀錄。\n"
            "💡 請嘗試輸入完整編號（如：投縣DF135）或村里名稱。"
        )

    output = [f"⛰️ 找到 {len(records)} 筆「{keyword}」相關調查紀錄：\n"]

    for r in records:
        stream_id, county, township, v_raw, h_raw, adj, fname, s_grp = r
        v_list = json.loads(v_raw) if v_raw else []
        h_list = json.loads(h_raw) if h_raw else []

        # 動態生成 R2 臨時安全下載連結
        download_url = get_r2_download_url(fname, s_grp)

        card = [
            f"📌 【{stream_id}】",
            f"📍 位置：{county}{township} ({'、'.join(v_list) if v_list else '未標明村里'})",
            f"📐 劃設沿革：{adj if adj else '無調整紀錄'}"
        ]

        if h_list:
            card.append("🕒 歷年重大災害：")
            for h in h_list[:2]:
                card.append(f"  • {h.get('year', '未標明')}：{h.get('description', '')[:35]}...")
        else:
            card.append("🕒 歷年重大災害：無紀錄")

        card.append(f"📄 原始報告：{fname}")
        
        if download_url:
            card.append(f"🔗 報告下載 (15分鐘內有效)：\n{download_url}")
        else:
            card.append("⚠️ 目前無法取得下載連結")

        output.append("\n".join(card))
        output.append("────────────────")

    return "\n\n".join(output)

# -------------------------------------------------------------
# 5. Webhook 路由
# -------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "service": "Debris Flow LineBot"}

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
                reply_content = format_stream_response(records, user_text)

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_content)]
                    )
                )

    return "OK"
