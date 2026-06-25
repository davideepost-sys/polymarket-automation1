import os
import sys
import requests
import pandas as pd
from datetime import datetime
import json

# ============================================================
# CONFIGURATION
# ============================================================

# The Graph API endpoint for Beefy P&L subgraph on Polygon
SUBGRAPH_URL = "https://api.thegraph.com/subgraphs/name/beefyfinance/polymarket-pnl"

# ============================================================
# DO NOT CHANGE BELOW THIS LINE
# ============================================================

def query_subgraph(query):
    """Execute a GraphQL query against the Beefy P&L subgraph"""
    url = SUBGRAPH_URL
    
    try:
        print(f"  Querying subgraph...")
        response = requests.post(url, json={'query': query}, timeout=30)
        print(f"  Response code: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        
        if 'errors' in data:
            print(f"⚠️ GraphQL errors: {data['errors']}")
            return None
            
        return data.get('data', {})
    except Exception as e:
        print(f"❌ Error querying subgraph: {e}")
        return None

def fetch_top_traders(limit=50, min_trades=10):
    """
    Fetch top traders from the Beefy P&L subgraph.
    Returns traders ranked by total realized PnL with win rate and trade count.
    """
    query = f"""
    {{
      accounts(
        first: {limit},
        orderBy: totalRealizedPnl,
        orderDirection: desc,
        where: {{ numTrades_gte: "{min_trades}" }}
      ) {{
        id
        numTrades
        totalRealizedPnl
        totalUnrealizedPnl
        totalFeesPaid
        winRate
        profitFactor
        maxDrawdown
        numWinningPositions
        numLosingPositions
        lastTradedTimestamp
      }}
    }}
    """
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {limit} traders from Beefy P&L subgraph...")
    
    result = query_subgraph(query)
    
    if not result or 'accounts' not in result:
        print("❌ Failed to fetch trader data from subgraph.")
        return []
    
    return result['accounts']

def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer (Beefy P&L)")
    print("=" * 60)
    
    # Step 1: Fetch traders from the subgraph
    raw_data = fetch_top_traders(limit=50, min_trades=10)
    
    if not raw_data:
        print("❌ No traders found. The subgraph may be down or the query may have issues.")
        print("💡 Trying alternative approach...")
        sys.exit(1)
    
    print(f"✅ Retrieved {len(raw_data)} traders with at least 10 trades.")
    
    # Step 2: Process the data
    results = []
    
    for entry in raw_data:
        wallet = entry.get('id', '')
        if not wallet:
            continue
        
        # Extract metrics
        total_trades = int(entry.get('numTrades', 0))
        total_pnl = float(entry.get('totalRealizedPnl', 0))
        win_rate = float(entry.get('winRate', 0))
        profit_factor = float(entry.get('profitFactor', 0))
        max_drawdown = float(entry.get('maxDrawdown', 0))
        winning_trades = int(entry.get('numWinningPositions', 0))
        losing_trades = int(entry.get('numLosingPositions', 0))
        
        # Win rate comes as a decimal (0.45 = 45%)
        win_rate_percent = round(win_rate * 100, 1)
        
        results.append({
            'Wallet': wallet,
            'Total_Trades': total_trades,
            'Winning_Trades': winning_trades,
            'Losing_Trades': losing_trades,
            'Win_Rate_%': win_rate_percent,
            'Total_PnL': round(total_pnl, 2),
            'Profit_Factor': round(profit_factor, 2),
            'Max_Drawdown_%': round(max_drawdown * 100, 1)
        })
    
    if not results:
        print("❌ No valid traders found.")
        sys.exit(1)
    
    # Step 3: Create DataFrame and sort by Win Rate
    df = pd.DataFrame(results)
    
    # Sort by Win Rate (highest first)
    df_sorted = df.sort_values(by='Win_Rate_%', ascending=False)
    
    # Step 4: Save to CSV
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_sorted.to_csv(filename, index=False)
    
    print("\n" + "=" * 60)
    print(f"✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df_sorted)} traders")
    print(f"💡 All data from free Beefy P&L subgraph – 0 API credits used!")
    
    print("\n=== TOP 5 TRADERS BY WIN RATE ===")
    print(df_sorted.head(5)[['Wallet', 'Win_Rate_%', 'Total_Trades', 'Profit_Factor']].to_string(index=False))
    
    print("\n" + "=" * 60)
    print("✅ Analysis complete!")

if __name__ == "__main__":
    analyze_traders()
