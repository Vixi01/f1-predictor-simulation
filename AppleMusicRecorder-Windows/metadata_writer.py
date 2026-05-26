"""
metadata_writer.py
Embeds title / artist / album / cover art into a FLAC file, renames it
to "Artist - Album - Title.flac", and saves a companion .jpg.
Accepts an optional on_saved(final_path) callback for post-save hooks.
Runs in a background thread so it never blocks the recording pipeline.
"""
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

from music_monitor import TrackInfo


def process(temp_path: str, track: Optional[TrackInfo], save_directory: str,
            on_saved: Optional[Callable[[str], None]] = None):
    t = threading.Thread(
        target=_embed_and_rename,
        args=(temp_path, track, save_directory, on_saved),
        daemon=True,
        name="MetadataWriter",
    )
    t.start()


def _embed_and_rename(temp_path: str, track: Optional[TrackInfo],
                      save_directory: str, on_saved: Optional[Callable]):
    if not os.path.exists(temp_path):
        return

    save_dir   = Path(save_directory)
    base_name  = track.safe_filename if track else _timestamp_name()
    final_path = _unique_path(save_dir / f"{base_name}.flac")
    art_path   = _unique_path(save_dir / f"{base_name}.jpg")

    try:
        audio = FLAC(temp_path)

        if track:
            audio["title"]  = [track.title]
            audio["artist"] = [track.artist]
            audio["album"]  = [track.album]

            if track.artwork_data:
                pic = Picture()
                pic.type = PictureType.COVER_FRONT
                pic.mime = "image/jpeg"
                pic.data = track.artwork_data
                audio.add_picture(pic)
                art_path.write_bytes(track.artwork_data)

        audio.save()
        os.replace(temp_path, final_path)
        print(f"[saved] {final_path.name}")

        if on_saved:
            try:
                on_saved(str(final_path))
            except Exception:
                pass

    except Exception as e:
        fallback = _unique_path(save_dir / f"{base_name}_untagged.flac")
        try:
            os.replace(temp_path, fallback)
        except Exception:
            pass
        print(f"[warn] metadata failed ({e}), saved as {fallback.name}")
        if on_saved:
            try:
                on_saved(str(fallback))
            except Exception:
                pass


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _timestamp_name() -> str:
    from datetime import datetime
    return "Recording at " + datetime.now().strftime("%Y-%m-%d %H.%M.%S")
