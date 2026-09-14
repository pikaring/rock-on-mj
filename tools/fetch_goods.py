# -*- coding: utf-8 -*-
"""Amazon Creators API で商品情報（商品名・画像・価格・リンク）を取り、assets/goods.json に書き出す。

    py tools/fetch_goods.py [--html index.html] [--out assets/goods.json]

- ASIN は紹介ページの <div class="good" data-asin="..."> から拾う（一覧を二重管理しない）
- 認証情報は環境変数 CREATORS_CLIENT_ID / CREATORS_CLIENT_SECRET（GitHub Actions の Secrets）
- 失敗したら非0で終了し、既存の goods.json には触らない（ページは直前の内容で表示され続ける）
- 標準ライブラリだけで動く
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = 'https://api.amazon.co.jp/auth/o2/token'       # FE（日本）
API_URL = 'https://creatorsapi.amazon/catalog/v1/getItems'
MARKETPLACE = 'www.amazon.co.jp'
RESOURCES = ['itemInfo.title', 'images.primary.large', 'offersV2.listings.price']


def post(url, body, headers, form=False, soft=False):
    if form:
        data = urllib.parse.urlencode(body).encode('utf-8')
        ctype = 'application/x-www-form-urlencoded'
    else:
        data = json.dumps(body).encode('utf-8')
        ctype = 'application/json'
    req = urllib.request.Request(url, data=data, headers={'Content-Type': ctype, **headers}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        msg = 'HTTP %d %s | %s' % (e.code, url, e.read().decode('utf-8', 'replace')[:600].replace('\n', ' '))
        if soft:
            print('  ×', msg)
            return None
        sys.exit(msg)


def get_token(cid, sec):
    """FE（日本）のJSON形式が本来の形。ダメなら form 形式、NA・EU のエンドポイントの順に試して原因を切り分ける。"""
    body = {'grant_type': 'client_credentials', 'client_id': cid, 'client_secret': sec,
            'scope': 'creatorsapi::default'}
    tries = [(TOKEN_URL, False), (TOKEN_URL, True),
             ('https://api.amazon.com/auth/o2/token', False),
             ('https://api.amazon.co.uk/auth/o2/token', False)]
    for url, form in tries:
        print('token:', url, 'form' if form else 'json')
        tok = post(url, body, {}, form=form, soft=True)
        if tok and tok.get('access_token'):
            return tok['access_token']
    sys.exit('トークンが取れません（Credential ID / Secret / Version を確認してください）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', default='index.html')
    ap.add_argument('--out', default='assets/goods.json')
    a = ap.parse_args()

    cid, sec = os.environ.get('CREATORS_CLIENT_ID'), os.environ.get('CREATORS_CLIENT_SECRET')
    tag = os.environ.get('AMAZON_TAG', 'redcomet-22')
    if not cid or not sec:
        sys.exit('CREATORS_CLIENT_ID / CREATORS_CLIENT_SECRET が未設定です')
    # 貼り付け時の前後の空白・改行で 401 になりやすいので落としておく。中身は出さない
    cid, sec = cid.strip(), sec.strip()
    # Credential ID は amzn1.application-oa2-client.…（61文字）、Secret は amzn1.oa2-cs.v1.…（80文字）。
    # 逆に登録されていても動くように入れ替える
    if cid.startswith('amzn1.oa2-cs.') and sec.startswith('amzn1.application-oa2-client.'):
        cid, sec = sec, cid
        print('※ ID と Secret が逆に登録されているので入れ替えて使います')
    print('client_id: %d文字 (%s)  secret: %d文字' % (
        len(cid), 'amzn1.' if cid.startswith('amzn1.') else '先頭が amzn1. ではない', len(sec)))

    html = io.open(a.html, encoding='utf-8').read()
    asins = sorted(set(re.findall(r'class="good"[^>]*data-asin="([A-Z0-9]{10})"', html)))
    if not asins:
        sys.exit('data-asin が見つかりません: ' + a.html)
    print('ASIN:', ', '.join(asins))

    token = get_token(cid, sec)

    items = {}
    for i in range(0, len(asins), 10):                      # 1回10件まで
        res = post(API_URL, {'itemIds': asins[i:i + 10], 'itemIdType': 'ASIN',
                             'marketplace': MARKETPLACE, 'partnerTag': tag, 'resources': RESOURCES},
                   {'Authorization': 'Bearer ' + token, 'x-marketplace': MARKETPLACE})
        for e in res.get('errors') or []:
            print('  ! ', e.get('code'), e.get('message'))
        for it in (res.get('itemResults') or res.get('itemsResult') or {}).get('items') or []:
            # 価格は listings[].price.money.displayAmount。カートボックス（isBuyBoxWinner）を優先する
            listings = ((it.get('offersV2') or {}).get('listings') or [])
            listings = sorted(listings, key=lambda l: not l.get('isBuyBoxWinner'))
            price = ((listings[0].get('price') or {}).get('money') or {}).get('displayAmount') if listings else None
            items[it['asin']] = {
                'title': ((it.get('itemInfo') or {}).get('title') or {}).get('displayValue'),
                'image': (((it.get('images') or {}).get('primary') or {}).get('large') or {}).get('url'),
                'price': price,
                'url': it.get('detailPageURL'),
            }
            print('  ok', it['asin'], (items[it['asin']]['title'] or '')[:40], price)
            if price is None:
                print('     offersV2:', json.dumps(it.get('offersV2'), ensure_ascii=False)[:400])

    if not items:
        sys.exit('商品が1件も取れませんでした')
    out = {'fetchedAt': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+09:00'),
           'items': items}
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    io.open(a.out, 'w', encoding='utf-8', newline='\n').write(json.dumps(out, ensure_ascii=False, indent=1) + '\n')
    print('書き出し:', a.out, '(%d件)' % len(items))


if __name__ == '__main__':
    main()
