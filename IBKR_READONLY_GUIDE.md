# AlphaAgent 接入 IBKR：本地只读预览

这一版接收已配置的 Dennis AlphaAgent 信号，在自己的 IBKR 实盘账户里核对期权合约、持仓和报价，生成交易预览。程序不提交、修改或撤销订单。实际交易由你在 IBKR 客户端确认。

当前的 Alpaca 模拟盘服务保持原状。这份接入配置不会移动已有持仓，也不会停止已有服务。

## 1. 先安装并登录电脑客户端

手机 App 或网页不能直接提供这一版程序使用的本地 Socket API。请在运行程序的 Windows 电脑安装官方 [Trader Workstation（TWS）](https://www.interactivebrokers.com/en/trading/download-tws.php)，登录你的实盘账户，自己完成二次验证。用户名、密码和验证码都只填在 IBKR 官方客户端里。

本程序也兼容已经安装的 IB Gateway。默认端口是 TWS 实盘 `7496`；IB Gateway 实盘通常使用 `4001`。程序仅允许连接本机 `127.0.0.1`，请不要开放公网 API 端口。

## 2. 在 TWS 开启只读 API

在 TWS 中打开 **Global Configuration → API → Settings**：

1. 勾选 **Enable ActiveX and Socket Clients**。
2. **保持勾选 Read-Only API**。
3. 确认 **Socket port = 7496**。
4. 仅允许本机连接；不要添加外部 IP。
5. 保存设置，并让 TWS 保持登录。

使用 IB Gateway 时相应端口改为 `4001`。如 TWS 弹出 API 连接询问，核对是本机程序后再允许。

官方参考：[安装与配置 TWS API](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/)。

## 3. 配置与安装本地依赖

在项目文件夹打开 PowerShell。首次配置运行：

```powershell
.\configure_ibkr_readonly.ps1
```

配置保存在 `.private_config/ibkr_bridge.json`，不会要求输入 IBKR 密码或 Discord Token。它只记录本机端口、账户绑定以及已有信号服务器的连接位置。需要读取现有云端信号时，使用你已有的 SSH 私钥和已核实的主机指纹文件；脚本不会修改它们。

上述默认配置可先做 TWS 连接检查。配置现有云端信号位置时使用：

```powershell
.\configure_ibkr_readonly.ps1 -ConfigureSource
```

如果前一步已生成配置，加 `-ReplaceExisting` 表示明确重建配置，并在随后重新核对和绑定账户。脚本仅要求填写已有服务器地址、SSH 用户名、私钥文件路径及已核实的主机指纹文件路径。不要创建或更换 SSH 密钥、不要关闭主机校验。

需要把只读预览发到自己的原 Discord 通知频道，可在配置命令追加 `-EnableDiscord`。它复用既有私有通知配置，不需要把 Webhook 或 Token 复制进新文件；默认关闭。

首次安装依赖时，明确执行：

```powershell
.\run_ibkr_readonly.ps1 -InstallDependencies -Preflight
```

这一步会创建独立的 `.private_deploy/ibkr_readonly_venv`，并从 Python 包源下载依赖。后续普通启动不会自动安装或升级软件。找不到 Python 时可加 `-PythonExe "C:\完整路径\python.exe"` 指定 Python。

## 4. 核对并绑定账户

连接检查会显示遮盖后的账户名称及账户 SHA-256 摘要。核对它确实是你准备观察的账户，复制该账户对应的摘要，在本机运行：

```powershell
.\run_ibkr_readonly.ps1 -BindAccountSha256 '这里填刚显示的64位摘要'
```

完整账户号码不会写入公开示例。账户未绑定前，可做连接检查或单独观察信号；IBKR 账户数据和报价预览不会启用。

`7496` 或 `4001` 只表示你选择的端口，不能证明登录的是实盘。请在 TWS 自己核对实盘登录、账户和 Read-Only API 设置后再绑定。

## 5. 开始观察

```powershell
.\run_ibkr_readonly.ps1
```

只运行一轮后退出：

```powershell
.\run_ibkr_readonly.ps1 -Once
```

尚未登录 TWS 时，可先单独查看已有云端接收器的信号状态：

```powershell
.\run_ibkr_readonly.ps1 -SourceOnly -Once
```

这个模式不连接 IBKR，不生成 IBKR 报价预览；仍需要先配置已有云端信号位置。首次启动前的信号可以列作历史观察，不会触发新预览或通知。

按 **Ctrl+C** 退出。TWS、电脑和网络都需要保持运行。重新登录或断线后，等待账户与行情重新确认；程序不会补买过期信号。

信号状态通过已有 SSH 连接资料读取，每轮会建立一次 SSH 会话。默认等待间隔为 1 秒，还要加上握手、远端读取和 IBKR 查询耗时；它不代表 1 秒内一定能跟上信号，也没有成交时效保证。

## 预览的含义

- 只处理配置中绑定的来源，展示源消息时间、信号时效、合约、持仓与报价。
- 买入参考上限为源信号 `Entry × 1.03`；价格按合约最小变动单位向下取整。超过 30 秒的 OPEN 仅供查看。
- `quantity` 默认是 `null`，由你决定实盘数量；模拟盘每笔 $3,000–$5,000 的预算不会自动套用。
- 价格与持仓只代表读取时刻。缺失、延迟或没有行情权限时会标明，不能把旧价或模拟值当作可成交价格。
- 所选账户的本地记录约每 30 秒刷新，明细仅保存在本机 `runtime/ibkr_readonly/account_latest.json`，不发到 Discord。余额来自 IBKR 的账户订阅，后续通常只推送变化字段，更新间隔可能达 3 分钟；本地刷新不会把旧余额变成新的券商快照。记录中区分字段接收时间和持仓快照时间。匹配到同一期权持仓也不表示程序已接管或确认它属于某条源信号。
- 有交易预览不代表已委托或成交。此程序没有发送订单的功能；需要你在 IBKR 自己完成买卖。

## 常见情况

| 提示或现象 | 处理 |
| --- | --- |
| 无法连接本机 API | 确认 TWS 已登录、Socket API 已开启、配置端口与客户端一致。 |
| 账户未绑定 | 先运行 `-Preflight`，核对账户后用 `-BindAccountSha256` 绑定。 |
| 期权没有实时买卖价 | 在 IBKR 确认相应市场行情权限；界面标为延迟的数据不能当作实时数据。 |
| 看不到新信号 | 先确认现有云端接收程序仍在线，再核对私有配置的信号来源。 |
| 提示依赖未安装 | 首次明确运行 `-InstallDependencies -Preflight`；普通启动不会自行下载。 |

开发者：本地配置、账户摘要、私钥、主机指纹文件和运行数据不得提交到 GitHub。不要关闭 TWS 的 Read-Only API，也不要给这一版增加自动实盘下单开关。
