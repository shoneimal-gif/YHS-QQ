> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 通用约定

> 响应信封、分页、时间语义与命名规则

## 响应信封

成功响应固定为 `data` + `_meta` 两段：

```json theme={null}
{
  "data": { "...": "业务数据" },
  "_meta": {
    "request_id": "req_...",
    "...": "分页游标、数据新鲜度等请求级元信息"
  }
}
```

错误响应只有 `error` 一段（形状见[错误处理](/errors)）。每个响应（无论成败）都带 `request_id`，反馈问题时请附上它。

* 字段命名统一 `snake_case`。
* 金额类字段以 `_usd` 后缀标注单位。
* 缺数据的字段返回 `null`，不会伪装成 `0` 或空串。
* 未知的查询参数不会被静默忽略，而是返回 `400 VALIDATION_ERROR`——拼错参数名能立刻被发现。

## 分页

列表、历史类端点以及部分大型对象快照使用游标分页：

1. 首次请求带 `limit`（各端点有默认值与上限，见 Reference）；
2. 响应的 `_meta.next_cursor` 非空时，原样作为 `cursor` 参数请求下一页；
3. `next_cursor` 为 `null` 或缺失即最后一页。

`cursor` 是不透明字符串：不要解析、不要构造、不要跨端点复用。非法或过期的 cursor 返回 `400`。

`options.chain_snapshot` 与 `options.contract_volume_profile` 的 cursor 还绑定当前公共快照；翻页期间快照刷新后，旧 cursor 会返回 `400 VALIDATION_ERROR`，此时应从第一页重新开始。Flow 的 bucket 端点把 `next_cursor` / `truncated` 放在 `data` 内，细节见 [flow 数据域](/api/domains/flow)。

## 时间

* **输入**：ISO 8601。允许 date-only（`YYYY-MM-DD`）；携带时间部分时必须带时区偏移（如 `2026-08-06T14:30:00-04:00`），不接受无时区的裸时间。
* **输出**：时间戳通常携带时区偏移或为 UTC（`Z` 后缀）。少数保留市场墙钟原值的字段会在领域页明确标注；例如 `options.chain_greeks_history` 的行级 `quotes[].snapshot_at` 是无 offset 的 ET，顶层 `as_of` 才是 ISO 8601 UTC 水位。
* **市场语义**：美股市场数据按 **ET（America/New\_York）交易日**组织。date-only 输入在市场语境下按 ET 日历日理解；个别端点的窗口边界细节在其参数说明中单独定义。
* 披露类数据（国会、财报、机构持仓）以**披露时间**与**事件时间**双时间轴呈现，字段含义见各端点 Reference。

## 标的与合约代码

* 股票标的使用大写美股代码（如 `SPY`、`AAPL`）。写法不合法返回 `400 TICKER_INVALID`；写法合法但不在服务范围返回 `404` 或 `503`（区别见[数据新鲜度](/api/data-freshness)）。
* 期权合约使用 OSI 格式（如 `SPY260620C00450000`）。
* 指数类标的的支持范围因端点而异，以 Reference 与 `GET /v1/discover` 为准。

## 幂等与重试

全部数据端点为 `GET`，天然幂等，可安全重试。重试策略见[数据新鲜度](/api/data-freshness)的程序模式小抄与[限流规则](/api/quota-and-rate-limits)。

## 浏览器与 CORS

API Key 面向服务端与本地程序设计，**不建议**在浏览器前端直接使用（Key 会暴露给页面访问者）。浏览器场景请使用产品页面本身。

## 版本与兼容

* 当前版本 `v1`，体现在路径前缀 `/v1/`。
* **新增**响应字段、新增可选参数、新增端点均为非破坏性变更，随时可能发生——你的解析代码应忽略未知字段。
* 正式开放（GA）端点的**删除/重命名**字段、路径或既有语义变更属于破坏性变更：会提前 **90 天**在 [changelog](/api/changelog) 预告后才生效。
* 明确标记为 Preview / Beta、尚处正式契约冻结前的端点可以在 changelog 公告后直接校正；公告必须列出旧契约、替代契约与生效条件。未标记的正式端点不得套用这一例外。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.