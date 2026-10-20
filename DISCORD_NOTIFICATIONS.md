# Discord 模拟订单通知

本地 Alpaca Paper 的 `ExecutePaper` 模式支持通过 Discord Webhook 发送订单通知。
已经创建 `#yhs-paper` 文字频道后，还需创建指向该频道的 Webhook，并保存到本机项目。
`Check`、`Preview` 和离线回放不发送订单通知。

## 创建频道 Webhook

1. 打开所在服务器的 **Server Settings（服务器设置）→ Integrations（集成）→ Webhooks**。
2. 选择 **Create Webhook（创建 Webhook）**，名称可以填 `YHS Paper`。
3. 在频道下拉选项中选择 `#yhs-paper`，保存，点击 **Copy Webhook URL（复制 Webhook URL）**。

上述入口及频道选择见 [Discord 官方 Webhook 说明](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)。
Webhook URL 具有向该频道发送消息的权限，不要贴到聊天、截图或 GitHub。

## 在本机配置

在当前项目的 PowerShell 执行：

```powershell
cd 'C:\Users\fuxia\Documents\ChatGPT\YHS QQ'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_discord.ps1
```

按提示粘贴完整 Webhook URL，输入会隐藏。脚本仅保存到本机 `.private_config/discord.json`，不会发送消息。
私有目录仅允许当前 Windows 用户和 SYSTEM 访问，并被 Git 忽略，不进入云部署包。
`YHS_DISCORD_WEBHOOK` 环境变量优先于配置文件；若当前会话已设置，脚本会提示，保存后恢复原环境值。
环境变量仍指向旧 Webhook 时，后续启动或测试会继续使用旧值；请按需要清除或更新该环境变量。

明确需要一条测试消息时，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_discord.ps1 -TestPush
```

测试仅发送标明测试的通知，不提交交易订单。成功提示表示 Discord 已确认创建频道消息，并显示消息 ID 尾号；请查看 `#yhs-paper`。
频道消息创建成功不代表手机已经弹出通知。手机需要安装并登录 Discord，并按需要开启该频道及系统通知。
响应丢失时，请先查看频道，再决定是否重试，避免重复测试消息。

