//! 音频前端 DSP（回声消除、高通）的 Python 绑定。
//!
//! 算法本身在 `aec.rs` / `highpass.rs`，不依赖 PyO3（以后给 App 加 uniffi / JNI 接口时直接复用）。
//! 这里的类和 Python 版接口一致（见 core/services/audio/aec.py、highpass.py），
//! 计算时释放 GIL；内部状态用 Mutex 保护，可以从不同线程调用（如收包线程 process、
//! 主循环 near_end_ratio_db）。

pub mod aec;
pub mod highpass;

use numpy::{AllowTypeChange, PyArray1, PyArray2, PyArrayLike1, PyArrayLike2, PyArrayMethods, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::sync::Mutex;

fn to_vec(a: &PyArrayLike1<'_, f64, AllowTypeChange>) -> Vec<f64> {
    match a.as_slice() {
        Ok(s) => s.to_vec(),
        Err(_) => a.as_array().iter().copied().collect(),
    }
}

/// Rust 版 SubbandEchoCanceller（与 core/services/audio/aec.py 同接口）。
#[pyclass(name = "EchoCanceller", module = "open_xiaoai_server")]
pub struct PyEchoCanceller {
    inner: Mutex<aec::SubbandEchoCanceller>,
    delay: usize,
    sample_rate: usize,
    n_fft: usize,
    hop: usize,
    taps: usize,
}

#[pymethods]
impl PyEchoCanceller {
    #[new]
    #[pyo3(signature = (sample_rate=16000, n_fft=1024, hop=256, taps=8, tau_s=3.0, solve_every=4, history_s=2.0, warmup_frames=16))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        sample_rate: usize,
        n_fft: usize,
        hop: usize,
        taps: usize,
        tau_s: f64,
        solve_every: usize,
        history_s: f64,
        warmup_frames: usize,
    ) -> PyResult<Self> {
        if n_fft < 2 || n_fft % 2 != 0 || hop == 0 || hop > n_fft || taps == 0 || sample_rate == 0 || tau_s <= 0.0 {
            return Err(PyValueError::new_err("回声消除参数无效"));
        }
        let cfg = aec::EchoCancellerConfig {
            sample_rate,
            n_fft,
            hop,
            taps,
            tau_s,
            solve_every,
            history_s,
            warmup_frames,
        };
        let inner = aec::SubbandEchoCanceller::new(&cfg);
        Ok(Self {
            delay: inner.delay,
            inner: Mutex::new(inner),
            sample_rate,
            n_fft,
            hop,
            taps,
        })
    }

    /// mic, ref：等长的一维数组（任意数值类型，按 float64 计算）。返回同样多的消除后采样（float64）。
    #[pyo3(signature = (mic, r#ref))]
    fn process<'py>(
        &self,
        py: Python<'py>,
        mic: PyArrayLike1<'py, f64, AllowTypeChange>,
        r#ref: PyArrayLike1<'py, f64, AllowTypeChange>,
    ) -> PyResult<Bound<'py, PyArray1<f64>>> {
        if mic.len() != r#ref.len() {
            return Err(PyValueError::new_err("mic 和 ref 长度必须相同"));
        }
        let mic = to_vec(&mic);
        let refs = to_vec(&r#ref);
        let out = py.allow_threads(|| self.inner.lock().unwrap().process(&mic, &refs));
        Ok(PyArray1::from_vec(py, out))
    }

    fn reduction_db(&self, py: Python<'_>) -> Option<f64> {
        py.allow_threads(|| self.inner.lock().unwrap().reduction_db())
    }

    #[pyo3(signature = (window_s=1.5, slice_s=0.3))]
    fn near_end_ratio_db(&self, py: Python<'_>, window_s: f64, slice_s: f64) -> Option<f64> {
        py.allow_threads(|| self.inner.lock().unwrap().near_end_ratio_db(window_s, slice_s))
    }

    #[getter]
    fn delay(&self) -> usize {
        self.delay
    }

    #[getter]
    fn ref_active(&self, py: Python<'_>) -> bool {
        py.allow_threads(|| self.inner.lock().unwrap().ref_active)
    }

    #[getter]
    fn frames(&self, py: Python<'_>) -> u64 {
        py.allow_threads(|| self.inner.lock().unwrap().frames)
    }

    #[getter]
    fn recent_end(&self, py: Python<'_>) -> u64 {
        py.allow_threads(|| self.inner.lock().unwrap().recent_end)
    }

    #[getter]
    fn sample_rate(&self) -> usize {
        self.sample_rate
    }

    #[getter]
    fn n_fft(&self) -> usize {
        self.n_fft
    }

    #[getter]
    fn hop(&self) -> usize {
        self.hop
    }

    #[getter]
    fn taps(&self) -> usize {
        self.taps
    }
}

