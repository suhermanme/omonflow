//! On-demand capture engine (ARCHITECTURE §3).
//!
//! One worker thread per capture attempt. The worker opens the source (which
//! may hang: CoreAudio opens have been seen to take >10 s), owns the stream,
//! drains the real-time ring into a `Session`, and closes the stream before
//! reporting "stopped". A worker that misses the ready timeout is *retired*:
//! never joined on the caller's path, reaped later if it ever returns.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

use super::session::{Params, Session};

const RING_SECONDS: usize = 2;
const DRAIN_EVERY: Duration = Duration::from_millis(5);
const MAX_EVENTS: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Opening,
    Warming,
    Ready,
    Active,
    Stopping,
    Stopped,
    Failed,
}

impl Phase {
    pub fn name(self) -> &'static str {
        match self {
            Phase::Opening => "opening",
            Phase::Warming => "warming",
            Phase::Ready => "ready",
            Phase::Active => "active",
            Phase::Stopping => "stopping",
            Phase::Stopped => "stopped",
            Phase::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug)]
pub enum FailKind {
    DeviceNotFound,
    OpenFailed,
}

#[derive(Clone, Debug)]
pub struct Event {
    pub kind: &'static str, // ready | activity | device_lost | xrun | error
    pub detail: String,
    pub at_ms: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Timing {
    pub open_ms: Option<f64>,
    pub first_audio_ms: Option<f64>,
    pub ready_ms: Option<f64>,
    pub warmup_ms: Option<f64>,
    pub activity_ms: Option<f64>,
    pub stop_requested_ms: Option<f64>,
    pub stream_closed_ms: Option<f64>,
}

pub struct Inner {
    pub phase: Phase,
    pub session: Option<Session>,
    pub timing: Timing,
    pub events: VecDeque<Event>,
    pub fail: Option<(FailKind, String)>,
    pub device_name: String,
    pub device_rate: u32,
    pub silent_warning: bool,
}

/// State shared between the caller and one worker.
pub struct Shared {
    pub inner: Mutex<Inner>,
    pub cv: Condvar,
    pub stop: AtomicBool,
    pub cancel: AtomicBool,
    pub stream_open: AtomicBool,
    pub device_lost: AtomicBool,
    pub dropped: AtomicU64,
    pub xruns: AtomicU64,
    pub callbacks: AtomicU64,
    pub level_bits: AtomicU32,
    pub started: Instant,
}

impl Shared {
    fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                phase: Phase::Opening,
                session: None,
                timing: Timing::default(),
                events: VecDeque::new(),
                fail: None,
                device_name: String::new(),
                device_rate: 0,
                silent_warning: false,
            }),
            cv: Condvar::new(),
            stop: AtomicBool::new(false),
            cancel: AtomicBool::new(false),
            stream_open: AtomicBool::new(false),
            device_lost: AtomicBool::new(false),
            dropped: AtomicU64::new(0),
            xruns: AtomicU64::new(0),
            callbacks: AtomicU64::new(0),
            level_bits: AtomicU32::new(0),
            started: Instant::now(),
        }
    }

    pub fn elapsed_ms(&self) -> f64 {
        self.started.elapsed().as_secs_f64() * 1000.0
    }

    pub fn set_phase(&self, inner: &mut Inner, phase: Phase) {
        inner.phase = phase;
        self.cv.notify_all();
    }

    fn event(&self, inner: &mut Inner, kind: &'static str, detail: String) {
        if inner.events.len() >= MAX_EVENTS {
            inner.events.pop_front();
        }
        inner.events.push_back(Event { kind, detail, at_ms: self.elapsed_ms() });
    }

    /// Smoothed level for UI meters (0..1).
    pub fn level(&self) -> f32 {
        f32::from_bits(self.level_bits.load(Ordering::Relaxed))
    }
}

// ---------------------------------------------------------------- sources

