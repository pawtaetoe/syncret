# syncret sync client for Windows: pull the latest approved release and link its skills into
# each agent with directory junctions (no admin rights needed). Run every 15 minutes by the
# scheduled task install.ps1 registers. Works in Windows PowerShell 5.1 and PowerShell 7.
param([switch]$Force)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
# windows powershell 5.1 may not offer tls 1.2 by default
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Config = Join-Path $env:APPDATA 'syncret\client.env'
$Data = Join-Path $env:LOCALAPPDATA 'syncret'
$Releases = Join-Path $Data 'releases'
$Agents = Join-Path $HOME '.agents\skills'
$Claude = Join-Path $HOME '.claude\skills'
$Copilot = Join-Path $HOME '.copilot\skills'
$Tar = Join-Path $env:SystemRoot 'System32\tar.exe'
$Keep = 3

function Say($text) { [Console]::Error.WriteLine("syncret-sync: $text") }
function Fail($text) { Say $text; exit 1 }

# a junction removed as a link only; Remove-Item -Recurse could delete what it points to
function UnlinkJunction($path) { [System.IO.Directory]::Delete($path, $false) }
function Test-Link($path) {
  $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  return $null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}
function Get-LinkTarget($path) { return [string](Get-Item -LiteralPath $path -Force).Target }

if (-not (Test-Path -LiteralPath $Config)) { Fail "no config at $Config; run install.ps1" }
$Url = ''; $Token = ''
foreach ($line in Get-Content -LiteralPath $Config) {
  $key, $value = $line -split '=', 2
  if ($key -eq 'SYNCRET_URL') { $Url = $value.TrimEnd('/') }
  if ($key -eq 'SYNCRET_TOKEN') { $Token = $value }
}
if (-not $Url -or -not $Token) { Fail "$Config needs SYNCRET_URL and SYNCRET_TOKEN" }
New-Item -ItemType Directory -Force -Path $Releases | Out-Null

