#!/usr/bin/env bash
# keni-agent install — 装 Python 依赖 + 写 LaunchAgent + 开机自启
#
# 用法:
#   bash install.sh --backend ws://你的服务器:8080/api/v1/agent/ws [--pair 6位码]
#
# 不传 --pair 就只装服务、不配对(后续手动跑 keni_agent.py --pair XXXXXX)。
# 重新装会覆盖 plist + 重载 launchd。

set -euo pipefail

BACKEND=""
PAIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend) BACKEND="$2"; shift 2 ;;
    --pair)    PAIR="$2";    shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [[ -z "$BACKEND" ]]; then
  echo "❌  必须传 --backend ws://你的服务器:8080/api/v1/agent/ws"
  echo "    APP → 远程控制 → 生成配对码,会复制带 backend 的完整命令"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "❌  没找到 python3。先 brew bundle 装一下:"
  echo "    cd $REPO_DIR && brew bundle"
  exit 1
fi

echo "📦  pip install -r requirements.txt ..."
"$PY" -m pip install --user -r "$REPO_DIR/requirements.txt" >/dev/null

# 配对(可选)—— 先跑一次 keni_agent.py --pair 把 token 缓存到 ~/.superapp_agent.json
if [[ -n "$PAIR" ]]; then
  echo "🔑  使用配对码 $PAIR 换 token..."
  "$PY" "$REPO_DIR/keni_agent.py" --pair "$PAIR" --backend "$BACKEND" --token "" 2>&1 | head -5 || true
  # --token "" 走的还是 pair 分支,但显式传空避免 argparse 误用历史 token
  # 真实兜底:redeem 失败 keni_agent.py 自己 sys.exit(1),这里 || true 让 install 继续
fi

# 写 LaunchAgent plist —— 用 sed 把模板里的 {{ }} 替换掉
LABEL="com.keni.agent"
PLIST_SRC="$REPO_DIR/com.keni.agent.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"

mkdir -p "$HOME/Library/LaunchAgents"
sed \
  -e "s|{{PYTHON}}|$PY|g" \
  -e "s|{{SCRIPT}}|$REPO_DIR/keni_agent.py|g" \
  -e "s|{{BACKEND}}|$BACKEND|g" \
  -e "s|{{HOME}}|$HOME|g" \
  -e "s|{{LABEL}}|$LABEL|g" \
  "$PLIST_SRC" > "$PLIST_DST"

# 重载 launchd —— bootout 老的(不存在也没事)+ bootstrap 新的
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"

echo ""
echo "✅  keni-agent 已安装并启动"
echo "    plist : $PLIST_DST"
echo "    log   : $LOG_DIR/keni-agent.out.log"
echo "    err   : $LOG_DIR/keni-agent.err.log"
echo ""
echo "📋  常用命令:"
echo "    停止:    launchctl bootout  gui/\$(id -u)/$LABEL"
echo "    重启:    launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "    看日志:  tail -f $LOG_DIR/keni-agent.out.log"
echo "    卸载:    bash uninstall.sh"
