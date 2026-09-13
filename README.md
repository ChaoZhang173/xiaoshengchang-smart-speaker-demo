# 小声场：Mac 智能音箱软件 Demo

这是一个直接使用 MacBook 麦克风和扬声器的本地语音助手原型：

默认真实模式的链路是：

`麦克风录音 → SiliconFlow 免费语音识别 → Agent Gateway → 工具/本地记忆/大模型 → Mac 系统朗读`

浏览器负责麦克风权限和声音播放，Python 本地服务负责安全地调用 API。项目不需要安装 Python 包、Homebrew、Node、FFmpeg 或 PortAudio，但这台 Mac 需要有 Python 3.9 或更高版本。启动器会先检查 `/usr/bin/python3`，再查找系统中其他可用的 `python3`。

如果这是朋友发来的分享包，请先阅读同目录的 `START-HERE.txt`。也可以让 Codex 阅读 `CODEX_HELP.md` 后协助排查；任何情况下都不要把 API Key 粘贴到聊天、截图或代码中。

## 使用说明

### 快速开始

1. 使用 macOS，并确认有 Python 3.9 或更高版本。
2. 完整下载或解压项目目录，不要直接在压缩包预览中运行。
3. 双击 `configure_api_key.command`，按提示输入自己的 SiliconFlow API Key。输入不会显示在屏幕上，Key 只保存到当前 Mac 的钥匙串。
4. 双击 `start.command`；保持 Terminal 窗口开启，等待浏览器自动打开。
5. 页面显示“真实 API · SiliconFlow”后，先输入文字测试，再允许浏览器使用麦克风。
6. 使用结束后，在启动服务的 Terminal 窗口按 `Control-C` 停止。

可以直接尝试：

```text
现在几点？
上海现在天气怎么样？
联网搜索 OpenAI 的最新消息
播放音乐
把音乐音量调到 35
记住我喜欢爵士乐
你还记得我什么吗？
忘记爵士乐
```

### 第一次启用 SiliconFlow

API Key 不要发到聊天里，也不要写进代码。

1. 确认 SiliconFlow 账户已经完成平台要求的实名认证；官方说明免费模型在认证后可用。
2. 双击 `configure_api_key.command`（默认就是 SiliconFlow 配置）。
3. 按窗口提示粘贴 API Key，然后按回车。输入时屏幕不会显示字符。
4. Key 会保存到 macOS 钥匙串，不会写入项目文件或网页。
5. 双击 `start.command`。浏览器会自动打开 Demo。
6. 页面右上角显示“真实 API · SiliconFlow”后，就可以先输入文字测试，再点击中央按钮测试麦克风。

下载或接收压缩包后，应先完整解压到“下载”或“文稿”目录。第一次打开未签名的 `.command` 脚本时，macOS 可能要求确认来源；只在确认分享者可信且压缩包未被修改的情况下，通过右键“打开”或“系统设置 → 隐私与安全性 → 仍要打开”批准。不要关闭 Gatekeeper，也不要在压缩包内部直接运行脚本。

如果旧的演示模式仍在运行，启动器会为 SiliconFlow 模式自动选择另一个可用端口并打开，不会再报“Address already in use”。启动窗口按 `Control-C` 可停止对应服务。

## 开发日志

完整版本记录见 [`CHANGELOG.md`](CHANGELOG.md)。

### 2026-09-14 · v0.4

- 增加统一的 Agent Gateway，把本地路由、记忆选择、大模型调用、工具执行和语音合成从界面流程中分离。
- 增加可替换的 Brain Adapter；当前接入 SiliconFlow 和可选 OpenAI，本地演示由 Gateway 的本地快速响应完成，尚未连接 Codex、手机或其他电脑。
- 所有由主脑或本地指令路由发起的白名单工具执行，现在都会先经过本地 Policy Engine。未知、停用、未获网络权限或未来需要确认的工具默认不会执行。
- 增加有界、脱敏、仅驻留当前进程内的 Execution Ledger，为以后排查日程、邮件和购物任务提供执行记录基础。
- 保留现有网页协议及受支持正常指令的文字、录音、显式长期记忆、天气、搜索和音乐行为，并补充跨模块回归测试。

### 2026-09-06 · v0.3

