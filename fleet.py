#!/usr/bin/env python3
"""fleet — monitor and launch multiple Claude Code sessions.

Reads Claude Code's own live session registry at ~/.claude/sessions/*.json.
Each running session writes {pid, name, cwd, status, kind, updatedAt, procStart, ...}.
  status: busy    -> mid-response (working)
          waiting -> awaiting your action (permission prompt / your turn)
          idle    -> done, sitting at the prompt
          shell   -> pane is at a shell, not at the claude prompt

Subcommands:
  fleet mon                 live curses dashboard (default)
  fleet ls                  plain table
  fleet new [preset] ...    launch a preconfigured claude in a new tmux window
  fleet jump <pid|name>     focus a session's tmux window
  fleet presets             list presets
  fleet restore             relaunch every parked session (what a reboot left)
  fleet resume <name|id>    relaunch one parked/closed session
  fleet forget <name|id>    drop a session from the roster
  fleet theme [dark|light|auto]  colour palette for the dashboard + tmux bar
  fleet update [--check]    move this install to the newest release
  fleet version

Persistence: every session on the fleet tmux server is remembered in
~/.fleet/roster/<sessionId>.json. Claude Code keeps the conversation itself,
so after a reboot `fleet up` rebuilds the server and `claude --resume`s each
remembered tab in its old directory with its old preset flags.
"""
import os, sys, json, glob, time, subprocess, argparse

__version__ = "0.1.0"
REPO = "jordanshamai/fleet"                    # GitHub owner/name — updates + install
REPO_URL = f"https://github.com/{REPO}"

HOME = os.path.expanduser("~")
FLEET_HOME = os.path.join(HOME, ".fleet")      # user state: config, roster, update cache
USER_CONFIG = os.path.join(FLEET_HOME, "config.json")
SESS_DIR = os.path.join(HOME, ".claude", "sessions")
# fleet's own files (presets, tmux conf, logs) live next to this script, wherever
# you cloned it — realpath so a `fleet` symlink on PATH resolves to the checkout.
FLEET_DIR = os.path.dirname(os.path.realpath(__file__))
PRESETS_FILE = os.path.join(FLEET_DIR, "presets.json")
TMUX_CONF = os.path.join(FLEET_DIR, "fleet.tmux.conf")

# Claude Code's own registry enum is exactly: busy | shell | idle | waiting.
# "shell" = the pane dropped to a shell, so it is live but not at a claude
# prompt — least urgent, sorted below idle. Anything unknown sorts last.
STATUS_ORDER = {"waiting": 0, "busy": 1, "idle": 2, "shell": 3}


def tmux_base():
    """`tmux -L <socket>` — fleet uses an isolated tmux server."""
    cfg = load_config()
    return ["tmux", "-L", cfg.get("tmux_socket", "fleet")]


def tmux_session_name():
    return load_config().get("tmux_session", "fleet")


# ------------------------------------------------------------ user config ---
def load_user_config():
    """~/.fleet/config.json — per-user settings (theme, update_check). Distinct
    from presets.json, which lives in the checkout and is versioned."""
    try:
        with open(USER_CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_user_config(**fields):
    cfg = load_user_config()
    cfg.update(fields)
    os.makedirs(FLEET_HOME, exist_ok=True)
    tmp = USER_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1, sort_keys=True)
    os.replace(tmp, USER_CONFIG)
    return cfg


# ----------------------------------------------------------------- themes ---
# One palette per background. Every colour is (256-colour index, 8-colour
# fallback, attrs) so the curses dashboard and the tmux status bar agree, and a
# 16-colour terminal still gets something readable.
#   dash: dashboard row colours by status (+ "hdr" for the title bar)
#   tmux: status bar / FLEET label / current tab / clock
THEMES = {
    "dark": {
        "dash": {"waiting": (220, "yellow", "bold"), "busy": (81, "cyan", ""),
                 "idle": (114, "green", "dim"), "shell": (245, "white", "dim"),
                 "parked": (245, "white", "dim"), "hdr": (255, "white", "bold")},
        "tmux": {"status": "bg=colour236,fg=colour250",
                 "label_on": "fg=black,bg=colour214,bold", "label_off": "fg=colour214,bold",
                 "current": "bg=colour24,fg=white,bold", "clock": "fg=colour245",
                 "win": {"waiting": "fg=colour220,bold", "busy": "fg=colour81",
                         "idle": "fg=colour114,dim", "shell": "fg=colour245"}},
    },
    "light": {
        "dash": {"waiting": (160, "red", "bold"), "busy": (25, "blue", ""),
                 "idle": (28, "green", ""), "shell": (244, "black", "dim"),
                 "parked": (244, "black", "dim"), "hdr": (236, "black", "bold")},
        "tmux": {"status": "bg=colour254,fg=colour236",
                 "label_on": "fg=white,bg=colour166,bold", "label_off": "fg=colour166,bold",
                 "current": "bg=colour31,fg=white,bold", "clock": "fg=colour244",
                 "win": {"waiting": "fg=colour160,bold", "busy": "fg=colour25",
                         "idle": "fg=colour28", "shell": "fg=colour244"}},
    },
}


