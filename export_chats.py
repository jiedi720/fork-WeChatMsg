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

# ---- 表情包（Type 47）：md5 -> export/media/emo/xxx.gif ----
EMOJI_STK = {}
_stk_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'export', 'emoji_cdn_map.json')
if os.path.exists(_stk_path):
    import json as _json2
    with open(_stk_path, encoding='utf-8') as _f2:
        EMOJI_STK = _json2.load(_f2)
EMOJI_MD5_RE = re.compile(r'\bmd5\s*=\s*"([0-9a-fA-F]{32})"')


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


# 非好友类会话：公众号 / 企业微信服务号 / 系统账号
SYS_ACCOUNTS = {'filehelper', 'weixin', 'fmessage', 'medianote', 'qqmail', 'tmessage',
                'newsapp', 'brandsessionholder', 'weibo', 'qqsync', 'floatbottle',
                'voipnotify', 'officialaccounts', 'mphelper', 'notification_messages',
                'exmail_tool', 'weixinreminder', 'qqsafe', 'snsbottle'}


def conv_kind(talker):
    """single / group / service"""
    if talker.endswith('@chatroom'):
        return 'group'
    if (talker.endswith('@openim') or talker.startswith('gh_')
            or talker in SYS_ACCOUNTS):
        return 'service'
    return 'single'