/// Synthetic source for tests and CI: simulates slow/hung opens, warm-up
/// silence, speech onset and device loss without any hardware.
#[derive(Clone, Debug)]
pub struct FakeSpec {
    pub rate: u32,
    pub open_delay_ms: u64,
    pub hang_ms: u64, // ignores cancel while "stuck in the driver"
    pub hangs_left: Arc<AtomicU32>, // how many opens hang (shared across workers)
    pub fail_open: Option<String>,
    pub warmup_zero_ms: u64,
    pub noise: f32,
    pub speech_after_ms: Option<u64>,
    pub speech_amp: f32,
    pub lose_after_ms: Option<u64>,
    pub block: usize,
}

impl Default for FakeSpec {
    fn default() -> Self {
        Self {
            rate: 48000,
            open_delay_ms: 20,
            hang_ms: 0,
            hangs_left: Arc::new(AtomicU32::new(u32::MAX)),
            fail_open: None,
            warmup_zero_ms: 0,
            noise: 0.003,
            speech_after_ms: Some(300),
            speech_amp: 0.1,
            lose_after_ms: None,
            block: 480,
        }
    }
}

#[derive(Clone, Debug)]
pub enum Source {
    Device(Option<String>), // cpal DeviceId string, None = system default
    Fake(FakeSpec),
}

enum StreamGuard {
    // Held only so that dropping the guard closes the stream (and the mic).
    #[allow(dead_code)]
    Cpal(cpal::Stream),
    Fake { stop: Arc<AtomicBool>, join: Option<JoinHandle<()>> },
}

impl Drop for StreamGuard {
    fn drop(&mut self) {
        if let StreamGuard::Fake { stop, join } = self {
            stop.store(true, Ordering::Relaxed);
            if let Some(j) = join.take() {
                let _ = j.join();
            }
        }
    }
}

pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub is_default: bool,
    pub rate: u32,
    pub channels: u16,
    pub format: String,
}

pub fn list_devices() -> Result<Vec<DeviceInfo>, String> {
    let host = cpal::default_host();
    let default_id = host.default_input_device().and_then(|d| d.id().ok()).map(|i| i.to_string());
    let mut out = Vec::new();
    for dev in host.input_devices().map_err(|e| e.to_string())? {
        let (Ok(id), Ok(desc), Ok(cfg)) = (dev.id(), dev.description(), dev.default_input_config()) else {
            continue;
        };
        let id = id.to_string();
        out.push(DeviceInfo {
            is_default: Some(&id) == default_id.as_ref(),
            id,
            name: desc.name().to_string(),
            rate: cfg.sample_rate(),
            channels: cfg.channels(),
            format: format!("{:?}", cfg.sample_format()),
        });
    }
    Ok(out)
}

type Producer = rtrb::Producer<f32>;
type Opened = (StreamGuard, rtrb::Consumer<f32>, u32, String);
type OpenResult = Result<Opened, (FailKind, String)>;

fn open_cpal(id: &Option<String>, shared: &Arc<Shared>) -> OpenResult {
    let host = cpal::default_host();
    let device = match id {
        None => host.default_input_device(),
        Some(s) => {
            let did: cpal::DeviceId = s.parse().map_err(|_| (FailKind::DeviceNotFound, format!("bad device id {s}")))?;
            host.device_by_id(&did)
        }
    }
    .ok_or((FailKind::DeviceNotFound, "input device not found".to_string()))?;
    let name = device.description().map(|d| d.name().to_string()).unwrap_or_default();
    let cfg = device.default_input_config().map_err(|e| (FailKind::OpenFailed, format!("input config: {e}")))?;
    let rate = cfg.sample_rate();
    let channels = cfg.channels() as usize;
    let (producer, consumer) = rtrb::RingBuffer::<f32>::new(rate as usize * RING_SECONDS);

    let err_shared = shared.clone();
    let on_error = move |e: cpal::Error| {
        match e.kind() {
            cpal::ErrorKind::DeviceNotAvailable => err_shared.device_lost.store(true, Ordering::Relaxed),
            cpal::ErrorKind::Xrun => {
                err_shared.xruns.fetch_add(1, Ordering::Relaxed);
            }
            _ => {
                let mut inner = err_shared.inner.lock().unwrap();
                err_shared.event(&mut inner, "error", e.to_string());
            }
        }
    };
    use cpal::SampleFormat as F;
    let stream = match cfg.sample_format() {
        F::F32 => build::<f32>(&device, cfg.config(), channels, producer, shared.clone(), on_error),
        F::I16 => build::<i16>(&device, cfg.config(), channels, producer, shared.clone(), on_error),
        F::I32 => build::<i32>(&device, cfg.config(), channels, producer, shared.clone(), on_error),
        F::U16 => build::<u16>(&device, cfg.config(), channels, producer, shared.clone(), on_error),
        other => return Err((FailKind::OpenFailed, format!("unsupported sample format {other:?}"))),
    }
    .map_err(|e| (FailKind::OpenFailed, format!("build stream: {e}")))?;
    stream.play().map_err(|e| (FailKind::OpenFailed, format!("start stream: {e}")))?;
    Ok((StreamGuard::Cpal(stream), consumer, rate, name))
}

