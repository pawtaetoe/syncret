# install the syncret sync client on Windows for this user: the script, a config readable only
# by you, and a scheduled task every 15 minutes. The token is read at a hidden prompt or from a
# file, never taken as an argument.
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Url https://syncret.example.com
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
param([string]$Url = '', [string]$TokenFile = '', [switch]$NoSync, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$Dir = Join-Path $env:LOCALAPPDATA 'syncret'
$Script = Join-Path $Dir 'sync.ps1'
$ConfigDir = Join-Path $env:APPDATA 'syncret'
$Config = Join-Path $ConfigDir 'client.env'
$Task = 'syncret-sync'

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $Script -Force -ErrorAction SilentlyContinue
  Write-Output "syncret-sync removed. Your skills stay on the last release; delete $Dir\releases,"
  Write-Output "the junctions in .claude\skills and .copilot\skills, .agents\skills and $Config to remove them too."
  exit 0
}

New-Item -ItemType Directory -Force -Path $Dir, $ConfigDir | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'sync.ps1') -Destination $Script -Force

if ($Url -or -not (Test-Path -LiteralPath $Config)) {
  if (-not $Url) { throw 'first install needs -Url' }
  if ($TokenFile) {
    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
  } else {
    $secure = Read-Host -AsSecureString 'Machine token from the syncret Machines page'
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
      [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  }
  if ($token -notmatch '^syncret_[A-Za-z0-9_-]+$') { throw 'that does not look like a machine token' }
  Set-Content -LiteralPath $Config -Value @("SYNCRET_URL=$($Url.TrimEnd('/'))", "SYNCRET_TOKEN=$token")
  # only this user may read the config: drop inherited access, grant the user alone
  icacls $Config /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

if ($NoSync) {
  Write-Output 'installed'
} else {
  Write-Output 'installed; first sync:'
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script -Force
}