def resolve_theme():
    """Theme name to use: $FLEET_THEME, then config.json, then auto-detect.
    auto = COLORFGBG (set by some terminals as "fg;bg": a bg of 0-6 or 8 is
    dark) — otherwise dark. Terminals rarely tell you their background, so
    `fleet theme light` is the reliable way."""
    want = os.environ.get("FLEET_THEME") or load_user_config().get("theme") or "auto"
    if want in THEMES:
        return want
    bg = os.environ.get("COLORFGBG", "").split(";")[-1]
    if bg.isdigit():
        return "dark" if int(bg) in (0, 1, 2, 3, 4, 5, 6, 8) else "light"
    return "dark"


def theme():
    return THEMES[resolve_theme()]


def apply_tmux_theme(name=None):
    """Push the palette into the running fleet tmux server (status bar, FLEET
    label, current-tab highlight). fleet.tmux.conf carries the dark defaults;
    this overrides them after every source-file / server start."""
    t = THEMES[name or resolve_theme()]["tmux"]
    label = (f"#{{?#{{==:#{{window_index}},0}},#[{t['label_on'].replace(',', '#,')}],"
             f"#[{t['label_off'].replace(',', '#,')}]}} FLEET #[default] ")
    for argv in (["set", "-g", "status-style", t["status"]],
                 ["set", "-g", "status-left", label],
                 ["set", "-g", "status-right", f"#[{t['clock']}]%H:%M "],
                 ["setw", "-g", "window-status-current-style", t["current"]],
                 ["set", "-g", "@fleet_theme", name or resolve_theme()]):
        subprocess.run(tmux_base() + argv, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


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


# ---------------------------------------------------------------- roster ---
# fleet's memory of which conversations were open on its tmux server, so a
# reboot (or a dead server) can bring them back. One small JSON per session —
# the same shape as Claude Code's own registry, and no shared file to race on.
# Claude Code keeps the transcript itself under ~/.claude/projects, so a row
# only holds what `claude --resume <id>` can't know: the tab name, the cwd and
# which preset (flags) launched it.
#   state: active  -> live last time fleet looked. No live process = "parked",
#                     and `fleet up` restores it when it (re)creates the server.
#          closed  -> exited or was killed WHILE fleet was watching; kept for a
#                     while as history so `fleet resume <name>` can bring it back.
ROSTER_DIR = os.environ.get("FLEET_ROSTER_DIR") or os.path.join(HOME, ".fleet", "roster")
ROSTER_KEEP_CLOSED = 30 * 86400     # seconds a closed row stays around
RESUME_GRACE = 20                   # seconds a just-relaunched row is not "parked"
                                    # (claude hasn't written its registry file yet)


def _roster_path(sid):
    return os.path.join(ROSTER_DIR, f"{sid}.json")


def roster_read():
    """{sessionId: row} for every roster file; unreadable files are skipped."""
    out = {}
    for path in glob.glob(os.path.join(ROSTER_DIR, "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        sid = os.path.basename(path)[:-5]
        d["sessionId"] = sid
        out[sid] = d
    return out


def roster_write(sid, row):
    """tmp + rename, so a reader never sees a half-written row."""
    os.makedirs(ROSTER_DIR, exist_ok=True)
    row = {k: v for k, v in row.items() if k != "sessionId"}
    tmp = _roster_path(sid) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(row, f, indent=1, sort_keys=True)
    os.replace(tmp, _roster_path(sid))


def roster_update(sid, **fields):
    """Merge fields into an existing row. No-op (returns None) if there is none."""
    row = roster_read().get(sid)
    if row is None:
        return None
    row.update(fields)
    roster_write(sid, row)
    return row


def roster_add(sid, name, cwd, preset, prompt=None, win_name=None):
    now = int(time.time())
    row = {"name": name, "win_name": win_name or name, "cwd": cwd, "preset": preset,
           "prompt": prompt, "state": "active", "created": now, "last_seen": now}
    roster_write(sid, row)
    return row


def roster_close(sid, why="exited"):
    row = roster_read().get(sid)
    if row and row.get("state") != "closed":
        roster_update(sid, state="closed", closed_at=int(time.time()), closed_why=why)
        flog("INFO", f"roster: closed '{row_label(row)}' ({why})")


def roster_forget(sid):
    try:
        os.remove(_roster_path(sid))
        return True
    except FileNotFoundError:
        return False


def row_label(row):
    return row.get("win_name") or row.get("name") or row.get("sessionId", "?")[:8]


def roster_parked(live_sids):
    """Active rows with no live process — what a reboot (or dead server) left."""
    now = time.time()
    out = []
    for sid, r in roster_read().items():
        if r.get("state") != "active" or sid in live_sids:
            continue
        if now - r.get("resumed_at", 0) < RESUME_GRACE:
            continue
        out.append(r)
    out.sort(key=lambda r: row_label(r).lower())
    return out


def roster_find(target, rows=None):
    """Row whose sessionId (or a prefix of it), name or tab name is `target`.
    Several tabs can share a name over time — the most recently seen one wins."""
    rows = roster_read() if rows is None else rows
    hits = [r for sid, r in rows.items()
            if sid == target or (len(target) >= 4 and sid.startswith(target))
            or r.get("name") == target or r.get("win_name") == target]
    hits.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
    return hits[0] if hits else None


def roster_reconcile(live, hosted, state):
    """Keep the roster in step with the fleet tmux server. Called every tick.
    `live`   rows from read_sessions();
    `hosted` {pid: tmux tab name} for the ones whose pane is on the FLEET server
             (a claude in some other terminal is not fleet's to remember);
    `state`  a dict the caller keeps across calls ({"seen": sids seen live}).
    A session that was live and vanishes WHILE fleet is watching exited or was
    killed -> closed. One that vanishes while fleet isn't running (reboot, dead
    server) stays active -> parked, and `fleet up` brings it back."""
    rows = roster_read()
    now = int(time.time())
    seen = state.setdefault("seen", set())
    live_hosted = set()
    for s in live:
        sid = s.get("sessionId")
        if not sid or s["kind"] == "bg" or s["pid"] not in hosted:
            continue
        live_hosted.add(sid)
        tab = hosted[s["pid"]] or s["name"]
        # only an explicit --name is worth re-passing on resume; a derived one
        # ("fleet-79") would otherwise get pinned forever
        name = s["name"] if s.get("nameSource") not in (None, "derived") else None
        row = rows.get(sid)
        if row is None:
            roster_add(sid, name, s["cwd"], preset=None, win_name=tab)
            flog("INFO", f"roster: adopted '{tab}' ({sid[:8]})")
        else:
            changed = {}
            if row.get("state") != "active":
                changed["state"] = "active"
            if tab and tab != row.get("win_name"):
                changed["win_name"] = tab
            if name and name != row.get("name"):
                changed["name"] = name
            if s["cwd"] and s["cwd"] != row.get("cwd"):
                changed["cwd"] = s["cwd"]
            if now - row.get("last_seen", 0) >= 60:
                changed["last_seen"] = now
            if changed:
                roster_update(sid, **changed)
        seen.add(sid)
    for sid in list(seen):
        if sid not in live_hosted:
            seen.discard(sid)
            row = rows.get(sid)
            if row and row.get("state") == "active":
                roster_close(sid, "exited")
    for sid, r in rows.items():
        if r.get("state") == "closed" and now - r.get("closed_at", now) > ROSTER_KEEP_CLOSED:
            roster_forget(sid)


# ------------------------------------------------------------- proc helpers ---
# Linux publishes process facts in /proc; macOS has none, so the same facts
# (start time, parent pid) come from `ps`. Each `ps` is a fork, which a
# once-a-second dashboard can't afford per pid — so on darwin the whole
# process table is read in one call and cached for a refresh cycle.
IS_DARWIN = sys.platform == "darwin"

_PS_TTL = 1.0                                  # seconds a snapshot stays fresh
# "at" is None until the first successful read — never 0, because monotonic()'s
# origin is undefined (on macOS it starts near 0, so a 0 sentinel reads as "just
# fetched" and would pin an empty snapshot for the process's first second).
_PS_CACHE = {"at": None, "ppid": {}, "start": {}}


def _ps_snapshot(force=False):
    """{pid: ppid} + {pid: start-time} for every visible process (darwin only)."""
    now = time.monotonic()
    if not force and _PS_CACHE["at"] is not None and (now - _PS_CACHE["at"]) < _PS_TTL:
        return _PS_CACHE
    ppid, start = {}, {}
    try:
        # lstart is rendered in UTC because that's what Claude Code stores in
        # procStart; bare `ps` would print local time and never compare equal.
        out = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,lstart="], text=True,
            stderr=subprocess.DEVNULL, env=dict(os.environ, TZ="UTC"))
    except Exception:
        out = ""
    for line in out.splitlines():
        parts = line.split(None, 2)            # lstart has spaces, so it goes last
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        ppid[pid] = int(parts[1]) if parts[1].isdigit() else None
        start[pid] = " ".join(parts[2].split())
    if ppid:                                   # keep the old snapshot if ps failed
        _PS_CACHE.update({"at": now, "ppid": ppid, "start": start})
    return _PS_CACHE


def _norm_start(v):
    """Whitespace-normalised start time, so the two sources compare equal."""
    return " ".join(str(v).split()) if v else ""


def proc_starttime(pid):
    """Process start time as a string comparable to the session file's procStart.
    darwin: `ps` lstart in UTC ('Thu Sep 17 14:08:56 2026').
    linux:  field 22 of /proc/pid/stat (starttime in clock ticks)."""
    if IS_DARWIN:
        return _ps_snapshot()["start"].get(pid)
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # comm can contain spaces/parens; everything after the last ')' is space-split
        rest = data[data.rfind(")") + 2:].split()
        return rest[19]  # field 22 = index 19 after (pid, comm) removed
    except Exception:
        return None


def pid_exists(pid):
    """Live-pid check that needs no /proc (signal 0 kills nothing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process — exists, just not ours
    except Exception:
        return False
    return True


def is_alive(pid, procstart=None):
    if not pid_exists(pid):
        return False
    if procstart:  # guard against PID reuse
        st = proc_starttime(pid)
        if st is not None and _norm_start(procstart) != _norm_start(st):
            return False
    return True


def ppid_of(pid):
    if IS_DARWIN:
        return _ps_snapshot()["ppid"].get(pid)
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def _children_map():
    """{ppid: [child pid, ...]} across every visible process."""
    children = {}
    if IS_DARWIN:
        for pid, pp in _ps_snapshot()["ppid"].items():
            if pp is not None:
                children.setdefault(pp, []).append(pid)
        return children
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pp = ppid_of(int(d))
        if pp is not None:
            children.setdefault(pp, []).append(int(d))
    return children


def descendants(pid):
    """All live descendant pids of `pid` (children, grandchildren, ...)."""
    children = _children_map()
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
    if IS_DARWIN:
        _ps_snapshot(force=True)            # never build the tree from a stale table
    targets = [pid] + descendants(pid)      # kill children before/with the parent
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = [p for p in targets if pid_exists(p)]
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
                if not any(pid_exists(p) for p in targets):
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
            "nameSource": d.get("nameSource"),
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
    """Map a claude session pid to its tmux pane by walking the parent-pid chain.
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
def build_claude_cmd(preset_name, name=None, prompt=None, session_id=None, resume=None):
    """Return the `claude ...` argv for a preset (+ optional name/prompt).
    session_id: pin a fresh session's id (so fleet knows it before claude starts).
    resume:     `--resume <id>` an existing conversation instead of starting one.
    An unknown/None preset (a tab fleet adopted rather than launched) = bare `claude`."""
    preset = load_config().get("presets", {}).get(preset_name) or {}
    cmd = ["claude", *preset.get("args", [])]
    settings = preset.get("settings")
    if settings:
        spath = settings if os.path.isabs(settings) else os.path.join(FLEET_DIR, settings)
        cmd += ["--settings", spath]
    if name:
        cmd += ["--name", name]
    if session_id:
        cmd += ["--session-id", session_id]
    if resume:
        cmd += ["--resume", resume]
    if prompt:
        cmd.append(prompt)
    return cmd


FLEET_BIN = os.path.join(FLEET_DIR, "fleet.py")


def wrap_inner(cmd, sid=None):
    """Wrap argv so the pane stays open (shows a prompt) after claude exits.
    With a session id, claude's exit also marks the roster row closed — so a
    tab you quit isn't relaunched next reboot (a reboot never runs this hook)."""
    quoted = " ".join(shell_quote(c) for c in cmd)
    hook = f"; {shell_quote(FLEET_BIN)} _exited {sid}" if sid else ""
    return f"{quoted}{hook}; echo; echo '[claude exited — press enter to close]'; read; exec $SHELL"


def fleet_server_up():
    return subprocess.run(tmux_base() + ["has-session", "-t", tmux_session_name()],
                          stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL).returncode == 0


def spawn_window(cwd, win_name, cmd, sid=None):
    """Open a new tmux window in the fleet session running `cmd` (becomes active).
    Targets the fleet session explicitly so it works from a standalone dashboard."""
    if not fleet_server_up():
        return "fleet tmux not running — start it with `fleet up` first"
    inner = wrap_inner(cmd, sid)
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


def start_session(preset, cwd, name, prompt, win_name=None):
    """Launch a fresh claude in a new fleet tab and remember it in the roster.
    The session id is minted here and pinned with --session-id, so the roster
    row exists before claude even starts. Returns an error string or None."""
    import uuid
    sid = str(uuid.uuid4())
    cmd = build_claude_cmd(preset, name, prompt, session_id=sid)
    win_name = win_name or name or f"{os.path.basename(cwd)}:{preset}"
    roster_add(sid, name, cwd, preset, prompt, win_name=win_name)
    err = spawn_window(cwd, win_name, cmd, sid=sid)
    if err:
        roster_forget(sid)
    return err


def transcript_exists(sid):
    """Claude Code only writes ~/.claude/projects/<cwd>/<sid>.jsonl once a
    conversation has a message — a tab that never got one has nothing to resume."""
    return bool(glob.glob(os.path.join(HOME, ".claude", "projects", "*", f"{sid}.jsonl")))


def resume_session(row):
    """Relaunch a roster row in a new fleet tab via `claude --resume`.
    Returns (ok, message)."""
    cwd = row.get("cwd") or HOME
    label = row_label(row)
    if not os.path.isdir(cwd):
        return False, f"can't resume '{label}': directory gone ({short_dir(cwd)})"
    sid = row["sessionId"]
    if not transcript_exists(sid):
        # nothing to resume (never got a message): start it over with the same
        # setup under a fresh id, and re-send the launch prompt if it had one
        roster_forget(sid)
        err = start_session(row.get("preset"), cwd, row.get("name"), row.get("prompt"),
                            win_name=row.get("win_name"))
        if err:
            return False, err
        flog("INFO", f"restarted '{label}' fresh ({sid[:8]} had no conversation)")
        return True, f"restarted '{label}' fresh (it had no conversation yet)"
    cmd = build_claude_cmd(row.get("preset"), row.get("name"), resume=sid)
    # write first: if claude exits at once the _exited hook must find the row
    now = int(time.time())
    roster_update(sid, state="active", resumed_at=now, last_seen=now)
    err = spawn_window(cwd, label, cmd, sid=sid)
    if err:
        return False, err
    flog("INFO", f"resumed '{label}' ({sid[:8]}) in {cwd}")
    return True, f"resumed '{label}'"


def restore_parked(quiet=False):
    """Relaunch every parked row (active in the roster, no live process) — what a
    reboot left behind. Returns the number relaunched."""
    live = {s["sessionId"] for s in read_sessions() if s.get("sessionId")}
    n = 0
    for row in roster_parked(live):
        ok, m = resume_session(row)
        flog("INFO" if ok else "ERROR", f"restore: {m}")
        if not quiet:
            print(m)
        n += int(ok)
    if n:   # spawning tabs moved focus — land on the dashboard
        subprocess.run(tmux_base() + ["select-window", "-t", dash_target()],
                       stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    return n


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


def apply_window_colors(sessions, cache):
    """Colour each tmux tab in the status bar by the status of the session in it,
    using the active theme's palette (same hues as the dashboard rows).
    `cache` (a dict) is kept across calls so we only issue tmux commands on change."""
    win_style = theme()["tmux"]["win"]
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
        style = win_style.get(win_status.get(key))      # None = window has no session
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
            # a brand-new server = the old one died (reboot, kill-server): bring
            # back every tab the roster still lists as active
            n = restore_parked()
            if n:
                print(f"restored {n} session(s) from the roster")
    # tell the server where its config lives so `prefix r` can reload it from
    # whatever directory fleet was cloned into
    subprocess.run(tmux_base() + ["set", "-g", "@fleet_conf", TMUX_CONF],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    apply_tmux_theme()


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


def _respawn_dash():
    """Soft restart: reload fleet.tmux.conf + theme and respawn window 0, so a
    new fleet.py / config takes effect. Agent tabs keep running, untouched.
    Returns an error string or None."""
    for argv in (["source-file", TMUX_CONF],
                 ["set", "-g", "@fleet_conf", TMUX_CONF]):
        subprocess.run(tmux_base() + argv,
                       stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    apply_tmux_theme()
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
        return err
    return None


def cmd_restart(hard=False, yes=False):
    """Restart the dashboard (default) or the whole fleet tmux server (--hard)."""
    if not fleet_server_up():
        print("fleet tmux isn't running — starting it fresh")
        cmd_up()
        return 0

    if not hard:
        err = _respawn_dash()
        if err:
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
        if s.get("sessionId"):          # a deliberate kill, not a reboot — don't
            roster_close(s["sessionId"], "hard restart")   # restore it below
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
    ensure_fleet_session()
    err = start_session(preset_name, cwd, name, prompt)
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
def cmd_ls(show_all=False):
    sess = read_sessions()
    live = {s["sessionId"] for s in sess if s.get("sessionId")}
    parked = roster_parked(live)
    closed = ([r for r in roster_read().values() if r.get("state") == "closed"]
              if show_all else [])
    closed.sort(key=lambda r: r.get("closed_at", 0), reverse=True)
    if not sess and not parked and not closed:
        print("no live claude sessions." + ("" if show_all else "  (fleet ls -a for history)"))
        return
    print(f"{'STATUS':8} {'NAME':22} {'KIND':6} {'AGE':>4} {'PID':>7}  DIR")
    for s in sess:
        print(f"{s['status']:8} {s['name'][:22]:22} {s['kind'][:6]:6} "
              f"{age(s['updated']):>4} {s['pid']:>7}  {short_dir(s['cwd'])}")
    for r in parked:
        print(f"{'parked':8} {row_label(r)[:22]:22} {(r.get('preset') or '-')[:6]:6} "
              f"{age(r.get('last_seen', 0) * 1000):>4} {r['sessionId'][:7]:>7}  {short_dir(r['cwd'])}")
    for r in closed:
        print(f"{'closed':8} {row_label(r)[:22]:22} {(r.get('preset') or '-')[:6]:6} "
              f"{age(r.get('closed_at', 0) * 1000):>4} {r['sessionId'][:7]:>7}  {short_dir(r['cwd'])}")
    if parked:
        print(f"\n{len(parked)} parked session(s): `fleet restore` relaunches them all, "
              f"`fleet resume <name>` one, `fleet forget <name>` drops one.")


def cmd_presets():
    cfg = load_config()
    for name, p in cfg.get("presets", {}).items():
        print(f"  {name:12} {p.get('desc','')}")


def cmd_watch():
    """Headless recorder: log session lifecycle even when the dashboard isn't open."""
    flog("INFO", "watch started")
    prev = []
    color_cache = {}
    rstate = {}
    try:
        while True:
            cur = read_sessions()
            diff_and_log(prev, cur)
            prev = cur
            try:
                apply_window_colors(cur, color_cache)
                roster_reconcile(cur, tmux_window_names(cur), rstate)
            except Exception as e:
                flog("ERROR", f"watch: {e!r}")
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
    C = _init_colors(curses)
    update_note = ""          # " · v0.2.0 available" once the background check says so
    _start_update_check()

    sel = 0
    rstate = {}            # roster_reconcile's memory of which sids it has seen live
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
            try:
                roster_reconcile(allsess, wnames, rstate)
                parked = _parked_rows(allsess)
            except Exception as e:
                flog("ERROR", f"roster: {e!r}")
                parked = []
            now = time.time()
            if now - last_color >= 4:        # throttle tab-colour updates: each one
                last_color = now             # redraws the status bar and would close
                try:                         # an open right-click menu
                    apply_window_colors(allsess, color_cache)
                except Exception:
                    pass
            bg_hidden = sum(1 for s in allsess if s["kind"] == "bg")
            sess = allsess if show_bg else [s for s in allsess if s["kind"] != "bg"]
            sess = sess + parked            # parked rows sit under the live ones
            if sel >= len(sess):
                sel = max(0, len(sess) - 1)
            h, w = stdscr.getmaxyx()
            # Only repaint when the visible data changes (not on every 1s tick) so an
            # open right-click menu / popup isn't dismissed by a redraw underneath it.
            # (age/clock are intentionally excluded from the signature.)
            latest = update_available()
            update_note = f"  ⬆ {latest} available: fleet update" if latest else ""
            sig = (tuple((s["pid"] or s["sessionId"], s["status"], s["display"], s["kind"])
                         for s in sess),
                   sel, show_bg, bg_hidden, msg, h, w, update_note)
            if sig != last_sig:
                last_sig = sig
                stdscr.erase()
                try:
                    counts = {"waiting": 0, "busy": 0, "idle": 0, "shell": 0}
                    for s in sess:
                        counts[s["status"]] = counts.get(s["status"], 0) + 1
                    bg_note = (f"  bg:{bg_hidden}(hidden)" if bg_hidden and not show_bg
                               else "  [showing bg]" if show_bg else "")
                    shell_note = f"  shell:{counts['shell']}" if counts["shell"] else ""
                    if parked:
                        shell_note += f"  parked:{len(parked)}"
                    title = (f" CLAUDE FLEET   waiting:{counts['waiting']}  "
                             f"busy:{counts['busy']}  idle:{counts['idle']}{shell_note}  "
                             f"shown:{len(sess)}{bg_note}{update_note}   {time.strftime('%H:%M:%S')}")
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
                    help1 = ("↑/↓ move  ⏎ jump/resume  n new  x kill/forget  a bg  "
                             + ("R restore all  " if parked else "")
                             + "r refresh   (Alt+`=here, Alt+1-9=tabs)")
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
                    if target["status"] == "parked":
                        ok, msg = resume_session(target["row"])
                    elif target["kind"] == "bg":
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
                if sess and sess[sel]["status"] == "parked":
                    msg = _confirm_forget(stdscr, sess[sel]["row"])
                elif sess:
                    msg = _confirm_kill(stdscr, sess[sel])
            elif ch == ord("R"):
                if parked:
                    n = restore_parked(quiet=True)
                    msg = f"restored {n} of {len(parked)} parked session(s)"
                else:
                    msg = "nothing parked to restore"
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


def _init_colors(curses):
    """Colour pairs for the active theme; 8-colour fallback below 256 colours."""
    if not curses.has_colors():
        return {}
    curses.start_color()
    many = curses.COLORS >= 256
    attrs = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "": 0}
    C = {}
    for i, (key, (c256, c8, attr)) in enumerate(theme()["dash"].items(), start=1):
        fg = c256 if many else getattr(curses, f"COLOR_{c8.upper()}", -1)
        try:
            curses.init_pair(i, fg, -1)
        except curses.error:
            curses.init_pair(i, -1, -1)
        C[key] = curses.color_pair(i) | attrs.get(attr, 0)
    return C


def _parked_rows(live):
    """Roster rows with no live process, shaped like read_sessions() rows so the
    dashboard can list them under the live ones."""
    live_sids = {s["sessionId"] for s in live if s.get("sessionId")}
    out = []
    for r in roster_parked(live_sids):
        label = row_label(r)
        out.append({"pid": None, "sessionId": r["sessionId"], "name": label,
                    "display": label, "cwd": r.get("cwd", ""), "status": "parked",
                    "kind": (r.get("preset") or "")[:4],
                    "updated": r.get("last_seen", 0) * 1000, "alive": False, "row": r})
    return out


def _confirm_forget(stdscr, row):
    import curses
    h, w = stdscr.getmaxyx()
    stdscr.timeout(-1)
    try:
        stdscr.addnstr(h - 1, 0,
                       f"forget parked '{row_label(row)}'? (transcript stays in ~/.claude) y/N "
                       .ljust(w)[:w-1], w - 1, curses.A_BOLD)
        stdscr.refresh()
        ch = stdscr.getch()
    finally:
        stdscr.timeout(1000)
    if ch in (ord("y"), ord("Y")):
        roster_forget(row["sessionId"])
        flog("INFO", f"roster: forgot '{row_label(row)}' ({row['sessionId'][:8]})")
        return f"forgot '{row_label(row)}'"
    return "forget cancelled"


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
    err = start_session(preset, cwd, name, prompt)
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
        if s.get("sessionId"):
            roster_close(s["sessionId"], "killed")
        try:
            n = kill_tree(s["pid"])
            flog("INFO", f"killed {s['name']} (pid {s['pid']}, {n} process(es))")
            return f"killed {s['name']} ({n} process(es))"
        except Exception as e:
            flog("ERROR", f"kill {s['name']} failed: {e!r}")
            return f"kill failed: {e}"
    return "kill cancelled"


# ---------------------------------------------------------------- updates ---
UPDATE_CACHE = os.path.join(FLEET_HOME, "update-check.json")
UPDATE_EVERY = 24 * 3600


def parse_version(v):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); anything unparsable -> None."""
    v = str(v).strip().lstrip("v")
    parts = v.split("-")[0].split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _fetch_latest_release():
    """Newest release tag on GitHub (e.g. 'v0.2.0'), or None. Network, ~5s max."""
    import urllib.request
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": f"fleet/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r).get("tag_name")
    except Exception:
        return None


def _write_update_cache(latest):
    os.makedirs(FLEET_HOME, exist_ok=True)
    tmp = UPDATE_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"checked": int(time.time()), "latest": latest}, f)
    os.replace(tmp, UPDATE_CACHE)


def _read_update_cache():
    try:
        with open(UPDATE_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def _start_update_check():
    """Once a day, ask GitHub for the newest release — in a background thread so
    the dashboard never blocks on the network. Off with config update_check=false."""
    if load_user_config().get("update_check", True) is False:
        return
    if time.time() - _read_update_cache().get("checked", 0) < UPDATE_EVERY:
        return
    import threading

    def run():
        latest = _fetch_latest_release()
        # a failed check still counts as checked: don't hammer the API when offline
        _write_update_cache(latest or _read_update_cache().get("latest"))
        if latest:
            flog("INFO", f"update check: latest release {latest}, running v{__version__}")
    threading.Thread(target=run, daemon=True).start()


def update_available():
    """Newest cached release tag if it's newer than this install, else None."""
    latest = _read_update_cache().get("latest")
    lv, mine = parse_version(latest or ""), parse_version(__version__)
    return latest if lv and mine and lv > mine else None


def _git(*args, check=True):
    return subprocess.run(["git", "-C", FLEET_DIR, *args], capture_output=True, text=True,
                          check=check)


def cmd_update(check_only=False, track_main=False):
    """Move this checkout to the newest release (or the tip of main with --main).
    On a branch (you're hacking on fleet) it fast-forwards that branch instead of
    detaching onto a tag. Refuses to touch a dirty working tree."""
    if not os.path.isdir(os.path.join(FLEET_DIR, ".git")):
        print(f"{FLEET_DIR} isn't a git checkout — reinstall with the installer:\n"
              f"  curl -fsSL https://raw.githubusercontent.com/{REPO}/main/install.sh | bash")
        return 1
    print(f"fleet v{__version__} at {FLEET_DIR}")
    r = _git("fetch", "--tags", "--quiet", "origin", check=False)
    if r.returncode != 0:
        print(f"fetch failed: {(r.stderr or '').strip()}")
        return 1
    tags = [t for t in _git("tag", "--list", "v*", "--sort=-v:refname").stdout.split()
            if parse_version(t)]
    latest = tags[0] if tags else None
    if latest:
        _write_update_cache(latest)
    mine = parse_version(__version__)
    newer = latest and parse_version(latest) > mine
    if check_only:
        print(f"newest release: {latest or 'none'}" + ("  — run `fleet update`" if newer else
                                                     "  — you're up to date"))
        return 0
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("working tree has local changes — commit or stash them, then `fleet update`")
        return 1
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if track_main or branch == "main":
        r = _git("checkout", "--quiet", "main", check=False) if branch != "main" else None
        r = _git("pull", "--ff-only", "--quiet", "origin", "main", check=False)
        what = "main"
    elif branch != "HEAD":
        r = _git("pull", "--ff-only", "--quiet", check=False)
        what = f"branch {branch}"
    else:
        if not latest:
            print("no release tags found on origin")
            return 1
        if not newer and _git("describe", "--tags", "--exact-match", check=False).stdout.strip() == latest:
            print(f"already on {latest} — up to date")
            return 0
        r = _git("checkout", "--quiet", latest, check=False)
        what = latest
    if r is not None and r.returncode != 0:
        print(f"update failed: {(r.stderr or '').strip()}")
        return 1
    ver = subprocess.run([sys.executable, os.path.join(FLEET_DIR, "fleet.py"), "version"],
                         capture_output=True, text=True).stdout.strip()
    print(f"updated to {what}: {ver}")
    if fleet_server_up():
        err = _respawn_dash()
        print("dashboard restarted with the new version" if not err else f"(restart failed: {err})")
    return 0


def cmd_theme(name=None):
    cur = resolve_theme()
    cfg = load_user_config().get("theme")
    if not name:
        print(f"theme: {cur}" + ("" if cfg else "  (auto-detected; set one with `fleet theme dark|light`)"))
        print(f"available: {', '.join(THEMES)}, auto")
        return 0
    if name != "auto" and name not in THEMES:
        print(f"unknown theme '{name}'. available: {', '.join(THEMES)}, auto")
        return 1
    save_user_config(theme=name)
    print(f"theme set to {name}" + (f" (resolves to {resolve_theme()})" if name == "auto" else ""))
    if fleet_server_up():
        apply_tmux_theme()
        err = _respawn_dash()
        print("applied to the tmux bar and dashboard" if not err else f"(dashboard restart failed: {err})")
    return 0


# ------------------------------------------------------------ roster cmds ---
def cmd_restore():
    if not fleet_server_up():
        print("fleet tmux isn't running — `fleet up` starts it AND restores parked sessions")
        return 1
    n = restore_parked()
    print(f"restored {n} session(s)" if n else "nothing parked — every remembered session is live")
    if n and not in_tmux():
        os.execvp(tmux_base()[0], tmux_base() + ["attach", "-t", tmux_session_name()])
    return 0


def cmd_resume(target):
    row = roster_find(target)
    if not row:
        print(f"no remembered session matching '{target}'  (fleet ls -a)")
        return 1
    live = next((s for s in read_sessions() if s.get("sessionId") == row["sessionId"]), None)
    if live:
        ok, m = jump_to(live["pid"])
        print(f"'{row_label(row)}' is already running — {m}")
        return 0 if ok else 1
    if not fleet_server_up():
        print("fleet tmux isn't running — `fleet up` starts it AND restores parked sessions")
        return 1
    ok, m = resume_session(row)
    print(m)
    if ok and not in_tmux():
        os.execvp(tmux_base()[0], tmux_base() + ["attach", "-t", tmux_session_name()])
    return 0 if ok else 1


def cmd_forget(target):
    row = roster_find(target)
    if not row:
        print(f"no remembered session matching '{target}'")
        return 1
    if any(s.get("sessionId") == row["sessionId"] for s in read_sessions()):
        print(f"'{row_label(row)}' is still running — kill it first (it would just be re-adopted)")
        return 1
    roster_forget(row["sessionId"])
    flog("INFO", f"roster: forgot '{row_label(row)}' ({row['sessionId'][:8]})")
    print(f"forgot '{row_label(row)}'  (its transcript is still in ~/.claude for `claude --resume`)")
    return 0


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(prog="fleet", add_help=True)
    ap.add_argument("-V", "--version", action="version", version=f"fleet v{__version__}")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("up", help="open/attach the fleet tmux (dashboard + agent tabs, one terminal)")
    pr = sub.add_parser("restart", help="restart the dashboard (soft) or whole fleet (--hard)")
    pr.add_argument("--hard", action="store_true",
                    help="tear down the tmux server and every agent tab, then start clean")
    pr.add_argument("-y", "--yes", action="store_true",
                    help="skip the --hard confirmation prompt")
    sub.add_parser("mon", help="run the dashboard TUI (used as window 0)")
    sub.add_parser("dash", help="alias for mon")
    pls = sub.add_parser("ls", help="plain table of live + parked sessions")
    pls.add_argument("-a", "--all", action="store_true",
                     help="also list closed sessions (resumable history)")
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

    sub.add_parser("restore", help="relaunch every parked session (what a reboot left behind)")
    prs = sub.add_parser("resume", help="relaunch one parked or closed session by name/id")
    prs.add_argument("target", help="tab name, --name, or session id (prefix ok)")
    pf = sub.add_parser("forget", help="drop a parked/closed session from the roster")
    pf.add_argument("target", help="tab name, --name, or session id (prefix ok)")
    px = sub.add_parser("_exited", help=argparse.SUPPRESS)   # exit hook from the tab wrapper
    px.add_argument("sid")

    pt = sub.add_parser("theme", help="show or set the colour palette (dark, light, auto)")
    pt.add_argument("name", nargs="?", choices=[*THEMES, "auto"])
    pu = sub.add_parser("update", help="update this install to the newest release")
    pu.add_argument("--check", action="store_true", help="only report whether one exists")
    pu.add_argument("--main", action="store_true", help="track the tip of main instead of releases")
    sub.add_parser("version", help="print the version")

    if sys.argv[1:2] == ["new"]:
        # `fleet new yolo -C dir -n name "prompt"`: plain parse_args stops
        # collecting positionals at the first option, then rejects the prompt.
        # intermixed parsing takes the prompt from anywhere on the line.
        args = pn.parse_intermixed_args(sys.argv[2:])
        args.cmd = "new"
    else:
        args = ap.parse_args()
    cmd = args.cmd or "up"

    if cmd == "up":
        cmd_up()
    elif cmd == "restart":
        sys.exit(cmd_restart(hard=args.hard, yes=args.yes))
    elif cmd in ("dash", "mon"):
        cmd_mon()
    elif cmd == "ls":
        cmd_ls(show_all=args.all)
    elif cmd == "restore":
        sys.exit(cmd_restore())
    elif cmd == "resume":
        sys.exit(cmd_resume(args.target))
    elif cmd == "forget":
        sys.exit(cmd_forget(args.target))
    elif cmd == "_exited":
        roster_close(args.sid, "exited")
    elif cmd == "theme":
        sys.exit(cmd_theme(args.name))
    elif cmd == "update":
        sys.exit(cmd_update(check_only=args.check, track_main=args.main))
    elif cmd == "version":
        print(f"fleet v{__version__}")
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
