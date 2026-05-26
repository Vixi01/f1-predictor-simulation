"""
recorder.py – Apple Music Recorder for Windows
Sits in the system tray. Automatically records Apple Music to lossless FLAC,
splits on track changes, and stops when playback stops.

Usage:  python recorder.py
        (or double-click run.bat)
"""
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import pystray
from PIL import Image, ImageDraw

import metadata_writer
import quality_check
from audio_capture import AudioCapture
from music_monitor import MusicMonitor, TrackInfo


# ── Config ─────────────────────────────────────────────────────────────────────

def get_save_directory() -> str:
    """Default: ~/Music/Apple Music Recordings"""
    return str(Path.home() / "Music" / "Apple Music Recordings")


SAVE_DIR = get_save_directory()

# ── State ───────────────────────────────────────────────────────────────────────

capture:  Optional[AudioCapture] = None
monitor:  Optional[MusicMonitor] = None
recording = False
_lock = threading.Lock()
_quality_report: Optional[quality_check.QualityReport] = None

# ── Audio control ───────────────────────────────────────────────────────────────

def start_recording():
    global capture, recording
    with _lock:
        if recording:
            return
        try:
            capture = AudioCapture(SAVE_DIR)
            capture.start()
            recording = True
            print("[recording] started")
        except Exception as e:
            print(f"[error] Could not start recording: {e}")
            capture = None


def stop_recording():
    global capture, recording, _last_track
    with _lock:
        if not recording or capture is None:
            return
        temp = capture.stop()
        track = _last_track
        recording = False
        print("[recording] stopped")

    if temp:
        metadata_writer.process(temp, track, SAVE_DIR)


def split_recording(new_track: Optional[TrackInfo]):
    """Close current file (process it) and open a new one for the new track."""
    global _last_track
    with _lock:
        if not recording or capture is None:
            return
        finished_path, _ = capture.split()
        old_track = _last_track
        _last_track = new_track

    if finished_path:
        metadata_writer.process(finished_path, old_track, SAVE_DIR)


# ── Music monitor callbacks ─────────────────────────────────────────────────────

_last_track: Optional[TrackInfo] = None


def on_track_changed(track: Optional[TrackInfo]):
    global _last_track

    if track is None:
        # Playback stopped
        stop_recording()
        _last_track = None
        update_tray_title("Apple Music Recorder — idle")
        return

    if not recording:
        # New track started from stopped state
        _last_track = track
        start_recording()
    else:
        # Track changed mid-recording — split the file
        split_recording(track)

    label = f"♪  {track.artist} – {track.title}"
    update_tray_title(label)
    print(f"[track] {label}")


# ── System tray ─────────────────────────────────────────────────────────────────

_tray_icon: Optional[pystray.Icon] = None


def _make_icon_image(recording_active: bool) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (220, 50, 50) if recording_active else (80, 180, 80)
    draw.ellipse([8, 8, size - 8, size - 8], fill=color)
    # Simple music note shape (two filled rectangles)
    draw.rectangle([24, 20, 30, 42], fill="white")
    draw.rectangle([36, 16, 42, 38], fill="white")
    draw.rectangle([24, 20, 42, 26], fill="white")
    return img


def update_tray_title(title: str):
    if _tray_icon:
        _tray_icon.title = title


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


def _on_quit(icon, item):
    stop_recording()
    if monitor:
        monitor.stop()
    icon.stop()


def _build_menu():
    return pystray.Menu(
        pystray.MenuItem("Open recordings folder", _on_open_folder),
        pystray.MenuItem("Audio quality check", _on_show_quality),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _on_quit),
    )


def run_tray():
    global _tray_icon
    icon_img = _make_icon_image(False)
    _tray_icon = pystray.Icon(
        name="AppleMusicRecorder",
        icon=icon_img,
        title="Apple Music Recorder — idle",
        menu=_build_menu(),
    )

    def _on_setup(icon):
        icon.visible = True
        # Show quality warning once after tray is ready
        if _quality_report and _quality_report.warning:
            icon.notify(
                "Lossless may not be active. Right-click -> Audio quality check for details.",
                "Apple Music Recorder — Quality Warning",
            )

    _tray_icon.run(setup=_on_setup)


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    global monitor, capture, _quality_report

    # Ensure save directory exists
    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[init] Saving recordings to: {SAVE_DIR}")

    # Run audio quality check before starting monitor
    try:
        _tmp_capture = AudioCapture(SAVE_DIR)
        _quality_report = quality_check.check(_tmp_capture._device_info)
        _tmp_capture._pa.terminate()
        print(f"[quality] {_quality_report.info}")
        if _quality_report.warning:
            print(f"[quality] WARNING: {_quality_report.warning}")
    except Exception as e:
        print(f"[quality] Could not run audio quality check: {e}")

    print("[init] Watching for Apple Music playback…")

    monitor = MusicMonitor()
    monitor.on_track_changed = on_track_changed
    monitor.start()

    # Tray runs on main thread (required by Windows)
    run_tray()


if __name__ == "__main__":
    main()
