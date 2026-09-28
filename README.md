# fleet — run a fleet of Claude Code sessions from one terminal

`fleet` watches Claude Code's own live session registry (`~/.claude/sessions/*.json`)
and hosts your sessions as tabs in an **isolated tmux server**, so you can run many
agents at once, see at a glance which one needs you, and jump straight to it — all
inside a single terminal window.

```
 CLAUDE FLEET   waiting:1  busy:2  idle:1  shown:4   14:22:07
  STATUS   NAME                   KIND  AGE   DIR
▶ waiting  crm-refactor           inte    4s  ~/dev/crm
  busy     docs-sweep             inte   31s  ~/dev/docs
  busy     ai-infra-graph         inte    2s  ~/dev/personal/ai-infra-graph
  idle     scratch                inte    1h  ~
↑/↓ move  ⏎ jump/resume  n new  x kill/forget  a bg  r refresh   (Alt+`=here, Alt+1-9=tabs)
```

## Status signals (read straight from Claude Code)

| status    | meaning |
|-----------|---------|
| `waiting` | **awaiting your action** — permission prompt or your turn (yellow) |
| `busy`    | mid-response, working (cyan) |
| `idle`    | done, sitting at the prompt (dim green) |
| `shell`   | pane dropped to a shell, not at the claude prompt (grey) |
| `parked`  | remembered by fleet but not running — a reboot left it behind (dim, listed last) |

Rows sort **waiting → busy → idle → shell**, so whatever needs you floats to the top.
Parked rows sit underneath; ⏎ on one brings it back.

## Requirements

- macOS, Linux, or Windows via WSL
- `python3` with `curses` (stdlib on macOS and most Linux)
- `tmux` 3.2+ (the installer offers to install it)
- Claude Code (`claude`) on your PATH, run once

## Install

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/jordanshamai/fleet/main/install.sh | bash
```

That clones the newest release into `~/.fleet/app`, offers to install `tmux`/`git`
if they're missing, and puts a `fleet` command in `~/.local/bin`. Flags: `-y`
(answer yes), `--main` (track main instead of releases), `--uninstall`.
`BIN_DIR=~/bin` and `FLEET_HOME=...` are honoured.

**Windows** — fleet runs inside WSL. In PowerShell:

```powershell
irm https://raw.githubusercontent.com/jordanshamai/fleet/main/install.ps1 | iex
```

That runs the Linux installer inside your default WSL distro and adds a `fleet`
command to Windows so `fleet` works from Windows Terminal, PowerShell or cmd.
Claude Code has to be installed inside WSL as well.

**From a checkout** (hacking on fleet): `./install.sh` points the `fleet` command
at *that* checkout instead of cloning.

## Update

```bash
fleet update            # move to the newest release (restarts the dashboard)
fleet update --check    # just say whether one exists
fleet update --main     # follow the tip of main instead
```

The dashboard checks GitHub for a new release once a day in the background and
shows `⬆ vX.Y.Z available: fleet update` in its title bar. Turn that off with
`"update_check": false` in `~/.fleet/config.json`. A checkout on a branch is
fast-forwarded rather than moved to a tag, so a dev install keeps working.
`fleet update` refuses to touch a checkout with uncommitted changes.

## Themes

The dashboard and the tmux status bar share one palette. `dark` is the default;
`light` is tuned for white/light backgrounds.

```bash
fleet theme light       # or dark, or auto — saved in ~/.fleet/config.json, applied live
fleet theme             # show the current one
```

`FLEET_THEME=light` overrides the config for one run. `auto` reads the
`COLORFGBG` variable that some terminals export, and falls back to dark —
terminals rarely announce their background, so set it explicitly if it guesses
wrong. Palettes live in `THEMES` in `fleet.py` if you want to add your own.

## Start here

```bash
fleet          # = `fleet up`: opens/attaches the fleet tmux session
```

