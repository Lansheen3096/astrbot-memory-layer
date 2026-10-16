#!/usr/bin/env python3
"""记忆复核 WebUI —— 给 kb-review 那套 CLI 套一个能点的界面（零 token、无第三方依赖）。

分工：
  读  直接以只读方式打开宿主机上的 doc.db（WAL 库只读连接在宿主可用，实测 OK）
  写  不自己改库，一律 shell 到 review.sh batch —— 那里面才有停容器、备份、
      走框架 FaissVecDB.delete() 三处同步的完整流程

启动：
  python3 webui.py                  # 默认 127.0.0.1:8790
  HOST=0.0.0.0 PORT=8790 python3 webui.py    # 注意：绑 0.0.0.0 等于同网段任何人都能删记忆

env：KB_ROOT / KB_NAME / REVIEW_SH / HOST / PORT
"""
from __future__ import annotations

import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
KB_ROOT = os.environ.get("KB_ROOT", f"{HOME}/astrbot/data/knowledge_base")
KB_NAME = os.environ.get("KB_NAME", "长期记忆")
# 默认用跟本文件放在一起的那个 review.sh —— 这样整个 tools/ 目录可以整体搬走
REVIEW_SH = os.environ.get("REVIEW_SH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "review.sh"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8790"))
# 口令文件：存在且非空就要求带口令（绑定非 127.0.0.1 时必须留着）。每次请求现读 → 改口令无需重启。
TOKEN_FILE = os.environ.get("TOKEN_FILE", f"{HOME}/.config/kb-review-web/token")
COOKIE = "kbrw"
COOKIE_ATTRS = f"Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"

# 同一时刻只允许一个写操作（batch 会停容器，串行才安全）
WRITE_LOCK = threading.Lock()
FAILS: dict[str, tuple[int, float]] = {}
FAILS_LOCK = threading.Lock()

# 写入任务：异步跑，页面靠 /api/job 轮询进度。
# 为什么不直接在一个请求里跑完：写入要 30~60 秒（停容器→备份→改→起容器），
# 这期间用户一刷新/切走，回执就送不回来 —— 页面只能报错，而库里其实已经改完了（实踩过）。
JOB: dict = {"id": 0, "running": False, "ok": None, "log": [], "plan": {},
             "started": 0.0, "finished": 0.0}
JOB_LOCK = threading.Lock()
MAX_LOG_LINES = 400
JOB_TIMEOUT = 900


def job_snapshot() -> dict:
    with JOB_LOCK:
        j = dict(JOB)
        j["log"] = list(JOB["log"])
    j["elapsed"] = round((j["finished"] or time.time()) - j["started"], 1) if j["started"] else 0.0
    j.pop("plan", None)
    return j


