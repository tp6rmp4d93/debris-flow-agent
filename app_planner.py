# -*- coding: utf-8 -*-
import io
import json
import math
import datetime
import httpx
import re
import pandas as pd
import streamlit as st

# ==============================================================================
# 1. 頁面基礎設定與全台縣市相鄰矩陣
# ==============================================================================
st.set_page_config(page_title="土石流潛勢溪流現勘智慧排程系統", page_icon="⛰️", layout="wide")

st.title("⛰️ 土石流潛勢溪流現勘智慧排程系統")
st.caption("農業部農村發展及水土保持署 現調行程智慧規劃與雲端履歷模組")

# 台灣縣市相鄰矩陣 (用於判斷是否全程自駕)
ADJACENT_MAP = {
    "基隆市": ["台北市", "新北市", "臺北市"],
    "台北市": ["基隆市", "新北市"],
    "臺北市": ["基隆市", "新北市"],
    "新北市": ["基隆市", "台北市", "臺北市", "桃園市", "宜蘭縣"],
    "桃園市": ["新北市", "新竹縣", "新竹市", "宜蘭縣"],
    "新竹縣": ["桃園市", "新竹市", "苗栗縣", "台中市", "臺中市", "宜蘭縣"],
    "新竹市": ["新竹縣", "苗栗縣"],
    "苗栗縣": ["新竹縣", "新竹市", "台中市", "臺中市"],
    "台中市": ["苗栗縣", "彰化縣", "南投縣", "宜蘭縣", "花蓮縣", "新竹縣"],
    "臺中市": ["苗栗縣", "彰化縣", "南投縣", "宜蘭縣", "花蓮縣", "新竹縣"],
    "彰化縣": ["台中市", "臺中市", "南投縣", "雲林縣"],
    "南投縣": ["台中市", "臺中市", "彰化縣", "雲林縣", "嘉義縣", "高雄市", "花蓮縣"],
    "雲林縣": ["彰化縣", "南投縣", "嘉義縣", "嘉義市"],
    "嘉義縣": ["雲林縣", "嘉義市", "台南市", "臺南市", "高雄市", "南投縣"],
    "嘉義市": ["嘉義縣"],
    "台南市": ["嘉義縣", "高雄市"],
    "臺南市": ["嘉義縣", "高雄市"],
    "高雄市": ["台南市", "臺南市", "屏東縣", "嘉義縣", "南投縣", "台東縣", "臺東縣", "花蓮縣"],
    "屏東縣": ["高雄市", "台東縣", "臺東縣"],
    "宜蘭縣": ["新北市", "桃園市", "新竹縣", "台中市", "臺中市", "花蓮縣"],
    "花蓮縣": ["宜蘭縣", "台中市", "臺中市", "南投縣", "高雄市", "台東縣", "臺東縣"],
    "台東縣": ["花蓮縣", "高雄市", "屏東縣"],
    "臺東縣": ["花蓮縣", "高雄市", "屏東縣"],
    "澎湖縣": [], "金門縣": [], "連江縣": []
}

HSR_STATIONS = {
    "台南高鐵站": {"lat": 22.9248, "lng": 120.2858, "hsr_time_from_tpe": 90},
    "左營高鐵站": {"lat": 22.6874, "lng": 120.3078, "hsr_time_from_tpe": 105},
    "台中高鐵站": {"lat": 24.1121, "lng": 120.6160, "hsr_time_from_tpe": 55},
    "嘉義高鐵站": {"lat": 23.4590, "lng": 120.3235, "hsr_time_from_tpe": 75},
    "花蓮火車站": {"lat": 23.9933, "lng": 121.6015, "hsr_time_from_tpe": 130},
    "台東火車站": {"lat": 22.7933, "lng": 121.1232, "hsr_time_from_tpe": 210},
    "台北高鐵站": {"lat": 25.0478, "lng": 121.5170, "hsr_time_from_tpe": 0},
}

