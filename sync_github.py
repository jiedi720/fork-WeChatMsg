# -*- coding: utf-8 -*-
"""把本目录的代码改动脱敏后同步到 https://github.com/jiedi720/fork-WeChatMsg

用法（在 D:\\Tools\\WeChatMsg 下）：
    python sync_github.py            # 只看会同步哪些文件
    python sync_github.py --push     # 真正提交并推送
"""
import os
import re
import subprocess
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
REPO = r'D:\Tools\wechatmsg-repo'

# 需要同步的文件（新增文件也加到这里）
FILES = [
    'export_chats.py', 'build_emoji.py', 'decrypt_media.py', 'fetch_avatars.py',
    'make_previews.py', '_decrypt_all.py', '_merge.py', 'wx4_tool.py',
    'wx4_findkey.py', 'start.bat', 'refresh_images.bat', 'emoji_map.json',
    'sync_github.py', 'find_backup_key.py', 'decrypt_backup.py', 'backup_inventory.py',
    'parse_backup_text.py', 'import_backup.py', 'decrypt_backup_media.py', 'decrypt_backup_voice.py',
    'decrypt_emoji.py',
    'decrypt_backup_media_full.py',
]

def sanitize(text):
    """脱敏：不把本机账号 / 数据库密钥推上公网。"""
    text = re.sub(r'wxid_[0-9a-z]{6,}', 'wxid_your_account', text)
    # 64 位十六进制串只在 KEY = "..." 场景替换，避免误伤
    text = re.sub(r'(KEY\s*=\s*")[0-9a-f]{64}(")', r'\1REPLACE_WITH_YOUR_WX_KEY\2', text)
    return text


def main():
    push = '--push' in sys.argv
    if not os.path.isdir(os.path.join(REPO, '.git')):
        print('[x] 找不到仓库目录：%s' % REPO)
        return 1

    changed = []
    for name in FILES:
        src = os.path.join(SRC, name)
        dst = os.path.join(REPO, name)
        if not os.path.isfile(src):
            continue
        with open(src, 'r', encoding='utf-8', errors='replace') as f:
            text = sanitize(f.read())
        old = None
        if os.path.isfile(dst):
            with open(dst, 'r', encoding='utf-8', errors='replace') as f:
                old = f.read()
        if text != old:
            with open(dst, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
            changed.append(name)

    if not changed:
        print('[=] 没有改动，无需同步')
        return 0

    print('[+] 已更新 %d 个文件：' % len(changed))
    for n in changed:
        print('    ' + n)

    if not push:
        print('\n[!] 预览模式。加 --push 才会提交并推送。')
        return 0

    def run(args):
        print('  $ git ' + ' '.join(args))
        return subprocess.call(['git'] + args, cwd=REPO)

    run(['add', '-A'])
    msg = input('\n提交说明（直接回车用默认）：').strip() or 'chore: 同步本地改动'
    if run(['commit', '-m', msg]) != 0:
        print('[!] 没有可提交的内容，或提交失败')
    run(['push', 'origin', 'master'])
    print('\n[✓] 完成：https://github.com/jiedi720/fork-WeChatMsg')
    return 0


if __name__ == '__main__':
    sys.exit(main())
