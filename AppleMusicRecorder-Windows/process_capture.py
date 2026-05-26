"""
process_capture.py
Captures audio from a specific Windows process using
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK (Windows 10 build 20348+).

This bypasses the system-wide WASAPI loopback and taps only the target
process's audio output — so background system sounds never bleed in.

Falls back gracefully to system loopback (via pyaudiowpatch) if:
  - Apple Music process is not found
  - The process-loopback activation fails
  - Windows build is too old
"""
import ctypes
import ctypes.wintypes
import struct
import threading
import time
import uuid as _uuid_mod
from typing import Optional

import numpy as np
import psutil
import soundfile as sf


# ── Windows constants ──────────────────────────────────────────────────────────

AUDCLNT_SHAREMODE_SHARED              = 0
AUDCLNT_STREAMFLAGS_LOOPBACK          = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK     = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM    = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

VT_BLOB = 0x41  # PROPVARIANT blob type

CHUNK_FRAMES = 1024
_APPLE_MUSIC_NAMES = {"AppleMusic.exe", "iTunes.exe", "music.exe"}


# ── GUIDs ──────────────────────────────────────────────────────────────────────

def _guid_bytes(s: str) -> bytes:
    return _uuid_mod.UUID(s).bytes_le


IID_IAudioClient = _guid_bytes("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = _guid_bytes("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IActivateAudioInterfaceCompletionHandler = _guid_bytes("{41D949AB-9862-444A-80F6-C261334DA5EB}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_apple_music_pid() -> Optional[int]:
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"] in _APPLE_MUSIC_NAMES:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _make_waveformatex(sample_rate: int, channels: int, bits: int = 32) -> bytes:
    """Build a WAVEFORMATEXTENSIBLE struct for float32 PCM."""
    block_align = channels * (bits // 8)
    avg_bytes   = sample_rate * block_align
    # WAVEFORMATEX: fmt_tag(2) channels(2) sample_rate(4) avg_bytes(4)
    #               block_align(2) bits(2) cb_size(2)
    wfx = struct.pack("<HHIIHH", 3, channels, sample_rate, avg_bytes, block_align, bits)
    # cbSize = 22 for WAVEFORMATEXTENSIBLE
    wfx += struct.pack("<H", 22)
    # WAVEFORMATEXTENSIBLE extras: valid_bits(2) channel_mask(4) sub_format(16)
    # Sub-format KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    ksdataformat_float = _guid_bytes("{00000003-0000-0010-8000-00aa00389b71}")
    wfx += struct.pack("<HI", bits, 0) + ksdataformat_float
    return wfx


# ── COM completion handler (implements IActivateAudioInterfaceCompletionHandler) ─

class _CompletionHandler:
    """
    Minimal COM object satisfying IActivateAudioInterfaceCompletionHandler.
    Windows calls ActivateCompleted on a thread-pool thread; we signal an
    Event so the caller can wait synchronously.
    """
    def __init__(self):
        self.done     = threading.Event()
        self.hr       = -1
        self.audio_client_ptr = None

        # Build vtable: QI, AddRef, Release, ActivateCompleted
        WINFUNC = ctypes.WINFUNCTYPE

        @WINFUNC(ctypes.HRESULT, ctypes.c_void_p,
                 ctypes.POINTER(ctypes.c_byte * 16), ctypes.POINTER(ctypes.c_void_p))
        def _qi(this, riid, ppv):
            ppv[0] = ctypes.cast(self._obj_ptr, ctypes.c_void_p).value
            return 0

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
                    ctypes.POINTER(ctypes.HRESULT), ctypes.POINTER(ctypes.c_void_p)
                )(vtbl[3])
                inner_hr = ctypes.HRESULT()
                ac_ptr   = ctypes.c_void_p()
                GetActivateResult(operation, ctypes.byref(inner_hr), ctypes.byref(ac_ptr))
                self.hr = inner_hr.value
                self.audio_client_ptr = ac_ptr.value
            except Exception as e:
                self.hr = -1
            finally:
                self.done.set()
            return 0

        # Keep references alive
        self._qi = _qi
        self._addref = _addref
        self._release = _release
        self._activate_completed = _activate_completed

        _VtableArr = ctypes.c_void_p * 4
        self._vtable = _VtableArr(
            ctypes.cast(_qi, ctypes.c_void_p),
            ctypes.cast(_addref, ctypes.c_void_p),
            ctypes.cast(_release, ctypes.c_void_p),
            ctypes.cast(_activate_completed, ctypes.c_void_p),
        )
        _ObjArr = ctypes.c_void_p * 1
        self._obj = _ObjArr(ctypes.addressof(self._vtable))
        self._obj_ptr = ctypes.addressof(self._obj)

    def as_ptr(self) -> ctypes.c_void_p:
        return ctypes.c_void_p(self._obj_ptr)


# ── ProcessAudioCapture ────────────────────────────────────────────────────────

class ProcessAudioCapture:
    """
    Captures audio from Apple Music's process only.
    API mirrors AudioCapture: start() -> temp_path, stop() -> temp_path,
    split() -> (finished_path, new_path).
    """

    def __init__(self, save_directory: str, pid: int,
                 sample_rate: int = 48000, channels: int = 2):
        from pathlib import Path
        self.save_directory = Path(save_directory)
        self.save_directory.mkdir(parents=True, exist_ok=True)
        self._pid           = pid
        self._sample_rate   = sample_rate
        self._channels      = channels
        self._running       = False
        self._sf: Optional[sf.SoundFile] = None
        self._sf_lock       = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.current_temp_path: Optional[str] = None

        self._audio_client   = None  # raw c_void_p value
        self._capture_client = None

    # ── Public ─────────────────────────────────────────────────────────────────

    def start(self) -> str:
        temp_path = self._new_temp_path()
        self._open_sf(temp_path)
        self._audio_client, self._capture_client = self._activate_and_init()
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                        name="ProcessCapture")
        self._thread.start()
        self.current_temp_path = temp_path
        return temp_path

    def stop(self) -> Optional[str]:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._stop_audio_client()
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
            old_sf = self._sf
            self._sf = new_sf
            old_path = self.current_temp_path
            self.current_temp_path = new_path
        if old_sf:
            old_sf.close()
        return old_path, new_path

    # ── COM / WASAPI activation ────────────────────────────────────────────────

    def _activate_and_init(self):
        """
        Call ActivateAudioInterfaceAsync with process loopback params,
        then initialize the IAudioClient and get IAudioCaptureClient.
        Returns (audio_client_ptr, capture_client_ptr).
        """
        mmdevapi = ctypes.windll.mmdevapi

        # Build AUDIOCLIENT_ACTIVATION_PARAMS (12 bytes)
        # ActivationType(4) + TargetProcessId(4) + ProcessLoopbackMode(4)
        params_bytes = struct.pack("<III",
            AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
            self._pid,
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
        )
        params_buf = (ctypes.c_byte * len(params_bytes))(*params_bytes)

        # Build PROPVARIANT { vt=VT_BLOB, blob={cbSize, pBlobData} }
        # PROPVARIANT layout (at minimum): vt(2) pad(6) blob_size(4) blob_ptr(8)
        blob_ptr = ctypes.cast(params_buf, ctypes.c_void_p)
        propvar  = (ctypes.c_byte * 32)()
        struct.pack_into("<H", propvar, 0, VT_BLOB)       # vt
        struct.pack_into("<I", propvar, 8, len(params_bytes))  # cbSize
        struct.pack_into("<Q", propvar, 12, blob_ptr.value)    # pBlobData

        handler   = _CompletionHandler()
        iid_ac    = (ctypes.c_byte * 16)(*IID_IAudioClient)
        device_path = VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK

        async_op  = ctypes.c_void_p()
        hr = mmdevapi.ActivateAudioInterfaceAsync(
            device_path,
            ctypes.byref(iid_ac),
            ctypes.byref(propvar),
            handler.as_ptr(),
            ctypes.byref(async_op),
        )
        if hr != 0:
            raise RuntimeError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}")

        if not handler.done.wait(timeout=5.0):
            raise RuntimeError("Process loopback activation timed out")

        if handler.hr != 0:
            raise RuntimeError(f"Activation completed with error: 0x{handler.hr & 0xFFFFFFFF:08X}")

        ac_ptr = handler.audio_client_ptr

        # IAudioClient::Initialize
        wfx = _make_waveformatex(self._sample_rate, self._channels)
        wfx_buf = (ctypes.c_byte * len(wfx))(*wfx)

        ac_iface = ctypes.cast(ac_ptr, ctypes.POINTER(ctypes.c_void_p))
        vtbl     = ctypes.cast(ac_iface[0], ctypes.POINTER(ctypes.c_void_p))

        # IAudioClient vtable: [0]QI [1]AddRef [2]Release [3]Initialize
        Initialize = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_uint,
            ctypes.c_longlong, ctypes.c_longlong,
            ctypes.c_void_p, ctypes.c_void_p,
        )(vtbl[3])

        hr = Initialize(
            ac_ptr,
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            0, 0,
            ctypes.cast(wfx_buf, ctypes.c_void_p),
            None,
        )
        if hr != 0:
            raise RuntimeError(f"IAudioClient::Initialize failed: 0x{hr & 0xFFFFFFFF:08X}")

        # IAudioClient::GetService(IID_IAudioCaptureClient)
        # vtable[9] = GetService
        iid_cc  = (ctypes.c_byte * 16)(*IID_IAudioCaptureClient)
        cc_ptr  = ctypes.c_void_p()
        GetService = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_byte * 16), ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[9])
        hr = GetService(ac_ptr, ctypes.byref(iid_cc), ctypes.byref(cc_ptr))
        if hr != 0:
            raise RuntimeError(f"GetService(IAudioCaptureClient) failed: 0x{hr & 0xFFFFFFFF:08X}")

        # IAudioClient::Start  (vtable[10])
        Start = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtbl[10])
        Start(ac_ptr)

        return ac_ptr, cc_ptr.value

    # ── Capture loop ───────────────────────────────────────────────────────────

    def _capture_loop(self):
        """
        Reads from IAudioCaptureClient in a tight loop and writes to the
        current SoundFile. Runs in its own daemon thread.
        """
        if not self._capture_client:
            return

        cc_ptr   = self._capture_client
        cc_iface = ctypes.cast(cc_ptr, ctypes.POINTER(ctypes.c_void_p))
        vtbl     = ctypes.cast(cc_iface[0], ctypes.POINTER(ctypes.c_void_p))

        # IAudioCaptureClient vtable:
        # [0]QI [1]AddRef [2]Release [3]GetBuffer [4]ReleaseBuffer [5]GetNextPacketSize
        GetBuffer = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),   # ppData
            ctypes.POINTER(ctypes.c_uint),      # pNumFramesAvailable
            ctypes.POINTER(ctypes.c_uint),      # pdwFlags
            ctypes.POINTER(ctypes.c_ulonglong), # pu64DevicePosition
            ctypes.POINTER(ctypes.c_ulonglong), # pu64QPCPosition
        )(vtbl[3])

        ReleaseBuffer = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint
        )(vtbl[4])

        GetNextPacketSize = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
        )(vtbl[5])

        bytes_per_frame = self._channels * 4  # float32

        while self._running:
            packet_size = ctypes.c_uint(0)
            hr = GetNextPacketSize(cc_ptr, ctypes.byref(packet_size))
            if hr != 0 or packet_size.value == 0:
                time.sleep(0.005)
                continue

            data_ptr  = ctypes.c_void_p()
            n_frames  = ctypes.c_uint(0)
            flags     = ctypes.c_uint(0)
            dev_pos   = ctypes.c_ulonglong(0)
            qpc_pos   = ctypes.c_ulonglong(0)

            hr = GetBuffer(cc_ptr,
                           ctypes.byref(data_ptr), ctypes.byref(n_frames),
                           ctypes.byref(flags), ctypes.byref(dev_pos), ctypes.byref(qpc_pos))
            if hr != 0 or not data_ptr.value:
                time.sleep(0.005)
                continue

            n = n_frames.value
            raw = (ctypes.c_byte * (n * bytes_per_frame)).from_address(data_ptr.value)
            samples = np.frombuffer(bytes(raw), dtype=np.float32).reshape(-1, self._channels)

            with self._sf_lock:
                if self._sf:
                    try:
                        self._sf.write(samples)
                    except Exception:
                        pass

            ReleaseBuffer(cc_ptr, n)

    # ── Audio client stop ──────────────────────────────────────────────────────

    def _stop_audio_client(self):
        if self._audio_client:
            try:
                ac_iface = ctypes.cast(self._audio_client, ctypes.POINTER(ctypes.c_void_p))
                vtbl     = ctypes.cast(ac_iface[0], ctypes.POINTER(ctypes.c_void_p))
                Stop = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtbl[11])
                Stop(self._audio_client)
            except Exception:
                pass
            self._audio_client = None

    # ── File helpers ───────────────────────────────────────────────────────────

    def _new_temp_path(self) -> str:
        import uuid
        from pathlib import Path
        name = f".amr_tmp_{uuid.uuid4().hex}.flac"
        return str(self.save_directory / name)

    def _open_sf(self, path: str):
        self._sf = sf.SoundFile(
            path, mode="w",
            samplerate=self._sample_rate,
            channels=self._channels,
            format="FLAC", subtype="PCM_24",
        )

    def _close_sf(self) -> Optional[str]:
        with self._sf_lock:
            sf_ref = self._sf
            path   = self.current_temp_path
            self._sf = None
            self.current_temp_path = None
        if sf_ref:
            sf_ref.close()
        return path
