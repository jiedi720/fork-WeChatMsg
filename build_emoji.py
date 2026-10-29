# -*- coding: utf-8 -*-
"""构建 export/media/emoji/ 与 emoji_map.json（中文表情代码 -> 图片文件名）。

数据来源：
  1. emoji_src/modern/*.png  —— airinghost/wechat-emoji 的 web.wechat.com 官方 109 张
     （文件名形如 055_捂脸.png，128x128 透明，覆盖微信 8.x 新增表情）
  2. emoji_src/classic_b64.json —— qiuyinghua/wechat-emoticons 的 106 张经典表情
     （英文键，补 modern 里没有的篮球/足球/闪电/刀/乒乓/饭/投降/Hooray 等）

输出：
  export/media/emoji/e001.png … e109.png（modern，ASCII 名避免路径编码问题）
  export/media/emoji/<english>.png（classic 补充）
  emoji_map.json  {"捂脸": "e055.png", "微笑": "e001.png", …}
"""
import base64
import json
import os
import re

SRC_MODERN = 'emoji_src/modern'
SRC_CLASSIC = 'emoji_src/classic_b64.json'
OUT = 'export/media/emoji'
MAP = 'emoji_map.json'

# classic 中文名 <-> 英文名（README 位置顺序一致）
EN_ROWS = [
    ['Smile', 'Grimance', 'Drool', 'Scowl', 'CoolGuy', 'Sob', 'Shy', 'Silent', 'Sleep', 'Cry', 'Awkward'],
    ['Angry', 'Tongue', 'Grin', 'Surprise', 'Frown', 'Ruthless', 'Blush', 'Scream', 'Puke', 'Chuckle', 'Joyful'],
    ['Slight', 'Smug', 'Hungry', 'Drowsy', 'Panic', 'Sweat', 'Laugh', 'Commando', 'Determined', 'Scold', 'Shocked'],
    ['Shhh', 'Dizzy', 'Tormented', 'Toasted', 'Skull', 'Hammer', 'Wave', 'Speechless', 'NosePick', 'Clap', 'Shame'],
    ['Trick', 'Bah! L', 'Bah! R', 'Yawn', 'Pooh-pooh', 'Shrunken', 'TearingUp', 'Sly', 'Kiss', 'Wrath', 'Whimper'],
    ['Cleaver', 'Watermelon', 'Beer', 'Basketball', 'PingPong', 'Coffee', 'Rice', 'Pig', 'Rose', 'Wilt', 'Lips'],
    ['Heart', 'BrokenHeart', 'Cake', 'Lightning', 'Bomb', 'Dagger', 'Soccer', 'Ladybug', 'Poop', 'Moon', 'Sun'],
    ['Gift', 'Hug', 'ThumbsUp', 'ThumbsDown', 'Shake', 'Peace', 'Fight', 'Beckon', 'Fist', 'Pinky', 'RockOn'],
    ['Nuh-uh', 'OK', 'InLove', 'Blowkiss', 'Waddle', 'Tremble', 'Aaagh!', 'Twirl', 'Kotow', 'Dramatic', 'JumpRope'],
    ['Surrender', 'Hooray', 'Meditate', 'Smooch', 'TaiChi L', 'TaiChi R'],
]
CN_ROWS = [
    ['微笑', '撇嘴', '色', '发呆', '得意', '流泪', '害羞', '闭嘴', '睡', '大哭', '尴尬'],
    ['发怒', '调皮', '呲牙', '惊讶', '难过', '酷', '冷汗', '抓狂', '吐', '偷笑', '愉快'],
    ['白眼', '傲慢', '饥饿', '困', '惊恐', '流汗', '憨笑', '悠闲', '奋斗', '咒骂', '疑问'],
    ['嘘', '晕', '疯了', '衰', '骷髅', '敲打', '再见', '擦汗', '抠鼻', '鼓掌', '糗大了'],
    ['坏笑', '左哼哼', '右哼哼', '哈欠', '鄙视', '委屈', '快哭了', '阴险', '亲亲', '吓', '可怜'],
    ['菜刀', '西瓜', '啤酒', '篮球', '乒乓', '咖啡', '饭', '猪头', '玫瑰', '凋谢', '嘴唇'],
    ['爱心', '心碎', '蛋糕', '闪电', '炸弹', '刀', '足球', '瓢虫', '便便', '月亮', '太阳'],
    ['礼物', '拥抱', '强', '弱', '握手', '胜利', '抱拳', '勾引', '拳头', '差劲', '爱你'],
    ['NO', 'OK', '爱情', '飞吻', '跳跳', '发抖', '怄火', '转圈', '磕头', '回头', '跳绳'],
    ['投降', 'Hooray', 'Meditate', 'Smooch', 'TaiChi L', 'TaiChi R'],
]
OVERRIDE = {
    'CoolGuy': 'cool_guy', 'Awkward': 'akward', 'Ladybug': 'lady_bug',
    'Bah! L': 'bah_l', 'Bah! R': 'bah_r', 'Aaagh!': 'aaagh',
    'Nuh-uh': 'nuh_uh', 'Pooh-pooh': 'pooh_pooh', 'TaiChi L': 'taichi_l', 'TaiChi R': 'taichi_r',
    'PingPong': 'ping_pong', 'BrokenHeart': 'broken_heart', 'ThumbsUp': 'thumbs_up',
    'ThumbsDown': 'thumbs_down', 'RockOn': 'rock_on', 'InLove': 'in_love',
    'JumpRope': 'jump_rope', 'NosePick': 'nose_pick', 'TearingUp': 'tearing_up', 'OK': 'ok',
}


def en_key(en):
    if en in OVERRIDE:
        return OVERRIDE[en]
    return re.sub(r'(?<=[a-z])(?=[A-Z])', '_', en).lower()


def main():
    os.makedirs(OUT, exist_ok=True)
    out_map = {}

    # 1) modern
    names = sorted(os.listdir(SRC_MODERN))
    for fn in names:
        m = re.match(r'^(\d{3})_(.+)\.png$', fn)
        if not m:
            continue
        cn = m.group(2)
        dst = 'e' + m.group(1) + '.png'
        with open(os.path.join(SRC_MODERN, fn), 'rb') as s, open(os.path.join(OUT, dst), 'wb') as d:
            d.write(s.read())
        out_map[cn] = dst

    # 2) classic 补充
    b64 = json.load(open(SRC_CLASSIC, encoding='utf-8'))
    added = 0
    for erow, crow in zip(EN_ROWS, CN_ROWS):
        for en, cn in zip(erow, crow):
            if cn in out_map:
                continue
            k = en_key(en)
            uri = b64.get(k)
            if not uri:
                continue
            dst = k + '.png'
            with open(os.path.join(OUT, dst), 'wb') as f:
                f.write(base64.b64decode(uri.split(',', 1)[1]))
            out_map[cn] = dst
            added += 1

    json.dump(out_map, open(MAP, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print('modern:', len(names), '· classic 补充:', added, '· 合计映射:', len(out_map))


if __name__ == '__main__':
    main()
