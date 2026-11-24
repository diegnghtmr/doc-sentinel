#!/usr/bin/env pwsh
$record = @{ args = @('python') + @($args); env = @{} } | ConvertTo-Json -Compress -Depth 5
Add-Content -LiteralPath $env:FAKE_LOG -Value $record -Encoding utf8
if (@($args) -contains '-c') { exit 0 }
if ($env:FAKE_PYTHON_STDOUT) { [Console]::Out.Write($env:FAKE_PYTHON_STDOUT) }
if ($env:FAKE_PYTHON_CODE) { exit [int]$env:FAKE_PYTHON_CODE }
exit 0
