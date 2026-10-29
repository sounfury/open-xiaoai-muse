#!/system/bin/sh
# 开机后启动守护循环（bridge 本身仍由 /data/adb/service.d/openxiaoai-bridge.sh 启动）
MODDIR=${0%/*}
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 5; done
sleep 15
nohup setsid sh "$MODDIR/guard.sh" daemon </dev/null >/dev/null 2>&1 &
