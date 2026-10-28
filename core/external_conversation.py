"""外部后端连续对话控制：协调录音、监听灯效、语音识别、请求与播报。

After a custom wake word triggers wakeup, this module drives either:
  - local VAD -> ASR -> backend -> TTS
  - XiaoAI native ASR -> backend -> TTS

The selected input path runs independently of the XiaoZhi session state
machine.

Key design decisions:
  - Uses per-session asyncio.Future objects so it never conflicts with the
    XiaoZhi wakeup session state machine.
  - Never calls abort_xiaoai() (which would break the FileMonitor).
  - TTS playback is awaited, so the next listening round only starts after
    the response has finished playing — unless the user barges in: with the
    speaker's echo reference live (barge_in.enabled), the microphone stays on
    while speaking, KWS keeps listening, and a wake word stops the reply and
    starts the next turn right away (speech after the wake word is kept).
  - With the echo reference live, every turn listens from a position in the
    GlobalStream history (replay): speech right after the wake word or right
    after the reply ends is not lost, even while the ready sound is playing.
  - Exit words follow core/exit_words.py: "拜拜" exits now, "关灯，拜拜" is
    answered first and then exits.
  - The backend is a ChatBackend (core/backends.py); this module only uses its
    public methods, so every backend shares the same conversation flow.
  - Backends with supports_streaming() (OpenAI / Hermes) stream the reply: it
    is cut into segments and the first one plays as soon as it arrives; a short
    filler ("稍等" / "我查一下") covers tool calls; a wake word stops it at any
    point, including while the backend is still thinking.
  - Lights are state-driven (core/services/conversation_light.py) and never
    block the voice flow.
"""

import asyncio
import os
from typing import TYPE_CHECKING

import open_xiaoai_server

from core.exit_words import AFTER_ANSWER, DEFAULT_EXIT_WORDS, NOW, match_exit_intent
from core.utterance import is_filler_only, is_wake_echo, looks_unfinished
from core.ref import get_kws, get_speaker, get_vad, get_xiaoai
from core.services.audio.stream import GlobalStream
from core.services.conversation_light import ConversationLight, effects_from_config
from core.services.tts.segment_player import (
    CallbackSegmentPlayer,
    NativeTTSSettings,
    PrefetchSegmentPlayer,
    SegmentPlayer,
    XiaoAINativeSegmentPlayer,
)
from core.services.tts.segmenter import ReplySegmenter, SegmenterSettings
from core.services.tts.wake_reply import WakeReplyPlayer, phrases_from_config, play_reply
from core.utils.config import ConfigManager
from core.utils.turn_timer import TurnTimer, lag_ms

if TYPE_CHECKING:
    from core.backends import ChatBackend

_NOTIFY_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "tts_notify.mp3",
)

_SEND_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "send_notify.mp3",
)

