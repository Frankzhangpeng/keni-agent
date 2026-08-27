# keni-agent

手机远程控制 macOS、Windows 与 Linux 桌面电脑的本地守护脚本。配合 [keni APP](https://github.com/Frankzhangpeng/super) 使用 —— 手机端发命令，电脑端流式回传输出。

## 它能干什么

- **NL → 代码操作**:服务端安全开关启用对应 provider 后，手机可下发代码任务，Mac 上运行 CLI 并把结果流回手机
- **多 LLM CLI 即插即用**:Claude / OpenClaw / Codex / Cursor 任选,APP 远控屏下拉切换
- **安全闸**:破坏性命令同时发起电脑与手机确认，任一端明确允许即可执行；Windows/Linux/macOS 都使用原生确认界面
- **多会话并发**:手机可以同时跑多条命令,逐条 kill
- **Soul Memory 注入**:NL 命令自动带上后端记忆("按我平时的习惯整理…"这种指代能理解)
- **跨平台常驻**:macOS LaunchAgent、Linux systemd user service、Windows current-user Scheduled Task
- **连接证明**:上报 OS/架构/沙箱能力，并用持久 Ed25519 密钥响应后端挑战

## 从 APP 安装

APP 的“远程控制 → 添加桌面设备”会按所选平台生成固定到不可变 commit 的完整命令。安装器使用项目内 `.venv`，不会修改系统 Python 包。

### macOS / Linux

需要 Git、Python 3。Linux 需要 systemd user service；若缺少 `venv`，Debian/Ubuntu 安装 `python3-venv`。

```bash
bash install.sh --backend wss://你的服务器/api/v1/agent/ws --pair ABCXYZ
```

### Windows 10 / 11

只需要 Python 3；APP 生成的 PowerShell 命令会下载固定版本并调用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -Backend wss://你的服务器/api/v1/agent/ws -Pair ABCXYZ
```

Windows 以当前用户、Limited 权限注册计划任务，不要求管理员权限。日志位于 `%LOCALAPPDATA%\KENI\Logs\keni-agent.log`。

## 卸载

macOS/Linux：`bash uninstall.sh`。Windows：`powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1`。加 `--purge-token`（PowerShell 为 `-PurgeToken`）才会同时删除本地配对 token 与 Ed25519 私钥。

## 手动调试

不想走系统常驻服务时可前台运行：

```bash
python3 keni_agent.py --backend ws://你的服务器:8080/api/v1/agent/ws
```

token 第一次配对后缓存在 `~/.superapp_agent.json`,后续直接跑就行。

## NL Provider

| cmd_type     | 实际命令                          | 装法                                   |
| ------------ | --------------------------------- | -------------------------------------- |
| `claude_code`| `claude --print <prompt>`         | https://github.com/anthropics/claude-code |
| `openclaw`   | `openclaw review --stdin`(stdin) | 视上游而定                              |
| `codex`      | `codex exec <prompt>`             | 视上游而定                              |
| `cursor`     | `cursor-agent --prompt <prompt>`  | 视上游而定                              |

没装的 provider 在手机端会回 `命令未找到`。装好 CLI 后还需部署侧显式开启
对应 NL provider 安全闸；默认关闭时不会把自然语言误当 Shell 执行。

加新 provider 改 `keni_agent.py` 的 `PROVIDERS` dict 即可。同步要改两个 super 仓库文件:
- `backend/handlers/agent_exec.go` 的 `RouteAgentExec` 白名单
- `flutter_app/lib/screens/remote_control/rc_cmd_type.dart` 的 provider 定义

super 仓库的 CI(`.github/workflows/check-agent-providers.yml`)会校验三处白名单一致。

## 安全模型与平台差异

| 命令类别       | 行为                                     |
| -------------- | ---------------------------------------- |
| safe(只读)   | 直接执行(`ls/cat/grep/git status...`) |
| confirm        | 桌面原生弹窗 + 手机 WS 并行确认，任一端允许即继续；双方拒绝或超时 abort |
| banned         | 客户端不可触达(`sudo/su/bash/sh/zsh/nc`) |
| NL provider    | 永远 confirm —— LLM 输出不可预测       |

确认 60 秒内无任何回应 → 默认拒绝。

macOS 使用 `sandbox-exec`，Linux 优先使用 `nsjail`、其次 `firejail`。如果 Linux 没有可用沙箱，Shell 仍可使用，但 NL provider 会在 agent 与后端两侧都 fail-closed。Windows 当前也只开放 Shell：仓库没有经过签名和设备证明的 AppContainer 启动器，因此 agent 会诚实上报 `sandbox=none`，绝不伪装成已沙箱化来点亮 NL provider。

## 协议(WS 帧)

agent ↔ backend ↔ phone 三方走同一条 WS 通道,帧类型:

- `agent_registered` / `agent_disconnect`(连接生命周期)
- `agent_exec` / `agent_output`(命令执行 + 流式输出)
- `agent_confirm_request` / `agent_confirm_response`(确认握手)
- `agent_sessions_request` / `agent_sessions` / `agent_kill_exec`(并发会话管理)

详见 super 仓库 `backend/handlers/agent.go`。

## License

MIT
