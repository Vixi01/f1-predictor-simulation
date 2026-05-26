"""
recorder.py – Apple Music Recorder for Windows
Smart recording orchestrator with:
  - Per-process audio capture (Apple Music only, no bleed)
  - Pause / seek detection → discards interrupted recordings
  - Duration verification → only saves complete recordings
  - Recording DB → skips already-verified songs, flags incomplete ones

Usage:  python recorder.py  /  double-click run.bat
"""
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import pystray
from PIL import Image, ImageDraw

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import metadata_writer
import quality_check
from audio_capture import AudioCapture
from music_monitor import MusicMonitor, TrackInfo
from recording_db import RecordingDB, Status
from session_guard import SessionGuard, ContaminatingSession


# ── Config ─────────────────────────────────────────────────────────────────────

SAVE_DIR = str(Path.home() / "Music" / "Apple Music Recordings")

DURATION_TOLERANCE_SEC  = 3.0   # verified if recorded duration ± this matches SMTC
ALLOW_UNVERIFIED_DURATION = True # save even when SMTC provides no duration
MIN_TRACK_DURATION_SEC  = 30    # skip tracks shorter than this (interludes/skits)


# ── State ───────────────────────────────────────────────────────────────────────

_capture:  Optional[AudioCapture] = None
_monitor:  Optional[MusicMonitor] = None
_db:       Optional[RecordingDB]  = None
_lock      = threading.Lock()

_recording      = False
_current_track: Optional[TrackInfo] = None
_recording_interrupted = False   # set on pause or seek
_quality_report = None
_guard: Optional[SessionGuard] = None


# ── Capture factory (tries process loopback, falls back to system loopback) ────

def _on_silence_detected() -> None:
    msg = "No audio detected — check Apple Music is routed to VB-Cable."
    print(f"[warn] {msg}")
    _notify(msg, "Apple Music Recorder — No Audio")


def _make_capture(save_dir: str) -> AudioCapture:
    try:
        from process_capture import ProcessAudioCapture, find_apple_music_pid
        pid = find_apple_music_pid()
        if pid:
            cap = ProcessAudioCapture(save_dir, pid)
            print(f"[capture] Using per-process loopback for PID {pid} (Apple Music only)")
            return cap
    except Exception as e:
        print(f"[capture] Per-process loopback unavailable: {e}")
    cap = AudioCapture(save_dir, on_silence=_on_silence_detected)
    print("[capture] Using system-wide WASAPI loopback (fallback)")
    return cap


# ── Recording control ───────────────────────────────────────────────────────────

def _start_recording(track: TrackInfo):
    global _capture, _recording, _recording_interrupted
    with _lock:
        if _recording:
            return

        cap = _make_capture(SAVE_DIR)
        temp = None
        try:
            temp = cap.start()
        except Exception as e:
            print(f"[capture] Primary capture failed ({e}), falling back to system loopback")
            try:
                cap  = AudioCapture(SAVE_DIR, on_silence=_on_silence_detected)
                temp = cap.start()
                print("[capture] Using system-wide WASAPI loopback (fallback)")
            except Exception as e2:
                print(f"[error] Could not start recording: {e2}")
                return

        _capture = cap
        _recording = True
        _recording_interrupted = False
        if _db:
            _db.mark_recording(track.artist, track.album, track.title,
                               track.duration_sec, temp)
        print(f"[recording] started -> {Path(temp).name}")
        _set_tray_recording(True)
        if _guard:
            _guard.check_now()
            _guard.start()


def _stop_recording(reason: str = "natural end") -> Optional[tuple]:
    """Stop and return (temp_path, track, was_interrupted)."""
    global _capture, _recording
    with _lock:
        if not _recording or _capture is None:
            return None
        temp  = _capture.stop()
        track = _current_track
        was_interrupted = _recording_interrupted
        _recording = False
        print(f"[recording] stopped ({reason})")
    _set_tray_recording(False)
    if _guard:
        _guard.stop()
    return temp, track, was_interrupted


def _split_recording(new_track: TrackInfo):
    global _capture, _current_track
    with _lock:
        if not _recording or _capture is None:
            return
        finished_path, _ = _capture.split()
        old_track        = _current_track
        was_interrupted  = _recording_interrupted
        _current_track   = new_track

    if finished_path and old_track:
        _finalize(finished_path, old_track, was_interrupted)


