#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取新浪财经期货日K，输出复盘引擎所需的 OHLC JSON。

用法:
  python3 tools/fetch_ohlc.py --symbols SC2612,EB2701 --start 2026-10-09 --end 2026-10-26 --out /tmp/ohlc.json

说明:
  - symbol 支持主力连续（如 MA0）与具体合约（如 BU2701）。
  - 只输出 [start, end] 区间内、且有行情的日期。
"""
import argparse
import csv
import json
import os
import re
import time
import urllib.request
from datetime import datetime

BASE = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
        "var%20_{sym}=/InnerFuturesNewService.getDailyKLine?symbol={sym}")


def fetch(symbol, timeout=25):
    url = BASE.format(sym=symbol)
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j < i:
        raise ValueError(f"{symbol}: no JSON array in response")
    return json.loads(raw[i:j + 1])


def symbols_from_predictions(reports_dir, asof, lookback):
    """从最近 N 个 predictions.csv 收集合约，免去手工维护合约列表。"""
    dates = sorted(
        [d for d in os.listdir(reports_dir)
         if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < asof and
         os.path.isfile(os.path.join(reports_dir, d, "predictions.csv"))],
        reverse=True)[:lookback]
    out = []
    for d in sorted(dates):
        with open(os.path.join(reports_dir, d, "predictions.csv"),
                  encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                c = (r.get("合约") or "").strip().upper()
                if c and c not in out:
                    out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔；留空则从 predictions.csv 推导")
    ap.add_argument("--reports-dir", default="")
    ap.add_argument("--asof", default="")
    ap.add_argument("--lookback", type=int, default=5)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sleep", type=float, default=0.25)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols and args.reports_dir and args.asof:
        symbols = symbols_from_predictions(args.reports_dir, args.asof, args.lookback)
    data, errors = {}, {}
    for s in symbols:
        try:
            rows = fetch(s)
            d = {}
            for r in rows:
                day = r.get("d")
                if not day or not (args.start <= day <= args.end):
                    continue
                d[day] = {
                    "open": float(r.get("o") or 0),
                    "high": float(r.get("h") or 0),
                    "low": float(r.get("l") or 0),
                    "close": float(r.get("c") or 0),
                    "settle": float(r.get("s") or 0),
                    "volume": float(r.get("v") or 0),
                    "oi": float(r.get("p") or 0),
                }
            data[s] = d
        except Exception as e:  # noqa: BLE001
            errors[s] = str(e)
        time.sleep(args.sleep)

    out = {
        "source": "sina_futures_daily_kline",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "start": args.start,
        "end": args.end,
        "daily": data,
        "errors": errors,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}: {len(data)} symbols, {sum(len(v) for v in data.values())} bars")
    if errors:
        print("errors:", json.dumps(errors, ensure_ascii=False))


if __name__ == "__main__":
    main()
