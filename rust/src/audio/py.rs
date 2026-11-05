//! Python binding: omonflow._core.AudioCore (ARCHITECTURE §3.2).
//!
//! `frozen` + interior mutability: methods may be called from several Python
//! threads at once (e.g. cancel while another thread waits for readiness).
//! Every blocking wait releases the GIL. Rust never calls into Python.

use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use super::engine::{self, FailKind, FakeSpec, Phase, Shared, Source, Worker};
use super::resample::{resample, wav_s16};
use super::session::Params;

pyo3::create_exception!(_core, AudioError, PyRuntimeError);
pyo3::create_exception!(_core, DeviceNotFound, AudioError);
pyo3::create_exception!(_core, DeviceTimeout, AudioError);
pyo3::create_exception!(_core, SessionActive, AudioError);
pyo3::create_exception!(_core, CaptureFailed, AudioError);

const TARGET_RATE: u32 = 16000;

struct State {
    source: Source,
    current: Option<Worker>,
    retired: Vec<Worker>,
    shut: bool,
}

#[pyclass(frozen)]
pub struct AudioCore {
    state: Mutex<State>,
    warmup_cap_ms: u32,
    max_seconds: u32,
}

fn ms(d: Option<f64>) -> Option<f64> {
    d.map(|v| (v * 10.0).round() / 10.0)
}

impl AudioCore {
    fn current_shared(&self) -> Option<Arc<Shared>> {
        self.state.lock().unwrap().current.as_ref().map(|w| w.shared.clone())
    }

    fn reap(st: &mut State) {
        st.retired.retain_mut(|w| !w.try_join(Duration::ZERO));
    }

    fn retire_current(st: &mut State) {
        if let Some(w) = st.current.take() {
            w.shared.cancel.store(true, Ordering::SeqCst);
            st.retired.push(w);
        }
    }
}

#[pymethods]
impl AudioCore {
    /// `fake`: optional dict for a synthetic source (tests/CI), keys of FakeSpec.
    #[new]
    #[pyo3(signature = (device_id=None, warmup_cap_ms=2500, max_seconds=600, fake=None))]
    fn new(device_id: Option<String>, warmup_cap_ms: u32, max_seconds: u32, fake: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let source = match fake {
            None => Source::Device(device_id),
            Some(d) => {
                let mut f = FakeSpec::default();
                macro_rules! take {
                    ($k:literal, $field:ident) => {
                        if let Some(v) = d.get_item($k)? {
                            f.$field = v.extract()?;
                        }
                    };
                }
                take!("rate", rate);
                take!("open_delay_ms", open_delay_ms);
                take!("hang_ms", hang_ms);
                take!("fail_open", fail_open);
                take!("warmup_zero_ms", warmup_zero_ms);
                take!("noise", noise);
                take!("speech_after_ms", speech_after_ms);
                take!("speech_amp", speech_amp);
                take!("lose_after_ms", lose_after_ms);
                take!("block", block);
                if let Some(v) = d.get_item("hang_times")? {
                    f.hangs_left = Arc::new(std::sync::atomic::AtomicU32::new(v.extract()?));
                }
                Source::Fake(f)
            }
        };
        Ok(Self {
            state: Mutex::new(State { source, current: None, retired: Vec::new(), shut: false }),
            warmup_cap_ms,
            max_seconds,
        })
    }

