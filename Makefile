SONGS := ~/pikaraoke-songs

.PHONY: install sing

# Create the venv (Python from .python-version) and install the Python requirements.
install:
	uv venv --allow-existing
	uv pip install -r requirements.txt

# App (mpv, QR code shown over videos) in the foreground, vocal splitter in the background; Ctrl+C stops both.
sing:
	@uv run python3 vocal_splitter.py -p -d $(SONGS) & SPLITTER=$$!; \
	trap 'kill $$SPLITTER 2>/dev/null' EXIT INT TERM; \
	./run.sh --player mpv --show-overlay
