# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenHomeKaraoke is a YouTube-based home karaoke system with DNN-based vocal splitting, speech recognition, and multi-user web interface. Originally forked from PiKaraoke, it runs on Windows, Linux, Mac OS, and Raspberry Pi.

## Core Architecture

### Multi-Process Design

Two processes: the main Flask app (`app.py`) and the vocal splitter (`vocal_splitter.py`, background DNN separation). Upstream runs them in separate tmux panes; this checkout's `run.sh` uses `-V` so `app.py` spawns the splitter itself via `multiprocessing`.

### Key Components

- **`app.py`**: Flask web server with WebSocket support (flask-sock)
  - Routes for home, queue, search, info pages
  - WebSocket endpoint at `/ws_init` for real-time client updates
  - Admin authentication system
  - Multi-language support via cookie-based locale detection

- **`karaoke.py`**: Core karaoke engine (Karaoke class)
  - Media playback via VLC or OMXPlayer
  - Queue management and song library
  - Real-time audio controls (transpose, tempo, audio/subtitle delay)
  - PyGame-based splash screen with QR code
  - Background vocal splitting coordination

- **`vocal_splitter.py`**: DNN-based vocal/instrumental separation
  - PyTorch model (`models/baseline.pth`, nets in `lib/nets.py`)
  - Runs as a separate process that polls the Flask app over HTTP (see Vocal Splitting Workflow)
  - Cloud splitting is done by `karaoke.py` instead (`_cloud_thread`), when `app.py --cloud` is set
  - Writes instrumental/vocal outputs into the `nonvocal/` and `vocal/` subdirectories

- **`lib/vlcclient.py`**: VLC remote control via HTTP API
  - Transpose (pitch shift) and tempo controls
  - Subtitle and audio delay adjustments
  - CDG karaoke file support

- **`lib/omxclient.py`**: OMXPlayer control for Raspberry Pi (legacy)

- **`lib/NLP.py`**: Speech recognition, transliteration utilities, and song library helpers (shared by `app.py` and `karaoke.py`)
- **`lib/settings.py`**: Search/sort settings — `MAX_WALK_LEVEL = 0` means song scanning is flat (top-level only, no subdirectory recursion), `FUZZY_PINYIN` and `FUZZY_GREEK` define phonetic equivalences for search

### WebSocket Communication

Real-time updates use WebSockets to minimize bandwidth:
- Status thread (`status_thread()` in app.py) pushes updates to connected clients
- Clients tracked by IP in `ip2websock` dict
- Updates sent only when state changes (`K.status_dirty` flag)
- Different update strategies per page (home vs queue)

### Song Library Management

- Songs stored in `download_path` (default: `$HOME/pikaraoke-songs`)
- Supports: mp4, mp3, zip (mp3+cdg), mkv, avi, webm, mov, m4a
- Background downloading via yt-dlp with browser cookie support
- Vocal/instrumental variants stored in `vocal/` and `nonvocal/` subdirectories
- Song metadata cached in `.mp3_volume.json.gz` for volume normalization
- Filenames with non-Latin characters sorted by transliteration (via unidecode)

### Multi-Language Support

- Language files in `lang/` directory (100+ languages)
- Base languages: `en_US` and `zh_CN` (manually maintained)
- Other languages auto-generated via Google Translate (see `translate.py` and `translate-all.sh`)
- Client language detected from cookies or Accept-Language header
- Update translations by editing base files and running `./translate-all.sh -c`

## Development Commands

### Setup

```bash
uv venv
uv pip install -r requirements.txt
```

### Running the Application

**Linux/Mac OS (full stack, single process):**
```bash
./run.sh
```
Runs `app.py -p 5001 -V`; `-V` starts the vocal splitter as a child process (no tmux). Without `-V`, the app assumes the upstream tmux layout (`vocal_restart()` sends keys to tmux pane `PiKaraoke:0.4`).

**Run only Flask app (no tmux, for development):**
```bash
uv run python3 app.py -d songs/ --windowed
```

