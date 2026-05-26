"""
session_guard.py
Detects other apps producing audio during Apple Music recording.
Uses pycaw to enumerate WASAPI audio sessions and check peak levels.
Fires an on_contamination callback when non-Apple-Music audio is detected.
"""
import threading
from dataclasses import dataclass
from typing import Callable, Optional

_APPLE_MUSIC_NAMES = {"applemusic.exe", "itunes.exe", "music.exe",
                      "python.exe", "pythonw.exe", "amplibraryagent.exe"}
_PEAK_THRESHOLD    = 0.01   # sessions louder than this are "active"
_CHECK_INTERVAL    = 5.0    # seconds between periodic checks


@dataclass(frozen=True)
class ContaminatingSession:
    process_name: str
    pid: int
    peak: float


class SessionGuard:
    def __init__(
        self,
        on_contamination: Callable[[list[ContaminatingSession]], None],
        peak_threshold: float = _PEAK_THRESHOLD,
        interval_sec: float  = _CHECK_INTERVAL,
    ):
        self._cb          = on_contamination
        self._threshold   = peak_threshold
        self._interval    = interval_sec
        self._stop        = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public ─────────────────────────────────────────────────────────────────

    def check_now(self) -> list[ContaminatingSession]:
        """Synchronous one-shot check. Returns list of offending sessions."""
        offenders = self._scan()
        if offenders:
            try:
                self._cb(offenders)
            except Exception:
                pass
        return offenders

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="SessionGuard"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    # ── Internal ───────────────────────────────────────────────────────────────

    def _scan(self) -> list[ContaminatingSession]:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        except ImportError:
            return []

        offenders = []
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            return []

        for session in sessions:
            try:
                proc = session.Process
                name = proc.name().lower() if proc else "system"
                if name in _APPLE_MUSIC_NAMES:
                    continue
                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                peak  = float(meter.GetPeakValue())
                if peak > self._threshold:
                    offenders.append(ContaminatingSession(
                        process_name=name,
                        pid=proc.pid if proc else 0,
                        peak=round(peak, 3),
                    ))
            except Exception:
                continue

        return offenders

    def _loop(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            while not self._stop.wait(self._interval):
                try:
                    offenders = self._scan()
                    if offenders:
                        try:
                            self._cb(offenders)
                        except Exception:
                            pass
                except Exception:
                    pass
        finally:
            comtypes.CoUninitialize()
