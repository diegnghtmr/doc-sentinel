"""S12 PowerShell parity on Linux and explicit simulations of Windows-only helpers."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from test_launchers import ROOT, Harness, LauncherScenarios

PS_IMAGE = "mcr.microsoft.com/powershell:lts-debian-12"


@pytest.fixture(scope="session")
def real_docker():
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker is not available for PowerShell integration")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"}
    info = subprocess.run([docker, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, check=False)
    if info.returncode:
        pytest.skip("Docker daemon is not available for PowerShell integration")
    inspect = subprocess.run([docker, "image", "inspect", PS_IMAGE], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, check=False)
    if inspect.returncode:
        pytest.skip("PowerShell image is not cached; tests never pull images")
    return docker, env


class PowerShellHarness(Harness):
    def __init__(self, root, docker):
        super().__init__(root, backend="pwsh")
        self.docker, self.docker_env = docker

    def container(self, command, variables=None):
        cmd = [self.docker, "run", "--rm", "--network=none", "-v", f"{self.root}:/case",
               "-v", f"{ROOT}:/repo:ro"]
        for key, value in (variables or {}).items():
            cmd.extend(["-e", f"{key}={value}"])
        cmd.extend([PS_IMAGE, "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", *command])
        return subprocess.run(cmd, cwd=self.root, env=self.docker_env, text=True,
                              capture_output=True, timeout=45, check=False)

    def run(self, *args):
        variables = {"PATH": "/case/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                     "FAKE_LOG": "/case/calls.jsonl", **self.config}
        args = [self.path(a) if isinstance(a, Path) else str(a) for a in args]
        return self.container(["-File", "/case/repo/scripts/doc-sentinel.ps1", *args], variables)

    def unit(self, expression):
        variables = {"PATH": "/case/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                     "FAKE_LOG": "/case/calls.jsonl", **self.config}
        return self.container(["-Command", f". /repo/scripts/doc-sentinel.ps1 -NoMain; {expression}"], variables)


@pytest.fixture
def harness(real_docker):
    with tempfile.TemporaryDirectory(prefix="doc-sentinel-pwsh-") as directory:
        yield PowerShellHarness(Path(directory) / "case with spaces", real_docker)


@pytest.mark.integration
class TestPowerShellLauncher(LauncherScenarios):
    def test_rebuild_switch(self, harness):
        result = harness.run("-Rebuild", "version")
        assert result.returncode == 0, result.stderr
        assert len(harness.calls_named("build")) == 1

    @pytest.mark.parametrize("build,expected", [
        (22631, "Windows 11 detected (build 22631)"),
        (19045, "Warning: only Windows 11 is supported/tested (detected build 19045)"),
    ])
    def test_windows_build_simulation(self, harness, build, expected):
        result = harness.unit(f"Get-WindowsBuildMessage -Build {build}")
        assert (result.returncode, result.stdout.strip(), result.stderr) == (0, expected, "")

    def test_windows_container_mode_simulation(self, harness):
        result = harness.unit("Get-ContainerModeError -OSType 'windows'")
        assert (result.returncode, result.stdout.strip(), result.stderr) == (
            0, "Docker Desktop is in Windows-containers mode; switch to Linux containers", "")

    def test_docker_desktop_discovery_and_start_decisions_simulated(self, harness):
        pf = harness.root / "Program Files"
        exe = pf / "Docker" / "Docker" / "Docker Desktop.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"not an executable: simulated discovery only")
        expr = ("$p = Find-DockerDesktop -ProgramFiles @('/case/Program Files'); "
                "Write-Output $p; Get-DockerStartupAction -DockerOnPath $false "
                "-InfoSucceeded $false -DesktopPath $p; "
                "Get-DockerStartupAction -DockerOnPath $false -InfoSucceeded $false -DesktopPath ''")
        result = harness.unit(expr)
        assert (result.returncode, result.stdout.splitlines(), result.stderr) == (
            0, ["/case/Program Files/Docker/Docker/Docker Desktop.exe", "start", "missing"], "")

    def test_windows_start_and_linux_mode_check_simulated(self, harness):
        harness.config["FAKE_INFO_CODE"] = "1"
        expr = ("function Get-WindowsBuild { 22631 }; "
                "function Find-DockerDesktop { param($ProgramFiles) '/case/desktop.exe' }; "
                "function Find-DockerCli { param($DesktopPath) '/case/bin/docker' }; "
                "function Start-Process { param($FilePath) $env:FAKE_INFO_CODE = '0' }; "
                "function Start-Sleep { param($Milliseconds) }; "
                "Initialize-Docker -Windows $true -TimeoutSeconds 2")
        result = harness.unit(expr)
        assert (result.returncode, result.stdout.strip(), result.stderr) == (
            0, "/case/bin/docker", "Windows 11 detected (build 22631)\nWaiting for Docker Desktop to start...\n")
        assert harness.calls_named("info") == [["info"], ["info"], ["info", "--format", "{{.OSType}}"]]

    def test_windows_missing_cli_on_path_starts_desktop_even_if_fallback_is_ready(self, harness):
        expr = ("function Get-WindowsBuild { 22631 }; "
                "function Find-DockerDesktop { param($ProgramFiles) '/case/desktop.exe' }; "
                "function Find-DockerCli { param($DesktopPath) '/case/bin/docker' }; "
                "function Test-DockerOnPath { $false }; "
                "function Start-Process { param($FilePath) $env:STARTED = 'started' }; "
                "function Start-Sleep { param($Milliseconds) }; "
                "Initialize-Docker -Windows $true -TimeoutSeconds 2; [Console]::Out.WriteLine($env:STARTED)")
        result = harness.unit(expr)
        assert (result.returncode, result.stdout.splitlines(), result.stderr) == (
            0, ["/case/bin/docker", "started"],
            "Windows 11 detected (build 22631)\nWaiting for Docker Desktop to start...\n")
        assert harness.calls_named("info") == [["info"], ["info"], ["info", "--format", "{{.OSType}}"]]

    def test_windows_mode_refusal_in_initialization_simulated(self, harness):
        harness.config["FAKE_OSTYPE"] = "windows"
        expr = ("function Get-WindowsBuild { 22631 }; "
                "function Find-DockerDesktop { param($ProgramFiles) '/case/desktop.exe' }; "
                "function Find-DockerCli { param($DesktopPath) '/case/bin/docker' }; "
                "try { Initialize-Docker -Windows $true -TimeoutSeconds 2 } "
                "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 2 }")
        result = harness.unit(expr)
        assert (result.returncode, result.stdout, result.stderr) == (
            2, "", ("Windows 11 detected (build 22631)\n"
                    "Docker Desktop is in Windows-containers mode; switch to Linux containers\n"))
        assert not harness.calls_named("run")

    def test_windows_full_run_command_simulated(self, harness):
        expr = ("$a = New-DockerRunArguments -Linux $false -Windows $true -User '' "
                "-InputDirectory 'C:\\Users\\Ana María\\Docs' "
                "-OutputDirectory 'C:\\Users\\Ana María\\Docs' -ReportDirectory 'D:\\Audits' "
                "-ContainerArguments @('doc-sentinel:local','clean','/work/in/informe final.docx',"
                "'-o','/work/out/informe final.cleaned.docx','--report','/work/report/audit.json'); "
                "ConvertTo-Json -InputObject @($a) -Compress")
        import json
        result = harness.unit(expr)
        assert result.returncode == 0 and result.stderr == ""
        assert json.loads(result.stdout) == [
            "run", "--rm", "--network=none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "-e", "DOC_SENTINEL_NO_NETNS=1",
            "-v", "C:\\Users\\Ana María\\Docs:/work/in:ro", "-v", "C:\\Users\\Ana María\\Docs:/work/out:rw",
            "-v", "D:\\Audits:/work/report:rw", "doc-sentinel:local", "clean", "/work/in/informe final.docx",
            "-o", "/work/out/informe final.cleaned.docx", "--report", "/work/report/audit.json"]

    def test_windows_paths_mount_and_case_insensitive_comparison_simulated(self, harness):
        expr = ("Convert-HostPathToMount -HostDirectory 'C:\\Users\\Ana María\\Docs' "
                "-ContainerDirectory '/work/in' -ReadOnly $true; "
                "Test-SamePath -First 'C:\\Users\\Ana María\\Docs\\informe final.docx' "
                "-Second 'c:\\users\\ANA MARÍA\\docs\\INFORME FINAL.docx' -Windows $true")
        result = harness.unit(expr)
        assert (result.returncode, result.stdout.splitlines(), result.stderr) == (
            0, ["C:\\Users\\Ana María\\Docs:/work/in:ro", "True"], "")
