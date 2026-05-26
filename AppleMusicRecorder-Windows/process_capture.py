"""
process_capture.py
Captures audio from Apple Music's process only via
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK (Windows 10 build 20348+).

All COM work (activation + capture loop) runs on a single MTA thread to
satisfy ActivateAudioInterfaceAsync's apartment requirements.
Falls back gracefully to system loopback if anything fails.
"""
import ctypes
import ctypes.wintypes
import struct
import threading
import time
import uuid as _uuid_mod
from pathlib import Path
from typing import Optional

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


# ── GUIDs ──────────────────────────────────────────────────────────────────────

def _gb(s: str) -> bytes:
    return _uuid_mod.UUID(s).bytes_le

IID_IAudioClient        = _gb("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = _gb("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_apple_music_pid() -> Optional[int]:
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"] in _APPLE_MUSIC_NAMES:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


# ── Completion handler COM object ──────────────────────────────────────────────

class _CompletionHandler:
    """
    Implements IActivateAudioInterfaceCompletionHandler as a raw COM vtable
    object. Windows calls ActivateCompleted on a thread-pool thread.
    """
    def __init__(self):
        self.done     = threading.Event()
        self.hr       = -1
        self.ac_ptr   = None  # raw c_void_p value of IAudioClient

        WINFUNC = ctypes.WINFUNCTYPE

        @WINFUNC(ctypes.HRESULT, ctypes.c_void_p,
                 ctypes.POINTER(ctypes.c_byte * 16),
                 ctypes.POINTER(ctypes.c_void_p))
        def _qi(this, riid, ppv):
            ppv[0] = ctypes.cast(self._obj_ptr, ctypes.c_void_p).value
            return 0  # S_OK

        @WINFUNC(ctypes.c_ulong, ctypes.c_void_p)
        def _addref(this): return 1

        @WINFUNC(ctypes.c_ulong, ctypes.c_void_p)
        def _release(this): return 1

        @WINFUNC(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)
        def _activate_completed(this, operation):
            try:
                # IActivateAudioInterfaceAsyncOperation vtable[3] = GetActivateResult
                op_iface = ctypes.cast(operation, ctypes.POINTER(ctypes.c_void_p))
                vtbl     = ctypes.cast(op_iface[0], ctypes.POINTER(ctypes.c_void_p))
                GetActivateResult = ctypes.WINFUNCTYPE(
                    ctypes.HRESULT, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.HRESULT),
                    ctypes.POINTER(ctypes.c_void_p),
                )(vtbl[3])
                inner_hr = ctypes.HRESULT()
                ac       = ctypes.c_void_p()
                GetActivateResult(operation,
                                  ctypes.byref(inner_hr),
                                  ctypes.byref(ac))
                self.hr     = inner_hr.value
                self.ac_ptr = ac.value
            except Exception:
                self.hr = -1
            finally:
                self.done.set()
            return 0

        # Keep all function objects alive
        self._qi = _qi; self._addref = _addref
        self._release = _release; self._activate_completed = _activate_completed

        _Vtbl = ctypes.c_void_p * 4
        self._vtable = _Vtbl(
            ctypes.cast(_qi,                  ctypes.c_void_p),
            ctypes.cast(_addref,              ctypes.c_void_p),
            ctypes.cast(_release,             ctypes.c_void_p),
            ctypes.cast(_activate_completed,  ctypes.c_void_p),
        )
        _Obj = ctypes.c_void_p * 1
        self._obj     = _Obj(ctypes.addressof(self._vtable))
        self._obj_ptr = ctypes.addressof(self._obj)

    def handler_ptr(self) -> ctypes.c_void_p:
        return ctypes.c_void_p(self._obj_ptr)


# ── IAudioClient vtable helpers ────────────────────────────────────────────────
# Vtable layout (0-based):
# [0]QI [1]AddRef [2]Release [3]Initialize [4]GetBufferSize [5]GetStreamLatency
# [6]GetCurrentPadding [7]IsFormatSupported [8]GetMixFormat [9]GetDevicePeriod
# [10]Start [11]Stop [12]Reset [13]SetEventHandle [14]GetService

