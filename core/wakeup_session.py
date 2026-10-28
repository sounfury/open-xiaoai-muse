import asyncio
import time

from core.backends import all_backends, get_backend
from core.ref import (
    get_app,
    get_kws,
    get_speaker,
    get_xiaozhi,
)
from core.services.protocols.typing import AbortReason
from core.utils.config import ConfigManager
from core.utils.logger import logger


class WakeupSessionManager:
    """Dispatches wakeup events to XiaoZhi or external backend controllers."""

    def __init__(self):
        self.config = ConfigManager.instance()
        # 当前的外部后端连续对话（同一时间最多一个：开始新会话前 reset_all_sessions 会停掉旧的）
        self._controller = None
        self._controller_task: asyncio.Task | None = None
        self._xiaozhi_future: asyncio.Future | None = None
        # 只在 MainApp.loop 读写；整个唤醒过程（含重置与会话）只有一个拥有者。
        self._wakeup_task: asyncio.Task | None = None
        self._interrupt_task: asyncio.Task | None = None

    def _get_loop(self):
        app = get_app()
        if app:
            return app.loop
        from core.xiaoai import XiaoAI
        return XiaoAI.async_loop

    async def _stop_device_playback(self):
        """先停播放；回采仍在线时保留录音，避免每次唤醒都重启采集。"""
        import open_xiaoai_server
        from core.xiaoai import XiaoAI

        step = "停止播放"
        started = time.monotonic()
        try:
            async with asyncio.timeout(5):
                speaker = get_speaker()
                if speaker:
                    await speaker.stop_device_audio()
                else:
                    await open_xiaoai_server.stop_playing()
                if not XiaoAI.echo_reference_live():
                    step = "恢复录音"
                    await open_xiaoai_server.start_recording()
                XiaoAI.set_recording_wanted(True)
        except TimeoutError:
            logger.error(f"[Wakeup] {step}超时，取消本次唤醒，避免延迟后突然播报")
            raise
        finally:
            elapsed = time.monotonic() - started
            if elapsed > 1:
                logger.warning(f"[Wakeup] 设备音频重置耗时 {elapsed:.1f}s（最后步骤: {step}）")

    def on_interrupt(self):
        # 原生事件来自桥接线程，业务状态和取消操作统一回到主循环。
        self._get_loop().call_soon_threadsafe(self._schedule_interrupt)

    def _schedule_interrupt(self):
        if self._interrupt_task and not self._interrupt_task.done():
            return
        logger.info("[Wakeup] XiaoAI wakeup — interrupting active sessions")
        self._interrupt_task = asyncio.create_task(self._interrupt_sessions(self._wakeup_task))
        self._interrupt_task.add_done_callback(self._log_interrupt_result)

    @staticmethod
    def _log_interrupt_result(task):
        if not task.cancelled() and task.exception():
            logger.error(f"[Wakeup] Interrupt failed: {task.exception()}")

    async def _interrupt_sessions(self, owner):
        kws = get_kws()
        if kws:
            kws.pause()
        try:
            if owner and not owner.done() and not owner.cancelling():
                owner.cancel()
            await self._stop_external_sessions()
            if owner:
                await asyncio.gather(owner, return_exceptions=True)
            if self._xiaozhi_future and not self._xiaozhi_future.done():
                self._xiaozhi_future.cancel()
            self._xiaozhi_future = None
            xiaozhi = get_xiaozhi()
            if xiaozhi:
                xiaozhi.stop_wakeup_session()
            from core.xiaoai import XiaoAI

            XiaoAI.stop_conversation()
            await self._stop_device_playback()
        finally:
            if kws:
                kws.resume()

    async def _stop_external_sessions(self):
        """取消并等到旧会话彻底退出，包括子播放器的清理。"""
        controller, task = self._controller, self._controller_task
        if controller and controller.is_active():
            controller.stop()
        if task and not task.done():
            # owner.cancel() 会沿 await 传播取消，不能再次取消正在清理播放器的任务。
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def on_wakeup(self):
        logger.info("[Wakeup] Wakeup session started")
        xiaozhi = get_xiaozhi()
        if xiaozhi:
            xiaozhi._is_first_round = True
            future = asyncio.run_coroutine_threadsafe(
                xiaozhi.start_wakeup_session(), self._get_loop()
            )
            self._xiaozhi_future = future

            def _clear_future(done_future):
                if self._xiaozhi_future is done_future:
                    self._xiaozhi_future = None

            future.add_done_callback(_clear_future)

    def on_speech(self, speech_buffer: bytes):
        """Called by VAD when speech is detected."""
        pass

    def on_silence(self):
        """Called by VAD when silence is detected."""
        pass

    def consume_xiaoai_asr_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Route XiaoAI native ASR results to the active external backend controller."""
        controller = self._controller
        if controller and controller.is_active():
            return controller.consume_xiaoai_recognize_result(
                dialog_id=dialog_id,
                text=text,
                is_final=is_final,
                is_vad_begin=is_vad_begin,
            )
        return False

    async def wakeup(self, text, source, position: int | None = None, detected_at: float | None = None):
        """统一在业务主循环处理唤醒；重复请求直接丢弃，不排队重放。"""
        loop = self._get_loop()
        if asyncio.get_running_loop() is not loop:
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                self.wakeup(text, source, position, detected_at), loop
            ))
        owner = asyncio.current_task()
        if self._wakeup_task and not self._wakeup_task.done():
            logger.debug(f"[Wakeup] 忽略会话期间的重复唤醒: {text}")
            return
        self._wakeup_task = owner
        kws = get_kws()
        if kws:
            kws.pause()
        try:
            if self._interrupt_task and not self._interrupt_task.done():
                await asyncio.shield(self._interrupt_task)
                if kws:
                    kws.pause()
            if detected_at is not None and time.monotonic() - detected_at > 2:
                logger.warning(f"[Wakeup] 丢弃过时唤醒: {text}（等待 {time.monotonic() - detected_at:.1f}s）")
                return
            await self._run_wakeup(text, source, position)
        finally:
            if self._wakeup_task is owner:
                self._wakeup_task = None
                if kws:
                    kws.resume()

    async def _run_wakeup(self, text, source, position: int | None):
        """position 是唤醒词结束的采样位置，供第一轮补录紧接着说出的内容。"""
        before_wakeup = self.config.get_app_config("wakeup.before_wakeup")
        if source != "kws":
            from core.utils.turn_timer import TurnTimer

            TurnTimer.begin(f"{source} 唤醒")  # KWS 命中时已在 KWS 线程开始计时
        logger.debug(f"[Wakeup] Received wakeup request from {source}: {text}")

        # Reset session_key to config default before each wakeup,
        # so paths that don't call set_*_session_key() always use the default.
        for backend in all_backends():
            backend.reset_session_key()

        should_wakeup = await before_wakeup(
            get_speaker(),
            text,
            source,
            get_app(),
        )
        logger.info(f"[Wakeup] before_wakeup returned: {should_wakeup}")
        from core.utils.turn_timer import TurnTimer

        TurnTimer.mark("before_wakeup")
        if should_wakeup is not None:
            await self.reset_all_sessions()
            TurnTimer.mark("重置会话")
        else:
            TurnTimer.clear()

        if should_wakeup == "xiaozhi":
            self.on_wakeup()
        elif should_wakeup is not None:
            backend = get_backend(should_wakeup)
            if backend is None:
                logger.error(f"[Wakeup] before_wakeup 返回了未知的后端: {should_wakeup!r}")
            elif not backend.is_enabled():
                logger.error(
                    f"[Wakeup] {backend.LABEL} 未启用（{' / '.join(backend.ENABLE_ENV)}），不进入连续对话"
                )
            else:
                await self._start_external_conversation(backend, position)

    async def _start_external_conversation(self, backend, wake_position):
        """唤醒拥有者等待整个会话结束；旧任务只能清理自己持有的引用。"""
        from core.external_conversation import ExternalConversationController

        controller = ExternalConversationController(backend)
        task = asyncio.create_task(controller.start(wake_position=wake_position))
        self._controller, self._controller_task = controller, task
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[Wakeup] {backend.NAME} conversation failed: {type(exc).__name__}: {exc}")
        finally:
            if self._controller_task is task:
                self._controller, self._controller_task = None, None

    async def reset_all_sessions(self):
        """Reset all active sessions before starting a new one.

        Stops XiaoAI continuous conversation, interrupts any active XiaoZhi
        session, and stops any external backend continuous conversation.
        """
        from core.xiaoai import XiaoAI
        from core.ref import get_xiaozhi

        # Stop XiaoAI continuous conversation
        XiaoAI.stop_conversation()

        # Interrupt active XiaoZhi session
        xiaozhi = get_xiaozhi()
        if xiaozhi and xiaozhi.is_connected():
            try:
                await xiaozhi.send_abort_speaking(AbortReason.ABORT)
            except Exception:
                pass

        # 停止标志不能中断正在等待的 TTS / HTTP，必须取消并等旧任务完成。
        await self._stop_external_sessions()

        # Stop all audio playback on the device
        await self._stop_device_playback()

        logger.debug("[Wakeup] All sessions reset")


EventManager = WakeupSessionManager()
