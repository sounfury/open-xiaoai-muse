"""用户配置：音箱接入、音频参数、唤醒路由及各 AI 后端设置。"""

import asyncio
import socket
import subprocess
import sys
import time
import uuid

import requests


# 每次唤醒生成独立 Session，对话互不干扰
# 适合以下场景：
#   - "提问 → 回答"式交互，不需要 Agent 记住上下文
#   - 长期使用同一 Session 导致 Agent 上下文窗口堆积过长，影响响应质量和速度
# def new_session_key():
#     return f"agent:main:session-{uuid.uuid4().hex[:8]}"


async def before_wakeup(speaker, text, source, app):
    """
    处理收到的用户消息，并决定是否唤醒 AI。

    参数：
        speaker : SpeakerManager，可调用 play/abort_xiaoai/wake_up 等方法
        text    : 识别到的文字内容
        source  : 唤醒来源
                    'kws'    — 本地关键词唤醒（用户说了唤醒词）
                    'xiaoai' — 小爱同学收到用户语音指令
        app     : MainApp 实例，可调用 send_to_backend(name, text) / send_to_hermes / send_to_openclaw 等方法

    返回值（后端名见 core/backends.py，对应的后端需要用环境变量启用）：
        "hermes"   — 进入 Hermes Agent 连续对话（HERMES_ENABLE=1）
        "openai"   — 进入通用 OpenAI 兼容服务连续对话（OPENAI_ENABLE=1，Ollama / LM Studio 等）
        "openclaw" — 进入 OpenClaw 连续对话（OPENCLAW_ENABLE=1）
        "qwenpaw"  — 进入 QwenPaw 连续对话（QWENPAW_ENABLE=1）
        "xiaozhi"  — 进入小智 AI 流程
        None       — 不做额外处理（可在此自行调用 app.send_to_backend 等）

    ---
    动态切换 session_key：
        每次进入此函数前，框架会自动将各后端的 session_key 重置为配置文件中的默认值。
        如需路由到其他 Agent，在 return "openclaw" 之前调用：
            app.set_backend_session_key("openclaw", "agent:main:xxx")
        不调用则自动使用 openclaw.session_key 的默认值，无需手动重置。
    """
    # --- 小爱管家："你好缪斯" → Hermes 连续对话（需 MUSE_ENABLE=1 和 HERMES_ENABLE=1）---
    # KWS 命中唤醒词，或小爱 ASR 文本以 muse.wake_words 中任一项开头（如"你好缪斯，我今晚想洗澡"、"Hey Muse，开灯"）
    from core.muse import match_muse_text, suppress_native_xiaoai

    if match_muse_text(text):
        if source == "xiaoai":
            await suppress_native_xiaoai(speaker)  # 拦截原生小爱对这句话的处理
        return "hermes"

    # 其他唤醒词 / 小爱指令不处理（上游的龙虾、小黑、小爪、小智演示路由已删除）
    return None


async def after_wakeup(speaker, source=None, session_key=None):
    """
    退出唤醒状态

    - source: 退出来源
        - 'xiaozhi': 小智对话超时退出
        - 'hermes' / 'openai' / 'openclaw' / 'qwenpaw': 对应后端的连续对话退出
    - session_key: 当前后端的 session_key
        可据此区分是哪个 Agent 退出，例如播放不同的退出提示语
    """
    if source == "hermes":
        # 用 Hermes 后端配置的 TTS（hermes.tts_provider），和对话回复同一个声音；TTS 不可用时自动回退小爱原生
        from core.hermes import HermesManager

        await HermesManager.play_text("再见")


