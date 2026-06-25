import os
import requests
import pandas as pd
from datetime import datetime
import time

# --- KONFIGURATION ---
# Hämtar API-nyckel från miljövariabler (Environment Variables) i GitHub Actions
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "DIN_API_NYCKEL_HÄR")

def fetch_from_polymarket(target_url):
    """
    Hjälpfunktion för att anropa ScraperAPI. 
    Tvingar 'premium': 'true' för att undvika 403/404 från Cloudflare.
    """
    payload = {
        'api_key': SCRAPER_API_KEY,
        'url': target_url,
        'premium': 'true',
        'render': 'false' # Vi behöver bara JSON-data, inte rendera JavaScript
    }
    
    try:
        response = requests.get('http://api.scraperapi.com', params=payload, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[FEL] API-anrop misslyckades för URL: {target_url} | Fel: {e}")
        return None

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Startar Smart Money Scraper...")
    
    # 1. Hämta Topp 50 från Leaderboard
    leaderboard_url = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit=50"
    print("[1/4] Hämtar Leaderboard (Top 50)...")
    
    leaderboard_data = fetch_from_polymarket(leaderboard_url)
    
    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("[AVBRYTER] Kunde inte hämta Leaderboard-data korrekt.")
        return

    traders = []
    
    # 2. Parsa Leaderboard & Beräkna Profit Rate
    print("[2/4] Beräknar lokal Profit Rate och filtrerar noll-volym...")
    for item in leaderboard_data:
        # Säkerställ att vi har en adress
        wallet = item.get('address') or item.get('user') or item.get('proxyWallet')
        if not wallet:
            continue
            
        profit = float(item.get('pnl', 0))
        volume = float(item.get('volume', 0))
        
        # Hoppa över om volymen är 0 för att undvika DivisionByZero
        if volume <= 0:
            continue
            
        profit_rate = profit / volume
        
        traders.append({
            'Wallet': wallet,
            'Profit': profit,
            'Volume': volume,
            'Profit_Rate': profit_rate
        })
        
    # Sortera lokalt (Högst Profit Rate först) och ta Topp 10
    top_10_traders = sorted(traders, key=lambda x: x['Profit_Rate'], reverse=True)[:10]
    
    # 3. Detaljerade API-anrop endast för Top 10
    print(f"[3/4] Hämtar User Stats för de {len(top_10_traders)} mest effektiva traders...")
    
    final_results = []
    
    for i, trader in enumerate(top_10_traders):
        wallet = trader['Wallet']
        stats_url = f"https://data-api.polymarket.com/v1/users/{wallet}/stats?timePeriod=WEEK"
        
        print(f"  -> Fetching stats for trader {i+1}/10 ({wallet[:8]}...)")
        stats_data = fetch_from_polymarket(stats_url)
        
        # Sätt standardvärden (Fallbacks)
        name = wallet[:8] + "..."
        win_rate = 0.0
        total_trades = 0
        
        if stats_data:
            # Polymarkets JSON-struktur kan variera, vi letar efter flera möjliga nycklar
            name = stats_data.get('userName') or stats_data.get('username') or name
            
            # Försök hämta totala trades
            total_trades = stats_data.get('totalTrades') or stats_data.get('tradesCount') or 0
            
            # Försök hämta win rate (antingen direkt, eller beräkna från vinst/förlust)
            if 'winRate' in stats_data:
                win_rate = float(stats_data['winRate'])
            else:
                winning_trades = stats_data.get('winningTrades', 0)
                if total_trades > 0:
                    win_rate = (winning_trades / total_trades) * 100
        else:
            print(f"     [VARNING] Kunde inte hämta data för {wallet[:8]}. Använder fallback-värden.")

        # Uppdatera traderns data
        trader_full_data = {
            'Wallet': wallet,
            'Name': name,
            'Win_Rate_%': round(win_rate, 2),
            'Total_Trades': int(total_trades),
            'Profit': round(trader['Profit'], 2),
            'Volume': round(trader['Volume'], 2),
            'Profit_Rate': round(trader['Profit_Rate'], 4)
        }
        final_results.append(trader_full_data)
        
        # Vänta 0.5 sekunder för att undvika Rate Limits
        time.sleep(0.5)

    # 4. Skapa DataFrame och spara till CSV
    print("[4/4] Skapar CSV och sammanfattning...")
    df = pd.DataFrame(final_results)
    
    # Ordna kolumnerna snyggt
    cols_order = ['Wallet', 'Name', 'Win_Rate_%', 'Total_Trades', 'Profit', 'Volume', 'Profit_Rate']
    df = df[cols_order]
    
    # Skapa filnamn med dagens datum
    date_str = datetime.now().strftime('%Y%m%d')
    csv_filename = f"smart_money_{date_str}.csv"
    
    df.to_csv(csv_filename, index=False)
    
    print(f"\n✅ Framgång! Sparad till filen: {csv_filename}")
    print("\n--- TOPP 5 SMART MONEY TRADERS ---")
    
    # Skriv ut en snygg sammanfattning av de 5 bästa i terminalen (viktigt för GitHub Actions loggar)
    top_5 = df.head(5)
    for index, row in top_5.iterrows():
        print(f"{index+1}. {row['Name']:<15} | Win Rate: {row['Win_Rate_%']:>5}% | Profit Rate: {row['Profit_Rate']:.4f} | Vol: ${row['Volume']}")

if __name__ == "__main__":
    main()
