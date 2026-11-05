//! Continuous capture engine (proof of concept).
//!
//! Threads:
//! - cpal real-time callback: downmix to mono, push into a lock-free SPSC
//!   ring (`rtrb`), update atomics. No locks, no allocation, no Python.
//! - "omonflow-audio" engine thread: owns the cpal stream (it is !Send on
//!   some hosts), drains the ring into `Timeline`, and serves commands.
//! - Callers (Python) talk to the engine thread over channels and block only
//!   on reply channels, with the GIL released.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

use crate::timeline::{Finished, Timeline};

pub struct EngineConfig {
    pub device_name: Option<String>,
    /// Rolling history kept at all times (must be >= largest pre-buffer).
    pub history_ms: u32,
    pub max_session_s: u32,
}

#[derive(Debug, Clone)]
pub struct StreamInfo {
    pub device_name: String,
    pub sample_rate: u32,
    pub channels: u16,
}

#[derive(Debug, Clone)]
pub struct StartInfo {
    pub pre_samples: usize,
    /// Time since the newest captured block arrived, at key-press time.
    /// Upper bound on how stale the end of the pre-buffer is.
    pub last_callback_age_us: u64,
}

pub struct Recording {
    pub finished: Finished,
    pub sample_rate: u32,
    pub overflowed: bool,
    /// Samples dropped because the ring was full (should always be 0).
    pub dropped_total: u64,
    pub device_lost: bool,
}

#[derive(Debug, Clone)]
pub struct DeviceEntry {
    pub name: String,
    pub is_default: bool,
    pub sample_rate: u32,
    pub channels: u16,
}

enum Command {
    Start { pre_ms: u32, reply: Sender<Result<StartInfo, String>> },
    Stop { post_ms: u32, reply: Sender<Result<Recording, String>> },
    Cancel,
    Shutdown,
}

#[derive(Default)]
struct Shared {
    level_bits: AtomicU32,   // f32 peak since last read
    dropped: AtomicU64,      // samples lost to a full ring
    callbacks: AtomicU64,
    last_cb_us: AtomicU64,   // micros since `epoch` of newest callback
    device_lost: AtomicBool,
    last_error: Mutex<Option<String>>,
}

pub struct Engine {
    cmd: Sender<Command>,
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
    pub info: StreamInfo,
}

pub fn list_input_devices() -> Result<Vec<DeviceEntry>, String> {
    let host = cpal::default_host();
    let default_name = host
        .default_input_device()
        .and_then(|d| d.description().ok().map(|x| x.name().to_string()));
    let mut out = Vec::new();
    for dev in host.input_devices().map_err(|e| e.to_string())? {
        let Ok(desc) = dev.description() else { continue };
        let Ok(cfg) = dev.default_input_config() else { continue };
        let name = desc.name().to_string();
        out.push(DeviceEntry {
            is_default: Some(&name) == default_name.as_ref(),
            name,
            sample_rate: cfg.sample_rate(),
            channels: cfg.channels(),
        });
    }
    Ok(out)
}

impl Engine {
    pub fn start(cfg: EngineConfig) -> Result<Engine, String> {
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let (ready_tx, ready_rx) = mpsc::channel();
        let shared = Arc::new(Shared::default());
        let shared2 = shared.clone();
        let thread = std::thread::Builder::new()
            .name("omonflow-audio".into())
            .spawn(move || engine_thread(cfg, cmd_rx, ready_tx, shared2))
            .map_err(|e| e.to_string())?;
        let info = ready_rx
            .recv_timeout(Duration::from_secs(10))
            .map_err(|_| "audio engine did not start within 10 s".to_string())??;
        Ok(Engine { cmd: cmd_tx, shared, thread: Some(thread), info })
    }

    pub fn start_session(&self, pre_ms: u32) -> Result<StartInfo, String> {
        let (tx, rx) = mpsc::channel();
        self.send(Command::Start { pre_ms, reply: tx })?;
        rx.recv_timeout(Duration::from_secs(2)).map_err(|e| e.to_string())?
    }

    /// Blocks until post-release padding has been captured.
    pub fn stop_session(&self, post_ms: u32) -> Result<Recording, String> {
        let (tx, rx) = mpsc::channel();
        self.send(Command::Stop { post_ms, reply: tx })?;
        rx.recv_timeout(Duration::from_millis(post_ms as u64 + 2000))
            .map_err(|e| e.to_string())?
    }

    pub fn cancel_session(&self) -> Result<(), String> {
        self.send(Command::Cancel)
    }

    /// Peak level since the previous call (0..1). For UI meters.
    pub fn take_level(&self) -> f32 {
        f32::from_bits(self.shared.level_bits.swap(0, Ordering::Relaxed))
    }

    pub fn stats(&self) -> (u64, u64, bool, Option<String>) {
        (
            self.shared.callbacks.load(Ordering::Relaxed),
            self.shared.dropped.load(Ordering::Relaxed),
            self.shared.device_lost.load(Ordering::Relaxed),
            self.shared.last_error.lock().unwrap().clone(),
        )
    }

    pub fn shutdown(&mut self) {
        let _ = self.cmd.send(Command::Shutdown);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }

