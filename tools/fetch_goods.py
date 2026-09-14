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
import urllib.request

TOKEN_URL = 'https://api.amazon.co.jp/auth/o2/token'       # FE（日本）
API_URL = 'https://creatorsapi.amazon/catalog/v1/getItems'
MARKETPLACE = 'www.amazon.co.jp'
RESOURCES = ['itemInfo.title', 'images.primary.medium', 'offersV2.listings.price']


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', **headers}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        sys.exit(f'HTTP {e.code} {url}\n{e.read().decode("utf-8", "replace")[:800]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', default='index.html')
    ap.add_argument('--out', default='assets/goods.json')
    a = ap.parse_args()

    cid, sec = os.environ.get('CREATORS_CLIENT_ID'), os.environ.get('CREATORS_CLIENT_SECRET')
    tag = os.environ.get('AMAZON_TAG', 'redcomet-22')
    if not cid or not sec:
        sys.exit('CREATORS_CLIENT_ID / CREATORS_CLIENT_SECRET が未設定です')

    html = io.open(a.html, encoding='utf-8').read()
    asins = sorted(set(re.findall(r'class="good"[^>]*data-asin="([A-Z0-9]{10})"', html)))
    if not asins:
        sys.exit('data-asin が見つかりません: ' + a.html)
    print('ASIN:', ', '.join(asins))

    tok = post(TOKEN_URL, {'grant_type': 'client_credentials', 'client_id': cid,
                           'client_secret': sec, 'scope': 'creatorsapi::default'}, {})
    token = tok.get('access_token') or sys.exit('トークンが取れません: ' + json.dumps(tok)[:300])

    items = {}
    for i in range(0, len(asins), 10):                      # 1回10件まで
        res = post(API_URL, {'itemIds': asins[i:i + 10], 'itemIdType': 'ASIN',
                             'marketplace': MARKETPLACE, 'partnerTag': tag, 'resources': RESOURCES},
                   {'Authorization': 'Bearer ' + token, 'x-marketplace': MARKETPLACE})
        for e in res.get('errors') or []:
            print('  ! ', e.get('code'), e.get('message'))
        for it in (res.get('itemResults') or res.get('itemsResult') or {}).get('items') or []:
            listings = ((it.get('offersV2') or {}).get('listings') or [])
            price = (listings[0].get('price') or {}).get('displayAmount') if listings else None
            items[it['asin']] = {
                'title': ((it.get('itemInfo') or {}).get('title') or {}).get('displayValue'),
                'image': (((it.get('images') or {}).get('primary') or {}).get('medium') or {}).get('url'),
                'price': price,
                'url': it.get('detailPageURL'),
            }
            print('  ok', it['asin'], (items[it['asin']]['title'] or '')[:40], price)

    if not items:
        sys.exit('商品が1件も取れませんでした')
    out = {'fetchedAt': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+09:00'),
           'items': items}
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    io.open(a.out, 'w', encoding='utf-8', newline='\n').write(json.dumps(out, ensure_ascii=False, indent=1) + '\n')
    print('書き出し:', a.out, f'({len(items)}件)')


if __name__ == '__main__':
    main()
