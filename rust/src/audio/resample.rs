//! Band-limited resampling (Hann-windowed sinc, tabulated kernel) and WAV.

const ZERO_CROSSINGS: f64 = 16.0;
const TABLE_RES: usize = 512; // kernel samples per input sample at unit cutoff

/// Resample mono `input` from `from` Hz to `to` Hz.
pub fn resample(input: &[f32], from: u32, to: u32) -> Vec<f32> {
    if from == to || input.is_empty() {
        return input.to_vec();
    }
    let ratio = to as f64 / from as f64;
    // Cutoff in cycles per input sample; 0.95 leaves a transition band.
    let cutoff = 0.5 * ratio.min(1.0) * 0.95;
    let scale = 2.0 * cutoff; // kernel argument scale
    let half_width = ZERO_CROSSINGS / scale; // in input samples
    let table_len = (ZERO_CROSSINGS * TABLE_RES as f64) as usize + 2;
    let table: Vec<f64> = (0..table_len)
        .map(|i| {
            let x = i as f64 / TABLE_RES as f64; // in zero crossings
            if x >= ZERO_CROSSINGS {
                return 0.0;
            }
            let sinc = if x == 0.0 { 1.0 } else { (std::f64::consts::PI * x).sin() / (std::f64::consts::PI * x) };
            let w = 0.5 + 0.5 * (std::f64::consts::PI * x / ZERO_CROSSINGS).cos();
            sinc * w
        })
        .collect();
    let kernel = |d: f64| -> f64 {
        let x = (d * scale).abs() * TABLE_RES as f64;
        let i = x as usize;
        if i + 1 >= table.len() {
            return 0.0;
        }
        let f = x - i as f64;
        table[i] * (1.0 - f) + table[i + 1] * f
    };
    let out_len = (input.len() as f64 * ratio).round() as usize;
    let last = input.len() as isize - 1;
    let mut out = Vec::with_capacity(out_len);
    for n in 0..out_len {
        let t = n as f64 / ratio;
        let lo = (t - half_width).ceil() as isize;
        let hi = (t + half_width).floor() as isize;
        let mut acc = 0.0f64;
        for i in lo.max(0)..=hi.min(last) {
            acc += input[i as usize] as f64 * kernel(t - i as f64);
        }
        out.push((acc * scale) as f32);
    }
    out
}

/// 16-bit PCM mono WAV.
pub fn wav_s16(samples: &[f32], rate: u32) -> Vec<u8> {
    let data_len = samples.len() * 2;
    let mut v = Vec::with_capacity(44 + data_len);
    v.extend_from_slice(b"RIFF");
    v.extend_from_slice(&((36 + data_len) as u32).to_le_bytes());
    v.extend_from_slice(b"WAVEfmt ");
    v.extend_from_slice(&16u32.to_le_bytes());
    v.extend_from_slice(&1u16.to_le_bytes()); // PCM
    v.extend_from_slice(&1u16.to_le_bytes()); // mono
    v.extend_from_slice(&rate.to_le_bytes());
    v.extend_from_slice(&(rate * 2).to_le_bytes());
    v.extend_from_slice(&2u16.to_le_bytes());
    v.extend_from_slice(&16u16.to_le_bytes());
    v.extend_from_slice(b"data");
    v.extend_from_slice(&(data_len as u32).to_le_bytes());
    for &s in samples {
        let q = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
        v.extend_from_slice(&q.to_le_bytes());
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(freq: f64, rate: u32, secs: f64, amp: f64) -> Vec<f32> {
        (0..(rate as f64 * secs) as usize)
            .map(|i| (amp * (std::f64::consts::TAU * freq * i as f64 / rate as f64).sin()) as f32)
            .collect()
    }

    fn rms(x: &[f32]) -> f64 {
        (x.iter().map(|&v| (v as f64).powi(2)).sum::<f64>() / x.len() as f64).sqrt()
    }

    #[test]
    fn length_and_passband() {
        for from in [48000u32, 44100, 32000, 22050, 8000] {
            let x = sine(1000.0, from, 1.0, 0.5);
            let y = resample(&x, from, 16000);
            assert!((y.len() as i64 - 16000).abs() <= 1, "{from}: {}", y.len());
            // Ignore edges; a 1 kHz tone keeps its level (0.5 amp → 0.3536 rms).
            let mid = &y[1000..15000];
            assert!((rms(mid) - 0.3536).abs() < 0.01, "{from}: rms {}", rms(mid));
        }
    }

    #[test]
    fn stopband_attenuated() {
        // 10 kHz is above the 8 kHz Nyquist of 16 kHz: must be removed, not aliased.
        let y = resample(&sine(10000.0, 48000, 1.0, 0.5), 48000, 16000);
        assert!(rms(&y[1000..15000]) < 0.005, "rms {}", rms(&y[1000..15000]));
    }

    #[test]
    fn wav_header() {
        let w = wav_s16(&[0.0, 1.0, -1.0], 16000);
        assert_eq!(&w[0..4], b"RIFF");
        assert_eq!(w.len(), 44 + 6);
        assert_eq!(i16::from_le_bytes([w[46], w[47]]), 32767);
    }
}
