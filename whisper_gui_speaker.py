# ROCK ON MJ 話者識別版
# 会議録音版（whisper_gui_meeting.py）に，NVIDIA Nemotron 3 Diarization による
# 話者識別（誰がいつ話したか）を加えたもの。話者識別は NeMo-Speech.cpp の
# nemo-speech.exe（CPU版）を別プロセスで実行し，その結果を発言に割り当てる。
# 話者識別が使えない・失敗したときは，会議録音版と同じ結果（話者なし）を出す。
import sys
import os
import bisect
import ctypes
import hashlib
import faulthandler
import json
import platform
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import traceback
import wave
import zlib
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

# CTranslate2 と onnxruntime がそれぞれ別のOpenMPランタイムを読み込むと，
# 環境によってはメッセージも出さずにプロセスごと落ちることがある。
os.environ.setdefault('KMP_DUPLICATE_LIB_OK', 'TRUE')
# CTranslate2 が選んだCPU命令セットや演算バックエンドを診断ログに残す。
os.environ.setdefault('CT2_VERBOSE', '1')


# 認識精度を上げるための既定の固有名詞・専門用語。
# 実際の辞書は exe と同じ場所の「辞書.txt」で編集できます（無ければ起動時に生成）。
# ここに並べているのは「こういう語を書く」という例です。
# 実際に使う固有名詞（社名・部署名・製品名・地名など）は辞書.txtに書いてください。
DEFAULT_TERMS = [
    '議事録', '議題', '審議', '検討事項', '報告事項', '決裁', '稟議', '要件定義',
    '予算', '決算', '見積', '契約', '入札', '仕様書', '納期', '進捗',
    '部長', '課長', '主任', '担当者', '関係部署', '定例会議', '打ち合わせ',
    'ガイドライン', '運用', '保守', '委託', '仕様変更', '議事進行',
]

# 平均対数尤度がこの値を下回るセグメントを「※要確認」とみなす
LOW_CONF_LOGPROB = -0.6

DICT_FILENAME = '辞書.txt'
LOG_FILENAME = '診断ログ.txt'

# 文字起こし1回に必要なメモリの目安。large-v3-turbo（model.bin 0.76GB）で実測すると
# 物理メモリのピークが約1.1GB，コミット（仮想メモリ）のピークが約1.9GB。
# 物理メモリはWindowsが他プロセスから融通するため空きが少なくても動くことが多い。
# 一方コミットは足りないとその場で確保に失敗する（＝無言で落ちる）ので厳しめに見る。
# ※ ctranslate2 4.8.0 以降はモデル読み込みで約3GBを余分にコミットする回帰があり，
#    コミット上限の小さいPCで落ちていた。4.7.2 に固定して回避している（specと同じ前提）。
MEM_FACTOR = 1.2
MEM_PHYS_MARGIN_GB = 0.2
MEM_COMMIT_MARGIN_GB = 1.0

GB = 1024.0 ** 3

# CTranslate2 に使わせるCPU命令セットの上限。'' にすると自動（CPU任せ）。
# AVX-512を持つPCでの強制終了対策。詳しくは configure_cpu_isa() を参照。
FORCE_CPU_ISA = 'AVX2'

# ---- 話者識別の設定 ----
# nemo-speech.exe は exe と同じ場所の diarizer\ に置く（無ければ PATH と
# NeMo-Speech.cpp の既定インストール先を探す）。モデル（GGUF）は models\diarization\ に置く。
# モデルは必ずローカルのファイルを渡す。名前だけ渡すと nemo-speech が
# Hugging Face から自動でダウンロードしに行き，ローカル完結でなくなるため。
DIARIZER_DIRNAME = 'diarizer'
DIARIZER_EXE = 'nemo-speech.exe' if os.name == 'nt' else 'nemo-speech'
DIAR_MODEL_DIRNAME = os.path.join('models', 'diarization')
# 優先して使うモデル。無ければ models\diarization\ にある最初の .gguf を使う。
DIAR_MODEL_PREFERRED = ('Nemotron-3-Diarization.q8_0.gguf',
                        'Nemotron-3-Diarization.gguf')
# 話者識別の打ち切り時間 = 基本 + 音声長 × 倍率（CPUでの速度は未実測のため余裕を持たせる）
DIAR_TIMEOUT_BASE_SEC = 600
DIAR_TIMEOUT_FACTOR = 3.0
# 話者識別（v3-offline）のメモリは音声の長さにほぼ比例する。Linux・CPUでの実測:
#   1分 0.15GB / 30分 0.64GB / 60分 1.17GB（処理時間は 3秒 / 98秒 / 195秒，4コア）
# 空きが足りないときは話者識別だけ飛ばす（落ちると文字起こしまで失敗して見えるため）。
DIAR_MEM_BASE_GB = 0.15
DIAR_MEM_PER_MIN_GB = 0.018
# この秒数より短い話者の切り替わりは，前後の話者に吸収する（「はい」等の細切れ防止）
SPEAKER_MIN_RUN_SEC = 0.3
# 発言がどの話者区間とも重ならないとき，この秒数以内の最寄りの話者を採る
SPEAKER_NEAREST_SEC = 1.0
# nemo-speech のよくある失敗と，その説明（stderr に含まれる文字列 → 表示する文）
# リリース版 0.1.0（2026-09時点）は Nemotron 3 Diarization を読めない。
# 2026-09-24 以降の main をビルドしたものが必要（README参照）。
DIAR_ERROR_HINTS = [
    ('pre_ln transformer variant is not supported',
     'nemo-speech が古く，Nemotron 3 Diarization に対応していません（READMEの手順で新しい版を用意してください）'),
    ("unknown diarizer geometry preset",
     'nemo-speech が古く，このモデル用の設定（v3-offline）に対応していません'),
    ('model index is missing',
     'nemo-speech の model-index.json が見つかりません（diarizer フォルダに置いてください）'),
]
# nemo-speech はローカルのモデルを渡しても，起動時にモデル一覧（model-index.json）を読む。
# 既定では exe の ..\share\nemo-speech\ を探すため，bin の中身だけを diarizer\ に
# コピーすると見つからない。次の場所にあれば環境変数で教える。
DIAR_INDEX_CANDIDATES = ('model-index.json',
                         os.path.join('..', 'share', 'nemo-speech', 'model-index.json'))


def app_base_dir():
    return os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__)


