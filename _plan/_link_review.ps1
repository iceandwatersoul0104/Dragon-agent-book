# -*- coding: utf-8 -*-
# Re-check all non-GitHub / non-arXiv external links via PowerShell.
# NOTE: keep this file ASCII-only. PowerShell -File reads it with the system
# ANSI codepage, so any CJK literal here corrupts the parse.
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot
$UA = 'Mozilla/5.0 (compatible; agent-book-editorial/1.0)'

$docs = @()
$docs += Get-ChildItem "$root\vol1" -Recurse -Filter '*.md' -File
$urls = @{}
foreach ($d in $docs) {
    $text = [IO.File]::ReadAllText($d.FullName)
    $m = [regex]::Matches($text, 'https?://[^\s\uff0c\u3002\u3001\uff1b\uff1a\uff09)\u3011\]"''`\uff0c\u3002\u3001\uff1b\uff1a\uff09]+')
    foreach ($x in $m) {
        $u = $x.Value.TrimEnd('.', ',', ';', ':', ')', ']', '"')
        $u = $u -replace '\uFF08.*$', ''
        if ($u -match 'github\.com' -or $u -match 'arxiv\.org') { continue }
        if (-not $urls.ContainsKey($u)) { $urls[$u] = @() }
        $urls[$u] += $d.Name
    }
}
Write-Host "links to check: $($urls.Count)"
$bad = @()
$i = 0
foreach ($u in ($urls.Keys | Sort-Object)) {
    $i++
    $status = 'OK'
    try {
        $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 20 -Headers @{ 'User-Agent' = $UA }
        $status = "OK $($r.StatusCode)"
    } catch {
        $m = $_.Exception.Message
        if ($m -match 'StatusCode\s*(\d+)') { $status = "HTTP $($Matches[1])" }
        elseif ($m -match 'SSL|TLS|closed') { $status = 'NETBLOCKED' }
        elseif ($m -match 'resolve|name') { $status = 'DNSFAIL' }
        else { $status = "ERR $($m.Substring(0,[Math]::Min(36,$m.Length)))" }
    }
    Write-Host ("{0,2}/{1}  {2,-56} {3}" -f $i, $urls.Count, $u.Substring(0,[Math]::Min(54,$u.Length)), $status)
    if ($status -notlike 'OK*') { $bad += [pscustomobject]@{ url = $u; status = $status; files = ($urls[$u] -join ',') } }
    Start-Sleep -Milliseconds 200
}
Write-Host ''
Write-Host '=== NOT-OK links ==='
$bad | Format-Table -AutoSize -Wrap | Out-String -Width 200
$bad | ConvertTo-Json -Depth 4 | Out-File -FilePath "$root\_plan\_link_review.json" -Encoding UTF8
Write-Host "wrote $root\_plan\_link_review.json"
