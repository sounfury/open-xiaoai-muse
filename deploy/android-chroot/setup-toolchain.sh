#!/bin/bash
# chroot 内运行：时区、uv、Rust（国内镜像）
set -e
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone
mkdir -p /root/.config/uv /root/.cargo /root/.pip
cat > /root/.config/uv/uv.toml <<'T'
[[index]]
url = "https://mirrors.ustc.edu.cn/pypi/simple"
default = true
T
cat > /root/.cargo/config.toml <<'T'
[source.crates-io]
replace-with = "rsproxy-sparse"
[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
[net]
git-fetch-with-cli = true
T
if ! command -v uv >/dev/null; then
  python3 -m venv /opt/uvenv
  /opt/uvenv/bin/pip install -q -i https://mirrors.ustc.edu.cn/pypi/simple uv
  ln -sf /opt/uvenv/bin/uv /usr/local/bin/uv
fi
if [ ! -x /root/.cargo/bin/cargo ]; then
  export RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
  curl -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y --profile minimal --default-toolchain stable
  grep -q RUSTUP_DIST_SERVER /root/.profile || cat >> /root/.profile <<'T'
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
T
fi
uv --version; /root/.cargo/bin/cargo --version; date
