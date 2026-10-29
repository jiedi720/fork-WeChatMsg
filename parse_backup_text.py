# -*- coding: utf-8 -*-
"""解析手机备份 BAK_0_TEXT 的消息正文

格式（逆向自 MsgSegments + protobuf）:
    BAK_0_TEXT 由 N 个独立片段组成，每片的 OffSet/Length 记录在 MsgSegments 表。
    片段 = AES-ECB(PKCS7) 加密的 protobuf：
        外层  1: 段内消息条数
              2: 单条消息（重复 N 次）
        内层  1: 消息类型
              3: {1: 发送者}      4: {1: 接收者}
              5: {1: 正文}        7: 创建时间（秒）
              8/11: 媒体信息      16: MsgSvrId

用法:
    .venv\\Scripts\\python.exe parse_backup_text.py [--db ...] [--bak ...] [--out ...]
"""
import argparse
import json
import os
import sqlite3
import sys
import time

try:
    from Cryptodome.Cipher import AES
except ImportError:
    from Crypto.Cipher import AES

TYPE_LABEL = {
    1: '文本', 3: '图片', 34: '语音', 37: '好友验证', 42: '名片', 43: '视频',
    47: '表情', 48: '位置', 49: '链接/文件', 50: '语音通话', 10000: '系统消息',
    10002: '系统消息',
}


# ---------- protobuf 盲解 ----------
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


def walk(b, max_fields=100000):
    """返回 [(field, wire, value)]，遇到坏数据即停"""
    i = 0
    out = []
    n = len(b)
    while i < n and len(out) < max_fields:
        try:
            tag, i = rd_varint(b, i)
        except IndexError:
            break
        f, w = tag >> 3, tag & 7
        if f == 0:
            break
        try:
            if w == 0:
                v, i = rd_varint(b, i)
            elif w == 2:
                ln, i = rd_varint(b, i)
                if i + ln > n:
                    break
                v = b[i:i + ln]
                i += ln
            elif w == 5:
                v = b[i:i + 4]
                i += 4
            elif w == 1:
                v = b[i:i + 8]
                i += 8
            else:
                break
        except IndexError:
            break
        out.append((f, w, v))
    return out


def _first_str(b):
    """内层 {1: 'xxx'} → 'xxx'"""
    for f, w, v in walk(b):
        if f == 1 and w == 2:
            try:
                return v.decode('utf-8')
            except UnicodeDecodeError:
                return v.decode('utf-8', 'replace')
    return ''


def unpad(pt):
    if not pt:
        return pt
    p = pt[-1]
    if 1 <= p <= 16 and len(pt) >= p and pt[-p:] == bytes([p]) * p:
        return pt[:-p]
    return pt


def parse_segment(pt):
    """解出的明文 → [msg dict]"""
    out = []
    for f, w, v in walk(pt):
        if f != 2 or w != 2:
            continue
        fields = {}
        for f2, w2, v2 in walk(v):
            fields.setdefault(f2, []).append((w2, v2))
        def get_str(k):
            for w2, v2 in fields.get(k, []):
                if w2 == 2:
                    return _first_str(v2)
            return ''
        def get_int(k):
            for w2, v2 in fields.get(k, []):
                if w2 == 0:
                    return v2
            return 0
        msg = {
            'ty': get_int(1),
            'f': get_str(3),
            'to': get_str(4),
            'c': get_str(5),
            'ts': get_int(7),
            'svr': get_int(16),
        }
        # 媒体：8 或 11 里有内容时留原始 hex 片段，供后续研究
        for k in (8, 11):
            for w2, v2 in fields.get(k, []):
                if w2 == 2 and v2:
                    msg['m'] = v2.hex()[:400]
                    msg['mk'] = k
                    break
        out.append(msg)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=r'D:\Temp\wxold\Backup_phone.plain.db')
    ap.add_argument('--bak', default=r'D:\Temp\wxold\BAK_0_TEXT')
    ap.add_argument('--out', default=r'D:\Temp\wxold\backup_msgs.jsonl')
    args = ap.parse_args()

    db = sqlite3.connect('file:%s?mode=ro' % args.db.replace('\\', '/'), uri=True)
    rows = db.execute(
        'SELECT talkerId,StartTime,EndTime,OffSet,Length,UsrName,SegmentId,FilePath '
        'FROM MsgSegments ORDER BY OffSet').fetchall()
    print('段数 %d  总长 %.1f MB' % (len(rows), sum(r[4] for r in rows) / 1048576))

    key = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup_key.txt'),
               encoding='utf-8').read().split()[0][:16].encode()
    print('AES-ECB key = %s' % key.decode())

    f = open(args.bak, 'rb')
    fout = open(args.out, 'w', encoding='utf-8', newline='\n')
    t0 = time.time()
    total = 0
    bad = 0
    types = {}
    tmin, tmax = None, None

    for n, (tid, st, et, off, ln, usr, seg, path) in enumerate(rows, 1):
        f.seek(off)
        raw = f.read(ln)
        if len(raw) != ln or ln == 0:
            bad += 1
            continue
        if ln % 16:
            bad += 1
            continue
        pt = unpad(AES.new(key, AES.MODE_ECB).decrypt(raw))
        msgs = parse_segment(pt)
        if not msgs:
            bad += 1
            continue
        for m in msgs:
            m['t'] = usr
            m['seg'] = seg
            fout.write(json.dumps(m, ensure_ascii=False) + '\n')
            total += 1
            types[m['ty']] = types.get(m['ty'], 0) + 1
            ts = m['ts']
            if ts:
                tmin = ts if tmin is None else min(tmin, ts)
                tmax = ts if tmax is None else max(tmax, ts)
        if n % 500 == 0:
            el = time.time() - t0
            sys.stdout.write('\r  %d/%d 段  消息 %d   %.1f MB/s' % (
                n, len(rows), total, sum(r[4] for r in rows[:n]) / 1048576 / max(el, .01)))
            sys.stdout.flush()
    f.close()
    fout.close()

    print('\n完成：%d 条消息  %d 段异常  耗时 %.0f s' % (total, bad, time.time() - t0))
    print('时间范围: %s ~ %s' % (
        time.strftime('%Y-%m-%d %H:%M', time.localtime(tmin)),
        time.strftime('%Y-%m-%d %H:%M', time.localtime(tmax))))
    print('类型分布:')
    for k, v in sorted(types.items(), key=lambda x: -x[1]):
        print('  %-6s %-8s %d' % (k, TYPE_LABEL.get(k, '?'), v))
    print('输出 %s  %.1f MB' % (args.out, os.path.getsize(args.out) / 1048576))


if __name__ == '__main__':
    main()
