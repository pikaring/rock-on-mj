# -*- coding: utf-8 -*-
"""配布用リポジトリ向けに、コミット済みのファイルだけを書き出す。

    py tools/export_public.py                      # ドライラン（既定）。コミット・pushはしない
    py tools/export_public.py --commit             # mirror_dir を入れ替えてコミット
    py tools/export_public.py --commit --push      # 確認プロンプトのあと push
    py tools/export_public.py --commit --tag v1.0.0  # 確認プロンプトのあと、タグを作って push

設定は tools/public_export.json。流れ:
  1. git archive HEAD を一時ディレクトリに展開（未コミット・ignore済みは含まれない）
  2. exclude_paths を削除
  3. PUBLIC-EXPORT:EXCLUDE-START〜END で囲まれたブロックを削除（HTML/MD）
  4. URL・オーナー名・リポジトリ名・著作権者名を設定値に置換、配布用workflowを配置
  5. 画像のメタデータを除去
  6. forbidden_strings を全文検索。1件でもあればエラー終了
  7. mirror_dir の中身（.git 以外）を入れ替え、git diff --stat を表示
  8. mirror_dir のローカル設定（git_name / git_email）で「Sync YYYY-MM-DD」をコミット
push・タグ・強制上書き（--force 系）はこのスクリプトでは行わない（push は通常の push のみ、要確認）。
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'public_export.json')
MARK_S = '<!-- PUBLIC-EXPORT:EXCLUDE-START -->'
MARK_E = '<!-- PUBLIC-EXPORT:EXCLUDE-END -->'
BLOCK_RE = re.compile(r'[ \t]*' + re.escape(MARK_S) + r'.*?' + re.escape(MARK_E) + r'[ \t]*\n?', re.S)
MARKER_EXT = ('.html', '.htm', '.md')
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
PLACEHOLDER_UNSET = ('<ID>', 'YOUR NAME')


class ExportError(Exception):
    pass


def run(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8')
    if check and r.returncode != 0:
        raise ExportError('コマンド失敗: %s\n%s' % (' '.join(args[:3]), r.stderr.strip()))
    return r


def load_config():
    with open(CONFIG, encoding='utf-8') as f:
        return json.load(f)


def repo_root():
    return run(['git', 'rev-parse', '--show-toplevel'], cwd=HERE).stdout.strip()


def is_excluded(rel, patterns):
    """gitignore 風: スラッシュを含まない（末尾以外）パターンは名前で、含むものはルート起点で照合。"""
    parts = rel.split('/')
    for pat in patterns:
        p = pat.rstrip('/')
        if '/' in p:
            if fnmatch.fnmatch(rel, p) or rel.startswith(p + '/'):
                return True
        else:
            if any(fnmatch.fnmatch(part, p) for part in parts):
                return True
    return False


def all_files(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != '.git']
        for f in files:
            full = os.path.join(d, f)
            yield full, os.path.relpath(full, root).replace(os.sep, '/')


def read_text(path):
    with open(path, 'rb') as f:
        b = f.read()
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return None


def write_text(path, s):
    with open(path, 'wb') as f:
        f.write(s.encode('utf-8'))


def step_archive(dest):
    dirty = run(['git', 'status', '--porcelain'], cwd=repo_root()).stdout.strip()
    if dirty:
        print('注意: 未コミットの変更があります（エクスポートには含まれません）。')
    tar_path = os.path.join(dest, '_src.tar')
    with open(tar_path, 'wb') as f:
        r = subprocess.run(['git', 'archive', '--format=tar', 'HEAD'], cwd=repo_root(), stdout=f)
    if r.returncode != 0:
        raise ExportError('git archive に失敗しました')
    out = os.path.join(dest, 'tree')
    os.mkdir(out)
    with tarfile.open(tar_path) as t:
        t.extractall(out)
    os.remove(tar_path)
    return out


def step_exclude(tree, cfg):
    n = 0
    for full, rel in list(all_files(tree)):
        if is_excluded(rel, cfg['exclude_paths']):
            os.remove(full)
            n += 1
    for d, dirs, files in os.walk(tree, topdown=False):
        if d != tree and not os.listdir(d):
            os.rmdir(d)
    print('  除外したファイル: %d' % n)


def step_markers(tree):
    blocks = 0
    for full, rel in all_files(tree):
        s = read_text(full)
        if s is None:
            continue
        if rel.lower().endswith(MARKER_EXT):
            if s.count(MARK_S) != s.count(MARK_E):
                raise ExportError('%s: EXCLUDE-START と EXCLUDE-END の数が合いません' % rel)
            new, k = BLOCK_RE.subn('', s)
            if k:
                write_text(full, new)
                blocks += k
                print('  %s: %d ブロック削除' % (rel, k))
    print('  削除したブロック合計: %d' % blocks)
    check_anchors(tree)


def check_anchors(tree):
    """ページ内リンク（href="#x"）の行き先が残っているか。"""
    for full, rel in all_files(tree):
        if not rel.lower().endswith(('.html', '.htm')):
            continue
        s = read_text(full)
        ids = set(re.findall(r'\bid="([^"]+)"', s))
        for m in re.finditer(r'href="#([^"]+)"', s):
            if m.group(1) not in ids and m.group(1) != 'top':
                line = s.count('\n', 0, m.start()) + 1
                raise ExportError('%s:%d: アンカー #%s の行き先がありません' % (rel, line, m.group(1)))
    print('  ページ内アンカー: 整合')


def step_replace(tree, cfg):
    fmt = {k: cfg[k] for k in ('public_owner', 'public_repo', 'public_pages_url', 'copyright_holder')}
    src_pages = cfg['source_pages_url'].rstrip('/')
    pub_pages = cfg['public_pages_url'].rstrip('/')
    src_path = '%s/%s' % (cfg['source_owner'], cfg['source_repo'])
    pub_path = '%s/%s' % (cfg['public_owner'], cfg['public_repo'])
    lits = [(x['from'], x['to'].format(**fmt)) for x in cfg.get('literal_replacements', [])]
    placeholders = {
        '{{PUBLIC_OWNER}}': cfg['public_owner'], '{{PUBLIC_REPO}}': cfg['public_repo'],
        '{{PUBLIC_PAGES_URL}}': cfg['public_pages_url'], '{{COPYRIGHT_HOLDER}}': cfg['copyright_holder'],
    }
    holder_files = set(cfg.get('holder_files', []))
    changed = 0
    for full, rel in all_files(tree):
        s = read_text(full)
        if s is None:
            continue
        o = s
        for a, b in lits:
            s = s.replace(a, b)
        s = s.replace(src_pages, pub_pages)
        s = s.replace(src_path, pub_path)
        if rel in holder_files:
            s = s.replace(cfg['source_copyright_holder'], cfg['copyright_holder'])
        for a, b in placeholders.items():
            s = s.replace(a, b)
        if s != o:
            write_text(full, s)
            changed += 1
    print('  置換したファイル: %d' % changed)


def step_workflow(tree, cfg):
    wf = os.path.join(tree, '.github', 'workflows')
    if os.path.isdir(wf):
        shutil.rmtree(wf)
    tpl = os.path.join(repo_root(), cfg['workflow_template'])
    if not os.path.exists(tpl):
        raise ExportError('workflow 雛形がありません: %s' % cfg['workflow_template'])
    os.makedirs(wf)
    shutil.copy(tpl, os.path.join(wf, 'build-exe.yml'))
    print('  配布用 workflow を配置: .github/workflows/build-exe.yml')


def step_images(tree):
    cleaned, skipped = [], []
    for full, rel in all_files(tree):
        low = rel.lower()
        if low.endswith(IMAGE_EXT):
            try:
                from PIL import Image
            except ImportError:
                raise ExportError('画像があるため Pillow が必要です（pip install pillow）')
            with Image.open(full) as im:
                im.load()
                had = sorted(k for k in im.info if k not in ('dpi', 'transparency'))
                fmt = im.format
                clean = Image.frombytes(im.mode, im.size, im.tobytes())
                if im.palette is not None:
                    clean.putpalette(im.getpalette())
            save_kw = {'optimize': True} if fmt in ('PNG', 'JPEG') else {}
            clean.save(full, format=fmt, **save_kw)
            cleaned.append((rel, had))
        elif low.endswith('.ico'):
            skipped.append(rel)
    for rel, had in cleaned:
        print('  %s: メタデータ除去%s' % (rel, '（削除: %s）' % ', '.join(had) if had else '（該当なし）'))
    for rel in skipped:
        print('  %s: ICO はメタデータ領域を持たないため未加工' % rel)


def step_verify(tree, cfg):
    words = [w for w in cfg['forbidden_strings'] if w]
    regexes = [re.compile(r) for r in cfg.get('forbidden_regexes', [])]
    hits = []
    for full, rel in all_files(tree):
        for w in words:                               # ファイル名・ディレクトリ名
            if w.lower() in rel.lower():
                hits.append('%s (ファイル名: %s)' % (rel, w))
        with open(full, 'rb') as f:
            raw = f.read()
        s = None
        try:
            s = raw.decode('utf-8')
        except UnicodeDecodeError:
            pass
        if s is not None:
            for no, line in enumerate(s.splitlines(), 1):
                low = line.lower()
                for w in words:
                    if w.lower() in low:
                        hits.append('%s:%d (%s)' % (rel, no, w))
                for r in regexes:
                    if r.search(line):
                        hits.append('%s:%d (トークン形式: %s)' % (rel, no, r.pattern[:12]))
            if MARK_S in s or MARK_E in s:
                hits.append('%s (EXCLUDE マーカーが残っています)' % rel)
        if s is None or b'\x00' in raw:               # バイナリ内（UTF-8 / UTF-16LE）
            lraw = raw.lower()
            for w in words:
                for enc in ('utf-8', 'utf-16-le'):
                    if w.lower().encode(enc) in lraw:
                        hits.append('%s (バイナリ内: %s)' % (rel, w))
                        break
    if hits:
        print('\n禁止文字列が見つかりました:')
        for h in sorted(set(hits)):
            print('  ' + h)
        raise ExportError('forbidden_strings に %d 件ヒットしました。エクスポートを中止します。' % len(set(hits)))
    wf = os.path.join(tree, '.github', 'workflows')
    n = len(os.listdir(wf)) if os.path.isdir(wf) else 0
    print('  禁止文字列: 0件（workflow %d 件を含む全 %d ファイルを検索）' % (n, sum(1 for _ in all_files(tree))))


def check_config_for_commit(cfg):
    bad = [k for k in ('git_email', 'copyright_holder') if any(p in cfg[k] for p in PLACEHOLDER_UNSET)]
    if bad:
        raise ExportError('public_export.json の %s が未設定です（<ID> / YOUR NAME のまま）。' % ', '.join(bad))


def mirror_path(cfg):
    p = cfg['mirror_dir']
    return p if os.path.isabs(p) else os.path.abspath(os.path.join(repo_root(), p))


def step_mirror(tree, cfg, commit):
    mirror = mirror_path(cfg)
    have_repo = os.path.isdir(os.path.join(mirror, '.git'))
    if not commit:
        if have_repo:
            with tempfile.TemporaryDirectory() as t:
                cur = os.path.join(t, 'cur')
                shutil.copytree(mirror, cur, ignore=shutil.ignore_patterns('.git'))
                r = run(['git', 'diff', '--no-index', '--stat', 'cur', os.path.relpath(tree, t)], cwd=t, check=False)
                print(r.stdout.strip() or '  現在の配布用リポジトリとの差分なし')
        else:
            print('  mirror_dir（%s）が未作成のため差分は出せません。エクスポート結果:' % cfg['mirror_dir'])
            for _, rel in sorted(all_files(tree), key=lambda x: x[1]):
                print('    ' + rel)
        return None
    if not have_repo:
        raise ExportError('mirror_dir がgitリポジトリではありません: %s（先に配布用リポジトリをclone）' % mirror)
    remote = run(['git', 'remote', 'get-url', 'origin'], cwd=mirror, check=False).stdout.strip()
    expect = '%s/%s' % (cfg['public_owner'], cfg['public_repo'])
    if expect.lower() not in remote.lower():
        raise ExportError('mirror_dir の origin が配布用リポジトリではありません: %s' % remote)
    for name in os.listdir(mirror):
        if name == '.git':
            continue
        p = os.path.join(mirror, name)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
    for name in os.listdir(tree):
        s, d = os.path.join(tree, name), os.path.join(mirror, name)
        shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy2(s, d)
    run(['git', 'config', 'user.name', cfg['git_name']], cwd=mirror)
    run(['git', 'config', 'user.email', cfg['git_email']], cwd=mirror)
    run(['git', 'add', '-A'], cwd=mirror)
    print(run(['git', 'diff', '--cached', '--stat'], cwd=mirror).stdout.strip() or '  差分なし')
    return mirror


def confirm(msg):
    return input('%s [yes/N]: ' % msg).strip().lower() == 'yes'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--commit', action='store_true', help='mirror_dir を入れ替えてコミットする（未指定ならドライラン）')
    ap.add_argument('--push', action='store_true', help='コミット後に push（確認あり）')
    ap.add_argument('--tag', metavar='vX.Y.Z', help='タグを作って push（確認あり）')
    ap.add_argument('--keep', action='store_true', help='一時ディレクトリを残して場所を表示する')
    a = ap.parse_args()
    if (a.push or a.tag) and not a.commit:
        ap.error('--push / --tag は --commit と一緒に指定してください')
    if a.tag and not re.fullmatch(r'v\d+\.\d+\.\d+', a.tag):
        ap.error('--tag は vX.Y.Z 形式で指定してください')

    cfg = load_config()
    if a.commit:
        check_config_for_commit(cfg)
    tmp = tempfile.mkdtemp(prefix='public_export_')
    try:
        print('1. git archive HEAD'); tree = step_archive(tmp)
        print('2. exclude_paths'); step_exclude(tree, cfg)
        print('3. EXCLUDE ブロック'); step_markers(tree)
        print('4. 置換・workflow'); step_replace(tree, cfg); step_workflow(tree, cfg)
        print('5. 画像メタデータ'); step_images(tree)
        print('6. 禁止文字列の検索'); step_verify(tree, cfg)
        print('7. mirror_dir'); mirror = step_mirror(tree, cfg, a.commit)
        if not a.commit:
            print('\nドライラン完了（コミット・pushはしていません）。')
            if a.keep:
                print('出力先: ' + tree)
            return 0
        if run(['git', 'diff', '--cached', '--quiet'], cwd=mirror, check=False).returncode == 0:
            print('コミットする差分がありません。')
        else:
            msg = 'Sync %s' % datetime.date.today().isoformat()
            run(['git', 'commit', '-m', msg], cwd=mirror)
            print('8. コミット: ' + msg)
        remote = run(['git', 'remote', 'get-url', 'origin'], cwd=mirror).stdout.strip()
        if a.push and confirm('%s へ push します。よろしいですか' % remote):
            r = run(['git', 'push', '-u', 'origin', 'HEAD'], cwd=mirror)
            print(r.stdout + r.stderr)
        if a.tag and confirm('タグ %s を作成し %s へ push します。よろしいですか' % (a.tag, remote)):
            run(['git', 'tag', a.tag], cwd=mirror)
            r = run(['git', 'push', 'origin', a.tag], cwd=mirror)
            print(r.stdout + r.stderr)
        return 0
    except ExportError as e:
        print('エラー: %s' % e, file=sys.stderr)
        return 1
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
