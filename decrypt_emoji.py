# -*- coding: utf-8 -*-
"""
恢复表情消息（Type 47）的图片。

思路：
  1. 从 MSG.db（本机）+ BackupMSG.db（手机备份）里把每条表情消息的 XML 抽出来，
     取 md5 / cdnurl / thumburl；
  2. 按 md5 去重（同一个表情被发了 N 次只下一次）；
  3. 先查本地 CustomEmotion 缓存有没有（微信自己缓存的原图），没有再走 CDN 下载；
  4. 落到 export/media/emo/<md5>.<ext>，同时输出 emoji_cdn_map.json 给导出器用。

用法:
    .venv\\Scripts\\python.exe decrypt_emoji.py            # 全量
    .venv\\Scripts\\python.exe decrypt_emoji.py --limit 200  # 试跑
"""
import argparse
import collections
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

MD5_RE = re.compile(r'\bmd5\s*=\s*"([0-9a-fA-F]{32})"')
CDN_RE = re.compile(r'\bcdnurl\s*=\s*"([^"]+)"')
THUMB_RE = re.compile(r'\bthumburl\s*=\s*"([^"]+)"')

MAGICS = [
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'GIF8', 'gif'),
    (b'RIFF', 'webp'),
    (b'BM', 'bmp'),
]

MAX_GIF = 900 * 1024          # 动图上限：超过就逐帧缩放，但仍是动图
MAX_STATIC = 1600 * 1024      # 静态图上限：超过就缩到 STATIC_W
STK_W = 160                   # 页面里表情的显示宽度，缩放目标就是它
STATIC_W = 1000
HEADERS = {'User-Agent': 'MicroMessenger Client', 'Referer': 'https://wx.qq.com/'}


def sniff(data):
    for m, e in MAGICS:
        if data.startswith(m):
            return e
    return None


def shrink_gif(data):
    """GIF / 动图 webp：逐帧缩放后重存 GIF —— 保留动画，只是变小。

    帧数太多时隔帧取，并把 duration 翻倍，总时长不变，体积再砍一半。
    """
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    n = getattr(im, 'n_frames', 1) or 1
    step = 1 if n <= 60 else 2
    frames, durs = [], []
    for i in range(0, n, step):
        im.seek(i)
        f = im.convert('RGBA')
        w, h = f.size
        if w > STK_W:
            f = f.resize((STK_W, max(1, round(h * STK_W / w))), Image.LANCZOS)
        frames.append(f)
        d = im.info.get('duration', 80) or 80
        durs.append(min(1000, d * step))
    if not frames:
        return data, None
    buf = io.BytesIO()
    frames[0].save(buf, 'GIF', save_all=True, append_images=frames[1:],
                   duration=durs, loop=0, disposal=2, optimize=True)
    return buf.getvalue(), 'gif'


def shrink_static(data):
    """静态大图（jpg/png）：等比缩到 STATIC_W，质量不掉太狠。"""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    if w > STATIC_W:
        im = im.resize((STATIC_W, max(1, round(h * STATIC_W / w))), Image.LANCZOS)
    buf = io.BytesIO()
    if im.mode in ('RGBA', 'P', 'LA'):
        im.convert('RGBA').save(buf, 'PNG', optimize=True)
        return buf.getvalue(), 'png'
    im.convert('RGB').save(buf, 'JPEG', quality=85, optimize=True)
    return buf.getvalue(), 'jpg'


def collect(md5_urls, db, only_existing=False):
    """把库里 Type 47 的 (md5, cdnurl, thumburl) 收进 md5_urls。"""
    if not os.path.exists(db):
        print(f'  跳过（不存在）{db}')
        return 0
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    n = 0
    for (s,) in con.execute("SELECT StrContent FROM MSG WHERE Type=47"):
        if not s:
            continue
        s = html.unescape(s)
        m = MD5_RE.search(s)
        if not m:
            continue
        md5 = m.group(1).lower()
        c = CDN_RE.search(s)
        t = THUMB_RE.search(s)
        rec = md5_urls.get(md5)
        if rec is None:
            md5_urls[md5] = {'cdn': c.group(1) if c else '', 'thb': t.group(1) if t else ''}
        else:
            if not rec['cdn'] and c:
                rec['cdn'] = c.group(1)
            if not rec['thb'] and t:
                rec['thb'] = t.group(1)
        n += 1
    con.close()
    print(f'  {os.path.basename(db)}: {n} 条表情消息')
    return n


