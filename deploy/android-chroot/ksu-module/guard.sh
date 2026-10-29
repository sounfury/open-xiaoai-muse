#!/system/bin/sh
# Muse 守护（KernelSU 模块 muse-guard）：
#   - 按时间表开关 ColorOS “睡眠待机优化”（secure deepsleep_switch_state：熄屏约 1 小时后关 Wi‑Fi）
#       每天 NIGHT_SAVE（02:00）起省电：待机优化打开
#       周末 / 节假日 OFFDAY_PROTECT（08:00）起保护，工作日（含调休补班）WORKDAY_PROTECT（18:30）起保护
#   - 保护时段里 Wi‑Fi 被系统关掉就重新打开
#   - 记录音箱是否连着 bridge（4399），写状态给网页界面和模块描述
# 用法：guard.sh daemon | once | status | text | sched [时刻] | set-mode auto|protect|save
#       | set-times HHMM HHMM HHMM | update-holidays | restart-bridge
MODDIR=/data/adb/modules/muse-guard
[ -f "$MODDIR/guard.sh" ] || MODDIR=$(cd "${0%/*}" && pwd)
STATE=/data/adb/muse-guard          # 配置、事件、节假日数据（持久）
RUN=/dev/muse-guard                 # 运行状态（tmpfs，每 15 秒刷新，不写闪存）
TOOLS=/data/local/ubuntu-tools
BB=/data/adb/ksu/bin/busybox
[ -x "$BB" ] || BB=/data/adb/magisk/busybox
INTERVAL=15
HOLIDAY_URL=https://raw.githubusercontent.com/NateScarlet/holiday-cn/master
mkdir -p "$STATE/holiday" "$RUN"

date() { "$BB" date "$@"; }
awk() { "$BB" awk "$@"; }

NIGHT_SAVE=0200
OFFDAY_PROTECT=0800
WORKDAY_PROTECT=1830
SPEAKER_PORT=4399
MULTIMIC_PORT=4401
[ -f "$STATE/config" ] && . "$STATE/config"

note() {
  echo "$(date '+%F %T') $*" >> "$STATE/events.log"
  if [ "$(wc -l < "$STATE/events.log")" -gt 1000 ]; then
    tail -n 500 "$STATE/events.log" > "$STATE/events.tmp" && mv "$STATE/events.tmp" "$STATE/events.log"
  fi
}