    #[getter]
    fn name(&self) -> &'static str {
        "rust"
    }

    #[staticmethod]
    fn enumerate_devices(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
        let devs = py.detach(engine::list_devices).map_err(CaptureFailed::new_err)?;
        devs.into_iter()
            .map(|d| {
                let o = PyDict::new(py);
                o.set_item("id", d.id)?;
                o.set_item("name", d.name)?;
                o.set_item("is_default", d.is_default)?;
                o.set_item("sample_rate", d.rate)?;
                o.set_item("channels", d.channels)?;
                o.set_item("format", d.format)?;
                Ok(o)
            })
            .collect()
    }

    /// Only while idle. None = system default device.
    fn select_device(&self, device_id: Option<String>) -> PyResult<()> {
        let mut st = self.state.lock().unwrap();
        if st.current.is_some() {
            return Err(SessionActive::new_err("cannot change device during a capture"));
        }
        if let Source::Device(_) = st.source {
            st.source = Source::Device(device_id);
        }
        Ok(())
    }

    /// Non-blocking: spawns a worker that opens the device.
    fn start_capture(&self) -> PyResult<()> {
        let mut st = self.state.lock().unwrap();
        if st.shut {
            return Err(AudioError::new_err("audio core is shut down"));
        }
        Self::reap(&mut st);
        if st.current.is_some() {
            return Err(SessionActive::new_err("a capture is already active"));
        }
        let mut p = Params::new(48000);
        p.warmup_cap_ms = self.warmup_cap_ms;
        p.max_seconds = self.max_seconds;
        let w = Worker::spawn(st.source.clone(), p).map_err(CaptureFailed::new_err)?;
        st.current = Some(w);
        Ok(())
    }

    /// Wait (GIL released) until CAPTURE_READY. On timeout the worker is
    /// retired and DeviceTimeout raised; the next start uses a fresh worker.
    fn wait_until_ready<'py>(&self, py: Python<'py>, timeout_ms: u64) -> PyResult<Bound<'py, PyDict>> {
        let shared = self.current_shared().ok_or_else(|| AudioError::new_err("no capture in progress"))?;
        let warmup_cap = self.warmup_cap_ms;
        let phase = py.detach(|| {
            let mut deadline = Instant::now() + Duration::from_millis(timeout_ms);
            let mut extended = false;
            let mut inner = shared.inner.lock().unwrap();
            loop {
                if matches!(inner.phase, Phase::Ready | Phase::Active | Phase::Stopped | Phase::Failed)
                    || shared.cancel.load(Ordering::Relaxed)
                {
                    return inner.phase;
                }
                let mut left = deadline.saturating_duration_since(Instant::now());
                if left.is_zero() && inner.phase == Phase::Warming && !extended {
                    // The device is open and delivering samples (still digital silence, e.g. a
                    // Bluetooth headset switching profile): readiness is bounded by the warm-up
                    // cap, so a slow open must not turn into a timeout.
                    extended = true;
                    deadline = Instant::now() + Duration::from_millis(warmup_cap as u64 + 500);
                    left = deadline.saturating_duration_since(Instant::now());
                }
                if left.is_zero() {
                    return inner.phase;
                }
                inner = shared.cv.wait_timeout(inner, left.min(Duration::from_millis(50))).unwrap().0;
            }
        });
        let inner = shared.inner.lock().unwrap();
        match phase {
            Phase::Ready | Phase::Active => {
                let d = PyDict::new(py);
                d.set_item("open_ms", ms(inner.timing.open_ms))?;
                d.set_item("first_audio_ms", ms(inner.timing.first_audio_ms))?;
                d.set_item("ready_ms", ms(inner.timing.ready_ms))?;
                d.set_item("warmup_ms", ms(inner.timing.warmup_ms))?;
                d.set_item("silent_warning", inner.silent_warning)?;
                d.set_item("device_name", inner.device_name.clone())?;
                d.set_item("device_rate", inner.device_rate)?;
                Ok(d)
            }
            Phase::Failed => {
                let (kind, msg) = inner.fail.clone().unwrap_or((FailKind::OpenFailed, "unknown".into()));
                drop(inner);
                let mut st = self.state.lock().unwrap();
                // Only clear the worker we waited for (a new one may have started).
                if st.current.as_ref().is_some_and(|w| Arc::ptr_eq(&w.shared, &shared)) {
                    if let Some(mut w) = st.current.take() {
                        w.try_join(Duration::from_millis(200));
                    }
                }
                Err(match kind {
                    FailKind::DeviceNotFound => DeviceNotFound::new_err(msg),
                    FailKind::OpenFailed => CaptureFailed::new_err(msg),
                })
            }
            Phase::Stopped => Err(AudioError::new_err("capture stopped before it became ready")),
            _ => {
                drop(inner);
                let mut st = self.state.lock().unwrap();
                let opening = phase == Phase::Opening;
                if st.current.as_ref().is_some_and(|w| Arc::ptr_eq(&w.shared, &shared)) {
                    Self::retire_current(&mut st);
                }
                Err(DeviceTimeout::new_err(if opening {
                    format!("microphone did not open within {timeout_ms} ms")
                } else {
                    format!("microphone opened but delivered no audio within {timeout_ms} ms")
                }))
            }
        }
    }

    /// Smoothed input level (0..1) for meters. Never blocks.
    fn get_input_level(&self) -> f32 {
        self.current_shared().map_or(0.0, |s| s.level())
    }

    /// Mark the end and close the stream; fast. Then call finalize_audio().
    fn stop_capture(&self) {
        if let Some(s) = self.current_shared() {
            let mut inner = s.inner.lock().unwrap();
            inner.timing.stop_requested_ms = Some(s.elapsed_ms());
            s.stop.store(true, Ordering::SeqCst);
            drop(inner);
        }
    }

    /// Wait for the stream to close, then trim, resample to 16 kHz, encode WAV.
    #[pyo3(signature = (timeout_ms=3000))]
    fn finalize_audio<'py>(&self, py: Python<'py>, timeout_ms: u64) -> PyResult<Bound<'py, PyDict>> {
        let mut worker = {
            let mut st = self.state.lock().unwrap();
            st.current.take().ok_or_else(|| AudioError::new_err("no capture to finalize"))?
        };
        worker.shared.stop.store(true, Ordering::SeqCst);
        let shared = worker.shared.clone();
        let joined = py.detach(|| worker.try_join(Duration::from_millis(timeout_ms)));
        if !joined {
            let mut st = self.state.lock().unwrap();
            st.retired.push(worker);
            return Err(DeviceTimeout::new_err("microphone did not close in time"));
        }
        let mut inner = shared.inner.lock().unwrap();
        let session = inner.session.take().ok_or_else(|| AudioError::new_err("no audio captured"))?;
        let timing = inner.timing.clone();
        let silent = inner.silent_warning;
        let events: Vec<String> = inner.events.iter().map(|e| e.kind.to_string()).collect();
        drop(inner);
        let rate = session.p.rate;
        let t0 = Instant::now();
        let (wav, trimmed_ms, duration_s, captured) = py.detach(|| {
            let (samples, lead) = session.trimmed();
            let out = resample(samples, rate, TARGET_RATE);
            let wav = wav_s16(&out, TARGET_RATE);
            (wav, session.ms(lead as u64), out.len() as f64 / TARGET_RATE as f64, session.total_in)
        });
        let finalize_ms = t0.elapsed().as_secs_f64() * 1000.0;
        let d = PyDict::new(py);
        d.set_item("wav", PyBytes::new(py, &wav))?;
        d.set_item("sample_rate", TARGET_RATE)?;
        d.set_item("duration_s", duration_s)?;
        d.set_item("trimmed_ms", trimmed_ms)?;
        d.set_item("device_rate", rate)?;
        d.set_item("captured_samples", captured)?;
        d.set_item("overflowed", session.overflowed)?;
        d.set_item("device_lost", shared.device_lost.load(Ordering::Relaxed))?;
        d.set_item("dropped", shared.dropped.load(Ordering::Relaxed))?;
        d.set_item("xruns", shared.xruns.load(Ordering::Relaxed))?;
        d.set_item("silent_warning", silent)?;
        d.set_item("activity", session.active_at.is_some())?;
        d.set_item("events", events)?;
        d.set_item("open_ms", ms(timing.open_ms))?;
        d.set_item("ready_ms", ms(timing.ready_ms))?;
        d.set_item("warmup_ms", ms(timing.warmup_ms))?;
        d.set_item("activity_ms", ms(timing.activity_ms))?;
        d.set_item("stop_to_closed_ms", match (timing.stop_requested_ms, timing.stream_closed_ms) {
            (Some(a), Some(b)) => Some(((b - a) * 10.0).round() / 10.0),
            _ => None,
        })?;
        d.set_item("finalize_ms", (finalize_ms * 10.0).round() / 10.0)?;
        d.set_item("stream_ms", match (timing.open_ms, timing.stream_closed_ms) {
            (Some(a), Some(b)) => Some(((b - a) * 10.0).round() / 10.0),
            _ => None,
        })?;
        Ok(d)
    }

    /// Close the stream and drop the audio. Never blocks for long.
    fn cancel_capture(&self, py: Python<'_>) {
        let worker = self.state.lock().unwrap().current.take();
        if let Some(mut w) = worker {
            w.shared.cancel.store(true, Ordering::SeqCst);
            w.shared.cv.notify_all();
            let joined = py.detach(|| w.try_join(Duration::from_millis(300)));
            if !joined {
                self.state.lock().unwrap().retired.push(w);
            }
        }
    }

    /// idle | opening | warming | ready | active | stopping | stopped | failed
    fn state(&self) -> &'static str {
        match self.current_shared() {
            None => "idle",
            Some(s) => s.inner.lock().unwrap().phase.name(),
        }
    }

    /// Drain events: list of {type, detail, at_ms}.
    fn poll_events<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let Some(s) = self.current_shared() else { return Ok(vec![]) };
        let events: Vec<_> = s.inner.lock().unwrap().events.drain(..).collect();
        let lost = s.device_lost.load(Ordering::Relaxed);
        let mut out = Vec::new();
        for e in events {
            let d = PyDict::new(py);
            d.set_item("type", e.kind)?;
            d.set_item("detail", e.detail)?;
            d.set_item("at_ms", e.at_ms)?;
            out.push(d);
        }
        if lost {
            let d = PyDict::new(py);
            d.set_item("type", "device_lost")?;
            d.set_item("detail", "input device disconnected")?;
            out.push(d);
        }
        Ok(out)
    }

    /// True if any OmonFlow-owned stream (current or retired) is still open.
    fn stream_open(&self) -> bool {
        let st = self.state.lock().unwrap();
        st.current.iter().chain(st.retired.iter()).any(|w| w.shared.stream_open.load(Ordering::SeqCst))
    }

    fn retired_count(&self) -> usize {
        let mut st = self.state.lock().unwrap();
        Self::reap(&mut st);
        st.retired.len()
    }

    /// Idempotent. Cancels everything; waits at most `grace_ms` in total.
    #[pyo3(signature = (grace_ms=1000))]
    fn shutdown(&self, py: Python<'_>, grace_ms: u64) {
        let workers: Vec<Worker> = {
            let mut st = self.state.lock().unwrap();
            st.shut = true;
            let mut v: Vec<Worker> = std::mem::take(&mut st.retired);
            v.extend(st.current.take());
            v
        };
        for w in &workers {
            w.shared.cancel.store(true, Ordering::SeqCst);
            w.shared.cv.notify_all();
        }
        let deadline = Instant::now() + Duration::from_millis(grace_ms);
        py.detach(|| {
            for mut w in workers {
                let left = deadline.saturating_duration_since(Instant::now());
                if !w.try_join(left) {
                    // Stuck in the driver: leave it; the process may still exit.
                    std::mem::forget(w);
                }
            }
        });
    }
}

