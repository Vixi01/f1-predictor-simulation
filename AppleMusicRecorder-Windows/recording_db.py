"""
recording_db.py
Tracks the status of every recorded song in a JSON file so we never
re-record a verified song and can flag incomplete captures for replacement.
"""
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class Status(str, Enum):
    VERIFIED   = "verified"     # full, duration-matched recording exists
    INCOMPLETE = "incomplete"   # recording was cut short (pause/seek/crash)
    RECORDING  = "recording"    # currently being captured


@dataclass
class Record:
    status: str
    path: str
    duration_expected: float    # seconds from SMTC
    duration_recorded: float    # seconds in saved file (-1 if unknown)
    recorded_at: str
    reason: str = ""            # why incomplete, if applicable


DB_FILENAME = ".recordings.json"


class RecordingDB:
    def __init__(self, save_directory: str):
        self._path = Path(save_directory) / DB_FILENAME
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    # ── Public API ─────────────────────────────────────────────────────────────

    def should_record(self, artist: str, album: str, title: str) -> tuple[bool, str]:
        """
        Returns (True, reason) if we should record, (False, reason) if not.
        We skip only VERIFIED records whose file still exists on disk.
        INCOMPLETE ones always get re-recorded.
        """
        key = self._key(artist, album, title)
        with self._lock:
            rec = self._data.get(key)
        if rec is None:
            return True, "no existing recording"
        if rec["status"] == Status.VERIFIED:
            file_path = rec.get("path", "")
            if file_path and Path(file_path).exists():
                return False, f"already verified ({file_path})"
            # File was deleted — reset to allow re-recording
            return True, "verified file was deleted, re-recording"
        if rec["status"] == Status.INCOMPLETE:
            return True, f"previous recording was incomplete: {rec.get('reason', '')}"
        return True, "re-recording"

    def mark_recording(self, artist: str, album: str, title: str,
                       expected_duration: float, path: str):
        self._upsert(artist, album, title, {
            "status": Status.RECORDING,
            "path": path,
            "duration_expected": expected_duration,
            "duration_recorded": -1,
            "recorded_at": _now(),
            "reason": "",
        })

    def mark_verified(self, artist: str, album: str, title: str,
                      path: str, duration_recorded: float, duration_expected: float):
        self._upsert(artist, album, title, {
            "status": Status.VERIFIED,
            "path": path,
            "duration_expected": duration_expected,
            "duration_recorded": duration_recorded,
            "recorded_at": _now(),
            "reason": "",
        })

    def mark_incomplete(self, artist: str, album: str, title: str,
                        path: str, reason: str, duration_recorded: float = -1,
                        duration_expected: float = -1):
        self._upsert(artist, album, title, {
            "status": Status.INCOMPLETE,
            "path": path,
            "duration_expected": duration_expected,
            "duration_recorded": duration_recorded,
            "recorded_at": _now(),
            "reason": reason,
        })

    def get_incomplete(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [(k, v) for k, v in self._data.items()
                    if v["status"] == Status.INCOMPLETE]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _key(self, artist: str, album: str, title: str) -> str:
        return f"{artist}|{album}|{title}"

    def _upsert(self, artist: str, album: str, title: str, record: dict):
        key = self._key(artist, album, title)
        with self._lock:
            self._data[key] = record
            self._save()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
