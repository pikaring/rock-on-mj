"""文字起こし前の音声の下ごしらえ（両方の版から使う）。

会場の後方に置いたPCのマイクで講義を録ると，声が小さく雑音に近くなり，
faster-whisper の無音除去（Silero VAD）が声の区間を「無音」として丸ごと捨てることがある。
そうなると20分以上の話が1行になり，出力が極端に短くなる（2026-09 自治大学校の録音で発生）。

  1. 音量の補正: 声の大きい所を基準に，全体へ同じゲインをかける
  2. 無音除去の強さの自動調整: 発話として残る割合が少なすぎたら，しきい値を下げて選び直す。
     それでも足りなければ無音除去を使わず，無音の判定は Whisper 自身に任せる
     （声が雑音より 6〜10dB 小さいと，Silero VAD はしきい値に関係なく発話0%に近くなるが，
     Whisper はその声の相当部分を書き起こせる。英語の実音声で実測）
  3. 5分ごとの音量と，声として拾えた割合をログに出す（次に問題が出たときの切り分け用）
  4. 抜けの補正: 雑音が多いと Whisper は30秒の窓の中身を1文に縮めたり，窓ごと飛ばしたり
     する（どの窓で起きるかは窓の区切り方しだいで，偶然に近い）。文字起こしの後で
     「長い時間の割に文字が極端に少ない所」「発話があるのに何も出ていない所」を探し，
     その区間だけ切り出して文字起こしし直し，文字が増えたら差し替える
"""
import dataclasses
import re

import numpy as np

SR = 16000
BLOCK = SR // 2                       # 音量を測る単位（0.5秒）
VAD_THRESHOLDS = (0.5, 0.35, 0.2)     # 先頭が faster-whisper の既定
MIN_SILENCE_MS = 500
TARGET_DB = -20.0                     # 声の大きい所をこの音量にそろえる
MAX_GAIN_DB = 30.0                    # 雑音だけの録音を持ち上げすぎない
MIN_KEPT = 0.6                        # 講義・会議なら，普通は6割以上が発話
# 抜けの補正の対象（日本語の講義・会議は，話していれば毎秒5〜8文字ほど出る）
SUSPECT_SEC = 15.0                    # これより長く…
SUSPECT_CPS = 1.5                     # …毎秒この文字数に満たないセグメント
GAP_SEC = 20.0                        # 発話があるのに，これより長く何も出ていない所
# 雑音や聞き取れない声に対して Whisper が出しがちな決まり文句（幻聴）。
# 動画の字幕で学習した名残で，講義・会議の発言としてはまず出てこない
HALLUCINATIONS = re.compile(
    r'(ご視聴(いただき)?ありがとうございました'
    r'|チャンネル登録(を|よろしく)?(お願いします|お願いいたします)?'
    r'|次回予告)[。．、！!\s]*')


def load_audio(path):
    from faster_whisper import decode_audio
    return decode_audio(path, sampling_rate=SR)


def _block_db(audio):
    n = len(audio) // BLOCK
    if n == 0:
        return np.array([-120.0])
    # 一度に2乗すると音声と同じ大きさ（54分で約200MB）の一時配列ができるので，
    # メモリの少ないPC向けに10分ずつ測る
    step = 1200
    r = np.empty(n)
    for i in range(0, n, step):
        j = min(i + step, n)
        b = audio[i * BLOCK:j * BLOCK].reshape(j - i, BLOCK)
        r[i:j] = np.sqrt(np.einsum('ij,ij->i', b, b, dtype=np.float64) / BLOCK)
    return 20 * np.log10(r + 1e-10)


def normalize(audio):
    """声の大きい所（0.5秒ごとの音量の上位5%）が TARGET_DB になるようにする。

    メモリの少ないPCに配慮して，コピーを作らずその場で書き換える。
    戻り値は (audio, かけたゲインdB)。
    """
    loud = float(np.percentile(_block_db(audio), 95))
    gain_db = min(TARGET_DB - loud, MAX_GAIN_DB)
    if gain_db > 3.0:   # 小さくする方向と，わずかな補正はしない
        audio *= np.float32(10 ** (gain_db / 20))
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio, gain_db
    return audio, 0.0