APP_CONFIG = {
    "wakeup": {
        # 本地 KWS 常驻监听的唤醒词（英文字母要全小写）。只留缪斯一组：每多一个词就多一条误唤醒路径。
        # 和 muse.kws_wake_words 相同（MUSE_ENABLE=1 时两边合并去重）；放在这里是为了不开 MUSE_ENABLE 时 KWS 也能用。
        # 单独的"缪斯"不放：验收时句子里提到"缪斯"（"她是我的缪斯"）几乎都会误唤醒，"妙思"也会。
        # 调整后用 scripts/kws_acceptance.py 验收（用法见脚本开头的说明）。
        "keywords": [
            "你好缪斯",
            "嗨缪斯",
            "哈喽缪斯",
            "嘿缪斯",
            "hi muse",
            "hey muse",
            "hello muse",
        ],
        # 静音多久后自动退出唤醒（秒）
        "timeout": 20,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    "kws": {
        # 唤醒词置信度加成（越高越难误触发，越低越灵敏）
        "keywords_score": 2.0,
        # 唤醒词检测阈值（越低越灵敏，越高越难触发）
        "keywords_threshold": 0.2,
        # 单独调整某个唤醒词的阈值 / 加成（键写法同 wakeup.keywords，没列出的用上面的默认值）。
        # 嗨缪斯 / 嘿缪斯 第一个字轻、容易被吃掉，阈值放低；hey / hello muse 会把 "museum" / "amuse" 认成它，阈值提高。
        "keyword_thresholds": {
            "嗨缪斯": 0.02,
            "嘿缪斯": 0.02,
            "hi muse": 0.1,
            "hey muse": 0.25,
            "hello muse": 0.35,
        },
        "keyword_scores": {},
        # 解码时保留的候选数：越大越不容易漏，也越耗 CPU（sherpa 默认 8）
        "max_active_paths": 16,
        # 同时跑几路检测，起点各错开多少 ms，任一路认出即唤醒。模型按约 320 ms 一块处理音频，
        # 唤醒词落在块里的位置会影响能不能认出来；[0] = 只跑一路
        "stream_offsets_ms": [0, 80, 160, 240],
        # VAD 判定开口前的这段音频也补进模型（唤醒词开头的字偏轻，VAD 判定时已经说出一小段）
        "preroll_ms": 300,
        # True：只在 VAD 判定有人说话时才跑唤醒检测（省 CPU）；False：每帧都跑
        "vad_gate": True,
        # 唤醒词检测时的最小静默时长（ms），静默超过该时长则判定为说完
        "min_silence_duration": 480,
    },
    "vad": {
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.10,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 500,
    },
    # 外部后端（Hermes / OpenAI / OpenClaw / QwenPaw）连续对话的轮次衔接
    "conversation": {
        # 唤醒后 / 每轮回答播完后播提示音。有回采时提示音不耽误听：这期间说的话会回放给 VAD
        "start_sound": True,
        "next_turn_sound": True,
        # 唤醒 / 打断后随机说一句应答语（和原生小爱一样），代替唤醒时的"叮"。
        # 小爱原生 TTS 时用小爱自己的声音：第一次在音箱上生成并缓存，之后直接播；其他 TTS 用对应的 TTS 合成。
        # 失败时退回"叮"。唤醒时的应答还受 start_sound 控制
        "wake_reply": {
            "enabled": True,
            "phrases": ["我在", "在", "哎", "在呢", "我在听"],
            "on_wake": True,  # 喚醒时
            "on_barge_in": True,  # 播报中被喚醒词打断时
        },
        # 一句话最长录多久（秒）。wakeup.timeout 只管"多久没开口就退出"，已经在说的不受它限制
        "max_utterance_s": 60,
        # 回答播完后跳过这么长再开始听（避开播报的最后一点余音），ms
        "tts_end_guard_ms": 150,
        # 唤醒词认出时已经过去一小段，从认出的位置往回这么多开始听（"你好缪斯，开灯"），ms
        "keyword_tail_ms": 100,
        # 没有回采、重新开麦后丢掉开头这么长（可能带着提示音余音），ms
        "mic_start_skip_ms": 150,
        # 断句：连续静音至少这么久才检查句尾（只用于连续对话），ms。
        # 允许句中换气和短暂停顿；识别检查期间仍持续收音，续话会撤销旧断句。
        "end_silence_ms": 1200,
        # 句尾像没说完（"帮我把"、"凌晨12点的"、"那个"）时，连续静音总共等到这么久，ms。
        # 规则见 core/utterance.py。设成和 end_silence_ms 一样就关闭额外等待。
        "hesitation_silence_ms": 2600,
        # 流式回复怎么切段（hermes.stream / openai.stream=True 时）：第一段尽快播，后面攒长一点少停顿
        "reply_segments": {
            "first_min_chars": 8,  # 第一段在句号处就出；在逗号处切需要至少这么多字
            "target_chars": 30,  # 后面的段攒到这么多字，在标点处切（本机 GPT-SoVITS：第 2 段短一点，第 1 段播完时它已合成好；原 60）
            "max_chars": 120,  # 一段最长
            "flush_delay_ms": 250,  # 流停住这么久，已经完整的句子先播
            "stall_force_ms": 1200,  # 流停住这么久，没有标点也先播
        },
        # 灯随你说话的音量起伏（和原生小爱一样，见 core/services/light_level.py）：
        # 音箱用 nc 连到 bridge 的 port，bridge 只送音量（同响度的噪声），不送语音内容
        "light_level": {
            "enabled": True,
            "port": 4400,
            "states": ["listening", "hearing"],  # 这些状态让灯跟麦克风走；播报时灯跟 TTS 走（设备自带）
            # 按"比环境底噪高多少"算宽度（底噪自动跟踪，和麦克风增益无关）；每轮日志有一行"麦克风音量"可参考
            "margin_db": 3,  # 比底噪高这么多 dB 开始变宽
            "span_db": 18,  # 再高这么多 dB 白光全宽
            "max_rms": 4000,  # 送给 vis 的最大噪声；vis≈RMS/85，25 左右白光已全宽，8000 以上会偶发溢出成 0
        },
        # 小爱原生 TTS 分段播放（mibrain 提前生成 + miplayer 依次播放）
        "native_tts": {
            "prefetch_segments": 2,  # 最多提前生成几段
            "generate_timeout_ms": 8000,
        },
    },
    "audio_input": {
        # 进入 VAD/KWS/ASR 前放大麦克风输入，1.0 = 不放大。
        # OwnDing 客户端（S32 >> 8）比旧客户端（S16，相当于 >> 16）响 256 倍，说话时已接近满幅，不要再放大；
        # 旧客户端时这里是 4.0。
        "gain": 1.0,
        # 高通滤波（Hz）：去掉这个频率以下的低频（空调 / 冰箱嗡声、桌面震动、音乐低音鼓），人声不受影响。
        # 单声道（VAD / KWS / ASR）和多麦链路都用。None / 0 = 关闭
        "highpass_hz": 120,
        # 高通实现："rust"（native/src/dsp/highpass.rs，默认）| "python"（逐采样循环，备用）
        "highpass_impl": "rust",
    },
    # 对话期间压低音乐（core/services/music_duck.py）：进入连续对话时压低，退出时恢复。
    # 音乐一直响会被 VAD / ASR 当成你在说话（歌词被识别成问题，对话停不下来）。
    "music_duck": {
        "enabled": True,
        # Spotify：经 SSH 长连接调 Oracle 上的 deploy/music-duck/music_duck.py（Spotify Web API，
        # 不管在电脑还是手机上放都管用）。bridge 退出 / 断网时脚本自己恢复音量。
        "spotify": {
            "enabled": True,
            "ssh_host": "oracle",  # ~/.ssh/config 里的主机名（密钥登录）
            # 不填则优先用 Git 自带的 ssh（Windows OpenSSH 嫌 ~/.ssh/config 权限太宽会拒绝）
            "ssh_bin": None,
            "command": "python3 -u ~/home-tools/spotify/music_duck.py serve",
            "volume": 15,  # 压到这个音量（%）；本来就更小则不动
        },
        # 音箱自己的播放器（mediaplayer）：正在播就暂停，退出时只继续由我们暂停的
        "speaker": {"enabled": True},
    },
    # 多麦唤醒（core/services/audio/multimic_input.py）：音箱 3 个麦克风 + 回采另走一条 arecord | nc，
    # 每路回声消除后 IVA 分离成 3 路（和原生小爱一样的思路），和原来的 ch0 并行检测，任一路命中即唤醒。
    # VAD / ASR 仍走原来的单声道。环境变量 MULTIMIC_ENABLE=1/0 可覆盖 enabled。
    # 离线对比：scripts/multimic_record.py 录 4 声道 → scripts/multimic_eval.py
    "multimic": {
        "enabled": True,
        "port": 4401,  # 音箱 nc 连过来的端口
        # 额外跑唤醒的信号："iva"（分离出的 3 路）、"sum"（3 麦平均）、"ch1" / "ch2"（单个麦克风）
        "outputs": ["iva"],
        "gain": 256,  # S16（≈ S32 >> 16）乘这个倍数，对齐线上单声道的电平
        "iva_alpha": 0.97,  # IVA 遗忘因子（每帧 16 ms；越大越稳、跟得越慢）
        "echo_cancel": True,  # 每路麦克风先用 ch3 回采消回声（播报中打断需要）
        "stale_s": 5,  # 这么久没收到数据就重新在音箱上启动录音
    },
    "barge_in": {
        # 播报时喊唤醒词（如"你好缪斯"）可以打断，然后直接说新的问题（需要 echo_reference）。
        # 回采不在线（旧客户端 / 连接刚建立）时自动退回"关麦播报"。
        "enabled": True,
        # 让音箱同时发回正在播放的声音（[麦克风, 播放回采] 双声道），bridge 据此消除回声。
        # 需要 OwnDing 版客户端（client-2026.10.09+）；只在 OH2P 上验证过。
        # 环境变量 BARGE_IN_ECHO_REFERENCE=1/0 可临时覆盖。
        "echo_reference": True,
        # 双声道里麦克风声道电平偏低，消除回声后乘这个倍数回到单声道录音的电平（之后再乘 audio_input.gain）
        "echo_mic_gain": 64,
        # 打断后补录：从唤醒词结束前这么多毫秒开始，接上"你好缪斯，明天天气怎么样"里后半句（ms）。
        # OwnDing 的 350 是给单音节的"七"定的；"缪斯"会被带进请求（"司今天有什么安排"）。
        # kws_acceptance.py dialog：350 → 52/100，200 → 86，150 → 90，100 → 88（2026-10-19）
        "speech_lookback_ms": 150,
        # 刚播放过声音的那一小段（播放中、停播后 echo_vad_hangover_ms 内），消除后剩下的残留
        # 仍可能被当成说话；这段时间里说话判定用这个更高的阈值（真人说话约 1.0）。None = 不提高
        "echo_vad_threshold": 0.7,
        "echo_vad_hangover_ms": 300,
        # 打断时比较"消除回声后剩下的声音"和"消掉的回声"（dB）。设成数字（如 -17）后，
        # 低于它的唤醒当作音箱自己的声音忽略；None 只记日志。
        "near_end_min_db": None,
        # 回声消除器（core/services/audio/aec.py）
        "echo_canceller": {
            # "python"（aec.py）| "rust"（native/src/dsp/aec.rs，计算时不占 GIL）；多麦链路也用这个
            # （multimic.echo_canceller.impl 可单独覆盖）。改了要重启 bridge
            "impl": "python",
            "taps": 8,  # 回声尾巴长度（帧，每帧 16 ms）
            "tau_s": 3.0,  # 回声路径估计的平均时长（秒）
        },
        # OH2P 的 hw:0,3（共享名 Capture）：4 声道 48 kHz，ch0 麦克风，ch3 播放回采
        "capture": {
            "pcm": "Capture",
            "channels": 4,
            "sample_rate": 48000,
            "mic_channel": 0,
            "ref_channel": 3,
            "mic_shift": 14,
            "ref_shift": 16,
        },
    },
    "asr": {
        # 支持 "sense_voice"（默认）、"paraformer"、"fire_red_asr" 或 "doubao"
        "model": "sense_voice",
        # 是否优先使用 INT8 量化模型（仅本地模型生效）
        "int8": True,
        # 可选：显式指定 core/models/ 下的模型目录名（仅本地模型生效）
        # "model_dir": "",
        "doubao": {
            # "standard": 录音文件识别标准版，调用 /submit + /query
            # "flash": 录音文件极速版，调用 /recognize/flash
            "mode": "standard",
            "app_key": "你的 App Key",
            "access_key": "你的 Access Key",
            # 火山 X-Api-Resource-Id：
            # standard 可选：
            #   "volc.bigasr.auc"  - 豆包录音文件识别模型 1.0
            #   "volc.seedasr.auc" - 豆包录音文件识别模型 2.0
            # flash 可选：
            #   "volc.bigasr.auc_turbo" - 录音文件极速版
            "resource_id": "volc.seedasr.auc",
            "language": "",
            "submit_timeout": 10,
            "query_timeout": 10,
            "poll_interval": 0.5,
            "max_wait_seconds": 20,
        },
    },
    "xiaozhi": {
        "OTA_URL": "http://127.0.0.1:8003/xiaozhi/ota/",
        "WEBSOCKET_URL": "ws://127.0.0.1:8000/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "DEVICE_ID": "7c:70:db:88:15:d9", #（可选）默认自动生成
        "VERIFICATION_CODE": "", # 首次登陆时，验证码会在这里更新
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧"],
        "max_listening_retries": 2,  # 最多连续重新唤醒次数
        "exit_prompt": "再见，主人",
        "continuous_conversation_keywords": ["开启连续对话", "启动连续对话", "我想跟你聊天"]
    },
    # TTS (Text-to-Speech) Configuration
    "tts": {
        "doubao": {
            # 豆包语音合成 API 配置
            # 文档地址: https://www.volcengine.com/docs/6561/1598757?lang=zh
            # 产品地址: https://www.volcengine.com/docs/6561/1871062
            "app_id": "xxxx",         # 你的 App ID
            "access_key": "xxxxxx",       # 你的 Access Key
            "default_speaker": "zh_female_vv_uranus_bigtts",  # 音色 https://www.volcengine.com/docs/6561/1257544?lang=zh
            "audio_format": "pcm",  # 推荐默认值：局域网稳定环境下首音更快、播放更顺
            "stream": True,  # 推荐默认值：边合成边播放，首音延迟更低
        },
        # 本机 GPT-SoVITS 井芹仁菜 v2 微调模型（E:\ai\kato\start_tts_server.bat 启动，OpenAI 兼容接口）
        # 服务不可用时 TTSRouter 会自动回退小爱原生 TTS
        "openai": {
            "base_url": "http://127.0.0.1:9881/v1",
            "api_key": "",
            "model": "gpt-sovits-nina-v2",
            "voice": "nina",  # 见 E:\ai\kato\server\voices.json（nina / nina2 = 两条参考音频；其它角色备份在 voices.kato.json / voices.yamada.json）
            "response_format": "wav",
            "speed": 1.0,
            "timeout": 60,
            # 服务端用多相滤波直接输出 24k，避免 Bridge 线性插值重采样带来的混叠
            "extra_body": {"sample_rate": 24000},
        },
    },
    # OpenClaw Configuration
    "openclaw": {
        "url": "ws://127.0.0.1:18789",  # OpenClaw WebSocket 地址
        "token": "your_openclaw_token",  # OpenClaw 认证令牌
        "listening_light": {
            "enabled": True,  # 每轮本地监听时亮灯，结束或中断时熄灯
            "effect_id": 9,  # OH2P 固件 noangle 监听灯效，已在 OpenAI 对话里验证
        },
        # 输入模式：
        #   - "local_asr": 现有链路，使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 实验链路，唤醒小爱后接管原生 ASR 结果给 OpenClaw
        "input_mode": "local_asr",
        # session_key 格式：agent:<agentId>:<rest>
        #   agentId: OpenClaw 中配置的 Agent ID（默认为 main）
        #   rest:    会话标识，可自由命名，用于区分不同来源/场景 （默认为 open-xiaoai-bridge)
        # 也可在运行时动态切换（下一条消息即刻生效，无需重连）：
        #   app.set_backend_session_key("openclaw", "agent:assistant:open-xiaoai-bridge")
        "session_key": "agent:main:open-xiaoai-bridge",
        "identity_path": "/app/openclaw/identity/device.json",  # 设备身份文件路径；容器部署时建议挂载持久化目录
        "tts_speed": 1.0,  # TTS 语速 (0.5-2.0)，仅豆包 TTS 生效，小爱原生 TTS 不支持调速
        "tts_speaker": "xiaoai",  # "xiaoai" = 小爱原生 TTS；填豆包音色 ID 则用豆包 TTS；不设置则使用 tts.doubao.default_speaker
        # 可按 agentId 单独覆盖音色，优先级高于 tts_speaker
        # agentId 来自 session_key，格式为：agent:<agentId>:<rest>
        # 示例：
        # "agent_tts_speakers": {
        #     "assistant": "zh_female_vv_uranus_bigtts",
        #     "xiaomei": "zh_female_shuangkuaisisi_moon_bigtts",
        #     "butler": "xiaoai",
        # },
        "agent_tts_speakers": {},
        "response_timeout": 120,  # 等待 OpenClaw agent 响应的超时时间（秒）
        # 退出连续对话的词（规则见 core/exit_words.py）：只说退出词（可带"好的""谢谢"或唤醒词）马上退出；
        # "关灯，拜拜"先回答再退出；"停止播放音乐""用英语怎么说再见"照常发给后端。设成 [] 关闭
        "exit_keywords": ["拜拜", "再见", "退出", "退下", "停止", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        # rule_prompt: 用于「自动播放」和「连续对话」场景
        #   - send_to_openclaw_and_play_reply() 会自动追加
        #   - 连续对话（ExternalConversationController）会自动追加
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        # rule_prompt_for_skill: 用于「Agent 自主播报」场景（方式三）
        #   - send_to_openclaw() 会自动追加
        #   - 告诉 Agent 需要调用 xiaoai-tts skill 来播报，因为服务端不会自动播放
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字，调用 `xiaoai-tts` skill 播报出来。字数控制在300字以内"
    },
    # Hermes Agent（API Server 模式，OpenAI 兼容接口 + Hermes 的会话和工具事件，见 core/hermes.py）
    # 启用：HERMES_ENABLE=1；环境变量 HERMES_BASE_URL / HERMES_API_KEY / HERMES_MODEL 覆盖下面的值
    "hermes": {
        # Defaults; start script env HERMES_* overrides these (api_key via .hermes_api_key)
        "base_url": "https://hermes.oci.sounfury.top/v1",
        "api_key": "",
        "model": "muse-home",
        "listening_light": {
            # 和原生小爱一样：提示音 / 播报 3 → 等你说、你在说 41（短白灯，随你的音量变宽）→ 思考 2 → 播报 3，
            # 退出时熄灭（见 core/services/conversation_light.py）
            "enabled": True,
            "effect_id": 41,  # 等你说：固件 wakeup.sh 的 asrstart 灯效（原来的黄灯是 9）
            "hearing_effect_id": 41,  # 你在说：同一个灯，只是有了音量
            "thinking_effect_id": 2,  # 固件 wakeup.sh 的 think 灯效
            "speaking_effect_id": 3,  # 播报中（可打断）：固件 wakeup.sh 的 speek 灯效
        },
        "failure_text": "我听到了，但缪斯服务暂时没有返回回答，请稍后再试",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 统一采用 agent:<agentId>:<rest> 格式，便于 after_wakeup 解析。
        # 它也是 Hermes 长期记忆的作用域（下面的 session_header），改了就读不到以前的记忆
        "session_key": "agent:default:open-xiaoai-bridge",
        # 把 session_key 作为请求头发给 Hermes，用于长期记忆作用域（跨唤醒不变）；留空不发
        "session_header": "X-Hermes-Session-Key",
        # 一次唤醒一个服务端会话：进入连续对话时生成新 ID，每轮请求都带这个头，只发当前一句，
        # Hermes 从 state.db 读回本次唤醒的历史（含工具调用结果）；退出对话后丢掉，下次唤醒换新的。
        # bridge 本地不保存历史。Hermes 要求配置 api_key 才接受这个头。
        "session_id_header": "X-Hermes-Session-Id",
        "system_prompt": "",
        "temperature": 0.7,
        "max_tokens": 512,
        "response_timeout": 120,
        # 连续对话用流式回复：第一句出来就播（切段见 conversation.reply_segments）；False = 等整段回复再播
        "stream": True,
        # 后端开始调用工具、还没有正文时先念一句（每轮最多一次，需要 Hermes 的 hermes.tool.progress 事件）
        "tool_filler": {
            "enabled": True,
            "text": "稍等",
            # 工具名包含键时用对应的话；设成 "" 表示这类工具不插话
            "tool_texts": {"web_search": "我查一下", "web_extract": "我查一下"},
            # 这些工具不算（tool_describe 只是读工具说明，每次调用工具前都有）
            "ignore_tools": ["tool_describe"],
        },
        # 回复里调用了这些工具（放 / 切 / 暂停音乐）就在回答完结束对话，不再接着听：对话中途才放的歌没被压低，
        # 接着听会把歌声当成你在说话，对话结束不了（OwnDing 和原生小爱放歌后也是直接结束）。[] = 关闭
        "end_after_tools": ["spotify_playback"],
        "tts_provider": "xiaoai",  # 小爱原生；改成 "openai" 用 tts.openai（本机 GPT-SoVITS 井芹仁菜音色，占约 3G 显存，需先启动服务）
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        "session_tts_speakers": {},
        "exit_keywords": ["拜拜", "再见", "退出", "退下", "停止", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字。字数控制在300字以内",
        "extra_body": {},
    },
    # 通用 OpenAI 兼容服务（OpenAI、Ollama、LM Studio 等 /v1/chat/completions）：before_wakeup 返回 "openai" 时使用
    # 启用：OPENAI_ENABLE=1；环境变量 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL 覆盖下面的值。
    # 每轮只发当前一句（加 system_prompt），没有多轮上下文。连续对话的其他项（listening_light、input_mode、
    # tts_provider、exit_keywords 等）和上面的 "hermes" 含义相同，需要时照着加
    "openai": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "",
        "model": "qwen3:8b",
        "listening_light": {"enabled": True},
        "input_mode": "local_asr",
        "system_prompt": "",
        "stream": True,
        "tts_provider": "xiaoai",
        "tts_speaker": "xiaoai",
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
    },
    # QwenPaw Configuration
    # 需先启动 QwenPaw: qwenpaw app
    "qwenpaw": {
        "base_url": "http://127.0.0.1:8088",
        # agent_id 仅作为回退：当 session_key 不含 agentId 时才使用。
        # 正常情况下 agentId 直接从 session_key 的第二段解析，无需单独配置。
        "agent_id": "default",
        "user_id": "open-xiaoai-bridge",
        "listening_light": {
            "enabled": True,  # 每轮本地监听时亮灯，结束或中断时熄灯
            "effect_id": 9,  # OH2P 固件 noangle 监听灯效，已在 OpenAI 对话里验证
        },
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 采用 agent:<agentId>:<sessionId> 格式：
        #   - 第二段 agentId 作为 X-Agent-Id 请求头发给服务端
        #   - 第三段起为 sessionId，作为请求体 session_id 发给服务端
        # 例如 agent:default:open-xiaoai-bridge -> agent=default, session=open-xiaoai-bridge
        "session_key": "agent:default:open-xiaoai-bridge",
        # QwenPaw 当前推荐使用后台任务接口：
        # POST /api/console/chat/task -> GET /api/console/chat/task/{task_id}
        "send_path": "/api/console/chat/task",
        "task_status_path": "/api/console/chat/task/{task_id}",
        # 认证（可选）：非空时默认使用 Authorization 认证头
        "auth_token": "",
        "response_timeout": 120,
        "poll_interval": 0.5,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        "session_tts_speakers": {},
        "exit_keywords": ["拜拜", "再见", "退出", "退下", "停止", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送给 QwenPaw 的，他看不到你回复的文字。字数控制在300字以内",
    },
    # 小爱管家（"你好缪斯"）唤醒词，需设置 MUSE_ENABLE=1
    # 命中后由 before_wakeup 路由到 Hermes 连续对话，对话参数见上面的 "hermes"
    "muse": {
        # 规范唤醒词：始终参与匹配
        "wake_word": "你好缪斯",
        # ASR 文本前缀匹配的完整唤醒词集合（环境变量 MUSE_WAKE_WORDS 优先，用 , ， 、 分隔）
        # 匹配规则：
        #   - 忽略大小写、全半角、空格和中英文标点："hi muse" 也匹配 "Hi Muse" / "himuse" / "Hi，Muse"
        #   - 只在句首匹配；句首的标点和 leading_fillers 里的语气词可以跳过
        #   - 最长优先（"你好缪斯" 先于 "缪斯"）；英文唤醒词要求词边界（"museum" 不算 "muse"）
        # 单独的"缪斯" / "muse"只在句首生效；妙思 / 木斯 / 慕斯 本身是常见词，只接受带问候语的写法
        "wake_words": [
            # 正写 + 英文
            "你好缪斯", "嗨缪斯", "哈喽缪斯", "嘿缪斯", "hi缪斯", "hey缪斯", "hello缪斯",
            "hi muse", "hey muse", "hello muse", "你好muse", "嗨muse", "哈喽muse", "嘿muse",
            "缪斯", "muse",
            # 同音字（miù sī），也可以单独使用
            "你好缪思", "嗨缪思", "哈喽缪思", "嘿缪思", "缪思",
            "你好缪丝", "嗨缪丝", "哈喽缪丝", "嘿缪丝", "缪丝",
            "你好谬斯", "嗨谬斯", "哈喽谬斯", "嘿谬斯", "谬斯",
            "你好谬思", "嗨谬思", "哈喽谬思", "嘿谬思", "谬思",
            # 近音字，只接受带问候语的写法
            "你好妙思", "嗨妙思", "哈喽妙思", "嘿妙思",
            "你好妙斯", "嗨妙斯", "哈喽妙斯", "嘿妙斯",
            "你好木斯", "嗨木斯", "哈喽木斯", "嘿木斯",
            "你好慕斯", "嗨慕斯", "哈喽慕斯", "嘿慕斯",
        ],
        # 合并进本地 KWS（常驻监听）的子集（环境变量 MUSE_KWS_WAKE_WORDS 优先；[] = 不额外加入）
        #   - 只放"问候语 + 名字"：单独的"缪斯" / "muse" 只有 2 个音节，常驻监听误唤醒风险高，只走 ASR 前缀
        #   - 同音/近音字只走 ASR：KWS 按汉字建模，每多一种写法就多一条常驻误唤醒路径
        #   - 这些词同时也会参与 ASR 前缀匹配；编码失败（模型词表没有的字）的会被跳过并告警
        "kws_wake_words": ["你好缪斯", "嗨缪斯", "哈喽缪斯", "嘿缪斯", "hi muse", "hey muse", "hello muse"],
        "wake_word_aliases": [],  # 追加到 wake_words 的额外写法（兼容旧配置）
        # 唤醒词前可以跳过的语气词（只在句首，可重复）；设为 [] 关闭
        # 不含"那个"：它也是指示代词（"那个缪斯是谁"），去掉会让单独的"缪斯"更容易误触发
        "leading_fillers": ["嗯", "呃", "额", "啊", "喂", "诶", "欸", "哎", "um", "uh", "hmm", "emm"],
        # "prefix"（默认）：唤醒词必须在句首
        # "contains"：任意位置包含唤醒词即可——会让单独的"缪斯"/"muse"严重误触发，不建议开启
        "match_mode": "prefix",
        # 小爱原生识别出缪斯文本时如何拦截原生回复：
        #   "abort"  — speaker.abort_xiaoai()（重启小爱服务，约 1-2 秒）
        #   "dialog" — 复用 bridge 的 dialog 拦截（不重启，但原生回复晚到时可能打断开场提示）
        #   "none"   — 不拦截
        "native_suppress": "abort",
    },
}
