#!/usr/bin/env python3
"""
keni Desktop Agent — 手机远程控制 macOS / Windows / Linux 的本地守护脚本

用法（首次）:
  python3 keni_agent.py --pair 你的6位码 --backend ws://你的服务器:8080/api/v1/agent/ws

用法（后续，token 已缓存）:
  python3 keni_agent.py --backend ws://你的服务器:8080/api/v1/agent/ws

也可以把 backend URL 写到环境变量,免每次传:
  export KENI_BACKEND_URL=ws://你的服务器:8080/api/v1/agent/ws

依赖: pip3 install -r requirements.txt
"""

import asyncio
import base64
import json
import os
import platform
import shlex
import shutil
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
except ImportError:  # pragma: no cover - installer creates a venv with this dependency
    websockets = None
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover - installer creates a venv with this dependency
    serialization = None
    Ed25519PrivateKey = None

# ── 重连 / 心跳参数(可经环境变量外置覆盖,免改码调优)────────────────
# 瞬时失败(网络抖动 / backend 重启 / 临时拒绝)指数退避重连:base 起步、×2 增长、封顶 max,
# 连上后重置 base。避免之前那种固定间隔在长时间断网时的无效高频重连。
RECONNECT_BASE = float(os.environ.get("KENI_RECONNECT_BASE", "2"))   # 首次退避秒数
RECONNECT_MAX = float(os.environ.get("KENI_RECONNECT_MAX", "60"))    # 退避上限秒数
PING_INTERVAL = float(os.environ.get("KENI_PING_INTERVAL", "25"))    # ws 心跳间隔
PING_TIMEOUT = float(os.environ.get("KENI_PING_TIMEOUT", "15"))      # ws 心跳超时
EXEC_PROGRESS_INTERVAL = float(os.environ.get("KENI_EXEC_PROGRESS_INTERVAL", "15"))
MAX_ACTIVE_SESSIONS = 3
AGENT_PROTOCOL_VERSION = "2"


def agent_platform() -> str:
    """Return the backend protocol OS token, never a display label."""
    system = platform.system().lower()
    return {
        "darwin": "darwin",
        "windows": "windows",
        "linux": "linux",
    }.get(system, system[:32] or "unknown")


def agent_arch() -> str:
    machine = platform.machine().lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine[:32] or "unknown")


def detect_sandbox_runtime() -> str:
    """Report only a sandbox that this process can actually use for child jobs."""
    override = os.environ.get("KENI_SANDBOX", "").strip().lower()
    current = agent_platform()
    allowed = {
        "darwin": {"sandbox-exec", "none"},
        "linux": {"nsjail", "firejail", "none"},
        "windows": {"none"},
    }.get(current, {"none"})
    if override:
        return override if override in allowed else "none"
    if current == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if current == "linux":
        if shutil.which("nsjail"):
            return "nsjail"
        if shutil.which("firejail"):
            return "firejail"
    # Windows must remain fail-closed until a signed AppContainer launcher exists.
    return "none"


ACTIVE_SANDBOX_MODE = detect_sandbox_runtime()

MACOS_SANDBOX_PROFILE = """
(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow file-read*)
(allow file-write* (subpath (param "USER_PROJECT_DIR")))
(allow file-write* (subpath "/tmp"))
(allow network*)
""".strip()


def sandboxed_command(cmd: list[str], working_dir: str) -> list[str]:
    """Wrap one argv in the detected OS sandbox without interpolating user text."""
    mode = ACTIVE_SANDBOX_MODE
    if mode == "sandbox-exec":
        return [
            "sandbox-exec", "-p", MACOS_SANDBOX_PROFILE,
            "-D", f"USER_PROJECT_DIR={working_dir}", "--", *cmd,
        ]
    if mode == "nsjail":
        mounts = ["/usr", "/bin", "/etc"]
        for candidate in ("/lib", "/lib64", "/sbin"):
            if os.path.exists(candidate):
                mounts.append(candidate)
        prefix = [
            "nsjail", "--mode", "o", "--quiet", "--time_limit", "1800",
            "--rlimit_fsize", "524288000", "--disable_clone_newnet",
            "--cwd", working_dir,
        ]
        for mount in mounts:
            prefix.extend(["--bindmount_ro", mount])
        prefix.extend(["--bindmount", working_dir, "--bindmount", "/tmp", "--"])
        return [*prefix, *cmd]
    if mode == "firejail":
        prefix = [
            "firejail", "--quiet", "--noprofile", "--private-tmp",
            "--caps.drop=all", "--nonewprivs", f"--whitelist={working_dir}",
        ]
        for candidate in ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt"):
            if os.path.exists(candidate):
                prefix.append(f"--read-only={candidate}")
        return [*prefix, "--", *cmd]
    return cmd

