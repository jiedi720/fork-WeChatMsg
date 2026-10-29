# -*- coding: utf-8 -*-
"""
WeChat 聊天记录导出（基于已解密的 PC 微信 3.x 数据库）

用法:
    .venv\\Scripts\\python.exe export_chats.py [--msg-dir app/DataBase/Msg] [--out export]
"""
import argparse
import csv
import hashlib
import html
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

TYPE_LABEL = {
    3: '图片',
    34: '语音',
    37: '好友验证',
    42: '名片',
    43: '视频',
    47: '表情',
    48: '位置',
    49: '链接/文件',
    50: '语音通话',
    10000: '系统消息',
}

WXID_RE = re.compile(rb'wxid_[a-zA-Z0-9_\-]{6,}')
STR_RE = re.compile(rb'[a-zA-Z0-9_@\-\.]{6,64}')

# ---- 表情：[捂脸] 这类代码 -> 本地 png ----
EMOJI_MAP = {}
_emoji_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emoji_map.json')
if os.path.exists(_emoji_path):
    import json as _json
    with open(_emoji_path, encoding='utf-8') as _f:
        EMOJI_MAP = _json.load(_f)
    EMOJI_MAP.setdefault('Grin', EMOJI_MAP.get('呲牙', ''))
EMOJI_RE = None
if EMOJI_MAP:
    _keys = sorted((k for k in EMOJI_MAP if EMOJI_MAP[k]), key=len, reverse=True)
    EMOJI_RE = re.compile(r'\[(' + '|'.join(map(re.escape, _keys)) + r')\]')


def emoji_html(text: str) -> str:
    """已 html.escape 过的文本里，把 [表情名] 替换成 <img>。"""
    if not EMOJI_RE or '[' not in text:
        return text

    def rep(m):
        cn = m.group(1)
        fn = EMOJI_MAP.get(cn)
        if not fn:
            return m.group(0)
        return f'<img class="emo" src="../media/emoji/{fn}" alt="[{html.escape(cn)}]" title="[{html.escape(cn)}]">'

    return EMOJI_RE.sub(rep, text)



def safe_name(name: str, fallback: str) -> str:
    name = (name or '').strip() or fallback
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name)
    return name[:60]


def hexdump_strings(blob):
    """从 BytesExtra 里粗提取所有可能的 wxid / 用户名"""
    if not blob:
        return []
    out = []
    for m in WXID_RE.finditer(blob):
        out.append(m.group().decode('utf-8', 'ignore'))
    if not out:
        for m in STR_RE.finditer(blob):
            s = m.group().decode('utf-8', 'ignore')
            if s.startswith('gh_') or s.endswith('@chatroom'):
                out.append(s)
    return out


DAT_PATH_RE = re.compile(rb'[\w\\:\.\-]*?MsgAttach[ -~]*?\.dat')


def build_avatar_map(msg_dir, avatar_dir):
    """用户名 -> 头像文件名（位于 export/media/avatar/）"""
    out = {}
    mm = os.path.join(msg_dir, 'MicroMsg.db')
    if not (os.path.isdir(avatar_dir) and os.path.exists(mm)):
        return out
    conn = sqlite3.connect(mm)
    try:
        names = [r[0] for r in conn.execute('SELECT usrName FROM ContactHeadImgUrl')]
    except sqlite3.OperationalError:
        names = []
    conn.close()
    for uname in names:
        if not uname:
            continue
        fname = hashlib.md5(uname.encode('utf-8')).hexdigest() + '.jpg'
        if os.path.exists(os.path.join(avatar_dir, fname)):
            out[uname] = fname
    return out


def read_self_wxid():
    conn = sqlite3.connect(os.path.join('app', 'DataBase', 'Msg.db'))
    try:
        row = conn.execute('SELECT value FROM userinfo WHERE id=42').fetchone()
        return (row[0] if row else '') or ''
    except sqlite3.OperationalError:
        return ''
    finally:
        conn.close()


MEDIA_ROOT = ''
_IMG_SIZE = {}


