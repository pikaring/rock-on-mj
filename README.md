# ROCK ON MJ

**録音（ROCK ON）と文字起こし（MJ）**をまとめてこなす、Windows上でローカル完結する音声文字起こしツールです。録音データを外部に送信せず、
すべてお使いのPC内で処理します。用途に応じて2つの版があります。

| ソース | 用途 | ビルド後の名称 |
|---|---|---|
| `whisper_gui_fw.py` | 手持ちの音声ファイルを文字起こし | 文字起こしツール |
| `whisper_gui_meeting.py` | 上記に加え、Web会議（相手の声）を録音して文字起こし | 文字起こし／会議録音ツール |

## 主な機能

- **AI音声認識**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（CTranslate2）による高精度な日本語文字起こし。既定モデルは `large-v3-turbo`（int8量子化）。
- **モデル自動選択**: `models/` 配下を `kotoba-whisper-v2.0 → large-v3-turbo → large-v3 → medium → small` の優先順で自動選択。
- **幻聴（ハルシネーション）対策**: 無音・雑音区間で同じ語を繰り返す暴走を、VAD（無音区間除去）＋ `condition_on_previous_text=False` ＋ `no_repeat_ngram_size` で抑制。
- **複数フォーマット出力**: テキスト(.txt) / 字幕(.srt) / Word(.docx) / Excel(.xlsx)。低信頼度セグメントには「※要確認」を付与（Excelは黄色塗り）。
- **カスタム辞書**: 同じフォルダの `辞書.txt` に固有名詞・専門用語を1行ずつ書くと認識精度が上がる（起動時に既定テンプレを自動生成）。
- **Web会議の録音（会議録音版のみ）**: `soundcard` によるWASAPIループバックで、スピーカー/ヘッドセットに流れる相手の声を録音 →そのまま文字起こし。管理者権限・ドライバ不要。
- **録音データの保全（会議録音版のみ）**: 録音開始と同時にWAVへ逐次書き込み（数秒ごとにヘッダ更新＋fsync）。デバイスエラー時は最大5回まで自動で録り直しを試み、復旧できなかった場合も途中までの音声を残してそのまま文字起こしに回せる。強制終了で取り残されたWAVは次回起動時にヘッダを自動修復。
- **オフライン動作**: インターネット接続なしで利用可能（モデル取得時のみ通信）。

> ⚠️ **Web会議を録音する際は、参加者への録音の告知・同意を必ず取得してください。**

## 動作環境

- Windows 10 / 11
- Python 3.12 以降（開発・ビルド時）
- メモリ 16GB 以上推奨

`large-v3-turbo`（`model.bin` 0.76GB）での実測は、物理メモリのピークが約1.1GB、
コミット（仮想メモリ）のピークが約1.9GB（`ctranslate2==4.7.2`）。

### ctranslate2 は 4.7.2 に固定する（重要）

**4.8.0 以降はモデル読み込み時に約3GBを余分にコミットする回帰がある。**
実際に触るのは1GB程度で、確保するだけして使わないため、メモリの大きい開発機では
気付かないが、**コミット上限の小さいPC（古いSurface等）では確保に失敗して、
エラーも出さずにプロセスごと落ちる**。同一音声・同一モデルでの実測:

| ctranslate2 | モデル読み込み時のピークコミット |
|---|---|
| 4.6.1 | 1,394MB |
| 4.7.2 | 1,392MB |
| 4.8.0 | 4,360MB |
| 4.8.1 | 4,361MB |

`compute_type`（int8 / default / auto / int8_float32）も `cpu_threads`(1/2/4) も
`beam_size`(1/5) もこの山には影響しない。faster-whisper 1.2.1 の要件は
`ctranslate2<5,>=4.0` なので 4.7.2 で問題ない。**アップグレードするときは
必ずこの計測をやり直すこと。**

## トラブルシューティング

### 「何も出ずにアプリが消える」と報告されたとき

Python側の例外はすべて画面のログに出るため、**無言で終了する場合はネイティブ側の
異常終了**（メモリ確保の失敗、ネットワーク越しの読み込み失敗、セキュリティ対策
ソフトによる停止など）を疑う。

exe と同じフォルダ（書き込めない場合は `%LOCALAPPDATA%\文字起こしツール`）に
**`診断ログ.txt`** を出力しているので、まずこれを送ってもらう。