# ── 白名单 ─────────────────────────────────────────────────

# 无需确认：只读 / 分析类
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "find", "pwd", "echo",
    "git",      # git 子命令单独判断
    "go",       # go build / test / fmt / vet
    "flutter",  # flutter analyze / test
    "python3", "python", "node",
    "which", "env", "printenv", "df", "du", "ps",
    "get-childitem", "get-content", "select-string", "get-location",
    "get-process", "get-command", "write-output",
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
    "copy-item", "move-item", "new-item", "remove-item", "set-content",
    "start-process", "stop-process", "invoke-webrequest", "curl.exe",
}

# 完全禁止
BANNED_COMMANDS = {
    "sudo", "su", "bash", "sh", "zsh", "nc", "ncat",
    "format-volume", "clear-disk", "initialize-disk", "stop-computer",
    "restart-computer",
}


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

    parts = shlex.split(instruction, posix=os.name != "nt") if instruction.strip() else []
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


# ── 本机原生确认弹窗 ────────────────────────────────────────

MACOS_DIALOG_SCRIPT = """
on run argv
  display dialog (item 2 of argv) buttons {"拒绝", "允许"} default button "允许" with icon caution with title (item 1 of argv) giving up after 60
end run
"""


def dialog_approved(stdout: str, returncode: int) -> bool:
    return (
        returncode == 0
        and "button returned:允许" in stdout
        and "gave up:false" in stdout
    )

def macos_dialog(title: str, message: str) -> bool:
    # 远程文本绝不能拼进 AppleScript 源码。通过 argv 传值后，反斜杠、引号、
    # 换行和 `do shell script` 都只会成为对话框字面量。
    r = subprocess.run(
        ["osascript", "-e", MACOS_DIALOG_SCRIPT, "--", title, message],
        capture_output=True,
        text=True,
    )
    return dialog_approved(r.stdout, r.returncode)


async def macos_dialog_async(title: str, message: str) -> bool:
    """可取消的生产路径；手机先决议时主动关闭仍显示在 Mac 上的旧弹窗。"""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", MACOS_DIALOG_SCRIPT, "--", title, message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    return dialog_approved(stdout.decode("utf-8", errors="replace"), proc.returncode)


WINDOWS_DIALOG_SCRIPT = r"""
Add-Type -AssemblyName PresentationFramework
$choice = [System.Windows.MessageBox]::Show(
  $env:KENI_DIALOG_MESSAGE,
  $env:KENI_DIALOG_TITLE,
  [System.Windows.MessageBoxButton]::YesNo,
  [System.Windows.MessageBoxImage]::Warning,
  [System.Windows.MessageBoxResult]::No
)
if ($choice -eq [System.Windows.MessageBoxResult]::Yes) { Write-Output 'APPROVED' }
""".strip()


async def windows_dialog_async(title: str, message: str) -> bool:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        return False
    proc = await asyncio.create_subprocess_exec(
        executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
        "-Command", WINDOWS_DIALOG_SCRIPT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "KENI_DIALOG_TITLE": title, "KENI_DIALOG_MESSAGE": message},
    )
    try:
        stdout, _ = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        raise
    return proc.returncode == 0 and stdout.decode(errors="replace").strip() == "APPROVED"


async def linux_dialog_async(title: str, message: str) -> bool:
    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog")
    if zenity:
        argv = [zenity, "--question", f"--title={title}", f"--text={message}", "--timeout=60"]
    elif kdialog:
        argv = [kdialog, "--title", title, "--warningyesno", message]
    else:
        return False
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await proc.wait() == 0
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        raise


async def local_dialog_async(title: str, message: str) -> bool:
    current = agent_platform()
    if current == "darwin":
        return await macos_dialog_async(title, message)
    if current == "windows":
        return await windows_dialog_async(title, message)
    if current == "linux":
        return await linux_dialog_async(title, message)
    return False


