"""唤醒模型单路成本：不同 stream_offsets_ms / max_active_paths 下，一直说话时每 32 ms 要算多久。

    .venv/bin/python scripts/kws_cost_bench.py --seconds 20
    .venv/bin/python scripts/kws_cost_bench.py --model-dir core/models/其他唤醒模型   # 换模型对比

不经过 VAD 门控（相当于一直有人说话的最坏情况），单线程。
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MUSE_ENABLE", "1")

from core.utils.onnx_runtime import prepare_onnxruntime  # noqa: E402

prepare_onnxruntime()

import sherpa_onnx  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from perf_bench import load_speech  # noqa: E402

from core.services.audio.kws.sherpa import SherpaKeywordSpotter  # noqa: E402
from core.utils.file import get_model_file_path  # noqa: E402

SR = 16000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--model-dir", default=None, help="换一套 encoder/decoder/joiner/tokens（默认 core/models）")
    parser.add_argument("--keywords", default=None, help="keywords.txt（默认 core/models/keywords.txt）")
    args = parser.parse_args()
    speech = load_speech(args.seconds)
    pcm = speech.astype("<i2").tobytes()
    step = 512 * 2
    keywords = args.keywords or get_model_file_path("keywords.txt")

    def make(offsets, paths):
        if args.model_dir is None:
            return SherpaKeywordSpotter(keywords, max_active_paths=paths, stream_offsets_ms=offsets)
        d = Path(args.model_dir)
        pick = lambda stem: str(next(iter(sorted(d.glob(f"{stem}*.int8.onnx")) or sorted(d.glob(f"{stem}*.onnx")))))  # noqa: E731
        spotter = object.__new__(SherpaKeywordSpotter)
        spotter.keyword_spotter = sherpa_onnx.KeywordSpotter(
            provider="cpu", num_threads=1, max_active_paths=paths, keywords_score=2.0, keywords_threshold=0.2,
            num_trailing_blanks=0, keywords_file=keywords, tokens=str(d / "tokens.txt"),
            encoder=pick("encoder"), decoder=pick("decoder"), joiner=pick("joiner"),
        )
        from core.services.audio.kws.sherpa import normalize_offsets

        spotter.offsets = normalize_offsets(offsets)
        spotter.reset()
        return spotter

    print(f"{args.seconds:.0f}s 连续人声，单线程、不门控；每 32 ms 音频的耗时：")
    for offsets in ([0], [0, 160], [0, 80, 160, 240]):
        for paths in (4, 8, 16):
            spotter = make(offsets, paths)
            hits = 0
            t = time.perf_counter()
            for s in range(0, len(pcm), step):
                if spotter.kws(pcm[s:s + step]):
                    hits += 1
            cost = time.perf_counter() - t
            print(f"  offsets={str(offsets):18s} paths={paths:2d}：每帧 {cost / (len(pcm) / step) * 1000:5.2f} ms，"
                  f"单核 {cost / args.seconds * 100:5.1f}%，命中 {hits}")


if __name__ == "__main__":
    main()
