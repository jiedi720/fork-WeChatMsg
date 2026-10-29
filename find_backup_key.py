# -*- coding: utf-8 -*-
"""从 WeChat.exe 内存里找出「手机备份 Backup.db」的 SQLCipher 密钥。

思路：备份是这台电脑的微信 3.9 做的，运行期必然把 key 放在内存里。
     扫出所有 32/64 位 hex 字符串 → 逐个当 SQLCipher 口令去解 Backup.db 首页 →
     能解出合法页头（page size = 0x1000 + 文件格式 1/2）的那个就是 key。

用法:
    .venv\\Scripts\\python.exe find_backup_key.py --db D:\\Temp\\wxold\\Backup_phone.db
    .venv\\Scripts\\python.exe find_backup_key.py --db ... --try-key ef63ab...
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
HEX = re.compile(rb'(?<![0-9a-fA-F])[0-9a-fA-F]{32,64}(?![0-9a-fA-F])')
PROCS = ('wechat.exe', 'weixin.exe')

_SALT = None
_HEAD = None


def _init_worker(salt, head):
    global _SALT, _HEAD
    _SALT = salt
    _HEAD = head


def _valid(dec):
    # SQLite 页头 offset16 = page size（4096 → 0x10 0x00），offset18/19 = 文件格式版本
    return dec[0:2] == b'\x10\x00' and dec[2:3] in (b'\x01', b'\x02') and dec[3:4] in (b'\x01', b'\x02')


def check(args):
    """(hexstr, kdf, iters, reserve) -> 命中则返回该组合"""
    hexstr, kdf, iters, reserve = args
    usable = PAGE - reserve
    if usable - 16 <= 0:
        return None
    iv = _HEAD[usable:usable + 16]
    body = _HEAD[16:usable]
    try:
        pw = hexstr.encode()
    except Exception:
        return None
    keys = []
    # 1) 口令派生（SQLCipher 默认路径）
    h = hashlib.pbkdf2_hmac(kdf, pw, _SALT, iters, 32)
    keys.append(h)
    # 2) 原始密钥（PRAGMA key = "x'...'"），仅 64 位 hex 有意义
    if len(hexstr) == 64:
        try:
            keys.append(bytes.fromhex(hexstr))
        except ValueError:
            pass
    for k in keys:
        try:
            dec = AES.new(k, AES.MODE_CBC, iv).decrypt(body)
        except Exception:
            continue
        if _valid(dec):
            return (hexstr, kdf, iters, reserve)
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
            for m in HEX.finditer(data):
                cands.add(m.group().decode().lower())
    print('读取 %.0f MB，候选 %d 个，用时 %.0fs' % (read_total / 1048576, len(cands), time.time() - t0))
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--try-key', default='')
    args = ap.parse_args()

    global _SALT, _HEAD
    with open(args.db, 'rb') as f:
        _HEAD = f.read(PAGE)
    _SALT = _HEAD[:16]
    print('db=%s  salt=%s' % (os.path.basename(args.db), _SALT.hex()))

    combos = [
        ('sha1', 64000, 48), ('sha1', 64000, 80),      # SQLCipher 3 / WCDB
        ('sha512', 256000, 48), ('sha512', 256000, 80),  # SQLCipher 4
        ('sha1', 4000, 48), ('sha1', 1, 48),           # 老版本 / 无迭代
    ]

    if args.try_key:
        r = check((args.try_key, 'sha1', 64000, 48))
        print('单测 %s … %s' % (args.try_key[:16], r))

    pid, pname = None, ''
    for p in psutil.process_iter(['name', 'pid']):
        n = (p.info['name'] or '').lower()
        if n in PROCS:
            pid, pname = p.info['pid'], n
            break
    if not pid:
        sys.exit('微信没在运行（WeChat.exe / Weixin.exe）')
    print('目标进程 %s pid=%d' % (pname, pid))

    cands = scan(pid)
    if not cands:
        sys.exit('内存里没找到 hex 候选')

    found = None
    t0 = time.time()
    # 分阶段：先跑最可能的组合（便宜的先来），命中就停
    for kdf, it, rs in combos:
        jobs = [(c, kdf, it, rs) for c in cands]
        print('  试 %s/%d/reserve=%d … %d 个' % (kdf, it, rs, len(jobs)))
        sys.stdout.flush()
        with Pool(args.workers, initializer=_init_worker, initargs=(_SALT, _HEAD)) as pool:
            for res in pool.imap_unordered(check, jobs, chunksize=8):
                if res:
                    found = res
                    pool.terminate()
                    break
        if found:
            break
    print('校验用时 %.0fs' % (time.time() - t0))
    if found:
        hexstr, kdf, iters, reserve = found
        print('KEY FOUND: %s  (kdf=%s iters=%d reserve=%d)' % (hexstr, kdf, iters, reserve))
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup_key.txt')
        with open(out, 'w', encoding='utf-8') as f:
            f.write('%s\n%s\n%d\n%d\n' % (hexstr, kdf, iters, reserve))
        print('已写入 %s' % out)
    else:
        print('未找到（key 可能已从内存释放，或不是 hex 字符串形式）')


if __name__ == '__main__':
    main()
