"""唤醒检测是否真的多线程并行：4 个检测器串行跑 vs 4 线程同时跑同一段音频。

    .venv/bin/python scripts/kws_gil_bench.py --seconds 20

另测一个纯 Python 计算线程（模拟前端 numpy 计算）同时在跑时，唤醒检测慢了多少。
"""

import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MUSE_ENABLE", "1")

import numpy as np  # noqa: E402

from core.utils.onnx_runtime import prepare_onnxruntime  # noqa: E402

prepare_onnxruntime()

from core.services.audio.aec import SubbandEchoCanceller  # noqa: E402
from core.services.audio.kws.detector import FRAME_SAMPLES, KeywordDetector  # noqa: E402
from core.services.audio.kws.sherpa import SherpaOnnx  # noqa: E402
from core.services.audio.vad.silero import create_silero  # noqa: E402
from core.utils.config import ConfigManager  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from perf_bench import load_speech  # noqa: E402

SR = 16000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--n", type=int, default=4)
    args = parser.parse_args()
    cfg = ConfigManager.instance().get_app_config
    settings = {
        "vad_threshold": float(cfg("kws.vad_threshold", cfg("vad.threshold", 0.1)) or 0.1),
        "min_silence_ms": float(cfg("kws.min_silence_duration", 480)),
        "preroll_ms": float(cfg("kws.preroll_ms", 300)),
    }
    speech = load_speech(args.seconds)
    pcm = speech.astype("<i2").tobytes()
    step = FRAME_SAMPLES * 2
    SherpaOnnx.start()

    for gate in (True, False):
        def make():
            return KeywordDetector(SherpaOnnx.new_spotter(), create_silero(), **settings, vad_gate=gate)

        def feed(det):
            for s in range(0, len(pcm), step):
                det.process(pcm[s:s + step])

        dets = [make() for _ in range(args.n)]
        t = time.perf_counter()
        for d in dets:
            feed(d)
        serial = time.perf_counter() - t

        dets = [make() for _ in range(args.n)]
        threads = [threading.Thread(target=feed, args=(d,)) for d in dets]
        c0 = time.process_time()
        t = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        parallel = time.perf_counter() - t
        cpu = time.process_time() - c0

        # 同时有一个前端计算线程（回声消除，纯 numpy）一直在跑
        dets = [make() for _ in range(args.n)]
        stop = threading.Event()

        def front():
            aec = SubbandEchoCanceller()
            rng = np.random.default_rng(0)
            mic, ref = rng.standard_normal(1024), rng.standard_normal(1024)
            while not stop.is_set():
                aec.process(mic, ref)

        fth = threading.Thread(target=front, daemon=True)
        fth.start()
        threads = [threading.Thread(target=feed, args=(d,)) for d in dets]
        t = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        contended = time.perf_counter() - t
        stop.set()
        fth.join()

        print(f"vad_gate={gate}，{args.n} 个检测器，{args.seconds:.0f}s 音频：")
        print(f"  串行 {serial:.2f}s；{args.n} 线程并行 {parallel:.2f}s（加速 {serial / parallel:.2f}x，"
              f"进程 CPU {cpu / parallel * 100:.0f}%）；并行 + 一个 numpy 前端线程 {contended:.2f}s")


if __name__ == "__main__":
    main()
