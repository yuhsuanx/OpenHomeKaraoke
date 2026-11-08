import os, sys, io, string, json, subprocess, yt_dlp, gzip
import pykakasi, pinyin, logging, requests, shutil, hashlib, re
from unidecode import unidecode
from urllib.parse import unquote
from opencc import OpenCC

from lib.ChineseNumber import *
from lib.settings import *

KKS = pykakasi.kakasi()
S2T = OpenCC('s2tw')
TITLE_NOISE = re.compile(r'純音樂|伴唱|伴奏|卡拉\s*OK|karaoke|KTV|官方|動態歌詞|歌詞版|歌詞|Official|Music Video|Lyrics?|MV|HD|4K|1080p', re.I)
HAS_CJK = re.compile(r'[㐀-鿿]')

def normalize_song_name(title):
	"""Turn a YouTube title into '歌手 - 歌名', e.g. '純音樂 周杰倫 Jay Chou ─《楓》Wild West KTV 伴奏' -> '周杰倫 - 楓'.
	Returns the title unchanged if no singer/song split is found."""
	def clean(s):
		s = TITLE_NOISE.sub(' ', s)
		s = re.sub(r'[【\[(（][^】\])）]*[】\])）]', ' ', s)	# drop bracketed extras like (歌詞版) 【HD】
		words = re.sub(r'([㐀-鿿])(?=[A-Za-z])', r'\1 ', s).split()	# '藍又時Shadya' -> '藍又時 Shadya'
		if HAS_CJK.search(s):	# '周杰倫 Jay Chou' -> '周杰倫'
			words = [w for w in words if HAS_CJK.search(w)]
		return ' '.join(words).strip(' -─—–|｜:：')
	# 〈〉 marks a song; 《》 can be the album/drama it's from: '曾沛慈〈After Everything〉原創影集《來！金來號！》插曲'
	m = re.search(r'〈(.+?)〉', title) or re.search(r'[《「『](.+?)[》」』]', title)
	if m:
		artist, song = title[:m.start()], m.group(1)
	elif m := re.search(r'\s[-–—]\s', title):	# 'A - B', also with an en/em dash: '梁文音Wen Yin Liang – 分手後不要做朋友'
		artist, song = title[:m.start()], title[m.end():]
	elif len(parts := re.split(r'(?<=[㐀-鿿])-|-(?=[㐀-鿿])', title)) > 1:	# '李玖哲Nicky Lee-想太多Think Too Much-完整版MV', not 'A-Lin'
		artist, song = parts[0], parts[1]
	elif m := next((b for b in re.finditer(r'【([^】]+)】', title) if clean(b.group(1))), None):	# '藍又時Shadya Lan【倫敦的愛情】Official MV'
		artist, song = title[:m.start()], m.group(1)
	else:
		return title
	artist, song = clean(artist), clean(song)
	return S2T.convert(f'{artist} - {song}') if artist and song else title

NAME_SEP = re.compile(r'[【】\[\]［］「」『』《》〈〉()（）｜|/♪~～:：,，、!！?？#＃+]+|\s+[-─—–]+\s+|(?<=[㐀-鿿])[-─—–]|[-─—–](?=[㐀-鿿])')
NAME_TOKEN = re.compile(r'[0-9]*[㐀-鿿][㐀-鿿0-9]*|[^\s㐀-鿿](?:[^㐀-鿿]*[^\s㐀-鿿])?')	# a Chinese run ('2002年的第一場雪'), or a Latin run with its spaces ('Jay Chou')
NAME_NOISE = re.compile(TITLE_NOISE.pattern + r'|高音質|完整版|純享版|劇情版|官方版|片尾|片頭|主題曲|插曲|電視劇|電影|原創|影集|ENG SUB|Pinyin|avex|Topic|VEVO|Channel', re.I)
SINGER_SEP = re.compile(r'\s*(?:&|、|，|,|/)\s*')	# same as lib.catalog: '那英 & 王菲'

def name_chunks(text):
	"""Pieces of a YouTube title or channel name that could be a singer or song name, noise words dropped:
	'Della丁噹【手掌心】MV官方版-中視古裝大戲[蘭陵王]片尾曲' -> ['Della', '丁噹', '手掌心', '中視古裝大戲', '蘭陵王']."""
	out = []
	for part in NAME_SEP.split(text or ''):
		for tok in NAME_TOKEN.findall(part):
			name = re.sub(r'\s+', ' ', NAME_NOISE.sub(' ', tok)).strip(' .-_')
			if name and (name == tok or len(name) > 1) and S2T.convert(name) not in out:	# '官方版' -> '版': drop the leftover
				out.append(S2T.convert(name))
	return out