- 新增真正的 `web_search` 白名单工具：默认真实模式可通过实验性的 Bing RSS 搜索公开网页标题与摘要；仅允许固定 Bing 搜索主机间最多两次受限重定向，不会打开结果链接或接收模型提供的网址。
- 修复“查询上海天气”“帮我查一下上海天气”等口语把整段前缀误当成城市的问题，并支持 `Paris, France` 与英文 `weather in ...` 句式。
- 修复 Open-Meteo 对“杭州市 / 北京市 / 上海市”等名称解析不稳的问题：先规范化常见“市”后缀、读取多个候选，再按名称、地点类型和人口排序，避免静默选到海外同名小地点。
- 明天、历史天气和多城市查询现在会明确说明当前能力边界，不再把时间词或多个城市拼成错误地名。
- 天气或搜索断网、无结果时会返回可朗读的安全提示，不再变成笼统的本地 500；构建标识已升级，重复双击不会误复用旧服务。

### 2026-09-01 · v0.2

#### 目前实现的功能

- Mac 浏览器录音与文字输入，SiliconFlow 语音转写和大模型回答，Mac 系统语音朗读。
- ToolRouter 固定白名单工具路由与 SiliconFlow Function Calling。
- 本地时间与 Open-Meteo 当前天气查询。
- SomaFM Groove Salad 白名单网络电台，支持播放、暂停、继续、停止和音量控制。
- AudioFocusManager：录音、思考和朗读时暂停音乐，结束后按条件自动恢复。
- SQLite 显式长期记忆：记住、查询、逐条删除、确认后全部清空；敏感信息拒绝保存或仅留本机。
- 本地演示模式、随机本地访问令牌、Host/Origin/Content-Type 校验和 macOS 钥匙串保存 API Key。
- SiliconFlow 与可选 OpenAI provider 分离，云端失败时保留文字结果或安全降级。

#### 已排除/修复的 Bug

- 修复端口 `8765` 被占用时启动失败：识别兼容实例，否则自动选择可用端口。
- 修复 SiliconFlow Key 与旧 OpenAI Key 混淆、导致进入错误 provider 的问题。
- 修复第二轮录音遇到 `socket.timeout` 后界面长期停留在“正在转录”的问题：超时被正确分类，失败后恢复可操作状态。
- 修复部分免费 ASR 模型连续调用出现 `503` 或超时的问题：默认改用 `TeleAI/TeleSpeechASR`，并为可重试的语音识别故障增加一次有上限的重试。
- 修复音乐与语音回答共用播放器、互相中断的问题：拆分独立播放器并统一管理音频焦点。
- 修复音量 `1` 被误解释为 `100%` 的问题；工具音量现在始终按 `0–100` 百分比处理。
- 修复否定命令可能被本地兜底路由反向执行的问题，例如“不要播放音乐”不再触发播放。
- 修复模糊“忘记”可能删除语义相反记忆的问题；不明确的删除要求改为页面确认。
- 修复不同人物的地点记忆可能互相覆盖，以及敏感记忆可能进入云端上下文的问题。
- 修复旧网页或旧服务被误认为新版本的问题：加入构建标识、健康检查和 provider 校验。
- 补齐 favicon，消除启动日志中的无意义 `404`。

#### 存在的问题

#### 待开发功能

## 当前免费链路

- 语音识别：`TeleAI/TeleSpeechASR`
- 文字回答：`Qwen/Qwen3.5-4B`
- 回答朗读：这台 Mac / 浏览器提供的系统语音，不调用 SiliconFlow 云端 TTS

## 新增能力

### Agent 基础架构

从 v0.4 起，用户的一轮普通指令会先进入 `AgentGateway`。它负责挑选有限且允许进入云端的本地长期记忆、通过 `BrainAdapter` 调用当前主脑，并把主脑提出的工具请求交给受控工具路由。`PolicyEngine` 是独立于大模型的最终权限判断者；模型可以建议调用，但不能自行授权。`ExecutionLedger` 只保存数量和大小都有上限的脱敏快照，重启服务后清空，也不会通过网页接口公开。

主脑返回的客户端动作本身没有执行权；Gateway 只会转发本轮由 `GovernedToolRouter` 按当前明确指令验证并签发的动作。音乐、天气和搜索采用完整命令语态判断：否定、引用、解释、假设或转述默认不执行；天气自动查询只接受本地精确地点表，隐式“最新消息”请求只接受封闭问句结构并发送可独立识别的问题子句，无法可靠分离的敏感上下文不会外发。

