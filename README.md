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
↑/↓ move  ⏎ jump  n new  x kill  a bg  r refresh   (Alt+`=here, Alt+1-9=tabs)
```

## Status signals (read straight from Claude Code)

| status    | meaning |
|-----------|---------|
| `waiting` | **awaiting your action** — permission prompt or your turn (yellow) |
| `busy`    | mid-response, working (cyan) |
| `idle`    | done, sitting at the prompt (dim green) |

Rows sort **waiting → busy → idle**, so whatever needs you floats to the top.

## Requirements

- Linux or WSL (reads `/proc`, so not macOS as-is)
- `python3` with `curses` (stdlib on Linux)
- `tmux` 3.2+
- Claude Code (`claude`) on your PATH

## Install

```bash
git clone <this-repo> ~/dev/fleet
cd ~/dev/fleet
./install.sh          # drops a `fleet` shim in ~/.local/bin pointing here
fleet                 # go
```

The repo *is* the install — presets, tmux config and logs live next to `fleet.py`,
so `git pull` updates the tool in place. Install the shim elsewhere with
`BIN_DIR=~/bin ./install.sh`.

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

## Commands

```
fleet up                 # open/attach fleet tmux + dashboard   (default)
fleet restart            # respawn the dashboard + reload config (agents untouched)
fleet restart --hard     # tear down the whole tmux server + every agent, start clean
fleet mon                # dashboard only (used as window 0; also fine by hand)
fleet ls                 # plain table, scriptable
fleet new [preset] [-C dir] [-n name] [prompt...]
fleet jump <pid|name>    # focus a session's tab
fleet presets            # list presets
fleet watch              # headless recorder: log lifecycle without the UI
fleet log [-n N]         # tail events.log + fleet.log + Claude's daemon.log
```

## Dashboard keys

```
↑/↓ (or j/k)  move            ⏎  jump to that session's tab
n  new session (modal: preset / dir / name / prompt — nothing launches until ⏎)
x  kill session (confirms; SIGTERM→SIGKILL the whole process tree)
a  show/hide background agents      r  refresh      q  quit to a shell
```

Background (headless subagent) sessions are **hidden by default** — they have no
terminal to jump to. Press `a` to show them; ⏎ on one opens `claude agents` so you
can attach.

## tmux niceties (fleet server only)

- **Mouse on**: wheel to scroll, click a pane, drag to select.
- **Copy to system clipboard**: drag-select or `y` in copy-mode — tries `clip.exe`
  (WSL), then `wl-copy`/`xclip`, plus OSC52.
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

`args` passes straight through to `claude`, so add your own freely. `presets.json`
also sets `tmux_session` / `tmux_socket` if you want different names.

For the `sandboxed` preset, put your Claude Code sandbox config in
`sandbox-settings.json` (ships as an empty `{}` stub). The schema is a top-level
`"sandbox"` key with `enabled`, `network.allowedDomains`/`deniedDomains`,
`filesystem.allowWrite`/`denyRead`/`allowRead`/`denyWrite`, `credentials`,
`allowUnsandboxedCommands`, `failIfUnavailable`. Sandboxing works in WSL/Linux
(bubblewrap), not native Windows; `--dangerously-skip-permissions` bypasses
prompts but **not** the sandbox.

## Typical loop

```bash
fleet                                   # land in the dashboard
# press `n` to launch, or from any pane:
fleet new yolo -C ~/dev/crm -n crm-refactor "start the auth refactor"
# work elsewhere; a row turns yellow when it needs you
# ↑/↓ to it, ⏎ to jump in, handle it, Alt+` back to the dashboard
```

Jump works for **any** session on the fleet tmux server, not just ones fleet
launched — pid→pane resolution walks the `/proc` parent chain.

## Observability

`events.log` records START / STATUS / END transitions, and flags `DIED WHILE BUSY`
when a session disappears mid-response — the tell for a crashed agent. `fleet.log`
holds fleet's own actions and any dashboard tracebacks (the TUI loop is
crash-wrapped so an error can't take down your tmux session). Both are gitignored.

## Notes / gotchas

- The dashboard only repaints when the visible data changes, so an open right-click
  menu isn't dismissed underneath you.
- Session liveness is checked against `/proc/<pid>` **plus** the process start time,
  so a recycled PID never shows up as a live session.
- `x` kills the session's whole descendant tree (subagents included) and cleans up
  the stale `~/.claude/sessions/*.json`.
- Never run destructive tmux commands (`kill-server`) against the `fleet` socket
  while sessions are live — use a throwaway `-L` socket for experiments.

## License

MIT — see [LICENSE](LICENSE).