# one run at a time: an exclusively opened lock file
try {
  $lock = [System.IO.File]::Open((Join-Path $Data 'lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
  Say 'another sync is running'; exit 0
}
$Work = Join-Path $Data ('.work.' + [guid]::NewGuid().ToString('N'))
try {
  New-Item -ItemType Directory -Path $Work | Out-Null

  # skip until the interval the server last gave has passed
  $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $last = 0; $interval = 0
  if (Test-Path (Join-Path $Data 'last_check')) { $last = [long](Get-Content (Join-Path $Data 'last_check')) }
  if (Test-Path (Join-Path $Data 'interval')) { $interval = [long](Get-Content (Join-Path $Data 'interval')) }
  if (-not $Force -and ($now - $last) -lt ($interval * 60)) { exit 0 }

  $headers = @{ Authorization = "Bearer $Token" }
  function Get-Release($path, $out, $timeout) {
    try {
      Invoke-WebRequest -Uri "$Url$path" -Headers $headers -OutFile $out -UseBasicParsing -TimeoutSec $timeout
      return 200
    } catch {
      $response = $_.Exception.Response
      if ($null -eq $response) { Fail "cannot reach ${Url}: $($_.Exception.Message)" }
      return [int]$response.StatusCode
    }
  }

  $latestFile = Join-Path $Work 'latest'
  $status = Get-Release '/release/latest?format=text' $latestFile 30
  if ($status -eq 401) { Fail "the server refused this machine's token; make a new one on the Machines page" }
  if ($status -eq 404) { Say 'nothing published yet'; Set-Content (Join-Path $Data 'last_check') $now; exit 0 }
  if ($status -ne 200) { Fail "unexpected answer $status from $Url/release/latest" }
  $id, $sha, $minutes = ((Get-Content -LiteralPath $latestFile -Raw).Trim() -split '\s+')
  if ($id -notmatch '^[0-9a-f]{12}$') { Fail 'bad release id from the server' }
  if ($sha -notmatch '^[0-9a-f]{64}$') { Fail 'bad sha256 from the server' }
  if ($minutes -notmatch '^[0-9]+$') { $minutes = 60 }
  Set-Content (Join-Path $Data 'interval') $minutes
  Set-Content (Join-Path $Data 'last_check') $now

  # download, check and unpack a release we do not have yet; it only appears once complete
  $release = Join-Path $Releases $id
  if (-not (Test-Path -LiteralPath $release)) {
    $archive = Join-Path $Work 'release.tar.gz'
    $status = Get-Release "/release/$id.tar.gz" $archive 300
    if ($status -ne 200) { Fail "download of release $id answered $status" }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLower()
    if ($actual -ne $sha) { Fail "release $id failed its sha256 check; not installed" }
    $unpacked = Join-Path $Work 'unpacked'
    New-Item -ItemType Directory -Path $unpacked | Out-Null
    & $Tar -xzf $archive -C $unpacked
    if ($LASTEXITCODE -ne 0) { Fail "could not unpack release $id" }
    if (-not (Test-Path (Join-Path $unpacked 'skills')) -or -not (Test-Path (Join-Path $unpacked 'links.tsv'))) {
      Fail "release $id is incomplete"
    }
    Move-Item -LiteralPath $unpacked -Destination $release
    Get-ChildItem -LiteralPath $release -Recurse -File | ForEach-Object { $_.IsReadOnly = $true }
    Say "installed release $id"
  }

  # point .agents\skills at the release; refuse to replace a real folder. a junction swap is
  # two steps, so an agent starting at that instant may briefly find no folder
  $target = Join-Path $release 'skills'
  New-Item -ItemType Directory -Force -Path (Split-Path $Agents) | Out-Null
  if ((Test-Path -LiteralPath $Agents) -and -not (Test-Link $Agents)) {
    Fail "$Agents is a real folder; move it away so syncret can manage it"
  }
  if (-not (Test-Link $Agents) -or (Get-LinkTarget $Agents) -ne $target) {
    if (Test-Link $Agents) { UnlinkJunction $Agents }
    New-Item -ItemType Junction -Path $Agents -Target $target | Out-Null
    Say "now on release $id"
  }
  Set-Content (Join-Path $Data 'current') $id

  # junctions into claude code and copilot as links.tsv says, through .agents\skills so the
  # next swap moves them too; only links this script made are ever changed or removed
  $wanted = @()
  foreach ($row in Get-Content -LiteralPath (Join-Path $release 'links.tsv')) {
    $folder, $toClaude, $toCopilot, $null = $row -split "`t"
    if ($folder -notmatch '^[A-Za-z0-9._-]+$') { continue }
    if ($toClaude -eq '1') { $wanted += Join-Path $Claude $folder }
    if ($toCopilot -eq '1') { $wanted += Join-Path $Copilot $folder }
  }
  function Test-OwnLink($path) {
    (Test-Link $path) -and (Get-LinkTarget $path).StartsWith($Agents, [StringComparison]::OrdinalIgnoreCase)
  }

  $made = @()
  foreach ($link in $wanted) {
    $linkTarget = Join-Path $Agents (Split-Path $link -Leaf)
    if (Test-OwnLink $link) {
      if ((Get-LinkTarget $link) -ne $linkTarget) {
        UnlinkJunction $link
        New-Item -ItemType Junction -Path $link -Target $linkTarget | Out-Null
      }
    } elseif (Test-Path -LiteralPath $link) {
      Say "leaving $link alone: it is not a syncret link"
      continue
    } else {
      New-Item -ItemType Directory -Force -Path (Split-Path $link) | Out-Null
      New-Item -ItemType Junction -Path $link -Target $linkTarget | Out-Null
    }
    $made += $link
  }
  $linksFile = Join-Path $Data 'links'
  if (Test-Path -LiteralPath $linksFile) {
    foreach ($link in Get-Content -LiteralPath $linksFile) {
      if ($link -and ($made -notcontains $link) -and (Test-OwnLink $link)) { UnlinkJunction $link }
    }
  }
  Set-Content -LiteralPath $linksFile -Value $made

  # keep the current release and the newest others, up to Keep in all
  Get-ChildItem -LiteralPath $Releases -Directory |
    Where-Object { $_.Name -ne $id -and -not $_.Name.StartsWith('.') } |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip ($Keep - 1) |
    ForEach-Object {
      Get-ChildItem -LiteralPath $_.FullName -Recurse -File | ForEach-Object { $_.IsReadOnly = $false }
      Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
} finally {
  if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
  $lock.Dispose()
}
exit 0
