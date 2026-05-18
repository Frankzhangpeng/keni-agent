#!/usr/bin/env python3
"""
keni Mac Agent — 手机远程控制 Mac 的本地守护脚本

用法（首次）:
  python3 keni_agent.py --pair 你的6位码 --backend ws://你的服务器:8080/api/v1/agent/ws

用法（后续，token 已缓存）:
  python3 keni_agent.py --backend ws://你的服务器:8080/api/v1/agent/ws

也可以把 backend URL 写到环境变量,免每次传:
  export KENI_BACKEND_URL=ws://你的服务器:8080/api/v1/agent/ws

依赖: pip3 install -r requirements.txt
"""

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import argparse
import urllib.request
import urllib.parse
import socket

# ── 安装依赖 ──────────────────────────────────────────────
try:
    import websockets
except ImportError:
    print("Installing websockets...")
    subprocess.run([sys.executable, "-m", "pip", "install", "websockets"], check=True)
    import websockets

# ── 白名单 ─────────────────────────────────────────────────

# 无需确认：只读 / 分析类
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "find", "pwd", "echo",
    "git",      # git 子命令单独判断
    "go",       # go build / test / fmt / vet
    "flutter",  # flutter analyze / test
    "python3", "python", "node",
    "which", "env", "printenv", "df", "du", "ps",
}

SAFE_GIT_SUBCMDS  = {"status", "log", "diff", "branch", "remote", "show", "stash", "fetch"}
CONFIRM_GIT_SUBCMDS = {"commit", "push", "reset", "rebase", "merge", "checkout", "pull", "clone"}

# 需要确认：写入 / 破坏性
CONFIRM_COMMANDS = {
    "rm", "mv", "cp", "mkdir", "touch", "chmod", "chown",
    "brew", "npm", "yarn", "pip3", "pip",
    "curl", "wget",
    "kill", "pkill",
    "open",
}

# 完全禁止
BANNED_COMMANDS = {"sudo", "su", "bash", "sh", "zsh", "nc", "ncat"}


# ── NL Provider 注册表 ─────────────────────────────────────
# 手机端 cmd_type 来这里查 → 拼实际 argv。新增 LLM CLI 在这里加一行即可。
#   level: 默认安全级别(全部 confirm — NL 输入永远要弹确认)
#   build: instruction(用户 prompt) → argv list
# 想加 cursor / aider / 自定义 IDE bridge:照样在 PROVIDERS 加 entry,然后
# 同步 super 仓库 backend handlers/actions.go execOpenRemoteControl 白名单 +
# backend handlers/agent.go RouteAgentExec 的 NL provider 白名单(让
# memory_context 注入也走到这个新 provider)。
PROVIDERS: dict[str, dict] = {
    "claude_code": {
        "level": "confirm",
        "build": lambda inst: ["claude", "--print", inst],
    },
    "openclaw": {
        "level": "confirm",
        "build": lambda inst: ["openclaw", "review", "--stdin"],
        # stdin: instruction 写进 stdin 而不是 argv(防止巨型 prompt 撑爆 argv)
        "stdin": True,
    },
    "codex": {
        "level": "confirm",
        "build": lambda inst: ["codex", "exec", inst],
    },
    "cursor": {
        "level": "confirm",
        "build": lambda inst: ["cursor-agent", "--prompt", inst],
    },
}


def classify(cmd_type: str, instruction: str) -> str:
    """返回 'safe' | 'confirm' | 'banned'"""
    if cmd_type in PROVIDERS:
        return PROVIDERS[cmd_type]["level"]

    parts = shlex.split(instruction) if instruction.strip() else []
    if not parts:
        return "banned"

    base = parts[0].lower()
    if base in BANNED_COMMANDS:
        return "banned"
    if base in CONFIRM_COMMANDS:
        return "confirm"
    if base == "git":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub in SAFE_GIT_SUBCMDS:
            return "safe"
        if sub in CONFIRM_GIT_SUBCMDS:
            return "confirm"
        return "confirm"
    if base in SAFE_COMMANDS:
        return "safe"
    # 未知命令默认需确认
    return "confirm"


# ── macOS 原生确认弹窗 ──────────────────────────────────────

