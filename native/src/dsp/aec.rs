//! 子带回声消除（`core/services/audio/aec.py` 的 Rust 版，逐步对照移植）。
//!
//! 算法来自 OwnDing/open-xiaoai（MIT，commit 7ecea6f）：每个频点用最近 `taps` 帧回采乘以
//! 传递函数估计回声，传递函数由指数平均的互谱求解；只在有播放时学习。本项目加的两道保护：
//!   - 前 `warmup_frames` 帧只学习不相减（历史帧不满时求解病态，系数会发散）；
//!   - 每个频点输出幅度不超过原麦克风（回声估计错误时不放大）。
//!
//! 和 Python 版的差别只在实现：
//!   - 所有按频点的量都按 [行][频点] 存放、实部虚部分开，最内层循环走 513 个频点，可以向量化；
//!   - 自相关矩阵是 Hermitian，只存上三角；
//!   - 解方程用批量 Cholesky（`conj(rxx) + reg` 是 Hermitian 正定），numpy 用的是 LU，
//!     两者只差舍入（1e-12 量级）；
//!   - 输入输出缓冲、最近 2 秒历史都是预分配缓冲，不再每次分配。
//! 数值上与 Python 版一致到 1e-6 以内（实测 1e-14 量级，见 scripts/dsp_compare.py）。

use num_complex::Complex64;
use realfft::{ComplexToReal, RealFftPlanner, RealToComplex};
use std::collections::VecDeque;
use std::sync::Arc;

#[derive(Clone, Debug)]
pub struct EchoCancellerConfig {
    pub sample_rate: usize,
    pub n_fft: usize,
    pub hop: usize,
    pub taps: usize,
    pub tau_s: f64,
    pub solve_every: usize,
    pub history_s: f64,
    pub warmup_frames: usize,
}

impl Default for EchoCancellerConfig {
    fn default() -> Self {
        Self {
            sample_rate: 16000,
            n_fft: 1024,
            hop: 256,
            taps: 8,
            tau_s: 3.0,
            solve_every: 4,
            history_s: 2.0,
            warmup_frames: 16,
        }
    }
}

/// 按 [行][频点] 存放的复数矩阵组（实部、虚部分开）
struct Planes {
    re: Vec<f64>,
    im: Vec<f64>,
    bins: usize,
}

impl Planes {
    fn new(rows: usize, bins: usize) -> Self {
        Self { re: vec![0.0; rows * bins], im: vec![0.0; rows * bins], bins }
    }

    #[inline]
    #[cfg_attr(not(test), allow(dead_code))]
    fn row(&self, r: usize) -> (&[f64], &[f64]) {
        let s = r * self.bins..(r + 1) * self.bins;
        (&self.re[s.clone()], &self.im[s])
    }

    #[inline]
    fn row_mut(&mut self, r: usize) -> (&mut [f64], &mut [f64]) {
        let s = r * self.bins..(r + 1) * self.bins;
        (&mut self.re[s.clone()], &mut self.im[s])
    }
}

/// 上三角 (i, j)，j >= i 的行号
#[inline]
fn tri(i: usize, j: usize, taps: usize) -> usize {
    debug_assert!(j >= i);
    i * taps - i * (i + 1) / 2 + j
}

