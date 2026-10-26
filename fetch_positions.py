#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 akshare（新浪源）抓取期货前 20 名会员持仓排名，输出「持仓席位」维度 JSON。

用法:
  python3 fetch_positions.py --varieties 沥青,苯乙烯,生猪,红枣,焦煤,鸡蛋,碳酸锂,甲醇 --date 2026-10-26 --out /workspace/positions.json
  python3 fetch_positions.py --contracts BU2611,EB2612 --date 2026-10-26 --out /workspace/positions.json

说明:
  - --varieties 支持品种代码或中文名，脚本自动按成交量/持仓量最大挑选主力合约。
  - 每个合约输出：前20名多头/空头持仓合计及增减、净持仓、集中度、前5席位明细。
  - --csv 追加/更新长期日志（默认建议 future-analysis-reports/positions-log.csv），供 5 日持仓趋势使用。
  - 当日数据未发布时，自动向前回退最多 5 个自然日，并在输出中标注实际数据日期。
  - 沙箱为全新环境时，脚本会自动 pip install akshare。
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

VAR_NAME = {
    "BU": "沥青", "EB": "苯乙烯", "LH": "生猪", "CJ": "红枣", "JM": "焦煤",
    "JD": "鸡蛋", "LC": "碳酸锂", "MA": "郑醇", "FU": "燃料油", "SC": "原油",
    "M": "豆粕", "C": "玉米", "J": "焦炭", "I": "铁矿石", "RB": "螺纹钢",
    "HC": "热卷", "AP": "苹果", "TA": "PTA", "PX": "对二甲苯", "EG": "乙二醇",
    "V": "PVC", "PP": "聚丙烯", "L": "塑料", "SR": "白糖", "CF": "棉花",
    "OI": "菜油", "RM": "菜粕", "SA": "纯碱", "FG": "玻璃", "UR": "尿素",
    "AG": "白银", "CU": "铜", "AL": "铝", "ZN": "锌", "NI": "镍", "AU": "黄金",
    "SP": "纸浆", "PG": "液化石油气", "RU": "橡胶", "SM": "锰硅", "SF": "硅铁",
}
ALIASES = {"甲醇": "MA", "郑醇": "MA"}


