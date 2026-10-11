# 把新版 YHS 机器人接到 Alpaca 模拟账户

## 当前完成情况

旧机器人的实际目录为 `C:\Users\fuxia\quant_bot`，其 `.env` 已用于只读连接。
新版使用相同的 `ALPACA_API_KEY` 和 `ALPACA_SECRET_KEY`，以标准库 REST 接口接入，旧机器人的股票调仓代码无需安装到新版。
旧目录没有被修改，密钥没有复制进交付文件。

2026-10-10 北京时间 21:42 的只读查询结果：

| 项目 | 查询结果 |
| --- | ---: |
| 账户类型 | Alpaca Paper |
| 净值 | $100,583.31 |
| 现金 | $71,042.11 |
| 期权购买力 | $85,812.71 |
| 期权交易等级 | Level 3 |
| 未完成订单 | 0 |
| 已有股票 | SPY 13 股、QQQ 13 股、SMH 16 股 |

详情见 [ALPACA_CONNECTION_CHECK.json](ALPACA_CONNECTION_CHECK.json)。这是查询时点的结果，不是实时余额。
本次没有提交、撤销或修改订单。没有启动常驻机器人或部署云端服务。
当前进程没有 YHS API Key，尚未完成真实 YHS 数据到 Paper 成交的整条流水线联调。
本次新增代码完成静态审阅和上述只读账户连接，尚未验证盘中订单成交、断线恢复或完整交易日表现。

## $100,000、每笔约 1% 的设置

| 控制项 | 默认设置 |
| --- | --- |
| 策略预算基数 | $100,000 |
| 每笔目标权利金 | `min(100000, 当前账户净值) × 1%`，绝对上限 $1,000 |
| 合约张数 | 按限价和费用预留取整，每笔最多 50 张；买不起一张则跳过 |
| 同时持仓 | 最多 10 笔，总权利金及预留最多 $10,000 |
| 当日开仓 | 最多 20 次尝试；未成交和拒绝尝试也计数，挂单占用预算 |
| 当日亏损熔断 | 已实现与有效盯市合计亏损达到 $3,000，停止新增并请求退出 |
| 本轮累计亏损熔断 | $10,000，停止新增并请求退出 |
| 标的 | 原 YHS Top100 清洗池；股票须有已知资产类型，ETF 只允许 SPY / QQQ / TLT / SOXL / TQQQ |
| 入场窗口 | 09:45–11:30、14:30–15:00 ET |
| 到期日 | 距今 5–10 个日历日 |
| Delta 绝对值 | 0.30–0.55，必须有有效行情字段 |
| 买卖价差 | 同时不超过中间价的 5% 和每股 $0.05 |
| 报价新鲜度 | 不超过 30 秒 |
| 平仓开始时间 | 正常 15:45 ET；半日市为实际收盘前 15 分钟 |

1% 按开仓权利金投入计算；风险基数随账户净值下降，上涨时仍以 $100,000 为上限。
例如每股权利金 $3.00，一张通常对应 100 股；约 $1,000 的预算可买 3 张，剩余金额留作现金。
整张合约、价格跳动、50 张上限、价差、购买力和其他限制都会使实际金额低于目标。
例如每股权利金 $0.25，单笔预算扣除 $2 费用预留后可买 39 张；每股 $3.00 则仍是 3 张。
费用预留默认每次订单 $1，是预算估计，成交盈亏不假定券商已经收取这一费用。

退出保持你的原始规则：−35% 止损；观察到峰值 +35% 后抬至成本线；峰值 +60% 后按期权价值从峰值回撤 20% 追踪；+150% 请求止盈。
止损和 $3,000 熔断是触发线，订单延迟、报价跳空、断线或未成交会导致实际亏损超过阈值。