def _load_notify_sound() -> bytes | None:
    """Decode tts_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_NOTIFY_SOUND_PATH):
        return None
    try:
        with open(_NOTIFY_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

def _load_send_sound() -> bytes | None:
    """Decode send_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_SEND_SOUND_PATH):
        return None
    try:
        with open(_SEND_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

_NOTIFY_PCM = _load_notify_sound()
_SEND_PCM = _load_send_sound()
from core.utils.logger import logger


class ReplyStream:
    """后台读后端的流式回复（backend.stream_reply），事件放进队列。

    创建时就开始请求（和"咻"提示音并行）。队列里是 ("text", str) / ("tool", dict)，
    最后是 ("end", None) 或 ("error", Exception)。close() 会断开请求。
    """

    def __init__(self, events):
        self._events = events
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for item in self._events:
                await self.queue.put(item)
            await self.queue.put(("end", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.queue.put(("error", exc))
        finally:
            try:
                await self._events.aclose()
            except Exception:
                pass

    async def close(self):
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class _ToolFiller:
    """后端开始调用工具、还没有正文时，先念一句"稍等"（每轮最多一次）。

    实测 Hermes：真正的工具开始到正文出来至少 2.5 秒（查温度约 2.5 秒、查新闻约 11 秒），
    这句不会拖慢回答。tool_describe（读工具说明）不算。
    """

    def __init__(self, cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.text = str(cfg.get("text", "稍等") or "")
        tool_texts = cfg.get("tool_texts", {})
        self.tool_texts = dict(tool_texts) if isinstance(tool_texts, dict) else {}
        ignore = cfg.get("ignore_tools", ["tool_describe"])
        self.ignore = {str(t) for t in ignore} if isinstance(ignore, (list, tuple, set)) else set()
        self.done = False

    def on_event(self, event: dict, spoken: bool) -> str | None:
        if not self.enabled or self.done:
            return None
        if spoken:
            # 正文已经在播，中途再调工具不插话
            self.done = True
            return None
        if event.get("status", "running") != "running":
            return None
        tool = str(event.get("tool") or "")
        if not tool or tool in self.ignore:
            return None
        self.done = True
        for key, text in self.tool_texts.items():
            if key and key in tool:
                return str(text) if text else None
        return self.text or None


class ExternalConversationController:
    """Manages multi-turn conversation for an external backend."""

    LOCAL_ASR_INPUT = "local_asr"
    XIAOAI_ASR_INPUT = "xiaoai_asr"
    XIAOAI_ASR_TIMEOUT = "__timeout__"

    def __init__(self, backend: "type[ChatBackend]"):
        self.config = ConfigManager.instance()
        self.backend = backend
        # 配置段、日志名、after_wakeup 的 source 都取自后端
        self.CONFIG_PREFIX = backend.CONFIG_SECTION
        self.BACKEND_NAME = backend.LABEL
        self.LOG_MODULE = f"{backend.LABEL} Conv"
        self.WAKEUP_SOURCE = backend.NAME
        self.active = False

        # Per-session asyncio.Future used to receive VAD events
        self._vad_future: asyncio.Future | None = None
        # Per-session asyncio.Future used to receive XiaoAI native ASR results
        self._xiaoai_asr_future: asyncio.Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Playback token for the current TTS session
        self._playback_token: int | None = None
        self._light: ConversationLight | None = None
        # 下一轮从 GlobalStream 历史的哪里开始听：(preroll_from, replay_from)，见 VAD.resume
        self._next_listen: tuple[int | None, int | None] | None = None
        # 下一轮是接着唤醒词听的（识别结果开头可能带着唤醒词，要去掉）
        self._next_after_keyword = False
        self._background_tasks: set[asyncio.Task] = set()
        # 这一轮回复里后端调用过的工具（流式的 hermes.tool.progress）
        self._reply_tools: set[str] = set()
        # 这一轮因为放了音乐等原因，回答完就结束对话
        self._end_after_reply = False

    @property
    def exit_keywords(self) -> list[str]:
        words = self._cfg("exit_keywords", None)
        return list(DEFAULT_EXIT_WORDS) if words is None else list(words)

    @property
    def timeout(self) -> int:
        return int(self.config.get_app_config("wakeup.timeout", 20))

    @property
    def input_mode(self) -> str:
        mode = self._cfg("input_mode", self.LOCAL_ASR_INPUT)
        if not isinstance(mode, str):
            return self.LOCAL_ASR_INPUT
        normalized = mode.strip().lower()
        if normalized in {self.LOCAL_ASR_INPUT, self.XIAOAI_ASR_INPUT}:
            return normalized
        logger.warning(
            f"Unknown {self.CONFIG_PREFIX}.input_mode={mode!r}, fallback to {self.LOCAL_ASR_INPUT}",
            module=self.LOG_MODULE,
        )
        return self.LOCAL_ASR_INPUT

    def uses_xiaoai_asr(self) -> bool:
        return self.input_mode == self.XIAOAI_ASR_INPUT

    # ---- public API ----

    def is_active(self) -> bool:
        return self.active

    async def start(self, wake_position: int | None = None):
        """Enter conversation mode.

        wake_position: KWS 命中时唤醒词结束的 GlobalStream 位置；回采在线时第一轮从这里接着听，
        "你好缪斯，开灯"一口气说完，"开灯"不会丢。
        """
        if self.active:
            logger.warning(f"[{self.LOG_MODULE}] Already active, ignoring start()")
            return
        self.active = True
        self.backend.begin_conversation()
        self._loop = asyncio.get_running_loop()
        self._next_listen = None
        self._next_after_keyword = False
        self._wake_position = wake_position
        light_cfg = self._cfg("listening_light", {})
        level_cfg = self._conv_cfg("light_level", {}) or {}
        # 随音量起伏：用小爱原生识别时设备自己会做，只在本地识别且灯效开启时用
        feeder = None
        if not self.uses_xiaoai_asr() and isinstance(light_cfg, dict) and light_cfg.get("enabled", False):
            from core.services.light_level import MicLevelFeeder

            feeder = MicLevelFeeder.from_config(level_cfg)
        self._light = ConversationLight(
            get_speaker(),
            effects_from_config(light_cfg, native_listening=self.uses_xiaoai_asr()),
            self._loop,
            level_feeder=feeder,
            level_states=level_cfg.get("states", ("listening", "hearing")) if isinstance(level_cfg, dict) else (),
        )

        logger.info(f"🎙️ 进入 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)
        # 音乐一直响会被当成你在说话：对话期间压低，stop() 时恢复（不阻塞）
        self._duck_music(True)
        phrases = phrases_from_config(self._conv_cfg("wake_reply", {}))
        if phrases and self._native_tts():
            # 把应答语提前在音箱上生成好（已有的跳过）
            self._background(WakeReplyPlayer.instance().prepare(get_speaker(), phrases))
        TurnTimer.mark("进入对话")

        try:
            await self._conversation_loop()
        except Exception as exc:
            import traceback
            logger.error(
                f"Conversation loop error: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}",
                module=self.LOG_MODULE,
            )
        finally:
            self.stop()
            pending = list(self._background_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            light, self._light = self._light, None
            if light is not None:
                await light.close()
            # 会话结束：无论是否在关麦期间退出，都允许看门狗恢复录音
            self._set_recording_wanted(True)

    def stop(self):
        """Exit conversation mode and clean up."""
        if not self.active:
            return
        self.active = False
        self.backend.end_conversation()
        self._cancel_vad_future()
        self._cancel_xiaoai_asr_future()
        kws = get_kws()
        if kws and hasattr(kws, "disarm_barge_in"):
            kws.disarm_barge_in()
        self._light_state("off")
        self._duck_music(False)
        if self._playback_token is not None:
            open_xiaoai_server.stop_tts_playback(self._playback_token)
            self._playback_token = None
        if self.uses_xiaoai_asr():
            speaker = get_speaker()
            if speaker and self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        speaker.wake_up(awake=False),
                        self._loop,
                    )
                except Exception as exc:
                    logger.debug(
                        f"Failed to stop XiaoAI native listening: {exc}",
                        module=self.LOG_MODULE,
                    )
        logger.info(f"👋 退出 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

    def _duck_music(self, duck: bool):
        try:
            from core.services.music_duck import MusicDucker

            ducker = MusicDucker.instance()
            if ducker is None:
                return
            if duck:
                ducker.duck(self._loop)
            else:
                ducker.restore(self._loop)
        except Exception as exc:
            logger.debug(f"Music duck failed: {type(exc).__name__}: {exc}", module=self.LOG_MODULE)

    def consume_xiaoai_recognize_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Consume XiaoAI native ASR events while the backend is waiting."""
        if not (
            self.active
            and self.uses_xiaoai_asr()
            and self._xiaoai_asr_future
        ):
            return False

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not is_final:
            logger.debug(
                f"Ignoring partial XiaoAI ASR result: {normalized_text}",
                module=self.LOG_MODULE,
            )
            return True

        # Silent wakeup may emit an empty final marker before real speech starts.
        if not normalized_text and is_vad_begin is False:
            logger.debug("Ignoring XiaoAI wake marker for native ASR", module=self.LOG_MODULE)
            return True

        if normalized_text:
            logger.debug(f"XiaoAI native ASR recognized: {normalized_text}", module=self.LOG_MODULE)
            self._resolve_xiaoai_asr_future(normalized_text)
            return True

        logger.debug(
            "XiaoAI native ASR received empty final result",
            module=self.LOG_MODULE,
        )
        self._resolve_xiaoai_asr_future(self.XIAOAI_ASR_TIMEOUT)
        return True

    # ===== 私有方法 =====

    def _cfg(self, key: str, default=None):
        return self.config.get_app_config(f"{self.CONFIG_PREFIX}.{key}", default)

    def _light_state(self, state: str):
        """切换会话灯状态（立即返回，可在任意线程调用）。"""
        if self._light is not None:
            self._light.set(state)

    def _barge_cfg(self, key: str, default=None):
        return self.config.get_app_config(f"barge_in.{key}", default)

    def _conv_cfg(self, key: str, default=None):
        """所有外部后端共用的轮次衔接配置（APP_CONFIG["conversation"]）。"""
        return self.config.get_app_config(f"conversation.{key}", default)

    def _conv_int(self, key: str, default: int) -> int:
        try:
            return max(0, int(self._conv_cfg(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _ms_to_samples(ms) -> int:
        try:
            return max(0, int(float(ms) * 16))
        except (TypeError, ValueError):
            return 0

    def _listen_after_keyword(self, position: int):
        """唤醒词在 position 附近结束：从它前面 keyword_tail_ms 开始重新检测说话，
        再往前 speech_lookback_ms 的音频只作为开头补给识别。"""
        self._next_after_keyword = True
        tail = self._ms_to_samples(self._conv_cfg("keyword_tail_ms", 100))
        lookback = self._ms_to_samples(self._barge_cfg("speech_lookback_ms", 350))
        position = int(position)
        self._next_listen = (max(0, position - max(lookback, tail)), max(0, position - tail))

    def _wake_names(self) -> list[str]:
        """当前的唤醒词（退出词判断时忽略句首的唤醒词）。"""
        names = [k for k in (self.config.get_app_config("wakeup.keywords", []) or []) if isinstance(k, str)]
        try:
            from core.muse import load_settings

            names += load_settings().wake_words
        except Exception:
            pass
        return names

    def _exit_intent(self, text: str) -> tuple[str | None, str | None]:
        try:
            return match_exit_intent(text, self.exit_keywords, self._wake_names())
        except Exception as exc:
            logger.warning(f"Exit word check failed: {type(exc).__name__}: {exc}", module=self.LOG_MODULE)
            return None, None

    def _streaming(self) -> bool:
        """后端支持流式回复且已开启（OpenAI / Hermes: <配置段>.stream）。"""
        try:
            return bool(self.backend.supports_streaming())
        except Exception:
            return False

    def _failure_text(self) -> str:
        return self._cfg("failure_text", "我听到了，但 AI 服务暂时没有返回回答，请稍后再试")

    def _barge_in_ready(self) -> bool:
        """打断已开启，且音箱正在发送回采（回声能消掉）。"""
        if not self._barge_cfg("enabled", False):
            return False
        xiaoai = get_xiaoai()
        live = getattr(xiaoai, "echo_reference_live", None)
        kws = get_kws()
        return bool(live and live() and kws and hasattr(kws, "arm_barge_in"))

    @staticmethod
    def _set_recording_wanted(wanted: bool):
        """告诉录音看门狗现在是否应该有录音（主动关麦时不要重新打开）。"""
        xiaoai = get_xiaoai()
        if xiaoai and hasattr(xiaoai, "set_recording_wanted"):
            xiaoai.set_recording_wanted(wanted)

    def _background(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ---- conversation loop ----

    async def _conversation_loop(self):
        """Run VAD -> ASR -> backend -> TTS turns until exit."""

        # 没有回采时：关麦 → 提示音 → 开麦，避免录到 before_wakeup 的播报和提示音。
        # 有回采时麦克风一直开着，回声由回声消除去掉；第一轮从唤醒词后面接着听，
        # 提示音期间说的话也会回放给 VAD。
        # VAD.resume() resets all state (speech_frames, input_bytes),
        # so speech detection starts clean when listening begins.
        start_sound = bool(self._conv_cfg("start_sound", True))
        if self._barge_in_ready():
            if self._wake_position is not None and not self.uses_xiaoai_asr():
                self._listen_after_keyword(self._wake_position)
            if start_sound:
                self._light_state("speaking")  # 应答语 / 提示音和播报一样用播报灯，播完回到短白灯
                await self._play_start_sound()
        else:
            await self._stop_recording()
            if start_sound:
                self._light_state("speaking")
                await self._play_start_sound()
            await self._start_recording()
            await self._skip_mic_start()
            TurnTimer.mark("重新开麦")
        logger.debug("Ready to listen", module=self.LOG_MODULE)

        while self.active:
            if self.uses_xiaoai_asr():
                result = await self._run_one_turn_with_xiaoai_asr()
            else:
                result = await self._run_one_turn_with_local_asr()
            if result in ("exit", "timeout"):
                if self.uses_xiaoai_asr():
                    await self._stop_xiaoai_native_listening()
                await self._call_after_wakeup()
                break
            elif result == "exit_after_answer":
                # 回答里已经道别了，不再播 after_wakeup 的告别语
                if self.uses_xiaoai_asr():
                    await self._stop_xiaoai_native_listening()
                logger.info("👋 回答完了，退出对话", module=self.LOG_MODULE)
                break
            elif result in ("error", "interrupted"):
                # 被 stop() 打断时静默退出，不播告别语、不再操作小爱监听
                break
        TurnTimer.clear()

    async def _play_start_sound(self):
        """唤醒后的应答语（失败时退回提示音）。"""
        if await self._play_wake_reply("wake"):
            TurnTimer.mark("应答语播完")
        else:
            await self._play_notify()
            TurnTimer.mark("提示音播完")

    def _log_turn_timing(self):
        """发给后端前打一行这一轮的耗时。"""
        summary = TurnTimer.finish(f"发给 {self.BACKEND_NAME}", GlobalStream.samples)
        if summary:
            logger.info(f"⏱️ {summary}", module=self.LOG_MODULE)

    async def _run_one_turn_with_local_asr(self) -> str:
        """Execute a single conversation turn.

        Returns:
            "continue" - turn completed, loop to next
            "exit"     - user said an exit keyword
            "exit_after_answer" - answered a request that ended with a goodbye
            "timeout"  - no speech detected within timeout
            "interrupted" - stop() was called before listening started
            "error"    - unrecoverable error
        """
        vad = get_vad()
        if not vad:
            logger.error("VAD not available", module=self.LOG_MODULE)
            return "error"

        # 1. Start listening for speech (recording is already active)
        self._light_state("listening")
        if not self.active:
            return "interrupted"
        TurnTimer.ensure("开始听")
        listen, self._next_listen = self._next_listen, None
        after_keyword, self._next_after_keyword = self._next_after_keyword, False
        preroll_from, replay_from = listen or (None, None)
        speech_bytes = await self._wait_for_speech(
            vad, preroll_from=preroll_from, replay_from=replay_from
        )
        if speech_bytes is None:
            TurnTimer.clear()
            return "timeout"

        self._light_state("thinking")
        logger.debug(
            f"Got speech buffer: {len(speech_bytes)} bytes",
            module=self.LOG_MODULE,
        )

        # 2. ASR: convert speech to text
        from core.services.audio.asr import ASRService

        cached = getattr(self, "_last_asr", None)
        if cached and cached[0] == len(speech_bytes):
            text = cached[1]  # 断句时已经识别过同一段音频
        else:
            text = ASRService.asr(speech_bytes, sample_rate=16000)
            TurnTimer.mark("识别", f"{len(speech_bytes) / 32000:.1f}s 音频")
        self._last_asr = None
        if after_keyword:
            # 接着唤醒词听的，开头可能带着唤醒词或它的尾音（"你好缪斯，明天天气怎么样"），去掉
            text = self._strip_wake_word(text)
        if not text or not any(character.isalnum() for character in text):
            logger.debug("ASR empty, retrying", module=self.LOG_MODULE)
            return "continue"
        skip = None
        if is_wake_echo(text, self._wake_names()):
            skip = f"🔇 只有唤醒词，接着听：{text}"
        elif is_filler_only(text):
            skip = f"🤔 只有语气词，接着听：{text}"
        if skip:
            logger.info(skip, module=self.LOG_MODULE)
            # 识别期间可能已经开口了：下一轮从稍早一点回放，不丢开头
            self._next_listen = (None, max(0, GlobalStream.samples - self._ms_to_samples(500)))
            return "continue"

        # 3. Check exit words
        intent, word = self._exit_intent(text)
        if intent == NOW:
            logger.info(f"Exit word: {word} ({text})", module=self.LOG_MODULE)
            TurnTimer.clear()
            return "exit"
        if intent == AFTER_ANSWER:
            logger.info(f"Exit after answer: {word} ({text})", module=self.LOG_MODULE)

        # 4. Send to backend
        reply = await self._ask_backend(text, send_sound=True)

        exit_after = intent == AFTER_ANSWER
        interrupted = await self._play_reply_and_resume(reply, vad, exit_after=exit_after)
        if (exit_after or self._end_after_reply) and not interrupted:
            return "exit_after_answer"
        return "continue"

    async def _run_one_turn_with_xiaoai_asr(self) -> str:
        """Execute a single conversation turn using XiaoAI native ASR."""
        TurnTimer.ensure("小爱开始听")
        text = await self._wait_for_xiaoai_asr_text()
        if text is None:
            logger.debug("XiaoAI native ASR turn timed out", module=self.LOG_MODULE)
            TurnTimer.clear()
            return "timeout"
        TurnTimer.mark("小爱识别")

        intent, word = self._exit_intent(text)
        if intent == NOW:
            logger.info(f"Exit word: {word} ({text})", module=self.LOG_MODULE)
            TurnTimer.clear()
            return "exit"
        if intent == AFTER_ANSWER:
            logger.info(f"Exit after answer: {word} ({text})", module=self.LOG_MODULE)

        self._light_state("thinking")
        reply = await self._ask_backend(text, send_sound=False)

        exit_after = intent == AFTER_ANSWER
        interrupted = await self._play_reply_and_resume(reply, exit_after=exit_after)
        if (exit_after or self._end_after_reply) and not interrupted:
            return "exit_after_answer"
        return "continue"

    async def _ask_backend(self, text: str, send_sound: bool) -> "str | ReplyStream":
        """把这句话发给后端：流式时返回还在生成的 ReplyStream，否则等到完整回复。

        send_sound：发出请求后播"咻"（和请求并行，给用户即时反馈）。
        """
        full_text = self.backend.with_rule_prompt(text)
        self._log_turn_timing()
        if self._streaming():
            # 流式：请求在后台读，边收边播
            reply = ReplyStream(self.backend.stream_reply(full_text))
            if send_sound:
                try:
                    await self._play_send_sound()
                except BaseException:
                    await reply.close()
                    raise
            return reply
        # 先发出请求（不阻塞等回复），再播"咻"，然后等回复
        run_id = await self.backend.submit(full_text)
        if send_sound:
            await self._play_send_sound()
        reply = await self.backend.wait_reply(run_id) if run_id else None
        if reply is None:
            logger.warning(f"No response from {self.BACKEND_NAME}", module=self.LOG_MODULE)
            return self._failure_text()
        return str(reply)

    def _strip_wake_word(self, text: str) -> str:
        """去掉句首的唤醒词（或唤醒词的尾音）；不含时原样返回。

        - "你好缪斯，明天天气怎么样" / "缪斯开灯" → 按缪斯唤醒词规则去掉
        - "好缪斯，开灯" → 去掉唤醒词的后半截（两个字以上）
        - "斯，开灯" → 只剩一个字的尾音，后面紧跟标点时才去掉（"思考一下"不动）
        """
        if not text:
            return text
        try:
            from core.muse import load_settings, match_settings_text

            match = match_settings_text(text, load_settings())
        except Exception:
            match = None
        if match:
            return match.content
        from core.muse import strip_wake_word_tail

        return strip_wake_word_tail(text, self._wake_names())

    # ---- VAD integration ----

    async def _wait_for_speech(
        self, vad, preroll_from: int | None = None, replay_from: int | None = None
    ) -> bytes | None:
        """Use VAD to detect speech and collect complete utterance.

        Follows the same two-step pattern as the XiaoZhi wakeup session:
          1. resume("speech") → wait for on_speech (voice detected)
          2. resume("silence") → keep recording → wait for on_silence (user stopped)

        The audio stream is tapped between step 1 and 2 to capture the
        full utterance that the VAD does not provide by itself.

        preroll_from / replay_from: see VAD.resume (listen from a position in
        the GlobalStream history, so speech that already started is kept).

        Returns:
            PCM bytes of captured speech, or None on timeout.
        """
        from core.wakeup_session import EventManager

        self._vad_future = self._loop.create_future()
        recording_frames: list[bytes] = []
        is_recording = False
        loop = self._loop

        # 断句：普通停顿至少等 1.2 秒；像没说完时等到 2.6 秒。
        # 检查期间 VAD 继续跑；每读到一帧人声就使旧识别结果失效。
        end_ms = max(32, self._conv_int("end_silence_ms", 1200))
        extra_ms = max(0, self._conv_int("hesitation_silence_ms", 2600) - end_ms)
        endpoint = {
            "pos": None, "extended": False, "version": 0,
            "checking": False, "silence_after_check": False, "asr": None,
        }
        self._last_asr = None
        vad.silence_override_ms = end_ms
        capture_future = self._vad_future
        check_task: asyncio.Task | None = None

        original_on_speech = EventManager.on_speech
        original_on_silence = EventManager.on_silence

        def _listening():
            return self.active and self._vad_future is capture_future and not capture_future.done()

        def _finish(version=None):
            if not _listening() or (version is not None and version != endpoint["version"]):
                return
            vad.pause()
            audio = b"".join(recording_frames)
            cached = endpoint["asr"]
            if cached and cached[0] == endpoint["version"]:
                # 检查后只有静音增加时，复用识别；有新的人声则一定重新识别。
                self._last_asr = (len(audio), cached[1])
            capture_future.set_result(audio)

        async def _check_unfinished(audio, version):
            """识别候选句尾，同时继续收音；续话会撤销旧候选。"""
            while _listening():
                text = ""
                try:
                    from core.services.audio.asr import ASRService

                    text = await loop.run_in_executor(None, lambda: ASRService.asr(audio, sample_rate=16000)) or ""
                except Exception as exc:
                    logger.debug(f"Endpoint ASR failed: {exc}", module=self.LOG_MODULE)
                if not _listening():
                    return
                if version != endpoint["version"]:
                    logger.debug("断句检查期间检测到续话，撤销旧断句", module=self.LOG_MODULE)
                    if endpoint["silence_after_check"]:
                        # 续话也已经停顿了，串行检查最新片段，不并发跑多个 ASR。
                        audio = b"".join(recording_frames)
                        version = endpoint["version"]
                        endpoint["silence_after_check"] = False
                        continue
                    endpoint["checking"] = False
                    return
                endpoint["checking"] = False
                endpoint["asr"] = (version, text)
                unfinished = bool(text and (looks_unfinished(text) or is_filler_only(text)))
                TurnTimer.mark(
                    "断句识别",
                    f"{len(audio) / 32000:.1f}s 音频" + ("，像没说完" if unfinished else ""),
                )
                if unfinished and not endpoint["silence_after_check"]:
                    endpoint["extended"] = True
                    logger.info(f"⏳ 像没说完，再等一下：{text}", module=self.LOG_MODULE)
                    # 已在持续听额外静音，识别耗时也包含在等待内，不重置录音。
                    return
                _finish(version)
                return

        def _schedule_check(audio, version):
            nonlocal check_task
            if _listening():
                check_task = loop.create_task(_check_unfinished(audio, version))

        def _on_speech_hook(speech_buffer: bytes):
            """Voice detected — save initial buffer, start recording, wait for silence."""
            nonlocal is_recording
            recording_frames.append(speech_buffer)
            is_recording = True
            TurnTimer.mark("开口", f"VAD 落后 {lag_ms(vad.stream.unread_samples())}ms")
            self._light_state("hearing")
            logger.debug(f"VAD speech detected, buffer size: {len(speech_buffer)}", module=self.LOG_MODULE)
            vad.resume("silence")

        def _on_silence_hook():
            """停顿时立即接着收音，后台检查候选句尾。"""
            if not _listening():
                return
            logger.debug("VAD detected silence", module=self.LOG_MODULE)
            TurnTimer.mark("停顿", f"VAD 落后 {lag_ms(vad.stream.unread_samples())}ms")
            if not extra_ms or endpoint["extended"]:
                loop.call_soon_threadsafe(_finish)
                return
            checking = endpoint["checking"]
            if checking:
                endpoint["silence_after_check"] = True
            else:
                endpoint["checking"] = True
                endpoint["silence_after_check"] = False
                audio = b"".join(recording_frames)
                version = endpoint["version"]
            # pause() 会停掉 VAD 输入，必须从刚读到的位置补回检查期间的声音。
            vad.silence_override_ms = max(32, extra_ms)
            vad.resume("silence", replay_from=endpoint["pos"])
            if not checking:
                loop.call_soon_threadsafe(_schedule_check, audio, version)

        # Tap into VAD's audio stream to record frames while waiting for silence
        _orig_handle_speech = vad._handle_speech_frame
        _orig_handle_silence = vad._handle_silence_frame

        def _recording_speech_frame(frames):
            if is_recording:
                recording_frames.append(bytes(frames))
                endpoint["pos"] = vad.stream.position()
                TurnTimer.speech_end_pos = endpoint["pos"]  # 最后一帧人声：算"说完到发出"
                endpoint["version"] += 1
                endpoint["asr"] = None
                endpoint["silence_after_check"] = False
                endpoint["extended"] = False
                vad.silence_override_ms = end_ms
            _orig_handle_speech(frames)

        def _recording_silence_frame(frames):
            if is_recording:
                recording_frames.append(bytes(frames))
                # 在原始处理（可能 pause 清掉缓冲）之前记下读到哪里了，继续听时从这里接上
                endpoint["pos"] = vad.stream.position()
            _orig_handle_silence(frames)

        EventManager.on_speech = _on_speech_hook
        EventManager.on_silence = _on_silence_hook
        vad._handle_speech_frame = _recording_speech_frame
        vad._handle_silence_frame = _recording_silence_frame

        try:
            if replay_from is not None and replay_from > GlobalStream.samples:
                # 播完后的保护段还没过完：等它过完再开始
                await asyncio.sleep(min(1.0, (replay_from - GlobalStream.samples) / 16000))
            if preroll_from is None and replay_from is None:
                vad.resume("speech")
            else:
                vad.resume("speech", preroll_from=preroll_from, replay_from=replay_from)
            try:
                # timeout 只管"多久没开口"；已经在说了就接着等它说完（最多 max_utterance_s）
                return await asyncio.wait_for(asyncio.shield(self._vad_future), timeout=self.timeout)
            except asyncio.TimeoutError:
                if not is_recording:
                    raise
            logger.debug("Still speaking at timeout, keep listening", module=self.LOG_MODULE)
            return await asyncio.wait_for(
                self._vad_future, timeout=max(1, self._conv_int("max_utterance_s", 60))
            )

        except asyncio.TimeoutError:
            logger.debug("VAD timeout, no speech detected", module=self.LOG_MODULE)
            vad.pause()
            return None

        finally:
            # 检查期间也持续收音；退出本次捕获后再恢复共享 VAD 的回调。
            vad.pause()
            if check_task and not check_task.done():
                check_task.cancel()
                await asyncio.gather(check_task, return_exceptions=True)
            EventManager.on_speech = original_on_speech
            EventManager.on_silence = original_on_silence
            vad._handle_speech_frame = _orig_handle_speech
            vad._handle_silence_frame = _orig_handle_silence
            vad.silence_override_ms = None
            self._vad_future = None

    def _cancel_vad_future(self):
        """Cancel any pending VAD future."""
        if self._vad_future and not self._vad_future.done():
            self._loop.call_soon_threadsafe(self._vad_future.cancel)
        self._vad_future = None

    async def _wait_for_xiaoai_asr_text(self) -> str | None:
        """Wake XiaoAI and wait for a final native ASR result."""
        speaker = get_speaker()
        if not speaker:
            logger.error("Speaker not available", module=self.LOG_MODULE)
            return None

        deadline = self._loop.time() + self.timeout
        while self.active:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None

            self._xiaoai_asr_future = self._loop.create_future()
            try:
                wait_seconds = max(0.1, remaining)
                logger.debug(
                    f"Triggering XiaoAI native ASR, waiting up to {wait_seconds:.1f}s",
                    module=self.LOG_MODULE,
                )
                await speaker.wake_up(awake=True, silent=True)
                result = await asyncio.wait_for(
                    self._xiaoai_asr_future,
                    timeout=wait_seconds,
                )
                if result == self.XIAOAI_ASR_TIMEOUT:
                    logger.debug(
                        "XiaoAI native ASR ended without speech (native timeout), retrying",
                        module=self.LOG_MODULE,
                    )
                    continue
                return result
            except asyncio.TimeoutError:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None
            finally:
                self._xiaoai_asr_future = None

        return None

    def _resolve_xiaoai_asr_future(self, text: str):
        """Resolve the pending XiaoAI ASR future on the owner loop."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.set_result, text)

    def _cancel_xiaoai_asr_future(self):
        """Cancel any pending XiaoAI ASR future."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.cancel)
        self._xiaoai_asr_future = None

    async def _stop_xiaoai_native_listening(self):
        """Exit XiaoAI native listening before playing the goodbye prompt."""
        speaker = get_speaker()
        if not speaker:
            return
        try:
            await speaker.stop_device_audio()
            await speaker.wake_up(awake=False)
            await asyncio.sleep(0.15)
        except Exception as exc:
            logger.debug(
                f"Failed to stop XiaoAI native listening: {exc}",
                module=self.LOG_MODULE,
            )

    # ---- Recording control (physical mute/unmute via remote arecord) ----

    async def _stop_recording(self):
        """Kill the remote arecord process so the mic doesn't pick up TTS."""
        self._set_recording_wanted(False)
        try:
            await open_xiaoai_server.stop_recording()
            logger.debug("Recording stopped", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"stop_recording error: {exc}", module=self.LOG_MODULE)

    async def _start_recording(self):
        """Restart the remote arecord process to resume mic input."""
        self._set_recording_wanted(True)
        try:
            await open_xiaoai_server.start_recording()
            logger.debug("Recording started", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"start_recording error: {exc}", module=self.LOG_MODULE)

    async def _skip_mic_start(self):
        """没有回采、刚重新开麦时丢掉开头一小段（可能带着提示音 / 播报的余音）。"""
        try:
            skip_s = max(0.0, float(self._conv_cfg("mic_start_skip_ms", 150)) / 1000)
        except (TypeError, ValueError):
            skip_s = 0.0
        if skip_s:
            await asyncio.sleep(skip_s)

    # ---- TTS ----

    async def _play_reply_and_resume(
        self, reply: "str | ReplyStream", vad=None, exit_after: bool = False
    ) -> bool:
        """播报回答，结束后准备下一轮监听；被唤醒词打断返回 True。

        reply 是完整文本，或者还在生成的 ReplyStream（边收边分段播放）。
        回采在线时不关麦，播报中（流式时包括后端还在思考时）喊唤醒词可以打断：
        直接进入下一轮，不播提示音。
        播完后下一轮从"播完 + tts_end_guard_ms"开始回放，用户一接话就能听到。
        没有回采时关麦播报，播完（可选）放提示音再开麦，丢掉开麦后的一小段。
        exit_after：这是最后一轮（"关灯，拜拜"），播完不再准备下一轮。
        回复里调用了 end_after_tools 里的工具（放音乐）时也一样，并把 self._end_after_reply 置为 True。
        """
        barge_in = self._barge_in_ready()
        interrupted = False
        self._reply_tools = set()
        self._end_after_reply = False
        try:
            if not barge_in:
                await self._stop_recording()
            if not isinstance(reply, ReplyStream):
                # 流式时等第一段真正开播再切灯
                self._light_state("speaking")
            if barge_in:
                interrupted = await self._play_tts_interruptible(reply)
            else:
                await self._speak(reply)
        finally:
            if isinstance(reply, ReplyStream):
                await reply.close()
            if not interrupted and not exit_after and self._reply_ends_conversation():
                exit_after = self._end_after_reply = True
            if self.active and not interrupted and not exit_after:
                TurnTimer.clear()  # 下一轮从"开始听"重新计时
                next_sound = bool(self._conv_cfg("next_turn_sound", True))
                # 有提示音时保持播报灯到提示音播完，下一轮开始时再切短白灯
                self._light_state("speaking" if next_sound else "listening")
                if barge_in:
                    guard = self._ms_to_samples(self._conv_cfg("tts_end_guard_ms", 150))
                    self._next_listen = (None, GlobalStream.samples + guard)
                    if next_sound:
                        await self._play_notify()
                else:
                    if next_sound:
                        await self._play_notify()
                    await self._start_recording()
                    await self._skip_mic_start()
            elif not barge_in:
                # 最后一轮 / 已停止：麦克风交还给 KWS
                await self._start_recording()
        if not interrupted and self.active and not exit_after:
            logger.debug("Ready for next conversation round", module=self.LOG_MODULE)
        return interrupted

    async def _play_tts_interruptible(self, reply: "str | ReplyStream") -> bool:
        """边播报边检测唤醒词；被打断返回 True。"""
        kws = get_kws()
        hit = self._loop.create_future()

        def on_hit(keyword, position):  # KWS 线程
            self._loop.call_soon_threadsafe(self._on_barge_in_hit, hit, keyword, position)

        tts = asyncio.create_task(self._speak(reply))
        kws.arm_barge_in(on_hit)
        try:
            await asyncio.wait({tts, hit}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            tts.cancel()
            hit.cancel()
            # 等播放器 abort / HTTP 断流完成，才能把播放权交给下一个会话。
            await asyncio.gather(tts, return_exceptions=True)
            raise
        finally:
            kws.disarm_barge_in()
        if not hit.done() or hit.cancelled():
            hit.cancel()
            await tts
            return False

        keyword, position = hit.result()
        reply = self._wake_reply_wanted("barge_in")
        # 先切灯让用户马上看到打断生效，再停播报（要说"我在"时直接是播报灯）
        self._light_state("speaking" if reply else "listening")
        token = self._playback_token
        if token is not None:
            open_xiaoai_server.stop_tts_playback(token)
        tts.cancel()
        await asyncio.gather(tts, return_exceptions=True)
        TurnTimer.mark("停播报")
        speaker = get_speaker()
        self._listen_after_keyword(position)
        logger.info(f"✋ 打断播报: {keyword}", module=self.LOG_MODULE)
        if reply:
            # 先停干净再应答（stop_device_audio 会 killall miplayer，不能和应答语并发）
            if speaker:
                await speaker.stop_device_audio()
                TurnTimer.mark("停设备音频")
            await self._play_wake_reply("barge_in")
            TurnTimer.mark("应答语播完")
            self._light_state("listening")
        elif speaker:
            self._background(speaker.stop_device_audio())
        return True

    def _wake_reply_wanted(self, when: str) -> bool:
        cfg = self._conv_cfg("wake_reply", {})
        if not isinstance(cfg, dict) or not cfg.get(f"on_{when}", True):
            return False
        return bool(phrases_from_config(cfg))

    def _native_tts(self) -> bool:
        from core.services.tts.router import TTSRouter

        provider = TTSRouter.resolve_provider(
            self.backend.tts_provider(),
            self.backend.get_tts_speaker_for_session_key(),
        )
        return provider == "xiaoai" and get_speaker() is not None

    async def _play_wake_reply(self, when: str) -> bool:
        """唤醒 / 打断后随机说一句"我在"（conversation.wake_reply）。没配置或失败返回 False。"""
        if not self._wake_reply_wanted(when):
            return False
        phrases = phrases_from_config(self._conv_cfg("wake_reply", {}))
        text = await play_reply(get_speaker(), phrases, self._native_tts(), fallback=self._play_tts)
        if text:
            logger.info(f"🙋 {text}", module=self.LOG_MODULE)
        return bool(text)

    def _on_barge_in_hit(self, hit: asyncio.Future, keyword: str, position: int):
        """播报中 KWS 命中：确认像真人在喊（near_end_min_db）再打断。"""
        if hit.done():
            return
        xiaoai = get_xiaoai()
        ratio_fn = getattr(xiaoai, "near_end_ratio_db", None)
        ratio = ratio_fn() if ratio_fn else None
        min_db = self._barge_cfg("near_end_min_db", None)
        ratio_text = "-" if ratio is None else f"{ratio:.1f} dB"
        if min_db is not None and ratio is not None and ratio < float(min_db):
            logger.info(
                f"🙉 忽略疑似音箱自己的声音: {keyword}（近端 {ratio_text} < {min_db} dB）",
                module=self.LOG_MODULE,
            )
            TurnTimer.clear()  # KWS 命中时已经开始计时
            return
        logger.info(f"Barge-in: {keyword}, near-end {ratio_text}", module=self.LOG_MODULE)
        hit.set_result((keyword, position))

    async def _speak(self, reply: "str | ReplyStream"):
        if isinstance(reply, ReplyStream):
            await self._speak_stream(reply)
        else:
            await self._play_tts(reply)

    def _new_segment_player(self, on_start=None) -> SegmentPlayer:
        """小爱原生 TTS 用预生成流水线；OpenAI 兼容 / MLX-Audio 提前合成后面的段；其他 TTS 按段依次播。"""
        from core.services.tts.router import TTSRouter

        def on_play_start():
            self._light_state("speaking")
            if on_start:
                on_start()

        def on_idle():
            # 可播的都播完了、后端还在生成（例如"稍等"之后还在调工具）：回到思考灯
            self._light_state("thinking")

        provider = TTSRouter.resolve_provider(
            self.backend.tts_provider(),
            self.backend.get_tts_speaker_for_session_key(),
        )
        speaker = get_speaker()
        if provider == "xiaoai" and speaker is not None:
            return XiaoAINativeSegmentPlayer(
                speaker,
                NativeTTSSettings.from_config(self._conv_cfg("native_tts", {})),
                on_play_start=on_play_start,
                on_idle=on_idle,
            )
        if provider in TTSRouter.PREFETCHABLE_PROVIDERS:
            tts_speaker = self.backend.get_tts_speaker_for_session_key()

            async def synth(text: str):
                return await TTSRouter.synthesize_openai_compatible(
                    text, provider=provider, tts_speaker=tts_speaker
                )

            async def play(result):
                audio, audio_format = result
                self._playback_token = open_xiaoai_server.begin_playback_session()
                try:
                    await TTSRouter.play_audio_bytes(audio, audio_format, provider=provider)
                finally:
                    self._playback_token = None

            async def fallback(text: str):
                await TTSRouter._fallback_to_xiaoai(text, log_prefix=self.LOG_MODULE)

            return PrefetchSegmentPlayer(
                synth,
                play,
                fallback_fn=fallback,
                prefetch=self._conv_cfg("native_tts", {}).get("prefetch_segments", 2),
                on_play_start=on_play_start,
                on_idle=on_idle,
            )
        return CallbackSegmentPlayer(self._play_tts, on_play_start=on_play_start, on_idle=on_idle)

    async def _speak_stream(self, reply: ReplyStream):
        """边收流式回复边分段播放；被取消（打断 / stop）时停掉播放和请求。"""
        seg_cfg = self._conv_cfg("reply_segments", {}) or {}
        segmenter = ReplySegmenter(SegmenterSettings.from_config(seg_cfg))
        try:
            flush_s = max(0.05, float(seg_cfg.get("flush_delay_ms", 250)) / 1000)
        except (TypeError, ValueError, AttributeError):
            flush_s = 0.25
        filler = _ToolFiller(self._cfg("tool_filler", {}))
        loop = asyncio.get_running_loop()
        started = loop.time()
        marks: dict[str, float] = {}

        def on_start():
            marks.setdefault("play", loop.time() - started)

        player = self._new_segment_player(on_start)
        segments: list[str] = []  # 正文段（不含"稍等"）
        last_text_at = started
        failed = False
        finished = False

        async def add(texts: list[str]):
            for t in texts:
                if not segments:
                    marks["text"] = loop.time() - started
                segments.append(t)
                await player.add(t)

        try:
            await player.start()
            while True:
                try:
                    kind, value = await asyncio.wait_for(reply.queue.get(), timeout=flush_s)
                except asyncio.TimeoutError:
                    await add(segmenter.on_stall((loop.time() - last_text_at) * 1000))
                    continue
                if kind == "text":
                    last_text_at = loop.time()
                    await add(segmenter.feed(value))
                elif kind == "tool":
                    if value.get("tool"):
                        self._reply_tools.add(str(value["tool"]))
                    text = filler.on_event(value, spoken=bool(segments))
                    if text:
                        marks["filler"] = loop.time() - started
                        logger.info(f"💬 {text}（{value.get('tool')}）", module=self.LOG_MODULE)
                        await player.add(text)
                elif kind == "error":
                    failed = True
                    logger.warning(f"Reply stream failed: {value}", module=self.LOG_MODULE)
                    break
                else:
                    break
            await add(segmenter.finish())
            if segmenter.dropped:
                logger.info(f"Dropped leading English: {segmenter.dropped}", module=self.LOG_MODULE)
            if not segments:
                if not failed:
                    logger.warning(f"Empty reply from {self.BACKEND_NAME}", module=self.LOG_MODULE)
                await player.add(self._failure_text())
            await player.finish()
            finished = True

            def fmt(key):
                return f"{marks[key]:.1f}s" if key in marks else "-"

            logger.info(
                f"🗣️ 流式回复：稍等 {fmt('filler')}，正文 {fmt('text')}，开播 {fmt('play')}，"
                f"{len(segments)} 段（播完 {player.played}/{player.added}），共 {loop.time() - started:.1f}s"
                + (f"，错误: {player.error}" if player.error else ""),
                module=self.LOG_MODULE,
            )
            if player.error and player.played == 0 and segments:
                # 分段播放整个没起来（例如 mibrain 生成失败）：整段走原来的播放方式
                logger.warning("Segment playback failed, falling back to one-shot TTS", module=self.LOG_MODULE)
                await self._play_tts(" ".join(segments))
        finally:
            if not finished:
                await player.abort()
            await reply.close()

    def _reply_ends_conversation(self) -> bool:
        """这一轮放了 / 切了 / 暂停了音乐：回答完就结束对话（OwnDing 和原生小爱都这样）。

        音乐是对话中途才开始放的，进对话时压低音量没压到它；接着听的话 VAD 会把歌声当成你在说话，
        对话永远结束不了。Hermes 的工具事件不带参数，分不出"播放"和"查当前歌曲"，一律结束。
        """
        tools = self._cfg("end_after_tools", ["spotify_playback"]) or []
        hit = sorted(self._reply_tools & {str(t) for t in tools})
        if hit:
            logger.info(f"🎵 这轮调用了 {', '.join(hit)}，回答完结束对话", module=self.LOG_MODULE)
        return bool(hit)

    async def _play_tts(self, text: str):
        """Play text via Doubao TTS (blocks until playback finishes)."""
        self._playback_token = open_xiaoai_server.begin_playback_session()
        try:
            await self.backend.play_text(
                text,
                tts_speaker=self.backend.get_tts_speaker_for_session_key(),
                playback_token=self._playback_token,
            )
        except Exception as exc:
            logger.error(
                f"TTS playback error: {exc}",
                module=self.LOG_MODULE,
            )
            speaker = get_speaker()
            if speaker:
                await speaker.play(text=text)
        finally:
            self._playback_token = None

    async def _play_notify(self):
        """Play the listening-ready notification sound via PCM buffer."""
        if not _NOTIFY_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_NOTIFY_PCM)
                # Wait for playback to finish: PCM is int16 at 24000Hz
                duration = len(_NOTIFY_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Notify sound error: {exc}", module=self.LOG_MODULE)

    async def _play_send_sound(self):
        """Play the send notification before waiting for backend response."""
        if not _SEND_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_SEND_PCM)
                # 等待播放完成：PCM 为 int16，24000Hz
                duration = len(_SEND_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Send sound error: {exc}", module=self.LOG_MODULE)

    async def _call_after_wakeup(self):
        """Call the user-defined after_wakeup hook.

        The hook is user-supplied config; guard it so a bug in the callback
        (e.g. an IndexError while parsing session_key) never tears down the
        conversation exit flow.
        """
        after_wakeup = self.config.get_app_config("wakeup.after_wakeup")
        if after_wakeup:
            speaker = get_speaker()
            if speaker:
                try:
                    await after_wakeup(
                        speaker,
                        source=self.WAKEUP_SOURCE,
                        session_key=self.backend.get_session_key(),
                    )
                except Exception as exc:
                    import traceback
                    logger.error(
                        f"after_wakeup hook error: {type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}",
                        module=self.LOG_MODULE,
                    )
