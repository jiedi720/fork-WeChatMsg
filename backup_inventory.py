# -*- coding: utf-8 -*-
"""从已解密的 Backup.db 导出「备份内容清单」

用法: .venv\\Scripts\\python.exe backup_inventory.py [--db ...] [--since 2025-07-01]
"""
import argparse
import csv
import datetime
import os
import sqlite3

DEF_DB = r'D:\Temp\wxold\Backup_phone.plain.db'
OUT_CSV = r'D:\Temp\wxold\备份清单.csv'


def ts(ms):
    if not ms:
        return ''
    return datetime.datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d %H:%M')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEF_DB)
    ap.add_argument('--since', default='2025-07-01')
    ap.add_argument('--out', default=OUT_CSV)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    c = conn.cursor()

    # Session: talker, StartTime, EndTime, TotalSize, NickName
    sess = {}
    for talker, st, en, size, nick in c.execute(
            'SELECT talker, StartTime, EndTime, TotalSize, NickName FROM Session'):
        sess[talker] = dict(start=st, end=en, size=size, nick=nick)
    id2name = {i: u for i, u in c.execute('SELECT rowid, UsrName FROM Name2ID')}

    # 每个会话的文本段统计
    agg = {}
    for tid, st, en, ln, cnt in c.execute(
            'SELECT talkerId, MIN(StartTime), MAX(EndTime), SUM(Length), COUNT(*) '
            'FROM MsgSegments GROUP BY talkerId'):
        agg[tid] = dict(start=st, end=en, bytes=ln, segs=cnt)

    since_ms = int(datetime.datetime.strptime(args.since, '%Y-%m-%d').timestamp() * 1000)
    recent = {}
    for tid, st, en, ln, cnt in c.execute(
            'SELECT talkerId, MIN(StartTime), MAX(EndTime), SUM(Length), COUNT(*) '
            'FROM MsgSegments WHERE EndTime >= ? GROUP BY talkerId', (since_ms,)):
        recent[tid] = dict(bytes=ln, segs=cnt)

    rows = []
    for tid, name in id2name.items():
        a = agg.get(tid)
        s = sess.get(name, {})
        nick = s.get('nick') or ''
        r = recent.get(tid)
        rows.append(dict(
            会话=name,
            备注=nick,
            最早=ts(a['start']) if a else '',
            最晚=ts(a['end']) if a else '',
            文本段数=a['segs'] if a else 0,
            文本MB=round((a['bytes'] or 0) / 1048576, 2) if a else 0,
            近期段数=r['segs'] if r else 0,
            近期MB=round((r['bytes'] or 0) / 1048576, 2) if r else 0,
        ))
    rows.sort(key=lambda x: -x['文本MB'])

    csv_path = args.out
    try:
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    except PermissionError:
        # 文件被 Excel / 预览占用时换个名字，别让整条流水线断掉
        csv_path = os.path.splitext(args.out)[0] + '_%s.csv' % datetime.datetime.now().strftime('%H%M%S')
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print('原 CSV 被占用，改写到 %s' % csv_path)

    total_gap = sum(r['近期MB'] for r in rows)
    gap_chats = sum(1 for r in rows if r['近期段数'])

    # 给 index.html 用的汇总（export_chats.py 会读这个文件）
    import json
    root = os.path.dirname(os.path.abspath(__file__))
    js = {
        'summary': {
            'sessions': len(rows),
            'start': min((r['最早'] for r in rows if r['最早']), default=''),
            'end': max((r['最晚'] for r in rows if r['最晚']), default=''),
            'textMB': round(sum(r['文本MB'] for r in rows), 1),
            'segments': sum(r['文本段数'] for r in rows),
            'media': c.execute('SELECT COUNT(*) FROM MsgMedia').fetchone()[0],
            'since': args.since,
            'recentChats': gap_chats,
            'recentMB': round(total_gap, 1),
        },
        'sessions': [[r['会话'], r['备注'], r['最早'], r['最晚'], r['文本MB'], r['文本段数'], r['近期段数']]
                     for r in rows],
    }
    js_path = os.path.join(root, 'backup_index.json')
    with open(js_path, 'w', encoding='utf-8') as f:
        json.dump(js, f, ensure_ascii=False, separators=(',', ':'))
    print('汇总写入 %s' % js_path)
    print('会话 %d 个，清单写入 %s' % (len(rows), args.out))
    print('%s 之后仍活跃的会话 %d 个，文本合计 %.1f MB' % (args.since, gap_chats, total_gap))
    print('\n前 15 个最活跃（按文本体积）：')
    for r in rows[:15]:
        print('  %-32s %s ~ %s  %6.2f MB  %d 段' % (r['会话'][:32], r['最早'][:10], r['最晚'][:10], r['文本MB'], r['文本段数']))
    conn.close()


if __name__ == '__main__':
    main()
