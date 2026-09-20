#!/usr/bin/env bash
# fleet installer — puts a `fleet` command on your PATH pointing at this checkout.
# Everything else (presets.json, fleet.tmux.conf, logs) stays here in the repo,
# so `git pull` updates the tool in place.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/fleet"

# --- prerequisites ----------------------------------------------------------
missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v tmux    >/dev/null || missing+=("tmux")
command -v claude  >/dev/null || missing+=("claude (Claude Code CLI)")
if [ ${#missing[@]} -gt 0 ]; then
  echo "fleet needs: ${missing[*]}" >&2
  echo "install them first, then re-run ./install.sh" >&2
  exit 1
fi

python3 - <<'PY' || { echo "fleet needs python3 with the curses module" >&2; exit 1; }
import curses  # noqa: F401
PY

if [ ! -d "$HOME/.claude/sessions" ]; then
  echo "warning: ~/.claude/sessions not found — fleet reads Claude Code's live session"
  echo "         registry from there. Run \`claude\` once, then try again."
fi

# --- install ----------------------------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$TARGET" <<EOF
#!/usr/bin/env bash
exec python3 "$REPO/fleet.py" "\$@"
EOF
chmod +x "$TARGET"

echo "installed: $TARGET  ->  $REPO/fleet.py"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo
     echo "NOTE: $BIN_DIR is not on your PATH. Add this to ~/.bashrc or ~/.zshrc:"
     echo "      export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo
echo "next: run  fleet   (opens the fleet tmux session with the dashboard)"
