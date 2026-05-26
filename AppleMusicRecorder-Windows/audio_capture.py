"""
audio_capture.py
Records Windows system audio (WASAPI loopback) to a lossless FLAC temp file.
Supports atomic file-swapping so recordings can be split per track without
stopping the audio stream.
"""
import os
import threading  # used by _sf_lock
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf
from typing import Callable, Optional


CHUNK = 1024   # frames per read
SILENCE_RMS_THRESHOLD = 30      # int16 RMS below this is considered silence (~-60 dBFS)
SILENCE_DURATION_SEC  = 10.0    # seconds of sustained silence before firing callback


class AudioCapture:
    def __init__(self, save_directory: str,
                 on_silence: Optional[Callable[[], None]] = None):
        self.save_directory = Path(save_directory)
        self.save_directory.mkdir(parents=True, exist_ok=True)

        self._pa = pyaudio.PyAudio()
        self._stream: Optional[pyaudio.Stream] = None
        self._sf: Optional[sf.SoundFile] = None
        self._sf_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._on_silence   = on_silence
        self._silent_frames: int  = 0
        self._silence_fired: bool = False

        self._device_info = self._find_loopback_device()
        self._sample_rate = int(self._device_info["defaultSampleRate"])
        self._channels = min(int(self._device_info["maxInputChannels"]), 2)

        # Path of the temp file currently being written
        self.current_temp_path: Optional[str] = None

    # ── Device discovery ───────────────────────────────────────────────────────

    def _find_loopback_device(self) -> dict:
        try:
            self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            raise RuntimeError(
                "WASAPI not available on this system. "
                "Make sure you're running Windows 10/11 with a working audio device."
            )

        devices = [self._pa.get_device_info_by_index(i)
                   for i in range(self._pa.get_device_count())]

        # Prefer CABLE Output as a direct input (captures exactly what VB-Cable receives)
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        for d in devices:
            name = d["name"].lower()
            if ("cable output" in name and "16ch" not in name
                    and d["maxInputChannels"] >= 1
                    and d.get("hostApi") == wasapi["index"]):
                print(f"[capture] Using VB-Cable direct input: {d['name']}")
                return d

        # Fallback: default system loopback
        loopbacks = [d for d in devices if d.get("isLoopbackDevice")]
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in loopbacks:
            if default_out["name"] in d["name"]:
                print(f"[capture] Using system loopback: {d['name']}")
                return d

        raise RuntimeError(
            "Could not find a WASAPI loopback device.\n"
            "Open Sound Settings → make sure your speakers/headphones are the default output."
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> str:
        """Open the audio stream and start recording. Returns the temp file path."""
        temp_path = self._new_temp_path()
        self._open_file(temp_path)
        self._running = True

        # Callback mode avoids blocking reads — WASAPI loopback only delivers
        # frames when audio is actually playing, so a blocking read() would hang
        # forever in silence. Callback mode fires only when data is available.
        def _callback(in_data, frame_count, time_info, status):
            if not self._running:
                return (None, pyaudio.paComplete)
            samples = np.frombuffer(in_data, dtype=np.int16).reshape(-1, self._channels)
            with self._sf_lock:
                if self._sf:
                    try:
                        self._sf.write(samples)
                    except Exception:
                        pass

            # Silence detection
            if self._on_silence and samples.size > 0:
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                if rms < SILENCE_RMS_THRESHOLD:
                    self._silent_frames += frame_count
                else:
                    self._silent_frames = 0
                    self._silence_fired = False
                if (not self._silence_fired
                        and self._silent_frames / self._sample_rate >= SILENCE_DURATION_SEC):
                    self._silence_fired = True
                    try:
                        self._on_silence()
                    except Exception:
                        pass

            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._sample_rate,
            input=True,
            input_device_index=self._device_info["index"],
            frames_per_buffer=CHUNK,
            stream_callback=_callback,
        )
        self._stream.start_stream()
        self.current_temp_path = temp_path
        return temp_path

    def stop(self) -> Optional[str]:
        """Stop recording and close the current file. Returns the closed file path."""
        self._running = False
        self._silent_frames = 0
        self._silence_fired = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        return self._close_file()

    def split(self) -> tuple[str, str]:
        """
        Atomically swap to a new temp file without stopping the stream.
        Returns (finished_path, new_path).
        """
        self._silent_frames = 0
        self._silence_fired = False
        new_path = self._new_temp_path()
        new_sf = sf.SoundFile(
            new_path, mode="w",
            samplerate=self._sample_rate,
            channels=self._channels,
            format="FLAC", subtype="PCM_16",
        )

        with self._sf_lock:
            old_sf = self._sf
            self._sf = new_sf
            old_path = self.current_temp_path
            self.current_temp_path = new_path

        if old_sf:
            old_sf.close()

        return old_path, new_path

    # ── File helpers ───────────────────────────────────────────────────────────

    def _new_temp_path(self) -> str:
        name = f".amr_tmp_{uuid.uuid4().hex}.flac"
        return str(self.save_directory / name)

    def _open_file(self, path: str):
        self._sf = sf.SoundFile(
            path, mode="w",
            samplerate=self._sample_rate,
            channels=self._channels,
            format="FLAC", subtype="PCM_16",
        )

    def _close_file(self) -> Optional[str]:
        with self._sf_lock:
            sf_ref = self._sf
            path = self.current_temp_path
            self._sf = None
            self.current_temp_path = None
        if sf_ref:
            sf_ref.close()
        return path

    def __del__(self):
        try:
            self._pa.terminate()
        except Exception:
            pass
