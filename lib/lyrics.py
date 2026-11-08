"""KTV-style lyrics: fetch from LRCLIB, time every character against the song's own audio with Whisper (stable-ts),
and write an .ass subtitle (two rows, next line shown ahead, per-character colour wipe) that VLC plays over the video."""
import os, re, json, difflib, logging, statistics, subprocess, tempfile, urllib.parse, urllib.request
from types import SimpleNamespace
import numpy as np, pinyin
from opencc import OpenCC
from PIL import Image
try:
	from ocrmac import ocrmac	# Apple Vision text recognition, for lyrics burned into the video (macOS only)
except ImportError:
	ocrmac = None

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
	"""LRCLIB's record for the song, trying each singer alone if the whole artist ('UNIVERSAL MUSIC TAIWAN & 梁文音') finds nothing."""
	singers = [s for s in re.split(r'\s*(?:&|、|，|,|/)\s*', artist) if s]	# as lib.NLP.SINGER_SEP
	for q in [{'artist_name': a, 'track_name': track} for a in dict.fromkeys([artist] + singers) if a] or [{'q': track}]:
		res = json.load(urllib.request.urlopen('https://lrclib.net/api/search?' + urllib.parse.urlencode(q), timeout=15))
		if res := [r for r in res if r.get('plainLyrics') or r.get('syncedLyrics')]:
			break
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
		ws = align_slot(audio, lines[i], a, min(next((t + off for t in times[i + 1:] if t is not None), a + 10), a + 10))
		logging.info(f'Lyrics: moved line {i + 1} "{lines[i]}" from {per_line[i][0].start if per_line[i] else None} to {ws[0].start:.2f}s')
		per_line[i] = ws

def align_slot(audio, text, a, b):
	"""Whisper words of `text` (one or more lines) aligned within secs a..b of `audio` (16 kHz samples), or spread
	evenly over the slot if that fails."""
	lo = max(0, a - 1)
	try:
		res = _model.align(audio[int(lo * 16000):int((b + 0.5) * 16000)], text, language='zh', verbose=None)
		ws = [SimpleNamespace(word=w.word, start=w.start + lo, end=w.end + lo) for seg in res.segments for w in seg.words]
	except Exception:
		ws = []
	if not ws:
		toks = re.findall(r"[A-Za-z']+|\S", text)
		step = min(b - a, 0.5 * len(toks)) / len(toks)
		ws = [SimpleNamespace(word=' ' + w if w[0].isascii() else w, start=a + k * step, end=a + (k + 1) * step) for k, w in enumerate(toks)]
	return ws

# --- On-screen lyrics: most Chinese MVs burn the lyrics into the video. Reading them (OCR) gives each line's on-screen
# window, which already accounts for story intros and dialogue; Whisper then only times the colour fill inside it.
TOK = re.compile(r"[A-Za-z']+|\w")	# an English word, or one Chinese character / digit
OCR_FPS = 2	# frames read per second
OCR_SAMPLES = 30	# full frames read to find where on screen the lyrics are

def tok_keys(text):
	"""Tokens of text by sound, since OCR and lyric sites differ in variants like 爲/為."""
	return [t.lower() if t[0].isascii() else pinyin.get(t, format='strip') for t in TOK.findall(text)]

def lcs(a, b):
	"""Index pairs of a longest common subsequence of a and b: the most matches in order, so a verse shown twice
	pairs with its 1st and 2nd showing."""
	A, B = np.array(a, dtype=object), np.array(b, dtype=object)
	dp = np.zeros((len(a) + 1, len(b) + 1), dtype=np.int32)
	for i in range(1, len(a) + 1):
		dp[i, 1:] = np.maximum.accumulate(np.maximum(dp[i - 1, 1:], dp[i - 1, :-1] + (B == A[i - 1])))
	out, i, j = [], len(a), len(b)
	while i and j:
		if a[i - 1] == b[j - 1] and dp[i, j] == dp[i - 1, j - 1] + 1:
			out.append((i - 1, j - 1)); i -= 1; j -= 1
		elif dp[i - 1, j] >= dp[i, j - 1]:
			i -= 1
		else:
			j -= 1
	return out[::-1]

def similar(a, b):
	return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

def ocr(path):
	"""[(text, top, bottom)] read from an image, top/bottom as fractions of its height from the top."""
	res = ocrmac.OCR(Image.open(path), language_preference=['zh-Hant'], recognition_level='accurate').recognize()
	return [(S2T.convert(t), 1 - (y + h), 1 - y) for t, conf, (x, y, w, h) in res if conf >= 0.3]	# Vision's origin is bottom-left