def _img_size_file(path):
    """用标准库读图片宽高，不依赖 Pillow。支持 jpg / png / gif / webp。"""
    import struct
    with open(path, 'rb') as f:
        head = f.read(32)
        if head[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack('>II', head[16:24])
            return w, h
        if head[:6] in (b'GIF87a', b'GIF89a'):
            w, h = struct.unpack('<HH', head[6:10])
            return w, h
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            return _webp_size(f)
        if head[:2] == b'\xff\xd8':
            f.seek(2)
            return _jpeg_size(f)
    return None


def _jpeg_size(f):
    import struct
    while True:
        b = f.read(1)
        if not b:
            return None
        if b != b'\xff':
            continue
        marker = f.read(1)
        while marker == b'\xff':
            marker = f.read(1)
        if not marker:
            return None
        m = marker[0]
        if m in (0xd8, 0xd9) or 0xd0 <= m <= 0xd7:
            continue
        seg = f.read(2)
        if len(seg) < 2:
            return None
        length = struct.unpack('>H', seg)[0]
        if m in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            data = f.read(5)
            if len(data) < 5:
                return None
            h, w = struct.unpack('>HH', data[1:5])
            return w, h
        f.seek(length - 2, 1)


def _webp_size(f):
    import struct
    f.seek(12)
    chunk = f.read(16)
    if len(chunk) < 16:
        return None
    if chunk[:4] == b'VP8 ':
        w, h = struct.unpack('<HH', chunk[10:14])
        return w & 0x3fff, h & 0x3fff
    if chunk[:4] == b'VP8L':
        bits = struct.unpack('<I', chunk[9:13])[0]
        return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
    if chunk[:4] == b'VP8X':
        w = int.from_bytes(chunk[12:15], 'little') + 1
        h = int.from_bytes(chunk[15:18], 'little') + 1
        return w, h
    return None


def image_size(rel):
    """读图片真实宽高（缓存）。读不到返回 None，页面上就不带宽高属性，行为与以前一致。"""
    if not MEDIA_ROOT or not rel:
        return None
    if rel in _IMG_SIZE:
        return _IMG_SIZE[rel]
    try:
        size = _img_size_file(os.path.join(MEDIA_ROOT, rel))
    except Exception:
        size = None
    _IMG_SIZE[rel] = size
    return size


def build_media_index(media_dir):
    """{相对路径(不含扩展名, 小写): (相对路径带扩展名, 是否缩略图)}"""
    idx = {}
    if not os.path.isdir(media_dir):
        return idx
    for root, _, files in os.walk(media_dir):
        for f in files:
            if not f.lower().endswith(('.jpg', '.png', '.gif', '.webp')):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, media_dir).replace('\\', '/')
            idx[os.path.splitext(rel)[0].lower()] = (rel, '_t.' in f.lower())
    return idx


def media_for_message(bytes_extra, media_idx):
    """从 BytesExtra 里找图片文件，优先原图"""
    if not bytes_extra or not media_idx:
        return None
    cands = []
    for m in DAT_PATH_RE.finditer(bytes_extra):
        p = m.group().decode('utf-8', 'ignore')
        low = p.lower()
        k = low.find('msgattach')
        if k < 0:
            continue
        rel = p[k + len('msgattach'):].lstrip('\\/\\')
        rel = rel.replace('\\', '/')
        rel = os.path.splitext(rel)[0].lower()
        is_thumb = rel.endswith('_t')
        cands.append((is_thumb, rel))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])  # 原图优先
    full = None
    thumb = None
    for is_thumb, rel in cands:
        hit = media_idx.get(rel)
        if not hit:
            continue
        if is_thumb and not thumb:
            thumb = hit[0]
        elif not is_thumb and not full:
            full = hit[0]
    if not full and not thumb:
        return None
    # 内联预览优先级：700px 预览 > 原图本身（原图不大时直接用）> 200px 缩略图
    prev = None
    for is_thumb, rel in cands:
        if is_thumb:
            continue
        hit = media_idx.get(rel + '_p')
        if hit:
            prev = hit[0]
            break
    src = prev or full or thumb
    return {'full': full or thumb, 'thumb': src}


def parse_appmsg(content: str):
    """解析 type=49 的 appmsg xml，返回 (标题, 子类型)"""
    if not content:
        return '', ''
    title = re.search(r'<title>(.*?)</title>', content, re.S)
    atype = re.search(r'<type>(.*?)</type>', content, re.S)
    des = re.search(r'<des>(.*?)</des>', content, re.S)
    t = html.unescape(title.group(1)) if title else ''
    if not t and des:
        t = html.unescape(des.group(1))
    return t.strip(), (atype.group(1) if atype else '')


def uncompress_xml(compress):
    """type=49 的正文常常只存在 CompressContent（前面 2 字节头，后面是 xml）"""
    if not compress:
        return ''
    s = compress.decode('utf-8', 'ignore')
    cand = [i for i in (s.find('<?xml'), s.find('<msg')) if i >= 0]
    if cand:
        s = s[min(cand):]
    return s


APPTYPE_LABEL = {
    '3': '音乐', '4': '链接', '5': '链接', '6': '文件', '8': '表情包',
    '19': '聊天记录', '33': '小程序', '36': '小程序', '50': '通话',
    '51': '视频号', '57': '回复', '87': '群公告', '2000': '转账', '2001': '红包',
}


def parse_appmsg2(s):
    """type=49 的 xml 在本机库里有字节损坏，用容错正则抠出标题/描述/链接/类型"""
    if not s:
        return '', '', '', ''
    s2 = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)

    def grab(tag):
        m = re.search(r'<%s>\s*(?:<!\[CDATA\[)?([^\n]{0,300}?)\s*(?:\]\]>)?\s*</' % tag, s2, re.S)
        if m:
            return m.group(1).strip()
        m = re.search(r'%s>\s*(?:<!\[CDATA\[)?([^\n]{0,120}?)\s*(?:\]\]>)?\s*</' % tag, s2, re.S)
        return m.group(1).strip() if m else ''

    title = grab('title')
    des = grab('des')
    m = re.search(r'https?://[^\s"\'<>\u3000\\]+', s2)
    url = m.group(0) if m else ''
    if url:
        for _ in range(3):
            url = url.replace('&amp;', '&')
        url = re.split(r'[\s$"\'<>\\]', url)[0].rstrip('.,);\'"')
    ty = re.search(r'<type>\s*(\d{1,4})', s2) or re.search(r'[^-a-z]type>\s*(\d{1,4})', s2)
    atype = ty.group(1) if ty else ''

    def tidy(x):
        x = html.unescape(re.sub(r'<[^>]*>|]]>', '', x)).strip()
        return re.sub(r'\s+', ' ', x)

    return tidy(title), tidy(des), url, atype


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(ts)