pub struct SubbandEchoCanceller {
    n_fft: usize,
    hop: usize,
    taps: usize,
    bins: usize,
    solve_every: usize,
    warmup_frames: usize,
    sample_rate: usize,
    window: Vec<f64>,
    window_sq: Vec<f64>,
    lam: f64,
    /// 回采历史自相关的上三角（taps(taps+1)/2 行）
    rxx: Planes,
    /// 麦克风与回采历史的互相关（taps 行）
    rxy: Planes,
    /// 当前传递函数（taps 行）
    filter: Planes,
    /// 最近 taps 帧回采频谱（行 0 最新）
    history: Planes,
    pub frames: u64,
    /// 尚未凑满一帧的输入（开头预置 n_fft - hop 个 0，输出长度恒等于输入长度）
    mic_buf: Vec<f64>,
    ref_buf: Vec<f64>,
    overlap: Vec<f64>,
    norm: Vec<f64>,
    /// 已算好但还没返回的输出 / 对应被减掉的回声
    out_pending: Vec<f64>,
    echo_pending: Vec<f64>,
    /// 输入到输出的延迟（采样）
    pub delay: usize,
    recent_cap: usize,
    recent_out: VecDeque<f64>,
    recent_echo: VecDeque<f64>,
    /// 最后一个 recent 采样之后的输出采样序号
    pub recent_end: u64,
    pub ref_active: bool,
    mic_power: f64,
    out_power: f64,
    // FFT
    fwd: Arc<dyn RealToComplex<f64>>,
    inv: Arc<dyn ComplexToReal<f64>>,
    planner: RealFftPlanner<f64>,
    fft_in: Vec<f64>,
    fft_scratch: Vec<Complex64>,
    inv_scratch: Vec<Complex64>,
    spec: Vec<Complex64>,
    y_re: Vec<f64>,
    y_im: Vec<f64>,
    residual: Vec<Complex64>,
    time_out: Vec<f64>,
    /// (1-lam)·history（更新相关矩阵用）
    gh: Planes,
    // 批量 Cholesky 的工作区：L 的下三角（按 tri(k, i) 存 L[i][k]）、对角倒数（taps × bins）、右端
    chol: Planes,
    diag_inv: Vec<f64>,
    rhs: Planes,
    ok: Vec<bool>,
    // 按频点的临时缓冲
    sc_reg: Vec<f64>,
    sc_d: Vec<f64>,
    sc_r: Vec<f64>,
    sc_i: Vec<f64>,
}

impl SubbandEchoCanceller {
    pub fn new(cfg: &EchoCancellerConfig) -> Self {
        let n = cfg.n_fft;
        let hop = cfg.hop;
        let taps = cfg.taps;
        let bins = n / 2 + 1;
        let tri_rows = taps * (taps + 1) / 2;
        // np.sqrt(np.hanning(n + 1)[:n])：hanning(M)[k] = 0.5 - 0.5 cos(2πk / (M - 1))
        let window: Vec<f64> = (0..n)
            .map(|k| {
                let v = 0.5 - 0.5 * (2.0 * std::f64::consts::PI * k as f64 / n as f64).cos();
                v.max(0.0).sqrt()
            })
            .collect();
        let window_sq = window.iter().map(|w| w * w).collect();
        let mut rxx = Planes::new(tri_rows, bins);
        for i in 0..taps {
            rxx.row_mut(tri(i, i, taps)).0.fill(1e-12);
        }
        let mut planner = RealFftPlanner::<f64>::new();
        let fwd = planner.plan_fft_forward(n);
        let inv = planner.plan_fft_inverse(n);
        let fft_scratch = fwd.make_scratch_vec();
        let inv_scratch = inv.make_scratch_vec();
        let recent_cap = (cfg.history_s * cfg.sample_rate as f64) as usize;
        Self {
            n_fft: n,
            hop,
            taps,
            bins,
            solve_every: cfg.solve_every.max(1),
            warmup_frames: cfg.warmup_frames,
            sample_rate: cfg.sample_rate,
            window,
            window_sq,
            lam: (-(hop as f64) / (cfg.sample_rate as f64 * cfg.tau_s)).exp(),
            rxx,
            rxy: Planes::new(taps, bins),
            filter: Planes::new(taps, bins),
            history: Planes::new(taps, bins),
            frames: 0,
            mic_buf: vec![0.0; n - hop],
            ref_buf: vec![0.0; n - hop],
            overlap: vec![0.0; n],
            norm: vec![0.0; n],
            out_pending: vec![0.0; hop],
            echo_pending: vec![0.0; hop],
            delay: n,
            recent_cap,
            recent_out: VecDeque::with_capacity(recent_cap + 1),
            recent_echo: VecDeque::with_capacity(recent_cap + 1),
            recent_end: 0,
            ref_active: false,
            mic_power: 0.0,
            out_power: 0.0,
            fwd,
            inv,
            planner,
            fft_in: vec![0.0; n],
            fft_scratch,
            inv_scratch,
            spec: vec![Complex64::new(0.0, 0.0); bins],
            y_re: vec![0.0; bins],
            y_im: vec![0.0; bins],
            residual: vec![Complex64::new(0.0, 0.0); bins],
            time_out: vec![0.0; n],
            gh: Planes::new(taps, bins),
            chol: Planes::new(tri_rows, bins),
            diag_inv: vec![0.0; taps * bins],
            rhs: Planes::new(taps, bins),
            ok: vec![true; bins],
            sc_reg: vec![0.0; bins],
            sc_d: vec![0.0; bins],
            sc_r: vec![0.0; bins],
            sc_i: vec![0.0; bins],
        }
    }

