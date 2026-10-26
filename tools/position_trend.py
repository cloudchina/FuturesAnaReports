#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于 positions-log.csv 生成「5日持仓趋势」，作为深度分析的持仓维度参考。

用法:
  python3 tools/position_trend.py --log future-analysis-reports/positions-log.csv \
      --asof 2026-10-26 --lookback 5 --out-txt /workspace/position-trend.txt --out-json /workspace/position-trend.json

输出:
  - txt：可直接贴入分析上下文的表格（最新净持仓、当日变化、窗口累计变化、连续同向天数、集中度、状态）
  - json：机器可读的同口径指标
"""
import argparse
import csv
import json
import os
from collections import defaultdict, OrderedDict


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_log(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("品种") and r.get("日期")]


def status_label(net, delta):
    if net is None or delta is None:
        return "数据不足"
    if net >= 0 and delta >= 0:
        return "净多增仓"
    if net >= 0 and delta < 0:
        return "净多减仓"
    if net < 0 and delta < 0:
        return "净空增仓"
    return "净空减仓"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--asof", required=True)
    ap.add_argument("--lookback", type=int, default=5)
    ap.add_argument("--out-txt", default="")
    ap.add_argument("--out-json", default="")
    args = ap.parse_args()

    rows = [r for r in load_log(args.log) if r["日期"] <= args.asof]
    dates = sorted({r["日期"] for r in rows})[-args.lookback:]
    rows = [r for r in rows if r["日期"] in dates]

    # 同一品种同一日可能同时存在新旧合约（换月期），按总持仓最大者为主力
    picked = {}
    for r in rows:
        key = (r["品种"], r["日期"])
        score = (num(r["总持仓"]) or 0) or ((num(r["前20多单"]) or 0) + (num(r["前20空单"]) or 0))
        if key not in picked or score > picked[key][0]:
            picked[key] = (score, r)

    by_var = defaultdict(list)
    for (_, _), (_, r) in picked.items():
        by_var[r["品种"]].append(r)

    result = OrderedDict()
    for var in sorted(by_var):
        rs = sorted(by_var[var], key=lambda r: r["日期"])
        series = [(r["日期"], num(r["净持仓"]), num(r["净持仓变化"]),
                   num(r["多单集中度"]), num(r["空单集中度"]), r["合约"]) for r in rs]
        last = series[-1]
        first = series[0]
        net = last[1]
        delta = (net - first[1]) if (len(series) > 1 and net is not None and first[1] is not None) else None

        streak = 0
        for _, _, chg, _, _, _ in reversed(series):
            if chg is None:
                break
            if streak == 0:
                streak = 1 if chg > 0 else (-1 if chg < 0 else 0)
                if streak == 0:
                    break
            elif (streak > 0 and chg > 0) or (streak < 0 and chg < 0):
                streak += 1 if streak > 0 else -1
            else:
                break

        contracts = {s[5] for s in series}
        result[var] = {
            "contract": last[5],
            "net": net,
            "net_chg_today": last[2],
            "net_chg_window": delta,
            "consecutive_days": streak,
            "long_concentration": last[3],
            "short_concentration": last[4],
            "status": status_label(net, delta if delta is not None else last[2]),
            "days": len(series),
            "contract_changed": len(contracts) > 1,
        }

    lines = []
    lines.append(f"5日持仓趋势（前20名会员净持仓，{dates[0] if dates else '—'} ~ {dates[-1] if dates else '—'}，共{len(dates)}个交易日）")
    lines.append("  品种  合约      最新净持仓   当日变化   窗口累计变化  连续同向  多/空集中度   状态")
    for var, m in result.items():
        net = f"{m['net']:+.0f}" if m["net"] is not None else "—"
        chg = f"{m['net_chg_today']:+.0f}" if m["net_chg_today"] is not None else "—"
        win = f"{m['net_chg_window']:+.0f}" if m["net_chg_window"] is not None else "—"
        streak = f"{abs(m['consecutive_days'])}日{'↑' if m['consecutive_days'] > 0 else '↓'}" if m['consecutive_days'] else "—"
        conc = (f"{m['long_concentration']:.1%}/{m['short_concentration']:.1%}"
                if m["long_concentration"] is not None and m["short_concentration"] is not None else "—")
        flag = "（换月）" if m["contract_changed"] else ""
        lines.append(f"  {var:<4}  {m['contract']:<8}  {net:<10}  {chg:<8}  {win:<12}  {streak:<6}  {conc:<13}  {m['status']}{flag}")
    if len(dates) < 3:
        lines.append("  注：持仓日志不足 3 个交易日，趋势结论仅供参考。")
    lines.append("  注：净持仓 = 前20名多头合计 − 前20名空头合计；连续同向为净持仓变化连续同向的天数（↑净持仓增加/↓减少）。")

    text = "\n".join(lines) + "\n"
    if args.out_txt:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_txt)), exist_ok=True)
        with open(args.out_txt, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out_txt}")
    else:
        print(text, end="")

    if args.out_json:
        payload = {"asof": args.asof, "lookback": args.lookback,
                   "dates": dates, "varieties": result}
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"wrote {args.out_json}")


if __name__ == "__main__":
    main()
