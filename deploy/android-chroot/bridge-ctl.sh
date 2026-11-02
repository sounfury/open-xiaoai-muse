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
# 绕开 VPN 的目标：Hermes / MusicDuck 所在的 Oracle、chroot resolv.conf 里的两个 DNS
BYPASS_HOSTS="132.226.76.2 223.5.5.5 119.29.29.29"
# Oracle 的 IPv4 偶尔被墙，IPv6 不会；FlClash 关了 IPv6，这条路由不加 chroot 连不上 v6
BYPASS_HOSTS6="2603:c020:25:8000:0:2990:73c1:ee6b"
BYPASS_PRIO=9000   # 排在 netd 的规则之前（netd 从 9999 起）

# 锁放 tmpfs；PID 记录保留原路径，但必须同时验证本次开机和进程启动时刻。
LOCKDIR=/dev/open-xiaoai-bridge
mkdir -p "$RUN" "$LOCKDIR"

pid_of() { local pid rest; read -r pid rest 2>/dev/null < "$1" && echo "$pid"; }
note() { echo "$(date '+%F %T') $*" >> "$SUP_LOG"; }

# stat 的 comm 可以包含空格和括号，先去掉最后一个 ") " 之前的内容。
process_start() (
  case "$1" in ''|*[!0-9]*|0|1) exit 1 ;; esac
  stat=$(cat "/proc/$1/stat" 2>/dev/null) || exit 1
  set -- ${stat##*) }
  [ "$#" -ge 20 ] && [ "$1" != Z ] && [ "$1" != X ] || exit 1
  shift 19
  echo "$1"
)

write_pid() (
  ticks=$(process_start "$2") || exit 1
  boot=$(cat /proc/sys/kernel/random/boot_id) || exit 1
  printf '%s %s %s\n' "$2" "$boot" "$ticks" > "$1.tmp.$$" && mv "$1.tmp.$$" "$1"
)

# 旧版纯数字记录直接视为失效；不能凭旧 PID 发信号。
identity_alive() (
  read -r pid boot ticks extra 2>/dev/null < "$1" || exit 1
  [ -n "$ticks" ] && [ -z "$extra" ] || exit 1
  [ "$boot" = "$(cat /proc/sys/kernel/random/boot_id)" ] || exit 1
  [ "$ticks" = "$(process_start "$pid")" ]
)

matches_role() (
  pid=$1; role=$2
  process_start "$pid" >/dev/null || exit 1
  case "$role" in
    supervisor)
      tr '\0' '\n' 2>/dev/null < "/proc/$pid/cmdline" | awk -v script="$TOOLS/bridge-ctl.sh" '
        NR == 1 { n=split($0,a,"/"); shell=(a[n]=="sh") }
        NR == 2 { script_ok=($0==script) }
        NR == 3 { mode_ok=($0=="__supervise") }
        END { exit !(NR==3 && shell && script_ok && mode_ok) }' ;;
    bridge)
      [ "$(readlink "/proc/$pid/root" 2>/dev/null)" = "$ROOT" ] || exit 1
      tr '\0' '\n' 2>/dev/null < "/proc/$pid/cmdline" | awk '
        NR == 1 { n=split($0,a,"/"); python=(a[n]=="python" || a[n]=="python3") }
        NR == 2 { main=($0=="main.py") }
        END { exit !(NR==2 && python && main) }' ;;
    *) exit 1 ;;
  esac
)

valid_pid() {
  identity_alive "$1" && matches_role "$(pid_of "$1")" "$2" && identity_alive "$1"
}

# 找回守护循环之外启动的 bridge，也用于停止前逐个验证，避免按命令子串误杀。
bridge_pids() (
  # 先筛选候选进程，再核对 root/argv；逐个 readlink 所有 /proc 在 Android 上很慢。
  for pid in $(pgrep -f '(^|/)(python|python3) main[.]py$'); do
    matches_role "$pid" bridge && echo "$pid"
  done
)