def video_frames(video, vf, tmp):
	for f in os.listdir(tmp):
		os.remove(os.path.join(tmp, f))
	subprocess.run(['ffmpeg', '-v', 'error', '-i', video, '-vf', vf, '-q:v', '3', tmp + '/%05d.jpg'], check=True)
	return sorted(os.path.join(tmp, f) for f in os.listdir(tmp))

def ocr_windows(video, dur, lines, times):
	"""((start, end) on screen for each lyric line, the lines with words only the screen shows added), or None if
	the video doesn't show these lyrics (or OCR isn't available, it needs macOS). Lines never shown are placed by
	LRC time, or in the gap between their neighbours."""
	if ocrmac is None:
		return None
	keys = [tok_keys(ln) for ln in lines]
	is_lyric = lambda t: len(k := tok_keys(t)) >= 3 and max(similar(k, lk) for lk in keys) >= 0.6
	with tempfile.TemporaryDirectory() as tmp:
		# 1. find the screen band the lyrics are shown in, from text in sample frames that matches a lyric line
		spans = [(a, b) for f in video_frames(video, f'fps={OCR_SAMPLES}/{dur:.0f},scale=960:-1', tmp) for t, a, b in ocr(f) if is_lyric(t)]
		if len(spans) < 3:
			return None
		top, bottom = max(0, min(a for a, _ in spans) - 0.03), min(1, max(b for _, b in spans) + 0.03)
		# 2. read that band through the whole video: [readings, start, end, keys] per text shown. Frames whose text
		# sounds alike are merged, so OCR noise (昨日 read as 昨旦 in one frame) doesn't cut a line into short pieces
		segs, live = [], []
		for i, f in enumerate(video_frames(video, f'fps={OCR_FPS},crop=iw:ih*{bottom - top:.3f}:0:ih*{top:.3f},scale=960:-1', tmp)):
			now = i / OCR_FPS
			live = [s for s in live if s[2] >= now - 1]	# at most a frame or two missed
			for t, _, _ in ocr(f):
				if not (k := tok_keys(t)):
					continue
				s = max(live, key=lambda s: similar(k, s[3]), default=None)
				if s and similar(k, s[3]) >= 0.6:
					s[0].append(t)
					s[2] = now + 1 / OCR_FPS
					s[3] = max(s[3], k, key=len)	# compare with the fullest reading, not one cut off by a fade
				else:
					live.append(s := [[t], now, now + 1 / OCR_FPS, k])
					segs.append(s)
	# each text is its most common reading (the longest on ties) and how many frames read it that way;
	# ones shown under 1s are transition blur
	segs = sorted(([t := max(r, key=lambda t: (r.count(t), len(t))), a, b, r.count(t)] for r, a, b, _ in segs if b - a >= 1), key=lambda s: s[1])
	# 3. match lyric lines to the text read, in order; credits, names and dialogue subtitles match nothing
	lyr = [(i, k) for i, kl in enumerate(keys) for k in range(len(kl))]
	scr = [(s, t) for s, seg in enumerate(segs) for t in TOK.findall(seg[0])]	# (segment, token) on screen
	hits, pairs, run = {}, {}, []
	for p in lcs([keys[i][k] for i, k in lyr], [tok_keys(t)[0] for _, t in scr]) + [(None, None)]:
		if run and p[0] is not None and p[0] - run[-1][0] <= 2 and p[1] - run[-1][1] <= 3:	# keep runs of >= 3 near-consecutive matches
			run.append(p); continue
		if len(run) >= 3:
			for a, b in run:
				hits.setdefault(lyr[a][0], []).append(scr[b][0])	# one entry per matched token
				pairs.setdefault(lyr[a][0], []).append((lyr[a][1], b))
		run = [p]
	lines = [screen_words(ln, pairs.get(i, []), scr, segs) for i, ln in enumerate(lines)]
	win = [None] * len(lines)
	for i, ss in hits.items():
		if len(ss) >= 0.4 * len(keys[i]):
			win[i] = (min(segs[s][1] for s in ss), max(segs[s][2] for s in ss))
	seen = [i for i in range(len(lines)) if win[i]]
	logging.info(f'Lyrics: {len(seen)}/{len(lines)} lines found on screen (rows {top:.2f}-{bottom:.2f} of the height)')
	if len(seen) < len(lines) / 2:
		return None
	# 4. lines not seen: LRC time, offset like the nearest seen line
	anchors = [i for i in seen if times[i] is not None]
	for i in range(len(lines)):
		if not win[i] and times[i] is not None and anchors:
			j = min(anchors, key=lambda j: abs(times[j] - times[i]))
			s = times[i] + win[j][0] - times[j]
			win[i] = (s, s + 0.5 * len(keys[i]) + 1)
	# 5. still unplaced (no LRC time): share the gap between the neighbouring placed lines, by length
	i = 0
	while i < len(lines):
		if win[i]:
			i += 1; continue
		j = i
		while j < len(lines) and not win[j]:
			j += 1
		need = [0.5 * len(keys[k]) + 1 for k in range(i, j)]	# a generous singing time per line
		a = win[i - 1][1] if i else (win[j][0] - sum(need) if j < len(lines) else None)
		b = win[j][0] if j < len(lines) else (a + sum(need) if a is not None else None)
		if a is not None and b - a >= 1:
			for k, n in zip(range(i, j), need):
				win[k] = (a, a + (b - a) * n / sum(need))
				a = win[k][1]
		i = j
	return win, lines