def _discard_recording(reason: str):
    """Stop recording and delete the temp file — recording is unusable."""
    global _capture, _recording, _recording_interrupted
    with _lock:
        if not _recording or _capture is None:
            return
        temp = _capture.stop()
        _recording = False
        _recording_interrupted = True
    _set_tray_recording(False)
    if _guard:
        _guard.stop()
    if temp:
        try:
            os.remove(temp)
        except Exception:
            pass
    print(f"[recording] discarded — {reason}")
    if _current_track and _db:
        _db.mark_incomplete(_current_track.artist, _current_track.album,
                            _current_track.title, "", reason)


# ── Finalization & verification ─────────────────────────────────────────────────

def _finalize(temp_path: str, track: TrackInfo, was_interrupted: bool):
    """Tag, verify duration, update DB, save or discard."""
    if not os.path.exists(temp_path):
        return

    if was_interrupted:
        # Already discarded or flagged — just clean up
        try:
            os.remove(temp_path)
        except Exception:
            pass
        if _db:
            _db.mark_incomplete(track.artist, track.album, track.title,
                                 "", "interrupted (pause/seek)")
        print(f"[verify] discarded incomplete recording of: {track.title}")
        return

    # Measure actual recorded duration
    try:
        import soundfile as sf
        info = sf.info(temp_path)
        recorded_sec = info.duration
    except Exception:
        recorded_sec = 0.0

    expected_sec = track.duration_sec

    if expected_sec > 0:
        delta = abs(recorded_sec - expected_sec)
        if delta <= DURATION_TOLERANCE_SEC:
            verdict = "verified"
        else:
            verdict = "incomplete"
            reason  = (f"duration mismatch: recorded {recorded_sec:.1f}s "
                       f"vs expected {expected_sec:.1f}s")
    else:
        # No expected duration from SMTC — trust it if ALLOW_UNVERIFIED_DURATION
        verdict = "verified" if ALLOW_UNVERIFIED_DURATION else "incomplete"
        reason  = "SMTC did not provide track duration"

    if verdict == "verified":
        # Pass to metadata writer — it renames and tags the file
        metadata_writer.process(temp_path, track, SAVE_DIR,
                                on_saved=lambda final: _on_saved(track, final, recorded_sec, expected_sec))
    else:
        # Keep but flag incomplete so we re-record next time
        try:
            import soundfile as sf
            import uuid
            incomplete_name = f"{track.safe_filename}_incomplete_{uuid.uuid4().hex[:6]}.flac"
            incomplete_path = str(Path(SAVE_DIR) / incomplete_name)
            import shutil
            shutil.move(temp_path, incomplete_path)
        except Exception:
            incomplete_path = temp_path
        if _db:
            _db.mark_incomplete(track.artist, track.album, track.title,
                                 incomplete_path, reason, recorded_sec, expected_sec)
        print(f"[verify] INCOMPLETE — {reason}")
        _notify(f"Incomplete recording: {track.title}\n{reason}\nWill re-record next time it plays.")


def _on_saved(track: TrackInfo, final_path: str, recorded_sec: float, expected_sec: float):
    if _db:
        _db.mark_verified(track.artist, track.album, track.title,
                          final_path, recorded_sec, expected_sec)
    print(f"[verify] VERIFIED — {track.title} ({recorded_sec:.1f}s)")


# ── Monitor callbacks ───────────────────────────────────────────────────────────

