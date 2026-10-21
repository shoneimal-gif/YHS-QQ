[CmdletBinding()]
param(
    [string]$ConfigPath = '',
    [string]$PythonExe = '',
    [switch]$InstallDependencies,
    [switch]$Preflight,
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$BindAccountSha256 = '',
    [switch]$ObserveOnly,
    [switch]$Once,
    [switch]$ExecutePaper
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$taskActionCount = @($Preflight, [bool]$BindAccountSha256, $ObserveOnly, $ExecutePaper) |
    Where-Object { [bool]$_ } | Measure-Object
if ($taskActionCount.Count -gt 1 -or ($BindAccountSha256 -and $Once)) {
    throw '预检、账户绑定、只读观察和模拟执行请分别运行。'
}
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot '.private_config\ibkr_paper.json' }
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw '尚未配置。请先运行 .\configure_ibkr_paper.ps1。'
}
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$venvPath = Join-Path $PSScriptRoot '.private_deploy\ibkr_paper_venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$requirementsPath = Join-Path $PSScriptRoot 'ibkr_paper\requirements.txt'

function Find-SetupPython {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw '-PythonExe 指向的文件不存在。'
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    $candidates = @(
        (Join-Path $PSScriptRoot '.private_deploy\ibkr_readonly_venv\Scripts\python.exe'),
        (Join-Path $PSScriptRoot '.private_deploy\personal_reader_venv\Scripts\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\*') { return $command.Source }
    throw '未找到 Python 3.10+；可加 -PythonExe 指定 Python 的完整路径。'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    if ($InstallDependencies) {
        if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
            throw '缺少 ibkr_paper\requirements.txt，请完整取得项目文件。'
        }
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            $setupPython = Find-SetupPython -ExplicitPath $PythonExe
            Write-Host '正在创建独立的 IBKR 模拟盘环境。'
            & $setupPython -m venv $venvPath
            if ($LASTEXITCODE -ne 0) { throw '创建环境未完成；没有连接 IBKR。' }
        }
        Write-Host '按你的 -InstallDependencies 指令下载并安装依赖。'
        & $venvPython -m pip --disable-pip-version-check --no-input install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成；没有连接 IBKR。' }
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw '首次请运行 .\run_ibkr_paper.ps1 -InstallDependencies -Preflight。'
    }
    $arguments = @('-B', '-X', 'utf8', '-m', 'ibkr_paper.runner', '--config', $resolvedConfig)
    if ($Preflight) { $arguments += '--preflight' }
    elseif ($BindAccountSha256) { $arguments += @('--bind-account-sha256', $BindAccountSha256.ToLowerInvariant()) }
    elseif ($ExecutePaper) { $arguments += '--execute-paper' }
    else { $arguments += '--observe-only' }
    if ($Once) { $arguments += '--once' }

    if ($ExecutePaper) {
        Write-Host 'IBKR 模拟盘自动跟单：程序将再次核对 DU 账户、模拟端口、来源和风控后处理新信号。'
    } else {
        Write-Host 'IBKR 模拟盘预检或只读观察：本次不会提交订单。'
    }
    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw '程序已停止，请按上方固定分类核对 Gateway、DU 账户绑定、来源和已有委托。不要删除账本或重复发送未知订单。'
    }
} finally {
    Pop-Location
}