def scan_local(root):
    """微信本地表情缓存：CustomEmotion\<XX>\<大写MD5>，文件名即 md5。"""
    out = {}
    if not os.path.isdir(root):
        return out
    for d in os.listdir(root):
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        for f in os.listdir(p):
            out[f.lower()] = os.path.join(p, f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--msg-dir', default=os.path.join('app', 'DataBase', 'Msg'))
    ap.add_argument('--extra-msg', default=r'D:\Temp\wxold\BackupMSG.db')
    ap.add_argument('--wx-root', default=r'E:\OneDrive\Documents\WeChat Files\wxid_your_account')
    ap.add_argument('--out', default=os.path.join('export', 'media', 'emo'))
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--threads', type=int, default=12)
    ap.add_argument('--force', action='store_true', help='忽略已有产物，全部重下（换压缩策略后用）')
    args = ap.parse_args()
    force = args.force

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    print('1) 收集表情 md5 …')
    md5_urls = {}
    collect(md5_urls, os.path.join(args.msg_dir, 'MSG.db'))
    collect(md5_urls, args.extra_msg)
    print(f'   唯一表情 {len(md5_urls)} 个')

    local = scan_local(os.path.join(args.wx_root, 'FileStorage', 'CustomEmotion'))
    print(f'2) 本地 CustomEmotion 缓存 {len(local)} 个文件')

    # 已有产出先当缓存，支持断点续跑
    have = {}
    for f in os.listdir(out_dir):
        stem, _, ext = f.rpartition('.')
        if stem and ext:
            have[stem] = f
    print(f'   已存在产物 {len(have)} 个')

    lock = threading.Lock()
    stat = collections.Counter()
    size_total = [0]

    sess_local = threading.local()

    def session():
        s = getattr(sess_local, 's', None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            sess_local.s = s
        return s

    def fetch_one(md5):
        if (not force) and md5 in have:
            with lock:
                stat['skip'] += 1
            return md5, have[md5]
        rec = md5_urls[md5]
        urls = [(rec['cdn'], True), (rec['thb'], False)]
        for url, full in urls:
            if not url:
                continue
            for attempt in range(2):
                try:
                    r = session().get(url, timeout=20)
                    if r.status_code != 200:
                        break
                    data = r.content
                    ext = sniff(data)
                    if not ext or len(data) < 64:
                        break
                    anim = ext in ('gif', 'webp')
                    cap = MAX_GIF if anim else MAX_STATIC
                    # 静态图过大且还有缩略图可退，改用缩略图
                    if full and (not anim) and len(data) > cap and rec['thb'] and url != rec['thb']:
                        break
                    if len(data) > cap:
                        nd, ne = (shrink_gif if anim else shrink_static)(data)
                        if ne:
                            data, ext = nd, ne
                    name = f'{md5}.{ext}'
                    tmp = os.path.join(out_dir, name + '.part')
                    with open(tmp, 'wb') as fp:
                        fp.write(data)
                    os.replace(tmp, os.path.join(out_dir, name))
                    with lock:
                        stat['ok'] += 1
                        stat[ext] += 1
                        size_total[0] += len(data)
                    return md5, name
                except Exception:
                    if attempt == 1:
                        break
                    time.sleep(0.6)
        with lock:
            stat['fail'] += 1
        return md5, None

    targets = list(md5_urls)
    if args.limit:
        targets = targets[:args.limit]
    print(f'3) 开始下载 {len(targets)} 个（{args.threads} 线程）…')
    t0 = time.time()
    result = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for md5, name in ex.map(fetch_one, targets):
            done += 1
            if name:
                result[md5] = 'emo/' + name
            if done % 500 == 0:
                el = time.time() - t0
                print(f'   {done}/{len(targets)}  成功 {stat["ok"]}  失败 {stat["fail"]}  '
                      f'{el:.0f}s  预计剩余 {el / done * (len(targets) - done):.0f}s')

    # 把上一轮已经有的也并进 map，避免重跑后旧图丢引用
    for f in os.listdir(out_dir):
        stem, _, ext = f.rpartition('.')
        if stem and ext and ext in ('jpg', 'png', 'gif', 'webp', 'bmp'):
            result.setdefault(stem, 'emo/' + f)

    map_path = os.path.join('export', 'emoji_cdn_map.json')
    with open(map_path, 'w', encoding='utf-8') as fp:
        json.dump(result, fp, ensure_ascii=False, separators=(',', ':'))

    print(f'\n完成：成功 {stat["ok"]}，复用 {stat["skip"]}，失败 {stat["fail"]}')
    print('格式分布:', {k: v for k, v in stat.items() if k in ('jpg', 'png', 'gif', 'webp', 'bmp')})
    print(f'总大小 {size_total[0] / 1048576:.1f} MB，耗时 {time.time() - t0:.0f}s')
    print('映射表:', map_path, f'（{len(result)} 条）')
    print('输出目录:', out_dir)


if __name__ == '__main__':
    main()
