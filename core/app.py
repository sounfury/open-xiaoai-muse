"""Main application controller.

This module manages the main application flow, coordinating between:
- XiaoAI (Xiaomi speaker service)
- XiaoZhi (AI conversation service)
- External chat backends (core/backends.py: Hermes, OpenAI-compatible, OpenClaw, QwenPaw)
- Audio system (VAD, KWS, Codec)
"""

import asyncio
import os
import threading
import time

from core.xiaozhi import XiaoZhi
from core.xiaoai import XiaoAI
from core.ref import set_xiaozhi, set_app
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.services.protocols.typing import (
    DeviceState,
    EventType,
)
from core.backends import ChatBackend, get_backend
from core.muse import is_muse_enabled
from core.services.api_server import APIServer
from core.services.diagnostics import diagnostics


class MainApp:
    """Main application controller."""

    _instance = None

    @classmethod
    def instance(
        cls,
        enable_xiaozhi: bool = True,
        backends: tuple[str, ...] = (),
    ):
        """Get singleton instance.

        Args:
            enable_xiaozhi: Whether to enable XiaoZhi AI connection (default: True)
            backends: Enabled external chat backends, e.g. ("hermes",) (see core/backends.py)
        """
        if cls._instance is None:
            cls._instance = MainApp(enable_xiaozhi=enable_xiaozhi, backends=backends)
        return cls._instance

    def __init__(
        self,
        enable_xiaozhi: bool = True,
        backends: tuple[str, ...] = (),
    ):
        """Initialize the main application.

        Args:
            enable_xiaozhi: Whether to enable XiaoZhi AI connection
            backends: Enabled external chat backends (names registered in core/backends.py)
        """
        unknown = [name for name in backends if get_backend(name) is None]
        if unknown:
            raise ValueError(f"Unknown backends: {unknown}")
        if MainApp._instance is not None:
            raise Exception("MainApp is singleton, use instance() to get instance")
        MainApp._instance = self

        # Config
        self.config = ConfigManager.instance()

        # Feature flags
        self._enable_xiaozhi = enable_xiaozhi
        self._backends = tuple(backends)

        # Device state
        self.device_state = DeviceState.IDLE
        self.current_text = ""
        self.current_emotion = "neutral"

        # Event loop and threads
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.config_watch_thread = None
        self.shutdown_requested = False
        self.running = False

        # Task queue
        self.main_tasks = []
        self.mutex = threading.Lock()

        # Events
        self.events = {
            EventType.SCHEDULE_EVENT: threading.Event(),
            EventType.AUDIO_INPUT_READY_EVENT: threading.Event(),
        }

        # XiaoZhi instance (protocol layer)
        self.xiaozhi = None

        # API Server
        self.api_server = None
        self._enable_api_server = False

        set_app(self)

    @property
    def protocol(self):
        """Access XiaoZhi protocol for backward compatibility."""
        if self.xiaozhi:
            return self.xiaozhi.protocol
        return None

    def _enabled_backends(self) -> list[type[ChatBackend]]:
        return [backend for backend in map(get_backend, self._backends) if backend is not None]

    def _local_asr_backends(self) -> list[str]:
        """启用且 input_mode 是 local_asr 的后端（需要本地音频输入和 ASR 模型）。"""
        return [
            backend.LABEL
            for backend in self._enabled_backends()
            if self.config.get_app_config(f"{backend.CONFIG_SECTION}.input_mode", "local_asr")
            == "local_asr"
        ]

    def run(self, enable_api_server: bool = False):
        """Start the main application.

        Args:
            enable_api_server: Whether to start the HTTP API Server
        """
        self._enable_api_server = enable_api_server

        # Check audio input status
        audio_input_enabled = os.environ.get(
            "AUDIO_INPUT_ENABLE", "true"
        ).strip().lower() in ("true", "1", "yes", "on")
        
        if not audio_input_enabled and self._enable_xiaozhi:
            raise RuntimeError(
                "Audio input is disabled (AUDIO_INPUT_ENABLE=false) but XiaoZhi is enabled. "
                "Either enable audio input or disable XiaoZhi."
            )
        
        local_asr_backends = self._local_asr_backends()
        if not audio_input_enabled and local_asr_backends:
            raise RuntimeError(
                "Audio input is disabled (AUDIO_INPUT_ENABLE=false) but "
                f"{', '.join(local_asr_backends)} uses 'local_asr' mode. "
                "Either enable audio input, or set input_mode='xiaoai_asr' in config."
            )

        # Create event loop thread
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        self._start_config_watcher()

        time.sleep(0.1)

        # Initialize XiaoAI service
        # Windows: pyo3-async-runtimes start_server does not bind when scheduled via
