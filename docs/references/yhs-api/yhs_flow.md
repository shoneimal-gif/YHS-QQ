> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# flow — 期权资金流

> 查询期权成交流、标的榜单、合约链、分钟桶与合约历史

Flow 域把期权成交流按成交、标的、合约和时间窗口组织成可直接查询的聚合数据。你可以从全市场筛选开始，进入某个标的的概览与榜单，再下钻到精确合约的分钟 bars、桶内成交和多日历史。

所有端点都使用 `flow:read` scope，每次成功调用 weight 为 1。REST 与 MCP 提供同一组 11 个能力。

## 端点

**成交筛选**

| command | 路径 | 内容 |
| - | - | - |
| `flow.trades` | `/v1/flow/trades` | 筛选聚合后的期权成交流；默认 `kind=order`、`min_premium_usd=10000`、`limit=25` |

**按标的查看**

| command | 路径 | 内容 |
| - | - | - |
| `flow.ticker_boards` | `/v1/flow/tickers/{ticker}/boards` | 标的情绪摘要，以及按成交量、权利金、OI、异常程度和 sweep 排列的五组榜单 |
| `flow.ticker_overview` | `/v1/flow/tickers/{ticker}/overview` | 1 日或 7 日 Flow 摘要与 Volume/Premium/OI/Volume-to-OI 排名 |
| `flow.ticker_chain` | `/v1/flow/tickers/{ticker}/chain` | 当日有 Flow 活动的合约链，支持 cursor 分页 |
| `flow.contract_options` | `/v1/flow/tickers/{ticker}/contract-options` | 一个必填到期日下的合约目录，支持 cursor 分页 |
| `flow.ticker_bars` | `/v1/flow/tickers/{ticker}/bars` | 标的盘中 Call/Put 权利金 bars |
| `flow.ticker_bucket` | `/v1/flow/tickers/{ticker}/bucket` | 日级或 1/5/15/30/60 分钟窗口的 0DTE 与其余到期 cohort 摘要、Top contracts 或成交明细 |

**按合约查看**

| command | 路径 | 内容 |
| - | - | - |
| `flow.contract_lookup` | `/v1/flow/contracts/lookup` | 按完整 OCC 合约代码查询合约身份与当日 Flow 活动 |
| `flow.contract_bars` | `/v1/flow/contracts/{contract_symbol}/bars` | 单合约的盘中合约量 bars 或净权利金 bars |
| `flow.contract_bucket` | `/v1/flow/contracts/{contract_symbol}/bucket` | 单合约某个精确五分钟桶内的聚合成交，支持 cursor 分页 |
| `flow.contract_history` | `/v1/flow/contracts/{contract_symbol}/history` | 单合约最多 30 个交易日的 contract-day 历史 |

字段级参数、枚举和响应结构见顶部「API Reference」标签页。

## 示例

```bash theme={null}
# 默认筛选：当日 Order 聚合、权利金至少 $10,000、最多 25 行
curl -s https://api.yehangshe.com/v1/flow/trades \
  -H "Authorization: Bearer sk_live_xxxxxxxx"

# 只看 SPY，最低权利金提高到 $50,000
curl -s "https://api.yehangshe.com/v1/flow/trades?tickers=SPY&kind=order&min_premium_usd=50000" \
  -H "Authorization: Bearer sk_live_xxxxxxxx"

# SPY 最近 7 个交易日的 Flow 概览
curl -s "https://api.yehangshe.com/v1/flow/tickers/SPY/overview?window=7d" \
  -H "Authorization: Bearer sk_live_xxxxxxxx"

# 精确合约查询；冒号做 URL 编码
curl -s "https://api.yehangshe.com/v1/flow/contracts/lookup?contract_symbol=O%3ASPY261218C00600000" \
  -H "Authorization: Bearer sk_live_xxxxxxxx"
```

## 日期与代码

* 所有 Flow 日期参数都是 **ET 交易日**，只接受 `YYYY-MM-DD`，不接受 date-time。
* 建议统一使用带 `O:` 前缀的完整 OCC 合约代码。`flow.trades` 的 `contract_symbols` 筛选要求使用这种 canonical 形式；多个 ticker、合约、到期日、right、side 或 strategy 用逗号分隔。
* `SPX` 是产品级汇总 ticker，概览、bars 和 bucket 会覆盖月度与周度 root；需要精确区分 `SPX` / `SPXW` 时，使用支持 `root` 的合约目录、boards 或 chain 端点。

## 分页与完整性

* `flow.trades`、`flow.contract_options`、`flow.ticker_chain` 和 `flow.contract_bars` 的下一页游标位于 `_meta.next_cursor`。
* `flow.contract_bucket` 与 `flow.ticker_bucket` 的 `next_cursor` / `truncated` 位于 `data` 内，因为它们同时表达源窗口本身是否达到上限。
* cursor 绑定 command、ticker/合约、查询参数、交易日和当前结果快照。跨查询复用，或翻页期间结果发生变化时，服务端返回 `400 VALIDATION_ERROR`；从第一页重新开始即可。
* `truncated=true` 表示源窗口已经达到自身上限。即使最后一页的 `next_cursor` 为 `null`，也不能把该窗口解释为完整全集。

## 注意点

* `flow.contract_lookup` 查询完整 OCC 代码时，会返回当日已经出现 Flow 活动的合约，即使它暂时还没有出现在按到期日浏览的 `flow.contract_options` 中。精确 lookup 与合约目录服务不同场景，不应要求两者在盘中每一刻完全一致。
* Flow 返回的是聚合后的成交与统计，不是逐笔原始成交。`print_count` 表示一个公开聚合包含的成交笔数。
* 金额字段使用 `*_usd`，比例字段使用 fractional `*_pct`（例如 `0.25` 表示 25%）；缺失值为 `null`，不会补成 `0`。
* 读模型短暂不可用时返回 `503 READ_MODEL_UNAVAILABLE`。GET 请求可安全重试；具体策略见[数据新鲜度](/api/data-freshness)。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.