    fn send(&self, c: Command) -> Result<(), String> {
        self.cmd.send(c).map_err(|_| "audio engine is not running".to_string())
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn engine_thread(
    cfg: EngineConfig,
    cmd_rx: Receiver<Command>,
    ready: Sender<Result<StreamInfo, String>>,
    shared: Arc<Shared>,
) {
    let epoch = Instant::now();
    let host = cpal::default_host();
    let device = match &cfg.device_name {
        None => host.default_input_device(),
        Some(want) => host.input_devices().ok().and_then(|mut it| {
            it.find(|d| d.description().map(|x| x.name() == want).unwrap_or(false))
        }),
    };
    let Some(device) = device else {
        let _ = ready.send(Err("no matching input device".into()));
        return;
    };
    let supported = match device.default_input_config() {
        Ok(c) => c,
        Err(e) => {
            let _ = ready.send(Err(format!("input config: {e}")));
            return;
        }
    };
    let rate = supported.sample_rate();
    let channels = supported.channels() as usize;
    let device_name = device
        .description()
        .map(|d| d.name().to_string())
        .unwrap_or_default();

    // Ring sized for 2 s; the engine thread drains every ~2 ms.
    let (mut producer, mut consumer) = rtrb::RingBuffer::<f32>::new(rate as usize * 2);
    let cb_shared = shared.clone();
    let err_shared = shared.clone();

    let stream = device.build_input_stream::<f32, _, _>(
        supported.config(),
        move |data: &[f32], _info| {
            let mut peak = 0f32;
            for frame in data.chunks_exact(channels) {
                let mono = frame.iter().sum::<f32>() / channels as f32;
                peak = peak.max(mono.abs());
                if producer.push(mono).is_err() {
                    cb_shared.dropped.fetch_add(1, Ordering::Relaxed);
                }
            }
            let prev = f32::from_bits(cb_shared.level_bits.load(Ordering::Relaxed));
            if peak > prev {
                cb_shared.level_bits.store(peak.to_bits(), Ordering::Relaxed);
            }
            cb_shared.callbacks.fetch_add(1, Ordering::Relaxed);
            cb_shared
                .last_cb_us
                .store(epoch.elapsed().as_micros() as u64, Ordering::Relaxed);
        },
        move |err: cpal::Error| {
            if matches!(err.kind(), cpal::ErrorKind::DeviceNotAvailable) {
                err_shared.device_lost.store(true, Ordering::Relaxed);
            }
            *err_shared.last_error.lock().unwrap() = Some(err.to_string());
        },
        None,
    );
    let stream = match stream {
        Ok(s) => s,
        Err(e) => {
            let _ = ready.send(Err(format!("build stream (f32): {e}")));
            return;
        }
    };
    if let Err(e) = stream.play() {
        let _ = ready.send(Err(format!("start stream: {e}")));
        return;
    }
    let _ = ready.send(Ok(StreamInfo { device_name, sample_rate: rate, channels: channels as u16 }));

    let ms = |m: u32| (rate as u64 * m as u64 / 1000) as usize;
    let mut timeline = Timeline::new(ms(cfg.history_ms), rate as usize * cfg.max_session_s as usize);
    let mut scratch: Vec<f32> = Vec::with_capacity(rate as usize);
    let mut pending_stop: Option<Sender<Result<Recording, String>>> = None;

    let finish = |f: Finished, overflowed: bool, shared: &Shared| Recording {
        finished: f,
        sample_rate: rate,
        overflowed,
        dropped_total: shared.dropped.load(Ordering::Relaxed),
        device_lost: shared.device_lost.load(Ordering::Relaxed),
    };

    loop {
        // 1. Drain everything the callback produced so far.
        scratch.clear();
        while let Ok(s) = consumer.pop() {
            scratch.push(s);
        }
        if !scratch.is_empty() {
            if let Some(f) = timeline.push(&scratch) {
                if let Some(reply) = pending_stop.take() {
                    let _ = reply.send(Ok(finish(f, timeline.overflowed(), &shared)));
                }
            }
        }
        // Device vanished while waiting for padding: hand back what we have.
        if pending_stop.is_some() && shared.device_lost.load(Ordering::Relaxed) {
            if let Some(f) = timeline.finish_now() {
                let _ = pending_stop.take().unwrap().send(Ok(finish(f, timeline.overflowed(), &shared)));
            }
        }

        // 2. Serve commands, waiting at most ~2 ms.
        match cmd_rx.recv_timeout(Duration::from_millis(2)) {
            Ok(Command::Start { pre_ms, reply }) => {
                // Drain first so the pre-buffer includes the newest audio.
                scratch.clear();
                while let Ok(s) = consumer.pop() {
                    scratch.push(s);
                }
                timeline.push(&scratch);
                let age = (epoch.elapsed().as_micros() as u64)
                    .saturating_sub(shared.last_cb_us.load(Ordering::Relaxed));
                let pre = ms(pre_ms).min(timeline.history_len());
                let res = if timeline.start(ms(pre_ms)) {
                    Ok(StartInfo { pre_samples: pre, last_callback_age_us: age })
                } else {
                    Err("a recording session is already active".into())
                };
                let _ = reply.send(res);
            }
            Ok(Command::Stop { post_ms, reply }) => match timeline.stop(ms(post_ms)) {
                Some(f) => {
                    let _ = reply.send(Ok(finish(f, timeline.overflowed(), &shared)));
                }
                None if matches!(timeline.state(), crate::timeline::SessionState::Finishing { .. }) => {
                    pending_stop = Some(reply);
                }
                None => {
                    let _ = reply.send(Err("no active recording session".into()));
                }
            },
            Ok(Command::Cancel) => {
                timeline.cancel();
                pending_stop = None;
            }
            Ok(Command::Shutdown) | Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => {}
        }
    }
    drop(stream);
}
