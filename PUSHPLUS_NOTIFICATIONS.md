# 个人微信模拟订单通知

本地 Alpaca Paper 的 `ExecutePaper` 模式支持通过 pushplus 公众号发送个人微信订单通知。
网页已登录或已关注公众号，仍需把 Token 配置到本机项目。`Check`、`Preview` 和离线回放不发送订单通知。

## 在本机配置

在 [pushplus 官网](https://www.pushplus.plus/push1.html) 扫码登录，关注「pushplus 推送加」公众号，完成实名认证，复制自己的 Token。
当前微信渠道要求发送者实名认证；未实名账号无法调用发送接口。见 [官方额度说明](https://pushplus.plus/doc/guide/use.html)。
Token 具有发送权限，不要贴到聊天、截图或 GitHub。

在当前项目的 PowerShell 执行：

```powershell
cd 'C:\Users\fuxia\Documents\ChatGPT\YHS QQ'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_pushplus.ps1
```

按提示粘贴 Token，输入会隐藏。脚本仅保存到本机 `.private_config/pushplus.json`，不会发送消息。
私有目录仅允许当前 Windows 用户和 SYSTEM 访问，并被 Git 忽略，不进入云部署包。
`YHS_PUSHPLUS_TOKEN` 环境变量优先于配置文件；若当前会话已设置，脚本会提示并保留原值。

明确需要一条测试消息时，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_pushplus.ps1 -TestPush
```

测试仅发送标明测试的通知，不提交交易订单。成功提示表示 pushplus 已受理，并显示流水号尾号；请到个人微信确认是否收到。
接口返回 `code=200` 是异步受理结果，不等于公众号已送达。响应丢失时，请先查看个人微信或 pushplus 消息记录，再决定是否重试。

## 启动模拟盘

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_alpaca_paper.ps1 -Mode ExecutePaper
```

启动日志应显示「pushplus 个人微信订单推送已启用」。已经运行的进程不会自动加载新配置，需在适当时段重新启动。
有持仓时停止程序会进入现有退出与对账流程；重新启动通知不能作为保持仓位的暂停操作。

## 具体操作通知

- 买入、卖出订单得到券商响应并通过归属校验后发送提交通知；提交仍可能未成交。
- 新增成交时区分部分成交、全部成交，显示本次新增张数、该订单累计成交张数、成交均价及该笔策略剩余仓位。
- 消息列出标的、期权合约、Call/Put、到期日、行权价、买卖方向、张数、价格与金额。限价预估金额与成交金额分别标明。
- 买入为限价开仓，卖出为市价平仓；消息注明订单类型和程序观察时间。
- 卖出消息包含退出原因；完整平仓附已实现毛盈亏，未包含未知费用。
- 下单失败或被拒绝、取消或过期、提交结果未知均明确当前状态；未知结果仍需券商对账，不能视为成交。

消息标题标明 `YHS QQ｜Alpaca Paper`。不发送完整账户、原始券商响应或密钥。
没有发往券商的本地开仓意图不作为已下单通知。加载历史账本不会补发历史已平仓通知；恢复对账后看到的新成交可以通知。

## 推送时效和额度

推送按订单事件触发。程序收到并保存券商订单状态后，把通知交给后台队列，不在交易监控线程内等待微信网络响应。
券商订单与报价对账目标为每 5 秒一次，网络、锁和请求延迟可能延后。YHS 策略仍按盘中 5/12 分钟轮询，券商报价轮询不等于秒级策略信号。
pushplus 队列串行发送，请求间隔至少 12.5 秒，公众号投递也有延迟，因此不承诺秒级到达。

当前实名免费账户的微信渠道额度为每日 200 次请求、每分钟 5 次；错误请求也会消耗额度。见 [官方额度说明](https://pushplus.plus/doc/guide/use.html)。
本项目保守限制自动请求每日 180 次，包含失败重试；同一 Token 的手动测试或其他程序请求不在此计数中，仍可能使官方额度超限。
达到本地日限额或识别到官方日限额后，待发通知保留至次日继续。Token、实名认证或账号身份失败会暂停自动重试，需修正配置并重新启动程序。

公众号默认消息需点击链接查看具体订单详情。在公众号发送「激活消息」后，后续 24 小时内的 5 条消息可直接显示正文，超过时间或条数需重新激活。见 [官方显示说明](https://pushplus.plus/doc/help/showmessage.html)。

## 待发队列和迁移

待发事件保存在同一账户状态目录的 `pushplus_outbox.json`，不含 Token。请求失败保留事件并退避重试；重新启动且启用通道后继续待发通知。
同一事件会去重，但接口已受理而响应丢失时，重试仍可能产生重复消息。受理成功后的微信投递失败需通过 pushplus 消息记录排查，不能用本地队列保证送达。
只有进程运行时才能主动通知；电脑休眠、关机或进程被强杀时不能发出新消息。

关闭此通道可移除本机 `.private_config/pushplus.json` 并确保未设置 `YHS_PUSHPLUS_TOKEN`，下次启动生效。保留交易账本和待发队列。
Linux 云部署不会复制本机 Webhook 或 Token；服务器到位后另行配置凭据及读取路径。

## 当前检查范围

本次实现仅作静态检查和语法解析，尚未使用真实 Token 发送消息，也未验证盘中模拟订单通知。
