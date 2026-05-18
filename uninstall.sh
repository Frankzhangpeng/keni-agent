#!/usr/bin/env bash
# keni-agent 卸载 —— bootout LaunchAgent + 删 plist + 删 token 缓存(可选)

set -euo pipefail
LABEL="com.keni.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "✅  LaunchAgent 已移除"

read -p "是否也删除 token 缓存 ~/.superapp_agent.json ? (y/N) " yn
if [[ "$yn" == "y" || "$yn" == "Y" ]]; then
  rm -f "$HOME/.superapp_agent.json"
  echo "✅  token 缓存已删"
fi

echo ""
echo "💡  Python 包(websockets)留着没删 —— pip install --user 装的,要清自己跑:"
echo "    python3 -m pip uninstall websockets"