def on_track_changed(track: Optional[TrackInfo]):
    global _current_track

    if track is None:
        result = _stop_recording("playback stopped")
        _current_track = None
        update_tray_title("Apple Music Recorder — idle")
        if result:
            temp, old_track, interrupted = result
            if temp and old_track:
                _finalize(temp, old_track, interrupted)
        return

    # Skip if we're joining the song mid-way — recording would be incomplete
    if track.position_sec > 1:
        print(f"[skip] {track.title} — already {track.position_sec:.0f}s in, waiting for full play")
        if _db:
            _db.mark_incomplete(track.artist, track.album, track.title,
                                "", "joined mid-song, needs full play from start")
        _current_track = track
        update_tray_title(f"[skip] {track.artist} – {track.title}")
        return

    # Skip very short tracks (interludes, skits, ad jingles)
    if track.duration_sec > 0 and track.duration_sec < MIN_TRACK_DURATION_SEC:
        print(f"[skip] {track.title} — too short ({track.duration_sec:.0f}s)")
        _current_track = track
        update_tray_title(f"[skip] {track.artist} – {track.title}")
        return

    # Check DB — skip if already verified
    if _db:
        should, reason = _db.should_record(track.artist, track.album, track.title)
        if not should:
            print(f"[skip] {track.title} — {reason}")
            _current_track = track
            update_tray_title(f"[verified] {track.artist} – {track.title}")
            return
        if "incomplete" in reason:
            print(f"[re-record] {track.title} — {reason}")

    if _recording:
        _split_recording(track)
    else:
        _current_track = track
        _start_recording(track)

    update_tray_title(f"REC  {track.artist} – {track.title}")
    print(f"[track] {track.artist} – {track.title}")


def on_playback_status(status: str):
    global _recording_interrupted
    if status == "paused" and _recording:
        _recording_interrupted = True
        _discard_recording("user paused playback")
        update_tray_title("Apple Music Recorder — paused (recording discarded)")
    elif status == "playing" and not _recording and _current_track:
        # Resumed — check DB again and start fresh
        on_track_changed(_current_track)


def on_seek_detected(position_sec: float):
    global _recording_interrupted
    if _recording:
        _recording_interrupted = True
        _discard_recording(f"user seeked to {position_sec:.1f}s")
        update_tray_title("Apple Music Recorder — seeked (recording discarded)")


# ── Tray ────────────────────────────────────────────────────────────────────────

_tray_icon: Optional[pystray.Icon] = None


def _make_icon(recording_active: bool) -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (220, 50, 50) if recording_active else (80, 180, 80)
    draw.ellipse([8, 8, size - 8, size - 8], fill=color)
    draw.rectangle([24, 20, 30, 42], fill="white")
    draw.rectangle([36, 16, 42, 38], fill="white")
    draw.rectangle([24, 20, 42, 26], fill="white")
    return img


def update_tray_title(title: str):
    if _tray_icon:
        _tray_icon.title = title


def _set_tray_recording(active: bool):
    if _tray_icon:
        try:
            _tray_icon.icon = _make_icon(active)
        except Exception:
            pass


def _notify(message: str, title: str = "Apple Music Recorder"):
    if _tray_icon:
        try:
            _tray_icon.notify(message, title)
        except Exception:
            pass


def _retry_untagged(save_dir: str):
    """On startup, find *_untagged.flac files and attempt to re-tag them."""
    folder = Path(save_dir)
    untagged = list(folder.glob("*_untagged.flac"))
    if not untagged:
        return
    print(f"[startup] Found {len(untagged)} untagged file(s), retrying...")
    recovered = 0
    for path in untagged:
        try:
            result = metadata_writer.retag_untagged_file(path, folder)
            if result is not None:
                recovered += 1
                print(f"[startup] re-tagged: {result.name}")
            else:
                print(f"[startup] could not parse filename: {path.name}")
        except Exception as e:
            print(f"[startup] re-tag error {path.name}: {e}")
    print(f"[startup] Re-tagged {recovered}/{len(untagged)} file(s).")


def _export_playlist(save_dir: str) -> Optional[Path]:
    """Write an M3U playlist of all FLAC recordings. Returns the playlist path."""
    from mutagen.flac import FLAC as _FLAC
    import soundfile as _sf
    folder = Path(save_dir)
    flacs  = sorted(folder.glob("*.flac"))
    if not flacs:
        return None
    playlist_path = folder / "Apple Music Recordings.m3u"
    lines = ["#EXTM3U"]
    for f in flacs:
        try:
            audio    = _FLAC(str(f))
            artist   = (audio.get("artist") or [""])[0]
            title    = (audio.get("title")  or [f.stem])[0]
            duration = int(_sf.info(str(f)).duration)
        except Exception:
            artist, title, duration = "", f.stem, -1
        lines.append(f"#EXTINF:{duration},{artist} - {title}")
        lines.append(str(f))
    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist_path


