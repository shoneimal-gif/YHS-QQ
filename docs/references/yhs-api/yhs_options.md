> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# options — 期权结构

> 期权链快照、整链历史 Greeks、OI 与成交量分布、单合约行情

期权链的结构面数据：从「这只标的的期权盘口长什么样」（链快照、ATM 链）到「历史上整条链的 Greeks 如何变化」，再到 OI/成交量分布、max pain 与单合约行情。全域按标的/合约 on-demand 查询。

## 端点

**链与结构（按标的）**

| command | 路径 | 内容 |
| - | - | - |
| `options.chain_snapshot` | `/v1/options/chain-snapshot/{ticker}` | 单到期日完整期权链：逐合约报价水位、OI 与 Greeks，支持 `limit` / cursor 分页。**必填 `expiration`**（`YYYY-MM-DD`，不支持一次取全部到期） |
| `options.atm_chains` | `/v1/options/atm-chains/{ticker}` | ATM 附近的链切片。**必填 `expiration`** |
| `options.chain_history` | `/v1/options/chain-history/{ticker}` | 链快照的历史回溯。**必填 `expiration` / `right`（C 或 P）/ `from` / `to` / `interval`** |
| `options.chain_greeks_history` | `/v1/options/chain-greeks-history/{ticker}` | 整条期权链的历史 IV/Greeks + 窗口起点 OI。**必填 `expiration` / `right` / `from` / `to` / `interval`** |
| `options.expiry_breakdown` | `/v1/options/expiry-breakdown/{ticker}` | 按到期日分解的结构概览 |
| `options.oi_change` | `/v1/options/oi-change/{ticker}` | 未平仓合约（OI）变动 |
| `options.oi_per_expiry` | `/v1/options/oi-per-expiry/{ticker}` | OI 按到期日分布 |
| `options.oi_per_strike` | `/v1/options/oi-per-strike/{ticker}` | OI 按行权价分布 |
| `options.options_volume` | `/v1/options/options-volume/{ticker}` | 期权成交量概览 |
| `options.volume_oi_per_expiry` | `/v1/options/volume-oi-per-expiry/{ticker}` | 成交量与 OI 按到期日对照 |
| `options.price_levels` | `/v1/options/price-levels/{ticker}` | 期权成交的价格档位分布 |
| `options.max_pain` | `/v1/options/max-pain/{ticker}` | 各到期日的 max pain 价位 |

**单合约（按 OSI 合约代码）**

| command | 路径 | 内容 |
| - | - | - |
| `options.contract_daily` | `/v1/options/contract-daily/{contract}` | 合约日级行情序列。**必填 `from` / `to`** |
| `options.contract_intraday` | `/v1/options/contract-intraday/{contract}` | 合约日内行情 |
| `options.contract_greeks_series` | `/v1/options/contract-greeks-series/{contract}` | 合约希腊值时间序列。**必填 `from` / `to` / `interval`** |
| `options.contract_volume_profile` | `/v1/options/contract-volume-profile/{contract}` | 合约按成交价格分组的成交量分布，支持 `limit` / cursor 分页 |

**全市场**

| command | 路径 | 内容 |
| - | - | - |
| `options.optionable_tickers` | `/v1/options/optionable-tickers` | 可交易期权的标的清单 |

## 示例

```bash theme={null}
# SPY 的 OI 按行权价分布
curl -s https://api.yehangshe.com/v1/options/oi-per-strike/SPY \
  -H "Authorization: Bearer sk_live_xxxxxxxx"

# 指定到期日的完整期权链（首次请求可能 202；有 next_cursor 时继续翻页）
curl -s "https://api.yehangshe.com/v1/options/chain-snapshot/SPY?expiration=2026-12-18&limit=400" \
  -H "Authorization: Bearer sk_live_xxxxxxxx"

# 单到期日、单方向的整链历史 Greeks
curl -s "https://api.yehangshe.com/v1/options/chain-greeks-history/SPY?expiration=2026-09-18&right=C&from=2026-09-16&to=2026-09-18&interval=1h" \
  -H "Authorization: Bearer sk_live_xxxxxxxx"
```

## 注意点

* **OI 是滞后指标**：未平仓合约数按行业惯例为上一交易日结算后的口径，日内不实时变化；「今天的 OI 变动」要到次日才能完整体现。
* `chain_snapshot` 返回完整单到期日集合：`total_contracts` 是完整数量，`returned_contracts` 是当前页数量；当 `_meta.next_cursor` 非空时继续翻页。每一页都是一次 weight 1 的调用。每行的 `quoted_at` 是该合约报价自己的时间水位，判断报价是否新鲜时优先看它。
* `contract_volume_profile` 使用相同的分页方式，完整数量和当前页数量分别为 `total_levels` / `returned_levels`。
* `chain_greeks_history` 的 OI 是窗口起点后首个上报日的单次快照，不是逐日序列；需要单合约逐日 OI 时使用 `options.contract_daily`。返回可能因响应预算截断，请检查 `total_contracts` / `returned_contracts` / `returned_rows` 与每个合约的 `quotes_truncated`。
* `chain_greeks_history` 的 `contracts[].quotes[].snapshot_at` 是没有 offset 的 ET 市场墙钟；顶层 `as_of` 才是 ISO 8601 UTC 水位。解析行级时间时请显式使用 `America/New_York`，不要按本机时区猜测。
* 历史整链查询的最大跨度随 `interval` 改变：`1m` 约 2 天、`5m` 约 6 天、`10m` 约 11 天、`15m` 约 17 天、`30m` / `1h` 最多 31 天。需要更长窗口时调粗粒度或拆分日期范围。
* RTH 之外的链快照可能仍是上一交易时段的报价与 Greeks；每个交易日 09:30 ET 的首根历史 Greeks 也可能因为尚无有效 NBBO 而出现深度实值合约 `implied_vol_pct=0`、`delta=±1`、`gamma=0`。这些是数据状态，不应直接当作可成交价格或稳定波动率。
* 单合约端点使用 OSI 代码（`标的 + 到期YYMMDD + C/P + 行权价×1000 补零到 8 位`），写错格式返回 `400 CONTRACT_SYMBOL_INVALID`。
* 结构分布类端点（OI/volume 分布、max pain）适合做每日结构分析；需要当日盘中动态可以结合 [flow 域](/api/domains/flow)与 [derived 域](/api/dealer-gex)的实时读面。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.