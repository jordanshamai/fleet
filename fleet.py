#!/usr/bin/env python3
"""fleet — monitor and launch multiple Claude Code sessions.

Reads Claude Code's own live session registry at ~/.claude/sessions/*.json.
Each running session writes {pid, name, cwd, status, kind, updatedAt, procStart, ...}.
  status: busy    -> mid-response (working)
          waiting -> awaiting your action (permission prompt / your turn)
          idle    -> done, sitting at the prompt

Subcommands:
  fleet mon                 live curses dashboard (default)
  fleet ls                  plain table
  fleet new [preset] ...    launch a preconfigured claude in a new tmux window
  fleet jump <pid|name>     focus a session's tmux window
  fleet presets             list presets
"""
import os, sys, json, glob, time, subprocess, argparse

HOME = os.path.expanduser("~")
SESS_DIR = os.path.join(HOME, ".claude", "sessions")
# fleet's own files (presets, tmux conf, logs) live next to this script, wherever
# you cloned it — realpath so a `fleet` symlink on PATH resolves to the checkout.
FLEET_DIR = os.path.dirname(os.path.realpath(__file__))
PRESETS_FILE = os.path.join(FLEET_DIR, "presets.json")
TMUX_CONF = os.path.join(FLEET_DIR, "fleet.tmux.conf")

STATUS_ORDER = {"waiting": 0, "busy": 1, "idle": 2}


def tmux_base():
    """`tmux -L <socket>` — fleet uses an isolated tmux server."""
    cfg = load_config()
    return ["tmux", "-L", cfg.get("tmux_socket", "fleet")]


def tmux_session_name():
    return load_config().get("tmux_session", "fleet")


# ---------------------------------------------------------------- logging ---
FLEET_LOG = os.path.join(FLEET_DIR, "fleet.log")     # fleet's own actions + errors
EVENTS_LOG = os.path.join(FLEET_DIR, "events.log")   # session lifecycle transitions
DAEMON_LOG = os.path.join(HOME, ".claude", "daemon.log")


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def flog(level, msg):
    """Append to fleet's app log (launches, kills, jumps, errors/tracebacks)."""
    try:
        with open(FLEET_LOG, "a") as f:
            f.write(f"{_ts()} [{level}] {msg}\n")
    except Exception:
        pass


def elog(event, s, detail=""):
    """Append a session lifecycle event."""
    try:
        with open(EVENTS_LOG, "a") as f:
            f.write(f"{_ts()} {event:6} {str(s.get('name','?'))[:28]:28} "
                    f"pid={s.get('pid'):<7} status={s.get('status','?'):8} "
                    f"kind={s.get('kind','?'):5} {detail}\n")
    except Exception:
        pass


def diff_and_log(prev, cur):
    """Record what changed between two session snapshots (START/STATUS/END).
    An END while status was 'busy' is a strong 'died mid-work / crashed' signal."""
    prev_by = {s["pid"]: s for s in prev}
    cur_by = {s["pid"]: s for s in cur}
    for pid, s in cur_by.items():
        if pid not in prev_by:
            elog("START", s, s.get("cwd", ""))
        elif prev_by[pid]["status"] != s["status"]:
            elog("STATUS", s, f"({prev_by[pid]['status']} -> {s['status']})")
    for pid, s in prev_by.items():
        if pid not in cur_by:
            note = "(DIED WHILE BUSY)" if s.get("status") == "busy" else f"(was {s.get('status')})"
            elog("END", s, note)


# ------------------------------------------------------------------ config ---
def load_config():
    try:
        with open(PRESETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"tmux_session": "fleet", "presets": {"default": {"desc": "", "args": []}}}


