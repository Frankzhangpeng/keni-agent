#!/usr/bin/env bash
# Remove the macOS/Linux user service. Add --purge-token to remove pairing data.

set -euo pipefail
PURGE_TOKEN=0
if [[ "${1:-}" == "--purge-token" ]]; then PURGE_TOKEN=1; fi

case "$(uname -s)" in
  Darwin)
    LABEL="com.keni.agent"
    PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✅  macOS LaunchAgent removed"
    ;;
  Linux)
    systemctl --user disable --now keni-agent.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/keni-agent.service"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "✅  Linux systemd user service removed"
    ;;
  *)
    echo "❌  Use uninstall.ps1 on Windows" >&2
    exit 2
    ;;
esac

if [[ "$PURGE_TOKEN" -eq 1 ]]; then
  rm -f "$HOME/.superapp_agent.json"
  echo "✅  Pairing token and local agent key removed"
else
  echo "ℹ️  Pairing data kept at ~/.superapp_agent.json"
fi
