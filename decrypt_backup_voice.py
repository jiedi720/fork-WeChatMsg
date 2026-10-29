# -*- coding: utf-8 -*-
"""解密手机备份里的语音（SILK → MP3）

链路：MsgFileSegment 定位 → AES-ECB(前16字节密钥) → 去掉 \x02#!SILK_V3 头
      → 按 2 字节小端长度切帧（丢掉尾部残帧）→ pysilk 解码成 PCM → lameenc 编码 MP3

用法:
    .venv\\Scripts\\python.exe decrypt_backup_voice.py
"""
import argparse
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys
import time

import lameenc
import pysilk
from Cryptodome.Cipher import AES

SILK_HDR = b'#!SILK_V3'


def frames_of(pt):
    """切出完整的 SILK 帧（2 字节小端长度 + 数据）"""
    i = 10 if pt[:10] == b'\x02#!SILK_V3' else (9 if pt[:9] == SILK_HDR else -1)
    if i < 0:
        return []
    data = pt[i:]
    out = []
    n = len(data)
    j = 0
    while j + 2 <= n:
        ln = int.from_bytes(data[j:j + 2], 'little')
        j += 2
        if ln <= 0 or ln > 300 or j + ln > n:
            break
        out.append(data[j:j + ln])
        j += ln
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=r'D:\Temp\wxold\Backup_phone.plain.db')
    ap.add_argument('--msgs', default=r'D:\Temp\wxold\BackupMSG.db')
    ap.add_argument('--nas', default=r'\\DXP4800PLUS\娱乐\微信备份\android_b0504eb8aea550ef668cc844cf06aaf4')
    ap.add_argument('--out', default=r'D:\Tools\WeChatMsg\export\media')
    ap.add_argument('--map', default=r'D:\Tools\WeChatMsg\voice_map.json')
    args = ap.parse_args()

    key = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup_key.txt'),
               encoding='utf-8').read().split()[0][:16].encode()

    m = sqlite3.connect('file:%s?mode=ro' % args.msgs.replace('\\', '/'), uri=True)
    ids = [r[0] for r in m.execute(
        "SELECT DISTINCT MediaId FROM MSG WHERE Type=34 AND MediaId<>''")]
    m.close()
    print('待解语音 %d 条' % len(ids))

    db = sqlite3.connect('file:%s?mode=ro' % args.db.replace('\\', '/'), uri=True)
    str2id = {r[0]: r[1] for r in db.execute('SELECT MediaIdStr, MediaId FROM MsgMedia')}
    seg = {}
    for mk, fn, off, ln in db.execute('SELECT MapKey, FileName, OffSet, Length FROM MsgFileSegment'):
        seg.setdefault(str(mk), []).append((fn, off, ln))
    db.close()

    plan = {}
    for idstr in ids:
        mid = str2id.get(idstr)
        if mid is None:
            continue
        for fn, off, ln in seg.get(str(mid), []):
            plan.setdefault(fn, []).append((off, ln, idstr))

    outdir = os.path.join(args.out, 'voice')
    os.makedirs(outdir, exist_ok=True)
    mapping = {}
    frames_total = 0
    t0 = time.time()
    done = 0
    secs = 0.0

    for fn in sorted(plan):
        path = os.path.join(args.nas, fn)
        if not os.path.exists(path):
            print('缺文件', path)
            continue
        with open(path, 'rb') as f:
            for off, ln, idstr in sorted(plan[fn]):
                if idstr in mapping:
                    continue
                f.seek(off)
                raw = f.read(ln)
                if len(raw) < 32:
                    continue
                pt = AES.new(key, AES.MODE_ECB).decrypt(raw[:len(raw) // 16 * 16])
                fr = frames_of(pt)
                if not fr:
                    continue
                try:
                    pcm = io.BytesIO()
                    pysilk.decode(io.BytesIO(SILK_HDR + b''.join(
                        struct.pack('<h', len(x)) + x for x in fr)), pcm, 24000)
                    pcm = pcm.getvalue()
                except Exception:
                    continue
                if len(pcm) < 2000:
                    continue
                enc = lameenc.Encoder()
                enc.set_bit_rate(32)
                enc.set_in_sample_rate(24000)
                enc.set_channels(1)
                enc.set_quality(5)
                mp3 = enc.encode(pcm) + enc.flush()
                name = hashlib.md5(idstr.encode('utf-8')).hexdigest()[:20] + '.mp3'
                with open(os.path.join(outdir, name), 'wb') as w:
                    w.write(mp3)
                dur = round(len(pcm) / 2 / 24000, 1)
                mapping[idstr] = {'p': 'voice/' + name, 'd': dur}
                frames_total += len(fr)
                secs += dur
                done += 1
                if done % 100 == 0:
                    el = time.time() - t0
                    sys.stdout.write('\r  %d 条  %.0f 分钟音频  %.0f s' % (done, secs / 60, el))
                    sys.stdout.flush()

    json.dump(mapping, open(args.map, 'w', encoding='utf-8'), ensure_ascii=False)
    print('\n解出 %d 条语音  总时长 %.1f 分钟  帧 %d  耗时 %.0f s' % (
        len(mapping), secs / 60, frames_total, time.time() - t0))
    if mapping:
        sizes = [os.path.getsize(os.path.join(outdir, os.path.basename(v['p'])))
                 for v in list(mapping.values())[:200]]
        print('平均 %.0f KB/条  映射表 %s' % (sum(sizes) / len(sizes) / 1024, args.map))


if __name__ == '__main__':
    main()
