#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双周期趋势与入场位置过滤器（日线定方向、小时定位置）。

用法:
  python tools/trend_location.py --predictions future-analysis-reports/2026-11-19/predictions.csv \
      --daily .tmp/ohlc.json --hourly .tmp/intraday.json \
      --position-trend future-analysis-reports/2026-11-19/position-trend.json \
      --out future-analysis-reports/2026-11-19/trend-location.json

判定口径:
  - 日线趋势：EMA10/20/60 排列 + EMA20 5周期斜率 + 收盘位置打分 S；
              ADX14>=20 且 |S|>=3 为趋势，否则震荡。
  - 小时趋势：同一套指标，ADX14>=18 且 |S|>=3 为趋势。
  - 日线定方向，小时定位置；逆日线方向默认不通过。
  - 趋势日：做多要求距小时EMA10在 [-0.3, +0.8] ATR，>1.0 不追、<-0.6 不接；
            做空对称。
  - 震荡日：做多要求日线%B<=0.2 或距20日低点<=0.5 ATR，且小时%B<=0.25；
            做空对称；窄幅震荡禁止低吸/高抛，只做放量突破。
  - 位置评分 = 方向一致(40) + 位置(30) + 确认(20) + 赔率(10)，>=60 且无硬伤才通过。
"""
import argparse
import csv
import json
import os


def ema(vals, n):
    out, s = [], None
    k = 2 / (n + 1)
    for v in vals:
        s = v if s is None else s + k * (v - s)
        out.append(s)
    return out


def atr_series(highs, lows, closes, n=14):
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    s, out = None, []
    for t in trs:
        s = t if s is None else s + (t - s) / n
        out.append(s)
    return out


def adx_series(highs, lows, closes, n=14):
    plus, minus, trs = [], [], []
    for i in range(len(closes)):
        up = highs[i] - highs[i - 1] if i else 0
        dn = lows[i - 1] - lows[i] if i else 0
        plus.append(up if (up > dn and up > 0) else 0)
        minus.append(dn if (dn > up and dn > 0) else 0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]) if i else 0,
                       abs(lows[i] - closes[i - 1]) if i else 0))
    def smooth(xs):
        s, out = None, []
        for x in xs:
            s = x if s is None else s + (x - s) / n
            out.append(s)
        return out
    atr = smooth(trs)
    pdi = [100 * p / a if a else 0 for p, a in zip(smooth(plus), atr)]
    mdi = [100 * m / a if a else 0 for m, a in zip(smooth(minus), atr)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0 for p, m in zip(pdi, mdi)]
    return smooth(dx)


def boll(closes, n=20, k=2):
    out = []
    for i in range(len(closes)):
        if i < n - 1:
            out.append((None, None, None))
            continue
        seg = closes[i - n + 1:i + 1]
        ma = sum(seg) / n
        var = sum((x - ma) ** 2 for x in seg) / n
        sd = var ** 0.5
        out.append((ma + k * sd, ma, ma - k * sd))
    return out


def pct_rank(series, value):
    vals = [x for x in series if x is not None]
    if not vals:
        return 0.5
    return sum(1 for x in vals if x <= value) / len(vals)


def metrics(bars):
    """bars: [{date, open, high, low, close, volume}]，按时间升序。"""
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    vols = [b.get("volume", 0) for b in bars]
    e10, e20, e60 = ema(closes, 10), ema(closes, 20), ema(closes, 60)
    slope = (e20[-1] / e20[-6] - 1) * 100 if len(e20) >= 6 and e20[-6] else 0
    adx = adx_series(highs, lows, closes)
    atr = atr_series(highs, lows, closes)
    boll_all = boll(closes)
    bw_series = [((u - l) / m if (u is not None and m) else None) for u, m, l in boll_all]
    bup, bmid, blow = boll_all[-1]
    rng = (bup - blow) if (bup is not None and blow is not None and bup != blow) else 0
    pb = ((closes[-1] - blow) / rng) if rng else 0.5
    score = ((1 if e10[-1] > e20[-1] else -1) + (1 if e20[-1] > e60[-1] else -1) +
             (1 if closes[-1] > e20[-1] else -1) + (1 if slope > 0 else -1))
    return {
        "close": closes[-1], "ema10": e10[-1], "ema20": e20[-1], "ema60": e60[-1],
        "slope": round(slope, 3), "score": score, "adx": round(adx[-1], 1),
        "atr": round(atr[-1], 2), "boll_up": bup, "boll_mid": bmid, "boll_low": blow,
        "pct_b": round(pb, 3), "bandwidth": round(bw_series[-1] or 0, 4),
        "bandwidth_rank": round(pct_rank(bw_series[-60:-1], bw_series[-1] or 0), 2),
        "dist_ema10_atr": round((closes[-1] - e10[-1]) / atr[-1], 2) if atr[-1] else 0,
        "prior_low_10": min(lows[-11:-1]), "prior_high_10": max(highs[-11:-1]),
        "prior_low_20": min(lows[-21:-1]), "prior_high_20": max(highs[-21:-1]),
        "dist_prior_low_atr": round((closes[-1] - min(lows[-21:-1])) / atr[-1], 2) if atr[-1] else 0,
        "dist_prior_high_atr": round((max(highs[-21:-1]) - closes[-1]) / atr[-1], 2) if atr[-1] else 0,
        "last_close": closes[-1], "prev_high": highs[-2], "prev_low": lows[-2],
        "vol_ratio": round(vols[-1] / (sum(vols[-21:-1]) / 20), 2) if len(vols) > 21 and sum(vols[-21:-1]) else 0,
    }


def trend_of(m, adx_min):
    if m["adx"] >= adx_min and abs(m["score"]) >= 3:
        if m["score"] > 0:
            strength = "强" if m["adx"] >= 30 and m["score"] == 4 else "中"
            return "多", strength
        strength = "强" if m["adx"] >= 30 and m["score"] == -4 else "中"
        return "空", strength
    return "震荡", "弱"


def load_daily(path, asof):
    raw = json.load(open(path, encoding="utf-8")).get("daily", {})
    out = {}
    for sym, days in raw.items():
        bars = []
        for d in sorted(x for x in days if x <= asof):
            b = days[d]
            bars.append({"date": d, "open": b["open"], "high": b["high"], "low": b["low"],
                         "close": b["close"], "volume": b.get("volume", 0)})
        out[sym.upper()] = bars
    return out


def load_hourly(path, asof):
    raw = json.load(open(path, encoding="utf-8"))
    raw = raw.get("intraday", raw)
    out = {}
    for sym, days in raw.items():
        bars = []
        for d in sorted(x for x in days if x <= asof):
            for b in days[d]:
                bars.append({"date": b.get("t", d), "open": b["open"], "high": b["high"],
                             "low": b["low"], "close": b["close"], "volume": b.get("volume", 0)})
        out[sym.upper()] = bars
    return out


def evaluate(row, daily, hourly, pos, asof):
    code = (row.get("品种") or "").strip()
    contract = (row.get("合约") or "").strip().upper()
    direction = (row.get("方向") or "").strip()
    try:
        lo, hi = float(row.get("入场下沿") or 0), float(row.get("入场上沿") or 0)
        target = float(row.get("目标1") or 0)
        stop0 = float(row.get("止损") or 0)
    except ValueError:
        lo = hi = target = stop0 = 0
    entry = (lo + hi) / 2 if (lo or hi) else 0
    item = {"code": code, "contract": contract, "direction": direction,
            "entry_low": lo, "entry_high": hi, "entry": entry, "asof": asof,
            "daily": {}, "hourly": {}, "rule_pass": False, "fail_reasons": [],
            "location_score": 0, "score_parts": {}, "size_factor": 0.0,
            "allowed_direction": "—", "verdict": "—"}

    dbars = daily.get(contract, [])
    hbars = hourly.get(contract, [])
    if len(dbars) < 65:
        item["fail_reasons"].append("日线数据不足")
        item["verdict"] = "数据不足"
        return item
    md = metrics(dbars)
    dt, ds = trend_of(md, 20)
    item["daily"] = {**{k: (round(v, 4) if isinstance(v, float) else v) for k, v in md.items()},
                     "trend": dt, "strength": ds}
    item["allowed_direction"] = {"多": "多", "空": "空", "震荡": "双向（仅边缘）"}[dt]
    if len(hbars) < 65:
        item["fail_reasons"].append("小时数据不足")
        item["verdict"] = "数据不足"
        return item
    mh = metrics(hbars)
    ht, hs = trend_of(mh, 18)
    item["hourly"] = {**{k: (round(v, 4) if isinstance(v, float) else v) for k, v in mh.items()},
                      "trend": ht, "strength": hs}

    if direction not in ("多", "空") or entry == 0:
        item["verdict"] = "中性/无入场价，不评分"
        return item

    # 方向一致分
    edge_long = md["pct_b"] <= 0.2 or md["dist_prior_low_atr"] <= 0.5
    edge_short = md["pct_b"] >= 0.8 or md["dist_prior_high_atr"] <= 0.5
    if dt == direction:
        align = 40 if ht == direction else 30
    elif dt == "震荡":
        align = 25 if ((direction == "多" and edge_long) or (direction == "空" and edge_short)) else 10
    else:
        align = 0

    # 位置分
    if dt == direction:
        if direction == "多":
            loc = 30 * max(0.0, 1 - abs(mh["dist_ema10_atr"] - 0.2) / 0.8)
        else:
            loc = 30 * max(0.0, 1 - abs(mh["dist_ema10_atr"] + 0.2) / 0.8)
    elif dt == "震荡":
        if direction == "多":
            loc = max(30 * max(0.0, 1 - md["pct_b"] / 0.2), 30 * max(0.0, 1 - md["dist_prior_low_atr"] / 0.5))
        else:
            loc = max(30 * max(0.0, 1 - (1 - md["pct_b"]) / 0.2), 30 * max(0.0, 1 - md["dist_prior_high_atr"] / 0.5))
    else:
        loc = 0

    # 确认分
    conf = 0
    if direction == "多" and mh["last_close"] > mh["prev_high"]:
        conf += 10
    if direction == "空" and mh["last_close"] < mh["prev_low"]:
        conf += 10
    if mh["vol_ratio"] >= 1.2:
        conf += 5
    v = (pos or {}).get("varieties", {}).get(code, {})
    cd = v.get("consecutive_days")
    if direction == "多" and isinstance(cd, (int, float)) and cd > 0:
        conf += 5
    if direction == "空" and isinstance(cd, (int, float)) and cd < 0:
        conf += 5

    # 赔率分
    if direction == "多":
        stop = min(mh["prior_low_10"], mh["ema20"]) - 0.3 * mh["atr"]
        risk = entry - stop
        rr = ((target or entry + 2 * risk) - entry) / risk if risk > 0 else 0
    else:
        stop = max(mh["prior_high_10"], mh["ema20"]) + 0.3 * mh["atr"]
        risk = stop - entry
        rr = (entry - (target or entry - 2 * risk)) / risk if risk > 0 else 0
    rr_score = 10 if rr >= 2 else (5 if rr >= 1.5 else 0)

    score = round(align + loc + conf + rr_score, 1)
    item["location_score"] = score
    item["score_parts"] = {"alignment": round(align, 1), "location": round(loc, 1),
                           "confirmation": conf, "risk_reward": rr_score, "rr": round(rr, 2)}
    item["suggest_stop"] = round(stop, 2) if risk > 0 else None

    fails = item["fail_reasons"]
    # 硬性约束
    if dt not in ("震荡", direction):
        fails.append(f"逆日线（日线{dt}）")
    if dt == direction:
        if direction == "多":
            if mh["dist_ema10_atr"] > 1.0:
                fails.append(f"追多（距小时EMA10 {mh['dist_ema10_atr']:+.2f}ATR>1）")
            if mh["dist_ema10_atr"] < -0.6:
                fails.append(f"破位（距小时EMA10 {mh['dist_ema10_atr']:+.2f}ATR<-0.6）")
            if ht == "震荡" and mh["pct_b"] > 0.5:
                fails.append(f"小时区间中上部做多（%B {mh['pct_b']:.2f}>0.5）")
        else:
            if mh["dist_ema10_atr"] < -1.0:
                fails.append(f"追空（距小时EMA10 {mh['dist_ema10_atr']:+.2f}ATR<-1）")
            if mh["dist_ema10_atr"] > 0.6:
                fails.append(f"反弹过高（距小时EMA10 {mh['dist_ema10_atr']:+.2f}ATR>0.6）")
            if ht == "震荡" and mh["pct_b"] < 0.5:
                fails.append(f"小时区间中下部做空（%B {mh['pct_b']:.2f}<0.5）")
    if dt == "震荡":
        if direction == "多" and not edge_long:
            fails.append(f"不在日线区间下沿（%B {md['pct_b']:.2f}，距20日低 {md['dist_prior_low_atr']:.2f}ATR）")
        if direction == "空" and not edge_short:
            fails.append(f"不在日线区间上沿（%B {md['pct_b']:.2f}，距20日高 {md['dist_prior_high_atr']:.2f}ATR）")
        if md["bandwidth_rank"] < 0.4:
            breakout = ((direction == "多" and md["last_close"] > md["boll_up"] + 0.3 * md["atr"] and md["vol_ratio"] >= 1.2) or
                        (direction == "空" and md["last_close"] < md["boll_low"] - 0.3 * md["atr"] and md["vol_ratio"] >= 1.2))
            if not breakout:
                fails.append("窄幅震荡禁止低吸/高抛（仅放量突破）")
    if rr_score == 0:
        fails.append(f"盈亏比不足（{rr:.2f}<1.5）")
    if score < 60:
        fails.append(f"位置评分不足（{score:.0f}<60）")

    item["rule_pass"] = len(fails) == 0
    if item["rule_pass"]:
        item["size_factor"] = 0.5 if (dt == "震荡" or ht == "震荡") else 1.0
        item["verdict"] = "通过"
    else:
        item["size_factor"] = 0.0
        item["verdict"] = "不通过"
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--daily", required=True)
    ap.add_argument("--hourly", required=True)
    ap.add_argument("--position-trend", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--asof", default="")
    args = ap.parse_args()

    asof = args.asof
    if not asof:
        asof = os.path.basename(os.path.dirname(os.path.abspath(args.predictions)))
    daily = load_daily(args.daily, asof)
    hourly = load_hourly(args.hourly, asof)
    pos = {}
    if args.position_trend and os.path.isfile(args.position_trend):
        pos = json.load(open(args.position_trend, encoding="utf-8"))

    rows = list(csv.DictReader(open(args.predictions, encoding="utf-8-sig")))
    items = [evaluate(r, daily, hourly, pos, asof) for r in rows]
    out = {"asof": asof, "source": "tools/trend_location.py",
           "rule": "日线定方向、小时定位置；趋势看EMA10，震荡看布林/前低前高",
           "items": {x["code"]: x for x in items}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"{'品种':<4}{'方向':<4}{'日线':<4}{'小时':<4}{'评分':<6}{'判定':<6}原因")
    for x in items:
        print(f"{x['code']:<4}{x['direction']:<4}{x['daily'].get('trend', '-'):<4}"
              f"{x['hourly'].get('trend', '-'):<4}{x['location_score']:<6}{x['verdict']:<6}"
              f"{'；'.join(x['fail_reasons'])}")
    passed = sum(1 for x in items if x["rule_pass"])
    print(f"wrote {args.out}: {len(items)} 条，规则通过 {passed} 条")


if __name__ == "__main__":
    main()
