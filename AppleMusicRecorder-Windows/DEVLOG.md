# Apple Music Recorder — Development Log

## Goal
Automatically record Apple Music playback to lossless FLAC files on Windows,
with clean isolation (no bleed from other apps), smart deduplication, and
zero manual intervention during normal use.

---

## Environment

### System
- Windows 11 Pro 10.0.26200
- Python 3.12 (CPython)
- Shell: bash / PowerShell 5.1

### Python Dependencies (requirements.txt)
```
pyaudiowpatch      # WASAPI loopback capture (fork of PyAudio with loopback support)
soundfile          # Read/write FLAC files
numpy              # Audio sample processing
mutagen            # FLAC tag embedding (title, artist, album, artwork)
Pillow             # Tray icon image generation
pystray            # Windows system tray icon
psutil             # Find Apple Music process PID
winsdk             # Windows Runtime bindings (SMTC — System Media Transport Controls)
requests           # iTunes API artwork download
pycaw              # Windows Core Audio (session enumeration for guard)
comtypes           # COM interop (used in process_capture.py attempt)
```

### Audio Setup (CRITICAL)
- **VB-Audio Virtual Cable** must be installed (free, vb-audio.com/Cable)
- In Windows Settings → Sound → Volume Mixer → Apple Music → Output: **"CABLE Input (VB-Audio Virtual Cable)"**
- The recorder captures from **CABLE Output** (direct input device, index varies)
- This isolates Apple Music audio completely from Chrome, games, etc.
- "Listen to this device" on CABLE Output in Recording tab lets you hear music while routing

### Apple Music
- Microsoft Store version (AppleMusic.exe, PID varies)
- Lossless/Hi-Res Lossless enabled in Apple Music settings
- Windows audio set to 24-bit 96kHz for SDAC (headphone DAC)
- VB-Cable defaults to 48kHz — recordings are 48kHz FLAC (still lossless PCM)

---

## File Structure

```
AppleMusicRecorder-Windows/
├── recorder.py           # Main entry point — orchestrator, tray icon, recording logic
├── audio_capture.py      # WASAPI audio capture via pyaudiowpatch
├── music_monitor.py      # SMTC polling — detects Apple Music tracks, seek, pause
├── metadata_writer.py    # Tags FLAC files, fetches artwork, renames files
├── recording_db.py       # JSON database — tracks verified/incomplete recordings
├── session_guard.py      # Detects other apps producing audio (warning only)
├── process_capture.py    # Attempted per-process loopback (currently non-functional)
├── quality_check.py      # Checks audio device sample rate on startup
├── DEVLOG.md             # This file
├── run.bat               # Double-click launcher
└── requirements.txt
```

### Recordings saved to:
`C:\Users\<user>\Music\Apple Music Recordings\`

Files:
- `Artist — Album -  - Title.flac` — tagged FLAC
- `Artist — Album -  - Title.jpg` — companion artwork
- `.recordings.json` — recording database (hidden)
- `Apple Music Recordings.m3u` — exported playlist (on demand)

---

## Architecture

### Recording Flow
```
Apple Music plays
    → SMTC (music_monitor.py polls every 0.3s)
        → on_track_changed() fires in recorder.py
            → check DB: skip if verified, re-record if incomplete
            → check duration: skip if < 30s
            → _start_recording()
                → AudioCapture captures from CABLE Output (VB-Cable)
                → writes .flac temp file
    → on_playback_status("paused") → _discard_recording()
    → on_seek_detected() → _discard_recording()
    → track ends / new track starts
        → _stop_recording() or _split_recording()
        → _finalize(): verify duration ± 3s
        → metadata_writer.process(): tag + rename + artwork
        → DB: mark_verified()