# 模块界面和命令行共用同一套身份校验。
process_status() {
  if valid_pid "$BR_PID" bridge; then echo bridge=running; else echo bridge=stopped; fi
  if valid_pid "$SUP_PID" supervisor; then echo supervisor=running; else echo supervisor=stopped; fi
}

# FlClash 熄屏后被 ColorOS Hans 断网（OAppNetControlService 的 blackList），tun0 成了黑洞，
# chroot 里 root 的流量全部卡死。这里让 uid 0 到上述目标直接查 wlan0 路由表，其他 App 仍走 VPN。
# wlan0 表为空（Wi-Fi 断开）时规则自动落空，继续走后面的规则。
bypass_rules() {   # bypass_rules <-4|-6> <目标...>
  fam=$1; shift
  rules=$(ip "$fam" rule show 2>/dev/null)
  for h in "$@"; do
    echo "$rules" | grep -q "^$BYPASS_PRIO:.*to $h uidrange 0-0 lookup wlan0" && continue
    # 表号会随网卡重建而变（旧规则显示成数字），先清掉再加
    ip "$fam" rule del to "$h" priority "$BYPASS_PRIO" 2>/dev/null
    ip "$fam" rule add to "$h" uidrange 0-0 lookup wlan0 priority "$BYPASS_PRIO" 2>/dev/null \
      && note "vpn bypass added: $h"
  done
}

bypass_vpn() {
  bypass_rules -4 $BYPASS_HOSTS
  bypass_rules -6 $BYPASS_HOSTS6
}

tune() {
  # Wi-Fi 省电会让持续的音频流抖动；重连后会被打开，所以守护循环里定期重设
  iw dev wlan0 set power_save off 2>/dev/null
  bypass_vpn
}

supervise_cleanup() {
  # 不删除别的进程后来写入的记录；退出后 flock 随 FD 自动释放。
  if [ "$(pid_of "$SUP_PID")" = "$$" ] && identity_alive "$SUP_PID"; then
    rm -f "$SUP_PID"
  fi
  echo "$WAKELOCK" > /sys/power/wake_unlock 2>/dev/null
  note "supervisor stopped"
}

supervise() {
  exec 8>"$LOCKDIR/supervisor.lock"
  # Android mksh 默认不向外部命令继承额外 FD，必须显式传给 flock。
  flock -n 8 8>&8 || return 1
  write_pid "$SUP_PID" "$$" || return 1
  trap supervise_cleanup EXIT
  trap 'exit 0' INT TERM
  echo $$ > /dev/cpuset/cgroup.procs 2>/dev/null   # 根 cpuset：0-7 全部核心（background 只有 0-5）
  echo "$WAKELOCK" > /sys/power/wake_lock
  sh "$TOOLS/chroot.sh" mount 8>&- || return 1
  note "supervisor started pid=$$"
  fails=0
  while [ -f "$ENABLED" ]; do
    tune
    started=$(date +%s)
    # 守护进程意外退出后，接管仍在运行的 bridge，不能再启动第二份。
    set -- $(bridge_pids)
    if [ "$#" -gt 1 ]; then note "multiple bridge processes found; refusing to start"; return 1; fi
    if [ "$#" -eq 1 ]; then
      br=$1
      note "bridge adopted pid=$br"
    else
      sh "$TOOLS/chroot.sh" run "exec $APP/deploy/android-chroot/run-bridge.sh" 8>&- </dev/null >/dev/null 2>&1 &
      br=$!
      note "bridge started pid=$br"
    fi
    if write_pid "$BR_PID" "$br"; then
      # chroot/exec 期间 cmdline 会变化，等待期间按同一进程的启动时刻跟踪。
      while identity_alive "$BR_PID" && [ -f "$ENABLED" ]; do
        sleep 15 8>&-
        tune
      done
    fi
    wait "$br" 2>/dev/null
    rm -f "$BR_PID"
    [ -f "$ENABLED" ] || break
    ran=$(( $(date +%s) - started ))
    if [ "$ran" -lt 60 ]; then fails=$((fails + 1)); else fails=0; fi
    delay=$((3 * (1 << (fails > 6 ? 6 : fails))))
    [ "$delay" -gt 300 ] && delay=300
    note "bridge exited after ${ran}s, restart in ${delay}s (fails=$fails)"
    sleep "$delay" 8>&-
  done
}