label() { [ "$1" = protect ] && echo "保护" || echo "省电"; }
tomin() { h=${1%??}; m=${1#??}; echo $(( ${h#0} * 60 + ${m#0} )); }
at_epoch() { date -d "$1 ${2%??}:${2#??}" +%s; }   # 日期 HHMM → epoch

# ---- 节假日（holiday-cn：isOffDay=true 放假，false 调休上班）----

rebuild_holidays() {
  # 下载的（新）排在模块自带的前面，同一天只取第一条
  for f in "$STATE"/holiday/*.json "$MODDIR"/holiday/*.json; do
    [ -f "$f" ] || continue
    tr -d ' \n\r\t' < "$f" | grep -o '"name":"[^"]*","date":"[0-9-]*","isOffDay":[a-z]*' |
      sed 's/"name":"\([^"]*\)","date":"\([0-9-]*\)","isOffDay":\([a-z]*\)/\2 \3 \1/'
  done | awk '!seen[$1]++' | sort > "$STATE/holidays.tmp"
  mv "$STATE/holidays.tmp" "$STATE/holidays.txt"
}

update_holidays() {
  y=$(date +%Y)
  got=""
  for year in "$y" "$((y + 1))"; do
    for url in "$HOLIDAY_URL/$year.json" "https://ghfast.top/$HOLIDAY_URL/$year.json" \
               "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/$year.json"; do
      if curl -fsSL --max-time 20 -o "$STATE/holiday/.dl" "$url" 2>/dev/null &&
         grep -q "\"year\": *$year" "$STATE/holiday/.dl"; then
        if ! cmp -s "$STATE/holiday/.dl" "$STATE/holiday/$year.json"; then
          mv "$STATE/holiday/.dl" "$STATE/holiday/$year.json"
          got="$got $year"
        fi
        break
      fi
    done
  done
  rm -f "$STATE/holiday/.dl"
  date +%s > "$STATE/holiday/.checked"
  rebuild_holidays
  [ -n "$got" ] && note "节假日数据已更新：$got"
}

holidays_due() {
  last=$(cat "$STATE/holiday/.checked" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -gt 43200 ]
}

# 某天放不放假：DAY_OFF=1/0，DAY_DESC 给人看
day_info() {  # $1=YYYY-MM-DD $2=星期 1-7
  d=$1; w=$2
  [ -f "$STATE/holidays.txt" ] || rebuild_holidays
  line=$(grep "^$d " "$STATE/holidays.txt" | head -n 1)
  if [ -n "$line" ]; then
    set -- $line
    if [ "$2" = true ]; then DAY_OFF=1; DAY_DESC="节假日（$3）"; else DAY_OFF=0; DAY_DESC="调休上班（$3）"; fi
    return
  fi
  if [ "$w" -ge 6 ]; then DAY_OFF=1; DAY_DESC="周末"; else DAY_OFF=0; DAY_DESC="工作日"; fi
  grep -q "^${d%%-*}-" "$STATE/holidays.txt" || DAY_DESC="$DAY_DESC（${d%%-*} 年放假安排还没拿到，只按周六日算）"
}

# 时间表：给定时刻的模式 SCHED 和下一次切换时刻 NEXT_EPOCH
sched_at() {  # $1=epoch
  sd=$(date -d "@$1" +%F)
  day_info "$sd" "$(date -d "@$1" +%u)"
  n=$(tomin "$(date -d "@$1" +%H%M)")
  if [ "$DAY_OFF" = 1 ]; then ps=$OFFDAY_PROTECT; else ps=$WORKDAY_PROTECT; fi
  if [ "$n" -lt "$(tomin $NIGHT_SAVE)" ]; then
    SCHED=protect; NEXT_EPOCH=$(at_epoch "$sd" $NIGHT_SAVE)
  elif [ "$n" -lt "$(tomin $ps)" ]; then
    SCHED=save; NEXT_EPOCH=$(at_epoch "$sd" $ps)
  else
    SCHED=protect; NEXT_EPOCH=$(at_epoch "$(date -d "@$(($1 + 86400))" +%F)" $NIGHT_SAVE)
  fi
}

# ---- 状态采集 ----

# 连着某个本地端口的对端 IPv4（/proc/net/tcp*，01=ESTABLISHED）
peer_of() {
  port=$(printf ':%04X' "$1")
  cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk -v p="$port" '
    function h(x) { return (index("0123456789ABCDEF", substr(x,1,1)) - 1) * 16 + index("0123456789ABCDEF", substr(x,2,1)) - 1 }
    $4 == "01" && substr($2, length($2) - 4) == p {
      split($3, r, ":"); a = substr(r[1], length(r[1]) - 7)
      print h(substr(a,7,2)) "." h(substr(a,5,2)) "." h(substr(a,3,2)) "." h(substr(a,1,2)); exit }'
}

alive_pidfile() { p=$(cat "$1" 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

tick() {
  now=$(date +%s)
  sched_at "$now"
  TODAY_DESC=$DAY_DESC; MODE=$SCHED; WHY=schedule; SWITCH_AT=$NEXT_EPOCH
  OVERRIDE=""; OVERRIDE_UNTIL=0
  [ -f "$STATE/override" ] && . "$STATE/override"
  if [ -n "$OVERRIDE" ]; then
    if [ "$now" -lt "$OVERRIDE_UNTIL" ]; then
      MODE=$OVERRIDE; WHY=manual; SWITCH_AT=$OVERRIDE_UNTIL
    else
      rm -f "$STATE/override"; OVERRIDE_UNTIL=0
      note "手动设置到期，恢复按时间表"
    fi
  fi

  last_mode=$(cat "$STATE/last_mode" 2>/dev/null)
  if [ "$last_mode" != "$MODE" ]; then
    echo "$MODE" > "$STATE/last_mode"
    note "进入$(label $MODE)模式（$([ $WHY = manual ] && echo 手动 || echo 按时间表)，今天：$TODAY_DESC）"
  fi

  want=1; [ "$MODE" = protect ] && want=0
  ds=$(settings get secure deepsleep_switch_state 2>/dev/null)
  if [ "$ds" != "$want" ]; then
    settings put secure deepsleep_switch_state "$want"
    note "睡眠待机优化：$ds → $want"
    ds=$want
  fi

  wifi=$(cmd wifi status 2>/dev/null)
  case "$wifi" in "Wifi is enabled"*) wifi_on=1 ;; *) wifi_on=0 ;; esac
  if [ "$MODE" = protect ] && [ "$wifi_on" = 0 ]; then
    note "Wi‑Fi 被关掉了，重新打开"
    svc wifi enable
  fi
  ssid=$(echo "$wifi" | sed -n 's/.*connected to "\([^"]*\)".*/\1/p' | head -n 1 | tr -d '\\')
  ip=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{sub(/\/.*/, "", $2); print $2; exit}')

  peer=$(peer_of "$SPEAKER_PORT")
  mm=$(peer_of "$MULTIMIC_PORT")
  conn=0; [ -n "$peer" ] && conn=1
  set -- $(cat "$STATE/last_conn" 2>/dev/null)
  if [ "$1" != "$conn" ]; then
    echo "$conn $now" > "$STATE/last_conn"
    since=$now
    if [ "$conn" = 1 ]; then note "音箱已连接（$peer）"; else note "音箱断开（Wi‑Fi $([ $wifi_on = 1 ] && echo 开着 || echo 关着)，IP ${ip:-无}）"; fi
  else
    since=$2
  fi

  bridge=stopped
  alive_pidfile "$TOOLS/run/bridge.pid" && bridge=running
  supervisor=stopped
  alive_pidfile "$TOOLS/run/supervisor.pid" && supervisor=running

  years=$(cut -c1-4 "$STATE/holidays.txt" 2>/dev/null | sort -u | tr '\n' ' ')
  checked=$(cat "$STATE/holiday/.checked" 2>/dev/null || echo 0)

  cat > "$RUN/status.tmp" <<EOF
{"updated":$now,"conn":$conn,"conn_since":$since,"peer":"$peer","multimic":"$mm",
"bridge":"$bridge","supervisor":"$supervisor","wifi_on":$wifi_on,"ip":"$ip","ssid":"$ssid",
"deepsleep":"$ds","mode":"$MODE","why":"$WHY","switch_at":$SWITCH_AT,"sched":"$SCHED","sched_next":$NEXT_EPOCH,
"today":"$TODAY_DESC","times":{"night_save":"$NIGHT_SAVE","offday_protect":"$OFFDAY_PROTECT","workday_protect":"$WORKDAY_PROTECT"},
"holiday_years":"${years% }","holiday_checked":$checked}
EOF
  mv "$RUN/status.tmp" "$RUN/status.json"

  # 模块描述只在内容变化时改写
  if [ "$conn" = 1 ]; then c="🟢 音箱已连接（$(date -d "@$since" '+%m-%d %H:%M') 起）"; else c="🔴 音箱未连接（$(date -d "@$since" '+%m-%d %H:%M') 起）"; fi
  [ "$bridge" = running ] || c="$c｜⚠ bridge 没在运行"
  m="$([ "$MODE" = protect ] && echo 🛡 || echo 🌙) $(label $MODE)中，$(date -d "@$SWITCH_AT" '+%H:%M') 切换"
  [ "$WHY" = manual ] && m="$m（手动）"
  desc="$c｜$m｜今天：$TODAY_DESC"
  if ! grep -qxF "description=$desc" "$MODDIR/module.prop"; then
    sed -i "s|^description=.*|description=$desc|" "$MODDIR/module.prop"
  fi
}

daemon() {
  if alive_pidfile "$RUN/guard.pid"; then echo "already running ($(cat "$RUN/guard.pid"))"; return 0; fi
  echo $$ > "$RUN/guard.pid"
  note "守护启动"
  rebuild_holidays
  while :; do
    if holidays_due; then date +%s > "$STATE/holiday/.checked"; ( update_holidays ) & fi
    tick
    sleep "$INTERVAL"
  done
}

set_mode() {
  case "$1" in
    auto)
      rm -f "$STATE/override"; note "手动：恢复按时间表" ;;
    protect|save)
      now=$(date +%s)
      sched_at "$now"
      if [ "$1" = "$SCHED" ]; then
        # 和时间表一致：一直保持到时间表下一次回到这个模式（比如凌晨 1 点选保护 = 今晚不省电）
        sched_at $((NEXT_EPOCH + 60))
      fi
      printf 'OVERRIDE=%s\nOVERRIDE_UNTIL=%s\n' "$1" "$NEXT_EPOCH" > "$STATE/override"
      note "手动：$(label "$1")到 $(date -d "@$NEXT_EPOCH" '+%m-%d %H:%M')" ;;
    *) echo "usage: set-mode auto|protect|save" >&2; return 2 ;;
  esac
  tick
}

