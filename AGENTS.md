# AGENTS.md

> open-xiaoai-muse：基于 open-xiaoai 的小爱音箱 AI 中枢（你好缪斯 → Hermes，也支持小智 AI、OpenClaw 等）。
> 接管音箱音频输入输出，实现与第三方 AI 的对话。由 coderzc/open-xiaoai-bridge 分叉，并移植了 OwnDing/open-xiaoai 的回声消除等能力。
> 仓库改名前叫 open-xiaoai-bridge：音箱 `/tmp/open-xiaoai-bridge-*` 文件、各后端 `session_key`、平板 `/opt/open-xiaoai-bridge` 等运行时标识沿用旧名，不要顺手改（改 pid 文件名会让音箱上的旧进程没人收拾）。

## 系统架构

详见 [README.md 系统架构](README.md#系统架构)（含 Mermaid 流程图和各模块工作流程）。

## 项目结构

```
open-xiaoai-muse/
├── main.py                        # 入口：解析环境变量，启动 MainApp
├── config.py                      # 用户配置（唤醒词、路由钩子、TTS、OpenClaw 等）
├── core/
│   ├── app.py                     # MainApp 主控制器（单例，管理生命周期）
│   ├── xiaoai.py                  # XiaoAI 设备接入 / 事件桥接
│   ├── xiaoai_conversation.py     # 小爱连续对话策略
│   ├── xiaozhi.py                 # 小智 AI WebSocket 协议客户端
│   ├── backends.py                # 外部对话后端：ChatBackend 统一接口 + 注册表（路由名 → 后端类）
│   ├── hermes.py                  # Hermes Agent 后端（OpenAI 兼容 + 服务端会话、长期记忆头、工具事件）
│   ├── openai.py                  # 通用 OpenAI 兼容后端（Ollama / LM Studio 等，HermesManager 的父类）
│   ├── openclaw.py                # OpenClaw 网关客户端（连接、消息、TTS 播放）
│   ├── qwenpaw.py                 # QwenPaw HTTP 后端
│   ├── external_conversation.py   # 外部后端连续对话（VAD → ASR → 后端 → TTS，播报中打断），所有后端共用
│   ├── wakeup_session.py          # 唤醒分发：before_wakeup 路由到小智或外部后端连续对话
│   ├── muse.py                    # 小爱管家（你好缪斯）唤醒词：集合、文本解析、KWS 子集、拦截原生小爱
│   ├── ref.py                     # 全局引用注册表（get/set 依赖注入）
│   ├── models/                    # 模型文件（KWS/VAD/ASR，.gitignore 排除）
│   ├── assets/sounds/             # 音效（tts_notify.mp3 等）
│   ├── services/
│   │   ├── speaker.py             # SpeakerManager 音箱硬件控制
│   │   ├── api_server.py          # HTTP REST API（aiohttp）
│   │   ├── conversation_light.py  # 连续对话灯效（状态驱动、后台切换）
│   │   ├── light_level.py         # 聆听灯随麦克风音量起伏（推噪声给音箱 vis）
│   │   ├── music_duck.py          # 对话期间压低音乐：Spotify（SSH 长连接 → Oracle 上 deploy/music-duck/music_duck.py）+ 音箱 mediaplayer 暂停
│   │   ├── tts/router.py           # 统一 TTS provider 路由与播放
│   │   ├── tts/wake_reply.py       # 唤醒 / 打断后的应答语（小爱原生 TTS 生成后缓存在音箱 /tmp）
│   │   ├── tts/segmenter.py        # 流式回复切段 + 朗读前清理（Markdown / <think> / 开头英文）
│   │   ├── tts/segment_player.py   # 分段播放（小爱原生预生成流水线 / 按段回调）
│   │   ├── audio/
│   │   │   ├── stream.py          # GlobalStream 全局音频流（多路输入广播，保留最近 3 秒）
│   │   │   ├── aec.py             # 子带回声消除（移植自 OwnDing/open-xiaoai）
│   │   │   ├── highpass.py        # 流式高通滤波（audio_input.highpass_hz，单声道和多麦都用）
│   │   │   ├── multimic.py        # 多麦前端：流式 STFT、在线 AuxIVA 盲源分离、3 麦求和
│   │   │   ├── multimic_input.py  # 多麦唤醒输入：音箱 arecord 4 声道 | nc → AEC → IVA → 额外几路 KWS 输入
│   │   │   ├── codec.py           # 音频编解码
│   │   │   ├── vad/silero.py      # Silero VAD 语音活动检测（ONNX）
│   │   │   ├── kws/sherpa.py      # Sherpa KWS 关键词唤醒
│   │   │   └── asr/sherpa.py      # Sherpa ASR 离线语音识别（SenseVoice）
│   │   ├── tts/doubao.py          # 豆包 TTS 客户端（火山引擎）
│   │   ├── tts/openai.py           # OpenAI-compatible TTS 协议客户端
│   │   ├── tts/mlx_audio.py        # MLX-Audio TTS provider（复用 OpenAI 协议客户端）
│   │   └── protocols/
│   │       ├── websocket_protocol.py  # 小智 WebSocket 协议实现
│   │       └── typing.py              # 协议类型定义
│   └── utils/
│       ├── logger.py              # 彩色日志（XiaozhiLogger 单例）
│       ├── config.py              # ConfigManager（嵌套路径查询、热重载）
│       ├── config_loader.py       # config.py 动态导入
│       ├── base.py                # 基础工具
│       └── file.py                # 文件工具
├── native/                        # Rust PyO3 扩展（maturin 编译）
│   └── src/
│       ├── lib.rs                 # 模块入口：on_output_data, start_server, stop/start_recording, stop/start_playing
│       ├── server.rs              # WebSocket 音频服务器（TCP :4399）
│       ├── python.rs              # Python 回调注册中心（HashMap）
│       ├── macros.rs              # 辅助宏
│       └── tts/                   # TTS 音频处理（流式、PCM 直通、MP3 解码）
├── app/openclaw/                  # OpenClaw 设备身份存储（Ed25519 密钥）
├── skills/xiaoai-tts/             # Agent 工具：通过 HTTP API 控制小爱播放
└── tests/                         # 测试脚本
```

## 核心组件

### MainApp (core/app.py)

应用主控制器，单例模式，管理全部服务生命周期。

- `instance(enable_xiaozhi, backends)` → 单例获取；`backends` 是启用的后端路由名（main.py 按 `HERMES_ENABLE` 等环境变量生成）
- `run(enable_api_server)` → 启动各服务
- `set_device_state(state)` → 管理设备状态（IDLE / LISTENING / SPEAKING / CONNECTING）
- `send_text(text)` → 发送文本到小智
- `send_to_backend(name, text, wait_response, play_reply)` → 单次发送到外部后端（返回 run_id 或回复文本）；`send_to_hermes` / `send_to_openclaw` / `*_and_play_reply` 等是快捷方法
- `set_backend_session_key(name, key)` → 运行时切换后端 session_key（每次唤醒前恢复默认）
- `schedule(callback)` → 主线程任务队列
- `shutdown()` → 优雅关闭

**边界约束**:
- `MainApp` 是业务主循环和设备状态的单一入口
- `device_state` 以 `MainApp` 为准，其他模块通过代理回写，不各自维护平行状态
- `MainApp.loop` 是业务协程的主调度循环

### XiaoAI (core/xiaoai.py)

小爱音箱交互接口，类级变量（classmethod 风格）。

- `init_xiaoai()` → 初始化原生服务，注册事件处理
- `on_event(event)` → 处理小爱事件（RecognizeResult / AudioPlayer）
- `on_input_data(data)` / `on_output_data(data)` → 麦克风 / 扬声器音频回调
- `on_input_packet((data, metadata))` → 新客户端录音帧；`layout == "mic_ref"`（[麦克风, 播放回采] 双声道）时先过回声消除，再按单声道送进 `GlobalStream`
- `echo_reference_live()` / `echo_active()` / `near_end_ratio_db()` → 回采是否在线、当前输入是否可能带播放残留、打断时近端人声强度
- `set_recording_wanted(wanted)` → 会话层主动关麦时置 False；录音看门狗只在"应该有录音"且断流 4 秒后重新请求录音
- `run_shell(script, timeout)` → 远端 shell 执行
- 内部维护独立 `async_loop`（后台线程），仅用于原生扩展回调和事件桥接

**边界约束**:
- 负责设备接入和事件桥接，不承载连续对话策略
- 连续对话状态放在 `xiaoai_conversation.py`
- `async_loop` 不应承载新的业务状态机

### XiaoZhi (core/xiaozhi.py)

小智 AI WebSocket 协议客户端，单例模式。

- `connect()` / `disconnect()` → 连接管理
- `send_audio(frames)` / `send_text(text)` → 发送音频 / 文本
- `send_start_listening(mode)` / `send_abort_speaking(reason)` → 协议命令
- 回调委托：`on_incoming_audio`, `on_incoming_json`, `on_network_error` 等

**边界约束**:
- 只负责协议收发，不负责唤醒策略和连续对话策略
- `session_id` 必须由服务端消息更新，不能长期使用空值发送控制消息

### 外部对话后端 (core/backends.py)

Hermes / OpenAI / OpenClaw / QwenPaw 都继承 `ChatBackend`（classmethod 单例风格），在 `_REGISTRY` 里按路由名注册；`before_wakeup` 返回的字符串、`after_wakeup` 的 `source`、配置段名和后端一一对应。

- 每个后端声明 `NAME`（路由名）、`LABEL`（日志名）、`CONFIG_SECTION`、`ENABLE_ENV`、`DEFAULT_SESSION_KEY`
- 必须实现：`initialize_from_config` / `connect` / `close` / `is_enabled` / `is_connected` / `set_session_key` / `get_tts_speaker_for_session_key` / `send` / `send_and_play_reply` / `_send_and_track` / `_wait_response` / `_play_response_with_tts`
- 可选：`supports_streaming()` + `stream_reply(text)`（流式）、`begin_conversation()` / `end_conversation()`（服务端会话）
- 对外只用公开方法：`with_rule_prompt` / `submit` + `wait_reply` / `stream_reply` / `play_text` / `tts_provider` / `get_session_key` / `reset_session_key`

**边界约束**:
- 连续对话、唤醒分发、MainApp 只通过 `ChatBackend` 的公开方法使用后端，不访问 `_rule_prompt` 等私有成员
- 新增后端：写一个继承 `ChatBackend` 的类，在 `_REGISTRY` 加一行；不要在 wakeup_session / app 里加 if/elif
- `HermesManager` 继承 `OpenAIManager`：类属性会继承，子类的运行状态（`_initialized`、等待回复的 dict）由 `OpenAIManager.__init_subclass__` 重设，新增类级可变状态时要一起加进去
- Hermes 的 `session_key`（默认 `agent:default:open-xiaoai-bridge`）是长期记忆作用域，改了就读不到以前的记忆

### OpenClawManager (core/openclaw.py)

OpenClaw 网关客户端，管理 WebSocket 连接、消息分发、自动重连、TTS 播放。

- `initialize_from_config(enabled)` → 从 config 初始化
- `connect()` → 建立连接（Ed25519 设备身份认证）
- `send(text, wait_response)` → 发送消息，返回 run_id 或回复文本，失败返回 None
- `send_and_play(text, wait_response)` → 发送并 TTS 播放回复
- `is_connected()` / `is_enabled()` → 状态查询

**内部机制**:
- Ed25519 设备身份认证（密钥存储在 `app/openclaw/identity/`）
- WebSocket ping/pong + tick 事件监控连接健康
- 指数退避重连（初始 1s，最大 60s）
- 请求 ID 映射 `_pending: dict[str, asyncio.Future]` 追踪响应
- TTS 播放：通过共享 TTS Router 选择 `xiaoai`、`doubao`、`openai` 或 `mlx_audio` provider；缺省时保持 `tts_speaker` 的旧选择逻辑
- Rust TTS 播放使用单一活动 `playback_token`：开始新的 Rust TTS 会使旧 token 失效；`stop_tts_playback(token)` 只应由持有该 token 的调用方定向停止自己的播放

**连接参数限制**:
- `client.id`: 必须是 OpenClaw 预定义常量
- `client.mode`: 必须是预定义常量
- `session_key`: 只从 config.py 读取

### ExternalConversationController (core/external_conversation.py)

外部后端连续对话控制器，所有后端共用：`ExternalConversationController(backend)`，配置段、日志名、`after_wakeup` 的 source 都取自 backend。唤醒词触发后进入独立的 VAD → ASR → 后端 → TTS 循环。

- `start()` → 进入对话模式（调 `backend.begin_conversation()`）
- `stop()` → 退出对话（调 `backend.end_conversation()`）
- `is_active()` → 状态查询

**对话循环** (`_run_one_turn`):
1. VAD 检测语音开始（`_wait_for_speech`）
2. 录制完整语音（VAD 帧 hook）
3. SherpaASR 离线识别
4. 退出关键词检测
5. 发送到后端（`_ask_backend`：流式时返回 `ReplyStream`，否则 `submit` → 播"咻" → `wait_reply`）
6. TTS 播放回复（等待完成，或被唤醒词打断）
7. 恢复监听

**回声防护与播报中打断**（`barge_in` 配置，实现在 `core/external_conversation.py`）:
- 回采在线（`barge_in.enabled` 且 `XiaoAI.echo_reference_live()`）时：播报期间**不关麦**，回声由 `core/services/audio/aec.py` 消除；KWS 通过 `arm_barge_in(handler)` 在暂停状态下继续检测，命中后先切灯，再用自己持有的 `playback_token` 停 TTS；启用打断应答语时先等待 `stop_device_audio()` 完成，再随机说一句应答语（"我在"/"在"/"哎"…，`conversation.wake_reply.on_barge_in`）后进入下一轮；关闭应答语时后台停播并继续监听
- 打断命中位置（`GlobalStream` 绝对采样位置）往前 `speech_lookback_ms` 作为下一轮 `VAD.resume("speech", preroll_from=...)` 的预录，"你好缪斯，明天天气怎么样"后半句不丢；ASR 结果去掉句首唤醒词
- 回声残留期间（播放中及停播后 `echo_vad_hangover_ms`）VAD 用更高的 `echo_vad_threshold`
- 回采不在线时退回旧方式：`stop_recording` → kill 远端 arecord → 麦克风物理静音，TTS 和提示音都在关麦期间播放
- `VAD.resume()` 会自动 `_reset_state()` + `input_bytes.clear()`，清除旧的 `speech_frames` 和音频流缓冲（带 `preroll_from` 时再从 `GlobalStream` 历史补回）

**流式回复和分段播报**（后端 `supports_streaming()` 为真时，目前是 Hermes / OpenAI）:
- `OpenAIManager.stream_reply(text)` 产出 `("text", delta)`；`HermesManager` 通过 `_sse_events` 把 `event: hermes.tool.progress` 转成 `("tool", dict)`；只取 `delta.content`，不取 `reasoning_content`
- 会话由 Hermes 管理：进入连续对话时 `HermesManager.begin_conversation()` 生成新会话 ID，本次唤醒每轮请求带 `X-Hermes-Session-Id`、只发当前一句，Hermes 从 state.db 读回本次唤醒的历史（含工具调用结果和读过的 skill）；退出（再见 / 超时 / 被打断）时 `end_conversation()` 丢掉。bridge 本地不存历史；单次请求（API / 拦截小爱指令）每次一个新 ID。会话 ID 需要 `api_key`（Hermes 的 API_SERVER_KEY）
  - 不要再把历史放进 `messages`：Hermes 对重复 `skill_view` 只回“内容未变”，客户端自己带纯文字历史时模型看不到 skill 正文，也看不到之前的工具调用
- `ReplyStream` 在后台读流（创建即发请求，和"咻"并行）；`_speak_stream` 用 `core/services/tts/segmenter.py` 切段，送进 `core/services/tts/segment_player.py`
- `tts_speaker == "xiaoai"`：`XiaoAINativeSegmentPlayer`（`mibrain text_to_speech save=1` 预生成 + `miplayer -f` 依次播放，临时文件 `/tmp/open-xiaoai-bridge-tts-*.mp3`）；其他 provider：`CallbackSegmentPlayer` 按段调 `_play_tts`
- 工具开始、还没有正文时念一次 `hermes.tool_filler`（"稍等" / "我查一下"；配置段跟随后端）
- 打断覆盖整个回复过程（包括后端还在思考）：取消 `_speak` 任务 → `player.abort()`（`killall miplayer`、删临时文件）+ `ReplyStream.close()`（断开 HTTP）
- 分段播放一段都没播成（`player.error` 且 `played == 0`）时，整段退回 `_play_tts`
- 回复里调用了 `hermes.end_after_tools`（默认 `spotify_playback`）里的工具时，回答完直接结束对话（不播告别语）：对话中途才放的歌没被 music_duck 压低，接着听会把歌声当成人声，对话永远结束不了。Hermes 工具事件不带参数，分不出播放和查询，一律结束

**灯效**（`core/services/conversation_light.py`）:
- Hermes / 缪斯会话调用 `_light_state(state)`，和原生小爱一致：提示音 / 回答 `speaking` 3（跟播放音量起伏，miplayer 自带）→ `listening` / `hearing` 41（短白灯，随你的音量变宽）→ `thinking` 2 → `speaking` 3 → `off`；不再用黄灯 9
- 聆听灯随音量（`core/services/light_level.py`，配置 `conversation.light_level`）：音箱 `vis` 从 `/tmp/mic_audio.fifo` 读 48 kHz PCM 算音量、UDP 发给 `ledd`；`ubus call mic_audio set_event '{"event":1,"value":1}'` 切到麦克风、`value:2` 切回播放。bridge 开 TCP 4400，音箱 `nc` 连进来写 FIFO；bridge 只送"同响度的噪声"，不送语音
  - `vis` 要求输入不能明显慢于实时，否则关掉重开 FIFO，`nc` 写入 EPIPE 被杀：bridge 用自己的时钟每 110 ms 发一帧 9600 字节（不跟麦克风包走），音箱端 `nc` 被杀自动重连
  - 不能比实时快太多，否则 FIFO 积压成延迟；当前保留小幅噪声作为静音底值；噪声 RMS 约 8000 时实测 `vis` 偶发输出 0，具体原因未确认
  - 宽度按"比跟踪到的底噪高多少 dB"算（`margin_db` / `span_db`），每轮 INFO 日志有一行"麦克风音量"
  - 设备时钟比 bridge 主机快约 12 秒，对照音箱 strace 和 bridge 日志时注意换算
- 应答语（`core/services/tts/wake_reply.py`，配置 `conversation.wake_reply`）：唤醒 / 打断后随机说一句代替"叮"；小爱原生 TTS 时首次用 `mibrain text_to_speech` 生成、存 `/tmp/open-xiaoai-bridge-reply-<hash>.mp3` 复用，`miplayer` 播。打断时必须先 `await stop_device_audio()` 再播（它会 `killall miplayer`）
- 超时：`wakeup.timeout` 秒没开口就退出（播 after_wakeup 告别语）；等待期结束时若已经开口，则继续等待至多 `conversation.max_utterance_s` 秒
- 后台任务按最新状态切灯，不阻塞语音流程；`set()` 可在 VAD/KWS 线程调用

**VAD 状态泄漏陷阱**:
- `VAD.start()` 只初始化线程并保持暂停，输入流以 `start=False` 创建；会话调用 `resume("speech"/"silence")` 后才订阅音频。空闲唤醒由独立 KWS 负责。
- 2026-10-26 真机复现：首次唤醒前 `target=None` 仍处理人声，累计 768.8 秒历史（list 容器约 196 MB）；每帧 `bytes(speech_frames)` 平均耗时约 143 ms，拖慢 KWS/收包并导致停播 RPC 超时。仅在 `target="speech"` 保存开口预录，且仅派发 `on_speech` 时转换 bytes；等停顿时的完整录音由会话层 / codec 保存。
- 如果 `resume()` 不调用 `_reset_state()`，旧帧（唤醒词回声、TTS 回声）会泄漏到下一轮检测，导致 ASR 识别出幽灵音频
- `pause()` 会调 `_reset_state()`，但 `resume()` 必须也调——两者都需要清理状态

**边界约束**:
- 使用独立 VAD Future，不与 WakeupSessionManager 冲突
- 播报被等待完成后才继续监听；唯一的提前结束方式是打断（或 `stop()`）
- 主动关麦（`_stop_recording`）必须同时 `set_recording_wanted(False)`，否则录音看门狗会在播报中重新打开麦克风；会话结束时恢复为 True
- 自己持有并管理当前 TTS 的 `playback_token`；停止 OpenClaw 对话时应调用 `stop_tts_playback(token)`，不要在外层直接无 token 全局停止 Rust TTS

### WakeupSessionManager (core/wakeup_session.py)

小智唤醒会话状态机，协调 KWS → VAD → 小智 / 外部后端的唤醒流程。同一时间最多一个外部后端连续对话（`_controller` / `_controller_task`）。

- `wakeup(text, source)` → 处理唤醒（调用 `before_wakeup` 钩子，路由到 XiaoZhi 或 `core/backends.py` 里注册的后端）
- `wait_next_step(timeout)` → 异步等待状态变化（带待决状态缓冲）
- `update_step(step, step_data)` → 更新步骤
- 事件回调：`on_interrupt()`, `on_wakeup()`, `on_tts_start()`, `on_tts_end()`, `on_speech()`, `on_silence()`
- `on_interrupt()` → 小爱唤醒时：cancel 外部后端对话 task、停止设备音频播放、恢复录音通道、stop XiaoAI conversation

**路由规则**（`before_wakeup` 返回值）:
- `"xiaozhi"` → 走小智流程
- `"hermes"` → 走 Hermes 连续对话；"你好缪斯"唤醒词（`MUSE_ENABLE=1`）路由到这里
- `"openai"` / `"openclaw"` / `"qwenpaw"` → 对应后端的连续对话
- 其他字符串或后端没启用 → 记错误日志，不进入对话
- `None` → 不处理（用户自行处理）

**小爱管家（muse）边界约束**:
- `core/muse.py` 只负责唤醒词（匹配、KWS 子集、拦截原生小爱），不再有自己的请求链路；对话由 `"hermes"` 路由负责
- `core/muse.py` 顶层不能导入 `open_xiaoai_server`（config.py / 测试会直接导入）

**边界约束**:
- 它是"小智唤醒会话状态机"，不是通用事件总线
- 只允许缓存 `on_speech` / `on_silence` 等外部探测信号
- 不要缓存 `on_wakeup` / `on_interrupt` 等控制步骤

### XiaoAIConversationController (core/xiaoai_conversation.py)

小爱自身的连续对话管理。

- `handle_text_command(text, speaker)` → 处理退出 / 连续对话关键词
- `handle_listening_timeout(speaker)` → 超时重试逻辑
- `handle_audio_player_instruction(header_name)` → 检测播放器指令退出
- `handle_playing_status(playing_status, speaker)` → TTS 完成后重新唤醒

**边界约束**:
- 小爱连续对话和小智唤醒 / 会话超时是两套独立机制
- 只有在"小爱连续对话确实激活"时才允许停止
- 小智超时退出时不应打印"小爱停止连续对话"日志

### SpeakerManager (core/services/speaker.py)

音箱硬件控制。

- `play(text, url, buffer, blocking, timeout)` → 播放文字 / URL / PCM 缓冲
- `stop_device_audio()` → 停止设备上的播放链路（阻塞 TTS / 非阻塞 TTS / PCM），并重启 PCM 播放通道
- `wake_up(awake, silent)` → 唤醒 / 休眠小爱
- `abort_xiaoai()` → 中断小爱当前操作
- `ask_xiaoai(text, silent)` → 让小爱执行指令
- `run_shell(command, timeout)` → RPC shell

**边界约束**:
- `stop_device_audio()` 只负责"停播放"，不负责恢复录音；`start_recording()` 属于会话层恢复逻辑，应由 `WakeupSessionManager` / `ExternalConversationController` 等上层按场景决定

### APIServer (core/services/api_server.py)

HTTP REST API 服务器（aiohttp），端口可配（默认 9092），默认监听 `127.0.0.1`（`API_SERVER_HOST`）。
设置 `API_SERVER_TOKEN` 后除 `/api/health` 外都需要 `Authorization: Bearer <token>`。

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/play/text` | POST | 播放文本 |
| `/api/play/url` | POST | 播放 URL |
| `/api/play/file` | POST | 播放本地文件 |
| `/api/status` | GET | 获取设备状态 |
| `/api/wakeup` | POST | 唤醒设备 |
| `/api/interrupt` | POST | 中断播放 |
| `/api/health` | GET | 健康检查 |
| `/api/tts/doubao` | POST | Doubao TTS 合成 |
| `/api/tts/doubao_voices` | GET | 获取音色列表 |

### 音频处理链

| 模块 | 文件 | 职责 |
|------|------|------|
| GlobalStream | `audio/stream.py` | 多路输入广播（模拟 PyAudio API） |
| VAD | `audio/vad/silero.py` | Silero ONNX 语音活动检测 |
| KWS | `audio/kws/sherpa.py` | Sherpa ONNX 关键词唤醒（信心度 2.0，阈值 0.2） |
| ASR | `audio/asr/sherpa.py` | Sherpa SenseVoice 离线语音识别（懒加载，INT8 量化） |
| TTS | `tts/openai.py`, `tts/mlx_audio.py`, `tts/doubao.py` | OpenAI-compatible TTS、MLX-Audio 与豆包 TTS（一次性/本地音频播放） |

### 多麦唤醒（multimic，默认关闭）

- 音箱 `hw:0,3`（Capture）4 声道 48 kHz S32：**ch0～ch2 是 3 个麦克风**，ch3 是播放回采；client 的 echo_ref 只发 ch0 + ch3
- 原生小爱（`mipns-xiaomi`，配置 `/usr/share/mipns/vpm/json_segment/flexkws.json5`）：Kalman AEC → 去混响 → IVA 分离 3 路 → 每路一个约 200 KB 的专用唤醒模型（只有"小爱同学"的 10 个音素单元，不能改成别的词）→ 选通道 → 二级模型复核
- 我们的做法（`multimic.enabled` / `MULTIMIC_ENABLE=1`）：bridge 开 4401，音箱 `arecord -D Capture -f S16_LE -r 16000 -c 4 | nc`（不改 client；S16 ≈ S32 >> 16，乘 `gain` 256 对齐线上电平）→ 每麦 AEC → 在线 AuxIVA → 额外 3 路 `KeywordDetector`（`SherpaOnnx.new_spotter()` 各自独立模型，在线程池里和 ch0 并行解码；共用模型串行时每 32 ms 帧要算约 48 ms，说话时唤醒会晚 0.4～0.5 秒），和 ch0 并行；第一路命中即唤醒，2 秒内其余路的命中只记日志
- VAD / ASR / 打断补录仍走原来的单声道；看门狗 `stale_s` 秒没数据就重新在音箱上启动
- 离线对比：`scripts/multimic_record.py` 录 4 声道 → `scripts/multimic_eval.py --asr`

### 对话期间压低音乐（music_duck）

- 音乐一直响会被唤醒后的 VAD / ASR 当成用户说话（歌词被识别成问题，对话停不下来）；`ExternalConversation.start()` 压低、`stop()` 恢复，都不阻塞
- Spotify：bridge 启动时建 SSH 长连接（每次新开 SSH 到 Oracle 要 2～3 秒），stdin 发 `duck` / `restore`；远端用 Spotify Web API，压到 `volume`（默认 15%），设备不支持调音量就暂停
  - `/me/player` 的音量要 1～1.5 秒才更新：恢复时读到"压低后"或"压低前"的值都照常恢复，只有第三个值才算用户自己调过
  - SSH 断开（bridge 退出 / 断网）远端自己恢复；压低超过 300 秒也自动恢复
  - access token 只放内存（Hermes 容器以 root 写的 token.json ubuntu 读不了）；Spotify 换发 refresh_token 时必须原子写回 auth.json，否则 Hermes 的 spotify_ctl 失效
  - 必须用 Git 自带的 ssh：Windows OpenSSH 嫌 `~/.ssh/config` 权限太宽（CodexSandboxUsers）会直接退出
- 音箱播放器：`player_get_play_status` status == 1 时 `player_play_operation pause`，退出时只在 status == 2 且是我们暂停的才 `play`（status 含义未在真机播放时验证）
- 高通滤波（`audio_input.highpass_hz` 120）：离线对比（146 条合成唤醒词 × 3 种起点，叠加真实房间底噪 / 50 Hz 嗡声）对唤醒率影响在 ±1% 内，不伤人声也不提升唤醒

### Rust 原生扩展 (native/)

通过 maturin + PyO3 编译的 `open_xiaoai_server` Python 模块。

| 文件 | 职责 |
|------|------|
| `lib.rs` | 模块入口：`on_output_data`, `start_server`, `stop/start_recording`, `stop/start_playing`, `run_shell` |
| `server.rs` | TCP :4399 WebSocket 服务器，处理音频流和事件路由 |
| `python.rs` | Python 回调注册中心（`register_fn` / `call_fn`），跨语言调用 |
| `tts/` | TTS 音频处理：HTTP 流式请求、MP3 解码、PCM 直通 |

## 运行模式

### 模式 1: 仅小爱（默认）
```bash
uv run main.py
```
- 不启动 KWS/VAD 初始化
- `core/services/audio/kws/keywords.py` 在此模式下应直接退出成功

### 模式 2: 小智 AI
```bash
XIAOZHI_ENABLE=1 uv run main.py
```
- 启动 VAD + KWS，唤醒后连接小智 AI
- KWS 初始化失败应视为启动失败

### 模式 3: OpenClaw
```bash
OPENCLAW_ENABLE=1 uv run main.py
```
- 小爱指令拦截 → 转发到 OpenClaw → TTS 播放结果

### 模式 4: 小智 + OpenClaw（混合）
```bash
XIAOZHI_ENABLE=1 OPENCLAW_ENABLE=1 uv run main.py
```
- config.py `before_wakeup` 按唤醒词路由到小智或 OpenClaw 连续对话
- OpenClaw 连续对话：VAD → ASR → OpenClaw → TTS 循环
- 退出关键词：config `openclaw.exit_keywords`

### 启用 API Server
```bash
API_SERVER_ENABLE=1 uv run main.py
```

## 开发规范

### 代码风格
- 中文注释和文档字符串
- 英文 commit message
- 类型提示: `dict[str, asyncio.Future]`

### 异步编程
- 所有 I/O 使用 `async/await`
- 线程安全使用 `asyncio.run_coroutine_threadsafe()`
- `MainApp.loop` 是业务协程主循环
- `XiaoAI.async_loop` 仅用于原生扩展回调桥接，不挂新业务状态机

### 日志规范
- 所有日志必须带模块标识：通过 `module=` 参数或 `[Module]` 前缀
- 使用 `core.utils.logger.logger`，禁止裸 `print`
- 调试输出用 `DEBUG` 级别，不污染 `INFO`
- 消息体不要重复模块名（模块名已在日志前缀中）
- 唯一允许的裸输出：启动 ASCII banner

### 全局引用 (ref.py)
- `set_app/get_app`, `set_xiaozhi/get_xiaozhi`, `set_xiaoai/get_xiaoai`
- `set_vad/get_vad`, `set_kws/get_kws`, `set_speaker/get_speaker`
- `set_audio_codec/get_audio_codec`, `set_speech_frames/get_speech_frames`

### 本机运行注意
- bridge 在跑时不要用 `uv run` / `uv sync`：它会重装项目包，`open_xiaoai_server.pyd` 被占用删不掉，重装中断后 `open_xiaoai_server/__init__.py` 会丢，下次启动报 `no attribute 'register_fn'`。跑脚本直接用 `.venv/Scripts/python.exe`
- 已经弄坏时：从 `dist-migration/*.whl` 取回 `open_xiaoai_server/__init__.py`（或 `uv pip install --force-reinstall --no-deps` 这个 wheel，需先停 bridge）
- 重启：结束 `start-bridge.bat` 的 cmd 进程树，再 `schtasks /Run /TN \OpenXiaoAI-Bridge`；日志在 `logs/bridge-autostart.log`
- 2026-10-27 起 bridge 改在平板（OPPO Pad Mini，SM8845P，KernelSU root，192.168.1.217）上运行，Windows 计划任务已 Disabled；音箱 `/data/open-xiaoai/server.txt` 指向 `ws://192.168.1.217:4399`（原值备份在 `server.txt.pc-bak`）。光猫（联通 ZXHN G7606V6，192.168.1.1）"基本配置 → LAN配置 → 静态分配客户端配置"已绑定：平板 `c0:2f:cd:a3:e9:df`（Wi‑Fi 用设备 MAC）→ .217，音箱 `50:fe:39:f4:a5:b3` → .73。**同一时间只能有一个 bridge 连音箱**，切回 PC 前先停平板

### 平板部署（deploy/android-chroot/）
- Ubuntu 24.04 arm64 chroot 在 `/data/local/ubuntu`，项目在 chroot 内 `/opt/open-xiaoai-bridge`（git archive + 模型 + `.hermes_api_key`，不是 git 仓库）；管理脚本在 Android 侧 `/data/local/ubuntu-tools/`，都要用 `su -M` 跑（挂载进全局命名空间）
- 运维：`adb shell "su -M -c 'sh /data/local/ubuntu-tools/bridge-ctl.sh status|start|stop|restart|log 50'"`；日志 `/data/local/ubuntu/opt/open-xiaoai-bridge/logs/bridge-autostart.log`，守护日志 `/data/local/ubuntu-tools/run/supervisor.log`
- 开机自启：`/data/adb/service.d/openxiaoai-bridge.sh`（等 boot_completed + wlan0 有 IP）。守护循环持 wakelock、每 15 秒重设 `iw dev wlan0 set power_save off`、放根 cpuset（0-7 核），bridge 退出按退避重启
- sherpa-onnx 的 arm64 wheel 不带 ONNX Runtime：需要 `onnxruntime/capi/libonnxruntime.so` 软链 + `LD_LIBRARY_PATH`（`run-bridge.sh` 自动处理）
- 平板上的 FlClash VPN 会让 chroot 外网走 tun0，Hermes 连接慢且偶发 DNS 失败；保持关闭或让 132.226.76.2 直连
- chroot 里还装了 Node v24（`/opt/node`）、pi（`@earendil-works/pi-coding-agent`，`/root/.pi/agent/` 用 magpie.oci 的 12 个模型）和 paseo（`@getpaseo/cli`，`/root/.paseo`，监听 127.0.0.1:6767 + relay 配对，开机由 service.d 脚本 `paseo start`）；重装见 `setup-agents.sh`
- SSH：chroot 内 sshd 监听 2222（仅密钥，`UsePAM no`；`setup-sshd.sh`，开机由 service.d 启动）。PC 上 `ssh tablet`（`~/.ssh/config`，默认用无密码短语的 `~/.ssh/tablet_ed25519`；`id_ed25519` 有密码短语，BatchMode 用不了）。SSH 进来是在 chroot 里，碰不到 Android 侧的 `bridge-ctl.sh`；要重启 bridge 可以 `pkill -INT -f "python main.py"`，守护循环约 3 秒后自动拉起
- 开发副本：chroot 里 `/root/projects/open-xiaoai-bridge` 是 git clone（远端 sounfury / origin，给 paseo / pi 用），`core/models`、`tests/data`、`.hermes_api_key` 软链到 `/opt` 那份；`/opt/open-xiaoai-bridge` 只用来运行。开发副本里不要跑 `main.py`（和运行中的 bridge 抢端口）。chroot 里 uv 默认 `UV_FROZEN=1`：uv.toml 配了 USTC 镜像，不冻结会把整个 uv.lock 改写成镜像地址；venv 里 `sherpa_onnx/lib/libonnxruntime.so` 软链到 onnxruntime 的 .so
- chroot 里操作 Android：`android '命令'`（`/usr/local/bin/android`，`nsenter -t 1 -m -r` 回到 Android 侧，环境变量取自 zygote）。`android 'input tap x y'`、`android 'screencap -p' > s.png`、`android 'uiautomator dump /data/local/tmp/ui.xml'` 再 `android 'cat ...'` 读出来；屏幕 1680x2520
- Git Bash 调 adb 要 `MSYS_NO_PATHCONV=1`，否则 `/data/...` 被改成 Windows 路径
- 更新代码：`git archive HEAD` 推到 `/data/local/tmp/xfer/`，在 chroot 里解压覆盖后 `bridge-ctl.sh restart`；依赖变了才在 chroot 里 `uv sync`
- 性能基准 `scripts/perf_bench.py`（持续说话最坏情况）：平板实时率约 0.52（熄屏 0.56），PC 0.85；SenseVoice 识别 5 秒音频约 105 ms

### 兼容约束
- `CLI` 环境变量不再作为功能开关，不要引入依赖 `CLI` 的运行时分支
- `XIAOZHI_ENABLE=0` 时必须允许跳过 KWS 初始化
- `scripts/start.sh` 在仅小爱模式下不应检查 `core/models/` 下的模型文件

## 测试
不写任何单测，直接连接音箱做验收测试

## 音箱设备控制命令

小爱音箱（LX06 等）基于 OpenWrt + busybox，设备端命令和行为如下：

### 音频播放通道

音箱上有多条独立的音频播放通道，中断时需要分别处理：

| 通道 | 进程/服务 | 触发方式 | 中断方式 |
|------|-----------|---------|---------|
| PCM 直通 | `aplay` | `open_xiaoai_server.start_playing()` → WebSocket stream | `open_xiaoai_server.stop_playing()` |
| 阻塞 TTS | `tts_play.sh` → `miplayer -f <file>` | `speaker.play(blocking=True)` | `killall tts_play.sh miplayer` |
| 分段 TTS（流式回复） | `miplayer -f /tmp/open-xiaoai-bridge-tts-*.mp3` | `XiaoAINativeSegmentPlayer`（先 `mibrain text_to_speech save=1` 生成） | `player.abort()` / `killall miplayer` |
| 非阻塞 TTS | `mibrain_service` (内部播放) | `speaker.play(blocking=False)` → `ubus call mibrain text_to_speech` | `mphelper pause`（不一定可靠） |
| 媒体播放器 | `mediaplayer` (系统守护进程) | `ubus call mediaplayer player_play_url` | `mphelper pause` / `ubus call mediaplayer player_play_operation '{"action":"pause"}'` |

### tts_play.sh 工作流程

`/usr/sbin/tts_play.sh` 是设备上的阻塞 TTS 脚本，内部流程：
1. `mphelper pause` — 暂停当前播放
2. `ubus call mibrain text_to_speech '{"text":"...","save":1}'` — 生成音频文件到 `/tmp/tts/`
3. `miplayer -f <path>` — 播放音频文件（子进程）
4. `rm <path>` — 清理临时文件

**关键注意事项**：
- 杀掉 `tts_play.sh` **不会**自动杀掉子进程 `miplayer`，必须同时 `killall miplayer`
- `miplayer` 是一次性播放器（非守护进程），杀掉后不影响后续 TTS 调用
- busybox 的 `pkill` 无法匹配到 `miplayer`，必须用 `killall`

### 录音通道

| 操作 | 命令 | 说明 |
|------|------|------|
| 停止录音 | `open_xiaoai_server.stop_recording()` | 杀掉设备端 `arecord` 进程，麦克风静音 |
| 恢复录音 | `open_xiaoai_server.start_recording()` | 重启 `arecord`，音频数据恢复流入 `GlobalStream` |

**注意**：回采不在线时，对话中 TTS 播放会 `stop_recording` 防止回声（回采在线时不关麦）。如果在此期间触发中断（"小爱同学"），必须在中断处理中调用 `start_recording` 恢复录音，否则 KWS 将因无音频数据而永久失效。

### on_interrupt 中断处理要点

`on_interrupt()` 触发时（用户喊"小爱同学"），需要完成以下步骤：
1. Cancel OpenClaw asyncio task
2. 让 OpenClaw controller 自己停止当前 TTS（使用自己持有的 `playback_token`）
3. `SpeakerManager.stop_device_audio()` — 停止阻塞 TTS / 非阻塞 TTS / PCM，并重置 PCM 通道
4. `start_recording` — 恢复录音（KWS 依赖此通道）
5. `XiaoAI.stop_conversation()` — 停止连续对话

### 不可用的中断方式

以下方式在实践中验证**不可靠或有副作用**：
- `abort_xiaoai()`（重启 `mico_aivs_lab`）— 会导致小爱整体不可用，恢复需 1-2 秒
- `pkill miplayer` — busybox `pkill` 无法匹配 `miplayer` 进程名
- `ubus call mediaplayer player_play_operation '{"action":"pause"}'` — 对 `mibrain text_to_speech` 触发的播放无效

### 相关讨论

- [open-xiaoai#36](https://github.com/idootop/open-xiaoai/issues/36) — 小爱 TTS 打断方案讨论

## 参考资源

- 项目主页: https://github.com/sounfury/open-xiaoai-muse
- 上游（分叉基础）: https://github.com/coderzc/open-xiaoai-bridge
- 回声消除 / 唤醒调优 / 音箱客户端来源: https://github.com/OwnDing/open-xiaoai（`7ecea6f`，见 `docs/ownding-migration-plan.md`）
- 刷机教程: https://github.com/idootop/open-xiaoai/blob/main/docs/flash.md
- Client 端（OwnDing 版）: https://github.com/OwnDing/open-xiaoai/blob/main/packages/client-rust/README.md