def start_batch_job(plan: dict) -> dict:
    """起一个后台写入任务。返回 {'ok': True} 或 {'ok': False, 'log': 原因}。"""
    if not WRITE_LOCK.acquire(blocking=False):
        return {"ok": False, "log": "已有一个写入在进行，等它跑完再点。"}
    with JOB_LOCK:
        JOB.update(id=JOB["id"] + 1, running=True, ok=None, log=[], plan=plan,
                   started=time.time(), finished=0.0)
        job_id = JOB["id"]

    def add(line: str) -> None:
        line = line.rstrip("\r\n")
        if line.strip():
            with JOB_LOCK:
                JOB["log"].append(line)
                del JOB["log"][:-MAX_LOG_LINES]

    def worker() -> None:
        ok = False
        proc = None
        timer = None
        try:
            n = {k: len(v) for k, v in plan.items() if v}
            add(f"本次 {sum(n.values())} 项：" + "、".join(f"{k} {v}" for k, v in n.items()))
            proc = subprocess.Popen(
                [REVIEW_SH, "batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                env={**os.environ, "TZ": os.environ.get("TZ") or "Asia/Shanghai"})
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps(plan))
            proc.stdin.close()
            timer = threading.Timer(JOB_TIMEOUT, proc.kill)   # 卡死时的兜底
            timer.start()
            for line in proc.stdout:            # 逐行收：页面就能看到实时进度
                add(line)
            code = proc.wait()
            ok = code == 0
            add("✅ 应用完成" if ok else f"❌ 退出码 {code}")
        except Exception as exc:  # noqa: BLE001
            add(f"❌ 执行失败：{type(exc).__name__}: {exc}")
        finally:
            if timer:
                timer.cancel()
            if proc and proc.poll() is None:
                proc.kill()
            with JOB_LOCK:
                JOB.update(running=False, ok=ok, finished=time.time())
            WRITE_LOCK.release()

    threading.Thread(target=worker, name=f"kb-batch-{job_id}", daemon=True).start()
    return {"ok": True, "job_id": job_id}


def access_token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _snapshot(db_path: str) -> tuple[str, str]:
    """把 sqlite 库（连同 -wal/-shm）拷进临时目录再读。

    为什么不直接只读打开：WAL 模式下的只读连接需要能写 `-shm`，而知识库目录是 root 属主
    （实测报 `attempt to write a readonly database`；容器刚写过库之后必然触发）。
    拷一份我们自己的副本 → 连接可读写 → WAL 正常恢复。约 130 KB，代价可忽略。
    """
    tmp = tempfile.mkdtemp(prefix="kb-read-")
    base = os.path.basename(db_path)
    for suf in ("", "-wal", "-shm"):
        src = db_path + suf
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp, base + suf))
    return os.path.join(tmp, base), tmp


