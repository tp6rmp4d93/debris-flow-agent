import os
import json
from urllib.parse import quote
import streamlit as st
import folium
from folium import GeoJson, LayerControl
from streamlit_folium import st_folium
import requests
import boto3
from botocore.config import Config

# -------------------------------------------------------------
# 1. 頁面配置與 Secrets
# -------------------------------------------------------------
st.set_page_config(page_title="土石流潛勢溪流 Leaflet GIS Agent", page_icon="⛰️", layout="wide")

TURSO_URL = st.secrets.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = st.secrets.get("TURSO_AUTH_TOKEN", "")

# -------------------------------------------------------------
# 2. 載入與快取 GeoJSON 空間圖層 (純原生 json，無需 GeoPandas)
# -------------------------------------------------------------
@st.cache_data
def load_gis_layers():
    def load_json_file(file_path):
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    watershed_data = load_json_file("./geojson_layers/watershed.geojson")
    stream_data = load_json_file("./geojson_layers/stream.geojson")
    impact_data = load_json_file("./geojson_layers/impact_zone.geojson")
    return watershed_data, stream_data, impact_data

# -------------------------------------------------------------
# 3. Leaflet 地圖建構核心邏輯
# -------------------------------------------------------------
def filter_geojson_by_id(geojson_dict, stream_id):
    """依照溪流編號過濾 GeoJSON Feature"""
    if not geojson_dict or not stream_id:
        return geojson_dict
    filtered_features = [
        f for f in geojson_dict.get("features", [])
        if f.get("properties", {}).get("stream_id") == stream_id
    ]
    return {"type": "FeatureCollection", "features": filtered_features}

def build_leaflet_map(selected_stream_id: str = None):
    watershed_data, stream_data, impact_data = load_gis_layers()
    
    # 預設台灣中心座標
    map_center = [23.9756, 120.9738]
    zoom_level = 8

    m = folium.Map(
        location=map_center,
        zoom_start=zoom_level,
        tiles="OpenStreetMap",
        control_scale=True
    )

    # 1. 集水區圖層 (Polygon)
    if watershed_data:
        data_to_show = filter_geojson_by_id(watershed_data, selected_stream_id) if selected_stream_id else watershed_data
        if data_to_show.get("features"):
            GeoJson(
                data_to_show,
                name="集水區 (Watershed)",
                style_function=lambda x: {
                    "fillColor": "#3388ff",
                    "color": "#1A5276",
                    "weight": 1.5,
                    "fillOpacity": 0.2
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["stream_id", "county", "township"],
                    aliases=["溪流編號:", "縣市:", "鄉鎮:"],
                    localize=True
                )
            ).add_to(m)

    # 2. 潛勢溪流圖層 (LineString)
    if stream_data:
        data_to_show = filter_geojson_by_id(stream_data, selected_stream_id) if selected_stream_id else stream_data
        if data_to_show.get("features"):
            GeoJson(
                data_to_show,
                name="潛勢溪流 (Stream Line)",
                style_function=lambda x: {
                    "color": "#0022AA",
                    "weight": 4,
                    "opacity": 0.85
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["stream_id"],
                    aliases=["溪流編號:"],
                    localize=True
                )
            ).add_to(m)

    # 3. 影響範圍圖層 (Polygon)
    if impact_data:
        data_to_show = filter_geojson_by_id(impact_data, selected_stream_id) if selected_stream_id else impact_data
        if data_to_show.get("features"):
            GeoJson(
                data_to_show,
                name="影響範圍 (Impact Zone)",
                style_function=lambda x: {
                    "fillColor": "#E74C3C",
                    "color": "#C0392B",
                    "weight": 2,
                    "dashArray": "4, 4",
                    "fillOpacity": 0.4
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["stream_id", "risk_level"],
                    aliases=["溪流編號:", "潛勢等級:"],
                    localize=True
                ),
                popup=folium.GeoJsonPopup(
                    fields=["stream_id", "county", "township", "risk_level"],
                    aliases=["溪流編號", "縣市", "鄉鎮", "風險等級"]
                )
            ).add_to(m)

    LayerControl(collapsed=False).add_to(m)
    return m

