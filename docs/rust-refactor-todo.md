# 待办：音频前端用 Rust 重写

> 状态：待评估，还没开始。最后更新：2026-10-28。
> 范围：从"收到音箱音频包"到"得到干净的单声道和多麦分离结果"这一段（回声消除、高通、多麦分离、聆听灯推流）。
> 会话控制、后端请求、TTS、灯效、设备命令留在 Python；以后做 Android App 时这部分用 Kotlin 写。

## 1. 为什么做

- **GIL**：音频回调、回声消除、多麦分离、4 路唤醒检测、主循环都在抢同一把锁，平板 8 个核用不起来。Rust 计算时可以释放 GIL（PyO3 `py.allow_threads`），前端放到别的核上跑，不再拖慢唤醒检测和主循环。
- **numpy 小矩阵开销**：多麦分离每帧是 257 个频点的 3×3 复数矩阵，时间主要花在 numpy 每次调用的固定开销上；回声消除每次调用都 `np.concatenate` 新分配数组。
- **少依赖**：去掉 scipy（只用在高通滤波），以后做 App 时这层直接编成 Android 库，Python 版和 App 共用同一份信号处理代码。

不做的理由也要记着：10 月 26 日 VAD 越跑越慢是每帧复制全部历史造成的，属于设计问题，换语言修不了。动手前先测量（见第 0 步）。

## 2. 待办

### 第 0 步：测量，确认值得做

- [ ] 在平板上跑 `scripts/perf_bench.py --seconds 60`，记下各阶段每帧耗时（平均 / p95 / 最大）和整条链路实时率（目前约 0.52，熄屏 0.56）
- [ ] 看时间分布：回声消除（主麦 1 路 + 多麦 3 路）、多麦分离、唤醒检测（4 路）各占多少
- [ ] 判断：如果大头在唤醒检测（sherpa-onnx 内部，本来就是 C++），重写前端收益有限，本文档降级为"做 App 时再做"
- [ ] 准备对比用的录音，存进 `tests/data/`（不进 Git）：
  - 主麦：`ECHO_REF_DUMP=<文件.wav>` 启动 bridge，录一段播报中说话的 [麦克风, 回采] 双声道
  - 多麦：`scripts/multimic_record.py` 录 4 声道（3 麦 + 回采），远场、电视噪声各一段

### 第 1 步：回声消除 `core/services/audio/aec.py`（157 行，收益最高）

- [ ] 在 `native/` 加模块（如 `src/dsp/aec.rs`），用 rustfft 实现 `SubbandEchoCanceller`，参数和默认值保持一致：`n_fft=1024`、`hop=256`、`taps=8`、`tau_s=3.0`、`solve_every=4`、`warmup_frames=16`、`history_s=2.0`
- [ ] 保留本项目加的两道保护：前 `warmup_frames` 帧只学习不相减；每个频点输出幅度不超过原麦克风
- [ ] 每 4 帧解一次 513 个 8×8 复数线性方程（带 trace 正则），手写或用 nalgebra
- [ ] 输入输出缓冲、最近 2 秒的输出 / 回声历史改成环形缓冲，不再每次分配
- [ ] PyO3 暴露同名类和接口：`process(mic, ref) -> 干净音频`、`delay`、`ref_active`、`reduction_db()`、`near_end_ratio_db(window_s, slice_s)`；计算时 `allow_threads`
- [ ] 离线对比：同一段录音跑 Python 版和 Rust 版，逐样本误差（相对 RMS）在 1e-6 量级；`near_end_ratio_db` 结果一致
- [ ] 加开关（如 `barge_in.echo_canceller.impl = "python" | "rust"`），默认先用 Python，验收后再切
- [ ] 音箱验收：播报中喊"你好缪斯"能打断；播报不会自己打断自己（近端门限 `near_end_min_db` 照常生效）

### 第 2 步：高通滤波 `core/services/audio/highpass.py`

