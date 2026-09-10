#!/usr/bin/env python3
"""PolyGun curve-quality test.

This file is intentionally isolated from the production scraper and Telegram bot.
It reads a passing traders_*.csv, fetches closed positions, and writes a new CSV.

Strategy:
  1. Fetch up to INITIAL_POSITIONS (300) closed positions.
  2. Measure the real calendar coverage of those positions.
  3. If coverage is shorter than MIN_TIME_COVERAGE_DAYS, extend up to
     MAX_POSITIONS (1000) for that trader only.
  4. Aggregate realized PnL by UTC calendar day, then judge the cumulative
     daily curve rather than the trade-by-trade curve.
  5. Never delete source rows. The output is a ranking/diagnostic experiment.

No claim is made that this guarantees a particular percentage of visually good
traders. Thresholds are explicit so they can be evaluated and adjusted using
real output instead of being hidden in the code.
"""

import csv
import glob
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "PolyGunVersatilityCurve/2.0"

PAGE_SIZE = 50
INITIAL_POSITIONS = 300
MAX_POSITIONS = 2000
MIN_TIME_COVERAGE_DAYS = 14.0
MIN_UNIQUE_DAYS = 7
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 1.5
POLITE_DELAY_SECONDS = 0.12

# These are deliberately diagnostic thresholds, not production scraper filters.
MIN_POSITIVE_DAYS_SHARE = 0.60
MIN_TREND_R2 = 0.60
MAX_DRAWDOWN_TO_TOTAL = 0.75
MAX_SINGLE_DAY_SHARE = 0.60
MAX_CONSECUTIVE_NEGATIVE_DAYS = 4


class FetchError(RuntimeError):
    pass


_last_request_at = 0.0


def polite_wait():
    global _last_request_at
    now = time.monotonic()
    wait = POLITE_DELAY_SECONDS - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch_page(wallet, offset):
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
        polite_wait()
        try:
            with urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, list):
                raise FetchError("closed-positions response was not a list")
            return data
        except HTTPError as error:
            last_error = error
            if error.code not in (429, 500, 502, 503, 504):
                return []
        except (URLError, TimeoutError, ValueError, FetchError) as error:
            last_error = error
        time.sleep(RETRY_BASE_SECONDS * (attempt + 1))

    raise FetchError(f"failed to fetch offset {offset}: {last_error}")


def fetch_closed_positions(wallet, limit):
    """Fetch at most limit rows, preserving API order and removing duplicates."""
    rows = []
    seen = set()

    for offset in range(0, limit, PAGE_SIZE):
        try:
            page = fetch_page(wallet, offset)
        except FetchError:
            break
        if not page:
            break

        for row in page:
            # A stable row fingerprint prevents accidental duplicate pages from
            # distorting counts or daily totals.
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
            if fingerprint not in seen:
                seen.add(fingerprint)
                rows.append(row)

        if len(page) < PAGE_SIZE:
            break

    return rows[:limit]


