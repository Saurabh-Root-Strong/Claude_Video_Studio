param([string]$Url, [string]$Out, [long]$Expected)
# Resumable download: loop curl -C - until file reaches expected size.
for ($i = 1; $i -le 200; $i++) {
    $have = if (Test-Path $Out) { (Get-Item $Out).Length } else { 0 }
    if ($Expected -gt 0 -and $have -ge $Expected) { break }
    curl.exe -L -s -S -C - --connect-timeout 20 --speed-limit 100000 --speed-time 15 -o $Out $Url
    $now = if (Test-Path $Out) { (Get-Item $Out).Length } else { 0 }
    "try $i : {0:N1} / {1:N1} MB (exit $LASTEXITCODE)" -f ($now/1MB), ($Expected/1MB)
    if ($LASTEXITCODE -eq 0 -and ($Expected -le 0 -or $now -ge $Expected)) { break }
    Start-Sleep -Seconds 2
}
$final = if (Test-Path $Out) { (Get-Item $Out).Length } else { 0 }
"DONE $Out {0:N1} MB, expected {1:N1} MB, complete={2}" -f ($final/1MB), ($Expected/1MB), ($final -eq $Expected)
