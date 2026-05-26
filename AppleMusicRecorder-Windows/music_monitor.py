"""
music_monitor.py
Polls the Windows SMTC every 0.3 s to detect Apple Music playback.
Exposes:
  on_track_changed(TrackInfo | None)   – song changed or stopped
  on_playback_status(status_str)       – "playing" | "paused" | "stopped"
  on_seek_detected(position_sec)       – user scrubbed the timeline

TrackInfo now includes duration_sec and position_sec from SMTC.
"""
import asyncio
import threading
import time
import requests
from dataclasses import dataclass, field
from typing import Optional, Callable


@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str
    duration_sec: float = 0.0
    artwork_data: Optional[bytes] = field(default=None, repr=False)

    def __eq__(self, other):
        if not isinstance(other, TrackInfo):
            return False
        return (self.title == other.title and
                self.artist == other.artist and
                self.album == other.album)

    @property
    def safe_filename(self) -> str:
        raw = f"{self.artist} - {self.album} - {self.title}"
        for ch in r'/\:*?"<>|':
            raw = raw.replace(ch, "_")
        return raw[:200]


# Tolerance for seek detection: if position jumps more than this vs expected,
# we treat it as a user seek.
_SEEK_THRESHOLD_SEC = 4.0
_POLL_INTERVAL = 0.3


class MusicMonitor:
    def __init__(self):
        self.on_track_changed:     Optional[Callable] = None
        self.on_playback_status:   Optional[Callable] = None  # ("playing"|"paused"|"stopped")
        self.on_seek_detected:     Optional[Callable] = None  # (position_sec: float)

        self._current: Optional[TrackInfo] = None
        self._last_status: str = "stopped"
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Seek detection state
        self._playback_start_wall: Optional[float] = None   # wall time when play began
        self._playback_start_pos: float = 0.0               # SMTC position when play began

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="MusicMonitor")
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while self._running:
                try:
                    result = loop.run_until_complete(self._poll())
                except Exception:
                    result = None
                time.sleep(_POLL_INTERVAL)
        finally:
            loop.close()

    async def _poll(self):
        try:
            import winsdk.windows.media.control as wmc
        except ImportError:
            return

        try:
            manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            session = manager.get_current_session()
        except Exception:
            return

        if session is None:
            self._emit_status("stopped")
            self._emit_track(None)
            return

        # ── Playback status ───────────────────────────────────────────────────
        try:
            playback = session.get_playback_info()
            PLAYING = wmc.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
            PAUSED  = wmc.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PAUSED
            is_playing = playback and playback.playback_status == PLAYING
            is_paused  = playback and playback.playback_status == PAUSED
        except Exception:
            is_playing = is_paused = False

        if is_playing:
            raw_status = "playing"
        elif is_paused:
            raw_status = "paused"
        else:
            raw_status = "stopped"

        self._emit_status(raw_status)

        if not is_playing and not is_paused:
            self._emit_track(None)
            return

        # ── Media properties ──────────────────────────────────────────────────
        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            props = None

        if props is None or not props.title:
            self._emit_track(None)
            return

        title  = props.title       or ""
        artist = props.artist      or ""
        album  = props.album_title or ""

        # ── Timeline (duration + position) ────────────────────────────────────
        duration_sec = 0.0
        position_sec = 0.0
        try:
            tl = session.get_timeline_properties()
            if tl:
                # WinRT TimeSpan values are in 100-nanosecond ticks
                duration_sec = tl.end_time.duration / 1e7
                position_sec = tl.position.duration / 1e7
        except Exception:
            pass

        # ── Seek detection ────────────────────────────────────────────────────
        if is_playing:
            self._check_seek(position_sec)

        # ── Track change ──────────────────────────────────────────────────────
        if not title:
            self._emit_track(None)
            return

        candidate = TrackInfo(title=title, artist=artist, album=album,
                              duration_sec=duration_sec)

        if candidate != self._current:
            artwork = self._fetch_artwork(artist, album, title)
            candidate.artwork_data = artwork
            self._emit_track(candidate)
            # Reset seek tracking for new song
            if is_playing:
                self._playback_start_wall = time.monotonic()
                self._playback_start_pos  = position_sec

    # ── Seek detection ────────────────────────────────────────────────────────

    def _check_seek(self, position_sec: float):
        if self._playback_start_wall is None:
            self._playback_start_wall = time.monotonic()
            self._playback_start_pos  = position_sec
            return

        elapsed_wall = time.monotonic() - self._playback_start_wall
        expected_pos = self._playback_start_pos + elapsed_wall
        delta = abs(position_sec - expected_pos)

        if delta > _SEEK_THRESHOLD_SEC:
            # Seek detected — reset tracking from new position
            self._playback_start_wall = time.monotonic()
            self._playback_start_pos  = position_sec
            if self.on_seek_detected:
                try:
                    self.on_seek_detected(position_sec)
                except Exception:
                    pass

    # ── Emitters ──────────────────────────────────────────────────────────────

    def _emit_status(self, status: str):
        if status != self._last_status:
            self._last_status = status
            if status == "stopped":
                # Reset seek tracking
                self._playback_start_wall = None
            elif status == "playing" and self._playback_start_wall is None:
                self._playback_start_wall = time.monotonic()
                self._playback_start_pos  = 0.0
            if self.on_playback_status:
                try:
                    self.on_playback_status(status)
                except Exception:
                    pass

    def _emit_track(self, track: Optional[TrackInfo]):
        if track != self._current:
            self._current = track
            if self.on_track_changed:
                try:
                    self.on_track_changed(track)
                except Exception:
                    pass

    # ── Artwork ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_artwork(artist: str, album: str, title: str) -> Optional[bytes]:
        try:
            term = f"{artist} {album}".strip() or title
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": term, "media": "music", "entity": "album", "limit": 1},
                timeout=5,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None
            url = results[0].get("artworkUrl100", "")
            if not url:
                return None
            url = url.replace("100x100bb", "1000x1000bb")
            img_resp = requests.get(url, timeout=8)
            img_resp.raise_for_status()
            return img_resp.content
        except Exception:
            return None
