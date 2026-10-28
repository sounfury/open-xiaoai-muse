import asyncio
import json
import os
import threading
import time
import wave

import numpy as np
import open_xiaoai_server

from core.ref import get_app, get_speaker, set_xiaoai
from core.services.audio.stream import GlobalStream
from core.services.diagnostics import diagnostics
from core.services.speaker import SpeakerManager
from core.wakeup_session import EventManager
from core.xiaoai_conversation import XiaoAIConversationController
from core.utils.base import json_decode
from core.utils.config import ConfigManager
from core.utils.logger import logger

ASCII_BANNER = """
▄▖      ▖▖▘    ▄▖▄▖
▌▌▛▌█▌▛▌▚▘▌▀▌▛▌▌▌▐ 
▙▌▙▌▙▖▌▌▌▌▌█▌▙▌▛▌▟▖
  ▌                
                                                                                                                
"""

# 音箱录音带播放回采（OwnDing 客户端的 echo_ref）：一个麦克风 + 回采声道。
# OH2P 的 hw:0,3（共享名 Capture）4 声道，第 4 声道是播放回采。
# 采样率必须是线上 16 kHz 的 3 倍；shift 是 S32 → S16 的右移位数。
DEFAULT_ECHO_CAPTURE = {
    "pcm": "Capture",
    "channels": 4,
    "sample_rate": 48000,
    "mic_channel": 0,
    "ref_channel": 3,
    "mic_shift": 14,
    "ref_shift": 16,
}
# 超过这么久没有双声道录音，视为音箱已不发送回采（不能打断）
ECHO_REFERENCE_STALE_S = 2.0
# 录音看门狗：每隔多久检查一次；连接还在但超过多久没有音频就重新请求录音
RECORDING_WATCHDOG_INTERVAL_S = 5.0
RECORDING_STALL_S = 4.0


