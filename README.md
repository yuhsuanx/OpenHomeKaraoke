# OpenHomeKaraoke

A Mac-first home KTV for Mandarin songs: play karaoke videos from YouTube, remove vocals with a neural network, show KTV-style synced lyrics, and let everyone queue songs from their phone.

## Requirements & install

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- ffmpeg / ffprobe
- mpv (recommended) or VLC

```bash
brew install mpv ffmpeg
brew install --cask vlc          # only if you use the VLC player

git clone https://github.com/yuhsuanx/OpenHomeKaraoke.git
cd OpenHomeKaraoke
make install                     # uv venv + uv pip install -r requirements.txt

# Song folder. The vocal/ and nonvocal/ subfolders turn vocal splitting on.
mkdir -p ~/pikaraoke-songs/{vocal,nonvocal}
```

The vocal-splitter model (`models/baseline.pth`) is included in the repo. Then start it with `make sing` (see [Running](#running)).

## About this fork

This project started as [PiKaraoke](https://github.com/vicwomg/pikaraoke) by Vic Wong. Wang Xuancong turned it into [OpenHomeKaraoke](https://github.com/xuancong84/OpenHomeKaraoke), adding web sockets, speech recognition and tsurumeso's [DNN vocal remover](https://github.com/tsurumeso/vocal-remover). This fork (yuhsuanx, from November 2026) focuses on one setup: a Mac on a TV, Mandarin songs, phones as remotes. The full upstream git history is kept. Many thanks to both upstream authors.

## What's different from upstream

- KTV-style lyrics: synced lyrics from LRCLIB, timed per character with Whisper, shown as a two-row colour wipe.
- Optional mpv player that switches original / instrumental / vocal tracks and lyrics live, in one window for the whole session.
- Vocal splitting on the Apple GPU (MPS), with loudness normalized across all three tracks.
- New web UI (Bootstrap 5, dark theme, phone-first).
- Downloads named "Singer - Title" in Traditional Chinese, and a song catalog file.
- UI languages cut down to `zh_TW` (default) and `en_US`.
- Tested on macOS only. Windows and Raspberry Pi are not supported.

## Features

### Playback

- **VLC** (default): plays the generated `.ass` lyrics and crops black bars. Switching original / instrumental / vocal restarts VLC at the current position.
- **mpv** (`--player mpv`): one window for the whole session. Original, instrumental and vocal tracks are loaded together, so switching is instant. Live control of lyrics on/off, audio and subtitle delay, speed, transpose (needs mpv built with rubberband) and fullscreen. Volume goes up to 200%. Uses VideoToolbox hardware decoding (macOS).
- mpv keys: `F` or double-click toggles fullscreen; `ESC` or closing the window quits.
- Splash screen between songs: dark theme, QR code card on the left, up next / now playing / preparing on the right. In mpv mode it is drawn inside the mpv window.
- `--show-overlay` (mpv): the join QR code stays in the bottom-left corner over videos.
- When someone queues a song, a card pops up in the bottom-right corner (mpv).
- Before a song plays, the app waits for its vocal split and lyrics (up to 300 s). Black bars are detected in the background.

### Vocal splitting

- `vocal_splitter.py` separates every song into instrumental (`nonvocal/`) and vocals-only (`vocal/`) tracks. Queued songs go first, then the rest of the library.
- Uses CUDA if present, otherwise the Apple GPU (MPS): about 13 s per song, compared with about 2 min on CPU. `-g -1` forces CPU.
- Original, vocal and instrumental tracks are normalized to -12 LUFS, so switching tracks doesn't change the volume.
- Runs as its own process. The app detects it and waits for it. `--only queue` or `--only library` limits what it splits, and each process uses its own temp files, so two splitters can share the work.

### Lyrics

- Fetches synced lyrics from [LRCLIB](https://lrclib.net). For duets it also tries each singer separately. Uploads with English translation lines mixed in are skipped.
- Converts lyrics to Traditional Chinese (OpenCC `s2tw`) and removes credit lines and headers.
- Times each character against the separated vocal track with Whisper (stable-ts, `small` model on CPU, downloaded the first time it's used).
- Writes a two-row KTV `.ass` file with a colour wipe. Stray lines are re-timed, and the screen clears during long breaks.
- On macOS, reads lyrics burned into the video with Apple Vision (ocrmac) to find when each line appears. On other systems it uses Whisper alone.
- Queued songs are processed by a background worker. If no lyrics are found, the song isn't retried and the splash screen says so.

### Web UI

- Bootstrap 5.3 with Bootstrap Icons, dark theme. Tab bar at the bottom on phones, at the top on desktop.
- Now-playing card: singer and title, 0–200% volume slider, and buttons for pause, 切歌 (skip) and 導唱 (guide vocal).
- Search has two tabs, 本機歌曲 (local library) and YouTube. YouTube results without a duration (channels, playlists, live streams) are hidden.
- Before downloading, a dialog asks you to confirm the singer(s) and title. Suggestion chips come from the YouTube title and channel.
- Queue: long-press a row to drag it to a new position.
- Library: quick filter, A–Z chips, pagination.

### Downloads & library

- Downloaded files are named `Singer - Title` in Traditional Chinese. Brackets like 〈〉《》【】 and noise words like KTV, 伴奏, MV and Official are cleaned up.
- yt-dlp prefers 1080p H.264 (no 4K/AV1) and updates itself at startup.
- Song catalog in `<songs>/.library.json` (singers, title and original YouTube title for each song). It is synced on every scan, and duets are split into separate singers.
- Lyrics files follow renames and deletes. Deleting a song also removes its crop and volume cache entries.

Upstream features that are still in the code but untested in this fork: cloud vocal splitting and speech recognition / voice search (`-C/--cloud`), admin password, `--ssl`, saved per-song delays, CDG/zip files.

## Platform support

| Platform | Status |
|---|---|
| macOS, Apple Silicon | Tested |
| Linux | Untested. The VLC path may work, without OCR or MPS. The mpv options used are macOS-specific. |
| Windows, Raspberry Pi | Not supported in this fork. Use [upstream](https://github.com/xuancong84/OpenHomeKaraoke). |

## Running

### All in one

```bash
make sing
```

This starts the vocal splitter in the background, then runs `./run.sh --player mpv --show-overlay`. Ctrl+C stops both.

The song folder is set in two places: the `-d` argument in `run.sh` and the `SONGS` variable in the `Makefile`. To use a different folder, change both.

### By hand

```bash
./run.sh                                                    # app (VLC by default)
./run.sh --player mpv --show-overlay                        # app with mpv
uv run python3 vocal_splitter.py -p -d ~/pikaraoke-songs    # splitter, in another terminal
```

`run.sh` runs:

```bash
uv run python3 app.py -d ~/pikaraoke-songs -p 5001 --vlc-port 5003 --windowed "$@"
```

Then open the URL printed at startup, or scan the QR code on the TV, from a phone on the same network.

### Ports

- **5001**: web UI. Port 5000 is taken by the macOS AirPlay Receiver.
- **5002**: HTTPS on port + 1. The app serves this so phone browsers can use the microphone.
- **5003**: VLC remote-control API, moved out of 5002's way.

The splitter's port (5001) is hardcoded in `vocal_splitter.py`. If you change `-p`, change it there too.

Don't run `app.py -V` (`--run-vocal`) while a separate splitter is running. `-V` starts a second splitter.

## Command-line options

Main options for `app.py`. Run `uv run python3 app.py --help` for the full list.

| Option | Description |
|---|---|
| `-d`, `--download-path` | Song folder |
| `-p`, `--port` | Web UI port (default 5000; `run.sh` uses 5001) |
| `--player {vlc,mpv}` | Video player (default `vlc`) |
| `--mpv-path` | mpv binary (default: `mpv` on PATH, otherwise `/opt/homebrew/bin/mpv`) |
| `--show-overlay` | Show the join QR code over videos |
| `-w`, `--windowed` | Start in a window instead of fullscreen |
| `-L`, `--lang` | UI language: `zh_TW` (default) or `en_US` |
| `--vlc-port` | VLC HTTP API port. Must not equal port + 1. |
| `-c`, `--browser-cookies` | Browser cookies for yt-dlp: `auto` (default), `none`, or a path |
| `--admin-password` | Lock queue editing, player controls and other features behind a password |
| `--ssl` | Serve HTTPS instead of HTTP |
| `-V`, `--run-vocal` | Start the vocal splitter as a child process |
| `-l`, `--log-level` | Log level (10 DEBUG, 20 INFO, 30 WARNING, ...) |

Splitter options for `vocal_splitter.py`: `-d` (song folder), `-p` (post-process), `-g -1` (force CPU), `--only {queue,library}`.

## Song folder layout

```
~/pikaraoke-songs/
├── Singer - Title---<YouTubeID>.mp4   # downloaded songs
├── vocal/                             # vocals-only tracks (create this folder)
├── nonvocal/                          # instrumental tracks (create this folder)
├── lyrics/<file>.ass                  # generated KTV lyrics
├── .library.json                      # song catalog: singers, title, YouTube title
├── .crop.json                         # black-bar crop cache
└── .mp3_volume.json.gz                # volume cache
```

The `---<YouTubeID>` suffix is kept in file names and hidden in the UI.

## Troubleshooting

- **Port 5000 already in use**: on macOS this is AirPlay Receiver. Use `./run.sh` (port 5001), or turn off AirPlay Receiver in System Settings.
- **Instrumental / vocal options greyed out**: create `vocal/` and `nonvocal/` in the song folder, then make sure the splitter is running.
- **"No lyrics found"**: LRCLIB has no synced lyrics for that singer and title, or the name doesn't match. Songs with no lyrics aren't retried automatically.
- **YouTube download fails**: yt-dlp updates itself at startup, so restart the app first. If YouTube asks you to sign in, pass cookies with `-c` (for example a browser cookie path), or try `-c none`.

## Credits

- [PiKaraoke](https://github.com/vicwomg/pikaraoke) by Vic Wong
- [OpenHomeKaraoke](https://github.com/xuancong84/OpenHomeKaraoke) by Wang Xuancong
- [vocal-remover](https://github.com/tsurumeso/vocal-remover) by tsurumeso
- [LRCLIB](https://lrclib.net) for synced lyrics
- [stable-ts](https://github.com/jianfch/stable-ts) and [OpenAI Whisper](https://github.com/openai/whisper)
- [mpv](https://mpv.io), [VLC](https://www.videolan.org), [FFmpeg](https://ffmpeg.org), [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Bootstrap](https://getbootstrap.com) and Bootstrap Icons, [SortableJS](https://github.com/SortableJS/Sortable)
- [OpenCC](https://github.com/BYVoid/OpenCC), [ocrmac](https://github.com/straussmaximilian/ocrmac), [PyTorch](https://pytorch.org)

## License

GPL-3.0. See [LICENSE](LICENSE).

This is a modified version of OpenHomeKaraoke by Wang Xuancong, which is itself derived from PiKaraoke by Vic Wong. Modified by yuhsuanx starting November 2026; see the git history for details.
