#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易所仓单日报采集（SHFE/INE、CZCE、GFEX、DCE 多源回退）。

用法:
  python3 tools/fetch_warehouse.py --date 2026-11-20 --varieties 沥青,燃料油,红枣,甲醇,碳酸锂,苯乙烯,焦煤 --out future-analysis-reports/2026-11-20/warehouse.json

说明:
  - 上期所/上期能源：旧接口 .../dailydata/{date}dailystock.dat 已对近期日期返回404，
    改用官网新址 /data/tradedata/future/stockdata/dailystock_{date}/ZH/all.html 解析。
  - 郑商所：akshare.futures_warehouse_receipt_czce（可用）。
  - 广期所：akshare 解析层字段变更会报 KeyError，本脚本直接调用官方 JSON 接口并自行汇总。
  - 大商所：官网接口返回 412（反爬），失败时回退东方财富库存（标注口径为「交易所库存/仓单代理」，非严格仓单）。
"""
import argparse
import io
import json
import os
import re
import sys
from datetime import datetime

import akshare as ak
import pandas as pd
import requests

SHFE_NAMES = {"沥青": "石油沥青", "燃料油": "燃料油", "低硫燃油": "低硫燃料油", "石油沥青": "石油沥青"}
CZCE_CODES = {"红枣": "CJ", "甲醇": "MA", "郑醇": "MA", "PTA": "TA", "纯碱": "SA", "玻璃": "FG"}
GFEX_CODES = {"碳酸锂": "LC", "工业硅": "SI"}
DCE_CODES = {"苯乙烯": "EB", "生猪": "LH", "焦煤": "JM", "鸡蛋": "JD", "焦炭": "J",
             "豆粕": "M", "玉米": "C", "铁矿石": "I", "聚丙烯": "PP", "PVC": "V", "塑料": "L"}
CODE_ALIAS = {"BU": "沥青", "FU": "燃料油", "LU": "低硫燃油", "CJ": "红枣", "MA": "甲醇",
              "LC": "碳酸锂", "SI": "工业硅", "EB": "苯乙烯", "LH": "生猪", "JM": "焦煤",
              "JD": "鸡蛋", "J": "焦炭", "M": "豆粕", "C": "玉米", "I": "铁矿石",
              "PP": "聚丙烯", "V": "PVC", "L": "塑料"}


def norm(name):
    n = name.strip()
    return CODE_ALIAS.get(n.upper(), n)


def shfe_receipt(date, targets):
    url = (f"https://www.shfe.com.cn/data/tradedata/future/stockdata/"
           f"dailystock_{date}/ZH/all.html")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    tables = pd.read_html(io.StringIO(r.text))
    out = {}
    for name in targets:
        real = SHFE_NAMES.get(name, name)
        total, chg = 0.0, 0.0
        found = False
        for df in tables:
            if df.empty or df.shape[1] < 4:
                continue
            first = str(df.iloc[0, 0])
            if real not in first:
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
            out[name] = {"date": date, "receipts": round(total, 1), "chg": round(chg, 1),
                         "source": "SHFE 仓单日报（官网新址）"}
    return out


def czce_receipt(date, names):
    data = ak.futures_warehouse_receipt_czce(date=date)
    out = {}
    for name in names:
        code = CZCE_CODES.get(name)
        if not code or code not in data:
            continue
        df = data[code]
        subt = df[df.iloc[:, 0].astype(str) == "小计"]
        qty_cols = [c for c in df.columns if "仓单数量" in str(c)]
        qty = sum(float(subt[c].fillna(0).sum()) for c in qty_cols) if qty_cols else 0
        chg = float(subt["当日增减"].fillna(0).sum()) if "当日增减" in df else 0
        fc = float(subt["有效预报"].fillna(0).sum()) if "有效预报" in df else 0
        out[name] = {"date": date, "receipts": round(qty, 1), "chg": round(chg, 1),
                     "forecast": round(fc, 1), "source": "CZCE 仓单日报"}
    return out


def gfex_receipt(date, names):
    r = requests.post("http://www.gfex.com.cn/u/interfacesWebTdWbillWeeklyQuotes/loadList",
                      data={"gen_date": date}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    rows = (r.json() or {}).get("data") or []
    out = {}
    for name in names:
        code = GFEX_CODES.get(name)
        if not code:
            continue
        qty = sum(float(x.get("wbillQty") or 0) for x in rows
                  if str(x.get("varietyOrder", "")).lower() == code.lower())
        diff = sum(float(x.get("diff") or 0) for x in rows
                   if str(x.get("varietyOrder", "")).lower() == code.lower())
        if qty or diff or rows:
            out[name] = {"date": date, "receipts": round(qty, 1), "chg": round(diff, 1),
                         "source": "GFEX 仓单日报（官方JSON）"}
    return out


def dce_receipt(date, names):
    out = {}
    try:
        df = ak.futures_warehouse_receipt_dce(date=date)
        for name in names:
            code = DCE_CODES.get(name)
            if not code:
                continue
            sub = df[df["品种代码"].astype(str).str.upper() == code]
            if sub.empty:
                continue
            out[name] = {"date": date,
                         "receipts": round(float(sub["今日仓单量（手）"].fillna(0).sum()), 1),
                         "chg": round(float(sub["增减（手）"].fillna(0).sum()), 1),
                         "source": "DCE 仓单日报"}
    except Exception:  # noqa: BLE001
        pass
    if len(out) == len([n for n in names if n in DCE_CODES]):
        return out
    # 回退：东方财富库存（口径为交易所库存代理，非严格仓单）
    for name in names:
        if name in out or name not in DCE_CODES:
            continue
        try:
            inv = ak.futures_inventory_em(symbol=name).dropna().tail(3)
            last = inv.iloc[-1]
            out[name] = {"date": str(last["日期"]), "receipts": float(last["库存"]),
                         "chg": float(last["增减"]), "source": "EM 库存（DCE仓单接口412反爬，代理口径）",
                         "note": "非严格仓单，仅作库存维度参考"}
        except Exception:  # noqa: BLE001
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD 或 YYYYMMDD")
    ap.add_argument("--varieties", required=True, help="逗号分隔，支持中文名或品种代码")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    date = args.date.replace("-", "")
    names = [norm(x) for x in args.varieties.split(",") if x.strip()]

    shfe_targets = [n for n in names if n in SHFE_NAMES]
    czce_targets = [n for n in names if n in CZCE_CODES]
    gfex_targets = [n for n in names if n in GFEX_CODES]
    dce_targets = [n for n in names if n in DCE_CODES]

    result, errors = {}, {}
    for label, fn, targets in (
            ("SHFE", shfe_receipt, shfe_targets),
            ("CZCE", czce_receipt, czce_targets),
            ("GFEX", gfex_receipt, gfex_targets),
            ("DCE", dce_receipt, dce_targets)):
        if not targets:
            continue
        try:
            result.update(fn(date, targets))
        except Exception as e:  # noqa: BLE001
            errors[label] = repr(e)[:160]

    for n in names:
        if n not in result:
            errors.setdefault(n, "未获取到仓单数据")
    out = {"asof": f"{date[:4]}-{date[4:6]}-{date[6:]}",
           "fetched_at": datetime.now().isoformat(timespec="seconds"),
           "source": "SHFE新址/CZCE/GFEX官方/DCE(或EM回退)", "warehouse": result, "errors": errors}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    for k, v in result.items():
        print(f"{k} {v['date']} 仓单 {v['receipts']:.0f}（{v['chg']:+.0f}） [{v['source']}]")
    if errors:
        print("errors:", json.dumps(errors, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
