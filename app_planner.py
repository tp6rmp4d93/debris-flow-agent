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
st.caption("農業部農村發展及水土保持署 現調行程智慧規劃與雲端履歷模組 (整合最新 1753 圖資)")

ADJACENT_MAP = {
    "基隆市": ["台北市", "新北市", "臺北市"],
    "台北市": ["基隆市", "新北市"], "臺北市": ["基隆市", "新北市"],
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
    "台南市": ["嘉義縣", "高雄市"], "臺南市": ["嘉義縣", "高雄市"],
    "高雄市": ["台南市", "臺南市", "屏東縣", "嘉義縣", "南投縣", "台東縣", "臺東縣", "花蓮縣"],
    "屏東縣": ["高雄市", "台東縣", "臺東縣"],
    "宜蘭縣": ["新北市", "桃園市", "新竹縣", "台中市", "臺中市", "花蓮縣"],
    "花蓮縣": ["宜蘭縣", "台中市", "臺中市", "南投縣", "高雄市", "台東縣", "臺東縣"],
    "台東縣": ["花蓮縣", "高雄市", "屏東縣"], "臺東縣": ["花蓮縣", "高雄市", "屏東縣"]
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

def get_transit_info(county):
    region, hsr = "north", "台北高鐵站"
    if any(c in county for c in ["臺南", "台南", "高雄", "屏東", "臺東", "台東"]):
        region = "south"
        hsr = "台南高鐵站" if ("臺南" in county or "台南" in county) else ("左營高鐵站" if ("高雄" in county or "屏東" in county) else "台東火車站")
    elif any(c in county for c in ["臺中", "台中", "彰化", "南投", "雲林", "嘉義", "苗栗"]):
        region = "central"
        hsr = "台中高鐵站" if ("臺中" in county or "台中" in county or "苗栗" in county or "彰化" in county) else ("嘉義高鐵站" if ("雲林" in county or "嘉義" in county) else "台中高鐵站")
    elif any(c in county for c in ["花蓮", "宜蘭"]):
        region = "east"
        hsr = "花蓮火車站" if "花蓮" in county else "台北高鐵站"
    return region, hsr

# ==============================================================================
# 2. 輔助函式與狀態初始化
# ==============================================================================
@st.cache_data
def load_all_stream_data():
    geojson_db = {}
    history_db = {}
    
    try:
        with open("debrisstream1753_20260113_wgs84.geojson", "r", encoding="utf-8") as f:
            gj = json.load(f)
            for feature in gj.get("features", []):
                props = feature["properties"]
                geom = feature["geometry"]
                sid = props.get("Debrisno")
                if not sid: continue
                
                fallback_lat, fallback_lng = 23.5, 120.5
                if geom and geom.get("coordinates"):
                    try:
                        pt = geom["coordinates"][0][0]
                        fallback_lng, fallback_lat = pt[0], pt[1]
                    except Exception:
                        pass

                geojson_db[sid] = {
                    "county": props.get("County01", ""),
                    "town": props.get("Town01", ""),
                    "village": props.get("Vill01", ""),
                    "mark": props.get("Mark", f"{sid}附近"),
                    "lat": fallback_lat,
                    "lng": fallback_lng
                }
    except Exception as e:
        st.warning(f"尚未載入 GeoJSON 基礎圖資：{e}")

    try:
        with open("stream_database.json", "r", encoding="utf-8") as f:
            history_db = json.load(f)
    except Exception:
        pass

    return geojson_db, history_db

if "geojson_db" not in st.session_state or "history_db" not in st.session_state:
    g_db, h_db = load_all_stream_data()
    st.session_state.geojson_db = g_db
    st.session_state.history_db = h_db

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

def round_time_to_15_mins(dt):
    minutes = dt.minute
    rounded_minutes = round(minutes / 15.0) * 15
    return dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=rounded_minutes)