async def wait_for_confirmation(local_future, event, result, timeout=60) -> bool:
    """Desktop and phone race; either explicit approval wins, otherwise fail closed."""
    phone_task = asyncio.create_task(event.wait())
    waiting = {local_future, phone_task}
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while waiting:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            done, _ = await asyncio.wait(
                waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                return False
            if local_future in done:
                waiting.remove(local_future)
                if local_future.result() is True:
                    return True
            if phone_task in done:
                waiting.remove(phone_task)
                if result.get("approved") is True:
                    return True
        return False
    finally:
        phone_task.cancel()
        if not local_future.done():
            local_future.cancel()
        await asyncio.gather(phone_task, local_future, return_exceptions=True)


# ── 命令执行（流式输出）─────────────────────────────────────

# 全局 active session 表：exec_id → SessionInfo（proc / 元数据）
# 用于手机端可视化「正在跑的会话」+ 单条 kill
import time as _time  # 避免和上面 import 顺序冲突


class SessionInfo:
    __slots__ = ("exec_id", "cmd_type", "instruction", "started_at", "proc", "pgid", "killed")

    def __init__(self, exec_id, cmd_type, instruction):
        self.exec_id    = exec_id
        self.cmd_type   = cmd_type
        self.instruction = instruction
        self.started_at = _time.time()
        self.proc       = None  # 子进程实例（asyncio.subprocess.Process）
        self.pgid       = None
        self.killed     = False


ACTIVE_SESSIONS: dict[str, SessionInfo] = {}
# reservation 覆盖“等待 Mac/手机确认”与“已启动进程”两个阶段，避免跨 Pod
# 后端转发时把 3 并发保护绕成无限确认弹窗。所有变更都在 asyncio 事件循环内，
# check + add 之间没有 await，因而不需要额外锁。
EXEC_RESERVATIONS: set[str] = set()


def reserve_exec(exec_id: str) -> bool:
    if not exec_id or exec_id in EXEC_RESERVATIONS:
        return False
    if len(EXEC_RESERVATIONS) >= MAX_ACTIVE_SESSIONS:
        return False
    EXEC_RESERVATIONS.add(exec_id)
    return True


def local_reservation_key(exec_id: str) -> str:
    # 老协议允许空 exec_id；只给本机并发记账生成私有 key，不改变线上帧。
    if exec_id:
        return exec_id
    return f"legacy:{id(asyncio.current_task())}:{_time.time_ns()}"

# 由 agent_registered 帧从 backend 拿到；上报会话时附在帧里，方便手机端归属
SELF_AGENT_ID: str = ""
SELF_CONN_ID: str = ""
AGENT_PRIVATE_KEY = None
AGENT_PUBLIC_KEY_B64: str = ""


def raw_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def raw_b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def ensure_agent_identity_key(cache: dict):
    """Create one persistent Ed25519 key for WS challenge responses."""
    global AGENT_PRIVATE_KEY, AGENT_PUBLIC_KEY_B64
    if Ed25519PrivateKey is None or serialization is None:
        raise RuntimeError("cryptography is missing; reinstall keni-agent")
    encoded = str(cache.get("ed25519_private_key") or "")
    try:
        private = Ed25519PrivateKey.from_private_bytes(raw_b64decode(encoded))
    except Exception:
        private = Ed25519PrivateKey.generate()
        private_raw = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cache["ed25519_private_key"] = raw_b64(private_raw)
        save_cache(cache)
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    AGENT_PRIVATE_KEY = private
    AGENT_PUBLIC_KEY_B64 = raw_b64(public_raw)
    return private


def build_attest_message(
    nonce: str, agent_id: str, conn_id: str, sandbox_mode: str, ts_agent: int,
) -> bytes:
    fields = ["v1", nonce, agent_id, conn_id, sandbox_mode, str(ts_agent)]
    return b"\x00".join(field.encode("utf-8") for field in fields)


async def answer_attest_challenge(ws, msg: dict):
    nonce = str(msg.get("nonce") or "")
    if not nonce or not SELF_AGENT_ID or not SELF_CONN_ID or AGENT_PRIVATE_KEY is None:
        return
    ts_agent = int(_time.time())
    payload = build_attest_message(
        nonce, SELF_AGENT_ID, SELF_CONN_ID, ACTIVE_SANDBOX_MODE, ts_agent,
    )
    signature = AGENT_PRIVATE_KEY.sign(payload)
    await ws.send(json.dumps({
        "type": "agent_attest_response",
        "nonce": nonce,
        "sig": raw_b64(signature),
        "sandbox_mode": ACTIVE_SANDBOX_MODE,
        "ts_agent": ts_agent,
    }))


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


async def progress_heartbeat(ws, exec_id: str):
    """可选协议帧；老 backend 会忽略，新手机据此保持长任务为 active。"""
    while True:
        await asyncio.sleep(EXEC_PROGRESS_INTERVAL)
        sess = ACTIVE_SESSIONS.get(exec_id)
        if sess is None:
            return
        await ws.send(json.dumps({
            "type": "agent_exec_progress",
            "exec_id": exec_id,
            "started_at": int(sess.started_at),
        }))


def terminate_process_tree(proc, pgid=None) -> bool:
    """终止整个进程组，避免 shell/CLI 的子孙进程留在电脑后台。"""
    if proc is None and pgid is None:
        return False
    try:
        if os.name == "posix":
            target = pgid if pgid is not None else os.getpgid(proc.pid)
            os.killpg(target, signal.SIGTERM)
        else:
            if proc.returncode is not None:
                return False
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return True
    except (ProcessLookupError, OSError):
        return False


async def reap_process_tree(proc, pgid=None, grace: float = 5.0):
    if proc is None and pgid is None:
        return
    if os.name == "posix" and pgid is None and proc is not None:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = None
    terminate_process_tree(proc, pgid)
    deadline = asyncio.get_running_loop().time() + grace
    if proc is not None and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            pass
    if os.name == "posix" and pgid is not None:
        # leader 可能先退出；继续探测保存下来的 pgid，宽限后杀仍忽略 TERM 的子孙。
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, OSError):
                return
            await asyncio.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    elif proc is not None and proc.returncode is None:
        terminate_process_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def execute(
    ws, exec_id: str, cmd_type: str, instruction: str, working_dir: str,
    *, reservation_held: bool = False,
):
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

    working_dir = os.path.abspath(os.path.expanduser(working_dir))
    if not os.path.isdir(working_dir):
        working_dir = os.path.dirname(os.path.abspath(__file__))

    acquired_here = False
    reservation_key = exec_id
    if not reservation_held:
        reservation_key = local_reservation_key(exec_id)
    if not reservation_held and not reserve_exec(reservation_key):
        await send_output(
            ws, exec_id, "并发任务过多，请等待已有任务结束\n",
            done=True, exit_code=75, error="too_many_inflight",
        )
        return
    if not reservation_held:
        acquired_here = True

    sess = SessionInfo(exec_id, cmd_type, instruction)
    ACTIVE_SESSIONS[exec_id] = sess
    await broadcast_sessions(ws)

    progress_task = None
    try:
        if cmd_type in PROVIDERS:
            launch_cmd = cmd
        elif agent_platform() == "windows":
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if not powershell:
                raise FileNotFoundError("powershell.exe")
            launch_cmd = [
                powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", instruction,
            ]
        else:
            launch_cmd = ["/bin/sh", "-c", instruction]
        launch_cmd = sandboxed_command(launch_cmd, working_dir)

        proc_kwargs = dict(
            stdin=asyncio.subprocess.PIPE if use_stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
            env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1"},
        )
        if os.name == "posix":
            proc_kwargs["start_new_session"] = True
        else:
            proc_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # Always use argv execution. Shell semantics live only in the explicit
        # /bin/sh or PowerShell argv above, so user text never becomes a launcher argument.
        proc = await asyncio.create_subprocess_exec(*launch_cmd, **proc_kwargs)
        sess.proc = proc
        if os.name == "posix":
            sess.pgid = os.getpgid(proc.pid)
        else:
            sess.pgid = proc.pid
        progress_task = asyncio.create_task(progress_heartbeat(ws, exec_id))

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
        await send_output(
            ws,
            exec_id,
            "",
            done=True,
            exit_code=exit_code,
            error="killed" if sess.killed else None,
        )

    except asyncio.CancelledError:
        # backend 当前会把 agent 断连的 session 立即标 cancelled。兼容该语义：
        # 不在旧连接上幽灵续跑，也不自动换 exec_id 重放非幂等命令。
        await reap_process_tree(sess.proc, sess.pgid)
        raise
    except FileNotFoundError:
        cmd_name = launch_cmd[0] if 'launch_cmd' in locals() and launch_cmd else (cmd[0] if cmd else instruction)
        await send_output(ws, exec_id, f"命令未找到: {cmd_name}\n", done=True, exit_code=127)
    except Exception as e:
        # 传输失败也必须先收进程树；不能再向同一个坏 ws 发送失败信息后丢掉句柄。
        await reap_process_tree(sess.proc, sess.pgid)
        try:
            await send_output(ws, exec_id, f"执行错误: {e}\n", done=True, exit_code=1)
        except Exception:
            pass
    finally:
        if progress_task is not None:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
        ACTIVE_SESSIONS.pop(exec_id, None)
        if acquired_here:
            EXEC_RESERVATIONS.discard(reservation_key)
        await broadcast_sessions(ws)


