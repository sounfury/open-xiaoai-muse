#!/system/bin/sh
# 解压 Ubuntu base 到 /data/local/ubuntu 并做基础配置（只需运行一次）
set -e
ROOT=/data/local/ubuntu
TARBALL=$1
[ -f "$TARBALL" ] || { echo "缺少 rootfs 包: $TARBALL" >&2; exit 1; }
if [ -d "$ROOT/usr" ]; then
  echo "已存在 $ROOT，跳过解压"
else
  mkdir -p "$ROOT"
  tar -xzpf "$TARBALL" -C "$ROOT"
fi

# DNS / hosts（Android 没有 /etc/resolv.conf）
rm -f "$ROOT/etc/resolv.conf"
printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n' > "$ROOT/etc/resolv.conf"
printf '127.0.0.1 localhost\n::1 localhost\n127.0.1.1 tablet\n' > "$ROOT/etc/hosts"
echo tablet > "$ROOT/etc/hostname"

# 国内镜像（arm64 走 ubuntu-ports）
cat > "$ROOT/etc/apt/sources.list.d/ubuntu.sources" <<'EOF'
Types: deb
URIs: http://mirrors.ustc.edu.cn/ubuntu-ports/
Suites: noble noble-updates noble-backports noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF

# Android 内核对非 root 用户联网有限制，apt 下载不降权
echo 'APT::Sandbox::User "root";' > "$ROOT/etc/apt/apt.conf.d/01-android-sandbox"
echo "rootfs 安装完成: $ROOT"
