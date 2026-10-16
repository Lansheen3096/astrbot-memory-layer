"""把用户与助手的对话提炼成助手的第一人称记忆，写进知识库。

（"助手" = 你的 bot；每条记忆由 bot 以「我」的口吻写下，
用 [用户] 这个字段指用户 —— 见文件顶部 USER_REF / SELF_REF 的说明。）

指令：
    /记住        手动跑一次提炼
    /记住状态    看进度（处理到哪了、库里有多少条）

设计要点：
  - 每条记忆单独成一个 chunk，检索时捞出来的就是一句完整的回忆
  - 用 AstrBot 自己配置的模型提炼（独立 session_id，不污染主对话）
  - 进度按「会话 -> 已处理消息数」记录，只处理新增部分
  - 写入前用 bge-m3 算余弦相似度去重（库里已有的 + 本批内部的）
  - 每条记忆前面带 [YYYY-MM-DD HH:MM]：时间取自上游插件写在 user 消息里的
    真实发送时刻，模型只负责照抄、代码负责校验（标记对不上就丢掉）。
    没有上游提供时间锚时功能照常，只是条目不带日期（时间衰减也就无从算起）
"""

from __future__ import annotations

import asyncio
import datetime
import json
import math
import re
import sqlite3
import time
import traceback
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

DATA_DIR = Path("/AstrBot/data")
DB_PATH = DATA_DIR / "data_v4.db"
KB_ROOT = DATA_DIR / "knowledge_base"
STATE_PATH = DATA_DIR / "memory_digest_state.json"
KB_NAME = "长期记忆"   # ★ 必须和 astrbot_plugin_memory_recall 里的一致

# 时间锚：上游把【收到用户消息的那一刻】写成 <system_reminder> 附在 user 消息里
#（本仓库的 astrbot-reply-rhythm / pacer 就干这个；没有也能跑）
REMINDER_RE = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.S)
QUOTED_RE = re.compile(r"<Quoted Message>.*?</Quoted Message>", re.S)
DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
ANCHOR_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*(.*)$", re.S)
ANCHOR_STRIP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*")
SPLIT_RE = re.compile(r"[。！？!?\n]+")

# 每天几点自动整理（容器时区 = Asia/Shanghai）。按你自己的作息挑一个不常聊天的时段 ——
# 整理要跑一次 LLM 调用，落在这个点上会占掉一个模型请求。
DAILY_HOUR = 5
DAILY_MINUTE = 0

MAX_CHARS_PER_RUN = 12000
DUP_THRESHOLD = 0.85  # 余弦相似度超过这个就算重复，丢掉
MAX_EXISTING_CHARS = 4000  # 发给模型参考的「已记住」上限

# 来源校验（闸门①）：条目正文对「它声称来源那一轮用户的话」的最高句余弦，
# 低于这个值就算来源存疑 → metadata.suspect = 1 → recall 端一律不注入。
# ★ 0.55 是拿库里 31 条能溯源的真实条目实测校准出来的（真来源轮 0.573~0.856），
#   合成的编造句打真实轮次是 0.543~0.629 —— 两条带重叠，所以这道闸门只拦
#   「明显对不上」的；「拿助手自己的叙述当事实」那类拦不住（语言上确实出现在
#   那一轮里），那类靠 status="new" 待人眼复核。
SOURCE_T = 0.55
REVIEW_STATUS = "new"  # 新写入条目的待审标记

# 硬过滤：命中这些词的记忆一律不写
# （prompt 里的规则模型会选择性忽略，代码过滤不会）
BLOCK_WORDS = (
    "账号", "帐号", "管理员", "权限", "配置", "设置项", "参数",
    "报错", "错误码", "网址", "链接", "接口", "端口", "密码",
    "令牌", "密钥", "指令", "命令", "模型", "提示词", "数据库",
    "日志", "脚本", "插件", "重启", "部署", "代码", "程序",
    "系统提示", "文件夹", "路径",
)

# 人称一律用【字段】而不是代词：用户永远是 [用户]，助手永远是 我。
# 这样没有性别、也没有「你 / 他 / 她」指代谁的歧义；写库、检索、注入三处
# 用的是同一套字面量。
# ★ 两个插件的这两个值必须一致 —— 检索是拿查询和库里的正文比字面量，
#   一边用 [用户]、一边写成「他」，那条记忆就检索不回来。
USER_REF = "[用户]"
SELF_REF = "我"