def _on_open_folder(icon, item):
    os.startfile(SAVE_DIR)


def _on_show_quality(icon, item):
    if _quality_report is None:
        icon.notify("Quality check not yet run.", "Audio Quality")
        return
    msg = _quality_report.info
    if _quality_report.warning:
        msg += "\n\n" + _quality_report.warning
    icon.notify(msg, "Audio Quality Check")


def _on_export_playlist(icon, item):
    try:
        result = _export_playlist(SAVE_DIR)
        if result is None:
            icon.notify("No recordings found.", "Export Playlist")
            return
        icon.notify(f"Saved: {result.name}", "Export Playlist")
        os.startfile(SAVE_DIR)
    except Exception as e:
        icon.notify(f"Export failed: {e}", "Export Playlist")


def _on_show_incomplete(icon, item):
    if _db is None:
        return
    incomplete = _db.get_incomplete()
    if not incomplete:
        icon.notify("No incomplete recordings.", "Recording Status")
        return
    names = "\n".join(k.split("|")[2] for k, _ in incomplete[:8])
    icon.notify(f"{len(incomplete)} incomplete recording(s):\n{names}", "Needs Re-recording")


def _on_quit(icon, item):
    result = _stop_recording("quit")
    if result:
        temp, track, interrupted = result
        if temp and track:
            _finalize(temp, track, interrupted)
    if _monitor:
        _monitor.stop()
    icon.stop()


def run_tray():
    global _tray_icon
    _tray_icon = pystray.Icon(
        name="AppleMusicRecorder",
        icon=_make_icon(False),
        title="Apple Music Recorder — idle",
        menu=pystray.Menu(
            pystray.MenuItem("Open recordings folder", _on_open_folder),
            pystray.MenuItem("Export playlist (M3U)",  _on_export_playlist),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Audio quality check",    _on_show_quality),
            pystray.MenuItem("Incomplete recordings",  _on_show_incomplete),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _on_quit),
        ),
    )

    def _on_setup(icon):
        icon.visible = True
        if _quality_report and _quality_report.warning:
            icon.notify(
                "Lossless may not be active. Right-click -> Audio quality check for details.",
                "Apple Music Recorder — Quality Warning",
            )
        # Announce any incomplete recordings from previous sessions
        if _db:
            incomplete = _db.get_incomplete()
            if incomplete:
                icon.notify(
                    f"{len(incomplete)} song(s) need re-recording from a previous session.\n"
                    "Play them in Apple Music to capture them.",
                    "Incomplete Recordings",
                )

    _tray_icon.run(setup=_on_setup)


# ── Entry point ─────────────────────────────────────────────────────────────────

def _acquire_single_instance() -> object:
    """Create a named Windows mutex. Returns the handle if this is the only instance,
    or exits the process if another instance is already running."""
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "AppleMusicRecorder_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[error] Another instance of Apple Music Recorder is already running.")
        sys.exit(0)
    return mutex  # keep reference alive for process lifetime


def main():
    global _monitor, _db, _quality_report, _guard

    _mutex = _acquire_single_instance()

    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[init] Saving recordings to: {SAVE_DIR}")

    _db = RecordingDB(SAVE_DIR)

    _retry_untagged(SAVE_DIR)

    # Session guard runs silently — VB-Cable isolates recording so warnings
    # about other apps are not actionable.
    _guard = SessionGuard(on_contamination=lambda offenders: None)

    # Audio quality check
    try:
        _tmp = AudioCapture(SAVE_DIR)
        _quality_report = quality_check.check(_tmp._device_info)
        _tmp._pa.terminate()
        print(f"[quality] {_quality_report.info}")
        if _quality_report.warning:
            print(f"[quality] WARNING: {_quality_report.warning}")
    except Exception as e:
        print(f"[quality] Could not run audio quality check: {e}")

    print("[init] Watching for Apple Music playback...")

    _monitor = MusicMonitor()
    _monitor.on_track_changed   = on_track_changed
    _monitor.on_playback_status = on_playback_status
    _monitor.on_seek_detected   = on_seek_detected
    _monitor.start()

    run_tray()


if __name__ == "__main__":
    main()
