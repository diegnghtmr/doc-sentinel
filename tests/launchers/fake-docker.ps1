#!/usr/bin/env pwsh
$dockerArgs = @($args)
$config = @{}
Get-ChildItem Env: | Where-Object { $_.Name -like 'FAKE_*' } | ForEach-Object {
    $config[$_.Name] = $_.Value
}
$record = @{ args = $dockerArgs; env = $config } | ConvertTo-Json -Compress -Depth 5
Add-Content -LiteralPath $env:FAKE_LOG -Value $record -Encoding utf8
$code = 0
if ($dockerArgs.Count -ge 1 -and $dockerArgs[0] -eq 'info') {
    if ($env:FAKE_INFO_CODE) { $code = [int]$env:FAKE_INFO_CODE }
    if ($code -eq 0 -and $dockerArgs -contains '--format') {
        if ($env:FAKE_OSTYPE) { [Console]::Out.WriteLine($env:FAKE_OSTYPE) }
        else { [Console]::Out.WriteLine('linux') }
    }
} elseif ($dockerArgs.Count -ge 2 -and $dockerArgs[0] -eq 'image' -and $dockerArgs[1] -eq 'inspect') {
    if ($env:FAKE_IMAGE_CODE) { $code = [int]$env:FAKE_IMAGE_CODE }
    if ($code -eq 0 -and $dockerArgs -contains '--format') { if ($env:FAKE_IMAGE_ID) { [Console]::Out.WriteLine($env:FAKE_IMAGE_ID) } else { [Console]::Out.WriteLine('sha256:fake') } }
} elseif ($dockerArgs.Count -ge 1 -and $dockerArgs[0] -eq 'build') {
    [Console]::Out.WriteLine('build stdout')
    [Console]::Error.WriteLine('build stderr')
    if ($env:FAKE_BUILD_CODE) { $code = [int]$env:FAKE_BUILD_CODE }
} elseif ($dockerArgs.Count -ge 1 -and $dockerArgs[0] -eq 'run') {
    [Console]::Out.Write($env:FAKE_RUN_STDOUT)
    [Console]::Error.Write($env:FAKE_RUN_STDERR)
    if ($env:FAKE_RUN_CODE) { $code = [int]$env:FAKE_RUN_CODE }
} else {
    [Console]::Error.WriteLine('unexpected fake docker call')
    $code = 99
}
exit $code
