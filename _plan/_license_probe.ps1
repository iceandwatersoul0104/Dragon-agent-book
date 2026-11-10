# ASCII-only PowerShell script (ANSI codepage safe).
# Probe GitHub license for arbitrary repos and optionally append to the
# verified store (a JSON file). Usage:
#   powershell -File _license_probe.ps1 owner/repo [owner/repo ...]
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Store = Join-Path $PSScriptRoot 'vol2-verified-refs.json'

function Get-Page([string]$url) {
  for ($i = 0; $i -lt 4; $i++) {
    try {
      $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30 `
           -Headers @{ 'User-Agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }
      return $r.Content
    } catch { Start-Sleep -Seconds (3 + $i * 3) }
  }
  return $null
}

function Get-License([string]$repo) {
  $html = Get-Page "https://github.com/$repo"
  if (-not $html) { return 'FETCH_FAIL' }

  # 1) sidebar "X license"
  $m = [regex]::Match($html, '>\s*([A-Za-z0-9\.\-\+ ]{2,40}?)\s+license\s*<')
  if ($m.Success) { return ("SIDEBAR: " + $m.Groups[1].Value.Trim()) }

  # 2) LICENSE file link -> fetch raw text via github.com/<repo>/raw/<ref>/<file>
  $m = [regex]::Match($html, 'href="/[^"]+/blob/([^/"]+)/(LICEN[SC]E[^"#]*)"')
  if ($m.Success) {
    $ref = $m.Groups[1].Value
    $file = $m.Groups[2].Value
    $txt = Get-Page "https://github.com/$repo/raw/$ref/$file"
    if ($txt) {
      $lines = $txt -split "`r?`n"
      for ($i = 0; $i -lt [Math]::Min(40, $lines.Count); $i++) {
        $s = $lines[$i].Trim()
        if ($s -match '^(MIT License|Apache License|GNU (AFFERO |LESSER )?GENERAL PUBLIC|Mozilla Public License|BSD \d-Clause|ISC License|The Unlicense|Boost Software License|CC0 1\.0 Universal|Creative Commons Zero)') {
          return ("RAW: " + $s)
        }
      }
      for ($i = 0; $i -lt [Math]::Min(40, $lines.Count); $i++) {
        $s = $lines[$i].Trim()
        if ($s -match '(?i)except for|enterprise/|does not apply|separately licensed') {
          return ("RAW_NOTE: " + $s)
        }
      }
      return ('RAW_HEAD: ' + ($lines | Where-Object { $_.Trim() -ne '' } | Select-Object -First 3) -join ' / ')
    }
    return "RAW_FAIL: $ref/$file"
  }
  return 'NO_MATCH'
}

foreach ($repo in $args) {
  $res = Get-License $repo
  Write-Output ("{0,-40} {1}" -f $repo, $res)
  Start-Sleep -Seconds 4
}
Write-Output 'DONE'
