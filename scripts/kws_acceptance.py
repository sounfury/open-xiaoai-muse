"""唤醒词验收：用和线上相同的检测逻辑（KeywordDetector + Sherpa + Silero）算命中率和误唤醒。

三种用法：

1) 合成样本（先生成：uv run --no-project --with edge-tts python scripts/kws_samples.py）

    .venv/Scripts/python.exe scripts/kws_acceptance.py synth
    .venv/Scripts/python.exe scripts/kws_acceptance.py synth --augment full --kt 缪斯=0.3 --paths 16

   每条样本按 --augment 加几种变化（音量、起点错位、电视背景声），分唤醒词统计命中率，
   并统计相近发音 / 日常说话 / 电视长段的误唤醒。默认多进程并行（-j），解码结果缓存在
   <data>/.pcm/；调参时加 --limit-voices 3 --augment quick 只要几十秒。

2) 一口气说"唤醒词，请求"和退出词（阶段 4：从唤醒词后面接着听、去唤醒词、退出词规则）

    .venv/Scripts/python.exe scripts/kws_acceptance.py dialog [--delays 300,1500] [-v]

   用真实的 GlobalStream / VAD（回放、预录）+ SenseVoice 识别，模拟唤醒后过一会儿（提示音）才开始听。

3) 真机录音（bridge 用 AUDIO_INPUT_DUMP=xxx.wav 录下来的单声道，或 ECHO_REF_DUMP 的双声道）

    .venv/Scripts/python.exe scripts/kws_acceptance.py wav 录音.wav --expect 20
    .venv/Scripts/python.exe scripts/kws_acceptance.py wav echo.wav --mic-ref   # 先过回声消除

   列出每次唤醒的时间和唤醒词；--expect 给出实际说了几遍，算命中率。

参数默认取 config.py（kws / vad 段），命令行可以覆盖，方便试参数后再写回 config。
退出码：synth 下每个唤醒词安静环境命中率达标（中文 --min-rate，英文 --min-rate-en）、
且相近发音 / 日常 / 电视长段的误唤醒 ≤ --max-false 时为 0。小声 + 电视背景那几档只报告不卡。
"""

import argparse
import json
import os
import random
import sys
import tempfile
import time
import wave
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MUSE_ENABLE", "1")  # 唤醒词列表里要有缪斯那一组

import numpy as np  # noqa: E402

from core.muse import normalize_wake_text  # noqa: E402
from core.services.audio.kws.detector import FRAME_SAMPLES, KeywordDetector  # noqa: E402
from core.services.audio.kws.keywords import generate_keywords_file, get_keyword_texts  # noqa: E402
from core.services.audio.kws.sherpa import SherpaKeywordSpotter, spotter_settings  # noqa: E402
from core.services.audio.vad.silero import create_silero  # noqa: E402
from core.utils.config import ConfigManager  # noqa: E402

SR = 16000
DEFAULT_DATA = ROOT / "tests" / "data" / "kws_tts"

# (音量 dB, 起点错位 ms, 背景声 SNR dB 或 None)
AUGMENTS = {
    "quick": [(0, 0, None), (-18, 160, 10)],
    "standard": [(g, o, n) for g in (0, -18) for o in (0, 160) for n in (None, 10)],
    "full": [(g, o, n) for g in (0, -12, -24) for o in (0, 100, 200) for n in (None, 15, 5)],
}
CLEAN = (0, 0, None)  # 每档 augment 都有：正常音量、不错位、无背景
PEAK_DBFS = -3.0  # 样本先归一到这个峰值，再按 gain 调（真机说话时接近满幅）
FLOOR_DBFS = -60.0  # 底噪（真机安静时峰值约 60～120）
LEAD_S, TAIL_S = 0.4, 0.6  # 前面留给 VAD / 预录，后面留给最后一个字解码


# ---- 配置 ----


