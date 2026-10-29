#!/bin/sh
# 把 ksu-module/ 打包成 KernelSU 模块 zip（取已提交的 HEAD，LF 换行）：
#   sh deploy/android-chroot/build-ksu-module.sh  →  deploy/android-chroot/muse-guard.zip
# 安装：KernelSU 管理器 → 模块 → 从本地安装，重启生效。
# 不想重启：把目录复制到 /data/adb/modules/muse-guard（chmod 755 *.sh），
#   先杀掉旧守护（kill $(cat /dev/muse-guard/guard.pid)），再 sh /data/adb/modules/muse-guard/service.sh &
set -e
cd "$(dirname "$0")/../.."
git -c core.autocrlf=false archive --format=zip -o deploy/android-chroot/muse-guard.zip HEAD:deploy/android-chroot/ksu-module
echo "deploy/android-chroot/muse-guard.zip"
