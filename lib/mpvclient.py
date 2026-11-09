"""mpv player control over its JSON IPC socket (--input-ipc-server).

One mpv window for the whole session: songs are loaded with `loadfile` (per-file options, reset when the
song ends) and between songs mpv idles while the app's splash screen is shown as an OSD overlay.
Duck-types the VLCClient calls karaoke.py shares between players (stop/seek/pause/play/restart/kill/
is_running/is_playing/get_info_xml/is_transposing), plus live setters VLC can't do without a restart:
audio track (select_audio), subtitle visibility, delays, speed, transpose and fullscreen.
Volume is kept on VLC's scale (256 = 100%) so the web UI is unchanged."""
import os, sys, json, time, socket, shutil, logging, subprocess, threading

VLC_100 = 256	# VLC HTTP volume for 100%


def get_default_mpv_path():
	return shutil.which('mpv') or '/opt/homebrew/bin/mpv'


def pitch_filter(semitones):
	"""mpv --af value that shifts pitch by semitones ('' for none)."""
	semitones = float(semitones or 0)
	return f'@rb:rubberband=pitch-scale={2 ** (semitones / 12):.6f}' if semitones else ''


class MPVClient:
	vol_increment = 10	# VLC volume units, as VLCClient

	def __init__(self, path=None):
		self.path = path or get_default_mpv_path()
		self.sock_path = f'/tmp/pikaraoke-mpv-{os.getpid()}.sock'	# AF_UNIX paths must stay short (104 bytes on macOS)
		self.cmd_base = [
			self.path,
			f'--input-ipc-server={self.sock_path}',
			'--idle=yes', '--force-window=yes',	# stay open between songs (the splash overlay is drawn on the idle window)
			'--auto-window-resize=no', '--geometry=50%x50%',	# window size doesn't follow each video's size
			'--no-input-default-bindings', '--osc=no', '--osd-level=0',	# like VLC --no-keyboard-events/--no-mouse-events
			'--sub-auto=no', '--audio-file-auto=no',
			'--volume-max=200',	# VLC's web UI volume goes up to 512 = 200%
			'--hwdec=videotoolbox-copy',	# hardware decode, but copy frames out: handing AV1 frames straight to Vulkan (auto-safe) lost the GPU (VK_ERROR_DEVICE_LOST)
			'--title=pikaraoke',
			'--quiet',	# no status line spam in the app's console
		]
		self.splash_path = f'/tmp/pikaraoke-mpv-{os.getpid()}-splash.bgra'
		self.overlay_key = None	# what the splash overlay currently shows, None if hidden
		self.image_keys = {}	# overlay id -> what it shows (1: QR code over videos, 2: queue toast), None if hidden
		self.process = None
		self.is_transposing = False	# True while (re)starting, so get_state() returns the cached state
		self.lock = threading.Lock()
		self.last_play = None	# (file, options, audio_files) of the last play_file(), to resume it after a GPU loss
		self.recovering = False	# True while mpv is reopened after a GPU loss, so alive() stays True

	# ---- IPC ----
	def _request(self, *commands, timeout=2):
		"""Send commands on one connection; return their responses in order (None for ones that failed)."""
		if self.process is None or self.process.poll() is not None:
			return [None] * len(commands)
		try:
			with socket.socket(socket.AF_UNIX) as s:
				s.settimeout(timeout)
				s.connect(self.sock_path)
				s.sendall(b''.join(json.dumps({'command': list(c), 'request_id': i}).encode() + b'\n' for i, c in enumerate(commands)))
				res, buf = {}, b''
				while len(res) < len(commands):
					data = s.recv(65536)
					if not data:
						break
					buf += data
					*lines, buf = buf.split(b'\n')
					for line in lines:
						msg = json.loads(line)
						if 'request_id' in msg:	# skip async events
							res[msg['request_id']] = msg
				return [res.get(i) for i in range(len(commands))]
		except (OSError, ValueError) as e:
			logging.debug(f'mpv IPC failed {commands}: {e}')
			return [None] * len(commands)

	def get(self, *props):
		"""Property values (None if unavailable)."""
		return [(r or {}).get('data') for r in self._request(*[('get_property', p) for p in props])]

	def set_property(self, name, value):
		r = self._request(('set_property', name, value))[0]
		if not r or r.get('error') != 'success':
			logging.warning(f'mpv set {name}={value} failed: {r}')
		return r

	def command(self, *args):
		return self._request(args)[0]

	# ---- window ----
	def start(self, fullscreen=True):
		"""Open the mpv window (idle, black until the splash overlay or a song is shown)."""
		if os.path.exists(self.sock_path):
			os.remove(self.sock_path)
		command = self.cmd_base + (['--fs', '--ontop'] if fullscreen else [])
		logging.info(f'mpv command: {command}')
		self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		threading.Thread(target=self._watch_output, args=(self.process,), daemon=True).start()
		t0 = time.time()
		while self.alive() and self.command('get_property', 'pid') is None and time.time() - t0 < 10:
			time.sleep(0.05)
		# default key bindings are off (like VLC); F and ESC act like they did in the pygame splash window:
		# F toggles fullscreen, ESC (or closing the window) quits the app
		for key, cmd in [('f', 'cycle fullscreen'), ('MBTN_LEFT_DBL', 'cycle fullscreen'), ('ESC', 'quit')]:
			self.command('keybind', key, cmd)

	def alive(self):
		"""mpv process (the window) is open."""
		return self.recovering or (self.process is not None and self.process.poll() is None)

	def _watch_output(self, process):
		"""Pass mpv's console output through. If it loses the GPU (VK_ERROR_DEVICE_LOST, the window stays frozen
		for good), reopen mpv and resume the song where it was."""
		lost = False
		for line in process.stdout:
			sys.stdout.buffer.write(line)
			sys.stdout.flush()
			if not lost and b'VK_ERROR_DEVICE_LOST' in line:
				lost = True
				threading.Thread(target=self._recover).start()

	def _recover(self):
		logging.warning('mpv lost the GPU (VK_ERROR_DEVICE_LOST): reopening it and resuming the song')
		with self.lock:
			self.is_transposing = True	# the song counts as playing meanwhile
			try:
				names = ['path', 'time-pos', 'pause', 'fullscreen', 'track-list', 'volume', 'af', 'speed',
				         'audio-delay', 'sub-delay', 'sub-visibility']
				state = dict(zip(names, self.get(*names)))
				audio = [t.get('external-filename', '') for t in state.pop('track-list') or [] if t['type'] == 'audio' and t.get('selected')]
				path, pos, fs = state.pop('path'), state.pop('time-pos'), state.pop('fullscreen')
				self.recovering = True
				self.kill()
				self.start(bool(fs))
				self.recovering = False
				self.overlay_key, self.image_keys = None, {}	# the new window has no overlays; karaoke.py re-adds them
				if not path or not self.last_play or self.last_play[0] != path:
					return
				_, options, audio_files = self.last_play
				self.set_property('pause', True)
				if not self._loadfile(path, list(options) + [('start', pos or 0)]):
					return
				for f in audio_files:
					self.command('audio-add', f, 'auto')
				self.select_audio(audio[0] if audio else '')
				for k, v in state.items():
					if v is not None and k != 'pause':
						self.set_property(k, v)
				self.set_property('pause', bool(state['pause']))
			except Exception as e:
				logging.error(f'mpv recovery failed: {e}')
			finally:
				self.recovering = self.is_transposing = False

	def show_overlay(self, bgra, w, h, dw, dh):
		"""Show a w x h BGRA image (opaque) scaled to dw x dh over the whole window; no-op if unchanged."""
		key = (hash(bgra), w, h, dw, dh)
		if key == self.overlay_key:
			return
		with open(self.splash_path, 'wb') as fp:	# mpv >= 0.42 reads the file before the command returns
			fp.write(bgra)
		r = self.command('overlay-add', 0, 0, 0, self.splash_path, 0, 'bgra', w, h, w * 4, dw, dh)
		self.overlay_key = key if r and r.get('error') == 'success' else None

	def hide_overlay(self):
		if self.overlay_key is not None:
			self.command('overlay-remove', 0)
			self.overlay_key = None

	def show_image(self, oid, key, bgra, w, h, x, y, dw=None, dh=None):
		"""Show a w x h BGRA image at (x, y), scaled to dw x dh, as overlay oid (higher ids draw on top);
		no-op if it already shows key."""
		if self.image_keys.get(oid) == key:
			return
		path = f'/tmp/pikaraoke-mpv-{os.getpid()}-{oid}.bgra'
		with open(path, 'wb') as fp:
			fp.write(bgra)
		r = self.command('overlay-add', oid, x, y, path, 0, 'bgra', w, h, w * 4, dw or w, dh or h)
		self.image_keys[oid] = key if r and r.get('error') == 'success' else None

	def hide_image(self, oid):
		if self.image_keys.get(oid) is not None:
			self.command('overlay-remove', oid)
			self.image_keys[oid] = None

	def osd_size(self):
		return self.get('osd-width', 'osd-height')

	# ---- playback ----
	def _loadfile(self, file_path, options, timeout=15):
		"""loadfile with per-file options; wait until the first frame is ready. Returns True on success."""
		opts = ','.join(f'{k}=%{len(str(v).encode())}%{v}' for k, v in options)	# %len% quoting: paths may hold , or =
		try:
			with socket.socket(socket.AF_UNIX) as s:
				s.settimeout(timeout)
				s.connect(self.sock_path)
				s.sendall(json.dumps({'command': ['loadfile', file_path, 'replace', -1, opts], 'request_id': 0}).encode() + b'\n')
				buf, t0 = b'', time.time()
				while time.time() - t0 < timeout:
					data = s.recv(65536)
					if not data:
						return False
					buf += data
					*lines, buf = buf.split(b'\n')
					for msg in map(json.loads, lines):
						if msg.get('request_id') == 0 and msg.get('error') != 'success':
							logging.error(f'mpv loadfile failed: {msg}')
							return False
						if msg.get('event') == 'playback-restart':
							return True
						if msg.get('event') == 'end-file' and msg.get('reason') == 'error':
							logging.error(f'mpv could not play {file_path}: {msg}')
							return False
		except (OSError, ValueError) as e:
			logging.error(f'mpv loadfile failed: {e}')
		return False

	def play_file(self, file_path, volume, options=[], audio_files=[], audio_file='', start_paused=False):
		"""Load file_path into the open mpv window, add the external audio_files, and select audio_file
		('' = the video's own audio). options: (name, value) mpv options for this file only, e.g.
		('sub-files-append', path); mpv restores them when the file ends.
		Returns {'has_video', 'has_subtitle', 'volume'}, or None if it failed."""
		with self.lock:
			self.is_transposing = True
			try:
				self.set_property('pause', True)	# select the audio track first, so the wrong track never sounds
				if volume:
					self.vol_set(volume)
				logging.info(f'mpv loadfile {file_path} {options}')
				self.last_play = (file_path, options, audio_files)
				if not self._loadfile(file_path, options):
					return None
				for f in audio_files:	# loadfile keeps only the last of repeated audio-files-append options
					self.command('audio-add', f, 'auto')
				tracks = self.get('track-list')[0] or []
				self.select_audio(audio_file, tracks)
				has_video = any(t['type'] == 'video' and not t.get('albumart') for t in tracks)
				if has_video:
					self.hide_overlay()	# the paused first frame is ready, so no black gap
				if not start_paused:
					self.set_property('pause', False)
				return {
					'has_video': has_video,
					'has_subtitle': any(t['type'] == 'sub' for t in tracks),
					'volume': self.get_volume(),
				}
			except Exception as e:
				logging.error(f'Playing file in mpv failed: {e}')
				return None
			finally:
				self.is_transposing = False

	def select_audio(self, audio_file='', tracks=None):
		"""Switch live to the external audio track audio_file, or to the file's own audio if ''.
		An audio_file not loaded at start (e.g. split finished mid-song) is added on the fly."""
		tracks = tracks or self.get('track-list')[0] or []
		audio = [t for t in tracks if t['type'] == 'audio']
		if audio_file:
			match = [t for t in audio if t.get('external-filename') == audio_file]
			if not match:
				return self.command('audio-add', audio_file, 'select')
		else:
			match = [t for t in audio if not t.get('external')] or audio[:1]
		if match:
			return self.set_property('aid', match[0]['id'])

	def stop(self):
		"""End the song; mpv stays open (idle) for the splash."""
		return self.command('stop')

	def pause(self, save_status=True):
		return self.set_property('pause', True)

	def play(self):
		return self.set_property('pause', False)

	def seek(self, seek_sec):
		return self.command('seek', float(seek_sec), 'absolute')

	def restart(self):
		self.seek(0)
		return self.play()

	def set_transpose(self, semitones):
		return self.set_property('af', pitch_filter(semitones))

	def set_fullscreen(self, fullscreen):
		self.set_property('ontop', bool(fullscreen))
		return self.set_property('fullscreen', bool(fullscreen))

	# ---- volume / speed (VLC volume scale) ----
	def get_volume(self):
		v = self.get('volume')[0]
		return None if v is None else round(v * VLC_100 / 100)

	def vol_set(self, value):
		self.set_property('volume', max(0., min(200., float(value) * 100 / VLC_100)))
		return self.get_volume()

	def vol_up(self):
		return self.vol_set((self.get_volume() or 0) + self.vol_increment)

	def vol_down(self):
		return self.vol_set((self.get_volume() or 0) - self.vol_increment)

	def playspeed_set(self, value):
		self.set_property('speed', float(value))
		return self.get('speed')[0]

	# ---- status ----
	def get_info_xml(self, xml=None):
		"""Same keys as VLCClient.get_info_xml(), for Karaoke.get_state()."""
		pos, dur, vol, paused, adelay, sdelay, speed = self.get('time-pos', 'duration', 'volume', 'pause',
		                                                        'audio-delay', 'sub-delay', 'speed')
		if pos is None and dur is None:
			return {}
		return {
			'position': (pos or 0) / dur if dur else 0.,
			'length': round(dur or 0),
			'time': round(pos or 0),
			'volume': None if vol is None else round(vol * VLC_100 / 100),
			'state': 'paused' if paused else 'playing',
			'audiodelay': adelay,
			'subtitledelay': sdelay,
			'rate': speed,
		}

	def is_running(self):
		"""A song is loaded (what Karaoke.is_file_playing() means); False while idle between songs."""
		return self.is_transposing or (self.alive() and self.get('idle-active')[0] is False)

	def is_playing(self):
		return self.is_running() and self.get('pause')[0] is False

	def is_paused(self):
		return self.is_running() and self.get('pause')[0] is True

	def kill(self):
		"""Close the mpv window."""
		if self.alive():
			self.command('quit')
			try:
				self.process.wait(2)
			except subprocess.TimeoutExpired:
				self.process.kill()
		for f in (self.sock_path, self.splash_path):
			if os.path.exists(f):
				os.remove(f)
