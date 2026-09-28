#!/usr/bin/env bash
# fleet installer — macOS and Linux (incl. WSL). Two ways to run it:
#
#   curl -fsSL https://raw.githubusercontent.com/jordanshamai/fleet/main/install.sh | bash
#       clones the newest release into ~/.fleet/app and puts `fleet` on your PATH
#
#   ./install.sh
#       from a checkout: installs THAT checkout (no clone) — for hacking on fleet
#
# Options / env:
#   -y, --yes        answer yes to "install tmux/git now?" prompts   (FLEET_YES=1)
#   --main           track the tip of main instead of releases      (FLEET_CHANNEL=main)
#   --uninstall      remove the `fleet` command (and optionally ~/.fleet/app)
#   FLEET_HOME       user state dir, default ~/.fleet  (config, roster, the app clone)
#   BIN_DIR          where the `fleet` command goes, default ~/.local/bin
set -euo pipefail

REPO="jordanshamai/fleet"
FLEET_HOME="${FLEET_HOME:-$HOME/.fleet}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
CHANNEL="${FLEET_CHANNEL:-release}"
YES="${FLEET_YES:-}"
UNINSTALL=""
for a in "$@"; do
  case "$a" in
    -y|--yes)    YES=1 ;;
    --main)      CHANNEL=main ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)   sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
    *) printf 'fleet install: unknown option %s\n' "$a" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'fleet install: %s\n' "$*" >&2; exit 1; }
ask() {   # ask "question" -> yes(0)/no(1). -y answers yes; no terminal answers no.
  [ -n "$YES" ] && return 0
  [ -r /dev/tty ] || return 1
  printf '%s [y/N] ' "$1" > /dev/tty
  read -r ans < /dev/tty || return 1
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --- uninstall ---------------------------------------------------------------
if [ -n "$UNINSTALL" ]; then
  rm -f "$BIN_DIR/fleet" && say "removed $BIN_DIR/fleet"
  if [ -d "$FLEET_HOME/app" ] && ask "also delete the app clone $FLEET_HOME/app? (your roster + config in $FLEET_HOME stay)"; then
    rm -rf "$FLEET_HOME/app" && say "removed $FLEET_HOME/app"
  fi
  say "(to forget remembered sessions too: rm -rf $FLEET_HOME)"
  exit 0
fi

# --- local checkout, or clone? -------------------------------------------------
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$(dirname "$SELF")/fleet.py" ]; then
  APP="$(cd "$(dirname "$SELF")" && pwd)"; MODE=local
else
  APP="$FLEET_HOME/app"; MODE=clone
fi

# --- prerequisites -------------------------------------------------------------
pm=""
if   command -v brew    >/dev/null 2>&1; then pm="brew install"
elif command -v apt-get >/dev/null 2>&1; then pm="sudo apt-get install -y"
elif command -v dnf     >/dev/null 2>&1; then pm="sudo dnf install -y"
elif command -v pacman  >/dev/null 2>&1; then pm="sudo pacman -S --noconfirm"
elif command -v zypper  >/dev/null 2>&1; then pm="sudo zypper install -y"
elif command -v apk     >/dev/null 2>&1; then pm="sudo apk add"
fi

need() {   # need <command> <package> <human name>
  command -v "$1" >/dev/null 2>&1 && return 0
  say "missing: $3"
  if [ -n "$pm" ] && ask "install it now with '$pm $2'?"; then
    $pm "$2" || die "couldn't install $2"
    command -v "$1" >/dev/null 2>&1 || die "$1 still not found after installing $2"
  else
    die "install $3 and re-run"
  fi
}
need python3 python3 "python3"
need tmux    tmux    "tmux 3.2+"
[ "$MODE" = clone ] && need git git "git"
python3 -c 'import curses' 2>/dev/null \
  || die "python3 has no curses module (Debian/Ubuntu: apt-get install python3 — it's in the stdlib elsewhere)"
tv="$(tmux -V 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
if [ -n "$tv" ] && [ "$(printf '%s\n3.2\n' "$tv" | sort -V | head -1)" != "3.2" ]; then
  say "warning: tmux $tv is older than 3.2 — the tab menus and some keys may not work"
fi
command -v claude >/dev/null 2>&1 \
  || say "note: 'claude' (Claude Code) isn't on your PATH — fleet needs it to launch sessions." \
         "Install it from https://claude.com/claude-code and run it once."

# --- get the code ----------------------------------------------------------------
if [ "$MODE" = clone ]; then
  mkdir -p "$FLEET_HOME"
  if [ -d "$APP/.git" ]; then
    git -C "$APP" fetch --tags --quiet origin
  else
    say "cloning https://github.com/$REPO into $APP"
    git clone --quiet "https://github.com/$REPO.git" "$APP"
  fi
  if [ "$CHANNEL" = main ]; then
    git -C "$APP" checkout --quiet main
    git -C "$APP" pull --ff-only --quiet origin main
  else
    tag="$(git -C "$APP" tag --list 'v*' --sort=-v:refname | head -1)"
    [ -n "$tag" ] || die "no release tags found in $REPO"
    git -C "$APP" checkout --quiet "$tag"
  fi
fi

# --- the `fleet` command -----------------------------------------------------------
mkdir -p "$BIN_DIR"
printf '#!/usr/bin/env bash\nexec python3 "%s/fleet.py" "$@"\n' "$APP" > "$BIN_DIR/fleet"
chmod +x "$BIN_DIR/fleet"
say "installed $("$BIN_DIR/fleet" version)  ->  $APP"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say
     say "NOTE: $BIN_DIR is not on your PATH. Add this to ~/.zshrc or ~/.bashrc:"
     say "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
[ -d "$HOME/.claude/sessions" ] \
  || say "note: ~/.claude/sessions doesn't exist yet — run \`claude\` once so it appears."
say
say "next:  fleet                (dashboard + agent tabs, one terminal)"
say "       fleet theme light    if your terminal has a light background"
say "       fleet update         later, to move to the newest release"
