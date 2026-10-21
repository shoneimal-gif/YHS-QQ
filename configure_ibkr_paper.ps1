[CmdletBinding()]
param(
    [ValidateSet(4002, 7497)]
    [int]$IbkrPort = 4002,
    [ValidateRange(1, 2147483647)]
    [int]$ClientId = 17932,
    [switch]$ConfirmPaperLogin,
    [switch]$EnableExecution,
    [switch]$ReuseExistingSource,
    [switch]$ConfigureSource,
    [string]$SourceHost = '',
    [string]$SourceUser = '',
    [ValidateRange(1, 65535)]
    [int]$SourcePort = 22,
    [string]$IdentityFile = '',
    [string]$KnownHostsFile = '',
    [switch]$EnableDiscord,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($EnableExecution -and -not $ConfirmPaperLogin) {
    throw '启用模拟执行前，请核对 Gateway 的 Paper Trading 登录，并显式加 -ConfirmPaperLogin。'
}
$hasSourceInput = [bool]($ConfigureSource -or $SourceHost -or $SourceUser -or $IdentityFile -or $KnownHostsFile)
if ($ReuseExistingSource -and $hasSourceInput) {
    throw '复用已有信号位置和手动配置来源请分别选择一种。'
}

function Assert-OrdinaryPath {
    param([string]$Path, [bool]$Directory)
    if (-not [System.IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\') -or $Path.StartsWith('//')) {
        throw '请使用本机已有文件的完整路径。'
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -ne $Directory) { throw '文件或目录类型不符合要求。' }
    $cursor = $item
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw '配置和 SSH 文件路径不得经过符号链接或重解析点。'
        }
        if ($cursor -is [System.IO.DirectoryInfo]) { $cursor = $cursor.Parent } else { $cursor = $cursor.Directory }
    }
    return $item.FullName
}

$privateDirectory = Join-Path $PSScriptRoot '.private_config'
$configPath = Join-Path $privateDirectory 'ibkr_paper.json'
$examplePath = Join-Path $PSScriptRoot 'ibkr_paper\config.example.json'
$ledgerPath = Join-Path $PSScriptRoot 'runtime\ibkr_paper\ledger.sqlite3'
if ($ReplaceExisting -and (Test-Path -LiteralPath $ledgerPath)) {
    throw '已有 IBKR 模拟账本，禁止重建配置。请保留原配置、账户绑定和全部账本；修改账户或资金策略需要单独迁移，不能删除账本后重绑。'
}
if (-not (Test-Path -LiteralPath $examplePath -PathType Leaf)) {
    throw '缺少 ibkr_paper\config.example.json，请完整取得程序。'
}
if (Test-Path -LiteralPath $privateDirectory) {
    [void](Assert-OrdinaryPath -Path $privateDirectory -Directory $true)
} else {
    [void](New-Item -ItemType Directory -Path $privateDirectory)
}
if (Test-Path -LiteralPath $configPath) {
    [void](Assert-OrdinaryPath -Path $configPath -Directory $false)
    if (-not $ReplaceExisting) {
        throw '已有模拟盘配置，本次未覆盖。仅在尚无模拟账本时，才可显式加 -ReplaceExisting 重建并重新核对账户。'
    }
}

$config = Get-Content -LiteralPath $examplePath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.ibkr.port = $IbkrPort
$config.ibkr.client_id = $ClientId
$config.paper_login_confirmed = [bool]$ConfirmPaperLogin
$config.execute_paper = [bool]$EnableExecution
$config.notifications.enabled = [bool]$EnableDiscord

if ($ReuseExistingSource) {
    $existingPath = Join-Path $privateDirectory 'ibkr_bridge.json'
    [void](Assert-OrdinaryPath -Path $existingPath -Directory $false)
    if ((Get-Item -LiteralPath $existingPath).Length -gt 32768) {
        throw '已有来源配置大小不符合要求。'
    }
    try {
        $existing = Get-Content -LiteralPath $existingPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($existing.source.kind -ne 'ssh_live_readonly') { throw 'source_kind' }
        # Copy only source connection metadata. Never copy a live IBKR port,
        # account binding, execution setting or any credential content.
        foreach ($name in @('kind', 'ssh_host', 'ssh_user', 'ssh_port', 'identity_file', 'known_hosts_file')) {
            $config.source.$name = $existing.source.$name
        }
        $SourceHost = [string]$config.source.ssh_host
        $SourceUser = [string]$config.source.ssh_user
        $SourcePort = [int]$config.source.ssh_port
        $IdentityFile = [string]$config.source.identity_file
        $KnownHostsFile = [string]$config.source.known_hosts_file
    } catch {
        throw '无法复用已有信号位置；请用 -ConfigureSource 填写已有连接资料。'
    }
}

if ($hasSourceInput -or $ReuseExistingSource) {
    if (-not $ReuseExistingSource) {
        if (-not $SourceHost) { $SourceHost = Read-Host '已有信号服务器的 IP 或域名' }
        if (-not $SourceUser) { $SourceUser = Read-Host '已有 SSH 用户名' }
        if (-not $IdentityFile) { $IdentityFile = Read-Host '已有 SSH 私钥的完整路径（不粘贴内容）' }
        if (-not $KnownHostsFile) { $KnownHostsFile = Read-Host '已有且已核实的 known_hosts 文件完整路径' }
    }
    $SourceHost = $SourceHost.Trim()
    $SourceUser = $SourceUser.Trim()
    if ($SourceHost -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$' -or
        $SourceHost.Contains('..') -or $SourceUser -cnotmatch '^[a-z_][a-z0-9_-]{0,31}$' -or
        $SourcePort -lt 1 -or $SourcePort -gt 65535) {
        throw '服务器地址、SSH 用户名或端口格式不符合要求。'
    }
    if (-not $IdentityFile -or -not $KnownHostsFile) {
        throw '读取云端信号需要已有私钥和已核实的主机指纹文件。'
    }
    $config.source.ssh_host = $SourceHost
    $config.source.ssh_user = $SourceUser
    $config.source.ssh_port = $SourcePort
    $config.source.identity_file = Assert-OrdinaryPath -Path $IdentityFile.Trim().Trim('"') -Directory $false
    $config.source.known_hosts_file = Assert-OrdinaryPath -Path $KnownHostsFile.Trim().Trim('"') -Directory $false
}

$json = $config | ConvertTo-Json -Depth 8
$temporaryPath = Join-Path $privateDirectory ('ibkr_paper.' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    [System.IO.File]::WriteAllText($temporaryPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        [System.IO.File]::Replace($temporaryPath, $configPath, $null)
    } else {
        [System.IO.File]::Move($temporaryPath, $configPath)
    }
} finally {
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Host '已保存独立的 IBKR 模拟盘配置；尚未连接账户、启动交易或发送通知。'
Write-Host '下一步先运行 -Preflight，核对 DU 模拟账户并绑定；仅端口 4002 不能证明是模拟盘。'
if (-not ($hasSourceInput -or $ReuseExistingSource)) {
    Write-Host '本次未配置云端信号位置；开始跟踪前还需要配置来源。'
}
if ($EnableExecution) {
    Write-Host '配置中已允许模拟执行，但仍须核对绑定账户，并显式使用 -ExecutePaper 启动。'
}
Write-Output '{"ibkr_paper_config_saved":true,"account_bound":false,"connected":false,"orders_submitted":false,"ssh_modified":false}'