def macos_dialog(title: str, message: str) -> bool:
    escaped = message.replace('"', '\\"')
    script = (
        f'display dialog "{escaped}" '
        f'buttons {{"拒绝", "允许"}} '
        f'default button "允许" '
        f'with icon caution '
        f'with title "{title}"'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return "允许" in r.stdout


# ── 命令执行（流式输出）─────────────────────────────────────

# 全局 active session 表：exec_id → SessionInfo（proc / 元数据）
# 用于手机端可视化「正在跑的会话」+ 单条 kill
import time as _time  # 避免和上面 import 顺序冲突


class SessionInfo:
    __slots__ = ("exec_id", "cmd_type", "instruction", "started_at", "proc")

    def __init__(self, exec_id, cmd_type, instruction):
        self.exec_id    = exec_id
        self.cmd_type   = cmd_type
        self.instruction = instruction
        self.started_at = _time.time()
        self.proc       = None  # 子进程实例（asyncio.subprocess.Process）


ACTIVE_SESSIONS: dict[str, SessionInfo] = {}

# 由 agent_registered 帧从 backend 拿到；上报会话时附在帧里，方便手机端归属
SELF_AGENT_ID: str = ""


async def broadcast_sessions(ws):
    """把当前所有正在跑的会话上报给手机端 —— 用户能看到 / kill。"""
    sessions = [
        {
            "exec_id":    s.exec_id,
            "cmd_type":   s.cmd_type,
            "instruction": s.instruction[:200],  # 截断超长 NL prompt
            "started_at": int(s.started_at),
        }
        for s in ACTIVE_SESSIONS.values()
    ]
    try:
        await ws.send(json.dumps({
            "type":     "agent_sessions",
            "agent_id": SELF_AGENT_ID,
            "sessions": sessions,
        }, ensure_ascii=False))
    except Exception:
        pass


async def execute(ws, exec_id: str, cmd_type: str, instruction: str, working_dir: str):
    use_stdin = False
    try:
        if cmd_type in PROVIDERS:
            provider = PROVIDERS[cmd_type]
            cmd = provider["build"](instruction)
            use_stdin = bool(provider.get("stdin"))
        else:
            cmd = shlex.split(instruction)
    except ValueError as e:
        await send_output(ws, exec_id, f"命令解析失败: {e}\n", done=True, exit_code=1)
        return

    if not os.path.isdir(working_dir):
        working_dir = os.path.expanduser("~")

    sess = SessionInfo(exec_id, cmd_type, instruction)
    ACTIVE_SESSIONS[exec_id] = sess
    await broadcast_sessions(ws)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if use_stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
            env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1"},
        )
        sess.proc = proc

        if use_stdin and proc.stdin is not None:
            try:
                proc.stdin.write(instruction.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:
                pass

        while True:
            chunk = await proc.stdout.read(512)
            if not chunk:
                break
            await send_output(ws, exec_id, chunk.decode("utf-8", errors="replace"), done=False)

        exit_code = await proc.wait()
        await send_output(ws, exec_id, "", done=True, exit_code=exit_code)

    except FileNotFoundError:
        cmd_name = cmd[0] if cmd else instruction
        await send_output(ws, exec_id, f"命令未找到: {cmd_name}\n", done=True, exit_code=127)
    except Exception as e:
        await send_output(ws, exec_id, f"执行错误: {e}\n", done=True, exit_code=1)
    finally:
        ACTIVE_SESSIONS.pop(exec_id, None)
        await broadcast_sessions(ws)


def kill_session(exec_id: str) -> bool:
    """手机端请求 kill 指定 exec_id。返回是否找到并 terminate。"""
    sess = ACTIVE_SESSIONS.get(exec_id)
    if not sess or not sess.proc:
        return False
    try:
        sess.proc.terminate()
        return True
    except Exception:
        return False


async def send_output(ws, exec_id: str, output: str, *, done: bool, exit_code: int = 0):
    msg: dict = {"type": "agent_output", "exec_id": exec_id, "output": output, "done": done}
    if done:
        msg["exit_code"] = exit_code
    await ws.send(json.dumps(msg, ensure_ascii=False))


# ── 主 Agent 循环 ──────────────────────────────────────────

async def run(backend_url: str, token: str, device_name: str, default_dir: str):
    uri = f"{backend_url}?token={token}&device_name={device_name}"
    # exec_id → asyncio.Event + result dict（用于等待手机确认回复）
    pending: dict[str, tuple[asyncio.Event, dict]] = {}

    print(f"🔌  Connecting to {backend_url} ...")

    async for ws in websockets.connect(uri, ping_interval=25, ping_timeout=15):
        try:
            print(f"✅  Agent '{device_name}' connected!")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                t = msg.get("type")

                # ── 注册确认 ────────────────────────────────
                if t == "agent_registered":
                    global SELF_AGENT_ID
                    SELF_AGENT_ID = msg.get("agent_id", "") or ""
                    print(f"🆔  Agent ID : {SELF_AGENT_ID}")

                # ── 断开指令 ────────────────────────────────
                elif t == "agent_disconnect":
                    print("👋  收到断开指令，Agent 退出")
                    return

                # ── 执行命令 ────────────────────────────────
                elif t == "agent_exec":
                    asyncio.create_task(handle_exec(ws, msg, pending, default_dir))

                # ── 手机确认回复 ─────────────────────────────
                elif t == "agent_confirm_response":
                    exec_id = msg.get("exec_id", "")
                    if exec_id in pending:
                        event, result = pending[exec_id]
                        result["approved"] = msg.get("approved", False)
                        event.set()

                # ── 列出当前正在跑的会话（手机切到 tab 时拉一次）─
                elif t == "agent_sessions_request":
                    await broadcast_sessions(ws)

                # ── 杀掉指定会话 ─────────────────────────────
                elif t == "agent_kill_exec":
                    target_exec = msg.get("exec_id", "")
                    if kill_session(target_exec):
                        print(f"🔪  Killed exec_id={target_exec}")
                    else:
                        print(f"⚠️   Kill: exec_id={target_exec} not found")

        except websockets.ConnectionClosed:
            print("⚠️  Connection closed, reconnecting in 5s...")
            await asyncio.sleep(5)


async def handle_exec(ws, msg: dict, pending: dict, default_dir: str):
    exec_id    = msg.get("exec_id", "")
    cmd_type   = msg.get("cmd_type", "shell")
    instruction = msg.get("instruction", "").strip()
    working_dir = msg.get("working_dir") or default_dir
    agent_id    = msg.get("agent_id", "")
    # 后端在转发任何 NL provider（claude_code/openclaw/codex/cursor）指令时
    # 会附带 Soul Memory（习惯/偏好/关系/事实），让 CLI 能理解"按我平时的习惯
    # 整理文件夹"这种指代。shell 不会被注入。
    memory_ctx = (msg.get("memory_context") or "").strip()
    if cmd_type in PROVIDERS and memory_ctx:
        instruction = f"{memory_ctx}\n\n## 用户指令\n{instruction}"

    print(f"📥  [{cmd_type}] {instruction[:80]}")

    level = classify(cmd_type, instruction)

    if level == "banned":
        await send_output(ws, exec_id, f"🚫 命令已禁止: {instruction.split()[0]}\n", done=True, exit_code=126)
        return

    if level == "confirm":
        # 1. 向手机发送确认请求
        await ws.send(json.dumps({
            "type":        "agent_confirm_request",
            "exec_id":     exec_id,
            "agent_id":    agent_id,
            "cmd_type":    cmd_type,
            "instruction": instruction,
            "prompt":      f"将要执行:\n{instruction}",
        }, ensure_ascii=False))

        # 2. 同时在 Mac 弹窗（asyncio 线程池执行阻塞调用）
        event  = asyncio.Event()
        result = {"approved": False}
        pending[exec_id] = (event, result)

        loop = asyncio.get_event_loop()
        mac_approved = await loop.run_in_executor(
            None, macos_dialog, "知己 · 远程指令", f"将要执行:\n{instruction}"
        )

        # 哪边先确认都行：Mac 弹窗 or 手机端
        if mac_approved:
            result["approved"] = True
            event.set()
        else:
            # 等待手机端回复（最多 60s）
            try:
                await asyncio.wait_for(event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

        pending.pop(exec_id, None)

        if not result["approved"]:
            print(f"❌  Rejected: {instruction[:60]}")
            await send_output(ws, exec_id, "❌ 已拒绝执行\n", done=True, exit_code=130)
            return

    print(f"▶️   Executing: {instruction[:60]}")
    await execute(ws, exec_id, cmd_type, instruction, working_dir)


# ── Token 缓存 ────────────────────────────────────────────
# 文件名继续用 ~/.superapp_agent.json — 老用户升级时 token 不重置。

CACHE_FILE = os.path.expanduser("~/.superapp_agent.json")

def load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(data: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)
    os.chmod(CACHE_FILE, 0o600)

def login(http_base: str, email: str, password: str) -> str:
    """调用后端登录接口，返回 JWT token"""
    url = f"{http_base}/api/v1/auth/login"
    body = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            token = data.get("token", "")
            if not token:
                print(f"❌  登录失败：{data.get('error', '未知错误')}")
                sys.exit(1)
            return token
    except Exception as e:
        print(f"❌  登录请求失败: {e}")
        sys.exit(1)


def redeem_pair_code(http_base: str, code: str) -> str:
    """用一次性配对码（在 keni APP 里生成）换 JWT。
    免去在 Mac 上输入邮箱密码——配对码 5 分钟有效，单次使用。"""
    url = f"{http_base}/api/v1/agent/pair/redeem"
    body = json.dumps({"code": code.strip().upper()}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            token = data.get("token", "")
            if not token:
                print(f"❌  配对失败：{data.get('error', '未知错误')}")
                sys.exit(1)
            return token
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", str(e))
        except Exception:
            err = str(e)
        print(f"❌  配对码无效或已过期：{err}")
        print("    请在 keni APP 里 → 远程控制 → 重新生成配对码")
        sys.exit(1)
    except Exception as e:
        print(f"❌  配对请求失败: {e}")
        sys.exit(1)

def ws_to_http(ws_url: str) -> str:
    """把 ws:// 转成 http://，wss:// 转成 https://"""
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")

def default_device_name() -> str:
    return socket.gethostname()

# ── 入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="keni Mac Agent")
    parser.add_argument("--pair",     help="一次性配对码（在 keni APP → 远程控制 里生成，6 位字母数字）")
    parser.add_argument("--email",    help="账号邮箱（旧登录方式，建议改用 --pair）")
    parser.add_argument("--password", help="账号密码（旧登录方式，建议改用 --pair）")
    parser.add_argument("--token",    help="直接指定 JWT token（可选，优先级最高）")
    parser.add_argument("--device",   default=None, help="设备显示名称（默认取主机名）")
    parser.add_argument("--backend",  default=os.environ.get("KENI_BACKEND_URL", "ws://localhost:8080/api/v1/agent/ws"),
                        help="WS backend URL,默认读 KENI_BACKEND_URL 环境变量,再不行 fallback localhost")
    parser.add_argument("--dir",      default=os.getcwd(), help="默认工作目录")
    args = parser.parse_args()

    cache = load_cache()

    # 解析 token（优先级：命令行 token > 配对码 > 邮箱密码 > 缓存）
    token = args.token or ""
    if not token and args.pair:
        http_base = ws_to_http(args.backend).rsplit("/api/", 1)[0]
        print(f"🔑  使用配对码登录 ({args.pair.upper()})...")
        token = redeem_pair_code(http_base, args.pair)
        cache["token"] = token
        cache["backend"] = args.backend
        save_cache(cache)
        print("✅  配对成功，token 已缓存到 ~/.superapp_agent.json")
    if not token and args.email and args.password:
        http_base = ws_to_http(args.backend).rsplit("/api/", 1)[0]
        print(f"🔑  登录中 ({args.email})...")
        token = login(http_base, args.email, args.password)
        cache["token"] = token
        cache["backend"] = args.backend
        save_cache(cache)
        print("✅  登录成功，token 已缓存到 ~/.superapp_agent.json")
    if not token:
        token = cache.get("token", "")
    if not token:
        print("❌  未找到 token。请用一次性配对码登录：")
        print("    1) 打开 keni APP → 远程控制 → 生成配对码")
        print("    2) python3 keni_agent.py --pair 你的6位码 --backend <你的WS URL>")
        sys.exit(1)

    device = args.device or cache.get("device") or default_device_name()
    cache["device"] = device
    save_cache(cache)

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    try:
        asyncio.run(run(args.backend, token, device, args.dir))
    except (KeyboardInterrupt, SystemExit):
        print("\n👋  Agent stopped")


if __name__ == "__main__":
    main()
