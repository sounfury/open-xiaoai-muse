//! 2 阶 Butterworth 高通（二阶节，Direct Form II transposed，与 scipy.signal.sosfilt 相同）。
//!
//! 系数按 `scipy.signal.butter(2, cutoff, btype="highpass", fs=fs, output="sos")` 的结果计算：
//! 双线性变换 + 预畸变，K = tan(π·fc/fs)：
//!   b = [1, -2, 1] / (1 + √2·K + K²)，a = [1, 2(K² - 1), 1 - √2·K + K²] / (1 + √2·K + K²)
//! 跨包保留状态，每声道独立；状态从 0 开始（不按第一个采样预置）。

pub struct HighPass {
    b: [f64; 3],
    a: [f64; 2],
    channels: usize,
    /// 每声道两个状态
    zi: Vec<[f64; 2]>,
}

impl HighPass {
    pub fn new(cutoff_hz: f64, channels: usize, sample_rate: f64) -> Self {
        let k = (std::f64::consts::PI * cutoff_hz / sample_rate).tan();
        let s2 = std::f64::consts::SQRT_2;
        let norm = 1.0 / (1.0 + s2 * k + k * k);
        Self {
            b: [norm, -2.0 * norm, norm],
            a: [2.0 * (k * k - 1.0) * norm, (1.0 - s2 * k + k * k) * norm],
            channels,
            zi: vec![[0.0; 2]; channels],
        }
    }

    pub fn sos(&self) -> [f64; 6] {
        [self.b[0], self.b[1], self.b[2], 1.0, self.a[0], self.a[1]]
    }

    /// data：按采样交错存放的各声道（单声道就是普通序列），原地滤波。
    pub fn process_interleaved(&mut self, data: &mut [f64]) {
        let ch = self.channels;
        let [b0, b1, b2] = self.b;
        let [a1, a2] = self.a;
        for c in 0..ch {
            let [mut z0, mut z1] = self.zi[c];
            let mut i = c;
            while i < data.len() {
                let x = data[i];
                let y = b0 * x + z0;
                z0 = b1 * x - a1 * y + z1;
                z1 = b2 * x - a2 * y;
                data[i] = y;
                i += ch;
            }
            self.zi[c] = [z0, z1];
        }
    }

    pub fn reset(&mut self) {
        for z in &mut self.zi {
            *z = [0.0; 2];
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn coefficients_match_scipy_120hz() {
        // scipy.signal.butter(2, 120, btype="highpass", fs=16000, output="sos")
        let hp = HighPass::new(120.0, 1, 16000.0);
        let sos = hp.sos();
        let want = [
            0.9672272827147773, -1.9344545654295546, 0.9672272827147773,
            1.0, -1.9333802258799302, 0.9355289049791784,
        ];
        for (g, w) in sos.iter().zip(want) {
            assert!((g - w).abs() < 1e-14, "{sos:?}");
        }
    }

    #[test]
    fn removes_dc() {
        let mut hp = HighPass::new(120.0, 2, 16000.0);
        let mut data = vec![1000.0; 2 * 16000];
        hp.process_interleaved(&mut data);
        assert!(data[data.len() - 1].abs() < 1e-3);
        assert!(data[data.len() - 2].abs() < 1e-3);
    }
}
