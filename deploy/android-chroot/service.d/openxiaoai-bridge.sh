#!/system/bin/sh
# KernelSU 开机脚本：复制到 /data/adb/service.d/（chmod 755）。
# 等系统启动完成、Wi-Fi 拿到 IP 后启动 bridge。
LOG=/data/local/ubuntu-tools/run/boot.log
mkdir -p /data/local/ubuntu-tools/run

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 5; done
i=0
while [ $i -lt 60 ] && ! ip -4 addr show wlan0 2>/dev/null | grep -q "inet "; do
  sleep 5; i=$((i + 1))
done
sleep 10
echo "$(date '+%F %T') boot: wlan0=$(ip -4 addr show wlan0 | awk '/inet /{print $2}')" >> "$LOG"
touch /data/local/ubuntu-tools/run/enabled
sh /data/local/ubuntu-tools/bridge-ctl.sh start >> "$LOG" 2>&1

# SSH（2222，仅密钥）；sshd 自己转后台
if [ -x /data/local/ubuntu/usr/sbin/sshd ]; then
  sh /data/local/ubuntu-tools/chroot.sh run "mkdir -p /run/sshd && /usr/sbin/sshd" </dev/null >> "$LOG" 2>&1
fi

# paseo 守护进程（chroot 内 /root/.paseo，配置由 paseo onboard 生成）；没装就跳过
if [ -x /data/local/ubuntu/usr/local/bin/paseo ]; then
  sleep 5
  setsid sh /data/local/ubuntu-tools/chroot.sh run "cd /root && paseo start --timeout 300" </dev/null >> "$LOG" 2>&1 &
fi