- 末尾に `==== 正常終了 ====` が無ければ異常終了。その直前の行が落ちた場所。
  - `WhisperModel 読み込み開始` の後で止まる → モデル読み込み（メモリ不足、
    `model.bin` の破損、ネットワーク越しの読み込み）
  - `音声の読み込みとVAD（無音除去）を通過` の前で止まる → 音声デコード、
    または onnxruntime（VAD）
- `faulthandler` を有効にしているため、アクセス違反等でもPython側のスタックが残る。
- 冒頭の `==== 起動 ====` にPCのメモリ・コア数・実行場所を記録している。

開始前に `model.bin` の有無とサイズ、空きメモリ、ネットワークパス上かどうかを
`_preflight()` で確認し、危ない場合は落ちる前にダイアログで知らせる。

### 第11世代Core（AVX-512あり）でモデル読み込み中に落ちる

`Windows fatal exception: access violation` が `transcribe.py` の
`ctranslate2.models.Whisper(...)` で出る場合。メモリではない（空きが十分でも起きる）。

| CPU | AVX-512 | 結果 |
|---|---|---|
| 第11世代 Tiger Lake（Model 140） | あり | **落ちる** |
| 第12世代 Alder Lake | 無効 | 動く |
| 第13世代 Raptor Lake | 無効 | 動く |

対策は `configure_cpu_isa()` で AVX2 に固定すること。**罠として
`CT2_FORCE_CPU_ISA` だけでは効かない。** これは CTranslate2 自身のカーネルにしか
効かず、実際の行列演算を担う Intel MKL は自前で命令セットを選ぶため、
`MKL_ENABLE_INSTRUCTIONS` も併せて指定する必要がある（両方とも環境変数で
上書きできるので、切り分け時は `AVX512` や `GENERIC` を外から与えられる）。
`CT2_USE_MKL=0` で MKL 自体を oneDNN に切り替える手もある。

**ただし、この制限は AVX-512 を持つCPUにだけかけること。** AVX-512 を持たない
第12・13世代にかけると事故は防げず、MKL が **AVX-VNNI**（int8の積和演算。
AVX-512の中身のうちAI推論に効く部分を256ビット幅で使えるようにしたもので、
Alder Lake 以降が持つ）を使わなくなるぶん遅くなるだけになる。第13世代
(i5-1335U)・2分の会議音声・条件を交互に入れ替えて3回ずつの実測:

| MKLの制限 | 文字起こし所要（3回） | 中央値 |
|---|---|---|
| なし | 120.5 / 157.5 / 125.5 秒 | 125.5 |
| `MKL_ENABLE_INSTRUCTIONS=AVX2` | 197.5 / 186.1 / 164.5 秒 | **186.1（+48%）** |

制限側の3回すべてが制限なしの3回すべてより遅く、きれいに分離する。
**ブロックでまとめて測ると発熱による周波数変動に埋もれて差が見えないので、
必ず交互に測ること**（最初にそれで誤った結論を出した）。
なお `MKL_ENABLE_INSTRUCTIONS` が効いているかは、セグメント数が 34→36 に変わる
ことでも分かる（演算経路が変わるため。誤差レベルで精度の優劣ではない）。

### GUIプロセスの別スレッドで落ちる（真因・2026-09-09〜10）

第11世代機で `ctranslate2.models.Whisper(...)` がアクセス違反で即死していた件の結論。
**AVX-512でもメモリでもモデル破損でもVC++ランタイムでもなかった。**
落ちるのは常に `Thread-1 (_run)`（GUIのバックグラウンドスレッド）で、
メインスレッドは `tkinter mainloop` にいた。**同じ処理を子プロセスのメインスレッドで
動かすと、追加設定なしで完走する**（50分31秒の音声を42分20秒で処理）。

そのPCには**資産管理ソフトとウイルス対策ソフト（AMSIプロバイダ）のDLLが3本注入**
されており、`CFG=1`（開発機は0）だった。何が注入されているかは、起動時の診断ログの
「外部DLL（起動時）」に出る。

注入されたフックがDLL初期化に割り込み、スタックの小さいサブスレッドで
破綻していたと考えるのが最も説明が付く（メインスレッドはスタックが大きい）。
なお `import av`（FFmpeg）のアクセス違反は**成功時も出続けている**が、
SEHで処理されて実害が無い。faulthandlerが報告するので紛らわしい。

