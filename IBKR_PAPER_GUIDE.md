# AlphaAgent → IBKR 模拟盘自动跟单

这套独立程序读取已验证的 Dennis AlphaAgent 信号，在绑定的 **IBKR Paper Trading 模拟账户**提交期权限价单，记录券商委托和成交，并可发送 Discord 通知。现有实盘只读程序、Alpaca Paper 服务及其账本保持独立；本入口不接管其他程序或手工建立的持仓。

## 1. 先核对 Gateway 登录

在 IB Gateway 选择 **IB API → Paper Trading**，自己完成账号登录和手机验证。用户名、密码和验证码只填在官方客户端。

打开 Gateway 的 **Configure → Settings → API → Settings**，确认 Socket API 已开启、仅允许本机连接。Gateway 模拟端口通常为 **4002**；TWS 模拟盘通常为 **7497**。端口可以配置，所以端口本身不能证明账户属于模拟盘；本程序还必须核对返回的唯一 `DU` 模拟账户及其精确 SHA-256 绑定。

预检期间保留 **Read-Only API**。完成模拟身份核对后，如要启用模拟下单，才在仍登录 **Paper Trading** 的客户端中取消 **Read-Only API**。程序不会替你修改客户端设置。实盘只读接入仍使用原配置和入口。

官方参考：[IB Gateway](https://www.interactivebrokers.com/docs/tws-api/doc/architecture/the-trader-workstation/the-ib-gateway)、[TWS API 配置](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/)。

## 2. 保存独立配置

在项目文件夹的 PowerShell 中运行。若先前已配置好同一云端信号来源，可明确复用其连接位置：

```powershell
.\configure_ibkr_paper.ps1 -ReuseExistingSource -ConfirmPaperLogin -EnableExecution -EnableDiscord
```

这一步只保存 `.private_config/ibkr_paper.json`，不会连接 IBKR、启动交易、发送通知或修改 SSH。复用时只复制现有 `ibkr_bridge.json` 的来源服务器地址、用户名、端口以及已有 SSH 私钥和已核实主机指纹文件的路径；不会复制实盘端口、账户绑定或执行配置。

没有已有来源配置时，改用：

```powershell
.\configure_ibkr_paper.ps1 -ConfigureSource -ConfirmPaperLogin -EnableExecution -EnableDiscord
```

按提示填写已有连接资料，不要粘贴任何私钥内容。SSH 文件、权限和主机校验不改变。通知复用自己现有的 Discord 私有配置；默认示例关闭执行、通知，且没有账户或来源绑定。

如已存在模拟配置，脚本默认拒绝覆盖。只有**尚未创建模拟账本**且明确需要重建配置时，才可加 `-ReplaceExisting` 并重新绑定。已有 `runtime/ibkr_paper/ledger.sqlite3` 时，脚本禁止重建；请保留原配置、账户绑定、去重和委托记录。更换账户或资金策略需要单独迁移，不能删除账本后重绑。

## 3. 安装独立环境并预检

```powershell
.\run_ibkr_paper.ps1 -InstallDependencies -Preflight
```

只在显式指定 `-InstallDependencies` 时创建 `.private_deploy/ibkr_paper_venv` 并下载固定依赖；普通启动不安装或升级软件。预检只读连接模拟账户，显示遮盖的账号和账户摘要，不提交订单。未绑定或识别到其他账户时不会启用执行。

## 4. 核对并绑定模拟账户

先在 Gateway 核对 **Paper Trading** 登录，再将预检显示的唯一 `DU` 账户摘要填入下面命令：

```powershell
.\run_ibkr_paper.ps1 -BindAccountSha256 '这里填预检显示的64位摘要'
```

这一步只保存精确账户绑定；预检和观察不会激活交易账本。首次用 `-ExecutePaper` 启动执行器时，系统创建独立账本并保存信号生效时间；后续重启继续沿用，不会把以前的信号当成新信号追买。已有账本时不允许直接换绑其他账户，不能共用旧账户的持仓归属和资金预留记录。

## 5. 观察后开始模拟跟单

不提交订单地查看来源与账户状态：

```powershell
.\run_ibkr_paper.ps1 -ObserveOnly -Once
```

核对模拟账户后，在 **Paper Trading** 的 Gateway 中取消 **Read-Only API**，随后显式启动：

```powershell
.\run_ibkr_paper.ps1 -ExecutePaper
```

配置中的 `execute_paper`、模拟登录确认、精确账户绑定，以及启动参数 `-ExecutePaper` 必须同时满足。仅登录 Paper Trading 或仅保存配置不会启动交易。无执行参数时，入口默认只读观察。

程序需要本地电脑、Gateway、网络持续在线。默认等待 1 秒，再加 SSH 握手、来源验证和 IBKR 请求耗时；这是轮询设置，不是 1 秒内成交保证。按 **Ctrl+C** 会停止本地程序；已有券商委托仍需按券商实际状态核对。

## 已保留的规则

| 项目 | 第一版规则 |
| --- | --- |
| 来源 | 只接受已验证的 Dennis 原频道 AlphaAgent，新 OPEN 最多 30 秒，CLOSE 最多 120 秒。 |
| 买价 | 最多为 Entry 上方 3%，按合约合法价格步长向下取整，使用限价单。 |
| 单笔 | 目标 $4,000，允许 $3,000–$5,000，计入 $2 预留；整张不满足范围则跳过，最多 50 张。 |
| 总预算 | 本程序期权持仓成本与未决委托预留合计最多 $10,000。 |
| 平仓 | 仅处理本程序已确认成交并唯一归属的相同合约、相同层级数量；不推断手工或其他程序持仓属于本信号。 |
| 失效 | 修改、撤回、来源不明或等待核查时不增加新仓，保留失效和去重证据。 |
| 委托未知 | 保存原委托身份及资金预留，查询原订单和成交；不会因为超时自动重新下同一单。 |
| 开仓超时 | 30 秒后按原委托核对并申请取消；取消请求和取消回执都不能替代成交核对。 |

委托被接受、通知到达、价格触及限价，都不等于成交。Discord 会区分信号、委托状态和已确认成交。行情权限缺失、报价不完整或非实时数据时，不把模拟值当作当前可成交价格。

## 文件与运行范围

- 私有配置：`.private_config/ibkr_paper.json`。
- 独立账本及通知记录：`runtime/ibkr_paper/`，不得删除以重新触发历史信号。
- 执行程序仅支持本机模拟端口和绑定的 `DU` 账户；实盘只读入口仍是 `run_ibkr_readonly.ps1`。
- GitHub 只同步源码和脱敏示例，不同步账号摘要、密钥、SSH 文件、真实消息、订单或运行账本。运行代码更新不等于自动部署或启动。