def load_contacts(mm_path):
    conn = sqlite3.connect(mm_path)
    res = {}
    for uname, alias, nick, remark in conn.execute(
            "SELECT UserName, Alias, NickName, Remark FROM Contact"):
        res[uname] = {
            'alias': alias or '',
            'nick': nick or '',
            'remark': remark or '',
        }
    conn.close()
    return res


def display_name(uname, contacts):
    c = contacts.get(uname)
    if c:
        return c['remark'] or c['nick'] or c['alias'] or uname
    if uname.endswith('@chatroom'):
        return uname
    return uname


def render_message(row, contacts, self_names, media_idx=None, avatar_map=None, self_wxid=''):
    (local_id, mtype, sub_type, is_sender, create_time, talker,
     content, display, bytes_extra, compress) = row

    sender = ''
    sender_uname = talker
    if talker.endswith('@chatroom') and not is_sender:
        cands = [u for u in hexdump_strings(bytes_extra) if u != talker]
        if cands:
            sender_uname = cands[0]
            sender = display_name(sender_uname, contacts)
    if is_sender and self_wxid:
        sender_uname = self_wxid
    avatar = (avatar_map or {}).get(sender_uname, '')

    text = ''
    kind = 'text'
    thumb_only = False
    if mtype == 1:
        text = content or ''
        if text.endswith('\n'):
            text = text[:-1]
    elif mtype == 49:
        xml = content or uncompress_xml(compress)
        title, des, url, atype = parse_appmsg2(xml)
        # MSG.SubType 才是可靠的类型来源（xml 在本机库里有字节损坏）
        if sub_type:
            atype = str(sub_type)
        label = APPTYPE_LABEL.get(atype, '分享')
        if atype in ('5', '4') and (title or url):
            host = ''
            if url:
                mhost = re.match(r'https?://([^/]+)', url)
                host = mhost.group(1) if mhost else ''
            inner = f'<div class="lkt">{html.escape(title or url)}</div>'
            if des and des != title:
                inner += f'<div class="lkd">{html.escape(des[:60])}</div>'
            if host:
                inner += f'<div class="lku">{html.escape(host)}</div>'
            if url:
                text = f'<a class="lk" href="{html.escape(url)}" target="_blank" rel="noreferrer">{inner}</a>'
            else:
                text = inner
            kind = 'link'
        else:
            head = f'[{label}] '
            if atype == '6':
                head = '[文件] '
            elif atype == '19':
                head = '[合并转发的聊天记录] '
            elif atype == '57':
                head = '[回复] '
            text = head + (title or '(内容无法解析)')
            kind = 'card'
    elif mtype == 10000:
        raw = content or ''
        if '<revoked' in raw or 'revokemsg' in raw:
            text = '[撤回了一条消息]'
        elif '红包' in raw:
            text = '[红包]'
        elif '<' in raw:
            text = '[系统提示]'
        else:
            text = raw
        kind = 'system'
    elif mtype == 42:
        text = '[名片] ' + (content or '')[:40]
        kind = 'card'
    elif mtype == 3:
        media = media_for_message(bytes_extra, media_idx)
        if media:
            href = '../media/' + media['full']
            src = '../media/' + media['thumb']
            # 带上真实宽高：图片未加载完也不会撑开页面，保证「打开即在最新一条」不跳
            dim = image_size(media['thumb'])
            dim_s = f' width="{dim[0]}" height="{dim[1]}"' if dim else ''
            text = (f'<a href="{html.escape(href)}" target="_blank" data-zoom>'
                    f'<img class="pic" loading="lazy"{dim_s} src="{html.escape(src)}" alt="图片"></a>')
            kind = 'image'
            thumb_only = '_t.' in src
        else:
            text = '[图片]'
            kind = 'placeholder'
    else:
        text = '[' + TYPE_LABEL.get(mtype, f'消息类型{mtype}') + ']'
        if mtype == 34 and display:
            text += ' ' + display[:20]
        kind = 'placeholder'

    cinfo = contacts.get(sender_uname) or contacts.get(talker) or {}
    return {
        'id': local_id,
        'time': create_time,
        'is_sender': bool(is_sender),
        'sender': '我' if is_sender else (sender or ''),
        'uname': sender_uname or talker,
        'nick': cinfo.get('nick', ''),
        'alias': cinfo.get('alias', ''),
        'remark': cinfo.get('remark', ''),
        'avatar': avatar,
        'text': text,
        'kind': kind,
        'thumb_only': thumb_only,
        'type': mtype,
    }