    pub fn sample_rate(&self) -> usize {
        self.sample_rate
    }

    /// mic、ref 等长；返回同样多的消除回声后的采样（整体延迟 `delay`）。
    pub fn process(&mut self, mic: &[f64], refs: &[f64]) -> Vec<f64> {
        assert_eq!(mic.len(), refs.len(), "mic 和 ref 长度必须相同");
        let n_in = mic.len();
        self.mic_buf.extend_from_slice(mic);
        self.ref_buf.extend_from_slice(refs);
        let (n, hop) = (self.n_fft, self.hop);
        let mut start = 0;
        while self.mic_buf.len() - start >= n {
            self.frame(start);
            start += hop;
        }
        if start > 0 {
            self.mic_buf.drain(..start);
            self.ref_buf.drain(..start);
        }
        let result: Vec<f64> = self.out_pending.drain(..n_in.min(self.out_pending.len())).collect();
        let echo: Vec<f64> = self.echo_pending.drain(..n_in.min(self.echo_pending.len())).collect();
        debug_assert_eq!(result.len(), n_in);
        self.remember(&result, &echo);
        result
    }

    fn frame(&mut self, start: usize) {
        let (n, hop, taps, bins) = (self.n_fft, self.hop, self.taps, self.bins);

        // history[:, 1:] = history[:, :-1]；history[:, 0] = rfft(ref * w)
        self.history.re.copy_within(0..(taps - 1) * bins, bins);
        self.history.im.copy_within(0..(taps - 1) * bins, bins);
        for k in 0..n {
            self.fft_in[k] = self.ref_buf[start + k] * self.window[k];
        }
        self.fwd
            .process_with_scratch(&mut self.fft_in, &mut self.spec, &mut self.fft_scratch)
            .expect("rfft");
        {
            let (hr, hi) = self.history.row_mut(0);
            for b in 0..bins {
                hr[b] = self.spec[b].re;
                hi[b] = self.spec[b].im;
            }
        }
        // y = rfft(mic * w)
        for k in 0..n {
            self.fft_in[k] = self.mic_buf[start + k] * self.window[k];
        }
        self.fwd
            .process_with_scratch(&mut self.fft_in, &mut self.spec, &mut self.fft_scratch)
            .expect("rfft");
        for b in 0..bins {
            self.y_re[b] = self.spec[b].re;
            self.y_im[b] = self.spec[b].im;
        }
        self.ref_active = self.ref_buf[start + n - hop..start + n].iter().any(|&v| v != 0.0);

        if self.ref_active {
            self.update_correlations();
            if self.frames % self.solve_every as u64 == 0 {
                self.solve_filters();
            }
            self.frames += 1;
        }

        if self.frames < self.warmup_frames as u64 {
            for b in 0..bins {
                self.residual[b] = Complex64::new(self.y_re[b], self.y_im[b]);
            }
        } else {
            self.subtract_echo();
        }

        // e = irfft(residual, n) * w；irfft 忽略直流和奈奎斯特频点的虚部
        self.residual[0].im = 0.0;
        self.residual[bins - 1].im = 0.0;
        self.inv
            .process_with_scratch(&mut self.residual, &mut self.time_out, &mut self.inv_scratch)
            .expect("irfft");
        let scale = 1.0 / n as f64;
        for k in 0..n {
            self.overlap[k] += self.time_out[k] * scale * self.window[k];
            self.norm[k] += self.window_sq[k];
        }
        let mic_hop = &self.mic_buf[start..start + hop];
        let mut mic_sq = 0.0;
        let mut out_sq = 0.0;
        for k in 0..hop {
            let out = self.overlap[k] / self.norm[k].max(1e-6);
            self.out_pending.push(out);
            self.echo_pending.push(mic_hop[k] - out);
            mic_sq += mic_hop[k] * mic_hop[k];
            out_sq += out * out;
        }
        self.overlap.copy_within(hop.., 0);
        self.overlap[n - hop..].fill(0.0);
        self.norm.copy_within(hop.., 0);
        self.norm[n - hop..].fill(0.0);
        if self.ref_active {
            self.mic_power = 0.95 * self.mic_power + 0.05 * (mic_sq / hop as f64);
            self.out_power = 0.95 * self.out_power + 0.05 * (out_sq / hop as f64);
        }
    }

