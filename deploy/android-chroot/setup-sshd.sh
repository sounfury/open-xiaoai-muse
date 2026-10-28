#!/bin/bash
# chroot 内运行：OpenSSH 服务（2222 端口，仅密钥登录）
# 用法: setup-sshd.sh [公钥文件...]   公钥追加进 /root/.ssh/authorized_keys
set -e
export DEBIAN_FRONTEND=noninteractive
command -v sshd >/dev/null || apt-get install -y -q --no-install-recommends openssh-server >/dev/null

cat > /etc/ssh/sshd_config.d/10-tablet.conf <<'EOF'
Port 2222
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
# chroot 里 pam_loginuid 写 /proc/self/loginuid 会失败导致登录被拒
UsePAM no
PrintMotd no
AcceptEnv LANG LC_*
ClientAliveInterval 60
ClientAliveCountMax 3
EOF

# UsePAM no 时 /etc/environment 不生效，登录环境放 profile.d
cat > /etc/profile.d/10-tablet.sh <<'EOF'
export LANG=${LANG:-C.UTF-8}
export TZ=Asia/Shanghai
case ":$PATH:" in *:/root/.local/bin:*) ;; *) PATH=/root/.local/bin:/root/.cargo/bin:$PATH ;; esac
export PATH
# 不让 uv 把镜像地址写进 uv.lock（改依赖时临时 UV_FROZEN=0）
export UV_FROZEN=1
EOF
# ssh 主机 '命令' 不读 profile，只读 .bashrc（且在非交互判断之前）
grep -q 10-tablet.sh /root/.bashrc 2>/dev/null || sed -i '1i . /etc/profile.d/10-tablet.sh; [ -f /root/.cargo/env ] && . /root/.cargo/env' /root/.bashrc

mkdir -p -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
for f in "$@"; do
  key=$(tr -d '\r' < "$f")  # Windows 生成的公钥是 CRLF
  grep -qxF "$key" /root/.ssh/authorized_keys || echo "$key" >> /root/.ssh/authorized_keys
done
chown -R root:root /root/.ssh && chmod 600 /root/.ssh/authorized_keys

install -m 755 "$(dirname "$0")/android" /usr/local/bin/android 2>/dev/null || true  # chroot 里执行 Android 侧命令
ssh-keygen -A >/dev/null
mkdir -p /run/sshd && chmod 755 /run/sshd
/usr/sbin/sshd -t
if [ -f /run/sshd.pid ] && kill -0 "$(cat /run/sshd.pid)" 2>/dev/null; then
  kill -HUP "$(cat /run/sshd.pid)"
else
  /usr/sbin/sshd
fi
echo "sshd running, authorized keys: $(grep -c . /root/.ssh/authorized_keys)"
for k in /etc/ssh/ssh_host_ed25519_key.pub; do ssh-keygen -lf "$k"; done