CSS = """
:root{--bg:#ededed;--panel:#f7f7f7;--me:#95ec69;--other:#fff;--text:#1a1a1a;--sub:#8a8a8a;--line:#e0e0e0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 "Microsoft YaHei","PingFang SC",sans-serif}
a{color:#1a6fc4;text-decoration:none}
.wrap{max-width:860px;margin:0 auto;padding:16px;width:100%}
header{position:sticky;top:0;background:#f2f2f2cc;backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:10px 16px;z-index:5}
/* 会话页：消息按时间从旧到新、自上而下正常排列（DOM 顺序即时间顺序）；
   打开时由页面底部脚本瞬间定位到最末，无动画、无滚动过程 */
body.chat{height:100vh;overflow:hidden;display:flex;flex-direction:column}
body.chat header{position:relative;flex:0 0 auto}
body.chat .scroll{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;scroll-behavior:auto}
header:has(.dbtn){padding-right:128px}
header h1{font-size:16px;margin:0 0 2px}
header .meta{font-size:12px;color:var(--sub)}
.day{text-align:center;margin:18px 0 8px}
.day span{background:#dadada;color:#666;font-size:12px;padding:2px 10px;border-radius:10px}
.msg{display:flex;margin:8px 0;gap:8px;align-items:flex-start}
.msg.me{flex-direction:row-reverse}
.avatar{width:34px;height:34px;border-radius:4px;background:#cfcfcf;flex:0 0 34px;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;overflow:hidden;cursor:pointer}
.avatar img{width:100%;height:100%;object-fit:cover}
.ava-sm{width:22px;height:22px;border-radius:4px;object-fit:cover;vertical-align:-6px;margin-right:6px}
.bubble{max-width:min(560px,72vw);background:var(--other);border-radius:6px;padding:8px 12px;white-space:pre-wrap;word-break:break-word;box-shadow:0 0 1px #0001}
.mid{min-width:0}
.msg.me .bubble{background:var(--me)}
.msg.system{justify-content:center}
.msg.system .bubble{background:#dadada;color:#666;font-size:12px;box-shadow:none;max-width:min(660px,82vw);text-align:center}
.msg .who{font-size:12px;color:var(--sub);margin-bottom:2px}
.msg .tm{font-size:11px;color:#b0b0b0;align-self:flex-end;margin:0 4px}
.msg:target .bubble{box-shadow:0 0 0 2px #f5a623}
.msg:target{background:#fff8e6;border-radius:6px}
.bubble.card{background:#fff;border:1px solid var(--line)}
.bubble.link,.msg.me .bubble.link{padding:0;background:transparent;box-shadow:none;border:none}
.lk{display:block;background:#fff;border:1px solid var(--line);border-radius:6px;padding:10px 12px;color:inherit}
.lk:hover{border-color:#b9cee6;background:#fbfdff}
.lkt{font-size:13px;line-height:1.45;color:#1a1a1a;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.lkd{font-size:12px;color:var(--sub);margin-top:4px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.lku{font-size:11px;color:#9aa0a6;margin-top:7px;border-top:1px solid var(--line);padding-top:5px}
.bubble.ph{color:#666;font-style:normal;background:#fff8e6}
.bubble.img{padding:3px;background:transparent;box-shadow:none;position:relative}
.bubble.img.thumb::after{content:"缩略";position:absolute;right:7px;bottom:7px;background:#00000066;color:#fff;
font-size:10px;line-height:1;padding:3px 5px;border-radius:8px;pointer-events:none}
.bubble.img img.pic{display:block;max-width:300px;max-height:300px;height:auto;width:auto;border-radius:4px;cursor:zoom-in}
.lb{display:none;position:fixed;inset:0;background:#000000cc;z-index:80;align-items:center;justify-content:center;cursor:zoom-out}
.lb.on{display:flex}
.lb img{max-width:92vw;max-height:92vh;border-radius:4px;box-shadow:0 10px 50px #0007}
img.emo{width:20px;height:20px;vertical-align:-4px;margin:-1px 1px 0;image-rendering:auto}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px;text-align:left}
th{background:#f7f7f7;position:sticky;top:0;font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{background:#eee}
th .ar{font-size:10px;margin-left:4px;color:#c8c8c8}
th.sorted{color:#1a6fc4}
th.sorted .ar{color:#1a6fc4}
tr:hover td{background:#fafafa}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:#444}
input[type=search]{width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:6px;margin:10px 0;font-size:13px}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;background:#e8f0fe;color:#1a6fc4}
.tag.g{background:#e6f7e9;color:#1f8b3a}
td .ellip{display:block;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 会话列表里的消息命中区 */
#hits{margin-top:14px}
#hits .hh{font-size:12px;color:var(--sub);margin:0 0 6px 2px}
.hit{background:#fff;border-bottom:1px solid var(--line);padding:9px 12px;font-size:13px;line-height:1.55}
.hit:first-child{border-top-left-radius:8px;border-top-right-radius:8px}
.hit:last-child{border-bottom-left-radius:8px;border-bottom-right-radius:8px;border-bottom:none}
.hit .h{font-size:12px;color:var(--sub);margin-bottom:3px}
.hit .h b{color:#1a6fc4;font-weight:600}
.hit .x{word-break:break-word;color:var(--text)}
mark{background:#ffe58f;padding:0 1px;border-radius:2px}
.empty{color:var(--sub);font-size:13px;padding:12px 2px}
/* 头像信息卡 */
.ucard{display:none;position:fixed;z-index:60;background:#fff;border:1px solid var(--line);border-radius:8px;
box-shadow:0 10px 28px #0002;padding:11px 13px;min-width:190px;max-width:290px;font-size:13px}
.ucard.on{display:block}
.ucard .uc-n{font-weight:600;font-size:14px;line-height:1.4;word-break:break-all}
.ucard .uc-r{font-size:12px;color:var(--sub);margin-top:1px}
.ucard .uc-row{font-size:12px;color:#444;margin-top:5px;word-break:break-all;display:flex;gap:6px}
.ucard .uc-row i{color:var(--sub);font-style:normal;flex:0 0 48px}
/* 按日期查找 */
.dbtn{position:absolute;right:16px;top:11px;font-size:12px;padding:4px 10px;border:1px solid var(--line);
background:#fff;border-radius:14px;cursor:pointer;color:#1a6fc4}
.dbtn:hover{background:#f2f6fc}
.dpanel{display:none;position:absolute;right:16px;top:64px;width:304px;background:#fff;
border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px #0002;z-index:20;overflow:hidden}
.dpanel.on{display:block}
.dp-head{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:11px;color:var(--sub);
padding:8px 6px 5px;border-bottom:1px solid var(--line)}
.dp-body{max-height:62vh;overflow-y:auto;padding:0 6px 8px;overscroll-behavior:contain}
.cmh{font-size:11px;color:var(--sub);padding:9px 4px 1px}
.cgrid{display:grid;grid-template-columns:repeat(7,1fr)}
.cd{display:block;text-align:center;padding:6px 0;font-size:13px;color:#c9c9c9;line-height:1.2}
a.cd{color:#1a1a1a;text-decoration:none;border-radius:50%;cursor:pointer}
a.cd:hover{background:#eef3fa;color:#1a6fc4}
.cd.pad{visibility:hidden}
.cd.today{background:#07c160;color:#fff!important;border-radius:50%}
.day:target span{background:#f5a623;color:#fff}
"""


