param(
    [ValidateSet('Check', 'Preview', 'ExecutePaper')]
    [string]$Mode = 'Check',
    [string]$AlpacaEnvFile = (Join-Path $env:USERPROFILE 'quant_bot\.env'),
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
        if ($command -and $command.Source -notlike '*\WindowsApps\*') {
            $PythonExe = $command.Source
        } else {
            throw '请安装 Python 3.10+，并通过 -PythonExe 指定 python.exe 的完整路径。'
        }
    }
}

$arguments = @('-X', 'utf8', 'main.py')
if ($Mode -eq 'Check') {
    $arguments += '--alpaca-check'
} else {
    $arguments += @('--alpaca-paper', '--config', 'config.alpaca_paper_100k.json', '--alpaca-policy', 'alpaca_policy_100k.json')
    if ($Mode -eq 'ExecutePaper') {
        $arguments += '--execute-paper'
    }
}
if ($AlpacaEnvFile) {
    if (-not (Test-Path -LiteralPath $AlpacaEnvFile -PathType Leaf)) {
        throw "找不到 Alpaca 密钥文件：$AlpacaEnvFile。可传 -AlpacaEnvFile '' 改用环境变量。"
    }
    $arguments += @('--alpaca-env-file', $AlpacaEnvFile)
}

$temporaryYhsKey = $false
try {
    if ($Mode -ne 'Check' -and -not $env:YHS_API_KEY) {
        $secret = Read-Host '请输入 YHS API Key（隐藏输入）' -AsSecureString
        $env:YHS_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
        $temporaryYhsKey = $true
        if (-not $env:YHS_API_KEY) { throw 'YHS API Key 为空。' }
    }
    Write-Host "运行模式：$Mode；交易端点固定为 Alpaca Paper。"
    & $PythonExe @arguments
    if ($LASTEXITCODE -ne 0) { throw "机器人返回错误码 $LASTEXITCODE，请查看终端和 runtime 日志。" }
} finally {
    if ($temporaryYhsKey) { Remove-Item Env:YHS_API_KEY -ErrorAction SilentlyContinue }
}