def make_getter(args):
    """config.py 的取值，叠加命令行覆盖。"""
    base = ConfigManager.instance().get_app_config
    overrides = {}
    if args.threshold is not None:
        overrides["kws.keywords_threshold"] = args.threshold
    if args.score is not None:
        overrides["kws.keywords_score"] = args.score
    if args.paths is not None:
        overrides["kws.max_active_paths"] = args.paths
    if args.offsets is not None:
        overrides["kws.stream_offsets_ms"] = [int(x) for x in args.offsets.split(",") if x.strip()]
    if args.preroll is not None:
        overrides["kws.preroll_ms"] = args.preroll
    if args.no_vad_gate:
        overrides["kws.vad_gate"] = False
    for flag, key in ((args.kt, "kws.keyword_thresholds"), (args.ks, "kws.keyword_scores")):
        if flag:
            merged = dict(base(key, {}) or {})
            for item in flag:
                word, _, value = item.rpartition("=")
                merged[word] = float(value)
            overrides[key] = merged
    if args.keywords:
        overrides["wakeup.keywords"] = [w.strip() for w in args.keywords.split(",") if w.strip()]
        overrides["muse.kws_wake_words"] = []

    def get(path=None, default=None):
        if path in overrides:
            return overrides[path]
        return base(path, default)

    return get


class Bench:
    """一个检测器实例：每段音频前 reset，逐帧送进去。"""

    def __init__(self, getter):
        self.getter = getter
        self.texts = get_keyword_texts(getter)
        fd, self.keywords_file = tempfile.mkstemp(suffix=".txt", prefix="kws_")
        os.close(fd)
        if not generate_keywords_file(self.keywords_file, getter, self.texts):
            raise SystemExit("没有可用的唤醒词")
        self.lines = Path(self.keywords_file).read_text(encoding="utf8").splitlines()
        self.spotter_cfg = spotter_settings(getter)
        self.spotter = SherpaKeywordSpotter(self.keywords_file, **self.spotter_cfg)
        self.detector_cfg = {
            "vad_threshold": getter("vad.threshold", 0.10),
            "min_silence_ms": getter("kws.min_silence_duration", 480),
            "preroll_ms": getter("kws.preroll_ms", 300),
            "vad_gate": bool(getter("kws.vad_gate", True)),
        }
        self.detector = KeywordDetector(self.spotter, create_silero(), **self.detector_cfg)
        self.audio_s = 0.0
        self.cpu_s = 0.0

    def describe(self) -> str:
        return (
            f"唤醒词 {len(self.lines)} 个: {self.texts}\n"
            f"keywords.txt:\n  " + "\n  ".join(self.lines) + "\n"
            f"spotter: {self.spotter_cfg}\n detector: {self.detector_cfg}"
        )

    def run(self, audio: np.ndarray, reset: bool = True) -> list[tuple[float, str]]:
        """audio: int16。返回 [(命中时间秒, 唤醒词)]。"""
        if reset:
            self.detector.reset()
        hits = []
        n = len(audio) // FRAME_SAMPLES * FRAME_SAMPLES
        started = time.perf_counter()
        for i in range(0, n, FRAME_SAMPLES):
            result = self.detector.process(audio[i : i + FRAME_SAMPLES].tobytes())
            if result:
                hits.append(((i + FRAME_SAMPLES) / SR, result))
        self.cpu_s += time.perf_counter() - started
        self.audio_s += n / SR
        return hits

    def close(self):
        try:
            os.unlink(self.keywords_file)
        except OSError:
            pass


# ---- 音频 ----