/// Run the session rules over `samples` in blocks (parity tests with the
/// Python fallback). Returns ready/activity indices and the noise floor.
#[pyfunction]
#[pyo3(signature = (samples, rate, block=480))]
fn analyze_session<'py>(py: Python<'py>, samples: Vec<f32>, rate: u32, block: usize) -> PyResult<Bound<'py, PyDict>> {
    let mut s = super::session::Session::new(Params::new(rate));
    for chunk in samples.chunks(block.max(1)) {
        s.push(chunk);
    }
    let d = PyDict::new(py);
    d.set_item("ready_at", s.ready_at)?;
    d.set_item("first_nonzero", s.first_nonzero)?;
    d.set_item("active_at", s.active_at)?;
    d.set_item("silent_warning", s.silent_warning)?;
    d.set_item("floor", s.floor)?;
    Ok(d)
}

/// Resampler exposed for parity/quality tests.
#[pyfunction]
fn resample_audio(samples: Vec<f32>, from_rate: u32, to_rate: u32) -> Vec<f32> {
    resample(&samples, from_rate, to_rate)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<AudioCore>()?;
    m.add_function(wrap_pyfunction!(analyze_session, m)?)?;
    m.add_function(wrap_pyfunction!(resample_audio, m)?)?;
    let py = m.py();
    m.add("AudioError", py.get_type::<AudioError>())?;
    m.add("DeviceNotFound", py.get_type::<DeviceNotFound>())?;
    m.add("DeviceTimeout", py.get_type::<DeviceTimeout>())?;
    m.add("SessionActive", py.get_type::<SessionActive>())?;
    m.add("CaptureFailed", py.get_type::<CaptureFailed>())?;
    Ok(())
}
