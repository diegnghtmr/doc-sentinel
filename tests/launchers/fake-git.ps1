#!/usr/bin/env pwsh
$gitArgs = @($args)
$repo = ''
if ($gitArgs.Count -ge 2 -and $gitArgs[0] -eq '-C') { $repo = $gitArgs[1]; $gitArgs = @($gitArgs | Select-Object -Skip 2) }
if (-not $env:FAKE_GIT_HEAD) { exit 128 }
if ($gitArgs.Count -eq 2 -and $gitArgs[0] -eq 'rev-parse' -and $gitArgs[1] -eq '--show-toplevel') {
    if ($env:FAKE_GIT_TOPLEVEL) { [Console]::Out.WriteLine($env:FAKE_GIT_TOPLEVEL) } else { [Console]::Out.WriteLine($repo) }
} elseif ($gitArgs.Count -eq 2 -and $gitArgs[0] -eq 'rev-parse' -and $gitArgs[1] -eq 'HEAD') {
    [Console]::Out.WriteLine($env:FAKE_GIT_HEAD)
} elseif ($gitArgs.Count -ge 2 -and $gitArgs[0] -eq 'status' -and $gitArgs[1] -eq '--porcelain') {
    if ($env:FAKE_GIT_DIRTY) { [Console]::Out.Write($env:FAKE_GIT_DIRTY) }
} else { exit 99 }
exit 0
