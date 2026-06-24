import requests
import pandas as pd
from datetime import datetime
import sys

def fetch_top_traders():
    url = "https://lb-api.polymarket.com/leaderboard"
    params = {
        "window": "1w", 
        "limit": 100,   
        "sortBy": "volume" 
    }
    
    # Updated headers to mimic a modern desktop browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://polymarket.com/"
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Pinging Polymarket Leaderboard API...")
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        print(f"API Response Code: {response.status_code}")
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP Error occurred: {e}")
        if response.status_code == 403:
            print("👉 Error 403 means Polymarket is blocking the GitHub cloud runner IP or requires advanced headers.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Connection error: {e}")
        sys.exit(1)

def analyze_traders():
    data = fetch_top_traders()
    
    # Verify the API returned a readable list
    if not isinstance(data, list):
        print(f"❌ Unexpected data format. Expected a list, but received: {type(data)}")
        print(f"Data snippet: {str(data)[:300]}")
        sys.exit(1)
        
    print(f"Successfully downloaded data for {len(data)} traders. Processing metrics...")
    traders = []
    
    for idx, entry in enumerate(data):
        try:
            # Polymarket sometimes changes key names; check address or user fallback
            wallet = entry.get('address') or entry.get('user') or f"Unknown_{idx}"
            
            # Ensure numbers are treated as floats, defaulting to 0 if missing/null
            profit = float(entry.get('amount') or 0)
            volume = float(entry.get('volume') or 0)
            
            if volume <= 0:
                continue
                
            # Calculate your custom efficiency metric
            profit_rate = profit / volume
            
            traders.append({
                'Wallet': wallet,
                'Profit': profit,
                'Volume': volume,
                'Profit_Rate': profit_rate
            })
        except Exception as e:
            # Individual entry errors won't crash the entire machine now
            print(f"⚠️ Skipping row {idx} due to calculation error: {e}")
            continue
            
    if not traders:
        print("❌ No valid records found with volume greater than 0.")
        sys.exit(1)
        
    # Process into Pandas
    df = pd.DataFrame(traders)
    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)
    
    # Generate dynamic file name matching 'smart_money_*.csv'
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_sorted.to_csv(filename, index=False)
    print(f"✅ Data successfully sorted and saved as: {filename}")
    
    print("\n=== TOP 5 MOST EFFICIENT TRADERS OF THE WEEK ===")
    print(df_sorted.head(5).to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
