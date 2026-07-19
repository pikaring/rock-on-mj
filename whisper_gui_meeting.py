import sys
import os
import ctypes
import faulthandler
import json
import platform
import queue
import struct
import subprocess
import tempfile
import threading
import time
import traceback
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


# ---- 出力ファイル生成 ----

def save_txt(path, full_text, segs, low_flags):
    lines = [full_text]
    if segs:
        lines.append('\n\n=== タイムスタンプ付き ===')
        for seg, low in zip(segs, low_flags):
            mark = '  ※要確認' if low else ''
            lines.append(f'[{fmt_mmss(seg.start)} - {fmt_mmss(seg.end)}] {seg.text.strip()}{mark}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def save_srt(path, segs):
    with open(path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(segs, 1):
            f.write(f'{i}\n{fmt_srt(seg.start)} --> {fmt_srt(seg.end)}\n{seg.text.strip()}\n\n')


def save_docx(path, full_text, segs, low_flags, audio_name):
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.add_heading('文字起こし結果', level=0)
    p = doc.add_paragraph()
    run = p.add_run(f'対象ファイル: {audio_name}')
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    doc.add_heading('全文', level=1)
    doc.add_paragraph(full_text)

    doc.add_heading('タイムスタンプ付き', level=1)
    for seg, low in zip(segs, low_flags):
        para = doc.add_paragraph()
        tstamp = para.add_run(f'[{fmt_mmss(seg.start)} - {fmt_mmss(seg.end)}] ')
        tstamp.font.color.rgb = RGBColor(0x00, 0x78, 0xD4)
        para.add_run(seg.text.strip())
        if low:
            warn = para.add_run('  ※要確認')
            warn.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            warn.bold = True
    doc.save(path)


def save_xlsx(path, segs, low_flags):
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '文字起こし'
    headers = ['時間帯', '開始', '終了', '発言', '要確認', '信頼度']
    ws.append(headers)
    head_font = Font(bold=True)
    for c in ws[1]:
        c.font = head_font
    yellow = PatternFill(start_color='FFF6C0', end_color='FFF6C0', fill_type='solid')

    prev_band = None
    for seg, low in zip(segs, low_flags):
        band_min = int(seg.start // 60)
        band = f'{band_min:02d}:00〜{band_min:02d}:59'
        show_band = '' if band == prev_band else band
        prev_band = band
        row = [show_band, fmt_mmss(seg.start), fmt_mmss(seg.end),
               seg.text.strip(), '※要確認' if low else '', round(seg.avg_logprob, 2)]
        ws.append(row)
        if low:
            for c in ws[ws.max_row]:
                c.fill = yellow

    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 7
    ws.column_dimensions['C'].width = 7
    ws.column_dimensions['D'].width = 70
    ws.column_dimensions['E'].width = 9
    ws.column_dimensions['F'].width = 8
    for row in ws.iter_rows(min_row=2):
        row[3].alignment = Alignment(wrap_text=True, vertical='top')
    wb.save(path)


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
        audio_path, language='ja', beam_size=5,
        initial_prompt=job['prompt'],
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,
        no_repeat_ngram_size=3)

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
    full_text = ''.join(s.text for s in segs)
    low_flags = [s.avg_logprob < LOW_CONF_LOGPROB for s in segs]
    low_n = sum(low_flags)

    elapsed = time.time() - start
    mm, ss = divmod(int(elapsed), 60)
    _emit('status', m=f'完了（処理時間: {mm}分{ss}秒）')
    _emit('log', m=f'\n処理時間: {mm}分{ss}秒')
    if low_n:
        _emit('log', m=f'※要確認の箇所: {low_n}件（低信頼度）')
    _emit('log', m='\n=== 文字起こし結果 ===')
    _emit('log', m=full_text)

    # 別名保存ボタン用のテキスト（親が読み戻す）
    txt_lines = [full_text, '\n\n=== タイムスタンプ付き ===']
    for seg, low in zip(segs, low_flags):
        mark = '  ※要確認' if low else ''
        txt_lines.append(
            f'[{fmt_mmss(seg.start)} - {fmt_mmss(seg.end)}] {seg.text.strip()}{mark}')
    result_path = job['result_path']
    with open(result_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))

    base = os.path.splitext(audio_path)[0]
    audio_name = os.path.basename(audio_path)
    saved = []
    if job['out_txt']:
        p = base + '_文字起こし.txt'
        save_txt(p, full_text, segs, low_flags)
        saved.append(p)
    if job['out_srt']:
        p = base + '.srt'
        save_srt(p, segs)
        saved.append(p)
    if job['out_docx']:
        p = base + '_文字起こし.docx'
        save_docx(p, full_text, segs, low_flags, audio_name)
        saved.append(p)
    if job['out_xlsx']:
        p = base + '_文字起こし.xlsx'
        save_xlsx(p, segs, low_flags)
        saved.append(p)

    _emit('done', saved=saved, result_path=result_path)


class WhisperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('ROCK ON MJ  －  会議録音＆文字起こし')
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

        tk.Label(self, text='🎤 ROCK ON MJ',
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

        ttk.Separator(self, orient='horizontal').grid(
            row=8, column=0, columnspan=3, sticky='ew', padx=12, pady=4)

        # 進捗
        self.status_var = tk.StringVar(value='待機中')
        tk.Label(self, textvariable=self.status_var, font=('メイリオ', 9),
                 bg='#f0f0f0', anchor='w'
                 ).grid(row=9, column=0, columnspan=3, sticky='w', padx=14, pady=(4, 2))
        self.progress = ttk.Progressbar(self, length=500, mode='indeterminate')
        self.progress.grid(row=10, column=0, columnspan=3, padx=14, pady=(0, 8))

        # ログ
        self.log = scrolledtext.ScrolledText(
            self, width=66, height=14, font=('ＭＳ ゴシック', 9), state='disabled')
        self.log.grid(row=11, column=0, columnspan=3, padx=12, pady=4)

        # ボタン
        btn_frame = tk.Frame(self, bg='#f0f0f0')
        btn_frame.grid(row=12, column=0, columnspan=3, pady=12)
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
            workdir = os.path.join(tempfile.gettempdir(), '文字起こしツール')
            os.makedirs(workdir, exist_ok=True)
            work = os.path.join(workdir, '_job.json')
            result_path = os.path.join(workdir, '_result.txt')
            job = dict(model_path=model_path, audio_path=audio_path,
                       prompt=build_initial_prompt(app_base_dir()),
                       result_path=result_path,
                       out_txt=self.out_txt.get(), out_srt=self.out_srt.get(),
                       out_docx=self.out_docx.get(), out_xlsx=self.out_xlsx.get())
            with open(work, 'w', encoding='utf-8') as f:
                json.dump(job, f, ensure_ascii=False)

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
