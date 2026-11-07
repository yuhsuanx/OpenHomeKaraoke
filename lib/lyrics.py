"""KTV-style lyrics: fetch from LRCLIB, time every character against the song's own audio with Whisper (stable-ts),
and write an .ass subtitle (two rows, next line shown ahead, per-character colour wipe) that VLC plays over the video."""
import os, re, json, logging, statistics, subprocess, urllib.parse, urllib.request
from types import SimpleNamespace
from opencc import OpenCC

S2T = OpenCC('s2tw')
STRAY = 4	# secs a line's Whisper start may drift from its LRCLIB start (after the overall offset) before it's re-timed
BREAK = 6	# secs without singing that count as an instrumental break: the screen clears
LEAD = 3	# secs a line shows before it's sung, after a break and at the start
CREDIT = re.compile(r'[:：]|^\s*(作詞|作曲|編曲|製作|監製)')	# credit lines like '作詞：方文山'
HAN = re.compile(r'[㐀-鿿]')
_model = None

def translated(rec):
	"""True if a Chinese song's lyrics carry an English translation line under each line, e.g. one 想見你想見你想見你 upload."""
	ls = [ln for ln in (rec.get('plainLyrics') or rec['syncedLyrics']).splitlines() if ln.strip()]
	en = sum(1 for ln in ls if not HAN.search(ln) and re.search(r'[A-Za-z]{3}', ln))
	return bool(HAN.search(''.join(ls))) and en > len(ls) / 3

def lrclib(artist, track, duration):
	q = {'artist_name': artist, 'track_name': track} if artist else {'q': track}
	res = json.load(urllib.request.urlopen('https://lrclib.net/api/search?' + urllib.parse.urlencode(q), timeout=15))
	res = [r for r in res if r.get('plainLyrics') or r.get('syncedLyrics')]
	res = [r for r in res if not translated(r)] or res
	return min(res, key=lambda r: abs((r.get('duration') or 0) - duration)) if res else None

def clean_line(ln, artist, track):
	"""A lyric line without punctuation, or '' for credit and title header lines."""
	if CREDIT.search(ln):
		return ''
	ln = re.sub(r'\s+', ' ', re.sub(r'[^\w\s\']', ' ', ln)).strip()	# drop punctuation
	return '' if ln.replace(' ', '') in (track, artist + track, track + artist) else ln

def lyric_lines(rec, artist, track):
	raw = re.sub(r'\[[\d:.]+\]', '', rec.get('plainLyrics') or rec['syncedLyrics'])	# some plainLyrics carry [mm:ss] tags too
	return [c for ln in S2T.convert(raw).splitlines() if (c := clean_line(ln, artist, track))]