def screen_words(line, pairs, scr, segs):
	"""line with the words the MV shows but LRCLIB's copy lacks, e.g. '傻裡傻氣地在夢想中醒來' as sung and shown:
	'傻裡傻氣地在每一個今天的夢想中醒來'. pairs: (token in line, index into scr) matches, in order. Only pure insertions
	of Chinese characters between two matched neighbours, read the same in 2+ frames, are taken; where the screen
	differs (an OCR misreading like 醒 -> 醛) LRCLIB's character is kept."""
	spans = [m.span() for m in TOK.finditer(line)]
	adds = []
	for (k1, b1), (k2, b2) in zip(pairs, pairs[1:]):
		gap = scr[b1 + 1:b2]
		if k2 == k1 + 1 and 0 < len(gap) <= 8 and all(HAN.fullmatch(t) and segs[s][3] >= 2 for s, t in gap):
			adds.append((spans[k1][1], ''.join(t for _, t in gap)))
	for pos, text in reversed(adds):
		line = line[:pos] + text + line[pos:]
	if adds:
		logging.info(f'Lyrics: added from the screen: {line}')
	return line

def align_in_windows(lines, win, audio_path):
	"""Whisper words per line, aligned inside each line's on-screen window, a few consecutive lines per call
	(each call costs ~1s however short)."""
	audio = stable_whisper.audio.load_audio(audio_path, sr=16000, verbose=False)
	runs = []
	for i in (i for i in range(len(lines)) if win[i]):
		if runs and runs[-1][-1] == i - 1 and len(runs[-1]) < 6 and win[i][0] - win[i - 1][1] < 3:
			runs[-1].append(i)
		else:
			runs.append([i])
	per_line = [[] for _ in lines]
	for r in runs:
		ws = align_slot(audio, '\n'.join(lines[i] for i in r), win[r[0]][0] + 0.5, win[r[-1]][1] + 0.5)
		for i, got in zip(r, words_per_line([lines[i] for i in r], ws)):
			if not got or not win[i][0] - 1.5 <= got[0].start <= win[i][1]:	# landed outside its window: redo it alone
				got = align_slot(audio, lines[i], win[i][0] + 0.5, win[i][1] + 0.5)
			per_line[i] = unsqueeze(got, win[i][1])
	return per_line

def unsqueeze(ws, end):
	"""Words Whisper failed to align ('Failed to align the last N words') come back ~0 secs long, piled after the
	last good word, so they'd flash by unseen: spread 3+ such trailing words evenly up to `end` instead."""
	k = len(ws)
	while k and ws[k - 1].end - ws[k - 1].start < 0.05:
		k -= 1
	if len(ws) - k < 3:
		return ws
	a = ws[k - 1].end if k else ws[0].start
	step = max(0.1, min(0.6, (end - a) / (len(ws) - k)))
	return ws[:k] + [SimpleNamespace(word=w.word, start=a + n * step, end=a + (n + 1) * step) for n, w in enumerate(ws[k:])]

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
	times = lrc_times(rec, lines, artist, track)
	if _model is None:
		_model = stable_whisper.load_model('small', device='cpu')
	try:
		found = ocr_windows(video, dur, lines, times)
	except Exception as e:
		logging.warning(f'Lyrics: reading on-screen lyrics failed ({e}), using Whisper alone')
		found = None
	if found:
		win, lines = found
		per_line = align_in_windows(lines, win, audio)
	else:
		# voice-activity detection keeps lyrics out of silent intros, but only works on a music-free vocal track
		result = _model.align(audio, '\n'.join(lines), language='zh', vad=(audio != video), verbose=None)
		per_line = words_per_line(lines, [w for seg in result.segments for w in seg.words])
		fix_strays(lines, per_line, times, audio)
	write_ass(per_line, out_path)
	return True