def ensure_akshare():
    try:
        import akshare  # noqa: F401
    except ImportError:
        print("akshare 未安装，正在安装 ...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "akshare"], check=True)
    import akshare
    return akshare


def variety_of(contract):
    """从合约代码提取品种代码（兼容 J2701 这类单字母品种）。"""
    return "".join(ch for ch in contract if ch.isalpha()).upper()


def norm_code(token):
    t = token.strip()
    if t.upper() in VAR_NAME:
        return t.upper()
    if t in ALIASES:
        return ALIASES[t]
    for code, name in VAR_NAME.items():
        if t == name:
            return code
    return None


def resolve_contracts(ak, varieties, errors):
    """按持仓量挑每个品种的主力合约。"""
    out = []
    for v in varieties:
        name = VAR_NAME.get(v)
        if not name:
            errors[v] = "未收录的品种，请改用 --contracts 直接指定合约"
            continue
        try:
            df = ak.futures_zh_realtime(symbol=name)
            df = df[df["symbol"].astype(str).str.match(rf"^{v}\d{{4}}$")]
            if df.empty:
                errors[v] = "未取到可交易合约列表"
                continue
            df = df.copy()
            df["position"] = df["position"].astype(float)
            row = df.loc[df["position"].idxmax()]
            out.append((v, str(row["symbol"]), float(row["position"])))
        except Exception as e:  # noqa: BLE001
            errors[v] = f"主力合约解析失败: {e}"
    return out


def leg(ak, contract, date, symbol):
    df = ak.futures_hold_pos_sina(symbol=symbol, contract=contract, date=date)
    if df is None or df.empty:
        return None
    col = {"多单持仓": "多单持仓", "空单持仓": "空单持仓", "成交量": "成交量"}[symbol]
    rows = []
    for _, r in df.iterrows():
        rows.append([str(r["会员简称"]), float(r[col]), float(r["比上交易增减"])])
    return rows


def upsert_csv(path, date, data):
    """把当日快照幂等写入长期日志（键：日期+品种+合约），供 5 日持仓趋势使用。"""
    fields = ["日期", "品种", "合约", "总持仓", "前20多单", "前20空单", "净持仓",
              "多单增减", "空单增减", "净持仓变化", "多单集中度", "空单集中度"]
    day = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    rows = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows[(r.get("日期"), r.get("品种"), r.get("合约"))] = r
    for c, d in data.items():
        rows[(day, d["variety"], c)] = {
            "日期": day, "品种": d["variety"], "合约": c,
            "总持仓": d["total_oi"], "前20多单": d["long_top20"], "前20空单": d["short_top20"],
            "净持仓": d["net_top20"], "多单增减": d["long_top20_chg"],
            "空单增减": d["short_top20_chg"], "净持仓变化": d["net_chg"],
            "多单集中度": d["long_concentration"], "空单集中度": d["short_concentration"],
        }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(rows, key=lambda x: (x[0] or "", x[1] or "", x[2] or "")):
            w.writerow({k2: rows[k].get(k2, "") for k2 in fields})


def pick_date(ak, contract):
    """从指定日期向前找最近一个有持仓数据的交易日。"""
    today = datetime.now().date()
    for i in range(6):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        try:
            if leg(ak, contract, ds, "多单持仓"):
                return ds
        except Exception:  # noqa: BLE001
            continue
    return today.strftime("%Y%m%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--varieties", default="", help="品种代码或中文名，逗号分隔")
    ap.add_argument("--contracts", default="", help="直接指定合约，逗号分隔")
    ap.add_argument("--date", default="", help="YYYYMMDD 或 YYYY-MM-DD，缺省为最近有数据的交易日")
    ap.add_argument("--out", required=True)
    ap.add_argument("--csv", default="", help="长期日志 CSV 路径（按 日期+品种+合约 幂等 upsert）")
    ap.add_argument("--ohlc", default="", help="可选：tools/fetch_ohlc.py 产出的日K JSON，用于补总持仓算集中度")
    args = ap.parse_args()

    ak = ensure_akshare()
    errors = {}

    oi_map = {}
    if args.ohlc and os.path.isfile(args.ohlc):
        with open(args.ohlc, encoding="utf-8") as f:
            for sym, days in (json.load(f).get("daily") or {}).items():
                oi_map[sym.upper()] = {d.replace("-", ""): v.get("oi") for d, v in days.items()}

    targets = []
    if args.contracts:
        for c in args.contracts.split(","):
            c = c.strip().upper()
            if c:
                targets.append((variety_of(c), c, None))
    variants = [norm_code(x) for x in args.varieties.split(",") if x.strip()]
    bad = [x for x, y in zip(args.varieties.split(","), variants) if y is None and x.strip()]
    for b in bad:
        errors[b] = "未收录的品种，请改用 --contracts 直接指定合约"
    variants = [v for v in variants if v]
    if variants:
        targets += resolve_contracts(ak, variants, errors)

    if not targets:
        print(json.dumps({"error": "no target", "errors": errors}, ensure_ascii=False))
        return

    date = None
    if args.date:
        date = args.date.replace("-", "")
    else:
        date = pick_date(ak, targets[0][1])

    data = {}
    for v, contract, oi in targets:
        try:
            longs = leg(ak, contract, date, "多单持仓")
            shorts = leg(ak, contract, date, "空单持仓")
            if not longs or not shorts:
                errors[contract] = f"{date} 无持仓排名数据"
                continue
            total_oi = (oi_map.get(contract.upper(), {}) or {}).get(date) or oi
            long_sum = sum(x[1] for x in longs)
            short_sum = sum(x[1] for x in shorts)
            long_chg = sum(x[2] for x in longs)
            short_chg = sum(x[2] for x in shorts)
            data[contract] = {
                "variety": v or variety_of(contract),
                "date": date,
                "total_oi": total_oi,
                "long_top20": long_sum,
                "short_top20": short_sum,
                "net_top20": long_sum - short_sum,
                "long_top20_chg": long_chg,
                "short_top20_chg": short_chg,
                "net_chg": long_chg - short_chg,
                "long_concentration": round(long_sum / total_oi, 4) if total_oi else None,
                "short_concentration": round(short_sum / total_oi, 4) if total_oi else None,
                "top_long": longs[:5],
                "top_short": shorts[:5],
            }
        except Exception as e:  # noqa: BLE001
            errors[contract] = str(e)

    out = {
        "source": "akshare.futures_hold_pos_sina",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "date": date,
        "positions": data,
        "errors": errors,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if args.csv:
        upsert_csv(args.csv, date, data)
        print(f"upsert {args.csv}")

    for c, d in data.items():
        lc, sc = d["long_concentration"], d["short_concentration"]
        conc = f"多{lc:.1%}/空{sc:.1%}" if (lc is not None and sc is not None) else "集中度 未取总持仓"
        print(f"{c} {d['date']} 前20多单 {d['long_top20']:.0f}（{d['long_top20_chg']:+.0f}） "
              f"空单 {d['short_top20']:.0f}（{d['short_top20_chg']:+.0f}） "
              f"净持仓 {d['net_top20']:+.0f}（{d['net_chg']:+.0f}） {conc}")
    print(f"wrote {args.out}: {len(data)} contracts")
    if errors:
        print("errors:", json.dumps(errors, ensure_ascii=False))


if __name__ == "__main__":
    main()
