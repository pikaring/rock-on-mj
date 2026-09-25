"""話者識別版のexeを実際に動かして確かめる（GitHub Actions の Windows で使う）。

1. 画面を起動し，診断ログに「話者識別: exe=... モデル=...」が出る（部品が見つかった）ことを確認
2. exe を --transcribe-worker で起動し，話者識別つきの文字起こしが最後まで通ることを確認
   （4形式の出力・話者A/B・Excelの話者列・一時WAVの削除）

使い方:
  python tools/smoke_speaker_exe.py --app-dir <exeのあるフォルダ> \
      --whisper-model <faster-whisperのモデルフォルダ> --audio <複数人の会議音声.wav> [--language en]
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

EXE_NAME = '会議録音ツール_話者識別.exe'
LOG_NAME = '診断ログ.txt'


def fail(msg):
    print(f'::error::{msg}')
    sys.exit(1)


def check_gui(app_dir):
    exe = os.path.join(app_dir, EXE_NAME)
    log = os.path.join(app_dir, LOG_NAME)
    if os.path.exists(log):
        os.remove(log)
    proc = subprocess.Popen([exe], cwd=app_dir)
    try:
        deadline = time.time() + 180   # 初回起動はセキュリティスキャンで遅いことがある
        text = ''
        while time.time() < deadline:
            time.sleep(3)
            try:
                with open(log, encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                continue
            if '話者識別: exe=' in text:
                break
            if proc.poll() is not None:
                break
        print('---- 診断ログ（画面起動） ----')
        print(text[-3000:])
        line = next((ln for ln in text.splitlines() if '話者識別: exe=' in ln), '')
        if not line:
            fail('画面が起動しないか，話者識別の部品を調べる前に止まりました')
        if 'exe=None' in line or 'モデル=None' in line:
            fail(f'話者識別の部品が見つかりません: {line}')
        if proc.poll() is not None:
            fail(f'画面が終了しました（終了コード {proc.returncode}）')
        print('OK: 画面が起動し，話者識別の部品が見つかった')
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def check_worker(app_dir, whisper_model, audio, language):
    exe = os.path.join(app_dir, EXE_NAME)
    base = app_dir
    diarizer = os.path.join(base, 'diarizer', 'nemo-speech.exe')
    ggufs = glob.glob(os.path.join(base, 'models', 'diarization', '*.gguf'))
    if not os.path.isfile(diarizer) or not ggufs:
        fail('diarizer\\nemo-speech.exe または models\\diarization\\*.gguf がありません')

    # 画面と同じく一時フォルダの日本語名のフォルダを使う（日本語パスの確認も兼ねる）
    workdir = os.path.join(tempfile.gettempdir(), '文字起こしツール_話者識別')
    os.makedirs(workdir, exist_ok=True)
    diar_dir = os.path.join(tempfile.gettempdir(), 'rock_on_mj_diar')
    os.makedirs(diar_dir, exist_ok=True)
    if not diar_dir.isascii():
        fail(f'一時フォルダが英数字だけではありません: {diar_dir}')
    job = dict(model_path=whisper_model, audio_path=audio, prompt='',
               result_path=os.path.join(workdir, '_result.txt'),
               out_txt=True, out_srt=True, out_docx=True, out_xlsx=True,
               diarize=True, diarizer_exe=diarizer, diar_model=ggufs[0],
               # 画面と同じく英数字だけの作業フォルダ（diar_workdir と同じ場所）
               diar_wav=os.path.join(diar_dir, 'diar_input.wav'),
               rttm_path=os.path.join(diar_dir, 'diar_result.rttm'),
               language=language)
    for p in (job['rttm_path'], job['diar_wav']):
        if os.path.exists(p):
            os.remove(p)
    job_path = os.path.join(workdir, '_job.json')
    with open(job_path, 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False)

    err_path = os.path.join(workdir, 'worker_stderr.txt')
    start = time.time()
    with open(err_path, 'w', encoding='utf-8') as err:
        proc = subprocess.Popen([exe, '--transcribe-worker', job_path],
                                stdout=subprocess.PIPE, stderr=err,
                                encoding='utf-8', errors='replace')
        done = None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                print('?? ' + line)
                continue
            if msg['t'] in ('log', 'error'):
                print(('ERROR ' if msg['t'] == 'error' else '') + msg.get('m', ''))
            elif msg['t'] == 'done':
                done = msg
        proc.wait()
    print(f'---- 処理時間 {time.time() - start:.1f}秒  終了コード {proc.returncode} ----')
    with open(err_path, encoding='utf-8', errors='replace') as f:
        err_text = f.read()
    print('---- 子プロセスの stderr（末尾） ----')
    print(err_text[-4000:])

    if proc.returncode != 0 or not done:
        fail(f'文字起こしが完了しませんでした（終了コード {proc.returncode}）')
    if len(done['saved']) != 4:
        fail(f'出力が4つそろっていません: {done["saved"]}')
    if not os.path.exists(job['rttm_path']):
        fail('話者識別の結果（RTTM）がありません（話者識別が失敗した）')
    if os.path.exists(job['diar_wav']):
        fail('話者識別用の一時WAVが残っています')

    txt = next(p for p in done['saved'] if p.endswith('.txt'))
    with open(txt, encoding='utf-8') as f:
        body = f.read()
    print('---- 出力（txt） ----')
    print(body[:3000])
    if '話者A：' not in body or '話者B：' not in body:
        fail('txt に話者A・話者Bが出ていません')

    import openpyxl
    xlsx = next(p for p in done['saved'] if p.endswith('.xlsx'))
    head = [c.value for c in openpyxl.load_workbook(xlsx).active[1]]
    if '話者' not in head:
        fail(f'Excel に話者の列がありません: {head}')
    print('OK: 話者識別つきの文字起こしが最後まで通った')


def main():
    # Windows のコンソール（cp1252 等）でも日本語のログを出せるようにする
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--app-dir', required=True)
    ap.add_argument('--whisper-model', required=True)
    ap.add_argument('--audio', required=True)
    ap.add_argument('--language', default='ja')
    a = ap.parse_args()
    check_gui(a.app_dir)
    check_worker(a.app_dir, a.whisper_model, a.audio, a.language)
    # 画面起動で作られるファイルは配布物に含めない
    for name in (LOG_NAME, '辞書.txt'):
        p = os.path.join(a.app_dir, name)
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(os.path.join(a.app_dir, '会議録音'), ignore_errors=True)


if __name__ == '__main__':
    main()
