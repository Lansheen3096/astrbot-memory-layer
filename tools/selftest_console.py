#!/usr/bin/env python3
"""Embedding 配置自测（在 AstrBot 镜像里跑，不碰线上配置）。

用法：
    docker run --rm \
      -v "$PWD/astrbot_plugin_memory_digest":/plugin:ro \
      -v "$PWD/tools":/tools:ro \
      soulter/astrbot:latest python /tools/selftest_console.py

它做四件事：
  1. 起一个假的 OpenAI / Ollama embedding 服务；
  2. 直接 import 插件的 main.py，测 `_probe_embedding` / `_build_provider_config`；
  3. 用假 provider/kb manager 测 `_apply_embedding` 的维度闸门、模型闸门、
     强制切换与自动建库；
  4. 测「配置页保存 → 热重载 → `_sync_embedding_from_config` 自动应用」。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

PLUGIN_MAIN = os.environ.get("PLUGIN_MAIN", "/plugin/main.py")

VECTOR_DIM = 8


class _FakeEmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        inputs = payload.get("input") or [""]
        if isinstance(inputs, str):
            inputs = [inputs]
        if self.path == "/v1/embeddings":
            body = {
                "data": [
                    {"embedding": [0.01 * (i + 1)] * VECTOR_DIM}
                    for i in range(len(inputs))
                ]
            }
        elif self.path == "/api/embed":
            body = {"embeddings": [[0.02] * VECTOR_DIM for _ in inputs]}
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


class FakeEP:
    def __init__(self, model: str, dimension: int):
        self.model = model
        self.provider_config = {"embedding_model": model}
        self.dimension = dimension

    async def get_embedding(self, _text):
        return [0.0] * self.dimension

    async def get_embeddings(self, texts):
        return [[0.0] * self.dimension for _ in texts]

    def get_dim(self):
        return self.dimension


class FakeVecDB:
    def __init__(self, dimension: int, count: int):
        self.dimension = dimension
        self.count = count
        self.embedding_storage = SimpleNamespace(
            index=SimpleNamespace(d=dimension),
            dimension=dimension,
        )

    async def count_documents(self):
        return self.count


class FakeKB:
    def __init__(
        self, dimension: int = VECTOR_DIM, count: int = 5, model: str = "bge-m3"
    ):
        self.kb = SimpleNamespace(
            kb_id="kb-test",
            kb_name="长期记忆",
            embedding_provider_id="old_provider",
            rerank_provider_id="rr-test",
        )
        self.vec_db = FakeVecDB(dimension, count)
        self.ep = FakeEP(model, dimension)

    async def get_ep(self):
        return self.ep


class FakeProviderManager:
    def __init__(self):
        self.providers_config: list[dict] = []
        self.embedding_provider_insts: list = []
        self.rerank_provider_insts: list = []
        self.instance = None

    async def create_provider(self, cfg):
        self.providers_config.append(dict(cfg))
        self.instance = FakeEP(cfg["embedding_model"], cfg["embedding_dimensions"])

    async def update_provider(self, provider_id, cfg):
        self.providers_config = [
            dict(cfg) if item.get("id") == provider_id else item
            for item in self.providers_config
        ]
        if not any(item.get("id") == provider_id for item in self.providers_config):
            self.providers_config.append(dict(cfg))
        self.instance = FakeEP(cfg["embedding_model"], cfg["embedding_dimensions"])

    async def get_provider_by_id(self, provider_id):
        if any(item.get("id") == provider_id for item in self.providers_config):
            return self.instance
        return None


class FakeKBManager:
    def __init__(self, pm: FakeProviderManager):
        self.pm = pm
        self.helper = FakeKB()
        self.created = False

    async def get_kb_by_name(self, _name):
        return self.helper

    async def update_kb(self, **kwargs):
        if kwargs.get("embedding_provider_id"):
            self.helper.kb.embedding_provider_id = kwargs["embedding_provider_id"]
        self.helper.kb.rerank_provider_id = kwargs.get(
            "rerank_provider_id", self.helper.kb.rerank_provider_id
        )
        if self.pm.instance:
            self.helper.ep = self.pm.instance
        return self.helper

    async def create_kb(self, **kwargs):
        self.created = True
        self.helper = FakeKB(count=0, model="")
        self.helper.kb.embedding_provider_id = kwargs["embedding_provider_id"]
        self.helper.kb.rerank_provider_id = kwargs.get("rerank_provider_id")
        if self.pm.instance:
            self.helper.ep = self.pm.instance
        self.helper.vec_db.count = 0
        return self.helper


class FakeContext:
    def __init__(self):
        self.provider_manager = FakeProviderManager()
        self.kb_manager = FakeKBManager(self.provider_manager)
        self.config = {}

    def get_config(self, umo=None):  # noqa: ARG002
        return self.config


def load_plugin():
    spec = importlib.util.spec_from_file_location("memory_digest_selftest", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def expect_value(condition, message):
    if not condition:
        raise AssertionError(message)


async def expect_raises(coro, needle: str):
    try:
        await coro
    except ValueError as exc:
        expect_value(needle in str(exc), f"预期包含 {needle!r}，实际：{exc}")
        return
    raise AssertionError(f"预期抛 ValueError（{needle}），但没有")


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEmbeddingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        plugin = load_plugin()
        ctx = FakeContext()
        instance = plugin.Main(ctx, config={})

        # 纯函数
        expect_value(
            plugin.Main._normalize_local_target("127.0.0.1", 11434)
            == ("127.0.0.1", 11434),
            "local target 127.0.0.1",
        )
        expect_value(
            plugin.Main._normalize_local_target("http://host.docker.internal:11434", 1)
            == ("host.docker.internal", 11434),
            "local target URL 形式应解析出 host/port",
        )
        expect_value(
            plugin.Main._openai_base("https://api.openai.com")
            == "https://api.openai.com/v1",
            "OpenAI base 应补 /v1",
        )
        expect_value(
            plugin.Main._openai_base("https://api.openai.com/v1/embeddings")
            == "https://api.openai.com/v1",
            "OpenAI base 应剥掉 /embeddings",
        )

        # 云端探测
        cloud_cfg = plugin.Main._normalize_settings(
            {
                "embedding_mode": "cloud",
                "cloud_api_base": f"http://127.0.0.1:{port}/v1",
                "cloud_api_key": "test-key",
                "cloud_model": "text-embedding-3-small",
                "cloud_dimensions": 0,
                "embedding_timeout": 10,
            }
        )
        probe = await instance._probe_embedding(cloud_cfg)
        expect_value(probe["dimension"] == VECTOR_DIM, "云端探测维度")
        expect_value(probe["endpoint"].endswith("/v1/embeddings"), "云端探测 URL")

        # 本机 Ollama
        local_cfg = plugin.Main._normalize_settings(
            {
                "embedding_mode": "local",
                "local_host": "127.0.0.1",
                "local_port": port,
                "local_api": "ollama",
                "local_model": "bge-m3",
                "local_dimensions": 0,
                "embedding_timeout": 10,
            }
        )
        probe = await instance._probe_embedding(local_cfg)
        expect_value(probe["dimension"] == VECTOR_DIM, "Ollama 探测维度")

        # 本机 OpenAI 兼容
        local_cfg_openai = plugin.Main._normalize_settings(
            {
                "embedding_mode": "local",
                "local_host": "127.0.0.1",
                "local_port": port,
                "local_api": "openai",
                "local_model": "bge-m3",
                "local_dimensions": 0,
                "embedding_timeout": 10,
            }
        )
        probe = await instance._probe_embedding(local_cfg_openai)
        expect_value(probe["dimension"] == VECTOR_DIM, "本机 OpenAI 探测维度")

        # provider 配置形态
        provider_cfg = instance._build_provider_config(cloud_cfg, VECTOR_DIM)
        expect_value(provider_cfg["type"] == "openai_embedding", "云端 provider type")
        expect_value(
            provider_cfg["embedding_dimensions"] == VECTOR_DIM, "provider 维度"
        )
        expect_value(provider_cfg["embedding_api_key"] == "test-key", "provider key")
        local_provider = instance._build_provider_config(local_cfg, VECTOR_DIM)
        expect_value(
            local_provider["type"] == "ollama_embedding", "Ollama provider type"
        )
        expect_value(
            not local_provider["embedding_api_base"].endswith("/v1"),
            "Ollama base 不应带 /v1",
        )

        # 配置页留空 key 时沿用已保存 provider 的 key
        ctx.provider_manager.providers_config = [
            {
                "id": plugin.MANAGED_PROVIDER_ID,
                "embedding_api_key": "saved-key",
            }
        ]
        expect_value(
            instance._effective_cloud_key({"cloud_api_key": ""}) == "saved-key",
            "空 key 应保留已保存的 provider key",
        )

        # 模型不同：默认拒绝
        ctx.kb_manager.helper = FakeKB(dimension=VECTOR_DIM, count=5, model="bge-m3")
        ctx.provider_manager.providers_config = []
        ctx.provider_manager.instance = None
        await expect_raises(
            instance._apply_embedding(
                cloud_cfg,
                instance._build_provider_config(cloud_cfg, VECTOR_DIM),
                VECTOR_DIM,
                force=False,
                create_kb=False,
            ),
            "不同模型",
        )

        # 维度不同：即使 force 也拒绝
        ctx.kb_manager.helper = FakeKB(
            dimension=1024, count=5, model="text-embedding-3-small"
        )
        await expect_raises(
            instance._apply_embedding(
                cloud_cfg,
                instance._build_provider_config(cloud_cfg, VECTOR_DIM),
                VECTOR_DIM,
                force=True,
                create_kb=False,
            ),
            "维度",
        )

        # 同维度换模型 + force：成功，并且 provider 被创建
        ctx.kb_manager.helper = FakeKB(dimension=VECTOR_DIM, count=5, model="bge-m3")
        ctx.provider_manager.providers_config = []
        ctx.provider_manager.instance = None
        result = await instance._apply_embedding(
            cloud_cfg,
            instance._build_provider_config(cloud_cfg, VECTOR_DIM),
            VECTOR_DIM,
            force=True,
            create_kb=False,
        )
        expect_value("已更新知识库绑定" in result["message"], "force 保存应成功")
        expect_value(
            ctx.kb_manager.helper.kb.embedding_provider_id
            == plugin.MANAGED_PROVIDER_ID,
            "知识库应绑定到 managed provider",
        )

        # 知识库不存在 + 自动创建
        ctx.kb_manager.helper = None
        ctx.kb_manager.created = False
        ctx.provider_manager.providers_config = []
        ctx.provider_manager.instance = None
        result = await instance._apply_embedding(
            cloud_cfg,
            instance._build_provider_config(cloud_cfg, VECTOR_DIM),
            VECTOR_DIM,
            force=False,
            create_kb=True,
        )
        expect_value(result["created_kb"] is True, "应自动创建知识库")
        expect_value(ctx.kb_manager.created is True, "create_kb 应被调用")

        # 配置页保存 → 热重载 → 加载时自动应用
        ctx.kb_manager.helper = FakeKB(dimension=VECTOR_DIM, count=0, model="")
        ctx.provider_manager.providers_config = []
        ctx.provider_manager.instance = None
        instance.config = {
            "embedding_mode": "local",
            "local_host": "127.0.0.1",
            "local_port": port,
            "local_api": "ollama",
            "local_model": "bge-m3",
            "local_dimensions": 0,
            "create_kb_if_missing": True,
            "force_rebind": False,
        }

        async def _noop_save(cfg):
            instance.config.update(cfg)

        instance._save_settings_async = _noop_save
        await instance._sync_embedding_from_config()
        expect_value(
            ctx.kb_manager.helper.kb.embedding_provider_id
            == plugin.MANAGED_PROVIDER_ID,
            "配置页加载时应把知识库绑定到 managed provider",
        )
        expect_value(
            instance._settings()["local_dimensions"] == VECTOR_DIM,
            "自动探测的维度应写回配置",
        )

        print(f"PASS: embedding config selftest ({VECTOR_DIM}-dim fake server)")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
