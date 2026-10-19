#!/usr/bin/env python3
"""按 AstrBot 4.29 的插件加载路径验证元数据 / 配置 schema / i18n / views / 路由。

用法：
    docker run --rm \
      -v "$PWD/astrbot_plugin_memory_digest":/plugin:ro \
      -v "$PWD/tools":/tools:ro \
      soulter/astrbot:latest python /tools/selftest_plugin_docs.py

不启动 AstrBot，不碰线上数据：只调用框架自己的加载函数。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_manager import PluginManager
from astrbot.dashboard.api.plugins import _match_registered_web_api
from astrbot.dashboard.services.plugin_page_service import PluginPageService

PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", "/plugin"))
PLUGIN_NAME = "astrbot_plugin_memory_digest"


class DummyPluginManager:
    def __init__(self):
        self.plugin_store_path = PLUGIN_DIR.parent
        self.reserved_plugin_path = PLUGIN_DIR.parent


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


async def main() -> None:
    # 1) metadata
    metadata = PluginManager._load_plugin_metadata(str(PLUGIN_DIR))
    expect(metadata is not None, "metadata.yaml 应能解析")
    expect(metadata.name == PLUGIN_NAME, f"插件名不对：{metadata.name}")
    expect(metadata.version == "0.2.0", f"版本不对：{metadata.version}")
    expect(bool(metadata.display_name), "缺少 display_name")
    expect(bool(metadata.short_desc), "缺少 short_desc")
    expect(metadata.astrbot_version == ">=4.29.0,<5", "astrbot_version 不对")
    expect(bool(metadata.repo), "缺少 repo")

    # 2) _conf_schema.json + AstrBotConfig 默认值
    schema = PluginManager._load_plugin_config_schema(
        str(PLUGIN_DIR / "_conf_schema.json")
    )
    expect("embedding_mode" in schema, "schema 缺少 embedding_mode")
    expect(schema["cloud_api_key"].get("secret") is True, "API Key 应标记 secret")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AstrBotConfig(
            config_path=str(Path(tmp) / f"{PLUGIN_NAME}_config.json"),
            schema=schema,
        )
        expect(cfg["embedding_mode"] == "kb", "默认模式应为 kb")
        expect(cfg["local_port"] == 11434, "默认端口应为 11434")
        cfg["embedding_mode"] = "local"
        cfg["local_port"] = 18888
        cfg.save_config()
        reread = AstrBotConfig(
            config_path=str(Path(tmp) / f"{PLUGIN_NAME}_config.json"),
            schema=schema,
        )
        expect(reread["embedding_mode"] == "local", "配置应能保存并回读")
        expect(reread["local_port"] == 18888, "端口应能保存并回读")

    # 3) i18n
    i18n = PluginManager._load_plugin_i18n(str(PLUGIN_DIR))
    expect("zh-CN" in i18n and "en-US" in i18n, "应加载中英文 i18n")
    title = (
        i18n["zh-CN"].get("views", {}).get("embedding-console", {}).get("title")
    )
    expect(title == "Embedding 控制台", f"视图标题不对：{title!r}")
    expect(
        i18n["en-US"].get("views", {}).get("embedding-console", {}).get("title")
        == "Embedding Console",
        "英文视图标题不对",
    )

    # 4) views 发现与资源解析
    page_service = PluginPageService(DummyPluginManager())
    meta = StarMetadata(
        name=PLUGIN_NAME,
        root_dir_name=PLUGIN_DIR.name,
        activated=True,
    )
    pages = await page_service.discover_plugin_pages(meta)
    names = [page.name for page in pages]
    expect(names == ["embedding-console"], f"视图发现结果不对：{names}")
    for asset in ("", "app.js", "style.css"):
        path = await page_service.resolve_plugin_page_file(
            meta, "embedding-console", asset
        )
        expect(path.is_file(), f"视图资源不存在：{asset or 'index.html'}")

    html = (PLUGIN_DIR / "views" / "embedding-console" / "index.html").read_text(
        encoding="utf-8"
    )
    expect("app.js" in html and 'type="module"' in html, "index.html 应引入模块脚本")
    js = (PLUGIN_DIR / "views" / "embedding-console" / "app.js").read_text(
        encoding="utf-8"
    )
    expect("window.AstrBotPluginView" in js, "app.js 应使用 bridge SDK")
    expect('apiGet("status")' in js, "app.js 应调用 status 接口")
    expect(
        'apiPost("save"' in js and 'apiPost("test"' in js,
        "app.js 应调用 test/save 接口",
    )

    # 5) register_web_api 路由能匹配 Dashboard 的扩展路径
    routes = [
        (f"/{PLUGIN_NAME}/status", None, ["GET"], ""),
        (f"/{PLUGIN_NAME}/test", None, ["POST"], ""),
        (f"/{PLUGIN_NAME}/save", None, ["POST"], ""),
    ]
    for route, _handler, methods, _desc in routes:
        subpath = route.lstrip("/")
        matched = _match_registered_web_api(routes, subpath, methods[0])
        expect(matched is not None, f"路由未匹配：{subpath}")
        # 错误方法不应匹配
        wrong = "POST" if methods[0] == "GET" else "GET"
        expect(
            _match_registered_web_api(routes, subpath, wrong) is None,
            f"错误方法不应匹配：{subpath} {wrong}",
        )

    print("PASS: metadata / config schema / i18n / views / routes")


if __name__ == "__main__":
    asyncio.run(main())