fn build<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    channels: usize,
    mut producer: Producer,
    shared: Arc<Shared>,
    on_error: impl FnMut(cpal::Error) + Send + 'static,
) -> Result<cpal::Stream, cpal::Error>
where
    T: cpal::SizedSample,
    f32: cpal::FromSample<T>,
{
    device.build_input_stream::<T, _, _>(
        config,
        move |data: &[T], _| {
            // Real-time callback: convert, downmix, push. No locks, no allocation.
            for frame in data.chunks_exact(channels) {
                let mut sum = 0.0f32;
                for &s in frame {
                    sum += <f32 as cpal::FromSample<T>>::from_sample_(s);
                }
                if producer.push(sum / channels as f32).is_err() {
                    shared.dropped.fetch_add(1, Ordering::Relaxed);
                }
            }
            shared.callbacks.fetch_add(1, Ordering::Relaxed);
        },
        on_error,
        None,
    )
}

fn open_fake(f: &FakeSpec, shared: &Arc<Shared>) -> OpenResult {
    if f.hang_ms > 0 && f.hangs_left.try_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1)).is_ok() {
        std::thread::sleep(Duration::from_millis(f.hang_ms)); // stuck: ignores cancel
    }
    let deadline = Instant::now() + Duration::from_millis(f.open_delay_ms);
    while Instant::now() < deadline {
        if shared.cancel.load(Ordering::Relaxed) {
            return Err((FailKind::OpenFailed, "cancelled".into()));
        }
        std::thread::sleep(Duration::from_millis(2));
    }
    if let Some(msg) = &f.fail_open {
        return Err((FailKind::OpenFailed, msg.clone()));
    }
    let (mut producer, consumer) = rtrb::RingBuffer::<f32>::new(f.rate as usize * RING_SECONDS);
    let stop = Arc::new(AtomicBool::new(false));
    let (stop2, spec, sh) = (stop.clone(), f.clone(), shared.clone());
    let join = std::thread::spawn(move || {
        let t0 = Instant::now();
        let mut n: u64 = 0;
        let mut seed: u32 = 12345;
        let block_dur = Duration::from_secs_f64(spec.block as f64 / spec.rate as f64);
        while !stop2.load(Ordering::Relaxed) {
            let ms = n * 1000 / spec.rate as u64;
            if spec.lose_after_ms.is_some_and(|l| ms >= l) {
                sh.device_lost.store(true, Ordering::Relaxed);
                break;
            }
            for _ in 0..spec.block {
                let ms = n * 1000 / spec.rate as u64;
                let v = if ms < spec.warmup_zero_ms {
                    0.0
                } else {
                    seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                    let noise = spec.noise * ((seed >> 8) as f32 / (1u32 << 24) as f32 * 2.0 - 1.0);
                    let speech = match spec.speech_after_ms {
                        Some(s) if ms >= s => {
                            spec.speech_amp * (n as f32 * 300.0 * std::f32::consts::TAU / spec.rate as f32).sin()
                        }
                        _ => 0.0,
                    };
                    noise + speech
                };
                if producer.push(v).is_err() {
                    sh.dropped.fetch_add(1, Ordering::Relaxed);
                }
                n += 1;
            }
            sh.callbacks.fetch_add(1, Ordering::Relaxed);
            // Real-time pacing.
            let due = t0 + block_dur * (n / spec.block as u64) as u32;
            if let Some(wait) = due.checked_duration_since(Instant::now()) {
                std::thread::sleep(wait);
            }
        }
    });
    Ok((StreamGuard::Fake { stop, join: Some(join) }, consumer, f.rate, "Fake microphone".into()))
}