## 启动模拟盘

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_alpaca_paper.ps1 -Mode ExecutePaper
```

启动日志应显示「Discord订单推送已启用」。已经运行的进程不会自动加载新配置，需在适当时段重新启动。
有持仓时停止程序会进入现有退出与对账流程。不要为了加载通知强制结束进程；重启通知不是保持仓位的暂停操作。

## 具体操作通知

- 买入、卖出订单得到券商响应并通过归属校验后发送提交通知；提交仍可能未成交。
- 新增成交时区分部分成交、全部成交，显示本次新增张数、该订单累计成交张数、成交均价及该笔策略剩余仓位。
- 消息列出标的、期权合约、Call/Put、到期日、行权价、买卖方向、张数、价格与金额。限价预估金额、本次新增成交价格/金额、订单累计成交均价/金额分别标明。
- 买入为限价开仓，卖出为市价平仓；消息注明订单类型和程序观察时间。
- 卖出消息包含退出原因；部分平仓显示本次确认的已实现毛盈亏及累计部分平仓毛盈亏，仅分摊已成交张数的入场成本。
  完整平仓附整笔已实现毛盈亏，未包含未知费用；剩余仓位和未确认平仓不计入已实现收益。
- 下单失败或被拒绝、取消或过期、提交结果未知均明确当前状态；未知结果仍需券商对账，不能视为成交。
  终止或未知订单存在已确认部分成交时，消息分别列出原委托限价、确认成交均价与金额，避免把成交均价称作限价。

消息标题标明 `YHS QQ｜Alpaca Paper`。不发送完整账户、原始券商响应或密钥。
没有发往券商的本地开仓意图不作为已下单通知。加载历史账本不会补发历史已平仓通知；恢复对账后看到的新成交可以通知。

## 推送时效和网络

推送按订单事件触发。程序收到并保存券商订单状态后，把通知交给后台队列，不在交易监控线程内等待 Discord 网络响应。
券商订单与报价对账目标为每 5 秒一次，网络、锁和请求延迟可能延后。YHS 策略仍按盘中 5/12 分钟轮询，券商报价轮询不等于秒级策略信号。
Discord 队列按事件入队顺序串行发送，提交、部分成交与最终状态不会因断网重试被后续事件越过。
请求间隔至少 1 秒，并依据服务器返回的限流信息安排间隔；遇到 HTTP 429 时保留消息并按照最长有效重试时间等待。
当前进程使用单调时钟计算等待时间，校准系统时钟不会提前结束限流；绝对重试时间同时保存供重启恢复。
运行程序的电脑或服务器需要能够访问 Discord。网络、排队和手机通知设置都会影响到达时间，不承诺手机秒级提醒。

## 当前仅使用 Discord

当前 `ExecutePaper` 入口只加载 Discord，不读取或重新启用企业微信与 pushplus 的私有配置或环境变量。
历史通道模块和文档仍保留在源码中，升级不会删除其配置、交易账本或待发队列。
本机停用微信的私有配置不上传 GitHub，也不复制到云服务器。云端是否配置通知凭据须在服务器上单独确认。

## 待发队列和迁移

待发事件保存在同一账户状态目录的 `discord_outbox.json`，不含 Webhook。队列原子替换，并在 Linux 同步文件及目录。
订单账本先保存 `pending_notifications`，再尝试通知入队；只有队列接受且持久化后，账本才移除这条待交付事件。
崩溃发生在这两个保存步骤之间时，重启会用相同事件 ID 再次交付，已入队或已发送的事件由 Discord 队列去重。
请求失败保留事件并以 30、60、120、240、480、600 秒上限退避重试；HTTP 429 使用服务器给出的重试时间。
HTTP 401/403/404 等配置或权限错误暂停发送并保留队列；修正凭据、重新启动后继续待发通知。
队列损坏或保存失败时停止通知发送，保留现有文件并记录不含凭据的诊断；不会删除队列、重置账本或把失败的入队视为成功。
同一事件会去重，但 Discord 已创建消息而响应丢失时，重试仍可能产生重复消息。
成功发送须得到 `wait=true` 返回的消息 ID、匹配的 Webhook ID 和有效频道 ID；HTTP 204 或无效响应不视为已确认创建。
关闭时立即停止接收新事件，在有限时间内尝试发送已持久化事件；超时仍保留待发工作。关闭期间尚未完成的旧 HTTP 响应不会覆盖重启进程的新队列。
只有进程运行时才能主动通知；电脑休眠、关机或进程被强杀时不能发出新消息。

关闭 Discord 通道可移走本机 `.private_config/discord.json` 并确保未设置 `YHS_DISCORD_WEBHOOK`，下次启动生效。保留交易账本和待发队列。
Linux 云部署不会复制本机 Webhook；服务器需通过 `deploy/linux/configure_keys.py --discord` 单独配置。
runner 从 systemd 的 `LoadCredential` 副本读取 Webhook，仅向执行模式注入 `YHS_DISCORD_WEBHOOK`；未配置时保持通知关闭。
安装默认不启动自动下单，完整凭据与操作说明见 [CLOUD_DEPLOY.md](CLOUD_DEPLOY.md)。

## 当前检查范围

2026-10-11 的云端离线验证实际执行：

```bash
python3 -X utf8 -m unittest discover -s tests -p test_discord_notifications.py -v
```

26 项 HTTP 与队列测试通过，全部使用合成 Webhook、临时目录、模拟响应及模拟时钟。
覆盖收据校验、重定向拒绝、429/4xx/5xx、断网重试、队列损坏、磁盘保存失败、重启去重、限流和关闭期间响应。
没有读取个人配置、发送真实通知或提交券商订单；真实 Discord 连通性、手机提醒及盘中 Paper 订单仍未验证。
完整离线检查及回放结果见 [CLOUD_VALIDATION_REPORT.md](CLOUD_VALIDATION_REPORT.md)。
