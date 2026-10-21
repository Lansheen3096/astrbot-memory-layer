# astrbot-memory-layer

一条**完整的长期记忆管线**：**写入（提炼）→ 注入（召回）→ 复核（人审）**。

> **这个仓库是聚合入口，不再是插件源码仓库。**
> 两个插件已拆成独立仓库，源码、issue、market 发布都在那边；
> 本仓库负责三件事：**版本锁定（install.sh + 联合 tag）**、**复核台 `tools/`**、**兼容矩阵**。

| 组件 | 仓库 | 版本 | 作用 |
|---|---|---|---|
| 写入侧 | [`astrbot_plugin_memory_digest`](https://github.com/Lansheen3096/astrbot_plugin_memory_digest) | `v0.2.2` | 把对话提炼成第一人称记忆写进知识库；配置页 + Embedding 控制台 |
| 注入侧 | [`astrbot_plugin_memory_recall`](https://github.com/Lansheen3096/astrbot_plugin_memory_recall) | `v0.2.0` | 接管记忆注入：状态层全量、偏好 / 事件层过闸门、事件按时间递减 |
| 复核台 | 本仓库 `tools/` | — | 命令行 + 本地 WebUI，清掉待审条目和存疑条目 |

两个插件**必须一起装**，两个配置页里的 `kb_name` 必须一致。

## 安装

```bash
# 拉到临时目录，看过再拷进 AstrBot
git clone --depth 1 --branch memory-layer-v0.1.0 https://github.com/Lansheen3096/astrbot-memory-layer
cd astrbot-memory-layer && ./install.sh --dest ./plugins

# 或者直接拉进插件目录
./install.sh --dest <AstrBot>/data/plugins
docker compose restart astrbot
```

`install.sh` 按固定 tag 拉两个独立仓库（`--print` 只看版本不落盘）。
插件在市场里也能单独装，但版本组合以本仓库的联合 tag 为准。

装好后：

1. 在 WebUI 里建好知识库（默认名 `长期记忆`），或在写入侧配置页保留
   「知识库不存在时自动创建」；两个配置页里的 `kb_name` 必须一致。
2. 打开「长期记忆（写入侧）」→ 配置，或插件详情页的「Embedding 控制台」，
   选「跟随知识库 / 云端 API / 本机服务」再保存。插件热重载并探测、创建 / 更新 provider、绑定知识库。
3. 给知识库配一个 rerank provider（写入侧只管 embedding，不管 rerank）。
4. 看 AstrBot 日志确认结果，或用 `/记住状态` 看当前来源和已绑定维度。

## 版本兼容矩阵

| 聚合 tag | `memory_digest` | `memory_recall` | AstrBot |
|---|---|---|---|
| `memory-layer-v0.1.0` | `v0.2.2` | `v0.2.0` | `>= 4.28.2` |

- 核心提炼 / 注入逻辑在 **v4.28.2 / v4.29.1** 上实测过。
- 「Embedding 控制台」这个可视化视图需要 **>= 4.29**；4.28.2 上配置页正常，视图不显示。
- 插件 tag 跟着各自 `metadata.yaml` 的 version 走；聚合 tag 每次发版递增，与插件版本解耦。

## 为什么分三层

一条平铺的记忆列表在长会话里只有两种结局：要么全塞进上下文（噪声淹掉信号），
要么全靠相似度取 top-N（「他一直住在哪」这种长期事实，会被当天一句无关的话挤掉）。

这套的切法不按相似度，按**这条记忆加不加日期才成立**：

```
state        界定双方「是什么关系」的：称呼 / 关系定位 / 陪伴承诺
             —— 长期且不依赖某天。每轮全量注入，不过闸门。

preference   长期成立的习惯、喜好、准则、环境事实 ——「总是这样」
event        具体某天发生的事，或有明确时限的事实 ——「那次这样」
             —— 这两层走检索 + 闸门，event 还要按时间递减。
```

判据交给模型自己判（`kind` 字段），代码做硬校验：不在枚举里就丢掉。
提炼提示词刻意不写角色身份 —— 换一个 bot 人设不用改插件。
具体流水线、闸门阈值、时间锚校验都在写入侧和注入侧的 README 里。

---

## tools/ —— 复核台

**没有它，前面两个插件会留一个永远清不掉的队列。**
`memory_digest` 写库时把新条目标成 `status="new"`，`memory_recall` 见到打 0.5 折；
来源对不上的条目标 `suspect=1`，`memory_recall` 见到直接不注入。两处都要人过目才能清，
这个工具就是那个「过目」的入口。

跑在**宿主机**上（不是容器里），零第三方依赖，纯 stdlib。

```
tools/
  review.py     真正干活的：读/写知识库，走框架自己的 vec_db 接口
  review.sh     包装层：读操作在跑着的容器里执行；写操作先停容器再改
  webui.py      本地网页，给 CLI 套一个能点的界面
  selftest.sh   在知识库【副本】上验一遍读改写全流程
  selftest_console.py  embedding 配置自测（假服务，不碰线上）
  selftest_plugin_docs.py  metadata / schema / i18n / 视图路由自测（需 4.29 代码）
```

### 命令行

```bash
cd tools
./review.sh list [--all | --sid 0001 | --kind event]   # 只读
./review.sh stats                                      # 只读，含三处一致性核对
./review.sh keep    0001 0002     # 转 ok + 清掉 suspect
./review.sh suspect 0003          # 标记存疑
./review.sh delete  0004 [--yes]  # 删除（doc.db / FTS / faiss 三处一起动）
./review.sh batch  < plan.json    # 一批一次做完，只停起一次容器
```

⚠️ **多个 sid 用空格分隔，不是逗号**（每个 sid 是独立参数）。sid 是 4 位零填充字符串、
精确匹配 —— `1` 匹配不上 `0001`。

### 为什么写操作要停容器

知识库是「doc.db + FTS 索引 + FAISS 向量」三份，框架自己有原子双写接口
（`vec_db.delete` / `vec_db.insert`）。两边同时写 `index.faiss` 会打架，所以写操作
= 停容器 → 用同一个镜像起一次性容器改 → 起回来。`review.sh` 挂了 `trap`，异常退出也会把容器起回来。

**一批多种操作要走 `batch`**：逐个跑 `keep` / `delete` 会各停起一次容器（每次约 40 秒），
复核一轮下来 bot 反复离线。

### 网页版

```bash
cd tools
python3 webui.py                            # 默认 127.0.0.1:8790
HOST=0.0.0.0 PORT=8790 python3 webui.py     # ⚠️ 见下
```

⚠️ **绑到 `0.0.0.0` 之前先想清楚**：这一页上就是记忆正文，能连到的人**既能读也能删**。
「在家里」不是安全边界（访客网络、物联网设备、手机丢了都算）。要绑非本机地址：
在 `~/.config/kb-review-web/token` 写一个口令（0600），口令存在就要求登录，
登录后发 `HttpOnly` cookie（一年）；另支持 `http://<host>:8790/?token=xxxx`
一次性链接直接在手机上打开。口令每次请求现读 —— 改口令不用重启服务。
明文 HTTP，只当局域网内措施用。

界面上：顶部两个筛选（待审 / 存疑 · 全部）+ 刷新 + 一致性检查；每条右边四个按钮
（通过 / 存疑 / 取消存疑 / 删除）；底部「应用」把选中项合成一个 batch 计划提交。
写入是**异步任务**（返回 `job_id`，页面每 1.5 秒轮询进度）—— 因为一次写入要 30~60 秒，
写完才返回的话中途刷新就收不到回执，页面报错而库里其实已经改完了。
页面加载时会先探一次任务状态，所以刷新能接上。

### 环境变量

```
KB_ROOT    知识库根目录     默认 ~/astrbot/data/knowledge_base
KB_NAME    知识库名字       默认 长期记忆   ★ 要和两个插件配置页的 kb_name 一致
REVIEW_SH  review.sh 路径   默认与本文件同目录
HOST/PORT  WebUI 监听        默认 127.0.0.1:8790
TOKEN_FILE 口令文件          默认 ~/.config/kb-review-web/token
```

### 自测

`selftest.sh` 在知识库的**副本**上真删一条、标一条存疑、再 keep 回来，全程不碰线上库：

```bash
cd tools
docker run --rm -v ~/astrbot/data:/AstrBot/data -v "$PWD":/kb:ro \
  soulter/astrbot:latest bash /kb/selftest.sh
```

⚠️ 脚本里默认的两个 sid 是占位值 —— 先 `./review.sh list` 抄两个真实的 4 位编号，
用 `SID_DELETE=… SID_SUSPECT=…` 传进去。

---

## 已知依赖与坑

- **rerank provider 是硬依赖。** 没有它，`recall` 每轮只注入状态层（会打
  `rerank provider 不可用`），偏好 / 事件层全部拿不到。插件不会替你配 rerank。
- **切换 embedding 前先看维度。** 维度不同会被直接拒绝；同维度换模型
  需要打开配置页的「允许同维度换模型」，否则旧记忆会检索异常。
  最稳妥的做法是新建一个知识库再切。
- **配置页 / 控制台保存会写 AstrBot 全局配置。** 插件会创建 / 更新
  `memory_layer_embedding` 这个 provider。之后卸载插件，provider 不会自动删除，
  需要到 WebUI 提供商页手动清理。
- **配置页 / 控制台保存会触发热重载。** 加载时后台探测 endpoint；如果 endpoint
  不可达，只打错误日志，不会让插件加载失败，旧绑定保持不变。
- **知识库条目没有「编辑」接口。** 框架的 WebUI API 只有 POST / DELETE。
  要改一条已写入的记忆，正确做法是走 `vec_db.delete(chunk_id)` +
  `vec_db.insert(new_text, metadata, id=chunk_id)` 双写 ——
  直接改 `doc.db` 里的 text 只会动三处中的一处（FTS 和 FAISS 还是旧的）。
- **进度状态文件损坏会重建**（打一行 warning），代价是那一轮重扫。
- **`conversations` 表是只读打开的**（`mode=ro`）。如果上游插件接管了历史写入，
  要确认它写进去的 role 是规整交替的 —— 尾巴上挂着两条连续的 `user`，
  提炼出来的记忆质量会明显变差。
- **人称字段是硬约定。** 两个插件的 `USER_REF` / `SELF_REF` 必须一致；
  库里的旧条目如果是别的写法，切字段之后就检索不回来。
- 代码来自一个长期在跑的自用部署，注释里保留了踩坑原因和实测数字，
  请当"经验笔记 + 可用代码"看。

## License

MIT