# run_coroutine_threadsafe on a background loop; run it with asyncio.run in its own thread.
        threading.Thread(
            target=lambda: asyncio.run(XiaoAI.init_xiaoai()),
            daemon=True,
            name="xiaoai-ws-server",
        ).start()

        if self._enable_xiaozhi:
            # Create XiaoZhi instance
            self.xiaozhi = XiaoZhi.instance()
            self.xiaozhi.set_app(self)
            set_xiaozhi(self.xiaozhi)

            # Initialize XiaoZhi connection
            asyncio.run_coroutine_threadsafe(self._init_xiaozhi(), self.loop)

        # Initialize enabled external chat backends
        for backend in self._enabled_backends():
            backend.initialize_from_config()
            asyncio.run_coroutine_threadsafe(backend.connect(), self.loop)

        # Start API Server if enabled
        if self._enable_api_server:
            host = os.environ.get("API_SERVER_HOST", "127.0.0.1")
            port = int(os.environ.get("API_SERVER_PORT", 9092))
            self.api_server = APIServer(host=host, port=port)
            asyncio.run_coroutine_threadsafe(self.api_server.start(), self.loop)

        # Start main loop thread
        main_loop_thread = threading.Thread(target=self._main_loop)
        main_loop_thread.daemon = True
        main_loop_thread.start()

        # Start audio services
        enable_muse = is_muse_enabled()
        if self._enable_xiaozhi or self._backends or enable_muse:
            # Check audio input via env var (same as Rust), default True
            # Supports: "true"/"false", "1"/"0", "yes"/"no", "on"/"off"
            audio_input_enabled = os.environ.get(
                "AUDIO_INPUT_ENABLE", "true"
            ).strip().lower() in ("true", "1", "yes", "on")
            if audio_input_enabled:
                from core.services.audio.vad import VAD
                from core.services.audio.kws import KWS
                VAD.start()
                KWS.start()
                logger.info("[MainApp] Audio input enabled (VAD/KWS started)")
                try:
                    from core.services.music_duck import MusicDucker

                    ducker = MusicDucker.instance()
                    if ducker is not None:
                        ducker.warm_up()
                except Exception as exc:
                    logger.warning(f"[MainApp] Music duck warm-up failed: {exc}")
            else:
                logger.info("[MainApp] Audio input disabled (VAD/KWS not started)")

            # Pre-warm local ASR only when an enabled backend is configured to use it.
            if audio_input_enabled and local_asr_backends:
                from core.services.audio.asr import ASRService

                threading.Thread(
                    target=ASRService.ensure_loaded,
                    daemon=True,
                    name="asr-warmup",
                ).start()

    def _run_event_loop(self):
        """Run asyncio event loop in separate thread."""
        asyncio.set_event_loop(self.loop)
        diagnostics.start()
        self.loop.run_forever()

    def _start_config_watcher(self):
        """Start config file watcher thread."""
        if self.config_watch_thread and self.config_watch_thread.is_alive():
            return

        self.config_watch_thread = threading.Thread(
            target=self._watch_config_file,
            daemon=True,
        )
        self.config_watch_thread.start()

    def _watch_config_file(self):
        """Poll config file for changes and hot-reload."""
        config_path = self.config.get_config_path()
        last_mtime = None

        while True:
            if self.shutdown_requested:
                break

            try:
                current_mtime = os.path.getmtime(config_path)
                if last_mtime is None:
                    last_mtime = current_mtime
                elif current_mtime != last_mtime:
                    last_mtime = current_mtime
                    self.config.reload_app_config()
                    logger.info(f"[Config] Reloaded runtime config from {config_path}")
            except Exception as exc:
                logger.warning(f"[Config] Failed to reload config: {exc}")

            time.sleep(1)

    async def _init_xiaozhi(self):
        """Initialize XiaoZhi connection and audio."""
        self.device_state = DeviceState.CONNECTING
        await self.xiaozhi.connect()
        self.xiaozhi.init_audio()

    def _main_loop(self):
        """Main application loop."""
        self.running = True

        while self.running:
            for event_type, event in self.events.items():
                if event.is_set():
                    event.clear()

                    if event_type == EventType.AUDIO_INPUT_READY_EVENT:
                        if self.xiaozhi:
                            self.xiaozhi.handle_input_audio()
                    elif event_type == EventType.SCHEDULE_EVENT:
                        self._process_scheduled_tasks()

            time.sleep(0.01)

    def _process_scheduled_tasks(self):
        """Process scheduled tasks."""
        with self.mutex:
            tasks = self.main_tasks.copy()
            self.main_tasks.clear()

        for task in tasks:
            try:
                task()
            except Exception as exc:
                logger.error(
                    f"[MainApp] Scheduled task failed: {type(exc).__name__}: {exc}"
                )

    def schedule(self, callback):
        """Schedule task to main loop."""
        with self.mutex:
            if "abort_speaking" in str(callback):
                if any("abort_speaking" in str(task) for task in self.main_tasks):
                    return
            self.main_tasks.append(callback)
        self.events[EventType.SCHEDULE_EVENT].set()

    # State management

    def set_chat_message(self, role, message):
        """Set chat message."""
        self.current_text = message

    def set_emotion(self, emotion):
        """Set emotion."""
        self.current_emotion = emotion

    def alert(self, title, message):
        """Show alert."""
        logger.warning(f"[Alert] {title}: {message}")

    # Shutdown

    def shutdown(self):
        """Shutdown the application."""
        self.shutdown_requested = True
        diagnostics.stop()
        self.running = False

        if self.xiaozhi:
            self.xiaozhi.shutdown()

        if self.api_server:
            asyncio.run_coroutine_threadsafe(
                self.api_server.stop(), self.loop
            )

        # Close enabled external chat backends (connections, pending waiters)
        for backend in self._enabled_backends():
            asyncio.run_coroutine_threadsafe(backend.close(), self.loop)

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)

        if self.config_watch_thread and self.config_watch_thread.is_alive():
            self.config_watch_thread.join(timeout=1.0)

    # Public API

    async def send_text(self, text):
        """Send text to XiaoZhi."""
        if self.xiaozhi and self.xiaozhi.is_connected():
            await self.xiaozhi.send_text(text)

    async def send_to_backend(
        self,
        name: str,
        text: str,
        wait_response: bool = False,
        play_reply: bool = False,
    ) -> str | None:
        """Send a one-off message to an external backend ("hermes" / "openai" / "openclaw" / "qwenpaw").

        play_reply=False (skill-based autonomous playback): appends rule_prompt_for_skill.
        play_reply=True: appends rule_prompt and plays the reply via the backend's TTS.
        Returns run_id or response text on success, None on failure.
        """
        backend = get_backend(name)
        if backend is None:
            logger.error(f"[MainApp] 未知的后端: {name!r}")
            return None
        try:
            if play_reply:
                return await backend.send_and_play_reply(
                    backend.with_rule_prompt(text), wait_response=wait_response
                )
            return await backend.send(
                backend.with_rule_prompt(text, for_skill=True), wait_response=wait_response
            )
        except Exception as e:
            logger.error(f"[MainApp] 发送消息到 {backend.LABEL} 失败: {type(e).__name__}: {e}")
            return None

    def set_backend_session_key(self, name: str, session_key: str):
        """Override a backend's session key at runtime (reset to the config default on each wakeup).

        Call this in before_wakeup before returning the backend name to route
        the conversation to a different session, e.g. "agent:user123:my-app".
        """
        backend = get_backend(name)
        if backend is None:
            raise ValueError(f"Unknown backend: {name!r}")
        backend.set_session_key(session_key)

    # 各后端的快捷方法（config.py 的 before_wakeup 里可以直接用）

    async def send_to_hermes(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("hermes", text, wait_response)

    async def send_to_hermes_and_play_reply(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("hermes", text, wait_response, play_reply=True)

    def set_hermes_session_key(self, session_key: str):
        self.set_backend_session_key("hermes", session_key)

    async def send_to_openai(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("openai", text, wait_response)

    async def send_to_openai_and_play_reply(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("openai", text, wait_response, play_reply=True)

    def set_openai_session_key(self, session_key: str):
        self.set_backend_session_key("openai", session_key)

    async def send_to_openclaw(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("openclaw", text, wait_response)

    async def send_to_openclaw_and_play_reply(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("openclaw", text, wait_response, play_reply=True)

    def set_openclaw_session_key(self, session_key: str):
        self.set_backend_session_key("openclaw", session_key)

    async def send_to_qwenpaw(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("qwenpaw", text, wait_response)

    async def send_to_qwenpaw_and_play_reply(self, text: str, wait_response: bool = False) -> str | None:
        return await self.send_to_backend("qwenpaw", text, wait_response, play_reply=True)

    def set_qwenpaw_session_key(self, session_key: str):
        self.set_backend_session_key("qwenpaw", session_key)
