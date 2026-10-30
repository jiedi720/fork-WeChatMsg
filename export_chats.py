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
VID_THUMB = {}
VID_FILE = {}
BK_VIDEO = {}
FILE_MAP = {}
LOCAL_IMG = {}
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


def build_local_img_index(msg_db, media_idx):
    """{(会话, 时间戳): (原图, 列表用图)} —— 本机微信缓存的原图。
    手机备份里的图片往往只有缩略图，用时间戳把它对回本机原图。

    msg_db 既可以是单个库文件，也可以是 Msg 目录：微信 3.9 把历史消息
    拆在 MSG0~MSG3.db 里。实测这些分库的 (会话, 时间戳) 与 MSG.db 完全重叠，
    所以多读不会多出图片，这里保留目录支持只是兜底。"""
    idx = {}
    if not media_idx:
        return idx
    dbs = []
    if msg_db and os.path.isdir(msg_db):
        for f in sorted(os.listdir(msg_db)):
            if re.fullmatch(r'MSG\d[\w\-]*\.db', f, re.I) or f.lower() == 'msg.db':
                dbs.append(os.path.join(msg_db, f))
    elif msg_db and os.path.exists(msg_db):
        dbs = [msg_db]
    for p in dbs:
        try:
            c = sqlite3.connect('file:%s?mode=ro' % p.replace('\\', '/'), uri=True)
            rows = c.execute('SELECT StrTalker, CreateTime, BytesExtra FROM MSG WHERE Type=3').fetchall()
            c.close()
        except sqlite3.Error:
            continue
        for talker, ct, be in rows:
            m = media_for_message(be, media_idx)
            if m:
                idx[(talker, ct)] = (m['full'], m['thumb'])
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
    '51': '视频号', '53': '接龙', '57': '回复', '62': '不支持的内容', '76': '音乐',
    '87': '群公告', '2000': '转账', '2001': '红包',
}

# 文件卡片左侧色块按扩展名取色
EXT_COLOR = {
    'pdf': '#e0484b', 'doc': '#2b579a', 'docx': '#2b579a',
    'xls': '#217346', 'xlsx': '#217346', 'csv': '#217346',
    'ppt': '#d24726', 'pptx': '#d24726',
    'zip': '#b8860b', 'rar': '#b8860b', '7z': '#b8860b',
    'txt': '#6b7280', 'md': '#6b7280', 'json': '#6b7280',
    'jpg': '#0ea5e9', 'jpeg': '#0ea5e9', 'png': '#0ea5e9', 'gif': '#0ea5e9',
    'mp4': '#7c3aed', 'mov': '#7c3aed', 'mp3': '#0f9d58', 'wav': '#0f9d58',
    'py': '#3572a5', 'js': '#b8860b', 'html': '#d24726',
}


# 把这几类微信标签的**单聊**合并成一个「工作」分类
WORK_LABELS = ['博世', '蔚来', '安财', '前同事', '工作业务']
# 求职：猎头 / HR 标签，或备注名以「猎头-」「HR-」开头；offer 也算求职
JOB_LABELS = ['猎头', 'HR', 'offer']
# 其他：租房 / 房东 / 中介 / 快递 / 游戏 / 报销发票 / ET5 标签；商家、外卖、维修等杂项名字
OTHER_LABEL_KEYWORDS = ('租房', '快递', '游戏', '报销发票', 'ET5', '外国人', '在日相识')
OTHER_NAME_KEYWORDS = ('租房', '房东', '中介', '外卖', '酒店', '出租', '维修', '轮毂',
                       '驿站', '美食', '机器人', '充值', '电信', '道路救', '改色',
                       '软件', '泊寓', '工作室')
# 同学：初中 / 大工 / 东工大 / 材日1001 / 熊井村石研 等标签
CLASSMATE_LABELS = ['初中', '大工', '东工大', '材日1001', '熊井村石', '高中']
MANUAL_LABELS = {'刘东来': ['初中'], 'julia-ju': ['猎头'], '叶子': ['猎头'],
                 'Qi Hu 🍓': ['前同事', '蔚来'], 'mj💠7915': ['ET5'], 'sha456sha55': ['猎头']}
# 手工备注（数据库里没有备注的联系人）：wxid -> 显示名
MANUAL_NAMES = {'wxid_your_account': '任雪琦'}
# 分组兜底名单：group_override.txt 每行「组名|关键字」，标准规则没命中的再按它归组
_group_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'group_override.txt')
GROUP_OVERRIDES = []
if os.path.exists(_group_f):
    with open(_group_f, encoding='utf-8') as _f:
        for _l in _f:
            _l = _l.strip()
            if _l and not _l.startswith('#') and '|' in _l:
                _g, _kw = _l.split('|', 1)
                _g = {'工作': 'work', '求职': 'job', '其他': 'other'}.get(_g.strip())
                if _g:
                    GROUP_OVERRIDES.append((_g, _kw.strip()))


def fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ''
    if n < 1024:
        return '%d B' % n
    for u in ('KB', 'MB', 'GB'):
        n /= 1024.0
        if n < 1024 or u == 'GB':
            return '%.1f %s' % (n, u)
    return ''