set_times() {
  for t in "$1" "$2" "$3"; do
    case "$t" in [0-2][0-9][0-5][0-9]) ;; *) echo "时间要写成 4 位 HHMM：$t" >&2; return 2 ;; esac
  done
  if [ "$(tomin "$1")" -ge "$(tomin "$2")" ] || [ "$(tomin "$2")" -gt "$(tomin "$3")" ]; then
    echo "要求 夜间省电 < 周末保护 ≤ 工作日保护" >&2; return 2
  fi
  printf 'NIGHT_SAVE=%s\nOFFDAY_PROTECT=%s\nWORKDAY_PROTECT=%s\n' "$1" "$2" "$3" > "$STATE/config"
  NIGHT_SAVE=$1; OFFDAY_PROTECT=$2; WORKDAY_PROTECT=$3
  note "时间表改为：每天 ${1%??}:${1#??} 起省电，周末/节假日 ${2%??}:${2#??}、工作日 ${3%??}:${3#??} 起保护"
  tick
}

text_status() {
  tick
  awk 'BEGIN{RS=""} {gsub(/[{}\n]/,""); n=split($0,a,","); for(i=1;i<=n;i++) print a[i]}' "$RUN/status.json"
  echo "--- 最近事件"
  tail -n 15 "$STATE/events.log" 2>/dev/null
}

