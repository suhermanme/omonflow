//! Rolling pre-buffer + recording session logic.
//!
//! Pure, hardware-free state machine. The engine thread drains the real-time
//! ring buffer and feeds blocks into `Timeline::push`. Every sample has an
//! absolute index (count of samples ever pushed), which makes "no gaps, no
//! duplicates, correct order" checkable in tests.

use std::collections::VecDeque;

#[derive(Debug, Clone, PartialEq)]
pub enum SessionState {
    Idle,
    Recording,
    /// Key released; waiting until `end_index` samples have been captured.
    Finishing { end_index: u64 },
}

#[derive(Debug, Clone)]
pub struct Finished {
    pub samples: Vec<f32>,
    /// Absolute index of the first sample in `samples`.
    pub start_index: u64,
    /// How many pre-buffer samples were actually available at start.
    pub pre_samples: usize,
}

pub struct Timeline {
    /// Last `capacity` samples, oldest first.
    history: VecDeque<f32>,
    capacity: usize,
    /// Absolute index of the next sample to be pushed.
    next_index: u64,
    state: SessionState,
    session: Vec<f32>,
    session_start: u64,
    session_pre: usize,
    max_session: usize,
    overflowed: bool,
}

impl Timeline {
    /// `capacity`: rolling history size in samples (must cover the largest
    /// pre-buffer). `max_session`: hard cap on one recording, in samples.
    pub fn new(capacity: usize, max_session: usize) -> Self {
        Self {
            history: VecDeque::with_capacity(capacity),
            capacity,
            next_index: 0,
            state: SessionState::Idle,
            session: Vec::new(),
            session_start: 0,
            session_pre: 0,
            max_session,
            overflowed: false,
        }
    }

    pub fn next_index(&self) -> u64 {
        self.next_index
    }

    pub fn state(&self) -> &SessionState {
        &self.state
    }

    pub fn history_len(&self) -> usize {
        self.history.len()
    }

    /// Feed newly captured samples. Returns a finished recording when the
    /// post-release padding has been fully captured.
    pub fn push(&mut self, block: &[f32]) -> Option<Finished> {
        let mut finished = None;
        let mut block = block;

        // Recording: copy into the session, but never past a Finishing end.
        match self.state {
            SessionState::Recording => self.append_session(block),
            SessionState::Finishing { end_index } => {
                let remaining = end_index.saturating_sub(self.next_index) as usize;
                let take = remaining.min(block.len());
                self.append_session(&block[..take]);
                if self.next_index + take as u64 >= end_index {
                    // Push only the part before the boundary first, so
                    // indices stay exact, then finish, then push the rest.
                    self.push_history(&block[..take]);
                    finished = Some(self.take_finished());
                    block = &block[take..];
                }
            }
            SessionState::Idle => {}
        }
        self.push_history(block);
        finished
    }

    fn push_history(&mut self, block: &[f32]) {
        for &s in block {
            if self.history.len() == self.capacity {
                self.history.pop_front();
            }
            self.history.push_back(s);
        }
        self.next_index += block.len() as u64;
    }

    fn append_session(&mut self, block: &[f32]) {
        let room = self.max_session.saturating_sub(self.session.len());
        if block.len() > room {
            self.overflowed = true;
        }
        self.session.extend_from_slice(&block[..block.len().min(room)]);
    }

    /// Key pressed. Seeds the session with up to `pre_samples` of history.
    /// Returns false if a session is already active.
    pub fn start(&mut self, pre_samples: usize) -> bool {
        if self.state != SessionState::Idle {
            return false;
        }
        let pre = pre_samples.min(self.history.len());
        self.session.clear();
        self.session.reserve(pre + 16_000 * 10);
        let skip = self.history.len() - pre;
        self.session.extend(self.history.iter().skip(skip).copied());
        self.session_start = self.next_index - pre as u64;
        self.session_pre = pre;
        self.overflowed = false;
        self.state = SessionState::Recording;
        true
    }

    /// Key released. Recording continues for `post_samples` more samples.
    /// With zero padding the session finishes immediately.
    pub fn stop(&mut self, post_samples: usize) -> Option<Finished> {
        if self.state != SessionState::Recording {
            return None;
        }
        if post_samples == 0 {
            return Some(self.take_finished());
        }
        self.state = SessionState::Finishing {
            end_index: self.next_index + post_samples as u64,
        };
        None
    }