def _speech_mask(n_blocks, stamps):
    mask = np.zeros(n_blocks, dtype=bool)
    for t in stamps:
        mask[t['start'] // BLOCK:(t['end'] + BLOCK - 1) // BLOCK] = True
    return mask


def choose_vad(audio, log=print):
    """しきい値を順に試し，発話が十分に残る最初のものを選ぶ。

    戻り値は (transcribe() に渡す vad_parameters, 0.5秒ごとの発話の有無)。
    どのしきい値でも足りなければ vad_parameters は None（無音除去を使わない）。
    """
    from faster_whisper.vad import get_speech_timestamps, VadOptions

    db = _block_db(audio)
    best = None
    for th in VAD_THRESHOLDS:
        stamps = get_speech_timestamps(
            audio, VadOptions(threshold=th, min_silence_duration_ms=MIN_SILENCE_MS))
        mask = _speech_mask(len(db), stamps)
        log(f'  無音除去（しきい値{th}）: 発話として残るのは {mask.mean() * 100:.0f}%')
        if best is None or mask.mean() > best[1].mean():
            best = (th, mask)
        if mask.mean() >= MIN_KEPT:
            break
    th, mask = best
    _log_blocks(db, mask, log)
    if mask.mean() < MIN_KEPT:
        log('  → 声が小さく雑音に近いため，無音除去を使わずに文字起こしします'
            '（時間が長めにかかります）')
        return None, mask
    if th != VAD_THRESHOLDS[0]:
        log(f'  → 声を捨てすぎていたため，無音除去を弱めました（しきい値{th}）')
    return dict(threshold=th, min_silence_duration_ms=MIN_SILENCE_MS), mask


def is_hallucination(text):
    """雑音や聞き取れない声から出た決まり文句（1セグメントまるごと）かどうか。"""
    return HALLUCINATIONS.fullmatch(text.strip()) is not None


def _log_blocks(db, mask, log, minutes=5):
    per = minutes * 60 * SR // BLOCK
    log(f'  {minutes}分ごとの音量と発話の割合:')
    for i in range(0, len(db), per):
        d = db[i:i + per]
        m = mask[i:i + per]
        vol = float(np.percentile(d, 90))
        mark = '  ※ほぼ拾えていません' if m.mean() < 0.3 else ''
        log(f'    {i * BLOCK // SR // 60:3d}分〜  音量 {vol:6.1f}dB  発話 {m.mean() * 100:3.0f}%{mark}')


def prepare(path, log=print):
    """読み込み→音量補正→無音除去の強さを決める。

    戻り値は (audio, vad_parameters, 発話の有無)。vad_parameters が None なら無音除去を使わない。
    """
    audio = load_audio(path)
    before = float(np.percentile(_block_db(audio), 95))
    audio, gain = normalize(audio)
    if gain:
        log(f'  音量が小さいため {gain:.0f}dB 持ち上げました（声の大きい所 {before:.0f}dB → {TARGET_DB:.0f}dB）')
    vad_parameters, mask = choose_vad(audio, log)
    return audio, vad_parameters, mask


def _speech_ratio(mask, a, b):
    m = mask[int(a * SR) // BLOCK:int(b * SR) // BLOCK]
    return float(m.mean()) if len(m) else 0.0


def _suspects(segs, total, mask, use_vad):
    """文字起こしし直す区間 [(開始秒, 終了秒)] を探す。"""
    spans = []
    prev = 0.0
    for s in list(segs) + [None]:
        start = total if s is None else s.start
        # 何も出ていない長い空白のうち，無音除去の判定で発話らしさがある所だけ
        # （休憩などの本当の無音をやり直すと，幻聴を差し込むおそれがある）。
        # 無音除去を使わなかったときは判定が控えめに出るので，基準を下げる
        if start - prev > GAP_SEC and _speech_ratio(mask, prev, start) > (0.3 if use_vad else 0.1):
            # 直前の発言の語尾を含めると，Whisper がそれだけ書いて残りを飛ばしやすい（実測）
            spans.append((prev + 0.3, start))
        if s is None:
            break
        dur = s.end - s.start
        if dur > SUSPECT_SEC and len(s.text.strip()) / dur < SUSPECT_CPS:
            spans.append((s.start, s.end))
        prev = s.end
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + 1.0:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def repair(model, audio, segs, vad_parameters, mask, transcribe_kwargs, log=print,
           progress=None, rounds=3):
    """抜けていそうな区間を切り出して文字起こしし直し，文字が増えたら差し替える。

    切り出した区間でも窓が飛ばされることがあるので，残った抜けに対して最大 rounds 回くり返す。
    """
    total = len(audio) / SR
    use_vad = vad_parameters is not None
    tried = set()
    out = list(segs)
    for r in range(rounds):
        spans = [sp for sp in _suspects(out, total, mask, use_vad)
                 if (round(sp[0], 1), round(sp[1], 1)) not in tried]
        if not spans:
            break
        log(f'>>> 抜けの補正{f"（{r + 1}回目）" if r else ""}: {len(spans)}か所'
            f'（計{sum(b - a for a, b in spans) / 60:.1f}分）を文字起こしし直します')
        improved = False
        for n, (a, b) in enumerate(spans, 1):
            tried.add((round(a, 1), round(b, 1)))
            if progress:
                progress(n, len(spans))
            out, better = _redo(model, audio, out, a, b, total, transcribe_kwargs, log)
            improved |= better
        if not improved:
            break
    return out


def _redo(model, audio, out, a, b, total, transcribe_kwargs, log):
    old = [s for s in out if s.end > a and s.start < b]
    a0 = a   # 前に余白を付けない（直前の発言の語尾が入ると，それだけ書いて止まりやすい）
    clip = audio[int(a0 * SR):int(min(total, b + 0.5) * SR)]
    new, _ = model.transcribe(clip, vad_filter=False, **transcribe_kwargs)
    new = [dataclasses.replace(s, start=round(s.start + a0, 2), end=round(s.end + a0, 2))
           for s in new if not is_hallucination(s.text)]
    old_n = sum(len(s.text.strip()) for s in old)
    new_n = sum(len(s.text.strip()) for s in new)
    mm, ss = divmod(int(a), 60)
    if new_n > old_n:
        out = sorted([s for s in out if s not in old] + new, key=lambda s: s.start)
        log(f'  {mm:02d}:{ss:02d}〜 {old_n}文字 → {new_n}文字に補正')
        return out, True
    log(f'  {mm:02d}:{ss:02d}〜 変化なし（{old_n}文字）')
    return out, False