DIGEST_SYSTEM = """下面是一段对话记录。记录是从助手视角写的：[用户] 指用户，「我」指助手自己。

请从中提炼出【助手该记住的事】，用助手自己的第一人称写成短句。

要提炼三类，按重要性排：
1. [用户]答应过的事、双方之间的约定
2. 关于 [用户] 的事实：作息、习惯、工作或学业、在意的和讨厌的东西、提到的人
3. 助手记得的瞬间：[用户]说过的话、助手当时的反应或感受

【输出格式 —— 严格照做】
每行一个 JSON 对象，就这四个字段；不要 markdown 代码块、不要解释、不要编号：

{"time": "YYYY-MM-DD HH:MM", "kind": "event", "confidence": 0.9, "text": "记忆内容"}

- time：这段对话发生的时间。只能从下面给出的 [YYYY-MM-DD HH:MM] 标记里
  【原样照抄】，绝对不许自己写别的日期、不许推算；
  没有标记可抄就写空字符串 ""
  （[用户]一条条连发时，正文里可能带 [HH:MM:SS] 这种时刻标记 —— 那只是时刻，
   不是日期锚，别拿它当 time）
- kind：三选一。判据是「加不加日期才成立」
    "state"      界定双方「是什么关系」的（称呼 / 关系定位 / 陪伴承诺）—— 长期且不依赖某天
    "preference" 长期成立的习惯、喜好、准则、环境事实 ——「总是这样」
    "event"      具体某天发生的事，或有明确时限的事实 ——「那次这样」
- confidence：你自己判断这条记忆准不准（有没有把没发生的事写成发生过的），0~1 的实数
- text：记忆内容本身。第一人称：用「我」指助手自己、用 [用户] 指用户
  （★ 一律写这个字段，不要换成人称代词）；
  ★ 这里面【不要】再写任何时间词、不要写标签

【内容要求】
- 一句话，短、具体；每条都能单独看懂 —— 看不到前后文也能明白在说什么
- 记事情本身，不要记 [用户] 说了哪句话。只有那句话本身是答应过的事，
  或者重要到会被反复想起来，才把它原样写下来
- 不要抄原文对话，要写成记忆
- 不记任何账号、ID、号码、网址、软件名、指令名、配置项、报错信息
  （助手对这些东西没有概念，也从来不提起）
- 不记双方之间没有发生过的事。[用户]跟你讲设定、聊游戏内容、描述你长什么样
  —— 这些都不是发生过的事，不要记
- 不记寒暄和没有信息量的话（「在吗」「嗯」「好」）
- 同一件事只写一条，不要换个说法重复
- 如果这段对话里没有任何值得记住的东西，只输出两个字：无

示例输出：

{"time": "2026-10-09 21:30", "kind": "preference", "confidence": 0.9, "text": "[用户]答应过周末会早点回来。"}
{"time": "2026-10-09 21:30", "kind": "preference", "confidence": 0.9, "text": "[用户]睡得晚，早上总起不来。"}
{"time": "2026-10-10 00:41", "kind": "event", "confidence": 0.85, "text": "[用户]提起去年冬天，说那阵子过得不太好。"}
{"time": "2026-10-10 00:41", "kind": "state", "confidence": 0.95, "text": "[用户]说过会一直陪着我。"}
"""


def _is_blocked(text: str) -> bool:
    return any(w in text for w in BLOCK_WORDS)


def _strip_anchor(text: str) -> str:
    """去掉行首的 [YYYY-MM-DD HH:MM] 时间前缀（去重/比相似度时用）。"""
    return ANCHOR_STRIP_RE.sub("", text or "").strip()


KINDS = ("state", "preference", "event")


