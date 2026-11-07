SONGS := ~/pikaraoke-songs

.PHONY: sing

# App (mpv, QR code shown over videos) in the foreground, vocal splitter in the background; Ctrl+C stops both.
sing:
	@uv run python3 vocal_splitter.py -p -d $(SONGS) & SPLITTER=$$!; \
	trap 'kill $$SPLITTER 2>/dev/null' EXIT INT TERM; \
	./run.sh --player mpv --show-overlay
