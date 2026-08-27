#!/usr/bin/env bash
# keni-agent installer for macOS and Linux.
# Usage: bash install.sh --backend wss://host/api/v1/agent/ws [--pair CODE]

set -euo pipefail

BACKEND=""
PAIR=""
SANDBOX=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend) BACKEND="${2:-}"; shift 2 ;;
    --pair) PAIR="${2:-}"; shift 2 ;;
    --sandbox) SANDBOX="${2:-}"; shift 2 ;;
    -h|--help) sed -n 's/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "❌  Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$BACKEND" ]]; then
  echo "❌  --backend is required" >&2
  exit 2
fi

OS_NAME="$(uname -s)"
if [[ "$OS_NAME" != "Darwin" && "$OS_NAME" != "Linux" ]]; then
  echo "❌  install.sh supports macOS/Linux. On Windows run install.ps1 in PowerShell." >&2
  exit 2
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "❌  Python 3 is required. Install Python 3, then run this command again." >&2
  exit 1
fi

VENV_DIR="$REPO_DIR/.venv"
if ! "$PYTHON" -m venv "$VENV_DIR"; then
  echo "❌  Python venv is unavailable." >&2
  if [[ "$OS_NAME" == "Linux" ]]; then
    echo "    Debian/Ubuntu: sudo apt install python3-venv" >&2
  else
    echo "    Install a current Python 3 from python.org or Homebrew." >&2
  fi
  exit 1
fi

VENV_PY="$VENV_DIR/bin/python"
echo "📦  Installing agent dependencies in an isolated virtual environment..."
"$VENV_PY" -m pip install --disable-pip-version-check -r "$REPO_DIR/requirements.txt"

if [[ -n "$PAIR" ]]; then
  echo "🔑  Redeeming the one-time pairing code..."
  "$VENV_PY" "$REPO_DIR/keni_agent.py" --pair "$PAIR" --pair-only --backend "$BACKEND"
fi

RUNNER="$REPO_DIR/run-agent.sh"
printf '#!/usr/bin/env bash\nset -euo pipefail\nexport PYTHONUNBUFFERED=1\n' > "$RUNNER"
if [[ -n "$SANDBOX" ]]; then
  printf 'export KENI_SANDBOX=%q\n' "$SANDBOX" >> "$RUNNER"
fi
printf 'exec %q %q --backend %q\n' "$VENV_PY" "$REPO_DIR/keni_agent.py" "$BACKEND" >> "$RUNNER"
chmod 700 "$RUNNER"

sed_replacement() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

if [[ "$OS_NAME" == "Darwin" ]]; then
  LABEL="com.keni.agent"
  PLIST_SRC="$REPO_DIR/com.keni.agent.plist.template"
  PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  LOG_DIR="$HOME/Library/Logs"
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  RUNNER_SED="$(sed_replacement "$(xml_escape "$RUNNER")")"
  HOME_SED="$(sed_replacement "$(xml_escape "$HOME")")"
  sed \
    -e "s|{{RUNNER}}|$RUNNER_SED|g" \
    -e "s|{{HOME}}|$HOME_SED|g" \
    -e "s|{{LABEL}}|$LABEL|g" \
    "$PLIST_SRC" > "$PLIST_DST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
  launchctl enable "gui/$(id -u)/$LABEL"
  echo "✅  keni-agent is running as a macOS LaunchAgent"
  echo "    Logs: $LOG_DIR/keni-agent.{out,err}.log"
else
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "❌  systemd user services are required on this Linux release." >&2
    echo "    You can still debug with: $RUNNER" >&2
    exit 1
  fi
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT_FILE="$UNIT_DIR/keni-agent.service"
  mkdir -p "$UNIT_DIR"
  RUNNER_SED="$(sed_replacement "$RUNNER")"
  HOME_SED="$(sed_replacement "$HOME")"
  sed \
    -e "s|{{RUNNER}}|$RUNNER_SED|g" \
    -e "s|{{WORKDIR}}|$HOME_SED|g" \
    "$REPO_DIR/keni-agent.service.template" > "$UNIT_FILE"
  systemctl --user daemon-reload
  systemctl --user enable --now keni-agent.service
  echo "✅  keni-agent is running as a Linux systemd user service"
  echo "    Logs: journalctl --user -u keni-agent -f"
fi

echo "    Uninstall: bash $REPO_DIR/uninstall.sh"
