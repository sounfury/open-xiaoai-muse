#!/bin/bash
# 运行中 bridge 各线程 CPU 占用：隔 N 秒读两次 /proc/<pid>/task/*/stat（utime+stime），
# 用 py-spy dump 把系统线程号对应到 Python 线程名（Python 3.12 不设系统线程名）。
#   bash scripts/thread_cpu.sh [秒数，默认 30]
set -e
PID=$(pgrep -f "python main.py" | head -n1)
[ -n "$PID" ] || { echo "bridge 没在跑"; exit 1; }
SEC=${1:-30}
HZ=$(getconf CLK_TCK)
PYSPY=$(command -v py-spy || echo "$HOME/.local/bin/py-spy")

# tid -> Python 线程名；py-spy dump 的行形如：Thread 0x7F... (idle): "multimic-conn"，加 --native 前还有 tid
: > /tmp/tcpu.names
if [ -x "$PYSPY" ]; then
  "$PYSPY" dump --pid "$PID" 2>/dev/null | sed -n 's/^Thread \(0x[0-9A-Fa-f]*\) .*"\(.*\)".*/\1 \2/p' > /tmp/tcpu.pyth || true
fi

snap() {
  for t in /proc/$PID/task/*; do
    tid=${t##*/}
    rest=$(sed 's/.*) //' "$t/stat" 2>/dev/null) || continue
    set -- $rest
    echo "$tid $((${12} + ${13}))"
  done
}
snap | sort > /tmp/tcpu.a
sleep "$SEC"
snap | sort > /tmp/tcpu.b

# py-spy 的 0x... 是 pthread id；用 --json 拿 os_thread_id 更可靠
if [ -x "$PYSPY" ]; then
  "$PYSPY" dump --pid "$PID" --json 2>/dev/null | python3 -c '
import json, sys
for t in json.load(sys.stdin):
    print(t.get("os_thread_id"), (t.get("thread_name") or "?").replace(" ", "_"))
' > /tmp/tcpu.names || true
fi

join /tmp/tcpu.a /tmp/tcpu.b | sort -k1,1 | join -a1 - <(sort -k1,1 /tmp/tcpu.names) | awk -v hz="$HZ" -v sec="$SEC" '
  { d = $3 - $2; name = (NF >= 4) ? $4 : "native-" $1;
    sub(/_[0-9]+$/, "_*", name); sub(/-[0-9]+$/, "-*", name);
    if (name ~ /^native-/) name = "native(Rust/onnxruntime 线程)";
    sum[name] += d; total += d }
  END { for (n in sum) if (sum[n] > 0) printf "%6.1f%%  %s\n", sum[n] / hz / sec * 100, n;
        printf "%6.1f%%  合计（单核百分比）\n", total / hz / sec * 100 }' | sort -rn