def name_suggestions(yt_title, channel=''):
	"""Best-guess singers and song title for a YouTube video, plus pieces of its title and channel name to
	pick from instead (shown in the search page's download dialog)."""
	name, chunks, from_channel = normalize_song_name(yt_title), name_chunks(yt_title), name_chunks(channel)
	if ' - ' in name:	# parsed (the result may equal the title: 'Justin Bieber - Baby ft. Ludacris')
		artist, title = name.split(' - ', 1)
		singers = [s for s in SINGER_SEP.split(artist) if s]
	else:	# the title names no singer ('離歌'): the channel ('信樂團 Shin Band') usually does
		singers = from_channel[:1]
		title = next((c for c in chunks if c not in singers), S2T.convert(yt_title))
	dedupe = lambda xs: list(dict.fromkeys(xs))[:12]
	return {'singers': singers, 'title': title,
	        'singer_chips': dedupe(singers + from_channel + chunks), 'title_chips': dedupe([title] + chunks)}
filelist, cookies_opt = [], []
downloading_songs = {}
to_pinyin = lambda t: pinyin.get(t, format='numerical')
translit = lambda t: unidecode(t).lower()
get_alpha = lambda t: ''.join([c for c in t if c in string.ascii_letters])
get_alnum = lambda t: ''.join([c for c in t if c in string.ascii_letters+string.digits])
to_romaji = lambda t: ' '.join([its['hepburn'] for its in KKS.convert(t)])
ls_media_files = lambda fullpath: sorted([f'{fullpath}/{f}'.replace('//','/') for f in os.listdir(fullpath) if not f.startswith('.') and '.'+f.split('.')[-1] in media_file_exts])
ls_subdir = lambda fullpath: sorted([g.rstrip('/') for f in os.listdir(fullpath) for g in [f'{fullpath}/{f}'.replace('//','/')] if not f.startswith('.') and os.path.isdir(g)])


def sec2hhmmss(sec, sub_second=False):
	try:
		s = float(sec)
		return f'{int(s//3600):02d}:{int((s%3600)//60):02d}:{int(s%60):02d}.{int(s%1*100):02d}'
	except:
		return ''

def hhmmss2sec(hms):
	hh, mm, ss = [float(i) for i in (['0', '0']+hms.split(':'))[-3:]]
	return hh*3600 + mm*60 + ss

expand_path = lambda t: os.path.expanduser(os.path.expandvars(t))

def Open(fn, mode='r', **kwargs):
	if fn == '-':
		return sys.stdin if mode.startswith('r') else sys.stdout
	fn = expand_path(fn)
	return gzip.open(fn, mode, **kwargs) if fn.lower().endswith('.gz') else open(fn, mode, **kwargs)

md5sum = lambda filepath: hashlib.md5(Open(filepath,'rb').read()).hexdigest()

def Try(*args):
	exc = ''
	for arg in args:
		try:
			return arg() if callable(arg) else arg
		except Exception as e:
			exc = e
	return str(exc)

get_filesize = lambda fn: Try(lambda: os.path.getsize(fn), 0)

def fuzzy(txt, dct=FUZZY_PINYIN):
	for src, tgt in dct.items():
		txt = txt.replace(src, tgt)
	return txt


def str_search(name, name_list):
	# 1. exact full match
	if name in name_list:
		return [name_list.index(name)]

	# 2. exact substring match
	res = [[ii, -abs(len(it)-len(name))] for ii,it in enumerate(name_list) if (name and name in it) or (it and it in name)]
	return [it[0] for it in sorted(res, key=lambda t:t[1])] if res else []


def filepath2songtitle(fn):
	s = os.path.basename(unquote(fn).rstrip('/')).split('.')[0].strip()
	return os.path.basename(os.path.dirname(unquote(fn).rstrip('/')))+s if s.isdigit() else s