def decode(path: Path) -> np.ndarray:
    """mp3 / wav → 16 kHz 单声道 int16（双声道 wav 原样返回 (n, 2)）。"""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as f:
            if f.getsampwidth() != 2:
                raise SystemExit(f"{path}: 只支持 16 位 wav")
            rate, channels = f.getframerate(), f.getnchannels()
            data = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")
        if rate != SR:
            raise SystemExit(f"{path}: 采样率 {rate}，需要 16000")
        return data.reshape(-1, channels) if channels > 1 else data
    import open_xiaoai_server

    pcm = open_xiaoai_server.decode_audio(path.read_bytes(), format=path.suffix.lstrip(".").lower(), sample_rate=SR)
    return np.frombuffer(pcm, dtype=np.int16)


def db(x: float) -> float:
    return 10 ** (x / 20)


def augment(clip: np.ndarray, gain_db, offset_ms, snr_db, babble: np.ndarray, rng) -> np.ndarray:
    x = clip.astype(np.float64)
    peak = np.max(np.abs(x)) or 1.0
    x = x / peak * 32767 * db(PEAK_DBFS + gain_db)
    lead = np.zeros(int((LEAD_S + offset_ms / 1000) * SR))
    tail = np.zeros(int(TAIL_S * SR))
    y = np.concatenate([lead, x, tail])
    if snr_db is not None and len(babble) > len(y):
        start = rng.randrange(0, len(babble) - len(y))
        noise = babble[start : start + len(y)].astype(np.float64)
        voiced = x[np.abs(x) > 0.05 * np.max(np.abs(x))] if np.any(x) else x
        p_sig = np.mean(voiced**2) if len(voiced) else 1.0
        p_noise = np.mean(noise**2) or 1.0
        y = y + noise * np.sqrt(p_sig / p_noise / db(snr_db) ** 2)
    y = y + rng_normal(rng, len(y)) * 32767 * db(FLOOR_DBFS)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def rng_normal(rng, n):
    return np.random.default_rng(rng.randrange(1 << 30)).standard_normal(n)


# ---- synth ----


def load_pcm(data_dir: Path, name: str) -> np.ndarray:
    """解码样本；结果缓存在 <data>/.pcm/ 下，第二次起不用再解码 mp3。"""
    cache = data_dir / ".pcm" / (name + ".npy")
    if cache.exists():
        return np.load(cache)
    audio = decode(data_dir / name)
    cache.parent.mkdir(exist_ok=True)
    np.save(cache, audio)
    return audio


_WORKER: dict = {}


def _worker_init(args, data_dir: str, tv_files: list[str]):
    data = Path(data_dir)
    _WORKER["bench"] = Bench(make_getter(args))
    _WORKER["data"] = data
    _WORKER["babble"] = (
        np.concatenate([load_pcm(data, f) for f in tv_files]) if tv_files else np.zeros(0, dtype=np.int16)
    )


def _worker_run(task):
    """一条样本 × 所有变化 → [(变化, 命中列表)]；电视长段不加变化。"""
    m, variants, seed = task
    bench, data = _WORKER["bench"], _WORKER["data"]
    clip = load_pcm(data, m["file"])
    rng = random.Random(f"{seed}/{m['file']}")
    if m["kind"] == "tv":
        audio = augment(clip, 0, 0, None, babble=np.zeros(0), rng=rng)
        return m, [(None, bench.run(audio), len(audio) / SR)], bench.cpu_s
    out = []
    for v in variants:
        audio = augment(clip, *v, babble=_WORKER["babble"], rng=rng)
        out.append((v, bench.run(audio), len(audio) / SR))
    return m, out, bench.cpu_s