case "$1" in
  daemon) daemon ;;
  once) tick; cat "$RUN/status.json" ;;
  status) [ -f "$RUN/status.json" ] || tick; cat "$RUN/status.json" ;;
  text) text_status ;;
  sched)  # 调试：某个时刻按时间表是什么模式，如 guard.sh sched "2026-10-31 07:00"
    e=$(date +%s); [ -n "$2" ] && e=$(date -d "$2" +%s)
    sched_at "$e"
    echo "$(date -d "@$e" '+%F %a %H:%M') $DAY_DESC → $(label $SCHED)，$(date -d "@$NEXT_EPOCH" '+%m-%d %H:%M') 切换" ;;
  set-mode) set_mode "$2" ;;
  set-times) set_times "$2" "$3" "$4" ;;
  update-holidays) update_holidays; tick; echo "年份：$(cut -c1-4 "$STATE/holidays.txt" | sort -u | tr '\n' ' ')" ;;
  restart-bridge)
    note "手动：重启 bridge"
    "$BB" nsenter -t 1 -m -- sh "$TOOLS/bridge-ctl.sh" restart 2>&1 || sh "$TOOLS/bridge-ctl.sh" restart ;;
  *) echo "usage: $0 daemon|once|status|text|set-mode M|set-times A B C|update-holidays|restart-bridge" >&2; exit 2 ;;
esac
