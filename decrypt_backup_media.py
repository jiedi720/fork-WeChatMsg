# -*- coding: utf-8 -*-
"""解密手机备份里的图片（BAK_N_MEDIA）

格式：每个媒体文件被切成若干段，位置记录在 Backup.db 的 MsgFileSegment
      (MapKey=MsgMedia.MediaId, FileName, OffSet, Length, InnerOffSet, TotalLen)
      段内容同样是 AES-ECB + 密钥前 16 字节，解出来就是原始文件（jpg/png/gif/webp/pdf…）

用法:
    .venv\\Scripts\\python.exe decrypt_backup_media.py [--nas <备份目录>] [--out <export/media>]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

from Cryptodome.Cipher import AES


def kind(b):
    if b[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if b[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'webp'
    if b[:2] == b'BM':
        return 'bmp'
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=r'D:\Temp\wxold\Backup_phone.plain.db')
    ap.add_argument('--ids', default=r'D:\Temp\wxold\bk_media_ids.json')
    ap.add_argument('--nas', default=r'\\DXP4800PLUS\娱乐\微信备份\android_b0504eb8aea550ef668cc844cf06aaf4')
    ap.add_argument('--out', default=r'D:\Tools\WeChatMsg\export\media')
    ap.add_argument('--map', default=r'D:\Tools\WeChatMsg\bk_media_map.json')
    args = ap.parse_args()

    key = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup_key.txt'),
               encoding='utf-8').read().split()[0][:16].encode()

    db = sqlite3.connect('file:%s?mode=ro' % args.db.replace('\\', '/'), uri=True)
    str2id = {r[0]: r[1] for r in db.execute('SELECT MediaIdStr, MediaId FROM MsgMedia')}
    # MapKey(MediaId) -> (FileName, OffSet, Length)
    seg = {}
    for mk, fn, off, ln in db.execute('SELECT MapKey, FileName, OffSet, Length FROM MsgFileSegment'):
        seg.setdefault(str(mk), []).append((fn, off, ln))
    db.close()

    ids = json.load(open(args.ids, encoding='utf-8'))
    print('待解媒体 id %d 个' % len(ids))

    # 按文件归组，顺序读，减少 NAS 随机跳转
    plan = {}
    skip = 0
    for idstr in ids:
        mid = str2id.get(idstr)
        if mid is None:
            skip += 1
            continue
        for fn, off, ln in seg.get(str(mid), []):
            plan.setdefault(fn, []).append((off, ln, idstr))
    if skip:
        print('找不到索引跳过 %d 个' % skip)

    outdir = os.path.join(args.out, 'bk')
    os.makedirs(outdir, exist_ok=True)
    mapping = {}
    done = 0
    bytes_out = 0
    t0 = time.time()
    total_seg = sum(len(v) for v in plan.values())

    for fn in sorted(plan):
        path = os.path.join(args.nas, fn)
        if not os.path.exists(path):
            print('缺文件', path)
            continue
        rows = sorted(plan[fn])
        with open(path, 'rb') as f:
            for off, ln, idstr in rows:
                if idstr in mapping:
                    continue
                f.seek(off)
                raw = f.read(ln)
                if len(raw) < 16:
                    continue
                pt = AES.new(key, AES.MODE_ECB).decrypt(raw[:len(raw) // 16 * 16])
                k = kind(pt)
                if not k:
                    continue
                name = hashlib.md5(idstr.encode('utf-8')).hexdigest()[:20] + '.' + k
                with open(os.path.join(outdir, name), 'wb') as w:
                    w.write(pt)
                mapping[idstr] = 'bk/' + name
                done += 1
                bytes_out += len(pt)
                if done % 2000 == 0:
                    el = time.time() - t0
                    sys.stdout.write('\r  %d/%d 段  %d 张  %.0f MB  %.1f MB/s' % (
                        done, total_seg, len(mapping), bytes_out / 1048576,
                        bytes_out / 1048576 / max(el, .01)))
                    sys.stdout.flush()
        print('\n  完成 %s' % fn)

    json.dump(mapping, open(args.map, 'w', encoding='utf-8'), ensure_ascii=False)
    print('\n解出 %d 张图片  %.1f MB  耗时 %.0f s' % (
        len(mapping), bytes_out / 1048576, time.time() - t0))
    print('映射表 %s' % args.map)


if __name__ == '__main__':
    main()
