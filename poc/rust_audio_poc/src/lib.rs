pub mod engine;
pub mod timeline;

#[cfg(feature = "python")]
mod python {
    use pyo3::exceptions::PyRuntimeError;
    use pyo3::prelude::*;
    use pyo3::types::{PyBytes, PyDict};

    use crate::engine::{self, Engine, EngineConfig};

    fn err(e: String) -> PyErr {
        PyRuntimeError::new_err(e)
    }

    /// Python handle to the capture engine. All blocking waits release the GIL.
    #[pyclass]
    struct AudioEngine {
        inner: Option<Engine>,
    }

    impl AudioEngine {
        fn engine(&self) -> PyResult<&Engine> {
            self.inner.as_ref().ok_or_else(|| err("engine is shut down".into()))
        }
    }

    #[pymethods]
    impl AudioEngine {
        #[new]
        #[pyo3(signature = (device=None, history_ms=1000, max_session_s=300))]
        fn new(py: Python<'_>, device: Option<String>, history_ms: u32, max_session_s: u32) -> PyResult<Self> {
            let cfg = EngineConfig { device_name: device, history_ms, max_session_s };
            let e = py.detach(|| Engine::start(cfg)).map_err(err)?;
            Ok(Self { inner: Some(e) })
        }

        #[getter]
        fn sample_rate(&self) -> PyResult<u32> {
            Ok(self.engine()?.info.sample_rate)
        }

        #[getter]
        fn device_name(&self) -> PyResult<String> {
            Ok(self.engine()?.info.device_name.clone())
        }

        /// Key pressed. Returns {"pre_samples", "last_callback_age_us"}.
        fn start_session<'py>(&self, py: Python<'py>, pre_ms: u32) -> PyResult<Bound<'py, PyDict>> {
            let e = self.engine()?;
            let info = py.detach(|| e.start_session(pre_ms)).map_err(err)?;
            let d = PyDict::new(py);
            d.set_item("pre_samples", info.pre_samples)?;
            d.set_item("last_callback_age_us", info.last_callback_age_us)?;
            Ok(d)
        }

        /// Key released. Returns a dict with `samples` as little-endian
        /// float32 bytes (use numpy.frombuffer for a zero-copy view).
        #[pyo3(signature = (post_ms=0))]
        fn stop_session<'py>(&self, py: Python<'py>, post_ms: u32) -> PyResult<Bound<'py, PyDict>> {
            let e = self.engine()?;
            let rec = py.detach(|| e.stop_session(post_ms)).map_err(err)?;
            let s = &rec.finished.samples;
            // One copy: Rust Vec -> Python bytes. (A buffer-protocol object
            // could avoid it; not worth it for <= a few MB per dictation.)
            let raw: &[u8] = unsafe { std::slice::from_raw_parts(s.as_ptr() as *const u8, s.len() * 4) };
            let d = PyDict::new(py);
            d.set_item("samples", PyBytes::new(py, raw))?;
            d.set_item("sample_rate", rec.sample_rate)?;
            d.set_item("start_index", rec.finished.start_index)?;
            d.set_item("pre_samples", rec.finished.pre_samples)?;
            d.set_item("overflowed", rec.overflowed)?;
            d.set_item("dropped_total", rec.dropped_total)?;
            d.set_item("device_lost", rec.device_lost)?;
            Ok(d)
        }

        fn cancel_session(&self) -> PyResult<()> {
            self.engine()?.cancel_session().map_err(err)
        }

        /// Peak input level since the previous call, 0..1.
        fn level(&self) -> PyResult<f32> {
            Ok(self.engine()?.take_level())
        }

        fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
            let (callbacks, dropped, lost, last_error) = self.engine()?.stats();
            let d = PyDict::new(py);
            d.set_item("callbacks", callbacks)?;
            d.set_item("dropped", dropped)?;
            d.set_item("device_lost", lost)?;
            d.set_item("last_error", last_error)?;
            Ok(d)
        }

        fn shutdown(&mut self, py: Python<'_>) {
            if let Some(mut e) = self.inner.take() {
                py.detach(move || e.shutdown());
            }
        }
    }

    #[pyfunction]
    fn list_input_devices<'py>(py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let devs = py.detach(engine::list_input_devices).map_err(err)?;
        devs.into_iter()
            .map(|x| {
                let d = PyDict::new(py);
                d.set_item("name", x.name)?;
                d.set_item("is_default", x.is_default)?;
                d.set_item("sample_rate", x.sample_rate)?;
                d.set_item("channels", x.channels)?;
                Ok(d)
            })
            .collect()
    }

    #[pymodule]
    fn omonflow_audio_poc(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<AudioEngine>()?;
        m.add_function(wrap_pyfunction!(list_input_devices, m)?)?;
        Ok(())
    }
}
