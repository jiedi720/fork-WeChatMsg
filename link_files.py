# -*- coding: utf-8 -*-
"""把本机微信收到的文件复制到 export/media/file/，并生成「文件名 -> 相对路径」索引。

微信把收到的文件放在 FileStorage/File/YYYY-MM/文件名，
聊天记录里的文件消息只有文件名（xml 的 <title>），用它就能对上。

用法:
    .venv\\Scripts\\python.exe link_files.py
"""
import argparse
import hashlib
import json
import os
import shutil
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wx-root',
                    default=r'E:\OneDrive\Documents\WeChat Files\wxid_your_account')
    ap.add_argument('--out', default=r'D:\Tools\WeChatMsg\export\media')
    ap.add_argument('--map', default=r'D:\Tools\WeChatMsg\file_map.json')
    args = ap.parse_args()

    src = os.path.join(args.wx_root, 'FileStorage', 'File')
    if not os.path.isdir(src):
        sys.exit('找不到目录: %s' % src)

    dst_root = os.path.join(args.out, 'file')
    os.makedirs(dst_root, exist_ok=True)

    mapping = {}
    if os.path.exists(args.map):
        try:
            mapping.update(json.load(open(args.map, encoding='utf-8')))
        except Exception:
            pass

    n_new = n_skip = 0
    total = 0
    for root, _, files in os.walk(src):
        for f in files:
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if not sz:
                continue
            ext = os.path.splitext(f)[1].lower()
            if len(ext) > 8:
                ext = ''
            name = hashlib.md5(f.encode('utf-8')).hexdigest()[:16] + ext
            dst = os.path.join(dst_root, name)
            total += sz
            if not os.path.exists(dst):
                try:
                    shutil.copy2(p, dst)
                    n_new += 1
                except OSError:
                    continue
            else:
                n_skip += 1
            mapping.setdefault(f, 'file/' + name)

    json.dump(mapping, open(args.map, 'w', encoding='utf-8'), ensure_ascii=False)
    print('文件 %d 个（新复制 %d，已有 %d），共 %.1f MB' % (
        len(mapping), n_new, n_skip, total / 1048576))
    print('映射表 %s' % args.map)


if __name__ == '__main__':
    main()
