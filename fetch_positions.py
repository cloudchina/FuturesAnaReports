#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 akshare（新浪源）抓取期货前 20 名会员持仓排名，输出「持仓席位」维度 JSON。

同时顺带采集交易所仓单日报，在 --out 同目录生成 warehouse.json（失败不影响持仓结果）。

用法:
  python3 fetch_positions.py --varieties 沥青,苯乙烯,生猪,红枣,焦煤,鸡蛋,碳酸锂,甲醇 --date 2026-10-26 --out /workspace/positions.json
  python3 fetch_positions.py --contracts BU2611,EB2612 --date 2026-10-26 --out /workspace/positions.json

说明:
  - --varieties 支持品种代码或中文名，脚本自动按成交量/持仓量最大挑选主力合约。
  - 每个合约输出：前20名多头/空头持仓合计及增减、净持仓、集中度、前5席位明细。
  - 持仓排名取数顺序：新浪 → 交易所官方（GFEX/SHFE）→ 向前回退最多5个自然日，并标注实际日期与来源。
  - 仓单：SHFE 官网新址解析、CZCE、GFEX 官方 JSON；DCE 反爬时回退 EM 库存并标注口径。
  - --csv 追加/更新长期日志（默认建议 future-analysis-reports/positions-log.csv），供 5 日持仓趋势使用。
  - 沙箱为全新环境时，脚本会自动 pip install akshare。
