"""Whisper モデルを models/<名前>/ に取得する。

アプリは models/<名前>/model.bin を探すため、HuggingFace のキャッシュ形式
（models--xxx/snapshots/...）ではなく、フォルダへ直接展開する。

    py tools/download_model.py                 # large-v3-turbo（既定）
    py tools/download_model.py kotoba-whisper-v2.0
"""
import os
import sys

# SSLインスペクションのあるプロキシ環境でも Windows の証明書ストアで検証できるようにする
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')

# 日本語を表せないコンソール（英語版Windows の cp1252 等）でも落ちないようにする
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

from huggingface_hub import snapshot_download  # noqa: E402

REPOS = {
    'large-v3-turbo': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
    'kotoba-whisper-v2.0': 'kotoba-tech/kotoba-whisper-v2.0-faster',
    'large-v3': 'Systran/faster-whisper-large-v3',
    'medium': 'Systran/faster-whisper-medium',
    'small': 'Systran/faster-whisper-small',
}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else 'large-v3-turbo'
    if name not in REPOS:
        sys.exit(f'未知のモデル名: {name}（{", ".join(REPOS)} から選択）')

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dest = os.path.join(root, 'models', name)
    if os.path.isfile(os.path.join(dest, 'model.bin')):
        print(f'取得済み: {dest}')
        return

    print(f'モデルを取得します: {REPOS[name]} -> {dest}')
    print('（1〜2GB あります。回線によっては数分〜数十分かかります）')
    snapshot_download(REPOS[name], local_dir=dest,
                      allow_patterns=['*.bin', '*.json', '*.txt'])
    if not os.path.isfile(os.path.join(dest, 'model.bin')):
        sys.exit('model.bin が見つかりません。取得に失敗した可能性があります。')
    print(f'完了: {dest}')


if __name__ == '__main__':
    main()