def load_labels(mm_path):
    """用户名 -> [标签名]，以及标签总表（按微信里的顺序）"""
    out, order = {}, []
    if not os.path.exists(mm_path):
        return out, order
    conn = sqlite3.connect(mm_path)
    try:
        id2name = {}
        for lid, name in conn.execute('SELECT LabelId, LabelName FROM ContactLabel'):
            if name:
                id2name[str(lid)] = name
                order.append(name)
        for uname, ids in conn.execute("SELECT UserName, LabelIDList FROM Contact "
                                      "WHERE LabelIDList<>'' AND LabelIDList IS NOT NULL"):
            names = [id2name[x.strip()] for x in (ids or '').split(',') if x.strip() in id2name]
            if names:
                out[uname] = names
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return out, order


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
BK_MEDIA = {}
BF_MEDIA = {}
OG_MAP = {}
VOICE_MAP = {}
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
    srcname = grab('sourcedisplayname')
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

    return tidy(title), tidy(des), url, atype, tidy(srcname)


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
     content, display, bytes_extra, compress) = row[:10]

    sender_hint = row[10] if len(row) > 10 else ''
    media_bk = (row[11] or '') if len(row) > 11 else ''
    if sender_hint == talker:      # 群消息里取不到发送者时退回，不显示群号当人名
        sender_hint = ''

    sender = ''
    sender_uname = talker
    if talker.endswith('@chatroom') and not is_sender:
        if sender_hint:
            sender_uname = sender_hint
            sender = display_name(sender_uname, contacts)
        else:
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
        title, des, url, atype, srcname = parse_appmsg2(xml)
        # MSG.SubType 才是可靠的类型来源（xml 在本机库里有字节损坏）
        if sub_type:
            atype = str(sub_type)
        label = APPTYPE_LABEL.get(atype, '分享')
        if atype in ('5', '4') and (title or url):
            host = ''
            if url:
                mhost = re.match(r'https?://([^/]+)', url)
                host = (mhost.group(1) if mhost else '').replace('www.', '')
            og = OG_MAP.get(url, '') if url else ''
            origin = srcname or host
            t = html.escape(title or url)
            d = html.escape(des[:60]) if (des and des != title) else ''
            if og:
                right = (f'<div class="lk-i"><img loading="lazy" src="../media/{html.escape(og)}"'
                         f' alt=""></div>')
            else:
                right = f'<div class="lk-i"><span class="ph">{html.escape((origin or "链")[:1])}</span></div>'
            body = (f'<div class="lk-t"><div class="lkt">{t}</div>'
                    + (f'<div class="lkd">{d}</div>' if d else '')
                    + (f'<div class="lku">{html.escape(origin)}</div>' if origin else '')
                    + '</div>' + right)
            if url:
                text = (f'<a class="lk" href="{html.escape(url)}" target="_blank" '
                        f'rel="noreferrer">{body}</a>')
            else:
                text = f'<div class="lk">{body}</div>'
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
    elif mtype == 48:
        # 位置：<location x="纬度" y="经度" label="地址" poiname="地点名" />
        _x = re.search(r'\bx="([\d.]+)"', content or '')
        _y = re.search(r'\by="([\d.]+)"', content or '')
        _pn = re.search(r'\bpoiname="([^"]*)"', content or '')
        _lb = re.search(r'\blabel="([^"]*)"', content or '')
        pn = html.unescape(_pn.group(1)) if _pn else ''
        lb = html.unescape(_lb.group(1)) if _lb else ''
        lat = _x.group(1) if _x else ''
        lng = _y.group(1) if _y else ''
        if pn or lb:
            from urllib.parse import quote
            head = html.escape(pn or lb)
            addr = html.escape(lb) if (pn and lb and lb != pn) else ''
            href = ''
            if lat and lng:
                href = ('https://uri.amap.com/marker?position=%s,%s&name=%s'
                        '&src=wxexport&coordinate=gaode&callnative=0' % (lng, lat, quote(pn or lb)))
            inner = (f'<div class="loc-i">📍</div><div class="loc-t">'
                     f'<div class="loc-n">{head}</div>'
                     + (f'<div class="loc-a">{addr}</div>' if addr else '')
                     + '</div><div class="loc-go">›</div>')
            if href:
                text = f'<a class="loc" href="{html.escape(href)}" target="_blank" rel="noreferrer">{inner}</a>'
            else:
                text = f'<div class="loc">{inner}</div>'
            kind = 'location'
        else:
            text = '[位置]'
            kind = 'placeholder'
    elif mtype == 42:
        text = '[名片] ' + (content or '')[:40]
        kind = 'card'
    elif mtype == 3:
        _base = media_bk[:-7] if media_bk.endswith('__thumb') else media_bk
        bfimg = BF_MEDIA.get(_base) if _base else None
        bkimg = BK_MEDIA.get(media_bk) if media_bk else None
        if bfimg:
            # 列表里先放缩略图（刷得快），点开看的是备份里的真原图
            src = bkimg or bfimg
            dim = image_size(src)
            dim_s = f' width="{dim[0]}" height="{dim[1]}"' if dim else ''
            text = (f'<a href="../media/{html.escape(bfimg)}" data-zoom>'
                    f'<img class="pic" loading="lazy"{dim_s} src="../media/{html.escape(src)}" alt="图片"></a>')
            kind = 'image'
            thumb_only = False
        elif bkimg:
            dim = image_size(bkimg)
            dim_s = f' width="{dim[0]}" height="{dim[1]}"' if dim else ''
            text = (f'<a href="../media/{html.escape(bkimg)}" data-zoom>'
                    f'<img class="pic" loading="lazy"{dim_s} src="../media/{html.escape(bkimg)}" alt="图片"></a>')
            kind = 'image'
            thumb_only = media_bk.endswith('__thumb')
        else:
            media = media_for_message(bytes_extra, media_idx)
            if media:
                href = '../media/' + media['full']
                src = '../media/' + media['thumb']
                # 带上真实宽高：图片未加载完也不会撑开页面，保证「打开即在最新一条」不跳
                dim = image_size(media['thumb'])
                dim_s = f' width="{dim[0]}" height="{dim[1]}"' if dim else ''
                text = (f'<a href="{html.escape(href)}" data-zoom>'
                        f'<img class="pic" loading="lazy"{dim_s} src="{html.escape(src)}" alt="图片"></a>')
                kind = 'image'
                thumb_only = '_t.' in src
            else:
                text = '[图片]'
                kind = 'placeholder'
    elif mtype == 34 and media_bk and VOICE_MAP.get(media_bk):
        v = VOICE_MAP[media_bk]
        text = (f'<audio class="au" controls preload="none" src="../media/{html.escape(v["p"])}">'
                f'</audio><span class="dur">{v["d"]}"</span>')
        kind = 'voice'
    elif mtype == 47:
        _m = EMOJI_MD5_RE.search(content or '')
        _fn = EMOJI_STK.get(_m.group(1).lower()) if _m else None
        if _fn:
            text = (f'<a href="../media/{html.escape(_fn)}" data-zoom>'
                    f'<img class="stk" loading="lazy" src="../media/{html.escape(_fn)}" alt="表情"></a>')
            kind = 'sticker'
        else:
            text = '[表情]'
            kind = 'placeholder'
    else:
        text = '[' + TYPE_LABEL.get(mtype, f'消息类型{mtype}') + ']'
        if mtype == 34 and display:
            text += ' ' + display[:20]
        kind = 'placeholder'

    cinfo = contacts.get(sender_uname) or contacts.get(talker) or {}
    if not text:
        text = '[空消息]'
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
:root{--bg:#ededed;--panel:#f7f7f7;--card:#fff;--me:#95ec69;--other:#fff;--text:#1a1a1a;
--text2:#444;--sub:#8a8a8a;--line:#e0e0e0;--chip:#dadada;--chip-t:#666;--hover:#fafafa;
--hi:#fff8e6;--gold:#8a6d2f;--ava:#cfcfcf;--link:#1a6fc4}
html[data-theme=dark]{color-scheme:dark;--bg:#111112;--panel:#191a1b;--card:#212223;--me:#2f7d4f;--other:#262728;
--text:#e7e7e7;--text2:#c2c2c2;--sub:#909090;--line:#303132;--chip:#2c2d2e;--chip-t:#a8a8a8;
--hover:#262728;--hi:#3a331e;--gold:#c9a35c;--ava:#3a3b3c;--link:#5aa9e6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 "Microsoft YaHei","PingFang SC",sans-serif}
a{color:var(--link);text-decoration:none}
.wrap{max-width:860px;margin:0 auto;padding:16px;width:100%}
header{position:sticky;top:0;background:var(--panel);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:10px 16px;z-index:5}
/* 会话页：消息按时间从旧到新、自上而下正常排列（DOM 顺序即时间顺序）；
   打开时由页面底部脚本瞬间定位到最末，无动画、无滚动过程 */
body.chat{height:100vh;overflow:hidden;display:flex;flex-direction:column}
body.chat header{position:relative;flex:0 0 auto}
body.chat .scroll{flex:1 1 auto;min-height:0;overflow:hidden;display:flex;flex-direction:column}
/* 消息列表用 column-reverse 承载：视觉上仍是「最旧在上、最新在下」，
   但滚动原点就是视觉底部 —— 打开瞬间已在最新一条，纯 CSS，不依赖任何脚本 */
body.chat .wrap{flex:1 1 auto;min-height:0;overflow-y:auto;display:flex;flex-direction:column-reverse;overscroll-behavior:contain;scroll-behavior:auto;max-width:none;padding:16px}
/* 滚动条铺满整宽，内容仍收在 860px 内居中；flex:0 0 auto 防止被压缩 */
body.chat .msg,body.chat .day{flex:0 0 auto;width:100%;max-width:860px;margin-left:auto;margin-right:auto}
header:has(.dbtn){padding-right:128px}
header h1{font-size:16px;margin:0 0 2px}
header .meta{font-size:12px;color:var(--sub)}
.day{text-align:center;margin:18px 0 8px}
.day span{background:var(--chip);color:var(--chip-t);font-size:12px;padding:2px 10px;border-radius:10px}
.msg{display:flex;margin:8px 0;gap:8px;align-items:flex-start}
.msg.me{flex-direction:row-reverse}
.avatar{width:34px;height:34px;border-radius:4px;background:var(--ava);flex:0 0 34px;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;overflow:hidden;cursor:pointer}
.avatar img{width:100%;height:100%;object-fit:cover}
.ava-sm{width:22px;height:22px;border-radius:4px;object-fit:cover;vertical-align:-6px;margin-right:6px}
.ava-ph{display:inline-flex;width:22px;height:22px;border-radius:4px;background:var(--ava);color:#fff;
font-size:12px;line-height:1;align-items:center;justify-content:center;vertical-align:-6px;margin-right:6px}
.bubble{max-width:min(560px,72vw);background:var(--other);border-radius:6px;padding:8px 12px;white-space:pre-wrap;word-break:break-word;box-shadow:0 0 1px #0001}
.mid{min-width:0}
.msg.me .bubble{background:var(--me)}
.msg.system{justify-content:center}
.msg.system .bubble{background:var(--chip);color:var(--chip-t);font-size:12px;box-shadow:none;max-width:min(660px,82vw);text-align:center}
.msg .who{font-size:12px;color:var(--sub);margin-bottom:2px}
.msg .tm{font-size:11px;color:var(--sub);align-self:flex-end;margin:0 4px}
.msg:target .bubble{box-shadow:0 0 0 2px #f5a623}
.msg:target{background:var(--hi);border-radius:6px}
.bubble.card{background:var(--card);border:1px solid var(--line)}
.bubble.link,.msg.me .bubble.link{padding:0;background:transparent;box-shadow:none;border:none}
.lk{display:flex;gap:10px;align-items:stretch;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:11px 12px;color:inherit;max-width:min(430px,74vw);text-decoration:none}
.lk:hover{border-color:var(--link);background:var(--hover)}
.lk-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.lkt{font-size:13px;line-height:1.45;color:var(--text);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.lkd{font-size:12px;color:var(--sub);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.lku{font-size:11px;color:var(--sub);margin-top:auto;padding-top:4px}
.lk-i{width:56px;height:56px;flex:none;align-self:center;border-radius:6px;overflow:hidden;
background:var(--chip);display:flex;align-items:center;justify-content:center}
.lk-i img{width:100%;height:100%;object-fit:cover;display:block}
.lk-i .ph{font-size:20px;font-weight:600;color:var(--sub)}
.bubble.ph{color:var(--chip-t);font-style:normal;background:var(--hi)}
.bubble.img{padding:3px;background:transparent;box-shadow:none;position:relative}
.bubble.img.thumb::after{content:"缩略";position:absolute;right:7px;bottom:7px;background:#00000066;color:#fff;
font-size:10px;line-height:1;padding:3px 5px;border-radius:8px;pointer-events:none}
.bubble .au{height:32px;max-width:250px;vertical-align:middle;display:inline-block}
.bubble.stk{padding:0;background:transparent;box-shadow:none}
.bubble .stk{max-width:160px;max-height:160px;display:block;border-radius:4px;cursor:zoom-in}
.bubble.loc{padding:0;background:transparent;box-shadow:none;max-width:min(360px,72vw)}
.loc{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);
border-radius:9px;padding:10px 12px;text-decoration:none;color:var(--text)}
.loc:hover{background:var(--hover)}
.loc-i{font-size:20px;line-height:1;flex:none}
.loc-t{min-width:0;flex:1}
.loc-n{font-size:13px;font-weight:600;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.loc-a{font-size:12px;color:var(--sub);margin-top:3px;line-height:1.35;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.loc-go{color:var(--sub);font-size:18px;flex:none}
.bubble .dur{font-size:11px;color:var(--sub);margin-left:6px;vertical-align:middle}
.bubble.img img.pic{display:block;max-width:300px;max-height:300px;height:auto;width:auto;border-radius:4px;cursor:zoom-in}
.lb{display:none;position:fixed;inset:0;background:#000000cc;z-index:80;align-items:center;justify-content:center;cursor:zoom-out}
.lb.on{display:flex}
.lb img{max-width:92vw;max-height:92vh;border-radius:4px;box-shadow:0 10px 50px #0007}
img.emo{width:20px;height:20px;vertical-align:-4px;margin:-1px 1px 0;image-rendering:auto}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px;text-align:left}
th{background:var(--panel);position:sticky;top:0;font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{background:var(--hover)}
th .ar{font-size:10px;margin-left:4px;color:var(--sub)}
.tbtn{position:absolute;right:16px;top:11px;font-size:14px;padding:3px 10px;border:1px solid var(--line);
background:var(--card);border-radius:14px;cursor:pointer;line-height:1.3}
.tbtn:hover{background:var(--hover)}
body.chat .tbtn{right:150px}
header:has(.tbtn){padding-right:64px}
header:has(.dbtn){padding-right:250px}
.tabs{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:14px 0 10px}
.tb{border:1px solid var(--line);background:var(--card);color:var(--text2);border-radius:16px;padding:5px 14px;
font-size:13px;cursor:pointer;font-family:inherit}
.tb.on{background:#07c160;color:#fff;border-color:#07c160}
.tb b{font-weight:600;margin-left:3px;opacity:.85}
th.sorted{color:var(--link)}
th.sorted .ar{color:var(--link)}
tr:hover td{background:var(--hover)}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--text2)}
input[type=search]{width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:6px;margin:10px 0;
font-size:13px;background:var(--card);color:var(--text)}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;background:#1a6fc422;color:var(--link)}
.tag.g{background:#1f8b3a22;color:#2fa457}
.tag.s{background:#8a6d2f22;color:var(--gold)}
.ltag{display:inline-block;font-size:10px;line-height:1.6;padding:0 5px;margin-left:4px;
border-radius:4px;background:var(--chip);color:var(--chip-t);vertical-align:1px}
.lblsel{margin-left:auto;font-size:12px;padding:4px 8px;border:1px solid var(--line);
border-radius:14px;background:var(--card);color:var(--text);font-family:inherit;cursor:pointer;max-width:190px}
td .ellip{display:block;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.bkc{color:var(--gold);white-space:nowrap;font-size:12px}
td.dt{white-space:nowrap}
@media (min-width:720px){#t{table-layout:fixed}#t th:nth-child(1),#t td:nth-child(1){width:31%}#t th:nth-child(2),#t td:nth-child(2){width:10%}#t th:nth-child(3),#t td:nth-child(3){width:9%}#t th:nth-child(4),#t td:nth-child(4){width:12%}#t th:nth-child(5),#t td:nth-child(5){width:11%}#t th:nth-child(6),#t td:nth-child(6){width:27%}#t td:nth-child(1),#t td:nth-child(4),#t td:nth-child(5){white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#t td .ellip{max-width:none}}
.bkbar{margin:14px 0 10px;padding:10px 14px;background:var(--hi);border:1px solid var(--line);
border-radius:8px;font-size:13px;color:var(--gold);line-height:1.7}
.bkbar b{color:var(--gold)}
.bkbar .warn{display:block;margin-top:2px;color:#d4574a;font-size:12px}
.bkbar .note{display:block;margin-top:2px;color:var(--gold);font-size:12px;opacity:.85}
details#bkd{margin:0 0 12px;font-size:13px}
details#bkd summary{cursor:pointer;padding:8px 0;color:var(--link)}
#bt th{cursor:default}
#bt td:first-child{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 会话列表里的消息命中区 */
#hits{margin-top:14px}
#hits .hh{font-size:12px;color:var(--sub);margin:0 0 6px 2px}
.hit{background:var(--card);border-bottom:1px solid var(--line);padding:9px 12px;font-size:13px;line-height:1.55}
.hit:first-child{border-top-left-radius:8px;border-top-right-radius:8px}
.hit:last-child{border-bottom-left-radius:8px;border-bottom-right-radius:8px;border-bottom:none}
.hit .h{font-size:12px;color:var(--sub);margin-bottom:3px}
.hit .h b{color:var(--link);font-weight:600}
.hit .x{word-break:break-word;color:var(--text)}
mark{background:#ffe58f;padding:0 1px;border-radius:2px}
html[data-theme=dark] mark{background:#6b5b1f;color:var(--text)}
.empty{color:var(--sub);font-size:13px;padding:12px 2px}
/* 头像信息卡 */
.ucard{display:none;position:fixed;z-index:60;background:var(--card);border:1px solid var(--line);border-radius:8px;
box-shadow:0 10px 28px #0002;padding:11px 13px;min-width:190px;max-width:290px;font-size:13px}
.ucard.on{display:block}
.ucard .uc-n{font-weight:600;font-size:14px;line-height:1.4;word-break:break-all}
.ucard .uc-r{font-size:12px;color:var(--sub);margin-top:1px}
.ucard .uc-row{font-size:12px;color:var(--text2);margin-top:5px;word-break:break-all;display:flex;gap:6px}
.ucard .uc-row i{color:var(--sub);font-style:normal;flex:0 0 48px}
/* 按日期查找 */
.dbtn{position:absolute;right:16px;top:11px;font-size:12px;padding:4px 10px;border:1px solid var(--line);
background:var(--card);border-radius:14px;cursor:pointer;color:var(--link)}
.dbtn:hover{background:var(--hover)}
.dpanel{display:none;position:absolute;right:16px;top:64px;width:304px;background:var(--card);
border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px #0002;z-index:20;overflow:hidden}
.dpanel.on{display:block}
.dp-head{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:11px;color:var(--sub);
padding:8px 6px 5px;border-bottom:1px solid var(--line)}
.dp-body{max-height:62vh;overflow-y:auto;padding:0 6px 8px;overscroll-behavior:contain}
.cmh{font-size:11px;color:var(--sub);padding:9px 4px 1px}
.cgrid{display:grid;grid-template-columns:repeat(7,1fr)}
.cd{display:block;text-align:center;padding:6px 0 7px;font-size:13px;color:var(--sub);
opacity:.42;line-height:1.2;position:relative}
a.cd{color:var(--text);opacity:1;font-weight:600;text-decoration:none;border-radius:8px;cursor:pointer}
a.cd::after{content:"";position:absolute;left:50%;transform:translateX(-50%);bottom:2px;
width:3px;height:3px;border-radius:50%;background:var(--link)}
a.cd:hover{background:var(--hover);color:var(--link)}
.cd.pad{visibility:hidden}
.cd.today{background:#07c160;color:#fff!important;border-radius:50%}
.thmseg{position:absolute;right:16px;top:10px;display:flex;background:var(--card);
border:1px solid var(--line);border-radius:14px;overflow:hidden}
.thmseg button{border:0;background:transparent;padding:4px 8px;font-size:12px;line-height:1;
cursor:pointer;color:var(--text);font-family:inherit}
.thmseg button+button{border-left:1px solid var(--line)}
.thmseg button.on{background:var(--link);color:#fff}
body.chat .thmseg{right:150px}
header:has(.thmseg){padding-right:100px}
.cbtn{right:130px}
header:has(.cbtn){padding-right:214px}
.dpcal{top:76px;width:332px}
.ctt{position:fixed;z-index:60;display:none;background:var(--card);border:1px solid var(--line);
border-radius:9px;box-shadow:0 10px 28px #0003;padding:9px 10px;font-size:12px;max-width:290px}
.ctt.on{display:block}
.ctt .s{color:var(--sub)}
.ctabs{display:flex;gap:6px;padding:8px 8px 2px}
.ctb{flex:1;font-size:12px;padding:4px 0;border:1px solid var(--line);background:transparent;
border-radius:6px;cursor:pointer;color:var(--sub);font-family:inherit}
.ctb:hover{background:var(--hover)}
.ctb.on{background:var(--link);border-color:var(--link);color:#fff}
.ca{width:18px;height:18px;border-radius:4px;object-fit:cover;flex:none;background:var(--chip)}
span.ca{display:inline-flex;align-items:center;justify-content:center;background:var(--ava);color:#fff;font-size:10px;line-height:1;flex:none}
.ctr{margin-top:6px;max-height:220px;overflow-y:auto}
.ctr a{display:flex;align-items:center;justify-content:space-between;gap:12px;
padding:3px 5px;border-radius:5px;color:var(--text)}
.ctr a:hover{background:var(--hover);color:var(--link)}
.ctr a i{font-style:normal;color:var(--sub);flex:none}
.ctr a .cn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ctr .empt{padding:6px 5px;color:var(--sub)}
.day:target span{background:#f5a623;color:#fff}
"""


def write_chat_page(path, talker, msgs, contacts, self_label, avatar='', nav=''):
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
<title>{html.escape(name)}</title><script>(function(){{var m='auto';try{{m=localStorage.getItem('wxthm')||'auto'}}catch(e){{}}var d=m==='dark'||(m==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);if(d)document.documentElement.setAttribute('data-theme','dark');}})();</script><style>{CSS}</style></head><body class="chat">
<header><h1>{h_ava}{html.escape(name)}</h1>
<div class="meta">{html.escape(talker)} · 共 {len(msgs)} 条 · {fmt_ts(msgs[0]['time'])} ~ {fmt_ts(msgs[-1]['time'])} · <a href="../index.html">返回会话列表</a>{nav}</div>
<div class="thmseg" id="thm"><button data-m="auto" title="跟随系统">🌗</button><button data-m="light" title="浅色模式">☀️</button><button data-m="dark" title="深色模式">🌙</button></div>
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
        who = '' if (m['is_sender'] or m['kind'] == 'system' or not m['sender']) else f'<div class="who">{html.escape(m["sender"])}</div>'
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
        bubble_cls = 'bubble' + (' card' if m['kind'] == 'card' else '') + (' link' if m['kind'] == 'link' else '') + (' ph' if m['kind'] == 'placeholder' else '') + (' img' if m['kind'] == 'image' else '') + (' stk' if m['kind'] == 'sticker' else '') + (' loc' if m['kind'] == 'location' else '') + (' thumb' if m.get('thumb_only') else '')
        body = m['text'] if m['kind'] in ('image', 'link', 'voice', 'sticker', 'location') else emoji_html(html.escape(m['text']))
        blocks.append(f"""<div class="{cls}" id="m{m['id']}">{ava}
<div class="mid"><div>{who}</div><div class="{bubble_cls}">{body}</div></div>
<div class="tm">{datetime.fromtimestamp(m['time']).strftime('%H:%M')}</div></div>""")

    # 倒序写入 DOM：配合 .wrap 的 column-reverse，视觉上仍是「最旧在上、最新在下」，
    # 而滚动原点天然落在视觉底部 —— 打开即是最新一条，纯 CSS 生效，无需任何脚本
    parts.extend(reversed(blocks))

    parts.append("""</div></div>
<script>
/* 只在带锚点（#m消息 / #d日期）时跳转；没有锚点就停在原生位置——最新一条 */
function jumpHash(){
  if(!location.hash) return;
  var el=null;
  try{ el=document.querySelector(location.hash); }catch(e){ return; }
  if(el) el.scrollIntoView({block:'center'});
}
jumpHash();
addEventListener('load',jumpHash);
addEventListener('hashchange',jumpHash);
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
  else rows.push(['微信号','<span style="color:var(--sub)">未设置</span>']);
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
</script>
<script>(function(){var box=document.getElementById('thm');if(!box)return;
var mq=matchMedia('(prefers-color-scheme: dark)');
var btns=[].slice.call(box.querySelectorAll('button'));
var cur='auto';try{cur=localStorage.getItem('wxthm')||'auto'}catch(e){}
if(['auto','light','dark'].indexOf(cur)<0)cur='auto';
function apply(){var d=cur==='dark'||(cur==='auto'&&mq.matches);
document.documentElement.setAttribute('data-theme',d?'dark':'light');
btns.forEach(function(x){x.classList.toggle('on',x.dataset.m===cur)});
try{localStorage.setItem('wxthm',cur)}catch(e){}}
apply();btns.forEach(function(x){x.addEventListener('click',function(){cur=x.dataset.m;apply();});});
if(mq.addEventListener)mq.addEventListener('change',apply);})();</script></body></html>""".replace('__DAYS__', day_json).replace('__Y1__', str(first_dt.year)).replace('__M1__', str(first_dt.month)).replace('__Y2__', str(last_dt.year)).replace('__M2__', str(last_dt.month)))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--msg-dir', default=os.path.join('app', 'DataBase', 'Msg'))
    ap.add_argument('--extra-msg', default=r'D:\Temp\wxold\BackupMSG.db',
                    help='手机备份解出的消息库（parse_backup_text.py + import_backup.py 产出）')
    ap.add_argument('--out', default='export')
    ap.add_argument('--limit', type=int, default=0, help='只导出消息最多的前 N 个会话（调试用）')
    ap.add_argument('--clean', action='store_true', help='导出前删除旧的会话页（会触发批量删除确认）')
    args = ap.parse_args()

    msg_dir = os.path.abspath(args.msg_dir)
    out_dir = os.path.abspath(args.out)
    msg_db = os.path.join(msg_dir, 'MSG.db')
    mm_db = os.path.join(msg_dir, 'MicroMsg.db')
    if not os.path.exists(msg_db):
        sys.exit(f'找不到 {msg_db}，请先解密数据库')

    contacts = load_contacts(mm_db) if os.path.exists(mm_db) else {}
    LABEL_OF, LABEL_NAMES = load_labels(mm_db)
    print('标签: %d 个 / 带标签的会话 %d 个' % (len(LABEL_NAMES), len(LABEL_OF)))
    global MEDIA_ROOT, BK_MEDIA, BF_MEDIA, OG_MAP, VOICE_MAP
    MEDIA_ROOT = os.path.join(out_dir, 'media')
    _vmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_map.json')
    if os.path.exists(_vmap):
        import json as _jv
        with open(_vmap, encoding='utf-8') as _f:
            VOICE_MAP.update(_jv.load(_f))
        print('备份语音: %d 条' % len(VOICE_MAP))
    _bkmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bk_media_map.json')
    if os.path.exists(_bkmap):
        import json as _jm
        with open(_bkmap, encoding='utf-8') as _f:
            BK_MEDIA.update(_jm.load(_f))
        print('备份图片: %d 张' % len(BK_MEDIA))
    _ogmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'og_map.json')
    if os.path.exists(_ogmap):
        import json as _jo
        with open(_ogmap, encoding='utf-8') as _fo:
            OG_MAP.update(_jo.load(_fo))
        print('分享封面: %d 张' % len(OG_MAP))
    _bfmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bf_media_map.json')
    if os.path.exists(_bfmap):
        import json as _jm2
        with open(_bfmap, encoding='utf-8') as _f2:
            BF_MEDIA.update(_jm2.load(_f2))
        print('备份原图: %d 张' % len(BF_MEDIA))
    media_idx = build_media_index(MEDIA_ROOT)
    avatar_map = build_avatar_map(msg_dir, os.path.join(out_dir, 'media', 'avatar'))
    self_wxid = read_self_wxid()
    print(f'图片索引: {len(media_idx)} 个 · 头像: {len(avatar_map)} 个 · 本人: {self_wxid}')
    conn = sqlite3.connect(msg_db)
    rows = conn.execute("""
        SELECT localId, Type, SubType, IsSender, CreateTime, StrTalker, StrContent, DisplayContent, BytesExtra, CompressContent, MsgSvrID
        FROM MSG ORDER BY StrTalker, CreateTime, localId
    """).fetchall()
    pc_talkers = {r[5] for r in rows}
    bk_talkers = set()

    # 先按会话存「原始行」，不提前 render：93 万条 rec dict 会吃掉 1GB+ 内存
    raw = {}
    from collections import Counter
    seen_svr = Counter()
    seen_key = Counter()
    for r in rows:
        talker = r[5] or ''
        svr = r[10] or 0
        if svr:
            seen_svr[(talker, svr)] += 1
        seen_key[(talker, r[4], r[1], hashlib.md5((r[6] or '').encode('utf-8', 'ignore')).hexdigest()[:8])] += 1
        raw.setdefault(talker, []).append(
            (r[0], r[1], r[2], r[3], r[4], talker, r[6], r[7], r[8], r[9], ''))
    conn.close()
    print('本机: %d 条' % len(rows))

    # ---- 手机备份（BAK_0_TEXT 解出的正文）----
    if args.extra_msg and os.path.exists(args.extra_msg):
        ex = sqlite3.connect(args.extra_msg)
        try:
            add = 0
            dup = 0
            lid = 10 ** 8
            for talker, ty, is_sender, ts, content, svr, hint, mid in ex.execute(
                    'SELECT StrTalker, Type, IsSender, CreateTime, StrContent, MsgSvrID, SenderHint, MediaId '
                    'FROM MSG ORDER BY StrTalker, CreateTime'):
                bk_talkers.add(talker)
                if svr and seen_svr.get((talker, svr)):
                    seen_svr[(talker, svr)] -= 1
                    dup += 1
                    continue
                k = (talker, ts, ty, hashlib.md5((content or '').encode('utf-8', 'ignore')).hexdigest()[:8])
                if seen_key.get(k):
                    seen_key[k] -= 1
                    dup += 1
                    continue
                lid += 1
                raw.setdefault(talker, []).append(
                    (lid, ty, 0, is_sender, ts, talker, content, '', None, None, hint, mid))
                add += 1
            print('备份: 新增 %d 条 · 与本机重复跳过 %d 条' % (add, dup))
        finally:
            ex.close()
        # 备份里的会话昵称补齐（本机通讯录里没有的群/人）
        try:
            ex = sqlite3.connect(args.extra_msg)
            for talker, nick in ex.execute('SELECT talker, nick FROM BkSession'):
                if not nick:
                    continue
                c = contacts.setdefault(talker, {'alias': '', 'nick': '', 'remark': ''})
                if not c.get('nick'):
                    c['nick'] = nick
            ex.close()
        except sqlite3.OperationalError:
            pass

    for talker in raw:
        raw[talker].sort(key=lambda r: (r[4], r[0]))

    os.makedirs(out_dir, exist_ok=True)
    chat_dir = os.path.join(out_dir, 'chats')
    os.makedirs(chat_dir, exist_ok=True)
    # 旧会话页不再逐个删除（批量删除会触发安全确认）；直接覆盖同名文件，
    # 需要彻底清残留时把整个 chats 目录改名即可
    if args.clean:
        for f in sorted(os.listdir(chat_dir)):
            if re.match(r'^\d{3,4}_.*\.html$', f):
                try:
                    os.remove(os.path.join(chat_dir, f))
                except OSError:
                    pass

    index_rows = []
    CAL = {}          # 日期 -> [[会话序号, 条数], ...] 首页日历用
    srch_files = []
    srch_recs = []
    csv_f = open(os.path.join(out_dir, 'all_messages.csv'), 'w', encoding='utf-8-sig', newline='')
    csv_w = csv.writer(csv_f)
    csv_w.writerow(['会话', '微信号', '发送方', '是否我发的', '时间', '类型', '内容'])
    CHUNK = 6000
    order = sorted(raw, key=lambda t: -len(raw[t]))
    kws = []
    # 排除名单：exclude_sessions.txt 每行一个会话名（子串匹配）或完整 talker
    _exf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exclude_sessions.txt')
    if os.path.exists(_exf):
        with open(_exf, encoding='utf-8') as _f:
            kws = [l.strip() for l in _f if l.strip() and not l.startswith('#')]
        if kws:
            _before = len(order)
            order = [t for t in order
                     if not any(k == t or k in display_name(t, contacts) for k in kws)]
            print('排除会话 %d 个（名单 %d 条）' % (_before - len(order), len(kws)))
    if args.limit:
        order = order[:args.limit]
    for i, talker in enumerate(order, 1):
        rws = raw.pop(talker)
        name = display_name(talker, contacts)
        base = f'{i:03d}_{safe_name(name, talker)}'
        chunks = [rws[j:j + CHUNK] for j in range(0, len(rws), CHUNK)] or [[]]
        files = []
        lastm = None
        thumbs = 0
        for pi, rpart in enumerate(chunks, 1):
            part = [render_message(x, contacts, None, media_idx, avatar_map, self_wxid)
                    for x in rpart]
            fn = f'{base}.html' if len(chunks) == 1 else f'{base}_p{pi}.html'
            nav = ''
            if len(chunks) > 1:
                prev = f' · <a href="{base}_p{pi - 1}.html">← 更早</a>' if pi > 1 else ''
                nxt = f' · <a href="{base}_p{pi + 1}.html">更晚 →</a>' if pi < len(chunks) else ''
                nav = f' · 第 {pi}/{len(chunks)} 片{prev}{nxt}'
            write_chat_page(os.path.join(chat_dir, fn), talker, part, contacts, None,
                            avatar_map.get(talker, ''), nav)
            files.append(fn)
            thumbs += sum(1 for m in part if m.get('thumb_only'))
            lastm = part[-1] if part else lastm
            fi = len(srch_files)
            srch_files.append([name if len(chunks) == 1 else f'{name} ({pi}/{len(chunks)})',
                               'chats/' + fn])
            # 搜索索引只取每会话最近 800 条：93 万条全量会让 index.html 涨到 60MB+
            for m in part[-800:]:
                body = {'image': '[图片]', 'voice': '[语音]'}.get(m['kind'], m['text'])
                srch_recs.append([fi, m['time'], m['sender'] or name, body[:120], m['id']])
            for m in part:
                val = {'image': '[图片]', 'voice': '[语音]'}.get(
                    m['kind'], re.sub(r'<[^>]+>', '', m['text']))
                csv_w.writerow([name, talker, m['sender'] or name, '是' if m['is_sender'] else '否',
                                fmt_ts(m['time']), m['type'], val])
        if lastm['kind'] in ('image', 'voice'):
            last_plain = '[图片]' if lastm['kind'] == 'image' else '[语音]'
        else:
            last_plain = re.sub(r'<[^>]+>', '', lastm['text'])
        _ci = len(index_rows)
        _dc = {}
        for _x in rws:
            _d = datetime.fromtimestamp(_x[4]).strftime('%Y-%m-%d')
            _dc[_d] = _dc.get(_d, 0) + 1
        for _d, _c in _dc.items():
            CAL.setdefault(_d, []).append([_ci, _c])
        index_rows.append({
            'name': name,
            'talker': talker,
            'count': len(rws),
            'start': fmt_ts(rws[0][4]),
            'end': fmt_ts(lastm['time']),
            'last': last_plain[:60].replace('\n', ' '),
            'full': last_plain[:400].replace('\n', ' '),
            'thumbs': thumbs,
            'file': 'chats/' + files[-1],
            'is_group': talker.endswith('@chatroom'),
            'kind': conv_kind(talker),
            'labels': LABEL_OF.get(talker, []),
            'avatar': avatar_map.get(talker, ''),
            'pages': len(chunks),
        })

    # index.html
    # 手机备份清单（可选）：由 backup_inventory.py 生成 backup_index.json
    bk_sum, bk_map, bk_only = {}, {}, []
    bk_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup_index.json')
    if os.path.exists(bk_path):
        import json as _json
        with open(bk_path, encoding='utf-8') as _f:
            _bk = _json.load(_f)
        bk_sum = _bk.get('summary') or {}
        for _r in _bk.get('sessions') or []:
            bk_map[_r[0]] = _r
        _have = {it['talker'] for it in index_rows}
        bk_only = [r for r in (_bk.get('sessions') or []) if r[0] not in _have and (r[4] or 0) > 0
                   and not any(k == r[0] or k in (r[1] or '') for k in kws)]
        bk_only.sort(key=lambda r: -(r[4] or 0))

    GMAP = {'single': 0, 'group': 1, 'service': 2}
    tr = []
    for it in index_rows:
        _k = it.get('kind') or ('group' if it['is_group'] else 'single')
        tag = {'group': '<span class="tag g">群</span>',
               'service': '<span class="tag s">服务号</span>'}.get(_k, '<span class="tag">单聊</span>')
        lbls = ''.join(f'<span class="ltag">{html.escape(x)}</span>' for x in it.get('labels') or [])
        if it.get('avatar'):
            ava = f'<img class="ava-sm" src="media/avatar/{it["avatar"]}" alt="">'
        else:
            ava = f'<span class="ava-ph">{html.escape(it["name"][:1])}</span>'
        has_pc = it['talker'] in pc_talkers
        has_bk = it['talker'] in bk_talkers
        src = '本机+备份' if (has_pc and has_bk) else ('仅备份' if has_bk else '本机')
        bk_cell = f'<td class="bkc" data-v="{src}">{src}</td>'
        tr.append(f"""<tr data-n="{html.escape(it['name'] + ' ' + it['talker'], quote=True)}" data-g="{GMAP[_k]}" data-l="{html.escape('|'.join(it.get('labels') or []), quote=True)}"><td data-v="{html.escape(it['name'])}">{ava}<a href="{html.escape(it['file'])}">{html.escape(it['name'])}</a> {tag}{lbls}</td>
<td class="num" data-t="num" data-v="{it['count']}">{it['count']}</td><td class="num" data-t="num" data-v="{it['thumbs']}">{it['thumbs'] or '—'}</td><td class="dt" data-v="{it['end']}">{it['end'][:10]}</td>{bk_cell}<td data-v="{html.escape(it['full'])}"><span class="ellip" title="{html.escape(it['full'])}">{html.escape(it['last'])}</span></td></tr>""")

    _lcount = {}
    for it in index_rows:
        for _x in it.get('labels') or []:
            _lcount[_x] = _lcount.get(_x, 0) + 1
    lbl_sel = '<select id="lbl" class="lblsel" title="按微信标签筛选"><option value="">全部标签</option>'
    for _n in LABEL_NAMES:
        if _n in _lcount:
            lbl_sel += ('<option value="%s">%s (%d)</option>'
                        % (html.escape(_n, quote=True), html.escape(_n), _lcount[_n]))
    lbl_sel += '</select>'

    total = sum(it['count'] for it in index_rows)
    n_group = sum(1 for it in index_rows if it.get('kind') == 'group')
    n_service = sum(1 for it in index_rows if it.get('kind') == 'service')
    n_single = len(index_rows) - n_group - n_service

    bk_bar = bk_det = ''
    if bk_sum:
        n_bk = sum(1 for t in bk_talkers)
        bk_bar = (
            '<div class="bkbar">📱 已合并手机备份正文 · 备份里 <b>%s</b> 个会话 / %s ~ %s / %s 段 / 媒体 %s 条'
            '<span class="note">「来源」列的「备份」表示该会话的聊天记录部分或全部来自手机备份（BAK_0_TEXT）</span></div>'
            % (n_bk, (bk_sum.get('start') or '')[:10], (bk_sum.get('end') or '')[:10],
               bk_sum.get('segments'), bk_sum.get('media', '—')))
        if bk_only:
            brow = []
            for r in bk_only[:300]:
                nick = (' <span class="tag">%s</span>' % html.escape(r[1])) if r[1] else ''
                brow.append('<tr><td>%s%s</td><td>%s ~ %s</td><td class="num">%s</td><td class="num">%s</td></tr>'
                            % (html.escape(r[0]), nick, (r[2] or '')[:10], (r[3] or '')[:10],
                               r[4], r[6] or '—'))
            bk_det = (
                '<details id="bkd"><summary>备份里还有 <b>%d</b> 个当前导出没有的会话'
                '（按文本体积排序，最多显示 300）</summary>'
                '<table id="bt"><thead><tr><th>会话</th><th>覆盖范围</th><th>文本 MB</th>'
                '<th>%s 后段数</th></tr></thead><tbody>%s</tbody></table></details>'
                % (len(bk_only), bk_sum.get('since', ''), ''.join(brow)))
    import json
    if len(srch_recs) > 300000:
        srch_recs.sort(key=lambda r: -r[1])
        del srch_recs[300000:]
    conv_list = [[it['name'], it['file'], 1 if it['is_group'] else 0, it.get('avatar', '')]
                 for it in index_rows]
    data_json = json.dumps({'f': srch_files, 'r': srch_recs,
                            'c': CAL, 'v': conv_list},
                           ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    idx = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>微信聊天记录导出</title><script>(function(){{var m='auto';try{{m=localStorage.getItem('wxthm')||'auto'}}catch(e){{}}var d=m==='dark'||(m==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);if(d)document.documentElement.setAttribute('data-theme','dark');}})();</script>
<style>{CSS}</style></head><body>
<header><h1>微信聊天记录导出</h1>
<div class="meta">{len(index_rows)} 个会话 · {total} 条消息 · 生成于 {time.strftime('%Y-%m-%d %H:%M')}</div>
<div class="thmseg" id="thm"><button data-m="auto" title="跟随系统">🌗</button><button data-m="light" title="浅色模式">☀️</button><button data-m="dark" title="深色模式">🌙</button></div>
<button class="tbtn cbtn" id="cbtn" title="按日期查找：悬停看当天和谁聊了多少条">📅</button>
<div class="dpanel dpcal" id="cpanel">
<div class="ctabs"><button class="ctb on" data-g="all">全部</button><button class="ctb" data-g="0">单聊</button><button class="ctb" data-g="1">群聊</button></div>
<div class="dp-head"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
<div class="dp-body" id="cg"></div>
</div>
<div class="ctt" id="ctt"></div></header>
<div class="wrap"><input type="search" id="q" placeholder="搜会话名 / 微信号 / 聊天内容关键字…" autocomplete="off">
{bk_bar}
<div class="tabs"><button class="tb" data-g="all">全部 <b>{len(index_rows)}</b></button><button class="tb on" data-g="0">单聊 <b>{n_single}</b></button><button class="tb" data-g="1">群聊 <b>{n_group}</b></button><button class="tb" data-g="2" title="公众号 / 企业微信服务号 / 系统账号（文件传输助手等）">服务号 <b>{n_service}</b></button>{lbl_sel}</div>
<table id="t"><thead><tr>
<th data-col="0">会话<span class="ar"></span></th>
<th data-col="1" class="sorted desc">消息数<span class="ar">▼</span></th>
<th data-col="2" title="只有缩略图、需要在微信里点开才能缓存的图片数">缺原图<span class="ar"></span></th>
<th data-col="3">最近<span class="ar"></span></th>
<th data-col="4" title="本机微信数据库 / 手机备份（BAK_0_TEXT）">来源<span class="ar"></span></th>
<th data-col="5">最后一条<span class="ar"></span></th>
</tr></thead>
<tbody>{''.join(tr)}</tbody></table>
{bk_det}
<div id="hits"></div></div>
<script>
const DATA={data_json};
const F=DATA.f, R=DATA.r;
const q=document.getElementById('q');
const tbody=document.querySelector('#t tbody');
const ths=[...document.querySelectorAll('#t th')];
const hits=document.getElementById('hits');
const state={{col:1,asc:false}};
let tabG='0';
const tabs=[...document.querySelectorAll('.tb')];
tabs.forEach(function(b){{b.addEventListener('click',function(){{
  tabs.forEach(function(x){{x.classList.remove('on');}});
  b.classList.add('on'); tabG=b.dataset.g; run();
}});}});
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
const lblSel=document.getElementById('lbl');
function run(){{
  const raw=q.value.trim(), kw=raw.toLowerCase();
  const lw=lblSel?lblSel.value:'';
  // 1) 会话表：无关键字显示全部；有关键字只留会话名 / 微信号匹配的
  const rows=[...tbody.querySelectorAll('tr')];
  let convHit=0;
  rows.forEach(r=>{{
    const ok=(!kw||r.dataset.n.toLowerCase().includes(kw))&&(tabG==='all'||r.dataset.g===tabG)
      &&(!lw||(r.dataset.l||'').split('|').indexOf(lw)>=0);
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
if(lblSel) lblSel.addEventListener('change',run);
run();

/* ---- 按日期查找：全局日历 ---- */
const CAL=DATA.c||{{}}, CONV=DATA.v||[];
var cbtn=document.getElementById('cbtn'), cpanel=document.getElementById('cpanel'),
    cg=document.getElementById('cg'), ctt=document.getElementById('ctt');
function pad2(n){{return n<10?'0'+n:''+n;}}
function cells(y,m){{
  var dow=new Date(y,m-1,1).getDay(), total=new Date(y,m,0).getDate(), h='';
  for(var i=0;i<dow;i++) h+='<span class="cd pad"></span>';
  for(var d=1;d<=total;d++){{
    var k=y+'-'+pad2(m)+'-'+pad2(d);
    h += CAL[k] ? '<a class="cd" data-d="'+k+'">'+d+'</a>' : '<span class="cd">'+d+'</span>';
  }}
  return h;
}}
(function(){{
  var ks=Object.keys(CAL).sort();
  if(!ks.length){{cbtn.style.display='none';return;}}
  var y1=+ks[0].slice(0,4), y2=+ks[ks.length-1].slice(0,4), h='';
  for(var y=y1;y<=y2;y++) for(var m=1;m<=12;m++)
    h+='<div class="cmh">'+y+'年'+m+'月</div><div class="cgrid">'+cells(y,m)+'</div>';
  cg.innerHTML=h;
}})();
var calG='all';
function tipShow(a){{
  var k=a.dataset.d;
  var list=(CAL[k]||[]).filter(function(x){{
    if(calG==='all') return true;
    var c=CONV[x[0]]||[];
    return String(c[2])===calG;
  }}).sort(function(p,q){{return q[1]-p[1];}});
  var tot=0; list.forEach(function(x){{tot+=x[1];}});
  var out='<div><b>'+k+'</b> <span class="s">'+list.length+' 个会话 · '+tot+' 条</span></div><div class="ctr">';
  if(!list.length){{
    out+='<div class="empt">这天没有'+(calG==='0'?'单聊':calG==='1'?'群聊':'')+'记录</div>';
  }}
  list.slice(0,10).forEach(function(x){{
    var c=CONV[x[0]]||['?','#'];
    out+='<a href="'+c[1]+'#d'+k+'">'
       + (c[3] ? '<img class="ca" src="media/avatar/'+c[3]+'" alt="">' : '<span class="ca">'+esc([...String(c[0])][0]||'')+'</span>')
       + '<span class="cn">'+esc(c[0])+'</span><i>'+x[1]+'</i></a>';
  }});
  if(list.length>10) out+='<div class="s" style="padding:4px 5px">还有 '+(list.length-10)+' 个会话…</div>';
  ctt.innerHTML=out+'</div>'; ctt.classList.add('on');
  var r=a.getBoundingClientRect(), w=ctt.offsetWidth, hh=ctt.offsetHeight;
  var left=Math.min(Math.max(8,r.left+r.width/2-w/2), innerWidth-w-8);
  var top=r.bottom+6;
  if(top+hh>innerHeight-8) top=Math.max(8,r.top-hh-6);
  ctt.style.left=left+'px'; ctt.style.top=top+'px';
}}
cg.addEventListener('mouseover',function(e){{
  var a=e.target.closest('a.cd');
  if(a) tipShow(a); else ctt.classList.remove('on');
}});
cg.addEventListener('mouseleave',function(){{ctt.classList.remove('on');}});
cbtn.addEventListener('click',function(e){{
  cpanel.classList.toggle('on');
  if(cpanel.classList.contains('on')) cg.scrollTop=0;
  e.stopPropagation();
}});
[...document.querySelectorAll('.ctb')].forEach(function(b){{
  b.addEventListener('click',function(e){{
    [...document.querySelectorAll('.ctb')].forEach(function(x){{x.classList.remove('on');}});
    b.classList.add('on'); calG=b.dataset.g; ctt.classList.remove('on');
    e.stopPropagation();
  }});
}});
document.addEventListener('click',function(e){{
  if(!e.target.closest('#cpanel') && !e.target.closest('#cbtn')) cpanel.classList.remove('on');
  if(!e.target.closest('#ctt')) ctt.classList.remove('on');
}});
</script>
<script>(function(){{var box=document.getElementById('thm');if(!box)return;
var mq=matchMedia('(prefers-color-scheme: dark)');
var btns=[].slice.call(box.querySelectorAll('button'));
var cur='auto';try{{cur=localStorage.getItem('wxthm')||'auto'}}catch(e){{}}
if(['auto','light','dark'].indexOf(cur)<0)cur='auto';
function apply(){{var d=cur==='dark'||(cur==='auto'&&mq.matches);
document.documentElement.setAttribute('data-theme',d?'dark':'light');
btns.forEach(function(x){{x.classList.toggle('on',x.dataset.m===cur)}});
try{{localStorage.setItem('wxthm',cur)}}catch(e){{}}}}
apply();btns.forEach(function(x){{x.addEventListener('click',function(){{cur=x.dataset.m;apply();}});}});
if(mq.addEventListener)mq.addEventListener('change',apply);}})();</script></body></html>"""
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(idx)
    old_search = os.path.join(out_dir, 'search.html')
    if os.path.exists(old_search):
        os.remove(old_search)

    # CSV（逐会话流式写入，避免把 93 万条渲染结果留在内存）
    csv_f.close()

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