**対策＝文字起こしを別プロセスで実行する**（`run_worker()` と `_spawn_worker()`）。
- 同じexeを `--transcribe-worker <ジョブJSON>` で再実行する
- 進捗・ログ・結果はJSON1行ずつで親へ返す（`os.write(1, ...)`。frozenでは
  `sys.stdout` が使えないことがあるため記述子へ直接書く）
- 子のstderrは診断ログへ向ける。**これでCTranslate2のネイティブログが確実に残る**
  （親プロセスでの `os.dup2` はコンソール無しのexeでは効かないことがある）
- 異常終了したら `FALLBACKS` の設定を順に落として自動で再試行し、
  成功した設定をログに残す（通常→MKLなし→1スレッド→GENERIC）
- 終了コードは `EXIT_REASONS` で日本語化（0xC0000005＝アクセス違反 等）

### モデルファイルの破損

`models/<名前>/model.crc32` があれば、起動時に `model.bin` と照合する
（`check_model_file()`、0.8秒）。配布物を作るときは必ずこのファイルも同梱すること。
無ければ照合しない（自前モデルに差し替えた場合を壊さないため）。

なお exe は UPX 圧縮していない（セキュリティ対策ソフトの誤検知と、
ネイティブDLLの破損を避けるため）。

## セットアップ（ソースから動かす場合）

```powershell
py -m pip install faster-whisper "ctranslate2==4.7.2" av openpyxl python-docx soundcard pyinstaller
```

### モデルの取得

モデル本体はリポジトリに含めていません（サイズが大きいため）。初回は faster-whisper で取得します。

```powershell
py -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8', download_root='models')"
```

取得したモデルを `models/large-v3-turbo/`（`model.bin` 等を含むフォルダ）として配置します。

> プロキシでSSLインスペクションを行う環境では、HuggingFaceからのダウンロードが
> 証明書エラーで失敗することがあります。その場合は `truststore` を使うと回避できます:
> `py -m pip install truststore` の後、ダウンロード用スクリプト冒頭で
> `import truststore; truststore.inject_into_ssl()` を実行してから取得してください。

### 実行（ビルドせずに動かす）

```powershell
py whisper_gui_fw.py          # ファイル文字起こし版
py whisper_gui_meeting.py     # 会議録音版
```

## ビルド（配布用 exe の作成）

[PyInstaller](https://pyinstaller.org/) で onedir 形式の実行ファイルを作成します。

```powershell
py -m PyInstaller 文字起こしツール_fw.spec            # ファイル文字起こし版
py -m PyInstaller 文字起こしツール_会議録音_fw.spec     # 会議録音版
```

- **onedir 構成**（EXE + `_internal/` フォルダ）。配布は「フォルダごと」渡します。
- 出力後、`dist/<名称>/models/` にモデルフォルダを配置すれば単体で動作します。
- 各PCでの**初回起動はセキュリティスキャンのため約90〜120秒**かかります（2回目以降は数秒）。

## ファイル構成

```
whisper_gui_fw.py               # ファイル文字起こし版 本体
whisper_gui_meeting.py          # 会議録音版 本体（fw版＋録音機能）
文字起こしツール_fw.spec           # ファイル文字起こし版 PyInstaller spec
文字起こしツール_会議録音_fw.spec    # 会議録音版 PyInstaller spec
```

## ライセンス

このリポジトリのソース（`whisper_gui_*.py`・`*.spec`・`make_icon.py`）は
**MIT License** です（[LICENSE](LICENSE)）。

**ビルド済みの実行ファイルは配布していません。** 上の「ビルド（配布用 exe の作成）」の
手順で各自作成してください。モデルも各自で取得します（同「モデルの取得」）。

ビルドした実行ファイルには第三者のライブラリが同梱されます。再配布する場合は
それぞれのライセンス表示が必要です。主なものは次のとおり（`_internal` 配下に
各パッケージのライセンス本文が同梱されています）。

| 同梱物 | ライセンス |
|---|---|
| faster-whisper / CTranslate2 / Whisperモデル | MIT |
| FFmpeg（PyAV が同梱するDLL群） | **LGPL** |
| Intel MKL（`ctranslate2.dll` に静的リンク） | Intel の再頒布条件に従う |
| onnxruntime / tokenizers / huggingface_hub | MIT / Apache-2.0 |
| tqdm | MPL-2.0 AND MIT |

**実行ファイルはコード署名していません。** 初回起動時に SmartScreen の警告が出たり、
ウイルス対策ソフトがスキャンのため数十秒ブロックしたりします。
