# 待办：音频前端用 Rust 重写

> 状态：第 0 步已测；第 1、2 步已上线（音箱验收通过，默认 rust）。最后更新：2026-10-28。
> 范围：从"收到音箱音频包"到"得到干净的单声道和多麦分离结果"这一段（回声消除、高通、多麦分离、聆听灯推流）。
> 会话控制、后端请求、TTS、灯效、设备命令留在 Python；以后做 Android App 时这部分用 Kotlin 写。

## 1. 为什么做

- **GIL**：音频回调、回声消除、多麦分离、4 路唤醒检测、主循环都在抢同一把锁，平板 8 个核用不起来。Rust 计算时可以释放 GIL（PyO3 `py.allow_threads`），前端放到别的核上跑，不再拖慢唤醒检测和主循环。
- **numpy 小矩阵开销**：多麦分离每帧是 257 个频点的 3×3 复数矩阵，时间主要花在 numpy 每次调用的固定开销上；回声消除每次调用都 `np.concatenate` 新分配数组。
- **少依赖**：去掉 scipy（只用在高通滤波），以后做 App 时这层直接编成 Android 库，Python 版和 App 共用同一份信号处理代码。

不做的理由也要记着：10 月 26 日 VAD 越跑越慢是每帧复制全部历史造成的，属于设计问题，换语言修不了。动手前先测量（见第 0 步）。

## 2. 待办

### 第 0 步：测量，确认值得做

- [x] 在平板上跑 `scripts/perf_bench.py --seconds 60`，记下各阶段每帧耗时（平均 / p95 / 最大）和整条链路实时率（目前约 0.52，熄屏 0.56）
- [x] 看时间分布：回声消除（主麦 1 路 + 多麦 3 路）、多麦分离、唤醒检测（4 路）各占多少
- [x] 判断：如果大头在唤醒检测（sherpa-onnx 内部，本来就是 C++），重写前端收益有限，本文档降级为"做 App 时再做"
- [ ] 准备对比用的录音，存进 `tests/data/`（不进 Git）：
  - [x] 播报中的真实回声：`tests/data/aec/tts_echo_4ch.wav`（multimic_record.py 录 40 秒，其间 API 播一段 TTS；ch0～ch2 + 回采，主麦和多麦的回声消除都能用）
  - [ ] 播报中有人说话的一段（需要有人配合）
  - [ ] 多麦远场、电视噪声各一段（第 3 步用）

**测量结果（2026-10-28，平板，bridge 同时在跑）**

单组件（`perf_bench.py` 新加的单线程测量，按单核占用）：回声消除每路 2.5%、AuxIVA 4.5%、高通 0.2%、
唤醒检测每路 13%（VAD 门控、一直说话；不门控 23%）、VAD 0.6%。线上组合合计：前端 15%、唤醒 52%，
**唤醒检测占 77%**。

运行中 bridge 各线程 60 秒 CPU（`scripts/thread_cpu.sh`，用 py-spy 对应线程名）：多麦 3 路唤醒 69%、
主麦唤醒 17%、`multimic-conn`（回声消除 ×3 + 高通 + IVA）30.6%、原生线程（含主麦回声消除）4.7%，
合计 122%。前端约占 29%。

GIL：`scripts/kws_gil_bench.py` 显示 sherpa 解码时释放 GIL（4 线程加速 2.8 倍，CPU 379%），旁边再加一个
一直在跑的 numpy 回声消除线程只慢 5%。`perf_bench.py --precompute-front`（前端先算好、不占 GIL 的上限）
实时率 0.628 → 0.539，每帧唤醒 17.8 → 16.8 ms。所以前端抢 GIL 对唤醒的拖慢不大，"一直说话"的瓶颈是唤醒本身。

判断：大头在唤醒检测。但前端那 30% 左右的核是 24 小时一直在烧的，第 1、2 步工作量小、还能去掉 scipy，
照做；第 3 步（IVA，4.5%）等第 1 步上线后再看。**更大的收益在唤醒检测本身**（另记一条，
见文末"唤醒检测的开销"）。

### 第 1 步：回声消除 `core/services/audio/aec.py`（157 行，收益最高）

- [x] 在 `native/` 加模块 `src/dsp/aec.rs`，用 realfft（rustfft）实现 `SubbandEchoCanceller`，参数和默认值保持一致：`n_fft=1024`、`hop=256`、`taps=8`、`tau_s=3.0`、`solve_every=4`、`warmup_frames=16`、`history_s=2.0`
- [x] 保留本项目加的两道保护：前 `warmup_frames` 帧只学习不相减；每个频点输出幅度不超过原麦克风
- [x] 每 4 帧解一次 513 个 8×8 复数线性方程（带 trace 正则）：手写批量 Cholesky（矩阵 Hermitian 正定），按频点向量化；自相关只存上三角
- [x] 输入输出缓冲、最近 2 秒的输出 / 回声历史改成预分配缓冲（`VecDeque`），不再每次分配
- [x] PyO3 暴露 `open_xiaoai_server.EchoCanceller`：`process(mic, ref)`、`delay`、`ref_active`、`frames`、`recent_end`、`reduction_db()`、`near_end_ratio_db(window_s, slice_s)`；计算时 `allow_threads`，内部 Mutex（收包线程 process、主循环 near_end_ratio_db 可以同时调）；输入接受任意数值类型和跨步视图
- [x] 离线对比（`scripts/dsp_compare.py`）：合成数据和真实录音、三种包长，输出相对 RMS 误差 5e-15～1.5e-14，`near_end_ratio_db` 最多差 2e-9 dB，状态一致
- [x] 加开关 `barge_in.echo_canceller.impl = "python" | "rust"`（多麦也用它，`multimic.echo_canceller.impl` 可单独覆盖）
- [x] 音箱验收：播报中喊"你好缪斯"能打断；播报不会自己打断自己（2026-10-28 通过）
- [x] 验收后把仓库默认切到 rust（Python 版保留，`impl = "python"` 可切回）

