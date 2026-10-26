#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取新浪财经期货小时线，供复盘引擎判定「同日双触」谁先到达。

用法:
  python3 tools/fetch_intraday.py --symbols SC2612,EB2701 --start 2026-10-19 --end 2026-10-26 --out /tmp/intraday.json

说明:
  - 接口只返回最近约 1000 根小时线，足够覆盖 5 个交易日的复盘窗口。
  - 输出结构 {"合约": {"交易日": [{"t": 时间, "open/high/low/close/v": ...}]}}；
    交易日归属与日K口径一致，仅取当日 09:00~15:30 日盘分时，
    因为新浪日K的 high/low 不含当晚夜盘。
  - 该接口为分钟快照聚合，极值与日K可能有细微差异，仅用于同日双触的先后判定。
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
        "var%20_{sym}=/InnerFuturesNewService.getFewMinLine?symbol={sym}&type=60")


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
            bars = {}
            for r in fetch(s):
                t = r.get("d") or ""
                day, hm = t[:10], t[11:16]
                if not (args.start <= day <= args.end):
                    continue
                # 只保留日盘，与新浪日K的 high/low 口径一致
                if not ("09:00" <= hm <= "15:30"):
                    continue
                bars.setdefault(day, []).append({
                    "t": t,
                    "open": float(r.get("o") or 0),
                    "high": float(r.get("h") or 0),
                    "low": float(r.get("l") or 0),
                    "close": float(r.get("c") or 0),
                    "volume": float(r.get("v") or 0),
                })
            data[s] = {d: sorted(v, key=lambda x: x["t"]) for d, v in bars.items()}
        except Exception as e:  # noqa: BLE001
            errors[s] = str(e)
        time.sleep(args.sleep)

    out = {
        "source": "sina_futures_hourly",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "start": args.start,
        "end": args.end,
        "intraday": data,
        "errors": errors,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}: {len(data)} symbols, "
          f"{sum(len(v) for v in data.values())} days, "
          f"{sum(len(b) for v in data.values() for b in v.values())} bars")
    if errors:
        print("errors:", json.dumps(errors, ensure_ascii=False))


if __name__ == "__main__":
    main()