def write_chat_page(path, talker, msgs, contacts, self_label, avatar=''):
    name = display_name(talker, contacts)
    h_ava = f'<img class="ava-sm" src="../media/avatar/{avatar}" alt="">' if avatar else ''

    # 每个有消息的日期 -> 条数，交给前端画日历
    day_count = {}
    for m in msgs:
        d = datetime.fromtimestamp(m['time']).strftime('%Y-%m-%d')
        day_count[d] = day_count.get(d, 0) + 1
    import json
    day_json = json.dumps(day_count, ensure_ascii=False, separators=(',', ':'))
    first_dt = datetime.fromtimestamp(msgs[0]['time'])
    last_dt = datetime.fromtimestamp(msgs[-1]['time'])

    parts = [f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(name)}</title><style>{CSS}</style></head><body class="chat">
<header><h1>{h_ava}{html.escape(name)}</h1>
<div class="meta">{html.escape(talker)} · 共 {len(msgs)} 条 · {fmt_ts(msgs[0]['time'])} ~ {fmt_ts(msgs[-1]['time'])} · <a href="../index.html">返回会话列表</a></div>
<button class="dbtn" id="dbtn">📅 按日期查找</button>
<div class="dpanel" id="dpanel">
<div class="dp-head"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
<div class="dp-body" id="cal-grid"></div>
</div></header>
<div class="scroll"><div class="wrap">"""]
    blocks = []
    last_day = None
    for m in msgs:
        day = datetime.fromtimestamp(m['time']).strftime('%Y-%m-%d')
        if day != last_day:
            blocks.append(f'<div class="day" id="d{day}"><span>{day}</span></div>')
            last_day = day
        cls = 'msg me' if m['is_sender'] else 'msg'
        if m['kind'] == 'system':
            cls = 'msg system'
        initial = (m['sender'] or name)[:1]
        who = '' if m['is_sender'] or m['kind'] == 'system' else f'<div class="who">{html.escape(m["sender"] or name)}</div>'
        # 头像上挂联系人信息，点击时弹卡
        attrs = []
        for key, val in (('data-n', m.get('nick', '')), ('data-r', m.get('remark', '')),
                         ('data-a', m.get('alias', '')), ('data-u', m.get('uname', '')),
                         ('data-g', m['sender'] if not m['is_sender'] else '')):
            if val:
                attrs.append(f'{key}="{html.escape(val, quote=True)}"')
        attr_s = (' ' + ' '.join(attrs)) if attrs else ''
        if m.get('avatar'):
            ava = f'<div class="avatar"{attr_s}><img src="../media/avatar/{m["avatar"]}" alt=""></div>'
        else:
            ava = f'<div class="avatar"{attr_s}>{html.escape(initial)}</div>'
        bubble_cls = 'bubble' + (' card' if m['kind'] == 'card' else '') + (' link' if m['kind'] == 'link' else '') + (' ph' if m['kind'] == 'placeholder' else '') + (' img' if m['kind'] == 'image' else '') + (' thumb' if m.get('thumb_only') else '')
        body = m['text'] if m['kind'] in ('image', 'link') else emoji_html(html.escape(m['text']))
        blocks.append(f"""<div class="{cls}" id="m{m['id']}">{ava}
