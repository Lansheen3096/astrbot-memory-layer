#!/usr/bin/env python3
"""记忆条目的复核工具（列出 / 保留 / 标存疑 / 删除）。

删除【必须】走框架的 FaissVecDB.delete() —— 它会同时清 doc.db 的 documents、
documents_fts（sparse 检索）和 index.faiss（dense 检索）三处；手改 SQL 只会动第一处，
另外两处会带着旧内容继续被检索到。

用法（用同目录的 review.sh 调，别直接跑）：

    ./review.sh list                 # 列出待审（status != ok）
    ./review.sh list --all           # 全部
    ./review.sh list --sid 0063      # 看单条完整字段
    ./review.sh keep 0063 0064       # 过目通过 → status=ok
    ./review.sh suspect 0070         # 标来源存疑（recall 端不再注入）
    ./review.sh unsuspect 0070
    ./review.sh delete 0065           # 真删（三处同步，删前自动备份）
    ./review.sh batch < plan.json      # 一次做完一批（WebUI 的「应用」走这条）
    ./review.sh stats

写操作会自动先备份 doc.db / index.faiss，并且由 review.sh 负责停-跑-起容器。
batch 的 plan.json 形如 {"keep": ["0063"], "suspect": ["0070"], "unsuspect": [], "delete": ["0065"]}：
一个 sid 只认最靠前的那个操作（优先级 delete > keep > suspect > unsuspect），
没有任何有效 sid 时不动库、不备份、也不碰容器。
"""
import json
import os
import sqlite3
import sys
import time

KB_NAME = os.environ.get("KB_NAME", "长期记忆")
KB_ROOT = os.environ.get("KB_ROOT", "/AstrBot/data/knowledge_base")


def kb_dir() -> str:
    k = sqlite3.connect(os.path.join(KB_ROOT, "kb.db"))
    row = k.execute("select kb_id from knowledge_bases where kb_name=?", (KB_NAME,)).fetchone()
    k.close()
    if not row:
        sys.exit(f"找不到知识库「{KB_NAME}」")
    return os.path.join(KB_ROOT, row[0])


def load(kb: str):
    con = sqlite3.connect(os.path.join(kb, "doc.db"), timeout=30)
    rows = []
    for doc_id, text, md, created in con.execute(
        "select doc_id, text, metadata, created_at from documents"
    ):
        m = json.loads(md or "{}")
        rows.append({
            "doc_id": doc_id, "text": text or "", "meta": m,
            "sid": m.get("sid") or "????", "kind": m.get("kind") or "?",
            "date": m.get("event_date") or "-", "status": m.get("status"),
            "suspect": m.get("suspect"), "c_user": m.get("c_user"),
            "conf": m.get("confidence"), "created": created,
        })
    con.close()
    rows.sort(key=lambda r: r["sid"])
    return rows


