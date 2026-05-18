# keni-agent

手机远程控制 Mac 的本地守护脚本。配合 [keni APP](https://github.com/Frankzhangpeng/super) 使用 —— 手机端发命令(shell / Claude Code / OpenClaw / Codex / Cursor),Mac 端流式回输出。

## 它能干什么

- **NL → 代码操作**:手机说"帮我用 claude review 一下当前 PR",Mac 上自动跑 `claude --print` 并把结果流回手机
- **多 LLM CLI 即插即用**:Claude / OpenClaw / Codex / Cursor 任选,APP 远控屏下拉切换
- **安全闸**:破坏性命令(`rm` / `git push` / 任意 NL 提示)必须 macOS 弹窗 + 手机确认才执行;`sudo` / `bash` 直接禁止
- **多会话并发**:手机可以同时跑多条命令,逐条 kill
- **Soul Memory 注入**:NL 命令自动带上后端记忆("按我平时的习惯整理…"这种指代能理解)

## 安装

### 1. 装依赖

```bash
git clone https://github.com/Frankzhangpeng/keni-agent.git ~/keni-agent
cd ~/keni-agent
brew bundle                                    # 装 python@3.12
python3 -m pip install --user -r requirements.txt  # 装 websockets
```

### 2. 在 keni APP 生成配对码

打开 APP → 远程控制 → "添加 Mac" → 复制 6 位码(5 分钟有效)。

### 3. 配对 + 自启

```bash
bash install.sh \
  --backend ws://你的服务器:8080/api/v1/agent/ws \
  --pair ABCXYZ
```

完成后 agent 会:
- 写入 `~/Library/LaunchAgents/com.keni.agent.plist`
- 立即启动 + 开机自启 + 崩了自动重拉
- 日志:`~/Library/Logs/keni-agent.{out,err}.log`

## 卸载

```bash
bash uninstall.sh
```

## 手动调试

不想走 launchd,前台跑:

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

没装的 provider 在手机端会回 `命令未找到`,装上即用,不必改 agent 代码。

加新 provider 改 `keni_agent.py` 的 `PROVIDERS` dict 即可。同步要改两个 super 仓库文件:
- `backend/handlers/actions.go` 的 `execOpenRemoteControl` 白名单
- `backend/handlers/agent.go` 的 `RouteAgentExec` NL 白名单

super 仓库的 CI(`.github/workflows/check-agent-providers.yml`)会校验三处白名单一致。

## 安全模型

| 命令类别       | 行为                                     |
| -------------- | ---------------------------------------- |
| safe(只读)   | 直接执行(`ls/cat/grep/git status...`) |
| confirm        | 双弹窗:Mac AppleScript + 手机 WS,任一拒绝就 abort |
| banned         | 客户端不可触达(`sudo/su/bash/sh/zsh/nc`) |
| NL provider    | 永远 confirm —— LLM 输出不可预测       |

确认 60 秒内无任何回应 → 默认拒绝。

## 协议(WS 帧)

agent ↔ backend ↔ phone 三方走同一条 WS 通道,帧类型:

- `agent_registered` / `agent_disconnect`(连接生命周期)
- `agent_exec` / `agent_output`(命令执行 + 流式输出)
- `agent_confirm_request` / `agent_confirm_response`(确认握手)
- `agent_sessions_request` / `agent_sessions` / `agent_kill_exec`(并发会话管理)

详见 super 仓库 `backend/handlers/agent.go`。

## License

MIT