本轮按你的要求增加模拟采样容量：$100,000 是预算基数，同时用于期权的金额最多约 10%。
每个标的最多一个持仓，仍保留信号去重、冷却、节点疲劳和报价检查。10 笔持仓与每日 20 次是上限，实际数量取决于有效信号和可用报价。
`allowed_symbols: null` 表示使用 YHS 清洗后的整个池；如填入代码列表，则进一步限制该池。
配置已经补充 AAPL、MSFT、NVDA、TSLA、AMD、AMZN、META、GOOGL、AVGO、NFLX 的股票分类，用 [Nasdaq 官方证券目录](https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt) 核对为股票且 ETF 标志为 N。
这只补充资产分类；每个代码仍须实际出现在当日 Top100 并通过价格、量能和后续信号筛选。其他缺失资产类型的标的仍会被剔除，需补充已核对的 `symbol_types`。
本版尚未生成策略执行账本，可以直接使用新设置；已建立的账户账本会检测风险配置摘要变化并拒绝自动重置，更新已有实验必须保留历史和熔断状态。
这些参数是可复盘的初始配置，未证明策略能够盈利。

## 怎样评估模拟结果

同一笔交易中的多张合约记为一个交易样本。优先累计不同标的、不同交易日的有效信号，再比较分组表现。
每天保存 `trades_history.csv` 与 `summary.json`，报告包括：

- 实际平仓胜率、平均每笔已实现盈亏、平均已实现 ROI。
- 盈利因子：已平仓盈利金额合计 / 已平仓亏损金额绝对值合计；无亏损样本时显示无可用分母。
- 已平仓交易权益曲线最大回撤，明确排除未平仓浮亏；另报当前未实现盈亏和报价过期状态。
- 按标的及 Call / Put 分组的交易笔数与表现，以及原来的方向正确率、Peak MFE。
- 当前已成交未平仓合约张数、仍未成交的买入张数、挂单和未知订单数量。

成交盈亏来自 Alpaca Paper 的确认成交，未包含未知费用。方向正确率的 Peak MFE≥15% 定义与最终平仓胜率分别统计。
交易多张数扩大同一信号的盈亏金额；更多有效信号才增加可观察的交易样本。

## Windows 具体怎么用

在本文件所在目录打开 PowerShell。启动脚本默认找到本电脑可用的 Codex Python；迁移到别的电脑时可用 `-PythonExe` 指定 Python 3.10+。

### 1. 只检查 Alpaca 连接

```powershell
.\run_alpaca_paper.ps1 -Mode Check
```

它读取旧 `.env`，显示脱敏账户信息和持仓数量，不需要 YHS Key，不提交订单。
如果 PowerShell 阻止执行脚本，可直接使用下面的 Python 命令；无需更改系统执行策略。

```powershell
$Python = 'C:\Users\fuxia\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python -X utf8 main.py --alpaca-check --alpaca-env-file 'C:\Users\fuxia\quant_bot\.env'
```

### 2. 预览 YHS 交易信号

```powershell
.\run_alpaca_paper.ps1 -Mode Preview
```

提示时输入自己的 YHS API Key，输入内容隐藏。该模式使用真实数据额度，符合条件时输出合约、数量、限价和拒绝原因，默认不向 Alpaca 写订单。
需要美股交易时段和有效 YHS 数据。休市时读取 Alpaca 日历并退出；本进程只运行一个美东交易日。
假设数据字段不足、价差过宽或合约买不起，系统会跳过；没有交易是有效结果。

### 3. 向模拟账户提交订单

先保证同一个 Alpaca Paper 账户只有这一个自动下单程序。检查旧机器人的计划任务和运行进程，或为新版创建独立 Paper 账户并使用其新密钥。
新版的进程锁只约束同一电脑、同一安装目录，无法阻止旧 Autopilot、另一台电脑或手工操作。

```powershell
.\run_alpaca_paper.ps1 -Mode ExecutePaper
```

等价直接命令：

```powershell
& $Python -X utf8 main.py --alpaca-paper --execute-paper --config config.alpaca_paper_100k.json --alpaca-policy alpaca_policy_100k.json --alpaca-env-file 'C:\Users\fuxia\quant_bot\.env'
```

直接命令需先设置当前进程的 `YHS_API_KEY`；脚本会在缺失时用隐藏输入询问。
订单始终发往 `https://paper-api.alpaca.markets`，没有通过参数切换到实盘的入口。
只支持买入 Call/Put 和卖出自己持有的期权平仓，不实施卖出开仓、裸卖或多腿组合。

## 如何看结果与恢复

