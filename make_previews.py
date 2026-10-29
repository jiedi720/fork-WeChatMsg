# -*- coding: utf-8 -*-
"""给 export/media 里的原图生成 700px 预览（*_p.jpg），供聊天页内联显示。

微信本机大量图片只有 Thumb 缩略图；能拿到原图的那些，直接内联原图又太大
（单张几十 MB），所以统一压成 700px、quality 82 的预览，点开再看原图。

用法:
    .venv\\Scripts\\python.exe make_previews.py [--media export/media]
"""
import argparse
import os
import sys

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # 有些图是拼接长图，关掉解压炸弹保护

MAX_SIDE = 700
SKIP_DIRS = {'avatar', 'emoji'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--media', default=os.path.join('export', 'media'))
    args = ap.parse_args()
    root = os.path.abspath(args.media)
    if not os.path.isdir(root):
        sys.exit(f'找不到目录: {root}')

    made = skip_small = skip_thumb = fail = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d.lower() not in SKIP_DIRS]
        for fn in fns:
            low = fn.lower()
            if not low.endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')):
                continue
            stem = os.path.splitext(fn)[0]
            if stem.endswith('_t') or stem.endswith('_p'):
                skip_thumb += 1
                continue
            src = os.path.join(dp, fn)
            dst = os.path.join(dp, stem + '_p.jpg')
            if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
                continue
            try:
                im = Image.open(src)
                if im.format == 'JPEG':
                    im.draft('RGB', (MAX_SIDE * 2, MAX_SIDE * 2))
                w, h = im.size
                if max(w, h) <= MAX_SIDE + 60:
                    skip_small += 1
                    continue
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                if im.mode in ('RGBA', 'LA', 'P'):
                    im = im.convert('RGBA')
                    bg = Image.new('RGB', im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert('RGB')
                im.save(dst, 'JPEG', quality=82, optimize=True, progressive=True)
                made += 1
            except Exception as e:
                fail += 1
                print('  跳过', fn, type(e).__name__)
    print(f'生成预览 {made} 个 · 尺寸已够略过 {skip_small} · 缩略图跳过 {skip_thumb} · 失败 {fail}')


if __name__ == '__main__':
    main()
