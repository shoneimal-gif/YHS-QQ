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
- 消息列出标的、期权合约、Call/Put、到期日、行权价、买卖方向、张数、价格与金额。限价预估金额与成交金额分别标明。
- 买入为限价开仓，卖出为市价平仓；消息注明订单类型和程序观察时间。
- 卖出消息包含退出原因；完整平仓附已实现毛盈亏，未包含未知费用。
- 下单失败或被拒绝、取消或过期、提交结果未知均明确当前状态；未知结果仍需券商对账，不能视为成交。

消息标题标明 `YHS QQ｜Alpaca Paper`。不发送完整账户、原始券商响应或密钥。
没有发往券商的本地开仓意图不作为已下单通知。加载历史账本不会补发历史已平仓通知；恢复对账后看到的新成交可以通知。

## 推送时效和网络

推送按订单事件触发。程序收到并保存券商订单状态后，把通知交给后台队列，不在交易监控线程内等待 Discord 网络响应。
券商订单与报价对账目标为每 5 秒一次，网络、锁和请求延迟可能延后。YHS 策略仍按盘中 5/12 分钟轮询，券商报价轮询不等于秒级策略信号。
Discord 队列串行发送，依据服务器返回的限流信息安排间隔；遇到 HTTP 429 时保留消息并按照重试时间等待。
运行程序的电脑或服务器需要能够访问 Discord。网络、排队和手机通知设置都会影响到达时间，不承诺手机秒级提醒。

## 同时配置多个通道

配置 Discord 不会自动关闭已有的企业微信或 pushplus 通道。多个通道配置有效时，同一订单事件会分别发送。
如果只需要 Discord，可手动移走本机旧通道配置，并清除相应环境变量，再在适当时段重启程序：

| 通道 | 本机配置文件 | 优先读取的环境变量 |
| --- | --- | --- |
| 企业微信 | `.private_config/wecom.json` | `YHS_WECOM_WEBHOOK` |
| pushplus 个人微信 | `.private_config/pushplus.json` | `YHS_PUSHPLUS_TOKEN` |
| Discord | `.private_config/discord.json` | `YHS_DISCORD_WEBHOOK` |

保留交易账本和各通道待发队列；通知配置脚本不会自动删除旧通道数据。

## 待发队列和迁移

待发事件保存在同一账户状态目录的 `discord_outbox.json`，不含 Webhook。请求失败保留事件并退避重试；重新启动且启用通道后继续待发通知。
同一事件会去重，但 Discord 已创建消息而响应丢失时，重试仍可能产生重复消息。
只有进程运行时才能主动通知；电脑休眠、关机或进程被强杀时不能发出新消息。

关闭 Discord 通道可移走本机 `.private_config/discord.json` 并确保未设置 `YHS_DISCORD_WEBHOOK`，下次启动生效。保留交易账本和待发队列。
Linux 云部署不会复制本机 Webhook；服务器到位后另行配置凭据及读取路径。

## 当前检查范围

本次实现仅作静态检查和语法解析，尚未使用真实 Webhook 发送消息，也未验证盘中模拟订单通知。
