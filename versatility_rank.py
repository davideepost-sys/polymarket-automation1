#!/usr/bin/env python3
"""PolyGun versatility ranking clone.

Reads an existing traders_*.csv, adds a transparent proxy ranking, and writes
versatility_traders_*.csv. It never deletes rows and never changes the source CSV.
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone


def number(row, key):
    value = row.get(key, "")
    if value is None or str(value).strip() in {"", "N/A", "None", "null"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp(value):
    return max(0.0, min(1.0, value))


def scale(value, low, high):
    if value is None:
        return None
    if high <= low:
        return 1.0
    return clamp((value - low) / (high - low))


def proxy(row):
    pr = number(row, "ProfitRate")
    wr = number(row, "WinRate")
    rr = number(row, "RR")
    weekly_trades = number(row, "WeeklyTrades")
    markets = number(row, "MarketCount")
    avg_win = number(row, "AvgWin")
    avg_loss = number(row, "AvgLoss")
    hold = number(row, "AvgHoldingDays")

    parts = []
    weights = []

    if pr is not None:
        parts.append(scale(pr, 0.10, 0.75))
        weights.append(0.25)
    if wr is not None:
        parts.append(scale(wr, 60.0, 100.0))
        weights.append(0.20)
    if rr is not None:
        parts.append(scale(min(rr, 5.0), 0.50, 3.0))
        weights.append(0.20)
    if weekly_trades is not None:
        parts.append(scale(min(weekly_trades, 150.0), 21.0, 150.0))
        weights.append(0.10)
    if markets is not None:
        parts.append(scale(min(markets, 30.0), 1.0, 30.0))
        weights.append(0.10)
    if hold is not None:
        hold_score = 1.0 - min(abs(hold - 0.50) / 1.50, 1.0)
        parts.append(clamp(hold_score))
        weights.append(0.05)
    if avg_win is not None and avg_loss is not None and avg_win > 0 and avg_loss < 0:
        loss_ratio = abs(avg_loss) / avg_win
        parts.append(1.0 - min(loss_ratio / 2.0, 1.0))
        weights.append(0.10)

    if not parts or not sum(weights):
        return None, "INSUFFICIENT_DATA"

    score = 100.0 * sum(p * w for p, w in zip(parts, weights)) / sum(weights)
    available = len(parts)
    if available < 3:
        label = "INSUFFICIENT_DATA"
    elif score >= 70:
        label = "PROXY_STRONG"
    elif score >= 50:
        label = "PROXY_BALANCED"
    else:
        label = "PROXY_MIXED"
    return round(score, 2), label


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else None
    if not source:
        candidates = sorted(glob.glob("traders_*.csv"), key=os.path.getmtime, reverse=True)
        if not candidates:
            raise SystemExit("No traders_*.csv found")
        source = candidates[0]

    with open(source, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("Source CSV is empty")

    for row in rows:
        score, label = proxy(row)
        row["StabilityProxyScore"] = "N/A" if score is None else f"{score:.2f}"
        row["StabilityProxyClass"] = label
        row["RRQuality"] = "OBSERVED" if number(row, "RR") is not None else "N/A"

    rows.sort(
        key=lambda row: (
            row["StabilityProxyClass"] != "INSUFFICIENT_DATA",
            float(row["StabilityProxyScore"]) if row["StabilityProxyScore"] != "N/A" else -1.0,
        ),
        reverse=True,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = f"versatility_traders_{stamp}.csv"
    fields = list(rows[0].keys())
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Read {len(rows)} rows from {source}")
    print(f"Wrote {len(rows)} rows to {output}")
    print("No rows were removed; this is a ranking-only experiment.")


if __name__ == "__main__":
    main()
