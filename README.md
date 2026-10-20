# Dennis AlphaAgent → Alpaca Paper

当前开发主线：读取 Dennis 原频道的已验证 AlphaAgent 期权信号，交由独立跟单程序执行
Alpaca **模拟盘**，并将结果推送 Discord。旧 YHS QQ 策略代码保留。

## 当前代码

| 部分 | 路径 | 职责 |
| --- | --- | --- |
| 信号接收 | `alpha_discord/personal_observer.py` | 来源校验、CREATE/UPDATE/DELETE、持久化 |
| 来源适配 | `alpha_discord/personal_source.py` | 读取原始证据，处理修改、撤回和验证状态 |
| 模拟跟单 | `alpha_discord/copier.py` | 限价、时效、整张预算、订单对账与成交归属 |
| 共用账户 | `shared_account.py` | 账户锁、资金预留、未知订单拦截 |
| 通知 | `stock_quant/notifications.py` | 持久化 Discord 通知队列 |
| 信号诊断 | `alpha_discord/diagnostics.py` | 只读静态快照，区分来源状态与执行证据 |

已同步消息校验修复：对经过验证且只改变 Embed 传输字段的 UPDATE 保留原信号，
真实交易内容变化、删除、来源不一致或验证待定仍受原有约束。

OPEN 信号最多 30 秒、买价最多比 Entry 高 3%；单笔目标 $4,000，允许
$3,000–$5,000（含每笔 $2 预留），期权在仓及委托预留总额最多 $10,000。
CLOSE 只处理本程序确认拥有的同合约、同层级数量。

## 新增执行优化

新单可显式启用3%封顶限价、同轮共享账户锁内的短时对账复用，以及等待阶段的
本地消息变化唤醒。原有预算、时效、来源和未知订单约束继续适用；旧委托保持原规则。
配置缺省保持旧行为，详见 [LATENCY_UPDATE.md](LATENCY_UPDATE.md)。

## GitHub / Codex 云端开发

阅读 [开发说明](DEVELOPMENT.md)、[云端任务](CLOUD_TASK.txt) 和 [仓库规则](AGENTS.md)。
选择包含当前代码的开发分支，让云端完成小范围修改并提交 PR。
云端开发环境与运行交易的服务器是两个独立环境；GitHub 更新不会自动更新服务器。

仓库不包含密钥、私有账户绑定、个人频道绑定、真实消息或账本。示例配置默认关闭
执行，且缺少必须的私有授权，不能直接启动下单。

## 只读诊断

Python 3.11+，诊断模块仅使用标准库。对运维另外提供的一致静态快照运行：

```sh
python -B -m alpha_discord.diagnostics --observer-runtime /path/to/observer-snapshot --copier-runtime /path/to/copier-snapshot --limit 20
```

输出区分源消息状态、跟单决策、提交证据及已记录成交；源后续失效不会抹掉已有成交。
缺失数据库会返回不可用，不推断为未下单。非空 WAL 会返回
`active_wal_requires_consistent_snapshot`；本工具不制作备份、不 checkpoint、不修改账本。
快照不是实时券商核对，不含原始消息内容，也不会联网或发送通知。

## 运行依赖与旧代码

信号接收使用隔离环境的 `alpha_discord/requirements.personal.txt`，不要与
`discord.py` 混装。跟单核心使用标准库，Discord 通知需要另配 `requests`；
依赖锁定及安装属于独立部署流程，本次没有安装或联网验证。
Linux service 文件是模板，必须先补齐私有绑定、运行目录和独立环境后审阅部署。

`main.py`、YHS detector、旧启动脚本和 `deploy/` 主要对应保留的历史系统；
其部署、回放及旧验证报告不适用于当前 Dennis 跟单链路。不要用旧部署包覆盖运行中的服务。
