import os, sqlite3, shutil, datetime

BASE = r"D:\Tools\WeChatMsg\app\DataBase\Msg"
SRCS = ["MSG1.db", "MSG2.db", "MSG2-Y9000P_AI.db", "MSG3.db"]
TARGET = os.path.join(BASE, "MSG.db")

shutil.copyfile(os.path.join(BASE, "MSG0.db"), TARGET)

dst = sqlite3.connect(TARGET)
dst.execute("PRAGMA journal_mode=WAL")
dst_cols = [r[1] for r in dst.execute("PRAGMA table_info(MSG)")]


def ensure_talker(usr):
    r = dst.execute("SELECT rowid FROM Name2ID WHERE UsrName=?", (usr,)).fetchone()
    if r:
        return r[0]
    return dst.execute("INSERT INTO Name2ID(UsrName) VALUES(?)", (usr,)).lastrowid


seen = set()
for (svr, ct, tk) in dst.execute("SELECT MsgSvrID, CreateTime, StrTalker FROM MSG"):
    seen.add((svr, ct, tk))

dst.execute("BEGIN")
total = 0
for name in SRCS:
    path = os.path.join(BASE, name)
    if not os.path.exists(path):
        continue
    src = sqlite3.connect(path)
    cols = [r[1] for r in src.execute("PRAGMA table_info(MSG)")]
    shared = [c for c in cols if c in dst_cols and c.lower() != "localid"]
    id_map = {rid: usr for rid, usr in src.execute("SELECT rowid, UsrName FROM Name2ID")}
    idx_talker = shared.index("TalkerId")
    idx_svr = shared.index("MsgSvrID")
    idx_ct = shared.index("CreateTime")
    idx_sk = shared.index("StrTalker") if "StrTalker" in shared else None

    remap = {}
    out = []
    dup = 0
    for row in src.execute(f"SELECT {', '.join(shared)} FROM MSG"):
        row = list(row)
        sk = (row[idx_svr], row[idx_ct], row[idx_sk] if idx_sk is not None else "")
        if row[idx_svr] and sk in seen:
            dup += 1
            continue
        tid = row[idx_talker]
        if tid not in remap:
            usr = id_map.get(tid)
            remap[tid] = ensure_talker(usr) if usr else tid
        row[idx_talker] = remap[tid]
        seen.add(sk)
        out.append(tuple(row))

    ph = ",".join("?" * len(shared))
    dst.executemany(f"INSERT OR IGNORE INTO MSG ({', '.join(shared)}) VALUES ({ph})", out)
    total += len(out)
    print(f"{name:22s} 新增 {len(out):5d} 去重 {dup:4d}")
    src.close()

dst.execute("COMMIT")
dst.close()

c = sqlite3.connect(TARGET)
n, mn, mx = c.execute("SELECT count(*), min(CreateTime), max(CreateTime) FROM MSG").fetchone()
print(f"总计 {n} 条: {datetime.datetime.fromtimestamp(mn)} -> {datetime.datetime.fromtimestamp(mx)}")
