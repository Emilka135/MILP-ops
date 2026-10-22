"""Pitch-preserving FFmpeg atempo at the source rate, then anti-aliased telephony conversion."""
import subprocess
import numpy as np

PROCESSING_VERSION = 'pcm48-atempo-resample-2'
SOURCE_RATE = 48000


def process_audio(audio, rate, output_rate=8000):
    if not .5 <= rate <= 2.5:raise ValueError('Speech rate must be 0.5–2.5')
    # No shell; filter arguments contain only validated numeric values.
    # FFmpeg skips samples above 2.0; chain stages to preserve pitch and signal.
    tempos = [2.0, float(rate) / 2] if rate > 2 else [float(rate)]
    filters=','.join(f'atempo={value}' for value in tempos)+f',aresample={int(output_rate)}'
    result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','f32le','-ar',str(SOURCE_RATE),'-ac','1','-i','pipe:0','-af',filters,'-f','f32le','-ar',str(output_rate),'-ac','1','pipe:1'],input=np.asarray(audio,dtype='<f4').tobytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,check=True)
    value=np.frombuffer(result.stdout,dtype='<f4').copy()
    if not len(value) or not np.isfinite(value).all():raise ValueError('Invalid synthesized audio')
    # Leave 1 dB headroom without amplifying background noise.
    peak=float(np.max(np.abs(value)))
    if peak>.89:value*=.89/peak
    return value
