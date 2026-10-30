# -*- coding: utf-8 -*-
"""把手机备份里的**视频原文件**解出来（BAK_N_MEDIA 的 xxx_backup 段）。

来由：备份消息 type=43 的 MediaId 指向 `xxx_backup__thumb`，
     去掉 `__thumb` 就是原视频 id，段表/密钥和图片完全一样，
     解出来是标准 MP4（ftypisom），实测 10~15MB，是完整视频。

体积控制：小于 --direct-max 的原样复制（保留音轨）；
         超过的用 libx264 压到 640px、crf 30、无音轨。

用法:
    .venv\\Scripts\\python.exe decrypt_backup_video.py [--limit N] [--max-side 640]
"""
import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
from fractions import Fraction

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=r'D:\Temp\wxold\Backup_phone.plain.db')
    ap.add_argument('--msgdb', default=r'D:\Temp\wxold\BackupMSG.db')
    ap.add_argument('--nas', default=r'\\DXP4800PLUS\娱乐\微信备份\android_b0504eb8aea550ef668cc844cf06aaf4')
    ap.add_argument('--out', default=r'D:\Tools\WeChatMsg\export\media')
    ap.add_argument('--map', default=r'D:\Tools\WeChatMsg\bk_video_map.json')
    ap.add_argument('--max-side', type=int, default=640)
    ap.add_argument('--crf', type=int, default=30)
    ap.add_argument('--direct-max', type=float, default=1000.0,
                    help='小于这个 MB 的原样复制（实测全部 440 条共 1.66GB，中位 1.9MB，全量复制即可）')
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

    msg = sqlite3.connect('file:%s?mode=ro' % args.msgdb.replace('\\', '/'), uri=True)
    want, seen = [], set()
    for (m,) in msg.execute("SELECT MediaId FROM MSG WHERE Type=43 AND MediaId<>''"):
        b = m[:-7] if m.endswith('__thumb') else m
        if b not in seen:
            seen.add(b)
            want.append(b)
    msg.close()

    plan = []
    for idstr in want:
        mid = id_of.get(idstr)
        if mid is None:
            continue
        ss = segs.get(mid)
        if not ss:
            continue
        plan.append((idstr, sorted(ss)))
    print('视频消息 %d 条，能在备份里定位 %d 条' % (len(want), len(plan)))
    if args.limit:
        plan = plan[:args.limit]
    if not plan:
        sys.exit('没有可解的视频')

    total = sum(max(s[2] for s in ss) for _, ss in plan)
    print('原始体积约 %.2f GB' % (total / 1073741824))

    outdir = os.path.join(args.out, 'bv')
    os.makedirs(outdir, exist_ok=True)
    mapping = {}
    if os.path.exists(args.map):
        try:
            mapping.update(json.load(open(args.map, encoding='utf-8')))
            print('已有产物 %d 条，本轮只补缺' % len(mapping))
        except Exception:
            pass

    try:
        from PIL import Image
    except ImportError:
        sys.exit('缺 Pillow')

    def read_seg(fn, off, ln, retry=2):
        p = os.path.join(args.nas, fn)
        for _ in range(retry):
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

    done = ok = bad = small = 0
    bytes_out = 0
    t0 = time.time()

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
        if done % 20 == 0:
            el = time.time() - t0
            sys.stdout.write('\r  %d/%d  解出 %d 条  %.0f MB  %.1f 条/s   ' % (
                done, len(plan), ok, bytes_out / 1048576, done / max(el, .01)))
            sys.stdout.flush()
        if bad_seg:
            bad += 1
            continue
        data = bytes(buf)
        if data[4:8] != b'ftyp':
            bad += 1
            continue

        base = hashlib.md5(idstr.encode('utf-8')).hexdigest()[:20]
        mp4_name = base + '.mp4'
        jpg_name = base + '.jpg'
        mp4_path = os.path.join(outdir, mp4_name)
        jpg_path = os.path.join(outdir, jpg_name)

        import av
        dur = 0
        try:
            with av.open(io.BytesIO(data)) as c:
                vs = c.streams.video[0]
                if vs.duration and vs.time_base:
                    dur = float(vs.duration * vs.time_base)
                elif c.duration:
                    dur = float(c.duration) / 1e6
        except Exception:
            bad += 1
            continue

        if len(data) <= args.direct_max * 1048576:
            with open(mp4_path, 'wb') as w:
                w.write(data)
            got_jpg = False
            if not os.path.exists(jpg_path):
                try:
                    with av.open(io.BytesIO(data)) as c:
                        for fr in c.decode(video=0):
                            fr.to_image().save(jpg_path, quality=82)
                            got_jpg = True
                            break
                except Exception:
                    pass
            small += 1
        else:
            got_jpg = False
            try:
                with av.open(io.BytesIO(data)) as r:
                    vs = r.streams.video[0]
                    W, H = vs.codec_context.width, vs.codec_context.height
                    sc = min(1.0, float(args.max_side) / max(W, H))
                    W2 = max(2, int(W * sc) // 2 * 2)
                    H2 = max(2, int(H * sc) // 2 * 2)
                    # 帧率要取整数：微信视频常是 4992000/166399 这种分数，
                    # 直接喂给 add_stream 会推不出 time_base（None），mux 报 EINVAL。
                    try:
                        rate = max(1, int(round(float(vs.average_rate or 25))))
                    except (TypeError, ValueError):
                        rate = 25
                    with av.open(mp4_path, mode='w', format='mp4') as w:
                        # 注意：不能给 add_stream 传 options —— mp4 muxer 会把它当成
                        # 自己的选项，mux 时报 EINVAL(22)。用默认编码参数即可。
                        vo = w.add_stream('libx264', rate=rate, width=W2, height=H2,
                                          pix_fmt='yuv420p')
                        vo.time_base = Fraction(1, rate)
                        i = 0
                        for fr in r.decode(video=0):
                            if i == 0:
                                try:
                                    # 用 reformat 出的新帧转图，避免 to_image 影响原帧
                                    fr.reformat(format='rgb24').to_image().save(jpg_path, quality=82)
                                    got_jpg = True
                                except Exception:
                                    pass
                            nf = fr.reformat(width=W2, height=H2, format='yuv420p')
                            nf.pts = i
                            i += 1
                            for pkt in vo.encode(nf):
                                w.mux(pkt)
                        for pkt in vo.encode(None):
                            w.mux(pkt)
            except Exception as e:
                import traceback
                if done <= 8:
                    traceback.print_exc()
                try:
                    os.remove(mp4_path)
                except OSError:
                    pass
                bad += 1
                continue

        sz = os.path.getsize(mp4_path)
        bytes_out += sz
        mapping[idstr] = {
            'v': 'bv/' + mp4_name,
            't': ('bv/' + jpg_name) if got_jpg else '',
            'd': round(dur, 1),
        }
        ok += 1
        if ok % 10 == 0:
            json.dump(mapping, open(args.map, 'w', encoding='utf-8'), ensure_ascii=False)

    json.dump(mapping, open(args.map, 'w', encoding='utf-8'), ensure_ascii=False)
    print('\n解出视频 %d 条（其中 %d 条原样保留），跳过 %d，共 %.1f MB，耗时 %.0f s' % (
        ok, small, bad, bytes_out / 1048576, time.time() - t0))
    print('映射表 %s' % args.map)


if __name__ == '__main__':
    main()
