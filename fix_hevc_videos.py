# -*- coding: utf-8 -*-
"""把 export/media/bv 里 HEVC(H.265) 的视频转成 H.264，让浏览器都能直接播。

Chrome / Chromium 默认不含 HEVC 解码器（Windows 装了 HEVC 扩展的 Edge 才行），
所以备份里那批 hevc 视频在页面上只有时长没有画面。这里就地转码成 h264。

用法:
    .venv\\Scripts\\python.exe fix_hevc_videos.py [--crf 26] [--max-side 720]
"""
import argparse
import json
import os
import shutil
import sys
import time
from fractions import Fraction

import av


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', default=r'D:\Tools\WeChatMsg\bk_video_map.json')
    ap.add_argument('--dir', default=r'D:\Tools\WeChatMsg\export\media\bv')
    ap.add_argument('--crf', type=int, default=26)
    ap.add_argument('--max-side', type=int, default=720)
    args = ap.parse_args()

    mapping = json.load(open(args.map, encoding='utf-8'))
    todo = []
    for k, v in mapping.items():
        p = os.path.join(os.path.dirname(args.dir), v['v'])
        if not os.path.exists(p):
            continue
        try:
            with av.open(p) as c:
                if c.streams.video[0].codec_context.name in ('hevc', 'h265'):
                    todo.append((k, p))
        except Exception:
            pass
    print('HEVC 视频 %d 条待转码' % len(todo))
    if not todo:
        return 0

    ok = bad = 0
    t0 = time.time()
    for i, (k, p) in enumerate(todo, 1):
        tmp = p + '.tmp.mp4'
        try:
            with av.open(p) as r:
                vs = r.streams.video[0]
                W, H = vs.codec_context.width, vs.codec_context.height
                sc = min(1.0, float(args.max_side) / max(W, H))
                W2 = max(2, int(W * sc) // 2 * 2)
                H2 = max(2, int(H * sc) // 2 * 2)
                try:
                    rate = max(1, int(round(float(vs.average_rate or 25))))
                except (TypeError, ValueError):
                    rate = 25
                with av.open(tmp, mode='w', format='mp4') as w:
                    vo = w.add_stream('libx264', rate=rate, width=W2, height=H2, pix_fmt='yuv420p')
                    vo.time_base = Fraction(1, rate)
                    n = 0
                    for fr in r.decode(video=0):
                        nf = fr.reformat(width=W2, height=H2, format='yuv420p')
                        nf.pts = n
                        n += 1
                        for pkt in vo.encode(nf):
                            w.mux(pkt)
                    for pkt in vo.encode(None):
                        w.mux(pkt)
            os.replace(tmp, p)
            ok += 1
        except Exception as e:
            bad += 1
            try:
                os.remove(tmp)
            except OSError:
                pass
            print('  [x] %s %s: %s' % (os.path.basename(p), type(e).__name__, str(e)[:60]))
        if i % 10 == 0:
            print('  %d/%d  转好 %d  失败 %d  %.0fs' % (
                i, len(todo), ok, bad, time.time() - t0))
    print('完成：转码 %d 条，失败 %d 条，耗时 %.0fs' % (ok, bad, time.time() - t0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