- [ ] 2 阶 Butterworth 二阶节，系数在 Rust 里按 `cutoff_hz` 计算（和 `scipy.signal.butter(..., output="sos")` 一致），跨包保留状态，支持单声道和多声道
- [ ] 接口同 `HighPass.process(x)` / `reset()`；`make_highpass(cutoff, channels)` 返回值不变
- [ ] 对比 scipy 输出一致后，从 `pyproject.toml` 去掉 scipy

### 第 3 步：多麦分离 `core/services/audio/multimic.py`（115 行）

- [ ] `StreamingSTFT`：sqrt-hann 窗、50% 重叠、`nfft=512`，完全重构
- [ ] `OnlineAuxIVA`：3×3 求逆和解方程用闭式公式；保留对角载荷（1e-9）和求逆失败时退回单位阵；投影回参考麦克风
- [ ] 接口同 `process(block) -> (n', K)`，输出比输入晚一帧
- [ ] 用 `scripts/multimic_eval.py` 对比：IVA 3 路的唤醒命中数和误唤醒数与 Python 版相同

### 第 4 步：把链路接到 Rust 收包的地方

- [ ] 多麦（`core/services/audio/multimic_input.py` 的 `_process`）：4401 端口收包、拆声道、乘 `gain`、每麦回声消除 + 高通 + IVA，整条放进 Rust 线程；Python 只按 512 采样一帧取 3 路结果（`pull()` 接口不变）。看门狗、音箱上的 `arecord | nc` 命令和 pid 文件留在 Python
- [ ] 主麦（`core/xiaoai.py` 的 `on_input_packet` / `_cancel_echo`）：`mic_ref` 双声道包在 `native/src/server.rs` 收到后直接做回声消除和高通，再交给 Python。`echo_reference_live` / `echo_active` / `near_end_ratio_db` 需要的状态（最近一次见到回采的时间、回声拖尾截止位置）由 Rust 提供
- [ ] 诊断（`core/services/diagnostics.py`）里 `input.aec`、`multimic.dsp` 的计时改为从 Rust 读

### 第 5 步：聆听灯推流 `core/services/light_level.py`（可选，工作量小）

- [ ] 每 110 ms 准时写一帧 9600 字节噪声（音箱 `vis` 读慢了会关掉 FIFO），改成 Rust 线程定时，避免 Python 抢 GIL 时的抖动
- [ ] 音量跟踪（底噪、`margin_db` / `span_db`、`max_rms`）可以留在 Python，只把"按时写帧"搬过去
- [ ] 先确认现在有没有抖动问题（看日志里 nc 重连次数），没有就不做

## 3. 不搬的

| 部分 | 原因 |
|---|---|
| 唤醒检测、VAD、语音识别 | 计算在 sherpa-onnx / onnxruntime 里，本来就是 C++；PC 版重写没收益。做 App 时直接用 sherpa-onnx 的 Android 库 |
| `GlobalStream`（`audio/stream.py`） | 只有 VAD、唤醒检测也搬走时才值得一起搬，否则数据要在两边来回传 |
| 会话控制（`external_conversation.py`、`wakeup_session.py`、`core/backends.py` 及各后端） | 主要在等网络和设备，最常改，留在 Python |
| TTS、切段、灯效、设备命令、`music_duck.py`、`diagnostics.py` | 同上；诊断依赖 Python 自身的线程栈 |

## 4. 约束

- 每一步都能单独上线，Python 版保留作备用，配置开关可以切回去；全部验收稳定后再删 Python 实现
- 输出和 Python 版逐样本对比通过后，才上音箱；音箱验收看：播报中打断、远场唤醒率、电视噪声下的误唤醒、`perf_bench.py` 实时率
- PC（Windows）和平板（chroot arm64）都要编译通过；平板上依赖变了要在 chroot 里 `uv sync`，PC 上 bridge 在跑时不要 `uv sync`（见 AGENTS.md "本机运行注意"）
- `native/` 依赖的 OwnDing crate 固定在 `7ecea6f`，新增依赖（rustfft、nalgebra 等）不要连带升级它
- 以后做 App：这一层再加一个 uniffi / JNI 接口给 Kotlin，算法代码不动