def cmd_synth(args) -> int:
    data_dir = Path(args.data)
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"没有样本：{manifest_path}\n先运行 uv run --no-project --with edge-tts python scripts/kws_samples.py")
        return 2
    manifest = [m for m in json.loads(manifest_path.read_text(encoding="utf8")) if m["kind"] != "exit"]
    if args.limit_voices:
        voices = sorted({x["voice"] for x in manifest})
        keep = set(voices[: args.limit_voices]) | ({"en-US-JennyNeural"} & set(voices))
        manifest = [m for m in manifest if m["voice"] in keep]
    if args.only:
        manifest = [m for m in manifest if m["kind"] != "wake" or m["expect"] in set(args.only.split(","))]
    # 不在当前唤醒词表里的唤醒词（比如单独的"缪斯"）不算命中率
    configured = {normalize_wake_text(t) for t in get_keyword_texts(make_getter(args))}
    skipped = sorted({m["expect"] for m in manifest if m["kind"] == "wake" and normalize_wake_text(m["expect"]) not in configured})
    if skipped:
        print(f"不在唤醒词表里，跳过: {skipped}")
        manifest = [m for m in manifest if m["kind"] != "wake" or m["expect"] not in skipped]
    variants = AUGMENTS[args.augment]
    tv_files = [m["file"] for m in manifest if m["kind"] == "tv"]

    bench = Bench(make_getter(args))  # 只用来打印配置
    print(bench.describe())
    bench.close()
    for m in manifest:  # 先在主进程把缓存解码好
        load_pcm(data_dir, m["file"])

    wake = defaultdict(lambda: {"n": 0, "hit": 0, "exact": 0, "miss": []})
    word_variant = defaultdict(lambda: [0, 0])  # (唤醒词, 变化) -> [命中, 总数]
    by_variant = defaultdict(lambda: [0, 0])
    false_hits = defaultdict(list)
    counts = defaultdict(int)
    tv_hits, tv_seconds, audio_s = [], 0.0, 0.0

    tasks = [(m, variants, args.seed) for m in manifest]
    # 电视长段最长，先发出去
    tasks.sort(key=lambda t: t[0]["kind"] != "tv")
    jobs = max(1, args.jobs)
    started = time.perf_counter()
    if jobs == 1:
        _worker_init(args, str(data_dir), tv_files)
        results = map(_worker_run, tasks)
    else:
        import multiprocessing as mp

        pool = mp.get_context("spawn").Pool(jobs, _worker_init, (args, str(data_dir), tv_files))
        results = pool.imap_unordered(_worker_run, tasks, chunksize=2)

    done = 0
    for m, runs, cpu in results:
        done += 1
        if done % 50 == 0:
            print(f"  ... {done}/{len(tasks)}", flush=True)
        for v, hits, seconds in runs:
            audio_s += seconds
            if m["kind"] == "tv":
                tv_seconds += seconds
                tv_hits += [(m["text"][:12], m["voice"], f"{t:.1f}s", h) for t, h in hits]
                continue
            counts[m["kind"]] += 1
            if m["kind"] == "wake":
                stat = wake[m["expect"]]
                stat["n"] += 1
                by_variant[v][1] += 1
                word_variant[(m["expect"], v)][1] += 1
                if hits:
                    stat["hit"] += 1
                    by_variant[v][0] += 1
                    word_variant[(m["expect"], v)][0] += 1
                    if normalize_wake_text(hits[0][1]) == normalize_wake_text(m["expect"]):
                        stat["exact"] += 1
                else:
                    stat["miss"].append(f"{m['text']} / {m['voice']} {m['rate']} / {v}")
            elif hits:
                false_hits[m["kind"]].append((m["text"], m["voice"], v, [h for _, h in hits]))
    if jobs > 1:
        pool.close()
        pool.join()
    wall = time.perf_counter() - started

    print(
        "\n== 唤醒命中率（任一唤醒词命中都算唤醒；exact = 命中的正是这个词）==\n"
        f"   安静 = {CLEAN}，最难 = 命中率最低的一档；及格线只看安静：中文 ≥ {args.min_rate:.0%}，英文 ≥ {args.min_rate_en:.0%}"
    )
    failed = False
    for word, st in sorted(wake.items(), key=lambda kv: kv[0]):
        rates = {v: h / n for (w, v), (h, n) in word_variant.items() if w == word and n}
        clean = rates.get(CLEAN)
        worst_v = min(rates, key=rates.get) if rates else None
        need = args.min_rate_en if word.isascii() else args.min_rate
        bad = clean is not None and clean < need
        failed |= bad
        print(
            f"  {word:<11} 总体 {st['hit'] / st['n']:6.1%}  安静 {clean if clean is not None else 0:6.1%}"
            f"  最难 {rates.get(worst_v, 0):6.1%} {worst_v}  exact {st['exact'] / st['n']:6.1%}"
            f"  ({st['hit']}/{st['n']}){'  ✗' if bad else ''}"
        )
    print("\n== 按变化（音量 dB, 起点错位 ms, 背景 SNR dB）==")
    for v, (h, n) in sorted(by_variant.items(), key=lambda kv: str(kv[0])):
        print(f"  {str(v):<20} {h / n:6.1%} ({h}/{n})")
    if args.verbose:
        for word, s in wake.items():
            for miss in s["miss"]:
                print(f"  漏: {miss}")

    false_total = 0
    for kind, label in (("negative", "相近发音 / 日常"), ("name", "句中提到缪斯")):
        items = false_hits.get(kind, [])
        print(f"\n== 误唤醒：{label}  {len(items)}/{counts[kind]} ==")
        for text, voice, v, hs in items if args.verbose else items[:12]:
            print(f"  {text} / {voice} / {v} → {hs}")
        if kind == "negative":
            false_total += len(items)
    per_hour = len(tv_hits) / tv_seconds * 3600 if tv_seconds else 0
    print(f"\n== 误唤醒：电视长段  {len(tv_hits)} 次 / {tv_seconds / 60:.1f} 分钟（约 {per_hour:.1f} 次/小时）==")
    for item in tv_hits:
        print(f"  {item}")
    false_total += len(tv_hits)

    print(f"\n处理 {audio_s / 60:.1f} 分钟音频，{jobs} 个进程，用时 {wall:.1f}s")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "wake": {k: {kk: vv for kk, vv in s.items() if kk != "miss"} for k, s in wake.items()},
            "false_negative": len(false_hits.get("negative", [])),
            "false_name": len(false_hits.get("name", [])),
            "tv_hits": len(tv_hits), "tv_seconds": tv_seconds,
        }, ensure_ascii=False, indent=1), encoding="utf8")
    ok = not failed and false_total <= args.max_false
    print("\n结果：" + ("通过" if ok else "未通过") + f"（安静环境命中率达标，误唤醒 ≤ {args.max_false}）")
    return 0 if ok else 1