    /// rxx = lam·rxx + (1-lam)·h hᴴ（只更新上三角）；rxy = lam·rxy + (1-lam)·y·conj(h)
    ///
    /// 与 numpy 相同的运算顺序：先算 (1-lam)·h_i、(1-lam)·y，再乘 conj(h_j)。
    fn update_correlations(&mut self) {
        let (taps, bins) = (self.taps, self.bins);
        let lam = self.lam;
        let gain = 1.0 - lam;
        for (g, h) in self.gh.re.iter_mut().zip(&self.history.re) {
            *g = *h * gain;
        }
        for (g, h) in self.gh.im.iter_mut().zip(&self.history.im) {
            *g = *h * gain;
        }
        for i in 0..taps {
            let gr = &self.gh.re[i * bins..][..bins];
            let gi = &self.gh.im[i * bins..][..bins];
            for j in i..taps {
                let br = &self.history.re[j * bins..][..bins];
                let bi = &self.history.im[j * bins..][..bins];
                let row = tri(i, j, taps);
                let rr = &mut self.rxx.re[row * bins..][..bins];
                let ri = &mut self.rxx.im[row * bins..][..bins];
                for b in 0..bins {
                    // (a+bi)(c-di) = (ac+bd) + (bc-ad)i
                    rr[b] = rr[b] * lam + (gr[b] * br[b] + gi[b] * bi[b]);
                    ri[b] = ri[b] * lam + (gi[b] * br[b] - gr[b] * bi[b]);
                }
            }
            let ar = &self.history.re[i * bins..][..bins];
            let ai = &self.history.im[i * bins..][..bins];
            let yr = &self.y_re[..bins];
            let yi = &self.y_im[..bins];
            let xr = &mut self.rxy.re[i * bins..][..bins];
            let xi = &mut self.rxy.im[i * bins..][..bins];
            for b in 0..bins {
                let (gyr, gyi) = (yr[b] * gain, yi[b] * gain);
                xr[b] = xr[b] * lam + (gyr * ar[b] + gyi * ai[b]);
                xi[b] = xi[b] * lam + (gyi * ar[b] - gyr * ai[b]);
            }
        }
    }

