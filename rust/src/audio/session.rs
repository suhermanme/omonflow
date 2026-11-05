//! Capture session analysis: warm-up, readiness, noise floor, activity.
//! Pure logic, no hardware. The same rules are mirrored by the Python
//! fallback (src/omonflow/audio/session.py).
//!
//! Readiness (SPEC §6.1): CAPTURE_READY at the first sample that is not exact
//! digital silence, or when the warm-up cap is reached (then `silent_warning`).
//! Activity (UI only): 10 ms RMS windows; after readiness the first
//! `floor_windows` give a noise floor (20th percentile, robust to early
//! speech); activity = RMS above max(min(floor*factor, cap), min_rms) for
//! `hold_windows` consecutive windows. Activity never changes the audio.

#[derive(Clone, Debug)]
pub struct Params {
    pub rate: u32,
    pub warmup_cap_ms: u32,
    pub max_seconds: u32,
    pub floor_windows: usize,
    pub activity_factor: f32,
    pub activity_cap: f32,
    pub activity_min_rms: f32,
    pub hold_windows: u32,
}

impl Params {
    pub fn new(rate: u32) -> Self {
        Self {
            rate,
            // Bluetooth headsets deliver exact zeros for 0.5–1.5 s while macOS switches
            // them to the headset (call) profile; readiness waits for real signal.
            warmup_cap_ms: 2500,
            max_seconds: 600,
            floor_windows: 15,
            activity_factor: 4.0,
            activity_cap: 0.05,
            activity_min_rms: 0.01,
            hold_windows: 6,
        }
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct Flags {
    pub became_ready: bool,
    pub became_active: bool,
}

pub struct Session {
    pub p: Params,
    pub samples: Vec<f32>,
    pub total_in: u64,
    pub overflowed: bool,
    pub ready_at: Option<u64>,
    pub first_nonzero: Option<u64>,
    pub silent_warning: bool,
    pub active_at: Option<u64>,
    win_len: usize,
    win_sumsq: f32,
    win_count: usize,
    floor_rms: Vec<f32>,
    pub floor: Option<f32>,
    hold: u32,
    pub last_rms: f32,
}

impl Session {
    pub fn new(p: Params) -> Self {
        let win_len = (p.rate as usize / 100).max(1);
        let cap = p.rate as usize * p.max_seconds as usize;
        Self {
            samples: Vec::with_capacity((p.rate as usize * 30).min(cap)),
            p,
            total_in: 0,
            overflowed: false,
            ready_at: None,
            first_nonzero: None,
            silent_warning: false,
            active_at: None,
            win_len,
            win_sumsq: 0.0,
            win_count: 0,
            floor_rms: Vec::new(),
            floor: None,
            hold: 0,
            last_rms: 0.0,
        }
    }

    fn max_samples(&self) -> usize {
        self.p.rate as usize * self.p.max_seconds as usize
    }

    /// Feed captured mono samples (device rate). Every sample is kept.
    pub fn push(&mut self, block: &[f32]) -> Flags {
        let mut flags = Flags::default();
        let room = self.max_samples().saturating_sub(self.samples.len());
        if block.len() > room {
            self.overflowed = true;
        }
        self.samples.extend_from_slice(&block[..block.len().min(room)]);

        for &s in block {
            let idx = self.total_in;
            self.total_in += 1;
            if self.first_nonzero.is_none() && s != 0.0 {
                self.first_nonzero = Some(idx);
            }
            if self.ready_at.is_none() {
                let cap = self.p.rate as u64 * self.p.warmup_cap_ms as u64 / 1000;
                if self.first_nonzero.is_some() || idx + 1 >= cap {
                    self.ready_at = Some(idx);
                    self.silent_warning = self.first_nonzero.is_none();
                    flags.became_ready = true;
                }
            }
            self.win_sumsq += s * s;
            self.win_count += 1;
            if self.win_count == self.win_len {
                let rms = (self.win_sumsq / self.win_len as f32).sqrt();
                self.win_sumsq = 0.0;
                self.win_count = 0;
                self.last_rms = rms;
                if self.ready_at.is_some()
                    && self.window_after_ready(idx)
                    && self.analyse(rms)
                    && self.active_at.is_none()
                {
                    self.active_at = Some(idx);
                    flags.became_active = true;
                }
            }
        }
        flags
    }

    fn window_after_ready(&self, idx: u64) -> bool {
        // Only windows that ended at least one window after readiness.
        idx >= self.ready_at.unwrap_or(u64::MAX) + self.win_len as u64 - 1
    }

    fn analyse(&mut self, rms: f32) -> bool {
        if self.floor.is_none() {
            self.floor_rms.push(rms);
            if self.floor_rms.len() >= self.p.floor_windows {
                let mut v = self.floor_rms.clone();
                v.sort_by(|a, b| a.partial_cmp(b).unwrap());
                self.floor = Some(v[v.len() / 5]);
            } else {
                return false;
            }
        }
        let floor = self.floor.unwrap();
        let threshold = (floor * self.p.activity_factor).min(self.p.activity_cap).max(self.p.activity_min_rms);
        if rms > threshold {
            self.hold += 1;
        } else {
            self.hold = 0;
        }
        self.hold >= self.p.hold_windows
    }

    /// Samples with leading exact digital silence removed (device warm-up).
    pub fn trimmed(&self) -> (&[f32], usize) {
        let lead = match self.first_nonzero {
            Some(i) => (i as usize).min(self.samples.len()),
            None => self.samples.len(),
        };
        (&self.samples[lead..], lead)
    }

    pub fn ms(&self, samples: u64) -> f64 {
        samples as f64 * 1000.0 / self.p.rate as f64
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(n: usize, amp: f32, rate: u32) -> Vec<f32> {
        (0..n).map(|i| amp * (i as f32 * 440.0 * std::f32::consts::TAU / rate as f32).sin()).collect()
    }

    fn noise(n: usize, amp: f32, seed: &mut u32) -> Vec<f32> {
        (0..n)
            .map(|_| {
                *seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                amp * ((*seed >> 8) as f32 / (1u32 << 24) as f32 * 2.0 - 1.0)
            })
            .collect()
    }

    #[test]
    fn ready_waits_for_real_signal_and_keeps_warmup() {
        let mut s = Session::new(Params::new(48000));
        let f = s.push(&vec![0.0; 4800]); // 100 ms exact zeros
        assert!(!f.became_ready);
        let mut seed = 1;
        let f = s.push(&noise(480, 0.002, &mut seed));
        assert!(f.became_ready);
        assert_eq!(s.ready_at, Some(4800));
        assert!(!s.silent_warning);
        assert_eq!(s.samples.len(), 5280, "warm-up samples are kept until finalize");
        let (trimmed, lead) = s.trimmed();
        assert_eq!(lead, 4800);
        assert_eq!(trimmed.len(), 480);
    }

    #[test]
    fn warmup_cap_gives_ready_with_warning() {
        let mut s = Session::new(Params::new(16000));
        // A Bluetooth headset switching profile: 1.5 s of exact zeros is not "ready" yet.
        assert!(!s.push(&vec![0.0; 16000 * 15 / 10]).became_ready);
        let f = s.push(&vec![0.0; 16000]);
        assert!(f.became_ready && s.silent_warning);
    }

    #[test]
    fn bluetooth_silence_then_signal_is_ready_without_warning() {
        let mut s = Session::new(Params::new(16000));
        assert!(!s.push(&vec![0.0; 16000]).became_ready);
        let f = s.push(&vec![0.01; 160]);
        assert!(f.became_ready && !s.silent_warning);
    }

    #[test]
    fn activity_needs_speech_above_floor() {
        let rate = 48000;
        let mut s = Session::new(Params::new(rate));
        let mut seed = 7;
        // 400 ms room noise: ready, floor learned, no activity.
        let f = s.push(&noise(rate as usize * 4 / 10, 0.003, &mut seed));
        assert!(f.became_ready && !f.became_active);
        assert!(s.floor.unwrap() < 0.003);
        // A 30 ms click is too short.
        assert!(!s.push(&tone(1440, 0.3, rate)).became_active);
        assert!(!s.push(&noise(4800, 0.003, &mut seed)).became_active);
        // 200 ms of speech-level signal triggers activity once.
        let f = s.push(&tone(9600, 0.1, rate));
        assert!(f.became_active);
        assert!(!s.push(&tone(9600, 0.1, rate)).became_active);
    }

    #[test]
    fn loud_room_does_not_flag_activity() {
        let rate = 16000;
        let mut s = Session::new(Params::new(rate));
        let mut seed = 3;
        let f = s.push(&noise(rate as usize * 2, 0.02, &mut seed));
        assert!(f.became_ready && !f.became_active, "steady noise is not speech");
    }

    #[test]
    fn bounded_length() {
        let mut p = Params::new(1000);
        p.max_seconds = 2;
        let mut s = Session::new(p);
        s.push(&vec![0.1; 5000]);
        assert_eq!(s.samples.len(), 2000);
        assert!(s.overflowed);
        assert_eq!(s.total_in, 5000);
    }

    #[test]
    fn order_preserved_across_blocks() {
        let mut s = Session::new(Params::new(8000));
        let mut next = 1.0f32;
        for len in [1usize, 7, 64, 333, 1000, 3] {
            let b: Vec<f32> = (0..len).map(|_| { next += 1.0; next }).collect();
            s.push(&b);
        }
        assert!(s.samples.windows(2).all(|w| w[1] == w[0] + 1.0));
    }
}
