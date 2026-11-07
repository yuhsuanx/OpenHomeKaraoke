"""KTV-style lyrics: fetch from LRCLIB, time every character against the song's own audio with Whisper (stable-ts),
and write an .ass subtitle (two rows, next line shown ahead, per-character colour wipe) that VLC plays over the video."""
import os, re, json, subprocess, urllib.parse, urllib.request
from opencc import OpenCC

S2T = OpenCC('s2tw')
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

def lyric_lines(rec, artist, track):
	raw = re.sub(r'\[[\d:.]+\]', '', rec.get('plainLyrics') or rec['syncedLyrics'])	# some plainLyrics carry [mm:ss] tags too
	lines = []
	for ln in S2T.convert(raw).splitlines():
		if CREDIT.search(ln):
			continue
		ln = re.sub(r'\s+', ' ', re.sub(r'[^\w\s\']', ' ', ln)).strip()	# drop punctuation
		if ln and ln.replace(' ', '') not in (track, artist + track, track + artist):	# drop title header lines
			lines.append(ln)
	return lines

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

def ts(t):
	t = max(0, t)
	return f'{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}'

def write_ass(lines, words, path):
	# map aligned words back to lyric lines by counting non-space characters
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
	spans = [(ws[0].start, ws[-1].end) if ws else None for ws in per_line]
	events = []
	for i, ws in enumerate(per_line):
		if not ws:
			continue
		start, end = spans[i]
		prev = next((spans[j] for j in range(i - 1, -1, -1) if spans[j]), None)
		prev2 = next((spans[j] for j in range(i - 2, -1, -1) if spans[j]), None) if i >= 2 else None
		show = prev[0] if prev else start - 3	# appear while the line before is sung
		if prev2:
			show = max(show, prev2[1])	# but not before the previous line on this row is done
		show = min(show, start)
		nxt = next((spans[j] for j in range(i + 1, len(spans)) if spans[j]), None)
		hide = max(end, nxt[0]) if nxt else end + 1
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
	global _model
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
	write_ass(lines, [w for seg in result.segments for w in seg.words], out_path)
	return True
