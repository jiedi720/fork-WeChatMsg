# -*- coding: utf-8 -*-
"""
扫描 Weixin.exe 内存里的 64 位 hex 候选，并用真实数据库首页并行校验，
找出微信 4.x 的 SQLCipher 密钥。

用法: .venv\\Scripts\\python.exe wx4_findkey.py [--db <某个 .db>] [--workers 16]
"""
import argparse
import hashlib
import os
import re
import sys
import time
from multiprocessing import Pool

import psutil
import pymem
from Cryptodome.Cipher import AES

PAGE = 4096
HEX64 = re.compile(rb'(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])')
DEFAULT_DB = r'E:\OneDrive\xwechat_files\wxid_your_account_465d\db_storage\message\message_0.db'

_SALT = None
_SALT2 = None
_HEAD = None


def _init_worker(salt, head):
    global _SALT, _HEAD
    _SALT = salt
    _HEAD = head


def check_candidate(args):
    hexstr, iters, reserve = args
    try:
        pw = bytes.fromhex(hexstr)
    except ValueError:
        return None
    usable = PAGE - reserve
    key = hashlib.pbkdf2_hmac('sha512', pw, _SALT, iters, 32)
    dec = AES.new(key, AES.MODE_CBC, _HEAD[usable:usable + 16]).decrypt(_HEAD[16:usable])
    if dec[0:2] == b'\x10\x00' and dec[2:3] in (b'\x01', b'\x02') and dec[3:4] in (b'\x01', b'\x02'):
        return (hexstr, iters, reserve)
    return None


def scan(pid, chunk=32 * 1024 * 1024):
    pm = pymem.Pymem()
    pm.open_process_from_id(pid)
    cands = set()
    addr = 0
    read_total = 0
    t0 = time.time()
    while True:
        try:
            mbi = pymem.memory.virtual_query(pm.process_handle, addr)
        except Exception:
            break
        size = mbi.RegionSize
        base = mbi.BaseAddress
        if size == 0:
            break
        addr = base + size
        # MEM_COMMIT=0x1000, 排除 PAGE_NOACCESS/PAGE_GUARD
        if mbi.State != 0x1000 or mbi.Protect in (0x00, 0x01) or (mbi.Protect & 0x100):
            continue
        off = 0
        while off < size:
            n = min(chunk, size - off)
            try:
                data = pm.read_bytes(base + off, n)
            except Exception:
                break
            read_total += n
            off += n
            for m in HEX64.finditer(data):
                cands.add(m.group().decode())
    print(f'读取 {read_total/1048576:.0f} MB，候选 {len(cands)} 个，{time.time()-t0:.0f}s')
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--workers', type=int, default=16)
    args = ap.parse_args()

    pid = None
    for p in psutil.process_iter(['name', 'pid']):
        if (p.info['name'] or '').lower() == 'weixin.exe':
            pid = p.info['pid']
            break
    if not pid:
        sys.exit('Weixin.exe 没在运行')

    global _SALT, _HEAD
    with open(args.db, 'rb') as f:
        _HEAD = f.read(PAGE)
    _SALT = _HEAD[:16]
    print(f'db={os.path.basename(args.db)} salt={_SALT.hex()}')

    cands = scan(pid)
    jobs = [(c, it, r) for c in cands for it in (256000, 64000) for r in (80, 48)]
    print(f'待校验组合 {len(jobs)} 个，{args.workers} 进程并行…')
    t0 = time.time()
    found = None
    with Pool(args.workers, initializer=_init_worker, initargs=(_SALT, _HEAD)) as pool:
        for res in pool.imap_unordered(check_candidate, jobs, chunksize=4):
            if res:
                found = res
                pool.terminate()
                break
    print(f'耗时 {time.time()-t0:.0f}s')
    if found:
        hexstr, iters, reserve = found
        print(f'KEY FOUND: {hexstr}  (kdf_iter={iters}, reserve={reserve})')
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wx4_key.txt'), 'w') as f:
            f.write(f'{hexstr}\n{iters}\n{reserve}\n')
    else:
        print('未找到')


if __name__ == '__main__':
    main()