def resource_path(name):
    """同梱リソースの場所（exeでは _internal の中）。"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---- 診断ログ（原因不明の強制終了を追えるようにする） ----

_diag_file = None
_diag_lock = threading.Lock()


def _writable_dir():
    """exeと同じ場所が書けないとき（読み取り専用の共有フォルダ等）は個人フォルダへ。"""
    base = app_base_dir()
    probe = os.path.join(base, '.write_test')
    try:
        with open(probe, 'w') as f:
            f.write('')
        os.remove(probe)
        return base
    except Exception:
        alt = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                           '文字起こしツール')
        try:
            os.makedirs(alt, exist_ok=True)
            return alt
        except Exception:
            return os.path.expanduser('~')


def setup_diag():
    """診断ログを開き，異常終了時のスタックも残るようにする。"""
    global _diag_file
    path = os.path.join(_writable_dir(), LOG_FILENAME)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
            os.replace(path, path + '.old')
    except Exception:
        pass
    try:
        _diag_file = open(path, 'a', encoding='utf-8', errors='replace', buffering=1)
    except Exception:
        return None
    # exe（コンソール無し）では sys.stdout/stderr が None のため，
    # ライブラリ側の出力先としても診断ログを使う。
    no_console = sys.stdout is None or sys.stderr is None
    if sys.stdout is None:
        sys.stdout = _diag_file
    if sys.stderr is None:
        sys.stderr = _diag_file
    # CTranslate2 の詳細ログは C++ 側が OS のファイル記述子(1,2)へ直接書くので，
    # sys.stderr を差し替えるだけでは拾えない。記述子ごと診断ログへ向ける。
    # これをやらないと，どのCPU命令セットを選んだかが記録されない。
    # コンソールから動かしているとき（ソースでの開発時）は画面表示を奪わない。
    if no_console or getattr(sys, 'frozen', False):
        try:
            os.dup2(_diag_file.fileno(), 1)
            os.dup2(_diag_file.fileno(), 2)
        except Exception:
            pass
    try:
        # クラッシュ（アクセス違反・異常命令・abort）でもPython側のスタックを残す
        faulthandler.enable(file=_diag_file, all_threads=True)
    except Exception:
        pass
    sys.excepthook = lambda t, v, tb: diag(
        '未処理の例外:\n' + ''.join(traceback.format_exception(t, v, tb)))
    try:
        threading.excepthook = lambda a: diag(
            '未処理の例外(スレッド):\n'
            + ''.join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    except Exception:
        pass
    return path


def diag(msg):
    if _diag_file is None:
        return
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with _diag_lock:
        try:
            _diag_file.write(f'[{stamp}] {msg}\n')
            _diag_file.flush()
        except Exception:
            pass


def _cpu_feature(index):
    try:
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(index))
    except Exception:
        return False


def has_avx2():
    """このCPUがAVX2を使えるか（Windowsに聞く）。"""
    return _cpu_feature(40)      # PF_AVX2_INSTRUCTIONS_AVAILABLE


def has_avx512():
    """このCPUがAVX-512を使えるか。"""
    return _cpu_feature(41)      # PF_AVX512F_INSTRUCTIONS_AVAILABLE


def configure_cpu_isa():
    """CTranslate2 と Intel MKL が使うCPU命令セットの上限を決める。

    第11世代Core（Tiger Lake，AVX-512あり）でモデル読み込み中にアクセス違反で
    プロセスごと落ちる。第12世代・第13世代（AVX-512は無効）や，同じ8GBの
    サーフェスでも12世代機では起きない。AVX-512の演算経路だけが違うため，
    既定ではAVX2までに抑える（速度差はわずか，安定性を優先）。

    **CT2_FORCE_CPU_ISA だけでは足りない。** これはCTranslate2自身のカーネルに
    しか効かず，実際の行列演算を担う Intel MKL は自前で命令セットを選ぶため，
    MKL_ENABLE_INSTRUCTIONS も併せて指定する必要がある。

    どちらも環境変数で自分で指定した場合はそちらを優先するので，
    切り分けのときは AVX512 / GENERIC などを外から与えられる。
    """
    names = ('CT2_FORCE_CPU_ISA', 'MKL_ENABLE_INSTRUCTIONS')
    preset = {n: os.environ.get(n) for n in names}
    if any(preset.values()):
        return ' / '.join(f'{n}={v}（環境変数の指定）' for n, v in preset.items() if v)
    if not FORCE_CPU_ISA:
        return '自動'
    if not has_avx512():
        # AVX-512を持たないCPU（第12・13世代等）に制限をかけても事故は防げず，
        # MKLがAVX-VNNI（int8の積和演算）を使わなくなるぶん遅くなるだけ。
        # 実測（第13世代・2分の会議音声・交互に3回ずつ）で中央値125.5秒→186.1秒。
        return '制限なし（AVX-512を持たないCPU）'
    if FORCE_CPU_ISA == 'AVX2' and not has_avx2():
        return '自動（AVX2非対応のCPU）'
    for name in names:
        os.environ[name] = FORCE_CPU_ISA
    return f'{FORCE_CPU_ISA}に制限（AVX-512を持つCPUのため）'


def foreign_modules():
    """読み込み済みDLLのうち，このツールのフォルダにもWindowsにも属さないものを返す。

    セキュリティ対策ソフト等がプロセスに注入したDLLを見つけるため。
    そうしたフックはネイティブライブラリの読み込みに割り込むので，
    原因不明のアクセス違反の出どころとして真っ先に疑う対象になる。
    """
    try:
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        hproc = k32.GetCurrentProcess()
        count = 2048
        arr = (ctypes.c_void_p * count)()
        needed = ctypes.c_ulong()
        if not psapi.EnumProcessModules(ctypes.c_void_p(hproc), ctypes.byref(arr),
                                        ctypes.sizeof(arr), ctypes.byref(needed)):
            return ['（取得できず）']
        n = min(count, needed.value // ctypes.sizeof(ctypes.c_void_p))
        base = os.path.normcase(app_base_dir())
        windir = os.path.normcase(os.environ.get('SystemRoot', r'C:\Windows'))
        out = []
        buf = ctypes.create_unicode_buffer(260)
        # 引数の型を明示しないと，モジュールハンドル（大きな整数）が
        # int に丸められて OverflowError になる。
        psapi.GetModuleFileNameExW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
        for i in range(n):
            if not psapi.GetModuleFileNameExW(ctypes.c_void_p(hproc),
                                              ctypes.c_void_p(arr[i]),
                                              buf, len(buf)):
                continue
            path = os.path.normcase(buf.value)
            if path.startswith(base) or path.startswith(windir):
                continue
            out.append(buf.value)
        return out
    except Exception as e:
        return [f'（取得できず: {e}）']


def mitigation_policies():
    """このプロセスに掛かっているWindowsの保護機能（Exploit Protection）を返す。
    CET（シャドースタック）等が有効だと，独自の制御フローを使うDLLが
    アクセス違反で落ちることがあるため，切り分けの材料として記録する。
    """
    POLICIES = {2: '動的コード制限', 7: 'CFG', 10: 'イメージ読み込み制限',
                15: 'シャドースタック(CET)'}
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        hproc = k32.GetCurrentProcess()
        out = []
        for pid, name in POLICIES.items():
            val = ctypes.c_ulong(0)
            ok = k32.GetProcessMitigationPolicy(
                ctypes.c_void_p(hproc), pid, ctypes.byref(val), ctypes.sizeof(val))
            out.append(f'{name}={val.value if ok else "不明"}')
        return ' / '.join(out)
    except Exception as e:
        return f'（取得できず: {e}）'


def check_model_file(model_dir):
    """model.bin が配布時のものと同じか照合する。

    配布時に models/<名前>/model.crc32 を同梱しておき，それがあるときだけ
    照合する（自分でモデルを差し替えた場合は照合しない）。
    ZIPの展開失敗やコピー中の破損を，読み込みで落ちる前に捕まえるため。
    戻り値: (ok, メッセージ)。ok が None は「照合情報が無い」。
    """
    path = os.path.join(model_dir, 'model.bin')
    ref = os.path.join(model_dir, 'model.crc32')
    if not os.path.exists(ref):
        return None, '照合情報なし'
    try:
        with open(ref, encoding='utf-8') as f:
            expected = f.read().strip().lower()
    except Exception as e:
        return None, f'照合情報を読めず（{e}）'
    try:
        crc = 0
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
        actual = f'{crc & 0xFFFFFFFF:08x}'
    except Exception as e:
        return False, f'モデルファイルを読めません（{e}）'
    if actual == expected:
        return True, f'照合OK（{actual}）'
    return False, f'不一致（期待 {expected} / 実際 {actual}）'


def memory_gb():
    """(搭載GB, 空き物理GB, 空きコミットGB)。取得できなければ (None, None, None)。"""
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None, None, None
        return st.ullTotalPhys / GB, st.ullAvailPhys / GB, st.ullAvailPageFile / GB
    except Exception:
        return None, None, None


def is_network_path(path):
    """ネットワークドライブ／UNCパス上かどうか。"""
    try:
        path = os.path.abspath(path)
        if path.startswith('\\\\'):
            return True
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return False
        return ctypes.windll.kernel32.GetDriveTypeW(drive + os.sep) == 4  # DRIVE_REMOTE
    except Exception:
        return False


def log_environment():
    total, avail, commit = memory_gb()
    if total is None:
        mem_line = '  メモリ: 取得できませんでした'
    else:
        mem_line = (f'  メモリ: 搭載{total:.1f}GB / 空き{avail:.1f}GB / '
                    f'空きコミット{commit:.1f}GB')
    diag('\n'.join([
        '==== 起動 ====',
        f'  実行ファイル: {sys.executable}',
        f'  基準フォルダ: {app_base_dir()}'
        + ('  ※ネットワーク上' if is_network_path(app_base_dir()) else ''),
        f'  Python: {sys.version.split()[0]}  frozen={getattr(sys, "frozen", False)}',
        f'  OS: {platform.platform()}',
        f'  CPU論理コア数: {os.cpu_count()}',
        f'  CPU: {platform.processor()}  AVX2={has_avx2()} AVX512={has_avx512()}',
        f'  使わせる命令セット: {configure_cpu_isa()}',
        mem_line,
        f'  保護機能: {mitigation_policies()}',
    ]))
    # 起動直後の時点で注入されているDLL（＝常駐ソフト由来）を控えておく。
    # 文字起こし開始時にもう一度取ると，何が後から割り込んだかが分かる。
    mods = foreign_modules()
    diag('外部DLL（起動時）: ' + ('\n    ' + '\n    '.join(mods) if mods else 'なし'))


def ensure_dict_template(base_dir):
    """辞書.txt が無ければ既定語をテンプレートとして書き出す。"""
    path = os.path.join(base_dir, DICT_FILENAME)
    if not os.path.exists(path):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('# 認識させたい固有名詞・専門用語を1行に1つ書いてください。\n')
                f.write('# 行頭が # の行はコメントとして無視されます。編集後は保存するだけで反映されます。\n')
                for t in DEFAULT_TERMS:
                    f.write(t + '\n')
        except Exception:
            pass
    return path


def build_initial_prompt(base_dir):
    """辞書.txt（無ければ既定語）から文脈プロンプトを組み立てる。"""
    terms = []
    path = os.path.join(base_dir, DICT_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        terms.append(line)
        except Exception:
            pass
    if not terms:
        terms = DEFAULT_TERMS
    return '会議の議事録です。専門用語：' + '、'.join(terms) + '。'


def fmt_mmss(t):
    return f'{int(t // 60):02d}:{int(t % 60):02d}'


def fmt_srt(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def fmt_eta(seconds):
    seconds = int(max(seconds, 0))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f'{h}時間{m}分'
    return f'{m}分{s:02d}秒'


# ---- 発言（話者つき） ----

class Utterance:
    """出力の1行分の発言。faster-whisper の Segment と同じ名前の属性を持つ。
    speaker は「話者A」等の表示名。話者識別をしなかったときは None。"""
    __slots__ = ('start', 'end', 'text', 'avg_logprob', 'speaker')

    def __init__(self, start, end, text, avg_logprob, speaker=None):
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob
        self.speaker = speaker


UNKNOWN_SPEAKER = '話者不明'


def has_speakers(utts):
    return any(u.speaker for u in utts)


def speaker_prefix(u):
    return f'{u.speaker}：' if u.speaker is not None else ''


def _join_texts(parts):
    """発言をつなぐ。日本語はそのまま詰め，英単語どうしの境目にだけ空白を入れる。"""
    out = ''
    for t in parts:
        if out and out[-1].isascii() and t[0].isascii():
            out += ' '
        out += t
    return out


def speaker_turns(utts):
    """同じ話者が続く発言をまとめる。[(話者, 本文), ...]"""
    turns = []
    for u in utts:
        text = u.text.strip()
        if not text:
            continue
        if turns and turns[-1][0] == u.speaker:
            turns[-1][1].append(text)
        else:
            turns.append((u.speaker, [text]))
    return [(spk, _join_texts(parts)) for spk, parts in turns]


def build_full_text(utts):
    if not has_speakers(utts):
        return ''.join(u.text for u in utts)
    return '\n'.join(f'{spk or UNKNOWN_SPEAKER}：{text}'
                     for spk, text in speaker_turns(utts))


def speaker_summary(utts):
    """話者ごとの発言時間と回数（登場順）。[(話者, 秒, 回数), ...]"""
    order, total, count = [], {}, {}
    for spk, text in speaker_turns(utts):
        count[spk] = count.get(spk, 0) + 1
    for u in utts:
        if u.speaker not in total:
            order.append(u.speaker)
            total[u.speaker] = 0.0
        total[u.speaker] += max(0.0, u.end - u.start)
    return [(spk or UNKNOWN_SPEAKER, total[spk], count.get(spk, 0)) for spk in order]


def format_txt(full_text, utts, low_flags):
    lines = [full_text]
    if utts:
        lines.append('\n\n=== タイムスタンプ付き ===')
        for u, low in zip(utts, low_flags):
            mark = '  ※要確認' if low else ''
            lines.append(f'[{fmt_mmss(u.start)} - {fmt_mmss(u.end)}] '
                         f'{speaker_prefix(u)}{u.text.strip()}{mark}')
    if has_speakers(utts):
        lines.append('\n\n=== 話者ごとの発言時間 ===')
        for spk, sec, n in speaker_summary(utts):
            lines.append(f'{spk}: {fmt_mmss(sec)}（{n}回）')
    return '\n'.join(lines)


# ---- 出力ファイル生成 ----

def save_txt(path, full_text, utts, low_flags):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(format_txt(full_text, utts, low_flags))


def save_srt(path, utts):
    with open(path, 'w', encoding='utf-8') as f:
        for i, u in enumerate(utts, 1):
            f.write(f'{i}\n{fmt_srt(u.start)} --> {fmt_srt(u.end)}\n'
                    f'{speaker_prefix(u)}{u.text.strip()}\n\n')


def save_docx(path, full_text, utts, low_flags, audio_name):
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.add_heading('文字起こし結果', level=0)
    p = doc.add_paragraph()
    run = p.add_run(f'対象ファイル: {audio_name}')
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    doc.add_heading('全文', level=1)
    if has_speakers(utts):
        for spk, text in speaker_turns(utts):
            para = doc.add_paragraph()
            para.add_run(f'{spk or UNKNOWN_SPEAKER}：').bold = True
            para.add_run(text)
    else:
        doc.add_paragraph(full_text)

    doc.add_heading('タイムスタンプ付き', level=1)
    for u, low in zip(utts, low_flags):
        para = doc.add_paragraph()
        tstamp = para.add_run(f'[{fmt_mmss(u.start)} - {fmt_mmss(u.end)}] ')
        tstamp.font.color.rgb = RGBColor(0x00, 0x78, 0xD4)
        if u.speaker is not None:
            para.add_run(speaker_prefix(u)).bold = True
        para.add_run(u.text.strip())
        if low:
            warn = para.add_run('  ※要確認')
            warn.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            warn.bold = True

    if has_speakers(utts):
        doc.add_heading('話者ごとの発言時間', level=1)
        for spk, sec, n in speaker_summary(utts):
            doc.add_paragraph(f'{spk}: {fmt_mmss(sec)}（{n}回）')
    doc.save(path)


def save_xlsx(path, utts, low_flags):
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment

    with_spk = has_speakers(utts)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '文字起こし'
    headers = ['時間帯', '開始', '終了'] + (['話者'] if with_spk else []) + \
              ['発言', '要確認', '信頼度']
    ws.append(headers)
    head_font = Font(bold=True)
    for c in ws[1]:
        c.font = head_font
    yellow = PatternFill(start_color='FFF6C0', end_color='FFF6C0', fill_type='solid')

    prev_band = None
    for u, low in zip(utts, low_flags):
        band_min = int(u.start // 60)
        band = f'{band_min:02d}:00〜{band_min:02d}:59'
        show_band = '' if band == prev_band else band
        prev_band = band
        row = [show_band, fmt_mmss(u.start), fmt_mmss(u.end)]
        if with_spk:
            row.append(u.speaker or UNKNOWN_SPEAKER)
        row += [u.text.strip(), '※要確認' if low else '', round(u.avg_logprob, 2)]
        ws.append(row)
        if low:
            for c in ws[ws.max_row]:
                c.fill = yellow

    widths = [14, 7, 7] + ([10] if with_spk else []) + [70, 9, 8]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(ord('A') + i)].width = w
    text_col = 4 if with_spk else 3
    for row in ws.iter_rows(min_row=2):
        row[text_col].alignment = Alignment(wrap_text=True, vertical='top')
    ws.freeze_panes = 'A2'
    wb.save(path)


# ---- 話者識別（NeMo-Speech.cpp の nemo-speech.exe を別プロセスで実行） ----

def find_diarizer(base):
    """nemo-speech.exe を探す。見つからなければ None。"""
    cands = [os.path.join(base, DIARIZER_DIRNAME, DIARIZER_EXE),
             os.path.join(base, DIARIZER_DIRNAME, 'bin', DIARIZER_EXE)]
    local = os.environ.get('LOCALAPPDATA')
    if local:
        # NeMo-Speech.cpp のインストーラの既定の場所
        cands.append(os.path.join(local, 'Programs', 'NeMoSpeech', 'bin', DIARIZER_EXE))
    cands.append(shutil.which('nemo-speech'))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_diar_model(base):
    """話者識別モデル（GGUF）を探す。見つからなければ None。"""
    d = os.path.join(base, DIAR_MODEL_DIRNAME)
    for name in DIAR_MODEL_PREFERRED:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    try:
        ggufs = sorted(n for n in os.listdir(d) if n.lower().endswith('.gguf'))
    except OSError:
        return None
    return os.path.join(d, ggufs[0]) if ggufs else None


def diar_preset(model_path):
    """一括処理向けの設定名。旧モデル（Sortformer v2, 4人まで）と
    Nemotron 3 Diarization（v3, 8人まで）で名前が違う。"""
    name = os.path.basename(model_path).lower()
    return 'offline' if 'sortformer' in name else 'v3-offline'


# nemo-speech（C++製）はコマンドラインのパスをANSIコードページで受け取るため，
# 日本語を含むパスは「???」に化けて開けない（GitHub Actions の Windows で確認。
# 日本語版Windows（CP932）なら通る可能性もあるが当てにしない）。
# 渡すパスは必ず英数字だけにする。

def ascii_path(path):
    """英数字だけのパスを返す。そのままで駄目なら短い名前（8.3形式）を試す。
    どちらも駄目なら None（8.3形式はドライブの設定で無効なことがある）。"""
    if path.isascii():
        return path
    if os.name == 'nt':
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
            if 0 < n < len(buf) and buf.value.isascii():
                return buf.value
        except Exception:
            pass
    return None


def diar_workdir():
    """話者識別の作業フォルダ（英数字だけのパス）。用意できなければ None。
    変換した会議音声を置くので，まずは本人しか読めない一時フォルダを使う。"""
    cands = [os.path.join(tempfile.gettempdir(), 'rock_on_mj_diar')]
    shared = os.environ.get('ProgramData')
    if shared:
        # 一時フォルダのパスにユーザー名（日本語）が入る場合の逃げ道。音声は使ったら消す
        user = hashlib.sha1(os.path.expanduser('~').encode('utf-8')).hexdigest()[:8]
        cands.append(os.path.join(shared, 'rock_on_mj_diar', user))
    for d in cands:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            continue
        a = ascii_path(d)
        if a:
            return a
    return None


def ascii_model_path(model_path, workdir):
    """モデルのパスが英数字だけでなければ，作業フォルダに英数字の名前で
    ハードリンク（できなければコピー）を作ってそちらを返す。次回からは使い回す。"""
    a = ascii_path(model_path)
    if a:
        return a
    dst = os.path.join(workdir, 'diar_model' + os.path.splitext(model_path)[1])
    try:
        if (os.path.getsize(dst) == os.path.getsize(model_path)
                and os.path.getmtime(dst) >= os.path.getmtime(model_path)):
            return dst
    except OSError:
        pass
    try:
        os.remove(dst)
    except OSError:
        pass
    try:
        os.link(model_path, dst)
    except OSError:
        shutil.copyfile(model_path, dst)   # 別ドライブ等でハードリンクできない
    return dst


def write_wav16k(audio_path, out_path):
    """話者識別用に 16kHz・モノラル・16bit のWAVを書き出す（nemo-speech はWAVのみ対応）。
    戻り値は音声の長さ（秒）。"""
    import numpy as np
    from faster_whisper import decode_audio

    audio = decode_audio(audio_path, sampling_rate=16000)
    with wave.open(out_path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        step = 16000 * 60
        for i in range(0, len(audio), step):
            pcm = (np.clip(audio[i:i + step], -1, 1) * 32767).astype('<i2')
            w.writeframes(pcm.tobytes())
    return len(audio) / 16000.0


def parse_rttm(path):
    """RTTMを読み，[(開始秒, 終了秒, 話者ID), ...] を開始順で返す。"""
    turns = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            parts = line.split()
            if len(parts) < 8 or parts[0] != 'SPEAKER':
                continue
            try:
                start, dur = float(parts[3]), float(parts[4])
            except ValueError:
                continue
            if dur > 0:
                turns.append((start, start + dur, parts[7]))
    turns.sort()
    return turns


class SpeakerTimeline:
    """話者区間の一覧から「この時間帯に話していたのは誰か」を引く。"""

    def __init__(self, turns):
        self.turns = sorted(turns)
        self.starts = [t[0] for t in self.turns]
        self.max_len = max((t[1] - t[0] for t in self.turns), default=0.0)

    def speaker_at(self, t0, t1):
        """t0〜t1 と最も長く重なる話者。重なりが無ければ近くの話者，それも無ければ None。"""
        if not self.turns:
            return None
        lo = bisect.bisect_left(self.starts, t0 - self.max_len)
        hi = bisect.bisect_right(self.starts, t1)
        overlap = {}
        for s, e, spk in self.turns[lo:hi]:
            ov = min(e, t1) - max(s, t0)
            if ov > 0:
                overlap[spk] = overlap.get(spk, 0.0) + ov
        if overlap:
            return max(overlap, key=overlap.get)
        # 重なりが無い（無音の直前・直後など）→ 近い区間の話者
        best, best_gap = None, SPEAKER_NEAREST_SEC
        lo = bisect.bisect_left(self.starts, t0 - self.max_len - SPEAKER_NEAREST_SEC)
        hi = bisect.bisect_right(self.starts, t1 + SPEAKER_NEAREST_SEC)
        for s, e, spk in self.turns[lo:hi]:
            gap = max(s - t1, t0 - e)
            if gap <= best_gap:
                best, best_gap = spk, gap
        return best


def split_by_speaker(seg, timeline):
    """Whisperのセグメントを話者の切り替わりで分ける。
    単語ごとの時刻（word_timestamps）があれば単語単位で，無ければセグメント単位で判定する。
    戻り値の speaker は話者ID（RTTMの値）のまま。表示名は label_speakers() で付ける。"""
    words = [w for w in (getattr(seg, 'words', None) or []) if w.word]
    if not words:
        return [Utterance(seg.start, seg.end, seg.text, seg.avg_logprob,
                          timeline.speaker_at(seg.start, seg.end))]

    labels = [timeline.speaker_at(w.start, w.end) for w in words]
    # 判定できなかった単語は直前（先頭なら直後）の話者に寄せる
    for i in range(1, len(labels)):
        if labels[i] is None:
            labels[i] = labels[i - 1]
    for i in range(len(labels) - 2, -1, -1):
        if labels[i] is None:
            labels[i] = labels[i + 1]
    if labels[0] is None:
        labels = [timeline.speaker_at(seg.start, seg.end)] * len(words)

    runs = []
    for w, spk in zip(words, labels):
        if runs and runs[-1][0] == spk:
            runs[-1][1].append(w)
        else:
            runs.append([spk, [w]])

    def run_len(r):
        return r[1][-1].end - r[1][0].start

    # ごく短い切り替わりは前の話者に吸収（先頭だけは後ろへ）
    merged = []
    for r in runs:
        if merged and (r[0] == merged[-1][0] or run_len(r) < SPEAKER_MIN_RUN_SEC):
            merged[-1][1].extend(r[1])
        else:
            merged.append(r)
    if len(merged) > 1 and run_len(merged[0]) < SPEAKER_MIN_RUN_SEC:
        merged[1][1][:0] = merged[0][1]
        merged.pop(0)

    return [Utterance(ws[0].start, ws[-1].end, ''.join(w.word for w in ws).strip(),
                      seg.avg_logprob, spk)
            for spk, ws in merged]


def label_speakers(utts):
    """話者IDを登場順に「話者A」「話者B」…へ置き換える。判定できなかった発言は「話者不明」。"""
    names = {}
    for u in utts:
        if u.speaker is None:
            u.speaker = UNKNOWN_SPEAKER
            continue
        if u.speaker not in names:
            n = len(names)
            names[u.speaker] = f'話者{chr(ord("A") + n)}' if n < 26 else f'話者{n + 1}'
        u.speaker = names[u.speaker]
    return len(names)


def diarizer_env(exe):
    """nemo-speech を動かす環境変数。model-index.json の場所を教える。"""
    env = dict(os.environ)
    if not env.get('NEMO_SPEECH_MODEL_INDEX'):
        for rel in DIAR_INDEX_CANDIDATES:
            p = os.path.normpath(os.path.join(os.path.dirname(exe), rel))
            if os.path.isfile(p):
                env['NEMO_SPEECH_MODEL_INDEX'] = p
                break
    return env


def run_diarizer(exe, model_path, wav_path, rttm_path, duration, workdir):
    """nemo-speech.exe で話者識別し，RTTMを書き出す。失敗したら例外。
    model_path・wav_path・rttm_path は英数字だけのパスであること（ascii_path 参照）。"""
    err_path = os.path.join(workdir, 'diar_stderr.txt')
    cmd = [exe, 'diarize', wav_path,
           '--model', model_path,
           '--backend', 'cpu', '--preset', diar_preset(model_path),
           '--format', 'rttm', '--output', rttm_path, '--force']
    _emit('log', m=f'  実行: {os.path.basename(exe)} diarize（{diar_preset(model_path)}）')
    timeout = DIAR_TIMEOUT_BASE_SEC + duration * DIAR_TIMEOUT_FACTOR
    start = time.time()
    flags = 0x08000000 if os.name == 'nt' else 0   # CREATE_NO_WINDOW
    with open(err_path, 'w', encoding='utf-8', errors='replace') as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, env=diarizer_env(exe), creationflags=flags)
        last = 0
        while proc.poll() is None:
            time.sleep(0.5)
            elapsed = time.time() - start
            if elapsed > timeout:
                proc.kill()
                proc.wait()
                raise RuntimeError(f'{int(timeout // 60)}分たっても終わらないため中断しました')
            if elapsed - last >= 5:
                last = elapsed
                _emit('status', m=f'話者を識別中...（{fmt_eta(elapsed)}経過）')
    try:
        with open(err_path, encoding='utf-8', errors='replace') as f:
            err_text = f.read()
    except OSError:
        err_text = ''
    # 子の stderr は診断ログにつながっているので，nemo-speech の出力も残しておく
    if err_text:
        try:
            os.write(2, ('---- nemo-speech ----\n' + err_text + '\n').encode('utf-8'))
        except Exception:
            pass
    if proc.returncode != 0:
        for key, hint in DIAR_ERROR_HINTS:
            if key in err_text:
                raise RuntimeError(hint)
        tail = ' / '.join(err_text.strip().splitlines()[-3:])
        raise RuntimeError(f'{exit_reason(proc.returncode)}  {tail}'.strip())
    if not os.path.exists(rttm_path):
        raise RuntimeError('結果ファイル（RTTM）が作られませんでした')
    return time.time() - start


def diarize_step(job):
    """話者識別を行い，話者区間 [(開始, 終了, 話者ID), ...] を返す。
    使えない・失敗したときは None（話者なしで文字起こしを続ける）。
    別設定での再試行時に同じ処理を繰り返さないよう，結果のRTTMがあれば再利用する。"""
    rttm_path = job['rttm_path']
    if os.path.exists(rttm_path):
        _emit('log', m='>>> 話者識別: 前回の試行の結果を再利用します')
        return parse_rttm(rttm_path)

    exe, model = job.get('diarizer_exe'), job.get('diar_model')
    if not exe or not model:
        _emit('log', m='⚠ 話者識別の準備ができていないため，話者なしで文字起こしします')
        return None
    wav_path = job['diar_wav']
    workdir = os.path.dirname(rttm_path)
    try:
        _emit('status', m='話者識別の準備中（音声を変換）...')
        _emit('log', m='>>> 話者識別（誰が話したか）を開始...')
        _emit('log', m=f'  モデル: {os.path.basename(model)}')
        if not (wav_path.isascii() and rttm_path.isascii()):
            raise RuntimeError('作業フォルダを英数字だけの場所に用意できませんでした')
        model = ascii_model_path(model, workdir)
        duration = write_wav16k(job['audio_path'], wav_path)
        need = DIAR_MEM_BASE_GB + DIAR_MEM_PER_MIN_GB * duration / 60
        _, _, commit = memory_gb()
        if commit is not None and commit < need + MEM_COMMIT_MARGIN_GB:
            _emit('log', m=f'⚠ メモリが足りないため話者識別を省略します'
                           f'（空きコミット{commit:.1f}GB / 目安{need + MEM_COMMIT_MARGIN_GB:.1f}GB）')
            _emit('log', m='　→ 話者なしで文字起こしを続けます')
            return None
        _emit('status', m='話者を識別中...')
        elapsed = run_diarizer(exe, model, wav_path, rttm_path, duration, workdir)
        turns = parse_rttm(rttm_path)
        n = len({t[2] for t in turns})
        mm, ss = divmod(int(elapsed), 60)
        _emit('log', m=f'  話者識別 完了: {n}人を検出（処理時間: {mm}分{ss}秒）')
        if not turns:
            _emit('log', m='⚠ 話し声を検出できなかったため，話者なしで出力します')
            return None
        return turns
    except Exception as e:
        try:
            os.write(2, ('話者識別に失敗:\n' + traceback.format_exc()).encode('utf-8'))
        except Exception:
            pass
        _emit('log', m=f'⚠ 話者識別に失敗しました（{e}）')
        _emit('log', m='　→ 話者なしで文字起こしを続けます')
        return None
    finally:
        # 変換した音声は一時ファイル。会議の音声なので残さない
        try:
            os.remove(wav_path)
        except OSError:
            pass


class StreamingWavWriter:
    """録音しながら逐次書き込みできるWAVライタ。

    数秒おきにヘッダのサイズ欄を書き直して flush するため，途中でエラーが
    起きても（アプリが強制終了しても）そこまでの音声がWAVとして再生できる。
    """

    def __init__(self, path, samplerate=16000, channels=1, sampwidth=2,
                 sync_interval=5.0):
        self.path = path
        self.samplerate = samplerate
        self.channels = channels
        self.sampwidth = sampwidth
        self.sync_interval = sync_interval
        self.data_bytes = 0
        self._f = open(path, 'wb')
        self._write_header()
        self._last_sync = time.time()

    def _write_header(self):
        byte_rate = self.samplerate * self.channels * self.sampwidth
        block_align = self.channels * self.sampwidth
        self._f.write(b'RIFF')
        self._f.write(struct.pack('<I', 36 + self.data_bytes))
        self._f.write(b'WAVEfmt ')
        self._f.write(struct.pack('<IHHIIHH', 16, 1, self.channels,
                                  self.samplerate, byte_rate, block_align,
                                  self.sampwidth * 8))
        self._f.write(b'data')
        self._f.write(struct.pack('<I', self.data_bytes))

    def write(self, pcm_bytes):
        if self._f is None:
            return
        self._f.write(pcm_bytes)
        self.data_bytes += len(pcm_bytes)
        if time.time() - self._last_sync >= self.sync_interval:
            self.sync()

    def sync(self):
        """ヘッダのサイズ欄を実データ長に更新してディスクへ書き出す。"""
        if self._f is None:
            return
        pos = self._f.tell()
        self._f.seek(4)
        self._f.write(struct.pack('<I', 36 + self.data_bytes))
        self._f.seek(40)
        self._f.write(struct.pack('<I', self.data_bytes))
        self._f.seek(pos)
        self._f.flush()
        try:
            os.fsync(self._f.fileno())
        except Exception:
            pass
        self._last_sync = time.time()

    def close(self):
        if self._f is None:
            return
        try:
            self.sync()
        except Exception:
            pass
        try:
            self._f.close()
        finally:
            self._f = None

    @property
    def duration(self):
        return self.data_bytes / float(self.samplerate * self.channels * self.sampwidth)


def repair_wav_headers(rec_dir):
    """強制終了などでヘッダが古いままのWAVを修復する（起動時に実行）。"""
    fixed = []
    try:
        names = os.listdir(rec_dir)
    except Exception:
        return fixed
    for name in names:
        if not name.lower().endswith('.wav'):
            continue
        path = os.path.join(rec_dir, name)
        try:
            size = os.path.getsize(path)
            if size <= 44:
                continue
            with open(path, 'r+b') as f:
                if f.read(4) != b'RIFF':
                    continue
                f.seek(36)
                if f.read(4) != b'data':
                    continue  # 標準的な44バイトヘッダ以外は触らない
                declared = struct.unpack('<I', f.read(4))[0]
                actual = size - 44
                if declared >= actual:
                    continue
                f.seek(4)
                f.write(struct.pack('<I', 36 + actual))
                f.seek(40)
                f.write(struct.pack('<I', actual))
            fixed.append(path)
        except Exception:
            continue
    return fixed


# ---- 文字起こしの実行（子プロセス側） ----
#
# CTranslate2 等のネイティブライブラリは，環境によってはアクセス違反で
# プロセスごと落ちる。本体と同じプロセスで動かすとアプリが道連れになるため，
# 文字起こしは別プロセスで実行し，落ちても本体は生き残るようにする。
# 落ちた場合は，使う命令セットや演算エンジンを段階的に落として自動で再試行する。

WORKER_FLAG = '--transcribe-worker'

# 落ちたときに順に試す設定。上から順に「速いが攻めた設定」→「遅いが安全な設定」。
FALLBACKS = [
    ('通常', {}),
    ('MKLを使わない', {'CT2_USE_MKL': '0', 'ONEDNN_MAX_CPU_ISA': 'AVX2'}),
    ('MKLなし・1スレッド', {'CT2_USE_MKL': '0', 'ONEDNN_MAX_CPU_ISA': 'AVX2',
                            'OMP_NUM_THREADS': '1'}),
    ('最も安全（GENERIC）', {'CT2_USE_MKL': '0', 'ONEDNN_MAX_CPU_ISA': 'SSE41',
                             'OMP_NUM_THREADS': '1', 'CT2_FORCE_CPU_ISA': 'GENERIC',
                             'MKL_ENABLE_INSTRUCTIONS': 'SSE4_2'}),
]

EXIT_REASONS = {
    -1073741819: 'アクセス違反 (0xC0000005)',
    -1073741795: '不正な命令 (0xC000001D)',
    -1073741571: 'スタックオーバーフロー (0xC00000FD)',
    -1073740791: 'ヒープの破損 (0xC0000374)',
    -1073741801: 'DLLを読み込めない (0xC0000135)',
}


def exit_reason(code):
    return EXIT_REASONS.get(code, f'終了コード {code}')


def _emit(kind, **kw):
    """親プロセスへ1行のJSONで報告する。
    frozen（コンソール無し）だと sys.stdout が使えないことがあるので，
    ファイル記述子1へ直接書く。"""
    kw['t'] = kind
    data = (json.dumps(kw, ensure_ascii=False) + '\n').encode('utf-8')
    try:
        os.write(1, data)
    except Exception:
        pass


def run_worker(job_path):
    """子プロセスの本体。文字起こしして結果を保存し，経過をJSONで親へ流す。"""
    try:
        err = os.fdopen(2, 'w', buffering=1, encoding='utf-8', errors='replace')
        faulthandler.enable(file=err, all_threads=True)
    except Exception:
        pass
    # utf-8-sig にしておくと，BOM付きで渡されても読める
    # （手で作ったJSONやPowerShellの出力にはBOMが付くことがある）。
    try:
        with open(job_path, encoding='utf-8-sig') as f:
            job = json.load(f)
    except Exception as e:
        _emit('error', m=f'指示ファイルを読めません（{job_path}）: {e}')
        raise SystemExit(2)

    from faster_whisper import WhisperModel

    model_path = job['model_path']
    audio_path = job['audio_path']
    threads = max(1, min(4, os.cpu_count() or 4))

    # 話者識別はWhisperのモデルを読み込む前に済ませる
    # （両方のモデルを同時にメモリへ載せず，ピークを会議録音版と同じに保つ）
    turns = diarize_step(job) if job.get('diarize') else None

    _emit('log', m='>>> モデル読み込み中...')
    _emit('log', m=f'  モデル: {model_path}')
    _emit('status', m='モデル読み込み中...')
    model = WhisperModel(model_path, device='cpu', compute_type='int8',
                         cpu_threads=threads, num_workers=1)
    _emit('log', m='  読み込み完了')

    _emit('status', m='文字起こし中（しばらくかかります）...')
    _emit('log', m='>>> 文字起こし開始...')
    start = time.time()
    segments, info = model.transcribe(
        audio_path, language=job.get('language', 'ja'), beam_size=5,
        initial_prompt=job['prompt'],
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,
        no_repeat_ngram_size=3,
        # 話者の切り替わりで発言を分けるため，話者識別するときは単語ごとの時刻も取る
        word_timestamps=bool(turns))

    duration = info.duration or 0
    _emit('log', m=f'  音声長: {int(duration // 60)}分{int(duration % 60)}秒')
    _emit('mode', m='determinate')

    segs = []
    for seg in segments:
        segs.append(seg)
        _emit('log', m=seg.text.strip())
        if duration > 0 and seg.end > 0:
            pct = min(seg.end / duration * 100, 100)
            elapsed = time.time() - start
            eta = elapsed * (duration - seg.end) / seg.end
            _emit('progress', v=pct)
            _emit('status', m=f'文字起こし中... {int(pct)}%  残り約{fmt_eta(eta)}')

    _emit('progress', v=100)
    if turns:
        timeline = SpeakerTimeline(turns)
        utts = []
        for seg in segs:
            utts.extend(split_by_speaker(seg, timeline))
        label_speakers(utts)
    else:
        utts = [Utterance(s.start, s.end, s.text, s.avg_logprob) for s in segs]
    full_text = build_full_text(utts)
    low_flags = [u.avg_logprob < LOW_CONF_LOGPROB for u in utts]
    low_n = sum(low_flags)

    elapsed = time.time() - start
    mm, ss = divmod(int(elapsed), 60)
    _emit('status', m=f'完了（処理時間: {mm}分{ss}秒）')
    _emit('log', m=f'\n処理時間: {mm}分{ss}秒')
    if low_n:
        _emit('log', m=f'※要確認の箇所: {low_n}件（低信頼度）')
    if has_speakers(utts):
        _emit('log', m='\n=== 話者ごとの発言時間 ===')
        for spk, sec, n in speaker_summary(utts):
            _emit('log', m=f'{spk}: {fmt_mmss(sec)}（{n}回）')
    _emit('log', m='\n=== 文字起こし結果 ===')
    _emit('log', m=full_text)

    # 別名保存ボタン用のテキスト（親が読み戻す）
    result_path = job['result_path']
    with open(result_path, 'w', encoding='utf-8') as f:
        f.write(format_txt(full_text, utts, low_flags))

    base = os.path.splitext(audio_path)[0]
    audio_name = os.path.basename(audio_path)
    saved = []
    if job['out_txt']:
        p = base + '_文字起こし.txt'
        save_txt(p, full_text, utts, low_flags)
        saved.append(p)
    if job['out_srt']:
        p = base + '.srt'
        save_srt(p, utts)
        saved.append(p)
    if job['out_docx']:
        p = base + '_文字起こし.docx'
        save_docx(p, full_text, utts, low_flags, audio_name)
        saved.append(p)
    if job['out_xlsx']:
        p = base + '_文字起こし.xlsx'
        save_xlsx(p, utts, low_flags)
        saved.append(p)

    _emit('done', saved=saved, result_path=result_path)


class WhisperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('ROCK ON MJ  －  会議録音＆文字起こし（話者識別版）')
        try:
            self.iconbitmap(resource_path('rock_on_mj.ico'))
        except Exception:
            pass
        self.resizable(False, False)
        self.configure(bg='#f0f0f0')
        self._result_text = ''
        self._running = False
        self._recording = False
        self._rec_writer = None
        self._rec_lock = threading.Lock()
        # ワーカースレッドからのGUI操作はこのキュー経由でメインスレッドが実行する
        self._ui_queue = queue.Queue()
        self._build_ui()

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.report_callback_exception = self._on_tk_error
        self.after(40, self._pump)

        base = app_base_dir()
        ensure_dict_template(base)
        # 前回，強制終了などでヘッダが未更新のまま残ったWAVを修復
        for p in repair_wav_headers(os.path.join(base, '会議録音')):
            self._log(f'⚠ 前回の録音ファイルを修復しました: {p}')
        # デフォルトのモデル（models配下を精度の高い順に自動選択）
        # kotoba-whisper（日本語特化）を最優先。無ければ従来のlarge系にフォールバック。
        for name in ('kotoba-whisper-v2.0', 'large-v3-turbo', 'large-v3', 'medium', 'small'):
            cand = os.path.join(base, 'models', name)
            if os.path.isdir(cand):
                self.model_var.set(cand)
                break

    def _build_ui(self):
        pad = dict(padx=12, pady=6)

        tk.Label(self, text='🎤 ROCK ON MJ  話者識別版',
                 font=('メイリオ', 14, 'bold'), bg='#f0f0f0'
                 ).grid(row=0, column=0, columnspan=3, pady=(14, 2))
        tk.Label(self, text='録音＆文字起こし  ローカル処理（録音データは外部送信されません）',
                 font=('メイリオ', 9), fg='gray', bg='#f0f0f0'
                 ).grid(row=1, column=0, columnspan=3, pady=(0, 8))

        ttk.Separator(self, orient='horizontal').grid(
            row=2, column=0, columnspan=3, sticky='ew', padx=12, pady=4)

        # モデルフォルダ
        tk.Label(self, text='モデルフォルダ',
                 font=('メイリオ', 10), bg='#f0f0f0'
                 ).grid(row=3, column=0, sticky='w', **pad)
        self.model_var = tk.StringVar()
        tk.Entry(self, textvariable=self.model_var, width=46, font=('メイリオ', 9)
                 ).grid(row=3, column=1, sticky='ew', padx=(0, 4), pady=6)
        tk.Button(self, text='参照', command=self._browse_model, width=6
                  ).grid(row=3, column=2, padx=(0, 12))

        # 音声ファイル
        tk.Label(self, text='音声ファイル',
                 font=('メイリオ', 10), bg='#f0f0f0'
                 ).grid(row=4, column=0, sticky='w', **pad)
        self.audio_var = tk.StringVar()
        tk.Entry(self, textvariable=self.audio_var, width=46, font=('メイリオ', 9)
                 ).grid(row=4, column=1, sticky='ew', padx=(0, 4), pady=6)
        tk.Button(self, text='参照', command=self._browse_audio, width=6
                  ).grid(row=4, column=2, padx=(0, 12))

        tk.Label(self, text='対応形式: WAV / M4A / MP3 / FLAC / MP4 対応（ffmpeg不要）',
                 font=('メイリオ', 8), fg='gray', bg='#f0f0f0'
                 ).grid(row=5, column=0, columnspan=3, sticky='w', padx=14, pady=(0, 4))

        # 会議録音（相手の声＝スピーカー/ヘッドセットに流れる音をキャプチャ）
        rec_frame = tk.Frame(self, bg='#eef4fb', bd=1, relief='groove')
        rec_frame.grid(row=6, column=0, columnspan=3, sticky='ew', padx=12, pady=(2, 4))
        tk.Label(rec_frame, text='🔴 Web会議を録音（相手の声をキャプチャ）',
                 font=('メイリオ', 10, 'bold'), bg='#eef4fb'
                 ).grid(row=0, column=0, columnspan=3, sticky='w', padx=8, pady=(6, 2))
        tk.Label(rec_frame, text='録音元:', font=('メイリオ', 9), bg='#eef4fb'
                 ).grid(row=1, column=0, sticky='e', padx=(8, 2))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(rec_frame, textvariable=self.device_var,
                                          width=38, state='readonly', font=('メイリオ', 9))
        self.device_combo.grid(row=1, column=1, sticky='w', pady=4)
        self.rec_btn = tk.Button(rec_frame, text='● 録音開始',
                                 font=('メイリオ', 10, 'bold'),
                                 bg='#c00000', fg='white', relief='flat',
                                 padx=12, pady=4, command=self._toggle_record)
        self.rec_btn.grid(row=1, column=2, padx=8)
        self.rec_time_var = tk.StringVar(value='')
        tk.Label(rec_frame, textvariable=self.rec_time_var, font=('メイリオ', 9),
                 fg='#c00000', bg='#eef4fb'
                 ).grid(row=2, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 6))
        self._populate_devices()

        # 出力形式
        out_frame = tk.Frame(self, bg='#f0f0f0')
        out_frame.grid(row=7, column=0, columnspan=3, sticky='w', padx=14, pady=(0, 4))
        tk.Label(out_frame, text='出力形式:', font=('メイリオ', 9), bg='#f0f0f0').pack(side='left')
        self.out_txt = tk.BooleanVar(value=True)
        self.out_srt = tk.BooleanVar(value=True)
        self.out_docx = tk.BooleanVar(value=True)
        self.out_xlsx = tk.BooleanVar(value=True)
        for text, var in (('テキスト(.txt)', self.out_txt), ('字幕(.srt)', self.out_srt),
                          ('Word(.docx)', self.out_docx), ('Excel(.xlsx)', self.out_xlsx)):
            tk.Checkbutton(out_frame, text=text, variable=var, font=('メイリオ', 9),
                           bg='#f0f0f0', activebackground='#f0f0f0').pack(side='left', padx=2)

        # 話者識別
        spk_frame = tk.Frame(self, bg='#f0f0f0')
        spk_frame.grid(row=8, column=0, columnspan=3, sticky='w', padx=14, pady=(0, 4))
        self.diarize_var = tk.BooleanVar(value=False)
        self.diarize_chk = tk.Checkbutton(
            spk_frame, text='話者を区別する（話者A・話者B…）', variable=self.diarize_var,
            font=('メイリオ', 9), bg='#f0f0f0', activebackground='#f0f0f0')
        self.diarize_chk.pack(side='top', anchor='w')
        self.diarize_info = tk.StringVar(value='')
        tk.Label(spk_frame, textvariable=self.diarize_info, font=('メイリオ', 8),
                 fg='gray', bg='#f0f0f0').pack(side='top', anchor='w', padx=22)
        self._refresh_diarizer(initial=True)

        ttk.Separator(self, orient='horizontal').grid(
            row=9, column=0, columnspan=3, sticky='ew', padx=12, pady=4)

        # 進捗
        self.status_var = tk.StringVar(value='待機中')
        tk.Label(self, textvariable=self.status_var, font=('メイリオ', 9),
                 bg='#f0f0f0', anchor='w'
                 ).grid(row=10, column=0, columnspan=3, sticky='w', padx=14, pady=(4, 2))
        self.progress = ttk.Progressbar(self, length=500, mode='indeterminate')
        self.progress.grid(row=11, column=0, columnspan=3, padx=14, pady=(0, 8))

        # ログ
        self.log = scrolledtext.ScrolledText(
            self, width=66, height=14, font=('ＭＳ ゴシック', 9), state='disabled')
        self.log.grid(row=12, column=0, columnspan=3, padx=12, pady=4)

        # ボタン
        btn_frame = tk.Frame(self, bg='#f0f0f0')
        btn_frame.grid(row=13, column=0, columnspan=3, pady=12)
        self.start_btn = tk.Button(
            btn_frame, text='　文字起こし開始　',
            font=('メイリオ', 11, 'bold'),
            bg='#0078d4', fg='white', relief='flat',
            padx=16, pady=6, command=self._start)
        self.start_btn.pack(side='left', padx=8)
        self.save_btn = tk.Button(
            btn_frame, text='　結果を別名保存　',
            font=('メイリオ', 11), state='disabled',
            padx=16, pady=6, command=self._save)
        self.save_btn.pack(side='left', padx=8)

    def _refresh_diarizer(self, initial=False):
        """話者識別に必要なもの（nemo-speech.exe とモデル）がそろっているか調べて表示する。"""
        base = app_base_dir()
        self._diarizer_exe = find_diarizer(base)
        self._diar_model = find_diar_model(base)
        ready = bool(self._diarizer_exe and self._diar_model)
        if ready:
            self.diarize_info.set(f'モデル: {os.path.basename(self._diar_model)}')
            self.diarize_chk.configure(state='normal')
            if initial:
                self.diarize_var.set(True)
        else:
            missing = []
            if not self._diarizer_exe:
                missing.append(f'{DIARIZER_DIRNAME}\\{DIARIZER_EXE}')
            if not self._diar_model:
                missing.append(f'{DIAR_MODEL_DIRNAME}\\*.gguf')
            self.diarize_info.set('未設定: ' + ' と '.join(missing) + ' を置いてください')
            self.diarize_var.set(False)
            self.diarize_chk.configure(state='disabled')
        if initial:
            diag(f'話者識別: exe={self._diarizer_exe}  モデル={self._diar_model}')
        return ready

    def _browse_model(self):
        path = filedialog.askdirectory(title='モデルフォルダを選択')
        if path:
            self.model_var.set(path)

    def _browse_audio(self):
        path = filedialog.askopenfilename(
            title='音声ファイルを選択',
            filetypes=[('音声・動画ファイル', '*.wav *.m4a *.mp3 *.flac *.mp4'),
                       ('すべてのファイル', '*.*')])
        if path:
            self.audio_var.set(path)

    # ---- 会議録音（ループバック） ----

    def _populate_devices(self):
        """再生デバイス（スピーカー/ヘッドセット）のループバック一覧を列挙。"""
        self._loopback_names = []
        try:
            import soundcard as sc
            default_name = str(sc.default_speaker().name)
            for m in sc.all_microphones(include_loopback=True):
                if getattr(m, 'isloopback', False):
                    self._loopback_names.append(m.name)
            self.device_combo['values'] = self._loopback_names
            if default_name in self._loopback_names:
                self.device_var.set(default_name)
            elif self._loopback_names:
                self.device_var.set(self._loopback_names[0])
        except Exception as e:
            self.device_combo['values'] = []
            self.device_var.set('録音機能を利用できません')
            self.rec_btn.configure(state='disabled')
            self._rec_error = str(e)

    def _toggle_record(self):
        if not self._recording:
            self._begin_record()
        else:
            self._end_record()

    def _begin_record(self):
        dev = self.device_var.get().strip()
        if not dev or dev == '録音機能を利用できません':
            messagebox.showerror('エラー', '録音元デバイスを選択してください。')
            return

        # 先に保存先WAVを開き，録音しながら逐次書き込む。
        # こうしておくと途中でエラーになってもそこまでの音声が残る。
        import datetime
        rec_dir = os.path.join(app_base_dir(), '会議録音')
        try:
            os.makedirs(rec_dir, exist_ok=True)
            fname = datetime.datetime.now().strftime('会議_%Y%m%d_%H%M%S.wav')
            path = os.path.join(rec_dir, fname)
            writer = StreamingWavWriter(path)
        except Exception as e:
            messagebox.showerror('エラー', '録音ファイルを作成できませんでした。\n' + str(e))
            return
        with self._rec_lock:
            self._rec_writer = writer

        self._recording = True
        self._rec_start = time.time()
        self.rec_btn.configure(text='■ 録音停止', bg='#404040')
        self.start_btn.configure(state='disabled')
        self.device_combo.configure(state='disabled')
        self._log('>>> 録音開始（相手の声をキャプチャ中... 会議終了後に「録音停止」）')
        self._log(f'    保存先: {path}  ※録音しながら随時保存しています')
        self._update_rec_time()
        threading.Thread(target=self._record_loop, args=(dev,), daemon=True).start()

    # 録音が途切れたときに再接続を試みる回数（連続失敗回数）
    MAX_REC_RETRY = 5

    def _record_loop(self, dev_name):
        import traceback
        import numpy as np
        import soundcard as sc

        fails = 0
        while self._recording:
            got = 0          # このセッションで取れたチャンク数（1個＝0.5秒）
            try:
                loop = sc.get_microphone(str(dev_name), include_loopback=True)
                rec_ctx = loop.recorder(samplerate=16000, channels=1)
                rec = rec_ctx.__enter__()
                try:
                    while self._recording:
                        chunk = np.asarray(rec.record(numframes=8000)).reshape(-1)
                        got += 1
                        if got == 1 and fails:
                            self._post(lambda: self._log(
                                '　→ 録音を再開しました（ここまでの音声は保存済みです）'))
                        pcm16 = (np.clip(chunk, -1, 1) * 32767).astype('<i2')
                        with self._rec_lock:
                            if self._rec_writer is None:
                                return
                            self._rec_writer.write(pcm16.tobytes())
                finally:
                    # recorder の後始末自体が例外を投げることがある
                    # （Error 0x100000001 等）ので握りつぶす
                    try:
                        rec_ctx.__exit__(None, None, None)
                    except Exception:
                        pass
            except Exception:
                if not self._recording:
                    break
                # 10秒以上録れていたなら，前の失敗とは別件の一時的な事象とみなす
                fails = 1 if got >= 20 else fails + 1
                err = traceback.format_exc()
                short = err.strip().splitlines()[-1]
                if fails > self.MAX_REC_RETRY:
                    self._recording = False
                    self._post(lambda err=err: self._log('\n❌ 録音エラー:\n' + err))
                    self._post(self._record_failed)
                    return
                self._post(lambda short=short, fails=fails: self._log(
                    f'⚠ 録音が中断されました（{short}）'
                    f'\n　 再接続を試みます... ({fails}/{self.MAX_REC_RETRY})'))
                time.sleep(1.0)

    def _record_failed(self):
        """録音スレッドが復旧できなかったとき（GUIスレッドで実行）。"""
        self.rec_btn.configure(text='● 録音開始', bg='#c00000')
        self.rec_time_var.set('')
        self.device_combo.configure(state='readonly')
        self._log('>>> 録音を終了し，ここまでの音声を保存します...')
        threading.Thread(target=self._finish_record,
                         kwargs=dict(auto_transcribe=False, partial=True),
                         daemon=True).start()

    def _update_rec_time(self):
        if self._recording:
            el = int(time.time() - self._rec_start)
            m, s = divmod(el, 60)
            self.rec_time_var.set(f'● 録音中… {m:02d}:{s:02d}')
            self.after(500, self._update_rec_time)

    def _end_record(self):
        self._recording = False
        self.rec_btn.configure(text='● 録音開始', bg='#c00000')
        self.rec_time_var.set('')
        self.device_combo.configure(state='readonly')
        self._log('>>> 録音停止。保存して文字起こしを開始します...')
        threading.Thread(target=self._finish_record,
                         kwargs=dict(auto_transcribe=True),
                         daemon=True).start()

    def _close_writer(self):
        """WAVを閉じてパスと録音長を返す。"""
        with self._rec_lock:
            writer = self._rec_writer
            self._rec_writer = None
        if writer is None:
            return None, 0.0
        dur = writer.duration
        writer.close()
        return writer.path, dur

    def _finish_record(self, auto_transcribe=True, partial=False):
        time.sleep(0.4)  # 録音ループの書き込み待ち
        path, dur = self._close_writer()
        if not path or dur < 0.1:
            self._post(lambda: self._log('録音データがありません。'))
            self._post(lambda: self.start_btn.configure(state='normal'))
            return
        head = '⚠ 途中までの録音を保存しました' if partial else '✅ 録音を保存'
        self._post(lambda: self._log(
            f'{head}: {path}  （{int(dur // 60)}分{int(dur % 60)}秒）'))
        self._post(lambda: self.audio_var.set(path))
        self._post(lambda: self.start_btn.configure(state='normal'))
        if auto_transcribe:
            self._post(self._start)
        elif partial:
            self._post(lambda: self._ask_transcribe_partial(path, dur))

    def _ask_transcribe_partial(self, path, dur):
        if messagebox.askyesno(
                '録音が中断されました',
                '録音中にエラーが発生しましたが，途中までの音声は保存されています。\n\n'
                f'{path}\n'
                f'（{int(dur // 60)}分{int(dur % 60)}秒）\n\n'
                'この音声の文字起こしを開始しますか？'):
            self._start()

    def _on_close(self):
        if self._recording:
            if not messagebox.askyesno(
                    '確認',
                    '録音中です。終了してよろしいですか？\n'
                    '（ここまでの録音は「会議録音」フォルダに保存されます）'):
                return
            self._recording = False
            time.sleep(0.5)  # 録音ループの書き込み待ち
        try:
            path, dur = self._close_writer()
        except Exception:
            path = None
        self.destroy()

    # ---- スレッド間のGUI受け渡し ----

    def _post(self, fn):
        """ワーカースレッドからのGUI操作を予約する（Tkはメインスレッドでのみ触る）。"""
        try:
            self._ui_queue.put_nowait(fn)
        except Exception:
            pass

    def _pump(self):
        for _ in range(300):
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                diag('GUI更新でエラー:\n' + traceback.format_exc())
        self.after(40, self._pump)

    def _on_tk_error(self, exc, val, tb):
        diag('Tkコールバックでエラー:\n' + ''.join(traceback.format_exception(exc, val, tb)))

    def _log(self, msg):
        # 画面表示と同じ内容を診断ログにも残す（強制終了時の「最後の一行」になる）
        diag(msg)
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _set_status(self, msg):
        self._post(lambda: self.status_var.set(msg))

    def _set_progress(self, value):
        self._post(lambda: self.progress.configure(value=value))

    def _preflight(self, model_path, audio_path):
        """開始前の環境チェック。落ちる前に原因を伝えるのが目的。"""
        model_bin = os.path.join(model_path, 'model.bin')
        try:
            bin_size = os.path.getsize(model_bin)
        except OSError:
            diag(f'model.bin が読めません: {model_bin}')
            messagebox.showerror('エラー',
                'モデルファイル（model.bin）が見つかりません。\n\n'
                f'{model_path}\n\n'
                'models フォルダをコピーし忘れているか，コピーが途中で失敗しています。')
            return False

        total, avail, commit = memory_gb()
        mem_note = (f'  空き{avail:.1f}GB / 空きコミット{commit:.1f}GB'
                    if avail is not None else '')
        diag(f'開始前チェック: model.bin={bin_size / GB:.2f}GB  音声={audio_path}{mem_note}')

        if bin_size < 10 * 1024 * 1024:
            messagebox.showerror('エラー',
                'モデルファイル（model.bin）のサイズが小さすぎます。\n'
                f'現在: {bin_size / (1024 * 1024):.0f}MB\n\n'
                'コピーが途中で終わっている可能性があります。'
                'models フォルダをコピーし直してください。')
            return False

        if is_network_path(model_path) or is_network_path(app_base_dir()):
            if not messagebox.askyesno('確認',
                    'ツールまたはモデルがネットワーク上（共有フォルダ）にあります。\n'
                    'ネットワーク越しだと，読み込みの途中で通信が途切れたときに\n'
                    'アプリが強制終了することがあります。\n\n'
                    'フォルダごとPC内（デスクトップ等）にコピーしてからのご利用を'
                    'おすすめします。\n\n'
                    'このまま実行しますか？'):
                return False

        if avail is not None:
            model_gb = bin_size / GB * MEM_FACTOR
            need_phys = model_gb + MEM_PHYS_MARGIN_GB
            need_commit = model_gb + MEM_COMMIT_MARGIN_GB
            short = []
            if avail < need_phys:
                short.append(f'　空きメモリ　　　: {avail:.1f}GB（目安 {need_phys:.1f}GB）')
            if commit < need_commit:
                short.append(f'　仮想メモリの余裕: {commit:.1f}GB（目安 {need_commit:.1f}GB）')
            if short:
                diag('メモリ不足の警告を表示: ' + ' / '.join(short))
                if not messagebox.askyesno('メモリが不足しています',
                        'メモリの空きが足りません。\n\n'
                        + '\n'.join(short)
                        + '\n\nこのまま実行すると，途中でアプリが何も表示せずに'
                          '終了することがあります。\n'
                          '他のアプリ（ブラウザ・Teams・Excel等）を閉じてから'
                          'もう一度お試しください。\n\n'
                          'このまま実行しますか？'):
                    return False
        return True

    def _start(self):
        model_path = self.model_var.get().strip()
        audio_path = self.audio_var.get().strip()

        if not model_path or not os.path.isdir(model_path):
            messagebox.showerror('エラー',
                'モデルフォルダが見つかりません。\n'
                '「参照」ボタンでモデルフォルダ（model.bin 等を含むフォルダ）を選択してください。')
            return
        if not audio_path or not os.path.exists(audio_path):
            messagebox.showerror('エラー',
                '音声ファイルが見つかりません。\n'
                '「参照」ボタンで音声ファイルを選択してください。')
            return
        if not self._preflight(model_path, audio_path):
            return
        # 起動後に diarizer フォルダやモデルを置いた場合にも気付けるよう，毎回確かめる
        self._refresh_diarizer()

        self.start_btn.configure(state='disabled')
        self.save_btn.configure(state='disabled')
        self._result_text = ''
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)

        threading.Thread(target=self._run, args=(model_path, audio_path), daemon=True).start()

    def _spawn_worker(self, job_path, overrides):
        """文字起こしの子プロセスを起動し，報告を画面に流す。
        戻り値: (成功したか, 保存したファイル, 別名保存用テキストのパス)"""
        env = dict(os.environ)
        env.update(overrides)
        env['PYTHONIOENCODING'] = 'utf-8'
        cmd = [sys.executable, WORKER_FLAG, job_path]
        if not getattr(sys, 'frozen', False):
            cmd = [sys.executable, os.path.abspath(__file__), WORKER_FLAG, job_path]
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE,
            stderr=(_diag_file or subprocess.DEVNULL),
            env=env, creationflags=CREATE_NO_WINDOW, bufsize=1,
            universal_newlines=True, encoding='utf-8', errors='replace')
        self._proc = proc
        saved, result_path, ok = [], None, False
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                diag('子プロセスからの不明な出力: ' + line[:200])
                continue
            kind = msg.get('t')
            if kind == 'log':
                self._post(lambda m=msg['m']: self._log(m))
            elif kind == 'status':
                self._set_status(msg['m'])
            elif kind == 'progress':
                self._set_progress(msg['v'])
            elif kind == 'mode':
                self._post(self.progress.stop)
                self._post(lambda: self.progress.configure(
                    mode='determinate', maximum=100, value=0))
            elif kind == 'error':
                diag('子プロセスからのエラー: ' + msg.get('m', ''))
                self._post(lambda m=msg.get('m', ''): self._log(f'  ❌ {m}'))
            elif kind == 'done':
                saved, result_path, ok = msg['saved'], msg['result_path'], True
        proc.wait()
        self._proc = None
        return (ok and proc.returncode == 0), saved, result_path, proc.returncode

    def _run(self, model_path, audio_path):
        try:
            # 配布時のモデルと同じか照合（展開失敗・コピー破損の早期発見）
            self._set_status('モデルファイルを確認中...')
            ok, cmsg = check_model_file(model_path)
            diag(f'モデル照合: {cmsg}')
            if ok is False:
                self._set_status('モデルファイルが壊れています')
                self._post(lambda: self._log(
                    f'\n❌ モデルファイルが壊れています（{cmsg}）。\n'
                    '　 ZIPをダウンロードし直し，新しいフォルダに展開してください。'))
                self._post(lambda: messagebox.showerror(
                    'モデルファイルが壊れています',
                    'モデルファイル（model.bin）が配布時のものと違います。\n\n'
                    'ZIPのダウンロードか展開が途中で失敗している可能性があります。\n'
                    'ZIPをダウンロードし直し，新しいフォルダに展開してからお試しください。'))
                return

            # 文字起こしは別プロセスで実行する。落ちても本体は生き残り，
            # 設定を段階的に落として自動で再試行できる。
            # 作業ファイルは一時フォルダへ。文字起こし全文が入るので，
            # ツールのフォルダには置かない（配布物に紛れ込むと情報漏れになる）。
            workdir = os.path.join(tempfile.gettempdir(), '文字起こしツール_話者識別')
            os.makedirs(workdir, exist_ok=True)
            work = os.path.join(workdir, '_job.json')
            result_path = os.path.join(workdir, '_result.txt')
            # 用意できないときは従来の作業フォルダ（日本語を含みうる）。
            # その場合 diarize_step が気付いて話者なしで続行する。
            diar_dir = diar_workdir() or workdir
            job = dict(model_path=model_path, audio_path=audio_path,
                       prompt=build_initial_prompt(app_base_dir()),
                       result_path=result_path,
                       out_txt=self.out_txt.get(), out_srt=self.out_srt.get(),
                       out_docx=self.out_docx.get(), out_xlsx=self.out_xlsx.get(),
                       diarize=self.diarize_var.get(),
                       diarizer_exe=self._diarizer_exe, diar_model=self._diar_model,
                       # nemo-speech に渡すパスは英数字だけにする（ascii_path 参照）
                       diar_wav=os.path.join(diar_dir, 'diar_input.wav'),
                       rttm_path=os.path.join(diar_dir, 'diar_result.rttm'))
            with open(work, 'w', encoding='utf-8') as f:
                json.dump(job, f, ensure_ascii=False)
            # 前回の話者識別の結果が残っていると，別の音声に使い回してしまう
            for stale in (job['rttm_path'], job['diar_wav']):
                try:
                    os.remove(stale)
                except OSError:
                    pass

            ok = False
            for i, (name, overrides) in enumerate(FALLBACKS):
                if i:
                    self._post(lambda n=name: self._log(
                        f'\n⚠ 設定を変えて再試行します（{n}）...'))
                diag(f'子プロセス起動: {name} {overrides or "（追加設定なし）"}')
                ok, saved, rpath, code = self._spawn_worker(work, overrides)
                if ok:
                    diag(f'成功した設定: {name}')
                    if i:
                        self._post(lambda n=name: self._log(
                            f'　→ この設定で成功しました: {n}\n'
                            '　 うまく動かないときは，この行を添えてお問い合わせください。'))
                    break
                reason = exit_reason(code)
                diag(f'子プロセスが異常終了: {name} → {reason}')
                self._post(lambda r=reason: self._log(f'　→ 異常終了しました（{r}）'))

            if not ok:
                self._set_status('文字起こしに失敗しました')
                self._post(lambda: self._log(
                    '\n❌ どの設定でも文字起こしを完了できませんでした。\n'
                    '　 診断ログ.txt を添えてお問い合わせください。'))
                self._post(lambda: messagebox.showerror(
                    '文字起こしに失敗しました',
                    'このPCでは文字起こし処理が異常終了します。\n\n'
                    '設定を変えて4通り試しましたが，いずれも完了できませんでした。\n'
                    'ツールのフォルダにある「診断ログ.txt」を添えて'
                    'お問い合わせください。'))
                return

            try:
                with open(rpath, encoding='utf-8') as f:
                    self._result_text = f.read()
            except Exception:
                self._result_text = ''
            self._post(lambda: self._log('\n✅ 保存しました:'))
            for p in saved:
                self._post(lambda pp=p: self._log(f'  {pp}'))
            self._post(lambda: self.save_btn.configure(state='normal'))
            return

        except MemoryError:
            diag('MemoryError:\n' + traceback.format_exc())
            self._set_status('メモリ不足で中断しました')
            self._post(lambda: self._log(
                '\n❌ メモリが足りず，処理を中断しました。\n'
                '　 他のアプリ（ブラウザ・Teams・Excel等）を閉じてからお試しください。'))
        except Exception:
            err = traceback.format_exc()
            diag('文字起こしでエラー:\n' + err)
            self._set_status('エラーが発生しました')
            self._post(lambda: self._log(f'\n❌ エラー:\n{err}'))
        finally:
            self._post(self.progress.stop)
            self._post(lambda: self.progress.configure(mode='determinate'))
            self._post(lambda: self.start_btn.configure(state='normal'))

    def _save(self):
        if not self._result_text:
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.txt',
            filetypes=[('テキストファイル', '*.txt'), ('すべてのファイル', '*.*')],
            title='名前をつけて保存')
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self._result_text)
            messagebox.showinfo('保存完了', f'保存しました:\n{path}')


if __name__ == '__main__':
    # 文字起こしの実行役として呼ばれた場合（親プロセスからの再入）。
    # 画面は出さず，結果をJSONで親へ返して終わる。
    if WORKER_FLAG in sys.argv:
        run_worker(sys.argv[sys.argv.index(WORKER_FLAG) + 1])
        sys.exit(0)

    log_path = setup_diag()
    log_environment()
    try:
        app = WhisperApp()
        if log_path:
            app._log(f'診断ログ: {log_path}')
        app.mainloop()
    except Exception:
        diag('起動できませんでした:\n' + traceback.format_exc())
        try:
            messagebox.showerror('エラー',
                '起動時にエラーが発生しました。\n\n'
                + traceback.format_exc()
                + (f'\n詳細: {log_path}' if log_path else ''))
        except Exception:
            pass
        raise
    diag('==== 正常終了 ====')
