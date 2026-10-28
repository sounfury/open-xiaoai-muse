#!/system/bin/sh
# Ubuntu chroot 管理（以 root 运行，建议 su -M 进入全局挂载命名空间）
# 用法: chroot.sh mount | umount | status | shell | run <命令...>
ROOT=/data/local/ubuntu
XFER=/data/local/tmp/xfer

mounted() { grep -q " $1 " /proc/mounts; }

do_mount() {
  [ -d "$ROOT/usr" ] || { echo "rootfs 不存在: $ROOT" >&2; exit 1; }
  # /data 是 nosuid,nodev：自绑定后重新挂载去掉限制
  if ! mounted "$ROOT"; then
    mount --bind "$ROOT" "$ROOT"
    mount -o remount,bind,suid,dev,exec "$ROOT" 2>/dev/null || echo "警告: remount suid/dev 失败" >&2
  fi
  mounted "$ROOT/dev"     || mount --bind /dev "$ROOT/dev"
  mounted "$ROOT/dev/pts" || mount --bind /dev/pts "$ROOT/dev/pts"
  mkdir -p "$ROOT/dev/shm" 2>/dev/null
  mounted "$ROOT/dev/shm" || mount -t tmpfs -o size=512M,mode=1777 tmpfs "$ROOT/dev/shm"
  mounted "$ROOT/proc"    || mount -t proc proc "$ROOT/proc"
  mounted "$ROOT/sys"     || mount -t sysfs sysfs "$ROOT/sys"
  mounted "$ROOT/tmp"     || mount -t tmpfs -o size=1G,mode=1777 tmpfs "$ROOT/tmp"
  mkdir -p "$XFER" "$ROOT/mnt/xfer"
  chmod 777 "$XFER"
  mounted "$ROOT/mnt/xfer" || mount --bind "$XFER" "$ROOT/mnt/xfer"
}

do_umount() {
  for m in mnt/xfer tmp sys proc dev/shm dev/pts dev ""; do
    p="$ROOT${m:+/$m}"
    mounted "$p" && umount -l "$p"
  done
  return 0
}

enter() {
  do_mount
  exec chroot "$ROOT" /usr/bin/env -i \
    HOME=/root TERM="${TERM:-xterm-256color}" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PATH=/root/.local/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    "$@"
}

case "$1" in
  mount) do_mount ;;
  umount) do_umount ;;
  status) grep " $ROOT" /proc/mounts ;;
  shell) enter /bin/bash -l ;;
  run) shift; enter /bin/bash -lc "$*" ;;
  *) echo "用法: $0 mount|umount|status|shell|run <命令>" >&2; exit 2 ;;
esac