# ---- wav ----


def cmd_wav(args) -> int:
    bench = Bench(make_getter(args))
    print(bench.describe())
    total_hits, total_s = 0, 0.0
    for name in args.files:
        audio = decode(Path(name))
        if audio.ndim == 2:
            if not args.mic_ref:
                print(f"{name}: 双声道，按 [麦克风, 回采] 处理请加 --mic-ref；先只取第 1 声道")
                audio = audio[:, 0]
            else:
                audio = cancel_echo(audio, ConfigManager.instance().get_app_config)
        total_s += len(audio) / SR
        hits = bench.run(audio)
        total_hits += len(hits)
        print(f"\n{name}: {len(audio) / SR:.1f}s，唤醒 {len(hits)} 次，峰值 {20 * np.log10(max(1, np.max(np.abs(audio))) / 32768):.1f} dBFS")
        for t, h in hits:
            print(f"  {int(t // 60):02d}:{t % 60:05.2f}  {h}")
    if args.expect:
        print(f"\n命中率 {total_hits}/{args.expect} = {total_hits / args.expect:.1%}")
    elif total_s:
        print(f"\n共 {total_hits} 次 / {total_s / 60:.1f} 分钟（约 {total_hits / total_s * 3600:.1f} 次/小时）")
    bench.close()
    return 0


