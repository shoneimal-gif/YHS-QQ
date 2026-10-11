# 企业微信模拟订单通知

本地 Alpaca Paper 的 `ExecutePaper` 模式支持企业微信群机器人通知。
配置完成后，重新启动程序会自动读取本机 `.private_config/wecom.json`。`Check`、`Preview` 和离线回放不发送订单通知。

## 创建机器人

在企业微信 PC 端打开接收消息的企业微信群，进入聊天管理中的「消息推送」，添加「自定义消息推送」，复制 Webhook。
新版本的界面可能显示「群机器人」；接收位置是这个企业微信群。见 [腾讯官方创建说明](https://cloud.tencent.com/document/product/1759/128391)。
Webhook 含有发送权限，不要发到聊天、截图或 GitHub。

## 在本机保存

在当前项目的 PowerShell 执行：

```powershell
cd 'C:\Users\fuxia\Documents\ChatGPT\YHS QQ'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_wecom.ps1
```

提示时粘贴完整 Webhook，输入隐藏。脚本只保存配置，不发送消息。
私有配置目录限制当前 Windows 用户和 SYSTEM 访问，并被 Git 忽略，不进入云部署包。

明确需要测试推送时运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_wecom.ps1 -TestPush
```

这会向机器人所在群发送一条标明测试的消息，不提交任何交易订单。

## 运行交易程序

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_alpaca_paper.ps1 -Mode ExecutePaper
```

启动日志应出现「企业微信订单推送已启用」。已经运行的进程不会自动加载新配置，需在适当时段重新启动。
有持仓时停止程序会进入现有退出与对账流程，重新启动通知不要被当成保持仓位的暂停操作。

## 会收到什么

- 买入或卖出订单得到券商响应并通过归属校验：提交通知，仍可能未成交。
- 新增成交：区分部分成交与全部成交，注明本次订单累计成交张数和每股成交均价。
- 完整平仓：成交消息附退出原因与已实现毛盈亏，未包含未知费用。
- 提交失败/拒绝、确认取消/过期、提交结果未知：明确当前状态；未知结果仍需对账，不视为已成交。

消息包含 `YHS QQ｜Alpaca Paper`，不会发送完整账户、原始券商响应或密钥。
未向券商发送的本地开仓意图不作为已下单通知。历史账本加载不生成历史平仓通知；恢复后看到的新成交可以通知。

通知在交易账本成功保存后交给后台持久队列，网络请求不在交易监控线程执行。
发送间隔至少 3.2 秒，短请求超时；失败保留待发消息并退避重试。队列保存在同一账户状态目录的 `wecom_outbox.json`，不含 Webhook。
同一事件去重，但接口已接收而响应丢失时，重试仍可能产生重复；消息不是绝不丢失的交付保证。
电脑休眠、关机、进程被强杀时，程序不能主动通知；待发内容在之后重新启动并启用通知时继续尝试。

关闭通知可以移除本机 `.private_config/wecom.json` 并确保未设置 `YHS_WECOM_WEBHOOK`，下次启动生效。保留交易账本和待发队列。
Linux 云端迁移不会复制本机 Webhook；服务器到位后另行配置凭据及读取路径。

## 当前验证范围

本次实现仅作静态检查及语法解析，尚未用真实 Webhook 发送消息，也未验证盘中模拟订单通知。