<div class="mid"><div>{who}</div><div class="{bubble_cls}">{body}</div></div>
<div class="tm">{datetime.fromtimestamp(m['time']).strftime('%H:%M')}</div></div>""")

    # 正序拼接：最旧的在上、最新的在下，与微信一致
    parts.extend(blocks)

    parts.append("""</div></div>
<script>
/* 打开会话直接停在最新一条：解析完立即定位，不做动画，用户看不到滚动过程。
   带 #m<id> / #d<日期> 锚点（搜索结果、日历跳转）时交给浏览器定位。 */
(function(){
  var sc=document.querySelector('.scroll');
  if(!sc||location.hash) return;
  var pinned=true;
  sc.scrollTop=sc.scrollHeight;
  sc.addEventListener('wheel',function(){pinned=false;},{passive:true,once:true});
  sc.addEventListener('touchstart',function(){pinned=false;},{passive:true,once:true});
  sc.addEventListener('mousedown',function(){pinned=false;},{passive:true,once:true});
  addEventListener('hashchange',function(){pinned=false;});
  addEventListener('load',function(){ if(pinned&&!location.hash) sc.scrollTop=sc.scrollHeight; });
})();
</script>
<div class="ucard" id="ucard"></div>
<div class="lb" id="lb"><img alt=""></div>
<script>
var lb=document.getElementById('lb'), lbimg=lb.querySelector('img');
document.addEventListener('click',function(e){
  var a=e.target.closest('a[data-zoom]');
  if(a){e.preventDefault();lbimg.src=a.getAttribute('href');lb.classList.add('on');return;}
  if(e.target.closest('#lb')){lb.classList.remove('on');lbimg.removeAttribute('src');}
},true);
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'){lb.classList.remove('on');lbimg.removeAttribute('src');}
});
var card=document.getElementById('ucard');
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function hideCard(){card.classList.remove('on');}
document.addEventListener('click',function(e){
  if(!e.target.closest('#dpanel') && !e.target.closest('#dbtn')) dpanel.classList.remove('on');
  if(e.target.closest('#ucard')) return;
  var av=e.target.closest('.avatar');
  if(!av){hideCard();return;}
  var n=av.dataset.n||'', r=av.dataset.r||'', a=av.dataset.a||'', u=av.dataset.u||'', g=av.dataset.g||'';
  var h='<div class="uc-n">'+esc(n||r||u||'未知')+'</div>';
  if(r) h+='<div class="uc-r">备注：'+esc(r)+'</div>';
  var rows=[];
  if(g) rows.push(['群昵称',esc(g)]);
  if(a) rows.push(['微信号',esc(a)]);
  else rows.push(['微信号','<span style="color:#8a8a8a">未设置</span>']);
  if(u) rows.push(['wxid',esc(u)]);
  rows.forEach(function(x){h+='<div class="uc-row"><i>'+x[0]+'</i><span>'+x[1]+'</span></div>';});
  card.innerHTML=h;
  card.classList.add('on');
  var b=av.getBoundingClientRect(), w=card.offsetWidth, hh=card.offsetHeight;
  var left=b.right+8; if(left+w>innerWidth-8) left=Math.max(8,b.left-w-8);
  var top=Math.min(Math.max(8,b.top-4), innerHeight-hh-8);
  card.style.left=left+'px'; card.style.top=top+'px';
});
var dbtn=document.getElementById('dbtn'), dpanel=document.getElementById('dpanel');
var DAYS=__DAYS__, Y1=__Y1__, M1=__M1__, Y2=__Y2__, M2=__M2__;
var grid=document.getElementById('cal-grid');
function pad2(n){return n<10?'0'+n:''+n;}
function todayKey(){var d=new Date();return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate());}
var TODAY=todayKey();
function monthCells(y,m){
  var dow=new Date(y,m-1,1).getDay(), total=new Date(y,m,0).getDate(), h='';
  for(var i=0;i<dow;i++) h+='<span class="cd pad"></span>';
  for(var d=1;d<=total;d++){
    var k=y+'-'+pad2(m)+'-'+pad2(d), c=DAYS[k];
    var cls='cd'+(k===TODAY?' today':'');
    h+= c ? '<a class="'+cls+'" href="#d'+k+'" title="'+c+' 条">'+d+'</a>'
          : '<span class="'+cls+'">'+d+'</span>';
  }
  return h;
}
function buildCal(){
  var h='';
  for(var y=Y1;y<=Y2;y++){
    var ms=(y===Y1?M1:1), me=(y===Y2?M2:12);
    if(y<Y1||y>Y2) continue;
    for(var m=ms;m<=me;m++){
      h+='<div class="cmh">'+y+'年'+m+'月</div><div class="cgrid">'+monthCells(y,m)+'</div>';
    }
  }
  grid.innerHTML=h;
}
buildCal();
dbtn.addEventListener('click',function(e){
  dpanel.classList.toggle('on');
  if(dpanel.classList.contains('on')) grid.scrollTop=grid.scrollHeight;
  e.stopPropagation();
});
dpanel.addEventListener('click',function(e){
  if(e.target.closest('a.cd')) dpanel.classList.remove('on');
});
</script></body></html>""".replace('__DAYS__', day_json).replace('__Y1__', str(first_dt.year)).replace('__M1__', str(first_dt.month)).replace('__Y2__', str(last_dt.year)).replace('__M2__', str(last_dt.month)))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--msg-dir', default=os.path.join('app', 'DataBase', 'Msg'))
    ap.add_argument('--out', default='export')
    args = ap.parse_args()

    msg_dir = os.path.abspath(args.msg_dir)
    out_dir = os.path.abspath(args.out)
    msg_db = os.path.join(msg_dir, 'MSG.db')
    mm_db = os.path.join(msg_dir, 'MicroMsg.db')
    if not os.path.exists(msg_db):
        sys.exit(f'找不到 {msg_db}，请先解密数据库')

    contacts = load_contacts(mm_db) if os.path.exists(mm_db) else {}
    global MEDIA_ROOT
    MEDIA_ROOT = os.path.join(out_dir, 'media')
    media_idx = build_media_index(MEDIA_ROOT)
    avatar_map = build_avatar_map(msg_dir, os.path.join(out_dir, 'media', 'avatar'))
    self_wxid = read_self_wxid()
    print(f'图片索引: {len(media_idx)} 个 · 头像: {len(avatar_map)} 个 · 本人: {self_wxid}')
    conn = sqlite3.connect(msg_db)
    rows = conn.execute("""
        SELECT localId, Type, SubType, IsSender, CreateTime, StrTalker, StrContent, DisplayContent, BytesExtra, CompressContent
        FROM MSG ORDER BY StrTalker, CreateTime, localId
    """).fetchall()

    by_talker = {}
    for r in rows:
        talker = r[5] or ''
        rec = render_message((r[0], r[1], r[2], r[3], r[4], talker, r[6], r[7], r[8], r[9]),
                             contacts, None, media_idx, avatar_map, self_wxid)
        by_talker.setdefault(talker, []).append(rec)
    conn.close()

    os.makedirs(out_dir, exist_ok=True)
    chat_dir = os.path.join(out_dir, 'chats')
    os.makedirs(chat_dir, exist_ok=True)
    # 清掉上一次导出的会话页，避免会话改名/数量变化后留下旧文件
    for f in sorted(os.listdir(chat_dir)):
        if re.match(r'^\d{3}_.*\.html$', f):
            try:
                os.remove(os.path.join(chat_dir, f))
            except OSError:
                pass

    index_rows = []
    srch_files = []
    srch_recs = []
    for i, (talker, msgs) in enumerate(sorted(by_talker.items(), key=lambda kv: -len(kv[1])), 1):
        name = display_name(talker, contacts)
        fname = f'{i:03d}_{safe_name(name, talker)}.html'
        write_chat_page(os.path.join(chat_dir, fname), talker, msgs, contacts, None,
                        avatar_map.get(talker, ''))
        fi = len(srch_files)
        srch_files.append([name, 'chats/' + fname])
        for m in msgs:
            body = '[图片]' if m['kind'] == 'image' else m['text']
            srch_recs.append([fi, m['time'], m['sender'] or name, body[:120], m['id']])
        lastm = msgs[-1]
        last_plain = '[图片]' if lastm['kind'] == 'image' else re.sub(r'<[^>]+>', '', lastm['text'])
        index_rows.append({
            'name': name,
            'talker': talker,
            'count': len(msgs),
            'start': fmt_ts(msgs[0]['time']),
            'end': fmt_ts(lastm['time']),
            'last': last_plain[:60].replace('\n', ' '),
            'full': last_plain[:400].replace('\n', ' '),
            'thumbs': sum(1 for m in msgs if m.get('thumb_only')),
            'file': 'chats/' + fname,
            'is_group': talker.endswith('@chatroom'),
            'avatar': avatar_map.get(talker, ''),
        })

    # index.html
    tr = []
    for it in index_rows:
        tag = '<span class="tag g">群</span>' if it['is_group'] else '<span class="tag">单聊</span>'
        ava = (f'<img class="ava-sm" src="media/avatar/{it["avatar"]}" alt="">'
               if it.get('avatar') else '')
        tr.append(f"""<tr data-n="{html.escape(it['name'] + ' ' + it['talker'], quote=True)}"><td data-v="{html.escape(it['name'])}">{ava}<a href="{html.escape(it['file'])}">{html.escape(it['name'])}</a> {tag}</td>