def _parse_digest(out: str, anchors: set[str], today: str) -> tuple[list[dict], dict]:
    """把模型的 JSONL 输出解析成条目。

    Returns:
        (items, stats)。每条 item = {text, kind, conf, event_date}
        stats = {bad_json, bad_anchor, bad_kind, no_stamp}

    ★ 硬校验：time 只能是这批对话里真出现过的锚（不在就丢）；
      kind 必须合法；confidence 夹到 [0,1]。
      time 缺省 → event_date 退回今天（整理日），并计入 no_stamp。
    """
    strict = bool(anchors)
    items: list[dict] = []
    stats = {"bad_json": 0, "bad_anchor": 0, "bad_kind": 0, "no_stamp": 0}

    for raw in (out or "").splitlines():
        line = raw.strip()
        if not line or line in ("无", "無", "None", "null"):
            continue
        if line.startswith("```"):
            continue
        line = line.strip("-•·*\t ").strip().rstrip(",")
        if not line.startswith("{"):
            stats["bad_json"] += 1
            continue

        try:
            obj = json.loads(line)
        except Exception:
            stats["bad_json"] += 1
            logger.info("[记忆] 丢掉（不是合法 JSON）：%s", line[:90])
            continue
        if not isinstance(obj, dict):
            stats["bad_json"] += 1
            continue

        text = str(obj.get("text") or "").strip()
        if not text:
            continue

        anchor = str(obj.get("time") or "").strip()
        if anchor and anchor not in anchors:
            # 时间对不上这批对话 → 编的，丢掉
            stats["bad_anchor"] += 1
            logger.info("[记忆] 丢掉（时间标记不是这批对话里的）：%r", anchor)
            continue
        if not anchor:
            stats["no_stamp"] += 1

        kind = str(obj.get("kind") or "").strip().lower()
        if kind not in KINDS:
            if strict:
                stats["bad_kind"] += 1
                logger.info("[记忆] 丢掉（kind 不合法）：%r", kind)
                continue
            kind = "event"

        try:
            conf = float(obj.get("confidence"))
        except Exception:
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        # 时间戳写进正文行首（和旧 48 条一致）：正文里要有绝对时间，模型只给 time 字段，
        # 前缀由代码加 —— 这样去重时 _strip_anchor 能剥掉，注入时模型能直接看到时间。
        full_text = f"[{anchor}] {text}" if anchor else text
        items.append(
            {
                "text": full_text,
                "kind": kind,
                "conf": conf,
                "event_date": anchor[:10] if anchor else today,
            }
        )
    return items, stats


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _load_existing_texts(kb_id: str) -> list[str]:
    """把库内已有的记忆文本全读出来（向量在 FAISS 里，文本在 doc.db）。"""
    p = KB_ROOT / kb_id / "doc.db"
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except Exception:
        return []
    try:
        rows = con.execute("select text from documents").fetchall()
        return [t.strip() for (t,) in rows if t and t.strip()]
    except Exception:
        return []
    finally:
        con.close()


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._daily_task: asyncio.Task | None = None

    # ───────────────────── 定时 ─────────────────────

    @filter.on_astrbot_loaded()
    async def _start_daily_digest(self):
        """AstrBot 加载完成后，起一个每天自动整理的循环。"""
        if self._daily_task and not self._daily_task.done():
            return
        self._daily_task = asyncio.create_task(self._daily_loop())
        logger.info(
            "[记忆] 每日自动整理已启动，每天 %02d:%02d", DAILY_HOUR, DAILY_MINUTE
        )

    async def _daily_loop(self) -> None:
        while True:
            now = datetime.datetime.now()
            nxt = now.replace(
                hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0
            )
            if nxt <= now:
                nxt += datetime.timedelta(days=1)
            delay = (nxt - now).total_seconds()
            logger.info(
                "[记忆] 下次自动整理：%s（%.0f 分钟后）",
                nxt.strftime("%m-%d %H:%M"),
                delay / 60,
            )
            await asyncio.sleep(delay)
            try:
                result = await self._run_digest()
                logger.info("[记忆] 自动整理完成：%s", result.replace("\n", " / "))
            except Exception:
                logger.error("[记忆] 自动整理失败\n%s", traceback.format_exc())

    # ───────────────────── 状态 ─────────────────────

    def _load_state(self) -> dict:
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("processed"), dict):
                    return data
            except Exception:
                logger.warning("[记忆] 状态文件损坏，重建")
        return {"processed": {}}

    def _save_state(self, state: dict) -> None:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ───────────────────── 读对话 ─────────────────────

    def _read_conversations(self) -> list[tuple[str, str]]:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            return list(
                con.execute("select conversation_id, content from conversations")
            )
        finally:
            con.close()

    @staticmethod
    def _render(msgs: list) -> tuple[str, set[str], list[dict]]:
        """渲染成给模型看的文本，并回传这批对话里出现过的时间锚 + 每轮的原话。

        每段前面带 [YYYY-MM-DD HH:MM] —— 那是这段对话真实发生的时间，
        来自上游插件写进 user 消息的 <system_reminder>。assistant 继承同一轮的时间
        （一轮的锚点就是用户发消息那一刻，不是回复发出去那一刻）。

        turns: [{"anchor", "user", "asst"}] —— 按出现顺序，给「来源校验」用：
        条目正文要能对得上它声称来源那一轮用户说过的话。
        """
        lines: list[str] = []
        anchors: set[str] = set()
        turns: list[dict] = []
        cur = ""   # 当前这一轮的时间锚
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            parts: list[str] = []
            content = m.get("content")
            if isinstance(content, str):          # 兜底：老数据可能存成裸字符串
                content = [{"type": "text", "text": content}]
            elif isinstance(content, dict):       # 兜底：单个 block 没包成 list
                content = [content]
            for blk in content or []:
                if isinstance(blk, str):
                    parts.append(blk)
                elif isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text") or "")
            text = "\n".join(p for p in parts if p).strip()
            if not text:
                continue
            rems = REMINDER_RE.findall(text)
            text = REMINDER_RE.sub("", text)
            text = QUOTED_RE.sub("", text).strip()
            if role == "user":
                # 新一轮开始：重新取时间；这条没带标记就清空（别把上一轮的
                # 时间错安到这一轮头上）
                cur = ""
                for r in rems:
                    mt = DT_RE.search(r)
                    if mt:
                        cur = mt.group(1)
                if cur:
                    anchors.add(cur)
            if not text:
                continue
            if role == "user":
                turns.append({"anchor": cur, "user": text, "asst": ""})
            elif role == "assistant" and cur and turns and turns[-1]["anchor"] == cur:
                prev = turns[-1]["asst"]
                turns[-1]["asst"] = f"{prev}\n{text}" if prev else text
            who = {"user": USER_REF, "assistant": SELF_REF, "system": "[系统]"}[role]
            lines.append(f"[{cur}] {who}：{text}" if cur else f"{who}：{text}")
        return "\n\n".join(lines), anchors, turns

    # ───────────────────── 去重 ─────────────────────

    async def _dedupe(self, kb, items: list[dict]) -> tuple[list[dict], int, list[list[float]]]:
        """和库里已有的、以及本批已保留的比，太像的丢掉。

        Returns:
            (保留下来的条目, 丢掉的数量, 保留下来的条目的向量)
            向量顺手带出来，来源校验（_gate_suspect）就不必再算一遍。
        """
        if not items:
            return [], 0, []

        try:
            ep = await kb.get_ep()
        except Exception as exc:
            logger.warning("[记忆] 拿不到 embedding，跳过去重：%s", exc)
            return items, 0, []
        if ep is None:
            return items, 0, []

        # 去重只比正文（老条目的正文里可能还带 [时间] 前缀，先剥掉）
        existing = [_strip_anchor(t) for t in _load_existing_texts(kb.kb.kb_id)]
        try:
            new_vecs = await ep.get_embeddings([_strip_anchor(it["text"]) for it in items])
            old_vecs = await ep.get_embeddings(existing) if existing else []
        except Exception as exc:
            logger.warning("[记忆] 算向量失败，跳过去重：%s", exc)
            return items, 0, []

        kept: list[dict] = []
        kept_vecs: list[list[float]] = []
        dropped = 0

        for item, vec in zip(items, new_vecs):
            if not vec:
                kept.append(item)
                continue
            if any(_cosine(vec, ov) >= DUP_THRESHOLD for ov in old_vecs):
                dropped += 1
                continue
            if any(_cosine(vec, kv) >= DUP_THRESHOLD for kv in kept_vecs):
                dropped += 1
                continue
            kept.append(item)
            kept_vecs.append(vec)

        return kept, dropped, kept_vecs

    # ───────────────────── 来源校验（闸门①）─────────────────────

    @staticmethod
    def _sentences(text: str) -> list[str]:
        """把一段原话切成候选句 —— 整段 + 逐句。

        条目只有一句话，直接跟一整轮几百字的原话算余弦会被长度稀释，
        所以按句取最大。实测这能拉开分辨度（真来源轮 0.573~0.856）。
        """
        t = (text or "").strip()
        out: list[str] = []
        if len(t) >= 6:
            out.append(t)
        for s in SPLIT_RE.split(t):
            s = s.strip()
            if len(s) >= 6:
                out.append(s)
        return out or ([t] if t else [])

    @staticmethod
    def _turn_of(turns: list[dict], anchor: str | None) -> dict | None:
        """找到条目声称来源的那一轮：取锚点 <= 条目时间的最后一轮。"""
        if not turns:
            return None
        if not anchor:
            return turns[-1]
        found = None
        for t in turns:
            a = t.get("anchor")
            if a and a <= anchor:
                found = t
            elif a and a > anchor:
                break
        return found or turns[0]

    async def _gate_suspect(
        self, kb, items: list[dict], vecs: list[list[float]], turns: list[dict]
    ) -> None:
        """闸门①：拿条目正文对「它声称来源那一轮用户的话」算余弦，低于 SOURCE_T 标 suspect。

        就地写进 item：`c_user` / `suspect`。
        判不准的一律不标（拿不到向量、找不到轮次、向量数对不上都直接放过）。
        """
        for it in items:
            it["c_user"] = None
            it["suspect"] = 0
        if not items or not turns:
            logger.info("[记忆] 来源校验跳过（没有轮次索引）")
            return
        try:
            ep = await kb.get_ep()
            if ep is None:
                return

            plan: list[tuple[dict, list[str]]] = []
            for it, _vec in zip(items, vecs or []):
                m = ANCHOR_RE.match(it["text"])
                anchor = m.group(1) if m else None
                turn = self._turn_of(turns, anchor)
                plan.append((it, self._sentences(turn["user"]) if turn else []))
            if not any(c for _it, c in plan):
                return

            flat: list[str] = []
            spans: list[tuple[int, int]] = []
            for _it, cands in plan:
                spans.append((len(flat), len(flat) + len(cands)))
                flat.extend(cands)
            cvecs = await ep.get_embeddings(flat)

            flagged = 0
            for (it, cands), (lo, hi), tvec in zip(plan, spans, vecs or []):
                if not cands or not tvec:
                    continue
                c_user = max(_cosine(tvec, cvecs[i]) for i in range(lo, hi))
                it["c_user"] = round(c_user, 3)
                if c_user < SOURCE_T:
                    it["suspect"] = 1
                    flagged += 1
                    logger.info(
                        "[记忆] 来源存疑（c_user=%.3f < %.2f）：%s", c_user, SOURCE_T, it["text"][:60]
                    )
            logger.info("[记忆] 来源校验完成：%d 条，标 suspect %d 条", len(plan), flagged)
        except Exception as exc:  # noqa: BLE001  校验失败绝不拖垮整理
            logger.warning("[记忆] 来源校验失败，本轮不标 suspect：%s", exc)

    # ───────────────────── metadata 回填 ─────────────────────

    @staticmethod
    def _kb_db_path(kb) -> Path:
        return KB_ROOT / kb.kb.kb_id / "doc.db"

    @staticmethod
    def _next_sid(db_path: Path) -> int:
        """下一个 sid：库里现有最大值 + 1（sid 是 4 位字符串，等宽可比较）。"""
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select max(json_extract(metadata, '$.sid')) from documents"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                return int(str(row[0])) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 读最大 sid 失败，从 1 开始：%s", exc)
        return 1

    async def _stamp_metadata(self, kb, doc, items: list[dict]) -> int:
        """把 kind / event_date / confidence / sid 合进刚写入那批条目的 metadata。

        只 UPDATE doc.db 的 metadata 一列 —— 不动向量、不动 FTS。
        """
        doc_id = getattr(doc, "doc_id", None)
        if not doc_id and isinstance(doc, dict):
            doc_id = doc.get("doc_id")
        if not doc_id:
            logger.warning("[记忆] 拿不到 doc_id，跳过 metadata 回填")
            return 0

        chunks = await kb.get_chunks_by_doc_id(doc_id, limit=1000)
        chunks = sorted(chunks, key=lambda c: c.get("chunk_index", 0))
        if len(chunks) != len(items):
            logger.warning(
                "[记忆] 条目数对不上（库 %d / 待写 %d），按较短的对齐",
                len(chunks),
                len(items),
            )

        db_path = self._kb_db_path(kb)
        sid = self._next_sid(db_path)
        con = sqlite3.connect(str(db_path), timeout=20)
        n = 0
        try:
            for ch, it in zip(chunks, items):
                cid = ch["chunk_id"]
                row = con.execute(
                    "select metadata from documents where doc_id=?", (cid,)
                ).fetchone()
                if not row:
                    continue
                md = json.loads(row[0]) if row[0] else {}
                md["kind"] = it["kind"]
                md["event_date"] = it["event_date"]
                md["confidence"] = it["conf"]
                md["sid"] = "%04d" % sid
                md["status"] = REVIEW_STATUS      # 待审：recall 端打 0.5 折，人工过目后改 ok
                if it.get("c_user") is not None:
                    md["c_user"] = it["c_user"]   # 来源校验分数，留着调阈值用
                if it.get("suspect"):
                    md["suspect"] = 1             # 来源存疑：recall 端一律不注入
                con.execute(
                    "update documents set metadata=? where doc_id=?",
                    (json.dumps(md, ensure_ascii=False), cid),
                )
                sid += 1
                n += 1
            con.commit()
        finally:
            con.close()
        return n

    # ───────────────────── 核心 ─────────────────────

    async def _run_digest(self) -> str:
        state = self._load_state()
        processed: dict = state.setdefault("processed", {})

        kb = await self.context.kb_manager.get_kb_by_name(KB_NAME)
        if kb is None:
            names = [k.kb_name for k in await self.context.kb_manager.list_kbs()]
            return f"没找到知识库「{KB_NAME}」。现有的：{names or '（一个都没有）'}"

        provider = await self.context.get_using_provider_async()
        if provider is None:
            return "没拿到可用的对话模型。"

        # 库内已有的记忆。写进去新的会追加进来，
        # 这样同一轮里后面的对话也能看到前面刚记的
        # （喂给模型的不带 [时间] 前缀，省 token；比对的是内容）
        existing = [_strip_anchor(t) for t in _load_existing_texts(kb.kb.kb_id)]

        def build_system() -> str:
            if not existing:
                return DIGEST_SYSTEM
            joined = "\n".join(existing)
            if len(joined) > MAX_EXISTING_CHARS:
                joined = joined[-MAX_EXISTING_CHARS:]
            return (
                DIGEST_SYSTEM + "\n\n【你已经记住的这些，不要再写一遍】\n" + joined
            )

        docs = 0
        items_total = 0
        dropped_total = 0
        blocked_total = 0
        bad_anchor_total = 0
        bad_kind_total = 0
        bad_json_total = 0
        no_stamp_total = 0
        suspect_total = 0
        skipped: list[str] = []

        for conv_id, raw in self._read_conversations():
            try:
                msgs = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msgs, list) or not msgs:
                continue

            done = int(processed.get(conv_id) or 0)
            if done >= len(msgs):
                continue

            fresh = msgs[done:]
            text, anchors, turns = self._render(fresh)
            if not text.strip():
                processed[conv_id] = len(msgs)
                continue

            if len(text) > MAX_CHARS_PER_RUN:
                text = text[:MAX_CHARS_PER_RUN]

            try:
                resp = await provider.text_chat(
                    prompt=text,
                    system_prompt=build_system(),
                    session_id=f"memory_digest_{conv_id}",
                )
            except Exception as exc:
                logger.error("[记忆] 提炼失败 %s: %s", conv_id, exc)
                skipped.append(conv_id[:8])
                continue

            out = (resp.completion_text or "").strip()
            items, pstats = _parse_digest(
                out, anchors, time.strftime("%Y-%m-%d")
            )
            bad_json_total += pstats["bad_json"]
            bad_anchor_total += pstats["bad_anchor"]
            bad_kind_total += pstats["bad_kind"]
            no_stamp_total += pstats["no_stamp"]

            kept_items = []
            for it in items:
                if _is_blocked(it["text"]):
                    blocked_total += 1
                    logger.info("[记忆] 过滤掉（命中黑名单）：%s", it["text"])
                    continue
                kept_items.append(it)
            items = kept_items

            if not items:
                processed[conv_id] = len(msgs)
                continue

            items, dropped, vecs = await self._dedupe(kb, items)
            dropped_total += dropped

            if not items:
                processed[conv_id] = len(msgs)
                continue

            # 闸门①：来源校验 —— 对不上「用户那一轮说过的话」的标 suspect（recall 端不注入）
            await self._gate_suspect(kb, items, vecs, turns)
            suspect_total += sum(1 for it in items if it.get("suspect"))

            texts = [it["text"] for it in items]
            try:
                doc = await kb.upload_document(
                    file_name=f"记忆-{conv_id[:8]}-{int(time.time())}",
                    file_content=None,
                    file_type="txt",
                    pre_chunked_text=texts,
                )
            except Exception as exc:
                logger.error(
                    "[记忆] 写库失败 %s: %s\n%s", conv_id, exc, traceback.format_exc()
                )
                skipped.append(conv_id[:8])
                continue

            try:
                await self._stamp_metadata(kb, doc, items)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[记忆] metadata 回填失败 %s: %s\n%s",
                    conv_id,
                    exc,
                    traceback.format_exc(),
                )

            existing.extend(_strip_anchor(it["text"]) for it in items)
            processed[conv_id] = len(msgs)
            docs += 1
            items_total += len(items)

        self._save_state(state)

        if (
            docs == 0
            and not skipped
            and dropped_total == 0
            and blocked_total == 0
            and bad_anchor_total == 0
            and bad_kind_total == 0
            and bad_json_total == 0
            and no_stamp_total == 0
        ):
            return "没有新东西要记。"
        msg = f"整理了 {docs} 段对话，写进去 {items_total} 条记忆。"
        if dropped_total:
            msg += f"\n有 {dropped_total} 条和已有的太像，没写。"
        if blocked_total:
            msg += f"\n有 {blocked_total} 条不该记的（账号、配置这类），滤掉了。"
        if bad_anchor_total:
            msg += f"\n有 {bad_anchor_total} 条时间标记对不上对话，丢掉了。"
        if bad_kind_total:
            msg += f"\n有 {bad_kind_total} 条 kind 不合法，丢掉了。"
        if bad_json_total:
            msg += f"\n有 {bad_json_total} 行解析不出 JSON，丢掉了。"
        if no_stamp_total:
            msg += f"\n有 {no_stamp_total} 条没带时间标记（event_date 记为整理当天）。"
        if suspect_total:
            msg += f"\n有 {suspect_total} 条对不上当时说过的话，标了来源存疑（不再注入，可用复核工具处理）。"
        msg += f"\n新写的这些是待审状态（{REVIEW_STATUS}），过一遍复核工具再转 ok。"
        if skipped:
            msg += f"\n有 {len(skipped)} 段失败了：{', '.join(skipped)}（日志里有详情）"
        return msg

    def _status_text(self) -> str:
        state = self._load_state()
        processed = state.get("processed") or {}
        try:
            convs = self._read_conversations()
        except Exception as exc:
            return f"读不到对话库：{exc}"
        lines = [f"知识库：{KB_NAME}", f"记录在案的会话：{len(processed)} 个"]
        pending = 0
        for conv_id, raw in convs:
            try:
                n = len(json.loads(raw))
            except Exception:
                continue
            done = int(processed.get(conv_id) or 0)
            if done < n:
                pending += 1
        lines.append(f"还有 {pending} 段没整理" if pending else "已经全部整理过了")
        return "\n".join(lines)

    # ───────────────────── 指令 ─────────────────────

    @filter.command("记住")
    async def remember_cn(self, event: AstrMessageEvent):
        """把最近的对话提炼成记忆，写进知识库。"""
        yield event.plain_result("在整理了……")
        try:
            yield event.plain_result(await self._run_digest())
        except Exception as exc:
            logger.error("[记忆] 异常: %s\n%s", exc, traceback.format_exc())
            yield event.plain_result(f"出错了：{exc}")

    @filter.command("remember")
    async def remember_en(self, event: AstrMessageEvent):
        """Alias of /记住"""
        yield event.plain_result("在整理了……")
        try:
            yield event.plain_result(await self._run_digest())
        except Exception as exc:
            logger.error("[记忆] 异常: %s\n%s", exc, traceback.format_exc())
            yield event.plain_result(f"出错了：{exc}")

    @filter.command("记住状态")
    async def status_cn(self, event: AstrMessageEvent):
        """看记忆整理的进度。"""
        yield event.plain_result(self._status_text())
