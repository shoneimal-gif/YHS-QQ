[CmdletBinding()]
param(
    [ValidateSet(7496, 4001)]
    [int]$IbkrPort = 7496,
    [ValidateRange(1, 2147483647)]
    [int]$ClientId = 17931,
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

function Assert-OrdinaryPath {
    param([string]$Path, [bool]$Directory)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw '不使用符号链接或重解析点作为配置或 SSH 文件。'
    }
    if ($item.PSIsContainer -ne $Directory) { throw '文件或目录类型不符合要求。' }
    return $item.FullName
}

$privateDirectory = Join-Path $PSScriptRoot '.private_config'
$configPath = Join-Path $privateDirectory 'ibkr_bridge.json'
$examplePath = Join-Path $PSScriptRoot 'ibkr_bridge\config.example.json'

if (-not (Test-Path -LiteralPath $examplePath -PathType Leaf)) {
    throw '缺少 ibkr_bridge\config.example.json，请完整取得项目文件。'
}
if (Test-Path -LiteralPath $privateDirectory) {
    [void](Assert-OrdinaryPath -Path $privateDirectory -Directory $true)
} else {
    [void](New-Item -ItemType Directory -Path $privateDirectory)
}
if (Test-Path -LiteralPath $configPath) {
    [void](Assert-OrdinaryPath -Path $configPath -Directory $false)
    if (-not $ReplaceExisting) {
        throw '已有私有配置。为保留账户绑定，本次未覆盖；如要重新配置并重新绑定，请显式加 -ReplaceExisting。'
    }
}

$config = Get-Content -LiteralPath $examplePath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.ibkr.port = $IbkrPort
$config.ibkr.client_id = $ClientId
$config.notifications.enabled = [bool]$EnableDiscord

$hasSourceInput = [bool]($ConfigureSource -or $SourceHost -or $SourceUser -or $IdentityFile -or $KnownHostsFile)
if ($hasSourceInput) {
    if (-not $SourceHost) { $SourceHost = Read-Host '已有信号服务器的 IP 或域名（不输入密码）' }
    if (-not $SourceUser) { $SourceUser = Read-Host '已有 SSH 用户名' }
    if (-not $IdentityFile) { $IdentityFile = Read-Host '已有受保护 SSH 私钥的完整路径（不要粘贴私钥内容）' }
    if (-not $KnownHostsFile) { $KnownHostsFile = Read-Host '已有且已核实的 known_hosts 文件完整路径' }
    $SourceHost = $SourceHost.Trim()
    $SourceUser = $SourceUser.Trim()
    if ($SourceHost -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$' -or
        $SourceHost.Contains('..') -or $SourceUser -cnotmatch '^[a-z_][a-z0-9_-]{0,31}$') {
        throw '服务器地址或 SSH 用户名格式不符合要求；请填写已有连接的信息。'
    }
    if (-not $IdentityFile -or -not $KnownHostsFile) { throw '读取云端信号需要已有私钥和已核实的主机指纹文件。' }
    $config.source.ssh_host = $SourceHost
    $config.source.ssh_user = $SourceUser
    $config.source.ssh_port = $SourcePort
    $config.source.identity_file = Assert-OrdinaryPath -Path $IdentityFile.Trim().Trim('"') -Directory $false
    $config.source.known_hosts_file = Assert-OrdinaryPath -Path $KnownHostsFile.Trim().Trim('"') -Directory $false
}

# This file contains connection metadata, never an IBKR password or Discord token.
# SSH key files, their permissions, and known_hosts are only referenced, not changed.
$json = $config | ConvertTo-Json -Depth 8
$temporaryPath = Join-Path $privateDirectory ('ibkr_bridge.' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($temporaryPath, $json, $encoding)
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

Write-Host '已保存本地只读配置；没有连接 IBKR、启动服务或发送订单。'
Write-Host '请保持 TWS 的 Read-Only API 勾选，再运行连接检查并核对账户。'
if (-not $hasSourceInput) {
    Write-Host '本次尚未配置云端信号位置；连接检查可先运行，开始跟踪前还需要绑定已有信号服务器。'
}
if ($EnableDiscord) {
    Write-Host '已开启复用自己现有 Discord 通知的设置；本次没有读取或发送任何通知。'
}
Write-Output '{"ibkr_readonly_config_saved":true,"account_bound":false,"connected":false,"orders_submitted":false,"ssh_modified":false}'