<td class="num" data-t="num" data-v="{it['count']}">{it['count']}</td><td class="num" data-t="num" data-v="{it['thumbs']}">{it['thumbs'] or '—'}</td><td data-v="{it['end']}">{it['end'][:10]}</td><td data-v="{html.escape(it['full'])}"><span class="ellip" title="{html.escape(it['full'])}">{html.escape(it['last'])}</span></td></tr>""")

    total = sum(it['count'] for it in index_rows)
    import json
    data_json = json.dumps({'f': srch_files, 'r': srch_recs},
                           ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    idx = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>微信聊天记录导出</title>
<style>{CSS}</style></head><body>
<header><h1>微信聊天记录导出</h1>
<div class="meta">{len(index_rows)} 个会话 · {total} 条消息 · 生成于 {time.strftime('%Y-%m-%d %H:%M')}</div></header>
<div class="wrap"><input type="search" id="q" placeholder="搜会话名 / 微信号 / 聊天内容关键字…" autocomplete="off">
<table id="t"><thead><tr>
<th data-col="0">会话<span class="ar"></span></th>
<th data-col="1" class="sorted desc">消息数<span class="ar">▼</span></th>
<th data-col="2" title="只有缩略图、需要在微信里点开才能缓存的图片数">缺原图<span class="ar"></span></th>
<th data-col="3">最近<span class="ar"></span></th>
<th data-col="4">最后一条<span class="ar"></span></th>
</tr></thead>
<tbody>{''.join(tr)}</tbody></table>
<div id="hits"></div></div>
<script>
const DATA={data_json};
const F=DATA.f, R=DATA.r;
const q=document.getElementById('q');
const tbody=document.querySelector('#t tbody');
const ths=[...document.querySelectorAll('#t th')];
const hits=document.getElementById('hits');
const state={{col:1,asc:false}};
const T=R.map(function(x){{return String(x[3]).toLowerCase();}});

function sortBy(col,asc){{
  const rows=[...tbody.querySelectorAll('tr')];
  rows.sort((a,b)=>{{
    const va=a.children[col].dataset.v||'', vb=b.children[col].dataset.v||'';
    const numeric=a.children[col].dataset.t==='num';
    let r=numeric?(Number(va)-Number(vb)):String(va).localeCompare(String(vb),'zh');
    return asc?r:-r;
  }});
  rows.forEach(r=>tbody.appendChild(r));
  ths.forEach(th=>{{th.classList.remove('sorted','asc','desc');
    const s=th.querySelector('.ar'); if(s) s.textContent='';}});
  const th=ths[col];
  th.classList.add('sorted', asc?'asc':'desc');
  th.querySelector('.ar').textContent=asc?'▲':'▼';
}}

ths.forEach(th=>{{
  th.addEventListener('click',()=>{{
    const col=Number(th.dataset.col);
    const asc=(state.col===col)?!state.asc:true;
    state.col=col; state.asc=asc;
    sortBy(col,asc);
  }});
}});

function esc(s){{return s.replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
function fmt(t){{const d=new Date(t*1000);const p=n=>String(n).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}}
function hl(s,kw){{const i=s.toLowerCase().indexOf(kw);if(i<0)return esc(s);
  return esc(s.slice(0,i))+'<mark>'+esc(s.slice(i,i+kw.length))+'</mark>'+esc(s.slice(i+kw.length));}}

let timer=null;
function run(){{
  const raw=q.value.trim(), kw=raw.toLowerCase();
  // 1) 会话表：无关键字显示全部；有关键字只留会话名 / 微信号匹配的
  const rows=[...tbody.querySelectorAll('tr')];
  let convHit=0;
  rows.forEach(r=>{{
    const ok=!kw||r.dataset.n.toLowerCase().includes(kw);
    r.style.display=ok?'':'none';
    if(ok&&kw) convHit++;
  }});
  document.querySelector('#t').style.display=(kw&&!convHit)?'none':'';
  // 2) 消息内容
  if(!kw){{hits.innerHTML='';return;}}
  const MAX=300; let count=0, shown=0, buf=[];
  for(let i=0;i<R.length;i++){{
    if(T[i].indexOf(kw)<0) continue;
    count++;
    if(shown<MAX){{
      const r=R[i], f=F[r[0]];
      buf.push('<div class="hit"><div class="h"><b>'+esc(f[0])+'</b> · '+esc(r[2])+' · '+fmt(r[1])+
        '</div><a class="x" href="'+f[1]+'#m'+r[4]+'">'+hl(String(r[3]),kw)+'</a></div>');
      shown++;
    }}
  }}
  hits.innerHTML='<div class="hh">聊天内容命中 '+count+' 条'+(count>MAX?'，显示前 '+MAX+' 条':'')+
    '（点条目跳到原文位置）</div>'+(count?buf.join(''):'<div class="empty">没有匹配的聊天内容</div>');
}}
q.addEventListener('input',function(){{clearTimeout(timer);timer=setTimeout(run,160);}});
q.addEventListener('keydown',function(e){{if(e.key==='Enter'){{clearTimeout(timer);run();}}}});
</script></body></html>"""
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(idx)
    old_search = os.path.join(out_dir, 'search.html')
    if os.path.exists(old_search):
        os.remove(old_search)

    # CSV
    with open(os.path.join(out_dir, 'all_messages.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['会话', '微信号', '发送方', '是否我发的', '时间', '类型', '内容'])
        for talker, msgs in by_talker.items():
            cname = display_name(talker, contacts)
            for m in msgs:
                val = '[图片]' if m['kind'] == 'image' else m['text']
                w.writerow([cname, talker, m['sender'] or cname, '是' if m['is_sender'] else '否',
                            fmt_ts(m['time']), m['type'], val])

    with open(os.path.join(out_dir, 'summary.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['会话', '微信号', '类型', '消息数', '起始时间', '结束时间'])
        for it in index_rows:
            w.writerow([it['name'], it['talker'], '群聊' if it['is_group'] else '单聊',
                        it['count'], it['start'], it['end']])

    print(f'完成: {len(index_rows)} 个会话, {total} 条消息')
    print('入口:', os.path.join(out_dir, 'index.html'))


if __name__ == '__main__':
    main()
