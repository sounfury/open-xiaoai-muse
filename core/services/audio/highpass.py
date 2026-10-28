"""流式高通滤波：去掉 cutoff 以下的低频（空调 / 冰箱嗡声、桌面震动、脚步、音乐低音鼓）。

人声区分字音的信息主要在 300～4000 Hz，男声基频最低约 85 Hz，所以 100～150 Hz 以下去掉对识别没有影响。
原生小爱前端第一步就是高通（flexkws.json5 的 hpf 节点）；OwnDing 实测音箱底噪大部分在 150 Hz 以下。
2 阶 Butterworth（一个二阶节），跨包保留状态，每声道独立。

默认用 Rust 版（native/src/dsp/highpass.rs，`open_xiaoai_server.HighPassFilter`）；配置
`audio_input.highpass_impl = "python"` 或原生模块太旧时用这里的纯 Python 版（逐采样循环，慢，只作备用）。
两者系数与 `scipy.signal.butter(2, cutoff, btype="highpass", fs=16000, output="sos")` 一致，
滤波方式同 `scipy.signal.sosfilt`（Direct Form II transposed），不再依赖 scipy。
"""

import math

import numpy as np

from core.utils.logger import logger

SAMPLE_RATE = 16000
IMPLS = ("python", "rust")
_warned_fallback = False


def butter2_highpass_sos(cutoff_hz: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """2 阶 Butterworth 高通的二阶节系数 (1, 6)：[b0, b1, b2, 1, a1, a2]。

    双线性变换 + 预畸变，K = tan(π·fc/fs)：
    b = [1, -2, 1] / (1 + √2·K + K²)，a = [1, 2(K² - 1), 1 - √2·K + K²] / (1 + √2·K + K²)
    """
    k = math.tan(math.pi * cutoff_hz / sample_rate)
    norm = 1.0 / (1.0 + math.sqrt(2) * k + k * k)
    return np.array([[norm, -2.0 * norm, norm, 1.0, 2.0 * (k * k - 1.0) * norm, (1.0 - math.sqrt(2) * k + k * k) * norm]])


class HighPass:
    """纯 Python 版（备用）。"""

    def __init__(self, cutoff_hz: float, channels: int = 1, order: int = 2, sample_rate: int = SAMPLE_RATE):
        if order != 2:
            raise ValueError("只支持 2 阶")
        self.cutoff_hz = float(cutoff_hz)
        self.channels = channels
        self.sos = butter2_highpass_sos(self.cutoff_hz, sample_rate)
        # 从 0 开始（不按第一个采样预置），避免开头的直流偏置造成一下冲击
        self.zi = np.zeros((channels, 2))

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: (n,) 或 (n, channels) 的 float；返回同形状 float64。"""
        x = np.asarray(x, dtype=np.float64)
        if x.size == 0:
            return x
        b0, b1, b2, _, a1, a2 = (float(v) for v in self.sos[0])
        y = np.empty_like(x)
        cols = [(x, y)] if x.ndim == 1 else [(x[:, c], y[:, c]) for c in range(x.shape[1])]
        for c, (xc, yc) in enumerate(cols):
            z0, z1 = float(self.zi[c, 0]), float(self.zi[c, 1])
            out = []
            for v in xc.tolist():
                o = b0 * v + z0
                z0 = b1 * v - a1 * o + z1
                z1 = b2 * v - a2 * o
                out.append(o)
            yc[:] = out
            self.zi[c] = (z0, z1)
        return y

    def reset(self):
        self.zi[:] = 0


def make_highpass(cutoff_hz, channels: int = 1, impl: str | None = None):
    """cutoff_hz 为空 / 0 / 非法时返回 None（不滤波）。impl 缺省取 audio_input.highpass_impl（默认 rust）。"""
    global _warned_fallback
    try:
        cutoff = float(cutoff_hz or 0)
    except (TypeError, ValueError):
        return None
    if cutoff <= 0 or cutoff >= SAMPLE_RATE / 2:
        return None
    if impl is None:
        from core.utils.config import ConfigManager

        impl = ConfigManager.instance().get_app_config("audio_input.highpass_impl", "rust")
    impl = str(impl or "rust").strip().lower()
    if impl == "rust":
        try:
            from open_xiaoai_server import HighPassFilter

            return HighPassFilter(cutoff, channels=channels, sample_rate=SAMPLE_RATE)
        except ImportError:
            if not _warned_fallback:
                _warned_fallback = True
                logger.warning("原生模块里没有 HighPassFilter（需要重新编译），高通退回 Python 版", module="HighPass")
    elif impl != "python":
        logger.warning(f"未知的高通实现 {impl!r}，使用 python", module="HighPass")
    return HighPass(cutoff, channels=channels)