    /// 每个频点解 (conj(rxx) + reg·I) f = rxy，reg = trace / taps · 1e-3 + 1e-12。
    ///
    /// A = conj(rxx) + reg·I 是 Hermitian 正定：A[i][j] = conj(rxx[i][j])（j >= i），
    /// A[i][k] = rxx[k][i]（i > k）。按频点批量做 Cholesky A = L Lᴴ，再前代 / 回代。
    /// L[i][k]（i >= k）存在 chol 的 tri(k, i) 行。
    fn solve_filters(&mut self) {
        let (taps, bins) = (self.taps, self.bins);
        let reg = &mut self.sc_reg[..bins];
        reg.fill(0.0);
        for i in 0..taps {
            let rr = &self.rxx.re[tri(i, i, taps) * bins..][..bins];
            for b in 0..bins {
                reg[b] += rr[b];
            }
        }
        for v in reg.iter_mut() {
            *v = *v / taps as f64 * 1e-3 + 1e-12;
        }
        self.ok.fill(true);

        for k in 0..taps {
            // d = A[k][k] - Σ_{m<k} |L[k][m]|²
            let d = &mut self.sc_d[..bins];
            d.copy_from_slice(&self.rxx.re[tri(k, k, taps) * bins..][..bins]);
            for b in 0..bins {
                d[b] += reg[b];
            }
            for m in 0..k {
                let row = tri(m, k, taps);
                let lr = &self.chol.re[row * bins..][..bins];
                let li = &self.chol.im[row * bins..][..bins];
                for b in 0..bins {
                    d[b] -= lr[b] * lr[b] + li[b] * li[b];
                }
            }
            {
                let kk = tri(k, k, taps);
                let lr = &mut self.chol.re[kk * bins..][..bins];
                let li = &mut self.chol.im[kk * bins..][..bins];
                let dinv = &mut self.diag_inv[k * bins..][..bins];
                let ok = &mut self.ok[..bins];
                for b in 0..bins {
                    let mut v = d[b];
                    if !(v > 0.0 && v.is_finite()) {
                        ok[b] = false;
                        v = 1.0;
                    }
                    let s = v.sqrt();
                    lr[b] = s;
                    li[b] = 0.0;
                    dinv[b] = 1.0 / s;
                }
            }
            for i in k + 1..taps {
                // L[i][k] = (A[i][k] - Σ_{m<k} L[i][m]·conj(L[k][m])) / L[k][k]，A[i][k] = rxx[k][i]
                let src = tri(k, i, taps);
                let sr = &mut self.sc_r[..bins];
                let si = &mut self.sc_i[..bins];
                sr.copy_from_slice(&self.rxx.re[src * bins..][..bins]);
                si.copy_from_slice(&self.rxx.im[src * bins..][..bins]);
                for m in 0..k {
                    let (p, q) = (tri(m, i, taps), tri(m, k, taps));
                    let pr = &self.chol.re[p * bins..][..bins];
                    let pi = &self.chol.im[p * bins..][..bins];
                    let qr = &self.chol.re[q * bins..][..bins];
                    let qi = &self.chol.im[q * bins..][..bins];
                    for b in 0..bins {
                        // p·conj(q)
                        sr[b] -= pr[b] * qr[b] + pi[b] * qi[b];
                        si[b] -= pi[b] * qr[b] - pr[b] * qi[b];
                    }
                }
                let dinv = &self.diag_inv[k * bins..][..bins];
                let lr = &mut self.chol.re[src * bins..][..bins];
                let li = &mut self.chol.im[src * bins..][..bins];
                for b in 0..bins {
                    lr[b] = sr[b] * dinv[b];
                    li[b] = si[b] * dinv[b];
                }
            }
        }

        // 前代 L z = rxy
        self.rhs.re.copy_from_slice(&self.rxy.re);
        self.rhs.im.copy_from_slice(&self.rxy.im);
        for i in 0..taps {
            let (lo_r, hi_r) = self.rhs.re.split_at_mut(i * bins);
            let (lo_i, hi_i) = self.rhs.im.split_at_mut(i * bins);
            let br = &mut hi_r[..bins];
            let bi = &mut hi_i[..bins];
            for m in 0..i {
                let row = tri(m, i, taps);
                let lr = &self.chol.re[row * bins..][..bins];
                let li = &self.chol.im[row * bins..][..bins];
                let zr = &lo_r[m * bins..][..bins];
                let zi = &lo_i[m * bins..][..bins];
                for b in 0..bins {
                    // b -= L[i][m]·z[m]
                    br[b] -= lr[b] * zr[b] - li[b] * zi[b];
                    bi[b] -= lr[b] * zi[b] + li[b] * zr[b];
                }
            }
            let dinv = &self.diag_inv[i * bins..][..bins];
            for b in 0..bins {
                br[b] *= dinv[b];
                bi[b] *= dinv[b];
            }
        }
        // 回代 Lᴴ f = z：(Lᴴ)[i][m] = conj(L[m][i])，m > i
        for i in (0..taps).rev() {
            let (lo_r, hi_r) = self.rhs.re.split_at_mut((i + 1) * bins);
            let (lo_i, hi_i) = self.rhs.im.split_at_mut((i + 1) * bins);
            let br = &mut lo_r[i * bins..][..bins];
            let bi = &mut lo_i[i * bins..][..bins];
            for m in i + 1..taps {
                let row = tri(i, m, taps);
                let lr = &self.chol.re[row * bins..][..bins];
                let li = &self.chol.im[row * bins..][..bins];
                let fr = &hi_r[(m - i - 1) * bins..][..bins];
                let fi = &hi_i[(m - i - 1) * bins..][..bins];
                for b in 0..bins {
                    // b -= conj(L[m][i])·f[m]
                    br[b] -= lr[b] * fr[b] + li[b] * fi[b];
                    bi[b] -= lr[b] * fi[b] - li[b] * fr[b];
                }
            }
            let dinv = &self.diag_inv[i * bins..][..bins];
            for b in 0..bins {
                br[b] *= dinv[b];
                bi[b] *= dinv[b];
            }
        }
        // 求解失败（理论上有正则不会发生）的频点保留上一次的系数；numpy 会抛 LinAlgError
        let ok = &self.ok[..bins];
        for i in 0..taps {
            let sr = &self.rhs.re[i * bins..][..bins];
            let si = &self.rhs.im[i * bins..][..bins];
            let fr = &mut self.filter.re[i * bins..][..bins];
            let fi = &mut self.filter.im[i * bins..][..bins];
            for b in 0..bins {
                if ok[b] && sr[b].is_finite() && si[b].is_finite() {
                    fr[b] = sr[b];
                    fi[b] = si[b];
                }
            }
        }
    }

