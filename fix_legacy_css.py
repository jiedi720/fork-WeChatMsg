# -*- coding: utf-8 -*-
"""修复旧版导出的聊天页 CSS 类名冲突。

历史问题：标签 chip 用了 .lb，与图片灯箱 .lb{display:none;position:fixed;inset:0;z-index:80}
同名；后一条规则把灯箱变成可见的整屏遮罩，导致聊天页看起来「空白 / 页面不存在」。
新版本源码已把 chip 改名为 .ltag，本脚本用于就地修复已经导出的旧文件。
"""
import glob, os, sys

OLD = '.lb{display:inline-block;font-size:10px;line-height:1.6;padding:0 5px;margin-left:4px;'
NEW = '.ltag{display:inline-block;font-size:10px;line-height:1.6;padding:0 5px;margin-left:4px;'

root = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'export')
targets = glob.glob(os.path.join(root, 'chats', '*.html')) + [os.path.join(root, 'index.html')]
fixed = 0
for p in targets:
    if not os.path.isfile(p):
        continue
    with open(p, encoding='utf-8') as f:
        s = f.read()
    if OLD in s:
        with open(p, 'w', encoding='utf-8') as f:
            f.write(s.replace(OLD, NEW))
        fixed += 1
print('扫描 %d 个页面，修复 %d 个' % (len(targets), fixed))
