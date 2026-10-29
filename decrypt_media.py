# -*- coding: utf-8 -*-
"""
解密微信 PC 3.x 的 .dat 图片（单字节 XOR）到 export/media/

用法:
    .venv\\Scripts\\python.exe decrypt_media.py [--wx-root "E:\\OneDrive\\Documents\\WeChat Files\\wxid_xxx"] [--out export/media]
"""
import argparse
import os
import sys

MAGICS = [
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'GIF8', 'gif'),
    (b'RIFF', 'webp'),
    (b'BM', 'bmp'),
]


def detect(data: bytes):
    """返回 (xor_key, ext) 或 (None, None)"""
    head = data[:16]
    for key in range(256):
        dec = bytes(b ^ key for b in head)
        for magic, ext in MAGICS:
            if dec.startswith(magic):
                return key, ext
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wx-root', default=r'E:\OneDrive\Documents\WeChat Files\wxid_your_account')
    ap.add_argument('--out', default=os.path.join('export', 'media'))
    args = ap.parse_args()

    attach = os.path.join(args.wx_root, 'FileStorage', 'MsgAttach')
    out_root = os.path.abspath(args.out)
    if not os.path.isdir(attach):
        sys.exit(f'找不到目录: {attach}')

    ok = skip = 0
    ext_stat = {}
    for root, _, files in os.walk(attach):
        for f in files:
            if not f.lower().endswith('.dat'):
                continue
            src = os.path.join(root, f)
            with open(src, 'rb') as fp:
                data = fp.read()
            if not data:
                skip += 1
                continue
            key, ext = detect(data)
            if key is None:
                skip += 1
                continue
            rel = os.path.relpath(src, attach)
            rel = os.path.splitext(rel)[0] + '.' + ext
            dst = os.path.join(out_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as fp:
                fp.write(bytes(b ^ key for b in data))
            ok += 1
            ext_stat[ext] = ext_stat.get(ext, 0) + 1

    print(f'解密成功 {ok} 个，跳过 {skip} 个')
    print('格式分布:', ext_stat)
    print('输出目录:', out_root)


if __name__ == '__main__':
    main()