```

### Key Design Decisions

**VB-Cable for isolation**: Windows per-app volume mixer routes Apple Music
to CABLE Input. Recorder captures from CABLE Output (direct input device).
This gives complete isolation — Chrome, games, etc. never appear in recordings.

**Why not per-process WASAPI loopback**: `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`
via `ActivateAudioInterfaceAsync` was attempted extensively (process_capture.py).
Always fails with `E_ILLEGAL_METHOD_CALL (0x8000000E)` despite MTA COM thread
initialization. Suspected cause: COM apartment conflict with pystray/pyaudio
initializing COM before our MTA thread. VB-Cable is a better solution anyway.

**SMTC filtering**: `music_monitor.py` iterates all SMTC sessions and picks
only one whose `source_app_user_model_id` contains "applemusic", "itunes",
or "music". Prevents Chrome YouTube from being recorded as a track.

**Discard on pause/seek**: Any interruption discards the temp file immediately.
The recording DB marks the track INCOMPLETE so it gets re-recorded next play.

**Duration verification**: After recording, compares FLAC duration vs SMTC
expected duration (± 3s tolerance). SMTC often returns 0 for Apple Music
tracks, so `ALLOW_UNVERIFIED_DURATION = True` accepts those anyway.

---

## Development History

### Phase 1 — Basic Recorder
- WASAPI system loopback via pyaudiowpatch
- SMTC polling for track info
- Simple FLAC output with mutagen tags
- No deduplication, no pause detection

### Phase 2 — Quality Check
- `quality_check.py` added
- Checks WASAPI device sample rate on startup
- Reports lossless status (96kHz = lossless active)
- User set Windows audio to 24-bit/96kHz for their SDAC

### Phase 3 — Smart Recording
- Recording DB (JSON) for deduplication
- Pause detection → discard recording
- Seek detection → discard recording (false positive bug fixed: compare
  consecutive polls not position vs wall time; reset on new track)
- Duration verification (±3s)
- `_IGNORE_TITLES` to skip "Connecting…" Apple Music loading states
- Split recording (atomic file swap on track change without stopping stream)

### Phase 4 — Per-Process Isolation Attempt (FAILED)
Multiple iterations of `process_capture.py`:
1. Raw ctypes vtable → access violation at 0x1ED (wrong vtable index for GetService)
2. Raw vtable with correct indices → E_INVALIDARG from ActivateAudioInterfaceAsync
3. comtypes.COMObject handler → still E_INVALIDARG (PROPVARIANT layout wrong)
4. Fixed PROPVARIANT (ctypes Structure, correct 24-byte x64 layout) → "argument 1: TypeError: wrong type"
5. Added argtypes on ActivateAudioInterfaceAsync, QueryInterface for handler pointer → E_ILLEGAL_METHOD_CALL (0x8000000E)

Root cause of E_ILLEGAL_METHOD_CALL: Unknown. Likely COM apartment issue.
`process_capture.py` is kept but disabled — falls back to system loopback.

### Phase 5 — Session Contamination Guard
- `session_guard.py` using pycaw
- Detects other apps' audio during recording
- Excluded: python.exe, pythonw.exe, amplibraryagent.exe, apple music
- Later silenced entirely (not meaningful with VB-Cable isolation)

### Phase 6 — VB-Cable Isolation
- Attempted CABLE Input [Loopback] → silent recordings (Apple Music per-app
  routing bypasses the system-level loopback)
- Fixed: capture from CABLE Output as a direct input device (maxInputChannels=2)
- This is the correct and working approach

### Phase 7 — Polish & Features
- Single-instance mutex (Windows named mutex, ERROR_ALREADY_EXISTS = 183)
- SMTC Apple Music filter (source_app_user_model_id)
- Guard warnings silenced (not actionable with VB-Cable)
- Re-tag untagged files on startup
- Duplicate replace (overwrite instead of Song (2).flac)
- Tray icon: red when recording, green when idle
- Silence detection: warns if VB-Cable routing breaks (10s silence threshold)
- Skip short tracks: < 30s skipped (interludes/skits)
- Export playlist: M3U written to recordings folder on demand

---

## Known Issues / Non-Functional Code

### process_capture.py
- Per-process WASAPI loopback never worked
- `E_ILLEGAL_METHOD_CALL` from `ActivateAudioInterfaceAsync`
- Code is kept for future investigation but always falls back to VB-Cable
- If ever fixed, would eliminate the need for VB-Cable

### VB-Cable routing can reset
- Windows sometimes resets per-app audio routing after restart
- Silence detection (10s threshold) will warn the user via tray notification
- User must re-set Apple Music → CABLE Input in Windows Volume Mixer

### SMTC duration often 0
- Apple Music SMTC session frequently reports duration_sec = 0
- Duration verification is skipped for these (ALLOW_UNVERIFIED_DURATION = True)
- All recordings saved as-is; no exact duration match possible

### Artwork sometimes missing
- iTunes API lookup fails on some tracks (network, missing results)
- File saved without artwork as `*_untagged.flac`
- Startup retry (_retry_untagged) attempts re-tag on next launch

---

## Running the App

### Normal launch
```bat
run.bat
```
or
```
python recorder.py
```

### Monitoring (development)
```powershell
python -u recorder.py > amr_log.txt 2> amr_err.txt
```

### Kill all instances
```powershell
Stop-Process -Name "python","pythonw" -Force -ErrorAction SilentlyContinue
```

### Check recordings for silence (diagnostic)
```python
import soundfile as sf, numpy as np, os
folder = r'C:\Users\...\Music\Apple Music Recordings'
for f in os.listdir(folder):
    if f.endswith('.flac'):
        data, sr = sf.read(os.path.join(folder, f), frames=1000)
        print(f'{np.abs(data).max():.4f}  {f[:60]}')
```

### Reset a song in the DB (force re-record)
```python
import json
db_path = r'C:\Users\...\Music\Apple Music Recordings\.recordings.json'
db = json.load(open(db_path, encoding='utf-8'))
key = 'Artist|Album|Title'
del db[key]
json.dump(db, open(db_path,'w', encoding='utf-8'), ensure_ascii=False, indent=2)
```

---

## Git History (summary)
```
9231a2f  feat: re-tag untagged, duplicate replace, tray icon, silence detection, skip short, export playlist
07f2fb3  feat: VB-Cable isolation + single-instance lock + Apple Music SMTC filter
771d013  feat: comtypes completion handler + session contamination guard
(earlier) smart recording, DB, seek detection, quality check, basic recorder
```
