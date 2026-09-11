# -*- coding: utf-8 -*-
"""ROCK ON MJ のアイコン（エレキギター）を生成する。

外部の画像素材を使わず、この場で描いて .ico と確認用PNGを書き出す。
    py make_icon.py
"""
import os

from PIL import Image, ImageDraw

OUT_ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rock_on_mj.ico')
S = 1024                      # 下書きの解像度（縮小して滑らかにする）

BODY = (222, 68, 54)          # ボディ（赤）
BODY_DARK = (166, 42, 32)     # ボディの陰
NECK = (54, 38, 30)           # ネック（濃い木目）
HEAD = (38, 26, 22)           # ヘッド
PARTS = (232, 226, 214)       # ピックアップ・ブリッジ
STRING = (245, 240, 230)


def rounded(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def draw_guitar(img):
    d = ImageDraw.Draw(img)
    cx = S * 0.50

    # --- ボディ（下のふくらみ＋上のふくらみ＋くびれをつなぐ胴） ---
    lower = (cx - 300, S * 0.52, cx + 300, S * 0.92)
    upper = (cx - 250, S * 0.34, cx + 250, S * 0.68)
    d.ellipse(lower, fill=BODY)
    d.ellipse(upper, fill=BODY)
    d.rectangle((cx - 250, S * 0.55, cx + 250, S * 0.75), fill=BODY)

    # 陰影（右下を少し暗く）
    d.pieslice(lower, start=-20, end=110, fill=BODY_DARK)

    # --- ネック ---
    neck_w = 78
    rounded(d, (cx - neck_w / 2, S * 0.10, cx + neck_w / 2, S * 0.62), 24, NECK)

    # フレット
    for i in range(9):
        y = S * 0.155 + i * S * 0.047
        d.rectangle((cx - neck_w / 2 + 6, y, cx + neck_w / 2 - 6, y + 7), fill=PARTS)

    # --- ヘッド（少し広げて角を落とす） ---
    hw = 120
    d.polygon([(cx - hw / 2, S * 0.115), (cx + hw / 2, S * 0.115),
               (cx + hw / 2 - 10, S * 0.028), (cx - hw / 2 + 10, S * 0.028)], fill=HEAD)
    # ペグ
    for i in range(3):
        y = S * 0.045 + i * S * 0.026
        d.ellipse((cx - hw / 2 - 26, y, cx - hw / 2 - 4, y + 20), fill=PARTS)
        d.ellipse((cx + hw / 2 + 4, y, cx + hw / 2 + 26, y + 20), fill=PARTS)

    # --- ピックアップ2基とブリッジ ---
    d.rectangle((cx - 150, S * 0.605, cx + 150, S * 0.645), fill=PARTS)
    d.rectangle((cx - 150, S * 0.690, cx + 150, S * 0.730), fill=PARTS)
    rounded(d, (cx - 120, S * 0.775, cx + 120, S * 0.815), 12, PARTS)

    # --- 弦（ヘッドからブリッジまで） ---
    for off in (-24, -8, 8, 24):
        d.line([(cx + off, S * 0.045), (cx + off, S * 0.80)], fill=STRING, width=7)


img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
draw_guitar(img)
# 斜めに傾けるとギターらしく見え、小さいサイズでも輪郭が分かりやすい
img = img.rotate(-28, resample=Image.BICUBIC, expand=False)

SIZES = [256, 128, 64, 48, 32, 16]
img.save(OUT_ICO, sizes=[(s, s) for s in SIZES])
print('書き出し:', OUT_ICO, os.path.getsize(OUT_ICO), 'bytes')

# 紹介ページ（docs/）で使うPNGとファビコンも同じ絵から書き出す
docs = os.path.join(os.path.dirname(OUT_ICO), 'docs', 'assets')
if os.path.isdir(os.path.dirname(docs)):
    os.makedirs(docs, exist_ok=True)
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(docs, 'icon.png'))
    img.save(os.path.join(docs, 'favicon.ico'), sizes=[(s, s) for s in (48, 32, 16)])
    print('書き出し:', os.path.join(docs, 'icon.png'))

# 確認用：実寸を横に並べたPNG
strip = Image.new('RGBA', (sum(SIZES) + 20 * len(SIZES), 280), (250, 250, 250, 255))
x = 10
for s in SIZES:
    strip.paste(img.resize((s, s), Image.LANCZOS), (x, 10), img.resize((s, s), Image.LANCZOS))
    x += s + 20
prev = os.path.join(os.path.dirname(OUT_ICO), 'icon_preview.png')
strip.save(prev)
print('確認用:', prev)
