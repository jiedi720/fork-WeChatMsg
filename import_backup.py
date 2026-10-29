# -*- coding: utf-8 -*-
"""把 parse_backup_text.py 产出的 JSONL 转成「与本机 MSG 表同构」的 SQLite 库

输出 D:\\Temp\\wxold\\BackupMSG.db：
    MSG(localId, Type, SubType, IsSender, CreateTime, StrTalker, StrContent,
        DisplayContent, BytesExtra, CompressContent, MsgSvrID, SenderHint)
    BkSession(talker, nick, start, end, cnt)

这样 export_chats.py 可以直接把它当成第二个消息库合并导出。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

SELF = ''


def rd_varint(b, i):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def media_id(hx):
    """消息里 field11 的 hex → MediaIdStr（如 xxx_backup / xxx_backup__thumb）"""
    if not hx:
        return ''
    try:
        raw = bytes.fromhex(hx)
        tag, i = rd_varint(raw, 0)
        if tag >> 3 != 1 or (tag & 7) != 2:
            return ''
        ln, i = rd_varint(raw, i)
        return raw[i:i + ln].decode('utf-8', 'ignore')
    except Exception:
        return ''


# 群消息正文前缀：wxid_xxx:  /  自定义微信号:  /  数字@chatroom:
GROUP_PREFIX = re.compile(r'^([A-Za-z0-9_\-@\.]{4,64}):\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--jsonl', default=r'D:\Temp\wxold\backup_msgs.jsonl')
    ap.add_argument('--bdb', default=r'D:\Temp\wxold\Backup_phone.plain.db')
    ap.add_argument('--out', default=r'D:\Temp\wxold\BackupMSG.db')
    ap.add_argument('--self', default='')
    args = ap.parse_args()

    self_wxid = args.self
    if not self_wxid:
        try:
            c = sqlite3.connect(os.path.join('app', 'DataBase', 'Msg.db'))
            row = c.execute('SELECT value FROM userinfo WHERE id=42').fetchone()
            self_wxid = (row[0] if row else '') or ''
            c.close()
        except Exception:
            pass
    print('本人 wxid:', self_wxid)

    if os.path.exists(args.out):
        os.remove(args.out)
    out = sqlite3.connect(args.out)
    out.execute('PRAGMA journal_mode=OFF')
    out.execute('PRAGMA synchronous=OFF')
    out.execute('''CREATE TABLE MSG(localId INTEGER PRIMARY KEY, Type INT, SubType INT,
                   IsSender INT, CreateTime INT, StrTalker TEXT, StrContent TEXT,
                   DisplayContent TEXT, BytesExtra BLOB, CompressContent BLOB,
                   MsgSvrID INT, SenderHint TEXT, MediaId TEXT)''')
    out.execute('''CREATE TABLE BkSession(talker TEXT PRIMARY KEY, nick TEXT,
                   start INT, end INT, cnt INT)''')
    # 用户多次备份到同一目录会累积重复消息（同 MsgSvrID 最多出现过 16 次），
    # 先建唯一索引，后面 INSERT OR REPLACE 自动收敛成一条
    out.execute('CREATE UNIQUE INDEX ux_svr ON MSG(StrTalker, MsgSvrID) WHERE MsgSvrID>0')

    t0 = time.time()
    buf = []
    n = 0
    lid = 0
    talkers = {}
    with open(args.jsonl, encoding='utf-8') as f:
        for line in f:
            m = json.loads(line)
            talker = m['t']
            sender = m.get('f') or ''
            content = m.get('c') or ''
            # 群消息的正文带「发送者:\n」前缀（field 3 存的是群 id），要拆出来当发送者
            if talker.endswith('@chatroom'):
                pm = GROUP_PREFIX.match(content)
                if pm:
                    sender = pm.group(1)
                    content = content[pm.end():]
            is_sender = 1 if (self_wxid and sender == self_wxid) else 0
            lid += 1
            buf.append((lid, m.get('ty', 0), 0, is_sender, m.get('ts', 0), talker,
                        content, '', None, None, m.get('svr', 0), sender,
                        media_id(m.get('m'))))
            st = talkers.setdefault(talker, [m.get('ts', 0), m.get('ts', 0), 0])
            if m.get('ts', 0) < st[0]:
                st[0] = m['ts']
            if m.get('ts', 0) > st[1]:
                st[1] = m['ts']
            st[2] += 1
            if len(buf) >= 20000:
                out.executemany('INSERT OR REPLACE INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', buf)
                buf.clear()
                n += 20000
                sys.stdout.write('\r  %d 条' % n)
                sys.stdout.flush()
    if buf:
        out.executemany('INSERT OR REPLACE INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', buf)
        n += len(buf)
    out.execute('CREATE INDEX ix_talk ON MSG(StrTalker, CreateTime)')
    out.execute('CREATE INDEX ix_svr ON MSG(StrTalker, MsgSvrID)')

    # 会话昵称：从备份库 Session 表取
    try:
        b = sqlite3.connect('file:%s?mode=ro' % args.bdb.replace('\\', '/'), uri=True)
        rows = [(t, nick or '') for t, nick in b.execute('SELECT talker, NickName FROM Session')]
        b.close()
        for t, nick in rows:
            st = talkers.get(t)
            if st:
                out.execute('INSERT OR REPLACE INTO BkSession VALUES(?,?,?,?,?)',
                            (t, nick, st[0], st[1], st[2]))
        print('\n会话昵称 %d 条' % len(rows))
    except Exception as e:
        print('\n昵称导入失败:', e)

    out.commit()
    out.close()
    print('写入 %d 条消息 → %s  %.1f MB  耗时 %.0f s' % (
        n, args.out, os.path.getsize(args.out) / 1048576, time.time() - t0))


if __name__ == '__main__':
    main()