def parse_rich49(s):
    """把 type=49 的应用消息拆成可渲染的结构。

    微信把内容塞在 xml 里，而且 `<content>` / `<recorditem>` 里还会嵌一层
    xml（引用原文、合并转发的条目），所以全部走非贪婪正则 + 去标签。
    """
    d = {'kind': 'other', 'title': '', 'des': '', 'url': '', 'srcname': '',
         'fileext': '', 'size': 0, 'refname': '', 'reftext': '', 'lines': [], 'count': 0}
    if not s:
        return d
    s2 = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)

    def tidy(x):
        x = re.sub(r'<[^>]*>|]]>', '', html.unescape(x or '')).strip()
        return re.sub(r'[ \t]+', ' ', x)

    def grab(tag, txt=None):
        src = s2 if txt is None else txt
        m = re.search(r'<%s\b[^>]*>\s*(?:<!\[CDATA\[)?(.*?)\s*(?:\]\]>)?\s*</%s>' % (tag, tag), src, re.S)
        return m.group(1).strip() if m else ''

    def clean(x):
        x = tidy(x)
        return '' if x.lower() in ('null', 'none', 'undefined') else x

    d['title'] = clean(grab('title'))
    d['des'] = clean(grab('des'))
    m = re.search(r'https?://[^\s"\'<>\u3000\\]+', s2)
    if m:
        u = m.group(0)
        for _ in range(3):
            u = u.replace('&amp;', '&')
        d['url'] = re.split(r'[\s$"\'<>\\]', u)[0].rstrip('.,);\'"')
    d['srcname'] = tidy(grab('sourcedisplayname'))
    d['fileext'] = tidy(grab('fileext')).lower()
    _tl = grab('totallen')
    if _tl.isdigit():
        d['size'] = int(_tl)

    # 引用回复：<refermsg> 里放被引用的那条
    rm = re.search(r'<refermsg>(.*?)</refermsg>', s2, re.S)
    if rm:
        r = rm.group(1)
        d['kind'] = 'reply'
        d['refname'] = tidy(grab('displayname', r))
        rc = html.unescape(grab('content', r))
        if '<msg' in rc or '<?xml' in rc or '<appmsg' in rc:
            # 引用的是链接/文件/图片这类富媒体，正文是一整段 xml —— 只取它的标题
            _rt = tidy(grab('title', rc))
            if '<img' in rc:
                d['reftext'] = '[图片]'
            elif '<videomsg' in rc:
                d['reftext'] = '[视频]'
            elif _rt:
                d['reftext'] = '[链接] ' + _rt
            else:
                d['reftext'] = '[链接/文件]'
        elif '<img' in rc:
            d['reftext'] = '[图片]'
        elif '<videomsg' in rc or '<video' in rc:
            d['reftext'] = '[视频]'
        else:
            d['reftext'] = tidy(rc)

    # 合并转发：<recorditem> 里是 CDATA 包着的 <recordinfo>
    ri = re.search(r'<recorditem>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</recorditem>', s2, re.S)
    if ri:
        r = ri.group(1)
        d['kind'] = 'record'
        d['title'] = tidy(grab('title', r)) or d['title']
        for ln in re.split(r'\r?\n', html.unescape(grab('desc', r))):
            ln = tidy(ln)
            if ln:
                d['lines'].append(ln)
        try:
            d['count'] = int(re.search(r'count="(\d+)"', r).group(1))
        except (AttributeError, TypeError, ValueError):
            d['count'] = len(d['lines'])

    return d



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
    ty = (re.search(r'<type>\s*(?:<!\[CDATA\[)?\s*(\d{1,4})', s2)
          or re.search(r'[^-a-z]type>\s*(?:<!\[CDATA\[)?\s*(\d{1,4})', s2))
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
    if uname in MANUAL_NAMES:
        return MANUAL_NAMES[uname]
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
            ri = parse_rich49(xml)
            if os.environ.get('WXDBG') == '49':
                print('DBG49 atype=%r kind=%r title=%r' % (atype, ri['kind'], (ri['title'] or '')[:24]),
                      file=sys.stderr)
            if atype == '57' or ri['kind'] == 'reply':
                _n, _c = ri['refname'], ri['reftext']
                _q = ((f'<span class="rp-n">{html.escape(_n)}</span>') if _n else '')
                _q += html.escape(_c[:300]) if _c else '（引用内容已失效）'
                _t = ri['title'] or title or ''
                text = (f'<div class="rp"><div class="rp-q">{_q}</div>'
                        f'<div class="rp-t">{html.escape(_t)}</div></div>')
                kind = 'reply'
            elif atype in ('76', '3'):
                _t = ri['title'] or title or '音乐'
                _a = ri['des'] or des or ''
                _src = ri['srcname'] or ''
                _in = (f'<span class="mc-i">♪</span><span class="mc-t">'
                       f'<span class="mc-a">{html.escape(_t)}</span>'
                       + (f'<span class="mc-b">{html.escape(_a)}</span>' if _a else '')
                       + (f'<span class="mc-c">{html.escape(_src)}</span>' if _src else '')
                       + '</span>')
                if ri['url']:
                    text = (f'<a class="mc" href="{html.escape(ri["url"])}" target="_blank" '
                            f'rel="noreferrer">{_in}</a>')
                else:
                    text = f'<span class="mc">{_in}</span>'
                kind = 'music'
            elif atype == '6':
                _t = ri['title'] or title or '文件'
                _ext = (ri['fileext'] or os.path.splitext(_t)[1].lstrip('.')).lower()[:5]
                _sz = fmt_size(ri['size']) if ri['size'] else (ri['des'] or des or '')
                _bg = EXT_COLOR.get(_ext, '#8a8f99')
                _loc = FILE_MAP.get(_t)
                _in = (f'<span class="fc-i" style="background:{_bg}">'
                       f'{html.escape((_ext or "file").upper())}</span>'
                       f'<span class="fc-t"><span class="fc-n">{html.escape(_t)}</span>'
                       + (f'<span class="fc-s">{html.escape(_sz)}</span>' if _sz else '')
                       + '</span>')
                if _loc:
                    text = (f'<a class="fc" href="../media/{html.escape(_loc)}" '
                            f'title="点击打开本机文件">{_in}</a>')
                else:
                    text = f'<span class="fc">{_in}</span>'
                kind = 'file'
            elif atype == '19':
                _t = ri['title'] or title or '聊天记录'
                _ls = ri['lines'][:8]
                _cnt = ri['count'] or len(ri['lines'])
                _body = '\n'.join(html.escape(x) for x in _ls)
                _more = (f'<span class="rc-more">共 {_cnt} 条，这里显示前 {len(_ls)} 条</span>'
                         if _cnt > len(_ls) else '')
                text = (f'<div class="rc"><div class="rc-h">{html.escape(_t)}'
                        f'<i>{_cnt} 条</i></div>'
                        + (f'<div class="rc-l">{_body}</div>' if _body else '')
                        + f'{_more}</div>')
                kind = 'record'
            else:
                head = f'[{label}] '
                text = head + (ri['title'] or title or ri['des'] or des or '(内容无法解析)')
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
        # <msg username="wxid_xxx" nickname="张三" alias="..." ... />
        _raw = content or ''
        _nk = re.search(r'\bnickname="([^"]*)"', _raw)
        _al = re.search(r'\balias="([^"]*)"', _raw)
        _nm = html.unescape(_nk.group(1)) if _nk else ''
        _al = html.unescape(_al.group(1)) if _al else ''
        if _nm or _al:
            text = ('[名片] ' + (_nm or _al)
                    + (f'（{_al}）' if _nm and _al else ''))
            kind = 'card'
        elif _raw.strip().startswith('<'):
            text = '[名片]'
            kind = 'card'
        else:
            text = '[名片] ' + _raw[:40]
            kind = 'card'
    elif mtype == 3:
        _base = media_bk[:-7] if media_bk.endswith('__thumb') else media_bk
        bfimg = BF_MEDIA.get(_base) if _base else None
        bkimg = BK_MEDIA.get(media_bk) if media_bk else None
        if bfimg:
            # 直接显示备份原图（已压到 ≤1280px），列表里不再挂 120px 缩略图
            dim = image_size(bfimg)
            dim_s = f' width="{dim[0]}" height="{dim[1]}"' if dim else ''
            text = (f'<a href="../media/{html.escape(bfimg)}" data-zoom>'
                    f'<img class="pic" loading="lazy"{dim_s} src="../media/{html.escape(bfimg)}" alt="图片"></a>')
            kind = 'image'
            thumb_only = False
        elif LOCAL_IMG.get((talker, create_time)):
            # 本机有原图就用它
            _full, _th = LOCAL_IMG[(talker, create_time)]
            dim = image_size(_full)
            dim_s = f' width="{dim[0]}" height="{dim[1]}"' if dim else ''
            text = (f'<a href="../media/{html.escape(_full)}" data-zoom>'
                    f'<img class="pic" loading="lazy"{dim_s} src="../media/{html.escape(_full)}" alt="图片"></a>')
            kind = 'image'
            thumb_only = '_t.' in _full
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
    elif mtype == 43:
        # 视频：XML 里 length=mp4 字节数、cdnthumblength=封面字节数，按大小匹配本地缓存
        _raw = content or ''
        _ln = re.search(r'\blength="(\d+)"', _raw)
        _tl = re.search(r'\bcdnthumblength="(\d+)"', _raw)
        _pl = re.search(r'\bplaylength="(\d+)"', _raw)
        _sec = int(_pl.group(1)) if _pl else 0
        _dur = f'{_sec // 60}:{_sec % 60:02d}' if _sec else ''
        _mp4 = VID_FILE.get(_ln.group(1)) if _ln else None
        _jpg = VID_THUMB.get(_tl.group(1)) if _tl else None
        _vb = media_bk[:-7] if media_bk.endswith('__thumb') else media_bk
        _bkv = BK_VIDEO.get(_vb) if _vb else None
        if not _mp4 and _bkv and _bkv.get('v'):
            _mp4 = _bkv['v']
            if not _jpg and _bkv.get('t'):
                _jpg = _bkv['t']
            if not _sec and _bkv.get('d'):
                _sec = int(_bkv['d'])
                _dur = f'{_sec // 60}:{_sec % 60:02d}'
        if _mp4:
            _poster = f' poster="../media/{html.escape(_jpg)}"' if _jpg else ''
            text = (f'<video class="vid" controls preload="none" playsinline{_poster} '
                    f'src="../media/{html.escape(_mp4)}"></video>'
                    + (f'<span class="dur">{_dur}</span>' if _dur else ''))
            kind = 'video'
        elif _jpg:
            text = (f'<div class="vidph"><img loading="lazy" src="../media/{html.escape(_jpg)}" alt="视频">'
                    f'<span class="vplay">▶</span>'
                    + (f'<span class="vdur">{_dur}</span>' if _dur else '') + '</div>')
            kind = 'video'
        else:
            text = '[视频]' + (f' {_dur}' if _dur else '') + '（本地无缓存）'
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
.wrap{max-width:1100px;margin:0 auto;padding:16px;width:100%}
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
.ava-sm{width:22px;height:22px;border-radius:4px;object-fit:cover;vertical-align:middle;margin-right:6px}
.ava-ph{display:inline-flex;width:22px;height:22px;border-radius:4px;background:var(--ava);color:#fff;
font-size:12px;line-height:1;align-items:center;justify-content:center;vertical-align:middle;margin-right:6px}
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
/* 引用回复 */
.rp{max-width:min(340px,72vw)}
.rp-q{font-size:12px;line-height:1.5;color:var(--sub);background:var(--chip);border-left:2px solid var(--line);
border-radius:5px;padding:5px 8px;margin-bottom:6px;max-height:5em;overflow:hidden}
.rp-q .rp-n{color:var(--link);font-weight:600;margin-right:4px}
.rp-t{white-space:pre-wrap;word-break:break-word}
/* 音乐分享 */
.mc{display:flex;gap:10px;align-items:center;min-width:190px;max-width:min(300px,72vw);
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 11px;color:inherit;
text-decoration:none}
.mc:hover{border-color:var(--link);background:var(--hover)}
.mc-i{width:44px;height:44px;flex:none;border-radius:8px;display:flex;align-items:center;justify-content:center;
font-size:20px;color:#fff;background:linear-gradient(135deg,#31c27c,#1b8f5c)}
.mc-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.mc-a{display:block;font-size:13px;font-weight:600;color:var(--text);overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.mc-b{display:block;font-size:11px;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mc-c{display:block;font-size:10px;color:var(--sub);opacity:.8;margin-top:1px}
/* 文件 */
.fc{display:flex;gap:10px;align-items:center;min-width:210px;max-width:min(320px,72vw);
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 11px;color:inherit;
text-decoration:none}
a.fc:hover{border-color:var(--link);background:var(--hover)}
.fc-i{width:36px;height:42px;flex:none;border-radius:5px;display:flex;align-items:center;justify-content:center;
font-size:9px;font-weight:700;letter-spacing:.3px;color:#fff;text-transform:uppercase;line-height:1}
.fc-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.fc-n{font-size:13px;line-height:1.35;color:var(--text);display:-webkit-box;-webkit-line-clamp:2;
-webkit-box-orient:vertical;overflow:hidden;word-break:break-all}
.fc-s{font-size:11px;color:var(--sub)}
/* 合并转发的聊天记录 */
.rc{min-width:220px;max-width:min(330px,72vw);background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 12px}
.rc-h{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:13px;font-weight:600;
color:var(--text);padding-bottom:6px;border-bottom:1px solid var(--line)}
.rc-h i{font-style:normal;font-size:11px;font-weight:400;color:var(--sub);flex:none}
.rc-l{margin-top:6px;font-size:12px;line-height:1.55;color:var(--sub);white-space:pre-wrap;
word-break:break-word;max-height:9.5em;overflow:hidden}
.rc-more{display:block;margin-top:5px;font-size:11px;color:var(--sub);opacity:.8}
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
.bubble.vid{padding:4px}
video.vid{display:block;max-width:300px;max-height:340px;border-radius:4px;background:#000}
.vidph{position:relative;display:inline-block;line-height:0;cursor:default}
.vidph img{display:block;max-width:300px;max-height:340px;border-radius:4px;object-fit:cover}
.vidph .vplay{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
width:44px;height:44px;border-radius:50%;background:#000000a6;color:#fff;
display:flex;align-items:center;justify-content:center;font-size:17px;line-height:1;padding-left:3px}
.vidph .vdur{position:absolute;right:6px;bottom:6px;background:#000000a6;color:#fff;
font-size:11px;line-height:1.4;padding:0 5px;border-radius:4px}
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
.tb{border:1px solid var(--line);background:var(--card);color:var(--text2);border-radius:16px;padding:5px 12px;
font-size:13px;cursor:pointer;font-family:inherit;white-space:nowrap}
.tb.on{background:#07c160;color:#fff;border-color:#07c160}
.tb b{font-weight:600;margin-left:3px;opacity:.85}
th.sorted{color:var(--link)}
th.sorted .ar{color:var(--link)}
tr:hover td{background:var(--hover)}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--text2)}
input[type=search]{width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:6px;margin:10px 0;
font-size:13px;background:var(--card);color:var(--text)}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;background:#1a6fc422;color:var(--link)}
#t .tag{display:none}
body[data-tab="all"] #t .tag{display:inline-block}
.tag.g{background:#1f8b3a22;color:#2fa457}
.tag.s{background:#8a6d2f22;color:var(--gold)}
.ltag{display:inline-block;font-size:10px;line-height:1.6;padding:0 5px;margin-left:4px;
border-radius:4px;background:var(--chip);color:var(--chip-t);vertical-align:1px}
.lblsel{margin-left:auto;font-size:12px;padding:4px 8px;border:1px solid var(--line);
border-radius:14px;background:var(--card);color:var(--text);font-family:inherit;cursor:pointer;max-width:190px}
td .ellip{display:block;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.bkc{color:var(--gold);white-space:nowrap;font-size:12px}
td.dt{white-space:nowrap}
@media (min-width:720px){#t{table-layout:fixed}#t th:nth-child(1),#t td:nth-child(1){width:31%}#t th:nth-child(2),#t td:nth-child(2){width:10%}#t th:nth-child(3),#t td:nth-child(3){width:9%}#t th:nth-child(4),#t td:nth-child(4){width:12%}#t th:nth-child(5),#t td:nth-child(5){width:11%}#t th:nth-child(6),#t td:nth-child(6){width:25%}#t th:nth-child(7),#t td:nth-child(7){width:8%}#t td:nth-child(1),#t td:nth-child(4),#t td:nth-child(5){white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#t td .ellip{max-width:none}}
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
a.cd.sel{background:var(--link);color:#fff!important}
a.cd.sel::after{background:#fff}
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
.ybtn{right:183px}
header:has(.ybtn){padding-right:270px}
.dpy{top:76px;width:388px}
.yp-head{padding:9px 12px;font-size:12px;color:var(--sub);border-bottom:1px solid var(--line)}
.yp-y{position:sticky;top:0;z-index:2;background:var(--card);display:flex;align-items:baseline;justify-content:space-between;font-size:12px;font-weight:700;padding:11px 12px 5px;color:var(--text);border-bottom:1px solid var(--line)}
.yp-y i{font-style:normal;font-weight:400;font-size:11px;color:var(--sub)}
.yp-r{display:flex;align-items:center;gap:7px;padding:2px 12px;font-size:12px}
.yp-r.off{opacity:.42}
.yp-m{width:30px;flex:none;color:var(--sub);font-variant-numeric:tabular-nums}
.yp-b{flex:1;min-width:48px;height:7px;background:var(--chip);border-radius:4px;overflow:hidden}
.yp-b i{display:block;height:100%;background:var(--link);border-radius:4px;opacity:.82;transition:opacity .12s}
.yp-r:hover .yp-b i{opacity:1}
.yp-c{width:46px;flex:none;text-align:right;color:var(--text2);font-variant-numeric:tabular-nums}
.yp-n{width:88px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.yp-n:hover{color:var(--link)}
.yp-s{display:flex;align-items:center;gap:6px;padding:7px 10px;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:3;background:var(--card)}
.yp-s input{flex:1;min-width:0;font:inherit;font-size:12px;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);outline:none}
.yp-s input:focus{border-color:var(--link)}
.yp-s .x{flex:none;width:20px;height:20px;line-height:18px;text-align:center;border-radius:50%;border:1px solid var(--line);background:transparent;color:var(--sub);cursor:pointer;font-size:13px;padding:0;display:none}
.yp-s.has .x{display:block}
.yp-sum{padding:9px 12px 6px;font-size:12px;color:var(--sub);border-bottom:1px solid var(--line)}
.yp-sum b{color:var(--text);font-weight:700}
.yp-hit{display:flex;align-items:center;gap:7px;padding:5px 12px;font-size:12px;cursor:pointer;color:var(--text)}
.yp-hit:hover{background:var(--chip)}
.yp-hit .yp-c{width:auto}
.yp-empty{padding:14px 12px;font-size:12px;color:var(--sub);text-align:center}
.yp-r{cursor:pointer}
.yp-r.op,.yp-r:hover{background:var(--hover)}
.yp-d{padding:2px 12px 7px;border-bottom:1px solid var(--line)}
.yp-dh{font-size:11px;color:var(--sub);padding:3px 0 4px}
.yp-dr{display:flex;align-items:center;gap:6px;font-size:12px;padding:2px 0;color:var(--text)}
.yp-dr:hover{color:var(--link)}
.yp-dn{width:86px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.yp-db{flex:1;min-width:40px;height:5px;background:var(--chip);border-radius:3px;overflow:hidden}
.yp-db i{display:block;height:100%;background:var(--link);opacity:.7;border-radius:3px}
.yp-dc{width:46px;flex:none;text-align:right;color:var(--text2);font-variant-numeric:tabular-nums}
.yp-dp{width:34px;flex:none;text-align:right;color:var(--sub);font-variant-numeric:tabular-nums}
.yp-dm{font-size:11px;color:var(--sub);padding:3px 0 1px}
.mdc{text-align:center;white-space:nowrap}
.mdbtn{display:inline-block;font-size:11px;padding:3px 9px;border:1px solid var(--line);
border-radius:11px;color:var(--link);text-decoration:none;font-variant-numeric:tabular-nums}
.mdbtn:hover{background:var(--hover);border-color:var(--link)}
.dpcal{top:76px;width:332px;position:absolute}
body.chat .dpcal{top:64px}
body.chat .ctt{pointer-events:none}
.dpcal #cg{scrollbar-width:none;-ms-overflow-style:none;padding-right:16px}
.dpcal .dp-head{padding-right:16px}
.dpcal #cg::-webkit-scrollbar{width:0;height:0}
.csl{position:absolute;right:1px;width:15px;z-index:6;cursor:pointer;touch-action:none;user-select:none}
.csl::before{content:'';position:absolute;left:6px;top:0;bottom:0;width:3px;background:var(--chip);border-radius:2px}
.csl i{position:absolute;left:3px;width:9px;border-radius:3px;background:var(--sub);opacity:.5;
transition:opacity .12s;min-height:18px}
.csl:hover i,.csl.on i{opacity:.95}
.csl b{position:absolute;right:17px;white-space:nowrap;font-size:11px;font-weight:600;padding:2px 7px;
border-radius:6px;background:var(--card);border:1px solid var(--line);color:var(--text);
box-shadow:0 3px 10px #0002;transform:translateY(-50%);display:none;pointer-events:none;font-variant-numeric:tabular-nums}
.csl.on b{display:block}
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


def write_chat_page(path, talker, msgs, contacts, self_label, avatar='', nav='',
                    caldays=None, calpage=None):
    name = display_name(talker, contacts)
    h_ava = f'<img class="ava-sm" src="../media/avatar/{avatar}" alt="">' if avatar else ''

    # 每个有消息的日期 -> 条数，交给前端画日历
    # caldays：整个会话的日期（跨分片），日历要覆盖全部，而不是只覆盖当前分片
    if caldays:
        day_count = caldays
    else:
        day_count = {}
        for m in msgs:
            d = datetime.fromtimestamp(m['time']).strftime('%Y-%m-%d')
            day_count[d] = day_count.get(d, 0) + 1
    import json
    day_json = json.dumps(day_count, ensure_ascii=False, separators=(',', ':'))
    # 不在本分片的日期 -> 跳到所在分片文件
    page_json = json.dumps(calpage or {}, ensure_ascii=False, separators=(',', ':'))
    _ks = sorted(day_count) or [datetime.fromtimestamp(msgs[0]['time']).strftime('%Y-%m-%d')]
    first_dt = datetime.strptime(_ks[0], '%Y-%m-%d')
    last_dt = datetime.strptime(_ks[-1], '%Y-%m-%d')

    parts = [f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(name)}</title><script>(function(){{var m='auto';try{{m=localStorage.getItem('wxthm')||'auto'}}catch(e){{}}var d=m==='dark'||(m==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);if(d)document.documentElement.setAttribute('data-theme','dark');}})();</script><style>{CSS}</style></head><body class="chat">
<header><h1>{h_ava}{html.escape(name)}</h1>
<div class="meta">{html.escape(talker)} · 共 {len(msgs)} 条 · {fmt_ts(msgs[0]['time'])} ~ {fmt_ts(msgs[-1]['time'])} · <a href="../index.html">返回会话列表</a>{nav}</div>
<div class="thmseg" id="thm"><button data-m="auto" title="跟随系统">🌗</button><button data-m="light" title="浅色模式">☀️</button><button data-m="dark" title="深色模式">🌙</button></div>
<button class="dbtn" id="dbtn">📅 按日期查找</button>
<div class="dpanel dpcal" id="dpanel">
<div class="dp-head"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
<div class="dp-body" id="cg"></div>
<div class="csl" id="csl" title="拖动快速定位月份"><i></i><b id="cslv"></b></div>
</div>
<div class="ctt" id="ctt"></div></header>
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
        bubble_cls = 'bubble' + (' card' if m['kind'] == 'card' else '') + (' link' if m['kind'] == 'link' else '') + (' ph' if m['kind'] == 'placeholder' else '') + (' img' if m['kind'] == 'image' else '') + (' stk' if m['kind'] == 'sticker' else '') + (' loc' if m['kind'] == 'location' else '') + (' vid' if m['kind'] == 'video' else '') + (' thumb' if m.get('thumb_only') else '')
        body = (m['text'] if m['kind'] in ('image', 'link', 'voice', 'sticker', 'location',
                                           'video', 'reply', 'music', 'file', 'record')
                else emoji_html(html.escape(m['text'])))
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
var DAYS=__DAYS__, PGS=__PGS__, Y1=__Y1__, M1=__M1__, Y2=__Y2__, M2=__M2__;
var grid=document.getElementById('cg'), ctt=document.getElementById('ctt');
var csl=document.getElementById('csl'), cslb=csl?csl.querySelector('i'):null,
    cslv=document.getElementById('cslv'), cslDrag=false;
function pad2(n){return n<10?'0'+n:''+n;}
function todayKey(){var d=new Date();return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate());}
var TODAY=todayKey();
function monthCells(y,m){
  var dow=new Date(y,m-1,1).getDay(), total=new Date(y,m,0).getDate(), h='';
  for(var i=0;i<dow;i++) h+='<span class="cd pad"></span>';
  for(var d=1;d<=total;d++){
    var k=y+'-'+pad2(m)+'-'+pad2(d), c=DAYS[k], pg=PGS[k];
    var cls='cd'+(k===TODAY?' today':'');
    h+= c ? '<a class="'+cls+'" data-d="'+k+'" href="'+(pg?pg+'#d'+k:'#d'+k)+'">'+d+'</a>'
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

/* 悬停浮层：这天有多少条 */
function tipShow(a){
  var k=a.dataset.d||'', c=DAYS[k]||0, pg=PGS[k];
  ctt.innerHTML='<div><b>'+k+'</b> <span class="s">'+c+' 条消息</span></div>'
    + '<div class="s" style="margin-top:4px">点日期跳到那天'
    + (pg?'（在另一分片）':'')+'</div>';
  ctt.classList.add('on');
  var r=a.getBoundingClientRect(), w=ctt.offsetWidth, hh=ctt.offsetHeight;
  var left=Math.min(Math.max(8, r.left+r.width/2-w/2), innerWidth-w-8);
  var top=r.bottom+6;
  if(top+hh>innerHeight-8) top=Math.max(8, r.top-hh-6);
  ctt.style.left=left+'px'; ctt.style.top=top+'px';
}
grid.addEventListener('mouseover',function(e){
  var a=e.target.closest('a.cd');
  if(a) tipShow(a); else ctt.classList.remove('on');
});
grid.addEventListener('mouseleave',function(){ctt.classList.remove('on');});

/* 右侧滑块：拖动时实时显示当前年月 */
function cslLayout(){
  if(!csl||!cslb) return;
  if(!grid.clientHeight) return;
  csl.style.top=grid.offsetTop+'px';
  csl.style.height=grid.clientHeight+'px';
  cslSync();
}
function cslSync(){
  if(!csl||!cslb) return;
  var track=csl.clientHeight, sh=grid.scrollHeight, ch=grid.clientHeight;
  if(!track||!sh) return;
  var hh=Math.max(18, Math.round(track*ch/sh));
  if(hh>track) hh=track;
  cslb.style.height=hh+'px';
  var max=sh-ch, span=track-hh;
  cslb.style.top=((max>0&&span>0)? (grid.scrollTop/max)*span : 0)+'px';
}
function curLabel(){
  var top=grid.getBoundingClientRect().top, hs=grid.querySelectorAll('.cmh'), out='';
  for(var i=0;i<hs.length;i++){
    if(hs[i].getBoundingClientRect().top-top<=4) out=hs[i].textContent; else break;
  }
  return out || (hs[0]?hs[0].textContent:'');
}
function cslLabel(){
  if(!cslv||!cslb) return;
  cslv.textContent=curLabel();
  cslv.style.top=(parseFloat(cslb.style.top||'0')+cslb.offsetHeight/2)+'px';
}
function cslTo(clientY){
  var r=csl.getBoundingClientRect(), hh=cslb.offsetHeight, span=r.height-hh;
  var y=Math.max(0, Math.min(span, clientY-r.top-hh/2));
  var max=grid.scrollHeight-grid.clientHeight;
  grid.scrollTop=(span>0? y/span : 0)*(max>0?max:0);
  cslSync();
  cslLabel();
}
if(csl){
  csl.addEventListener('pointerdown',function(e){
    if(e.pointerType==='mouse' && e.button!==0) return;
    cslDrag=true; csl.classList.add('on');
    try{csl.setPointerCapture(e.pointerId);}catch(_e){}
    cslTo(e.clientY);
    e.preventDefault(); e.stopPropagation();
  });
  csl.addEventListener('pointermove',function(e){
    if(!cslDrag) return;
    cslTo(e.clientY);
    e.preventDefault();
  });
  function cslEnd(e){
    if(!cslDrag) return;
    cslDrag=false;
    csl.classList.remove('on');
    try{csl.releasePointerCapture(e.pointerId);}catch(_e){}
  }
  csl.addEventListener('pointerup',cslEnd);
  csl.addEventListener('pointercancel',cslEnd);
  csl.addEventListener('lostpointercapture',function(){cslDrag=false;csl.classList.remove('on');});
  grid.addEventListener('scroll',function(){cslSync(); if(cslDrag) cslLabel();});
  window.addEventListener('resize',cslLayout);
}

dbtn.addEventListener('click',function(e){
  dpanel.classList.toggle('on');
  if(dpanel.classList.contains('on')){
    grid.scrollTop=grid.scrollHeight;
    setTimeout(cslLayout,0);
  }else{
    ctt.classList.remove('on');
  }
  e.stopPropagation();
});
dpanel.addEventListener('click',function(e){
  if(e.target.closest('a.cd')){dpanel.classList.remove('on'); ctt.classList.remove('on');}
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
if(mq.addEventListener)mq.addEventListener('change',apply);})();</script></body></html>""".replace('__DAYS__', day_json).replace('__PGS__', page_json).replace('__Y1__', str(first_dt.year)).replace('__M1__', str(first_dt.month)).replace('__Y2__', str(last_dt.year)).replace('__M2__', str(last_dt.month)))
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
    global MEDIA_ROOT, BK_MEDIA, BF_MEDIA, OG_MAP, VOICE_MAP, VID_THUMB, VID_FILE, BK_VIDEO, FILE_MAP, LOCAL_IMG
    MEDIA_ROOT = os.path.join(out_dir, 'media')
    _vmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_map.json')
    if os.path.exists(_vmap):
        import json as _jv
        with open(_vmap, encoding='utf-8') as _f:
            VOICE_MAP.update(_jv.load(_f))
        print('备份语音: %d 条' % len(VOICE_MAP))
    _vidmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'video_map.json')
    if os.path.exists(_vidmap):
        import json as _jvd
        with open(_vidmap, encoding='utf-8') as _f:
            _vd = _jvd.load(_f)
        VID_THUMB.update(_vd.get('thumbs') or {})
        VID_FILE.update(_vd.get('files') or {})
        print('本地视频: 封面 %d / 视频 %d' % (len(VID_THUMB), len(VID_FILE)))
    _bvmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bk_video_map.json')
    if os.path.exists(_bvmap):
        import json as _jbv
        with open(_bvmap, encoding='utf-8') as _fbv:
            BK_VIDEO.update(_jbv.load(_fbv))
        print('备份视频: %d 条' % len(BK_VIDEO))
    _fmap = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'file_map.json')
    if os.path.exists(_fmap):
        import json as _jf
        with open(_fmap, encoding='utf-8') as _ff:
            FILE_MAP.update(_jf.load(_ff))
        print('本机文件: %d 个' % len(FILE_MAP))
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
    LOCAL_IMG.update(build_local_img_index(msg_dir, media_idx))
    print('本机原图映射: %d 条（按会话+时间戳）' % len(LOCAL_IMG))
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

    # 手工排除的会话（只有添加好友记录等，无保留价值）：完全不导出
    EXCLUDE_TALKERS = {'qy-saien',            # 曲媛
                       'rdhello09',           # lucky goddess
                       'wxid_your_account', # 橙子🍊
                       'wxid_your_account', # Melody
                       'wxid_your_account', # 青藤-on the way
                       'wxid_your_account', # 早晨
                       'wxid_your_account', # 夏洁
                       'wxid_your_account', # YouMe
                       'wxid_your_account', # wl3.
                       'wxid_your_account'} # Too
    for _t in EXCLUDE_TALKERS:
        raw.pop(_t, None)

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
    md_dir = os.path.join(out_dir, 'md')
    os.makedirs(md_dir, exist_ok=True)
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
        # 先把「日期 -> 分片」算好：单会话日历要覆盖整个会话，不能只看当前分片
        all_days, date_file = {}, {}
        for pi, rpart in enumerate(chunks, 1):
            _fn = f'{base}.html' if len(chunks) == 1 else f'{base}_p{pi}.html'
            for x in rpart:
                _d = datetime.fromtimestamp(x[4]).strftime('%Y-%m-%d')
                all_days[_d] = all_days.get(_d, 0) + 1
                if _d not in date_file:
                    date_file[_d] = 'chats/' + _fn
        files = []
        lastm = None
        thumbs = 0
        # Markdown 导出：一个会话一个 .md，按分片流式追加
        md_name = base + '.md'
        md_f = open(os.path.join(md_dir, md_name), 'w', encoding='utf-8')
        md_f.write(f'# {name}\n\n- 微信号：{talker}\n- 消息数：{len(rws)}\n'
                   f'- 时间：{fmt_ts(rws[0][4])} ~ {fmt_ts(rws[-1][4])}\n')
        md_day = None
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
                            avatar_map.get(talker, ''), nav,
                            all_days,
                            {d: f.split('/', 1)[-1] for d, f in date_file.items()
                             if f != 'chats/' + fn})
            files.append(fn)
            thumbs += sum(1 for m in part if m.get('thumb_only'))
            lastm = part[-1] if part else lastm
            fi = len(srch_files)
            srch_files.append([name if len(chunks) == 1 else f'{name} ({pi}/{len(chunks)})',
                               'chats/' + fn])
            # 搜索索引只取每会话最近 800 条：93 万条全量会让 index.html 涨到 60MB+
            for m in part[-800:]:
                body = {'image': '[图片]', 'voice': '[语音]', 'video': '[视频]',
                        'reply': '[回复]', 'music': '[音乐]', 'file': '[文件]',
                        'record': '[聊天记录]'}.get(m['kind'], m['text'])
                srch_recs.append([fi, m['time'], m['sender'] or name, body[:120], m['id']])
            for m in part:
                _pd = datetime.fromtimestamp(m['time']).strftime('%Y-%m-%d')
                if _pd not in date_file:
                    date_file[_pd] = 'chats/' + fn
                val = {'image': '[图片]', 'voice': '[语音]', 'video': '[视频]'}.get(
                    m['kind'], re.sub(r'<[^>]+>', '', m['text']))
                csv_w.writerow([name, talker, m['sender'] or name, '是' if m['is_sender'] else '否',
                                fmt_ts(m['time']), m['type'], val])
                if _pd != md_day:
                    md_f.write('\n## ' + _pd + '\n\n')
                    md_day = _pd
                if m['kind'] == 'system':
                    md_f.write('`' + fmt_ts(m['time']) + '` ' + val.replace('\n', ' ') + '\n\n')
                else:
                    _who = '我' if m['is_sender'] else (m['sender'] or name)
                    md_f.write('**' + fmt_ts(m['time'])[11:] + ' ' + _who + '**\n\n' + val + '\n\n')
        if lastm['kind'] in ('image', 'voice'):
            last_plain = {'image': '[图片]', 'voice': '[语音]', 'video': '[视频]'}[lastm['kind']]
        else:
            last_plain = re.sub(r'<[^>]+>', '', lastm['text'])
        md_f.close()
        _ci = len(index_rows)
        _dc = {}
        for _x in rws:
            _d = datetime.fromtimestamp(_x[4]).strftime('%Y-%m-%d')
            _dc[_d] = _dc.get(_d, 0) + 1
        _last_href = 'chats/' + files[-1]
        for _d, _c in _dc.items():
            _pf = date_file.get(_d) or _last_href
            CAL.setdefault(_d, []).append([_ci, _c, _pf] if _pf != _last_href else [_ci, _c])
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
            'md': 'md/' + md_name,
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

    GMAP = {'single': 0, 'group': 1, 'service': 2, 'work': 3, 'job': 4, 'other': 5,
            'classmate': 6}
    tr = []
    work_n = job_n = other_n = classmate_n = 0
    for it in index_rows:
        _k = it.get('kind') or ('group' if it['is_group'] else 'single')
        tag = {'group': '<span class="tag g">群</span>',
               'service': '<span class="tag s">服务号</span>'}.get(_k, '<span class="tag">单聊</span>')
        _lbs = list(it.get('labels') or [])
        # 手工补标签（数据库里没有的）：名字 -> 标签列表
        for _ml in MANUAL_LABELS.get(it['name'], []):
            if _ml not in _lbs:
                _lbs.append(_ml)
        # 分组规则：工作 / 求职 / 其他。标签或备注名命中都算。
        # 单聊全部参与；服务号（多为 @openim 的企业微信联系人）只按名字归到求职/其他，
        # 不参与工作（避免「博世会员互动官」这类公众号被误归）。
        if _k in ('single', 'service'):
            _nm = it['name'] or ''
            if (_k == 'single' and (any(x in WORK_LABELS for x in _lbs)
                                    or any(_nm.startswith(p) for p in WORK_LABELS))):
                _k = 'work'
                tag = '<span class="tag">工作</span>'
                work_n += 1
            elif (any(x in JOB_LABELS for x in _lbs) or '猎头' in _nm or 'HR' in _nm.upper()):
                _k = 'job'
                tag = '<span class="tag">求职</span>'
                job_n += 1
            elif (any(k in x for x in _lbs for k in OTHER_LABEL_KEYWORDS)
                  or any(k in _nm for k in OTHER_NAME_KEYWORDS)):
                _k = 'other'
                tag = '<span class="tag">其他</span>'
                other_n += 1
            elif any(l.startswith(x) for l in _lbs for x in CLASSMATE_LABELS):
                _k = 'classmate'
                tag = '<span class="tag">同学</span>'
                classmate_n += 1
            else:
                _ov = next((g for g, kw in GROUP_OVERRIDES if kw in _nm), None)
                if _ov:
                    _k = _ov
                    tag = {'work': '<span class="tag">工作</span>',
                           'job': '<span class="tag">求职</span>',
                           'other': '<span class="tag">其他</span>'}[_ov]
                    if _ov == 'work':
                        work_n += 1
                    elif _ov == 'job':
                        job_n += 1
                    else:
                        other_n += 1
        it['gnum'] = _k          # 记录最终分组，供下面计数用
        lbls = ''.join(f'<span class="ltag">{html.escape(x)}</span>'
                       for x in (it.get('labels') or []))
        if it.get('avatar'):
            ava = f'<img class="ava-sm" src="media/avatar/{it["avatar"]}" alt="">'
        else:
            ava = f'<span class="ava-ph">{html.escape(it["name"][:1])}</span>'
        has_pc = it['talker'] in pc_talkers
        has_bk = it['talker'] in bk_talkers
        src = '本机+备份' if (has_pc and has_bk) else ('仅备份' if has_bk else '本机')
        bk_cell = f'<td class="bkc" data-v="{src}">{src}</td>'
        md_cell = (f'<td class="mdc"><a class="mdbtn" href="{html.escape(it["md"], quote=True)}"'
                   f' download="{html.escape(os.path.basename(it["md"]), quote=True)}"'
                   f' title="下载该会话全部聊天记录（Markdown）">MD</a></td>')
        tr.append(f"""<tr data-n="{html.escape(it['name'] + ' ' + it['talker'], quote=True)}" data-g="{GMAP[_k]}" data-l="{html.escape('|'.join(_lbs), quote=True)}"><td data-v="{html.escape(it['name'])}">{ava}<a href="{html.escape(it['file'])}">{html.escape(it['name'])}</a> {tag}{lbls}</td>
<td class="num" data-t="num" data-v="{it['count']}">{it['count']}</td><td class="num" data-t="num" data-v="{it['thumbs']}">{it['thumbs'] or '—'}</td><td class="dt" data-v="{it['end']}">{it['end'][:10]}</td>{bk_cell}<td data-v="{html.escape(it['full'])}"><span class="ellip" title="{html.escape(it['full'])}">{html.escape(it['last'])}</span></td>{md_cell}</tr>""")

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
    n_class = sum(1 for it in index_rows if it.get('gnum') == 'classmate')
    n_group = sum(1 for it in index_rows if it.get('gnum') == 'group')
    n_service = sum(1 for it in index_rows if it.get('gnum') == 'service')
    n_single = sum(1 for it in index_rows if it.get('gnum') == 'single')

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
<button class="tbtn ybtn" id="ybtn" title="年度统计：每年每月聊得最多的人">📊</button>
<button class="tbtn cbtn" id="cbtn" title="按日期查找：悬停看当天和谁聊了多少条">📅</button>
<div class="dpanel dpy" id="ypanel"><div class="yp-head">年度统计 · 每月聊天最多的人</div><div class="yp-s" id="yps"><input id="ypq" type="text" autocomplete="off" placeholder="搜索单聊对象，看他的按月分布…"><button class="x" id="ypx" title="清空">×</button></div><div class="dp-body" id="yg"></div></div>
<div class="dpanel dpcal" id="cpanel">
<div class="ctabs"><button class="ctb" data-g="all">全部</button><button class="ctb on" data-g="0">单聊</button><button class="ctb" data-g="1">群聊</button></div>
<div class="dp-head"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
<div class="dp-body" id="cg"></div>
<div class="csl" id="csl" title="拖动快速定位月份"><i></i><b id="cslv"></b></div>
</div>
<div class="ctt" id="ctt"></div></header>
<div class="wrap"><input type="search" id="q" placeholder="搜会话名 / 微信号 / 聊天内容关键字…" autocomplete="off">
{bk_bar}
<div class="tabs"><button class="tb" data-g="all">全部 <b>{len(index_rows)}</b></button><button class="tb on" data-g="0">单聊 <b>{n_single}</b></button><button class="tb" data-g="3" title="博世 / 蔚来 / 安财 / 前同事 等单聊">工作 <b>{work_n}</b></button><button class="tb" data-g="4" title="猎头 / HR / offer 等单聊">求职 <b>{job_n}</b></button><button class="tb" data-g="6" title="初中 / 大工 / 东工大 / 材日1001 / 熊井村石研 等同学">同学 <b>{n_class}</b></button><button class="tb" data-g="5" title="租房、快递、商家服务等杂项">其他 <b>{other_n}</b></button><button class="tb" data-g="1">群聊 <b>{n_group}</b></button><button class="tb" data-g="2" title="公众号 / 企业微信服务号 / 系统账号（文件传输助手等）">服务号 <b>{n_service}</b></button>{lbl_sel}</div>
<table id="t"><thead><tr>
<th data-col="0">会话<span class="ar"></span></th>
<th data-col="1" class="sorted desc">消息数<span class="ar">▼</span></th>
<th data-col="2" title="只有缩略图、需要在微信里点开才能缓存的图片数">缺原图<span class="ar"></span></th>
<th data-col="3">最近<span class="ar"></span></th>
<th data-col="4" title="本机微信数据库 / 手机备份（BAK_0_TEXT）">来源<span class="ar"></span></th>
<th data-col="5">最后一条<span class="ar"></span></th>
<th>导出</th>
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
document.body.dataset.tab=tabG;
const tabs=[...document.querySelectorAll('.tb')];
tabs.forEach(function(b){{b.addEventListener('click',function(){{
  tabs.forEach(function(x){{x.classList.remove('on');}});
  b.classList.add('on'); tabG=b.dataset.g; document.body.dataset.tab=tabG; rebuildLbl(); run();
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
/* 标签下拉只显示当前分组里实际存在的标签（切分组时重建） */
function rebuildLbl(){{
  const cnt={{}};
  [...tbody.querySelectorAll('tr')].forEach(function(r){{
    if(tabG!=='all' && r.dataset.g!==tabG) return;
    const l=r.dataset.l||'';
    if(!l) return;
    l.split('|').forEach(function(x){{ if(x) cnt[x]=(cnt[x]||0)+1; }});
  }});
  const cur=lblSel.value;
  const ks=Object.keys(cnt).sort(function(a,b){{return cnt[b]-cnt[a]||(a<b?-1:1);}});
  lblSel.innerHTML='<option value="">全部标签</option>'
    + ks.map(function(k){{return '<option value="'+esc(k)+'">'+esc(k)+' ('+cnt[k]+')</option>';}}).join('');
  lblSel.style.display=ks.length?'':'none';
  lblSel.value=(cur&&cnt[cur])?cur:'';
}}
rebuildLbl();
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
var calG='0', pinnedDay=null, pinned=false, hideT=null, cYears=[];

/* 某天在当前视图（全部/单聊/群聊）里是否真的有记录 */
function dayHas(k,g){{
  var L=CAL[k]; if(!L||!L.length) return false;
  if(g==='all') return true;
  for(var i=0;i<L.length;i++){{
    var c=CONV[L[i][0]];
    if(c && String(c[2])===g) return true;
  }}
  return false;
}}
function cells(y,m){{
  var dow=new Date(y,m-1,1).getDay(), total=new Date(y,m,0).getDate(), h='';
  for(var i=0;i<dow;i++) h+='<span class="cd pad"></span>';
  for(var d=1;d<=total;d++){{
    var k=y+'-'+pad2(m)+'-'+pad2(d);
    h += dayHas(k,calG)
      ? '<a class="cd'+(k===pinnedDay?' sel':'')+'" data-d="'+k+'">'+d+'</a>'
      : '<span class="cd">'+d+'</span>';
  }}
  return h;
}}
function renderCal(){{
  var ks=Object.keys(CAL).sort();
  if(!ks.length){{cbtn.style.display='none';return;}}
  if(!cYears.length){{
    for(var y=+ks[0].slice(0,4); y<=+ks[ks.length-1].slice(0,4); y++) cYears.push(y);
  }}
  var h='';
  for(var i=0;i<cYears.length;i++) for(var m=1;m<=12;m++)
    h+='<div class="cmh">'+cYears[i]+'年'+m+'月</div><div class="cgrid">'+cells(cYears[i],m)+'</div>';
  var st=cg.scrollTop;
  cg.innerHTML=h;
  cg.scrollTop=st;
  if(pinnedDay){{
    var a=cg.querySelector('a.cd[data-d="'+pinnedDay+'"]');
    if(a) tipShow(a);
    else {{pinnedDay=null; pinned=false; ctt.classList.remove('on');}}
  }}
  cslLayout();
}}
function scheduleHide(){{
  clearTimeout(hideT);
  hideT=setTimeout(function(){{if(!pinned) ctt.classList.remove('on');}},200);
}}
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
    out+='<a href="'+(x[2]||c[1])+'#d'+k+'">'
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
renderCal();

/* 悬停预览；点击钉住（浮层保持显示，链接可点） */
cg.addEventListener('mouseover',function(e){{
  var a=e.target.closest('a.cd');
  if(a){{clearTimeout(hideT); tipShow(a);}}
  else if(!pinned){{ctt.classList.remove('on');}}
}});
cg.addEventListener('mouseleave',function(){{
  if(pinned){{
    var a=cg.querySelector('a.cd.sel');
    if(a) tipShow(a); else ctt.classList.remove('on');
  }} else scheduleHide();
}});
cg.addEventListener('click',function(e){{
  var a=e.target.closest('a.cd');
  if(!a) return;
  e.preventDefault(); e.stopPropagation();
  var d=a.dataset.d;
  var prev=cg.querySelector('a.cd.sel');
  if(prev) prev.classList.remove('sel');
  if(pinnedDay===d){{                      /* 再点同一天 = 取消选中 */
    pinnedDay=null; pinned=false; ctt.classList.remove('on'); return;
  }}
  a.classList.add('sel');
  pinnedDay=d; pinned=true;
  tipShow(a);
}});
ctt.addEventListener('mouseenter',function(){{clearTimeout(hideT);}});
ctt.addEventListener('mouseleave',function(){{if(!pinned) scheduleHide();}});
cbtn.addEventListener('click',function(e){{
  var _yp=document.getElementById('ypanel'); if(_yp) _yp.classList.remove('on');
  cpanel.classList.toggle('on');
  if(cpanel.classList.contains('on')){{
    var a=pinnedDay ? cg.querySelector('a.cd[data-d="'+pinnedDay+'"]') : null;
    if(a) a.scrollIntoView({{block:'center'}}); else cg.scrollTop=0;
    setTimeout(cslLayout,0);
  }}
  e.stopPropagation();
}});
[...document.querySelectorAll('.ctb')].forEach(function(b){{
  b.addEventListener('click',function(e){{
    [...document.querySelectorAll('.ctb')].forEach(function(x){{x.classList.remove('on');}});
    b.classList.add('on');
    calG=b.dataset.g;
    renderCal();                          /* 按新视图重标日期；钉住的日期若不存在会自动关闭 */
    e.stopPropagation();
  }});
}});
document.addEventListener('click',function(e){{
  if(!e.target.closest('#cpanel') && !e.target.closest('#cbtn') && !e.target.closest('#ctt')){{
    cpanel.classList.remove('on');
    pinnedDay=null; pinned=false;
    ctt.classList.remove('on');
  }}
}});

/* ---- 右侧滑块：拖动时实时显示当前年月 ---- */
var csl=document.getElementById('csl'),
    cslb=csl?csl.querySelector('i'):null,
    cslv=document.getElementById('cslv'),
    cslDrag=false;

function cslLayout(){{
  if(!csl||!cslb||!cg) return;
  if(!cg.clientHeight) return;                 /* 面板还没显示出来 */
  csl.style.top=cg.offsetTop+'px';
  csl.style.height=cg.clientHeight+'px';
  cslSync();
}}
function cslSync(){{
  if(!csl||!cslb) return;
  var track=csl.clientHeight, sh=cg.scrollHeight, ch=cg.clientHeight;
  if(!track||!sh) return;
  var hh=Math.max(18, Math.round(track*ch/sh));
  if(hh>track) hh=track;
  cslb.style.height=hh+'px';
  var max=sh-ch, span=track-hh;
  cslb.style.top=((max>0&&span>0)? (cg.scrollTop/max)*span : 0)+'px';
}}
/* 视口顶部对应的是哪个月（.cmh 是顺序排列的月份标题） */
function curLabel(){{
  var top=cg.getBoundingClientRect().top, hs=cg.querySelectorAll('.cmh'), out='';
  for(var i=0;i<hs.length;i++){{
    if(hs[i].getBoundingClientRect().top-top<=4) out=hs[i].textContent; else break;
  }}
  return out || (hs[0]?hs[0].textContent:'');
}}
function cslLabel(){{
  if(!cslv||!cslb) return;
  cslv.textContent=curLabel();
  cslv.style.top=(parseFloat(cslb.style.top||'0')+cslb.offsetHeight/2)+'px';
}}
function cslTo(clientY){{
  var r=csl.getBoundingClientRect(), hh=cslb.offsetHeight, span=r.height-hh;
  var y=Math.max(0, Math.min(span, clientY-r.top-hh/2));
  var max=cg.scrollHeight-cg.clientHeight;
  cg.scrollTop=(span>0? y/span : 0)*(max>0?max:0);
  cslSync();
  cslLabel();
}}
if(csl){{
  csl.addEventListener('pointerdown',function(e){{
    if(e.pointerType==='mouse' && e.button!==0) return;
    cslDrag=true; csl.classList.add('on');
    try{{csl.setPointerCapture(e.pointerId);}}catch(_e){{}}
    cslTo(e.clientY);
    e.preventDefault(); e.stopPropagation();
  }});
  csl.addEventListener('pointermove',function(e){{
    if(!cslDrag) return;
    cslTo(e.clientY);
    e.preventDefault();
  }});
  function cslEnd(e){{
    if(!cslDrag) return;
    cslDrag=false;
    csl.classList.remove('on');
    try{{csl.releasePointerCapture(e.pointerId);}}catch(_e){{}}
  }}
  csl.addEventListener('pointerup',cslEnd);
  csl.addEventListener('pointercancel',cslEnd);
  csl.addEventListener('lostpointercapture',function(){{cslDrag=false;csl.classList.remove('on');}});
  cg.addEventListener('scroll',function(){{cslSync(); if(cslDrag) cslLabel();}});
  window.addEventListener('resize',cslLayout);
}}

/* ---- 年度统计：每年每月聊得最多的人（只算单聊）---- */
(function(){{
  var ybtn=document.getElementById('ybtn'), ypanel=document.getElementById('ypanel'),
      yg=document.getElementById('yg'), yps=document.getElementById('yps'),
      ypq=document.getElementById('ypq'), ypx=document.getElementById('ypx');
  if(!ybtn||!ypanel||!yg) return;

  /* 聚合：年-月 -> {{会话索引: 条数}}；同时记下该月每个会话最早出现的日期及其分页
     （点名字要跳到「这个统计时间段开始」的位置，而不是会话末尾） */
  var YM={{}}, YTOT={{}}, FIRST={{}};
  Object.keys(CAL).sort().forEach(function(day){{
    var ym=day.slice(0,7), o=YM[ym]||(YM[ym]={{}}), fo=FIRST[ym]||(FIRST[ym]={{}}), lst=CAL[day];
    for(var i=0;i<lst.length;i++){{
      var ci=lst[i][0], cv=CONV[ci], n=lst[i][1];
      if(!cv || cv[2]===1) continue;              /* 跳过群聊 */
      o[ci]=(o[ci]||0)+n;
      YTOT[ym]=(YTOT[ym]||0)+n;
      if(!fo[ci]) fo[ci]=[day, lst[i][2]||cv[1]]; /* 日期升序遍历，首见即最早 */
    }}
  }});

  var YMS=Object.keys(YM).sort();          /* 升序：2021-02 … 2026-10 */

  /* ---- 视图一：每年每月聊得最多的人 ---- */
  function renderYear(){{
    var years={{}};
    YMS.forEach(function(ym){{
      var y=ym.slice(0,4); (years[y]||(years[y]=[])).push(ym);
    }});
    var h='';
    Object.keys(years).sort().forEach(function(y){{
      var rows=[], max=0;
      for(var m=1;m<=12;m++){{
        var key=y+'-'+(m<10?'0'+m:''+m), o=YM[key];
        var best=-1, bc=0, k;
        if(o) for(k in o){{ if(o[k]>bc){{ bc=o[k]; best=+k; }} }}
        if(bc>max) max=bc;
        rows.push({{m:m, key:key, bc:bc, best:best, tot:(YTOT[key]||0)}});
      }}
      var ytot=rows.reduce(function(a,r){{return a+r.tot;}},0);
      if(!ytot) return;
      h+='<div class="yp-y"><span>'+y+' 年</span><i>共 '+ytot.toLocaleString()+' 条</i></div>';
      rows.forEach(function(r){{
        if(!r.tot){{
          h+='<div class="yp-r off"><span class="yp-m">'+r.m+'月</span>'
           + '<span class="yp-b"><i style="width:0"></i></span>'
           + '<span class="yp-c">—</span><span class="yp-n"></span></div>';
          return;
        }}
        var c=CONV[r.best]||['?','#'], w=Math.max(3, Math.round(r.bc/max*100));
        var fd=(FIRST[r.key]||{{}})[r.best];
        var href = fd ? (fd[1]+'#d'+fd[0]) : c[1];
        h+='<div class="yp-r" data-k="'+r.key+'" title="'+esc(c[0])+' · '+r.bc.toLocaleString()
         + ' 条（当月单聊共 '+r.tot.toLocaleString()+' 条）'
         + (fd?'，点名字从 '+fd[0]+' 开始':'')+' · 点这行展开当月明细">'
         + '<span class="yp-m">'+r.m+'月</span>'
         + '<span class="yp-b"><i style="width:'+w+'%"></i></span>'
         + '<span class="yp-c">'+r.bc.toLocaleString()+'</span>'
         + '<a class="yp-n" href="'+href+'">'+esc(c[0])+'</a></div>';
      }});
    }});
    yg.innerHTML=h || '<div class="yp-empty">没有可统计的单聊记录</div>';
  }}

  function ptotal(ci){{
    var t=0;
    for(var j=0;j<YMS.length;j++) t+=(YM[YMS[j]][ci]||0);
    return t;
  }}

  /* ---- 视图二：指定对象的按月分布（跨全部年份，条数按他自己的峰值缩放）---- */
  function renderPerson(ci){{
    var c=CONV[ci]||['?','#'], rows=[], tot=0, max=0;
    YMS.forEach(function(ym){{
      var n=YM[ym][ci];
      if(!n) return;
      rows.push({{ym:ym, n:n, fd:(FIRST[ym]||{{}})[ci]}});
      tot+=n;
      if(n>max) max=n;
    }});
    if(!rows.length){{
      yg.innerHTML='<div class="yp-empty">没有与「'+esc(c[0])+'」的单聊记录</div>';
      return;
    }}
    var h='<div class="yp-sum"><b>'+esc(c[0])+'</b> · 共 '+tot.toLocaleString()+' 条 · '
      + rows[0].ym+' ~ '+rows[rows.length-1].ym+' · '+rows.length+' 个月有聊天</div>';
    var cy=null;
    rows.forEach(function(r){{
      var y=r.ym.slice(0,4), m=+r.ym.slice(5,7);
      if(y!==cy){{
        var yt=0;
        for(var j=0;j<rows.length;j++) if(rows[j].ym.slice(0,4)===y) yt+=rows[j].n;
        h+='<div class="yp-y"><span>'+y+' 年</span><i>共 '+yt.toLocaleString()+' 条</i></div>';
        cy=y;
      }}
      var w=Math.max(3, Math.round(r.n/max*100));
      var href = r.fd ? (r.fd[1]+'#d'+r.fd[0]) : c[1];
      h+='<div class="yp-r" title="'+r.ym+' 共 '+r.n.toLocaleString()+' 条'
       + (r.fd?'，点击从 '+r.fd[0]+' 开始':'')+'">'
       + '<span class="yp-m">'+m+'月</span>'
       + '<span class="yp-b"><i style="width:'+w+'%"></i></span>'
       + '<span class="yp-c">'+r.n.toLocaleString()+'</span>'
       + (r.fd?'<a class="yp-n" href="'+href+'">'+r.fd[0].slice(5)+' 起</a>'
              :'<span class="yp-n"></span>')+'</div>';
    }});
    yg.innerHTML=h;
  }}

  /* ---- 搜索 ---- */
  function onQuery(){{
    var q=(ypq.value||'').trim();
    yps.classList.toggle('has', !!q);
    if(!q){{ renderYear(); return; }}
    var ql=q.toLowerCase(), hs=[];
    for(var i=0;i<CONV.length;i++){{
      var c=CONV[i];
      if(!c || c[2]===1 || !c[0]) continue;        /* 只搜单聊 */
      if(c[0].toLowerCase().indexOf(ql)>=0) hs.push(i);
    }}
    if(!hs.length){{
      yg.innerHTML='<div class="yp-empty">没有匹配「'+esc(q)+'」的单聊对象</div>';
      return;
    }}
    if(hs.length===1){{ renderPerson(hs[0]); return; }}
    hs.sort(function(a,b){{ return ptotal(b)-ptotal(a); }});
    var h='<div class="yp-sum">匹配到 <b>'+hs.length+'</b> 个单聊对象，点一个看按月分布</div>';
    hs.slice(0,100).forEach(function(i){{
      h+='<div class="yp-hit" data-ci="'+i+'"><span class="yp-n" style="width:auto;flex:1">'
       + esc(CONV[i][0])+'</span><span class="yp-c">'+ptotal(i).toLocaleString()+'</span></div>';
    }});
    yg.innerHTML=h;
  }}

  /* 某个月的完整明细：谁占了多少条 */
  function monthDetail(key){{
    var o=YM[key]||{{}}, arr=[];
    for(var k in o) arr.push([+k, o[k]]);
    arr.sort(function(a,b){{ return b[1]-a[1]; }});
    var tot=0, i;
    for(i=0;i<arr.length;i++) tot+=arr[i][1];
    var mx=arr.length?arr[0][1]:0;
    var h='<div class="yp-d"><div class="yp-dh">'+key.slice(0,4)+'年'+(+key.slice(5))+'月 · 单聊共 '
        + tot.toLocaleString()+' 条 · '+arr.length+' 人</div>';
    for(i=0;i<arr.length && i<15;i++){{
      var x=arr[i], c=CONV[x[0]]||['?','#'], fd=(FIRST[key]||{{}})[x[0]];
      var href=fd?(fd[1]+'#d'+fd[0]):c[1];
      var w=Math.max(2, Math.round(x[1]/mx*100));
      h+='<a class="yp-dr" href="'+href+'">'
       + '<span class="yp-dn">'+esc(c[0])+'</span>'
       + '<span class="yp-db"><i style="width:'+w+'%"></i></span>'
       + '<span class="yp-dc">'+x[1].toLocaleString()+'</span>'
       + '<span class="yp-dp">'+Math.round(x[1]/tot*100)+'%</span></a>';
    }}
    if(arr.length>15) h+='<div class="yp-dm">还有 '+(arr.length-15)+' 人，再点月份行收起</div>';
    return h+'</div>';
  }}

  yg.addEventListener('click',function(e){{
    var el=e.target.closest('.yp-hit');
    if(el){{ renderPerson(+el.dataset.ci); e.preventDefault(); return; }}
    if(e.target.closest('a.yp-n')||e.target.closest('a.yp-dr')) return;  /* 名字是链接，照常跳转 */
    var r=e.target.closest('.yp-r');
    if(!r || r.classList.contains('off') || !r.dataset.k) return;
    e.preventDefault();
    var nx=r.nextElementSibling;
    if(nx && nx.classList.contains('yp-d')){{
      nx.parentNode.removeChild(nx); r.classList.remove('op'); return;
    }}
    var olds=yg.querySelectorAll('.yp-d'), i;
    for(i=0;i<olds.length;i++) olds[i].parentNode.removeChild(olds[i]);
    var ops=yg.querySelectorAll('.yp-r.op');
    for(i=0;i<ops.length;i++) ops[i].classList.remove('op');
    r.insertAdjacentHTML('afterend', monthDetail(r.dataset.k));
    r.classList.add('op');
  }});
  ypq.addEventListener('input', onQuery);
  ypx.addEventListener('click', function(){{ ypq.value=''; ypx.blur(); onQuery(); }});

  renderYear();
  ybtn.addEventListener('click',function(e){{
    ypanel.classList.toggle('on');
    cpanel.classList.remove('on');
    e.stopPropagation();
  }});
  document.addEventListener('click',function(e){{
    if(!e.target.closest('#ypanel') && !e.target.closest('#ybtn')) ypanel.classList.remove('on');
  }});
}})();
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
    _tt = sum(r['thumbs'] for r in index_rows)
    print('图片：缺原图（只有缩略图）%d 张 · 备份原图 %d · 备份视频 %d' % (
        _tt, len(BF_MEDIA), len(BK_VIDEO)))
    print('入口:', os.path.join(out_dir, 'index.html'))


if __name__ == '__main__':
    main()
