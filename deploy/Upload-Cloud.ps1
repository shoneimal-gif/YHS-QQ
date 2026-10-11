param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]*$')]
    [string]$Server,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')]
    [string]$User,
    [ValidateRange(1, 65535)]
    [int]$Port = 22,
    [string]$IdentityFile = '',
    [Parameter(Mandatory = $true)]
    [string]$Archive
)

$ErrorActionPreference = 'Stop'
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
if (-not $archivePath.EndsWith('.tar.gz')) { throw '请指定 package_cloud.py 生成的 .tar.gz 部署包。' }
$sshArgs = @('-p', "$Port", '-o', 'ConnectTimeout=15')
$scpArgs = @('-P', "$Port", '-o', 'ConnectTimeout=15')
if ($IdentityFile) {
    $identityPath = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshArgs += @('-i', $identityPath)
    $scpArgs += @('-i', $identityPath)
}
$target = "${User}@${Server}"
$stage = '/tmp/yhs-qq-' + [guid]::NewGuid().ToString('N')

& ssh @sshArgs $target "umask 077 && mkdir -m 700 '$stage'"
if ($LASTEXITCODE -ne 0) { throw 'SSH 连接或远端暂存目录创建失败。' }
& scp @scpArgs $archivePath "${target}:${stage}/bundle.tar.gz"
if ($LASTEXITCODE -ne 0) { throw '部署包上传失败。' }
$hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$command = "cd '$stage' && echo '$hash  bundle.tar.gz' | sha256sum -c - && mkdir app && tar -xzf bundle.tar.gz -C app && sudo bash app/deploy/linux/install.sh"
& ssh -t @sshArgs $target $command
if ($LASTEXITCODE -ne 0) { throw '云端安装失败，已停止；请查看上方输出。' }
Write-Output "安装已完成，服务尚未启动。远端暂存目录：$stage"
Write-Output "下一步在服务器隐藏输入 API 密钥，再执行只读检查和预览。详见 CLOUD_DEPLOY.md。"
