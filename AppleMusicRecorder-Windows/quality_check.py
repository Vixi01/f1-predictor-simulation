"""
quality_check.py
Inspects the WASAPI loopback device's native mix format to infer whether
Apple Music's lossless audio output is likely enabled.

Apple Music (lossless) → Windows audio engine typically outputs at
44100, 88200, 176400, or 192000 Hz and 24-bit depth.
Standard lossy (AAC 256 kbps) → Windows keeps its default mix rate,
usually 48000 Hz at 16 or 24-bit.

Because the UWP sandbox blocks direct access to Apple Music's settings.dat,
this stream-format inspection is the best available signal.
"""
import ctypes
import ctypes.wintypes
import struct
from dataclasses import dataclass
from typing import Optional


# Lossless sample rates Apple Music uses (Hz).
# 44100  = CD lossless
# 88200  = High-Res Lossless (2× CD)
# 176400 = High-Res Lossless (4× CD)
# 192000 = High-Res Lossless (max)
_LOSSLESS_RATES = {44100, 48000, 88200, 96000, 176400, 192000}

# Windows default output rate — seeing this does NOT prove lossy, but it
# means the audio engine is not running in a lossless-native mode.
# 48000 is only flagged as "default" when Apple Music hasn't changed it
# (i.e. lossless is off). Once lossless is on and Windows is set to 96kHz+
# it will show as a lossless rate instead.
_WINDOWS_DEFAULT_RATE = -1  # no longer used as a special case


@dataclass
class QualityReport:
    sample_rate: int
    channels: int
    # None when we cannot determine bit depth from pyaudio info alone
    bit_depth: Optional[int]
    lossless_likely: bool
    warning: Optional[str]
    info: str


def check(device_info: dict) -> QualityReport:
    """
    Build a QualityReport from the pyaudiowpatch device info dict returned
    by pa.get_device_info_by_index().
    """
    rate = int(device_info.get("defaultSampleRate", 0))
    channels = min(int(device_info.get("maxInputChannels", 2)), 2)

    # Try to read the bit depth from the WAVEFORMATEXTENSIBLE struct via
    # Windows MMDevice API.  Falls back gracefully if ctypes call fails.
    bit_depth = _probe_mix_format_bits(device_info)

    lossless_likely = rate in _LOSSLESS_RATES

    warning: Optional[str] = None
    if not lossless_likely:
        warning = (
            f"Audio stream is at {rate} Hz — lossless may NOT be active.\n\n"
            "To enable lossless in Apple Music:\n"
            "  1. Open Apple Music\n"
            "  2. Go to Settings (gear icon) > Playback\n"
            "  3. Under Audio Quality, enable Lossless Audio\n"
            "  4. Set Streaming to Lossless or Hi-Res Lossless\n\n"
            "Also set Windows output device to 24-bit, 44100 Hz or higher.\n"
            "Then restart this recorder."
        )

    if lossless_likely:
        depth_str = f"{bit_depth}-bit / " if bit_depth else ""
        info = f"Audio quality looks good: {depth_str}{rate // 1000} kHz — lossless likely active."
    else:
        depth_str = f"{bit_depth}-bit / " if bit_depth else ""
        info = f"Detected: {depth_str}{rate} Hz — lossless may NOT be active."

    return QualityReport(
        sample_rate=rate,
        channels=channels,
        bit_depth=bit_depth,
        lossless_likely=lossless_likely,
        warning=warning,
        info=info,
    )


# ── Optional: read native mix format via COM/MMDevice ─────────────────────────
# If this probe fails for any reason we just skip and leave bit_depth = None.

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator   = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioClient          = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
_CLSCTX_ALL = 0x17
_eRender     = 0
_eConsole    = 0


def _probe_mix_format_bits(device_info: dict) -> Optional[int]:
    """Query the Windows MMDevice mix format to get the native bit depth."""
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        # CoCreateInstance(CLSID_MMDeviceEnumerator, ...)
        enumerator = ctypes.POINTER(ctypes.c_void_p)()
        clsid = _guid(_CLSID_MMDeviceEnumerator)
        iid_enum = _guid(_IID_IMMDeviceEnumerator)
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_ALL,
            ctypes.byref(iid_enum), ctypes.byref(enumerator)
        )
        if hr != 0:
            return None

        # GetDefaultAudioEndpoint(eRender, eConsole, &device)
        device = ctypes.POINTER(ctypes.c_void_p)()
        iface = ctypes.cast(enumerator, ctypes.POINTER(ctypes.c_void_p))
        # IMMDeviceEnumerator vtable: [0]=QI [1]=AddRef [2]=Release
        #                              [3]=EnumAudioEndpoints [4]=GetDefaultAudioEndpoint
        vtbl = ctypes.cast(iface[0], ctypes.POINTER(ctypes.c_void_p))
        GetDefaultAudioEndpoint = ctypes.CFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[4])
        hr = GetDefaultAudioEndpoint(enumerator, _eRender, _eConsole,
                                     ctypes.byref(device))
        if hr != 0:
            return None

        # IMMDevice::Activate(IID_IAudioClient, ..., &audio_client)
        iid_ac = _guid(_IID_IAudioClient)
        audio_client = ctypes.POINTER(ctypes.c_void_p)()
        iface_dev = ctypes.cast(device, ctypes.POINTER(ctypes.c_void_p))
        vtbl_dev = ctypes.cast(iface_dev[0], ctypes.POINTER(ctypes.c_void_p))
        # IMMDevice vtable: [0]=QI [1]=AddRef [2]=Release [3]=Activate
        Activate = ctypes.CFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_byte * 16),
            ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)
        )(vtbl_dev[3])
        hr = Activate(device, ctypes.byref(iid_ac), _CLSCTX_ALL, None,
                      ctypes.byref(audio_client))
        if hr != 0:
            return None

        # IAudioClient::GetMixFormat(&fmt)
        fmt_ptr = ctypes.c_void_p()
        iface_ac = ctypes.cast(audio_client, ctypes.POINTER(ctypes.c_void_p))
        vtbl_ac = ctypes.cast(iface_ac[0], ctypes.POINTER(ctypes.c_void_p))
        # IAudioClient vtable: [0]=QI [1]=AddRef [2]=Release [3]=Initialize
        #                       [4]=GetBufferSize [5]=GetStreamLatency
        #                       [6]=GetCurrentPadding [7]=IsFormatSupported
        #                       [8]=GetMixFormat
        GetMixFormat = ctypes.CFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl_ac[8])
        hr = GetMixFormat(audio_client, ctypes.byref(fmt_ptr))
        if hr != 0 or not fmt_ptr:
            return None

        # WAVEFORMATEX layout: wFormatTag(2) nChannels(2) nSamplesPerSec(4)
        #   nAvgBytesPerSec(4) nBlockAlign(2) wBitsPerSample(2) cbSize(2)
        raw = (ctypes.c_byte * 18).from_address(fmt_ptr.value)
        bits = struct.unpack_from("<H", bytes(raw), 14)[0]  # wBitsPerSample

        ctypes.windll.ole32.CoTaskMemFree(fmt_ptr)
        return bits if bits in (16, 24, 32) else None

    except Exception:
        return None


def _guid(guid_str: str) -> ctypes.Array:
    """Parse a GUID string into a CLSID/IID ctypes byte array."""
    import uuid as _uuid
    b = _uuid.UUID(guid_str).bytes_le
    arr = (ctypes.c_byte * 16)()
    ctypes.memmove(arr, b, 16)
    return arr
