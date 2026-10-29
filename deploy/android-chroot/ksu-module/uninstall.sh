#!/system/bin/sh
# 卸载：停守护，睡眠待机优化恢复成系统默认（打开），删掉状态目录
p=$(cat /dev/muse-guard/guard.pid 2>/dev/null)
[ -n "$p" ] && kill "$p" 2>/dev/null
settings put secure deepsleep_switch_state 1
rm -rf /dev/muse-guard /data/adb/muse-guard
