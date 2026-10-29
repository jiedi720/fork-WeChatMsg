# -*- coding: utf-8 -*-
"""给分享链接（Type 49 / type=5）抓缩略图，让卡片恢复成微信里的样子。

微信自己是抓网页的 og:image 做封面，这里用同样的办法：
  1. 从两个库里把分享消息的 <url> 抠出来去重；
  2. 抓 HTML 里的 og:image / twitter:image / 微信专用 msg_cdn_url；
  3. 下载封面图，缩到 240px 存 export/media/og/<md5(url)>.<ext>；
  4. 输出 og_map.json 给导出器查。

用法:
    .venv\\Scripts\\python.exe fetch_link_thumbs.py [--limit 100]
"""
import argparse
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
HDR = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,image/*,*/*;q=0.8',
       'Accept-Language': 'zh-CN,zh;q=0.9'}

OG_PATTERNS = [
    r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image',
    r'<meta[^>]+(?:property|name)=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+name=["\']msg_cdn_url["\'][^>]+content=["\']([^"\']+)',   # 微信文章专用
]

MAX_W = 240
URL_RE = re.compile(r'<url>(?:<!\[CDATA\[)?(https?://[^<\]]+)')


def collect(db_path, urls):
    if not os.path.exists(db_path):
        return 0
    c = sqlite3.connect('file:%s?mode=ro' % db_path.replace('\\', '/'), uri=True)
    n = 0
    for (s,) in c.execute("SELECT StrContent FROM MSG WHERE Type=49"):
        if not s or ('<type>5<' not in s and '<type>4<' not in s):
            continue
        m = URL_RE.search(html.unescape(s))
        if not m:
            continue
        u = m.group(1).strip().rstrip('.,);\'"')
        if u.startswith('http'):
            urls.add(u)
            n += 1
    c.close()
    return n


def pick_og(html_text, base):
    for pat in OG_PATTERNS:
        m = re.search(pat, html_text, re.I)
        if m:
            u = html.unescape(m.group(1).strip())
            if u.startswith('//'):
                u = 'https:' + u
            return urljoin(base, u)
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--msg-dir', default=os.path.join('app', 'DataBase', 'Msg'))
    ap.add_argument('--extra-msg', default=r'D:\Temp\wxold\BackupMSG.db')
    ap.add_argument('--out', default=os.path.join('export', 'media', 'og'))
    ap.add_argument('--map', default='og_map.json')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--threads', type=int, default=16)
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    print('1) 收集分享链接 …')
    urls = set()
    n1 = collect(os.path.join(args.msg_dir, 'MSG.db'), urls)
    n2 = collect(args.extra_msg, urls)
    print('   本机 %d 条 / 备份 %d 条 → 唯一链接 %d 个' % (n1, n2, len(urls)))

    mapping = {}
    if os.path.exists(args.map):
        try:
            mapping = json.load(open(args.map, encoding='utf-8'))
            print('   已有 %d 条结果，跳过' % len(mapping))
        except Exception:
            mapping = {}

    todo = [u for u in urls if u not in mapping]
    if args.limit:
        todo = todo[:args.limit]

    lock = threading.Lock()
    stat = {'ok': 0, 'noog': 0, 'fail': 0, 'skip': 0}
    page_cache = {}
    tls = threading.local()

    def sess():
        s = getattr(tls, 's', None)
        if s is None:
            s = requests.Session()
            s.headers.update(HDR)
            tls.s = s
        return s

    try:
        from PIL import Image
    except ImportError:
        Image = None

    def one(u):
        try:
            r = sess().get(u, timeout=8, allow_redirects=True)
            if r.status_code != 200 or 'html' not in (r.headers.get('Content-Type') or ''):
                with lock:
                    stat['fail'] += 1
                    mapping[u] = ''
                return
            head = r.text[:400000]
            img = pick_og(head, r.url)
            if not img:
                with lock:
                    stat['noog'] += 1
                    mapping[u] = ''
                return
            ir = sess().get(img, timeout=10, referer=u) if False else sess().get(img, timeout=10, headers={'Referer': u})
            if ir.status_code != 200 or len(ir.content) < 200:
                with lock:
                    stat['noog'] += 1
                    mapping[u] = ''
                return
            data, ext = ir.content, None
            for mag, e in ((b'\xff\xd8\xff', 'jpg'), (b'\x89PNG', 'png'), (b'GIF8', 'gif'), (b'RIFF', 'webp')):
                if data.startswith(mag):
                    ext = e
                    break
            if not ext:
                with lock:
                    stat['noog'] += 1
                    mapping[u] = ''
                return
            if Image is not None:
                try:
                    im = Image.open(io.BytesIO(data))
                    if max(im.size) > MAX_W:
                        im.thumbnail((MAX_W, MAX_W), Image.LANCZOS)
                    buf = io.BytesIO()
                    if im.mode in ('RGBA', 'P', 'LA'):
                        im.convert('RGB').save(buf, 'JPEG', quality=80, optimize=True)
                    else:
                        im.convert('RGB').save(buf, 'JPEG', quality=80, optimize=True)
                    data, ext = buf.getvalue(), 'jpg'
                except Exception:
                    pass
            name = hashlib.md5(u.encode('utf-8')).hexdigest()[:20] + '.' + ext
            with open(os.path.join(outdir, name), 'wb') as f:
                f.write(data)
            with lock:
                stat['ok'] += 1
                mapping[u] = 'og/' + name
        except Exception:
            with lock:
                stat['fail'] += 1
                mapping[u] = ''

    print('2) 抓取 %d 个链接（%d 线程）…' % (len(todo), args.threads))
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for _ in ex.map(one, todo):
            done += 1
            if done % 100 == 0:
                el = time.time() - t0
                print('   %d/%d  封面 %d  无图 %d  失败 %d  %.0fs' % (
                    done, len(todo), stat['ok'], stat['noog'], stat['fail'], el))

    json.dump({k: v for k, v in mapping.items() if v}, open(args.map, 'w', encoding='utf-8'),
              ensure_ascii=False, separators=(',', ':'))
    print('\n完成：封面 %d 张，无 OG 图 %d，抓取失败 %d，耗时 %.0fs' % (
        stat['ok'], stat['noog'], stat['fail'], time.time() - t0))
    print('映射表 %s' % args.map)


if __name__ == '__main__':
    main()