def parse_timestamp(value):
    """Return Unix seconds for numeric, ISO-8601, or millisecond timestamps."""
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def parse_pnl(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def valid_points(rows):
    points = []
    for row in rows:
        timestamp = parse_timestamp(row.get("timestamp"))
        pnl = parse_pnl(row.get("realizedPnl"))
        if timestamp is not None and pnl is not None:
            points.append((timestamp, pnl))
    points.sort(key=lambda item: item[0])
    return points


def linear_regression(values):
    n = len(values)
    if n < 2:
        return 0.0, 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    ss_x = sum((i - x_mean) ** 2 for i in range(n))
    ss_y = sum((value - y_mean) ** 2 for value in values)
    covariance = sum((i - x_mean) * (value - y_mean)
                     for i, value in enumerate(values))
    slope = covariance / ss_x if ss_x else 0.0
    r2 = (covariance * covariance) / (ss_x * ss_y) if ss_x and ss_y else 0.0
    return slope, max(0.0, min(1.0, r2))


def longest_negative_streak(day_pnls):
    longest = 0
    current = 0
    for pnl in day_pnls:
        if pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def curve_metrics(rows, acquisition_mode):
    points = valid_points(rows)
    if not points:
        return {
            "CurveSignal": "INSUFFICIENT_DATA",
            "CurveQuality": "INSUFFICIENT_DATA",
            "DataMode": acquisition_mode,
            "PositionCount": 0,
            "OldestTimestamp": "N/A",
            "NewestTimestamp": "N/A",
            "TimeSpanDays": "N/A",
            "UniqueDays": 0,
            "PositiveDaysShare": "N/A",
            "TrendSlopePerDay": "N/A",
            "TrendR2": "N/A",
            "MaxDrawdown": "N/A",
            "DrawdownToTotal": "N/A",
            "LargestDayShare": "N/A",
            "MaxConsecutiveNegativeDays": "N/A",
            "CurveTotalPnL": "N/A",
        }

    by_day = defaultdict(float)
    for timestamp, pnl in points:
        day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        by_day[day] += pnl

    days = sorted(by_day)
    day_pnls = [by_day[day] for day in days]
    cumulative = []
    running = 0.0
    for pnl in day_pnls:
        running += pnl
        cumulative.append(running)

    oldest = points[0][0]
    newest = points[-1][0]
    span_days = (newest - oldest) / 86400.0
    total_pnl = cumulative[-1]
    slope, r2 = linear_regression(cumulative)

    peak = 0.0
    max_drawdown = 0.0
    for value in cumulative:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)

    positive_days_share = (
        sum(1 for pnl in day_pnls if pnl > 0) / len(day_pnls)
        if day_pnls else 0.0
    )
    drawdown_to_total = (
        max_drawdown / total_pnl if total_pnl > 0 else float("inf")
    )
    largest_day_share = (
        max(abs(pnl) for pnl in day_pnls) / abs(total_pnl)
        if total_pnl != 0 else float("inf")
    )
    negative_streak = longest_negative_streak(day_pnls)

    enough_time = span_days >= MIN_TIME_COVERAGE_DAYS
    enough_days = len(days) >= MIN_UNIQUE_DAYS
    upward = total_pnl > 0 and slope > 0
    smooth = (
        positive_days_share >= MIN_POSITIVE_DAYS_SHARE
        and r2 >= MIN_TREND_R2
        and drawdown_to_total <= MAX_DRAWDOWN_TO_TOTAL
        and largest_day_share <= MAX_SINGLE_DAY_SHARE
        and negative_streak <= MAX_CONSECUTIVE_NEGATIVE_DAYS
    )

    if not enough_time or not enough_days:
        signal = "INSUFFICIENT_TIME_COVERAGE"
        quality = "NOT_ENOUGH_HISTORY"
    elif not upward:
        signal = "NEGATIVE_OR_DOWN"
        quality = "NOT_UPWARD"
    elif smooth:
        signal = "SMOOTH_UPGOING"
        quality = "QUALIFIED"
    else:
        signal = "POSITIVE_VOLATILE"
        quality = "UPWARD_BUT_NOT_SMOOTH"

    def rounded(value, digits=4):
        return round(value, digits) if math.isfinite(value) else "N/A"

    return {
        "CurveSignal": signal,
        "CurveQuality": quality,
        "DataMode": acquisition_mode,
        "PositionCount": len(points),
        "OldestTimestamp": datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat(),
        "NewestTimestamp": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat(),
        "TimeSpanDays": round(span_days, 2),
        "UniqueDays": len(days),
        "PositiveDaysShare": rounded(positive_days_share),
        "TrendSlopePerDay": rounded(slope, 6),
        "TrendR2": rounded(r2),
        "MaxDrawdown": rounded(max_drawdown, 2),
        "DrawdownToTotal": rounded(drawdown_to_total),
        "LargestDayShare": rounded(largest_day_share),
        "MaxConsecutiveNegativeDays": negative_streak,
        "CurveTotalPnL": rounded(total_pnl, 2),
    }


def analyze_wallet(wallet):
    initial_rows = fetch_closed_positions(wallet, INITIAL_POSITIONS)
    initial_metrics = curve_metrics(initial_rows, "INITIAL_300")

    initial_span = initial_metrics.get("TimeSpanDays")
    needs_extension = (
        initial_metrics.get("PositionCount", 0) > 0
        and (
            not isinstance(initial_span, (int, float))
            or initial_span < MIN_TIME_COVERAGE_DAYS
        )
    )

    if needs_extension and len(initial_rows) < MAX_POSITIONS:
        final_rows = fetch_closed_positions(wallet, MAX_POSITIONS)
        final_metrics = curve_metrics(final_rows, "EXTENDED_UP_TO_2000")
        final_metrics["InitialPositionCount"] = initial_metrics.get("PositionCount", 0)
        final_metrics["InitialTimeSpanDays"] = initial_span
        return final_metrics

    initial_metrics["InitialPositionCount"] = initial_metrics.get("PositionCount", 0)
    initial_metrics["InitialTimeSpanDays"] = initial_span
    return initial_metrics