# ==============================================================================
# 2. 輔助函式與狀態初始化
# ==============================================================================
def load_stream_db():
    try:
        with open("stream_database.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 內建基礎防呆資料 (參考原始 GeoJSON 結構: Debrisno, County01, Town01, Vill01, Mark)[cite: 12]
        return {
            "南市DF038": {"county": "臺南市", "town": "南化區", "village": "關山里", "reason": "申請調整風險等級", "location": "南179線關山十六號橋", "dms": "23°12'50.5\"N 120°36'33.6\"E", "lat": 23.214028, "lng": 120.609333, "region": "south", "hsr": "台南高鐵站"},
            "高市E115-1": {"county": "高雄市", "town": "美濃區", "village": "福安里", "reason": "新增疑似土石流災害", "location": "雲自在禪院", "dms": "22°53'57.5\"N 120°30'47.6\"E", "lat": 22.899306, "lng": 120.513222, "region": "south", "hsr": "左營高鐵站"}
        }

if "stream_db" not in st.session_state:
    st.session_state.stream_db = load_stream_db()

def guess_county_from_id(stream_id):
    mapping = {
        "北": "新北市", "宜": "宜蘭縣", "桃": "桃園市", "竹縣": "新竹縣", "苗": "苗栗縣", 
        "中市": "臺中市", "彰": "彰化縣", "投": "南投縣", "雲": "雲林縣", "嘉縣": "嘉義縣", 
        "南市": "臺南市", "高市": "高雄市", "屏": "屏東縣", "東縣": "臺東縣", "花": "花蓮縣",
        "基市": "基隆市"
    }
    for prefix, county in mapping.items():
        if stream_id.startswith(prefix):
            return county
    return "待查"

def extract_county_from_text(text):
    for c in ADJACENT_MAP.keys():
        if c in text or c.replace("市", "").replace("縣", "") in text:
            return c
    return ""

def estimate_drive_time_minutes(lat1, lon1, lat2, lon2):
    if pd.isna(lat1) or pd.isna(lat2): return 60
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_km = R * c
    return max(15, round((dist_km * 1.4 / 35.0) * 60))

# ==============================================================================
# 3. 核心排程引擎 (整合相鄰縣市交通轉乘判斷)
# ==============================================================================
def run_schedule_simulation(stream_list, start_loc, start_date, start_time, group_name):
    MAX_PER_DAY = 3
    SURVEY_DURATION = 90
    LUNCH_DURATION = 60
    CAR_RENTAL_BUFFER = 15

    schedule_results = []
    daily_routes = []
    transport_modes_used = set()

    current_day_idx = 0
    current_day_date = datetime.datetime.combine(start_date, start_time)
    daily_count = 0
    current_time = current_day_date
    current_coords = None
    had_lunch_today = False
    day_waypoints = []
    
    start_county = extract_county_from_text(start_loc)

    for idx, stream in enumerate(stream_list):
        if daily_count >= MAX_PER_DAY:
            daily_routes.append({"day": current_day_idx + 1, "date": current_day_date.date(), "waypoints": list(day_waypoints)})
            day_waypoints = []
            current_day_idx += 1
            daily_count = 0
            had_lunch_today = False
            current_day_date = current_day_date + datetime.timedelta(days=1)
            current_time = datetime.datetime.combine(current_day_date.date(), datetime.time(8, 30))

        if current_day_idx == 0 and daily_count == 0:
            target_county = stream.get("county", "")
            
            # 判斷出發地與第一站是否相鄰或相同縣市
            is_adjacent = False
            if start_county and target_county:
                if start_county == target_county or target_county in ADJACENT_MAP.get(start_county, []):
                    is_adjacent = True

            # 非相鄰且跨區長途，判定為高鐵轉乘
            if not is_adjacent and stream.get("region") in ["south", "central", "east"] and ("台北" in start_loc or "新北" in start_loc):
                transport_modes_used.add("高鐵轉乘 + 租車 (含15分手續)")
                hsr_station = stream.get("hsr", "台南高鐵站")
                hsr_info = HSR_STATIONS.get(hsr_station, HSR_STATIONS["台南高鐵站"])
                current_time += datetime.timedelta(minutes=hsr_info["hsr_time_from_tpe"] + CAR_RENTAL_BUFFER)
                current_coords = {"lat": hsr_info["lat"], "lng": hsr_info["lng"], "name": hsr_station}
                day_waypoints.append(current_coords)
            else:
                transport_modes_used.add("全程直接自駕")
                start_coords = HSR_STATIONS.get(start_loc, {"lat": stream["lat"], "lng": stream["lng"]}) 
                current_coords = {"lat": start_coords["lat"], "lng": start_coords["lng"], "name": start_loc}
                day_waypoints.append(current_coords)

        travel_min = estimate_drive_time_minutes(current_coords["lat"], current_coords["lng"], stream["lat"], stream["lng"])
        
        # 若為自駕首站，強制預估至少 60 分鐘車程緩衝
        if current_day_idx == 0 and daily_count == 0 and "自駕" in list(transport_modes_used)[0]:
            travel_min = max(60, travel_min)

        arrival_time = current_time + datetime.timedelta(minutes=travel_min)

        if not had_lunch_today and (arrival_time.hour >= 12 or (arrival_time.hour == 11 and arrival_time.minute >= 45)):
            arrival_time += datetime.timedelta(minutes=LUNCH_DURATION)
            had_lunch_today = True

        leave_time = arrival_time + datetime.timedelta(minutes=SURVEY_DURATION)

        tw_year = arrival_time.year - 1911
        ampm = "AM" if arrival_time.hour < 12 else "PM"
        formatted_time = f"{tw_year}/{arrival_time.strftime('%m/%d')}\n{ampm}{arrival_time.strftime('%H:%M')}"
        formatted_loc = f"{stream['location']}\n({stream.get('dms', '')})" if stream.get('dms') else stream['location']

        schedule_results.append({
            "自訂排序": idx + 1, "項次": idx + 1, "組別": group_name,
            "縣市": stream.get("county", ""), "鄉鎮市區": stream.get("town", ""),
            "村里": stream.get("village", ""), "編號": stream["stream_id"],
            "回報原因": stream.get("reason", ""), "會合時間": formatted_time,
            "會合地點": formatted_loc, "lat": stream["lat"], "lng": stream["lng"],
            "raw_time": arrival_time, "day_no": current_day_idx + 1,
            "location_name": stream["location"], "coords_dms": stream.get("dms", ""),
            "source": stream.get("source", "MANUAL")
        })

        current_coords = {"lat": stream["lat"], "lng": stream["lng"], "name": stream["location"]}
        day_waypoints.append(current_coords)
        current_time = leave_time
        daily_count += 1

    if day_waypoints:
        daily_routes.append({"day": current_day_idx + 1, "date": current_day_date.date(), "waypoints": list(day_waypoints)})

    transit_desc = " / ".join(list(transport_modes_used)) if transport_modes_used else "全程直接自駕"
    return schedule_results, daily_routes, transit_desc

# ==============================================================================
# 4. 介面流程：步驟一 (輸入編號)
# ==============================================================================
st.subheader("步驟一：輸入待勘溪流編號")
col_btn1, col_btn2 = st.columns([1, 1])
with col_btn1:
    if st.button("載入南部跨區範例 (7 條)"):
        st.session_state["raw_streams_input"] = "南市DF038, 南市DF039, 南市DF043, 南市DF033, 南市DF034, 南市DF032, 屏縣DF074"
with col_btn2:
    if st.button("載入相鄰縣市自駕範例 (5 條)"):
        st.session_state["raw_streams_input"] = "基市DF012, 基市DF016, 宜縣DF135, 宜縣DF131, 宜縣DF132"

default_text = st.session_state.get("raw_streams_input", "基市DF012, 基市DF016, 宜縣DF135")
stream_input_text = st.text_area("請以逗號或換行分隔溪流編號：", value=default_text, height=75)
raw_ids = [s.strip() for s in stream_input_text.replace("\n", ",").split(",") if s.strip()]

# ==============================================================================
# 5. 介面流程：步驟二 (任務條件與溪流總表微調)
# ==============================================================================
st.markdown("---")
st.subheader("步驟二：任務條件與待勘溪流總表確認")

# 任務條件水平排版 (取代舊有側邊欄)
col_cond1, col_cond2, col_cond3, col_cond4 = st.columns(4)
start_location = col_cond1.text_input("出發地點 (系統將依此判定交通模式)", value="台北高鐵站")
start_date = col_cond2.date_input("現勘起始日期", value=datetime.date.today())
start_time = col_cond3.time_input("每日首站出發時間", value=datetime.time(7, 30))

col_g1, col_g2 = col_cond4.columns([1, 2])
group_name = col_g1.selectbox("組別", ["A", "B", "C"])
leader_info = col_g2.text_input("領隊電話", value="賴承農 0963")

# 準備可即時編輯的 DataFrame 總表
editor_data = []
for sid in raw_ids:
    db_item = st.session_state.stream_db.get(sid, {})
    editor_data.append({
        "溪流編號": sid,
        "縣市": db_item.get("county") or guess_county_from_id(sid),
        "鄉鎮市區": db_item.get("town", ""),
        "村里": db_item.get("village", ""),
        "回報原因": db_item.get("reason", "申請調整風險等級"),
        "會合地點": db_item.get("location", f"{sid}建議會合點"),
        "lat": db_item.get("lat", 23.5),
        "lng": db_item.get("lng", 120.5),
        "dms": db_item.get("dms", ""),
        "region": db_item.get("region", "north"),
        "hsr": db_item.get("hsr", "台北高鐵站")
    })

df_editor_init = pd.DataFrame(editor_data)

st.write("**📝 待勘溪流屬性微調** (支援直接修改，異動將同步更新至系統主資料庫)")
edited_streams_df = st.data_editor(
    df_editor_init,
    column_config={
        "溪流編號": st.column_config.TextColumn(disabled=True),
        "縣市": st.column_config.TextColumn(disabled=False),
        "鄉鎮市區": st.column_config.TextColumn(disabled=False),
        "村里": st.column_config.TextColumn(disabled=False),
        "回報原因": st.column_config.TextColumn("回報原因 (點擊修改)"),
        "會合地點": st.column_config.TextColumn("會合地點 (點擊修改)", width="large"),
        "lat": None, "lng": None, "dms": None, "region": None, "hsr": None
    },
    use_container_width=True,
    key="stream_master_editor"
)

# 將修改寫回 Session State 的資料庫中
parsed_streams = []
for idx, row in edited_streams_df.iterrows():
    sid = row["溪流編號"]
    if sid not in st.session_state.stream_db:
        st.session_state.stream_db[sid] = {}
    st.session_state.stream_db[sid].update({
        "county": row["縣市"],
        "town": row["鄉鎮市區"],
        "village": row["村里"],
        "reason": row["回報原因"],
        "location": row["會合地點"],
        "lat": row["lat"], "lng": row["lng"],
        "dms": row["dms"], "region": row["region"], "hsr": row["hsr"]
    })
    
    parsed_streams.append({
        "stream_id": sid,
        "county": row["縣市"], "town": row["鄉鎮市區"], "village": row["村里"],
        "reason": row["回報原因"], "location": row["會合地點"],
        "lat": row["lat"], "lng": row["lng"], "dms": row["dms"],
        "region": row["region"], "hsr": row["hsr"]
    })

# ==============================================================================
# 6. 介面流程：步驟三 (開始排程與預覽)
# ==============================================================================
if st.button("🚀 確認無誤，開始智慧路徑排程", type="primary", use_container_width=True):
    schedule_data, daily_routes, transit_mode = run_schedule_simulation(
        parsed_streams, start_location, start_date, start_time, group_name
    )
    st.session_state["schedule_data"] = schedule_data
    st.session_state["daily_routes"] = daily_routes
    st.session_state["transit_mode"] = transit_mode
    st.session_state["current_parsed_streams"] = parsed_streams

if "schedule_data" in st.session_state:
    st.markdown("---")
    st.subheader("步驟三：現勘排程預覽與路徑匯出")

    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("待勘總溪流", f"{len(st.session_state['schedule_data'])} 條")
    col_m2.metric("規劃總天數", f"{len(st.session_state['daily_routes'])} 天")
    col_m3.metric("系統判定交通模式", st.session_state["transit_mode"])
    col_m4.metric("工作節奏設定", "1.5h現勘 / 1h午休")

    df_view = pd.DataFrame(st.session_state["schedule_data"])
    display_cols = ["自訂排序", "項次", "組別", "縣市", "鄉鎮市區", "編號", "會合時間", "會合地點"]
    
    st.info("💡 **自訂路線微調**：若欲更改現勘順序，請直接修改下方「自訂排序」數字，並點擊右側「套用排序重算」。")
    
    col_v1, col_v2 = st.columns([5, 1])
    with col_v1:
        edited_sort_df = st.data_editor(
            df_view[display_cols],
            column_config={
                "自訂排序": st.column_config.NumberColumn(min_value=1, max_value=99, step=1, required=True),
                "會合時間": st.column_config.TextColumn(width="medium"),
                "會合地點": st.column_config.TextColumn(width="large"),
            },
            disabled=["項次", "組別", "縣市", "鄉鎮市區", "編號", "會合時間", "會合地點"],
            use_container_width=True,
            key="sort_editor"
        )
    with col_v2:
        if st.button("🔄 套用排序重算", use_container_width=True):
            sorted_indices = edited_sort_df.sort_values(by="自訂排序").index
            reordered_streams = [st.session_state["current_parsed_streams"][i] for i in sorted_indices]
            new_schedule, new_routes, new_transit = run_schedule_simulation(
                reordered_streams, start_location, start_date, start_time, group_name
            )
            st.session_state["schedule_data"] = new_schedule
            st.session_state["daily_routes"] = new_routes
            st.session_state["transit_mode"] = new_transit
            st.session_state["current_parsed_streams"] = reordered_streams
            st.rerun()

    st.write("**📱 各日 Google Maps 車機 / 手機多點導航路徑**")
    daily_gmaps_urls = []
    for route in st.session_state["daily_routes"]:
        pts = route["waypoints"]
        if len(pts) >= 2:
            origin = f"{pts[0]['lat']},{pts[0]['lng']}"
            dest = f"{pts[-1]['lat']},{pts[-1]['lng']}"
            waypoints_param = ""
            
            if len(pts) > 2:
                mid_pts = pts[1:-1]
                waypoints_str = "|".join(f"{p['lat']},{p['lng']}" for p in mid_pts)
                waypoints_param = f"&waypoints={waypoints_str}"
            
            gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}{waypoints_param}&travelmode=driving"
            daily_gmaps_urls.append(gmaps_url)
            
            pts_desc = " ➔ ".join([p["name"] for p in pts])
            tw_yr = route["date"].year - 1911
            date_str = f"{tw_yr}/{route['date'].strftime('%m/%d')}"

            col_r1, col_r2 = st.columns([5, 1])
            with col_r1:
                st.markdown(f"**第 {route['day']} 天 ({date_str})**：`{pts_desc}`")
            with col_r2:
                st.link_button("📍 開啟導航", gmaps_url, use_container_width=True)

    st.markdown("---")
    col_exp1, col_exp2 = st.columns(2)
    with col_exp1:
        export_df = pd.DataFrame(st.session_state["schedule_data"])[
            ["項次", "組別", "縣市", "鄉鎮市區", "村里", "編號", "回報原因", "會合時間", "會合地點"]
        ]
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            export_df.to_excel(writer, sheet_name="現勘行程表", index=False)
        st.download_button(
            label="📊 下載標準公文現勘行程表 (.xlsx)",
            data=excel_buffer.getvalue(),
            file_name=f"115年新增土石流潛勢溪流現調行程表_{group_name}組.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with col_exp2:
        if st.button("☁️ 儲存規劃紀錄至 Turso 雲端資料庫", type="secondary", use_container_width=True):
            turso_url = st.secrets.get("turso", {}).get("db_url", "").rstrip("/")
            turso_token = st.secrets.get("turso", {}).get("auth_token", "")
            
            if not turso_url or not turso_token:
                st.error("未配置 Turso 金鑰 (請確認 .streamlit/secrets.toml)")
            else:
                plan_id = f"PLAN-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{group_name}"
                stmts = [{
                    "type": "execute",
                    "stmt": {
                        "sql": "INSERT INTO web_survey_plans (plan_id, start_location, start_date, start_time, group_name, leader_info, total_streams, total_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        "args": [
                            {"type": "text", "value": plan_id}, {"type": "text", "value": start_location},
                            {"type": "text", "value": str(start_date)}, {"type": "text", "value": str(start_time)},
                            {"type": "text", "value": group_name}, {"type": "text", "value": leader_info},
                            {"type": "integer", "value": str(len(st.session_state["schedule_data"]))},
                            {"type": "integer", "value": str(len(st.session_state["daily_routes"]))}
                        ]
                    }
                }]
                
                for item in st.session_state["schedule_data"]:
                    stmts.append({
                        "type": "execute",
                        "stmt": {
                            "sql": "INSERT INTO web_survey_plan_nodes (plan_id, day_no, item_order, stream_id, county, town, meeting_time, meeting_location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            "args": [
                                {"type": "text", "value": plan_id},
                                {"type": "integer", "value": str(item["day_no"])},
                                {"type": "integer", "value": str(item["項次"])},
                                {"type": "text", "value": item["編號"]},
                                {"type": "text", "value": item["縣市"]},
                                {"type": "text", "value": item["鄉鎮市區"]},
                                {"type": "text", "value": item["會合時間"]},
                                {"type": "text", "value": item["location_name"]}
                            ]
                        }
                    })
                    
                with st.spinner("同步中..."):
                    try:
                        res = httpx.post(f"{turso_url}/v2/pipeline", headers={"Authorization": f"Bearer {turso_token}", "Content-Type": "application/json"}, json={"requests": stmts}, timeout=10.0)
                        if res.status_code == 200:
                            st.success(f"成功儲存單號：`{plan_id}`")
                        else:
                            st.error("寫入失敗")
                    except Exception as err:
                        st.error(f"連線異常: {err}")

# ==============================================================================
# 7. 系統管理與資料庫更新區 (移至網頁最底部)
# ==============================================================================
st.markdown("<br><br><br><hr>", unsafe_allow_html=True)
st.subheader("🛠️ 系統資料庫與底層管理")
st.write(f"當前系統記憶體共載入 **{len(st.session_state.stream_db)}** 筆溪流歷史會合點。")

uploaded_json = st.file_uploader("匯入由調查系統產出之 stream_database.json 擴充主庫：", type=["json"])
if uploaded_json:
    try:
        new_db = json.load(uploaded_json)
        st.session_state.stream_db.update(new_db)
        st.success(f"✅ 資料庫更新成功！已合併擴增至 {len(st.session_state.stream_db)} 筆資料。")
    except Exception as e:
        st.error(f"JSON 載入失敗: {e}")
