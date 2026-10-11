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

if ($env:YHS_PUSHPLUS_TOKEN) {
    Write-Host '当前会话已设置 YHS_PUSHPLUS_TOKEN：运行程序或测试时，环境变量优先于本机配置文件。本脚本会保留原环境值。'
}

if ($TestPush) {
    $testCode = @'
import re
from pathlib import Path
from pushplus_notifications import load_token, send_test_notification
try:
    token = load_token(Path.cwd())
    if not token:
        raise ValueError('not configured')
    serial = send_test_notification(token)
except Exception:
    raise SystemExit('测试推送未确认受理，请检查 Token、实名认证、网络及额度；响应丢失后重试可能产生重复测试消息。') from None
print('pushplus 已受理测试推送；请到个人微信确认。此次未提交交易订单。')
if isinstance(serial, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', serial):
    print('流水号尾号：' + serial[-8:])
'@
    & $PythonExe -X utf8 -c $testCode
    if ($LASTEXITCODE -ne 0) { throw 'pushplus 测试推送失败，详见上方脱敏提示。' }
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

$destinationPath = Join-Path $directory 'pushplus.json'
if (Test-Path -LiteralPath $destinationPath) {
    $destinationItem = Get-Item -LiteralPath $destinationPath
    if ($destinationItem.PSIsContainer -or ($destinationItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw '私有配置文件不是普通文件，请先检查 .private_config/pushplus.json。'
    }
}

$previousToken = $env:YHS_PUSHPLUS_TOKEN
try {
    $secret = Read-Host '粘贴 pushplus 的 Token（隐藏输入，不要发到聊天）' -AsSecureString
    $env:YHS_PUSHPLUS_TOKEN = [System.Net.NetworkCredential]::new('', $secret).Password
    $saveCode = @'
import json, os, tempfile
from pathlib import Path
from pushplus_notifications import validate_token
directory = Path.cwd() / '.private_config'
temporary = None
try:
    token = validate_token(os.environ.get('YHS_PUSHPLUS_TOKEN', ''))
    destination = directory / 'pushplus.json'
    if destination.is_symlink():
        raise ValueError('unexpected credential symlink')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, prefix='.pushplus-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({'token': token}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    temporary = None
except Exception:
    raise SystemExit('配置保存失败，请检查 Token 格式或私有目录权限。') from None
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
print('配置已保存到本机私有目录；未发送消息。重新启动 ExecutePaper 后启用订单推送。')
'@
    & $PythonExe -X utf8 -c $saveCode
    if ($LASTEXITCODE -ne 0) { throw 'pushplus 配置失败，详见上方脱敏提示。' }
} finally {
    if ($null -eq $previousToken) {
        Remove-Item Env:YHS_PUSHPLUS_TOKEN -ErrorAction SilentlyContinue
    } else {
        $env:YHS_PUSHPLUS_TOKEN = $previousToken
    }
}
