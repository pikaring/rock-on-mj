import sys
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox


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


def app_base_dir():
    return os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__)


def resource_path(name):
    """同梱リソースの場所（exeでは _internal の中）。"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


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


class WhisperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('ROCK ON MJ  －  文字起こし')
        try:
            self.iconbitmap(resource_path('rock_on_mj.ico'))
        except Exception:
            pass
        self.resizable(False, False)
        self.configure(bg='#f0f0f0')
        self._result_text = ''
        self._running = False
        self._build_ui()

        base = app_base_dir()
        ensure_dict_template(base)
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
        tk.Label(self, text='文字起こし  ローカル処理（録音データは外部送信されません）',
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

        # 出力形式
        out_frame = tk.Frame(self, bg='#f0f0f0')
        out_frame.grid(row=6, column=0, columnspan=3, sticky='w', padx=14, pady=(0, 4))
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
            row=7, column=0, columnspan=3, sticky='ew', padx=12, pady=4)

        # 進捗
        self.status_var = tk.StringVar(value='待機中')
        tk.Label(self, textvariable=self.status_var, font=('メイリオ', 9),
                 bg='#f0f0f0', anchor='w'
                 ).grid(row=8, column=0, columnspan=3, sticky='w', padx=14, pady=(4, 2))
        self.progress = ttk.Progressbar(self, length=500, mode='indeterminate')
        self.progress.grid(row=9, column=0, columnspan=3, padx=14, pady=(0, 8))

        # ログ
        self.log = scrolledtext.ScrolledText(
            self, width=66, height=14, font=('ＭＳ ゴシック', 9), state='disabled')
        self.log.grid(row=10, column=0, columnspan=3, padx=12, pady=4)

        # ボタン
        btn_frame = tk.Frame(self, bg='#f0f0f0')
        btn_frame.grid(row=11, column=0, columnspan=3, pady=12)
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

    def _log(self, msg):
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _set_status(self, msg):
        self.after(0, lambda: self.status_var.set(msg))

    def _set_progress(self, value):
        self.after(0, lambda: self.progress.configure(value=value))

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

        self.start_btn.configure(state='disabled')
        self.save_btn.configure(state='disabled')
        self._result_text = ''
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)

        threading.Thread(target=self._run, args=(model_path, audio_path), daemon=True).start()

    def _run(self, model_path, audio_path):
        try:
            from faster_whisper import WhisperModel

            # モデル読み込み
            self._set_status('モデル読み込み中...')
            self.after(0, lambda: self._log('>>> モデル読み込み中...'))
            self.after(0, lambda: self._log(f'  モデル: {model_path}'))
            model = WhisperModel(model_path, device='cpu', compute_type='int8')
            self.after(0, lambda: self._log('  読み込み完了'))

            prompt = build_initial_prompt(app_base_dir())

            # 文字起こし（音声の読み込み・解析はfaster-whisper内部で実行）
            self._set_status('文字起こし中（しばらくかかります）...')
            self.after(0, lambda: self._log('>>> 文字起こし開始...'))
            start = time.time()
            segments, info = model.transcribe(
                audio_path, language='ja', beam_size=5,
                initial_prompt=prompt,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
                no_repeat_ngram_size=3)

            duration = info.duration or 0
            self.after(0, lambda: self._log(
                f'  音声長: {int(duration // 60)}分{int(duration % 60)}秒'))

            # 進捗バーを確定モードに切り替え
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.progress.configure(mode='determinate', maximum=100, value=0))

            segs = []
            for seg in segments:
                segs.append(seg)
                self.after(0, lambda t=seg.text: self._log(t.strip()))
                if duration > 0 and seg.end > 0:
                    pct = min(seg.end / duration * 100, 100)
                    elapsed = time.time() - start
                    eta = elapsed * (duration - seg.end) / seg.end
                    self._set_progress(pct)
                    self._set_status(
                        f'文字起こし中... {int(pct)}%  残り約{fmt_eta(eta)}')

            self._set_progress(100)
            full_text = ''.join(s.text for s in segs)
            low_flags = [s.avg_logprob < LOW_CONF_LOGPROB for s in segs]
            low_n = sum(low_flags)

            elapsed = time.time() - start
            m, s = divmod(int(elapsed), 60)
            self._set_status(f'完了（処理時間: {m}分{s}秒）')
            self.after(0, lambda: self._log(f'\n処理時間: {m}分{s}秒'))
            if low_n:
                self.after(0, lambda: self._log(f'※要確認の箇所: {low_n}件（低信頼度）'))
            self.after(0, lambda: self._log('\n=== 文字起こし結果 ==='))
            self.after(0, lambda: self._log(full_text))

            # 保存用テキスト（別名保存ボタン用）
            txt_lines = [full_text, '\n\n=== タイムスタンプ付き ===']
            for seg, low in zip(segs, low_flags):
                mark = '  ※要確認' if low else ''
                txt_lines.append(
                    f'[{fmt_mmss(seg.start)} - {fmt_mmss(seg.end)}] {seg.text.strip()}{mark}')
            self._result_text = '\n'.join(txt_lines)

            # 各形式で音声ファイルと同じ場所に自動保存
            base = os.path.splitext(audio_path)[0]
            audio_name = os.path.basename(audio_path)
            saved = []
            if self.out_txt.get():
                p = base + '_文字起こし.txt'
                save_txt(p, full_text, segs, low_flags)
                saved.append(p)
            if self.out_srt.get():
                p = base + '.srt'
                save_srt(p, segs)
                saved.append(p)
            if self.out_docx.get():
                p = base + '_文字起こし.docx'
                save_docx(p, full_text, segs, low_flags, audio_name)
                saved.append(p)
            if self.out_xlsx.get():
                p = base + '_文字起こし.xlsx'
                save_xlsx(p, segs, low_flags)
                saved.append(p)

            self.after(0, lambda: self._log('\n✅ 保存しました:'))
            for p in saved:
                self.after(0, lambda pp=p: self._log(f'  {pp}'))
            self.after(0, lambda: self.save_btn.configure(state='normal'))

        except Exception:
            import traceback
            err = traceback.format_exc()
            self._set_status('エラーが発生しました')
            self.after(0, lambda: self._log(f'\n❌ エラー:\n{err}'))
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.progress.configure(mode='determinate'))
            self.after(0, lambda: self.start_btn.configure(state='normal'))

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
    app = WhisperApp()
    app.mainloop()
