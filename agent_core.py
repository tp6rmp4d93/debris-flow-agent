import os
import re
import json
from datetime import datetime, timedelta
import pandas as pd

class DebrisRainfallAgentCore:
    def __init__(self, db_dir="wra_rain_db/excel_data", debris_json="GetDebrisRainData.json"):
        script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
        
        candidate_paths = [
            db_dir,
            os.path.join(script_dir, "wra_rain_db", "excel_data"),
            os.path.join(os.getcwd(), "wra_rain_db", "excel_data"),
            "wra_rain_db/excel_data"
        ]
        
        self.excel_dir = None
        for p in candidate_paths:
            if p and os.path.exists(p) and any(f.endswith('.xlsx') for f in os.listdir(p)):
                self.excel_dir = p
                break
                
        if not self.excel_dir:
            self.excel_dir = db_dir

        json_path = debris_json if os.path.isabs(debris_json) else os.path.join(script_dir, debris_json)
        if not os.path.exists(json_path):
            json_path = os.path.join(os.getcwd(), debris_json)

        with open(json_path, 'r', encoding='utf-8') as f:
            self.df_debris = pd.DataFrame(json.load(f))
            
        self.durations = ["連續1小時", "連續3小時", "連續6小時", "連續12小時", "連續24小時", "事件總累計"]
        self.all_rain_records = self._load_database()

    def _load_database(self):
        records = []
        if not os.path.exists(self.excel_dir):
            return pd.DataFrame()

        for f in os.listdir(self.excel_dir):
            if f.endswith('.xlsx'):
                try:
                    file_path = os.path.join(self.excel_dir, f)
                    xls = pd.ExcelFile(file_path)
                    target_sheet = "全時段合併表" if "全時段合併表" in xls.sheet_names else xls.sheet_names[0]
                    df = pd.read_excel(xls, sheet_name=target_sheet)
                    df['來源檔案'] = f
                    records.append(df)
                except Exception:
                    pass

        if records:
            df_full = pd.concat(records, ignore_index=True)
            
            # 強健的欄位名稱自動對應 (支援 WRA 英文與中文原欄位)
            rename_mapping = {}
            for c in df_full.columns.tolist():
                c_str = str(c).strip()
                if c_str in ['StNo', '測站編號', '測站代號', 'stno']: rename_mapping[c] = '測站代號'
                elif c_str in ['StName', '站名', '測站名稱', 'stname']: rename_mapping[c] = '測站名稱'
                elif c_str in ['AdmiName', '縣市名稱', '縣市', 'adminame']: rename_mapping[c] = '縣市'
                elif c_str in ['Rain', '雨量', '累積雨量(mm)', '累積雨量_mm', 'rain']: rename_mapping[c] = '累積雨量_mm'
                elif c_str in ['EventNo', '事件編號', '事件代碼', 'eventno']: rename_mapping[c] = '事件代碼'
                elif c_str in ['EventName', '災害名稱', '事件名稱', 'eventname']: rename_mapping[c] = '事件名稱'
                elif c_str in ['BTime', '開始時間', '降雨起時間', 'btime']: rename_mapping[c] = '降雨起時間'
                elif c_str in ['ETime', '結束時間', '降雨訖時間', 'etime']: rename_mapping[c] = '降雨訖時間'
                elif c_str in ['Duration', '時段', '統計時長', 'duration']: rename_mapping[c] = '統計時長'

            if rename_mapping:
                df_full = df_full.rename(columns=rename_mapping)

            # 防禦性檢查：確保所有必要欄位絕對存在，避免 KeyError
            required_cols = ['測站代號', '測站名稱', '縣市', '累積雨量_mm', '事件代碼', '事件名稱', '統計時長', '來源檔案']
            for req_col in required_cols:
                if req_col not in df_full.columns:
                    df_full[req_col] = "-"

            df_full['測站代號'] = df_full['測站代號'].astype(str).str.strip()
            df_full['測站名稱'] = df_full['測站名稱'].astype(str).str.strip()
            df_full['縣市'] = df_full['縣市'].astype(str).str.strip()
            df_full['累積雨量_mm'] = pd.to_numeric(df_full['累積雨量_mm'], errors='coerce').fillna(0.0)
            
            def norm_duration(d_str):
                d_str = str(d_str).strip()
                if "總累計" in d_str or "事件總" in d_str or d_str == "0":
                    return "事件總累計"
                for h in [1, 3, 6, 12, 24]:
                    if f"{h}小時" in d_str or f"{h}h" in d_str.lower():
                        return f"連續{h}小時"
                return d_str
            df_full['統計時長'] = df_full['統計時長'].apply(norm_duration)
            return df_full
            
        return pd.DataFrame()

    def _get_station_aliases(self, station_id):
        candidates = set()
        if station_id and pd.notna(station_id):
            st_id = str(station_id).strip()
            candidates.add(st_id)
            if len(st_id) >= 3 and st_id.startswith(('C0', 'C1', 'C2')):
                suffix = st_id[2:]
                for pfx in ['C0', 'C1', 'C2']:
                    candidates.add(pfx + suffix)
        return list(candidates)

    def _find_event_fallback(self, event_df_all, county, town, vill):
        def check_streams(sub_streams):
            for _, r in sub_streams.iterrows():
                sid = r.get('STID1') or r.get('stid1')
                sname = r.get('STName1') or r.get('stname1')
                sid2 = r.get('STID2') or r.get('stid2')
                sname2 = r.get('STName2') or r.get('stname2')
                
                for s_id, s_name in [(sid, sname), (sid2, sname2)]:
                    if s_id and s_name:
                        cand = self._get_station_aliases(s_id)
                        matched = event_df_all[(event_df_all['測站代號'].isin(cand)) | ((event_df_all['測站名稱'] == s_name) & (event_df_all['縣市'] == county))]
                        if not matched.empty:
                            return matched, s_name, s_id
            return pd.DataFrame(), None, None

        c_col = 'County' if 'County' in self.df_debris.columns else 'county'
        t_col = 'Town' if 'Town' in self.df_debris.columns else ('town' if 'town' in self.df_debris.columns else 'township')
        v_col = 'Vill' if 'Vill' in self.df_debris.columns else ('vill' if 'vill' in self.df_debris.columns else 'villages')

        for streams, lvl in [
            (self.df_debris[(self.df_debris[c_col]==county) & (self.df_debris[t_col]==town) & (self.df_debris[v_col]==vill)], f"同村里 ({town}{vill})"),
            (self.df_debris[(self.df_debris[c_col]==county) & (self.df_debris[t_col]==town)], f"同鄉鎮 ({town})"),
            (self.df_debris[self.df_debris[c_col]==county], f"同縣市 ({county})")
        ]:
            m, name, sid = check_streams(streams)
            if not m.empty:
                return m, name, sid, lvl
        return pd.DataFrame(), None, None, None

    def execute_query(self, debris_no, event_keyword=None):
        if self.all_rain_records.empty:
            return "❌ 歷史雨量資料庫尚未載入或無資料，請確認 wra_rain_db/excel_data 路徑。"

        d_col = 'DebrisNO' if 'DebrisNO' in self.df_debris.columns else ('debrisno' if 'debrisno' in self.df_debris.columns else 'stream_id')
        stream_info = self.df_debris[self.df_debris[d_col] == debris_no]
        if stream_info.empty:
            return f"❌ 查無土石流潛勢溪流編號：{debris_no}。"

        stream = stream_info.iloc[0]
        
        def get_s_val(s, keys, default=""):
            for k in keys:
                if k in s and pd.notna(s[k]):
                    return s[k]
            return default

        county = get_s_val(stream, ['County', 'county'])
        town = get_s_val(stream, ['Town', 'town', 'township'])
        vill = get_s_val(stream, ['Vill', 'vill', 'villages'])
        st1_id = get_s_val(stream, ['STID1', 'stid1'])
        st1_name = get_s_val(stream, ['STName1', 'stname1'])
        st2_id = get_s_val(stream, ['STID2', 'stid2'])
        st2_name = get_s_val(stream, ['STName2', 'stname2'])
        alert_val = get_s_val(stream, ['AlertValue', 'alert_value'], 0)

        st1_cand = self._get_station_aliases(st1_id)
        df_sub = self.all_rain_records[(self.all_rain_records['測站代號'].isin(st1_cand)) | ((self.all_rain_records['測站名稱'] == st1_name) & (self.all_rain_records['縣市'] == county))].copy()
        active_name, active_id = st1_name, st1_id

        if df_sub.empty and st2_id:
            st2_cand = self._get_station_aliases(st2_id)
            df_sub = self.all_rain_records[(self.all_rain_records['測站代號'].isin(st2_cand)) | ((self.all_rain_records['測站名稱'] == st2_name) & (self.all_rain_records['縣市'] == county))].copy()
            active_name, active_id = st2_name, st2_id

        if df_sub.empty:
            return f"❌ 參考雨量站 [{active_name}] 於歷史雨量庫中查無紀錄。"

        hist_max = {}
        for d in self.durations:
            df_d = df_sub[df_sub['統計時長'] == d]
            if not df_d.empty:
                top = df_d.sort_values(by='累積雨量_mm', ascending=False).iloc[0]
                hist_max[d] = f"{top['累積雨量_mm']} mm [{top['事件名稱']} ({top['事件代碼']})]"
            else:
                hist_max[d] = "-"

        event_info = None
        if event_keyword:
            keyword_str = str(event_keyword).strip()
            df_ev_all = pd.DataFrame()

            m_date = re.search(r'(\d{2})(\d{2})', keyword_str)
            if m_date and len(keyword_str) <= 8:
                mm, dd = m_date.groups()
                try:
                    base_dt = datetime(2024, int(mm), int(dd))
                    prev_dt = base_dt - timedelta(days=1)
                    next_dt = base_dt + timedelta(days=1)
                    window_dates = [f"{mm}{dd}", prev_dt.strftime("%m%d"), next_dt.strftime("%m%d")]
                except:
                    window_dates = [f"{mm}{dd}"]

                matched_rows = []
                for _, row in self.all_rain_records.iterrows():
                    row_str = f"{row.get('事件名稱', '')} {row.get('來源檔案', '')} {row.get('事件代碼', '')}"
                    if any(wd in row_str for wd in window_dates):
                        matched_rows.append(row)
                if matched_rows:
                    df_ev_all = pd.DataFrame(matched_rows)

            if df_ev_all.empty:
                clean_kw = re.sub(r'(颱風|豪雨|大雨|\(\d{4}\)|\d{4}_?)', '', keyword_str).strip()
                df_ev_all = self.all_rain_records[
                    self.all_rain_records['事件代碼'].str.contains(keyword_str, case=False, na=False) |
                    self.all_rain_records['事件名稱'].str.contains(clean_kw if clean_kw else keyword_str, case=False, na=False) |
                    self.all_rain_records['來源檔案'].str.contains(keyword_str, case=False, na=False)
                ].copy()

            if not df_ev_all.empty:
                ev_name = df_ev_all.iloc[0]['事件名稱']
                ev_code = df_ev_all.iloc[0]['事件代碼']
                
                df_ev_matched = df_ev_all[(df_ev_all['測站代號'].isin(st1_cand)) | ((df_ev_all['測站名稱'] == st1_name) & (df_ev_all['縣市'] == county))]
                src_lvl = "原參考站"
                if df_ev_matched.empty:
                    df_ev_matched, m_name, m_sid, src_lvl = self._find_event_fallback(df_ev_all, county, town, vill)

                ev_vals = {}
                if not df_ev_matched.empty:
                    for d in self.durations:
                        sub_d = df_ev_matched[df_ev_matched['統計時長'] == d]
                        if not sub_d.empty:
                            top_sub = sub_d.sort_values(by='累積雨量_mm', ascending=False).iloc[0]
                            ev_vals[d] = f"{top_sub['累積雨量_mm']} mm"
                        else:
                            ev_vals[d] = "無提供"
                event_info = {'name': f"{ev_name} ({ev_code})", 'source': src_lvl, 'vals': ev_vals}

        response_text = f"📍 **【土石流潛勢溪流雨量查詢結果】**\n"
        response_text += f"- **溪流編號**：`{debris_no}`\n"
        response_text += f"- **地理位置**：{county}{town}{vill}\n"
        response_text += f"- **警戒基準值**：`{alert_val} mm`\n"
        response_text += f"- **參考雨量站**：{active_name} ({active_id})\n\n"

        response_text += f"📊 **【歷史最大降雨紀錄】**\n"
        for d in self.durations:
            response_text += f"• {d}：{hist_max[d]}\n"

        if event_info:
            response_text += f"\n🌪️ **【指定事件降雨：{event_info['name']}】** *(參考來源: {event_info['source']})*\n"
            for d in self.durations:
                response_text += f"• {d}：{event_info['vals'].get(d, '-')}\n"
        elif event_keyword:
            response_text += f"\n⚠️ **【指定事件檢索】**：查無符合 [{event_keyword}]（含前後一天時間視窗）的降雨事件紀錄。\n"

        return response_text