async def kill_session(exec_id: str) -> bool:
    """手机端请求 kill 指定 exec_id。返回是否找到并 terminate。"""
    sess = ACTIVE_SESSIONS.get(exec_id)
    if not sess or not sess.proc:
        return False
    try:
        sess.killed = True
        await reap_process_tree(sess.proc, sess.pgid)
        return True
    except Exception:
        return False


async def send_output(
    ws, exec_id: str, output: str, *, done: bool, exit_code: int = 0, error=None
):
    msg: dict = {"type": "agent_output", "exec_id": exec_id, "output": output, "done": done}
    if done:
        msg["exit_code"] = exit_code
    if error:
        msg["error"] = error
    await ws.send(json.dumps(msg, ensure_ascii=False))


# ── 主 Agent 循环 ──────────────────────────────────────────

async def run(backend_url: str, token: str, device_name: str, default_dir: str):
    query = urllib.parse.urlencode({
        "token": token,
        "device_name": device_name,
        "sandbox": ACTIVE_SANDBOX_MODE,
        "proto_version": AGENT_PROTOCOL_VERSION,
        "os": agent_platform(),
        "arch": agent_arch(),
        "pubkey": AGENT_PUBLIC_KEY_B64,
    })
    uri = f"{backend_url}{'&' if '?' in backend_url else '?'}{query}"
    # exec_id → asyncio.Event + result dict（用于等待手机确认回复）
    pending: dict[str, tuple[asyncio.Event, dict]] = {}

    backoff = RECONNECT_BASE  # 瞬时失败指数退避,连上即重置
    while True:
        print(f"🔌  Connecting to {backend_url} ...")
        reconnect_with_backoff = False
        try:
            ws = await websockets.connect(uri, ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT)
        except websockets.InvalidStatus as e:
            # 升级前被 HTTP 拒(非 101)。401/403/429 = 认证/权限/限流死路:token 失效、被吊销、
            # 地区禁用、或 auth-fail 触发限流(后端 body 附 action:reauth)。这类「重连也没用」——
            # 干净退出(exit 0),靠 plist KeepAlive.SuccessfulExit=false 让 launchd 不再拉起,
            # 终止死循环刷 429。恢复:重新 --pair 后 launchctl load。
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (401, 403, 429):
                print(f"❌  服务端拒绝连接 (HTTP {code}) —— token 失效 / 被吊销 / 被限流,重连无效。")
                print(f"    请重新配对后再启动:python3 keni_agent.py --pair <新6位码> --backend {backend_url}")
                print("    已停止自动重连(干净退出,launchd 不再拉起)。")
                sys.exit(0)
            print(f"⚠️  服务端拒绝 (HTTP {code}),{backoff:.0f}s 后重试...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)
            continue
        except (OSError, asyncio.TimeoutError) as e:
            print(f"⚠️  连接失败 ({e}),{backoff:.0f}s 后重连...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)
            continue
        backoff = RECONNECT_BASE  # 连上了 → 重置退避
        connection_tasks: set[asyncio.Task] = set()
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
                    global SELF_AGENT_ID, SELF_CONN_ID
                    SELF_AGENT_ID = msg.get("agent_id", "") or ""
                    SELF_CONN_ID = msg.get("conn_id", "") or ""
                    print(f"🆔  Agent ID : {SELF_AGENT_ID}")

                elif t == "agent_attest_challenge":
                    await answer_attest_challenge(ws, msg)

                # ── 断开指令 ────────────────────────────────
                elif t == "agent_disconnect":
                    print("👋  收到断开指令，Agent 退出")
                    return

                # ── 执行命令 ────────────────────────────────
                elif t == "agent_exec":
                    task = asyncio.create_task(
                        handle_exec(ws, msg, pending, default_dir)
                    )
                    connection_tasks.add(task)
                    task.add_done_callback(connection_tasks.discard)

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
                    if await kill_session(target_exec):
                        print(f"🔪  Killed exec_id={target_exec}")
                    else:
                        print(f"⚠️   Kill: exec_id={target_exec} not found")

        except websockets.ConnectionClosed:
            print(f"⚠️  Connection closed, reconnecting in {backoff:.0f}s...")
            reconnect_with_backoff = True
        finally:
            # 当前协议中 backend 会将断连任务收为 cancelled；同步取消所有绑定
            # 本连接的任务并 killpg，不能留下手机看不见的电脑进程。
            for task in tuple(connection_tasks):
                task.cancel()
            if connection_tasks:
                await asyncio.gather(*connection_tasks, return_exceptions=True)
            pending.clear()
            try:
                await ws.close()
            except Exception:
                pass
        if reconnect_with_backoff:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


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
    if cmd_type in PROVIDERS and ACTIVE_SANDBOX_MODE == "none":
        await send_output(
            ws, exec_id,
            "🚫 当前系统没有可验证的命令沙箱，自然语言代理保持关闭；Shell 远控仍可使用。\n",
            done=True, exit_code=126, error="sandbox_required_for_nl",
        )
        return

    reservation_key = local_reservation_key(exec_id)
    if not reserve_exec(reservation_key):
        await send_output(
            ws, exec_id, "并发任务过多，请等待已有任务结束\n",
            done=True, exit_code=75, error="too_many_inflight",
        )
        return

    try:
        if level == "confirm":
            event  = asyncio.Event()
            result = {"approved": False}
            # 先注册 pending 再发手机帧，避免极速响应抢在 map 写入前成为 orphan。
            pending[exec_id] = (event, result)
            try:
                await ws.send(json.dumps({
                    "type":        "agent_confirm_request",
                    "exec_id":     exec_id,
                    "agent_id":    agent_id,
                    "cmd_type":    cmd_type,
                    "instruction": instruction,
                    "prompt":      instruction,
                }, ensure_ascii=False))
                local_future = asyncio.create_task(
                    local_dialog_async(
                        "知己 · 远程指令", f"将要执行:\n{instruction}"
                    )
                )
                approved = await wait_for_confirmation(local_future, event, result)
            finally:
                pending.pop(exec_id, None)

            if not approved:
                print(f"❌  Rejected: {instruction[:60]}")
                await send_output(ws, exec_id, "❌ 已拒绝执行\n", done=True, exit_code=130)
                return

        print(f"▶️   Executing: {instruction[:60]}")
        await execute(
            ws, exec_id, cmd_type, instruction, working_dir,
            reservation_held=True,
        )
    finally:
        EXEC_RESERVATIONS.discard(reservation_key)


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
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    temp_file = f"{CACHE_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, CACHE_FILE)
    try:
        os.chmod(CACHE_FILE, 0o600)
    except OSError:
        # Windows ACLs are inherited from the user's profile; chmod is best-effort there.
        pass

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
    if websockets is None or Ed25519PrivateKey is None:
        print("❌  缺少运行依赖。请重新运行 install.sh / install.ps1。")
        sys.exit(1)
    parser = argparse.ArgumentParser(description="keni Desktop Agent")
    parser.add_argument("--pair",     help="一次性配对码（在 keni APP → 远程控制 里生成，6 位字母数字）")
    parser.add_argument("--pair-only", action="store_true",
                        help="只兑换并缓存配对 token，不启动常驻 WebSocket")
    parser.add_argument("--email",    help="账号邮箱（旧登录方式，建议改用 --pair）")
    parser.add_argument("--password", help="账号密码（旧登录方式，建议改用 --pair）")
    parser.add_argument("--token",    help="直接指定 JWT token（可选，优先级最高）")
    parser.add_argument("--device",   default=None, help="设备显示名称（默认取主机名）")
    parser.add_argument("--backend",  default=os.environ.get("KENI_BACKEND_URL", "ws://localhost:8080/api/v1/agent/ws"),
                        help="WS backend URL,默认读 KENI_BACKEND_URL 环境变量,再不行 fallback localhost")
    parser.add_argument(
        "--dir", default=os.path.dirname(os.path.abspath(__file__)),
        help="默认工作目录（未指定时仅限 agent 安装目录）",
    )
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
        if args.pair_only:
            return
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
    ensure_agent_identity_key(cache)

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    try:
        asyncio.run(run(args.backend, token, device, os.path.abspath(os.path.expanduser(args.dir))))
    except (KeyboardInterrupt, SystemExit):
        print("\n👋  Agent stopped")


if __name__ == "__main__":
    main()
