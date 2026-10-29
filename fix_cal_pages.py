# -*- coding: utf-8 -*-
"""给首页 DATA.c（日期日历）补上「该日期所在的具体分页文件」。
旧数据只存了会话的最后一页，导致 #d日期 锚点在多页会话里根本不存在。
本脚本直接扫描 export/chats/*.html 里的 id="dYYYY-MM-DD" 锚点重建映射。
"""
import json, os, re, glob, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'export')
IDX = os.path.join(ROOT, 'index.html')
CH = os.path.join(ROOT, 'chats')

DAY_ID = re.compile(r'id="d(\d{4}-\d{2}-\d{2})"')
PAGE_NO = re.compile(r'_p(\d+)\.html$')

s = open(IDX, encoding='utf-8').read()
i = s.find('const DATA=')
assert i > 0
j = i + len('const DATA=')
depth, instr, esc = 0, False, False
end = None
for k in range(j, len(s)):
    ch = s[k]
    if instr:
        if esc: esc = False
        elif ch == '\\': esc = True
        elif ch == '"': instr = False
        continue
    if ch == '"': instr = True
    elif ch in '[{': depth += 1
    elif ch in ']}':
        depth -= 1
        if depth == 0:
            end = k + 1
            break
assert end
old_blob = s[j:end]
obj = json.loads(old_blob)
CONV, CAL = obj['v'], obj['c']
print('会话 %d / 日期 %d' % (len(CONV), len(CAL)))

# 按会话前缀缓存： convIdx -> {日期: 相对路径}
cache = {}
def date_map(ci):
    if ci in cache:
        return cache[ci]
    href = CONV[ci][1]                     # chats/001_皮💜蛋_p13.html
    base = re.sub(r'(_p\d+)?\.html$', '', href)   # chats/001_皮💜蛋
    files = glob.glob(os.path.join(ROOT, base.replace('/', os.sep) + '*.html'))
    def key(p):
        m = PAGE_NO.search(p)
        return (0, 0) if not m else (1, int(m.group(1)))
    files.sort(key=key)                     # 单页优先，其后按页码
    d = {}
    for p in files:
        rel = 'chats/' + os.path.basename(p)
        try:
            txt = open(p, encoding='utf-8', errors='ignore').read()
        except OSError:
            continue
        for day in DAY_ID.findall(txt):
            if day not in d:
                d[day] = rel
    cache[ci] = d
    return d

fixed = missing = same = 0
for day, lst in CAL.items():
    for ent in lst:
        ci = ent[0]
        d = date_map(ci)
        pf = d.get(day)
        if not pf:
            missing += 1
            continue
        if pf != CONV[ci][1]:
            if len(ent) > 2:
                ent[2] = pf
            else:
                ent.append(pf)
            fixed += 1
        else:
            same += 1

print('修正 %d 个日期链接指向正确分页；原本就正确 %d；找不到该日期锚点 %d' % (fixed, same, missing))
print('涉及多页会话 %d 个' % sum(1 for v in cache.values() if len(v) > 1 or True))

new_blob = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
out = s[:j] + new_blob + s[end:]
open(IDX, 'w', encoding='utf-8').write(out)
print('index.html %d -> %d 字符' % (len(s), len(out)))