这次改造主要是为后续扩展打基础，当前不会自动获得日程、邮件、购物、Codex 或 iPhone 控制能力。高风险工具的“需要确认”状态已经具备内部表示，但还没有面向用户的确认令牌、设备验证和执行回执，因此暂时没有注册任何会产生购买、发送或删除效果的工具。详细边界和后续顺序见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

### 本地长期记忆

长期记忆使用 Python 自带的 SQLite，只保存用户明确要求记住的短文字，不保存原始录音。可以直接说或输入：

```text
记住我喜欢爵士乐
你还记得我什么吗？
忘记爵士乐
```

页面下方可以查看、逐条删除或二次确认后全部清空。清空“本次对话”只清短期会话，不会删除长期记忆。为了避免误删和语音泄露，私密记忆不能通过模糊语音指令删除或回显，需在本地页面中操作。

macOS 默认数据库位置：

```text
~/Library/Application Support/Xiaoshengchang/memory.sqlite3
```

程序新建的数据目录会尽力设置为 `0700`，数据库设置为 `0600`，并启用 SQLite `secure_delete`。也可以在启动前设置 `SMART_SPEAKER_DATA_DIR` 改变数据目录。不要把数据库作为普通附件分享，它可能包含私人偏好。

程序会把常见住址、联系方式、健康和财务信息标记成“仅本机私密”：它们可以在页面查看和删除，但不会被选入云端模型上下文。API Key、密码、令牌、助记词和恢复码等凭据会被拒绝保存。该分类是保守的本地规则，不应代替你自己判断；不想保留的信息请及时删除。

### 白名单网络电台

页面内置一个固定白名单电台：SomaFM Groove Salad，使用电台官方公布的 128 kbps MP3 直播流。可以点击页面按钮，也可以说：

```text
播放音乐
暂停音乐
继续播放
停止音乐
把音乐音量调到 35
```

