import os
import re
import json
import pandas as pd

class DebrisRainfallAgentCore:
    def __init__(self, db_dir="水利署全歷史事件雨量資料庫/事件明細Excel", debris_json="GetDebrisRainData.json"):
        script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
        self.excel_dir = db_dir if os.path.exists(db_dir) else os.path.join(script_dir, db_dir)
        
        json_path = debris_json if os.path.isabs(debris_json) else os.path.join(script_dir, debris_json)
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
                    xls = pd.ExcelFile(os.path.join(self.excel_dir, f))
                    if "全時段合併表" in xls.sheet_names:
                        records.append(pd.read_excel(xls, sheet_name="全時段合併表"))
                except:
                    pass
        if records:
            df = pd.concat(records, ignore_index=True)
            df['測站代號'] = df['測站代號'].astype(str).str.strip()
            df['測站名稱'] = df['測站名稱'].astype(str).str.strip()
            def norm_duration(d_str):
                d_str = str(d_str).strip()
                if "總累計" in d_str or "事件總" in d_str or d_str == "0":
                    return "事件總累計"
                for h in [1, 3, 6, 12, 24]:
                    if f"{h}小時" in d_str or f"{h}h" in d_str.lower():
                        return f"連續{h}小時"
                return d_str
            df['統計時長'] = df['統計時長'].apply(norm_duration)
            return df
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
                for sid, sname in [(r['STID1'], r['STName1']), (r.get('STID2'), r.get('STName2'))]:
                    if sid and sname:
                        cand = self._get_station_aliases(sid)
                        matched = event_df_all[(event_df_all['測站代號'].isin(cand)) | ((event_df_all['測站名稱'] == sname) & (event_df_all['縣市'] == county))]
                        if not matched.empty:
                            return matched, sname, sid
            return pd.DataFrame(), None, None

        # 同村里 -> 同鄉鎮 -> 同縣市
        for streams, lvl in [
            (self.df_debris[(self.df_debris['County']==county) & (self.df_debris['Town']==town) & (self.df_debris['Vill']==vill)], f"同村里 ({town}{vill})"),
            (self.df_debris[(self.df_debris['County']==county) & (self.df_debris['Town']==town)], f"同鄉鎮 ({town})"),
            (self.df_debris[self.df_debris['County']==county], f"同縣市 ({county})")
        ]:
            m, name, sid = check_streams(streams)
            if not m.empty:
                return m, name, sid, lvl
        return pd.DataFrame(), None, None, None

    def execute_query(self, debris_no, event_keyword=None):
        stream_info = self.df_debris[self.df_debris['DebrisNO'] == debris_no]
        if stream_info.empty:
            return f"❌ 查無土石流潛勢溪流編號：{debris_no}，請確認代碼是否正確（例如：屏縣DF021）。"

        stream = stream_info.iloc[0]
        county, town, vill = stream['County'], stream['Town'], stream['Vill']
        st1_id, st1_name = stream['STID1'], stream['STName1']
        st2_id, st2_name = stream.get('STID2'), stream.get('STName2')

        # 尋找歷史最大雨量測站
        st1_cand = self._get_station_aliases(st1_id)
        df_sub = self.all_rain_records[(self.all_rain_records['測站代號'].isin(st1_cand)) | ((self.all_rain_records['測站名稱'] == st1_name) & (self.all_rain_records['縣市'] == county))].copy()
        active_name, active_id = st1_name, st1_id

        if df_sub.empty and st2_id:
            st2_cand = self._get_station_aliases(st2_id)
            df_sub = self.all_rain_records[(self.all_rain_records['測站代號'].isin(st2_cand)) | ((self.all_rain_records['測站名稱'] == st2_name) & (self.all_rain_records['縣市'] == county))].copy()
            active_name, active_id = st2_name, st2_id

        if df_sub.empty:
            return f"❌ 參考雨量站 [{active_name}] 於歷史雨量庫中查無紀錄。"

        # 計算歷史最大
        hist_max = {}
        for d in self.durations:
            df_d = df_sub[df_sub['統計時長'] == d]
            if not df_d.empty:
                top = df_d.sort_values(by='累積雨量_mm', ascending=False).iloc[0]
                hist_max[d] = f"{top['累積雨量_mm']} mm [{top['事件名稱']} ({top['事件代碼']})]"
            else:
                hist_max[d] = "-"

        # 若指定特定事件
        event_info = None
        if event_keyword:
            clean_kw = re.sub(r'(颱風|豪雨|大雨|\(\d{4}\)|\d{4}_?)', '', event_keyword).strip()
            df_ev_all = self.all_rain_records[
                self.all_rain_records['事件代碼'].str.contains(event_keyword, case=False, na=False) |
                self.all_rain_records['事件名稱'].str.contains(clean_kw if clean_kw else event_keyword, case=False, na=False)
            ].copy()

            if not df_ev_all.empty:
                ev_name = df_ev_all.iloc[0]['事件名稱']
                ev_code = df_ev_all.iloc[0]['事件代碼']
                
                # 檢查原測站是否有此事件，無則回退
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

        # 組合結構化文字或 Markdown 回應
        response_text = f"📍 **【土石流潛勢溪流雨量查詢結果】**\n"
        response_text += f"- **溪流編號**：`{debris_no}`\n"
        response_text += f"- **地理位置**：{county}{town}{vill}\n"
        response_text += f"- **警戒基準值**：`{stream['AlertValue']} mm`\n"
        response_text += f"- **參考雨量站**：{active_name} ({active_id})\n\n"

        response_text += f"📊 **【歷史最大降雨紀錄】**\n"
        for d in self.durations:
            response_text += f"• {d}：{hist_max[d]}\n"

        if event_info:
            response_text += f"\n🌪️ **【指定事件降雨：{event_info['name']}】** *(參考來源: {event_info['source']})*\n"
            for d in self.durations:
                response_text += f"• {d}：{event_info['vals'].get(d, '-')}\n"

        return response_text