def find_source():
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        return sys.argv[1]
    files = sorted(glob.glob("traders_*.csv"), key=os.path.getmtime, reverse=True)
    if not files:
        raise SystemExit("No traders_*.csv source file found")
    return files[0]


def main():
    if "--self-test" in sys.argv:
        run_self_test()
        return

    source = find_source()
    with open(source, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("Source CSV is empty")

    for index, row in enumerate(rows, start=1):
        wallet = (row.get("TraderID") or row.get("Wallet") or "").strip()
        if wallet.lower().startswith("0x"):
            try:
                metrics = analyze_wallet(wallet)
            except Exception as error:
                metrics = {
                    "CurveSignal": "FETCH_ERROR",
                    "CurveQuality": "FETCH_ERROR",
                    "DataMode": "ERROR",
                    "PositionCount": 0,
                    "OldestTimestamp": "N/A",
                    "NewestTimestamp": "N/A",
                    "TimeSpanDays": "N/A",
                    "UniqueDays": 0,
                    "PositiveDaysShare": "N/A",
                    "TrendSlopePerDay": "N/A",
                    "TrendR2": "N/A",
                    "MaxDrawdown": "N/A",
                    "DrawdownToTotal": "N/A",
                    "LargestDayShare": "N/A",
                    "MaxConsecutiveNegativeDays": "N/A",
                    "CurveTotalPnL": "N/A",
                    "Error": str(error)[:200],
                }
        else:
            metrics = {
                "CurveSignal": "INSUFFICIENT_DATA",
                "CurveQuality": "INSUFFICIENT_DATA",
                "DataMode": "NO_WALLET",
                "PositionCount": 0,
                "OldestTimestamp": "N/A",
                "NewestTimestamp": "N/A",
                "TimeSpanDays": "N/A",
                "UniqueDays": 0,
                "PositiveDaysShare": "N/A",
                "TrendSlopePerDay": "N/A",
                "TrendR2": "N/A",
                "MaxDrawdown": "N/A",
                "DrawdownToTotal": "N/A",
                "LargestDayShare": "N/A",
                "MaxConsecutiveNegativeDays": "N/A",
                "CurveTotalPnL": "N/A",
            }
        row.update(metrics)
        print(
            f"Curve check {index}/{len(rows)}: "
            f"{row.get('Name', wallet)} -> {row['CurveSignal']} "
            f"({row['DataMode']}, {row['TimeSpanDays']} days)"
        )

    order = {
        "SMOOTH_UPGOING": 4,
        "POSITIVE_VOLATILE": 3,
        "NEGATIVE_OR_DOWN": 2,
        "INSUFFICIENT_TIME_COVERAGE": 1,
        "INSUFFICIENT_DATA": 0,
        "FETCH_ERROR": -1,
    }

    def numeric(row, key):
        try:
            return float(row.get(key, ""))
        except (TypeError, ValueError):
            return -float("inf")

    rows.sort(key=lambda row: (
        order.get(row.get("CurveSignal"), -1),
        numeric(row, "TrendR2"),
        numeric(row, "PositiveDaysShare"),
        numeric(row, "CurveTotalPnL"),
    ), reverse=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = f"versatility_curve_traders_{stamp}.csv"
    fields = list(rows[0].keys())
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    qualified = sum(row.get("CurveQuality") == "QUALIFIED" for row in rows)
    print(f"Read {len(rows)} passing traders from {source}")
    print(f"Qualified smooth/upgoing: {qualified}/{len(rows)}")
    print(f"Wrote {len(rows)} rows to {output}")
    print("No source rows were removed; this remains a ranking/diagnostic experiment.")


def run_self_test():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
    rows = []
    for day in range(21):
        rows.append({
            "timestamp": str(now - (20 - day) * 86400),
            "realizedPnl": "10",
        })
    result = curve_metrics(rows, "SELF_TEST")
    assert result["CurveSignal"] == "SMOOTH_UPGOING", result
    assert result["TimeSpanDays"] == 20.0, result
    assert result["UniqueDays"] == 21, result

    short_rows = rows[:5]
    short_result = curve_metrics(short_rows, "SELF_TEST")
    assert short_result["CurveSignal"] == "INSUFFICIENT_TIME_COVERAGE", short_result
    print("Self-test passed")


if __name__ == "__main__":
    main()
