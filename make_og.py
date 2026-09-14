# -*- coding: utf-8 -*-
"""SNSやチャットにリンクを貼ったときに出る画像（OGP、1200×630）を生成する。

    py make_og.py            → assets/og.png

アイコン（assets/icon.png）と紹介ページの見出しを、ページと同じ配色で組む。
3つの紹介ページ（rock-on-mj / ride-on-qc / reach-on-sanma）で同じスクリプトを使い、
リポジトリのフォルダ名で配色と文言を切り替える。
"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.basename(HERE)
W, H = 1200, 630

SITES = {
    'transcription-tool': dict(
        name='ROCK ON MJ', assets='docs/assets',
        head=['録音して、文字起こす。', 'ぜんぶ、このPCで。'],
        tag='Web会議の録音と文字起こしを、PCの中だけで。',
        url='pikaring.github.io/rock-on-mj',
        paper=(251, 248, 246), ink=(36, 28, 26), muted=(138, 128, 123), accent=(210, 64, 46), glow=(246, 214, 205)),
    'ride-on-qc': dict(
        name='Ride on QC', assets='assets',
        head=['曲がり角ごとに、', '地図と風が出る。'],
        tag='GPXとExcelから、iPhone向けのブルベ キューシートを1枚に。',
        url='pikaring.github.io/ride-on-qc',
        paper=(251, 250, 248), ink=(22, 32, 47), muted=(133, 140, 152), accent=(208, 138, 38), glow=(240, 228, 210)),
    'reach-on-sanma': dict(
        name='Reach on SANMA', assets='assets',
        head=['サンマは、', 'シンプルが美味い。'],
        tag='演出も登録もダウンロードも要らない三人麻雀。',
        url='pikaring.github.io/reach-on-sanma',
        paper=(250, 248, 244), ink=(31, 34, 38), muted=(134, 140, 150), accent=(217, 112, 44), glow=(243, 227, 211)),
}
S = SITES[REPO]


def font(names, size):
    for n in names:
        p = os.path.join(r'C:\Windows\Fonts', n)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


F_NAME = font(['NotoSans-Bold.ttf', 'YuGothB.ttc'], 46)
F_HEAD = font(['YuGothB.ttc', 'BIZ-UDGothicB.ttc', 'meiryob.ttc'], 68)
F_TAG = font(['BIZ-UDGothicR.ttc', 'YuGothM.ttc', 'meiryo.ttc'], 27)
F_URL = font(['NotoSans-Bold.ttf', 'YuGothB.ttc'], 24)

img = Image.new('RGB', (W, H), S['paper'])

# ヒーローと同じ、暖色の柔らかい光を右下に
glow = Image.new('RGB', (W, H), S['paper'])
gd = ImageDraw.Draw(glow)
gd.ellipse((520, 120, 1400, 900), fill=S['glow'])
glow = glow.filter(ImageFilter.GaussianBlur(120))
img = Image.blend(img, glow, 0.9)

d = ImageDraw.Draw(img)

# アイコン（左）
icon = Image.open(os.path.join(HERE, S['assets'], 'icon.png')).convert('RGBA').resize((280, 280), Image.LANCZOS)
shadow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
sd = ImageDraw.Draw(shadow)
sd.rounded_rectangle((96, 200, 376, 480), radius=62, fill=(0, 0, 0, 70))
shadow = shadow.filter(ImageFilter.GaussianBlur(28))
img.paste(shadow, (0, 0), shadow)
img.paste(icon, (90, 175), icon)

# 文字（右）
x = 440
d.text((x, 168), S['name'], font=F_NAME, fill=S['accent'])
y = 236
for line in S['head']:
    d.text((x, y), line, font=F_HEAD, fill=S['ink'])
    y += 86
# 右端に収まるまで少しずつ小さくする
f_tag, size = F_TAG, 27
while d.textlength(S['tag'], font=f_tag) > W - x - 60 and size > 18:
    size -= 1
    f_tag = font(['BIZ-UDGothicR.ttc', 'YuGothM.ttc', 'meiryo.ttc'], size)
d.text((x, y + 14), S['tag'], font=f_tag, fill=S['muted'])

# 下端：アクセントの帯と URL
d.rectangle((0, H - 10, W, H), fill=S['accent'])
d.text((x, H - 66), S['url'], font=F_URL, fill=S['muted'])

out = os.path.join(HERE, S['assets'], 'og.png')
img.save(out, optimize=True)
print('書き出し:', out, img.size)
