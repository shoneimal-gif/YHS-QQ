param(
    [switch]$TestPush,
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $PythonExe) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) {
        $PythonExe = $bundled
    } else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if (-not $command -or $command.Source -like '*\WindowsApps\*') {
            throw '请用 -PythonExe 指定可用的 Python 3.10+ 完整路径。'
        }
        $PythonExe = $command.Source
    }
}

if ($TestPush) {
    $testCode = @'
from pathlib import Path
from wecom_notifications import load_webhook, send_test_notification
try:
    webhook = load_webhook(Path.cwd())
    if not webhook:
        raise ValueError('not configured')
    send_test_notification(webhook)
except Exception:
    raise SystemExit('推送未确认成功，请检查机器人配置、网络及群内消息；重试可能产生重复测试消息。') from None
print('企业微信已确认接收测试消息；请查看机器人所在群。没有提交交易订单。')
'@
    & $PythonExe -X utf8 -c $testCode
    if ($LASTEXITCODE -ne 0) { throw '企业微信测试推送失败，详见上方脱敏提示。' }
    return
}

$directory = Join-Path $PSScriptRoot '.private_config'
if (Test-Path -LiteralPath $directory) {
    $item = Get-Item -LiteralPath $directory
    if (-not $item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw '私有配置目录不是普通目录，请先检查 .private_config。'
    }
} else {
    New-Item -ItemType Directory -Path $directory | Out-Null
}
# Give only this Windows user and SYSTEM access to the private configuration.
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$acl.SetOwner($userSid)
foreach ($sid in @($userSid, $systemSid)) {
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $directory -AclObject $acl

$previousWebhook = $env:YHS_WECOM_WEBHOOK
try {
    $secret = Read-Host '粘贴企业微信机器人 Webhook（隐藏输入）' -AsSecureString
    $env:YHS_WECOM_WEBHOOK = [System.Net.NetworkCredential]::new('', $secret).Password
    $saveCode = @'
import json, os, tempfile
from pathlib import Path
from wecom_notifications import validate_webhook
directory = Path.cwd() / '.private_config'
temporary = None
try:
    webhook = validate_webhook(os.environ.get('YHS_WECOM_WEBHOOK', ''))
    destination = directory / 'wecom.json'
    if destination.is_symlink():
        raise ValueError('unexpected credential symlink')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, prefix='.wecom-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({'webhook_url': webhook}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    temporary = None
except Exception:
    raise SystemExit('配置保存失败，请检查 Webhook 格式或私有目录权限。') from None
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
print('配置已保存到本机私有目录；未发送消息。重新启动 ExecutePaper 后启用订单推送。')
'@
    & $PythonExe -X utf8 -c $saveCode
    if ($LASTEXITCODE -ne 0) { throw '企业微信配置失败，详见上方脱敏提示。' }
} finally {
    if ($null -eq $previousWebhook) {
        Remove-Item Env:YHS_WECOM_WEBHOOK -ErrorAction SilentlyContinue
    } else {
        $env:YHS_WECOM_WEBHOOK = $previousWebhook
    }
}
