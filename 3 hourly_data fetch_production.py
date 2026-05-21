import requests
import pandas as pd
import os
import gspread
from io import StringIO
from datetime import datetime, timedelta
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURATION ---
JSON_FILE = "service_account.json"
STATIONS = {
    "42647": "Ahmedabad",
    "42654": "Gandhinagar",
    "42737": "Rajkot",
    "42840": "Surat",
    "42748": "Vadodara",
    "42838": "Bhavnagar",
    "42539": "Deesa",
    "42634": "Bhuj",
    "42631": "Naliya",
    "42639": "New Kandla",
    "42730": "Okha",
    "42731": "Dwarka",
    "42830": "Porbandar",
    "42909": "Veraval",
    "42834": "Amreli",
    "42914": "Diu",
    "42740": "Surendranagar",
    "42744": "Vallabh Vidyanagar",
    "42837": "Mahuva"
}

def get_station_data(station_id, station_name):
    print(f"-> Accessing Ogimet for {station_name}...")
    url = f"https://www.ogimet.com/cgi-bin/gsynres?lang=en&ind={station_id}&ndays=2&decoded=yes"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=20)
        
        tables = pd.read_html(StringIO(response.text), header=0)
        df = next((t for t in tables if 'Date' in t.columns), None)
        
        if df is None: return None

        clean = pd.DataFrame()

        # --- TWO-COLUMN TIMESTAMP MERGE ---
        date_part = df.iloc[:, 0].astype(str).str.strip()
        time_part = df.iloc[:, 1].astype(str).str.strip()
        
        combined_dt = date_part + " " + time_part
        clean['Timestamp_UTC'] = pd.to_datetime(combined_dt, errors='coerce')

        clean['Timestamp_IST'] = clean['Timestamp_UTC'] + timedelta(hours=5, minutes=30)
        clean['Timestamp_UTC_Label'] = clean['Timestamp_UTC'].dt.strftime('%Y-%m-%d %H:%M') + ' UTC'
        clean['Timestamp_IST_Label'] = clean['Timestamp_IST'].dt.strftime('%Y-%m-%d %H:%M') + ' IST'
        
        clean['Station'] = station_name
        clean['Temp'] = pd.to_numeric(df['T (C)'], errors='coerce')
        clean['Tmax'] = pd.to_numeric(df.get('Tmax (C)', pd.Series(dtype=float)), errors='coerce')
        clean['Tmin'] = pd.to_numeric(df.get('Tmin (C)', pd.Series(dtype=float)), errors='coerce')
        clean['DewPoint'] = pd.to_numeric(df.get('Td (C)', pd.Series(dtype=float)), errors='coerce')
        clean['Humidity'] = pd.to_numeric(df['Hr %'], errors='coerce')
        clean['Wind_Dir'] = df.get('ddd', pd.Series(dtype=str)).astype(str)
        clean['WindSpd'] = pd.to_numeric(df['ff kmh'], errors='coerce')
        clean['Pressure'] = pd.to_numeric(df['P sea hPa'], errors='coerce')
        clean['Visibility'] = pd.to_numeric(df.get('Vis km', pd.Series(dtype=float)), errors='coerce')
        clean['Condition'] = df.get('ww', pd.Series(dtype=str)).astype(str)
        clean['Prec_Raw'] = df['Prec (mm)'].astype(str)
        
        return clean.dropna(subset=['Timestamp_UTC'])
    except Exception as e:
        print(f"   [!] Error: {e}")
        return None

def process_rainfall(df):
    processed = []
    # Sort oldest to newest for math
    df = df.sort_values('Timestamp_UTC', ascending=True)
    
    for i in range(len(df)):
        row = df.iloc[i]
        prec_str = str(row['Prec_Raw'])
        
        try:
            val = prec_str.split('/')[0] if '/' in prec_str else "0.0"
            curr_cum = float(val) if val.replace('.','',1).isdigit() else 0.0
        except: 
            curr_cum = 0.0
        
        if i > 0:
            try:
                prev_prec_str = str(df.iloc[i-1]['Prec_Raw'])
                prev_val = prev_prec_str.split('/')[0] if '/' in prev_prec_str else "0.0"
                prev_cum = float(prev_val) if prev_val.replace('.','',1).isdigit() else 0.0
                diff = curr_cum - prev_cum
                rain_3h = curr_cum if diff < 0 else diff
            except:
                rain_3h = 0.0
        else:
            rain_3h = 0.0

        d = row.to_dict()
        d['Cumulative_Rain'] = curr_cum
        d['Reported Rain'] = round(rain_3h, 2)
        
        d['Timestamp_UTC'] = d.pop('Timestamp_UTC_Label')
        d['Timestamp_IST'] = d.pop('Timestamp_IST_Label')
        if 'Prec_Raw' in d: del d['Prec_Raw']
        processed.append(d)
        
    # Flip back to newest first for display
    return pd.DataFrame(processed).sort_values('Timestamp_UTC', ascending=False)

def update_google_sheets(all_data_frames):
    current_month_year = datetime.now().strftime("%B_%Y") 
    target_sheet_name = f"Synops_{current_month_year}"
    
    print(f"\n-> Target Spreadsheet: {target_sheet_name}")

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, scope)
    client = gspread.authorize(creds)
    
    try:
        sh = client.open(target_sheet_name)
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"   [!] Error: Spreadsheet '{target_sheet_name}' not found.")
        return

    for station_name, df_new in all_data_frames.items():
        tab_name = station_name[:31]
        
        try:
            worksheet = sh.worksheet(tab_name)
            # Check Column A for existing timestamps to prevent duplicates
            existing_timestamps = worksheet.col_values(1)[1:] 
            df_to_append = df_new[~df_new['Timestamp_UTC'].isin(existing_timestamps)]
            
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title=tab_name, rows="5000", cols="20")
            worksheet.update(values=[df_new.columns.tolist()], range_name='A1')
            df_to_append = df_new
            print(f"   -> Created new tab: {tab_name}")

        if not df_to_append.empty:
            # Sort ascending for insertion so that the newest row ends up at the very top (Row 2)
            df_to_append = df_to_append.sort_values('Timestamp_UTC', ascending=False)
            df_clean = df_to_append.fillna("")
            new_rows = df_clean.values.tolist()
            
            # Insert at Row 2 (pushes everything else down)
            worksheet.insert_rows(new_rows, row=2)
            print(f"   -> {tab_name}: Successfully added {len(new_rows)} new records at the top.")
        else:
            print(f"   -> {tab_name}: No new data to add.")

def run_sync():
    print(f"\n--- WEATHER SYNC START: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    all_data_frames = {}

    for s_id, s_name in STATIONS.items():
        raw = get_station_data(s_id, s_name)
        if raw is not None and not raw.empty:
            proc = process_rainfall(raw)
            all_data_frames[s_name] = proc
            print(f"   -> {s_name}: Data processed.")

    if all_data_frames:
        update_google_sheets(all_data_frames)
        print(f"\nSUCCESS: All stations synced to Google Sheets.")
    else:
        print("\n[!] No data was fetched to sync.")

if __name__ == "__main__":
    run_sync()
