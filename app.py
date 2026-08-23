import os
import json
from urllib.parse import quote
import streamlit as st
import geopandas as gpd
import folium
from folium import GeoJson, LayerControl
from streamlit_folium import st_folium
import libsql_experimental as libsql
import boto3
from botocore.config import Config

# -------------------------------------------------------------
# 1. 頁面配置與 Secrets
# -------------------------------------------------------------
st.set_page_config(page_title="土石流潛勢溪流 Leaflet GIS Agent", page_icon="⛰️", layout="wide")

TURSO_URL = st.secrets["TURSO_DATABASE_URL"]
TURSO_TOKEN = st.secrets["TURSO_AUTH_TOKEN"]
R2_ACCOUNT_ID = st.secrets["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = st.secrets["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = st.secrets["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = "debris-flow-reports"

# -------------------------------------------------------------
# 2. 載入與快取 GeoJSON 空間圖層 (避免每次操作重新讀檔)
# -------------------------------------------------------------
@st.cache_data
def load_gis_layers():
    watershed_gdf = gpd.read_file("./geojson_layers/watershed.geojson") if os.path.exists("./geojson_layers/watershed.geojson") else None
    stream_gdf = gpd.read_file("./geojson_layers/stream.geojson") if os.path.exists("./geojson_layers/stream.geojson") else None
    impact_gdf = gpd.read_file("./geojson_layers/impact_zone.geojson") if os.path.exists("./geojson_layers/impact_zone.geojson") else None
    return watershed_gdf, stream_gdf, impact_gdf

# -------------------------------------------------------------
# 3. Leaflet 地圖建構核心邏輯
# -------------------------------------------------------------
def build_leaflet_map(selected_stream_id: str = None):
    watershed_gdf, stream_gdf, impact_gdf = load_gis_layers()
    
    # 預設地圖中心 (台灣中心點)
    map_center = [23.9756, 120.9738]
    zoom_level = 8
    
    # 若使用者已選擇溪流，自動計算該溪流的中心點與縮放邊界
    if selected_stream_id and impact_gdf is not None:
        filtered = impact_gdf[impact_gdf["stream_id"] == selected_stream_id]
        if not filtered.empty:
            bounds = filtered.total_bounds # [minx, miny, maxx, maxy]
            map_center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
            zoom_level = 14

    # 建立 Leaflet 地圖實例
    m = folium.Map(
        location=map_center,
        zoom_start=zoom_level,
        tiles="OpenStreetMap",
        control_scale=True
    )

    # 1. 加入「集水區圖層」 (Polygon)
    if watershed_gdf is not None:
        data_to_show = watershed_gdf if not selected_stream_id else watershed_gdf[watershed_gdf["stream_id"] == selected_stream_id]
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

    # 2. 加入「潛勢溪流圖層」 (LineString)
    if stream_gdf is not None:
        data_to_show = stream_gdf if not selected_stream_id else stream_gdf[stream_gdf["stream_id"] == selected_stream_id]
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

    # 3. 加入「影響範圍圖層」 (Polygon)
    if impact_gdf is not None:
        data_to_show = impact_gdf if not selected_stream_id else impact_gdf[impact_gdf["stream_id"] == selected_stream_id]
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

    # 加入圖層右上角切換器
    LayerControl(collapsed=False).add_to(m)
    return m

# -------------------------------------------------------------
# 4. 資料庫查詢與 R2 安全下載
# -------------------------------------------------------------
def get_secure_download_url(file_name: str) -> str:
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

def query_database(query: str):
    conn = libsql.connect("turso_cache.db", sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.sync()
    cursor = conn.cursor()
    pat = f"%{query.strip()}%"
    cursor.execute("""
        SELECT stream_id, county, township, villages, disaster_history, demarcation_adjustments, file_name 
        FROM streams 
        WHERE stream_id LIKE ? OR county LIKE ? OR township LIKE ? OR villages LIKE ?
        LIMIT 20
    """, (pat, pat, pat, pat))
    rows = cursor.fetchall()
    conn.close()
    return rows

# -------------------------------------------------------------
# 5. 主畫面介面排版
# -------------------------------------------------------------
st.title("⛰️ 土石流潛勢溪流 Leaflet GIS 調查系統")
st.caption("整合 Leaflet 三大空間圖資（集水區、溪流、影響範圍）與 AI 結構化調查報告")

search_kw = st.text_input("🔍 請輸入溪流編號或村里關鍵字：", placeholder="例如：新北DF001、弘道里、三峽區")

col_map, col_info = st.columns([3, 2])

selected_id = None
if search_kw:
    records = query_database(search_kw)
    if records:
        selected_id = records[0][0]  # 預設定位至搜尋到的第一筆溪流

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
                stream_id, county, township, v_raw, h_raw, adj, fname = r
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
                    
                    dl_url = get_secure_download_url(fname)
                    st.link_button("⬇️ 安全下載原始調查報告 (PDF)", url=dl_url, use_container_width=True)
    else:
        st.info("請於上方輸入關鍵字搜尋，系統將自動定位空間圖資並展開報告內容。")