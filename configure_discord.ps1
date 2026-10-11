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

if ($env:YHS_DISCORD_WEBHOOK) {
    Write-Host '当前会话已设置 YHS_DISCORD_WEBHOOK：运行程序或测试时，环境变量优先于本机配置文件。本脚本会恢复并保留原环境值。'
}

if ($TestPush) {
    $testCode = @'
import re
from pathlib import Path
from discord_notifications import load_webhook, send_test_notification
try:
    webhook = load_webhook(Path.cwd())
    if not webhook:
        raise ValueError('not configured')
    message_id = send_test_notification(webhook)
except Exception:
    raise SystemExit('测试推送未确认创建，请检查 Webhook、频道权限和 Discord 网络连接；响应丢失后重试可能产生重复测试消息。') from None
print('Discord 已确认创建频道测试消息；请查看 #yhs-paper。此次未提交交易订单。')
if isinstance(message_id, str) and re.fullmatch(r'[0-9]{1,32}', message_id):
    print('消息 ID 尾号：' + message_id[-8:])
'@
    & $PythonExe -X utf8 -c $testCode
    if ($LASTEXITCODE -ne 0) { throw 'Discord 测试推送失败，详见上方脱敏提示。' }
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
$aclWriteReportedFailure = $false
try {
    Set-Acl -LiteralPath $directory -AclObject $acl
} catch {
    # Windows can report a privilege error after writing the directory DACL.
    # Continue only if a fresh read strictly confirms the intended permissions.
    $aclWriteReportedFailure = $true
}
try {
    $verifiedAcl = Get-Acl -LiteralPath $directory -ErrorAction Stop
    $verifiedRules = @($verifiedAcl.GetAccessRules(
        $true, $true, [System.Security.Principal.SecurityIdentifier]))
    $allowedSids = @($userSid.Value, $systemSid.Value)
    $requiredInheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    $seenSids = @{}
    if (-not $verifiedAcl.AreAccessRulesProtected -or $verifiedRules.Count -ne 2) {
        throw 'Unexpected directory access rules.'
    }
    foreach ($verifiedRule in $verifiedRules) {
        $verifiedSid = $verifiedRule.IdentityReference.Value
        if ($allowedSids -notcontains $verifiedSid -or $seenSids.ContainsKey($verifiedSid) -or
            $verifiedRule.IsInherited -or
            $verifiedRule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
            $verifiedRule.FileSystemRights -ne [System.Security.AccessControl.FileSystemRights]::FullControl -or
            $verifiedRule.InheritanceFlags -ne $requiredInheritance -or
            $verifiedRule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) {
            throw 'Unexpected directory access rule.'
        }
        $seenSids[$verifiedSid] = $true
    }
    foreach ($requiredSid in $allowedSids) {
        if (-not $seenSids.ContainsKey($requiredSid)) {
            throw 'Required directory access rule is missing.'
        }
    }
} catch {
    throw '无法确认私有配置目录仅允许当前用户和 SYSTEM 访问；未保存 Webhook。请先检查 .private_config 的权限。'
}
if ($aclWriteReportedFailure) {
    Write-Host '目录权限写入报告错误，但已重新核对权限完全符合要求；继续配置。'
}

$destinationPath = Join-Path $directory 'discord.json'
if (Test-Path -LiteralPath $destinationPath) {
    $destinationItem = Get-Item -LiteralPath $destinationPath
    if ($destinationItem.PSIsContainer -or ($destinationItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw '私有配置文件不是普通文件，请先检查 .private_config/discord.json。'
    }
}

$previousWebhook = $env:YHS_DISCORD_WEBHOOK
try {
    $secret = Read-Host '粘贴 #yhs-paper 的完整 Discord Webhook URL（隐藏输入，不要发到聊天）' -AsSecureString
    $env:YHS_DISCORD_WEBHOOK = [System.Net.NetworkCredential]::new('', $secret).Password
    $saveCode = @'
import json, os, tempfile
from pathlib import Path
from discord_notifications import validate_webhook
directory = Path.cwd() / '.private_config'
temporary = None
try:
    webhook = validate_webhook(os.environ.get('YHS_DISCORD_WEBHOOK', ''))
    destination = directory / 'discord.json'
    if destination.is_symlink():
        raise ValueError('unexpected credential symlink')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, prefix='.discord-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({'webhook_url': webhook}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    temporary = None
except Exception:
    raise SystemExit('配置保存失败，请检查完整 Discord Webhook URL 格式或私有目录权限。') from None
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
print('配置已保存到本机私有目录；未发送消息。重新启动 ExecutePaper 后启用 Discord 订单推送。')
'@
    & $PythonExe -X utf8 -c $saveCode
    if ($LASTEXITCODE -ne 0) { throw 'Discord 配置失败，详见上方脱敏提示。' }
} finally {
    if ($null -eq $previousWebhook) {
        Remove-Item Env:YHS_DISCORD_WEBHOOK -ErrorAction SilentlyContinue
    } else {
        $env:YHS_DISCORD_WEBHOOK = $previousWebhook
    }
}
