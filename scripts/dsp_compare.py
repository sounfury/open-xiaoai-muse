"""音频前端 Python 版 vs Rust 版逐样本对比（回声消除、高通），并比较耗时。

    .venv/bin/python scripts/dsp_compare.py                     # 合成数据（tests/data/kws_tts 样本）
    .venv/bin/python scripts/dsp_compare.py --wav rec.wav       # 再加一段真实录音：
        # 2 声道 [麦克风, 回采]（ECHO_REF_DUMP）或 4 声道 [3 麦, 回采]（multimic_record.py）

通过标准（docs/rust-refactor-todo.md 第 1、2 步）：
  - 回声消除输出相对 RMS 误差 ≤ 1e-6，near_end_ratio_db / reduction_db 一致（差 < 1e-6 dB）
  - 高通与 scipy.signal.sosfilt（如果装了）和 Python 版相对误差 ≤ 1e-9
"""

import argparse
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from core.services.audio.aec import SubbandEchoCanceller  # noqa: E402
from core.services.audio.highpass import HighPass  # noqa: E402

SR = 16000
PCM_DIR = ROOT / "tests" / "data" / "kws_tts" / ".pcm"
failures: list[str] = []


def rel_err(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denom = np.sqrt(np.mean(b * b)) or 1.0
    return float(np.sqrt(np.mean((a - b) ** 2)) / denom)


def check(name: str, ok: bool, detail: str):
    print(f"  [{'通过' if ok else '失败'}] {name}：{detail}")
    if not ok:
        failures.append(name)


def load_samples(seconds: float, offset: int = 0) -> np.ndarray:
    files = sorted(PCM_DIR.glob("*.npy"))
    if not files:
        raise SystemExit(f"没有样本缓存：{PCM_DIR}")
    parts, total, i = [], 0, offset
    while total < seconds * SR:
        a = np.load(files[i % len(files)]).astype(np.float64).reshape(-1)
        parts += [a, np.zeros(SR // 4)]
        total += len(a) + SR // 4
        i += 1
    return np.concatenate(parts)[: int(seconds * SR)]


def synth(seconds: float, seed: int = 0):
    """近端人声 + 断续播放的回声 + 底噪；返回 (mic, ref)，电平和线上主麦双声道接近。"""
    rng = np.random.default_rng(seed)
    near = load_samples(seconds, 0) / 64
    play = load_samples(seconds, 37)
    # 播放 4 秒、停 2 秒交替；前 1.5 秒不播（覆盖 ref 从无到有和预热）
    gate = np.zeros(len(play))
    t = int(1.5 * SR)
    while t < len(play):
        gate[t : t + 4 * SR] = 1
        t += 6 * SR
    ref = np.round(play * gate)
    rir = rng.standard_normal(300) * np.exp(-np.arange(300) / 60) * 0.05
    rir[40] += 0.3
    echo = np.convolve(ref, rir)[: len(ref)]
    mic = near + echo + rng.standard_normal(len(ref)) * 1.5
    return mic, ref


def chunks(n: int, mode: str, seed: int = 1):
    rng = np.random.default_rng(seed)
    s = 0
    while s < n:
        size = {"1440": 1440, "1024": 1024, "random": int(rng.integers(1, 3000))}[mode]
        yield s, min(n, s + size)
        s += size


def compare_aec(mic, ref, label: str, rust_cls):
    print(f"\n回声消除：{label}（{len(mic) / SR:.1f}s）")
    for mode in ("1440", "1024", "random"):
        py, rs = SubbandEchoCanceller(), rust_cls()
        out_py, out_rs, t_py, t_rs = [], [], 0.0, 0.0
        ne_diffs, red_diffs, ne_count = [], [], 0
        for k, (a, b) in enumerate(chunks(len(mic), mode)):
            t = time.perf_counter()
            out_py.append(py.process(mic[a:b], ref[a:b]))
            t_py += time.perf_counter() - t
            t = time.perf_counter()
            out_rs.append(rs.process(mic[a:b], ref[a:b]))
            t_rs += time.perf_counter() - t
            if k % 7 == 0:
                ne_p, ne_r = py.near_end_ratio_db(), rs.near_end_ratio_db()
                if (ne_p is None) != (ne_r is None):
                    ne_diffs.append(np.inf)
                elif ne_p is not None:
                    ne_diffs.append(abs(ne_p - ne_r))
                    ne_count += 1
                rp, rr = py.reduction_db(), rs.reduction_db()
                if (rp is None) != (rr is None):
                    red_diffs.append(np.inf)
                elif rp is not None:
                    red_diffs.append(abs(rp - rr))
        out_py, out_rs = np.concatenate(out_py), np.concatenate(out_rs)
        err = rel_err(out_rs, out_py)
        check(f"[{mode}] 输出", err <= 1e-6 and len(out_py) == len(out_rs) == len(mic),
              f"相对 RMS 误差 {err:.2e}，最大绝对误差 {np.max(np.abs(out_rs - out_py)):.2e}")
        ne = max(ne_diffs) if ne_diffs else 0.0
        check(f"[{mode}] near_end_ratio_db", ne < 1e-6, f"{ne_count} 次比较，最大差 {ne:.2e} dB")
        rd = max(red_diffs) if red_diffs else 0.0
        check(f"[{mode}] reduction_db", rd < 1e-6, f"最大差 {rd:.2e} dB；最终 {py.reduction_db()} / {rs.reduction_db()}")
        check(f"[{mode}] 状态", (py.ref_active, py.frames, py.recent_end) == (rs.ref_active, rs.frames, rs.recent_end),
              f"ref_active/frames/recent_end = {(py.ref_active, py.frames, py.recent_end)} / "
              f"{(rs.ref_active, rs.frames, rs.recent_end)}")
        audio_s = len(mic) / SR
        print(f"         耗时：Python {t_py / audio_s * 100:.2f}% 单核，Rust {t_rs / audio_s * 100:.2f}%"
              f"（快 {t_py / max(t_rs, 1e-9):.1f} 倍）")
    # int16 跨步视图（xiaoai.py 传的是 frames[:, 0]）
    frames = np.stack([np.clip(mic * 64, -32768, 32767), np.clip(ref, -32768, 32767)], axis=1).astype("<i2")
    py, rs = SubbandEchoCanceller(), rust_cls()
    a = np.concatenate([py.process(frames[s:e, 0], frames[s:e, 1]) for s, e in chunks(len(frames), "1440")])
    b = np.concatenate([rs.process(frames[s:e, 0], frames[s:e, 1]) for s, e in chunks(len(frames), "1440")])
    err = rel_err(b, a)
    check("int16 跨步输入", err <= 1e-6, f"相对 RMS 误差 {err:.2e}")


def compare_highpass(x3: np.ndarray, rust_cls):
    print(f"\n高通 120 Hz（{len(x3) / SR:.1f}s）")
    try:
        from scipy.signal import butter, sosfilt
    except ImportError:
        butter = sosfilt = None
    for channels in (1, 3):
        x = x3[:, 0] if channels == 1 else x3
        rs = rust_cls(120.0, channels=channels)
        py = HighPass(120.0, channels=channels)
        sos_err = float(np.max(np.abs(np.asarray(rs.sos) - py.sos)))
        out_rs, out_py, t_rs, t_py = [], [], 0.0, 0.0
        for a, b in chunks(len(x), "random"):
            t = time.perf_counter()
            out_rs.append(rs.process(x[a:b]))
            t_rs += time.perf_counter() - t
            t = time.perf_counter()
            out_py.append(py.process(x[a:b]))
            t_py += time.perf_counter() - t
        out_rs, out_py = np.concatenate(out_rs), np.concatenate(out_py)
        check(f"{channels} 声道 Rust vs Python", out_rs.shape == x.shape and rel_err(out_rs, out_py) <= 1e-9,
              f"相对误差 {rel_err(out_rs, out_py):.2e}，系数差 {sos_err:.1e}")
        if sosfilt is not None:
            sos = butter(2, 120, btype="highpass", fs=SR, output="sos")
            zi = np.zeros((1, 2, channels)) if channels > 1 else np.zeros((1, 2))
            ref_out = []
            for a, b in chunks(len(x), "random"):
                y, zi = sosfilt(sos, x[a:b], axis=0, zi=zi)
                ref_out.append(y)
            ref_out = np.concatenate(ref_out)
            check(f"{channels} 声道 Rust vs scipy", rel_err(out_rs, ref_out) <= 1e-9,
                  f"相对误差 {rel_err(out_rs, ref_out):.2e}，系数差 {float(np.max(np.abs(np.asarray(rs.sos) - sos))):.1e}")
        audio_s = len(x) / SR
        print(f"         耗时：Python 备用版 {t_py / audio_s * 100:.2f}% 单核，Rust {t_rs / audio_s * 100:.3f}%")
    rs = rust_cls(120.0, channels=3)
    empty = rs.process(np.zeros((0, 3)))
    check("空输入", empty.shape == (0, 3), f"shape {empty.shape}")
    rs.process(x3[:1000])
    rs.reset()
    fresh = rust_cls(120.0, channels=3)
    check("reset", np.array_equal(rs.process(x3[:500]), fresh.process(x3[:500])), "reset 后与新建的一致")


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as f:
        if f.getsampwidth() != 2 or f.getframerate() != SR:
            raise SystemExit(f"{path}：需要 16 kHz S16")
        data = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")
        return data.reshape(-1, f.getnchannels()).astype(np.float64)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--wav", action="append", default=[], help="真实录音（可多次）")
    args = parser.parse_args()
    try:
        from open_xiaoai_server import EchoCanceller, HighPassFilter
    except ImportError as exc:
        raise SystemExit(f"原生模块里没有 Rust DSP（先重新编译安装）：{exc}")

    mic, ref = synth(args.seconds)
    compare_aec(mic, ref, "合成（近端人声 + 断续播放回声 + 底噪）", EchoCanceller)
    for path in args.wav:
        data = read_wav(path)
        if data.shape[1] == 2:
            compare_aec(data[:, 0], data[:, 1], f"{path} [麦克风, 回采]", EchoCanceller)
        elif data.shape[1] == 4:
            for c in range(3):
                compare_aec(data[:, c], data[:, 3], f"{path} 麦克风 {c}", EchoCanceller)
        else:
            print(f"跳过 {path}：{data.shape[1]} 声道")

    rng = np.random.default_rng(3)
    x3 = np.stack([load_samples(args.seconds, k * 11) + 200 + rng.standard_normal(int(args.seconds * SR)) * 50
                   for k in range(3)], axis=1)
    compare_highpass(x3, HighPassFilter)

    print(f"\n{'全部通过' if not failures else '失败：' + '、'.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