"""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests

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


SHFE_VARS = {"BU", "FU", "LU", "RU", "SP", "AL", "CU", "ZN", "PB", "NI", "SN", "AU", "AG",
             "RB", "HC", "SS", "BR", "AO", "EC"}
GFEX_VARS = {"LC", "SI"}


def official_legs(ak, contract, date):
    """交易所官方持仓排名（新浪缺失时回退）：GFEX 官方接口、SHFE 官方接口。

    大商所官网接口返回 412 反爬，无法使用，仍依赖新浪（通常滞后 1 个交易日）。
    """
    v = variety_of(contract)
    try:
        if v in GFEX_VARS:
            data = ak.futures_gfex_position_rank(date=date)
            df = data.get(contract.lower())
            if df is None or df.empty:
                return None
            longs = [[str(r["long_party_name"]), float(r["long_open_interest"]),
                      float(r["long_open_interest_chg"])] for _, r in df.iterrows()]
            shorts = [[str(r["short_party_name"]), float(r["short_open_interest"]),
                       float(r["short_open_interest_chg"])] for _, r in df.iterrows()]
            return longs, shorts
        if v in SHFE_VARS:
            data = ak.get_shfe_rank_table(date=date, vars_list=[v])
            df = data.get(contract.lower())
            if df is None or df.empty:
                return None
            longs = [[str(r["long_party_name"]), float(r["long_open_interest"]),
                      float(r["long_open_interest_chg"])] for _, r in df.iterrows()]
            shorts = [[str(r["short_party_name"]), float(r["short_open_interest"]),
                       float(r["short_open_interest_chg"])] for _, r in df.iterrows()]
            return longs, shorts
    except Exception:  # noqa: BLE001
        return None
    return None


def resolve_legs(ak, contract, date):
    """按 新浪→交易所官方→向前回退最多5个自然日 的顺序取持仓排名。"""
    base = datetime.strptime(date, "%Y%m%d").date()
    for i in range(6):
        ds = (base - timedelta(days=i)).strftime("%Y%m%d")
        longs = leg(ak, contract, ds, "多单持仓")
        shorts = leg(ak, contract, ds, "空单持仓")
        if longs and shorts:
            return longs, shorts, ds, "sina"
        pair = official_legs(ak, contract, ds)
        if pair:
            return pair[0], pair[1], ds, "exchange"
    return None, None, None, None


# ---------------- 仓单日报（随持仓一并采集，输出 warehouse.json） ----------------
SHFE_WH = {"BU": "石油沥青", "FU": "燃料油", "LU": "低硫燃料油"}
CZCE_WH = {"CJ": "CJ", "MA": "MA"}
GFEX_WH = {"LC": "lc", "SI": "si"}
DCE_WH = {"EB": "EB", "LH": "LH", "JM": "JM", "JD": "JD", "J": "J",
          "M": "M", "C": "C", "I": "I", "PP": "PP", "V": "V", "L": "L"}


def wh_shfe(date, codes):
    url = (f"https://www.shfe.com.cn/data/tradedata/future/stockdata/"
           f"dailystock_{date}/ZH/all.html")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    tables = pd.read_html(io.StringIO(r.text))
    out = {}
    for code in codes:
        real = SHFE_WH[code]
        total = chg = 0.0
        found = False
        for df in tables:
            if df.empty or df.shape[1] < 4 or real not in str(df.iloc[0, 0]):
                continue
            found = True
            cols = [str(c) for c in df.columns]
            qty_col = next((i for i, c in enumerate(cols) if "期货" in c), 2)
            chg_col = next((i for i, c in enumerate(cols) if "增减" in c), 3)
            for _, row in df.iloc[1:].iterrows():
                cells = [str(x) for x in row.tolist()]
                if any(x in ("合计", "总计", "小计") for x in cells[:3]):
                    continue
                try:
                    total += float(str(row.iloc[qty_col]).replace(",", ""))
                    chg += float(str(row.iloc[chg_col]).replace(",", ""))
                except (ValueError, TypeError):
                    continue
        if found:
            out[code] = {"date": date, "receipts": round(total, 1), "chg": round(chg, 1),
                         "source": "SHFE 仓单日报（官网新址）"}
    return out


def wh_czce(ak, date, codes):
    data = ak.futures_warehouse_receipt_czce(date=date)
    out = {}
    for code in codes:
        key = CZCE_WH[code]
        if key not in data:
            continue
        df = data[key]
        subt = df[df.iloc[:, 0].astype(str) == "小计"]
        qty_cols = [c for c in df.columns if "仓单数量" in str(c)]
        qty = sum(float(subt[c].fillna(0).sum()) for c in qty_cols) if qty_cols else 0
        chg = float(subt["当日增减"].fillna(0).sum()) if "当日增减" in df else 0
        fc = float(subt["有效预报"].fillna(0).sum()) if "有效预报" in df else 0
        out[code] = {"date": date, "receipts": round(qty, 1), "chg": round(chg, 1),
                     "forecast": round(fc, 1), "source": "CZCE 仓单日报"}
    return out


def wh_gfex(date, codes):
    r = requests.post("http://www.gfex.com.cn/u/interfacesWebTdWbillWeeklyQuotes/loadList",
                      data={"gen_date": date}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    rows = (r.json() or {}).get("data") or []
    out = {}
    for code in codes:
        key = GFEX_WH[code].lower()
        qty = sum(float(x.get("wbillQty") or 0) for x in rows
                  if str(x.get("varietyOrder", "")).lower() == key)
        diff = sum(float(x.get("diff") or 0) for x in rows
                   if str(x.get("varietyOrder", "")).lower() == key)
        if rows:
            out[code] = {"date": date, "receipts": round(qty, 1), "chg": round(diff, 1),
                         "source": "GFEX 仓单日报（官方JSON）"}
    return out


def wh_dce(ak, date, codes):
    out = {}
    try:
        df = ak.futures_warehouse_receipt_dce(date=date)
        for code in codes:
            sub = df[df["品种代码"].astype(str).str.upper() == DCE_WH[code]]
            if sub.empty:
                continue
            out[code] = {"date": date,
                         "receipts": round(float(sub["今日仓单量（手）"].fillna(0).sum()), 1),
                         "chg": round(float(sub["增减（手）"].fillna(0).sum()), 1),
                         "source": "DCE 仓单日报"}
    except Exception:  # noqa: BLE001
        pass
    for code in codes:
        if code in out:
            continue
        try:
            inv = ak.futures_inventory_em(symbol=VAR_NAME[code]).dropna().tail(3)
            last = inv.iloc[-1]
            out[code] = {"date": str(last["日期"]), "receipts": float(last["库存"]),
                         "chg": float(last["增减"]),
                         "source": "EM 库存（DCE仓单接口412反爬，代理口径）",
                         "note": "非严格仓单，仅作库存维度参考"}
        except Exception:  # noqa: BLE001
            continue
    return out


def collect_warehouse(ak, date, codes):
    """按交易所分派采集仓单，返回 {code: {...}}；单交易所失败不影响其他交易所。"""
    codes = [c for c in dict.fromkeys(codes) if c]
    result, errors = {}, {}
    groups = (("SHFE", wh_shfe, [c for c in codes if c in SHFE_WH], False),
              ("CZCE", wh_czce, [c for c in codes if c in CZCE_WH], True),
              ("GFEX", wh_gfex, [c for c in codes if c in GFEX_WH], False),
              ("DCE", wh_dce, [c for c in codes if c in DCE_WH], True))
    for label, fn, targets, need_ak in groups:
        if not targets:
            continue
        try:
            result.update(fn(ak, date, targets) if need_ak else fn(date, targets))
        except Exception as e:  # noqa: BLE001
            errors[label] = repr(e)[:160]
    for c in codes:
        if c not in result:
            errors.setdefault(c, "未获取到仓单数据")
    return {"asof": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": "SHFE新址/CZCE/GFEX官方/DCE(或EM回退)",
            "warehouse": result, "errors": errors}


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
            longs, shorts, actual, source = resolve_legs(ak, contract, date)
            if not longs or not shorts:
                errors[contract] = f"{date} 无持仓排名数据（新浪及交易所官方均不可用）"
                continue
            total_oi = (oi_map.get(contract.upper(), {}) or {}).get(actual) or oi
            long_sum = sum(x[1] for x in longs)
            short_sum = sum(x[1] for x in shorts)
            long_chg = sum(x[2] for x in longs)
            short_chg = sum(x[2] for x in shorts)
            data[contract] = {
                "variety": v or variety_of(contract),
                "date": actual,
                "source": source,
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

    wh = None
    try:
        codes = [v or variety_of(c) for v, c, _ in targets]
        wh = collect_warehouse(ak, date, codes)
        wh_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "warehouse.json")
        with open(wh_path, "w", encoding="utf-8") as f:
            json.dump(wh, f, ensure_ascii=False, indent=2)
        for c, v in wh["warehouse"].items():
            print(f"仓单 {c} {v['date']} {v['receipts']:.0f}（{v['chg']:+.0f}） [{v['source']}]")
        print(f"wrote {wh_path}")
    except Exception as e:  # noqa: BLE001
        print("warehouse skipped:", repr(e)[:160], file=sys.stderr)

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
