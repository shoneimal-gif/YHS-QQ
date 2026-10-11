> ## Documentation Index
> Fetch the complete documentation index at: https://docs.yehangshe.com/llms.txt
> Use this file to discover all available pages before exploring further.

# 鉴权与 API Key

> Key 的生成、使用、撤销与安全实践

## 鉴权方式

所有 `/v1/*` 端点（除 `health` 与 `openapi.json` 外）使用 Bearer 鉴权：

```
Authorization: Bearer sk_live_xxxxxxxx
```

Key 以 `sk_live_` 为前缀。服务端只存储 Key 的哈希，明文只在生成瞬间展示一次。

## Key 管理

在[账户页](https://yehangshe.com/app/account)的「API 访问」分区自助管理：

| 操作 | 规则 |
| - | - |
| 生成 | label 必填、不超过 64 字符；明文仅展示一次 |
| 持有上限 | 每账户最多 **5 个 active Key** |
| 撤销 | 即时生效，被撤销的 Key 立刻不可用，不可恢复 |

建议按用途拆分 Key（例如 `my-bot`、`research-notebook` 各一个）：泄露时可以只撤销受影响的那个，而不打断其他程序。

## Key 与订阅状态

* API 访问权随订阅实时判定：订阅失效期间，请求返回 `402 AUTH_PAUSED`，Key 本身不会被删除。
* 恢复订阅后，同一个 Key 自动恢复可用，无需重新生成。

## 鉴权相关错误

| 状态码 | code | 含义 |
| - | - | - |
| 401 | `AUTH_MISSING` | 没有携带 `Authorization` header |
| 401 | `AUTH_INVALID` | Key 无效或已撤销 |
| 402 | `AUTH_PAUSED` | 订阅已失效，恢复订阅即恢复访问 |
| 403 | `SCOPE_INSUFFICIENT` | Key 缺少该端点所需 scope |

以上错误均**不消耗**月配额。完整错误码表见[错误处理](/errors)。

## 安全实践

* **不要**把 Key 写进前端代码、浏览器脚本或公开仓库。API Key 设计用于服务端与本地程序；浏览器场景请直接使用产品页面。
* 用环境变量或密钥管理工具注入 Key，不要硬编码。
* 怀疑泄露时：立即在账户页撤销该 Key，再生成新的。撤销即时生效。
* MCP 使用同一个 Key（配置在你的 AI 工具本地），安全属性与服务端使用一致；注意不要把含 Key 的 MCP 配置文件提交进仓库。


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.