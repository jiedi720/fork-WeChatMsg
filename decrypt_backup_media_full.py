# -*- coding: utf-8 -*-
"""解手机备份里的**原图**（BAK_N_MEDIA 的 xxx_backup / xxx__hd 段）

来由：备份消息里的 MediaId 指向的是 `xxx_backup__thumb`（缩略图），
     所以之前只解出 4.9 万张缩略图。把 `__thumb` 去掉就是同一张图的原图 id，
     段表和密钥完全一样，能解出 1000~2000px 的真原图。

用法:
    .venv\\Scripts\\python.exe decrypt_backup_media_full.py [--limit 100] [--max-w 1280]
"""
import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
import time

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad

MAGICS = [
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
    (b'RIFF', 'webp'),
    (b'BM', 'bmp'),
]


def kind(b):
    for m, e in MAGICS:
        if b[:len(m)] == m:
            return e
    return None


def wxgf_first_frame(data):
    """wxgf 是微信私有的 HEVC 动图容器：跳过 33 字节头，后面是标准 Annex-B 流
    （VPS 40010c01 / SPS / PPS / IDR）。取首帧转 RGBA 位图。"""
    p = data.find(b'\x00\x00\x00\x01\x40\x01')
    if p < 0:
        return None
    try:
        import av
        import io as _io
        c = av.open(_io.BytesIO(data[p:]), format='hevc')
        img = None
        for fr in c.decode(video=0):
            img = fr.to_image()
            break
        c.close()
        return img
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=r'D:\Temp\wxold\Backup_phone.plain.db')
    ap.add_argument('--msgdb', default=r'D:\Temp\wxold\BackupMSG.db')
    ap.add_argument('--nas', default=r'\\DXP4800PLUS\娱乐\微信备份\android_b0504eb8aea550ef668cc844cf06aaf4')
    ap.add_argument('--out', default=r'D:\Tools\WeChatMsg\export\media')
    ap.add_argument('--map', default=r'D:\Tools\WeChatMsg\bf_media_map.json')
    ap.add_argument('--max-w', type=int, default=1280, help='原图最长边超过就等比缩小')
    ap.add_argument('--quality', type=int, default=82)
    ap.add_argument('--max-mb', type=float, default=1.6, help='缩放后仍超过就再压一档')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    key = open(os.path.join(here, 'backup_key.txt'), encoding='utf-8').read().split()[0][:16].encode()

    db = sqlite3.connect('file:%s?mode=ro' % args.db.replace('\\', '/'), uri=True)
    id_of = {s: str(m) for s, m in db.execute('SELECT MediaIdStr, MediaId FROM MsgMedia') if s}
    segs = {}
    for mk, inner, ln, tl, fn, off in db.execute(
            'SELECT MapKey, InnerOffSet, Length, TotalLen, FileName, OffSet FROM MsgFileSegment'):
        segs.setdefault(str(mk), []).append((inner, ln, tl, fn, off))
    db.close()

    # 消息里引用的是 thumb id；去掉后缀就是原图 id
    msg = sqlite3.connect('file:%s?mode=ro' % args.msgdb.replace('\\', '/'), uri=True)
    want = []
    seen = set()
    for (m,) in msg.execute("SELECT MediaId FROM MSG WHERE Type=3 AND MediaId<>''"):
        base = m[:-7] if m.endswith('__thumb') else m
        if base not in seen:
            seen.add(base)
            want.append(base)
    msg.close()
    print('消息引用图片 %d 张（去重后的原图 id）' % len(want))

    plan = []
    miss = 0
    for idstr in want:
        mid = id_of.get(idstr)
        if mid is None:
            miss += 1
            continue
        ss = segs.get(mid)
        if not ss:
            miss += 1
            continue
        plan.append((idstr, sorted(ss)))
    print('能在备份里定位 %d 张，缺失 %d 张' % (len(plan), miss))
    if args.limit:
        plan = plan[:args.limit]
    if not plan:
        sys.exit('没有可解的媒体')

    total_len = sum(max(s[2] for s in ss) for _, ss in plan)
    print('原始体积约 %.2f GB，开始解密…' % (total_len / 1073741824))

    outdir = os.path.join(args.out, 'bf')
    os.makedirs(outdir, exist_ok=True)
    mapping = {}
    if os.path.exists(args.map):          # 断点续跑：上一轮解出来的不再重复解
        try:
            mapping.update(json.load(open(args.map, encoding='utf-8')))
            print('已有产物 %d 张，本轮只补缺' % len(mapping))
        except Exception:
            pass
    done = ok = bad = wxgf_ok = 0
    bytes_out = 0
    t0 = time.time()
    handles = {}

    try:
        from PIL import Image
    except ImportError:
        Image = None

    def read_seg(fn, off, ln, retry=2):
        """SMB 上长跑容易把缓存的句柄放坏，这里每次重开一次，读不到再试。"""
        p = os.path.join(args.nas, fn)
        for i in range(retry):
            try:
                with open(p, 'rb') as h:
                    h.seek(off)
                    raw = h.read(ln)
                if len(raw) >= 16:
                    return raw
            except OSError:
                pass
            time.sleep(0.4)
        return b''

    for idstr, ss in plan:
        if idstr in mapping:
            continue
        buf = bytearray(max(s[2] for s in ss))
        bad_seg = False
        for inner, ln, tl, fn, off in ss:
            raw = read_seg(fn, off, ln)
            if not raw:
                bad_seg = True
                break
            pt = AES.new(key, AES.MODE_ECB).decrypt(raw[:len(raw) // 16 * 16])
            try:
                pt = unpad(pt, 16)
            except ValueError:
                pass
            buf[inner:inner + len(pt)] = pt
        done += 1
        if done % 500 == 0:
            el = time.time() - t0
            sys.stdout.write('\r  %d/%d  %d 张  %.0f MB  %.0f 张/s' % (
                done, len(plan), ok, bytes_out / 1048576, done / max(el, .01)))
            sys.stdout.flush()
        if bad_seg:
            bad += 1
            continue
        data = bytes(buf)
        ext = kind(data)
        if not ext:
            if data[:4] == b'wxgf':
                im = wxgf_first_frame(data)
                if im is None:
                    bad += 1
                    continue
                if max(im.size) > args.max_w:
                    sc = args.max_w / max(im.size)
                    im = im.resize((max(1, round(im.size[0] * sc)), max(1, round(im.size[1] * sc))),
                                   Image.LANCZOS)
                o = io.BytesIO()
                im.convert('RGB').save(o, 'JPEG', quality=args.quality, optimize=True)
                data, ext = o.getvalue(), 'jpg'
                wxgf_ok += 1
            else:
                bad += 1
                continue

        # 太大就缩：聊天记录翻看用不到 4000px 的原图
        if Image is not None and ext in ('jpg', 'png', 'bmp'):
            try:
                im = Image.open(io.BytesIO(data))
                w, h = im.size
                need = max(w, h) > args.max_w or len(data) > args.max_mb * 1048576
                if need:
                    if max(w, h) > args.max_w:
                        sc = args.max_w / max(w, h)
                        im = im.resize((max(1, round(w * sc)), max(1, round(h * sc))), Image.LANCZOS)
                    o = io.BytesIO()
                    if im.mode in ('RGBA', 'LA', 'P'):
                        im.convert('RGB').save(o, 'JPEG', quality=args.quality, optimize=True)
                    else:
                        im.convert('RGB').save(o, 'JPEG', quality=args.quality, optimize=True)
                    data, ext = o.getvalue(), 'jpg'
            except Exception:
                pass

        name = hashlib.md5(idstr.encode('utf-8')).hexdigest()[:20] + '.' + ext
        with open(os.path.join(outdir, name), 'wb') as w:
            w.write(data)
        mapping[idstr] = 'bf/' + name
        ok += 1
        bytes_out += len(data)

    json.dump(mapping, open(args.map, 'w', encoding='utf-8'), ensure_ascii=False)
    print('\n解出原图 %d 张（其中 wxgf 动图首帧 %d 张，跳过 %d），%.2f GB，耗时 %.0f s' % (
        ok, wxgf_ok, bad, bytes_out / 1073741824, time.time() - t0))
    print('映射表 %s' % args.map)


if __name__ == '__main__':
    main()
