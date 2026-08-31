# -*- coding: utf-8 -*-
import io
import json
import math
import datetime
import httpx
import pandas as pd
import streamlit as st

# ==============================================================================
# 1. 頁面基礎設定與樣式
# ==============================================================================
st.set_page_config(
    page_title="土石流潛勢溪流現勘智慧排程系統",
    page_icon="⛰️",
    layout="wide"
)

st.title("⛰️ 土石流潛勢溪流現勘智慧排程系統")
st.caption("農業部農村發展及水土保持署 現調行程智慧規劃與雲端履歷模組")

# ==============================================================================
# 2. 輔助函式與資料庫載入
# ==============================================================================
HSR_STATIONS = {
    "台南高鐵站": {"lat": 22.9248, "lng": 120.2858, "hsr_time_from_tpe": 90},
    "左營高鐵站": {"lat": 22.6874, "lng": 120.3078, "hsr_time_from_tpe": 105},
    "台中高鐵站": {"lat": 24.1121, "lng": 120.6160, "hsr_time_from_tpe": 55},
    "嘉義高鐵站": {"lat": 23.4590, "lng": 120.3235, "hsr_time_from_tpe": 75},
    "花蓮火車站": {"lat": 23.9933, "lng": 121.6015, "hsr_time_from_tpe": 130},
    "台東火車站": {"lat": 22.7933, "lng": 121.1232, "hsr_time_from_tpe": 210}
}

DEFAULT_DEMO_DB = {
    "南市DF038": {"county": "臺南市", "town": "南化區", "village": "關山里", "reason": "申請調整風險等級(114年度集水區具新生崩塌地)", "location": "南179線關山十六號橋", "dms": "23°12'50.5\"N 120°36'33.6\"E", "lat": 23.214028, "lng": 120.609333, "region": "south", "hsr": "台南高鐵站"},
    "南市DF039": {"county": "臺南市", "town": "南化區", "village": "關山里", "reason": "申請調整風險等級(114年度集水區具新生崩塌地)", "location": "南179線關山十四號橋", "dms": "23°11'23.2\"N 120°36'2.8\"E", "lat": 23.189778, "lng": 120.600778, "region": "south", "hsr": "台南高鐵站"},
    "南市DF043": {"county": "臺南市", "town": "南化區", "village": "關山里", "reason": "申請調整風險等級(距前次更新已逾15年且具保全住戶)", "location": "南179-1線無名橋", "dms": "23°11'31.8\"N 120°37'7.7\"E", "lat": 23.192167, "lng": 120.618806, "region": "south", "hsr": "台南高鐵站"},
    "南市DF033": {"county": "臺南市", "town": "楠西區", "village": "龜丹里", "reason": "申請調整風險等級(距前次更新已逾15年且具保全住戶)", "location": "鐵谷山宮", "dms": "23°7'43.9\"N 120°31'28.9\"E", "lat": 23.128861, "lng": 120.524694, "region": "south", "hsr": "台南高鐵站"},
    "南市DF034": {"county": "臺南市", "town": "楠西區", "village": "灣丘里", "reason": "申請調整風險等級(114年度集水區具新生崩塌地)", "location": "妙玄宮", "dms": "23°9'47.1\"N 120°32'2.4\"E", "lat": 23.163083, "lng": 120.534000, "region": "south", "hsr": "台南高鐵站"},
    "南市DF032": {"county": "臺南市", "town": "楠西區", "village": "照興里", "reason": "申請調整風險等級(114年度集水區具新生崩塌地)", "location": "坑尾農路箱涵", "dms": "23°12'3.4\"N 120°28'18\"E", "lat": 23.200944, "lng": 120.471667, "region": "south", "hsr": "台南高鐵站"},
    "屏縣DF074": {"county": "屏東縣", "town": "來義鄉", "village": "南和村", "reason": "二次災害高風險區複勘", "location": "白鷺二號橋", "dms": "22°26'40\"N 120°39'40.8\"E", "lat": 22.444444, "lng": 120.661333, "region": "south", "hsr": "左營高鐵站"}
}