class XiaoAI:
    speaker = SpeakerManager()
    async_loop: asyncio.AbstractEventLoop = None
    config_manager = ConfigManager.instance()
    conversation = XiaoAIConversationController()
    _input_gain_enabled = False
    _input_gain = 1.0
    # 高通滤波（audio_input.highpass_hz），在增益之前；None = 不滤波
    _highpass = None
    _highpass_hz = None
    # 双声道录音里麦克风声道的电平比单声道录音低，乘这个倍数回到原来的电平
    _echo_mic_gain = 64.0
    _echo_dump: dict | None = None
    _input_dump: dict | None = None  # AUDIO_INPUT_DUMP：KWS / VAD 实际收到的单声道音频
    # 回声消除器（收到第一帧双声道录音时创建），见 core/services/audio/aec.py
    echo = None
    _echo_seen: float | None = None  # 最近一次收到双声道录音（monotonic）
    _echo_until = 0.0  # 这个时间之前的输入可能还带着（消除后的）播放残留
    _echo_until_sample = 0  # 同上，按 GlobalStream 绝对采样位置（回放历史时用）
    # 录音看门狗
    _last_input_at: float | None = None
    _recording_wanted = True
    _watchdog_thread: threading.Thread | None = None
    _async_loop_ready = threading.Event()
    _external_wakeup_keywords: set[str] = set()
    _muse_wakeup_keywords: set[str] = set()
    _suppressed_dialog_ids: set[str] = set()
    _suppressed_dialog_last_attempt: dict[str, float] = {}
    # dialog_id of the latest final command passed to before_wakeup (used by core/muse.py)
    last_recognize_dialog_id: str = ""
    _MAX_SUPPRESSED_DIALOGS = 100
    _SUPPRESS_RETRY_INTERVAL = 0.35

    @classmethod
    def refresh_runtime_config(cls, *_args):
        """从配置中心同步运行时参数。"""
        cls.conversation.apply_runtime_config(
            cls.config_manager.get_app_config("xiaoai", {})
        )
        gain = cls.config_manager.get_app_config("audio_input.gain", 1.0)
        try:
            gain = float(gain)
        except (TypeError, ValueError):
            gain = 1.0
        cls._input_gain = max(1.0, min(gain, 8.0))
        cls._input_gain_enabled = cls._input_gain > 1.0
        highpass_hz = cls.config_manager.get_app_config("audio_input.highpass_hz", None)
        if highpass_hz != cls._highpass_hz:
            from core.services.audio.highpass import make_highpass

            cls._highpass = make_highpass(highpass_hz)
            cls._highpass_hz = highpass_hz
            if cls._highpass is not None:
                logger.info(f"[XiaoAI] 输入高通滤波 {cls._highpass.cutoff_hz:g} Hz")
        wakeup_keywords = cls.config_manager.get_app_config("wakeup.keywords", [])
        cls._external_wakeup_keywords = {
            cls._normalize_text(keyword)
            for keyword in wakeup_keywords
            if isinstance(keyword, str) and cls._normalize_text(keyword)
        }
        # 小爱管家的 KWS 唤醒词在生成 keywords.txt 时才合并（MUSE_ENABLE=1），这里同步一份，
        # 用缪斯自己的归一化（忽略全半角 / 大小写 / 标点）比较，例如原生 ASR 给出 "Hey, Muse."
        from core.muse import kws_wake_words, normalize_wake_text

        cls._muse_wakeup_keywords = {
            normalize_wake_text(keyword)
            for keyword in kws_wake_words(cls.config_manager.get_app_config)
            if normalize_wake_text(keyword)
        }

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            return ""
        return "".join(text.strip().lower().split())

    @classmethod
    def _is_external_wakeup_text(cls, text: str) -> bool:
        normalized = cls._normalize_text(text)
        if not normalized:
            return False
        if normalized in cls._external_wakeup_keywords:
            return True
        if cls._muse_wakeup_keywords:
            from core.muse import normalize_wake_text

            return normalize_wake_text(text) in cls._muse_wakeup_keywords
        return False

    @classmethod
    async def _suppress_dialog(cls, dialog_id: str, reason: str):
        if not dialog_id:
            return

        # Prevent unbounded growth: if too many stale dialog_ids accumulated
        # (missed Dialog.Finish events), clear them all before adding new one
        if len(cls._suppressed_dialog_ids) >= cls._MAX_SUPPRESSED_DIALOGS:
            logger.debug(
                f"[XiaoAI] Clearing {len(cls._suppressed_dialog_ids)} stale suppressed dialog_ids"
            )
            cls._suppressed_dialog_ids.clear()
            cls._suppressed_dialog_last_attempt.clear()

        is_new_dialog = dialog_id not in cls._suppressed_dialog_ids
        cls._suppressed_dialog_ids.add(dialog_id)

        if is_new_dialog:
            logger.info(
                f"[XiaoAI] 🛑 停止小爱当前对话: {reason}"
            )

        now = time.monotonic()
        last_attempt = cls._suppressed_dialog_last_attempt.get(dialog_id, 0.0)
        if not is_new_dialog and (now - last_attempt) < cls._SUPPRESS_RETRY_INTERVAL:
            return

        cls._suppressed_dialog_last_attempt[dialog_id] = now
        try:
            await cls.speaker.run_shell(
                "killall tts_play.sh miplayer 2>/dev/null; mphelper pause"
            )
            if is_new_dialog:
                await cls.speaker.wake_up(awake=False)
        except Exception as exc:
            logger.debug(
                f"[XiaoAI] Failed to pause suppressed dialog {dialog_id}: {exc}"
            )

    @classmethod
    def on_input_packet(cls, packet):
        """新客户端的录音帧：(PCM bytes, metadata JSON)。

        双声道 [麦克风, 播放回采]（layout=mic_ref）时先消除回声，再按单声道送进 GlobalStream。
        """
        data, metadata = packet
        try:
            details = json.loads(metadata) if metadata else {}
            layout = details.get("layout")
        except (TypeError, ValueError, AttributeError):
            details, layout = {}, None
        diagnostics.input_packet(details)
        channels = 2 if layout == "mic_ref" else 1
        with diagnostics.measure("input.packet", audio_s=len(data) / (32000 * channels), warn_s=0.2):
            if layout == "mic_ref":
                frames = np.frombuffer(data, dtype="<i2").reshape(-1, 2)
                if cls._echo_dump is not None:
                    cls._append_echo_dump(frames)
                with diagnostics.measure("input.aec", audio_s=len(frames) / 16000):
                    data = cls._cancel_echo(frames)
            cls.on_input_data(data)

    @classmethod
    def _cancel_echo(cls, frames) -> bytes:
        """[麦克风, 回采] → 消除回声后的单声道 S16，电平与单声道录音一致。"""
        from core.services.audio.aec import create_echo_canceller, echo_canceller_impl

        barge_in = cls.config_manager.get_app_config("barge_in", {}) or {}
        if cls.echo is None:
            settings = barge_in.get("echo_canceller") or {}
            cls.echo = create_echo_canceller(
                echo_canceller_impl(barge_in),
                taps=int(settings.get("taps", 8)),
                tau_s=float(settings.get("tau_s", 3.0)),
            )
            logger.info(f"[XiaoAI] 回声消除：{type(cls.echo).__name__}（{type(cls.echo).__module__}）")
        now = time.monotonic()
        cls._echo_seen = now
        cleaned = cls.echo.process(frames[:, 0], frames[:, 1]) * cls._echo_mic_gain
        if np.any(frames[:, 1]):
            # 这一包的回声在消除器延迟之后才输出，房间里还会再响一小会
            hangover_s = float(barge_in.get("echo_vad_hangover_ms", 300)) / 1000
            delay_s = (len(frames) + cls.echo.delay) / 16000
            cls._echo_until = now + delay_s + hangover_s
            # 这一包从 GlobalStream.samples 开始写入
            cls._echo_until_sample = GlobalStream.samples + int((delay_s + hangover_s) * 16000)
        return np.clip(np.round(cleaned), -32768, 32767).astype(np.int16).tobytes()

    @classmethod
    def echo_reference_live(cls) -> bool:
        """音箱正在发送回采（最近 2 秒内收到过双声道录音）。"""
        seen = cls._echo_seen
        return seen is not None and time.monotonic() - seen < ECHO_REFERENCE_STALE_S

    @classmethod
    def echo_active(cls) -> bool:
        """当前输入可能还带着播放残留（正在播放或刚停）。"""
        return cls.echo_reference_live() and time.monotonic() < cls._echo_until

    @classmethod
    def echo_active_at(cls, sample: int) -> bool:
        """GlobalStream 里绝对位置 sample 处的输入可能带着播放残留。"""
        return cls.echo_reference_live() and sample < cls._echo_until_sample

    @classmethod
    def near_end_ratio_db(cls) -> float | None:
        """最近 1.5 秒里，消除回声后最响的一段比被消掉的回声高多少 dB（判断是不是真人在喊）。"""
        echo = cls.echo
        return echo.near_end_ratio_db() if echo is not None else None

    # ---- 录音看门狗 ----

    @classmethod
    def set_recording_wanted(cls, wanted: bool):
        """会话层主动关麦时置 False，看门狗不会把麦克风重新打开。"""
        cls._recording_wanted = bool(wanted)
        if wanted:
            cls._last_input_at = time.monotonic()

    @classmethod
    def _start_recording_watchdog(cls):
        if cls._watchdog_thread is not None:
            return
        cls._watchdog_thread = threading.Thread(
            target=cls._recording_watchdog, daemon=True, name="recording-watchdog"
        )
        cls._watchdog_thread.start()

    @classmethod
    def _recording_watchdog(cls):
        """录音断流（连接还在但几秒没有音频）时重新请求录音。"""
        stalled_logged = False
        while True:
            time.sleep(RECORDING_WATCHDOG_INTERVAL_S)
            last = cls._last_input_at
            if last is None or not cls._recording_wanted:
                stalled_logged = False
                continue
            idle = time.monotonic() - last
            if idle < RECORDING_STALL_S:
                stalled_logged = False
                continue
            app = get_app()
            loop = getattr(app, "loop", None)
            if loop is None or not loop.is_running():
                continue
            log = logger.debug if stalled_logged else logger.warning
            log(f"[XiaoAI] {idle:.0f}s 没有收到录音，重新请求录音")
            stalled_logged = True
            diagnostics.event("input.watchdog_restart", idle_ms=round(idle * 1000), wanted=cls._recording_wanted)
            asyncio.run_coroutine_threadsafe(cls._restart_recording(), loop)

    @staticmethod
    async def _restart_recording():
        try:
            await open_xiaoai_server.start_recording()
        except Exception as exc:
            logger.debug(f"[XiaoAI] 看门狗重新请求录音失败: {exc}")

    @classmethod
    def _setup_echo_reference(cls):
        """barge_in.echo_reference 打开时，请音箱同时发回播放回采。"""
        barge_in = cls.config_manager.get_app_config("barge_in", {}) or {}
        enabled = bool(barge_in.get("echo_reference", False))
        override = os.environ.get("BARGE_IN_ECHO_REFERENCE", "").strip().lower()
        if override:
            enabled = override in ("1", "true", "yes", "on")
        if not enabled:
            return
        if not hasattr(open_xiaoai_server, "set_echo_ref"):
            logger.warning("[XiaoAI] 原生扩展不支持 echo_ref，请重新编译 native")
            return
        capture = {**DEFAULT_ECHO_CAPTURE, **(barge_in.get("capture") or {})}
        try:
            cls._echo_mic_gain = float(barge_in.get("echo_mic_gain", 64))
        except (TypeError, ValueError):
            cls._echo_mic_gain = 64.0
        open_xiaoai_server.set_echo_ref(json.dumps(capture))
        logger.info(f"[XiaoAI] 🔁 请音箱同时发送播放回采: {capture}")

        # 调试：ECHO_REF_DUMP=<文件.wav> 把开头若干秒的原始双声道录音存下来，用于确认回采声道
        dump_path = os.environ.get("ECHO_REF_DUMP", "").strip()
        if dump_path:
            try:
                seconds = float(os.environ.get("ECHO_REF_DUMP_SECONDS", "30"))
            except ValueError:
                seconds = 30.0
            cls._echo_dump = {
                "path": dump_path,
                "max_frames": int(seconds * 16000),
                "chunks": [],
                "frames": 0,
            }
            logger.info(f"[XiaoAI] 将保存 {seconds:.0f}s 原始双声道录音到 {dump_path}")

    @classmethod
    def _append_echo_dump(cls, frames):
        dump = cls._echo_dump
        dump["chunks"].append(frames.copy())
        dump["frames"] += len(frames)
        if dump["frames"] < dump["max_frames"]:
            return
        cls._echo_dump = None
        audio = np.concatenate(dump["chunks"])[: dump["max_frames"]]

        def _write():
            try:
                with wave.open(dump["path"], "wb") as f:
                    f.setnchannels(2)
                    f.setsampwidth(2)
                    f.setframerate(16000)
                    f.writeframes(audio.astype("<i2").tobytes())
                logger.info(f"[XiaoAI] 原始双声道录音已保存: {dump['path']}")
            except Exception as exc:
                logger.error(f"[XiaoAI] 保存双声道录音失败: {exc}")

        threading.Thread(target=_write, daemon=True).start()

    @classmethod
    def on_input_data(cls, data: bytes):
        cls._last_input_at = time.monotonic()
        audio_array = np.frombuffer(data, dtype=np.int16)
        highpass = cls._highpass
        if highpass is not None and audio_array.size > 0:
            filtered = highpass.process(audio_array) * (cls._input_gain if cls._input_gain_enabled else 1.0)
            audio_array = np.clip(np.round(filtered), -32768, 32767).astype(np.int16)
        elif cls._input_gain_enabled and audio_array.size > 0:
            boosted = audio_array.astype(np.float32) * cls._input_gain
            audio_array = np.clip(boosted, -32768, 32767).astype(np.int16)
        pcm = audio_array.tobytes()
        if cls._input_dump is not None:
            cls._append_input_dump(pcm)
        diagnostics.count("input.delivered_samples", len(pcm) // 2)
        with diagnostics.measure("input.distribute", audio_s=len(pcm) / 32000, warn_s=0.2):
            GlobalStream.input(pcm)

    @classmethod
    def _setup_input_dump(cls):
        """调试 / 唤醒词验收：AUDIO_INPUT_DUMP=<文件.wav> 录下送进 KWS / VAD 的单声道音频
        （回声消除、增益之后），用 scripts/kws_acceptance.py wav 统计命中率和误唤醒。
        AUDIO_INPUT_DUMP_SECONDS 控制时长（默认 120 秒），录满后自动保存。"""
        path = os.environ.get("AUDIO_INPUT_DUMP", "").strip()
        if not path:
            return
        try:
            seconds = float(os.environ.get("AUDIO_INPUT_DUMP_SECONDS", "120"))
        except ValueError:
            seconds = 120.0
        cls._input_dump = {"path": path, "max_bytes": int(seconds * 16000) * 2, "data": bytearray()}
        logger.info(f"[XiaoAI] 🎙️ 将录下 {seconds:.0f}s 输入音频到 {path}")

    @classmethod
    def _append_input_dump(cls, pcm: bytes):
        dump = cls._input_dump
        dump["data"].extend(pcm)
        if len(dump["data"]) < dump["max_bytes"]:
            return
        cls._input_dump = None
        data = bytes(dump["data"][: dump["max_bytes"]])

        def _write():
            try:
                with wave.open(dump["path"], "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(16000)
                    f.writeframes(data)
                logger.info(f"[XiaoAI] 输入音频已保存: {dump['path']}")
            except Exception as exc:
                logger.error(f"[XiaoAI] 保存输入音频失败: {exc}")

        threading.Thread(target=_write, daemon=True).start()

    @classmethod
    def on_output_data(cls, data: bytes):
        async def on_output_data_async(data: bytes):
            return await open_xiaoai_server.on_output_data(data)

        asyncio.run_coroutine_threadsafe(
            on_output_data_async(data),
            cls.async_loop,
        )

    @classmethod
    async def run_shell(cls, script: str, timeout: float = 10 * 1000):
        return await open_xiaoai_server.run_shell(script, timeout)

    @classmethod
    async def on_event(cls, event: str):
        event_json = json_decode(event) or {}
        if not isinstance(event_json, dict):
            logger.debug("[XiaoAI] 忽略非字典事件负载")
            return

        event_data = event_json.get("data", {})
        event_type = event_json.get("event")

        if not event_json.get("event"):
            return

        # 记录所有事件用于调试监听退出
        logger.debug(f"[XiaoAI] 📡 收到事件: {event_type} | 数据: {event_data}")

        if event_type == "instruction":
            if not isinstance(event_data, dict):
                logger.debug(
                    f"[XiaoAI] 忽略非字典 instruction 数据: {event_data}"
                )
                return

            raw_line = event_data.get("NewLine")
            if not raw_line:
                return

            line = json_decode(raw_line) if isinstance(raw_line, str) else raw_line
            if not isinstance(line, dict):
                logger.debug(f"[XiaoAI] 忽略无法解析的指令行: {raw_line}")
                return

            header = line.get("header", {}) if isinstance(line.get("header"), dict) else {}
            dialog_id = header.get("dialog_id", "")
            namespace = header.get("namespace")
            header_name = header.get("name")

            if dialog_id and dialog_id in cls._suppressed_dialog_ids:
                if namespace in {"Nlp", "SpeechSynthesizer", "AudioPlayer"}:
                    await cls._suppress_dialog(
                        dialog_id,
                        f"{namespace}.{header_name}",
                    )
                    return
                if namespace == "Dialog" and header_name == "Finish":
                    cls._suppressed_dialog_ids.discard(dialog_id)
                    cls._suppressed_dialog_last_attempt.pop(dialog_id, None)
                    logger.debug(
                        f"[XiaoAI] Cleared suppressed dialog: {dialog_id}"
                    )
                    return

            if (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "SpeechRecognizer"
            ):
                if header_name == "RecognizeResult":
                    payload = line.get("payload", {})
                    if not isinstance(payload, dict):
                        logger.debug(
                            f"[XiaoAI] 忽略非字典 RecognizeResult payload: {payload}"
                        )
                        return

                    results = payload.get("results") or []
                    first_result = results[0] if results else {}
                    if isinstance(first_result, dict):
                        text = first_result.get("text") or ""
                    elif isinstance(first_result, str):
                        text = first_result
                    else:
                        text = ""
                    is_final = payload.get("is_final")
                    is_vad_begin = payload.get("is_vad_begin")

                    if EventManager.consume_xiaoai_asr_result(
                        dialog_id=dialog_id,
                        text=text,
                        is_final=is_final,
                        is_vad_begin=is_vad_begin,
                    ):
                        if dialog_id and text and is_final:
                            await cls._suppress_dialog(
                                dialog_id,
                                f"Bridge 接管原生 ASR",
                            )
                        return
                    
                    # 只有明确的 is_vad_begin=False 且没有文本时才触发唤醒
                    # 避免重复触发
                    if not text and is_vad_begin is False:
                        logger.wakeup("小爱同学", module="XiaoAI")
                        cls.conversation.reset_retries()
                        EventManager.on_interrupt()
                    elif text and is_final and cls._is_external_wakeup_text(text):
                        await cls._suppress_dialog(
                            dialog_id,
                            f"外部唤醒词接管: {text}",
                        )
                        await EventManager.wakeup(text, "kws")
                        return
                    elif text and is_final:
                        logger.info(f"[XiaoAI] 🔥 收到指令: {text}")
                        cls.last_recognize_dialog_id = dialog_id
                        cls.conversation.reset_retries()
                        await cls.conversation.handle_text_command(
                            text,
                            get_speaker(),
                        )
                        await EventManager.wakeup(text, "xiaoai")
                    elif is_final and not text:
                        logger.debug("[XiaoAI] 🛑 小爱监听超时自动退出")
                        await cls.conversation.handle_listening_timeout(
                            get_speaker()
                        )
            elif (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "AudioPlayer"
            ):
                cls.conversation.handle_audio_player_instruction(header_name)
        elif event_type == "playing":
            if not isinstance(event_data, str):
                logger.debug(
                    f"[XiaoAI] 忽略非字符串 playing 数据: {event_data}"
                )
                return

            playing_status = event_data.lower()
            
            get_speaker().status = playing_status
            await cls.conversation.handle_playing_status(
                playing_status,
                get_speaker(),
            )
        
        else:
            # 记录未处理的事件类型，可能包含监听退出信息
            logger.debug(f"[XiaoAI] ❓ 未处理的事件类型: {event_type} | 完整数据: {event_json}")

    @classmethod
    def __init_background_event_loop(cls):
        def run_event_loop():
            cls.async_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(cls.async_loop)
            cls._async_loop_ready.set()
            cls.async_loop.run_forever()

        thread = threading.Thread(target=run_event_loop, daemon=True)
        thread.start()
        cls._async_loop_ready.wait(timeout=2)

    @classmethod
    def __on_event(cls, event: str):
        future = asyncio.run_coroutine_threadsafe(
            cls.on_event(event),
            cls.async_loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(
                    f"[XiaoAI] Event handler failed: {type(exc).__name__}: {exc}"
                )

        future.add_done_callback(_log_result)

    @classmethod
    async def init_xiaoai(cls):
        cls.refresh_runtime_config()
        cls.config_manager.add_reload_listener(cls.refresh_runtime_config)
        set_xiaoai(XiaoAI)
        GlobalStream.on_output_data = cls.on_output_data
        cls._setup_echo_reference()
        cls._setup_input_dump()
        open_xiaoai_server.register_fn("on_input_data", cls.on_input_data)
        open_xiaoai_server.register_fn("on_input_packet", cls.on_input_packet)
        open_xiaoai_server.register_fn("on_event", cls.__on_event)
        cls.__init_background_event_loop()
        cls._start_recording_watchdog()
        logger.info("[XiaoAI] 启动小爱音箱服务...")
        print(ASCII_BANNER)
        await open_xiaoai_server.start_server()

    @classmethod
    def stop_conversation(cls):
        cls.conversation.stop()
