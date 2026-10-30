# -*- coding: utf-8 -*-
"""把微信本地缓存的视频/封面复制到 export/media/video/，并生成 video_map.json。

匹配方式：视频消息 XML 里带 length（mp4 字节数）和 cdnthumblength（封面字节数），
本地文件名是 md5 无法直接对上，但**文件大小是唯一的**，用它建索引最稳。
"""
import argparse, json, os, shutil, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wx-root', default=r'E:/OneDrive/Documents/WeChat Files\wxid_your_account')
    ap.add_argument('--out', default=os.path.join('export', 'media', 'video'))
    ap.add_argument('--map', default='video_map.json')
    args = ap.parse_args()

    src = os.path.join(args.wx_root, 'FileStorage', 'Video')
    dst = os.path.abspath(args.out)
    if not os.path.isdir(src):
        sys.exit('找不到目录: ' + src)
    os.makedirs(dst, exist_ok=True)

    thumbs, files = {}, {}
    nc = nv = 0
    for month in sorted(os.listdir(src)):
        d = os.path.join(src, month)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            ext = os.path.splitext(f)[1].lower()
            if ext not in ('.jpg', '.mp4', '.png'):
                continue
            size = os.path.getsize(p)
            target = os.path.join(dst, f)
            if not os.path.exists(target):
                shutil.copy2(p, target)
            rel = 'video/' + f
            if ext == '.mp4':
                files.setdefault(str(size), rel)
                nv += 1
            else:
                thumbs.setdefault(str(size), rel)
                nc += 1

    data = {'thumbs': thumbs, 'files': files}
    with open(args.map, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False)
    print('封面 %d 个 / 视频 %d 个 -> %s' % (nc, nv, dst))
    print('索引: %s（按文件大小）' % args.map)

if __name__ == '__main__':
    main()