    /// End any active session immediately, keeping what was captured
    /// (used when the device disappears mid-recording).
    pub fn finish_now(&mut self) -> Option<Finished> {
        match self.state {
            SessionState::Idle => None,
            _ => Some(self.take_finished()),
        }
    }

    pub fn cancel(&mut self) {
        self.session = Vec::new();
        self.state = SessionState::Idle;
    }

    pub fn overflowed(&self) -> bool {
        self.overflowed
    }

    fn take_finished(&mut self) -> Finished {
        self.state = SessionState::Idle;
        Finished {
            samples: std::mem::take(&mut self.session),
            start_index: self.session_start,
            pre_samples: self.session_pre,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Feed a ramp where sample value == absolute index, in uneven blocks.
    fn feed(t: &mut Timeline, n: usize, block: usize) -> Vec<Finished> {
        let mut out = Vec::new();
        let mut i = t.next_index();
        let end = i + n as u64;
        while i < end {
            let len = block.min((end - i) as usize);
            let b: Vec<f32> = (i..i + len as u64).map(|x| x as f32).collect();
            if let Some(f) = t.push(&b) {
                out.push(f);
            }
            i += len as u64;
        }
        out
    }

    fn assert_contiguous(f: &Finished) {
        for (k, s) in f.samples.iter().enumerate() {
            assert_eq!(*s as u64, f.start_index + k as u64, "gap/dup at {k}");
        }
    }

    #[test]
    fn pre_buffer_is_included_and_contiguous() {
        let mut t = Timeline::new(8000, 1_000_000);
        feed(&mut t, 20_000, 333);
        assert!(t.start(6400));
        feed(&mut t, 16_000, 512);
        let f = t.stop(0).unwrap();
        assert_eq!(f.pre_samples, 6400);
        assert_eq!(f.start_index, 20_000 - 6400);
        assert_eq!(f.samples.len(), 6400 + 16_000);
        assert_contiguous(&f);
    }

    #[test]
    fn post_padding_ends_exactly_at_boundary() {
        let mut t = Timeline::new(8000, 1_000_000);
        feed(&mut t, 10_000, 480);
        t.start(4000);
        feed(&mut t, 5_000, 480);
        assert!(t.stop(1600).is_none());
        let done = feed(&mut t, 5_000, 777); // boundary falls mid-block
        assert_eq!(done.len(), 1);
        let f = &done[0];
        assert_eq!(f.samples.len(), 4000 + 5000 + 1600);
        assert_contiguous(f);
        // History kept advancing past the boundary.
        assert_eq!(t.next_index(), 20_000);
    }

    #[test]
    fn pre_buffer_clamped_when_engine_just_started() {
        let mut t = Timeline::new(8000, 1_000_000);
        feed(&mut t, 1000, 256);
        t.start(6400);
        feed(&mut t, 1000, 256);
        let f = t.stop(0).unwrap();
        assert_eq!(f.pre_samples, 1000);
        assert_eq!(f.start_index, 0);
        assert_contiguous(&f);
    }

    #[test]
    fn back_to_back_sessions_do_not_leak_old_speech() {
        let mut t = Timeline::new(8000, 1_000_000);
        feed(&mut t, 9000, 512);
        t.start(4000);
        feed(&mut t, 3000, 512);
        let a = t.stop(0).unwrap();
        feed(&mut t, 200, 512); // short gap, less than pre-buffer
        t.start(4000);
        feed(&mut t, 3000, 512);
        let b = t.stop(0).unwrap();
        assert_contiguous(&a);
        assert_contiguous(&b);
        // Second pre-buffer only reaches back 4000 samples: it overlaps the
        // tail of the first session by design, but never earlier.
        assert_eq!(b.start_index, a.start_index + a.samples.len() as u64 + 200 - 4000);
    }

    #[test]
    fn memory_is_bounded() {
        let mut t = Timeline::new(8000, 32_000);
        feed(&mut t, 100_000, 512);
        assert_eq!(t.history_len(), 8000);
        t.start(8000);
        feed(&mut t, 100_000, 512);
        assert!(t.overflowed());
        let f = t.stop(0).unwrap();
        assert_eq!(f.samples.len(), 32_000);
        assert_contiguous(&f);
    }

    #[test]
    fn cancel_and_double_start() {
        let mut t = Timeline::new(8000, 1_000_000);
        feed(&mut t, 9000, 512);
        assert!(t.start(100));
        assert!(!t.start(100));
        t.cancel();
        assert_eq!(*t.state(), SessionState::Idle);
        assert!(t.stop(0).is_none());
    }
}
