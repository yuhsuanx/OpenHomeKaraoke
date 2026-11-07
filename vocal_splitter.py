#!/usr/bin/env python3

import os, re, sys, time, shutil
import argparse, requests, subprocess

import numpy as np
import soundfile as sf
import torch
from tqdm import tqdm

from lib import dataset
from lib import nets
from lib import spec_utils
import librosa


class Separator(object):

	def __init__(self, model, device=None, batchsize=1, cropsize=256, postprocess=False):
		self.model = model
		self.offset = model.offset
		self.device = device
		self.batchsize = batchsize
		self.cropsize = cropsize
		self.postprocess = postprocess

	def _postprocess(self, X_spec, mask):
		if self.postprocess:
			mask_mag = np.abs(mask)
			mask_mag = spec_utils.merge_artifacts(mask_mag)
			mask = mask_mag * np.exp(1.j * np.angle(mask))

		y_spec = X_spec * mask
		v_spec = X_spec - y_spec

		return y_spec, v_spec

	def _separate(self, X_spec_pad, roi_size):
		X_dataset = []
		patches = (X_spec_pad.shape[2] - 2 * self.offset) // roi_size
		for i in range(patches):
			start = i * roi_size
			X_spec_crop = X_spec_pad[:, :, start:start + self.cropsize]
			X_dataset.append(X_spec_crop)

		X_dataset = np.asarray(X_dataset)

		self.model.eval()
		with torch.no_grad():
			mask_list = []
			# To reduce the overhead, dataloader is not used.
			for i in tqdm(range(0, patches, self.batchsize)):
				X_batch = X_dataset[i: i + self.batchsize]
				X_batch = torch.from_numpy(X_batch).to(self.device)

				mask = self.model.predict_mask(X_batch)

				mask = mask.detach().cpu().numpy()
				mask = np.concatenate(mask, axis=2)
				mask_list.append(mask)

			mask = np.concatenate(mask_list, axis=2)

		return mask

	def separate(self, X_spec):
		n_frame = X_spec.shape[2]
		pad_l, pad_r, roi_size = dataset.make_padding(n_frame, self.cropsize, self.offset)
		X_spec_pad = np.pad(X_spec, ((0, 0), (0, 0), (pad_l, pad_r)), mode='constant')
		X_spec_pad /= np.abs(X_spec).max()

		mask = self._separate(X_spec_pad, roi_size)
		mask = mask[:, :, :n_frame]

		y_spec, v_spec = self._postprocess(X_spec, mask)

		return y_spec, v_spec

	def separate_tta(self, X_spec):
		n_frame = X_spec.shape[2]
		pad_l, pad_r, roi_size = dataset.make_padding(n_frame, self.cropsize, self.offset)
		X_spec_pad = np.pad(X_spec, ((0, 0), (0, 0), (pad_l, pad_r)), mode='constant')
		X_spec_pad /= X_spec_pad.max()

		mask = self._separate(X_spec_pad, roi_size)

		pad_l += roi_size // 2
		pad_r += roi_size // 2
		X_spec_pad = np.pad(X_spec, ((0, 0), (0, 0), (pad_l, pad_r)), mode='constant')
		X_spec_pad /= X_spec_pad.max()

		mask_tta = self._separate(X_spec_pad, roi_size)
		mask_tta = mask_tta[:, :, roi_size // 2:]
		mask = (mask[:, :, :n_frame] + mask_tta[:, :, :n_frame]) * 0.5

		y_spec, v_spec = self._postprocess(X_spec, mask)

		return y_spec, v_spec


# All tracks (original, nonvocal, vocal) are normalized to the same loudness, so switching
# vocal mode or songs does not change the perceived volume.
TARGET_LUFS = -12


def ffm_lufs(fn):
	err = subprocess.run(['ffmpeg', '-nostats', '-i', fn, '-vn', '-af', 'ebur128=framelog=quiet', '-f', 'null', '-'],
	                     capture_output = True, text = True).stderr
	m = re.findall(r'I:\s+(-?[\d.]+) LUFS', err)
	return float(m[-1]) if m else None


def loudness_filter(fn):
	lufs = ffm_lufs(fn)
	if lufs is None:
		return []
	gain = float(np.clip(TARGET_LUFS - lufs, -20, 12))
	if abs(gain) < 0.5:
		return []
	# a limiter (without auto-leveling, delay compensated) prevents clipping when boosting
	return ['-af', f'volume={gain:.2f}dB' + (',alimiter=limit=0.891:level=0:latency=1' if gain > 0 else '')]


def ffm_normalize_video(fn, tmp_fn):
	if not fn.lower().endswith('.mp4'):  # re-encoding to AAC only fits the mp4 container
		return
	af = loudness_filter(fn)
	if not af:
		return
	r = subprocess.run(['ffmpeg', '-y', '-i', fn, '-map', '0:v?', '-map', '0:a', '-map', '0:s?', '-c', 'copy',
	                    '-c:a', 'aac', '-b:a', '192k'] + af + [tmp_fn])
	if r.returncode == 0:
		try:
			os.replace(tmp_fn, fn)
		except Exception:
			os.remove(tmp_fn)


def ffm_wav2m4a(input_fn, output_fn, br = '128k'):
	input_fn, output_fn = [fn.replace('"', '\\"') for fn in [input_fn, output_fn]]
	subprocess.run(['ffmpeg', '-y', '-i', input_fn] + loudness_filter(input_fn) + ['-c:a', 'aac', '-b:a', br, output_fn])


def ffm_video2wav(input_fn, output_fn):
	# The built-in DNN model is trained on 44100 sampling rate, it can still run but does not work on other sampling rates
	input_fn, output_fn = [fn.replace('"', '\\"') for fn in [input_fn, output_fn]]
	subprocess.run(['ffmpeg', '-y', '-i', input_fn, '-f', 'wav', '-ar', '44100', output_fn])


def split_vocal_by_stereo(in_wav, out_wav_nonvocal, out_wav_vocal):
	try:
		# Create temporary filenames if not done yet
		X, sr = librosa.load(in_wav, sr=44100, mono=False, dtype = np.float32, res_type = 'kaiser_fast')
		if X.shape[0] < 2:
			return False
		if out_wav_nonvocal:
			sf.write(out_wav_nonvocal, X[0, :] - X[1, :], sr)
		if out_wav_vocal:
			sf.write(out_wav_vocal, X[0, :] + X[1, :], sr)
		return True
	except:
		return False


def split_vocal_by_dnn(in_wav, out_wav_nonvocal, out_wav_vocal, args):
	print('Loading wave source ...', end = ' ', flush = True)
	X, sr = librosa.load(in_wav, sr=args.sr, mono=False, dtype = np.float32, res_type = 'kaiser_fast')
	print('done', flush = True)

	if X.ndim == 1:
		# mono to stereo
		X = np.asarray([X, X])

	print('STFT of wave source ...', end = ' ', flush = True)
	X_spec = spec_utils.wave_to_spectrogram(X, args.hop_length, args.n_fft)
	print('done', flush = True)

	sp = Separator(args.model, args.device, args.batchsize, args.cropsize)

	if args.tta:
		y_spec, v_spec = sp.separate_tta(X_spec)
	else:
		y_spec, v_spec = sp.separate(X_spec)

	print('Inverse STFT of instruments ...', end = ' ', flush = True)
	wave = spec_utils.spectrogram_to_wave(y_spec, hop_length = args.hop_length)
	print('done', flush = True)
	sf.write(out_wav_nonvocal, wave.T, sr)

	if out_wav_vocal:
		print('Inverse STFT of vocals ...', end = ' ', flush = True)
		wave = spec_utils.spectrogram_to_wave(v_spec, hop_length = args.hop_length)
		print('done', flush = True)
		sf.write(out_wav_vocal, wave.T, sr)


song_path = ''
last_completed = ''
use_DNN = True

def get_next_file(cuda_device, only=None):
	"""Next song to split: queued songs first, then the library. only='queue'/'library' restricts to one of them,
	so two splitters can share the work (one for live downloads, one for the backlog)."""
	global song_path, use_DNN, last_completed
	try:
		obj = requests.get(f'http://localhost:5001/get_vocal_todo_list/{cuda_device.type}/{last_completed}').json()
		song_path = obj['download_path'].rstrip('/')
		use_DNN = obj['use_DNN']
	except:
		if not song_path:
			print('PiKaraoke is not running and --download-path is not specified, exiting ...')
			sys.exit()
		obj = {'queue': []}

	if not os.path.isdir(song_path+'/nonvocal') and not os.path.isdir(song_path+'/vocal'):
		return None
	for fn in (obj['queue'] if only != 'library' else []):
		bn = ('' if use_DNN else '.')+os.path.basename(fn)
		if os.path.isdir(song_path+'/nonvocal') and not os.path.isfile(f'{song_path}/nonvocal/{bn}.m4a'):
			return os.path.basename(fn)
		if os.path.isdir(song_path+'/vocal') and not os.path.isfile(f'{song_path}/vocal/{bn}.m4a'):
			return os.path.basename(fn)

	if only == 'queue':
		return None

	# get from listing directory, leaving queued songs to the queue splitter
	queued = {os.path.basename(fn) for fn in obj['queue']} if only == 'library' else set()
	for bn in [i for i in os.listdir(song_path) if not i.startswith('.') and os.path.isfile(song_path+'/'+i) and i not in queued]:
		bn1 = ('' if use_DNN else '.')+bn
		if os.path.isdir(song_path+'/nonvocal') and not os.path.isfile(f'{song_path}/nonvocal/{bn1}.m4a'):
			return bn
		if os.path.isdir(song_path+'/vocal') and not os.path.isfile(f'{song_path}/vocal/{bn1}.m4a'):
			return bn

	return None


def main(argv):
	global song_path, last_completed

	p = argparse.ArgumentParser()
	p.add_argument('--download-path', '-d', help = "Path for downloaded songs. Will be overridden by the one from HTTP request. "
					"Set this to forcefully run the vocal-splitter even when PiKaraoke is not running.", default = '')
	p.add_argument('--gpu', '-g', type = int, help = 'CUDA device ID for GPU inference, set to -1 to force to use CPU (default will try to use GPU if available)', default = None)
	p.add_argument('--pretrained_model', '-P', type = str, default = 'models/baseline.pth')
	p.add_argument('--sr', '-r', type = int, default = 44100)
	p.add_argument('--n_fft', '-f', type = int, default = 2048)
	p.add_argument('--hop_length', '-H', type = int, default = 1024)
	p.add_argument('--batchsize', '-B', type = int, default = 4)
	p.add_argument('--cropsize', '-c', type = int, default = 256)
	p.add_argument('--postprocess', '-p', action = 'store_true')
	p.add_argument('--tta', '-t', action = 'store_true')
	p.add_argument('--ramdir', '-rd', help = 'Temporary directory on RAMDISK to reduce I/O load',
	               default = 'z:/' if sys.platform.startswith('win') else '/dev/shm')
	p.add_argument('--only', choices = ['queue', 'library'], default = None,
	               help = 'Only split queued songs, or only the rest of the library (default: both, queued first)')
	args = p.parse_args(argv)

	song_path = os.path.expanduser(args.download_path).rstrip('/')

	# Determine the GPU device and load the DNN model
	print('Loading vocal-splitter model ...', end = ' ', flush = True)
	device = torch.device('cpu')
	model = nets.CascadedNet(args.n_fft, args.hop_length, 32, 128, True)
	model.load_state_dict(torch.load(args.pretrained_model, map_location = device))
	if (args.gpu is None or args.gpu >= 0) and torch.cuda.is_available():
		device = torch.device(f'cuda:{0 if args.gpu is None else args.gpu}')
		model.to(device)
	elif (args.gpu is None or args.gpu >= 0) and torch.backends.mps.is_available():
		device = torch.device('mps')
		model.to(device)
	args.model = model
	args.device = device
	print('done', flush = True)

	# set song_path global variable from local server
	get_next_file(device, args.only)

	# Use RAMDISK for large temporary .wav files for speed up if available
	RAMDIR = args.ramdir if os.path.isdir(args.ramdir) else song_path

	# Create temporary filenames if not done yet (per process, so several splitters can run at once)
	pid = os.getpid()
	in_wav, out_wav_vocal, out_wav_nonvocal, out_m4a_vocal, out_m4a_nonvocal, tmp_mp4 = \
		[f'{RAMDIR}/.{pid}.input.wav', f'{RAMDIR}/.{pid}.vocal.wav', f'{RAMDIR}/.{pid}.nonvocal.wav',
		 f'{song_path}/.{pid}.vocal.m4a', f'{song_path}/.{pid}.nonvocal.m4a', f'{song_path}/.{pid}.normalizing.mp4']

	# Main loop
	while True:
		next_file = get_next_file(device, args.only)
		if not next_file:
			time.sleep(2)
			continue

		# run vocal splitter on next_file
		print(f'Start processing {next_file} :')
		ffm_video2wav(song_path+'/'+next_file, in_wav)
		ffm_normalize_video(song_path+'/'+next_file, tmp_mp4)

		if use_DNN:
			split_vocal_by_dnn(in_wav, out_wav_nonvocal, out_wav_vocal, args)
			if os.path.isdir(song_path+'/nonvocal'):
				ffm_wav2m4a(out_wav_nonvocal, out_m4a_nonvocal)
				shutil.move(out_m4a_nonvocal, f'{song_path}/nonvocal/{next_file}.m4a')
			if os.path.isdir(song_path+'/vocal'):
				ffm_wav2m4a(out_wav_vocal, out_m4a_vocal)
				shutil.move(out_m4a_vocal, f'{song_path}/vocal/{next_file}.m4a')
		else:
			split_vocal_by_stereo(in_wav, out_wav_nonvocal, out_wav_vocal)
			if os.path.isdir(song_path+'/nonvocal'):
				ffm_wav2m4a(out_wav_nonvocal, out_m4a_nonvocal)
				shutil.move(out_m4a_nonvocal, f'{song_path}/nonvocal/.{next_file}.m4a')
			if os.path.isdir(song_path+'/vocal'):
				ffm_wav2m4a(out_wav_vocal, out_m4a_vocal)
				shutil.move(out_m4a_vocal, f'{song_path}/vocal/.{next_file}.m4a')
		last_completed = next_file


if __name__ == '__main__':
	main(sys.argv[1:])
