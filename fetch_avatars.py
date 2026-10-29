# -*- coding: utf-8 -*-
"""
下载联系人/群头像到 export/media/avatar/<md5(用户名)>.jpg

用法:
    .venv\\Scripts\\python.exe fetch_avatars.py
"""
import hashlib
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
MSG_DIR = os.path.join(ROOT, 'app', 'DataBase', 'Msg')
OUT_DIR = os.path.join(ROOT, 'export', 'media', 'avatar')
UA = {'User-Agent': 'Mozilla/5.0'}
NAME_RE = re.compile(rb'(?:wxid_[a-zA-Z0-9_\-]{6,}|[a-zA-Z][a-zA-Z0-9_\-]{5,19})')


def avatar_file(username: str) -> str:
    return os.path.join(OUT_DIR, hashlib.md5(username.encode('utf-8')).hexdigest() + '.jpg')


def wanted_usernames():
    names = set()
    conn = sqlite3.connect(os.path.join(MSG_DIR, 'MSG.db'))
    for (t,) in conn.execute('SELECT DISTINCT StrTalker FROM MSG'):
        if t:
            names.add(t)
    for (talker, be) in conn.execute(
            "SELECT StrTalker, BytesExtra FROM MSG WHERE IsSender=0 AND StrTalker LIKE '%@chatroom'"):
        for m in NAME_RE.finditer(be or b''):
            s = m.group().decode('utf-8', 'ignore')
            if s != talker and not s.endswith('@chatroom'):
                names.add(s)
    conn.close()
    return names


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    need = wanted_usernames()
    conn = sqlite3.connect(os.path.join(MSG_DIR, 'MicroMsg.db'))
    rows = conn.execute(
        'SELECT usrName, smallHeadImgUrl, bigHeadImgUrl FROM ContactHeadImgUrl').fetchall()
    conn.close()

    targets = []
    for uname, small, big in rows:
        if uname not in need:
            continue
        url = small or big
        if not url:
            continue
        dst = avatar_file(uname)
        if os.path.exists(dst) and os.path.getsize(dst) > 500:
            continue
        targets.append((uname, url, dst))

    print(f'待下载: {len(targets)} / 会话数 {len(need)}')

    def fetch(item):
        uname, url, dst = item
        try:
            r = requests.get(url, timeout=15, headers=UA)
            if r.status_code == 200 and len(r.content) > 300:
                with open(dst, 'wb') as f:
                    f.write(r.content)
                return 1
        except Exception:
            pass
        return 0

    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(fetch, targets):
            ok += res
    print(f'下载成功 {ok} 个 -> {OUT_DIR}')


if __name__ == '__main__':
    sys.exit(main())
