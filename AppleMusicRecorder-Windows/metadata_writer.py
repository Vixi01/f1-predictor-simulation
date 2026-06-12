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

from music_monitor import TrackInfo, MusicMonitor


def process(temp_path: str, track: Optional[TrackInfo], save_directory: str,
            on_saved: Optional[Callable[[str], None]] = None):
    t = threading.Thread(
        target=_embed_and_rename,
        args=(temp_path, track, save_directory, on_saved),
        daemon=True,
        name="MetadataWriter",
    )
    t.start()


def retag_untagged_file(untagged_path: Path, save_dir: Path, db=None) -> Optional[Path]:
    """Re-tag an *_untagged.flac file by parsing artist/album/title from its name.
    Fetches missing artwork from iTunes. Returns the renamed path on success, None on failure."""
    stem = untagged_path.stem
    if stem.endswith("_untagged"):
        stem = stem[: -len("_untagged")]

    parts = stem.rsplit(" - ", 2)
    if len(parts) != 3:
        return None
    artist, album, title = (p.strip() for p in parts)

    try:
        audio = FLAC(str(untagged_path))
        audio["title"]  = [title]
        audio["artist"] = [artist]
        audio["album"]  = [album]

        final_path = _replace_path(save_dir / f"{stem}.flac")

        art_dir  = save_dir / "Artwork"
        art_dir.mkdir(parents=True, exist_ok=True)
        art_path = art_dir / f"{stem}.jpg"
        artwork_data: Optional[bytes] = None

        if not art_path.exists():
            artwork_data = MusicMonitor._fetch_artwork(artist, album, title)

        if artwork_data:
            pic = Picture()
            pic.type = PictureType.COVER_FRONT
            pic.mime = "image/jpeg"
            pic.data = artwork_data
            audio.add_picture(pic)
            art_path.write_bytes(artwork_data)

        audio.save()
        os.replace(str(untagged_path), str(final_path))
        return final_path

    except Exception:
        return None


def _embed_and_rename(temp_path: str, track: Optional[TrackInfo],
                      save_directory: str, on_saved: Optional[Callable]):
    if not os.path.exists(temp_path):
        return

    save_dir = Path(save_directory)

    if track:
        final_path = _replace_path(save_dir / f"{track.safe_filename}.flac")
        art_dir    = save_dir / "Artwork"
        art_dir.mkdir(parents=True, exist_ok=True)
        art_path   = art_dir / f"{track.safe_filename}.jpg"
    else:
        name       = _timestamp_name()
        final_path = _replace_path(save_dir / f"{name}.flac")
        art_path   = None

    try:
        audio = FLAC(temp_path)

        if track:
            audio["title"]  = [track.title]
            audio["artist"] = [track.artist]
            audio["album"]  = [track.album]

            if track.artwork_data and art_path:
                pic = Picture()
                pic.type = PictureType.COVER_FRONT
                pic.mime = "image/jpeg"
                pic.data = track.artwork_data
                audio.add_picture(pic)
                art_path.write_bytes(track.artwork_data)

        audio.save()
        os.replace(temp_path, final_path)
        print(f"[saved] {final_path.relative_to(save_dir)}")

        if on_saved:
            try:
                on_saved(str(final_path))
            except Exception:
                pass

    except Exception as e:
        base_name = track.safe_filename if track else _timestamp_name()
        fallback  = save_dir / f"{base_name}_untagged.flac"
        try:
            os.replace(temp_path, str(fallback))
        except Exception:
            pass
        print(f"[warn] metadata failed ({e}), saved as {fallback.name}")
        if on_saved:
            try:
                on_saved(str(fallback))
            except Exception:
                pass


def _replace_path(path: Path) -> Path:
    """Return path as-is, deleting any existing file (and companion .jpg) at that location."""
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass
    jpg = path.with_suffix(".jpg")
    if jpg.exists():
        try:
            jpg.unlink()
        except Exception:
            pass
    return path


def _timestamp_name() -> str:
    from datetime import datetime
    return "Recording at " + datetime.now().strftime("%Y-%m-%d %H.%M.%S")
