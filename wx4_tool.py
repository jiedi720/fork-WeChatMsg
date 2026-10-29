# -*- coding: utf-8 -*-
"""
微信 4.x (Weixin.exe) 数据库解密

步骤:
  1) 启动并登录微信 4.x
  2) .venv\\Scripts\\python.exe wx4_tool.py key
  3) .venv\\Scripts\\python.exe wx4_tool.py decrypt --key <64位hex>
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time

import pymem
import psutil
from Cryptodome.Cipher import AES

PAGE = 4096
RESERVE = 80          # IV 16 + HMAC-SHA512 64
USABLE = PAGE - RESERVE
ITER = 256000
HEX64 = re.compile(rb'(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])')

DEFAULT_SRC = r'E:\OneDrive\xwechat_files\wxid_your_account_465d\db_storage'
DEFAULT_OUT = os.path.join('wx4_decrypted')


# ---------- SQLCipher4 ----------
def derive(password: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac('sha512', password, salt, ITER, 32)


def decrypt_db(src_path: str, out_path: str, password: bytes) -> bool:
    with open(src_path, 'rb') as f:
        data = f.read()
    if data[:16] == b'SQLite format 3\x00':
        with open(out_path, 'wb') as f:
            f.write(data)
        return True
    if len(data) < PAGE:
        return False
    salt = data[:16]
    key = derive(password, salt)

    out = bytearray()
    npages = len(data) // PAGE
    for i in range(npages):
        page = data[i * PAGE:(i + 1) * PAGE]
        if i == 0:
            ct = page[16:USABLE]
            iv = page[USABLE:USABLE + 16]
            dec = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
            out += b'SQLite format 3\x00' + dec
            out += b'\x00' * RESERVE
        else:
            ct = page[:USABLE]
            iv = page[USABLE:USABLE + 16]
            dec = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
            out += dec + b'\x00' * RESERVE

    with open(out_path, 'wb') as f:
        f.write(bytes(out))

    # 校验
    try:
        conn = sqlite3.connect(out_path)
        conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
        conn.close()
        return True
    except Exception:
        try:
            os.remove(out_path)
        except Exception:
            pass
        return False


def verify_key(password: bytes, db_path: str) -> bool:
    """只解第一页做校验"""
    try:
        with open(db_path, 'rb') as f:
            head = f.read(PAGE)
    except OSError:
        return False
    if len(head) < PAGE:
        return False
    salt = head[:16]
    key = derive(password, salt)
    dec = AES.new(key, AES.MODE_CBC, head[USABLE:USABLE + 16]).decrypt(head[16:USABLE])
    # 解出来的第 1 页从 offset 16 开始：页大小(0x1000) + 写入版本 + 读取版本
    return dec[0:2] == b'\x10\x00' and dec[2:3] in (b'\x01', b'\x02') and dec[3:4] in (b'\x01', b'\x02')


# ---------- 内存找 key ----------
def find_weixin_pid():
    for p in psutil.process_iter(['name', 'pid']):
        if (p.info['name'] or '').lower() == 'weixin.exe':
            return p.info['pid']
    return None


def scan_hex_keys(pid: int, limit_mb: int = 4096):
    pm = pymem.Pymem()
    pm.open_process_from_id(pid)
    cands = set()
    scanned = 0
    t0 = time.time()
    addr = 0
    while True:
        try:
            mbi = pymem.memory.virtual_query(pm.process_handle, addr)
        except Exception:
            break
        base = mbi.BaseAddress
        size = mbi.RegionSize
        if size == 0:
            break
        addr = base + size
        if mbi.State != 0x1000 or mbi.Protect in (0x01, 0x00):  # MEM_COMMIT, PAGE_NOACCESS
            continue
        if size > 128 * 1024 * 1024:
            continue
        try:
            chunk = pm.read_bytes(base, size)
        except Exception:
            continue
        scanned += size
        if scanned > limit_mb * 1024 * 1024:
            break
        for m in HEX64.finditer(chunk):
            cands.add(m.group().decode())
        if len(cands) > 500:
            break
    print(f'扫描 {scanned/1048576:.0f} MB, 候选 {len(cands)} 个, {time.time()-t0:.0f}s')
    return cands


def cmd_key(args):
    pid = find_weixin_pid()
    if not pid:
        sys.exit('微信 4.x 没在运行，请先启动并登录 Weixin.exe')
    print(f'Weixin.exe pid={pid}')
    cands = scan_hex_keys(pid, args.scan_mb)
    probe = os.path.join(args.src, 'message', 'message_0.db')
    if not os.path.exists(probe):
        probe = os.path.join(args.src, 'contact', 'contact.db')
    print(f'用 {probe} 校验密钥…')
    for c in cands:
        try:
            pw = bytes.fromhex(c)
        except ValueError:
            continue
        if verify_key(pw, probe):
            print('KEY FOUND:', c)
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wx4_key.txt'), 'w') as f:
                f.write(c)
            return 0
    print('未找到（可加大 --scan-mb 或换时间点重试）')
    return 1


# ---------- 解密目录 ----------
def cmd_decrypt(args):
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wx4_key.txt')
    key_hex = args.key
    if not key_hex and os.path.exists(key_path):
        key_hex = open(key_path).read().strip()
    if not key_hex:
        sys.exit('缺少 --key')
    pw = bytes.fromhex(key_hex)

    out_root = os.path.abspath(args.out)
    ok = fail = 0
    for root, _, files in os.walk(args.src):
        for f in files:
            if not f.endswith('.db') or f.endswith('.db-wal') or f.endswith('.db-shm'):
                continue
            src = os.path.join(root, f)
            rel = os.path.relpath(src, args.src)
            dst = os.path.join(out_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                ok += 1
                continue
            if decrypt_db(src, dst, pw):
                ok += 1
            else:
                fail += 1
                print('  skip', rel)
    print(f'解密完成: {ok} 成功, {fail} 跳过 -> {out_root}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('key')
    p1.add_argument('--src', default=DEFAULT_SRC)
    p1.add_argument('--scan-mb', type=int, default=4096)
    p1.set_defaults(func=cmd_key)

    p2 = sub.add_parser('decrypt')
    p2.add_argument('--key', default='')
    p2.add_argument('--src', default=DEFAULT_SRC)
    p2.add_argument('--out', default=DEFAULT_OUT)
    p2.set_defaults(func=cmd_decrypt)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
