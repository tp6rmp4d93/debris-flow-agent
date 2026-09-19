FROM python:3.10-slim

WORKDIR /app

# 安裝系統層級的地理圖層依賴庫 (若 geopandas 需要)
RUN apt-get update && apt-get install -y \
    build-essential \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 預設啟動 FastAPI 的服務，PORT 由 GCP Cloud Run 動態提供
ENV PORT=8080
EXPOSE 8080

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