    /// residual = y - Σ filter·history；输出幅度不超过麦克风本身
    fn subtract_echo(&mut self) {
        let (taps, bins) = (self.taps, self.bins);
        let er = &mut self.sc_r[..bins];
        let ei = &mut self.sc_i[..bins];
        er.fill(0.0);
        ei.fill(0.0);
        for i in 0..taps {
            let fr = &self.filter.re[i * bins..][..bins];
            let fi = &self.filter.im[i * bins..][..bins];
            let hr = &self.history.re[i * bins..][..bins];
            let hi = &self.history.im[i * bins..][..bins];
            for b in 0..bins {
                er[b] += fr[b] * hr[b] - fi[b] * hi[b];
                ei[b] += fr[b] * hi[b] + fi[b] * hr[b];
            }
        }
        let residual = &mut self.residual[..bins];
        let yr = &self.y_re[..bins];
        let yi = &self.y_im[..bins];
        for b in 0..bins {
            let (rr, ri) = (yr[b] - er[b], yi[b] - ei[b]);
            // 回声估计偏离时不放大（比较平方幅度，省掉 hypot）
            residual[b] = if rr * rr + ri * ri > yr[b] * yr[b] + yi[b] * yi[b] {
                Complex64::new(yr[b], yi[b])
            } else {
                Complex64::new(rr, ri)
            };
        }
    }

    fn remember(&mut self, out: &[f64], echo: &[f64]) {
        push_capped(&mut self.recent_out, out, self.recent_cap);
        push_capped(&mut self.recent_echo, echo, self.recent_cap);
        self.recent_end += out.len() as u64;
    }

    /// 有播放时平滑后的消除量（dB）；还没播放过返回 None。
    pub fn reduction_db(&self) -> Option<f64> {
        if self.mic_power <= 0.0 || self.out_power <= 0.0 {
            return None;
        }
        Some(10.0 * (self.mic_power / self.out_power).log10())
    }

    /// 最近 `window_s` 里，消除后最响的 `slice_s` 一段（300～3400 Hz）比那里被消掉的回声高多少 dB。
    pub fn near_end_ratio_db(&mut self, window_s: f64, slice_s: f64) -> Option<f64> {
        let sr = self.sample_rate as f64;
        let n = self
            .recent_out
            .len()
            .min(self.recent_echo.len())
            .min((window_s * sr) as usize);
        let size = (slice_s * sr) as usize;
        if size == 0 || n < size {
            return None;
        }
        let out: Vec<f64> = self.recent_out.iter().skip(self.recent_out.len() - n).copied().collect();
        let echo: Vec<f64> = self.recent_echo.iter().skip(self.recent_echo.len() - n).copied().collect();
        let step = (size / 3).max(1);
        let fft = self.planner.plan_fft_forward(size);
        let mut input = vec![0.0; size];
        let mut spec = fft.make_output_vec();
        let mut scratch = fft.make_scratch_vec();
        // rfftfreq(size, 1/sr)[k] = k * sr / size；保留 300～3400 Hz
        let band: Vec<usize> = (0..spec.len())
            .filter(|&k| {
                let f = k as f64 * sr / size as f64;
                (300.0..=3400.0).contains(&f)
            })
            .collect();
        let mut band_power = |data: &[f64]| -> f64 {
            input.copy_from_slice(data);
            fft.process_with_scratch(&mut input, &mut spec, &mut scratch).expect("rfft");
            band.iter()
                .map(|&k| {
                    let a = spec[k].norm();
                    a * a
                })
                .sum()
        };
        let mut best: Option<(f64, f64)> = None;
        let mut s = 0;
        while s + size <= n {
            let po = band_power(&out[s..s + size]);
            if best.map_or(true, |(bp, _)| po > bp) {
                let pe = band_power(&echo[s..s + size]);
                best = Some((po, pe));
            }
            s += step;
        }
        let (po, pe) = best?;
        Some(10.0 * ((po + 1e-12) / (pe + 1e-12)).log10())
    }
}