def findSong(name, lang=None, flist=filelist):
	name = name.lower().strip()
	name_list = [filepath2songtitle(fn).lower() for fn in flist]

	# 0. pre-transform
	if lang == 'el':
		name = fuzzy(name, FUZZY_GREEK)
		name_list = [fuzzy(n, FUZZY_GREEK) for n in name_list]

	# 1. exact full match of original form
	if name in name_list:
		return [name_list.index(name)]

	# 2. match by pinyin if Chinese or unknown
	if lang in [None, 'zh']:
		# 3. pinyin full match
		pinyin_list = [get_alnum(fuzzy(to_pinyin(num2zh(n)))) for n in name_list]
		pinyin_name = get_alnum(fuzzy(to_pinyin(num2zh(name))))
		res = str_search(pinyin_name, pinyin_list)
		if pinyin_name and res:
			return res
		pinyin_list = [get_alpha(fuzzy(to_pinyin(num2zh(n)))) for n in name_list]
		pinyin_name = get_alpha(fuzzy(to_pinyin(num2zh(name))))
		if pinyin_name and res:
			return res

	# 3. match by romaji if Japanese or unknown
	if lang in [None, 'ja']:
		# 5. romaji full match
		romaji_list = [get_alpha(to_romaji(n)) for n in name_list]
		romaji_name = get_alpha(to_romaji(name))
		res = str_search(romaji_name, romaji_list)
		if romaji_name and res:
			return res

	# 4. substring match
	res = str_search(name, name_list)
	if res:
		return res
	
	# 5. match by transliteration
	translit_list = [get_alpha(fuzzy(translit(n))) for n in name_list]
	translit_name = get_alpha(fuzzy(translit(name)))
	res = str_search(translit_name, translit_list)
	if translit_name and res:
		return res

	return []


def findMedia(base_path, name, lang=None, stack=0):
	f_lst = ls_media_files(base_path)
	res = findSong(name, lang, f_lst)
	if res:
		return [f_lst[i] for i in res]
	if stack<MAX_WALK_LEVEL:
		for d in ls_subdir(base_path):
			res = findMedia(d, name, lang, stack+1)
			if res:
				return res
	return []


# For yt-dlp
def call_yt_dlp(argv, get_stdout = False):
	ret_code = 0
	if get_stdout:
		old_stdout = sys.stdout
		sys.stdout = io.StringIO()
	try:
		yt_dlp.main(argv)
	except SystemExit as e:
		ret_code = e.code
	if get_stdout:
		ret_stdout = sys.stdout
		sys.stdout = old_stdout
		return ret_stdout.getvalue()
	return ret_code

def get_yt_dlp_json(url):
	out_json = call_yt_dlp(['-j', url], True).strip()
	if not out_json.startswith('{'):
		out_json = out_json[out_json.find('{'):]
	return json.loads(out_json)

def get_video_file_basename(url):
	try:
		info_json = get_yt_dlp_json(url)
		return f"{info_json['title']}.{info_json['ext']}"
	except Exception as e:
		logging.error(f'Error parsing JSON from "{url}": {e}')
		return get_alnum(url.split('//')[-1])+'.mp4'

def download_video(song_url, include_subtitles, high_quality, redownload):
	logging.info("Downloading video: " + song_url)
	downloading_songs[song_url] = 1
	out_bn = get_video_file_basename(song_url)
	tmp_fn = os.path.expanduser(f'{DOWNLOAD_PATH}/tmp/{out_bn}')
	out_fn = os.path.expanduser(f'{DOWNLOAD_PATH}/{out_bn}')

	# If file already present, skip downloading
	if get_filesize(out_fn)==0 or redownload:
		opt_quality = ['-f', 'bestvideo[height<=1080]+bestaudio[abr<=160]'] if high_quality else ['-f', 'mp4+m4a']
		opt_sub = ['--sub-langs', 'all', '--embed-subs'] if include_subtitles else []
		cmd = ['--fixup', 'force', '--socket-timeout', '3', '-R', 'infinite', '--remux-video', 'mp4'] \
			+ cookies_opt + opt_quality + ["-o", tmp_fn] + opt_sub + [song_url]
		logging.info("Youtube-dl command: " + " ".join(cmd))
		rc = call_yt_dlp(cmd)
		if get_filesize(tmp_fn)==0:
			logging.error("Error code while downloading, retrying without format options ...")
			cmd = ["-o", tmp_fn, '--socket-timeout', '3', '-R', 'infinite'] + [song_url]
			logging.debug("Youtube-dl command: " + " ".join(cmd))
			rc = call_yt_dlp(cmd)
		if get_filesize(tmp_fn):
			logging.debug("Song successfully downloaded: " + song_url)
			shutil.move(tmp_fn, out_fn)
			downloading_songs[song_url] = 0
		else:
			logging.error("Error downloading song: " + song_url)
			downloading_songs[song_url] = -1

	return out_fn if get_filesize(out_fn) else ''
