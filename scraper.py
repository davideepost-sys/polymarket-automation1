import requests
import pandas as pd
import time

def fetch_top_traders():
    # Polymarket's public leaderboard API (No API key needed)
    url = "https://lb-api.polymarket.com/leaderboard"
    params = {
        "window": "1w", # 1 week window
        "limit": 100,   # Top 100 traders
        "sortBy": "volume" # Pull by volume first, then filter by custom metric
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }
    
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()
    return response.json()

def analyze_traders():
    data = fetch_top_traders()
    traders = []
    
    for entry in data:
        wallet = entry.get('address')
        profit = entry.get('amount', 0) # PnL amount
        volume = entry.get('volume', 0)
        
        # Skip accounts with 0 volume to avoid division by zero errors
        if volume <= 0:
            continue
            
        # Calculate custom metric
        profit_rate = profit / volume
        
        traders.append({
            'Wallet': wallet,
            'Profit': profit,
            'Volume': volume,
            'Profit_Rate': profit_rate
        })
        
    # Convert to a DataFrame for easy sorting
    df = pd.DataFrame(traders)
    
    # Sort by Best Profit Rate (Descending)
    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)
    
    # Save the output to a CSV file
    df_sorted.to_csv("daily_smart_money.csv", index=False)
    print("Run complete. Top 5 Traders:")
    print(df_sorted.head(5))

if __name__ == "__main__":
    analyze_traders()