def cancel_echo(frames: np.ndarray, getter) -> np.ndarray:
    """和线上一样：[麦克风, 回采] 过回声消除，乘 echo_mic_gain 和 audio_input.gain。"""
    from core.services.audio.aec import SubbandEchoCanceller

    barge = getter("barge_in", {}) or {}
    settings = barge.get("echo_canceller") or {}
    echo = SubbandEchoCanceller(taps=int(settings.get("taps", 8)), tau_s=float(settings.get("tau_s", 3.0)))
    gain = float(barge.get("echo_mic_gain", 64)) * float(getter("audio_input.gain", 1.0))
    out = []
    for i in range(0, len(frames), 1024):  # 线上一包约 1024 采样
        chunk = frames[i : i + 1024]
        out.append(echo.process(chunk[:, 0], chunk[:, 1]) * gain)
    return np.clip(np.round(np.concatenate(out)), -32768, 32767).astype(np.int16)


# ---- dialog ----


class DialogSim:
    """离线模拟一轮外部后端对话的\"听\"：真实的 GlobalStream / VAD.resume（回放、预录、
    开口后接着读）+ SenseVoice 识别 + 去唤醒词，和 ExternalConversationController 的做法一致。"""

    def __init__(self, getter):
        from core.services.audio.stream import GlobalStream
        from core.services.audio.vad import VAD

        self.getter = getter
        self.gs = GlobalStream
        self.vad = VAD
        self.vad.apply_runtime_config()
        self.vad._initialize_audio_stream()
        self.vad.stream.stop_stream()
        self.vad.paused = True
        self.feed_pos = 0
        self.audio = np.zeros(0, dtype=np.int16)
        self.base = 0

    def load(self, audio: np.ndarray):
        """新的一段音频：之后的位置都相对 GlobalStream 当前的绝对位置。"""
        self.audio = audio
        self.base = self.gs.samples
        self.feed_pos = 0

    def feed_until(self, sample: int):
        """模拟音箱实时送音频：每包 1024 采样，送到 sample 为止。"""
        while self.feed_pos < min(sample, len(self.audio)):
            end = min(self.feed_pos + 1024, len(self.audio))
            self.gs.input(self.audio[self.feed_pos : end].tobytes())
            self.feed_pos = end

    def listen(self, preroll_from=None, replay_from=None) -> bytes | None:
        """同 _wait_for_speech：开口 → 接着录到说完。preroll/replay 是相对本段音频的位置。"""
        from core.services.audio.vad.silero import Silero
        from core.wakeup_session import EventManager

        vad = self.vad
        state = {"speech": None, "done": False}
        recording: list[bytes] = []

        def on_speech(buf):
            recording.append(buf)
            state["speech"] = True
            vad.resume("silence")

        def on_silence():
            state["done"] = True

        orig = EventManager.on_speech, EventManager.on_silence
        EventManager.on_speech, EventManager.on_silence = on_speech, on_silence
        try:
            to_abs = lambda x: None if x is None else self.base + x  # noqa: E731
            vad.resume("speech", preroll_from=to_abs(preroll_from), replay_from=to_abs(replay_from))
            while not state["done"]:
                frames = vad.stream.read(vad.frame_size)
                if len(frames) != vad.frame_size * 2:
                    if self.feed_pos >= len(self.audio):
                        break
                    self.feed_until(self.feed_pos + 1024)
                    continue
                position = vad.stream.position() - vad.frame_size
                is_speech = (Silero.vad(frames, SR) or 0) >= vad._speech_threshold(position)
                if state["speech"]:
                    recording.append(frames)
                if is_speech:
                    vad._handle_speech_frame(frames)
                else:
                    vad._handle_silence_frame(frames)
        finally:
            EventManager.on_speech, EventManager.on_silence = orig
            vad.pause()
        return b"".join(recording) if state["speech"] else None


