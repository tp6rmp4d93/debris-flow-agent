import os
import fiona
import geopandas as gpd

# 啟用 KML/KMZ 驅動支援
fiona.drvsupport.supported_drivers['KML'] = 'rw'
fiona.drvsupport.supported_drivers['LIBKML'] = 'rw'

# 原始圖資路徑 (請替換為你的檔案路徑)
INPUT_LAYERS = {
    "watershed": "./gis_data/集水區.shp",      # 或 .kml
    "stream": "./gis_data/潛勢溪流.shp",
    "impact_zone": "./gis_data/影響範圍.shp"
}

OUTPUT_DIR = "./geojson_layers"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def process_and_convert_layer(input_path: str, output_name: str, encoding: str = "utf-8"):
    """
    讀取 Shapefile/KML (EPSG:3826)，轉換座標至 EPSG:4326 (WGS84)，並輸出 GeoJSON
    若中文字亂碼，可將 encoding 改為 'cp950'
    """
    print(f"正在處理圖層：{input_path} ...")
    
    try:
        # 嘗試以指定編碼讀取
        gdf = gpd.read_file(input_path, encoding=encoding)
    except UnicodeDecodeError:
        print(f"  -> UTF-8 編碼失敗，切換為 CP950 (Big5) 讀取繁體中文字元...")
        gdf = gpd.read_file(input_path, encoding="cp950")

    # 確保設定原始座標為 EPSG:3826 (TWD97 121分帶)
    if gdf.crs is None:
        gdf.set_crs(epsg=3826, inplace=True)
    elif gdf.crs.to_epsg() != 3826 and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=3826)

    # 轉為 Leaflet 專用座標 EPSG:4326 (WGS84 經緯度)
    gdf_wgs84 = gdf.to_crs(epsg=4326)

    # 輸出為標準 UTF-8 GeoJSON
    output_path = os.path.join(OUTPUT_DIR, f"{output_name}.geojson")
    gdf_wgs84.to_file(output_path, driver="GeoJSON", encoding="utf-8")
    print(f"  -> 轉換完成：{output_path} (筆數: {len(gdf_wgs84)})")

def run_gis_pipeline():
    for layer_key, file_path in INPUT_LAYERS.items():
        if os.path.exists(file_path):
            process_and_convert_layer(file_path, layer_key)
        else:
            print(f"警告：找不到圖層檔案 {file_path}")

if __name__ == "__main__":
    run_gis_pipeline()