def _vtbl(ptr):
    iface = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
    return ctypes.cast(iface[0], ctypes.POINTER(ctypes.c_void_p))


def _ac_get_mix_format(ac_ptr) -> Optional[bytes]:
    """Call IAudioClient::GetMixFormat, return raw WAVEFORMATEX bytes."""
    try:
        vt = _vtbl(ac_ptr)
        GetMixFormat = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(vt[8])
        fmt_ptr = ctypes.c_void_p()
        hr = GetMixFormat(ac_ptr, ctypes.byref(fmt_ptr))
        if hr != 0 or not fmt_ptr.value:
            return None
        raw = bytes((ctypes.c_byte * 40).from_address(fmt_ptr.value))
        ctypes.windll.ole32.CoTaskMemFree(fmt_ptr)
        return raw
    except Exception:
        return None


def _parse_waveformat(raw: bytes):
    """Parse WAVEFORMATEX/WAVEFORMATEXTENSIBLE; return (rate, channels, bits)."""
    if len(raw) < 18:
        return 48000, 2, 32
    _fmt_tag, channels, rate, _, _, bits, _ = struct.unpack_from("<HHIIHH H", raw)
    return rate, channels, bits


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
        self._pid       = pid
        self._running   = False
        self._sf: Optional[sf.SoundFile] = None
        self._sf_lock   = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.current_temp_path: Optional[str] = None

        self._sample_rate = 48000
        self._channels    = 2
        self._bits        = 32

        self._init_error: Optional[Exception] = None
        self._ready      = threading.Event()

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> str:
        temp_path = self._new_temp_path()
        # SoundFile will be opened once format is known (inside MTA thread)
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
        new_sf = sf.SoundFile(
            new_path, mode="w",
            samplerate=self._sample_rate,
            channels=self._channels,
            format="FLAC", subtype="PCM_24",
        )
        with self._sf_lock:
            old_sf   = self._sf
            self._sf = new_sf
            old_path = self.current_temp_path
            self.current_temp_path = new_path
        if old_sf:
            old_sf.close()
        return old_path, new_path

    # ── MTA thread — activation + capture ─────────────────────────────────────

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

        # Open the SoundFile now that we know the real format
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

        # Stop IAudioClient
        try:
            vt   = _vtbl(ac_ptr)
            Stop = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vt[11])
            Stop(ac_ptr)
        except Exception:
            pass

        ole32.CoUninitialize()

    # ── Activation ─────────────────────────────────────────────────────────────

    def _activate_and_start(self):
        mmdevapi = ctypes.windll.mmdevapi

        # ── Build AUDIOCLIENT_ACTIVATION_PARAMS (12 bytes) ──────────────────
        params_bytes = struct.pack(
            "<III",
            AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
            self._pid,
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
        )
        params_buf = (ctypes.c_byte * len(params_bytes))(*params_bytes)
        blob_ptr   = ctypes.cast(params_buf, ctypes.c_void_p)

        # ── Build PROPVARIANT (24 bytes on 64-bit) ───────────────────────────
        # offset  0: VARTYPE vt (2)
        # offset  2-7: reserved padding (6)
        # offset  8: BLOB.cbSize (4)
        # offset 12-15: padding for 8-byte pointer alignment (4)
        # offset 16: BLOB.pBlobData (8)
        propvar = (ctypes.c_byte * 24)()
        struct.pack_into("<H", propvar, 0,  VT_BLOB)
        struct.pack_into("<I", propvar, 8,  len(params_bytes))
        struct.pack_into("<Q", propvar, 16, blob_ptr.value)

        handler  = _CompletionHandler()
        iid_ac   = (ctypes.c_byte * 16)(*IID_IAudioClient)
        async_op = ctypes.c_void_p()

        hr = mmdevapi.ActivateAudioInterfaceAsync(
            ctypes.c_wchar_p(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK),
            ctypes.byref(iid_ac),
            ctypes.byref(propvar),
            handler.handler_ptr(),
            ctypes.byref(async_op),
        )
        if hr != 0:
            raise RuntimeError(
                f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}"
            )

        if not handler.done.wait(timeout=8.0):
            raise RuntimeError("Activation completion handler timed out")
        if handler.hr != 0:
            raise RuntimeError(
                f"Activation error: 0x{handler.hr & 0xFFFFFFFF:08X}"
            )
        if not handler.ac_ptr:
            raise RuntimeError("Activation returned null audio client")

        ac_ptr = handler.ac_ptr

        # ── GetMixFormat to detect real sample rate / channels ───────────────
        fmt_raw = _ac_get_mix_format(ac_ptr)
        if fmt_raw:
            self._sample_rate, self._channels, self._bits = _parse_waveformat(fmt_raw)
            fmt_ptr_val = ctypes.cast(
                (ctypes.c_byte * len(fmt_raw))(*fmt_raw), ctypes.c_void_p
            )

        # ── Initialize ───────────────────────────────────────────────────────
        vt = _vtbl(ac_ptr)
        Initialize = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_uint,
            ctypes.c_longlong, ctypes.c_longlong,
            ctypes.c_void_p, ctypes.c_void_p,
        )(vt[3])

        wfx_buf = (ctypes.c_byte * len(fmt_raw))(*fmt_raw) if fmt_raw else (ctypes.c_byte * 0)()
        hr = Initialize(
            ac_ptr,
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK |
            AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            0, 0,
            ctypes.cast(wfx_buf, ctypes.c_void_p),
            None,
        )
        if hr != 0:
            raise RuntimeError(f"IAudioClient::Initialize failed: 0x{hr & 0xFFFFFFFF:08X}")

        # ── GetService(IAudioCaptureClient) — vtable index 14 ────────────────
        iid_cc = (ctypes.c_byte * 16)(*IID_IAudioCaptureClient)
        cc_ptr = ctypes.c_void_p()
        GetService = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_byte * 16),
            ctypes.POINTER(ctypes.c_void_p),
        )(vt[14])
        hr = GetService(ac_ptr, ctypes.byref(iid_cc), ctypes.byref(cc_ptr))
        if hr != 0:
            raise RuntimeError(
                f"GetService(IAudioCaptureClient) failed: 0x{hr & 0xFFFFFFFF:08X}"
            )

        # ── Start — vtable index 10 ───────────────────────────────────────────
        Start = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vt[10])
        Start(ac_ptr)

        return ac_ptr, cc_ptr.value

    # ── Capture loop ───────────────────────────────────────────────────────────

    def _capture_loop(self, cc_ptr_val: int):
        cc_iface = ctypes.cast(cc_ptr_val, ctypes.POINTER(ctypes.c_void_p))
        vt       = ctypes.cast(cc_iface[0], ctypes.POINTER(ctypes.c_void_p))

        # IAudioCaptureClient: [3]GetBuffer [4]ReleaseBuffer [5]GetNextPacketSize
        GetBuffer = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        )(vt[3])
        ReleaseBuffer = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint
        )(vt[4])
        GetNextPacketSize = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
        )(vt[5])

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

            frame_count = n.value
            raw = bytes((ctypes.c_byte * (frame_count * bytes_per_frame))
                        .from_address(data.value))

            # Convert to float32 for writing; then quantise to int32 for PCM_24
            if self._bits == 32:
                samples_f = np.frombuffer(raw, dtype=np.float32)
            elif self._bits == 16:
                samples_f = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                samples_f = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0

            samples_f = samples_f.reshape(-1, self._channels)
            # Quantise to int32 range for PCM_24 FLAC (soundfile expects int32 values)
            samples_i = (samples_f * 8388607.0).clip(-8388608, 8388607).astype(np.int32)

            with self._sf_lock:
                if self._sf:
                    try:
                        self._sf.write(samples_i)
                    except Exception:
                        pass

            ReleaseBuffer(cc_ptr_val, frame_count)

    # ── File helpers ───────────────────────────────────────────────────────────

    def _new_temp_path(self) -> str:
        import uuid
        return str(self.save_directory / f".amr_tmp_{uuid.uuid4().hex}.flac")

    def _close_sf(self) -> Optional[str]:
        with self._sf_lock:
            sf_ref = self._sf
            path   = self.current_temp_path
            self._sf = None
            self.current_temp_path = None
        if sf_ref:
            sf_ref.close()
        return path
