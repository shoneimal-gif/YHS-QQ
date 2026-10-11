# 行数据适配合同

Heatmap来自已公开的官方schema；其他三类是本项目的兼容行合同及别名。
缺失值保持None，不当作0。新增未知字段忽略，必需字段缺失拒绝信号。

## 榜单

支持 `data` 为行数组，或对象内 `items/rows/rankings/tickers/results/stocks` 行数组。

```json
{"data":[{"ticker":"NVDA","asset_type":"stock","spot_usd":188,"total_volume":100000,"total_oi":120000,"oi_change_pct":0.06,"pcr":0.55}]}
```

支持代码 `ticker/symbol`，现货 `spot_usd/stock_price_usd/underlying_price_usd/price_usd/stock_price/price`，
成交量 `total_volume/option_volume/options_volume/volume`，OI `total_oi/open_interest/total_open_interest/oi`。
类型 `asset_type/security_type/instrument_type/is_etf` 或配置 `symbol_types`。
`oi_change_pct/open_interest_change_pct` 是fraction，也可由 `previous_oi/prior_open_interest` 推导。
PCR `pcr/put_call_ratio/put_call_volume_ratio`，也可由call_volume与put_volume推导。

## Heatmap（官方）

```json
{"data":{"ticker":"NVDA","generated_at":"2026-10-09T13:30:00Z","session_date_et":"2026-10-09","spot_usd":188,"expirations":["2026-10-16"],"cells":[{"expiration":"2026-10-16","strike_usd":187,"net_dealer_gex_usd":2000000},{"expiration":"2026-10-16","strike_usd":195,"net_dealer_gex_usd":-1700000}]},"_meta":{}}
```

这是13:34UTC源值，13:35UTC后才允许处理。合成回放用顶层`simulated=true`说明人工时间水位。

## Flow

支持`data`为行数组或对象内`trades/items/rows/results`。

```json
{"data":{"trades":[{"timestamp":"2026-10-09T09:35:00-04:00","ticker":"NVDA","side":"ASK","right":"C","premium_usd":75000,"volume":2000,"open_interest":1500,"contract_symbol":"O:NVDA261016C00188000","strike":188,"expiration":"2026-10-16"}]}}
```

时间 `timestamp/occurred_at/executed_at/time`必须带时区；标的 `ticker/underlying_ticker/symbol`；
方向 `right/option_type/type`接受 C/P/CALL/PUT；侧 `side/trade_side`接受 ASK/AT_ASK/ABOVE_ASK。
权利金 `premium_usd/premium`；合约日量 `volume/day_volume/contract_volume/daily_volume`；OI `open_interest/oi`。
日期 `expiration/expiration_date`；合约 `contract_symbol/occ_symbol`。
此规则要求同到期同方向真实合约，用成交聚合的`contracts`代替日成交量不合格。

## 入场链

支持`data`为行数组或对象内`contracts/items/rows/results`。

```json
{"data":{"contracts":[{"contract_symbol":"O:NVDA261016C00188000","strike":188,"expiration":"2026-10-16","right":"C","bid":2.45,"ask":2.55,"delta":0.45,"gamma":0.04,"quoted_at":"2026-10-09T09:35:00-04:00"}]},"_meta":{"next_cursor":null}}
```

合约 `contract_symbol/symbol/occ_symbol`；行权价 `strike/strike_usd`；方向 `right/option_type`；
报价 `bid/bid_usd/bid_price_usd`与`ask/ask_usd/ask_price_usd`；
报价源时间 `quoted_at/timestamp/as_of`必须存在且新鲜，不能用顶层as_of替代未知行报价时间。
Greeks为`delta/gamma`，缺失时显式采用配置默认参数。
入场选择已被Flow确认的同一合约，OCC的标的/日期/方向/行权价由模拟交易模块二次校验。

## 采集与回放

JSONL第一条：`{"kind":"universe","session_date":"YYYY-MM-DD","payload":原始榜单}`。
后续每条：`{"kind":"frame","time":Heatmap接收时间,"ticker":"NVDA","heatmap":原始响应,"flow":可选,"flow_received_at":可选,"chain":可选,"chain_received_at":可选}`。
未触发阶段就不记录该阶段字段；不得用未来帧补齐旧帧的Flow或报价。
按采集顺序回放，不自动排序、不插值、不补零、不回填不存在的节点。
