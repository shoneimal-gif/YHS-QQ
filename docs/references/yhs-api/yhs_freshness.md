> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 数据新鲜度

> 数据准备、旧值兜底、404 与 503 的分工——Data API 的数据行为契约

本页是 Data API 的数据行为契约：新鲜度如何表达、四种数据状态（正常返回 / 准备中 / 旧值兜底 / 不可用）分别长什么样、你的程序该怎么处理。探测数据状态不消耗配额（见[配额](/api/quota-and-rate-limits)），可以放心试。

## 1. 正常返回：看内容时间，不看响应时间

响应的新鲜度以 `_meta` 里的**内容时间**表达（如 `as_of` 或各数据集自己的时间字段）：它是数据本身的时间戳，而不是服务器处理请求的时刻。判断「数据新不新」永远以内容时间为准。

不同数据集的更新节奏天然不同：行情类是秒到分钟级，披露类（国会交易、财报）是小时到天级。各数据集的节奏见对应的[目录导读](/api/catalog)。

## 2. `202`：数据正在准备

按标的查询的端点（目录里标注 on-demand 的），**首次**请求某个此前无人查询过的标的时可能返回：

```json theme={null}
{
  "data": null,
  "_meta": {
    "status": "materializing",
    "retry_after_seconds": 5,
    "hint": "Data is being prepared; retry shortly."
  }
}
```

这不是错误：该标的的数据正在准备，通常几秒内就绪。按 `retry_after_seconds` 重试即可（202 与重试均不消耗配额）。同一标的就绪后，后续请求直接返回数据。

## 3. 旧值兜底：`stale` 与 notice

数据更新暂时中断时，API 的选择是**返回最近一次的有效数据并如实标注**，而不是报错。此时 `_meta` 会带上 stale 标注与说明。你的程序应当：

* 需要严格新鲜度的场景（如实时信号）：检查内容时间，超出你的容忍窗口就跳过本轮；
* 容忍旧值的场景（如日报、回顾）：直接使用，无需特殊处理。

<Note>
  按季/按月披露的数据（机构 13F、财报、做空回补数据等）**不会**被标注 stale——数据是「几个月前的」本来就是这类披露的正常状态，内容时间如实给出，由你按披露节奏理解。
</Note>

## 4. `404` 与 `503`：一个别重试，一个请重试

这两个状态码的语义被刻意分开，请按语义处理：

| 状态 | code | 含义 | 你该做什么 |
| - | - | - | - |
| `404` | `NOT_FOUND` | **目录边界**：请求的键不在服务范围内 | 不要重试；查 `GET /v1/discover` 确认可用能力 |
| `404` | `RANGE_TOO_DEEP` | **范围问题**：历史查询的 `from` 早于保留窗口，标的本身受支持 | 收窄 `from`/`to` 到窗口内重试；**不要**放弃该标的 |
| `503` | `READ_MODEL_UNAVAILABLE` | **暂时不可用**：数据此刻缺失但属于服务范围 | 按 `error.retry_after_seconds` 重试 |

同为 `404`，`NOT_FOUND` 与 `RANGE_TOO_DEEP` 的处理方向相反——**程序分支请按 `error.code` 判断，不要只看 HTTP 状态码**。

`503` 的错误体携带 `recoverable: true` 与 `retry_after_seconds`，适合程序自动处理。

Dealer GEX 与 Dealer Heatmap snapshot 只接受当前应有的固定五分钟采样点：如果该段的固定末分钟尚未归档，会返回 `READ_MODEL_UNAVAILABLE`，不会用上一段或段内更早一分钟代替。Dealer history 中的同类空洞则如实缺行，不会插值或补零。

<Warning>
  一个已知的边界情况：对**确定不可能存在**的数据组合（例如查询一个已过期日期的快照、对不适用的标的类型查基本面），当前也可能返回带重试提示的 `503`。如果同一请求重试两三轮仍无进展，应当放弃并检查请求本身是否有意义，而不是无限重试。
</Warning>

## 程序模式小抄

```text theme={null}
200                    → 用数据；需要新鲜度时校验内容时间
202                    → sleep(retry_after_seconds) 后重试，最多几轮
404 NOT_FOUND          → 放弃该键；必要时查 discover
404 RANGE_TOO_DEEP     → 收窄 from/to 到保留窗口内重试，不要放弃标的
503                    → sleep(retry_after_seconds) 后重试；多轮无进展则放弃
429                    → 按 Retry-After 退避
```


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.