/// Rust 版 2 阶 Butterworth 高通（与 core/services/audio/highpass.py 的 HighPass 同接口）。
#[pyclass(name = "HighPassFilter", module = "open_xiaoai_server")]
pub struct PyHighPass {
    inner: Mutex<highpass::HighPass>,
    cutoff_hz: f64,
    channels: usize,
}

#[pymethods]
impl PyHighPass {
    #[new]
    #[pyo3(signature = (cutoff_hz, channels=1, sample_rate=16000))]
    fn new(cutoff_hz: f64, channels: usize, sample_rate: usize) -> PyResult<Self> {
        if channels == 0 || sample_rate == 0 || !(cutoff_hz > 0.0 && cutoff_hz < sample_rate as f64 / 2.0) {
            return Err(PyValueError::new_err("高通参数无效"));
        }
        Ok(Self {
            inner: Mutex::new(highpass::HighPass::new(cutoff_hz, channels, sample_rate as f64)),
            cutoff_hz,
            channels,
        })
    }

    /// x：(n,)（单声道）或 (n, channels)；返回同形状 float64。
    fn process<'py>(&self, py: Python<'py>, x: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        // 先看 ndim：PyArrayLike1 会把 (0, k) 的二维数组当成空序列提取成一维
        let ndim: usize = match x.getattr("ndim") {
            Ok(v) => v.extract().unwrap_or(1),
            Err(_) => 1,
        };
        if ndim == 1 {
            let arr = x.extract::<PyArrayLike1<'py, f64, AllowTypeChange>>()?;
            if self.channels != 1 {
                return Err(PyValueError::new_err(format!("需要 (n, {}) 的数组", self.channels)));
            }
            let mut data = to_vec(&arr);
            py.allow_threads(|| self.inner.lock().unwrap().process_interleaved(&mut data));
            return Ok(PyArray1::from_vec(py, data).into_any());
        }
        let arr = x.extract::<PyArrayLike2<'py, f64, AllowTypeChange>>()?;
        let shape = arr.shape();
        let (n, ch) = (shape[0], shape[1]);
        if ch != self.channels {
            return Err(PyValueError::new_err(format!("需要 (n, {}) 的数组，收到 (n, {ch})", self.channels)));
        }
        // 行优先（每行一个采样的各声道）= 交错存放
        let mut data: Vec<f64> = match arr.as_slice() {
            Ok(s) => s.to_vec(),
            Err(_) => arr.as_array().iter().copied().collect(),
        };
        py.allow_threads(|| self.inner.lock().unwrap().process_interleaved(&mut data));
        let out = PyArray1::from_vec(py, data);
        Ok(out.reshape([n, ch])?.into_any())
    }

    fn reset(&self, py: Python<'_>) {
        py.allow_threads(|| self.inner.lock().unwrap().reset())
    }

    /// 二阶节系数 [b0, b1, b2, 1, a1, a2]（同 scipy 的 sos 一行）
    #[getter]
    fn sos<'py>(&self, py: Python<'py>) -> Bound<'py, PyArray2<f64>> {
        let c = self.inner.lock().unwrap().sos();
        PyArray1::from_vec(py, c.to_vec()).reshape([1, 6]).expect("reshape")
    }

    #[getter]
    fn cutoff_hz(&self) -> f64 {
        self.cutoff_hz
    }

    #[getter]
    fn channels(&self) -> usize {
        self.channels
    }
}

pub fn init_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyEchoCanceller>()?;
    m.add_class::<PyHighPass>()?;
    Ok(())
}