def parse_custom_location(input_str):
    """智慧判斷使用者輸入為純文字、經緯度還是 Google Maps 連結"""
    if not input_str:
        return None, None, "empty"
    
    lat_pattern = r'(2[1-6]\.\d+)'
    lng_pattern = r'(119\.\d+|12[0-2]\.\d+)'
    
    # 1. 擷取 Google Maps 連結內的座標 (如 @24.123,121.456)
    url_match = re.search(fr'@{lat_pattern},{lng_pattern}', input_str)
    if url_match:
        return float(url_match.group(1)), float(url_match.group(2)), "url"
        
    # 2. 擷取純經緯度 (如 24.12345, 121.12345)
    coord_match = re.search(fr'{lat_pattern}[^\d\.A-Za-z]+{lng_pattern}', input_str)
    if coord_match:
        return float(coord_match.group(1)), float(coord_match.group(2)), "coord"
        
    return None, None, "text"

# ==============================================================================
# 3. 核心排程引擎
# ==============================================================================
def run_schedule_simulation(stream_list, start_locations_list, start_date, start_time, group_name):
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

    for idx, stream in enumerate(stream_list):
        # 切換隔日
        if daily_count >= MAX_PER_DAY:
            daily_routes.append({"day": current_day_idx + 1, "date": current_day_date.date(), "waypoints": list(day_waypoints)})
            day_waypoints = []
            current_day_idx += 1
            daily_count = 0
            had_lunch_today = False
            current_day_date = current_day_date + datetime.timedelta(days=1)
            # 每日時間重置為使用者填寫的時間
            current_time = datetime.datetime.combine(current_day_date.date(), start_time)

        # 每日首站出發點
        if daily_count == 0:
            day_start_loc = start_locations_list[current_day_idx] if current_day_idx < len(start_locations_list) else start_locations_list[-1]
            start_county = extract_county_from_text(day_start_loc)
            target_county = stream.get("county", "")
            
            is_adjacent = False
            if start_county and target_county:
                if start_county == target_county or target_county in ADJACENT_MAP.get(start_county, []):
                    is_adjacent = True

            # 跨區交通判斷
            if not is_adjacent and stream.get("region") in ["south", "central", "east"] and ("台北" in day_start_loc or "新北" in day_start_loc):
                transport_modes_used.add(f"D{current_day_idx+1}:高鐵轉乘+租車")
                hsr_station = stream.get("hsr", "台南高鐵站")
                hsr_info = HSR_STATIONS.get(hsr_station, HSR_STATIONS["台南高鐵站"])
                current_time += datetime.timedelta(minutes=hsr_info["hsr_time_from_tpe"] + CAR_RENTAL_BUFFER)
                current_coords = {"lat": hsr_info["lat"], "lng": hsr_info["lng"], "name": hsr_station}
                day_waypoints.append(current_coords)
            else:
                transport_modes_used.add(f"D{current_day_idx+1}:自駕前往")
                start_coords = HSR_STATIONS.get(day_start_loc, {"lat": stream["lat"], "lng": stream["lng"]}) 
                current_coords = {"lat": start_coords["lat"], "lng": start_coords["lng"], "name": day_start_loc}
                day_waypoints.append(current_coords)

        travel_min = estimate_drive_time_minutes(current_coords["lat"], current_coords["lng"], stream["lat"], stream["lng"])
        
        # 首站自駕強制緩衝
        if daily_count == 0 and "自駕" in list(transport_modes_used)[-1]:
            travel_min = max(60, travel_min)

        is_long_drive = travel_min > 90

        arrival_time = current_time + datetime.timedelta(minutes=travel_min)
        arrival_time = round_time_to_15_mins(arrival_time)

        if not had_lunch_today and (arrival_time.hour >= 12 or (arrival_time.hour == 11 and arrival_time.minute >= 45)):
            arrival_time += datetime.timedelta(minutes=LUNCH_DURATION)
            had_lunch_today = True

        leave_time = arrival_time + datetime.timedelta(minutes=SURVEY_DURATION)
        
        is_late = arrival_time.time() > datetime.time(16, 0)

        tw_year = arrival_time.year - 1911
        ampm = "AM" if arrival_time.hour < 12 else "PM"
        formatted_time = f"{tw_year}/{arrival_time.strftime('%m/%d')}\n{ampm}{arrival_time.strftime('%H:%M')}"
        
        if is_long_drive:
            formatted_time += "\n(⚠️車程>1.5h)"
            
        loc_dms = f"\n({stream.get('dms')})" if stream.get('dms') else ""
        formatted_loc = f"{stream['location']}{loc_dms}"

        schedule_results.append({
            "自訂排序": idx + 1, "項次": idx + 1, "組別": group_name,
            "縣市": stream.get("county", ""), "鄉鎮市區": stream.get("town", ""),
            "村里": stream.get("village", ""), "編號": stream["stream_id"],
            "回報原因": stream.get("reason", ""), "會合時間": formatted_time,
            "會合地點": formatted_loc, "lat": stream["lat"], "lng": stream["lng"],
            "raw_time": arrival_time, "day_no": current_day_idx + 1,
            "location_name": stream["location"], "coords_dms": stream.get("dms", ""),
            "source": stream.get("source", "MANUAL"), "is_late": is_late, "is_long_drive": is_long_drive
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
# 5. 介面流程：步驟二 (任務條件與溪流總表)
# ==============================================================================
st.markdown("---")
st.subheader("步驟二：任務條件與待勘溪流總表確認")

num_days_est = math.ceil(len(raw_ids) / 3) if raw_ids else 1
st.write("**📍 每日出發地點設定**")
start_locations_list = []
cols = st.columns(min(num_days_est, 4))
for i in range(num_days_est):
    with cols[i % 4]:
        default_val = "台北高鐵站" if i == 0 else "當地住宿飯店"
        loc = st.text_input(f"第 {i+1} 天出發地點", value=default_val, key=f"start_loc_{i}")
        start_locations_list.append(loc)
st.session_state["start_locations_list"] = start_locations_list

st.write("**📅 任務時程與分組設定**")
col_cond1, col_cond2, col_cond3, col_cond4 = st.columns(4)
start_date = col_cond1.date_input("現勘起始日期", value=datetime.date.today())
start_time = col_cond2.time_input("每日首站出發時間", value=datetime.time(7, 30))
group_name = col_cond3.selectbox("組別", ["A", "B", "C"])
leader_info = col_cond4.text_input("領隊電話", value="賴承農 0963")

editor_data = []
for sid in raw_ids:
    geo_item = st.session_state.geojson_db.get(sid, {})
    hist_item = st.session_state.history_db.get(sid, {})
    
    county = geo_item.get("county") or hist_item.get("county") or guess_county_from_id(sid)
    town = geo_item.get("town") or hist_item.get("town") or ""
    village = geo_item.get("village") or hist_item.get("village") or ""
    
    if "location" in hist_item and hist_item["location"]:
        location = hist_item["location"]
        source = "HISTORICAL_DB"
    else:
        location = geo_item.get("mark", f"{sid}交會處")
        source = "GEOJSON_MARK"

    lat = hist_item.get("lat") or geo_item.get("lat") or 23.5
    lng = hist_item.get("lng") or geo_item.get("lng") or 120.5
    region, hsr = get_transit_info(county)

    editor_data.append({
        "溪流編號": sid,
        "縣市": county,
        "鄉鎮市區": town,
        "村里": village,
        "回報原因": hist_item.get("reason", "申請調整風險等級"),
        "會合地點": location,
        "lat": lat, "lng": lng, "dms": hist_item.get("dms", ""),
        "region": region, "hsr": hsr, "source": source
    })

df_editor_init = pd.DataFrame(editor_data)

st.write("**📝 待勘溪流屬性微調** (支援貼上 Google 地圖連結或座標；若清空會合點將自動復原預設地標)")
edited_streams_df = st.data_editor(
    df_editor_init,
    column_config={
        "溪流編號": st.column_config.TextColumn(disabled=True),
        "縣市": st.column_config.TextColumn(disabled=False),
        "鄉鎮市區": st.column_config.TextColumn(disabled=False),
        "村里": st.column_config.TextColumn(disabled=False),
        "回報原因": st.column_config.TextColumn("回報原因 (點擊修改)"),
        "會合地點": st.column_config.TextColumn("會合地點 (點擊修改)", width="large"),
        "lat": None, "lng": None, "dms": None, "region": None, "hsr": None, "source": None
    },
    use_container_width=True,
    key="stream_master_editor"
)

parsed_streams = []
warning_messages = []

for idx, row in edited_streams_df.iterrows():
    sid = row["溪流編號"]
    loc = row["會合地點"]
    lat, lng, dms = row["lat"], row["lng"], row["dms"]
    
    # 【防呆 1】若使用者將地點清空，自動自 GeoJSON 圖資庫帶回原始 Mark 點位
    if pd.isna(loc) or str(loc).strip() == "":
        geo_item = st.session_state.geojson_db.get(sid, {})
        loc = geo_item.get("mark", f"{sid}交會處")
    
    # 【防呆 2】判斷使用者是否修改了地標
    old_loc = df_editor_init.loc[idx, "會合地點"]
    if str(loc) != str(old_loc):
        p_lat, p_lng, l_type = parse_custom_location(str(loc))
        if l_type in ["url", "coord"]:
            lat, lng = p_lat, p_lng
            dms = "" # 取得新座標後可清除舊有 dms，由系統自動使用數值
        elif l_type == "text":
            warning_messages.append(f"⚠️ `{sid}`：您輸入了純文字地標「{loc}」。若系統無法精確比對座標，路徑規劃可能產生誤判，建議貼上 Google Maps 連結或經緯度。")

    if sid not in st.session_state.history_db:
        st.session_state.history_db[sid] = {}
        
    st.session_state.history_db[sid].update({
        "county": row["縣市"], "town": row["鄉鎮市區"], "village": row["村里"],
        "reason": row["回報原因"], "location": loc,
        "lat": lat, "lng": lng, "dms": dms,
        "region": row["region"], "hsr": row["hsr"]
    })
    
    parsed_streams.append({
        "stream_id": sid,
        "county": row["縣市"], "town": row["鄉鎮市區"], "village": row["村里"],
        "reason": row["回報原因"], "location": loc,
        "lat": lat, "lng": lng, "dms": dms,
        "region": row["region"], "hsr": row["hsr"], "source": row["source"]
    })

if warning_messages:
    for msg in warning_messages:
        st.warning(msg)

# ==============================================================================
# 6. 介面流程：步驟三 (開始排程與預覽)
# ==============================================================================
if st.button("🚀 確認無誤，開始智慧路徑排程", type="primary", use_container_width=True):
    schedule_data, daily_routes, transit_mode = run_schedule_simulation(
        parsed_streams, st.session_state["start_locations_list"], start_date, start_time, group_name
    )
    st.session_state["schedule_data"] = schedule_data
    st.session_state["daily_routes"] = daily_routes
    st.session_state["transit_mode"] = transit_mode
    st.session_state["current_parsed_streams"] = parsed_streams

if "schedule_data" in st.session_state:
    st.markdown("---")
    st.subheader("步驟三：現勘排程預覽與路徑匯出")

    has_late = any(item.get("is_late") for item in st.session_state.get("schedule_data", []))
    has_long_drive = any(item.get("is_long_drive") for item in st.session_state.get("schedule_data", []))

    if has_late:
        st.error("🚨 **超時防護警示**：黃底標註之現勘點預估會合時間已超過下午 4:00。為考量山區現勘安全，建議減少該日點位數量或多展延一天行程。")
    if has_long_drive:
        st.warning("⚠️ **長途車程提醒**：部分相鄰點位車程預估大於 1.5 小時，系統已於會合時間加註提示，請評估駕駛疲勞或調整現勘順序。")

    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("待勘總溪流", f"{len(st.session_state.get('schedule_data', []))} 條")
    col_m2.metric("規劃總天數", f"{len(st.session_state.get('daily_routes', []))} 天")
    col_m3.metric("系統判定交通模式", st.session_state.get("transit_mode", "判定中"))
    col_m4.metric("工作節奏設定", "每站停留 1.5h / 逢 15 分進位")

    df_view = pd.DataFrame(st.session_state.get("schedule_data", []))
    display_cols = ["自訂排序", "項次", "組別", "縣市", "鄉鎮市區", "編號", "會合時間", "會合地點", "is_late"]
    
    def highlight_schedule(row):
        if row.get('is_late'):
            return ['background-color: #ffe066; color: #856404; font-weight: bold'] * len(row)
        return [''] * len(row)
        
    styled_df = df_view[display_cols].style.apply(highlight_schedule, axis=1)

    st.info("💡 **自訂路線微調**：若欲更改現勘順序，請直接修改下方「自訂排序」數字，並點擊右側「套用排序重算」。")
    
    col_v1, col_v2 = st.columns([5, 1])
    with col_v1:
        edited_sort_df = st.data_editor(
            styled_df,
            column_config={
                "自訂排序": st.column_config.NumberColumn(min_value=1, max_value=99, step=1, required=True),
                "會合時間": st.column_config.TextColumn(width="medium"),
                "會合地點": st.column_config.TextColumn(width="large"),
                "is_late": None
            },
            disabled=["項次", "組別", "縣市", "鄉鎮市區", "編號", "會合時間", "會合地點"],
            use_container_width=True,
            key="sort_editor"
        )
    with col_v2:
        if st.button("🔄 套用排序重算", use_container_width=True):
            sorted_indices = edited_sort_df.sort_values(by="自訂排序").index
            current_streams = st.session_state.get("current_parsed_streams", [])
            reordered_streams = [current_streams[i] for i in sorted_indices if i < len(current_streams)]
            
            new_schedule, new_routes, new_transit = run_schedule_simulation(
                reordered_streams, st.session_state.get("start_locations_list", []), start_date, start_time, group_name
            )
            st.session_state["schedule_data"] = new_schedule
            st.session_state["daily_routes"] = new_routes
            st.session_state["transit_mode"] = new_transit
            st.session_state["current_parsed_streams"] = reordered_streams
            st.rerun()

    st.write("**📱 各日 Google Maps 車機 / 手機多點導航路徑**")
    daily_gmaps_urls = []
    for route in st.session_state.get("daily_routes", []):
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
        export_df = pd.DataFrame(st.session_state.get("schedule_data", []))[
            ["項次", "組別", "縣市", "鄉鎮市區", "村里", "編號", "回報原因", "會合時間", "會合地點"]
        ]
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            export_df.to_excel(writer, sheet_name="現勘行程表", index=False)
        st.download_button(
            label="📊 下載公文附件行程表 (.xlsx)",
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
                            {"type": "text", "value": plan_id}, {"type": "text", "value": st.session_state.get("start_locations_list", [""])[0]},
                            {"type": "text", "value": str(start_date)}, {"type": "text", "value": str(start_time)},
                            {"type": "text", "value": group_name}, {"type": "text", "value": leader_info},
                            {"type": "integer", "value": str(len(st.session_state.get("schedule_data", [])))},
                            {"type": "integer", "value": str(len(st.session_state.get("daily_routes", [])))}
                        ]
                    }
                }]
                
                for item in st.session_state.get("schedule_data", []):
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
# 7. 系統管理與資料庫更新區
# ==============================================================================
st.markdown("<br><br><br><hr>", unsafe_allow_html=True)
st.subheader("🛠️ 系統資料庫與底層管理")
st.write(f"當前系統記憶體共載入 **{len(st.session_state.geojson_db)}** 筆圖資基礎資訊與 **{len(st.session_state.history_db)}** 筆歷史會合點。")

uploaded_json = st.file_uploader("匯入由調查系統產出之 stream_database.json 更新歷史會合點主庫：", type=["json"])
if uploaded_json:
    try:
        new_db = json.load(uploaded_json)
        st.session_state.history_db.update(new_db)
        st.success(f"✅ 資料庫更新成功！歷史會合點已擴增至 {len(st.session_state.history_db)} 筆。")
    except Exception as e:
        st.error(f"JSON 載入失敗: {e}")