That runs an **isolated tmux server** (`tmux -L fleet`, config `fleet.tmux.conf`) —
it never touches your default tmux server or any `~/.tmux.conf` you have. From a
fresh terminal tab, just run `fleet`.

## Layout: one terminal, dashboard + agent tabs

- The **dashboard is window 0**, shown as the yellow **FLEET** label at bottom-left.
  It's hidden from the numbered tab list — your agents are tabs 1-9.
- Window 0 re-runs the dashboard in a loop, so it can't be closed by accident
  (quitting the TUI with `q` drops you to a shell in that tab; `prefix &` on
  window 0 is refused too).
- Get back to it with **Alt+`**, by clicking the FLEET label, or Alt+←.

To pick up a new `fleet.py` or edited `fleet.tmux.conf`, run **`fleet restart`** —
it respawns window 0 and reloads the config, leaving your agent tabs running.
Use **`fleet restart --hard`** to kill the tmux server and every agent and come
back to an empty fleet (it lists what it'll terminate and asks first; `-y` skips
the prompt).

## Surviving a reboot

Every session on the fleet server is remembered in `~/.fleet/roster/<sessionId>.json`
(tab name, directory, preset, and the id Claude Code stores the conversation under).
Claude Code keeps the transcript itself, so fleet only needs to know *what to
`--resume` where*:

- `fleet new` mints the session id up front (`claude --session-id …`), so the
  roster row exists before claude starts. A `claude` you typed by hand in a fleet
  tab is **adopted** by the dashboard a second later.
- Quit or kill a session while fleet is watching and its row is marked `closed`
  (kept 30 days as history). Reboot, or lose the tmux server, and rows stay
  `active` with no process behind them — that's **parked**.
- When **`fleet up` has to create the tmux server, it restores every parked
  session**: same tab name, same directory, same preset flags, `claude --resume
  <id>`. Then it lands you on the dashboard. A tab that never got a message has
  no transcript, so it's started fresh with the same setup instead.
- `fleet restart --hard` closes the rows first, so it really does come back empty.

```
fleet ls                 # live rows + parked ones;  -a adds closed history
fleet restore            # relaunch every parked session now
fleet resume <name|id>   # relaunch one (parked or closed); jumps to it if it's live
fleet forget <name|id>   # drop a parked/closed row (the transcript stays in ~/.claude)
```

In the dashboard: ⏎ on a parked row resumes it, `x` forgets it, `R` restores all.
Nothing launches on login — fleet only relaunches when you run it.

## Commands

```
fleet up                 # open/attach fleet tmux + dashboard   (default;
                         #  restores parked sessions when it creates the server)
fleet restart            # respawn the dashboard + reload config (agents untouched)
fleet restart --hard     # tear down the whole tmux server + every agent, start clean
fleet mon                # dashboard only (used as window 0; also fine by hand)
fleet ls                 # plain table, scriptable
fleet new [preset] [-C dir] [-n name] [prompt...]
fleet jump <pid|name>    # focus a session's tab
fleet restore            # relaunch every parked session
fleet resume <name|id>   # relaunch one parked/closed session
fleet forget <name|id>   # drop a session from the roster
fleet presets            # list presets
fleet theme [dark|light|auto]
fleet update [--check] [--main]
fleet version
fleet watch              # headless recorder: log lifecycle without the UI
fleet log [-n N]         # tail events.log + fleet.log + Claude's daemon.log
```

## Dashboard keys

```
↑/↓ (or j/k)  move            ⏎  jump to that session's tab (or resume a parked one)
n  new session (modal: preset / dir / name / prompt — nothing launches until ⏎)
x  kill session (confirms; SIGTERM→SIGKILL the whole process tree) / forget a parked one
a  show/hide background agents      R  restore all parked      r  refresh      q  quit to a shell
```

Background (headless subagent) sessions are **hidden by default** — they have no
terminal to jump to. Press `a` to show them; ⏎ on one opens `claude agents` so you
can attach.

## tmux niceties (fleet server only)

- **Mouse on**: wheel to scroll, click a pane, drag to select.
- **Copy to system clipboard**: drag-select or `y` in copy-mode — tries `pbcopy`
  (macOS), then `clip.exe` (WSL), then `wl-copy`/`xclip`, plus OSC52.
- **Alt+←/→** prev/next tab · **Alt+1..9** jump to agent tab N · **Alt+`** dashboard.
- **Alt+,** / **Alt+.** move the current tab left/right. Right-click a tab (press
  and hold) for swap/rename/new/kill.