def query(db_path: str, sql: str, params: tuple = ()) -> list[tuple]:
    last = None
    for _ in range(3):                      # 容器恰好在写时可能拷到半截，重试即可
        snap, tmp = _snapshot(db_path)
        try:
            con = sqlite3.connect(snap, timeout=30)
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:        # noqa: PERF203
            last = exc
            time.sleep(0.5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    raise last if last else RuntimeError(f"读取 {db_path} 失败")


def kb_dir() -> str:
    rows = query(os.path.join(KB_ROOT, "kb.db"),
                 "select kb_id from knowledge_bases where kb_name=?", (KB_NAME,))
    if not rows:
        raise RuntimeError(f"找不到知识库「{KB_NAME}」")
    return os.path.join(KB_ROOT, rows[0][0])


def load_rows(kb: str) -> list[dict]:
    rows = []
    for doc_id, text, md, created in query(
            os.path.join(kb, "doc.db"),
            "select doc_id, text, metadata, created_at from documents order by doc_id"):
        try:
            m = json.loads(md or "{}")
        except Exception:  # noqa: BLE001
            m = {}
        rows.append({
            "sid": m.get("sid") or "????",
            "kind": m.get("kind") or "?",
            "date": m.get("event_date") or "-",
            "status": m.get("status") or "",
            "suspect": 1 if m.get("suspect") in (1, "1", True) else 0,
            "conf": m.get("confidence"),
            "text": (text or "").strip(),
        })
    rows.sort(key=lambda r: r["sid"])
    return rows


def counts(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "pending": sum(1 for r in rows if r["status"] != "ok" or r["suspect"]),
        "suspect": sum(1 for r in rows if r["suspect"]),
        "undeclared": sum(1 for r in rows if r["status"] != "ok"),
    }


def run_review(args: list[str], stdin: str | None = None, timeout: int = 900) -> tuple[int, str]:
    p = subprocess.run([REVIEW_SH, *args], input=stdin, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "TZ": os.environ.get("TZ") or "Asia/Shanghai"})
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if err:
        out = f"{out}\n[stderr]\n{err}".strip()
    return p.returncode, out


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>记忆复核</title>
<style>
 :root { --bg:#14161a; --card:#1d2027; --line:#2c313b; --fg:#e6e8ec; --dim:#98a0ad;
         --ok:#3fb950; --warn:#d29922; --bad:#f85149; --sel:#1f6feb; }
 body { margin:0; background:var(--bg); color:var(--fg);
        font:15px/1.5 -apple-system,"Noto Sans CJK SC",sans-serif;
        padding-bottom:calc(96px + env(safe-area-inset-bottom)); -webkit-text-size-adjust:100%; }
 * { box-sizing:border-box; }
 button { touch-action:manipulation; }
 header { position:sticky; top:0; z-index:5; background:#1a1d23; border-bottom:1px solid var(--line);
          padding:10px 14px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
 h1 { font-size:16px; margin:0 10px 0 0; font-weight:600; }
 .stat { color:var(--dim); font-size:13px; }
 .stat b { color:var(--fg); }
 button { background:var(--card); color:var(--fg); border:1px solid var(--line);
          border-radius:8px; padding:7px 12px; font-size:14px; cursor:pointer; }
 button:hover { border-color:#4b5563; }
 button.on { background:var(--sel); border-color:var(--sel); }
 button.ok.on { background:var(--ok); border-color:var(--ok); color:#08130a; }
 button.bad.on { background:var(--bad); border-color:var(--bad); }
 main { padding:12px 14px; display:grid; gap:10px; }
 .item { background:var(--card); border:1px solid var(--line); border-left-width:3px;
         border-radius:10px; padding:10px 12px; }
 .item.picked-keep { border-left-color:var(--ok); }
 .item.picked-del  { border-left-color:var(--bad); }
 .item.picked-sus  { border-left-color:var(--warn); }
 .meta { display:flex; gap:8px; align-items:center; flex-wrap:wrap; color:var(--dim);
         font-size:12px; margin-bottom:6px; }
 .sid { font-family:ui-monospace,monospace; color:var(--fg); font-weight:600; }
 .chip { border:1px solid var(--line); border-radius:999px; padding:1px 8px; }
 .chip.new { color:var(--warn); border-color:var(--warn); }
 .chip.sus { color:var(--bad); border-color:var(--bad); }
 .chip.ok { color:var(--ok); border-color:var(--ok); }
 .txt { white-space:pre-wrap; font-size:14px; }
 .acts { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; }
 .acts button { padding:5px 10px; font-size:13px; }
 .bar { position:fixed; left:0; right:0; bottom:0; background:#1a1d23; border-top:1px solid var(--line);
        padding:10px 14px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
 .bar .grow { flex:1; }
 .primary { background:var(--sel); border-color:var(--sel); }
 .danger { background:var(--bad); border-color:var(--bad); color:#fff; }
 .muted { color:var(--dim); font-size:13px; }
 pre { background:#0f1115; border:1px solid var(--line); border-radius:8px; padding:10px;
       max-height:40vh; overflow:auto; font-size:12.5px; white-space:pre-wrap; }
 .hide { display:none; }
 /* 移动端：字号、行距、点击区一起放大；按钮两列铺开，避免误触 */
 @media (max-width: 560px) {
   body { font-size:16px; }
   header { padding:8px 10px; gap:6px; }
   h1 { font-size:15px; margin:0 6px 0 0; }
   .stat { width:100%; font-size:12.5px; }
   header button { flex:1 1 auto; min-height:42px; }
   main { padding:10px; gap:8px; }
   .item { padding:10px 11px; }
   .meta { font-size:12px; gap:6px; }
   .txt { font-size:15.5px; line-height:1.65; }
   .acts { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
   .acts button { padding:11px 6px; font-size:14.5px; min-height:44px; }
   .bar { padding:8px 10px calc(8px + env(safe-area-inset-bottom)); gap:8px; }
   .bar button { min-height:42px; padding:10px 14px; }
   pre { font-size:13px; }
 }
</style></head><body>
<header>
  <h1>记忆复核</h1>
  <span class="stat" id="counts"></span>
  <span class="grow" style="flex:1"></span>
  <button id="f-pending" class="on" onclick="setFilter('pending')">待审/存疑</button>
  <button id="f-all" onclick="setFilter('all')">全部</button>
  <button onclick="refresh()">刷新</button>
  <button onclick="doStats()">一致性检查</button>
</header>
<main id="list"></main>
<div class="bar">
  <span class="muted" id="sel">未选择</span>
  <span class="grow"></span>
  <button onclick="clearSel()">清空</button>
  <button class="primary" id="applyBtn" onclick="apply()">应用</button>
</div>
<main><pre id="log" class="hide"></pre></main>
<script>
let ROWS = [], FILTER = 'pending';
const PICK = {};   // sid -> 'keep' | 'suspect' | 'unsuspect' | 'delete'

const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function setFilter(f) {
  FILTER = f;
  document.getElementById('f-pending').classList.toggle('on', f === 'pending');
  document.getElementById('f-all').classList.toggle('on', f === 'all');
  render();
}

function visible() {
  if (FILTER === 'all') return ROWS;
  return ROWS.filter(r => r.status !== 'ok' || r.suspect);
}

function render() {
  const rows = visible();
  document.getElementById('list').innerHTML = rows.length ? rows.map(r => {
    const chipStatus = r.status === 'ok' ? '<span class="chip ok">ok</span>'
      : r.status ? `<span class="chip new">${esc(r.status)}</span>` : '<span class="chip">无状态</span>';
    const chipSus = r.suspect ? '<span class="chip sus">⚠存疑</span>' : '';
    const pick = PICK[r.sid] || '';
    const cls = pick === 'keep' ? 'picked-keep' : pick === 'delete' ? 'picked-del'
      : pick ? 'picked-sus' : '';
    const b = (op, label, cls2) =>
      `<button class="${cls2} ${pick === op ? 'on' : ''}" onclick="pick('${r.sid}','${op}')">${label}</button>`;
    return `<div class="item ${cls}">
      <div class="meta">
        <span class="sid">${esc(r.sid)}</span>
        <span class="chip">${esc(r.kind)}</span>
        <span class="chip">${esc(r.date)}</span>
        ${chipStatus}${chipSus}
        ${r.conf != null ? `<span class="muted">conf ${r.conf}</span>` : ''}
      </div>
      <div class="txt">${esc(r.text)}</div>
      <div class="acts">
        ${b('keep','通过','ok')}${b('suspect','存疑')}${b('unsuspect','取消存疑')}${b('delete','删除','bad')}
      </div>
    </div>`;
  }).join('') : '<div class="muted">这个筛选下没有条目 🎉</div>';
  renderSel();
}

// 点同一个按钮 = 取消选择
function pick(sid, op) {
  if (PICK[sid] === op) delete PICK[sid];
  else if (op === 'delete' && !confirm(`确认要删除 ${sid} 吗？\\n\\n应用后不可撤销（服务端会先自动备份）。`)) return;
  else PICK[sid] = op;
  render();
}

function summary() {
  const c = {keep:0, suspect:0, unsuspect:0, delete:0};
  Object.values(PICK).forEach(v => c[v]++);
  return c;
}

function renderSel() {
  const c = summary();
  const n = Object.keys(PICK).length;
  const parts = [];
  if (c.keep) parts.push(`通过 ${c.keep}`);
  if (c.suspect) parts.push(`存疑 ${c.suspect}`);
  if (c.unsuspect) parts.push(`取消存疑 ${c.unsuspect}`);
  if (c.delete) parts.push(`删除 ${c.delete}`);
  document.getElementById('sel').innerHTML = n
    ? `已选 <b>${n}</b> 项：${parts.join(' / ')}` + (c.delete ? ' <span style="color:var(--bad)">（含删除）</span>' : '')
    : '未选择';
  document.getElementById('applyBtn').disabled = n === 0;
}

function clearSel() { for (const k in PICK) delete PICK[k]; render(); }

async function refresh() {
  const res = await fetch('/api/entries');
  if (res.status === 401) { location.reload(); return; }   // 口令被换过 → 回登录页
  const r = await res.json();
  ROWS = r.rows;
  const c = r.counts;
  document.getElementById('counts').innerHTML =
    `共 <b>${c.total}</b> 条 ｜ 待审 <b>${c.pending}</b> ｜ 存疑 <b>${c.suspect}</b>`;
  render();
}

async function doStats() {
  const el = document.getElementById('log');
  el.classList.remove('hide'); el.textContent = '跑一致性检查（只读，约 2 秒）…';
  const r = await (await fetch('/api/stats')).json();
  el.textContent = r.text || r.error || '(无输出)';
}

async function apply() {
  const c = summary();
  if (c.delete && !confirm(`本次包含 ${c.delete} 条删除，确认应用？`)) return;
  // 接口要的是【每类操作的 sid 列表】，不是计数
  const plan = {keep: [], suspect: [], unsuspect: [], delete: []};
  for (const sid in PICK) plan[PICK[sid]].push(sid);
  const el = document.getElementById('log');
  el.classList.remove('hide');
  el.textContent = '提交中…';
  document.getElementById('applyBtn').disabled = true;
  try {
    const res = await fetch('/api/apply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(plan)
    });
    if (res.status === 401) { location.reload(); return; }
    const j = await res.json();
    if (!j.ok) { el.textContent = j.log || j.error || '提交失败'; return; }
    clearSel();
    pollJob();                       // 已经在后台跑了，去看进度
  } catch (e) {
    el.textContent = '提交失败：' + e;
  } finally {
    renderSel();
  }
}

// 轮询写入进度。刷新页面也不影响：重新打开会自动接上。
async function pollJob() {
  const el = document.getElementById('log');
  let j;
  try {
    const res = await fetch('/api/job');
    if (res.status === 401) { location.reload(); return; }
    j = await res.json();
  } catch (e) { return; }
  el.classList.remove('hide');
  el.textContent = (j.log || []).join('\\n') || '…';
  el.scrollTop = el.scrollHeight;
  if (j.running) {
    document.getElementById('applyBtn').disabled = true;
    document.getElementById('sel').innerHTML = '写入进行中… ' + Math.round(j.elapsed) + 's';
    setTimeout(pollJob, 1500);
  } else if (j.finished) {
    document.getElementById('applyBtn').disabled = false;
    await refresh();                 // 写完刷一次列表
    renderSel();
  }
}

refresh();
fetch('/api/job').then(r => r.json()).then(j => { if (j && j.running) pollJob(); }).catch(() => {});
</script></body></html>
"""


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>记忆复核 · 口令</title>
<style>
 body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
        background:#14161a; color:#e6e8ec; font:16px/1.6 -apple-system,"Noto Sans CJK SC",sans-serif;
        padding:20px; -webkit-text-size-adjust:100%; }
 .box { width:100%; max-width:360px; background:#1d2027; border:1px solid #2c313b;
        border-radius:12px; padding:20px; }
 h1 { font-size:17px; margin:0 0 6px; }
 p { color:#98a0ad; font-size:13.5px; margin:0 0 14px; }
 input { width:100%; font-size:16px; padding:12px; border-radius:8px; border:1px solid #2c313b;
         background:#0f1115; color:#e6e8ec; }
 button { width:100%; margin-top:12px; min-height:46px; font-size:16px; border-radius:8px;
          border:1px solid #1f6feb; background:#1f6feb; color:#fff; touch-action:manipulation; }
 .err { color:#f85149; font-size:14px; min-height:20px; margin-top:10px; }
</style></head><body>
<div class="box">
  <h1>记忆复核</h1>
  <p>这台页面上是记忆正文，局域网可见 —— 先输口令。</p>
  <input id="t" type="password" autocomplete="current-password" placeholder="口令" autofocus>
  <button onclick="go()">进入</button>
  <div class="err" id="e"></div>
</div>
<script>
async function go() {
  const t = document.getElementById('t').value.trim();
  if (!t) return;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({token: t})});
  if (r.ok) location.reload();
  else document.getElementById('e').textContent = r.status === 429 ? '尝试太多，等几分钟再来。' : '口令不对';
}
document.getElementById('t').addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "kb-review-web/1.0"

    def log_message(self, fmt, *args):  # 保持 journal 安静，只记错误级别
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: list | None = None) -> None:
        """写响应。客户端先走（刷新 / 关页 / 连接被回收）时只记一行，不喷 traceback。"""
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            print(f"[kb-review-web] 客户端提前断开（{type(exc).__name__}），本次响应丢弃", flush=True)

    def _json(self, obj, code: int = 200, extra: list | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", extra)

    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def _auth_ok(self) -> bool:
        tok = access_token()
        return True if not tok else hmac.compare_digest(self._cookie_token(), tok)

    def _login_failed(self) -> bool:
        """同一 IP 连续失败太多 → 冷却，避免被慢慢试口令。"""
        ip = self.client_address[0]
        now = time.time()
        with FAILS_LOCK:
            n, until = FAILS.get(ip, (0, 0.0))
            if until > now:
                return True
            n += 1
            FAILS[ip] = (n, now + 300 if n >= 10 else 0.0)
            return n >= 10

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            tok = access_token()
            if tok and not self._auth_ok():
                # 支持 /?token=xxxx 一次性登录（手机上点链接就进来），随后靠 cookie
                if path == "/" and qs.get("token") and hmac.compare_digest(qs["token"][0], tok):
                    self._send(302, b"", "text/html; charset=utf-8",
                               [("Set-Cookie", f"{COOKIE}={tok}; {COOKIE_ATTRS}"), ("Location", "/")])
                elif path == "/":
                    self._send(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")
                else:
                    self._json({"error": "unauthorized"}, 401)
                return
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/entries":
                rows = load_rows(kb_dir())
                self._json({"rows": rows, "counts": counts(rows)})
            elif path == "/api/stats":
                _, out = run_review(["stats"], timeout=120)
                self._json({"text": out})
            elif path == "/api/job":
                self._json(job_snapshot())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        """任何异常都必须落成一个 JSON 响应。

        否则 socketserver 只会打 traceback 然后掐断连接，浏览器那边看到的是
        「NetworkError when attempting to fetch resource」——完全看不出真正的原因。
        """
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"请求体解析失败：{exc}"}, 400)
            return
        if not isinstance(body, dict):
            self._json({"error": "请求体必须是 JSON 对象"}, 400)
            return

        try:
            if path == "/api/login":
                self._post_login(body)
            elif path == "/api/apply":
                self._post_apply(body)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"服务端异常：{type(exc).__name__}: {exc}"}, 500)

    def _post_login(self, body: dict) -> None:
        tok = access_token()
        given = str(body.get("token") or "")
        if not tok:
            self._json({"ok": True, "note": "未设置口令"})
        elif hmac.compare_digest(given, tok):
            with FAILS_LOCK:
                FAILS.pop(self.client_address[0], None)
            self._json({"ok": True}, extra=[("Set-Cookie", f"{COOKIE}={tok}; {COOKIE_ATTRS}")])
        else:
            cooldown = self._login_failed()
            time.sleep(0.6)
            self._json({"ok": False, "cooldown": cooldown}, 429 if cooldown else 403)

    def _post_apply(self, body: dict) -> None:
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401)
            return
        plan = {}
        for k in ("keep", "suspect", "unsuspect", "delete"):
            v = body.get(k) or []
            if not isinstance(v, (list, tuple)):
                self._json({"ok": False, "log": f"计划格式不对：{k} 需要 sid 列表，收到 {type(v).__name__}。"
                                               f"刷新页面再试。"}, 400)
                return
            plan[k] = [str(s) for s in v]
        if sum(len(v) for v in plan.values()) == 0:
            self._json({"ok": True, "log": "没有要应用的操作。"})
            return
        # 不在这个请求里跑完：起后台任务，页面轮询 /api/job 看进度（见 start_batch_job 注释）
        self._json(start_batch_job(plan))


def main() -> None:
    kb = kb_dir()   # 启动就验证知识库找得到，早点报错
    rows = load_rows(kb)
    tok = access_token()
    guard = "口令已启用" if tok else "⚠️ 无口令（任何能连到的人都可读可删）"
    print(f"[kb-review-web] http://{HOST}:{PORT}  知识库「{KB_NAME}」{len(rows)} 条，"
          f"待审 {counts(rows)['pending']}；{guard}")
    if HOST not in ("127.0.0.1", "localhost") and not tok:
        print("[kb-review-web] ⚠️ 绑定了非本机地址却没有口令文件，建议立刻建一个：" f"{TOKEN_FILE}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
