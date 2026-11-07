#!/usr/bin/env bash
# Start OpenHomeKaraoke in one process; -V runs the vocal splitter as a child process.
# Port must match the one hardcoded in vocal_splitter.py (5001).

cd "`dirname $0`"
exec uv run python3 app.py -d ~/pikaraoke-songs -p 5001 --vlc-port 5003 -V --windowed "$@"
