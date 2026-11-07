# 0001: Evaluate mpv as the video player instead of VLC

**Status:** open, research before committing.
**Opened:** 2026-11-06

## Problem
Switching 原唱 / 去人聲 / 人聲 during a song closes and reopens the VLC window. It works, but the flash is jarring.

## What we found (VLC 3.0.24, macOS)
- **Instant switch is silent:** live `audio_track` switching to an `--input-slave` track (`nonvocal/`, `vocal/` m4a) gives no sound. The user confirmed this through the speakers on 1K - 就忘了吧. Switching back to the video's own track works.
- **Starting on a slave works:** starting VLC directly on a slave track (`--audio-track=1/2`) gives correct audio. So the current fix (commit pending, `karaoke.py: play_vocal`) restarts VLC at the current position on every switch.
- **One file doesn't help:** the video plus all 3 audio tracks remuxed into one mp4 (`ffmpeg -map 0:v -map 0:a -map 1:a -map 2:a -c copy`, 0.4 s) switches with sound in the VLC GUI. But the window still closes and reopens on each switch.
- **Tracks are in sync:** the vocal and nonvocal tracks are aligned with the original audio within 8 ms (checked on 3 songs).
- **Other VLC quirks hit so far:**
  - `--crop=WxH+X+Y` is misread (H treated as the bottom edge), so we use the `L+T+R+B` form instead.
  - `afile` audio output restarts its file on a track switch, so it can't be used to test switching.

## Why mpv
- **Seamless switching:** reportedly switches audio tracks, including external files via `--audio-file`, without touching video. Not yet verified on this Mac.
- **Lyrics:** renders ASS `\kf` karaoke tags with libass, the same as VLC.
- **Key change:** better pitch shift via the `rubberband` filter.
- **Control:** a JSON IPC socket (`--input-ipc-server`) for pause, seek, volume, `sub-delay`, `audio-delay` and `speed`. It also does precise seeking and has `--video-crop`.

## Cost
- **Replace the player layer:** replace `lib/vlcclient.py` (~380 lines) with an mpv client, and update the VLC-specific parts of `karaoke.py`: `play_file` params, status XML parsing, `has_video` / `has_subtitle`, and the omx/vlc branches.
- **Install:** `brew install mpv` (0.42.0 available, not installed).
- **Other platforms:** upstream targets VLC on Windows and Raspberry Pi too.

## Research checklist
- [ ] Install mpv, play 1K - 就忘了吧 with `--audio-file` nonvocal + vocal, and switch `aid` live: no flash, sound OK?
- [ ] Lyrics: `.ass` wipe renders and stays in sync after switching and seeking.
- [ ] Pitch shift via `af=rubberband=pitch-scale=...` is live, without a restart.
- [ ] Windowed / fullscreen behaviour on macOS, and whether other apps can come to the front.
- [ ] Crop via `--video-crop` with the output of `detect_black_bars()`.
- [ ] Decide: replace VLC everywhere, or mpv on macOS only.
