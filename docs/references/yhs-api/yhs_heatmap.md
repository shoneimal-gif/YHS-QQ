> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Dealer Heatmap API

> 读取 Dealer Heatmap 的 5 分钟固定采样 Top 节点快照与历史

## 概览

Dealer Heatmap Data API 提供产品 Heatmap 数据的公共窄投影：保留采样时真实存在的到期日，并为每个到期日最多返回十个最强正值节点和十个最强负值节点。它不是完整行权价网格，也不能用于复刻产品内整张 Heatmap。

<Warning>本数据集目前处于 Beta 契约收敛阶段；破坏性校正会在 [changelog](/api/changelog) 明确登记。</Warning>

| 端点 | 用途 | 配额消耗 |
| - | - | - |
| `GET /v1/derived/heatmap/{ticker}/snapshot` | 最新一个已完成的 5 分钟 Top 节点快照 | 1 unit / 次 |
| `GET /v1/derived/heatmap/{ticker}/history` | 指定到期日最近三个 NYSE 交易时段的 Top 节点历史 | 1 unit / 页 |

> API Key 在[账户页](https://yehangshe.com/app/account)自助生成（见[快速开始](/api/quickstart)）；鉴权方式为 `Authorization: Bearer sk_live_...`，配额包含于订阅。

## 采样与排名语义

API 每五分钟公开一个固定采样点。例如，`09:30-09:35 ET` 这一段固定读取 `09:34` 的分钟终值，并把 `generated_at` 或 `minute_at` 标记为该段起点 `09:30`。

* 这不是五分钟平均、合计或插值。
* 固定末分钟缺失时，该段直接缺失，不会回退到 `09:33` 等更早分钟。
* 每个到期日独立排名：正值从大到小取前十，负值从小到大取前十；相同数值按行权价升序。
* `net_dealer_gex_usd = 0` 的节点不进入 `cells`。

## Snapshot

```json theme={null}
{
  "data": {
    "ticker": "SPY",
    "generated_at": "2026-08-12T13:30:00Z",
    "session_date_et": "2026-08-12",
    "spot_usd": 642.18,
    "expirations": ["2026-08-12", "2026-08-14", "2026-08-21"],
    "cells": [
      {
        "expiration": "2026-08-14",
        "strike_usd": 645.0,
        "net_dealer_gex_usd": 2345678.0
      },
      {
        "expiration": "2026-08-14",
        "strike_usd": 640.0,
        "net_dealer_gex_usd": -1987654.0
      }
    ]
  }
}
```

* `expirations` 列出该固定采样分钟真实存在的全部归档到期日。
* 某个到期日即使全部节点为零，仍可出现在 `expirations` 中，但不会产生 `cells`。
* `cells` 只包含 `expiration`、`strike_usd` 和 `net_dealer_gex_usd`。
* API 不公开完整 strike 轴、逐节点 call/put 分解、跨到期行聚合、颜色比例尺或内部状态诊断。
* 不接受 `format` 参数；未知参数会返回 `400 VALIDATION_ERROR`。

正负值的产品解读见 [Heatmap 读图指南](/heatmap/reading-the-matrix)。缺席节点不能被解释为精确的 0。

## History

History 一次查询一个到期日，返回该到期日在各固定采样点上的 signed Top 节点。

| 参数 | 类型 | 说明 |
| - | - | - |
| `expiration` | `YYYY-MM-DD` | 必填，到期日（ET 日历日） |
| `from` / `to` | ISO 8601 | 可选；省略 `from` 时读取完整的三个 NYSE 交易时段窗口 |
| `limit` | 整数 | 默认 24，上限 24 |
| `cursor` | 不透明串 | 上一页 `_meta.next_cursor` 原样回传 |

旧的 `strike` 参数和 `/cell-history` 路径已不再属于公共 Data API 契约。

```bash theme={null}
curl -s "https://api.yehangshe.com/v1/derived/heatmap/SPY/history?expiration=2026-08-21&limit=24" \
  -H "Authorization: Bearer sk_live_xxxxxxxx"
```

响应按时间从新到旧排列：

```json theme={null}
{
  "data": [
    {
      "minute_at": "2026-08-12T15:55:00Z",
      "expiration": "2026-08-21",
      "spot_usd": 643.02,
      "cells": [
        {
          "strike_usd": 645.0,
          "net_dealer_gex_usd": 2123456.0
        },
        {
          "strike_usd": 640.0,
          "net_dealer_gex_usd": -1765432.0
        }
      ]
    }
  ],
  "_meta": {
    "next_cursor": null
  }
}
```

* 窗口固定为最近三个 NYSE 交易时段的 RTH；周末、节假日、夏令时与半日市按 NYSE 日历处理。
* `from` 按采样段起点包含，`to` 按采样段起点不包含。早于最老可用交易时段返回 `404 RANGE_TOO_DEEP`。
* 只有该到期日在固定末分钟真实存在时，该段才参与分页；到期日存在但所有值为零时返回空 `cells`。
* 归档为空洞时不生成占位行，不插值、不补零，也不回退到更早分钟。空结果是 `200` 加空数组。

## 新鲜度与调用频率

内容时间以 `generated_at`、`minute_at` 和 `_meta.data_freshness_seconds` 为准。由于公共投影每五分钟才形成一个新采样点，通常没有必要以秒级频率轮询；在需要最新点时，每个采样段完成后请求一次即可。

当前应有的 snapshot 采样点暂缺时返回 `503 READ_MODEL_UNAVAILABLE`。Heatmap 能力未开放或处于维护 / 恢复窗口时返回 `503 SERVICE_DISABLED`。两者的重试方式不同，见[错误处理](/errors)与[数据新鲜度](/api/data-freshness)。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.