白名单来源：[SomaFM Groove Salad 官方直连页](https://somafm.com/groovesalad/directstreamlinks.html)。播放器只接受本地后端给出的 `station_id`，大模型不能传入任意网址。播放时会直接连接 SomaFM，因此电台服务能看到正常网络连接所需的 IP 和请求信息。

音乐与回答使用独立播放器。录音或文字提问开始时，正在播放的音乐会暂停；回答朗读结束后，在用户没有要求停止或换歌的情况下自动恢复。

### 本地时间、天气、联网搜索和 Function Calling

SiliconFlow Chat Completions 会收到严格限制的工具说明。当前允许的工具只有：本地时间、Open-Meteo 当前天气、公开网页摘要搜索和白名单音乐控制。工具名称、字段和范围都会由本地 Python 再次验证；不会使用 `eval`，不会执行模型提供的命令、文件路径、SQL 或任意 URL。

天气使用免 Key 的 [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api) 和 [Forecast API](https://open-meteo.com/en/docs)。查询天气时，城市名称会发送给 Open-Meteo；结果缓存 10 分钟。可以说：

```text
现在几点？
上海现在天气怎么样？
请查询杭州市现在的天气
```

联网搜索使用免 Key 的 Bing RSS 结果，只读取最多 3 条标题、摘要和来源域名，不会继续打开搜索结果页面。可以说：

```text
联网搜索 OpenAI 的最新消息
帮我查一下今天的科技新闻
```

这条 RSS 路径适合个人软件 Demo，但[微软最初将其描述为实验性能力](https://blogs.bing.com/search/January-2005/RSS-Feeds-for-Search-Results)，未来可能调整或停止。程序会在服务异常时安全提示；若用于公开或商业产品，应替换为有正式服务契约的搜索 API。搜索词和正常连接所需的 IP 信息会发送给 Bing；在 SiliconFlow 模式下，返回的标题和摘要还会作为不可信工具数据交给模型整理。搜索结果中的文字绝不会被当成系统指令。

截至 2026-09-01，本机真实 API 自检已确认 `Qwen/Qwen3.5-4B` 能调用 `get_local_time` 并完成第二轮回答。模型能力可能调整；若工具参数暂不受模型支持，程序会对时间、天气、明确搜索命令和音乐命令使用同样受限的本地解析兜底，不会开放额外权限。

截至 2026-08-31，SiliconFlow 官方资料把 `TeleAI/TeleSpeechASR` 列为免费 ASR，并明确列入标准语音转文字接口支持范围；它在本机连续两轮测试中都约 3 秒完成。此前测试的 `Qwen/Qwen3-ASR-1.7B` 虽在价格页标为免费，但连续调用时第二轮多次超时，因此不再作为默认模型；`SenseVoiceSmall` 在本机测试中持续返回上游 503。免费模型仍有账户级调用频率限制；达到限额时会返回 429，需要稍后重试。模型与免费状态可能调整，请以 [SiliconFlow 免费 ASR 说明](https://docs.siliconflow.cn/cn/usercases/use-siliconcloud-in-BiBiKeyboard)、[实时价格页](https://siliconflow.cn/pricing) 和 [免费模型限额说明](https://docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation) 为准。

接口依据：

- [语音转文字 API](https://docs.siliconflow.cn/cn/api-reference/audio/create-audio-transcriptions)
- [Chat Completions API](https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions)

## 隐私与数据流向

SiliconFlow 模式会把本轮录音发送给 SiliconFlow 做语音识别，再把转录文字、系统提示、最近最多 2 轮对话和与当前问题最相关的最多 5 条非敏感长期记忆发送给文字模型。天气查询会把城市名发送给 Open-Meteo；联网搜索会把关键词发送给 Bing，并把最多 3 条标题、摘要和来源域名交给 SiliconFlow 整理。录音只在内存中处理，本地服务不会把它写入磁盘；停止服务后，本地内存中的短期对话历史会清空，显式长期记忆继续保存在本机 SQLite 中。

“长期记忆保存在本地”不表示它永远不会进入云端：为了回答当前问题，目前只有选中的少量相关、通过当前敏感规则复核的偏好类记忆，会作为明确标注的事实数据发送给当前大模型。其他类别、旧版本误标的敏感记录，以及程序识别出的 API Key、密码、PIN、令牌、私钥、助记词、恢复短语/恢复码和二进制录音不会进入该上下文。记忆和搜索外发共用 Unicode 规范化与凭据检测，因此全角字符、控制/默认忽略字符以及被空格或连字符拆开的凭据标签不会绕过检查；可选 OpenAI 模式也遵循同一规则。

API Key 只从 macOS 钥匙串读入本机 Python 进程：不会进入网页、URL、对话历史或接口响应。服务只监听 `127.0.0.1`，并校验每次启动生成的随机令牌、Host、Origin 和内容类型。

## 不配置 Key 也能体验

直接双击 `start.command`。没有可用 Key 时会进入本地演示：可以验证麦克风权限、录音、朗读、本地时间、长期记忆和音乐界面，但不会把录音或文字发送给 SiliconFlow，也不会生成真实大模型答案。演示模式下天气与搜索都不会联网，页面会提示先配置真实联网模式。

## 麦克风权限排查

如果误点了“不允许”：

1. 打开“系统设置 → 隐私与安全性 → 麦克风”。
2. 为正在使用的 Chrome 或 Safari 开启权限。
3. 回到页面刷新后再试。

浏览器会请求回声消除、降噪和自动增益。当前是半双工：播放回答时不会同时录音，以免扬声器的声音再次被麦克风收进去。

## 可选配置

默认会优先使用已保存的 SiliconFlow Key。可在启动前设置：

```text
SILICONFLOW_TRANSCRIBE_MODEL=TeleAI/TeleSpeechASR
SILICONFLOW_TEXT_MODEL=Qwen/Qwen3.5-4B
SILICONFLOW_REQUEST_TIMEOUT=60
SMART_SPEAKER_MODE=auto|mock|siliconflow|openai
SMART_SPEAKER_DATA_DIR=/自定义/本地数据目录
```

模型名称会在本地校验，API Base URL 固定为官方 `https://api.siliconflow.cn/v1`，不能通过环境变量重定向，以免钥匙串中的 Key 被发送到其他地址。

原来的 OpenAI 模式仍保留。需要时可双击 `optional_configure_openai_key.command` 保存 OpenAI Key，并以 `SMART_SPEAKER_MODE=openai` 明确启动。默认自动模式只选择 SiliconFlow；没有 SiliconFlow Key 时进入本地演示，不会悄悄回退到 OpenAI。

## 手动启动与自检

在项目目录运行：

```text
./start.command --no-browser
```

请使用启动器加载钥匙串中的 Key；直接运行 `python3 app.py` 不会读取钥匙串，通常会进入本地演示模式。启动后先打开 Terminal 显示的 `/api/health` 地址，确认 `provider_id` 为 `siliconflow`，再验证天气和联网搜索。

运行自动测试：

```text
python3 -m unittest discover -s tests -v
```
