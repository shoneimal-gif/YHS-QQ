# YHS 异动期权监控与模拟复盘

Python 3.10+，全部业务代码、网络请求、日志和测试只使用标准库，无需 pip。
六个 YHS 核心模块和 Alpaca Paper 适配器完整交付。
`--live` 读取 YHS 行情并操作本地估值模拟账本；`--alpaca-paper` 对接 Alpaca 模拟账户。
Alpaca 模式默认只生成交易预览，添加 `--execute-paper` 才能提交模拟订单。
交易地址固定为 Paper，无实盘模式。先读 [ALPACA_PAPER_GUIDE.md](ALPACA_PAPER_GUIDE.md)。

云服务器后台运行与迁移步骤见 [CLOUD_DEPLOY.md](CLOUD_DEPLOY.md)，部署脚本位于 `deploy/`。
Codex 云端开发与离线验证任务说明位于 [CLOUD_TASK.txt](CLOUD_TASK.txt)，可从包含此仓库的已发布云端环境开始。
订单通知仅启用 Discord，配置见 [DISCORD_NOTIFICATIONS.md](DISCORD_NOTIFICATIONS.md)。
旧企业微信与 pushplus 模块保留，但主程序不加载其凭据或启动发送线程。
私有通知配置和交易账本不随 GitHub 同步；Linux 服务器需另行配置 Discord 凭据。

## 快速运行

在本目录打开 PowerShell，已有可用 Python 时运行：

```powershell
python -X utf8 main.py --replay
```

本电脑的 `python.exe` 系统别名不可用，验证时使用了 Codex 自带运行时：

```powershell
$Python = 'C:\Users\fuxia\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python -X utf8 main.py --replay
```

默认生成新的 `runtime/replay/运行时间_随机后缀/`，不覆盖之前的结果。
固定输出目录必须是新的回放目录：

```powershell
& $Python -X utf8 main.py --replay --date 2026-10-09 --seed 7 --output-dir runtime/my_replay
```

实时监控需要自己的 YHS Key。不要把 Key 写入文件、提交到版本库或发在聊天里。
以下代码从隐藏输入读取后仅设置当前 PowerShell 进程的环境变量：

```powershell
$secret = Read-Host 'YHS API Key' -AsSecureString
$env:YHS_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
& $Python -X utf8 main.py --live --config config.example.json --output-dir runtime/live --capture runtime/live/2026-10-12.jsonl
Remove-Item Env:YHS_API_KEY
```

每个交易日使用一个新的采集 JSONL；同日重启可继续写同一文件。
所有实时运行应共用同一账本/额度目录；改变目录不能改变同一 YHS 账户共享配额的事实。
启动时读取免费 Discover 校准额度，同日读取已保存榜单，禁止再次请求榜单。
共用 `quota_state_path` 的运行模式通过其父目录的 `daily_universe/YYYY-MM-DD/claim.json` 共用每日榜单领取记录和原始缓存。
本进程运行一个美东交易日；收盘退出。长期运行可由系统计划任务每天启动。
运行期间机器须保持开机，进程须存活；本代码没有部署到云端或创建系统任务。

采集文件复盘不访问网络：

```powershell
& $Python -X utf8 main.py --replay --replay-file runtime/live/2026-10-12.jsonl
& $Python -X utf8 -m unittest discover -s tests -v
```

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | 配置数据类、美东 DST、分时调度、预算、交易时段覆盖 |
| `yhs_client.py` | HTTPS GET、全局限速/并发、免费重试、持久化额度与响应头熔断 |
| `detector.py` | Top100 清洗、净 GEX 节点、疲劳去重、按需 Flow、报价校验 |
| `paper_trader.py` | 现金/仓位账本、Delta-Gamma 估值、风控、CSV、复盘统计 |
| `mock_replay.py` | 78 个五分钟观察点、可复现模拟数据、五种退出路径 |
| `alpaca_client.py` | 标准库 Alpaca Paper REST、凭据复用、行情和订单查询 |
| `alpaca_trader.py` | 券商模拟成交账本、1% 仓位、限价订单、对账和退出 |
| `main.py` | 本地模拟 / 回放 / Alpaca 只读检查 / Paper 预览及执行 |
| `run_alpaca_paper.ps1` | Windows 只读检查、预览、模拟下单启动脚本 |
| `config.alpaca_paper_100k.json` | $100,000 模拟策略的 YHS 配置 |
| `alpaca_policy_100k.json` | Alpaca 单笔 1% 及组合风控配置 |
| `tests/` | 无网络的标准库 unittest 测试 |
| `example_replay/` | 已运行的合成全日回放结果，不是历史绩效 |

## 已核对的官方契约

