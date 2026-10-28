"""子带回声消除：音箱播报时也能听见唤醒词。

移植自 OwnDing/open-xiaoai（MIT，commit 7ecea6f）
examples/xiaozhi/xiaozhi/services/audio/aec.py。本项目只加了两道保护（见 _frame）：
  - 刚开始学习的前 `warmup_frames` 帧只学习不相减（此时历史帧不满，求解病态，系数会发散）；
  - 每个频点的输出幅度不超过原麦克风（回声估计错误时不会把声音放大）。
  在真机录音上，不加保护时每次开始播放的前 0.5 秒输出会被放大 15～30 dB。

音箱客户端开启 echo_ref 后，同时发回一个麦克风声道和它自己正在播放的声音
（硬件回采，与麦克风采样对齐）。线性子带滤波器从麦克风里减掉播放的声音，
房间里的人声与播放内容不相关，基本不受影响。

每个频点用最近 `taps` 帧回采乘以传递函数来估计回声，传递函数由指数平均的
互谱求解。滤波器只在有播放时学习，不播放时保持不变（回声路径就是音箱自己的
腔体，几乎不变），因此不需要双讲检测。OwnDing 先试过 WebRTC AEC3：它的残余
回声抑制把说话人也压掉了，大部分唤醒词识别不出来。

Rust 版（native/src/dsp/aec.rs，`open_xiaoai_server.EchoCanceller`）接口相同、计算时释放 GIL；
用 create_echo_canceller() 按配置 `echo_canceller.impl`（"python" | "rust"）选择。
"""

import numpy as np

from core.utils.logger import logger

IMPLS = ("python", "rust")
_warned_fallback = False


def create_echo_canceller(impl: str | None = None, **kwargs):
    """按 impl 创建回声消除器；"rust" 不可用（旧的原生模块）时退回 Python 版并记一次警告。"""
    global _warned_fallback
    impl = str(impl or "rust").strip().lower()
    if impl not in IMPLS:
        logger.warning(f"未知的回声消除实现 {impl!r}，使用 rust", module="AEC")
        impl = "rust"
    if impl == "rust":
        try:
            from open_xiaoai_server import EchoCanceller

            return EchoCanceller(**kwargs)
        except ImportError:
            if not _warned_fallback:
                _warned_fallback = True
                logger.warning("原生模块里没有 EchoCanceller（需要重新编译），回声消除退回 Python 版", module="AEC")
    return SubbandEchoCanceller(**kwargs)


def echo_canceller_impl(*sections: dict | None) -> str:
    """依次在几个配置段的 echo_canceller.impl 里取第一个设置了的值，默认 rust。"""
    for section in sections:
        value = ((section or {}).get("echo_canceller") or {}).get("impl")
        if value:
            return str(value)
    return "rust"


