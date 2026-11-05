//! omonflow._core: OmonFlow's native core.
//!
//! M02: registered global shortcuts (show/hide widget).
//! M03: on-demand audio capture (audio::py::AudioCore).

pub mod audio;

use std::collections::HashMap;
use std::str::FromStr;

use global_hotkey::hotkey::HotKey;
use global_hotkey::{GlobalHotKeyEvent, GlobalHotKeyManager, HotKeyState};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

pyo3::create_exception!(_core, ShortcutConflict, PyRuntimeError);
pyo3::create_exception!(_core, ShortcutUnavailable, PyRuntimeError);

/// Registered (non-monitoring) global shortcuts.
///
/// Must be created and used on the GUI main thread: on macOS the Carbon
/// handler is driven by the main run loop (Qt's), on Windows by the main
/// thread's message loop. Hence `unsendable`.
#[pyclass(unsendable)]
struct HotkeyManager {
    manager: GlobalHotKeyManager,
    registered: HashMap<u32, HotKey>,
}

#[pymethods]
impl HotkeyManager {
    #[new]
    fn new() -> PyResult<Self> {
        let manager = GlobalHotKeyManager::new()
            .map_err(|e| ShortcutUnavailable::new_err(e.to_string()))?;
        Ok(Self { manager, registered: HashMap::new() })
    }

    /// Validate a shortcut string like "cmd+shift+space" without registering.
    #[staticmethod]
    fn parse(spec: &str) -> PyResult<String> {
        HotKey::from_str(spec)
            .map(|h| h.into_string())
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    /// Register; returns an id used in `poll()` events.
    /// Raises ValueError (bad spec), ShortcutConflict (already taken),
    /// ShortcutUnavailable (OS refused for another reason).
    fn register(&mut self, spec: &str) -> PyResult<u32> {
        let hk = HotKey::from_str(spec).map_err(|e| PyValueError::new_err(e.to_string()))?;
        self.manager.register(hk).map_err(|e| match e {
            global_hotkey::Error::AlreadyRegistered(_) => ShortcutConflict::new_err(e.to_string()),
            other => ShortcutUnavailable::new_err(other.to_string()),
        })?;
        self.registered.insert(hk.id(), hk);
        Ok(hk.id())
    }

    fn unregister(&mut self, id: u32) -> PyResult<()> {
        if let Some(hk) = self.registered.remove(&id) {
            self.manager
                .unregister(hk)
                .map_err(|e| ShortcutUnavailable::new_err(e.to_string()))?;
        }
        Ok(())
    }

    fn unregister_all(&mut self) {
        for (_, hk) in self.registered.drain() {
            let _ = self.manager.unregister(hk);
        }
    }

    /// Drain pending events: list of (id, "pressed" | "released").
    #[staticmethod]
    fn poll() -> Vec<(u32, &'static str)> {
        let mut out = Vec::new();
        while let Ok(ev) = GlobalHotKeyEvent::receiver().try_recv() {
            let state = match ev.state() {
                HotKeyState::Pressed => "pressed",
                HotKeyState::Released => "released",
            };
            out.push((ev.id(), state));
        }
        out
    }
}

impl Drop for HotkeyManager {
    fn drop(&mut self) {
        self.unregister_all();
    }
}

#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_class::<HotkeyManager>()?;
    m.add("ShortcutConflict", m.py().get_type::<ShortcutConflict>())?;
    m.add("ShortcutUnavailable", m.py().get_type::<ShortcutUnavailable>())?;
    audio::py::register(m)?;
    Ok(())
}
