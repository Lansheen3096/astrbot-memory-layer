"""接管记忆注入，按「状态 / 偏好 / 事件」三层策略走。

为什么接管：框架自带那条链（dense 50 + sparse 50 → RRF → rerank → top N）一坨平铺，
做不到「状态全量 / 偏好过闸 / 事件时间递减」分层。所以把 `kb_names` 置空关掉自带注入，
由本插件接管。`on_llm_request` 的 hook 点在 `build_main_agent()` 之后
（pipeline/.../internal.py:312 build → :352 call_event_hook），顺序上本来就是最后一道。

闸门判据（实测校准，数据见下）：
    地板 FLOOR   top1 ≥ 0.05      —— 实测不相关的噪声区在 0.001~0.03
    领先 LEAD    top1 ≥ LEAD×top2 —— 避免「一堆都不相关、只是有一个略高」

> 为什么不是 0.75：20 条真实消息打 48 条真实条目，top1 中位数 0.013、最大 0.29，
> ≥0.75 是 0/20。这个分衡量「这条记忆是否真的答上了这句话」，绝对值和预期不一个量级。
> 改写后能到 0.6~0.99，但同一个「对」的命中也可能只有 0.10 —— 所以判据用相对领先，
> 不用绝对高分。

查询预处理：纯代码清洗（去括号壳、去语气词、去标点、人称归一、补主语）。
实测能把相关对的 top1 从 0.29 抬到 0.84、从 0.12 抬到 0.60，零延迟。
"""

from __future__ import annotations

import json
import re
from datetime import date

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart

KB_NAME = "长期记忆"  # ★ 必须和 astrbot_plugin_memory_digest 里的一致

# 消息里对助手的称呼（昵称、爱称都算），打分前统一归一成「他」。
# ★ 按你部署里实际会出现的称呼填；默认空 = 不做归一。
#   打分用的查询越干净越准 —— 称呼词对检索没有信息量，只会稀释相似度。
ADDRESS_WORDS: tuple[str, ...] = ()

FLOOR = 0.05  # 地板
LEAD = 2.0  # 领先倍数
STATE_MAX = 40  # 状态层最多几条（本来就是全量，这个只是保险）
RECALL_MAX = 3  # 偏好+事件合计最多注入几条
HALFLIFE_DAYS = 30.0  # 事件层时间衰减半衰期
DECAY_FLOOR = 0.35  # 时间衰减不会把一条压到比这个更低
NEW_DISCOUNT = 0.5  # status="new"（待审）降权
MAX_CHARS = 1500  # 注入文本总长上限
TRACE = True  # 每轮往日志写一行判据明细，方便以后调参

_SYS = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)
_BURST_TS = re.compile(
    r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*"
)  # 上游 burst 插件聚合消息的逐条时刻前缀
_BRK = re.compile(r"[（(]([^）)]*)[）)]")
_TAIL = re.compile(r"[~～…\.。！!？?，,、]+$")
_WS = re.compile(r"\s+")

# 人称一律用【字段】而不是代词：用户永远是 [用户]，助手永远是 我。
# 这样没有性别、也没有「你 / 他 / 她」指代谁的歧义；写库、检索、注入三处
# 用的是同一套字面量。
# ★ 两个插件的这两个值必须一致 —— 检索是拿查询和库里的正文比字面量，
#   一边用 [用户]、一边写成「他」，那条记忆就检索不回来。
USER_REF = "[用户]"
SELF_REF = "我"


def clean_query(raw: str) -> str:
    """把用户那句口语/动作描写洗成一句检索用的话（只用于打分，不给模型看）。

    下面几处 re.sub 的第二个参数有两种，别看混：
      ""   整块删掉 —— 它本身就是噪声（系统标记、时刻前缀、句尾标点）
      " "  换成【空格】—— ★ 不能删，删了会把左右两个词粘成一个新词，反而污染检索
    """
    if not raw:
        return ""
    m = _SYS.sub("", raw).split("\n")[0]
    m = _BURST_TS.sub("", m)  # 聚合过的消息首行带时刻前缀，先剥掉再打分
    acts = _BRK.findall(m)
    m = _BRK.sub(" ", m)
    body = _WS.sub(" ", m).strip()
    body = _TAIL.sub("", body).strip(" ，。~～…")
    for _w in ADDRESS_WORDS:  # 消息里对助手的称呼先归一掉，打分时只会干扰
        body = body.replace(_w, USER_REF)
    body = body.replace("你", SELF_REF)
    if not body and acts:  # 纯动作消息（整句都在括号里）
        body = acts[0].replace("你", SELF_REF)
    if body and not body.startswith((USER_REF, SELF_REF)):
        body = USER_REF + body
    return body.strip()