start() {
  if valid_pid "$SUP_PID" supervisor; then
    echo "already running (supervisor $(pid_of "$SUP_PID"))"
    return 0
  fi
  touch "$ENABLED"
  # 子进程关闭操作锁 FD；守护进程自行持有独立的生命周期锁。
  nohup setsid sh "$TOOLS/bridge-ctl.sh" __supervise 9>&- </dev/null >/dev/null 2>&1 &
  i=0
  while [ "$i" -lt 10 ]; do
    if valid_pid "$SUP_PID" supervisor; then
      echo "started (supervisor $(pid_of "$SUP_PID"))"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "supervisor failed to start; see $SUP_LOG" >&2
  return 1
}

stop() {
  rm -f "$ENABLED"
  # 先停守护，防止停止 bridge 时又被拉起；只向身份已验证的进程发信号。
  if valid_pid "$SUP_PID" supervisor; then
    sup=$(pid_of "$SUP_PID")
    kill "$sup" 2>/dev/null
    i=0
    while identity_alive "$SUP_PID" && [ "$i" -lt 20 ]; do sleep 1; i=$((i + 1)); done
    if valid_pid "$SUP_PID" supervisor; then
      echo "supervisor did not stop ($sup)" >&2
      return 1
    fi
  fi
  for pid in $(bridge_pids); do
    # 临时快照让后续强停也能验证启动时刻，不能杀掉期间复用同一 PID 的进程。
    record="$RUN/stop-$pid.pid"
    write_pid "$record" "$pid" || continue
    if valid_pid "$record" bridge; then kill -INT "$pid" 2>/dev/null; fi
    i=0
    while valid_pid "$record" bridge && [ "$i" -lt 10 ]; do sleep 1; i=$((i + 1)); done
    if valid_pid "$record" bridge; then
      kill -9 "$pid" 2>/dev/null && echo "force killed: $pid"
    fi
    rm -f "$record"
  done
  echo "$WAKELOCK" > /sys/power/wake_unlock 2>/dev/null
  rm -f "$SUP_PID" "$BR_PID"
  echo "stopped"
}

status() {
  sup=$(pid_of "$SUP_PID")
  if valid_pid "$SUP_PID" supervisor; then echo "supervisor: running ($sup)"; else echo "supervisor: stopped"; fi
  echo "bridge pids: $(bridge_pids | tr '\n' ' ')"
  echo "wakelock: $(grep -o "$WAKELOCK" /sys/power/wake_lock 2>/dev/null || echo none)"
  echo "wifi: $(iw dev wlan0 get power_save 2>/dev/null)  ip: $(ip -4 addr show wlan0 | awk '/inet /{print $2}')"
  echo "vpn bypass:"; { ip -4 rule show; ip -6 rule show; } | grep "^$BYPASS_PRIO:"
  echo "listening:"; netstat -tln 2>/dev/null | grep -E ':(4399|4400|4401|9092) '
  echo "speaker connections:"; netstat -tn 2>/dev/null | grep -E ':(4399|4400|4401) ' | grep ESTABLISHED
  echo "--- supervisor.log"; tail -n 5 "$SUP_LOG" 2>/dev/null
}

# 控制命令互斥；锁文件不可删除，否则并发调用可能锁住不同 inode。
case "$1" in
  start|stop|restart)
    exec 9>"$LOCKDIR/control.lock"
    flock -n 9 9>&9 || { echo "another bridge operation is in progress" >&2; exit 1; } ;;
esac

case "$1" in
  start) start ;;
  stop) stop ;;
  restart) stop && start ;;
  status) status ;;
  process-status) process_status ;;
  log) tail -n "${2:-50}" "$LOG" ;;
  __supervise) supervise ;;
  *) echo "usage: $0 start|stop|restart|status|process-status|log [n]" >&2; exit 2 ;;
esac
