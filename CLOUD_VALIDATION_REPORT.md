# YHS-QQ 云端离线验证报告

本次在最新 main `3c6b26805114e025b156307acb5be2643928a5dd` 的工作分支完成修复与离线验证。192 项测试全部通过，固定合成回放完成 78 个观察时刻、5 笔平仓、0 笔未平仓。没有调用真实行情/券商接口、提交或撤销真实/Paper 订单、发送真实通知；没有读取个人密钥、Webhook、私有配置或真实 runtime。

## 环境与源码

- 验证时间：2026-10-11T16:51:52+08:00（Asia/Shanghai）。
- 工作目录：`/workspace/YHS-QQ`；Python 3.12.14，Linux-6.18.44-x86_64-with-glibc2.41。
- 从已有 checkout 快进到 GitHub main，原工作目录无用户修改；未找到适用 AGENTS.md。
- 已读取 README.md、CLOUD_TASK.txt、VALIDATION.md、DISCORD_NOTIFICATIONS.md、DATA_FORMATS.md、ALPACA_PAPER_GUIDE.md、CLOUD_DEPLOY.md。
- 业务依赖仅标准库；没有额外安装依赖或启用后台服务。
- 修复后源码 SHA256：`80e24dd0ce51b1677f533f10492f4e770fb6c0fefafef3e71339d05930b80285`。逐文件摘要和精确测试统计见 CLOUD_VALIDATION_SUMMARY.json；摘要不包含报告本身或私有数据。

## 实际命令与结果