def backup(kb: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(kb, f"backup-{ts}")
    n = 1
    while os.path.exists(dst):          # 同一秒内连续操作不要互相覆盖
        n += 1
        dst = os.path.join(kb, f"backup-{ts}-{n}")
    os.makedirs(dst, exist_ok=True)
    for name in ("doc.db", "index.faiss"):
        src = os.path.join(kb, name)
        if os.path.exists(src):
            with open(src, "rb") as f:
                data = f.read()
            with open(os.path.join(dst, name), "wb") as f:
                f.write(data)
    print(f"[备份] {dst}")
    return dst


def show_line(r: dict) -> str:
    flags = []
    if r["status"] == "new":
        flags.append("待审")
    elif r["status"] != "ok":
        flags.append("无状态")
    if r["suspect"] in (1, "1", True):
        flags.append("⚠存疑")
    cu = f" c={r['c_user']}" if isinstance(r["c_user"], (int, float)) else ""
    return (f"  {r['sid']}  {r['kind']:<10} {r['date']}  "
            f"{'/'.join(flags) or 'ok'}{cu}\n        {r['text']}")


def cmd_list(rows: list, args: list) -> None:
    all_ = "--all" in args
    if "--sid" in args:
        sid = args[args.index("--sid") + 1]
        for r in rows:
            if r["sid"] == sid:
                print(json.dumps({k: v for k, v in r.items() if k != "doc_id"},
                                 ensure_ascii=False, indent=2))
                print("doc_id:", r["doc_id"])
                return
        sys.exit(f"没有 sid={sid}")

    sel = [r for r in rows if all_ or r["status"] != "ok" or r["suspect"] in (1, "1", True)]
    if "--kind" in args:
        k = args[args.index("--kind") + 1]
        sel = [r for r in sel if r["kind"] == k]
    print(f"{'全部' if all_ else '待审/存疑'} {len(sel)} 条（库内共 {len(rows)} 条）")
    for r in sel:
        print(show_line(r))


def cmd_stats(rows: list, kb: str) -> None:
    from collections import Counter
    print("总条目:", len(rows))
    print("分层:", dict(Counter(r["kind"] for r in rows)))
    print("状态:", dict(Counter(r["status"] or "(无)" for r in rows)))
    print("存疑:", sum(1 for r in rows if r["suspect"] in (1, "1", True)))
    sids = [r["sid"] for r in rows]
    print("sid 范围:", min(sids), "~", max(sids), "唯一:", len(set(sids)) == len(sids))
    con = sqlite3.connect(os.path.join(kb, "doc.db"), timeout=30)
    n_doc = con.execute("select count(*) from documents").fetchone()[0]
    n_fts = con.execute("select count(*) from documents_fts").fetchone()[0]
    con.close()
    try:
        import faiss
        n_faiss = faiss.read_index(os.path.join(kb, "index.faiss")).ntotal
    except Exception as exc:  # noqa: BLE001
        n_faiss = f"读不到({type(exc).__name__})"
    print(f"三处一致性: documents={n_doc}  fts={n_fts}  faiss={n_faiss}",
          "✅" if n_doc == n_fts == n_faiss else "❌ 不一致")


def meta_update(kb: str, sids: list, fn) -> None:
    rows = {r["sid"]: r for r in load(kb)}
    targets = []
    for s in sids:
        if s not in rows:
            print(f"  ! 没有 sid={s}，跳过")
            continue
        targets.append(rows[s])
    if not targets:
        return
    backup(kb)
    con = sqlite3.connect(os.path.join(kb, "doc.db"), timeout=30)
    for r in targets:
        m = r["meta"]
        fn(m)
        con.execute("update documents set metadata=? where doc_id=?",
                    (json.dumps(m, ensure_ascii=False), r["doc_id"]))
        print(f"  {r['sid']} → {json.dumps({k: m.get(k) for k in ('status', 'suspect')}, ensure_ascii=False)}  {r['text'][:40]}")
    con.commit()
    con.close()


def delete_many(kb: str, targets: list) -> None:
    """真删：必须走框架的 FaissVecDB.delete()，同时清 documents / documents_fts / index.faiss 三处。"""
    import asyncio

    sys.path.insert(0, "/AstrBot")
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

    class _NoEmbed:  # delete 不需要真的算向量，只要 get_dim 够构造
        def get_dim(self):
            return int(os.environ.get("EMBED_DIM", "1024"))

    async def run() -> None:
        db = FaissVecDB(os.path.join(kb, "doc.db"), os.path.join(kb, "index.faiss"), _NoEmbed())
        await db.initialize()
        before = await db.count_documents()
        for r in targets:
            await db.delete(r["doc_id"])
            print(f"  已删 {r['sid']}  {r['text'][:40]}")
        after = await db.count_documents()
        await db.close()
        print(f"条目数 {before} → {after}（应减少 {len(targets)}）")

    asyncio.run(run())


BATCH_OPS = ("delete", "keep", "suspect", "unsuspect")   # 一个 sid 只认最靠前的那个操作


def cmd_batch(kb: str, plan: dict) -> None:
    """一次停-跑-起里做完一批操作：stdin 读 JSON 计划 {「keep」:[sid…], …}。

    由 review.sh batch 调用 —— WebUI 的「应用」走这条，避免每个操作各停起一次容器。
    """
    rows = {r["sid"]: r for r in load(kb)}
    todo = {k: [] for k in BATCH_OPS}
    seen, unknown = set(), []
    for op in BATCH_OPS:
        for raw in plan.get(op) or []:
            s = str(raw).strip()
            if not s or s in seen:
                continue
            if s not in rows:
                unknown.append(s)
                continue
            seen.add(s)
            todo[op].append(rows[s])

    for s in unknown:
        print(f"  ! 没有 sid={s}，跳过")
    n = sum(len(v) for v in todo.values())
    if not n:
        print("[batch] 没有可执行的操作 —— 不改库、不备份、也没动容器")
        return

    print(f"[batch] 本次 {n} 项：" + "、".join(f"{k} {len(v)}" for k, v in todo.items() if v))
    backup(kb)

    con = sqlite3.connect(os.path.join(kb, "doc.db"), timeout=30)
    for op in BATCH_OPS:
        for r in todo[op]:
            m = r["meta"]
            if op == "keep":
                m.update(status="ok")
                m.pop("suspect", None)
            elif op == "suspect":
                m["suspect"] = 1
            elif op == "unsuspect":
                m.pop("suspect", None)
            else:
                continue
            con.execute("update documents set metadata=? where doc_id=?",
                        (json.dumps(m, ensure_ascii=False), r["doc_id"]))
            print(f"  {op:<9} {r['sid']}  → {json.dumps({k: m.get(k) for k in ('status', 'suspect')}, ensure_ascii=False)}"
                  f"  {r['text'][:40]}")
    con.commit()
    con.close()

    if todo["delete"]:
        delete_many(kb, todo["delete"])


def cmd_delete(kb: str, sids: list, assume_yes: bool) -> None:
    rows = {r["sid"]: r for r in load(kb)}
    targets = [rows[s] for s in sids if s in rows]
    for s in sids:
        if s not in rows:
            print(f"  ! 没有 sid={s}，跳过")
    if not targets:
        return
    print("将删除：")
    for r in targets:
        print(show_line(r))
    if not assume_yes:
        if input("确认删除？(yes/N) ").strip().lower() not in ("y", "yes"):
            sys.exit("已取消")

    backup(kb)
    delete_many(kb, targets)


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    kb = kb_dir()
    if cmd == "list":
        cmd_list(load(kb), rest)
    elif cmd == "stats":
        cmd_stats(load(kb), kb)
    elif cmd == "keep":
        # 过目通过 = 转 ok + 清掉存疑标记（要保留存疑就先别 keep）
        meta_update(kb, rest, lambda m: (m.update(status="ok"), m.pop("suspect", None)))
    elif cmd == "suspect":
        meta_update(kb, rest, lambda m: m.update(suspect=1))
    elif cmd == "unsuspect":
        meta_update(kb, rest, lambda m: m.pop("suspect", None))
    elif cmd == "delete":
        cmd_delete(kb, [a for a in rest if not a.startswith("-")], "--yes" in rest)
    elif cmd == "batch":
        raw = sys.stdin.read().strip()
        if not raw:
            sys.exit("batch 需要从 stdin 读 JSON 计划，例如 {\"keep\": [\"0090\"], \"delete\": [\"0091\"]}")
        try:
            plan = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"计划不是合法 JSON：{exc}")
        if not isinstance(plan, dict):
            sys.exit("计划必须是对象：{\"keep\": [...], \"suspect\": [...], \"unsuspect\": [...], \"delete\": [...]}")
        cmd_batch(kb, plan)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
