# Changelog

All notable changes to this project will be documented in this file.

## Unreleased (sounfury/open-xiaoai-muse)

### 重点更新

- 项目独立并改名为 open-xiaoai-muse（由 coderzc/open-xiaoai-bridge `a1f77a8` 分叉，并移植 OwnDing/open-xiaoai 的能力）；LICENSE 保留原有版权声明并加入 sounfury，README 致谢列出三个来源项目。删除 Docker 部署（Dockerfile、docker-compose.yml、镜像构建和发布 workflow）。以下 v1.0.7 及更早的版本是上游的发布记录。
- 新增小爱管家"你好缪斯"唤醒词（`MUSE_ENABLE=1` 启用）：KWS 命中唤醒词或小爱 ASR 文本以唤醒词开头时，`before_wakeup` 返回 `"openai"`，进入 OpenAI 兼容服务（Hermes）连续对话。"小爱同学"原生链路不变。
  - 早期版本把请求单次 POST 到小爱管家 webhook，并在只说唤醒词时追问一句；已移除（`MuseWebhookClient` / `MuseRequestHandler` / `core/muse_followup.py`、`MUSE_WEBHOOK_URL` / `MUSE_WEBHOOK_TOKEN`、`muse.url` 等配置）。
- 小爱管家唤醒词改为集合（容忍口误和 ASR 误识别）：默认包含 `你好缪斯` / `嗨缪斯` / `哈喽缪斯` / `hi缪斯` / `hi muse` / `hey muse` / 单独的 `缪斯` / `muse`，以及同音字（缪思、缪丝、谬斯、谬思）和近音字（妙思、妙斯、木斯、慕斯，仅限带问候语），共 52 个写法。可用 `MUSE_WAKE_WORDS`（`,` `，` `、` 分隔）或 `muse.wake_words` 覆盖。
  - 匹配规则：NFKC（全角 → 半角）+ 忽略大小写、空格和中英文标点；默认只在句首匹配（可跳过句首语气词 `muse.leading_fillers`）；最长优先；英文唤醒词要求词边界（`museum` 不触发 `muse`）。`match_mode = "contains"` 仅作为显式开启的选项保留。
  - 解析结果给出命中的那一项（配置里的写法）和去掉唤醒词及其后标点的剩余文本。
  - 本地 KWS 只并入"问候语 + 名字"子集（`muse.kws_wake_words` / `MUSE_KWS_WAKE_WORDS`，默认 `你好缪斯` `嗨缪斯` `哈喽缪斯` `hi muse` `hey muse` `hello muse`），`MUSE_ENABLE=1` 时生成 `keywords.txt` 自动合并、去重；单独的 2 音节名字和同音/近音字只走 ASR 前缀，降低常驻监听误唤醒。
  - 小爱原生 ASR 文本等于缪斯 KWS 唤醒词（忽略大小写和标点，如 `Hey Muse.`）时，同样按外部唤醒词接管。
- 关键词生成（`core/services/audio/kws/keywords.py`）改为逐条编码：含模型词表外字符的关键词跳过并告警（`sherpa_onnx.text2token` 原本会静默丢弃），全部失败时返回非零；关键词先做 NFKC 再转大写。
- HTTP API 新增可选 Bearer 鉴权：设置 `API_SERVER_TOKEN` 后 `/api/*`（`/api/health` 除外）需携带 `Authorization: Bearer <token>`；未设置时行为不变。`skills/xiaoai-tts` 支持 `OPENXIAOAI_API_TOKEN`。
- `/api/play/text` 支持可选 `request_id` 字段（写入日志并原样返回）。
- 连续对话灯效改为状态驱动、后台切换（`core/services/conversation_light.py`，替代 `listening_light.py`）：等你说 9 → 听到你在说 41 → 思考 2 → 播报 3 → 退出熄灭；切灯不再阻塞开始听。
- 播报中打断（`barge_in.enabled`，需要回采）：音箱发回播放回采，bridge 用子带回声消除（移植自 OwnDing，加了起步发散和放大保护）去掉回声；播报时不关麦，喊唤醒词立即停播并直接进入下一轮，唤醒词后紧接着说的话会补录。回采不在线时自动退回关麦播报。新增录音看门狗（断流 4 秒重新请求录音，主动关麦期间不触发）。
- `native/` 改用 OwnDing/open-xiaoai 的 crate（需要配套的 OwnDing 音箱客户端 `client-2026.10.09`+，旧客户端不兼容）：二进制音频帧、新连接接管旧连接、快速重连不重复播"已连接"；可请求播放回采（`barge_in.echo_reference`）。`audio_input.gain` 随之改回 1.0（新客户端电平高约 256 倍）。见 `docs/ownding-migration-plan.md`。

### 开发与测试

- 新增 `tests/test_muse.py`、`tests/test_muse_wake_words.py`、`tests/test_api_server.py`、`tests/test_conversation_light.py`、`tests/test_echo_ref_input.py`，无需音箱、原生扩展与模型。

## v1.0.7 - 2026-07-14

### 重点更新

