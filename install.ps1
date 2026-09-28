# fleet installer for Windows.
# fleet runs inside WSL (it drives tmux, which Windows doesn't have). This script
# installs it there and adds a `fleet` command you can run from PowerShell, cmd
# or Windows Terminal.
#
#   irm https://raw.githubusercontent.com/jordanshamai/fleet/main/install.ps1 | iex
#
# Requirements: WSL 2 with a Linux distro (`wsl --install` in an admin PowerShell,
# then reboot), and Claude Code installed INSIDE that distro.
$ErrorActionPreference = "Stop"
$Repo = "jordanshamai/fleet"

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Write-Host "fleet needs WSL. In an admin PowerShell run:  wsl --install   then reboot and re-run this."
    exit 1
}
$distros = @(wsl.exe -l -q 2>$null | ForEach-Object { $_.Trim([char]0).Trim() } | Where-Object { $_ })
if ($distros.Count -eq 0) {
    Write-Host "no WSL distro is installed. Run:  wsl --install -d Ubuntu   then re-run this."
    exit 1
}

Write-Host "installing fleet inside WSL ($($distros[0]))..."
wsl.exe -e bash -lc "curl -fsSL https://raw.githubusercontent.com/$Repo/main/install.sh | FLEET_YES=1 bash"
if ($LASTEXITCODE -ne 0) {
    Write-Host "the install inside WSL failed (see the output above)."
    exit 1
}

# Windows-side launcher: `fleet ...` -> runs ~/.local/bin/fleet inside WSL, same args.
$dir = Join-Path $env:LOCALAPPDATA "fleet"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$launcher = Join-Path $dir "fleet.cmd"
@'
@echo off
wsl.exe -e bash -lc "exec $HOME/.local/bin/fleet \"$@\"" fleet %*
'@ | Set-Content -Path $launcher -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $dir) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$dir", "User")
    Write-Host "added $dir to your user PATH (open a new terminal to pick it up)"
}

Write-Host ""
Write-Host "done. In a new terminal (Windows Terminal recommended):  fleet"
Write-Host "Claude Code must be installed inside WSL too:  wsl -e bash -lc 'claude --version'"
