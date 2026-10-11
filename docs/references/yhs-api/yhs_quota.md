> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 配额与限流

> 月配额、速率限制与零消耗规则

## 三层限制

| 维度 | 默认值 | 超限响应 |
| - | - | - |
| 月配额 | **100,000 units / 月**（随订阅附含） | `429 QUOTA_EXHAUSTED` |
| 速率 | **60 请求 / 分钟**（每账户）；另有 200 / 分钟的 IP 上限 | `429 RATE_LIMITED` + `Retry-After` |
| 并发 | **5 个同时在途请求**（每账户） | `429 IN_FLIGHT_EXCEEDED` |

配额与限流按**账户**计，同一账户的多个 Key 共享同一份额度。

## 配额怎么计

规则只有一条：**返回 `200` 的数据请求计 1 unit，其他一律不计。**

具体来说，以下情况全部零消耗：

* 参数错误（`400`）、鉴权失败（`401` / `402` / `403`）、限流（`429`）
* 数据尚未就绪的 `202`（见[数据新鲜度](/api/data-freshness)——按提示重试不会重复计费）
* 未命中目录的 `404`、服务端错误（`5xx`）
* `GET /v1/discover`、`/v1/health`、`/v1/openapi.json`（自描述与元数据端点免费）

因此你不需要为「探测」付费：会话开头查 `discover`、遇到 202 重试、试错参数，都不影响月配额。

## 实时余量

每个响应都携带余量头：

```
X-RateLimit-Limit: 60          # RPM 上限
X-RateLimit-Remaining: 57      # 当前分钟剩余
X-RateLimit-Reset: 1756742400  # 当前分钟窗口重置时刻（epoch 秒）
X-Quota-Limit: 100000          # 月配额
X-Quota-Remaining: 99871       # 本月剩余
X-Quota-Reset-At: 2026-09-01T00:00:00Z
```

也可以随时调 `GET /v1/discover`（免费）拿同样的信息。429 响应额外携带 `Retry-After`（秒）。

## 自动化程序的建议

* 尊重 `Retry-After`：收到 `429` 后按头部等待，不要立即重试。
* 控制并发在 5 以内；批量拉取时串行加小间隔比并发冲击更快跑完。
* 长期任务在启动时查一次 `discover` 的 `monthly_remaining`，接近耗尽时降频。
* 配额在每月第一天重置（精确时刻以 `X-Quota-Reset-At` 为准）。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.