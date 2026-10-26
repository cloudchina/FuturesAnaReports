#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5日滚动复盘引擎（确定性规则，避免不同会话口径漂移）。

规则要点：
  1) 取 asof 之前最近 N 个有 predictions.csv 的预测日；
  2) 每条预测按自身触发窗口评估，窗口上限 min(声明上限, 5) 个交易日；
  3) 先到为准：日线 high/low 判断目标/止损谁先触及；
     同日双触时优先用小时线判定，仍无法判定则记「双触未决」、不计分；
  4) 窗口未走完 → 未决，不进命中率分母；
  5) 窗口内目标/止损均未触及 → 以窗口末收盘相对预测日基准收盘的方向判定，
     阈值默认 ±0.3%，阈值内记「方向不明」、不计分；
  6) 方向归一：多/空/中性；观望类记中性，不参与方向评分；
  7) 置信度校准：按档位统计实际命中率，n>=min_samples 才给调整系数，裁剪 [0.7,1.3]。
"""
import argparse
import csv
import json
import os
import re
from collections import defaultdict

PRED_FIELDS = [
    "预测日期", "品种", "合约", "方向原文", "方向", "置信度", "窗口下限", "窗口上限",
    "窗口原文", "入场下沿", "入场上沿", "目标1", "目标2", "止损", "基准收盘价",
    "替换来源",
]
LOG_FIELDS = [
    "预测日期", "品种", "合约", "方向", "置信度", "窗口天数", "评估起始", "评估结束",
    "已评估天数", "入场触发", "方向命中", "目标触及", "止损触及", "先触及", "最终判定",
    "评估时点",
]
BUCKETS = [(">=70", 70, 101), ("60-69", 60, 70), ("50-59", 50, 60), ("<50", 0, 50)]


def to_num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_predictions(reports_dir, asof, lookback):
    dates = sorted(
        [d for d in os.listdir(reports_dir)
         if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < asof and
         os.path.isfile(os.path.join(reports_dir, d, "predictions.csv"))],
        reverse=True,
    )
    picked = []
    for d in dates:
        path = os.path.join(reports_dir, d, "predictions.csv")
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("品种")]
        if rows:
            picked.append((d, rows))
        if len(picked) >= lookback:
            break
    return list(reversed(picked))


def resolve_intraday(intraday, contract, day, direction, target, stop):
    bars = (intraday.get(contract) or {}).get(day)
    if not bars:
        return None
    for b in sorted(bars, key=lambda x: str(x.get("t", ""))):
        hi, lo = to_num(b.get("high")), to_num(b.get("low"))
        if hi is None or lo is None:
            continue
        if direction == "多":
            t = hi >= target
            s = lo <= stop
        else:
            t = lo <= target
            s = hi >= stop
        if t and s:
            return None          # 同一根小时线内仍无法分辨先后
        if t:
            return "target"
        if s:
            return "stop"
    return None


def evaluate(row, pred_date, ohlc, intraday, asof, threshold):
    code = (row.get("品种") or "").strip()
    contract = (row.get("合约") or "").strip()
    direction = (row.get("方向") or "中性").strip()
    conf = to_num(row.get("置信度"))
    wl = to_num(row.get("窗口下限")) or 1
    wu = to_num(row.get("窗口上限")) or 1
    wl_i = max(1, int(wl))
    wu_i = max(wl_i, int(wu))
    window_days = max(1, min(wu_i, 5))
    window_label = f"{wl_i}~{min(wu_i, 5)}" + ("*" if wu_i > 5 else "")
    entry_lo = to_num(row.get("入场下沿"))
    entry_hi = to_num(row.get("入场上沿"))
    target = to_num(row.get("目标1"))
    stop = to_num(row.get("止损"))
    base = to_num(row.get("基准收盘价"))

    series = ohlc.get(contract) or ohlc.get(code + "0")
    if not series:
        return dict(code=code, contract=contract, direction=direction, conf=conf,
                    window_days=window_days, window_label=window_label, status="NO_DATA", note="无行情数据",
                    pred_date=pred_date, entry_trigger="NA", dir_hit="NA",
                    target_hit="NA", stop_hit="NA", first="无", eval_days=0, eval_start="",
                    ret=None, hit_date="", last_close=None)

    if direction not in ("多", "空"):
        eds = sorted(d for d in series if pred_date < d <= asof)[:window_days]
        base_n = base if base is not None else (
            series[pred_date]["close"] if pred_date in series else None)
        ret_n = ((series[eds[-1]]["close"] / base_n) - 1.0) if (eds and base_n) else None
        return dict(code=code, contract=contract, direction=direction, conf=conf,
                    window_days=window_days, window_label=window_label, status="NEUTRAL", note="观望/中性，不计分",
                    pred_date=pred_date, entry_trigger="NA", dir_hit="NA",
                    target_hit="NA", stop_hit="NA", first="无", eval_days=len(eds), eval_start=(eds[0] if eds else ""),
                    ret=ret_n, hit_date="", last_close=(series[eds[-1]]["close"] if eds else None))

    eval_dates = sorted(d for d in series if pred_date < d <= asof)[:window_days]
    if not eval_dates:
        return dict(code=code, contract=contract, direction=direction, conf=conf,
                    window_days=window_days, window_label=window_label, status="NO_DATA", note="窗口内无行情",
                    pred_date=pred_date, entry_trigger="NA", dir_hit="NA",
                    target_hit="NA", stop_hit="NA", first="无", eval_days=0, eval_start="",
                    ret=None, hit_date="", last_close=None)

    if target is None or stop is None:
        return dict(code=code, contract=contract, direction=direction, conf=conf,
                    window_days=window_days, window_label=window_label, status="INCOMPLETE", note="目标/止损缺失",
                    pred_date=pred_date, entry_trigger="NA", dir_hit="NA",
                    target_hit="NA", stop_hit="NA", first="无", eval_days=len(eval_dates),
                    eval_start=eval_dates[0],
                    ret=None, hit_date="", last_close=None)

    if base is None and pred_date in series:
        base = series[pred_date]["close"]

    first, hit_date = None, ""
    for d in eval_dates:
        bar = series[d]
        if direction == "多":
            t = bar["high"] >= target
            s = bar["low"] <= stop
        else:
            t = bar["low"] <= target
            s = bar["high"] >= stop
        if t and s:
            r = resolve_intraday(intraday, contract, d, direction, target, stop)
            if r is None:
                first, hit_date = "double", d
            else:
                first, hit_date = r, d
            break
        if t:
            first, hit_date = "target", d
            break
        if s:
            first, hit_date = "stop", d
            break

    last_close = series[eval_dates[-1]]["close"]
    ret = ((last_close / base) - 1.0) if base else None

    if first == "target":
        status, note = "SUCCESS", "止盈成功"
    elif first == "stop":
        status, note = "FAIL", "止损失败"
    elif first == "double":
        status, note = "DOUBLE", "双触未决(不计分)"
    elif len(eval_dates) < window_days:
        status = "PENDING"
        note = (f"待窗口开启({len(eval_dates)}/{window_days})" if len(eval_dates) < wl_i
                else f"未决({len(eval_dates)}/{window_days})")
    elif ret is None:
        status, note = "NO_BASE", "缺基准收盘"
    elif direction == "多" and ret >= threshold:
        status, note = "EXPIRED_HIT", "窗口到期-方向命中"
    elif direction == "多" and ret <= -threshold:
        status, note = "EXPIRED_MISS", "窗口到期-方向未命中"
    elif direction == "空" and ret <= -threshold:
        status, note = "EXPIRED_HIT", "窗口到期-方向命中"
    elif direction == "空" and ret >= threshold:
        status, note = "EXPIRED_MISS", "窗口到期-方向未命中"
    else:
        status, note = "DIR_UNCLEAR", "窗口到期-方向不明(±阈值内)"

    if status in ("EXPIRED_HIT", "EXPIRED_MISS", "DIR_UNCLEAR") and not hit_date:
        hit_date = eval_dates[-1]

    # 入场是否触发
    e = "NA"
    if entry_lo is not None and entry_hi is not None:
        lows = [series[d]["low"] for d in eval_dates]
        highs = [series[d]["high"] for d in eval_dates]
        if direction == "多":
            e = "1" if min(lows) <= entry_hi else "0"
        else:
            e = "1" if max(highs) >= entry_lo else "0"

    dir_hit = {"SUCCESS": "1", "FAIL": "0",
               "EXPIRED_HIT": "1", "EXPIRED_MISS": "0"}.get(status, "NA")
    target_hit = "1" if first == "target" else "0"
    stop_hit = "1" if first == "stop" else "0"

    return dict(code=code, contract=contract, direction=direction, conf=conf,
                window_days=window_days, window_label=window_label, status=status, note=note,
                pred_date=pred_date, entry_trigger=e, dir_hit=dir_hit,
                target_hit=target_hit, stop_hit=stop_hit,
                first={"target": "目标", "stop": "止损", "double": "双触"}.get(first, "无"),
                eval_days=len(eval_dates), eval_start=eval_dates[0], ret=ret, hit_date=hit_date,
                last_close=last_close)


def bucket_of(conf):
    for name, lo, hi in BUCKETS:
        if lo <= conf < hi:
            return name
    return "<50"


def fmt_pct(x, digits=1):
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", required=True)
    ap.add_argument("--asof", required=True)
    ap.add_argument("--lookback", type=int, default=5)
    ap.add_argument("--ohlc", required=True)
    ap.add_argument("--intraday", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--factors-out", default="")
    ap.add_argument("--dir-threshold", type=float, default=0.003)
    ap.add_argument("--min-samples", type=int, default=5)
    args = ap.parse_args()

    with open(args.ohlc, encoding="utf-8") as f:
        ohlc = json.load(f).get("daily", {})
    intraday = {}
    if args.intraday and os.path.isfile(args.intraday):
        with open(args.intraday, encoding="utf-8") as f:
            raw = json.load(f)
        intraday = raw.get("intraday", raw) if isinstance(raw, dict) else raw

    preds = load_predictions(args.reports_dir, args.asof, args.lookback)
    results = []
    for d, rows in preds:
        for r in rows:
            results.append(evaluate(r, d, ohlc, intraday, args.asof, args.dir_threshold))

    by_code = defaultdict(list)
    for r in results:
        by_code[r["code"]].append(r)

    final_status = {"SUCCESS", "FAIL", "EXPIRED_HIT", "EXPIRED_MISS", "DIR_UNCLEAR"}
    scored = [r for r in results if r["status"] in final_status]
    dir_scored = [r for r in scored if r["dir_hit"] in ("1", "0")]
    dir_hits = sum(1 for r in dir_scored if r["dir_hit"] == "1")
    # 置信度定义的“结论触发”以目标位是否先触及为准（止盈成功=1，其余=0）
    concl_scored = [r for r in scored if r["conf"] is not None]
    concl_hits = sum(1 for r in concl_scored if r["target_hit"] == "1")

    # 置信度校准（基于结论触发率）
    calib = {}
    for name, lo, hi in BUCKETS:
        sub = [r for r in concl_scored if lo <= r["conf"] < hi]
        n = len(sub)
        h = sum(1 for r in sub if r["target_hit"] == "1")
        claimed = (sum(r["conf"] for r in sub) / n / 100.0) if n else None
        actual = (h / n) if n else None
        factor = 1.0
        if n >= args.min_samples and claimed and actual is not None:
            factor = max(0.7, min(1.3, round(actual / claimed, 2)))
        calib[name] = {"n": n, "hits": h, "claimed_avg": claimed,
                       "actual_hit_rate": actual, "factor": factor,
                       "applied": n >= args.min_samples}

    overall_factor = 1.0
    if len(concl_scored) >= args.min_samples:
        claimed_all = sum(r["conf"] for r in concl_scored) / len(concl_scored) / 100.0
        actual_all = concl_hits / len(concl_scored)
        if claimed_all:
            overall_factor = max(0.7, min(1.3, round(actual_all / claimed_all, 2)))

    factors = {
        "asof": args.asof,
        "lookback_days": args.lookback,
        "dir_threshold": args.dir_threshold,
        "min_samples": args.min_samples,
        "overall": {
            "n": len(concl_scored),
            "claimed_avg": (sum(r["conf"] for r in concl_scored) / len(concl_scored) / 100.0) if concl_scored else None,
            "actual_conclusion_hit_rate": (concl_hits / len(concl_scored)) if concl_scored else None,
            "factor": overall_factor,
            "direction_hit_rate": (dir_hits / len(dir_scored)) if dir_scored else None,
        },
        "buckets": calib,
        "apply": "今日基础置信度 × 对应档位系数（未达 min_samples 的档位系数为 1.0），结果裁剪到 10%~95%，再用于 60% 替换阈值判断",
    }

    # 写日志（幂等 upsert）
    log_path = args.log or os.path.join(args.reports_dir, "accuracy-log.csv")
    existing = {}
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = (r.get("预测日期"), r.get("品种"), r.get("合约"))
                existing[key] = r
    for r in results:
        key = (r["pred_date"], r["code"], r["contract"])
        existing[key] = {
            "预测日期": r["pred_date"], "品种": r["code"], "合约": r["contract"],
            "方向": r["direction"], "置信度": r["conf"], "窗口天数": r["window_days"],
            "评估起始": r["eval_start"], "评估结束": args.asof, "已评估天数": r["eval_days"],
            "入场触发": r["entry_trigger"], "方向命中": r["dir_hit"],
            "目标触及": r["target_hit"], "止损触及": r["stop_hit"],
            "先触及": r["first"], "最终判定": r["note"], "评估时点": args.asof,
        }
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        for key in sorted(existing, key=lambda k: (k[0] or "", k[1] or "", k[2] or "")):
            w.writerow({k: existing[key].get(k, "") for k in LOG_FIELDS})

    if args.factors_out:
        with open(args.factors_out, "w", encoding="utf-8") as f:
            json.dump(factors, f, ensure_ascii=False, indent=2)

    # 组装报告
    lines = []
    start_d = preds[0][0] if preds else "—"
    end_d = preds[-1][0] if preds else "—"
    lines.append("期货深度分析 · 5日滚动复盘（规则 v2）")
    lines.append(f"复盘预测日：{start_d} ~ {end_d}（{len(preds)} 个有效预测日）")
    lines.append(f"评估截止：{args.asof}　|　方向阈值：±{args.dir_threshold * 100:.1f}%　|　窗口上限：5 个交易日")
    lines.append(f"数据来源：predictions.csv + 新浪财经期货日K（{args.asof} 收盘）")
    lines.append("=" * 80)
    lines.append("")
    lines.append("一、逐品种追踪")
    lines.append("")

    for code in sorted(by_code):
        rs = sorted(by_code[code], key=lambda r: r["pred_date"])
        n_final = sum(1 for r in rs if r["dir_hit"] in ("1", "0"))
        n_hit = sum(1 for r in rs if r["dir_hit"] == "1")
        rate = (n_hit / n_final) if n_final else None
        streak = 0
        for r in reversed(rs):
            if r["dir_hit"] == "0":
                streak += 1
            elif r["dir_hit"] == "1":
                break
        lines.append(f"【{code}】有效样本 {n_final}，方向命中 {n_hit}"
                     f"（{fmt_pct(rate)}），当前连续错误 {streak} 次")
        lines.append("  日期        方向  置信  窗口  已评  区间涨跌   结果日      入场  目标  止损  判定")
        for r in rs:
            ret = fmt_pct(r["ret"]) if r["ret"] is not None else "—"
            result_day = r["hit_date"] or "—"
            lines.append(
                f"  {r['pred_date']}  {r['direction']:<3}  "
                f"{(str(int(r['conf'])) + '%') if r['conf'] is not None else '—':<4}  "
                f"{r['window_label']:<4}  {r['eval_days']}/{r['window_days']}   "
                f"{ret:<8}  {result_day:<10}  {r['entry_trigger']:<4}  {r['target_hit']:<4}  "
                f"{r['stop_hit']:<4}  {r['note']}"
            )
        lines.append("")

    lines.append("二、滚动汇总")
    lines.append(f"  预测记录 {len(results)} 条；可终评 {len(scored)} 条；方向有效样本 {len(dir_scored)} 条")
    lines.append(f"  结论触发率（止盈成功）：{concl_hits}/{len(concl_scored)} = "
                 f"{fmt_pct((concl_hits / len(concl_scored)) if concl_scored else None)}")
    lines.append(f"  方向命中率：{dir_hits}/{len(dir_scored)} = "
                 f"{fmt_pct((dir_hits / len(dir_scored)) if dir_scored else None)}")
    lines.append(f"  止损触发率：{sum(1 for r in scored if r['stop_hit'] == '1')}/{len(scored)}")
    ent = [r for r in results if r["entry_trigger"] in ("0", "1")]
    ent_hit = sum(1 for r in ent if r["entry_trigger"] == "1")
    lines.append(f"  入场触发率：{ent_hit}/{len(ent)}")
    pending = sum(1 for r in results if r["status"] == "PENDING")
    double = sum(1 for r in results if r["status"] == "DOUBLE")
    nodata = sum(1 for r in results if r["status"] in ("NO_DATA", "INCOMPLETE"))
    neutral = sum(1 for r in results if r["status"] == "NEUTRAL")
    unclear = sum(1 for r in results if r["status"] == "DIR_UNCLEAR")
    lines.append(f"  未决 {pending}；双触未决 {double}；中性/观望 {neutral}；方向不明 {unclear}；无数据/参数不全 {nodata}")
    lines.append("")

    lines.append("三、置信度校准（口径：结论是否触发=止盈成功）")
    lines.append(f"  整体：声称均值 {fmt_pct(factors['overall']['claimed_avg'])} vs 实际触发 "
                 f"{fmt_pct(factors['overall']['actual_conclusion_hit_rate'])} → 系数 {overall_factor}"
                 f"（方向命中率另为 {fmt_pct(factors['overall']['direction_hit_rate'])}）")
    for name, _, _ in BUCKETS:
        c = calib[name]
        lines.append(f"  {name:<6} n={c['n']:<2} 声称均值 {fmt_pct(c['claimed_avg'])}  "
                     f"实际触发 {fmt_pct(c['actual_hit_rate']):<6} 系数 {c['factor']}"
                     f"{'' if c['applied'] else '（样本不足，暂不调整）'}")
    lines.append("")

    lines.append("四、偏差诊断")
    diags = []
    for code, rs in sorted(by_code.items()):
        fin = [r for r in rs if r["dir_hit"] in ("1", "0")]
        if len(fin) >= 2:
            h = sum(1 for r in fin if r["dir_hit"] == "1")
            rate = h / len(fin)
            if rate <= 0.4:
                diags.append(f"{code} 近{len(fin)}次方向仅命中{h}次（{fmt_pct(rate)}），需复核其核心驱动是否已失效")
    hi_group = [r for r in dir_scored if r["conf"] is not None and r["conf"] >= 60]
    lo_group = [r for r in dir_scored if r["conf"] is not None and r["conf"] < 60]
    if hi_group and lo_group:
        hi_rate = sum(1 for r in hi_group if r["dir_hit"] == "1") / len(hi_group)
        lo_rate = sum(1 for r in lo_group if r["dir_hit"] == "1") / len(lo_group)
        if lo_rate > hi_rate + 0.15:
            diags.append(f"置信度倒挂：<60% 组命中 {fmt_pct(lo_rate)} 高于 ≥60% 组 {fmt_pct(hi_rate)}，"
                         f"高置信度存在虚高，按系数 {overall_factor} 下调")
    if ent and ent_hit / len(ent) < 0.5:
        diags.append(f"入场触发率仅 {ent_hit}/{len(ent)}：‘等回踩’型方案大量未成交，"
                     f"趋势型品种需增加突破追入备选")
    if len(results) and sum(1 for r in results if r["stop_hit"] == "1") / len(results) > 0.3:
        diags.append("止损触发率偏高，需检查止损是否过近或方向判断失准")
    if pending / max(1, len(results)) > 0.4:
        diags.append(f"未决样本占比 {pending}/{len(results)}，窗口尚未走完，本期结论仅供参考")
    if not diags:
        diags.append("未发现显著系统性偏差")
    for x in diags:
        lines.append(f"  - {x}")
    lines.append("")

    lines.append("五、注入今日分析的提示")
    bucket_desc = ", ".join(f"{k}:{v['factor']}" for k, v in calib.items())
    lines.append(f"  1) 今日基础置信度须先乘以校准系数：整体 ×{overall_factor}；"
                 f"按档位分别 ×{bucket_desc}")
    lines.append("  2) 若某品种近5日连续错误或命中率≤40%，其同向结论必须额外给出反向证据复核，"
                 "并在报告中标注‘该方向近期失效风险’。")
    for code, rs in sorted(by_code.items()):
        fin = [r for r in rs if r["dir_hit"] in ("1", "0")]
        if len(fin) >= 2:
            h = sum(1 for r in fin if r["dir_hit"] == "1")
            if h / len(fin) <= 0.4:
                lines.append(f"  - {code}：近{len(fin)}次仅中{h}次，重点审视其当前主导逻辑与近期行情的背离原因。")
    lines.append("")
    lines.append("六、数据说明")
    lines.append("  - 本复盘只使用每日 predictions.csv（机器可读）与日K行情，不解析 HTML、不做主观补判。")
    lines.append("  - 同日双触优先用小时线判先后；无小时线则记‘双触未决’，不计分。")
    lines.append("  - 未到期记‘未决’，不进入命中率分母；±阈值内记‘方向不明’，同样不计分。")

    text = "\n".join(lines) + "\n"
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}")
        print(f"wrote {log_path}")
        if args.factors_out:
            print(f"wrote {args.factors_out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