核对日期：2026-10-10。基准为 [YHS OpenAPI](https://api.yehangshe.com/v1/openapi.json)、
[配额规则](https://docs.yehangshe.com/api/quota-and-rate-limits)、
[Heatmap](https://docs.yehangshe.com/api/dealer-heatmap-snapshot)、
[Flow](https://docs.yehangshe.com/api/domains/flow)、
[Options](https://docs.yehangshe.com/api/domains/options)。

| 用途 | 实际 URL 路径与参数 |
| --- | --- |
| 每日榜单 | `/v1/market/options-volume-rank`，没有 `limit` 参数；响应后本地取 Top100 |
| Heatmap | `/v1/derived/heatmap/{ticker}/snapshot`，不是 `/heatmap/snapshot?ticker=` |
| Flow | `/v1/flow/trades?tickers=NVDA&kind=order&min_premium_usd=50000` |
| 入场期权链 | `/v1/options/chain-snapshot/{ticker}?expiration=YYYY-MM-DD&limit=400`，仅第一页 |
| 免费元数据 | `/v1/discover`、`/v1/health`、`/v1/openapi.json` |

鉴权是 `Authorization: Bearer <YHS_API_KEY>`。
只有数据请求的 HTTP200 计1单位，202/4xx/5xx不计，元数据即使200也不计。
200 响应 JSON 损坏仍然记费；网络断开、未获知状态则保留1单位不确定性预算，
该预留不是服务端已确认扣费。额度文件区分 `charged_200`、`uncertain` 和在途预留。

202 使用无限次、有上限的指数退避；收盘、用户中断或额度熔断会终止等待。
429 尊重 `Retry-After` 和窗口重置；401/402/403直接停止，其他不可重试4xx停止当前请求。
解析所有 HTTP 响应头，包括错误响应；零额度停止数据请求，Discover仍可读取。
请求起始间隔至少1.2秒，同时满足任意滚动60秒最多50次、最多3个在途请求。
跨线程和共用额度文件的进程共享控制；读取响应正文期间也占用在途槽位。

## 数据格式与拒绝规则

Heatmap 官方 `data` 含 `ticker`、`spot_usd`、`generated_at`、`session_date_et`、
`expirations`（日期字符串数组）和 `cells`。
cell 含 `expiration`、`strike_usd`、`net_dealer_gex_usd`，不提供 Call/Put 拆分。
系统选10日内同时具备正负节点、绝对净GEX总量最大的到期日，并提取最大正值/最小负值节点。
正、负节点来自同一到期日；不会把缺席节点当0。

官方 `generated_at=09:30` 的现货来自09:34，09:35之后才可见。
系统区分采样时间（段起点+4分钟）、接收时间和交易决策时间，避免倒填成交时间。
采集文件保存 Flow/链返回时刻，回放不提前使用稍后才返回的报价。
开盘09:30暂时没有已完成的当日段，系统等待后续样本；不能声称秒级捕捉。

榜单、Flow、期权链的公开 OpenAPI 当前未声明完整的逐行响应 schema。
本项目提供明确的兼容字段合同和别名适配，见 [DATA_FORMATS.md](DATA_FORMATS.md)。
模拟榜单/Flow/链遵循本项目合同；不能据此宣称它们与未公开的实际字段逐一一致。
未提供真实 Key，因此没有完成真实数据的端到端联调。
首次接入应核对脱敏数据字段，必要时修改 detector 的别名适配；缺必需字段会拒绝信号并记录原因。

若榜单没有资产类型，需要在配置 `symbol_types` 中提供经过确认的 `stock` / `etf` / `index`。
未知类型默认剔除，不能仅根据股票代码推断所有ETF。
仅5只白名单ETF保留；现货缺失或低于20美元剔除，不额外逐只拉现货来突破每日1次榜单预算。
先Top100后清洗，不从第101名补齐。Vol/OI≥0.65或OI增幅≥5%是量能准入；PCR极端加分。
PCR中性保留，方向与PCR明确相反的结构拒绝。YHS `*_pct` 是fraction：0.05表示5%。

疲劳按“进入0.35%触碰带→离开0.75%重置带→再次进入”计算独立回踩。
连续停留或重复时间戳不算多次；同日同到期同正节点第4次进入起拒绝信号。
Flow必须为过去15分钟内同方向ASK、premium≥50000、合约日成交量≥OI，且与节点到期一致。
不会拿大单张数 `contracts` 当作全天成交量；缺少日成交量/OI就不确认。
Flow同一合约须出现在本次链页中；否则放弃开仓，不偷偷翻页。
实际模式拒绝显式模拟报价；只有离线模式允许合成标识。

## 估值与风控口径

入场成本=`bid/ask中间价×1.02`，出场净价=`估算中间价×0.98`，每张100股。
模型始终锚定入场spot和入场中间价：

`mark=max(0, entry_mid + delta*(spot-entry_spot) + 0.5*gamma*(spot-entry_spot)**2)`

Call Delta为正，Put Delta为负；有有效Greeks就用入场链，否则默认±0.45和0.04。
不会每步重复累加Gamma项；持仓期间不拉期权链。
它是局部估值近似，未覆盖Theta、IV变化、动态Greeks、实际流动性与报价价差变化。
CSV `valuation_source=delta_gamma_model`，没有把估算价标成市场实盘成交价。

所有阈值按含双边滑点的净清算ROI判断：

1. 初始止损−35%。
2. Peak MFE达到35%后，止损线至少0%。
3. Peak MFE达到60%后，激活追踪线。
4. 达到150%强制止盈；多项同刻触发时硬顶止盈优先。
5. 收盘定时器取消请求并清平本地模拟仓位；异常/用户停止同样按最后已知模型价平仓。

默认 `trailing_mode=premium`：期权净值从峰值回撤20%，即
`stop_roi=max(0, (1+peak_roi)*0.8-1)`。
例如Peak ROI60%，追踪线为28%，不是40%。
如果想用收益率回撤20个百分点，设 `trailing_mode=roi`，追踪线为 `peak_roi-0.20`。
止损按下一次观测价格成交，因此0%保本线、−35%止损及150%止盈阈值不保证精确成交。
示例保本案例因观察跳跃和滑点实际为−3.92%，硬止损案例−39.63%；这是记录真实观测模型的结果。

“方向正确率”严格按单笔Peak MFE≥15%判断，不等同于最终盈利比例或收盘方向。
`Avg Peak MFE`统计已平仓交易；未平仓数单独披露。Peak为采样观察到的峰值，不是连续行情真峰值。

## 配额模型与日历

标准实时调度：早盘24次（09:30至11:25）、午间15次（11:30至14:18）、尾盘18次（14:30至15:55），共57轮。
完整合成回放依需求固定78轮（09:30至15:55），另在16:00执行清仓，不算第79轮。
57轮相较78轮减少26.92%，不是精确节约30%。

60只标的×57轮×22日=75,240次Heatmap；每日榜单22次、Flow最多1,320次、链最多440次，
合计上界77,022次/月（不含异常网络安全预留）。23个完整交易日对应80,523次。
Alpaca 多仓配置将 Flow / 链日预算分别提高到100 / 40次；60标的、22个完整交易日的相同上界为78,342次，23日为81,903次。
每月还预留5,000单位供持仓盯市。接近月上限时按剩余工作日预算缩减新标的池，保留额度区仅盯已有仓位。
重启可能额外发一次当前扫描；严格月额度/服务端余量熔断始终生效。
Flow/链在请求前持久化日计数，崩溃后不丢失额度预留。
榜单在请求前持久化每日claim；如果当次崩溃且榜单没有成功保存，安全停止新增信号，不重复抓榜单。

美东时钟有IANA ZoneInfo优先、标准库DST兜底，不依赖本机中国时间。
默认只识别周末；交易所节假日、半日市和特殊停市需在 `session_overrides` 明确配置。
例：`{"2026-11-26":null,"2026-11-27":"13:00"}`表示休市和半日市。
此示例仅展示格式；实际运营前须按交易所官方日历维护全年配置。
配置可覆盖非标准交易日，不会自行拉取额外收费的日历端点。

## 持久化与输出

`paper_state.json` / `replay_state.json` 保存现金、开仓和已平仓历史；原子写入且损坏状态不自动重置。
`detector_state.json` 保存节点回踩和信号冷却。
`quota_state.json` 保存月度账本、在途请求和滑动限速窗口。
`sessions/YYYY-MM-DD.json` 保存每日榜单、Flow/链计数和已处理样本。
`daily_universe/YYYY-MM-DD/claim.json` 位于额度目录，跨输出目录复用每日榜单；已领取但未保存响应时安全停止本日新增筛选。
OS锁限制同一模拟账本只有一个进程写入；模拟账本方法加RLock保护收盘定时器。
断电/机器休眠期间无法执行16点动作；恢复时补记清仓并标注恢复原因，不虚构错过的行情。
收盘只能使用最后有效spot估值，没有对收盘价进行额外API请求。
`stale`标明最后spot是否过期，`last_time`保留实际源时间。

`trades_history.csv` 的前17列严格为需求字段，后面追加数量、Greeks、来源、过期标记等审计字段。
每次导出原子重写全部已平仓历史，避免重启和重复导出重复追加；`summary.json` 给出统计，`system.log` 为中文日志。

本项目实现用户定义的GEX/Flow启发式规则。正Gamma节点不是必然支撑/阻力；成交量≥OI也不能单独证明开仓。
测试验证实现、预算及状态机一致性，没有验证策略在历史/实盘中的盈利能力。
