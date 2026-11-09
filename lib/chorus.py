"""Find a song's chorus (副歌) from its timed lyrics (.ass, see lib/lyrics.py): the block of lines that repeats."""
import functools, os, re

DIALOGUE = re.compile(r'^Dialogue: [^,]*,(\d+):(\d+):([\d.]+),[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$')

def sung_lines(ass_path):
	"""[(start sec, text)] of the lyric lines, start = when singing begins (the line is shown a bit earlier)."""
	out = []
	with open(ass_path, encoding='utf-8') as fp:
		for ln in fp:
			m = DIALOGUE.match(ln.rstrip('\n'))
			if not m:
				continue
			h, mi, s, body = m.groups()
			t = int(h) * 3600 + int(mi) * 60 + float(s)
			lead = re.match(r'(?:\{\\(?!k)[^}]*\})*\{\\k(\d+)\}', body)	# '{\k300}': silent lead-in before the first word
			text = re.sub(r'\{[^}]*\}', '', body)
			key = re.sub(r'[\W_]+', '', text).lower()
			if key:
				out.append((t + (int(lead.group(1)) / 100 if lead else 0), key))
	return out

def chorus_times(ass_path, title='', min_lines=2):
	"""Start times (sec) of each chorus, earliest first: the repeated block of lines covering the most of the
	song (length x times it repeats), or [] if no block of min_lines lines repeats. title: the song's name."""
	lines = sung_lines(ass_path)
	keys = [k for _, k in lines]
	n = len(keys)
	run = [[0] * (n + 1) for _ in range(n + 1)]	# run[i][j]: lines i.. and j.. match for this many lines
	for i in range(n - 1, -1, -1):
		for j in range(n - 1, i, -1):
			if keys[i] == keys[j]:
				run[i][j] = run[i + 1][j + 1] + 1
	def occurrences(i, L):	# non-overlapping starts of lines i..i+L-1
		starts = []
		for j in range(n - L + 1):
			if (j == i or run[min(i, j)][max(i, j)] >= L) and (not starts or j >= starts[-1] + L):
				starts.append(j)
		return starts
	best, best_i, best_L = 0, None, 0
	for i in range(n):
		for L in range(min_lines, max([run[i][j] for j in range(i + 1, n)], default=0) + 1):
			c = len(occurrences(i, L))
			if c >= 2 and L * c > best:
				best, best_i, best_L = L * c, i, L
	if best_i is None:
		return []
	starts = occurrences(best_i, best_L)
	# pre-chorus + chorus often repeat together, and the chorus once more alone at the end: start at the
	# first line of the block from which a part of it repeats more often than the whole block
	refined = next((more for d in range(1, best_L - min_lines + 1) for l in range(best_L - d, min_lines - 1, -1)
	                if len(more := occurrences(best_i + d, l)) > len(starts)), None)
	if refined:
		starts = refined
	elif len(title := re.sub(r'[\W_]+', '', title).lower()) >= 2:	# else the chorus is likely where the title is sung
		d = next((d for d in range(best_L) if title in keys[best_i + d]), 0)
		starts = [s + d for s in starts]
	times = []
	for s in starts:
		if not times or lines[s][0] - times[-1] > 5:	# badly timed lyrics can stack lines at one time
			times.append(lines[s][0])
	return times

@functools.lru_cache(maxsize=64)
def _cached(path, mtime):
	name = os.path.basename(path).rpartition('---')[0]	# '<artist> - <title>---<id>.<ext>.ass'
	return chorus_times(path, name.rpartition(' - ')[2])

def find_chorus(ass_path):
	"""chorus_times() of a lyrics file, cached; [] if there is no such file."""
	try:
		return _cached(ass_path, os.path.getmtime(ass_path))
	except (OSError, ValueError):
		return []