命令均在 `/workspace/YHS-QQ` 执行。日志重定向后明确保存并返回底层退出码。

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -X utf8 -m unittest discover -s tests -v`（修复前） | 72 项，70 通过、2 报错；日志 `/tmp/yhs-baseline-tests.log` | 1 |
| `PYTHONPATH=/tmp/yhs-offline-guard python3 -X utf8 -m unittest discover -s tests -v`（修复后） | 192 项全部通过，耗时 6.933 秒；日志 `/tmp/yhs-final-tests.log` | 0 |
| `PYTHONPATH=/tmp/yhs-offline-guard python3 -X utf8 main.py --replay --date 2026-10-09 --seed 7 --output-dir /tmp/yhs-qq-validation.ryTotN/output` | 78 时刻、5 平仓、API 配额 0 | 0 |
| `python3 /tmp/yhs_validate_replay.py` | JSON/CSV/账本笔数、盈亏、现金及五种退出原因一致 | 0 |
| `bash -n deploy/linux/install.sh` | shell 语法通过 | 0 |
| `systemd-analyze calendar --base-time='2026-01-01 00:00:00 UTC' 'Mon..Fri *-*-* 09:25:00 America/New_York'` | 冬季下一次 14:25 UTC，即美东 09:25 | 0 |
| 同一表达式，基准 `2026-07-01 00:00:00 UTC` | 夏季下一次 13:25 UTC，即美东 09:25 | 0 |
| `git diff --check` | 无空白错误 | 0 |

离线守卫是临时目录 `/tmp/yhs-offline-guard/sitecustomize.py` 中的 socket 连接拒绝器；测试仍使用标准 unittest runner、模拟 opener 和合成凭据。它不进入应用或部署包。上述 `/tmp` 日志、检查脚本和回放输出是当前验证实例的临时证据，不是下次任务中保证存在的文件。

| 测试模块 | 执行项数 | 通过 |
| --- | ---: | ---: |
| `test_alpaca_client` | 26 | 26 |
| `test_alpaca_trader` | 36 | 36 |
| `test_client` | 23 | 23 |
| `test_deploy` | 20 | 20 |
| `test_discord_notifications` | 26 | 26 |
| `test_main` | 12 | 12 |
| `test_main_paper` | 11 | 11 |
| `test_system` | 38 | 38 |

无失败、报错、跳过或预期失败；所有列出的测试已执行。针对性测试只用于定位，最终总数以完整套件为准。

## 失败定位与修复

1. **FakeTimer 接口缺失**：最新源码复现两个 `test_main` 的 `AttributeError`，均来自 `run_live` 的清理逻辑调用 `timer.is_alive()`。生产 Timer 接口正确；补齐受控替身的 `is_alive/join`，增加取消、join、超时和信号处理器恢复断言。保留原断言，没有删测试或降低校验。
2. **账本到通知队列之间崩溃丢事件**：旧 `_notification_events` 仅在内存。现在通知与交易状态一起保存为可兼容旧账本的 `pending_notifications`；只有 Discord 持久化入队返回 True 才清除。覆盖保存后崩溃、入队拒绝、入队后确认保存中断及同 ID 重放。
3. **订单响应校验与恢复**：在修改成交量、金额、时间、日收益前完整校验；未知/矛盾状态保留资金与订单 ID，不把未确认退出当成完整平仓。加载时拒绝损坏收益、负金额、非法取消状态、重复 ID 或非空账本缺失账户绑定，禁止静默重建预算或重新绑定。
4. **通知金额与状态**：区分委托限价预估、订单累计成交均价/金额、本次新增成交均价/金额、剩余仓位，以及已确认部分卖出与整笔完整平仓毛盈亏；取消、拒绝、未知保留已知限价/确认成交信息，未知金额不虚构。共享格式器位于历史 wecom_notifications.py，仅复用格式函数；主入口不加载微信凭据或启动微信线程。
5. **Discord 队列**：严格校验状态、原子替换并在 Linux 同步目录；按 FIFO 重试，遵守 429、桶重置、至少一秒间隔及双时钟等待。关闭立即停止新入队，超时保留待发消息，旧 HTTP 返回不覆盖新执行器队列。响应丢失仍可能重复消息，不能保证 exactly-once。
6. **Linux 部署**：可选 Discord 字段通过既有 root-only JSON/LoadCredential 传入，仅执行模式启用。隐藏配置支持添加/移除 Discord，轮换 API 保留已有合法配置；安装默认不启用服务，拒绝升级运行或已启用的实例。测试直接执行 manifest 校验和 runtime 原子导入块，已有非空账本逐字节保留，中断不发布半份状态，私有通知配置不打包。

Alpaca Paper 端点、10 万美元策略预算、1% 每笔目标、$1,000 单笔上限、50 张/10 个仓位/20 次日开仓及原风险参数文件均保持不变。客户端写请求没有自动重试；需要按 client_order_id 对账。

## CLOUD_TASK.txt A–I 验收

passed 指列明的离线检查范围；不是对应真实 API、服务或交易流程已经验证。

| 项目 | 状态 | 本次证据范围 |
| --- | --- | --- |
| A | passed | 离线 YHS 请求限频、额度、重试、鉴权与每日榜单 claim/缓存；不验证真实服务字段 |
| B | passed | 离线筛选、GEX/Flow、报价、过期/未来时间、疲劳去重及回放因果性 |
| C | passed | Paper 合成账户预算/数量、预留、未知订单占位、风险配置不可静默重置；风险参数文件未改 |
| D | passed | 固定 Paper 主机、拒绝未知/实盘路由、预览不提交与写请求不自动重试；合成 REST |
| E | passed | 合成部分/全部成交、取消/拒绝/未知、订单意图崩溃恢复、累计金额幂等、未确认退出不记完整平仓收益、持久化通知交接 |
| F | passed | 模拟 SIGTERM 取消/恢复处理器、半日市日历、休市不请求 YHS、健康/未结关停；未执行真实盘中退出 |
| G | passed | 同主机同安装账户锁、损坏/失去绑定的账本拒绝恢复、合成 runtime 原子迁移/中断恢复；跨主机不互斥 |
| H | unverified | 组件与 DST 检查通过；完整 systemd 安装/运行未验证 |
| I | passed | 合成 JSON/CSV/账本笔数、现金和盈亏一致；估值/Paper/费用边界、通知脱敏与只启用 Discord |

## 回放与未验证范围

输出目录：`/tmp/yhs-qq-validation.ryTotN/output`，包括 summary.json、trades_history.csv、replay_state.json、detector_state.json。5 个退出原因全部出现：HARD_STOP、BREAKEVEN_STOP、TRAILING_STOP、HARD_TAKE_PROFIT、SESSION_CLOSE。合成模型已实现盈亏 $392.08 仅用于状态机和算术验证，不是历史收益或 Paper 真实成交。

未验证：真实 YHS 字段/授权与 Alpaca 盘中端到端成交、断线恢复、完整交易日；真实 Discord 连通性与手机提醒；完整 Linux sudo 安装、LoadCredential 运行、systemd 服务启动/停止及 timer 触发；Windows/SSH 上传；全年交易所日历与盈利能力。已终结订单的迟到成交更正/trade-bust 和券商替换链不自动保证恢复，需人工核对，不能依此测试宣称全面支持。

同主机同安装锁不约束另一台机器或其他执行器。服务器尚未上线，模拟盘部署状态与策略盈利能力均未得到本次验证。旧 VALIDATION.md 的既有记录保留，不能作为本次真实网络操作证据。