# ------------------------------------------------------------- proc helpers ---
def proc_starttime(pid):
    """Field 22 of /proc/pid/stat (starttime in clock ticks), as str. None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # comm can contain spaces/parens; everything after the last ')' is space-split
        rest = data[data.rfind(")") + 2:].split()
        return rest[19]  # field 22 = index 19 after (pid, comm) removed
    except Exception:
        return None


def is_alive(pid, procstart=None):
    if not os.path.exists(f"/proc/{pid}"):
        return False
    if procstart:  # guard against PID reuse
        st = proc_starttime(pid)
        if st is not None and str(procstart) != str(st):
            return False
    return True


def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def descendants(pid):
    """All live descendant pids of `pid` (children, grandchildren, ...)."""
    children = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pp = ppid_of(int(d))
        if pp is not None:
            children.setdefault(pp, []).append(int(d))
    out, stack = [], list(children.get(pid, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, []))
    return out


def kill_tree(pid):
    """SIGTERM then SIGKILL a session and all its descendants (e.g. bg subagents).
    Also removes their stale ~/.claude/sessions/*.json so they leave the dashboard."""
    import signal
    targets = [pid] + descendants(pid)      # kill children before/with the parent
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = [p for p in targets if os.path.exists(f"/proc/{p}")]
        if not alive:
            break
        for p in alive:
            try:
                os.kill(p, sig)
            except Exception:
                pass
        # brief grace period for SIGTERM before escalating
        if sig == signal.SIGTERM:
            for _ in range(10):
                if not any(os.path.exists(f"/proc/{p}") for p in targets):
                    break
                time.sleep(0.05)
    # tidy up any session files left behind by the killed pids
    for p in targets:
        fp = os.path.join(SESS_DIR, f"{p}.json")
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass
    return len(targets)


# ---------------------------------------------------------------- sessions ---
def read_sessions(alive_only=True):
    out = []
    for path in glob.glob(os.path.join(SESS_DIR, "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        pid = d.get("pid")
        if pid is None:
            continue
        alive = is_alive(pid, d.get("procStart"))
        if alive_only and not alive:
            continue
        out.append({
            "pid": pid,
            "name": d.get("name") or f"pid-{pid}",
            "cwd": d.get("cwd", ""),
            "status": d.get("status", "?"),
            "kind": d.get("kind", "?"),
            "sessionId": d.get("sessionId") or d.get("session_id"),
            "updated": d.get("statusUpdatedAt") or d.get("updatedAt") or 0,
            "alive": alive,
        })
    # stable order: by status, then name, then pid — rows don't jump around
    # every second (unlike sorting by last-updated time).
    out.sort(key=lambda s: (STATUS_ORDER.get(s["status"], 9), s["name"].lower(), s["pid"]))
    return out


def short_dir(p):
    if p.startswith(HOME):
        p = "~" + p[len(HOME):]
    return p


def age(ms):
    if not ms:
        return "-"
    secs = max(0, int(time.time() - ms / 1000))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# -------------------------------------------------------------------- tmux ---
def in_tmux():
    return bool(os.environ.get("TMUX"))


def tmux_panes():
    """Return list of dicts for every pane across all tmux sessions."""
    try:
        fmt = "#{pane_pid}\t#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}"
        out = subprocess.check_output(tmux_base() + ["list-panes", "-a", "-F", fmt],
                                      text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            panes.append({"pane_pid": int(parts[0]), "session": parts[1],
                          "window": parts[2], "wname": parts[3], "pane_id": parts[4]})
    return panes


def find_pane_for_pid(pid, pane_pids=None):
    """Map a claude session pid to its tmux pane by walking the /proc parent chain.
    Pass a prebuilt {pane_pid: pane} map to avoid re-listing panes per call."""
    if pane_pids is None:
        pane_pids = {p["pane_pid"]: p for p in tmux_panes()}
    cur, hops = pid, 0
    while cur and cur > 1 and hops < 40:
        if cur in pane_pids:
            return pane_pids[cur]
        cur = ppid_of(cur)
        hops += 1
    return None


def tmux_window_names(sessions):
    """{session_pid: tmux window (tab) name} for sessions hosted in a tmux window,
    so a tab renamed from the UI shows up in the dashboard."""
    pane_pids = {p["pane_pid"]: p for p in tmux_panes()}
    out = {}
    for s in sessions:
        pane = find_pane_for_pid(s["pid"], pane_pids)
        if pane:
            out[s["pid"]] = pane["wname"]
    return out


def jump_to(pid):
    """Switch this terminal to the tmux window/pane hosting `pid`. Returns (ok, message)."""
    pane = find_pane_for_pid(pid)
    if not pane:
        return False, "no terminal for this session (background agent? try `claude agents`)"
    target = f"{pane['session']}:{pane['window']}"
    q = {"check": False, "stderr": subprocess.DEVNULL}   # never leak tmux stderr into curses
    try:
        if in_tmux():
            subprocess.run(tmux_base() + ["switch-client", "-t", target], **q)
        else:
            subprocess.run(tmux_base() + ["select-window", "-t", target], **q)
        subprocess.run(tmux_base() + ["select-pane", "-t", pane["pane_id"]], **q)
        return True, f"→ {pane['wname']} (tab {pane['window']})"
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------- launch ---
def build_claude_cmd(preset_name, name=None, prompt=None):
    """Return the `claude ...` argv for a preset (+ optional name/prompt)."""
    preset = load_config().get("presets", {})[preset_name]
    cmd = ["claude", *preset.get("args", [])]
    settings = preset.get("settings")
    if settings:
        spath = settings if os.path.isabs(settings) else os.path.join(FLEET_DIR, settings)
        cmd += ["--settings", spath]
    if name:
        cmd += ["--name", name]
    if prompt:
        cmd.append(prompt)
    return cmd


def wrap_inner(cmd):
    """Wrap argv so the pane stays open (shows a prompt) after claude exits."""
    quoted = " ".join(shell_quote(c) for c in cmd)
    return f"{quoted}; echo; echo '[claude exited — press enter to close]'; read; exec $SHELL"


def fleet_server_up():
    return subprocess.run(tmux_base() + ["has-session", "-t", tmux_session_name()],
                          stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL).returncode == 0


def spawn_window(cwd, win_name, cmd):
    """Open a new tmux window in the fleet session running `cmd` (becomes active).
    Targets the fleet session explicitly so it works from a standalone dashboard."""
    if not fleet_server_up():
        return "fleet tmux not running — start it with `fleet up` first"
    inner = wrap_inner(cmd)
    flog("INFO", f"launch '{win_name}' in {cwd}: {' '.join(cmd)}")
    # NOTE the trailing ':' — `new-window -t` takes a *target window*, so a bare
    # "fleet" matches a TAB named 'fleet' (or the current window) and tmux then
    # tries to create at that exact index → "create window failed: index N in use".
    # "fleet:" unambiguously means the session, and tmux picks the next free index.
    r = subprocess.run(tmux_base() + ["new-window", "-t", tmux_session_name() + ":",
                       "-c", cwd, "-n", win_name, inner],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # never let tmux's stderr vanish into the curses UI — log it and show it
        err = (r.stderr or r.stdout or "").strip() or f"tmux exited {r.returncode}"
        flog("ERROR", f"launch '{win_name}' failed: {err}")
        return f"launch failed: {err}"
    return None


def _resume_bg(target, fork=False):
    """Pull a background agent up in a new fleet tab.
    A *running* bg agent can't be `--resume`'d, so by default we open the agent
    view (`claude agents`) to find & attach to the live one. With fork=True we
    branch a copy of its conversation (`--resume <id> --fork-session`)."""
    if not fleet_server_up():
        return "fleet tmux not running — start it with `fleet up` first"
    cwd = target.get("cwd") if os.path.isdir(target.get("cwd") or "") else HOME
    sid = target.get("sessionId")
    if fork and sid:
        spawn_window(cwd, f"fork:{target['name'][:14]}",
                     ["claude", "--resume", sid, "--fork-session"])
        return f"forked a copy of '{target['name'][:20]}' in new tab"
    spawn_window(cwd, "agents", ["claude", "agents"])
    return f"opened `claude agents` — attach to '{target['name'][:24]}' from the list"


# window colours matching the dashboard rows (colour3=yellow, 6=cyan, 2=green)
_WIN_STYLE = {"waiting": "fg=colour3,bold", "busy": "fg=colour6", "idle": "fg=colour2,dim"}


def apply_window_colors(sessions, cache):
    """Colour each tmux tab in the status bar by the status of the session in it.
    `cache` (a dict) is kept across calls so we only issue tmux commands on change."""
    win_status = {}
    for s in sessions:
        pane = find_pane_for_pid(s["pid"])
        if not pane:
            continue
        key = f"{pane['session']}:{pane['window']}"
        cur = win_status.get(key)
        if cur is None or STATUS_ORDER.get(s["status"], 9) < STATUS_ORDER.get(cur, 9):
            win_status[key] = s["status"]     # highest-priority status wins (waiting>busy>idle)
    try:
        out = subprocess.check_output(
            tmux_base() + ["list-windows", "-a", "-F", "#{session_name}:#{window_index}"],
            text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return
    for key in out.split():
        style = _WIN_STYLE.get(win_status.get(key))     # None = window has no session
        if cache.get(key) == style:
            continue
        cache[key] = style
        if style is None:
            # no session in this window (e.g. the dashboard tab): drop our override
            # so it inherits the config's normal/current-tab styling.
            for opt in ("window-status-style", "window-status-current-style"):
                subprocess.run(tmux_base() + ["set", "-uw", "-t", key, opt],
                               stderr=subprocess.DEVNULL)
        else:
            subprocess.run(tmux_base() + ["set", "-w", "-t", key, "window-status-style", style],
                           stderr=subprocess.DEVNULL)
            subprocess.run(tmux_base() + ["set", "-w", "-t", key, "window-status-current-style",
                           style + ",reverse"], stderr=subprocess.DEVNULL)


# The dashboard runs in window 0 and auto-relaunches if it exits, so the FLEET slot
# is always the dashboard and can't be closed by quitting/crashing the TUI.
DASH_CMD = "while :; do fleet mon; sleep 0.3; done"


def _server_sessions():
    """All session names on the fleet tmux server ([] if the server isn't running)."""
    try:
        out = subprocess.check_output(tmux_base() + ["list-sessions", "-F", "#{session_name}"],
                                      text=True, stderr=subprocess.DEVNULL)
        return [s for s in out.split("\n") if s.strip()]
    except Exception:
        return []


def ensure_fleet_session():
    """Guarantee exactly one fleet session named `tmux_session_name()`, with the
    always-on dashboard as window 0. If the server is already running but the session
    was renamed, ADOPT it (rename) instead of creating a duplicate — otherwise you'd
    end up with two 'fleet' sessions showing different tabs."""
    sess = tmux_session_name()
    existing = _server_sessions()
    if sess not in existing:
        if existing:
            # server up but our name isn't here (it was renamed) → adopt to avoid a dup
            subprocess.run(tmux_base() + ["rename-session", "-t", existing[0], sess],
                           stderr=subprocess.DEVNULL)
        else:
            subprocess.run(tmux_base() + ["-f", TMUX_CONF, "new-session", "-d",
                            "-s", sess, "-n", "dash", DASH_CMD])
    # tell the server where its config lives so `prefix r` can reload it from
    # whatever directory fleet was cloned into
    subprocess.run(tmux_base() + ["set", "-g", "@fleet_conf", TMUX_CONF],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


def dash_target():
    """tmux target for the dashboard window (always index 0)."""
    return f"{tmux_session_name()}:0"


def _fleet_hosted_sessions():
    """Live claude sessions whose pane lives on the FLEET tmux server.
    Used by the hard restart so we only ever touch fleet's own agents — never a
    claude running in some other terminal."""
    pane_pids = {p["pane_pid"]: p for p in tmux_panes()}     # -L fleet only
    out = []
    for s in read_sessions():
        pane = find_pane_for_pid(s["pid"], pane_pids)
        if pane:
            out.append({**s, "window": pane["window"], "wname": pane["wname"]})
    return out


def cmd_restart(hard=False, yes=False):
    """Restart the dashboard (default) or the whole fleet tmux server (--hard)."""
    if not fleet_server_up():
        print("fleet tmux isn't running — starting it fresh")
        cmd_up()
        return 0

    if not hard:
        # soft: reload the config and respawn window 0, so a new fleet.py / new
        # fleet.tmux.conf takes effect. Agent tabs keep running, untouched.
        for argv in (["source-file", TMUX_CONF],
                     ["set", "-g", "@fleet_conf", TMUX_CONF]):
            subprocess.run(tmux_base() + argv,
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        try:
            wins = subprocess.check_output(
                tmux_base() + ["list-windows", "-t", tmux_session_name() + ":",
                               "-F", "#{window_index}"],
                text=True, stderr=subprocess.DEVNULL).split()
        except Exception:
            wins = []
        if "0" in wins:
            argv = ["respawn-window", "-k", "-t", dash_target(), DASH_CMD]
        else:   # dashboard window went missing — recreate it at index 0
            argv = ["new-window", "-d", "-t", dash_target(), "-n", "dash", DASH_CMD]
        r = subprocess.run(tmux_base() + argv, capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip() or f"tmux exited {r.returncode}"
            flog("ERROR", f"restart dashboard failed: {err}")
            print(f"restart failed: {err}")
            return 1
        flog("INFO", "dashboard restarted (soft)")
        print("dashboard restarted, config reloaded — agent tabs untouched")
        print("(for a full teardown incl. every agent: fleet restart --hard)")
        return 0

    # hard: tear down the tmux server and everything running on it
    hosted = _fleet_hosted_sessions()
    print(f"HARD RESTART — kills the fleet tmux server and every tab on it.")
    if hosted:
        print(f"\n{len(hosted)} claude session(s) will be terminated:")
        for s in hosted:
            print(f"  tab {s['window']:>2}  {s['status']:8} {s['wname'][:24]:24} "
                  f"{short_dir(s['cwd'])}")
    else:
        print("\nno live claude sessions on the fleet server.")
    if in_tmux():
        print("\nyou're inside the fleet tmux — this terminal will drop back to its\n"
              "shell when the server dies. Run `fleet` to come back up clean.")
    if not yes:
        try:
            if input("\nproceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted (no tty — pass -y to skip this prompt)")
            return 1

    # SIGTERM→SIGKILL each session's process tree first: kills bg subagents that
    # would otherwise outlive the server, and clears their stale session files
    for s in hosted:
        try:
            kill_tree(s["pid"])
        except Exception as e:
            flog("ERROR", f"restart --hard: kill {s['pid']} failed: {e!r}")
    flog("INFO", f"hard restart: killed {len(hosted)} session(s), killing tmux server")
    subprocess.run(tmux_base() + ["kill-server"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    # if we were running inside that server, we're already gone by here
    ensure_fleet_session()
    print("fleet restarted clean")
    if not in_tmux():
        os.execvp(tmux_base()[0], tmux_base() + ["attach", "-t", tmux_session_name()])
    return 0


def launch(preset_name, cwd, name, prompt_args):
    cfg = load_config()
    presets = cfg.get("presets", {})
    if preset_name not in presets:
        print(f"unknown preset '{preset_name}'. available: {', '.join(presets)}")
        return 1
    cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    prompt = " ".join(prompt_args) if prompt_args else None
    cmd = build_claude_cmd(preset_name, name, prompt)
    win_name = name or f"{os.path.basename(cwd)}:{preset_name}"
    ensure_fleet_session()
    err = spawn_window(cwd, win_name, cmd)
    if err:
        print(err)
        return 1
    if not in_tmux():
        os.execvp(tmux_base()[0], tmux_base() + ["attach", "-t", tmux_session_name()])
    return 0


def shell_quote(s):
    if s and all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# -------------------------------------------------------------- plain view ---
def cmd_ls():
    sess = read_sessions()
    if not sess:
        print("no live claude sessions.")
        return
    print(f"{'STATUS':8} {'NAME':22} {'KIND':6} {'AGE':>4} {'PID':>7}  DIR")
    for s in sess:
        print(f"{s['status']:8} {s['name'][:22]:22} {s['kind'][:6]:6} "
              f"{age(s['updated']):>4} {s['pid']:>7}  {short_dir(s['cwd'])}")


def cmd_presets():
    cfg = load_config()
    for name, p in cfg.get("presets", {}).items():
        print(f"  {name:12} {p.get('desc','')}")


def cmd_watch():
    """Headless recorder: log session lifecycle even when the dashboard isn't open."""
    flog("INFO", "watch started")
    prev = []
    color_cache = {}
    try:
        while True:
            cur = read_sessions()
            diff_and_log(prev, cur)
            prev = cur
            try:
                apply_window_colors(cur, color_cache)
            except Exception:
                pass
            time.sleep(1)
    except KeyboardInterrupt:
        flog("INFO", "watch stopped")


def _tail(path, n):
    try:
        with open(path) as f:
            return f.read().splitlines()[-n:]
    except FileNotFoundError:
        return None
    except Exception as e:
        return [f"(error reading {path}: {e})"]


def cmd_log(n=40):
    for label, path in [("SESSION EVENTS", EVENTS_LOG),
                        ("FLEET APP LOG", FLEET_LOG),
                        ("CLAUDE DAEMON LOG", DAEMON_LOG)]:
        print(f"\n\033[1m── {label}\033[0m  ({path})")
        lines = _tail(path, n)
        if lines is None:
            print("  (none yet)")
        elif not lines:
            print("  (empty)")
        else:
            for ln in lines:
                print("  " + ln)


# ---------------------------------------------------------------- TUI (mon) ---
def cmd_up():
    """Open (or attach to) the fleet tmux session — one terminal hosting the FLEET
    dashboard (window 0) plus your agent tabs. Never hijacks your current tab."""
    if in_tmux():
        return   # already inside the fleet tmux
    ensure_fleet_session()
    os.execvp(tmux_base()[0], tmux_base() + ["attach", "-t", tmux_session_name()])


def cmd_mon():
    import curses
    code = curses.wrapper(_mon_loop) or 0
    sys.exit(code)   # exit → the window-0 loop relaunches the dashboard


def _mon_loop(stdscr):
    import curses
    curses.curs_set(0)
    stdscr.timeout(1000)  # refresh cadence / getch timeout (ms)
    curses.use_default_colors()
    C = {}
    if curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)  # waiting
        curses.init_pair(2, curses.COLOR_CYAN, -1)    # busy
        curses.init_pair(3, curses.COLOR_GREEN, -1)   # idle
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        C = {"waiting": curses.color_pair(1) | curses.A_BOLD,
             "busy": curses.color_pair(2),
             "idle": curses.color_pair(3) | curses.A_DIM,
             "hdr": curses.color_pair(4) | curses.A_BOLD}

    sel = 0
    msg = ""
    show_bg = False        # hide background subagents by default (toggle with 'a')
    prev = []
    color_cache = {}
    last_sig = None
    last_color = 0.0
    flog("INFO", "dashboard started")
    while True:
        try:
            allsess = read_sessions()
            diff_and_log(prev, allsess)     # record transitions to events.log
            prev = allsess
            # prefer the tmux tab name so UI/right-click renames show here too
            try:
                wnames = tmux_window_names(allsess)
            except Exception:
                wnames = {}
            for s in allsess:
                s["display"] = wnames.get(s["pid"]) or s["name"]
            now = time.time()
            if now - last_color >= 4:        # throttle tab-colour updates: each one
                last_color = now             # redraws the status bar and would close
                try:                         # an open right-click menu
                    apply_window_colors(allsess, color_cache)
                except Exception:
                    pass
            bg_hidden = sum(1 for s in allsess if s["kind"] == "bg")
            sess = allsess if show_bg else [s for s in allsess if s["kind"] != "bg"]
            if sel >= len(sess):
                sel = max(0, len(sess) - 1)
            h, w = stdscr.getmaxyx()
            # Only repaint when the visible data changes (not on every 1s tick) so an
            # open right-click menu / popup isn't dismissed by a redraw underneath it.
            # (age/clock are intentionally excluded from the signature.)
            sig = (tuple((s["pid"], s["status"], s["display"], s["kind"]) for s in sess),
                   sel, show_bg, bg_hidden, msg, h, w)
            if sig != last_sig:
                last_sig = sig
                stdscr.erase()
                try:
                    counts = {"waiting": 0, "busy": 0, "idle": 0}
                    for s in sess:
                        counts[s["status"]] = counts.get(s["status"], 0) + 1
                    bg_note = (f"  bg:{bg_hidden}(hidden)" if bg_hidden and not show_bg
                               else "  [showing bg]" if show_bg else "")
                    title = (f" CLAUDE FLEET   waiting:{counts['waiting']}  "
                             f"busy:{counts['busy']}  idle:{counts['idle']}  "
                             f"shown:{len(sess)}{bg_note}   {time.strftime('%H:%M:%S')}")
                    stdscr.addnstr(0, 0, title.ljust(w), w, C.get("hdr", 0))
                    cols = f"  {'STATUS':8} {'NAME':22} {'KIND':4} {'AGE':>4}  DIR"
                    stdscr.addnstr(1, 0, cols.ljust(w), w, curses.A_UNDERLINE)
                    top = 2
                    for i, s in enumerate(sess):
                        if top + i >= h - 2:
                            break
                        marker = "▶ " if i == sel else "  "
                        row = (f"{marker}{s['status']:8} {s['display'][:22]:22} "
                               f"{s['kind'][:4]:4} {age(s['updated']):>4}  {short_dir(s['cwd'])}")
                        attr = C.get(s["status"], 0)
                        if i == sel:
                            attr = attr | curses.A_REVERSE
                        stdscr.addnstr(top + i, 0, row.ljust(w), w, attr)
                    if not sess:
                        hint = ("no live sessions — press 'n' to launch one" if not bg_hidden
                                else f"no interactive sessions — {bg_hidden} bg agent(s) hidden, press 'a'")
                        stdscr.addnstr(3, 2, hint, max(1, w - 3))
                    help1 = "↑/↓ move  ⏎ jump  n new  x kill  a bg  r refresh   (Alt+`=here, Alt+1-9=tabs)"
                    stdscr.addnstr(h - 1, 0, (msg or help1).ljust(w)[:w - 1],
                                   w - 1, curses.A_DIM if not msg else curses.A_BOLD)
                    stdscr.refresh()
                except curses.error:
                    pass                    # transient draw error (e.g. tiny/resizing term)

            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                break
            if ch == -1:
                msg = ""
                continue
            msg = ""
            if ch == ord("q"):
                flog("INFO", "dashboard quit")
                return 42          # signal the DASH loop to drop to a shell
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(sel + 1, max(0, len(sess) - 1))
            elif ch in (curses.KEY_UP, ord("k")):
                sel = max(sel - 1, 0)
            elif ch in (curses.KEY_ENTER, 10, 13):
                if sess:
                    target = sess[sel]
                    if target["kind"] == "bg":
                        msg = _resume_bg(target)
                    else:
                        ok, m = jump_to(target["pid"])
                        flog("INFO", f"jump {target['name']} -> {m}")
                        msg = m
            elif ch == ord("r"):
                msg = "refreshed"
            elif ch == ord("a"):
                show_bg = not show_bg
                sel = 0
                msg = "showing background agents" if show_bg else "hiding background agents"
            elif ch == ord("x"):
                if sess:
                    msg = _confirm_kill(stdscr, sess[sel])
            elif ch == ord("n"):
                default_dir = sess[sel]["cwd"] if sess else None
                msg = _new_dialog(stdscr, default_dir)
        except KeyboardInterrupt:
            break
        except Exception as e:
            import traceback
            flog("ERROR", f"dashboard loop: {e!r}")
            flog("TRACE", traceback.format_exc().replace("\n", " | "))
            time.sleep(0.3)                 # avoid a tight error-spin
    flog("INFO", "dashboard stopped")


def _new_dialog(stdscr, default_dir):
    """Blocking modal to configure and launch a new session.
    Returns a status message. Never launches until you press Enter."""
    import curses
    if not fleet_server_up():
        return "fleet tmux not running — run `fleet up` in your agent terminal first"
    cfg = load_config()
    presets = list(cfg.get("presets", {}).keys())
    descs = {n: cfg["presets"][n].get("desc", "") for n in presets}

    pi = 0                                    # selected preset index
    dir_val = default_dir or os.path.expanduser("~")
    name_val = ""
    prompt_val = ""
    field = 0                                 # 0=preset 1=dir 2=name 3=prompt
    text = {1: list(dir_val), 2: list(name_val), 3: list(prompt_val)}

    stdscr.timeout(-1)                        # BLOCKING input — no auto-refresh here
    curses.curs_set(1)
    try:
        while True:
            stdscr.erase()                    # full clear — no ghosting on resize
            h, w = stdscr.getmaxyx()
            bw = min(w - 4, 76)
            bx, by = (w - bw) // 2, max(1, h // 2 - 5)
            # clear the modal area
            for r in range(by, by + 11):
                stdscr.addnstr(r, bx, " " * bw, bw, curses.A_REVERSE)

            def put(row, label, value, active, hint=""):
                mark = "▸" if active else " "
                line = f" {mark} {label:8}{value}"
                attr = curses.A_REVERSE | (curses.A_BOLD if active else 0)
                stdscr.addnstr(by + row, bx, line.ljust(bw)[:bw], bw, attr)
                if hint:
                    stdscr.addnstr(by + row, bx + bw - len(hint) - 1, hint,
                                   len(hint), curses.A_REVERSE | curses.A_DIM)

            stdscr.addnstr(by, bx, " New Claude session ".center(bw, "─"), bw,
                           curses.A_REVERSE | curses.A_BOLD)
            put(1, "Preset", presets[pi], field == 0, "←/→")
            stdscr.addnstr(by + 2, bx + 12, descs[presets[pi]][:bw - 14],
                           bw - 14, curses.A_REVERSE | curses.A_DIM)
            put(3, "Dir", "".join(text[1]), field == 1)
            put(4, "Name", "".join(text[2]) or "(auto)", field == 2)
            put(5, "Prompt", "".join(text[3]) or "(none)", field == 3)
            for i, t in enumerate(["↑/↓ move field   ←/→ change preset",
                                   "type to edit   ⏎ launch   Esc cancel"]):
                stdscr.addnstr(by + 7 + i, bx + 2, t, bw - 4,
                               curses.A_REVERSE | curses.A_DIM)
            # place cursor at end of active text field
            if field in text:
                cx = bx + 3 + 8 + len("".join(text[field]))
                stdscr.move(by + (3 if field == 1 else 4 if field == 2 else 5),
                            min(cx, bx + bw - 1))
            stdscr.refresh()

            ch = stdscr.getch()
            if ch == 27:                                      # Esc
                return "new session cancelled"
            elif ch in (curses.KEY_ENTER, 10, 13):            # launch
                break
            elif ch in (curses.KEY_DOWN, 9):                  # ↓ / Tab
                field = (field + 1) % 4
            elif ch == curses.KEY_UP:                         # ↑
                field = (field - 1) % 4
            elif field == 0 and ch == curses.KEY_LEFT:
                pi = (pi - 1) % len(presets)
            elif field == 0 and ch == curses.KEY_RIGHT:
                pi = (pi + 1) % len(presets)
            elif field in text and ch in (curses.KEY_BACKSPACE, 127, 8):
                if text[field]:
                    text[field].pop()
            elif field in text and 32 <= ch < 127:
                text[field].append(chr(ch))
    finally:
        curses.curs_set(0)
        stdscr.timeout(1000)                                  # restore auto-refresh

    preset = presets[pi]
    cwd = os.path.abspath(os.path.expanduser("".join(text[1]).strip() or "~"))
    if not os.path.isdir(cwd):
        return f"dir not found: {cwd}"
    name = "".join(text[2]).strip() or None
    prompt = "".join(text[3]).strip() or None
    cmd = build_claude_cmd(preset, name, prompt)
    win_name = name or f"{os.path.basename(cwd)}:{preset}"
    err = spawn_window(cwd, win_name, cmd)
    return err or f"launched {preset} in {short_dir(cwd)}"


def _confirm_kill(stdscr, s):
    import curses
    h, w = stdscr.getmaxyx()
    nkids = len(descendants(s["pid"]))
    extra = f" + {nkids} subagent(s)" if nkids else ""
    stdscr.timeout(-1)                        # BLOCKING — don't auto-cancel
    try:
        stdscr.addnstr(h - 1, 0,
                       f"kill {s['name']} (pid {s['pid']}){extra}? y/N ".ljust(w)[:w-1],
                       w - 1, curses.A_BOLD)
        stdscr.refresh()
        ch = stdscr.getch()
    finally:
        stdscr.timeout(1000)
    if ch in (ord("y"), ord("Y")):
        try:
            n = kill_tree(s["pid"])
            flog("INFO", f"killed {s['name']} (pid {s['pid']}, {n} process(es))")
            return f"killed {s['name']} ({n} process(es))"
        except Exception as e:
            flog("ERROR", f"kill {s['name']} failed: {e!r}")
            return f"kill failed: {e}"
    return "kill cancelled"


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(prog="fleet", add_help=True)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("up", help="open/attach the fleet tmux (dashboard + agent tabs, one terminal)")
    pr = sub.add_parser("restart", help="restart the dashboard (soft) or whole fleet (--hard)")
    pr.add_argument("--hard", action="store_true",
                    help="tear down the tmux server and every agent tab, then start clean")
    pr.add_argument("-y", "--yes", action="store_true",
                    help="skip the --hard confirmation prompt")
    sub.add_parser("mon", help="run the dashboard TUI (used as window 0)")
    sub.add_parser("dash", help="alias for mon")
    sub.add_parser("ls", help="plain table of live sessions")
    sub.add_parser("presets", help="list presets")
    sub.add_parser("watch", help="headless recorder: log session events without the UI")
    pl = sub.add_parser("log", help="show recent session events + fleet/daemon logs")
    pl.add_argument("-n", "--num", type=int, default=40)

    pn = sub.add_parser("new", help="launch a preconfigured claude session")
    pn.add_argument("preset", nargs="?", default="default")
    pn.add_argument("-C", "--dir", default=None)
    pn.add_argument("-n", "--name", default=None)
    pn.add_argument("prompt", nargs="*")

    pj = sub.add_parser("jump", help="focus a session's tmux window")
    pj.add_argument("target", help="pid or name")

    args = ap.parse_args()
    cmd = args.cmd or "up"

    if cmd == "up":
        cmd_up()
    elif cmd == "restart":
        sys.exit(cmd_restart(hard=args.hard, yes=args.yes))
    elif cmd in ("dash", "mon"):
        cmd_mon()
    elif cmd == "ls":
        cmd_ls()
    elif cmd == "presets":
        cmd_presets()
    elif cmd == "watch":
        cmd_watch()
    elif cmd == "log":
        cmd_log(args.num)
    elif cmd == "new":
        sys.exit(launch(args.preset, args.dir, args.name, args.prompt))
    elif cmd == "jump":
        target = args.target
        sess = read_sessions()
        match = None
        if target.isdigit():
            match = next((s for s in sess if s["pid"] == int(target)), None)
        if not match:
            match = next((s for s in sess if s["name"] == target), None)
        if not match:
            print(f"no live session matching '{target}'")
            sys.exit(1)
        if match["kind"] == "bg":
            print(_resume_bg(match))
            sys.exit(0)
        ok, m = jump_to(match["pid"])
        print(m)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