@st.cache_data
def load_stream_db():
    try:
        with open("stream_database.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return data
    except Exception:
        return DEFAULT_DEMO_DB

stream_db = load_stream_db()

def estimate_drive_time_minutes(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_km = R * c
    return max(15, round((dist_km * 1.4 / 35.0) * 60))

def run_schedule_simulation(stream_list, start_loc, start_date, start_time, group_name):
    MAX_PER_DAY = 3
    SURVEY_DURATION = 90  # 1.5 hr
    LUNCH_DURATION = 60   # 1.0 hr
    CAR_RENTAL_BUFFER = 15

    schedule_results = []
    daily_routes = []

    current_day_idx = 0
    current_day_date = datetime.datetime.combine(start_date, start_time)
    daily_count = 0
    current_time = current_day_date
    current_coords = None
    had_lunch_today = False
    day_waypoints = []

    for idx, stream in enumerate(stream_list):
        if daily_count >= MAX_PER_DAY:
            daily_routes.append({
                "day": current_day_idx + 1,
                "date": current_day_date.date(),
                "waypoints": list(day_waypoints)
            })
            day_waypoints = []
            current_day_idx += 1
            daily_count = 0
            had_lunch_today = False
            current_day_date = current_day_date + datetime.timedelta(days=1)
            current_time = datetime.datetime.combine(current_day_date.date(), datetime.time(8, 30))

        if current_day_idx == 0 and daily_count == 0:
            is_north = any(k in start_loc for k in ["台北", "新北"])
            region = stream.get("region", "south")
            if is_north and region in ["south", "central", "east"]:
                hsr_station_name = stream.get("hsr", "台南高鐵站")
                hsr_info = HSR_STATIONS.get(hsr_station_name, HSR_STATIONS["台南高鐵站"])
                current_time += datetime.timedelta(minutes=hsr_info["hsr_time_from_tpe"] + CAR_RENTAL_BUFFER)
                current_coords = {"lat": hsr_info["lat"], "lng": hsr_info["lng"], "name": hsr_station_name}
                day_waypoints.append(current_coords)
            else:
                current_coords = {"lat": stream["lat"], "lng": stream["lng"], "name": start_loc}
                day_waypoints.append(current_coords)

        travel_min = 30
        if current_coords:
            travel_min = estimate_drive_time_minutes(current_coords["lat"], current_coords["lng"], stream["lat"], stream["lng"])

        arrival_time = current_time + datetime.timedelta(minutes=travel_min)

        if not had_lunch_today and (arrival_time.hour >= 12 or (arrival_time.hour == 11 and arrival_time.minute >= 45)):
            arrival_time += datetime.timedelta(minutes=LUNCH_DURATION)
            had_lunch_today = True

        leave_time = arrival_time + datetime.timedelta(minutes=SURVEY_DURATION)

        tw_year = arrival_time.year - 1911
        ampm = "AM" if arrival_time.hour < 12 else "PM"
        formatted_time = f"{tw_year}/{arrival_time.strftime('%m/%d')}\n{ampm}{arrival_time.strftime('%H:%M')}"
        formatted_loc = f"{stream['location']}\n({stream.get('dms', '')})"

        schedule_results.append({
            "自訂排序": idx + 1,
            "項次": idx + 1,
            "組別": group_name,
            "縣市": stream.get("county", ""),
            "鄉鎮市區": stream.get("town", ""),
            "村里": stream.get("village", ""),
            "編號": stream["stream_id"],
            "回報原因": stream.get("reason", "申請調整風險等級"),
            "會合時間": formatted_time,
            "會合地點": formatted_loc,
            "lat": stream["lat"],
            "lng": stream["lng"],
            "raw_time": arrival_time,
            "day_no": current_day_idx + 1,
            "location_name": stream["location"],
            "coords_dms": stream.get("dms", ""),
            "source": stream.get("source", "HISTORICAL_PDF")
        })

        current_coords = {"lat": stream["lat"], "lng": stream["lng"], "name": stream["location"]}
        day_waypoints.append(current_coords)
        current_time = leave_time
        daily_count += 1

    if day_waypoints:
        daily_routes.append({
            "day": current_day_idx + 1,
            "date": current_day_date.date(),
            "waypoints": list(day_waypoints)
        })

    return schedule_results, daily_routes

# ==============================================================================
# 3. 側邊欄：任務參數與資料庫狀態
# ==============================================================================
with st.sidebar:
    st.header("⚙️ 任務條件設定")
    start_location = st.text_input("出發地點", value="台北高鐵站")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        start_date = st.date_input("出發日期", value=datetime.date.today())
    with col_d2:
        start_time = st.time_input("出發時間", value=datetime.time(7, 30))

    group_name = st.selectbox("現勘組別", ["A", "B", "C"])
    leader_info = st.text_input("領隊 / 聯絡人電話", value="賴承農 0963-663193")

    st.markdown("---")
    st.write(f"📊 主庫載入溪流數: **{len(stream_db)}** 筆")
    uploaded_json = st.file_uploader("更新 stream_database.json", type=["json"])
    if uploaded_json:
        try:
            stream_db = json.load(uploaded_json)
            st.success(f"已更新資料庫！共 {len(stream_db)} 筆")
        except Exception as e:
            st.error(f"JSON 載入失敗: {e}")

# ==============================================================================
# 4. 主畫面：溪流輸入與排程試算
# ==============================================================================
st.subheader("1. 輸入待勘溪流編號")

col_btn1, col_btn2 = st.columns([1, 1])
with col_btn1:
    if st.button("載入南部範例 (7 條 - 自動排 3 天)"):
        st.session_state["raw_streams_input"] = "南市DF038, 南市DF039, 南市DF043, 南市DF033, 南市DF034, 南市DF032, 屏縣DF074"
with col_btn2:
    if st.button("載入中部範例 (4 條 - 自動排 2 天)"):
        st.session_state["raw_streams_input"] = "投縣DF106, 投縣DF105, 投縣DF101, 中市DF107"

default_text = st.session_state.get("raw_streams_input", "南市DF038, 南市DF039, 南市DF043, 南市DF033, 南市DF034, 南市DF032, 屏縣DF074")
stream_input_text = st.text_area("請以逗號或換行分隔溪流編號：", value=default_text, height=75)

if st.button("🚀 開始智慧路徑排程", type="primary"):
    raw_ids = [s.strip() for s in stream_input_text.replace("\n", ",").split(",") if s.strip()]
    parsed_streams = []
    for sid in raw_ids:
        item = stream_db.get(sid, {
            "county": "待查", "town": "待查", "village": "待查",
            "reason": "申請調整風險等級", "location": f"{sid}交會處",
            "dms": "23°00'00\"N 120°30'00\"E", "lat": 23.2, "lng": 120.6,
            "region": "south", "hsr": "台南高鐵站", "source": "NEED_GIS_OR_MANUAL"
        })
        parsed_streams.append({"stream_id": sid, **item})

    schedule_data, daily_routes = run_schedule_simulation(
        parsed_streams, start_location, start_date, start_time, group_name
    )
    st.session_state["schedule_data"] = schedule_data
    st.session_state["daily_routes"] = daily_routes
    st.session_state["current_parsed_streams"] = parsed_streams

# ==============================================================================
# 5. 排程預覽、互動修改與重算
# ==============================================================================
if "schedule_data" in st.session_state:
    st.markdown("---")
    st.subheader("2. 現勘排程預覽與手動微調")

    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("待勘總溪流", f"{len(st.session_state['schedule_data'])} 條")
    col_m2.metric("規劃總天數", f"{len(st.session_state['daily_routes'])} 天")
    col_m3.metric("交通轉乘", "高鐵 + 租車 15 分")
    col_m4.metric("工作節奏", "1.5h現勘 / 1h午休")

    df_view = pd.DataFrame(st.session_state["schedule_data"])
    display_cols = ["自訂排序", "項次", "組別", "縣市", "鄉鎮市區", "村里", "編號", "回報原因", "會合時間", "會合地點"]
    
    st.info("💡 提示：您可直接修改第一欄「自訂排序」中的數字調換順序，接著點擊下方「套用自訂排序重算」。")
    
    edited_df = st.data_editor(
        df_view[display_cols],
        column_config={
            "自訂排序": st.column_config.NumberColumn("自訂排序", min_value=1, max_value=99, step=1, required=True),
            "會合時間": st.column_config.TextColumn("會合時間", width="medium"),
            "會合地點": st.column_config.TextColumn("會合地點", width="large"),
        },
        disabled=["項次", "組別", "縣市", "鄉鎮市區", "村里", "編號", "回報原因", "會合時間", "會合地點"],
        use_container_width=True,
        key="data_editor"
    )

    if st.button("🔄 套用自訂排序重算"):
        # 依據使用者在介面上修改的自訂排序數值重新排序
        sorted_indices = edited_df.sort_values(by="自訂排序").index
        reordered_streams = [st.session_state["current_parsed_streams"][i] for i in sorted_indices]
        new_schedule, new_routes = run_schedule_simulation(
            reordered_streams, start_location, start_date, start_time, group_name
        )
        st.session_state["schedule_data"] = new_schedule
        st.session_state["daily_routes"] = new_routes
        st.session_state["current_parsed_streams"] = reordered_streams
        st.rerun()

    # ==============================================================================
    # 6. Google Maps 每日多點導航路徑
    # ==============================================================================
    st.subheader("3. 每日 Google Maps 車機 / 手機導航路徑")
    daily_gmaps_urls = []
    
    for route in st.session_state["daily_routes"]:
        pts = route["waypoints"]
        if len(pts) >= 2:
            origin = f"{pts[0]['lat']},{pts[0]['lng']}"
            dest = f"{pts[-1]['lat']},{pts[-1]['lng']}"
            waypoints_param = ""
            
            if len(pts) > 2:
                mid_pts = pts[1:-1]
                mid_coords_list = [f"{p['lat']},{p['lng']}" for p in mid_pts]
                waypoints_str = "|".join(mid_coords_list)
                waypoints_param = f"&waypoints={waypoints_str}"
            
            gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}{waypoints_param}&travelmode=driving"
            daily_gmaps_urls.append({"day": route["day"], "url": gmaps_url})
            
            pts_desc = " ➔ ".join([p["name"] for p in pts])
            tw_yr = route["date"].year - 1911
            date_str = f"{tw_yr}/{route['date'].strftime('%m/%d')}"

            col_r1, col_r2 = st.columns([4, 1])
            with col_r1:
                st.write(f"**第 {route['day']} 天 ({date_str})**：{pts_desc}")
            with col_r2:
                st.link_button("📍 開啟 Google Maps 導航", gmaps_url, use_container_width=True)

    # ==============================================================================
    # 7. 匯出 Excel 與 Turso 安全雲端同步
    # ==============================================================================
    st.markdown("---")
    st.subheader("4. 成果匯出與雲端資料庫儲存")

    col_exp1, col_exp2 = st.columns(2)

    with col_exp1:
        # 匯出標準公文 Excel
        export_df = pd.DataFrame(st.session_state["schedule_data"])[
            ["項次", "組別", "縣市", "鄉鎮市區", "村里", "編號", "回報原因", "會合時間", "會合地點"]
        ]
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            export_df.to_excel(writer, sheet_name="現勘行程表", index=False)
        excel_data = excel_buffer.getvalue()

        st.download_button(
            label="📊 下載標準公文現勘行程表 (.xlsx)",
            data=excel_data,
            file_name=f"115年新增及調整土石流潛勢溪流現調行程表_{group_name}組.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with col_exp2:
        # Turso 雲端安全儲存
        if st.button("☁️ 同步儲存至 Turso 雲端資料庫", type="secondary", use_container_width=True):
            turso_url = st.secrets.get("turso", {}).get("db_url", "").rstrip("/")
            turso_token = st.secrets.get("turso", {}).get("auth_token", "")

            if not turso_url or not turso_token:
                st.error("❌ 未在 `.streamlit/secrets.toml` 找到 Turso 連線設定！")
            else:
                now_str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                plan_id = f"PLAN-{now_str}-{group_name}"
                stmts = []

                # 母檔
                stmts.append({
                    "type": "execute",
                    "stmt": {
                        "sql": """INSERT INTO web_survey_plans 
                                  (plan_id, start_location, start_date, start_time, group_name, leader_info, total_streams, total_days, raw_input_streams, daily_gmaps_urls)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                        "args": [
                            {"type": "text", "value": plan_id},
                            {"type": "text", "value": start_location},
                            {"type": "text", "value": str(start_date)},
                            {"type": "text", "value": str(start_time)},
                            {"type": "text", "value": group_name},
                            {"type": "text", "value": leader_info},
                            {"type": "integer", "value": str(len(st.session_state["schedule_data"]))},
                            {"type": "integer", "value": str(len(st.session_state["daily_routes"]))},
                            {"type": "text", "value": stream_input_text},
                            {"type": "text", "value": json.dumps(daily_gmaps_urls)}
                        ]
                    }
                })

                # 明細節點
                for item in st.session_state["schedule_data"]:
                    stmts.append({
                        "type": "execute",
                        "stmt": {
                            "sql": """INSERT INTO web_survey_plan_nodes 
                                      (plan_id, day_no, item_order, custom_order, stream_id, county, town, village, reason, meeting_time, meeting_location, coords_dms, latitude, longitude, data_source)
                                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                        "args": [
                            {"type": "text", "value": plan_id},
                            {"type": "integer", "value": str(item["day_no"])},
                            {"type": "integer", "value": str(item["項次"])},
                            {"type": "integer", "value": str(item["自訂排序"])},
                            {"type": "text", "value": item["編號"]},
                            {"type": "text", "value": item["縣市"]},
                            {"type": "text", "value": item["鄉鎮市區"]},
                            {"type": "text", "value": item["村里"]},
                            {"type": "text", "value": item["回報原因"]},
                            {"type": "text", "value": item["會合時間"]},
                            {"type": "text", "value": item["location_name"]},
                            {"type": "text", "value": item["coords_dms"]},
                            {"type": "float", "value": item["lat"]} if item["lat"] is not None else {"type": "null"},
                            {"type": "float", "value": item["lng"]} if item["lng"] is not None else {"type": "null"},
                            {"type": "text", "value": item["source"]}
                        ]
                    }
                })

                with st.spinner("正在寫入 Turso 雲端資料庫..."):
                    try:
                        res = httpx.post(
                            f"{turso_url}/v2/pipeline",
                            headers={"Authorization": f"Bearer {turso_token}", "Content-Type": "application/json"},
                            json={"requests": stmts},
                            timeout=15.0
                        )
                        if res.status_code == 200:
                            st.success(f"🎉 成功同步至 Turso！規劃單號: `{plan_id}` (共 {len(st.session_state['schedule_data'])} 筆節點)")
                        else:
                            st.error(f"Turso 寫入失敗 ({res.status_code}): {res.text}")
                    except Exception as err:
                        st.error(f"連線異常: {err}")
