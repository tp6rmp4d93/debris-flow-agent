import os
import toml
import pandas as pd
import requests
import time

# -------------------------------------------------------------
# 1. 初始化設定與金鑰讀取
# -------------------------------------------------------------
secrets_path = ".streamlit/secrets.toml"
if not os.path.exists(secrets_path):
    print("❌ 找不到 .streamlit/secrets.toml 設定檔")
    exit()

config = toml.load(secrets_path)
TURSO_URL = config.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = config.get("TURSO_AUTH_TOKEN", "")

# 您的 Excel 檔案路徑
EXCEL_FILE_PATH = "您的Excel檔案路徑.xlsx"  # <--- 請修改此處 !!!
SHEET_NAME = "歷年土石流調整歷程總表"

if not os.path.exists(EXCEL_FILE_PATH):
    print(f"❌ 找不到 Excel 檔案：{EXCEL_FILE_PATH}")
    exit()

# -------------------------------------------------------------
# 2. Turso 資料庫執行函式
# -------------------------------------------------------------
def execute_turso_query(sql: str, args: list = None):
    http_url = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN.strip()}",
        "Content-Type": "application/json"
    }
    
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [{"type": "text", "value": str(v)} for v in args]

    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": stmt
            },
            {"type": "close"}
        ]
    }
    resp = requests.post(http_url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()["results"][0]["response"]["result"]

# -------------------------------------------------------------
# 3. 主執行流程
# -------------------------------------------------------------
def main():
    print("🚀 開始執行 2025 年度土石流調整歷程資料庫增量更新...")
    print("=" * 70)

    # A. 讀取 Excel 試算表
    print(f"📋 正在讀取 Excel 工作表: [{SHEET_NAME}]...")
    try:
        df = pd.read_excel(EXCEL_FILE_PATH, sheet_name=SHEET_NAME)
    except Exception as e:
        print(f"❌ 讀取 Excel 失敗: {e}")
        return

    # B. 篩選出需要更新 2025 資料的行
    # 邏輯：現今編號為空，且當年度說明或概述不為空
    update_targets = df[
        df['2026_DEBRISNO'].isna() & 
        (df['2025_修改說明'].notna() | df['2025_現勘初步情形'].notna())
    ].copy()

    total_targets = len(update_targets)
    print(f"🔍 找到 {total_targets} 筆潛在的 2025 年度更新資料。")
    print("-" * 70)

    updated_count = 0
    fail_count = 0

    # C. 逐筆處理並更新
    for idx, (excel_idx, row) in enumerate(update_targets.iterrows(), 1):
        stream_id = str(row['現今土石流潛勢溪流編號']).strip()
        
        if not stream_id or stream_id == 'nan':
            print(f"[{idx}/{total_targets}] ⚠️ 第 {excel_idx+2} 行溪流編號無效，跳過。")
            fail_count += 1
            continue

        # 整合 2025 說明文字 (優先使用概述)
        summary = row['2025_現勘初步情形']
        comment = row['2025_修改說明']
        
        # 判斷使用哪一個欄位
        update_text_content = ""
        if pd.notna(summary) and str(summary).strip():
            update_text_content = str(summary).strip()
        elif pd.notna(comment) and str(comment).strip():
            update_text_content = str(comment).strip()

        if not update_text_content:
            print(f"[{idx}/{total_targets}] ℹ️ 【{stream_id}】無 2025 調整說明內容，跳過。")
            continue

        new_entry = f"【2025年度】現勘評估概述：{update_text_content}"

        print(f"[{idx}/{total_targets}] 正在處理: 【{stream_id}】")

        try:
            # 1. 先從資料庫撈取既有的 adjustments
            sql_select = "SELECT demarcation_adjustments FROM streams WHERE stream_id = ?;"
            result_select = execute_turso_query(sql_select, [stream_id])
            
            rows_db = result_select.get("rows", [])
            
            if not rows_db:
                print(f"  ❌ 資料庫中找不到溪流 【{stream_id}】 的紀錄，跳過。")
                fail_count += 1
                continue

            current_adjustments = rows_db[0][0].get("value") or ""

            # 2. 組合新的內容 (附錄在底部)
            if current_adjustments:
                # 檢查是否已經存在 2025 更新 (避免重複執行)
                if "【2025年度】" in current_adjustments:
                    print(f"  ℹ️ 資料庫中已存在 2025 年度調整歷程，跳過覆蓋。")
                    continue
                
                final_adjustments = f"{current_adjustments}\n\n{new_entry}"
            else:
                final_adjustments = new_entry

            # 3. 更新回資料庫
            sql_update = "UPDATE streams SET demarcation_adjustments = ? WHERE stream_id = ?;"
            execute_turso_query(sql_update, [final_adjustments, stream_id])
            
            updated_count += 1
            print(f"  🎉 成功附錄更新！")

            # 避免觸發 API 頻率限制
            time.sleep(0.5)

        except Exception as e:
            print(f"  ❌ 更新失敗: {e}")
            fail_count += 1

    print("=" * 70)
    print(f"✅ 增量更新完成！共處理 {updated_count + fail_count} 筆，成功更新 {updated_count} 筆溪流之調整歷程。")
    print(f"ℹ️ 您可以重新整理 Streamlit 網頁，即可在 [📋 調查資料] 分頁查看最新的 2025 年度評估概述。")

if __name__ == "__main__":
    main()
