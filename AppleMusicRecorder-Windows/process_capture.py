"""
process_capture.py
Captures audio from Apple Music's process only via
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK (Windows 10 build 20348+).

Uses comtypes.COMObject for the completion handler so Windows gets a
correct vtable + IAgileObject support — the main reason the previous
raw-vtable implementation returned E_INVALIDARG.

Falls back gracefully to system loopback on any failure.
"""
import ctypes
import ctypes.wintypes
import struct
import threading
import time
import uuid as _uuid_mod
from pathlib import Path
from typing import Optional

import comtypes
from comtypes import GUID, HRESULT, IUnknown, COMMETHOD, COMObject
import numpy as np
import psutil
import soundfile as sf


# ── Windows constants ──────────────────────────────────────────────────────────

AUDCLNT_SHAREMODE_SHARED                = 0
AUDCLNT_STREAMFLAGS_LOOPBACK            = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM     = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
COINIT_MULTITHREADED                    = 0x0

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK          = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE     = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
VT_BLOB = 0x41

_APPLE_MUSIC_NAMES = {"AppleMusic.exe", "iTunes.exe", "Music.exe"}


# ── PROPVARIANT with correct 64-bit layout ─────────────────────────────────────
# On x64 Windows, PROPVARIANT is 24 bytes:
#   offset  0: VARTYPE vt          (2)
#   offset  2: wReserved1/2/3      (6)
#   offset  8: BLOB.cbSize         (4)   ← union starts here
#   offset 12: (4 bytes auto-pad for pointer alignment)
#   offset 16: BLOB.pBlobData      (8)

class _BLOB(ctypes.Structure):
    _fields_ = [
        ("cbSize",    ctypes.c_ulong),
        ("pBlobData", ctypes.c_void_p),
    ]

class _PV_UNION(ctypes.Union):
    _fields_ = [
        ("blob", _BLOB),
        ("_pad", ctypes.c_byte * 16),
    ]

class PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("vt",        ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("u",         _PV_UNION),
    ]


# ── GUIDs ──────────────────────────────────────────────────────────────────────

def _gb(s: str) -> bytes:
    return _uuid_mod.UUID(s).bytes_le

IID_IAudioClient        = _gb("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = _gb("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")


# ── comtypes COM interfaces for activation ─────────────────────────────────────

class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        # We declare the parameter as c_void_p so we can call GetActivateResult
        # via raw vtable ourselves — avoids circular interface definition issues.
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], ctypes.c_void_p, "activateOperation")),
    ]


class _CompletionHandler(COMObject):
    """
    Proper COM object (comtypes manages vtable, IAgileObject, refcounting).
    Windows calls ActivateCompleted on a thread-pool MTA thread.
    """
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler]

    def __init__(self):
        super().__init__()
        self.done   = threading.Event()
        self.hr     = -1
        self.ac_ptr = None   # raw IAudioClient pointer value

    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(
            self, activate_operation):
        try:
            # activate_operation is a raw c_void_p value (IActivateAudioInterfaceAsyncOperation*)
            # Call GetActivateResult via vtable index 3
            op_iface = ctypes.cast(activate_operation, ctypes.POINTER(ctypes.c_void_p))
            vtbl     = ctypes.cast(op_iface[0], ctypes.POINTER(ctypes.c_void_p))
            GetActivateResult = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p,
                ctypes.POINTER(ctypes.HRESULT),
                ctypes.POINTER(ctypes.c_void_p),
            )(vtbl[3])
            inner_hr = ctypes.HRESULT()
            ac       = ctypes.c_void_p()
            GetActivateResult(activate_operation,
                              ctypes.byref(inner_hr), ctypes.byref(ac))
            self.hr     = inner_hr.value
            self.ac_ptr = ac.value
        except Exception:
            self.hr = -1
        finally:
            self.done.set()
        return 0   # S_OK


# ── IAudioClient vtable helpers ────────────────────────────────────────────────
# [0]QI [1]AddRef [2]Release [3]Initialize [4]GetBufferSize [5]GetStreamLatency
# [6]GetCurrentPadding [7]IsFormatSupported [8]GetMixFormat [9]GetDevicePeriod
# [10]Start [11]Stop [12]Reset [13]SetEventHandle [14]GetService

def _vtbl(ptr):
    iface = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
    return ctypes.cast(iface[0], ctypes.POINTER(ctypes.c_void_p))


def _get_mix_format(ac_ptr) -> Optional[bytes]:
    try:
        vt = _vtbl(ac_ptr)
        GetMixFormat = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(vt[8])
        fmt = ctypes.c_void_p()
        if GetMixFormat(ac_ptr, ctypes.byref(fmt)) != 0 or not fmt.value:
            return None
        raw = bytes((ctypes.c_byte * 40).from_address(fmt.value))
        ctypes.windll.ole32.CoTaskMemFree(fmt)
        return raw
    except Exception:
        return None


