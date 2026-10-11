> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 错误处理

> Data API 错误信封与全部错误码——含义与处理方式

## 错误信封

所有错误响应共用一个形状。`code` 是稳定的机器可读标识（本页锚点与之一一对应），`message` / `hint` 是给人读的说明，可能随时间改进措辞——**程序请匹配 `code`，不要匹配文案**。

```json theme={null}
{
  "error": {
    "code": "QUOTA_EXHAUSTED",
    "tags": ["quota"],
    "message": "Monthly quota exhausted.",
    "hint": "Quota resets at the start of next month; see X-Quota-Reset-At.",
    "docs_url": "https://docs.yehangshe.com/errors#QUOTA_EXHAUSTED",
    "request_id": "req_..."
  }
}
```

可恢复的错误额外携带 `recoverable: true` 与 `retry_after_seconds`（body 为权威；`Retry-After` 响应头同时下发）。反馈问题时请附上 `request_id`（见[页尾](#get-help)）。

除标注外，下列错误响应均**不消耗**月配额。

## 鉴权与权限

<a id="AUTH_MISSING" />

### AUTH\_MISSING — 401

请求没有携带 `Authorization` header。按 `Authorization: Bearer sk_live_...` 携带 Key，见[鉴权](/api/authentication)。

<a id="AUTH_INVALID" />

### AUTH\_INVALID — 401

Key 无效或已被撤销。到[账户页](https://yehangshe.com/app/account)确认 Key 状态，必要时重新生成。

<a id="AUTH_PAUSED" />

### AUTH\_PAUSED — 402

订阅已失效，API 访问随之暂停。Key 未被删除：恢复订阅后同一个 Key 自动恢复可用。

<a id="SCOPE_INSUFFICIENT" />

### SCOPE\_INSUFFICIENT — 403

当前 Key 缺少访问该端点所需的 scope。用 `GET /v1/discover` 查看当前 Key 的 `access_scope` 与可用能力。

<a id="KEY_LIMIT_REACHED" />

### KEY\_LIMIT\_REACHED — 409

已持有 5 个 active Key，无法再生成。先在账户页撤销一个不再使用的 Key。

## 请求校验

<a id="VALIDATION_ERROR" />

### VALIDATION\_ERROR — 400

查询参数或请求体校验失败（非法日期、非法 cursor、未知参数等）。`hint` 会指出具体哪一项不合法；修正后重发。

<a id="TICKER_INVALID" />

### TICKER\_INVALID — 400

标的代码不符合该端点的词法要求（如含小写字母、长度超限）。注意这是「写法不合法」；写法合法但不在服务范围的代码返回的是 [`NOT_FOUND`](#NOT_FOUND) 或 [`READ_MODEL_UNAVAILABLE`](#READ_MODEL_UNAVAILABLE)。

<a id="CONTRACT_SYMBOL_INVALID" />

### CONTRACT\_SYMBOL\_INVALID — 400

期权合约代码不符合 OSI 格式。合约代码形如 `SPY260620C00450000`（标的 + 到期 YYMMDD + C/P + 行权价 ×1000 补零），具体以各端点 Reference 为准。

<a id="INDEX_UNSUPPORTED" />

### INDEX\_UNSUPPORTED — 422

该端点不支持所请求的指数类标的。这是**永久性**拒绝，重试不会改变结果；改用受支持的标的。

<a id="RANGE_TOO_DEEP" />

### RANGE\_TOO\_DEEP — 404

请求的历史范围早于该数据集的保留窗口。收窄 `from` 到保留窗口内（各数据集的窗口见其文档页）。

<a id="RESPONSE_TOO_LARGE" />

### RESPONSE\_TOO\_LARGE — 422

按当前参数生成的响应超出体积上限。API 不会截断 JSON（截断的 JSON 无法解析），而是返回本错误；按 `hint` 收窄查询：降低 `limit`、缩短时间范围，或使用 `format=summary` 档位（支持的端点会在 Reference 标注）。

## 数据可用性

<a id="NOT_FOUND" />

### NOT\_FOUND — 404

请求的资源不在服务目录内（**目录边界**，永久语义）。不要重试；用 `GET /v1/discover` 确认当前可用的数据集与覆盖面。

<a id="READ_MODEL_UNAVAILABLE" />

### READ\_MODEL\_UNAVAILABLE — 503

数据属于服务范围但此刻不可用（例如正在准备中或短暂缺失）。按 `retry_after_seconds` 重试。与 `404` 的分工见[数据新鲜度](/api/data-freshness)；多轮重试无进展时应检查请求组合是否本身无意义（如已过期日期的快照）。

<a id="SERVICE_DISABLED" />

### SERVICE\_DISABLED — 503

该能力当前未提供服务，两种可能：**能力尚未开放**（关注 [changelog](/api/changelog) 了解上线节奏），或**短暂的维护 / 恢复窗口**（如 Dealer Heatmap 的服务恢复期，通常分钟级）。本错误不携带 `retry_after_seconds`，**不适合程序自动重试循环**——与 [`READ_MODEL_UNAVAILABLE`](#READ_MODEL_UNAVAILABLE)（带重试间隔的可自动重试错误）区分处理：收到本错误应降级处理，稍后低频重试；持续数日不恢复则按「未开放」理解。

## 限流与配额

<a id="RATE_LIMITED" />

### RATE\_LIMITED — 429

超出速率限制（每账户 60 请求/分钟，或 IP 级 200/分钟）。按 `Retry-After` 等待后重试；持续触发说明并发或轮询频率需要下调。

<a id="QUOTA_EXHAUSTED" />

### QUOTA\_EXHAUSTED — 429

本月 100,000 units 配额已用完。配额每月初重置（精确时刻见 `X-Quota-Reset-At` 响应头）。提醒：只有 `200` 响应计费，见[配额规则](/api/quota-and-rate-limits)。

<a id="IN_FLIGHT_EXCEEDED" />

### IN\_FLIGHT\_EXCEEDED — 429

同时在途的请求超过 5 个。降低并发，等在途请求完成后再发。

<a id="CIRCUIT_OPEN" />

### CIRCUIT\_OPEN — 429

服务端熔断暂时开启（通常由持续异常流量触发）。按 `Retry-After` 退避，稍后自动恢复。

## 服务端

<a id="UPSTREAM_ERROR" />

### UPSTREAM\_ERROR — 502

数据链路故障。数据读取类端点通常会以旧值兜底（见[数据新鲜度](/api/data-freshness)），出现本错误说明该请求无法降级；稍后重试。

<a id="UPSTREAM_TIMEOUT" />

### UPSTREAM\_TIMEOUT — 504

数据链路超时。同上，稍后重试。

<a id="INTERNAL_ERROR" />

### INTERNAL\_ERROR — 500

未预期的服务端错误。稍后重试；持续出现时请附 `request_id` 反馈。

<a id="get-help" />

## 还是解决不了？

来 [Discord 社区](https://discord.gg/rcktTvUjKw)交流：描述你的问题，附上响应里的 `request_id` 与发生时间（含时区），能大幅加快定位。文档没讲清楚的地方也欢迎直接提——它会变成下一次更新。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.