param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^wss?://')]
  [string]$Backend,
  [string]$Pair = ''
)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RepoDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$AgentScript = Join-Path $RepoDir 'keni_agent.py'
$Runner = Join-Path $RepoDir 'run-agent.ps1'
$LogDir = Join-Path $env:LOCALAPPDATA 'KENI\Logs'
$LogFile = Join-Path $LogDir 'keni-agent.log'
$TaskName = 'KENI Agent'

$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
$Python = Get-Command python.exe -ErrorAction SilentlyContinue
if ($PyLauncher) {
  & $PyLauncher.Source -3 -m venv $VenvDir
} elseif ($Python) {
  & $Python.Source -m venv $VenvDir
} else {
  throw 'Python 3 is required. Install it from python.org, then run the copied command again.'
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
  throw 'Failed to create the isolated Python environment.'
}

Write-Host 'Installing keni-agent dependencies in an isolated virtual environment...'
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $RepoDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

if ($Pair) {
  Write-Host 'Redeeming the one-time pairing code...'
  & $VenvPython $AgentScript --pair $Pair --pair-only --backend $Backend
  if ($LASTEXITCODE -ne 0) { throw 'Pairing failed.' }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function ConvertTo-SingleQuotedLiteral([string]$Value) {
  return $Value.Replace("'", "''")
}
$RunnerBody = @"
`$ErrorActionPreference = 'Stop'
`$env:PYTHONUNBUFFERED = '1'
& '$(ConvertTo-SingleQuotedLiteral $VenvPython)' '$(ConvertTo-SingleQuotedLiteral $AgentScript)' --backend '$(ConvertTo-SingleQuotedLiteral $Backend)' *>> '$(ConvertTo-SingleQuotedLiteral $LogFile)'
exit `$LASTEXITCODE
"@
Set-Content -LiteralPath $Runner -Value $RunnerBody -Encoding UTF8

$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$ActionArgs = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $Runner
$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $ActionArgs
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Settings = New-ScheduledTaskSettingsSet `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Description 'KENI remote control agent (current user only)' `
  -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host 'KENI agent is installed and running as a current-user scheduled task.'
Write-Host "Logs: $LogFile"
Write-Host "Uninstall: powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $RepoDir 'uninstall.ps1')`""
