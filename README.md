# FuturesAnaReports

国内期货多品种深度分析日报的持久化仓库。每日定时任务产出的分析报告、复盘数据与关联图谱配置均保存于此。

## 目录结构

- `future-analysis-reports/{YYYY-MM-DD}/`：每日报告、`predictions.csv`、`positions.json`（当日持仓席位快照）
- `future-analysis-reports/positions-log.csv`：跨交易日持仓长期日志（日期+品种+合约，幂等 upsert）
- `fetch_positions.py`：akshare（新浪源）抓取前20名会员持仓排名，支持 `--csv` 写日志、`--ohlc` 补总持仓算集中度
- `tools/position_trend.py`：由日志生成 5 日持仓趋势（净持仓、增减、连续同向天数、集中度、换月标记）
- `tools/fetch_ohlc.py`、`tools/fetch_intraday.py`、`tools/review_engine.py`：复盘链路（日K、小时线、确定性判分）
