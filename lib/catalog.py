"""Song catalog: the singers and title of every song file, plus its original YouTube title, kept in
<download_path>/.library.json and keyed by YouTube id (by file name for songs without one).
Karaoke.get_available_songs() calls sync() on every library scan, so downloads, renames, deletes and
files copied in by hand all end up here. Edit the file only while the app is stopped: the app rewrites it."""
import os, re, json, time, logging, threading

ID_RE = re.compile(r'---([\w-]{11})\.\w+$')	# '<name>---<YouTube id>.<ext>'; ids are 11 chars and may start with '-'
SINGER_SEP = re.compile(r'\s*(?:&|、|，|,|/)\s*')	# '那英 & 王菲'; not '-' or '.' ('A-Lin', 'S.H.E')


def song_key(basename):
	m = ID_RE.search(basename)
	return m.group(1) if m else basename


def parse_name(basename):
	"""(singers, title) from a '歌手 - 歌名---id.ext' file name; ([], name) if it has no ' - '."""
	stem = ID_RE.sub('', basename) if ID_RE.search(basename) else os.path.splitext(basename)[0]
	artist, sep, title = stem.partition(' - ')
	return ([s for s in SINGER_SEP.split(artist) if s], title) if sep else ([], stem)


class Catalog:
	def __init__(self, path):
		self.path = path
		self.lock = threading.Lock()
		self.yt_titles = {}	# song key -> YouTube title of a just-downloaded song, stored by the next sync()
		try:
			with open(path) as fp:
				self.songs = json.load(fp)
		except FileNotFoundError:
			self.songs = {}
		except Exception as e:	# don't overwrite a file we couldn't read
			raise RuntimeError(f'Cannot read song catalog {path}: {e}')

	def sync(self, paths):
		"""Make the catalog match the song files at paths: add new files, follow renames, drop deleted ones."""
		with self.lock:
			changed, keys = False, set()
			for path in paths:
				bn = os.path.basename(path)
				key = song_key(bn)
				keys.add(key)
				e = self.songs.get(key)
				if e is None or e['file'] != bn:	# new, or renamed: the name is where singers/title come from
					singers, title = parse_name(bn)
					added = e['added'] if e else time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(path)))
					self.songs[key] = e = {**(e or {}), 'file': bn, 'singers': singers, 'title': title, 'added': added}
					changed = True
				if key in self.yt_titles:
					e['yt_title'] = self.yt_titles.pop(key)
					changed = True
			gone = set(self.songs) - keys
			if gone and not paths:	# an empty scan (e.g. the song folder unmounted) must not wipe the catalog
				logging.warning('Library scan found no songs; keeping the song catalog as it is')
			else:
				for key in gone:
					del self.songs[key]
				changed |= bool(gone)
			if changed:
				self.save()

	def save(self):
		tmp = self.path + '.tmp'
		with open(tmp, 'w') as fp:
			json.dump(self.songs, fp, ensure_ascii=False, indent=1)
		os.replace(tmp, self.path)	# atomic: a crash mid-write never leaves a half-written catalog
