# 録音文字起こしツール

Windows上で完結する、録音と文字起こしのツールです。録音も音声認識もすべてPC内で処理し、音声データを外部に送信しません。

| 版 | 用途 | 実行ファイル |
|---|---|---|
| 会議録音版（`whisper_gui_meeting.py`） | Web会議の録音（相手の声）と、音声ファイルの文字起こし | `mojiokoshi.exe` |
| ファイル文字起こし版（`whisper_gui_fw.py`） | 音声ファイルの文字起こしのみ | `mojiokoshi-fw.exe` |

## 機能

- 音声認識: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（既定モデル `large-v3-turbo`・int8）
- 出力: テキスト / 字幕（SRT）/ Word / Excel。低信頼度の箇所には「※要確認」を付与
- カスタム辞書: 同じフォルダの `辞書.txt` に固有名詞を1行ずつ書く
- 録音（会議録音版のみ）: WASAPIループバックでスピーカーの音を録音。管理者権限・ドライバ不要。録音中は随時保存し、デバイスエラー時は最大5回まで再接続を試みる
- オフライン動作（モデル取得時のみ通信）

> ⚠️ Web会議を録音するときは、参加者への告知と同意を必ず取ってください。
>
> ⚠️ 文字起こしの精度は高くありません。そのまま議事録などに使わず、生成AIシステムによる補完・校正を行ってください。要約する場合も、音声から直接ではなく、一度文字起こししたデータを使ってください。このツールに補完・校正・要約の機能はありません。

## ダウンロード

**[rokuon-mojiokoshi-tool-meeting.zip（会議録音版）](https://github.com/pikaring/rock-on-mj/releases/latest/download/rokuon-mojiokoshi-tool-meeting.zip)**
／ ファイル文字起こし版は [Releases](https://github.com/pikaring/rock-on-mj/releases) の `rokuon-mojiokoshi-tool-fw.zip`

1. ZIPを右クリック →「プロパティ」→「許可する」にチェック → 展開
2. `mojiokoshi.exe` を起動（初回は1〜2分かかります）

- 音声認識モデルを同梱しています（約800MB）。`_internal` と `models` は、exeと同じ場所に置いたままにしてください。
- 動作環境: Windows 10 / 11（64bit）、メモリ8GB以上を推奨
- コード署名をしていないため、SmartScreenの警告が出ます（「詳細情報」→「実行」）。
- ウイルス対策ソフトに誤検知された場合は、組織の管理者にexeのハッシュ（`SHA256SUMS.txt` 参照）の除外登録を依頼してください。

## 動かないとき

exeと同じフォルダの `診断ログ.txt` を添えて問い合わせてください。

- 文字起こしは別プロセスで実行し、異常終了した場合は設定を変えて自動で再試行します。
- 第11世代Coreなど一部のCPUでは、モデル読み込み時に落ちることがあります。再試行で回避されます。

## ソースから動かす

```powershell
py -m pip install -r requirements.txt
py tools\download_model.py        # models\large-v3-turbo\ に取得
py whisper_gui_meeting.py         # 会議録音版（ファイル文字起こし版は whisper_gui_fw.py）
```

`setup.bat`（パッケージとモデルの取得）と、`run_meeting.bat` / `run_fw.bat`（起動）も使えます。

### 依存パッケージの版

- `ctranslate2==4.7.2`: 4.8.0以降は、モデル読み込み時に約3GBを余分に確保し、メモリの少ないPCで落ちます。
- `av==18.1.0`: 19.0以降は、faster-whisper 1.2.1 の音声読み込みが失敗します。

版を上げるときは、必ず動作を確認してください。

## ビルド

```powershell
py -m PyInstaller 文字起こしツール_会議録音_fw.spec   # 会議録音版
py -m PyInstaller 文字起こしツール_fw.spec            # ファイル文字起こし版
```

`dist/<名称>/models/` にモデルを置けば単体で動きます。GitHub Actions（`.github/workflows/build-exe.yml`）でもビルドできます。新しい版は、`version_info.txt` の版を上げて公開します。

## ファイル構成

```
whisper_gui_meeting.py / whisper_gui_fw.py   本体（会議録音版 / ファイル文字起こし版）
*.spec                                       PyInstaller の設定
requirements.txt                             依存パッケージ
setup.bat, run_*.bat, tools/                 セットアップ・起動・モデル取得
version_info.txt                             exeに埋め込むバージョン情報
package/                                     配布ZIPに同梱する説明書・第三者ライセンス
.github/workflows/build-exe.yml              ビルド
```

## ライセンス

ソースは [MIT License](LICENSE) です。配布版には第三者のソフトウェアが同梱されており、ライセンスは `package/THIRD_PARTY_NOTICES.txt` にあります（FFmpeg は LGPL、Intel MKL は Intel の再頒布条件）。