fn push_capped(buf: &mut VecDeque<f64>, data: &[f64], cap: usize) {
    if data.len() >= cap {
        buf.clear();
        buf.extend(&data[data.len() - cap..]);
        return;
    }
    let overflow = (buf.len() + data.len()).saturating_sub(cap);
    buf.drain(..overflow);
    buf.extend(data);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rng(seed: u64) -> impl FnMut() -> f64 {
        let mut s = seed;
        move || {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((s >> 33) as f64 / (1u64 << 31) as f64) - 0.5
        }
    }

    #[test]
    fn output_length_matches_input() {
        let mut aec = SubbandEchoCanceller::new(&EchoCancellerConfig::default());
        for len in [1usize, 100, 256, 512, 1000, 1440, 3] {
            let mic = vec![0.5; len];
            let refs = vec![0.25; len];
            assert_eq!(aec.process(&mic, &refs).len(), len);
        }
    }

    /// 批量 Cholesky 解出的 f 满足 (conj(rxx) + reg) f = rxy
    #[test]
    fn solve_matches_definition() {
        let cfg = EchoCancellerConfig { n_fft: 16, hop: 4, taps: 4, ..Default::default() };
        let mut aec = SubbandEchoCanceller::new(&cfg);
        let mut r = rng(7);
        let (taps, bins) = (aec.taps, aec.bins);
        // 随机历史累积出正定的 rxx
        for _ in 0..50 {
            for v in aec.history.re.iter_mut().chain(aec.history.im.iter_mut()) {
                *v = r();
            }
            for b in 0..bins {
                aec.y_re[b] = r();
                aec.y_im[b] = r();
            }
            aec.update_correlations();
        }
        aec.solve_filters();
        for b in 0..bins {
            let a = |i: usize, j: usize| -> Complex64 {
                if j >= i {
                    let (rr, ri) = aec.rxx.row(tri(i, j, taps));
                    Complex64::new(rr[b], -ri[b])
                } else {
                    let (rr, ri) = aec.rxx.row(tri(j, i, taps));
                    Complex64::new(rr[b], ri[b])
                }
            };
            let trace: f64 = (0..taps).map(|i| aec.rxx.row(tri(i, i, taps)).0[b]).sum();
            let reg = trace / taps as f64 * 1e-3 + 1e-12;
            for i in 0..taps {
                let mut lhs = Complex64::new(0.0, 0.0);
                for j in 0..taps {
                    let mut aij = a(i, j);
                    if i == j {
                        aij.re += reg;
                    }
                    let (fr, fi) = aec.filter.row(j);
                    lhs += aij * Complex64::new(fr[b], fi[b]);
                }
                let (xr, xi) = aec.rxy.row(i);
                let want = Complex64::new(xr[b], xi[b]);
                assert!((lhs - want).norm() <= 1e-9 * (1.0 + want.norm()), "bin {b} row {i}: {lhs} vs {want}");
            }
        }
    }

    /// 各部分耗时：cargo test --release -p open_xiaoai_server --lib profile_parts -- --ignored --nocapture
    #[test]
    #[ignore]
    fn profile_parts() {
        use std::time::Instant;
        let cfg = EchoCancellerConfig::default();
        let n = 16000 * 20;
        let mut r = rng(1);
        let refs: Vec<f64> = (0..n).map(|_| r() * 3000.0).collect();
        let mic: Vec<f64> = (0..n).map(|i| r() * 30.0 + if i >= 40 { refs[i - 40] * 0.3 } else { 0.0 }).collect();
        let mut aec = SubbandEchoCanceller::new(&cfg);
        let t = Instant::now();
        for s in (0..n).step_by(1440) {
            let e = (s + 1440).min(n);
            aec.process(&mic[s..e], &refs[s..e]);
        }
        let total = t.elapsed().as_secs_f64();
        let frames = aec.frames as usize;
        let t = Instant::now();
        for _ in 0..frames / 4 {
            aec.solve_filters();
        }
        let solve = t.elapsed().as_secs_f64();
        let t = Instant::now();
        for _ in 0..frames {
            aec.update_correlations();
        }
        let corr = t.elapsed().as_secs_f64();
        println!(
            "20s 音频（{frames} 帧）：合计 {:.1} ms（单核 {:.2}%）；其中求解 {:.1} ms，相关矩阵更新 {:.1} ms，其余 {:.1} ms",
            total * 1e3,
            total / 20.0 * 100.0,
            solve * 1e3,
            corr * 1e3,
            (total - solve - corr) * 1e3
        );
    }
}