- 新增 OpenAI 兼容 Chat 后端，可对接任意 OpenAI 协议兼容的 LLM 服务作为对话后端。(#15)
- 新增 Doubao（豆包）ASR provider，支持使用豆包语音识别能力。(#13 by @gao19970120)
- 新增 FireRedASR INT8 后端支持，提供更多离线 ASR 模型选择。
- 支持 OpenClaw 协议 v4 握手，兼容新版 OpenClaw 客户端。(#20)

### 修复与优化

- 修复 OpenAI 模式下自定义唤醒词路由问题。(#17)
- 优化 MyStream 音频读取性能：改用基于 offset 的读取与惰性清理，避免每个音频帧触发 O(n) 的 memmove，降低 CPU 占用。(#22 by @法塔·艾莉娅)

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.6...v1.0.7

## v1.0.6 - 2026-04-05

### 重点更新

- 新增 WebSocket Bearer Token 鉴权支持，设置 `OPEN_XIAOAI_TOKEN` 环境变量后，客户端须在握手时携带 `Authorization: Bearer <token>` 请求头，否则连接将被拒绝（返回 401）。未设置该变量时保持原有无鉴权行为。
- 连接日志新增鉴权失败原因输出，方便快速定位认证问题。

### 修复与优化

- 修复 OpenClaw agent 事件未按 `run_id` 过滤的问题，避免多次唤醒后事件监听器持续累积导致的内存泄漏。

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.5...v1.0.6

## v1.0.5 - 2026-03-29

### 重点更新

- 新增小爱原生 ASR 模式 (`OPENCLAW_XIAOAI_NATIVE_ASR`)，可在 OpenClaw 连续对话中使用小爱自带的语音识别能力，降低对离线 ASR 模型的依赖。
- 新增可配置的音频输入增益 (config `audio.input_gain`)，支持调节麦克风输入音量以优化唤醒词识别灵敏度。
- 新增音频输入开关 (`AUDIO_INPUT_ENABLE`)，可在不需要音频输入时禁用以节省系统资源。
- 新增发送消息提示音，改善 OpenClaw 连续对话的交互体验。(#11 by @codertinat)

### 修复与优化

- 修复 OpenClaw 小爱原生 ASR 模式下的超时处理，确保桥接超时配置被正确遵循。
- 优化环境变量命名：`OPENCLAW_ENABLED` → `OPENCLAW_ENABLE`（保留向后兼容，新变量优先）。
- 优化 CMake 启动脚本，修复构建相关问题。(#11 by @codertinat)

### 文档更新

- 补充音频输入增益配置的 FAQ 说明。
- 优化 Docker FAQ 格式，统一文档风格。
- 更新 OpenClaw 连接说明文档。

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.4...v1.0.5

## v1.0.4 - 2026-03-26

### 重点更新

- 新增可配置的 ASR 后端，支持通过配置切换不同语音识别模型。
- 优化设备端音频播放链路，通过延迟启动播放降低 `aplay` underrun 问题。
- 优化长时间运行场景下的内部状态管理，减少潜在内存泄漏风险。

### 修复与优化

- 修复 `after_wakeup` 回调中未正确透传 `source` 参数的问题，改善小智/OpenClaw 会话退出后的收尾逻辑。
- 调整 XiaoZhi、XiaoAI、OpenClaw 以及原生音频相关实现，优化稳定性与部分边界行为。
- 补充和整理 Docker / README 相关说明，提升部署与使用时的可读性。

### 文档更新

- 补充并整理项目文档说明，优化 README 的来源说明、致谢与相关文案表达。
- 更新 LICENSE 中的版权声明，保留上游作者信息并补充当前项目维护者信息。
- 更新 Docker 使用说明，改善 Windows 用户的部署体验。(#8 by @JackieQiang)

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.3...v1.0.4

## v1.0.3 - 2026-03-25

### 重点更新

- 豆包 TTS 升级支持新的 2.0 音色，并补充配套的辅助脚本与接口文档，便于查询和验证可用音色。
- 新增 `scripts/clone_voice.py` 声音复刻脚本，支持提交音频样本并查询训练状态。
- 新增 `scripts/generate_tts.py` 音频生成脚本，可按指定 `speaker_id`、文本和情感参数导出音频文件。
- 新增播放服务端音频文件的能力，可通过 API 直接下发本地文件进行播放。
- 优化 OpenClaw TTS 打断与设备音频关闭流程，减少播放被打断后残留音频状态未清理的问题。

### 修复与优化

- 修复外部唤醒词触发时，小爱仍然回声式回复的问题，降低路由到第三方 AI 时的干扰。
- 修复用户喊出“小爱同学”打断后，小智唤醒会话没有完全恢复的问题，避免后续唤醒失效。
- 在 Doubao TTS API 返回成功前增加请求校验，避免无效请求被误判为成功。
- 优化 Doubao TTS 的错误处理与日志输出，减少重复报错，并在流式/后台播放失败时保留更完整的上下文。
- 调整 `docker-compose.yml`，移除 `network_mode: host`，改善默认 Docker Compose 部署的兼容性。
- 调整部分 XiaoZhi/OpenClaw 内部流程与日志细节，减少连续对话等待和排障成本。

### 文档更新

- 补充 Doubao TTS 接口、声音复刻和指定音色导出脚本的使用说明。

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.2...v1.0.3
