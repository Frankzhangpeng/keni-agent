param([switch]$PurgeToken)

$ErrorActionPreference = 'Stop'
$TaskName = 'KENI Agent'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Write-Host 'KENI current-user scheduled task removed.'

if ($PurgeToken) {
  $Cache = Join-Path $HOME '.superapp_agent.json'
  Remove-Item -LiteralPath $Cache -Force -ErrorAction SilentlyContinue
  Write-Host 'Pairing token and local agent key removed.'
} else {
  Write-Host 'Pairing data kept at ~/.superapp_agent.json. Use -PurgeToken to remove it.'
}