**Windows:**
```bash
python3 app.py -d <song-folder>
```

**Raspberry Pi (must run as sudo for pygame screen buffer):**
```bash
sudo env PATH=<anaconda3-bin>:$PATH python3 app.py
```

### Testing Individual Components

There is no automated test suite. Verify changes by running the app.

**Run vocal splitter standalone** (needs `app.py` running so it can fetch its todo list; `-p` is `--postprocess`, `-g -1` forces CPU):
```bash
python3 vocal_splitter.py -p -d ~/pikaraoke-songs/
```
There is no single-file mode; it only processes songs from the library.

### Key CLI Flags

| Flag | Description |
|------|-------------|
| `-p` / `--port` | Web server port (default: 5000) |
| `-d` / `--download-path` | Song library directory |
| `-w` / `--windowed` | Non-fullscreen mode |
| `--hide-splash-screen` | Run without the pygame splash screen |
| `--admin-password` | Enable admin-only features in web UI |
| `--ssl` | Enable HTTPS (required for browser microphone access) |
| `-C` / `--cloud <url>` | Cloud server URL for GPU vocal splitting and Whisper ASR |
| `-V` / `--run-vocal` | Explicitly launch vocal splitter subprocess from main app |
| `--vlc-port` | VLC HTTP API port (default: 5002) |
| `-l` / `--log-level` | Logging level (DEBUG=10, INFO=20, etc.) |
| `-c` / `--browser-cookies` | Browser cookie source for yt-dlp (auto/none/path) |

### Updating Translations

```bash
./translate-all.sh -c   # Regenerate all from base (en_US, zh_CN)
./translate-all.sh      # Add new languages only
```

### Dependencies

**Key external dependencies (must be in PATH):**
- VLC (video playback)
- ffmpeg (audio/video processing)
- yt-dlp (auto-updated on startup)

## Important Technical Details

### Player State Management

- VLC controlled via HTTP API on port 5002 (configurable via --vlc-port)
- Player state polled regularly in background threads
- State changes trigger WebSocket updates to all connected clients
- Audio/subtitle delays can be saved per-song in JSON files

### Vocal Splitting Workflow

1. Song downloaded to main directory (splitting is enabled only if `vocal/` and `nonvocal/` subdirs exist)
2. `vocal_splitter.py` loops on `GET http://localhost:<port>/get_vocal_todo_list/<device>/<last_completed>` (handled in `app.py`), which returns `download_path`, the now-playing + queued files (processed first), and `use_DNN`
3. The port in `vocal_splitter.py` is **hardcoded** and must match `app.py`'s `-p` port; change both together
4. Creates two versions: instrumental (`nonvocal/`) and vocals-only (`vocal/`)
5. User can toggle between mixed/instrumental/vocal during playback
6. On Windows, or with `-V`, `karaoke.py` launches the splitter as a subprocess itself

### Cloud Integration

Optional cloud server for GPU-accelerated tasks:
- Vocal splitting (via --cloud option)
- Speech recognition (Whisper ASR)
- Cloud server code referenced from OpenSmartLight project

### Platform-Specific Notes

- **Mac OS**: Uses VLC with special flags for native fullscreen
- **Windows**: Vocal splitter runs explicitly (not via tmux)
- **Raspberry Pi**: Can use OMXPlayer instead of VLC for better performance on older devices
- **Linux**: Uses tmux with PulseAudio source routing for recording

## File Naming Conventions

- Downloaded files include YouTube ID: `SongName---YOUTUBE_ID.mp4`
- YouTube ID automatically stripped in UI display
- Rename operations update internal `rename_history` dict
- Filenames sorted by Latin transliteration for international support

## Testing Notes

- `--windowed` for non-fullscreen testing; `--hide-splash-screen` to skip the splash screen
- Admin features require `--admin-password` flag
- Log levels controlled via `-l` or `--log-level` (DEBUG=10, INFO=20, etc.)
