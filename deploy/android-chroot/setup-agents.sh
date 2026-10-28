#!/bin/bash
# chroot 内运行：Node.js LTS + pi coding agent + paseo（npm 走 npmmirror）
set -e
NODE_VERSION=${NODE_VERSION:-v24.22.1}
if ! command -v node >/dev/null || [ "$(node -v)" != "$NODE_VERSION" ]; then
  curl -fsSL "https://npmmirror.com/mirrors/node/$NODE_VERSION/node-$NODE_VERSION-linux-arm64.tar.xz" -o /tmp/node.tar.xz
  rm -rf /opt/node && mkdir -p /opt/node
  tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1
  rm -f /tmp/node.tar.xz
  for b in node npm npx corepack; do ln -sf /opt/node/bin/$b /usr/local/bin/$b; done
fi
npm config set registry https://registry.npmmirror.com
npm config set prefix /usr/local
npm install -g @earendil-works/pi-coding-agent @getpaseo/cli
echo "node $(node -v), npm $(npm -v)"
echo "pi: $(command -v pi) $(pi --version 2>&1 | head -n1)"
echo "paseo: $(command -v paseo) $(paseo --version 2>&1 | head -n1)"