// ---------------------------------------------------------------- worker

pub struct Worker {
    pub shared: Arc<Shared>,
    pub join: Option<JoinHandle<()>>,
}

impl Worker {
    pub fn spawn(source: Source, mut params: Params) -> Result<Worker, String> {
        let shared = Arc::new(Shared::new());
        let sh = shared.clone();
        let join = std::thread::Builder::new()
            .name("omonflow-capture".into())
            .spawn(move || run(source, &mut params, sh))
            .map_err(|e| e.to_string())?;
        Ok(Worker { shared, join: Some(join) })
    }

    pub fn finished(&self) -> bool {
        self.join.as_ref().is_none_or(|j| j.is_finished())
    }

    /// Join if finished (or within `wait`); returns true if joined.
    pub fn try_join(&mut self, wait: Duration) -> bool {
        let deadline = Instant::now() + wait;
        while !self.finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(2));
        }
        if self.finished() {
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
            true
        } else {
            false
        }
    }
}

fn run(source: Source, params: &mut Params, shared: Arc<Shared>) {
    let opened = match &source {
        Source::Device(id) => open_cpal(id, &shared),
        Source::Fake(f) => open_fake(f, &shared),
    };
    let (guard, mut consumer, rate, name) = match opened {
        Ok(v) => v,
        Err((kind, msg)) => {
            let mut inner = shared.inner.lock().unwrap();
            inner.fail = Some((kind, msg));
            shared.set_phase(&mut inner, Phase::Failed);
            return;
        }
    };
    shared.stream_open.store(true, Ordering::SeqCst);
    params.rate = rate;
    {
        let mut inner = shared.inner.lock().unwrap();
        if shared.cancel.load(Ordering::Relaxed) {
            // Opened only after the caller gave up (retired): close at once.
            drop(inner);
            drop(guard);
            shared.stream_open.store(false, Ordering::SeqCst);
            let mut inner = shared.inner.lock().unwrap();
            shared.set_phase(&mut inner, Phase::Stopped);
            return;
        }
        inner.session = Some(Session::new(params.clone()));
        inner.device_name = name;
        inner.device_rate = rate;
        inner.timing.open_ms = Some(shared.elapsed_ms());
        shared.set_phase(&mut inner, Phase::Warming);
    }

    let mut scratch: Vec<f32> = Vec::with_capacity(rate as usize);
    let mut guard = Some(guard);
    loop {
        let stop = shared.stop.load(Ordering::Relaxed);
        let cancel = shared.cancel.load(Ordering::Relaxed);
        let lost = shared.device_lost.load(Ordering::Relaxed);
        if stop || cancel || lost {
            // Close the stream FIRST, then take the last samples.
            drop(guard.take());
            shared.stream_open.store(false, Ordering::SeqCst);
            scratch.clear();
            while let Ok(s) = consumer.pop() {
                scratch.push(s);
            }
            let mut inner = shared.inner.lock().unwrap();
            inner.timing.stream_closed_ms = Some(shared.elapsed_ms());
            if !cancel {
                if let Some(sess) = inner.session.as_mut() {
                    sess.push(&scratch);
                }
            } else {
                inner.session = None; // cancel discards audio
            }
            if lost && !cancel {
                shared.event(&mut inner, "device_lost", "input device disconnected".into());
            }
            shared.set_phase(&mut inner, Phase::Stopped);
            return;
        }

        scratch.clear();
        while let Ok(s) = consumer.pop() {
            scratch.push(s);
        }
        if !scratch.is_empty() {
            let mut inner = shared.inner.lock().unwrap();
            let now = shared.elapsed_ms();
            if inner.timing.first_audio_ms.is_none() {
                inner.timing.first_audio_ms = Some(now);
            }
            let (flags, rms, ready_at, active_at, warn, srate) = {
                let s = inner.session.as_mut().unwrap();
                let f = s.push(&scratch);
                (f, s.last_rms, s.ready_at, s.active_at, s.silent_warning, s.p.rate)
            };
            // UI level: RMS scaled into 0..1 (speech ≈ 0.05–0.2 RMS).
            shared.level_bits.store((rms * 6.0).min(1.0).to_bits(), Ordering::Relaxed);
            let ms = |x: u64| x as f64 * 1000.0 / srate as f64;
            if flags.became_ready {
                inner.timing.ready_ms = Some(now);
                inner.timing.warmup_ms = ready_at.map(ms);
                inner.silent_warning = warn;
                shared.event(&mut inner, "ready", if warn { "silent".into() } else { String::new() });
                shared.set_phase(&mut inner, Phase::Ready);
            }
            if flags.became_active {
                inner.timing.activity_ms = Some(now);
                let _ = active_at;
                shared.event(&mut inner, "activity", String::new());
                shared.set_phase(&mut inner, Phase::Active);
            }
        }
        std::thread::sleep(DRAIN_EVERY);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wait_phase(w: &Worker, want: &[Phase], ms: u64) -> Phase {
        let deadline = Instant::now() + Duration::from_millis(ms);
        let mut inner = w.shared.inner.lock().unwrap();
        while !want.contains(&inner.phase) {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            inner = w.shared.cv.wait_timeout(inner, left).unwrap().0;
        }
        inner.phase
    }

    #[test]
    fn fake_lifecycle_keeps_every_sample() {
        let spec = FakeSpec { warmup_zero_ms: 100, speech_after_ms: Some(250), ..Default::default() };
        let mut w = Worker::spawn(Source::Fake(spec), Params::new(48000)).unwrap();
        assert_eq!(wait_phase(&w, &[Phase::Ready], 2000), Phase::Ready);
        assert_eq!(wait_phase(&w, &[Phase::Active], 2000), Phase::Active);
        w.shared.stop.store(true, Ordering::Relaxed);
        assert_eq!(wait_phase(&w, &[Phase::Stopped], 2000), Phase::Stopped);
        assert!(w.try_join(Duration::from_millis(500)));
        assert!(!w.shared.stream_open.load(Ordering::SeqCst));
        let inner = w.shared.inner.lock().unwrap();
        let s = inner.session.as_ref().unwrap();
        assert_eq!(s.samples.len() as u64, s.total_in, "nothing dropped between callback and session");
        assert_eq!(w.shared.dropped.load(Ordering::Relaxed), 0);
        let warm = inner.timing.warmup_ms.unwrap();
        assert!((95.0..=115.0).contains(&warm), "warm-up {warm}");
    }

    #[test]
    fn cancel_discards() {
        let w = Worker::spawn(Source::Fake(FakeSpec::default()), Params::new(48000)).unwrap();
        wait_phase(&w, &[Phase::Ready], 2000);
        w.shared.cancel.store(true, Ordering::Relaxed);
        assert_eq!(wait_phase(&w, &[Phase::Stopped], 2000), Phase::Stopped);
        assert!(w.shared.inner.lock().unwrap().session.is_none());
    }

    #[test]
    fn device_loss_keeps_partial_audio() {
        let spec = FakeSpec { lose_after_ms: Some(400), ..Default::default() };
        let w = Worker::spawn(Source::Fake(spec), Params::new(48000)).unwrap();
        assert_eq!(wait_phase(&w, &[Phase::Stopped], 3000), Phase::Stopped);
        let inner = w.shared.inner.lock().unwrap();
        assert!(inner.session.as_ref().unwrap().samples.len() >= 48000 * 39 / 100);
        assert!(inner.events.iter().any(|e| e.kind == "device_lost"));
    }

    #[test]
    fn failed_open_reports() {
        let spec = FakeSpec { fail_open: Some("no permission".into()), ..Default::default() };
        let w = Worker::spawn(Source::Fake(spec), Params::new(48000)).unwrap();
        assert_eq!(wait_phase(&w, &[Phase::Failed], 2000), Phase::Failed);
        assert!(!w.shared.stream_open.load(Ordering::SeqCst));
    }
}
