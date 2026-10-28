#!/system/bin/sh
# Android 侧（root，su -M）管理 chroot 里的 bridge：
#   bridge-ctl.sh start | stop | restart | status | log [行数]
# start 起一个守护循环：挂载 chroot、持 wakelock、关 Wi-Fi 省电、放到全核 cpuset，bridge 退出后按退避重启。
TOOLS=/data/local/ubuntu-tools
ROOT=/data/local/ubuntu
APP=/opt/open-xiaoai-bridge
RUN=$TOOLS/run
SUP_PID=$RUN/supervisor.pid
BR_PID=$RUN/bridge.pid
ENABLED=$RUN/enabled
SUP_LOG=$RUN/supervisor.log
LOG=$ROOT$APP/logs/bridge-autostart.log
WAKELOCK=openxiaoai-bridge

mkdir -p "$RUN"

alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }
pid_of() { cat "$1" 2>/dev/null; }
note() { echo "$(date '+%F %T') $*" >> "$SUP_LOG"; }

# chroot 里正在跑的 main.py（守护循环之外启动的也算）
bridge_pids() {
  for p in /proc/[0-9]*; do
    [ "$(readlink "$p/root" 2>/dev/null)" = "$ROOT" ] || continue
    tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q "python main.py" && echo "${p#/proc/}"
  done
}

tune() {
  # Wi-Fi 省电会让持续的音频流抖动；重连后会被打开，所以守护循环里定期重设
  iw dev wlan0 set power_save off 2>/dev/null
}

supervise() {
  echo $$ > "$SUP_PID"
  echo $$ > /dev/cpuset/cgroup.procs 2>/dev/null   # 根 cpuset：0-7 全部核心（background 只有 0-5）
  echo "$WAKELOCK" > /sys/power/wake_lock
  sh "$TOOLS/chroot.sh" mount
  note "supervisor started pid=$$"
  fails=0
  while [ -f "$ENABLED" ]; do
    tune
    started=$(date +%s)
    sh "$TOOLS/chroot.sh" run "exec $APP/deploy/android-chroot/run-bridge.sh" </dev/null >/dev/null 2>&1 &
    echo $! > "$BR_PID"
    note "bridge started pid=$!"
    while alive "$(pid_of "$BR_PID")"; do
      sleep 15
      tune
    done
    [ -f "$ENABLED" ] || break
    ran=$(( $(date +%s) - started ))
    if [ "$ran" -lt 60 ]; then fails=$((fails + 1)); else fails=0; fi
    delay=$((3 * (1 << (fails > 6 ? 6 : fails))))
    [ "$delay" -gt 300 ] && delay=300
    note "bridge exited after ${ran}s, restart in ${delay}s (fails=$fails)"
    sleep "$delay"
  done
  echo "$WAKELOCK" > /sys/power/wake_unlock 2>/dev/null
  rm -f "$SUP_PID" "$BR_PID"
  note "supervisor stopped"
}

start() {
  if alive "$(pid_of "$SUP_PID")"; then
    echo "already running (supervisor $(pid_of "$SUP_PID"))"
    return 0
  fi
  touch "$ENABLED"
  # 脱离调用者（adb shell / ksud）独立运行
  nohup setsid sh "$0" __supervise </dev/null >/dev/null 2>&1 &
  sleep 1
  echo "started (supervisor $(pid_of "$SUP_PID"))"
}

stop() {
  rm -f "$ENABLED"
  pids=$(bridge_pids)
  if [ -n "$pids" ]; then
    kill -INT $pids 2>/dev/null   # main.py 只处理 SIGINT 做优雅退出
    i=0
    while [ $i -lt 10 ] && [ -n "$(bridge_pids)" ]; do sleep 1; i=$((i + 1)); done
    pids=$(bridge_pids)
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null && echo "force killed: $pids"
  fi
  sup=$(pid_of "$SUP_PID")
  alive "$sup" && kill "$sup" 2>/dev/null
  echo "$WAKELOCK" > /sys/power/wake_unlock 2>/dev/null
  rm -f "$SUP_PID" "$BR_PID"
  echo "stopped"
}

status() {
  sup=$(pid_of "$SUP_PID")
  if alive "$sup"; then echo "supervisor: running ($sup)"; else echo "supervisor: stopped"; fi
  echo "bridge pids: $(bridge_pids | tr '\n' ' ')"
  echo "wakelock: $(grep -o "$WAKELOCK" /sys/power/wake_lock 2>/dev/null || echo none)"
  echo "wifi: $(iw dev wlan0 get power_save 2>/dev/null)  ip: $(ip -4 addr show wlan0 | awk '/inet /{print $2}')"
  echo "listening:"; netstat -tln 2>/dev/null | grep -E ':(4399|4400|4401|9092) '
  echo "speaker connections:"; netstat -tn 2>/dev/null | grep -E ':(4399|4400|4401) ' | grep ESTABLISHED
  echo "--- supervisor.log"; tail -n 5 "$SUP_LOG" 2>/dev/null
}

case "$1" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  log) tail -n "${2:-50}" "$LOG" ;;
  __supervise) supervise ;;
  *) echo "usage: $0 start|stop|restart|status|log [n]" >&2; exit 2 ;;
esac
