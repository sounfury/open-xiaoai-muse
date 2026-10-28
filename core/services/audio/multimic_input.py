"""多麦克风唤醒输入：音箱 3 个麦克风 + 播放回采 → 回声消除 → IVA 分离 → 几路单声道给 KWS。

现状只用 ch0 一个麦克风；原生小爱用全部 3 个（mipns-xiaomi：AEC → 去混响 → IVA → 3 路各跑唤醒）。
这里给 KWS 额外加几路输入，和原来的 ch0 并行检测，任一路命中即唤醒。VAD / ASR / 打断补录仍走原来的单声道。

链路（不改音箱上的 client）：
- bridge 开 TCP 端口（multimic.port，默认 4401）。
- 通过 run_shell 让音箱后台跑 `arecord -D Capture -f S16_LE -r 16000 -c 4 | nc <bridge> <port>`：
  Capture 是 dsnoop，和 client 的录音、原生小爱共享；ALSA plug 转 16 kHz S16（≈ 原始 S32 >> 16），约 128 KB/s。
- 收到的数据在本模块的线程里：每路麦克风各过一个 SubbandEchoCanceller（ch3 回采），再 OnlineAuxIVA，
  乘 gain（默认 256，对齐线上单声道链路 S32 >> 8 的电平），按 512 采样一帧放进各路缓冲，KWS 线程来取。
- 看门狗：超过 stale_s 没收到数据就重新在音箱上启动（bridge 重启、音箱重启、WiFi 断开都能恢复）。

配置 APP_CONFIG["multimic"]，环境变量 MULTIMIC_ENABLE=1/0 可覆盖 enabled。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import threading
import time

import numpy as np

from core.utils.logger import logger
from core.services.diagnostics import diagnostics

LOG_MODULE = "MultiMic"
CHANNELS = 4  # ch0~ch2 麦克风，ch3 回采
MIC_CHANNELS = 3
REF_CHANNEL = 3
SR = 16000
FRAME = 512  # 和 KWS 帧长一致
BLOCK = 1024  # 每次处理的采样数（64 ms）
ARECORD = "arecord --quiet -t raw -D Capture -f S16_LE -r 16000 -c 4"
_PIDFILE = "/tmp/open-xiaoai-bridge-multimic.pid"  # 音箱上 arecord | nc 的 sh
OUTPUT_KINDS = ("iva", "sum", "ch1", "ch2")
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def multimic_enabled(cfg: dict | None) -> bool:
    env = os.environ.get("MULTIMIC_ENABLE", "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSY:
        return False
    return bool((cfg or {}).get("enabled", False))


class MultiMicInput:
    """单例。start() 后在后台收音频；KWS 线程用 pull() 取帧、discard() 丢弃。"""

    def __init__(self, cfg: dict):
        self.port = int(cfg.get("port", 4401))
        self.server_port = int(cfg.get("server_port", 4399))
        self.gain = float(cfg.get("gain", 256))
        self.alpha = float(cfg.get("iva_alpha", 0.97))
        self.stale_s = float(cfg.get("stale_s", 5))
        self.aec = bool(cfg.get("echo_cancel", True))
        kinds = [k for k in (cfg.get("outputs") or ["iva"]) if k in OUTPUT_KINDS]
        self.kinds = kinds or ["iva"]
        names: list[str] = []
        for kind in self.kinds:
            names.extend([f"iva{k}" for k in range(MIC_CHANNELS)] if kind == "iva" else [kind])
        self.names = names
        echo_cfg = cfg.get("echo_canceller") or {}
        self._echo_taps = int(echo_cfg.get("taps", 8))
        self._echo_tau = float(echo_cfg.get("tau_s", 3.0))
        from core.services.audio.aec import echo_canceller_impl
        from core.utils.config import ConfigManager

        # multimic.echo_canceller.impl 优先，其次和主麦同一个 barge_in.echo_canceller.impl
        self._echo_impl = echo_canceller_impl(cfg, ConfigManager.instance().get_app_config("barge_in", {}))
        self._lock = threading.Lock()  # 输出缓冲
        self._dsp_lock = threading.Lock()  # 回声消除 / IVA 状态（换连接时可能有两个接收线程）
        self._buffers: dict[str, bytearray] = {name: bytearray() for name in names}
        self._max_buffer = 2 * SR * 2  # 每路最多积 2 秒（KWS 线程跟不上就丢旧的）
        self._last_data = 0.0
        self._started = False
        self._conn: socket.socket | None = None
        self._reset_dsp()
        self.bytes_in = 0
        self.dsp_s = 0.0
        self.connects = 0

    def _reset_dsp(self):
        from core.services.audio.aec import create_echo_canceller
        from core.services.audio.multimic import OnlineAuxIVA

        from core.services.audio.highpass import make_highpass
        from core.utils.config import ConfigManager

        self._echo = [
            create_echo_canceller(self._echo_impl, taps=self._echo_taps, tau_s=self._echo_tau)
            for _ in range(MIC_CHANNELS)
        ]
        # 和单声道同一个高通（audio_input.highpass_hz），放在回声消除之后、IVA 之前
        self._highpass = make_highpass(
            ConfigManager.instance().get_app_config("audio_input.highpass_hz", None), channels=MIC_CHANNELS
        )
        self._iva = OnlineAuxIVA(MIC_CHANNELS, alpha=self.alpha) if "iva" in self.kinds else None
        self._pending = np.zeros((0, CHANNELS))

    # ---- KWS 线程接口 ----

    def pull(self, max_frames: int = 1) -> list[tuple[str, bytes]]:
        """每路最多取 max_frames 帧，避免多麦批处理长期挤占主麦检测。"""
        out: list[tuple[str, bytes]] = []
        with self._lock:
            n = min(max_frames, min(len(buf) for buf in self._buffers.values()) // (FRAME * 2))
            for i in range(n):
                for name, buf in self._buffers.items():
                    out.append((name, bytes(buf[i * FRAME * 2 : (i + 1) * FRAME * 2])))
            if n:
                for buf in self._buffers.values():
                    del buf[: n * FRAME * 2]
        return out

    def discard(self):
        with self._lock:
            for buf in self._buffers.values():
                buf.clear()

    def diagnostic_state(self, now: float) -> dict:
        """不等音频锁，防止诊断线程也被故障线程拖住。"""
        state = {
            "connected": self._conn is not None, "connects": self.connects,
            "input_age_ms": round((now - self._last_data) * 1000, 1) if self._last_data else None,
            "audio_s_total": round(self.bytes_in / (2 * CHANNELS * SR), 1),
            "dsp_s_total": round(self.dsp_s, 2),
        }
        if self._lock.acquire(blocking=False):
            try:
                state["queued_ms"] = {name: round(len(buf) / 32, 1) for name, buf in self._buffers.items()}
            finally:
                self._lock.release()
        else:
            state["buffer_lock_busy"] = True
        return state

    @property
    def live(self) -> bool:
        return time.monotonic() - self._last_data < self.stale_s

    # ---- 启动 ----

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._serve, daemon=True, name="multimic-recv").start()
        threading.Thread(target=self._watchdog, daemon=True, name="multimic-watchdog").start()
        logger.info(
            f"多麦唤醒输入已启用：端口 {self.port}，输出 {self.names}，gain {self.gain:g}，"
            f"IVA alpha {self.alpha}，回声消除 {(type(self._echo[0]).__name__) if self.aec else '关'}，"
            f"高通 {type(self._highpass).__name__ if self._highpass is not None else '关'}",
            module=LOG_MODULE,
        )

    def _serve(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.port))
        except OSError as exc:
            logger.error(f"多麦输入端口 {self.port} 绑定失败: {exc}", module=LOG_MODULE)
            return
        server.listen(2)
        while True:
            conn, addr = server.accept()
            try:
                private = ipaddress.ip_address(addr[0]).is_private
            except ValueError:
                private = False
            if not private:
                logger.warning(f"拒绝多麦输入连接 {addr[0]}", module=LOG_MODULE)
                conn.close()
                continue
            old, self._conn = self._conn, conn
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            self.connects += 1
            diagnostics.event("multimic.connected", connects=self.connects, replaced=old is not None)
            logger.info(f"音箱多麦录音已连接：{addr[0]}（第 {self.connects} 次）", module=LOG_MODULE)
            threading.Thread(target=self._receive, args=(conn,), daemon=True, name="multimic-conn").start()

    def _receive(self, conn: socket.socket):
        frame_bytes = 2 * CHANNELS
        pending = b""
        conn.settimeout(self.stale_s * 2)
        with self._dsp_lock:
            self._reset_dsp()  # 新连接：音频和上一段不连续
        try:
            while self._conn is conn:
                chunk = conn.recv(BLOCK * frame_bytes)
                if not chunk:
                    diagnostics.event("multimic.eof")
                    break
                self._last_data = time.monotonic()
                self.bytes_in += len(chunk)
                pending += chunk
                usable = len(pending) // frame_bytes * frame_bytes
                if usable:
                    data = np.frombuffer(pending[:usable], dtype="<i2").reshape(-1, CHANNELS)
                    pending = pending[usable:]
                    with self._dsp_lock:
                        if self._conn is not conn:
                            break
                        # TCP 碎包先攒满 BLOCK；预算按本次实际会处理的采样数计算。
                        processable = (len(self._pending) + len(data)) // BLOCK * BLOCK
                        with diagnostics.measure("multimic.dsp", audio_s=processable / SR, warn_s=0.2):
                            self._process(data.astype(np.float64))
        except (OSError, socket.timeout) as exc:
            diagnostics.event("multimic.receive_error", error_type=type(exc).__name__, current=self._conn is conn)
            logger.debug(f"多麦输入连接结束: {exc}", module=LOG_MODULE)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if self._conn is conn:
                self._conn = None
                logger.info("音箱多麦录音断开", module=LOG_MODULE)

    def _process(self, data: np.ndarray):
        started = time.perf_counter()
        self._pending = np.concatenate([self._pending, data])
        if len(self._pending) < BLOCK:
            return
        n = len(self._pending) // BLOCK * BLOCK
        block, self._pending = self._pending[:n], self._pending[n:]
        mics = block[:, :MIC_CHANNELS]
        ref = block[:, REF_CHANNEL]
        if self.aec:
            mics = np.stack([self._echo[c].process(mics[:, c], ref) for c in range(MIC_CHANNELS)], axis=1)
        if self._highpass is not None:
            mics = self._highpass.process(mics)
        outputs: dict[str, np.ndarray] = {}
        if self._iva is not None:
            y = self._iva.process(mics)
            for k in range(MIC_CHANNELS):
                outputs[f"iva{k}"] = y[:, k]
        if "sum" in self.kinds:
            outputs["sum"] = mics.mean(axis=1)
        for c in (1, 2):
            if f"ch{c}" in self.kinds:
                outputs[f"ch{c}"] = mics[:, c]
        with self._lock:
            for name, signal in outputs.items():
                pcm = np.clip(np.round(signal * self.gain), -32768, 32767).astype("<i2").tobytes()
                buf = self._buffers[name]
                buf.extend(pcm)
                if len(buf) > self._max_buffer:
                    diagnostics.count("multimic.dropped_samples", (len(buf) - self._max_buffer) // 2)
                    del buf[: len(buf) - self._max_buffer]
        self.dsp_s += time.perf_counter() - started

    # ---- 音箱端 ----

    def _device_command(self) -> str:
        pattern = f"[a]{ARECORD[1:]}"
        return (
            f"ip=$(netstat -tn 2>/dev/null | awk '$5 ~ /:{self.server_port}$/ && $6 == \"ESTABLISHED\" "
            f"{{split($5,a,\":\"); print a[1]; exit}}'); "
            f"[ -n \"$ip\" ] || exit 1; "
            f"kill $(cat {_PIDFILE} 2>/dev/null) 2>/dev/null; "
            f"for p in $(ps w | grep '{pattern}' | awk '{{print $1}}'); do kill $p; done; "
            # 不带 -p 时 busybox 会把已有的任意 sh 当成"已在运行"而不启动
            f"start-stop-daemon -S -b -m -p {_PIDFILE} -x /bin/sh -- -c \"{ARECORD} | nc $ip {self.port}\"; "
            f"echo started $ip"
        )

    def _watchdog(self):
        from core.ref import get_app, get_speaker

        last_try = 0.0
        last_report = time.monotonic()
        last_bytes, last_dsp = 0, 0.0
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            if now - last_report >= 60:
                audio_s = (self.bytes_in - last_bytes) / (2 * CHANNELS * SR)
                if audio_s > 0:
                    logger.debug(
                        f"近 60 秒收到 {audio_s:.1f}s 音频，处理用时 {self.dsp_s - last_dsp:.1f}s",
                        module=LOG_MODULE,
                    )
                last_report, last_bytes, last_dsp = now, self.bytes_in, self.dsp_s
            if self.live or now - last_try < max(10.0, self.stale_s * 2):
                continue
            app, speaker = get_app(), get_speaker()
            if app is None or speaker is None or getattr(app, "loop", None) is None:
                continue
            last_try = now
            diagnostics.event("multimic.restart_requested", input_age_ms=round((now - self._last_data) * 1000) if self._last_data else None)
            try:
                future = asyncio.run_coroutine_threadsafe(
                    speaker.run_shell(self._device_command(), timeout=5000), app.loop
                )
                # 等待超时不表示任务取消；记录最终完成时刻，识别延迟执行的重启。
                def completed(done, requested=now):
                    diagnostics.event("multimic.restart_finished", elapsed_ms=round((time.monotonic() - requested) * 1000), cancelled=done.cancelled())
                future.add_done_callback(completed)
                with diagnostics.measure("multimic.restart_wait", warn_s=2):
                    result = future.result(timeout=8)
            except Exception as exc:
                diagnostics.event("multimic.restart_wait_error", error_type=type(exc).__name__)
                logger.debug(f"启动音箱多麦录音失败: {type(exc).__name__}: {exc}", module=LOG_MODULE)
                continue
            if result is None or getattr(result, "exit_code", 1) != 0:
                logger.debug(
                    f"音箱多麦录音未启动（client 未连接？）: {getattr(result, 'stderr', '')}", module=LOG_MODULE
                )
            else:
                logger.info(f"已在音箱上启动多麦录音：{getattr(result, 'stdout', '').strip()}", module=LOG_MODULE)


_instance: MultiMicInput | None = None


def get_multimic(cfg: dict | None) -> MultiMicInput | None:
    """multimic 未启用时返回 None；启用时返回（并启动）单例。"""
    global _instance
    if not multimic_enabled(cfg):
        return None
    if _instance is None:
        _instance = MultiMicInput(cfg or {})
        _instance.start()
    return _instance
