# Windows PowerShell 5.1 and PowerShell 7. Arguments are always passed as arrays.
# Dot-source with -NoMain to exercise the platform-independent decision helpers.

function Get-WindowsBuild { [System.Environment]::OSVersion.Version.Build }
function Test-IsWindows11([int]$Build) { return $Build -ge 22000 }
function Get-WindowsBuildMessage([int]$Build) {
    if (Test-IsWindows11 -Build $Build) { return "Windows 11 detected (build $Build)" }
    return "Warning: only Windows 11 is supported/tested (detected build $Build)"
}
function Find-DockerDesktop([string[]]$ProgramFiles) {
    foreach ($base in $ProgramFiles) {
        if ([string]::IsNullOrWhiteSpace($base)) { continue }
        $candidate = Join-Path (Join-Path (Join-Path $base 'Docker') 'Docker') 'Docker Desktop.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}
function Get-DockerStartupAction([bool]$DockerOnPath, [bool]$InfoSucceeded, [string]$DesktopPath) {
    if ($DockerOnPath -and $InfoSucceeded) { return 'ready' }
    if ($DesktopPath) { return 'start' }
    return 'missing'
}
function Get-ContainerModeError([string]$OSType) {
    if ($OSType.Trim() -ne 'linux') {
        return 'Docker Desktop is in Windows-containers mode; switch to Linux containers'
    }
    return $null
}
function Convert-HostPathToMount([string]$HostDirectory, [string]$ContainerDirectory, [bool]$ReadOnly) {
    $mode = if ($ReadOnly) { 'ro' } else { 'rw' }
    return "${HostDirectory}:${ContainerDirectory}:${mode}"
}
function Test-SamePath([string]$First, [string]$Second, [bool]$Windows) {
    $comparison = if ($Windows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    return [string]::Equals($First, $Second, $comparison)
}
# Shared by the real launcher and the platform simulation: no shell command strings.
function New-DockerRunArguments(
    [bool]$Linux, [bool]$Windows, [string]$User, [string]$InputDirectory,
    [string]$OutputDirectory, [string]$ReportDirectory, [string[]]$ContainerArguments,
    [string]$ReviewDirectory, [string[]]$DisplayEnvironment
) {
    $result = @('run', '--rm', '--network=none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=1g',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '-e', 'DOC_SENTINEL_NO_NETNS=1')
    if ($Linux) { $result += @('--user', $User) }
    foreach ($entry in $DisplayEnvironment) { $result += @('-e', $entry) }
    if ($InputDirectory) {
        $result += @('-v', (Convert-HostPathToMount -HostDirectory $InputDirectory -ContainerDirectory '/work/in' -ReadOnly $true))
    }
    if ($OutputDirectory) {
        $result += @('-v', (Convert-HostPathToMount -HostDirectory $OutputDirectory -ContainerDirectory '/work/out' -ReadOnly $false))
    }
    $reuse = $OutputDirectory -and $ReportDirectory -and (Test-SamePath -First $OutputDirectory -Second $ReportDirectory -Windows $Windows)
    if ($ReportDirectory -and -not $reuse) {
        $result += @('-v', (Convert-HostPathToMount -HostDirectory $ReportDirectory -ContainerDirectory '/work/report' -ReadOnly $false))
    }
    if ($ReviewDirectory) {
        $result += @('-v', (Convert-HostPathToMount -HostDirectory $ReviewDirectory -ContainerDirectory '/work/review' -ReadOnly $false))
    }
    return $result + $ContainerArguments
}
function Write-LauncherError([string]$Message) { [Console]::Error.WriteLine($Message) }
function Show-Usage {
    @(
        'usage: doc-sentinel.ps1 [-Rebuild] [-StartTimeoutSeconds N] {clean,detect,sanitize,review-merge,version,intent} ...'
        '  clean FILE [-o PATH | --output-dir DIR] [--force-output] [--json|--agent-summary] [--report PATH]'
        '  detect FILE [--sanitize [-o PATH | --output-dir DIR] [--force-output]] [--agent-summary] [--review-dir DIR]'
        '  sanitize FILE --review-dir DIR [-o PATH | --output-dir DIR] [--force-output] [--report PATH] [--agent-summary]'
        '  review-merge --review-dir DIR [--report PATH] [--agent-summary]'
        '  version | intent TEXT...'
        '  update and --allow-network require the Python CLI on the host.'
    ) | ForEach-Object { [Console]::Out.WriteLine($_) }
}
function Get-FullHostPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Path))
}
# Resolve symlinks in each existing component; new leaves need an existing parent.
function Resolve-HostPath([string]$Path, [int]$Depth = 0) {
    if ($Depth -gt 64) { throw 'too many symbolic links' }
    $full = Get-FullHostPath $Path
    $parent = [IO.Path]::GetDirectoryName($full)
    if (-not $parent -or $parent -eq $full) { return $full }
    $parent = Resolve-HostPath -Path $parent -Depth ($Depth + 1)
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'directory does not exist' }
    $full = Join-Path $parent ([IO.Path]::GetFileName($full))
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and $item.LinkType -and $item.Target) {
        $target = [string](@($item.Target)[0])
        if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path $parent $target }
        return Resolve-HostPath -Path $target -Depth ($Depth + 1)
    }
    return $full
}
function Test-DockerOnPath {
    return [bool](Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
}
function Find-DockerCli([string]$DesktopPath) {
    $command = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    if ($DesktopPath) {
        $candidate = Join-Path (Join-Path (Join-Path (Split-Path -Parent $DesktopPath) 'resources') 'bin') 'docker.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}
function Test-DockerInfo([string]$DockerCli) {
    if (-not $DockerCli) { return $false }
    & $DockerCli info >$null 2>$null
    return $LASTEXITCODE -eq 0
}
function Initialize-Docker([bool]$Windows, [int]$TimeoutSeconds) {
    $desktop = $null
    if ($Windows) {
        [Console]::Error.WriteLine((Get-WindowsBuildMessage -Build (Get-WindowsBuild)))
        $desktop = Find-DockerDesktop -ProgramFiles @($env:ProgramFiles, ${env:ProgramW6432})
    }
    $docker = Find-DockerCli -DesktopPath $desktop
    $ready = Test-DockerInfo -DockerCli $docker
    if ($Windows) {
        $onPath = Test-DockerOnPath
        $action = Get-DockerStartupAction -DockerOnPath $onPath -InfoSucceeded $ready -DesktopPath $desktop
        if ($action -eq 'missing') {
            throw 'Docker Desktop is not installed. Install it with: winget install -e --id Docker.DockerDesktop'
        }
        if ($action -eq 'start') {
            try { Start-Process -FilePath $desktop -ErrorAction Stop | Out-Null }
            catch { throw "Could not start Docker Desktop: $($_.Exception.Message)" }
            $timer = [Diagnostics.Stopwatch]::StartNew()
            $ready = $false
            while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
                [Console]::Error.WriteLine('Waiting for Docker Desktop to start...')
                $remainingMs = [Math]::Max(1, [int](($TimeoutSeconds - $timer.Elapsed.TotalSeconds) * 1000))
                Start-Sleep -Milliseconds ([Math]::Min(2000, $remainingMs))
                $docker = Find-DockerCli -DesktopPath $desktop
                if (Test-DockerInfo -DockerCli $docker) { $ready = $true; break }
            }
            if (-not $ready) { throw "Docker Desktop did not start within $TimeoutSeconds seconds" }
        }
        $osType = & $docker info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Docker daemon is not available' }
        $modeError = Get-ContainerModeError -OSType ([string]$osType)
        if ($modeError) { throw $modeError }
    } else {
        if (-not $docker) { throw 'docker is not installed or not on PATH' }
        if (-not $ready) {
            if ((& uname -s) -eq 'Darwin') { throw 'Docker daemon is not available; open Docker Desktop and try again' }
            throw 'Docker daemon is not available'
        }
    }
    return $docker
}
function Test-SubmodulesPopulated([string]$Repo) {
    $root = Join-Path $Repo 'third_party'
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { return $false }
    $directories = @(Get-ChildItem -LiteralPath $root -Directory -Force)
    if ($directories.Count -eq 0) { return $false }
    foreach ($directory in $directories) {
        $contents = @(Get-ChildItem -LiteralPath $directory.FullName -Force | Where-Object { $_.Name -ne '.git' })
        if ($contents.Count -eq 0) { return $false }
    }
    return $true
}

if (($args.Count -gt 0 -and $args[0] -eq '-NoMain') -or $env:DOC_SENTINEL_LAUNCHER_TEST -eq '1') { return }

function Invoke-Launcher([string[]]$Arguments) {
    $windowsHost = $env:OS -eq 'Windows_NT'
    $rebuild = $false; $timeout = 180; $index = 0
    while ($index -lt $Arguments.Count) {
        if ($Arguments[$index] -eq '-Rebuild') { $rebuild = $true; $index++; continue }
        if ($Arguments[$index] -eq '-StartTimeoutSeconds') {
            $index++
            if ($index -ge $Arguments.Count -or -not [int]::TryParse($Arguments[$index], [ref]$timeout) -or $timeout -lt 1) {
                Write-LauncherError 'invalid -StartTimeoutSeconds'; return 2
            }
            $index++; continue
        }
        break
    }
    if ($index -ge $Arguments.Count) { Show-Usage; return 2 }
    $mode = $Arguments[$index]; $index++
    if ($mode -eq '--help' -or $mode -eq '-h') { Show-Usage; return 0 }
    if ($mode -eq 'update') {
        Write-LauncherError 'update runs on a git checkout with network: use the Python CLI on the host (doc-sentinel update)'; return 2
    }
    if (@('clean', 'detect', 'sanitize', 'review-merge', 'version', 'intent') -cnotcontains $mode) {
        Write-LauncherError "unknown command: $mode"; return 2
    }
    $inputPath = ''; $outputPath = ''; $outputDirOption = ''; $reportPath = ''; $reviewPath = ''; $json = $false; $agent = $false
    $defaultCleanOutput = ''; $force = $false; $promptGuard = $false; $failOn = ''; $passthrough = @(); $sanitized = $mode -eq 'sanitize'
    if ($mode -eq 'clean' -or $mode -eq 'detect' -or $mode -eq 'sanitize' -or $mode -eq 'review-merge') {
        while ($index -lt $Arguments.Count) {
            $arg = $Arguments[$index]; $index++
            if ($arg -eq '-h' -or $arg -eq '--help') { Show-Usage; return 0 }
            if ($arg -eq '--allow-network' -or $arg.StartsWith('--allow-network=')) {
                Write-LauncherError '--allow-network is not available in the container launcher'; return 2
            }
            if (@('-o', '--output', '--output-dir', '--report', '--fail-on', '--review-dir') -ccontains $arg) {
                if ($index -ge $Arguments.Count -or -not $Arguments[$index]) { Write-LauncherError "missing value for $arg"; return 2 }
                $value = $Arguments[$index]; $index++
                if ($arg -eq '-o' -or $arg -eq '--output') {
                    if ($mode -eq 'review-merge') { Write-LauncherError "unknown option: $arg"; return 2 }; $outputPath = $value
                } elseif ($arg -eq '--output-dir') {
                    if ($mode -eq 'review-merge') { Write-LauncherError "unknown option: $arg"; return 2 }; $outputDirOption = $value
                } elseif ($arg -eq '--report') { $reportPath = $value }
                elseif ($arg -eq '--review-dir') {
                    if ($mode -eq 'clean') { Write-LauncherError "unknown option: $arg"; return 2 }; $reviewPath = $value
                } else { if ($mode -ne 'detect') { Write-LauncherError "unknown option: $arg"; return 2 }; $failOn = $value }
            } elseif ($arg.StartsWith('--output=') -or ($arg.StartsWith('-o') -and $arg.Length -gt 2)) {
                if ($mode -eq 'review-merge') { Write-LauncherError "unknown option: $arg"; return 2 }
                if ($arg.StartsWith('--output=')) { $outputPath = $arg.Substring(9) } else { $outputPath = $arg.Substring(2) }
                if (-not $outputPath) { Write-LauncherError 'missing value for --output'; return 2 }
            } elseif ($arg.StartsWith('--output-dir=')) {
                if ($mode -eq 'review-merge') { Write-LauncherError "unknown option: $arg"; return 2 }
                $outputDirOption = $arg.Substring(13); if (-not $outputDirOption) { Write-LauncherError 'missing value for --output-dir'; return 2 }
            } elseif ($arg.StartsWith('--report=')) {
                $reportPath = $arg.Substring(9); if (-not $reportPath) { Write-LauncherError 'missing value for --report'; return 2 }
            } elseif ($arg.StartsWith('--review-dir=')) {
                if ($mode -eq 'clean') { Write-LauncherError "unknown option: $arg"; return 2 }
                $reviewPath = $arg.Substring(13); if (-not $reviewPath) { Write-LauncherError 'missing value for --review-dir'; return 2 }
            } elseif ($arg.StartsWith('--fail-on=')) {
                if ($mode -ne 'detect') { Write-LauncherError "unknown option: $arg"; return 2 }
                $failOn = $arg.Substring(10); if (-not $failOn) { Write-LauncherError 'missing value for --fail-on'; return 2 }
            } elseif ($arg -eq '--json' -and ($mode -eq 'clean' -or $mode -eq 'detect')) { $json = $true }
            elseif ($arg -eq '--agent-summary') { $agent = $true }
            elseif ($arg -eq '--sanitize' -and $mode -eq 'detect') { $sanitized = $true }
            elseif ($arg -eq '--force-output' -and $mode -ne 'review-merge') { $force = $true }
            elseif ($arg -eq '--prompt-guard' -and $mode -eq 'detect') { $promptGuard = $true }
            elseif ($arg -eq '--') {
                if ($index -ne $Arguments.Count - 1 -or $inputPath) { Write-LauncherError 'expected exactly one input file'; return 2 }
                $inputPath = $Arguments[$index]; $index++
            } elseif ($arg.StartsWith('-')) { Write-LauncherError "unknown option: $arg"; return 2 }
            else {
                if ($inputPath -or $mode -eq 'review-merge') { Write-LauncherError 'expected exactly one input file'; return 2 }
                $inputPath = $arg
            }
        }
        if ($json -and $agent) { Write-LauncherError '--json and --agent-summary are mutually exclusive'; return 2 }
        if ($outputPath -and $outputDirOption) { Write-LauncherError '--output and --output-dir are mutually exclusive'; return 2 }
        if ($mode -eq 'detect' -and -not $sanitized -and ($outputPath -or $outputDirOption -or $force)) {
            Write-LauncherError '--output and --force-output require --sanitize'; return 2
        }
        if ($mode -eq 'sanitize' -and -not $reviewPath) { Write-LauncherError 'sanitize requires --review-dir'; return 2 }
        if ($failOn -and @('high', 'medium') -cnotcontains $failOn) { Write-LauncherError 'invalid --fail-on value (choose high or medium)'; return 2 }
        if ($mode -ne 'review-merge') {
            if (-not $inputPath) { Write-LauncherError 'expected an input file'; return 2 }
            $inputDisplay = $inputPath
            try { $absoluteInput = Get-FullHostPath $inputPath }
            catch { Write-LauncherError 'cannot resolve input path'; return 2 }
            if (-not (Test-Path -LiteralPath $absoluteInput -PathType Leaf)) { Write-LauncherError "input not found: $absoluteInput"; return 2 }
            try { $inputPath = Resolve-HostPath $absoluteInput }
            catch { Write-LauncherError 'cannot resolve input path'; return 2 }
            $inDir = [IO.Path]::GetDirectoryName($inputPath); $inName = [IO.Path]::GetFileName($inputPath)
        } elseif (-not $reviewPath) { Write-LauncherError 'review-merge requires --review-dir'; return 2 }
        if ($reviewPath) {
            try { $reviewPath = Resolve-HostPath $reviewPath }
            catch { Write-LauncherError 'review parent directory does not exist'; return 2 }
            $comparison = if ($windowsHost) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
            $prefix = $reviewPath.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            if ($inputPath -and $inputPath.StartsWith($prefix, $comparison)) {
                Write-LauncherError 'review directory must not contain the input document'; return 2
            }
            if ($mode -eq 'detect') {
                try { New-Item -ItemType Directory -Path $reviewPath -Force -ErrorAction Stop | Out-Null }
                catch { Write-LauncherError 'cannot create review directory'; return 2 }
            } elseif (-not (Test-Path -LiteralPath $reviewPath -PathType Container)) {
                Write-LauncherError 'review directory does not exist'; return 2
            }
        }
        if ($mode -eq 'clean' -or $sanitized) {
            if ($outputPath) {
                try { $outputPath = Resolve-HostPath $outputPath }
                catch { Write-LauncherError 'output directory does not exist'; return 2 }
                if (Test-SamePath -First $inputPath -Second $outputPath -Windows $windowsHost) {
                    Write-LauncherError 'output path must differ from the input (no in-place cleaning)'; return 2
                }
                $outDir = [IO.Path]::GetDirectoryName($outputPath); $outName = [IO.Path]::GetFileName($outputPath)
            } else {
                if ($mode -eq 'clean' -and -not $outputDirOption) {
                    $extension = [IO.Path]::GetExtension($inName)
                    if (($inName.StartsWith('.') -and $inName.IndexOf('.', 1) -lt 0) -or $inName.EndsWith('.')) { $extension = '' }
                    $stem = if ($extension) { $inName.Substring(0, $inName.Length - $extension.Length) } else { $inName }
                    $defaultCleanOutput = Join-Path $inDir "$stem.cleaned$extension"
                }
                if (-not $outputDirOption) { $outputDirOption = $inDir }
                try { $outDir = Resolve-HostPath $outputDirOption }
                catch { Write-LauncherError 'output directory does not exist'; return 2 }
                if (-not (Test-Path -LiteralPath $outDir -PathType Container)) {
                    Write-LauncherError 'output directory does not exist'; return 2
                }
            }
        }
        if ($reportPath) {
            try { $reportPath = Resolve-HostPath $reportPath }
            catch { Write-LauncherError 'report directory does not exist'; return 2 }
            $reportIsInput = $inputPath -and (Test-SamePath -First $reportPath -Second $inputPath -Windows $windowsHost)
            if ($mode -eq 'clean' -or $sanitized) {
                if ($reportIsInput -or ($outputPath -and (Test-SamePath -First $reportPath -Second $outputPath -Windows $windowsHost))) {
                    Write-LauncherError 'report path must differ from the input and the output document'; return 2
                }
            } elseif ($reportIsInput) { Write-LauncherError 'report path must differ from the input document'; return 2 }
            $reportDir = [IO.Path]::GetDirectoryName($reportPath); $reportName = [IO.Path]::GetFileName($reportPath)
        }
    } else {
        while ($index -lt $Arguments.Count) { $passthrough += $Arguments[$index]; $index++ }
    }
    try { $docker = Initialize-Docker -Windows $windowsHost -TimeoutSeconds $timeout }
    catch { Write-LauncherError $_.Exception.Message; return 2 }
    $repo = Split-Path -Parent $PSScriptRoot
    if (-not $rebuild) { & $docker image inspect doc-sentinel:local >$null 2>$null; $rebuild = $LASTEXITCODE -ne 0 }
    if ($rebuild) {
        $dockerfile = Join-Path (Join-Path $repo 'containers') 'Dockerfile'
        if (-not (Test-Path -LiteralPath $dockerfile -PathType Leaf)) { Write-LauncherError "Dockerfile not found: $dockerfile"; return 2 }
        if (-not (Test-SubmodulesPopulated -Repo $repo)) { Write-LauncherError 'run: git submodule update --init --recursive'; return 2 }
        $buildArgs = @('build', '-f', $dockerfile, '-t', 'doc-sentinel:local', $repo)
        & $docker @buildArgs 2>&1 | ForEach-Object { [Console]::Error.WriteLine($_) }
        if ($LASTEXITCODE -ne 0) { return $LASTEXITCODE }
    }
    $hostIsLinux = -not $windowsHost -and (& uname -s) -eq 'Linux'
    $user = if ($hostIsLinux) { "$(id -u):$(id -g)" } else { '' }
    $inputDirectory = ''; $outputDirectory = ''; $reportDirectory = ''; $displayEnvironment = @()
    if ($mode -eq 'clean' -or $mode -eq 'detect' -or $mode -eq 'sanitize' -or $mode -eq 'review-merge') {
        if ($mode -ne 'review-merge') { $inputDirectory = $inDir }
        if ($mode -eq 'clean' -or $sanitized) { $outputDirectory = $outDir }
        if ($reportPath) { $reportDirectory = $reportDir }
        $sharedReportDir = ($mode -eq 'clean' -or $sanitized) -and $reportPath -and (Test-SamePath -First $reportDir -Second $outDir -Windows $windowsHost)
        $containerArgs = @('doc-sentinel:local', $mode)
        if ($mode -ne 'review-merge') { $containerArgs += "/work/in/$inName" }
        if ($mode -eq 'clean' -or $sanitized) {
            if ($outputPath) { $containerArgs += @('-o', "/work/out/$outName") }
            else { $containerArgs += @('--output-dir', '/work/out') }
            if ($force) { $containerArgs += '--force-output' }
        }
        if ($json) { $containerArgs += '--json' }
        if ($agent) { $containerArgs += '--agent-summary' }
        if ($reportPath) {
            $reportMount = if ($sharedReportDir) { '/work/out' } else { '/work/report' }
            $containerArgs += @('--report', "$reportMount/$reportName")
        }
        if ($failOn) { $containerArgs += @('--fail-on', $failOn) }
        if ($promptGuard) { $containerArgs += '--prompt-guard' }
        if ($reviewPath) { $containerArgs += @('--review-dir', '/work/review') }
        if ($mode -eq 'detect' -and $sanitized) { $containerArgs += '--sanitize' }
        if ($agent) {
            if ($inputPath) { $displayEnvironment += "DOC_SENTINEL_HOST_INPUT=$inputDisplay" }
            if ($outputPath) { $displayEnvironment += "DOC_SENTINEL_HOST_OUTPUT=$outputPath" }
            elseif ($mode -eq 'clean' -or $sanitized) { $displayEnvironment += "DOC_SENTINEL_HOST_OUTPUT_DIR=$outDir" }
            if ($reportPath) { $displayEnvironment += "DOC_SENTINEL_HOST_REPORT=$reportPath" }
            if ($reviewPath) { $displayEnvironment += "DOC_SENTINEL_HOST_REVIEW=$reviewPath" }
        }
    } else { $containerArgs = @('doc-sentinel:local', $mode) + $passthrough }
    $runArgs = @(New-DockerRunArguments -Linux $hostIsLinux -Windows $windowsHost -User $user `
        -InputDirectory $inputDirectory -OutputDirectory $outputDirectory -ReportDirectory $reportDirectory `
        -ReviewDirectory $reviewPath -DisplayEnvironment $displayEnvironment -ContainerArguments $containerArgs)
    # Invoke the native process at script scope, not in a captured function pipeline.
    # This keeps its stdout/stderr connected to the caller instead of capturing its output.
    $script:RunSpec = @{ Docker = $docker; Arguments = $runArgs; Mode = $mode; Output = $outputPath; DefaultCleanOutput = $defaultCleanOutput }
    return 0
}

$script:RunSpec = $null
$exitCode = Invoke-Launcher -Arguments @($args)
if ($exitCode -ne 0 -or -not $script:RunSpec) { exit $exitCode }
$docker = $script:RunSpec.Docker
$runArgs = $script:RunSpec.Arguments
& $docker @runArgs
$code = $LASTEXITCODE
if ($script:RunSpec.Mode -eq 'clean' -and $code -eq 0) {
    $cleanOutput = $script:RunSpec.Output
    if (-not $cleanOutput -and $script:RunSpec.DefaultCleanOutput) {
        try { $cleanOutput = Resolve-HostPath $script:RunSpec.DefaultCleanOutput }
        catch { $cleanOutput = $script:RunSpec.DefaultCleanOutput }
    }
    if ($cleanOutput) { [Console]::Error.WriteLine("Cleaned copy: $cleanOutput") }
}
exit $code
