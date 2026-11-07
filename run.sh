#!/usr/bin/env bash
# Start OpenHomeKaraoke. The vocal splitter runs as its own background process, started separately:
#   uv run python3 vocal_splitter.py -p -d ~/pikaraoke-songs
# Port must match the one hardcoded in vocal_splitter.py (5001).

cd "`dirname $0`"
exec uv run python3 app.py -d ~/pikaraoke-songs -p 5001 --vlc-port 5003 --windowed "$@"
