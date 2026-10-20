[CmdletBinding()]
param(
    [string]$ConfigPath = '',
    [string]$PythonExe = '',
    [switch]$InstallDependencies,
    [switch]$Preflight,
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$BindAccountSha256 = '',
    [switch]$SourceOnly,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (($Preflight -and $SourceOnly) -or ($BindAccountSha256 -and ($Preflight -or $SourceOnly -or $Once))) {
    throw '账户绑定、连接检查和仅查看信号请分别执行。'
}

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $PSScriptRoot '.private_config\ibkr_bridge.json'
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw '尚未配置。请先运行 .\configure_ibkr_readonly.ps1。'
}
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$venvPath = Join-Path $PSScriptRoot '.private_deploy\ibkr_readonly_venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$requirementsPath = Join-Path $PSScriptRoot 'ibkr_bridge\requirements.txt'

function Find-SetupPython {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw '-PythonExe 指向的文件不存在。'
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    $candidates = @(
        (Join-Path $PSScriptRoot '.private_deploy\personal_reader_venv\Scripts\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\*') { return $command.Source }
    throw '未找到 Python。请先安装 Python 3.10+，或加 -PythonExe 指定 python.exe 的完整路径。'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    if ($InstallDependencies) {
        if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
            throw '缺少 ibkr_bridge\requirements.txt，请确认已完整取得项目文件。'
        }
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            $setupPython = Find-SetupPython -ExplicitPath $PythonExe
            Write-Host '正在创建独立的 IBKR 只读环境。'
            & $setupPython -m venv $venvPath
            if ($LASTEXITCODE -ne 0) { throw '创建环境未完成；尚未连接 IBKR。' }
        }
        Write-Host '按你的 -InstallDependencies 指令下载并安装所需依赖。'
        & $venvPython -m pip --disable-pip-version-check install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成；尚未连接 IBKR。' }
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw '尚未安装独立环境。首次请运行 .\run_ibkr_readonly.ps1 -InstallDependencies -Preflight。'
    }

    $arguments = @('-X', 'utf8', '-m', 'ibkr_bridge.runner', '--config', $resolvedConfig)
    if ($Preflight) { $arguments += '--preflight' }
    if ($BindAccountSha256) { $arguments += @('--bind-account-sha256', $BindAccountSha256.ToLowerInvariant()) }
    if ($SourceOnly) { $arguments += '--source-only' }
    if ($Once) { $arguments += '--once' }

    Write-Host 'IBKR 实盘只读：只核对账户、信号与报价，不发送、修改或撤销订单。'
    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw '只读程序已停止，请按上方提示检查 TWS、账户绑定或来源配置。'
    }
} finally {
    Pop-Location
}
