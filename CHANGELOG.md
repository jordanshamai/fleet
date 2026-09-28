# Changelog

All notable changes to fleet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-28

First public release.

### Added
- Dashboard TUI reading Claude Code's live session registry: status per session
  (waiting / busy / idle / shell), jump to a session's tab, launch from presets,
  kill a session tree, tab colours that follow status.
- Isolated tmux server (`tmux -L fleet`) hosting the dashboard as window 0 plus
  one tab per agent, with mouse, clipboard and Alt-key navigation.
- macOS support: process facts come from one cached `ps` sweep instead of `/proc`.
- **Persistent sessions.** Every session on the fleet server is remembered in
  `~/.fleet/roster/`; `fleet up` restores parked sessions after a reboot with
  `claude --resume`, in their old directory and preset. `fleet restore`,
  `fleet resume`, `fleet forget`, `fleet ls -a`, and parked rows in the dashboard.
- **Themes.** `dark` and `light` palettes for the dashboard and the tmux bar,
  `fleet theme`, `FLEET_THEME`, and best-effort auto-detection from `COLORFGBG`.
- `fleet update` (moves a release install to the newest tag, or fast-forwards a
  dev checkout), a once-a-day background update check shown in the dashboard,
  `fleet version` / `--version`.
- Installers: `install.sh` for macOS/Linux (curl-able, installs missing deps on
  request, `--uninstall`), `install.ps1` for Windows via WSL.
- CI on Linux and macOS, and a tag-driven GitHub release workflow.

### Fixed
- `fleet new <preset> -C dir -n name "prompt"` rejected the prompt when it came
  after the options.
