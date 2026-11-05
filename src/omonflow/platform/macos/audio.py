"""macOS microphone permission and OS-level "is any input running" check."""

from __future__ import annotations

import ctypes
import ctypes.util
import struct

PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
_STATUS = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}


def mic_permission() -> str:
    """not_determined | restricted | denied | authorized | unknown.

    For an unbundled dev run macOS attributes this to the terminal app.
    """
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        return _STATUS.get(int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)), "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


class _Addr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_ca = None


def _coreaudio():
    global _ca
    if _ca is None:
        _ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        _ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        _ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    return _ca


def _size(obj, addr) -> int:
    n = ctypes.c_uint32(0)
    if _coreaudio().AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(n)):
        return 0
    return n.value


def input_devices_running() -> dict[int, bool]:
    """CoreAudio device id → "running somewhere" for every device with input.

    "Somewhere" means any process, so other apps using the mic count too.
    """
    ca = _coreaudio()
    glob, inpt = _fourcc("glob"), _fourcc("inpt")
    sysobj = 1  # kAudioObjectSystemObject
    addr = _Addr(_fourcc("dev#"), glob, 0)
    n = _size(sysobj, addr) // 4
    ids = (ctypes.c_uint32 * n)()
    size = ctypes.c_uint32(n * 4)
    ca.AudioObjectGetPropertyData(sysobj, ctypes.byref(addr), 0, None, ctypes.byref(size), ids)
    out = {}
    for dev in ids:
        if _size(dev, _Addr(_fourcc("stm#"), inpt, 0)) == 0:
            continue  # no input streams
        running = ctypes.c_uint32(0)
        sz = ctypes.c_uint32(4)
        ca.AudioObjectGetPropertyData(dev, ctypes.byref(_Addr(_fourcc("gone"), glob, 0)), 0, None,
                                      ctypes.byref(sz), ctypes.byref(running))
        out[int(dev)] = bool(running.value)
    return out


def any_input_running() -> bool:
    return any(input_devices_running().values())
