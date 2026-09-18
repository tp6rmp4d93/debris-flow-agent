import os
import json
import re
from collections import defaultdict
from urllib.parse import quote
import requests
import boto3
from botocore.config import Config
from fastapi import FastAPI, Request, HTTPException, Header, Response, status
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

# -------------------------------------------------------------
# 1. 服務初始化與環境變數設定
# -------------------------------------------------------------
app = FastAPI(title="Debris Flow LineBot Agent")

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
ADMIN_LINE_USER_ID = os.getenv("ADMIN_LINE_USER_ID", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("⚠️ 警告: 請設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET 環境變數。")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
r2_clients_cache = {}

# -------------------------------------------------------------
# 2. 解析年份與 R2 智慧分群預簽名下載
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
# 3. 輸入詞智慧拆解與意圖判斷
# -------------------------------------------------------------
def parse_user_input(user_input: str):
    """
    智慧拆解使用者輸入字串：
    例如 "高市DF053 莫拉克" -> main_term: "高市DF053", sub_filter: "莫拉克", is_rain_intent: True
    """
    cleaned = user_input.strip()
    is_rain_intent = any(k in cleaned for k in ["雨量", "降雨", "累積", "警戒值", "時雨量"])

    tokens = [t.strip() for t in re.split(r"[\s,]+", cleaned) if t.strip()]
    if not tokens:
        return "", "", False

    id_pattern = r"([A-Za-z\u4e00-\u9fa5]{1,4}[A-Za-z]{1,2}\d{2,4}(?:-\d+)?)"
    
    main_term = tokens[0]
    sub_tokens = tokens[1:]

    for i, t in enumerate(tokens):
        if re.search(id_pattern, t):
            main_term = t
            sub_tokens = [tokens[j] for j in range(len(tokens)) if j != i]
            break

    # 過濾掉純關鍵字 "雨量"，留下特定事件名稱如 "莫拉克"
    filter_words = [w for w in sub_tokens if w not in ["雨量", "降雨", "查詢"]]
    sub_filter = " ".join(filter_words)

    # 如果有帶事件名稱 (例如「莫拉克」)，預設帶有雨量災情查詢意圖
    if sub_filter:
        is_rain_intent = True

    return main_term, sub_filter, is_rain_intent

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
        SELECT 
            stream_id, 
            county, 
            township, 
            villages, 
            disaster_history, 
            demarcation_adjustments, 
            file_name, 
            storage_group, 
            risk_history, 
            dbno_old
        FROM streams 
        WHERE stream_id LIKE ? OR dbno_old LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ? OR file_name LIKE ?
        ORDER BY file_name DESC
        LIMIT 15;
    """
    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": [{"type": "text", "value": pat}] * 6
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
# 4. 解析與建構風險等級異動歷程 (垂直分行彩色標籤)
# -------------------------------------------------------------
def extract_valid_risk_json(group_records: list) -> str:
    """從溪流多份報告中獲取有效的 risk_history JSON"""
    for rec in group_records:
        raw = rec.get("risk_history")
        if raw and str(raw).strip().startswith("[") and str(raw).strip() != "[]":
            return str(raw).strip()
    return ""

def build_risk_history_boxes(group_records: list):
    """解析 risk_history JSON 為垂直排列元件"""
    risk_history_raw = extract_valid_risk_json(group_records)

    if not risk_history_raw:
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

        sorted_desc = sorted(change_records, key=lambda x: x["year"], reverse=True)

        risk_boxes = []
        for idx, item in enumerate(sorted_desc):
            y = item["year"]
            r_name = item["risk"]
            status = item["status"]

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
                        "flex": 4
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
                        "flex": 4
                    }
                ]
            }
            risk_boxes.append(row_box)

        return risk_boxes
    except Exception as e:
        return [{
            "type": "text",
            "text": f"• 風險等級解析異常: {e}",
            "size": "xs",
            "color": "#9CA3AF"
        }]

# -------------------------------------------------------------
# 5. 【土石流潛勢溪流雨量查詢結果】專屬 Flex 卡片
# -------------------------------------------------------------
def build_rainfall_result_bubble(stream_id: str, group_records: list, filter_kw: str = ""):
    """專屬雨量與警戒降雨查詢結果圖卡"""
    latest_rec = group_records[0]
    cty = latest_rec.get("county") or ""
    twn = latest_rec.get("township") or ""
    db_old = latest_rec.get("dbno_old") or ""

    villages = set()
    for rec in group_records:
        for v in rec.get("villages", []):
            villages.add(v)
    v_str = "、".join(sorted(villages)) if villages else "未標記村里"

    # 彙整所有致災事件
    all_disasters = []
    seen_events = set()
    for rec in group_records:
        for d in rec.get("disaster_history", []):
            event_key = f"{d.get('year')}_{d.get('scale_and_damage') or d.get('description')}"
            if event_key not in seen_events:
                seen_events.add(event_key)
                all_disasters.append(d)

    # 篩選特定關鍵字 (例如 莫拉克)
    if filter_kw:
        matched = [d for d in all_disasters if filter_kw.lower() in json.dumps(d, ensure_ascii=False).lower()]
        if matched:
            all_disasters = matched

    rain_items = []
    if all_disasters:
        for d in all_disasters[:8]:
            yr = d.get("year", "歷史降雨事件")
            rf = d.get("rainfall_info", "")
            dmg = d.get("scale_and_damage") or d.get("description", "無詳細災況紀錄")

            contents = [
                {
                    "type": "text",
                    "text": f"🌧️ {yr}",
                    "weight": "bold",
                    "size": "xs",
                    "color": "#1E40AF"
                }
            ]

            if rf and rf not in ["未載明", "none", "null", ""]:
                contents.append({
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#DBEAFE",
                    "cornerRadius": "sm",
                    "paddingAll": "6px",
                    "margin": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"致災降雨數據：{rf}",
                            "size": "xs",
                            "color": "#1E3A8A",
                            "weight": "bold",
                            "wrap": True
                        }
                    ]
                })
            else:
                contents.append({
                    "type": "text",
                    "text": "• 調查報告未載明詳細時/累積雨量數據",
                    "size": "xxs",
                    "color": "#94A3B8",
                    "margin": "xs"
                })

            contents.append({
                "type": "text",
                "text": f"災況規模：{dmg}",
                "size": "xs",
                "color": "#374151",
                "wrap": True,
                "margin": "xs"
            })

            rain_items.append({
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F8FAFC",
                "cornerRadius": "md",
                "paddingAll": "8px",
                "margin": "sm",
                "contents": contents
            })
    else:
        rain_items.append({
            "type": "text",
            "text": f"• 無符合「{filter_kw}」之雨量與致災降雨紀錄" if filter_kw else "• 報告內無重大致災降雨紀錄",
            "size": "xs",
            "color": "#94A3B8"
        })

    header_title = f"{stream_id}" + (f" (舊:{db_old})" if db_old else "")

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0284C7",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "text",
                    "text": "【土石流潛勢溪流雨量查詢結果】",
                    "weight": "bold",
                    "size": "sm",
                    "color": "#E0F2FE"
                },
                {
                    "type": "text",
                    "text": header_title,
                    "weight": "bold",
                    "size": "lg",
                    "color": "#FFFFFF",
                    "margin": "xs"
                },
                {
                    "type": "text",
                    "text": f"📍 {cty} {twn}（{v_str}）",
                    "size": "xs",
                    "color": "#F0F9FF",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "text",
                    "text": f"📊 歷史重大致災降雨一覽" + (f"（事件：{filter_kw}）" if filter_kw else ""),
                    "weight": "bold",
                    "size": "sm",
                    "color": "#0F172A"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": rain_items,
                    "margin": "xs"
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "💡 提示：降雨警戒值為即時研判指標，如需綜合調查報告，可直接輸入溪流編號查詢。",
                    "size": "xxs",
                    "color": "#64748B",
                    "wrap": True,
                    "margin": "md"
                }
            ]
        }
    }
    return bubble

# -------------------------------------------------------------
# 6. LINE 綜合潛勢溪流調查 Flex Message 視覺卡片構建
# -------------------------------------------------------------
def build_stream_flex_bubble(stream_id: str, group_records: list, filter_kw: str = ""):
    """建立聚合單一溪流之完整調查卡片"""
    latest_rec = group_records[0]
    cty = latest_rec.get("county") or ""
    twn = latest_rec.get("township") or ""
    db_old = latest_rec.get("dbno_old") or ""
    
    villages = set()
    for rec in group_records:
        for v in rec.get("villages", []):
            villages.add(v)
    v_str = "、".join(sorted(villages)) if villages else "未標記村里"

    adj = latest_rec.get("adjustments") or "無調整紀錄"
    for rec in group_records:
        curr_adj = rec.get("adjustments") or ""
        if len(curr_adj) > len(adj):
            adj = curr_adj

    report_years = [f"{rec.get('year')}年" for rec in group_records if rec.get('year', 0) > 0]
    years_summary = "、".join(report_years) if report_years else "無紀錄"

    risk_history_boxes = build_risk_history_boxes(group_records)

    all_disasters = []
    seen_events = set()
    for rec in group_records:
        for d in rec.get("disaster_history", []):
            event_key = f"{d.get('year')}_{d.get('scale_and_damage') or d.get('description')}"
            if event_key not in seen_events:
                seen_events.add(event_key)
                all_disasters.append(d)

    if filter_kw:
        matched_d = [d for d in all_disasters if filter_kw.lower() in json.dumps(d, ensure_ascii=False).lower()]
        if matched_d:
            all_disasters = matched_d

    disaster_boxes = []
    if all_disasters:
        for d in all_disasters[:6]:
            yr = d.get("year", "歷史事件")
            rf = d.get("rainfall_info", "")
            dmg = d.get("scale_and_damage") or d.get("description", "無詳細災情紀錄")
            
            box_contents = [
                {
                    "type": "text",
                    "text": f"🚨 {yr}",
                    "weight": "bold",
                    "size": "xs",
                    "color": "#DC2626"
                }
            ]
            
            if rf and rf not in ["未載明", "none", "null", ""]:
                box_contents.append({
                    "type": "box",
                    "layout": "horizontal",
                    "backgroundColor": "#EFF6FF",
                    "cornerRadius": "sm",
                    "paddingAll": "4px",
                    "margin": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"🌧️ 致災雨量：{rf}",
                            "size": "xxs",
                            "color": "#1D4ED8",
                            "weight": "bold",
                            "wrap": True
                        }
                    ]
                })
                
            box_contents.append({
                "type": "text",
                "text": dmg,
                "size": "xs",
                "color": "#374151",
                "wrap": True,
                "margin": "xs"
            })
            
            event_box = {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F9FAFB",
                "cornerRadius": "md",
                "paddingAll": "8px",
                "margin": "sm",
                "contents": box_contents
            }
            disaster_boxes.append(event_box)
    else:
        disaster_boxes.append({
            "type": "text",
            "text": f"• 無符合「{filter_kw}」之歷史災害紀錄" if filter_kw else "• 報告內無重大歷史災害紀錄",
            "size": "xs",
            "color": "#9CA3AF"
        })

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

    header_title = f"{stream_id}" + (f" (舊:{db_old})" if db_old else "")

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
                    "text": header_title,
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
                {
                    "type": "text",
                    "text": f"🕒 歷年重大災害與雨量情勢" + (f"（已篩選：{filter_kw}）" if filter_kw else ""),
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
# 7. FastAPI 路由與 Webhook 處理 (含防休眠與告警)
# -------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    """供 Render 系統與 UptimeRobot 定時 Ping 喚醒端點 (支援 GET 與 HEAD)"""
    return {"status": "ok", "service": "Debris Flow LineBot Server (Active)"}
@app.get("/")
def health_check():
    """UptimeRobot 每 5 分鐘 Ping 喚醒端點"""
    return {"status": "ok", "service": "Debris Flow LineBot Server (Active)"}

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
    body_text = body.decode("utf-8")

    try:
        events = parser.parse(body_text, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature.")

    # LINE Verify 測試快速通道
    if not events:
        return Response(content="OK", status_code=status.HTTP_200_OK)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                user_text = event.message.text.strip()
                
                # 分解出主查詢詞、篩選詞與是否為雨量意圖
                main_term, sub_filter, is_rain_intent = parse_user_input(user_text)

                try:
                    raw_records = query_turso_db(main_term)

                    if not raw_records:
                        reply_msg = TextMessage(
                            text=f"🔍 查詢關鍵字：「{user_text}」\n\n"
                                 "⚠️ 查無相符的土石流潛勢溪流紀錄。\n"
                                 "💡 建議輸入：\n"
                                 "• 現行編號 (如：高市DF053、中市DF004)\n"
                                 "• 舊編號 (如：宜蘭A089、花縣U113-1)\n"
                                 "• 雨量與事件查詢 (如：高市DF053 莫拉克、高市DF053 雨量)\n"
                                 "• 鄉鎮村里 (如：和平區、達觀里、竹山鎮)"
                        )
                    else:
                        grouped_streams = defaultdict(list)
                        for r in raw_records:
                            sid = r[0] or ""
                            cty = r[1] or ""
                            twn = r[2] or ""
                            v_raw = r[3] or ""
                            h_raw = r[4] or ""
                            adj = r[5] or "無調整紀錄"
                            fname = r[6] or ""
                            s_grp = r[7] or ""
                            r_hist = r[8] or ""
                            db_old = r[9] if len(r) > 9 else ""

                            v_list = json.loads(v_raw) if str(v_raw).startswith("[") else []
                            h_list = json.loads(h_raw) if str(h_raw).startswith("[") else []
                            yr = parse_report_year(fname)
                            
                            stream_key = sid.strip() if sid.strip() else f"{cty}{twn}未編號"
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
                                "dbno_old": db_old,
                                "year": yr
                            })

                        try:
                            bubbles = []
                            for sid_key, recs in list(grouped_streams.items())[:5]:
                                recs.sort(key=lambda x: x["year"], reverse=True)
                                
                                # 判斷是否呈現專屬【土石流潛勢溪流雨量查詢結果】
                                if is_rain_intent:
                                    bubble = build_rainfall_result_bubble(sid_key, recs, filter_kw=sub_filter)
                                else:
                                    bubble = build_stream_flex_bubble(sid_key, recs, filter_kw=sub_filter)
                                bubbles.append(bubble)

                            flex_payload = {"type": "carousel", "contents": bubbles} if len(bubbles) > 1 else bubbles[0]
                            alt_title = "【土石流潛勢溪流雨量查詢結果】" if is_rain_intent else "土石流潛勢溪流調查資料"
                            reply_msg = FlexMessage(
                                alt_text=f"⛰️ {alt_title}：{user_text}",
                                contents=FlexContainer.from_json(json.dumps(flex_payload))
                            )
                        except Exception as flex_err:
                            print(f"⚠️ Flex Message 異常，啟用降級模式: {flex_err}")
                            fallback_lines = [f"🌧️ 【土石流潛勢溪流雨量/調查結果】（{user_text}）：\n"]
                            for s_k, r_list in list(grouped_streams.items())[:3]:
                                first_r = r_list[0]
                                db_old_txt = f" (舊: {first_r.get('dbno_old')})" if first_r.get('dbno_old') else ""
                                fallback_lines.append(f"📌 【{s_k}】{db_old_txt} {first_r['county']}{first_r['township']}")
                                fallback_lines.append(f"• 歷年報告：共 {len(r_list)} 份")
                                
                                # 優先輸出降雨資訊
                                for d in first_r.get("disaster_history", [])[:3]:
                                    yr_t = d.get("year", "")
                                    rf_t = f" (雨量: {d.get('rainfall_info')})" if d.get('rainfall_info') else ""
                                    fallback_lines.append(f"• 🚨 {yr_t}{rf_t}")
                            reply_msg = TextMessage(text="\n".join(fallback_lines))

                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[reply_msg]
                        )
                    )
                except Exception as proc_err:
                    print(f"❌ 訊息處理流程異常: {proc_err}")

    return Response(content="OK", status_code=status.HTTP_200_OK)
