import os, sys, shutil, sqlite3, time

root = r"D:\Tools\WeChatMsg"
os.chdir(root)
sys.path.insert(0, root)

from app.decrypt.decrypt import decrypt

KEY = "REPLACE_WITH_YOUR_WX_KEY"
SRC = r"E:\OneDrive\Documents\WeChat Files\wxid_your_account\Msg"
OUT = os.path.join(root, "app", "DataBase", "Msg")
os.makedirs(OUT, exist_ok=True)

targets = []
for dirpath, dirs, files in os.walk(SRC):
    for f in files:
        low = f.lower()
        if not low.endswith(".db"):
            continue
        if low.startswith("fts"):
            continue
        targets.append(os.path.join(dirpath, f))

print("found:", len(targets))
for t in targets:
    base = os.path.basename(t)
    out_path = os.path.join(OUT, base)
    t0 = time.time()
    r = decrypt(KEY, t, out_path)
    print(f"{base:28s} {os.path.getsize(t)/1048576:7.1f}MB  {time.time()-t0:5.1f}s  -> {r}")