# -------------------------------------------------------------
# 4. Turso HTTP API 查詢與多帳號 R2 安全下載
# -------------------------------------------------------------
def query_turso_http(sql: str, params: list = None):
    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"}
    statement = {"sql": sql}
    if params:
        statement["args"] = [{"type": "text", "value": str(p)} for p in params]

    payload = {"requests": [{"type": "execute", "stmt": statement}, {"type": "close"}]}
    resp = requests.post(http_url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()
    
    rows = []
    res = resp.json()["results"][0]["response"]["result"]
    for r in res.get("rows", []):
        rows.append([col.get("value") for col in r])
    return rows

def get_secure_download_url(file_name: str, storage_group: str) -> str:
    group_cfg = st.secrets.get(storage_group, st.secrets.get("R2_GRP_1", {}))
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{group_cfg['ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=group_cfg["ACCESS_KEY"],
        aws_secret_access_key=group_cfg["SECRET_KEY"],
        config=Config(signature_version="s3v4")
    )
    encoded = quote(file_name)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": group_cfg["BUCKET"], "Key": file_name, "ResponseContentDisposition": f"attachment; filename*=UTF-8''{encoded}"},
        ExpiresIn=900
    )

def query_database(query: str):
    pat = f"%{query.strip()}%"
    sql = """
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name, storage_group
        FROM streams 
        WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ?
        LIMIT 20
    """
    return query_turso_http(sql, [pat, pat, pat, pat])

# -------------------------------------------------------------
# 5. 主畫面介面排版
# -------------------------------------------------------------
st.title("⛰️ 土石流潛勢溪流AI結構化調查報告查詢")
st.caption("整合 Leaflet 三大空間圖資（集水區、溪流、影響範圍）與 AI 結構化調查報告")

search_kw = st.text_input("🔍 請輸入溪流編號或村里關鍵字：", placeholder="例如：新北DF001、弘道里、三峽區")

col_map, col_info = st.columns([3, 2])

selected_id = None
records = []
if search_kw:
    try:
        records = query_database(search_kw)
        if records:
            selected_id = records[0][0]
    except Exception as e:
        st.error(f"資料庫連線失敗: {e}")

with col_map:
    st.markdown("##### 🗺️ Leaflet 圖資檢視（支援圖層勾選與縮放）")
    leaflet_map = build_leaflet_map(selected_stream_id=selected_id)
    st_folium(leaflet_map, width="100%", height=560, returned_objects=[])

with col_info:
    st.markdown("##### 📋 調查報告與沿革摘要")
    if search_kw:
        if not records:
            st.warning(f"查無「{search_kw}」相關資料。")
        else:
            for r in records:
                stream_id, county, township, v_raw, h_raw, adj, fname, s_grp = r
                v_list = json.loads(v_raw) if v_raw else []
                h_list = json.loads(h_raw) if h_raw else []
                
                with st.expander(f"📌 **{stream_id}** — {county}{township}", expanded=True):
                    st.markdown(f"**涵蓋村里：** `{'、'.join(v_list)}`")
                    st.markdown("**🕒 歷年重大災害：**")
                    if h_list:
                        for h in h_list:
                            st.markdown(f"- **{h.get('year')}**：{h.get('description')}")
                    else:
                        st.write("無災害紀錄。")
                    
                    st.markdown("**📐 劃設調整情形：**")
                    st.write(adj if adj else "無調整紀錄。")
                    
                    dl_url = get_secure_download_url(fname, s_grp)
                    st.link_button("⬇️ 安全下載原始調查報告 (PDF)", url=dl_url, use_container_width=True)
    else:
        st.info("請於上方輸入關鍵字搜尋，系統將自動定位空間圖資並展開報告內容。")
