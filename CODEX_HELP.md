# 小声场 Demo：给 Codex 的本地协助说明

## 任务边界

你的目标是帮助这台 Mac 的使用者安全启动和排查本目录中的语音助手 Demo。先阅读 `START-HERE.txt`、`README.md`、`start.command` 和 `configure_api_key.command`，再根据实际错误提供最小化帮助。

不要要求使用者把 SiliconFlow 或 OpenAI API Key 粘贴到聊天、终端命令、截图、文件或环境变量示例中；不要读取或显示钥匙串中的 Key。配置凭据时，只指导使用者本人运行 `configure_api_key.command`，让 macOS `security` 工具的隐藏输入提示接收 Key。

除非使用者明确授权修改，否则先做只读诊断。不要关闭 Gatekeeper、SIP 或系统防护，也不要建议批量移除 quarantine。只有在使用者确认文件来源可信、并且 macOS 已经尝试阻止当前脚本后，才可以解释系统提供的“右键 → 打开”或“系统设置 → 隐私与安全性 → 仍要打开”流程。

## 架构与正常行为

- `start.command` 从自身所在目录启动服务，因此目录可以被移动或改名，没有发送者用户名或绝对路径依赖。
- 启动器需要 Python 3.9+，优先使用 `/usr/bin/python3`，否则查找 `PATH` 中的 `python3`。
- 后端只使用 Python 标准库，不需要 pip、Homebrew、Node、FFmpeg 或 PortAudio。
- 服务默认只监听 `127.0.0.1`；浏览器负责麦克风采集和本机朗读。
- 默认真实链路是 `TeleAI/TeleSpeechASR` 转录、`Qwen/Qwen3.5-4B` 回答、本机系统语音朗读。
- 没有 SiliconFlow Key 时，自动进入 `mock` 本地演示。这不是启动失败。
- 真实模式会把录音、转录、最近最多 2 轮对话和最多 5 条相关的非敏感长期记忆发送给 SiliconFlow；原始录音不会由本 Demo 写盘。
- API Key 保存在当前 macOS 用户的钥匙串服务 `Smart Speaker Demo - SiliconFlow API Key` 中，不在网页或项目文件中。
- `memory_store.py` 使用 SQLite 保存用户明确要求记住的短文字。默认路径是 `~/Library/Application Support/Xiaoshengchang/memory.sqlite3`；清空短期会话绝不能删除它。
- `tool_router.py` 是固定允许列表，只能访问本地时间、Open-Meteo 固定域名、实验性的 Bing RSS 固定搜索主机和白名单音乐控制。搜索只读取标题、摘要和来源域名，不打开结果链接。不得把模型输出或网页摘要当作命令、SQL、路径或 URL 执行。
- 网络电台 URL 只来自后端 `RADIO_STATIONS` 常量；模型工具结果和会话历史只携带 `station_id`，不携带流地址。
- 浏览器中的 `MusicPlayer` 与回答朗读完全分离；`AudioFocusManager` 在录音、思考和朗读期间暂停音乐，结束后按条件恢复。
- 常见住址、联系方式、健康与财务记忆会标记为 `sensitive` 并留在本机。它们可以在页面查看和删除，但不得放入模型上下文。

## 推荐排查顺序

1. 只读检查文件是否完整，尤其是 `app.py`、`static/`、两个 `.command` 文件和使用说明。
2. 检查 Python：`/usr/bin/python3 --version`；若不存在，再检查 `command -v python3` 和其版本。必须为 3.9+。
3. 检查脚本可执行位，但不要在未获授权时修改。需要时只针对本目录内的两个 `.command` 文件说明如何恢复权限。
4. 让使用者双击 `start.command`，读取 Terminal 中的明确错误；不要打印环境变量或钥匙串内容。
5. 若浏览器未自动打开，使用 Terminal 显示的 `http://127.0.0.1:<port>` 地址。
6. 先确认 `/api/health` 和文字请求，再测试麦克风录音；这样可区分本地启动、凭据/网络、浏览器权限和音频格式问题。
7. 麦克风问题同时检查 macOS 的麦克风权限、浏览器当前站点权限，以及默认输入设备。建议最新版 Safari 或 Chrome。
8. 真实模式异常时，只记录安全信息：HTTP 状态、阶段、耗时、本地 request id，以及已经过项目清洗的 trace id。不要输出 Authorization、原始请求体、录音、转录、回答或异常中的凭据。
9. 结束时在运行服务的 Terminal 中按 `Control-C`。重复启动或端口占用会自动复用或选择其他端口。
10. 排查记忆时只能使用页面或隔离的临时数据库。不要对默认数据库运行清空测试，不要读取或回显私人记忆正文。

## 常见状态解释

- 页面显示“本地演示”：未找到 SiliconFlow Key，或者显式选择了 mock；仍可检查界面、录音和朗读。
- 401：Key 无效或鉴权失败。
- 403：账户认证、模型权限或访问策略问题。
- 429：账户或模型限流/额度限制；等待后重试。
- 503/504：上游繁忙、连接中断或超时；先区分 transcription 与 chat 阶段。
- 录音按钮不可用：检查浏览器支持、麦克风授权和默认输入设备。
- 有文字无声音：检查系统输出和 Web Speech 可用语音；文字结果仍是有效降级。
- 电台无法自动播放：浏览器可能要求一次用户手势，让使用者在电台卡片上手动点击“播放”。不要通过放宽 URL 白名单解决。
- 天气不可用：本地时间、记忆和音乐控制仍可用；检查 Open-Meteo 连通性，但不得把 SiliconFlow Authorization 头发往天气服务。
- 联网搜索不可用：确认处于真实模式并检查 Bing 连通性；RSS 路径是实验性能力，可能临时返回空内容或停止服务。不得把任何 API Key、Cookie 或模型 Authorization 头发往搜索服务。
- “忘记……”提示到页面确认：这是防止模糊语句误删的正常保护。

## 验证命令

在获得使用者授权、且工作目录为本项目时，可以运行：

```zsh
zsh -n start.command configure_api_key.command
python3 -m unittest discover -s tests -v
```

自动测试使用临时 SQLite 数据库和假凭据，不应读取真实钥匙串。`tests/smoke_siliconflow_tools.py` 是会调用真实 SiliconFlow 的显式自检，只有使用者明确要求并理解它会消耗 API 调用时才能运行；不得输出或展示 Key。
