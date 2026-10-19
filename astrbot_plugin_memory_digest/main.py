"""把用户与助手的对话提炼成助手的第一人称记忆，写进知识库。

（"助手" = 你的 bot；每条记忆由 bot 以「我」的口吻写下，
用 [用户] 这个字段指用户 —— 见文件顶部 USER_REF / SELF_REF 的说明。）

指令：
    /记住        手动跑一次提炼
    /记住状态    看进度（处理到哪了、库里有多少条）

Embedding 配置（插件配置页）：
    选择「跟随知识库 / 云端 API / 本机服务」；保存后热重载，
    由插件创建/更新 memory_layer_embedding 并绑定记忆知识库。

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
import shutil
import sqlite3
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_knowledge_base_path,
    get_astrbot_plugin_data_path,
)

try:  # AstrBot >= 4.29: 插件可视化视图 / 插件 Web API
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover - 老版本只是没有控制台入口
    WEB_API_AVAILABLE = False
    error_response = json_response = request = None

PLUGIN_NAME = "astrbot_plugin_memory_digest"
PLUGIN_VERSION = "0.2.0"

DATA_DIR = Path(get_astrbot_data_path())
DB_PATH = DATA_DIR / "data_v4.db"
KB_ROOT = Path(get_astrbot_knowledge_base_path())
PLUGIN_DATA_DIR = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
STATE_PATH = PLUGIN_DATA_DIR / "state.json"
LEGACY_STATE_PATH = DATA_DIR / "memory_digest_state.json"
KB_NAME = "长期记忆"  # ★ 必须和 astrbot_plugin_memory_recall 里的一致

# 配置页保存后会热重载插件，加载时按这组设置创建/更新 provider 并绑定知识库。
# provider ID 固定，便于重复保存时原地更新，也不会和用户手工创建的 provider 抢名字。
MANAGED_PROVIDER_ID = "memory_layer_embedding"
EMBEDDING_MODES = ("kb", "cloud", "local")
DEFAULT_SETTINGS = {
    "embedding_mode": "kb",
    "cloud_api_base": "",
    "cloud_api_key": "",
    "cloud_model": "text-embedding-3-small",
    "cloud_dimensions": 0,
    "local_host": "127.0.0.1",
    "local_port": 11434,
    "local_api": "ollama",
    "local_model": "bge-m3",
    "local_dimensions": 0,
    "embedding_timeout": 60,
    "create_kb_if_missing": True,
    "force_rebind": False,
}
SETTINGS_FILE = PLUGIN_DATA_DIR / "embedding_settings.json"

# 时间锚：上游把【收到用户消息的那一刻】写成 <system_reminder> 附在 user 消息里
# （本仓库的 astrbot-reply-rhythm / pacer 就干这个；没有也能跑）
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
    "账号",
    "帐号",
    "管理员",
    "权限",
    "配置",
    "设置项",
    "参数",
    "报错",
    "错误码",
    "网址",
    "链接",
    "接口",
    "端口",
    "密码",
    "令牌",
    "密钥",
    "指令",
    "命令",
    "模型",
    "提示词",
    "数据库",
    "日志",
    "脚本",
    "插件",
    "重启",
    "部署",
    "代码",
    "程序",
    "系统提示",
    "文件夹",
    "路径",
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
    def __init__(self, context: star.Context, config=None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.kb_name = str(self.config.get("kb_name") or KB_NAME)
        self._daily_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._embedding_lock = asyncio.Lock()
        self._register_web_apis()

    def _register_web_apis(self) -> None:
        """注册插件控制台用的后端 API。

        AstrBot >= 4.29 才提供 `astrbot.api.web`。老版本照常跑提炼/注入，
        只是没有控制台入口（日志里会留一行提示）。
        """
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[记忆] 当前 AstrBot 没有 astrbot.api.web，Embedding 控制台不可用；"
                "提炼与注入不受影响。"
            )
            return
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[记忆] context.register_web_api 不可用，跳过控制台 API 注册"
            )
            return
        routes = (
            (
                f"/{PLUGIN_NAME}/status",
                self.api_status,
                ["GET"],
                "Memory layer embedding console status",
            ),
            (
                f"/{PLUGIN_NAME}/test",
                self.api_test_embedding,
                ["POST"],
                "Probe an embedding endpoint without saving",
            ),
            (
                f"/{PLUGIN_NAME}/save",
                self.api_save_embedding,
                ["POST"],
                "Save embedding settings and bind the knowledge base",
            ),
            (
                f"/{PLUGIN_NAME}/review/list",
                self.api_review_list,
                ["GET"],
                "List memory entries for review",
            ),
            (
                f"/{PLUGIN_NAME}/review/apply",
                self.api_review_apply,
                ["POST"],
                "Apply review decisions",
            ),
        )
        for route, handler, methods, desc in routes:
            register(route, handler, methods, desc)

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
        # 配置页保存会热重载插件；这里把 embedding 设置落到 provider / 知识库。
        # 放到后台任务里，避免 endpoint 慢时拖住插件加载。
        if not (self._sync_task and not self._sync_task.done()):
            self._sync_task = asyncio.create_task(self._sync_embedding_from_config())

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
        # 兼容旧位置：之前写在 data/memory_digest_state.json。
        # 只迁移一次；新位置按文档要求放到 data/plugin_data/<plugin>/ 下。
        if not STATE_PATH.exists() and LEGACY_STATE_PATH.exists():
            try:
                PLUGIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(LEGACY_STATE_PATH, STATE_PATH)
                logger.info("[记忆] 进度文件已迁移到 %s", STATE_PATH)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[记忆] 迁移旧进度文件失败（继续用旧文件）：%s", exc)
        path = STATE_PATH if STATE_PATH.exists() else LEGACY_STATE_PATH
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("processed"), dict):
                    return data
            except Exception:
                logger.warning("[记忆] 状态文件损坏，重建")
        return {"processed": {}}

    def _save_state(self, state: dict) -> None:
        PLUGIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(STATE_PATH)

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
        cur = ""  # 当前这一轮的时间锚
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            parts: list[str] = []
            content = m.get("content")
            if isinstance(content, str):  # 兜底：老数据可能存成裸字符串
                content = [{"type": "text", "text": content}]
            elif isinstance(content, dict):  # 兜底：单个 block 没包成 list
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

    async def _dedupe(
        self, kb, items: list[dict]
    ) -> tuple[list[dict], int, list[list[float]]]:
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
            new_vecs = await ep.get_embeddings(
                [_strip_anchor(it["text"]) for it in items]
            )
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
                        "[记忆] 来源存疑（c_user=%.3f < %.2f）：%s",
                        c_user,
                        SOURCE_T,
                        it["text"][:60],
                    )
            logger.info(
                "[记忆] 来源校验完成：%d 条，标 suspect %d 条", len(plan), flagged
            )
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
                md["sid"] = f"{sid:04d}"
                md["status"] = (
                    REVIEW_STATUS  # 待审：recall 端打 0.5 折，人工过目后改 ok
                )
                if it.get("c_user") is not None:
                    md["c_user"] = it["c_user"]  # 来源校验分数，留着调阈值用
                if it.get("suspect"):
                    md["suspect"] = 1  # 来源存疑：recall 端一律不注入
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

        kb = await self.context.kb_manager.get_kb_by_name(self.kb_name)
        if kb is None:
            names = [k.kb_name for k in await self.context.kb_manager.list_kbs()]
            return (
                f"没找到知识库「{self.kb_name}」。现有的：{names or '（一个都没有）'}"
            )

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
            return DIGEST_SYSTEM + "\n\n【你已经记住的这些，不要再写一遍】\n" + joined

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
            items, pstats = _parse_digest(out, anchors, time.strftime("%Y-%m-%d"))
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

    # ───────────────────── Embedding 配置 ─────────────────────

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_settings(raw: dict | None) -> dict:
        out = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            out.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
        mode = str(out.get("embedding_mode") or "kb").strip().lower()
        out["embedding_mode"] = mode if mode in EMBEDDING_MODES else "kb"
        out["cloud_api_base"] = str(out.get("cloud_api_base") or "").strip()
        out["cloud_api_key"] = str(out.get("cloud_api_key") or "").strip()
        out["cloud_model"] = str(out.get("cloud_model") or "").strip()
        out["cloud_dimensions"] = max(0, Main._to_int(out.get("cloud_dimensions"), 0))
        out["local_host"] = str(out.get("local_host") or "127.0.0.1").strip()
        out["local_port"] = min(
            65535, max(1, Main._to_int(out.get("local_port"), 11434))
        )
        local_api = str(out.get("local_api") or "ollama").strip().lower()
        out["local_api"] = local_api if local_api in ("ollama", "openai") else "ollama"
        out["local_model"] = str(out.get("local_model") or "").strip()
        out["local_dimensions"] = max(0, Main._to_int(out.get("local_dimensions"), 0))
        out["embedding_timeout"] = min(
            300, max(5, Main._to_int(out.get("embedding_timeout"), 60))
        )
        out["create_kb_if_missing"] = Main._to_bool(
            out.get("create_kb_if_missing"), True
        )
        out["force_rebind"] = Main._to_bool(out.get("force_rebind"), False)
        return out

    def _fallback_settings(self) -> dict:
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                logger.warning("[记忆] 插件设置文件损坏，忽略：%s", exc)
        return {}

    def _settings(self) -> dict:
        raw: dict = {}
        if isinstance(self.config, dict):
            raw.update(
                {k: self.config[k] for k in DEFAULT_SETTINGS if k in self.config}
            )
        if not raw:
            raw.update(self._fallback_settings())
        return self._normalize_settings(raw)

    async def _save_settings_async(self, cfg: dict) -> None:
        cfg = self._normalize_settings(cfg)
        if isinstance(self.config, dict):
            for key in DEFAULT_SETTINGS:
                self.config[key] = cfg[key]
            save_async = getattr(self.config, "save_config_async", None)
            if callable(save_async):
                ok = await save_async()
                if ok is False:
                    logger.warning("[记忆] 配置保存被更新的快照覆盖，请刷新配置页确认")
                return
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
                return
        # 没有插件配置对象时（例如测试环境），退回到插件数据目录里的 JSON。
        PLUGIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)

    def _provider_configs(self) -> list[dict]:
        manager = getattr(self.context, "provider_manager", None)
        rows = getattr(manager, "providers_config", None) if manager else None
        return rows if isinstance(rows, list) else []

    def _managed_provider_config(self) -> dict | None:
        for item in self._provider_configs():
            if isinstance(item, dict) and item.get("id") == MANAGED_PROVIDER_ID:
                return dict(item)
        return None

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if len(value) <= 4:
            return "••••"
        return "••••" + value[-4:]

    def _provider_configs(self) -> list[dict]:
        manager = getattr(self.context, "provider_manager", None)
        rows = getattr(manager, "providers_config", None) if manager else None
        return rows if isinstance(rows, list) else []

    def _managed_provider_config(self) -> dict | None:
        for item in self._provider_configs():
            if isinstance(item, dict) and item.get("id") == MANAGED_PROVIDER_ID:
                return dict(item)
        return None

    def _public_settings(self, cfg: dict | None = None) -> dict:
        cfg = cfg or self._settings()
        existing = self._managed_provider_config() or {}
        has_key = bool(cfg["cloud_api_key"] or existing.get("embedding_api_key"))
        return {
            "embedding_mode": cfg["embedding_mode"],
            "cloud": {
                "api_base": cfg["cloud_api_base"],
                "model": cfg["cloud_model"],
                "dimensions": cfg["cloud_dimensions"],
                "api_key_set": has_key,
                "api_key_hint": self._mask_secret(
                    cfg["cloud_api_key"] or str(existing.get("embedding_api_key") or "")
                ),
            },
            "local": {
                "host": cfg["local_host"],
                "port": cfg["local_port"],
                "api": cfg["local_api"],
                "model": cfg["local_model"],
                "dimensions": cfg["local_dimensions"],
            },
            "timeout": cfg["embedding_timeout"],
        }

    def _merge_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        mode = (
            str(payload.get("embedding_mode") or payload.get("mode") or "kb")
            .strip()
            .lower()
        )
        if mode not in EMBEDDING_MODES:
            raise ValueError("embedding_mode 只能是 kb / cloud / local")

        cfg = self._settings()
        cfg["embedding_mode"] = mode

        cloud = payload.get("cloud")
        if isinstance(cloud, dict):
            if "api_base" in cloud:
                cfg["cloud_api_base"] = str(cloud.get("api_base") or "").strip()
            if "model" in cloud:
                cfg["cloud_model"] = str(cloud.get("model") or "").strip()
            if "dimensions" in cloud:
                cfg["cloud_dimensions"] = max(
                    0, self._to_int(cloud.get("dimensions"), 0)
                )
            key = cloud.get("api_key")
            if isinstance(key, str) and key.strip():
                cfg["cloud_api_key"] = key.strip()

        local = payload.get("local")
        if isinstance(local, dict):
            if "host" in local:
                cfg["local_host"] = str(local.get("host") or "").strip()
            if "port" in local:
                cfg["local_port"] = self._to_int(local.get("port"), cfg["local_port"])
            if "api" in local:
                cfg["local_api"] = str(local.get("api") or "").strip().lower()
            if "model" in local:
                cfg["local_model"] = str(local.get("model") or "").strip()
            if "dimensions" in local:
                cfg["local_dimensions"] = max(
                    0, self._to_int(local.get("dimensions"), 0)
                )

        timeout = payload.get("timeout", payload.get("embedding_timeout"))
        if timeout is not None:
            cfg["embedding_timeout"] = self._to_int(timeout, cfg["embedding_timeout"])

        cfg = self._normalize_settings(cfg)
        if mode == "cloud":
            cfg["cloud_api_base"] = self._validate_http_url(
                cfg["cloud_api_base"], "云端 API Base"
            )
            if not cfg["cloud_model"]:
                raise ValueError("云端 Embedding 模型不能为空")
        elif mode == "local":
            cfg["local_host"], cfg["local_port"] = self._normalize_local_target(
                cfg["local_host"], cfg["local_port"]
            )
            if not cfg["local_model"]:
                raise ValueError("本机 Embedding 模型不能为空")
        return cfg

    @staticmethod
    def _validate_http_url(raw: str, field: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            raise ValueError(f"{field} 不能为空")
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{field} 必须是 http(s) URL")
        return raw

    @staticmethod
    def _normalize_local_target(host_raw, port_raw) -> tuple[str, int]:
        raw = str(host_raw or "").strip()
        if not raw:
            raise ValueError("本机服务地址不能为空")
        port = Main._to_int(port_raw, 0)
        host = raw
        if "://" in raw:
            parsed = urlsplit(raw)
            host = parsed.hostname or ""
            if parsed.port:
                port = parsed.port
        else:
            try:
                parsed = urlsplit("//" + raw)
                host = parsed.hostname or raw
                if parsed.port:
                    port = parsed.port
            except ValueError:
                host = raw
        host = host.strip()
        if not host or "/" in host or "\\" in host:
            raise ValueError("本机服务地址不合法")
        if not (1 <= port <= 65535):
            raise ValueError("端口必须在 1~65535")
        return host, port

    @staticmethod
    def _http_authority(host: str, port: int) -> str:
        host = host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{port}"

    @staticmethod
    def _openai_base(raw: str) -> str:
        base = (raw or "").strip().rstrip("/")
        if base.endswith("/embeddings"):
            base = base[: -len("/embeddings")]
        if not re.search(r"/v\d+$", base):
            base += "/v1"
        return base

    @staticmethod
    def _redact_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            netloc = parsed.netloc
            if "@" in netloc:  # 不把 URL 里的 userinfo/凭据带进日志和回执
                netloc = netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except Exception:
            return url

    def _http_proxy(self) -> str | None:
        try:
            conf = self.context.get_config()
            proxy = str(conf.get("http_proxy") or "").strip()
            if proxy:
                return proxy
        except Exception:  # noqa: BLE001
            pass
        return None

    def _effective_cloud_key(self, cfg: dict) -> str:
        key = str(cfg.get("cloud_api_key") or "").strip()
        if key:
            return key
        existing = self._managed_provider_config() or {}
        return str(existing.get("embedding_api_key") or "").strip()

    async def _probe_embedding(self, cfg: dict) -> dict:
        mode = cfg["embedding_mode"]
        timeout = int(cfg["embedding_timeout"])
        headers: dict[str, str] = {}
        proxy = None
        key = ""

        if mode == "cloud":
            model = cfg["cloud_model"]
            base = self._openai_base(
                self._validate_http_url(cfg["cloud_api_base"], "云端 API Base")
            )
            url = base + "/embeddings"
            key = self._effective_cloud_key(cfg) or "not-needed"
            headers["Authorization"] = f"Bearer {key}"
            proxy = self._http_proxy()
            payload = {"model": model, "input": ["memory layer connectivity test"]}
        elif mode == "local":
            model = cfg["local_model"]
            host, port = self._normalize_local_target(
                cfg["local_host"], cfg["local_port"]
            )
            authority = self._http_authority(host, port)
            if cfg["local_api"] == "ollama":
                url = f"http://{authority}/api/embed"
                payload = {
                    "model": model,
                    "input": ["memory layer connectivity test"],
                }
            else:
                url = f"http://{authority}/v1/embeddings"
                headers["Authorization"] = "Bearer not-needed"
                payload = {
                    "model": model,
                    "input": ["memory layer connectivity test"],
                }
        else:
            raise ValueError("当前是「跟随知识库」，没有可测试的自定义端点")

        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(
                    url, json=payload, headers=headers, proxy=proxy
                ) as resp:
                    status = resp.status
                    body = await resp.text()
        except asyncio.TimeoutError as exc:
            raise ValueError(
                f"连接超时（{timeout}s）：{self._redact_url(url)}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ValueError(f"连接失败：{self._redact_url(url)}：{exc}") from exc

        if status != 200:
            safe_body = body.replace(key, "***") if key else body
            raise ValueError(f"HTTP {status}：{safe_body[:300]}")
        try:
            data = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("返回内容不是 JSON") from exc

        vec = None
        if mode == "local" and cfg["local_api"] == "ollama":
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                vec = embeddings[0]
            elif isinstance(data.get("embedding"), list):
                vec = data["embedding"]
        else:
            data_rows = data.get("data")
            if (
                isinstance(data_rows, list)
                and data_rows
                and isinstance(data_rows[0], dict)
            ):
                vec = data_rows[0].get("embedding")
            elif isinstance(data.get("embeddings"), list) and data["embeddings"]:
                vec = data["embeddings"][0]

        if not isinstance(vec, list) or not vec:
            safe_body = body.replace(key, "***") if key else body
            raise ValueError(f"返回里没有 embedding 向量：{safe_body[:200]}")
        dimension = len(vec)
        if dimension <= 0:
            raise ValueError("返回的 embedding 向量为空")

        latency = int((time.monotonic() - started) * 1000)
        logger.info(
            "[记忆] Embedding 探测成功：mode=%s model=%s dim=%d %dms",
            mode,
            model,
            dimension,
            latency,
        )
        return {
            "mode": mode,
            "model": model,
            "dimension": dimension,
            "latency_ms": latency,
            "endpoint": self._redact_url(url),
        }

    def _build_provider_config(self, cfg: dict, dimension: int) -> dict:
        mode = cfg["embedding_mode"]
        key = ""
        if mode == "cloud":
            provider_type = "openai_embedding"
            base = cfg["cloud_api_base"]
            model = cfg["cloud_model"]
            key = self._effective_cloud_key(cfg) or "not-needed"
        elif mode == "local":
            host, port = self._normalize_local_target(
                cfg["local_host"], cfg["local_port"]
            )
            authority = self._http_authority(host, port)
            model = cfg["local_model"]
            if cfg["local_api"] == "ollama":
                provider_type = "ollama_embedding"
                base = f"http://{authority}"
            else:
                provider_type = "openai_embedding"
                base = f"http://{authority}/v1"
                key = "not-needed"
        else:
            raise ValueError("跟随知识库模式不需要创建 provider")

        provider = {
            "id": MANAGED_PROVIDER_ID,
            "type": provider_type,
            "provider_type": "embedding",
            "enable": True,
            "embedding_api_base": base,
            "embedding_model": model,
            "embedding_dimensions": int(dimension),
            "timeout": int(cfg["embedding_timeout"]),
        }
        if provider_type == "openai_embedding":
            provider["embedding_api_key"] = key
        if mode == "cloud":
            proxy = self._http_proxy()
            if proxy:
                provider["proxy"] = proxy
        return provider

    def _provider_rows(self, kind: str) -> list[dict]:
        rows: list[dict] = []
        for item in self._provider_configs():
            if not isinstance(item, dict):
                continue
            ptype = str(item.get("type") or "")
            pkind = str(item.get("provider_type") or "")
            matched = ptype.endswith(f"_{kind}") or pkind == kind
            if kind == "embedding":
                matched = matched or ptype.endswith("embedding")
            if not matched:
                continue
            rows.append(
                {
                    "id": item.get("id") or "",
                    "type": ptype,
                    "model": item.get("embedding_model") or item.get("model") or "",
                    "base": item.get("embedding_api_base")
                    or item.get("api_base")
                    or "",
                }
            )
        return rows

    def _first_rerank_id(self) -> str | None:
        manager = getattr(self.context, "provider_manager", None)
        for inst in getattr(manager, "rerank_provider_insts", []) or []:
            try:
                meta = inst.meta()
                provider_id = getattr(meta, "id", None)
            except Exception:  # noqa: BLE001
                provider_id = None
            if provider_id:
                return str(provider_id)
        for item in self._provider_configs():
            if not isinstance(item, dict):
                continue
            ptype = str(item.get("type") or "")
            if ptype.endswith("rerank") or item.get("provider_type") == "rerank":
                if item.get("id"):
                    return str(item["id"])
        return None

    async def _check_rebind_safe(
        self, helper, dimension: int, provider_cfg: dict, force: bool
    ) -> None:
        doc_count = 0
        index_dim = 0
        try:
            vec_db = getattr(helper, "vec_db", None)
            if vec_db is not None:
                doc_count = int(await vec_db.count_documents())
                index = getattr(vec_db.embedding_storage, "index", None)
                index_dim = int(getattr(index, "d", 0) or 0)
                if not index_dim:
                    index_dim = int(
                        getattr(vec_db.embedding_storage, "dimension", 0) or 0
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 读取知识库向量信息失败：%s", exc)

        if doc_count <= 0:
            return
        if not index_dim:
            raise ValueError(
                f"知识库已有 {doc_count} 条条目，但读不到现有向量索引的维度，"
                "无法安全切换。请新建一个知识库，或先修复/重建现有索引。"
            )
        if dimension != index_dim:
            raise ValueError(
                f"知识库已有 {doc_count} 条向量，现有索引维度是 {index_dim}，"
                f"而新 embedding 返回 {dimension} 维。维度不同会直接损坏检索；"
                "请新建一个知识库，或清空旧条目后再切换。"
            )

        current_model = ""
        try:
            ep = await helper.get_ep()
            current_model = str(
                getattr(ep, "model", "")
                or (getattr(ep, "provider_config", {}) or {}).get("embedding_model")
                or ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 读取知识库当前 embedding 模型失败：%s", exc)

        new_model = str(provider_cfg.get("embedding_model") or "")
        if current_model and new_model and current_model != new_model and not force:
            raise ValueError(
                f"知识库已有 {doc_count} 条记忆，当前模型是 {current_model}，"
                f"新模型是 {new_model}。不同模型即使维度相同也不在同一向量空间；"
                "确认要切换请打开配置页的「允许同维度换模型」"
                "（旧记忆可能检索异常），或新建知识库。"
            )
        if (
            not current_model
            and helper.kb.embedding_provider_id != MANAGED_PROVIDER_ID
            and not force
        ):
            raise ValueError(
                "无法确认当前知识库使用的 embedding 模型；确认切换请打开"
                "配置页的「允许同维度换模型」，或新建知识库。"
            )

    async def _upsert_managed_provider(self, provider_cfg: dict) -> None:
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            raise ValueError(
                "拿不到 AstrBot provider_manager，无法创建 embedding provider"
            )
        if self._managed_provider_config():
            await manager.update_provider(MANAGED_PROVIDER_ID, provider_cfg)
        else:
            await manager.create_provider(provider_cfg)
        instance = await manager.get_provider_by_id(MANAGED_PROVIDER_ID)
        if instance is None:
            raise ValueError(
                "embedding provider 创建后没有加载成功，请查看 AstrBot 日志"
            )
        got_dim = self._to_int(getattr(instance, "get_dim", lambda: 0)(), 0)
        if got_dim and got_dim != int(provider_cfg["embedding_dimensions"]):
            raise ValueError(
                f"provider 维度 {got_dim} 与探测维度 "
                f"{provider_cfg['embedding_dimensions']} 不一致"
            )

    async def _apply_embedding(
        self,
        cfg: dict,
        provider_cfg: dict,
        dimension: int,
        *,
        force: bool,
        create_kb: bool,
    ) -> dict:
        kb_manager = self.context.kb_manager
        helper = await kb_manager.get_kb_by_name(self.kb_name)
        warnings: list[str] = []

        if helper is None and not create_kb:
            raise ValueError(
                f"找不到知识库「{self.kb_name}」。请先在 WebUI 创建，"
                "或打开配置页的「知识库不存在时自动创建」。"
            )
        if helper is not None:
            await self._check_rebind_safe(helper, dimension, provider_cfg, force)

        await self._upsert_managed_provider(provider_cfg)

        created = False
        if helper is None:
            rerank_id = self._first_rerank_id()
            helper = await kb_manager.create_kb(
                kb_name=self.kb_name,
                description="长期记忆（由 memory layer 配置创建）",
                embedding_provider_id=MANAGED_PROVIDER_ID,
                rerank_provider_id=rerank_id,
            )
            created = True
            if not rerank_id:
                warnings.append(
                    "知识库没有可用的 rerank provider，注入侧本轮只会给状态层；"
                    "请到 AstrBot 知识库设置里补一个 rerank provider。"
                )
        else:
            updated = await kb_manager.update_kb(
                kb_id=helper.kb.kb_id,
                kb_name=helper.kb.kb_name,
                embedding_provider_id=MANAGED_PROVIDER_ID,
                rerank_provider_id=helper.kb.rerank_provider_id,
            )
            if updated is None:
                raise ValueError("知识库更新失败，请查看 AstrBot 日志")
            if getattr(updated, "init_error", None):
                raise ValueError(f"知识库重新初始化失败：{updated.init_error}")
            helper = updated

        ep = await helper.get_ep()
        vector = await ep.get_embedding("memory layer binding check")
        if len(vector) != dimension:
            raise ValueError(
                f"绑定后 provider 返回 {len(vector)} 维，与探测的 {dimension} 维不一致"
            )

        action = "已创建知识库并绑定" if created else "已更新知识库绑定"
        message = (
            f"{action}：{MANAGED_PROVIDER_ID}（{provider_cfg['embedding_model']}"
            f" / {dimension} 维）"
        )
        logger.info("[记忆] %s", message)
        return {
            "message": message,
            "created_kb": created,
            "warnings": warnings,
        }

    @staticmethod
    def _to_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _expected_provider_config(self, cfg: dict) -> dict | None:
        dimension = self._to_int(
            cfg["cloud_dimensions"]
            if cfg["embedding_mode"] == "cloud"
            else cfg["local_dimensions"],
            0,
        )
        if dimension <= 0:
            return None
        return self._build_provider_config(cfg, dimension)

    async def _binding_matches(self, expected: dict) -> bool:
        current = self._managed_provider_config()
        if not current:
            return False
        for key in (
            "type",
            "embedding_api_base",
            "embedding_model",
            "embedding_dimensions",
            "embedding_api_key",
            "timeout",
            "proxy",
        ):
            if str(current.get(key) or "") != str(expected.get(key) or ""):
                return False
        try:
            helper = await self.context.kb_manager.get_kb_by_name(self.kb_name)
        except Exception:  # noqa: BLE001
            return False
        return bool(
            helper is not None
            and helper.kb.embedding_provider_id == MANAGED_PROVIDER_ID
        )

    async def _sync_embedding_from_config(self) -> None:
        """把配置页里的 embedding 设置落到 provider 和知识库。

        配置页保存后 AstrBot 会热重载插件，所以这里每次加载都跑一次；
        已经一致就跳过，不会重复重建向量库。
        """
        cfg = self._settings()
        if cfg["embedding_mode"] == "kb":
            return

        try:
            expected = self._expected_provider_config(cfg)
            if expected is not None and await self._binding_matches(expected):
                logger.info("[记忆] Embedding 配置未变化，跳过 provider/知识库更新")
                return

            probe = await self._probe_embedding(cfg)
            dimension = int(probe["dimension"])
            dimension_key = (
                "cloud_dimensions"
                if cfg["embedding_mode"] == "cloud"
                else "local_dimensions"
            )
            cfg[dimension_key] = dimension
            provider_cfg = self._build_provider_config(cfg, dimension)

            async with self._embedding_lock:
                result = await self._apply_embedding(
                    cfg,
                    provider_cfg,
                    dimension,
                    force=self._to_bool(cfg.get("force_rebind"), False),
                    create_kb=self._to_bool(cfg.get("create_kb_if_missing"), True),
                )

            # 探测到的维度写回配置，下次加载可以直接复用。
            try:
                await self._save_settings_async(cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[记忆] Embedding 维度写回配置失败：%s", exc)

            for warning in result.get("warnings") or []:
                logger.warning("[记忆] %s", warning)
        except Exception as exc:  # noqa: BLE001
            logger.error("[记忆] 按配置应用 embedding 失败：%s", exc)
            logger.error(traceback.format_exc())

    async def _kb_snapshot(self) -> dict:
        snapshot: dict = {
            "name": self.kb_name,
            "exists": False,
            "kb_id": "",
            "embedding_provider_id": "",
            "embedding_model": "",
            "embedding_base": "",
            "vector_dim": 0,
            "doc_count": 0,
            "rerank_provider_id": "",
            "managed_by_plugin": False,
        }
        helper = await self.context.kb_manager.get_kb_by_name(self.kb_name)
        if helper is None:
            return snapshot

        snapshot["exists"] = True
        snapshot["kb_id"] = helper.kb.kb_id
        snapshot["embedding_provider_id"] = helper.kb.embedding_provider_id or ""
        snapshot["rerank_provider_id"] = helper.kb.rerank_provider_id or ""
        snapshot["managed_by_plugin"] = (
            helper.kb.embedding_provider_id == MANAGED_PROVIDER_ID
        )
        try:
            vec_db = getattr(helper, "vec_db", None)
            if vec_db is not None:
                snapshot["doc_count"] = int(await vec_db.count_documents())
                index = getattr(vec_db.embedding_storage, "index", None)
                snapshot["vector_dim"] = int(getattr(index, "d", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 读取知识库条目数失败：%s", exc)

        try:
            ep = await helper.get_ep()
            snapshot["embedding_model"] = str(
                getattr(ep, "model", "")
                or (getattr(ep, "provider_config", {}) or {}).get("embedding_model")
                or ""
            )
            snapshot["embedding_base"] = str(
                (getattr(ep, "provider_config", {}) or {}).get("embedding_api_base")
                or getattr(ep, "base_url", "")
                or ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 读取知识库 embedding provider 失败：%s", exc)
        return snapshot

    def _digest_snapshot(self) -> dict:
        state = self._load_state()
        processed = state.get("processed") or {}
        pending = 0
        try:
            for conv_id, raw in self._read_conversations():
                try:
                    total = len(json.loads(raw))
                except Exception:
                    continue
                if int(processed.get(conv_id) or 0) < total:
                    pending += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 读取会话进度失败：%s", exc)
        return {
            "processed_conversations": len(processed),
            "pending_conversations": pending,
            "state_file": str(STATE_PATH),
        }

    def _recall_installed(self) -> bool:
        """注入侧插件是否已安装并加载。控制台用它决定审批页签是否可用。"""
        try:
            if self.context.get_registered_star("astrbot_plugin_memory_recall"):
                return True
        except Exception:
            pass
        try:
            plugins_dir = Path(get_astrbot_plugin_data_path()).parent / "plugins"
            return (plugins_dir / "astrbot_plugin_memory_recall").is_dir()
        except Exception:
            return False

    async def _status_payload(self) -> dict:
        return {
            "plugin": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
            "config": self._public_settings(),
            "kb": await self._kb_snapshot(),
            "providers": {
                "embedding": self._provider_rows("embedding"),
                "rerank": self._provider_rows("rerank"),
            },
            "digest": self._digest_snapshot(),
            "web_api": WEB_API_AVAILABLE,
            "recall_installed": self._recall_installed(),
        }

    async def api_status(self):
        """GET /<plugin>/status：控制台首屏需要的全部运行状态。"""
        try:
            return json_response(await self._status_payload())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[记忆] 控制台状态读取失败：%s\n%s", exc, traceback.format_exc()
            )
            return error_response(f"读取状态失败：{exc}", status_code=500)

    async def api_test_embedding(self):
        """POST /<plugin>/test：只探测，不保存、不改知识库。"""
        try:
            payload = await request.json(default={})
            cfg = self._merge_payload(payload)
            if cfg["embedding_mode"] == "kb":
                return json_response(
                    {
                        "mode": "kb",
                        "message": "当前是「跟随知识库」模式，不需要测试自定义端点。",
                        "kb": await self._kb_snapshot(),
                    }
                )
            probe = await self._probe_embedding(cfg)
            declared = (
                cfg["cloud_dimensions"]
                if cfg["embedding_mode"] == "cloud"
                else cfg["local_dimensions"]
            )
            warnings = []
            if declared and declared != probe["dimension"]:
                warnings.append(
                    f"填写的是 {declared} 维，实际探测到 {probe['dimension']} 维；"
                    "保存时会以实际探测值为准。"
                )
            return json_response({**probe, "warnings": warnings})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.error("[记忆] 控制台测试失败：%s\n%s", exc, traceback.format_exc())
            return error_response(f"测试失败：{exc}", status_code=500)

    async def api_save_embedding(self):
        """POST /<plugin>/save：保存设置，必要时创建/更新 provider 并绑定知识库。"""
        try:
            payload = await request.json(default={})
            cfg = self._merge_payload(payload)
            if cfg["embedding_mode"] == "kb":
                await self._save_settings_async(cfg)
                return json_response(
                    {
                        "ok": True,
                        "message": "已切换为「跟随知识库」，没有修改知识库绑定。",
                        "status": await self._status_payload(),
                    }
                )

            probe = await self._probe_embedding(cfg)
            dimension = int(probe["dimension"])
            cfg[
                "cloud_dimensions"
                if cfg["embedding_mode"] == "cloud"
                else "local_dimensions"
            ] = dimension
            provider_cfg = self._build_provider_config(cfg, dimension)

            force = bool(payload.get("force"))
            create_kb = bool(payload.get("create_kb", True))
            async with self._embedding_lock:
                result = await self._apply_embedding(
                    cfg,
                    provider_cfg,
                    dimension,
                    force=force,
                    create_kb=create_kb,
                )

            warnings = list(result.get("warnings") or [])
            try:
                await self._save_settings_async(cfg)
            except Exception as exc:  # noqa: BLE001
                logger.error("[记忆] 知识库已绑定，但插件配置保存失败：%s", exc)
                warnings.append(f"知识库已绑定，但插件配置写入失败：{exc}")

            try:
                status = await self._status_payload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[记忆] 绑定成功但读取状态失败：%s", exc)
                status = None
                warnings.append(f"绑定成功，但读取状态失败：{exc}")

            return json_response(
                {
                    "ok": True,
                    "message": result["message"],
                    "warnings": warnings,
                    "probe": probe,
                    "status": status,
                }
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.error("[记忆] 控制台保存失败：%s\n%s", exc, traceback.format_exc())
            return error_response(f"保存失败：{exc}", status_code=500)

    def _status_text(self) -> str:
        state = self._load_state()
        processed = state.get("processed") or {}
        try:
            convs = self._read_conversations()
        except Exception as exc:
            return f"读不到对话库：{exc}"
        managed = self._managed_provider_config() or {}
        embedding_note = ""
        if managed:
            embedding_note = (
                f"（{managed.get('embedding_model') or '?'} / "
                f"{managed.get('embedding_dimensions') or '?'} 维）"
            )
        lines = [
            f"知识库：{self.kb_name}",
            f"记录在案的会话：{len(processed)} 个",
            f"Embedding 来源：{self._settings()['embedding_mode']}{embedding_note}",
        ]
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

    # ───────────────────── 记忆审批（控制台） ─────────────────────

    async def _kb_obj(self):
        """拿当前配置对应的知识库对象；拿不到返回 None。"""
        kb_manager = getattr(self.context, "kb_manager", None)
        if kb_manager is None:
            return None
        try:
            return await kb_manager.get_kb_by_name(self.kb_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[记忆] 审批读取知识库失败：%s", exc)
            return None

    def _kb_doc_db(self, kb_id: str) -> Path:
        return KB_ROOT / kb_id / "doc.db"

    def _review_rows(self, kb_id: str) -> list[dict]:
        db_path = self._kb_doc_db(kb_id)
        if not db_path.is_file():
            return []
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
        try:
            rows = []
            for doc_id, text, metadata, created_at in con.execute(
                "select doc_id, text, metadata, created_at from documents"
            ):
                try:
                    meta = json.loads(metadata or "{}")
                except Exception:
                    meta = {}
                rows.append(
                    {
                        "doc_id": doc_id,
                        "sid": meta.get("sid") or "?",
                        "text": text or "",
                        "kind": meta.get("kind") or "event",
                        "status": meta.get("status"),
                        "suspect": meta.get("suspect"),
                        "event_date": meta.get("event_date"),
                        "created_at": created_at,
                        "meta": meta,
                    }
                )
            rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            return rows
        finally:
            con.close()

    async def api_review_list(self):
        """GET /<plugin>/review/list：列出知识库里的记忆条目与待审状态。"""
        try:
            kb = await self._kb_obj()
            if kb is None:
                return error_response(
                    f"找不到知识库「{self.kb_name}」，请先在 WebUI 创建。",
                    status_code=404,
                )
            kb_id = kb.kb.kb_id
            rows = self._review_rows(kb_id)
            counts = {
                "total": len(rows),
                "pending": sum(1 for r in rows if r["status"] != "ok" or r["suspect"]),
                "new": sum(1 for r in rows if r["status"] == "new"),
                "suspect": sum(1 for r in rows if r["suspect"]),
                "ok": sum(1 for r in rows if r["status"] == "ok"),
            }
            for row in rows:
                row.pop("meta", None)
            return json_response(
                {
                    "kb": {"id": kb_id, "name": self.kb_name},
                    "counts": counts,
                    "rows": rows,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[记忆] 审批列表读取失败：%s\n%s", exc, traceback.format_exc())
            return error_response(f"读取审批列表失败：{exc}", status_code=500)

    async def api_review_apply(self):
        """POST /<plugin>/review/apply：应用一批 keep / suspect / unsuspect / delete。"""
        try:
            payload = await request.json(default={})
            plan = payload.get("plan") or {}
            if not isinstance(plan, dict):
                return error_response("plan 必须是对象", status_code=400)

            kb = await self._kb_obj()
            if kb is None:
                return error_response(
                    f"找不到知识库「{self.kb_name}」", status_code=404
                )
            kb_id = kb.kb.kb_id
            rows = {r["sid"]: r for r in self._review_rows(kb_id)}

            order = ("delete", "keep", "suspect", "unsuspect")
            seen: set[str] = set()
            todo: dict[str, list[dict]] = {op: [] for op in order}
            unknown: list[str] = []
            for op in order:
                for raw in plan.get(op) or []:
                    sid = str(raw).strip()
                    if not sid or sid in seen:
                        continue
                    if sid not in rows:
                        unknown.append(sid)
                        continue
                    seen.add(sid)
                    todo[op].append(rows[sid])

            # 状态类：只改 metadata，不动向量索引
            changes: list[tuple[dict, dict]] = []
            for row in todo["keep"]:
                changes.append((row, {"status": "ok", "suspect": False}))
            for row in todo["suspect"]:
                changes.append(
                    (row, {"status": row["status"] or "new", "suspect": True})
                )
            for row in todo["unsuspect"]:
                changes.append(
                    (row, {"status": row["status"] or "new", "suspect": False})
                )

            if changes:
                con = sqlite3.connect(str(self._kb_doc_db(kb_id)), timeout=30)
                try:
                    for row, patch_meta in changes:
                        meta = dict(row["meta"])
                        meta.update(patch_meta)
                        con.execute(
                            "update documents set metadata=? where doc_id=?",
                            (json.dumps(meta, ensure_ascii=False), row["doc_id"]),
                        )
                    con.commit()
                finally:
                    con.close()

            # 删除必须走框架 API，三处（documents / FTS / faiss）一起清
            for row in todo["delete"]:
                await kb.delete_document(row["doc_id"])

            results = {
                "keep": len(todo["keep"]),
                "suspect": len(todo["suspect"]),
                "unsuspect": len(todo["unsuspect"]),
                "delete": len(todo["delete"]),
            }
            return json_response(
                {
                    "ok": True,
                    "applied": results,
                    "unknown": unknown,
                    "message": (
                        f"已应用：通过 {results['keep']} / 存疑 {results['suspect']} / "
                        f"取消存疑 {results['unsuspect']} / 删除 {results['delete']}"
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[记忆] 审批应用失败：%s\n%s", exc, traceback.format_exc())
            return error_response(f"应用失败：{exc}", status_code=500)