FONT_SIZE, MARGIN = 112, 80	# lyric font size and left/right margin, in the 1920x1080 script's pixels
HEADER = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Up,PingFang TC,{FONT_SIZE},&H00FF9F1E,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,2,0,1,5,0,7,{MARGIN},{MARGIN},60,1
Style: Down,PingFang TC,{FONT_SIZE},&H00FF9F1E,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,2,0,1,5,0,9,{MARGIN},{MARGIN},{60 + round(FONT_SIZE * 1.35)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''

def fit(text):
	"""'{\\fs..}' that shrinks a line too long for one row (a wrapped line would run into the other row), else ''."""
	plain = re.sub(r'\{[^}]*\}', '', text)
	width = sum(1 if ord(c) > 0x2e80 else 0.55 for c in plain) * (FONT_SIZE + 2)
	room = 1920 - 2 * MARGIN
	return '' if width <= room else '{\\fs%d}' % (FONT_SIZE * room // width)

def lrc_times(rec, lines, artist, track):
	"""LRCLIB's start time for each of `lines`, None where its synced lyrics have no matching line."""
	timed = []
	for ln in S2T.convert(rec.get('syncedLyrics') or '').splitlines():
		text = clean_line(re.sub(r'\[[\d:.]+\]', '', ln), artist, track)
		timed += [(int(m) * 60 + float(s), text) for m, s in re.findall(r'\[(\d+):(\d+(?:\.\d+)?)\]', ln) if text]
	timed.sort()
	times, j = [], 0
	for ln in lines:	# match in order, looking only a few lines ahead so a repeated chorus line can't skip a verse
		k = next((k for k in range(j, min(j + 3, len(timed))) if timed[k][1] == ln), None)
		times.append(None if k is None else timed[k][0])
		j = j if k is None else k + 1
	return times

def words_per_line(lines, words):
	"""Map aligned words back to lyric lines by counting non-space characters."""
	bounds, n = [], 0
	for ln in lines:
		n += len(ln.replace(' ', ''))
		bounds.append(n)
	per_line = [[] for _ in lines]
	pos, li = 0, 0
	for w in words:
		txt = w.word.strip()
		if not txt:
			continue
		while li < len(lines) - 1 and pos >= bounds[li]:
			li += 1
		per_line[li].append(w)
		pos += len(txt.replace(' ', ''))
	return per_line

def fix_strays(lines, per_line, times, audio_path):
	"""Re-time lines Whisper put far from their LRCLIB time (e.g. onto spoken dialogue in the MV): realign each one
	within its LRC slot, or spread it evenly over the slot if that fails. Skipped if LRCLIB's timing doesn't fit this video."""
	diffs = [ws[0].start - t for ws, t in zip(per_line, times) if ws and t is not None]
	if len(diffs) < 5:
		return
	off = statistics.median(diffs)	# LRCLIB may be timed for a version with a shorter/longer intro
	bad = [i for i, (ws, t) in enumerate(zip(per_line, times)) if t is not None and not (ws and abs(ws[0].start - t - off) <= STRAY)]
	if len(bad) > len(diffs) / 3:
		logging.warning(f'Lyrics: {len(bad)}/{len(lines)} lines disagree with LRCLIB timing, not fixing them')
		return
	audio = stable_whisper.audio.load_audio(audio_path, sr=16000, verbose=False) if bad else None
	for i in bad:
		a = times[i] + off
		b = min(next((t + off for t in times[i + 1:] if t is not None), a + 10), a + 10)
		lo = max(0, a - 1)
		try:
			res = _model.align(audio[int(lo * 16000):int((b + 0.5) * 16000)], lines[i], language='zh', verbose=None)
			ws = [SimpleNamespace(word=w.word, start=w.start + lo, end=w.end + lo) for seg in res.segments for w in seg.words]
		except Exception:
			ws = []
		if not ws:
			toks = re.findall(r"[A-Za-z']+|\S", lines[i])
			step = min(b - a, 0.5 * len(toks)) / len(toks)
			ws = [SimpleNamespace(word=' ' + w if w[0].isascii() else w, start=a + k * step, end=a + (k + 1) * step) for k, w in enumerate(toks)]
		logging.info(f'Lyrics: moved line {i + 1} "{lines[i]}" from {per_line[i][0].start if per_line[i] else None} to {ws[0].start:.2f}s')
		per_line[i] = ws

def ts(t):
	t = max(0, t)
	return f'{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}'

def write_ass(per_line, path):
	spans = [(ws[0].start, ws[-1].end) if ws else None for ws in per_line]
	events = []
	for i, ws in enumerate(per_line):
		if not ws:
			continue
		start, end = spans[i]
		prev = next((spans[j] for j in range(i - 1, -1, -1) if spans[j]), None)
		prev2 = next((spans[j] for j in range(i - 2, -1, -1) if spans[j]), None) if i >= 2 else None
		show = prev[0] if prev and start - prev[1] < BREAK else start - LEAD	# appear while the line before is sung, or LEAD secs ahead after a break
		if prev2:
			show = max(show, prev2[1])	# but not before the previous line on this row is done
		show = min(show, start)
		nxt = next((spans[j] for j in range(i + 1, len(spans)) if spans[j]), None)
		hide = max(end, nxt[0]) if nxt and nxt[0] - end < BREAK else end + 1	# clear the screen during breaks
		text, t = '', show
		for w in ws:
			if w.start > t:
				text += '{\\k%d}' % round((w.start - t) * 100)
			word = w.word.strip()
			if text and w.word.startswith(' ') and re.match(r'[A-Za-z]', word):	# keep English words apart, but not word pieces
				word = ' ' + word
			text += '{\\kf%d}%s' % (max(1, round((w.end - w.start) * 100)), word)
			t = max(t, w.end)
		events.append(f'Dialogue: 0,{ts(show)},{ts(hide)},{"Up" if i % 2 == 0 else "Down"},,0,0,0,,{fit(text)}{text}')
	with open(path, 'w') as fp:
		fp.write(HEADER + '\n'.join(events) + '\n')

def make_lyrics(video, audio, out_path):
	"""Write KTV lyrics for `video` (named '<artist> - <title>---<id>.<ext>') to out_path, timed on `audio`
	(the vocal-only track if there is one). Returns False if LRCLIB has no lyrics for it."""
	global _model, stable_whisper
	import stable_whisper
	name = os.path.basename(video).rpartition('---')[0] or os.path.splitext(os.path.basename(video))[0]
	artist, _, track = name.rpartition(' - ')
	dur = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', video]))
	rec = lrclib(artist, track, dur)
	if not rec:
		return False
	lines = lyric_lines(rec, artist, track)
	if _model is None:
		_model = stable_whisper.load_model('small', device='cpu')
	# voice-activity detection keeps lyrics out of silent intros, but only works on a music-free vocal track
	result = _model.align(audio, '\n'.join(lines), language='zh', vad=(audio != video), verbose=None)
	per_line = words_per_line(lines, [w for seg in result.segments for w in seg.words])
	fix_strays(lines, per_line, lrc_times(rec, lines, artist, track), audio)
	write_ass(per_line, out_path)
	return True
