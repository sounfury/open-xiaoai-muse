#!/bin/bash
# chroot 内启动 bridge（对应 Windows 的 start-bridge.bat）；由 bridge-ctl.sh 的守护循环调用，退出后它负责重启。
# 密钥：项目根目录 .hermes_api_key
APP=/opt/open-xiaoai-bridge
cd "$APP" || exit 1
mkdir -p logs
LOG=logs/bridge-autostart.log

# 日志超过 50 MB 就轮转一份
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 52428800 ]; then
  mv -f "$LOG" "$LOG.1"
fi

export API_SERVER_ENABLE=1
export API_SERVER_HOST=127.0.0.1
export API_SERVER_PORT=9092
export AUDIO_INPUT_ENABLE=1   # VAD/KWS 模型在 core/models
export MUSE_ENABLE=1          # "你好缪斯"唤醒词 → Hermes 连续对话

export HERMES_ENABLE=1
export HERMES_BASE_URL=https://hermes.oci.sounfury.top/v1
export HERMES_MODEL=muse-home
HERMES_API_KEY=$(head -n1 .hermes_api_key 2>/dev/null | tr -d '\r\n')
export HERMES_API_KEY
if [ -z "$HERMES_API_KEY" ]; then
  echo "[run-bridge] ERROR: .hermes_api_key missing or empty" >> "$LOG"
fi

export LOGLEVEL=INFO
export PYTHONIOENCODING=utf-8
export PYTHONFAULTHANDLER=1
export PYTHONUNBUFFERED=1
export TZ=Asia/Shanghai

# sherpa-onnx 的 arm64 包需要 libonnxruntime.so（onnxruntime 包里只有带版本号的文件）
ORT=$(ls -d "$APP"/.venv/lib/python3*/site-packages/onnxruntime/capi 2>/dev/null | head -n1)
if [ -n "$ORT" ] && [ ! -e "$ORT/libonnxruntime.so" ]; then
  ln -s "$(basename "$(ls "$ORT"/libonnxruntime.so.* | head -n1)")" "$ORT/libonnxruntime.so"
fi
export LD_LIBRARY_PATH="$ORT${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "[run-bridge] $(date '+%F %T') starting pid=$$" >> "$LOG"
exec "$APP/.venv/bin/python" main.py >> "$LOG" 2>&1