线上（平板、各采 60 秒 `thread_cpu.sh`）：`multimic-conn` 30.6% → 20.7%（剩下主要是 IVA），原生线程 4.7% → 2.4%。

耗时（平板）：单路每 32 ms 从 0.81 ms 降到 0.15 ms（单核 2.5% → 0.5%）；整条链路里多麦 3 路每块
6.0 → 0.86 ms、主麦每帧 1.68 → 0.31 ms；一直说话时实时率 0.580 → 0.531（前端全部不占 GIL 的上限是 0.539）。

### 第 2 步：高通滤波 `core/services/audio/highpass.py`

- [x] 2 阶 Butterworth 二阶节，系数在 Rust 里按 `cutoff_hz` 计算（和 `scipy.signal.butter(..., output="sos")` 差 4e-16），跨包保留状态，支持单声道和多声道（`native/src/dsp/highpass.rs`）
- [x] 接口同 `HighPass.process(x)` / `reset()`（`open_xiaoai_server.HighPassFilter`）；`make_highpass(cutoff, channels)` 返回值不变，按 `audio_input.highpass_impl`（默认 rust）选实现，Python 备用版改成逐采样循环（不再用 scipy）
- [x] 对比 scipy 输出一致（相对误差 1e-14）后，从 `pyproject.toml` / `uv.lock` 去掉 scipy

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

## 4. 唤醒检测的开销（第 0 步发现，不属于前端重写）

- 唤醒检测占 bridge CPU 的 70% 左右（线上 4 路合计约 86% 个核），是真正的大头；有人说话时还会积压
  （2026-10-28 22:07:28 的 `kws_backlog_over_500ms`，积压 613 ms）
- 模型：`core/models/encoder.onnx` 是整个中英双语流式识别 zipformer（int8 105 MB，`encoder_dims` 384、15 层、
  每块 32 帧），batch 固定为 1。`scripts/kws_cost_bench.py`：每条错位流每 32 ms 算 1.8 ms（单核 5.6%），
  和流数成正比；`max_active_paths` 4 / 8 / 16 几乎没有区别（开销在编码器）。线上 4 路检测 × 4 条流 = 16 条，
  门控全开时约 90% 个核
- 专用小模型 `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`（int8 编码器 4.4 MB，phone+ppinyin，支持英文）：
  每条流 0.72 ms，只省 60%（每条流各自算 fbank、Python 每帧转换和解码调用的固定开销不变）；准确率未测，
  要先让 `SherpaKeywordSpotter` / keywords 生成支持换模型和词表格式
- 负载分布（`scripts/kws_load_from_log.py`，10-27～10-28 共 48 小时）：没人说话时 4 路合计约 12% 个核（每帧
  4 次 Silero VAD + 分发到线程池的固定开销），晚上有人说话 / 开电视涨到 35～66%；门控打开的时间约 10～15%
- 命中来源（10-27 以来 21 次唤醒）：iva2 11 次、ch0 9 次、ch0 和 iva1 同帧 1 次；iva0 一次没有。唤醒后检测器
  马上被重置，看不出各路是不是互补
- 可以试（都要用 `scripts/kws_acceptance.py` 确认唤醒率不降）：
  - `kws.stream_offsets_ms` 4 条 → 2 条：有人说话时 −50%，只改配置，验收脚本支持 `--offsets 0,160`
  - 多麦用 ch0 的 VAD 统一门控、门控关着时不往线程池分发：空闲时 12% → 约 3%
  - 多麦只跑 1 路（按语音能量挑当前最响的 IVA 输出，类似原生小爱的"选通道"）：有人说话时 −50%
  - 换专用小模型：−60%，风险在准确率

## 5. 约束

- 每一步都能单独上线，Python 版保留作备用，配置开关可以切回去；全部验收稳定后再删 Python 实现
- 输出和 Python 版逐样本对比通过后，才上音箱；音箱验收看：播报中打断、远场唤醒率、电视噪声下的误唤醒、`perf_bench.py` 实时率
- PC（Windows）和平板（chroot arm64）都要编译通过；平板上依赖变了要在 chroot 里 `uv sync`，PC 上 bridge 在跑时不要 `uv sync`（见 AGENTS.md "本机运行注意"）
- `native/` 依赖的 OwnDing crate 固定在 `7ecea6f`，新增依赖（rustfft、nalgebra 等）不要连带升级它
- 以后做 App：这一层再加一个 uniffi / JNI 接口给 Kotlin，算法代码不动