`runtime/alpaca_paper_100k/` 保存中文日志、每日 YHS 筛选缓存、`trades_history.csv` 和 `summary.json`。
每日原始榜单在 `runtime/live/daily_universe/` 共用缓存，切换输出目录不会重新领取；所有实时运行须共用同一个 YHS 额度文件。
账户执行账本保存在 `runtime/alpaca_accounts/<账户 ID 的哈希>/alpaca_paper_state.json`，即使改变输出目录也复用同一执行账本。
账本绑定账户和风控配置，已受理但未成交的订单不会写成成交。
开仓前保存唯一 client order ID，遇到超时先查询；无法确认的请求保留未知状态，不盲目重复下单。
检测未归属的期权持仓或挂单、账户不一致或状态损坏时，停止新增。旧股票不被新版主动平仓。

后台目标每 5 秒查询 Alpaca 报价和订单状态；持仓报价通过一次批量请求读取，并逐合约校验源时间。见 [Alpaca 批量报价接口](https://docs.alpaca.markets/us/reference/optionlatestquotes)。
挂单优先复用本轮订单列表；终态或未知订单需要额外查询。网络、限频或大量成交对账会延长一轮耗时，5 秒是目标周期。YHS 信号仍为 5 / 12 分钟数据轮询。
券商 HTTP 受理不是成交；只有 `filled_qty` 和 `filled_avg_price` 确认后才记入成交和收益。
持仓估值使用 Alpaca 期权 bid，不用 Delta-Gamma 估算价触发券商卖单。
退出订单仍需确认成交，关停时会尝试对账，未确认的仓位继续保留为未结，不能称为已经清空。

当前退出依赖程序持续运行，没有向券商托管原生保护止损单。机器休眠、进程崩溃、网络中断或接口变慢时，不能保证按阈值或收盘前成交。
恢复时使用同一个执行账本核对远程订单和持仓；不要删除账本、改账户或改预算来绕过熔断。
预览模式禁止写订单，因此也不能替代执行模式管理已有模拟仓位。

## 行情与模拟成交的口径

默认使用 `indicative` 期权行情。它是衍生参考报价，并非实际 OPRA BBO；部分成交数据延迟 15 分钟。OPRA 需要相应订阅。见 [Alpaca 期权数据说明](https://docs.alpaca.markets/us/docs/historical-option-data)。

Alpaca Paper 的成交记录是券商模拟成交，不是交易所真实成交；Paper 不能完整反映排队、冲击、延迟滑点和费用。见 [Alpaca Paper 说明](https://docs.alpaca.markets/us/docs/paper-trading)。

Alpaca Paper 默认启用期权，买入 Call/Put 需要足够购买力和对应等级。见 [Alpaca 期权交易说明](https://docs.alpaca.markets/us/docs/options-trading)。

本地 `--replay` 仍使用 Delta-Gamma 合成估值，不会回放 Alpaca 真实订单撮合。
回放结果和 Paper 实际填写的成交 CSV 分别保存在不同账本中。

## 完全放在云端运行

可以。需要一台在交易时段持续在线、可访问 YHS 和 Alpaca HTTPS API 的服务器，以及 Python 3.10+。
这套机器人通过 REST 运行，不依赖本地 Codex 窗口、Robinhood MCP 或浏览器保持打开。
目前交付的是本地文件，没有租用服务器、上传密钥或创建云端任务。

迁移步骤：

1. 复制整个项目文件夹及需要恢复的 `runtime/` 到服务器；原运行进程停止后再迁移，同一账户只保留一个执行器。
2. 通过服务器密钥管理或受限权限环境文件提供 `ALPACA_API_KEY`、`ALPACA_SECRET_KEY`、`YHS_API_KEY`；不要把旧聊天或 `.env` 放进公共仓库。
3. 将工作目录设为项目目录，运行只读 `python3 main.py --alpaca-check`，然后运行预览命令。
4. 配置进程监管器和交易日启动；程序每个美东交易日收盘退出，使用 Alpaca 日历处理节假日和半日市。
5. 保存整个 `runtime/`，不要让每次启动都换成临时目录。失联时从 Alpaca Dashboard 核对订单和模拟仓位。

Linux 预览命令：

```bash
python3 main.py --alpaca-paper --config config.alpaca_paper_100k.json --alpaca-policy alpaca_policy_100k.json
```

添加 `--execute-paper` 才允许模拟下单。云端只是承载方式，不能消除行情、网络或程序风险。