def _residue(text: str, expect: str) -> str:
    """识别结果开头多出来、原请求里没有的字（多半是唤醒词尾音，如"司开灯"的"司"）。"""
    import difflib

    def norm(t):
        return "".join(ch for ch in normalize_wake_text(t) if ch.isalnum())

    a, b = norm(text), norm(expect)
    match = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return a[: match.a] if match.size and match.b == 0 else ""


def _similar(a: str, b: str) -> float:
    import difflib

    def norm(t):
        return "".join(ch for ch in normalize_wake_text(t) if ch.isalnum())

    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def cmd_dialog(args) -> int:
    from core.exit_words import DEFAULT_EXIT_WORDS, match_exit_intent
    from core.muse import load_settings, match_settings_text, strip_wake_word_tail
    from core.services.audio.asr import ASRService

    data_dir = Path(args.data)
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf8"))
    if args.limit_voices:
        voices = sorted({x["voice"] for x in manifest if x["voice"].startswith("zh")})
        keep = set(voices[: args.limit_voices])
        manifest = [m for m in manifest if m["voice"] in keep]
    getter = make_getter(args)
    settings = load_settings(getter)
    wake_words = settings.wake_words
    names = [w for w in (getter("wakeup.keywords", []) or []) if isinstance(w, str)] + wake_words
    exit_words = getter("hermes.exit_keywords", None)
    exit_words = list(DEFAULT_EXIT_WORDS) if exit_words is None else exit_words
    tail_ms = args.tail if args.tail is not None else getter("conversation.keyword_tail_ms", 100)
    lookback_ms = args.lookback if args.lookback is not None else getter("barge_in.speech_lookback_ms", 350)
    tail, lookback = int(tail_ms) * 16, int(lookback_ms) * 16
    print(f"从唤醒词命中位置往回 {tail_ms} ms 开始检测说话，往回 {lookback_ms} ms 开始补录")
    delays = [int(x) for x in args.delays.split(",")]

    bench = Bench(getter)
    configured = {normalize_wake_text(t) for t in bench.texts}
    sim = DialogSim(getter)
    rng = random.Random(args.seed)
    ASRService.ensure_loaded()
    failed = 0

    def strip(text):
        match = match_settings_text(text, settings)
        return match.content if match else strip_wake_word_tail(text, names)

    # 1) 一口气说\"唤醒词，请求\"：请求不丢
    items = []
    for m in manifest:
        parsed = match_settings_text(m["text"], settings) if m["kind"] == "wake" and m["rate"] == "+0%" else None
        if parsed and parsed.content and normalize_wake_text(m["expect"]) in configured:
            items.append(m)
    print(f"\n== 一口气说完：唤醒后 {delays} ms 才开始听（提示音等），请求要完整 ==")
    ok = total = 0
    for m in items:
        expect = match_settings_text(m["text"], settings).content
        audio = augment(load_pcm(data_dir, m["file"]), 0, 0, None, babble=np.zeros(0), rng=rng)
        audio = np.concatenate([audio, np.zeros(SR, dtype=np.int16)])
        hits = bench.run(audio)
        for delay in delays:
            total += 1
            if not hits:
                print(f"  ✗ 没唤醒: {m['text']} / {m['voice']}")
                continue
            pos = int(round(hits[0][0] * SR))
            sim.load(audio)
            sim.feed_until(pos + delay * 16)
            speech = sim.listen(max(0, pos - max(lookback, tail)), max(0, pos - tail))
            heard = ASRService.asr(speech, sample_rate=SR) if speech else ""
            text = strip(heard)
            score = _similar(text, expect)
            residue = _residue(text, expect)
            good = score >= args.min_similar and not residue
            ok += good
            if not good or args.verbose:
                print(f"  {'✓' if good else '✗'} {m['text']} / {m['voice']} / +{delay}ms → 识别 {heard!r} → {text!r} ({score:.2f})")
    print(f"  通过 {ok}/{total}")
    failed += total - ok

    # 2) 退出词：识别后判断
    items = [m for m in manifest if m["kind"] == "exit"]
    print("\n== 退出词：识别结果按 core/exit_words.py 判断 ==")
    ok = 0
    for m in items:
        audio = augment(load_pcm(data_dir, m["file"]), 0, 0, None, babble=np.zeros(0), rng=rng)
        heard = ASRService.asr(audio.tobytes(), sample_rate=SR)
        intent, word = match_exit_intent(heard, exit_words, names)
        got = intent or "none"
        good = got == m["expect"]
        ok += good
        if not good or args.verbose:
            print(f"  {'✓' if good else '✗'} {m['text']} / {m['voice']} → 识别 {heard!r} → {got}（期望 {m['expect']}）")
    print(f"  通过 {ok}/{len(items)}")
    failed += len(items) - ok
    bench.close()
    print("\n结果：" + ("通过" if not failed else f"未通过（{failed} 条）"))
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("synth", "wav", "dialog"):
        p = sub.add_parser(name)
        p.add_argument("--threshold", type=float, help="kws.keywords_threshold")
        p.add_argument("--score", type=float, help="kws.keywords_score")
        p.add_argument("--paths", type=int, help="kws.max_active_paths")
        p.add_argument("--offsets", help="kws.stream_offsets_ms，如 0,160")
        p.add_argument("--preroll", type=float, help="kws.preroll_ms")
        p.add_argument("--no-vad-gate", action="store_true", help="每帧都送进模型")
        p.add_argument("--kt", action="append", help="单个唤醒词阈值，如 嗨缪斯=0.05（可重复）")
        p.add_argument("--ks", action="append", help="单个唤醒词加权，如 嗨缪斯=3.0（可重复）")
        p.add_argument("--keywords", help="只用这些唤醒词（逗号分隔），覆盖 config")
    synth = sub.choices["synth"]
    synth.add_argument("--data", default=str(DEFAULT_DATA))
    synth.add_argument("--augment", choices=sorted(AUGMENTS), default="standard")
    synth.add_argument("--seed", type=int, default=7)
    synth.add_argument("--limit-voices", type=int, help="只用前 N 个中文语音（快速试参数）")
    synth.add_argument("--only", help="只测这些唤醒词的样本（逗号分隔，负样本照测）")
    synth.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2), help="并行进程数")
    synth.add_argument("--min-rate", type=float, default=0.9, help="中文唤醒词安静环境命中率及格线")
    synth.add_argument("--min-rate-en", type=float, default=0.8, help="英文唤醒词安静环境命中率及格线")
    synth.add_argument("--max-false", type=int, default=0)
    synth.add_argument("--json", help="结果另存为 json")
    synth.add_argument("-v", "--verbose", action="store_true")
    dialog = sub.choices["dialog"]
    dialog.add_argument("--data", default=str(DEFAULT_DATA))
    dialog.add_argument("--delays", default="300,1500", help="唤醒后过多久才开始听（ms，逗号分隔）")
    dialog.add_argument("--limit-voices", type=int, help="只用前 N 个中文语音")
    dialog.add_argument("--min-similar", type=float, default=0.6, help="去掉唤醒词后和原请求的相似度及格线")
    dialog.add_argument("--seed", type=int, default=7)
    dialog.add_argument("--lookback", type=int, help="覆盖 barge_in.speech_lookback_ms")
    dialog.add_argument("--tail", type=int, help="覆盖 conversation.keyword_tail_ms")
    dialog.add_argument("-v", "--verbose", action="store_true")
    wav = sub.choices["wav"]
    wav.add_argument("files", nargs="+")
    wav.add_argument("--expect", type=int, help="录音里实际说了几遍唤醒词")
    wav.add_argument("--mic-ref", action="store_true", help="双声道 [麦克风, 回采]：先过回声消除")
    args = parser.parse_args()
    return {"synth": cmd_synth, "wav": cmd_wav, "dialog": cmd_dialog}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