def _decay(event_date: str | None) -> float:
    if not event_date:
        return 1.0
    try:
        days = (date.today() - date.fromisoformat(str(event_date))).days
    except Exception:
        return 1.0
    if days <= 0:
        return 1.0
    return max(DECAY_FLOOR, 0.5 ** (days / HALFLIFE_DAYS))


def _norm_meta(md) -> dict:
    if isinstance(md, str):
        try:
            md = json.loads(md or "{}")
        except Exception:
            md = {}
    return md if isinstance(md, dict) else {}


class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.config = config or {}
        self.kb_name = str(self.config.get("kb_name") or KB_NAME)
        trace = self.config.get("trace", TRACE)
        self.trace = (
            trace
            if isinstance(trace, bool)
            else str(trace).strip().lower() in {"1", "true", "yes", "on"}
        )

    @filter.on_llm_request()
    async def recall(self, event: AstrMessageEvent, *args, **kwargs) -> None:
        req = kwargs.get("req") or (args[0] if args else None)
        if req is None:
            return
        try:
            await self._recall(event, req)
        except Exception as exc:  # noqa: BLE001  注入失败绝不能拖垮一轮对话
            logger.warning(
                f"[recall] 接管注入失败，本轮不注入记忆: {type(exc).__name__}: {exc}"
            )

    async def _recall(self, event: AstrMessageEvent, req) -> None:
        kb = await self.context.kb_manager.get_kb_by_name(self.kb_name)
        if kb is None:
            logger.warning(f"[recall] 找不到知识库「{self.kb_name}」")
            return
        vec_db = kb.vec_db
        raw = getattr(req, "prompt", None) or ""
        if not raw.strip():
            return
        query = clean_query(raw)
        if not query:
            return

        # ── 取全库（48 条这个体量，直接全量打分比 dense 预筛更准也更简单）──
        docs = await vec_db.document_storage.get_documents(
            metadata_filters={}, limit=1000
        )
        items = []
        for d in docs:
            md = _norm_meta(d.get("metadata"))
            if md.get("suspect") in (1, "1", True):  # 来源存疑 → 一律不注入
                continue
            items.append(
                {
                    "text": d.get("text") or "",
                    "kind": md.get("kind") or "event",
                    "sid": md.get("sid") or "?",
                    "event_date": md.get("event_date"),
                    "status": md.get("status"),
                }
            )
        if not items:
            return

        # ── 状态层：全量，不过闸 ──
        states = [it for it in items if it["kind"] == "state"][:STATE_MAX]

        # ── 偏好 / 事件：rerank 打分后过闸 ──
        pool = [it for it in items if it["kind"] in ("preference", "event")]
        recalled, top1, top2, passed = [], 0.0, 0.0, False
        if pool:
            rp = await kb.get_rp()
            if rp is None:
                logger.warning("[recall] rerank provider 不可用，本轮只注入状态层")
            else:
                res = await rp.rerank(
                    query=query, documents=[it["text"] for it in pool]
                )
                scored = []
                for r in res:
                    if 0 <= r.index < len(pool):
                        it = dict(pool[r.index])
                        s = float(r.relevance_score)
                        if it["kind"] == "event":
                            s *= _decay(it["event_date"])
                        if it["status"] == "new":
                            s *= NEW_DISCOUNT
                        it["score"] = s
                        scored.append(it)
                scored.sort(key=lambda x: x["score"], reverse=True)
                if scored:
                    top1 = scored[0]["score"]
                    top2 = scored[1]["score"] if len(scored) > 1 else 0.0
                    passed = top1 >= FLOOR and top1 >= LEAD * top2
                    if passed:
                        # 逐条也要过地板 —— 否则 top1 勉强过闸时会把 0.02 的噪声一起带进去
                        recalled = [
                            it for it in scored[:RECALL_MAX] if it["score"] >= FLOOR
                        ]

        parts = []
        if states:
            parts.append(
                "【你们现在的状态｜每轮都给】\n"
                + "\n".join(f"- {it['text']}" for it in states)
            )
        if recalled:
            parts.append(
                "【这轮可能用得上的回忆】\n"
                + "\n".join(f"- {it['text']}" for it in recalled)
            )

        if self.trace:
            logger.info(
                "[recall] q=%r top1=%.4f top2=%.4f 过闸=%s 状态=%d 回忆=%s",
                query[:40],
                top1,
                top2,
                passed,
                len(states),
                [it["sid"] for it in recalled] or "-",
            )

        if not parts:
            return

        block = "\n\n".join(parts)
        if len(block) > MAX_CHARS:
            block = block[:MAX_CHARS] + "…"
        req.extra_user_content_parts.append(
            TextPart(text="[记忆库]\n" + block).mark_as_temp()
        )