class SubbandEchoCanceller:
    def __init__(
        self,
        sample_rate=16000,
        n_fft=1024,
        hop=256,
        taps=8,
        tau_s=3.0,
        solve_every=4,
        history_s=2.0,
        warmup_frames=16,
    ):
        self.n_fft, self.hop, self.taps = n_fft, hop, taps
        self.solve_every = max(1, int(solve_every))
        self.sample_rate = sample_rate
        self.window = np.sqrt(np.hanning(n_fft + 1)[:n_fft])
        self.lam = float(np.exp(-hop / (sample_rate * tau_s)))
        bins = n_fft // 2 + 1
        self.rxx = np.tile(np.eye(taps, dtype=np.complex128) * 1e-12, (bins, 1, 1))
        self.rxy = np.zeros((bins, taps), dtype=np.complex128)
        self.filter = np.zeros((bins, taps), dtype=np.complex128)
        self.history = np.zeros((bins, taps), dtype=np.complex128)
        self.frames = 0
        self.warmup_frames = max(0, int(warmup_frames))
        # Frames are formed from the last n_fft samples; start as if preceded
        # by silence so output length always equals input length.
        self._mic = np.zeros(n_fft - hop)
        self._ref = np.zeros(n_fft - hop)
        self._overlap = np.zeros(n_fft)
        self._norm = np.zeros(n_fft)
        self._out = np.zeros(hop)  # finished samples not yet returned
        self._echo = np.zeros(hop)  # and the echo removed from them
        self.delay = n_fft  # samples from input to output
        # Recent output and echo estimate, for judging a wake word heard
        # during playback (see near_end_ratio_db).
        self._recent = int(history_s * sample_rate)
        self._recent_out = np.zeros(0)
        self._recent_echo = np.zeros(0)
        self.recent_end = 0  # output sample index after the last recent sample
        self.ref_active = False
        self._mic_power = self._out_power = 0.0

    def process(self, mic, ref):
        """mic, ref: float arrays of equal length. Returns as many cleaned
        samples, delayed by `self.delay`."""
        mic = np.asarray(mic, dtype=np.float64)
        ref = np.asarray(ref, dtype=np.float64)
        self._mic = np.concatenate([self._mic, mic])
        self._ref = np.concatenate([self._ref, ref])
        outs, echoes = [self._out], [self._echo]
        while len(self._mic) >= self.n_fft:
            out, echo = self._frame(self._mic[: self.n_fft], self._ref[: self.n_fft])
            outs.append(out)
            echoes.append(echo)
            self._mic = self._mic[self.hop :]
            self._ref = self._ref[self.hop :]
        out, echo = np.concatenate(outs), np.concatenate(echoes)
        n = len(mic)
        result, self._out = out[:n], out[n:]
        self._echo = echo[n:]
        self._remember(result, echo[:n])
        return result

    def _frame(self, mic, ref):
        n, hop, w = self.n_fft, self.hop, self.window
        x = np.fft.rfft(ref * w)
        y = np.fft.rfft(mic * w)
        self.history[:, 1:] = self.history[:, :-1]
        self.history[:, 0] = x
        self.ref_active = bool(np.any(ref[-hop:]))
        if self.ref_active:
            h = self.history
            self.rxx *= self.lam
            self.rxx += (1 - self.lam) * h[:, :, None] * np.conj(h[:, None, :])
            self.rxy *= self.lam
            self.rxy += (1 - self.lam) * y[:, None] * np.conj(h)
            if self.frames % self.solve_every == 0:
                trace = np.trace(self.rxx, axis1=1, axis2=2).real
                reg = np.eye(self.taps) * (trace[:, None, None] / self.taps * 1e-3 + 1e-12)
                self.filter = np.linalg.solve(np.conj(self.rxx) + reg, self.rxy[:, :, None])[:, :, 0]
            self.frames += 1
        if self.frames < self.warmup_frames:
            residual = y
        else:
            echo = np.sum(self.filter * self.history, axis=1)
            residual = y - echo
            # 回声估计偏离时不放大：输出幅度不超过麦克风本身
            louder = np.abs(residual) > np.abs(y)
            residual[louder] = y[louder]
        e = np.fft.irfft(residual, n) * w
        self._overlap += e
        self._norm += w * w
        out = self._overlap[:hop] / np.maximum(self._norm[:hop], 1e-6)
        self._overlap = np.concatenate([self._overlap[hop:], np.zeros(hop)])
        self._norm = np.concatenate([self._norm[hop:], np.zeros(hop)])
        if self.ref_active:
            m = mic[:hop]
            self._mic_power = 0.95 * self._mic_power + 0.05 * float(np.mean(m * m))
            self._out_power = 0.95 * self._out_power + 0.05 * float(np.mean(out * out))
        # Echo estimate for the same output samples: what was removed.
        return out, mic[:hop] - out

    def _remember(self, out, echo):
        self._recent_out = np.concatenate([self._recent_out, out])[-self._recent :]
        self._recent_echo = np.concatenate([self._recent_echo, echo])[-self._recent :]
        self.recent_end += len(out)

    def reduction_db(self):
        """Smoothed echo reduction while something plays (None before that)."""
        if self._mic_power <= 0 or self._out_power <= 0:
            return None
        return 10 * np.log10(self._mic_power / self._out_power)

    def near_end_ratio_db(self, window_s=1.5, slice_s=0.3):
        """In the last `window_s`, the loudest `slice_s` of cleaned output
        against the echo removed there, 300–3400 Hz. A real call stands out
        (measured ≥ −13 dB at 3 m, volume 75); 小七's own voice leaking
        through measured −20 dB."""
        n = min(len(self._recent_out), len(self._recent_echo), int(window_s * self.sample_rate))
        if n < int(slice_s * self.sample_rate):
            return None
        out = self._recent_out[-n:]
        echo = self._recent_echo[-n:]
        size = int(slice_s * self.sample_rate)
        step = max(1, size // 3)
        freqs = np.fft.rfftfreq(size, 1 / self.sample_rate)
        band = (freqs >= 300) & (freqs <= 3400)
        best = None
        for start in range(0, n - size + 1, step):
            o = np.abs(np.fft.rfft(out[start : start + size]))[band]
            e = np.abs(np.fft.rfft(echo[start : start + size]))[band]
            po, pe = float(np.sum(o * o)), float(np.sum(e * e))
            if best is None or po > best[0]:
                best = (po, pe)
        po, pe = best
        return 10 * np.log10((po + 1e-12) / (pe + 1e-12))