def _parse_wfx(raw: bytes):
    if len(raw) < 18:
        return 48000, 2, 32
    _, channels, rate, _, _, bits, _ = struct.unpack_from("<HHIIHH H", raw)
    return rate, channels, bits


# ── Helper ─────────────────────────────────────────────────────────────────────

def find_apple_music_pid() -> Optional[int]:
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"] in _APPLE_MUSIC_NAMES:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


# ── ProcessAudioCapture ────────────────────────────────────────────────────────

class ProcessAudioCapture:
    """
    Captures Apple Music process audio only.
    API-compatible with AudioCapture: start() → temp_path,
    stop() → temp_path, split() → (finished, new).
    """

    def __init__(self, save_directory: str, pid: int):
        self.save_directory = Path(save_directory)
        self.save_directory.mkdir(parents=True, exist_ok=True)
        self._pid         = pid
        self._running     = False
        self._sf: Optional[sf.SoundFile] = None
        self._sf_lock     = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.current_temp_path: Optional[str] = None

        self._sample_rate = 48000
        self._channels    = 2
        self._bits        = 32
        self._init_error: Optional[Exception] = None
        self._ready       = threading.Event()
        self._handler_ref = None   # keep handler alive across async wait

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> str:
        temp_path = self._new_temp_path()
        self.current_temp_path = temp_path
        self._running = True
        self._thread  = threading.Thread(
            target=self._mta_main, args=(temp_path,),
            daemon=True, name="ProcessCaptureMTA",
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self._running = False
            raise RuntimeError("Process audio activation timed out")
        if self._init_error:
            self._running = False
            raise self._init_error
        return temp_path

    def stop(self) -> Optional[str]:
        self._running = False
        if self._thread:
            self._thread.join(timeout=4)
            self._thread = None
        return self._close_sf()

    def split(self) -> tuple[str, str]:
        new_path = self._new_temp_path()
        new_sf   = sf.SoundFile(
            new_path, mode="w",
            samplerate=self._sample_rate,
            channels=self._channels,
            format="FLAC", subtype="PCM_24",
        )
        with self._sf_lock:
            old_sf, self._sf = self._sf, new_sf
            old_path, self.current_temp_path = self.current_temp_path, new_path
        if old_sf:
            old_sf.close()
        return old_path, new_path

    # ── MTA thread ─────────────────────────────────────────────────────────────

    def _mta_main(self, temp_path: str):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        try:
            ac_ptr, cc_ptr = self._activate_and_start()
        except Exception as e:
            self._init_error = e
            self._ready.set()
            ole32.CoUninitialize()
            return

        try:
            with self._sf_lock:
                self._sf = sf.SoundFile(
                    temp_path, mode="w",
                    samplerate=self._sample_rate,
                    channels=self._channels,
                    format="FLAC", subtype="PCM_24",
                )
        except Exception as e:
            self._init_error = e
            self._ready.set()
            ole32.CoUninitialize()
            return

        self._ready.set()
        self._capture_loop(cc_ptr)

        # Stop IAudioClient (vtable[11])
        try:
            Stop = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(_vtbl(ac_ptr)[11])
            Stop(ac_ptr)
        except Exception:
            pass

        ole32.CoUninitialize()

    # ── Activation ─────────────────────────────────────────────────────────────

    def _activate_and_start(self):
        mmdevapi = ctypes.windll.mmdevapi

        # Build AUDIOCLIENT_ACTIVATION_PARAMS
        params_bytes = struct.pack(
            "<III",
            AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
            self._pid,
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
        )
        params_buf = (ctypes.c_byte * len(params_bytes))(*params_bytes)

        # PROPVARIANT (ctypes.Structure handles alignment automatically)
        pv = PROPVARIANT()
        pv.vt           = VT_BLOB
        pv.blob.cbSize  = len(params_bytes)
        pv.blob.pBlobData = ctypes.cast(params_buf, ctypes.c_void_p)

        # Completion handler — comtypes COMObject (correct vtable + IAgileObject)
        handler = _CompletionHandler()
        self._handler_ref = handler   # keep alive until done

        # Get the raw COM pointer to pass to the windll function
        handler_iface = comtypes.cast(handler, IActivateAudioInterfaceCompletionHandler)
        handler_raw   = ctypes.cast(handler_iface, ctypes.c_void_p)

        iid_ac   = (ctypes.c_byte * 16)(*IID_IAudioClient)
        async_op = ctypes.c_void_p()

        hr = mmdevapi.ActivateAudioInterfaceAsync(
            ctypes.c_wchar_p(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK),
            ctypes.byref(iid_ac),
            ctypes.byref(pv),
            handler_raw,
            ctypes.byref(async_op),
        )
        if hr != 0:
            raise RuntimeError(
                f"ActivateAudioInterfaceAsync: 0x{hr & 0xFFFFFFFF:08X}"
            )

        if not handler.done.wait(timeout=8.0):
            raise RuntimeError("Activation timed out")
        if handler.hr != 0:
            raise RuntimeError(
                f"Activation error: 0x{handler.hr & 0xFFFFFFFF:08X}"
            )
        if not handler.ac_ptr:
            raise RuntimeError("Null audio client after activation")

        ac_ptr = handler.ac_ptr

        # GetMixFormat → detect real sample rate / channels
        fmt_raw = _get_mix_format(ac_ptr)
        if fmt_raw:
            self._sample_rate, self._channels, self._bits = _parse_wfx(fmt_raw)

        # Initialize (vtable[3])
        wfx_buf = (ctypes.c_byte * len(fmt_raw))(*fmt_raw) if fmt_raw else (ctypes.c_byte * 0)()
        Initialize = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_uint,
            ctypes.c_longlong, ctypes.c_longlong,
            ctypes.c_void_p, ctypes.c_void_p,
        )(_vtbl(ac_ptr)[3])
        hr = Initialize(
            ac_ptr, AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK |
            AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            0, 0,
            ctypes.cast(wfx_buf, ctypes.c_void_p), None,
        )
        if hr != 0:
            raise RuntimeError(f"IAudioClient::Initialize: 0x{hr & 0xFFFFFFFF:08X}")

        # GetService(IAudioCaptureClient) — vtable[14]
        iid_cc  = (ctypes.c_byte * 16)(*IID_IAudioCaptureClient)
        cc_ptr  = ctypes.c_void_p()
        GetService = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_byte * 16),
            ctypes.POINTER(ctypes.c_void_p),
        )(_vtbl(ac_ptr)[14])
        hr = GetService(ac_ptr, ctypes.byref(iid_cc), ctypes.byref(cc_ptr))
        if hr != 0:
            raise RuntimeError(f"GetService(CaptureClient): 0x{hr & 0xFFFFFFFF:08X}")

        # Start — vtable[10]
        Start = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(_vtbl(ac_ptr)[10])
        Start(ac_ptr)

        return ac_ptr, cc_ptr.value

    # ── Capture loop ───────────────────────────────────────────────────────────

    def _capture_loop(self, cc_ptr_val: int):
        cc_iface = ctypes.cast(cc_ptr_val, ctypes.POINTER(ctypes.c_void_p))
        vt       = ctypes.cast(cc_iface[0], ctypes.POINTER(ctypes.c_void_p))

        GetBuffer = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        )(vt[3])
        ReleaseBuffer = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint)(vt[4])
        GetNextPacketSize = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))(vt[5])

        bytes_per_frame = self._channels * (self._bits // 8)

        while self._running:
            pkt = ctypes.c_uint(0)
            if GetNextPacketSize(cc_ptr_val, ctypes.byref(pkt)) != 0 or pkt.value == 0:
                time.sleep(0.005)
                continue

            data  = ctypes.c_void_p()
            n     = ctypes.c_uint(0)
            flags = ctypes.c_uint(0)
            dp    = ctypes.c_ulonglong(0)
            qp    = ctypes.c_ulonglong(0)
            hr = GetBuffer(cc_ptr_val, ctypes.byref(data), ctypes.byref(n),
                           ctypes.byref(flags), ctypes.byref(dp), ctypes.byref(qp))
            if hr != 0 or not data.value:
                time.sleep(0.005)
                continue

            n_frames = n.value
            raw = bytes(
                (ctypes.c_byte * (n_frames * bytes_per_frame))
                .from_address(data.value)
            )

            if self._bits == 32:
                samples_f = np.frombuffer(raw, dtype=np.float32)
            elif self._bits == 16:
                samples_f = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                samples_f = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0

            samples_f = samples_f.reshape(-1, self._channels)
            samples_i = (samples_f * 8388607.0).clip(-8388608, 8388607).astype(np.int32)

            with self._sf_lock:
                if self._sf:
                    try:
                        self._sf.write(samples_i)
                    except Exception:
                        pass

            ReleaseBuffer(cc_ptr_val, n_frames)

    # ── File helpers ───────────────────────────────────────────────────────────

    def _new_temp_path(self) -> str:
        import uuid
        return str(self.save_directory / f".amr_tmp_{uuid.uuid4().hex}.flac")

    def _close_sf(self) -> Optional[str]:
        with self._sf_lock:
            sf_ref, self._sf = self._sf, None
            path, self.current_temp_path = self.current_temp_path, None
        if sf_ref:
            sf_ref.close()
        return path
