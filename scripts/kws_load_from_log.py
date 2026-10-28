"""从 bridge 日志的诊断周期汇总里统计唤醒检测的负载（每分钟一条）。

    python3 scripts/kws_load_from_log.py logs/bridge-autostart.log [--hours 48]

每路 kws.<name> 的 avg_ms × n 就是这一分钟花在这一路（VAD + 唤醒模型）上的时间；
按"门控常开时每帧约 7.2 ms、关着时约 0.9 ms（只有 VAD）"粗估门控打开的比例。
"""

import argparse
import json
import re
import statistics
from datetime import datetime, timedelta

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*\[Diagnostics\] 周期汇总 (\{.*\})\s*$")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("log")
    p.add_argument("--hours", type=float, default=48)
    p.add_argument("--open-ms", type=float, default=7.2, help="门控常开时每帧耗时（kws_cost_bench 测得）")
    p.add_argument("--closed-ms", type=float, default=0.9, help="门控关着时每帧耗时（只跑 VAD）")
    args = p.parse_args()
    rows = []
    with open(args.log, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line)
            if not m:
                continue
            try:
                data = json.loads(m.group(2))
            except ValueError:
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            timings = data.get("timings", {})
            window = float(data.get("window_s") or 60)
            per = {}
            for name, v in timings.items():
                if name.startswith("kws.") and not name.startswith("kws.wait"):
                    per[name[4:]] = v["avg_ms"] * v["n"] / 1000 / window  # 占单核比例
            if per:
                rows.append((t, data.get("cpu_core_pct"), per))
    if not rows:
        raise SystemExit("日志里没有诊断汇总")
    end = rows[-1][0]
    rows = [r for r in rows if r[0] >= end - timedelta(hours=args.hours)]
    names = sorted({n for _, _, per in rows for n in per})
    print(f"{rows[0][0]} ～ {end}，{len(rows)} 分钟")
    cpu = [c for _, c, _ in rows if c is not None]
    print(f"进程 CPU（单核%）：中位 {statistics.median(cpu):.0f}，平均 {statistics.mean(cpu):.0f}，p90 {sorted(cpu)[int(len(cpu) * 0.9)]:.0f}")
    frame_s = 0.032
    for n in names:
        vals = [per.get(n, 0) for _, _, per in rows]
        load = statistics.mean(vals)
        # load = open_ratio * open + (1-open_ratio) * closed（每帧 ms / 32 ms）
        o, c = args.open_ms / 1000 / frame_s, args.closed_ms / 1000 / frame_s
        ratio = min(1, max(0, (load - c) / (o - c)))
        busy = sum(1 for v in vals if v > (o + c) / 2) / len(vals)
        print(f"  {n:5s} 平均单核 {load * 100:5.1f}%，估计门控打开 {ratio:5.1%}；超过一半时间在跑模型的分钟 {busy:5.1%}")
    hours = {}
    for t, _, per in rows:
        hours.setdefault(t.hour, []).append(sum(per.values()))
    print("按小时（4 路合计单核%）：" + "  ".join(f"{h:02d}时 {statistics.mean(v) * 100:.0f}" for h, v in sorted(hours.items())))


if __name__ == "__main__":
    main()
