#!/usr/bin/env python3
"""Isolated curve test for the PolyGun versatility clone.

Reads the clone's passing traders_*.csv, fetches up to 300 recent closed
positions per passing wallet, builds a cumulative realized-PnL curve, and
writes a new CSV. It never changes the original scraper or latest_traders.csv.
"""

import csv
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "PolyGunVersatilityCurve/1.0"
PAGE_SIZE = 50
MAX_POSITIONS = 300
MAX_RETRIES = 3


def fetch_closed_positions(wallet):
    rows = []
    for offset in range(0, MAX_POSITIONS, PAGE_SIZE):
        params = urlencode({
            "user": wallet,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
            "limit": PAGE_SIZE,
            "offset": offset,
        })
        request = Request(
            f"{DATA_API}/closed-positions?{params}",
            headers={"User-Agent": USER_AGENT},
        )
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                with urlopen(request, timeout=20) as response:
                    page = json.loads(response.read().decode("utf-8"))
                if not isinstance(page, list):
                    return rows
                rows.extend(page)
                last_error = None
                break
            except HTTPError as error:
                last_error = error
                if error.code == 429 or error.code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return rows
            except (URLError, TimeoutError, ValueError) as error:
                last_error = error
                time.sleep(1.5 * (attempt + 1))
        if last_error is not None:
            return rows
        if len(page) < PAGE_SIZE:
            break
        time.sleep(0.12)
    return rows[:MAX_POSITIONS]


def curve_metrics(rows):
    points = []
    for row in rows:
        try:
            timestamp = float(row.get("timestamp"))
            pnl = float(row.get("realizedPnl"))
        except (TypeError, ValueError):
            continue
        points.append((timestamp, pnl))

    points.sort(key=lambda item: item[0])
    count = len(points)
    if count < 20:
        return {
            "CurveSignal": "INSUFFICIENT_DATA",
            "CurvePoints": count,
            "CurveTotalPnL": "N/A",
            "CurveMaxDrawdown": "N/A",
            "CurvePositiveShare": "N/A",
            "CurveTrendR2": "N/A",
        }

    cumulative = []
    running_total = 0.0
    for _, pnl in points:
        running_total += pnl
        cumulative.append(running_total)

    n = len(cumulative)
    x_mean = (n - 1) / 2.0
    y_mean = sum(cumulative) / n
    ss_x = sum((index - x_mean) ** 2 for index in range(n))
    ss_y = sum((value - y_mean) ** 2 for value in cumulative)
    covariance = sum(
        (index - x_mean) * (value - y_mean)
        for index, value in enumerate(cumulative)
    )
    slope = covariance / ss_x if ss_x else 0.0
    r2 = (covariance * covariance) / (ss_x * ss_y) if ss_x and ss_y else 0.0

    peak = cumulative[0]
    max_drawdown = 0.0
    for value in cumulative:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)

    total_pnl = cumulative[-1]
    positive_share = sum(1 for _, pnl in points if pnl > 0) / n
    drawdown_ratio = max_drawdown / max(abs(total_pnl), 1.0)

    if total_pnl <= 0 or slope <= 0:
        signal = "NEGATIVE_OR_DOWN"
    elif r2 >= 0.55 and drawdown_ratio <= 1.50 and positive_share >= 0.50:
        signal = "POSITIVE_STABLE"
    elif total_pnl > 0:
        signal = "POSITIVE_VOLATILE"
    else:
        signal = "FLAT_OR_MIXED"

    return {
        "CurveSignal": signal,
        "CurvePoints": n,
        "CurveTotalPnL": round(total_pnl, 2),
        "CurveMaxDrawdown": round(max_drawdown, 2),
        "CurvePositiveShare": round(positive_share, 4),
        "CurveTrendR2": round(r2, 4),
    }


def find_source():
    if len(sys.argv) > 1:
        return sys.argv[1]
    files = sorted(glob.glob("traders_*.csv"), key=os.path.getmtime, reverse=True)
    if not files:
        raise SystemExit("No traders_*.csv source file found")
    return files[0]


def main():
    source = find_source()
    with open(source, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("Source CSV is empty")

    order = {
        "POSITIVE_STABLE": 4,
        "POSITIVE_VOLATILE": 3,
        "FLAT_OR_MIXED": 2,
        "NEGATIVE_OR_DOWN": 1,
        "INSUFFICIENT_DATA": 0,
    }

    for index, row in enumerate(rows, start=1):
        wallet = (row.get("TraderID") or "").strip()
        if wallet.startswith("0x"):
            metrics = curve_metrics(fetch_closed_positions(wallet))
        else:
            metrics = {
                "CurveSignal": "INSUFFICIENT_DATA",
                "CurvePoints": 0,
                "CurveTotalPnL": "N/A",
                "CurveMaxDrawdown": "N/A",
                "CurvePositiveShare": "N/A",
                "CurveTrendR2": "N/A",
            }
        row.update(metrics)
        print(f"Curve check {index}/{len(rows)}: {row.get('Name', wallet)} -> {row['CurveSignal']}")

    rows.sort(
        key=lambda row: (
            order.get(row.get("CurveSignal"), 0),
            float(row.get("StabilityProxyScore") or 0),
            float(row.get("Score") or 0),
        ),
        reverse=True,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = f"versatility_curve_traders_{stamp}.csv"
    fields = list(rows[0].keys())
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Read {len(rows)} passing traders from {source}")
    print(f"Wrote {len(rows)} rows to {output}")
    print("No rows were removed. CurveSignal is ranking-only in this test.")


if __name__ == "__main__":
    main()