- Tabs are **colored by session status**, same palette as the dashboard rows.
- 50k lines of scrollback; window indices stay gap-free.
- `prefix r` reloads `fleet.tmux.conf`.

## Presets (edit `presets.json`)

| name       | effect |
|------------|--------|
| default    | normal permissions |
| yolo       | `--dangerously-skip-permissions` |
| auto       | `--permission-mode acceptEdits` |
| plan       | `--permission-mode plan` (read-only) |
| sandboxed  | skip-perms + `--settings sandbox-settings.json` |

On macOS the `sandboxed` preset still runs, but see the sandbox note below.

`args` passes straight through to `claude`, so add your own freely. `presets.json`
also sets `tmux_session` / `tmux_socket` if you want different names.

For the `sandboxed` preset, put your Claude Code sandbox config in
`sandbox-settings.json` (ships as an empty `{}` stub). The schema is a top-level
`"sandbox"` key with `enabled`, `network.allowedDomains`/`deniedDomains`,
`filesystem.allowWrite`/`denyRead`/`allowRead`/`denyWrite`, `credentials`,
`allowUnsandboxedCommands`, `failIfUnavailable`. Sandboxing works in WSL/Linux
(bubblewrap) and on macOS (Seatbelt), not native Windows;
`--dangerously-skip-permissions` bypasses prompts but **not** the sandbox.

## Typical loop

```bash
fleet                                   # land in the dashboard
# press `n` to launch, or from any pane:
fleet new yolo -C ~/dev/crm -n crm-refactor "start the auth refactor"
# work elsewhere; a row turns yellow when it needs you
# ↑/↓ to it, ⏎ to jump in, handle it, Alt+` back to the dashboard
```

Jump works for **any** session on the fleet tmux server, not just ones fleet
launched — pid→pane resolution walks the parent-pid chain.

## Observability

`events.log` records START / STATUS / END transitions, and flags `DIED WHILE BUSY`
when a session disappears mid-response — the tell for a crashed agent. `fleet.log`
holds fleet's own actions and any dashboard tracebacks (the TUI loop is
crash-wrapped so an error can't take down your tmux session). Both are gitignored.

## Notes / gotchas

- The dashboard only repaints when the visible data changes, so an open right-click
  menu isn't dismissed underneath you.
- Session liveness is checked against the pid **plus** its start time, so a recycled
  PID never shows up as a live session. Those facts come from `/proc` on Linux and
  from one cached `ps` sweep on macOS — Claude Code records `procStart` in UTC, which
  is why the macOS reader asks `ps` for UTC too.
- `x` kills the session's whole descendant tree (subagents included) and cleans up
  the stale `~/.claude/sessions/*.json`.
- Never run destructive tmux commands (`kill-server`) against the `fleet` socket
  while sessions are live — use a throwaway `-L` socket for experiments.

## Versioning and releases

fleet uses [semantic versioning](https://semver.org/); the version lives in
`__version__` in `fleet.py` and every release is a `vX.Y.Z` tag with notes in
[CHANGELOG.md](CHANGELOG.md). Pushing a tag runs the release workflow, which
checks the tag against `__version__`, runs the tests, and publishes a GitHub
release with the changelog section and a source tarball. CI runs the tests on
Linux and macOS for every push.

To cut a release: bump `__version__`, move the `Unreleased` notes under a new
heading in `CHANGELOG.md`, commit, then `git tag vX.Y.Z && git push --tags`.

## License

MIT — see [LICENSE](LICENSE).
