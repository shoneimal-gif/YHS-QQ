# YHS QQ 云服务器部署

目标环境：Ubuntu 22.04+ 或 Debian 12+，systemd，Python 3.10+。业务程序只使用标准库。
本目录的文件是部署材料；是否已经安装并运行，要以目标服务器上的服务状态和日志为准。

## 运行安排

- 服务使用专用的 `yhsqq` 账户运行，代码位于 `/opt/yhs-qq/app`。
- 每周一到周五，美东时间 09:25 启动。程序等到开盘再加载 YHS 数据；使用 Alpaca 日历识别休市、实际收盘和半日市。
- 定时器明确使用 `America/New_York`，夏令时由服务器时区数据库处理，无需按北京时间改时间。
- 正常收盘或休市退出后，等待下次定时启动。异常退出等待 60 秒重试，30 分钟内最多启动 5 次，达到上限后需要排查并恢复。
- 定时器支持补启动；服务器故障期间的策略监控无法补做。程序恢复时从持久化账本和券商状态对账。
- `runtime` 保存在 `/var/lib/yhs-qq/runtime`，`/opt/yhs-qq/app/runtime` 链接到该目录。重启、升级保留原始交易账本、额度记录和每日缓存。
- API 密钥位于服务器 `/etc/yhs-qq/credentials.json`，只有 root 可读，通过 systemd 的 `LoadCredential` 提供给服务；不进入部署包或 GitHub。

定时器和凭据机制参考 [systemd 时间表达式](https://manpages.ubuntu.com/manpages/jammy/man7/systemd.time.7.html) 与 [systemd 凭据文档](https://github.com/systemd/systemd/blob/main/docs/CREDENTIALS.md)。

## 1. 准备服务器连接

需要服务器 IP/域名、SSH 用户名、端口及本机可用的 SSH 身份。账户应能使用 sudo。
初次连接，按云控制台提供的 SSH 主机指纹核对服务器；不要关闭 SSH 主机身份校验。
机器人不需要开放网站端口，只需服务器能访问 YHS 和 Alpaca 的 HTTPS API。

## 2. 停止旧实例并打包

在本机当前目录的 PowerShell 执行。迁移前停止所有使用同一 Alpaca Paper 账户的旧执行器和自动启动任务。
跨主机的文件锁不能阻止两个执行器同时下单；云端启用后，本机只用 `Check` 查询账户。

```powershell
cd 'C:\Users\fuxia\Documents\ChatGPT\YHS QQ'
$Python = 'C:\Users\fuxia\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python -X utf8 .\deploy\package_cloud.py
```

输出部署包的完整路径、文件数和 SHA256。部署包在 `.private_deploy/`，包含私有交易状态，不上传公共仓库。
打包时持有本目录已有的运行锁，当前目录有运行实例则拒绝打包。
只打包业务源文件、100k 配置、Linux 服务文件与需要迁移的 `runtime`；排除密钥、日志、锁、临时文件和合成回放。
打包后不要重新在本地启动自动下单，以免本地账本继续变化。

## 3. 上传并安装

将下面的地址、用户和部署包路径换成实际值。如使用 SSH 私钥，增加 `-IdentityFile` 并填入本机文件路径。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\Upload-Cloud.ps1 -Server SERVER_IP -User ubuntu -Port 22 -Archive '.\.private_deploy\实际部署包.tar.gz'
```

上传脚本核对远端部署包 SHA256，再执行安装器。安装器创建专用用户、代码目录、状态目录、服务及定时器。
安装过程不启动服务。服务已经运行或定时器已经启用时，安装器拒绝覆盖；升级前需在适当时段停止服务和定时器。
已有服务器 `runtime` 非空时保持原样，不用本地旧账本覆盖。需要合并或更换账本时先核对两端状态。

## 4. 在服务器设置密钥

用 SSH 登录服务器，在服务器终端执行：

```bash
sudo python3 /opt/yhs-qq/app/deploy/linux/configure_keys.py
```

依次隐藏输入 Alpaca Paper 的 API Key、Secret Key 和 YHS API Key。不要把密钥发到聊天或放在命令参数中。
更新密钥前先停止云端服务和定时器，避免不同启动过程读取不同配置。

## 5. 只读检查和预览

下面的临时服务与正式服务使用同一工作目录、专用账户和凭据文件。
确认正式服务未运行后执行，不要与正式服务并行运行预览。

```bash
sudo systemd-run --unit=yhs-qq-check --wait --pipe --collect \
  --property=User=yhsqq --property=Group=yhsqq \
  --property=WorkingDirectory=/opt/yhs-qq/app \
  --property=LoadCredential=api:/etc/yhs-qq/credentials.json \
  --property=UMask=0077 \
  /usr/bin/python3 -u /opt/yhs-qq/app/deploy/linux/runner.py --check
```

检查账户、期权权限、订单与持仓。如果只有旧机器人未管理的股票持仓，程序会记录数量；策略期权持仓应与迁移账本一致。

```bash
sudo systemd-run --unit=yhs-qq-preview --wait --pipe --collect \
  --property=User=yhsqq --property=Group=yhsqq \
  --property=WorkingDirectory=/opt/yhs-qq/app \
  --property=LoadCredential=api:/etc/yhs-qq/credentials.json \
  --property=UMask=0077 \
  /usr/bin/python3 -u /opt/yhs-qq/app/deploy/linux/runner.py --preview
```

预览不写入订单，但在交易时段会使用 YHS 数据额度。休市时返回 `MARKET_CLOSED` 并退出，这只验证券商连接和对账，未验证盘中 YHS 信号。
已有策略期权仓位时，预览不能替代执行服务管理退出。预览在交易时段会持续到收盘；需要提前结束时，在另一 SSH 终端执行 `sudo systemctl stop yhs-qq-preview.service`。

## 6. 启用自动运行

确认本机旧执行器已停、密钥和账本核对完成、只读检查无异常后，在服务器执行：

```bash
sudo systemctl enable --now yhs-qq.timer
sudo systemctl list-timers yhs-qq.timer
```

正式服务允许 Alpaca Paper 模拟下单。定时器的补启动可能立即触发服务。
需要现在启动一次时：

```bash
sudo systemctl start yhs-qq.service
sudo journalctl -u yhs-qq.service -n 60 --no-pager
```

只有交易时段日志确认 YHS 候选池加载、正常扫描和对账后，才算盘中运行已验证。现在休市时验证范围限于安装、调度和连接。

## 查看、停止和恢复

```bash
# 状态和最近日志
sudo systemctl status yhs-qq.service yhs-qq.timer --no-pager
sudo journalctl -u yhs-qq.service -f

# 停止自动启动，再停止当前实例
sudo systemctl disable --now yhs-qq.timer
sudo systemctl stop yhs-qq.service

# 修复问题后清除启动次数限制并恢复
sudo systemctl reset-failed yhs-qq.service
sudo systemctl enable --now yhs-qq.timer
```

停止服务会向程序发送 SIGTERM，程序停止新开仓并走现有退出/对账流程；停止不等于券商订单已经成交，应核对日志和 Paper Dashboard。
日志同时位于持久化 `runtime/alpaca_paper_100k/system.log`。常驻日志需要按服务器容量配置保留时间；定期备份 `/var/lib/yhs-qq/runtime`。

## 部署范围

此方案提供定时启动、进程监管和持久化账本，不包含额外的远程报警服务。
当前止盈止损依赖程序在线监控，云服务器故障或接口失联时仍然需要从 Alpaca Paper 核对订单与仓位。
