# -*- coding: utf-8 -*-
"""ビルド前の確認: faster-whisper が音声ファイルを読めること（av との組み合わせの不整合を検出）。"""
import os
import tempfile
import wave

from faster_whisper.audio import decode_audio

path = os.path.join(tempfile.gettempdir(), 'smoke_decode.wav')
with wave.open(path, 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b'\0\0' * 16000)
n = len(decode_audio(path))
os.remove(path)
assert n > 0, 'decode_audio が空を返しました'
print('音声の読み込み OK (%d サンプル)' % n)
