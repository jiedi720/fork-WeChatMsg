# -*- coding: utf-8 -*-
"""解密手机备份的 Backup.db（SQLCipher3，PBKDF2-SHA1/64000，页 4096，reserve 48）

用法:
    .venv\\Scripts\\python.exe decrypt_backup.py --db D:\\Temp\\wxold\\Backup_phone.db
输出: 同名 .plain.db
"""
import argparse
import hashlib
import os
import sqlite3
import sys

from Cryptodome.Cipher import AES

PAGE = 4096
RESERVE = 48
ITERS = 64000
SQLITE_HDR = b'SQLite format 3\x00'


def derive(pw: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac('sha1', pw, salt, ITERS, 32)


def decrypt_db(src, dst, key_hex):
    with open(src, 'rb') as f:
        raw = f.read()
    total = len(raw)
    pages = total // PAGE
    rest = total % PAGE
    salt = raw[:16]
    key = derive(key_hex.encode(), salt)
    out = bytearray()

    # 第 1 页：0..16 是 salt，16..4048 是密文，4048..4064 是 IV
    iv = raw[PAGE - RESERVE: PAGE - RESERVE + 16]
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16: PAGE - RESERVE])
    out += SQLITE_HDR + plain + b'\x00' * RESERVE          # 16 + 4032 + 48 = 4096

    # 其余页：base..base+4048 密文，随后 16 字节 IV
    for n in range(1, pages):
        base = n * PAGE
        iv = raw[base + PAGE - RESERVE: base + PAGE - RESERVE + 16]
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[base: base + PAGE - RESERVE])
        out += plain + b'\x00' * RESERVE                     # 4048 + 48 = 4096

    if rest:
        out += raw[pages * PAGE:]

    with open(dst, 'wb') as f:
        f.write(out)
    print('写出 %s  %.1f MB  %d 页' % (dst, len(out) / 1048576, pages))
    return pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--key', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup_key.txt'))
    args = ap.parse_args()

    key_hex = open(args.key, encoding='utf-8').read().split('\n')[0].strip()
    dst = os.path.splitext(args.db)[0] + '.plain.db'
    decrypt_db(args.db, dst, key_hex)

    conn = sqlite3.connect(dst)
    cur = conn.cursor()
    tabs = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print('\n表 %d 个：%s' % (len(tabs), ', '.join(tabs)))
    for t in tabs:
        try:
            cnt = cur.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            cols = [c[1] for c in cur.execute('PRAGMA table_info("%s")' % t)]
            print('\n[%s] %d 行' % (t, cnt))
            print('  列: %s' % ', '.join(cols))
            for row in cur.execute('SELECT * FROM "%s" LIMIT 3' % t):
                print('  样本:', str(row)[:300])
        except Exception as e:
            print('  %s 读取失败: %s' % (t, e))
    conn.close()


if __name__ == '__main__':
    main()
