# astrbot-memory-layer

一条**完整的长期记忆管线**：**写入（提炼）→ 注入（召回）→ 复核（人审）**。

> 当前分支：`feature/embedding-console`
> 在已有 `main` 的记忆层基础上，按 AstrBot 新版插件文档补了插件配置、
> 元数据与**插件可视化视图「Embedding 控制台」**。

| 组件 | 作用 |
|---|---|
| [`astrbot_plugin_memory_digest`](astrbot_plugin_memory_digest) | 把对话提炼成 bot 的第一人称记忆写进知识库；自带 Embedding 控制台 |
| [`astrbot_plugin_memory_recall`](astrbot_plugin_memory_recall) | 接管记忆注入：状态层全量、偏好/事件层过闸门、事件按时间递减 |
| [`tools/`](tools) | 复核台：命令行 + 本地 WebUI，用来清掉待审条目和存疑条目 |

核心提炼/注入逻辑在 AstrBot v4.28.x 上长期跑过；控制台要求 **>= 4.29**，
并在 **v4.29.1** 上验证（4.28 上插件照常加载，只是没有控制台视图）。用的是框架自带的
知识库（embedding + FAISS）和一个 rerank provider。**两个插件必须一起装**，
`KB_NAME` 常量也要一致。

> **前置**：这套东西只管「记什么」和「什么时候拿出来」，不管「怎么存」。
> 存储、向量、检索都交给 AstrBot 的知识库。所以先建一个知识库
> （WebUI → 知识库）。embedding 可以在插件控制台里配；rerank 仍要在知识库设置里配。

---

## 为什么要分三层

一条平铺的记忆列表，在长会话里只有两种结局：要么全塞进上下文（噪声淹掉信号），
要么全靠相似度取 top-N（「他一直住在哪」这种长期事实，会被当天一句无关的话挤掉）。

这套的切法不是按「相似度」，是按**这条记忆加不加日期才成立**：

```
state        界定双方「是什么关系」的：称呼 / 关系定位 / 陪伴承诺
             —— 长期且不依赖某天。每轮全量注入，不过闸门。

preference   长期成立的习惯、喜好、准则、环境事实 ——「总是这样」
event        具体某天发生的事，或有明确时限的事实 ——「那次这样」
             —— 这两层走检索 + 闸门，event 还要按时间递减。
```

判据交给模型自己判（`kind` 字段），但**代码做硬校验**：`kind` 不在枚举里就丢掉
（不是兜底成 `event` —— 模型判不出来说明它根本没读懂这条，留着是噪声）。

---

## 1. astrbot_plugin_memory_digest —— 写入侧

### 流水线

```
读 conversations 表 → 渲染成带时间锚的文本 → 交给 LLM 按 DIGEST_SYSTEM 提炼
  → 逐行解析 JSONL（时间锚 / kind / confidence 三项硬校验）
  → bge-m3 余弦去重（对库里已有的 + 对本批内部的，阈值 0.85）
  → 来源校验（闸门①）→ 写进知识库
```

### 几个关键设计

**① 进度按「会话 → 已处理消息数」记，只处理新增部分。**
状态文件在 `data/memory_digest_state.json`。不用每次重扫全库。

**② 独立 session_id 调模型**，不污染主对话的上下文。

**③ 时间锚只能照抄，不能推算。**
每条记忆前面带 `[YYYY-MM-DD HH:MM]`。这个时间来自上游写在 user 消息里的
`<system_reminder>` —— 模型只负责**从给定的锚里原样抄一个**，抄了不在本批对话里的
时间就整条丢掉（`bad_anchor`）。模型很爱自己编日期，这一条拦掉的是最常见的一类假记忆。

> 时间锚是**可选依赖**。本仓库的 [`astrbot-reply-rhythm`](https://github.com/Lansheen3096/astrbot-reply-rhythm)
> 里的 `pacer` 就负责把「收到消息的那一刻」写进 user 消息。没有它也能跑，
> 只是条目不带日期，`event` 层的时间衰减也就无从算起。

**④ 来源校验（闸门①）—— 拦「模型编的记忆」。**
写库之后，拿条目正文去和「它声称来源那一轮用户说过的话」算句余弦；低于阈值就打
`metadata.suspect = 1`，注入侧一律不放行。

阈值是实测校准的，不是拍的：

```
真来源轮   0.573 ~ 0.856
合成的编造句 0.543 ~ 0.629
```

两条分布**有重叠**，所以这一闸只拦「明显对不上」的。语言上确实出现在那一轮、
但其实是模型自己的叙述被当成事实那类，它拦不住 —— 那类靠
`status="new"` 标记待人眼复核（注入时会降权 0.5）。

**⑤ 硬过滤词表**（`BLOCK_WORDS`）。
提示词里写「不许记账号、报错、配置项」，模型会选择性忽略；代码过滤不会。
命中即不写。

**⑥ 提炼提示词刻意不写角色身份。**
`DIGEST_SYSTEM` 只描述任务（提炼哪三类、`kind` 怎么判、输出什么格式），
不提任何具体角色的设定与来历 —— 那部分属于你的部署，用你自己的 system prompt 去管。
换一个 bot 人设不用改这个插件。

**⑦ 人称一律用【字段】，不用代词。**
两个插件在文件顶部声明**同一对**常量，取值必须一致：

```
USER_REF = "[用户]"    用户
SELF_REF = "我"        助手自己
```

对话记录的说话人标签、写进库里的记忆正文、检索时归一后的查询 —— 三处用的是同一套字面量。
没有性别，也没有「你 / 他 / 她到底指谁」的歧义，模型不用做任何人称翻译。

代词方案（写「他/她」）在这里是走不通的：标签上并列写可以，但归一是要**参与检索的字面量**，
库里的正文写的是「他…」或「她…」中的一个，查询里塞并列写法两头都不像。换成固定字段之后
这个矛盾直接消失 —— 两边天然是同一个字符串。

⚠️ **迁库注意**：换人称字段等于换了一套字面量。库里已有的条目如果是用「他/我」这类写法
存的，改了字段之后它们就检索不回来了 —— 要么继续保持原来那套写法，要么清库重跑一次提炼。

### 指令

```
/记住        手动跑一次提炼
/记住状态    看进度（处理到哪了、库里有多少条）
```

另有每天 05:00 的自动整理（`DAILY_HOUR` / `DAILY_MINUTE`）。

---

## 2. astrbot_plugin_memory_recall —— 注入侧

### 为什么接管框架自带的注入

框架自带那条链是 `dense 50 + sparse 50 → RRF → rerank → top N` 一坨平铺，
做不到「状态全量 / 偏好过闸 / 事件时间递减」的分层。所以把知识库的
`kb_names` 置空关掉自带注入，由本插件接管。

`on_llm_request` 的 hook 点在 `build_main_agent()` **之后**，顺序上本来就是最后一道 ——
所以能拿到最终 prompt，也能把注入块挂上去。

### 闸门判据

```
地板 FLOOR   top1 ≥ 0.05         实测不相关的噪声区在 0.001 ~ 0.03
领先 LEAD    top1 ≥ LEAD × top2  避免「一堆都不相关、只是有一个略高」时误放
```

**为什么不直接用绝对高分（比如 0.75）：**
20 条真实消息打 48 条真实条目，top1 中位数 0.013、最大 0.29 —— ≥0.75 是 0/20。
rerank 分衡量的是「这条记忆是否真的答上了这句话」，它的绝对值和直觉不在一个量级上。
所以判据用**相对领先**，不用绝对高分。

放行之后**逐条还要过一次地板** —— 否则 top1 勉强过闸时，会把 0.02 的噪声一起带进去。

`state` 层不走闸门（它本来就该每轮都在），只用 `STATE_MAX` 兜个底。

### 时间递减

`event` 层的分数乘一个半衰期衰减：`0.5 ** (天数 / HALFLIFE_DAYS)`，
默认半衰期 30 天，且有 `DECAY_FLOOR` 兜底（不会被压到 0）。
`preference` 和 `state` **不衰减** —— 它们本来就是「不依赖某天」才被分到这一层的。

### 查询预处理

拿去打分的不是整段 prompt，而是**纯代码清洗**出来的那一句用户输入：
去括号壳（动作描写）、去尾标点、人称归一、补主语。
零延迟，实测有效：

```
相关对 top1   0.29 → 0.84
相关对 top1   0.12 → 0.60
```

人称归一是可配置的：消息里对 bot 的称呼（`ADDRESS_WORDS`，默认空）
先统一换掉再打分 —— 称呼词对检索没有信息量，只会稀释相似度。
归一的目标是 `USER_REF` / `SELF_REF`，理由和取值见上面第 ⑦ 条。

⚠️ 函数里几处 `re.sub` 的第二个参数有两种，别看混：`""` 是**整块删掉**（它本身就是噪声），
`" "` 是**换成空格** —— 后者不能删，删了会把左右两个词粘成一个新词，反而污染检索。

### 注入形态

```
【你们现在的状态｜每轮都给】
- …（state 层全量）

【这轮可能用得上的回忆】
- …（过闸的 preference + event）
```

整块挂在 `req.extra_user_content_parts` 上并 `mark_as_temp()` —— **临时注入，
不写进历史**，否则下一轮它会被当成用户说过的话再检索一次，越滚越脏。
总长有 `MAX_CHARS` 上限。

---

## 3. 插件控制台 —— Embedding 来源

写入侧插件带一个可视化视图 `views/embedding-console/`。入口在 AstrBot WebUI 的
插件页 →「记忆层 · 写入侧」详情页 → **Embedding 控制台**。

它解决的是「配 embedding 要改 AstrBot 全局配置 / 记不住 provider 名字」这件事。
控制台里选一种来源，测试连通性，保存时会：

1. 在 AstrBot 里创建或原地更新一个固定 ID 的 embedding provider
   （`memory_layer_embedding`）；
2. 把记忆知识库（`KB_NAME`，默认「长期记忆」）的 `embedding_provider_id`
   指向它；
3. 重新初始化知识库的向量库，并用一条测试文本回读向量，确认维度一致；
4. 保存插件配置 `data/config/astrbot_plugin_memory_digest_config.json`。

### 三种来源

```
跟随知识库   不改任何绑定，记忆层直接用知识库当前的 embedding provider。
云端 API     OpenAI 兼容的 /v1/embeddings：填 API Base、API Key、模型。
             维度留 0 会自动探测。
本机服务     填监听地址 + 端口，接口类型二选一：
             · Ollama         → POST {base}/api/embed
             · OpenAI 兼容    → POST {base}/v1/embeddings
             适合 Ollama / TEI / vLLM / LM Studio / llama.cpp 等本机服务。
```

维度不用手算：点「测试连接」会真的发一条 `memory layer connectivity test`，
把返回向量的长度显示出来；保存时以实际探测值为准。

### 安全闸门（重要）

知识库里的旧向量和新 embedding 必须在同一个向量空间，否则不会报错、只会
**静默检索错乱**。所以控制台在写绑定之前会检查：

- **维度不一致 → 直接拒绝。** 例：现有索引 1024 维，新服务返回 768 维。
  请新建知识库，或清空旧条目后再切换。
- **模型名不一致 → 默认拒绝**，需要勾选「强制切换（同维度）」。
  不同模型即使都是 1024 维，向量空间也不一样。
- **知识库不存在** → 默认自动创建；如果 AstrBot 里有可用的 rerank provider，
  会一并挂上，否则提示你补一个（注入侧没有 rerank 时只会注入状态层）。

API Key 只写入本机配置，`/status` 接口只返回 `api_key_set` 和末四位掩码，
不回显明文；控制台保存时留空表示保持原 Key。

### 容器部署的地址坑

`127.0.0.1` 在 AstrBot 容器里指的是**容器自己**，不是宿主机。本机 embedding 服务
跑在宿主机上时，填 `host.docker.internal`；如果 Docker 不支持这个主机名，
用 `ip route` 查网桥地址（Linux 上通常是 `172.17.0.1` 或你自定义的网桥网关）。
控制台里也写了这条提示。

### 权限边界

控制台接口走 AstrBot Dashboard 的插件鉴权，只有已登录的管理员能用。它按设计可以
向任意填写的地址发测试请求（包括内网/本机端口），所以 **6185 面板不要直接暴露公网**；
要远程管理就走 Tailscale / 反代加认证。

### 后端接口

| 方法 | 路由 | 作用 |
|---|---|---|
| GET | `/<plugin>/status` | 知识库、provider、条目数、待整理会话、当前设置 |
| POST | `/<plugin>/test` | 只探测端点，不写配置、不改绑定 |
| POST | `/<plugin>/save` | 保存设置，创建/更新 provider 并绑定知识库 |

视图通过 `window.AstrBotPluginView` bridge 调用，路由注册用
`context.register_web_api()`，请求/响应走 `astrbot.api.web` 的
`request` / `json_response` / `error_response` —— 都是插件开发文档里的新接口。

### 配置项

`_conf_schema.json` 里保存了同一组值（`embedding_mode`、`cloud_*`、`local_*`、
`embedding_timeout`），所以在插件配置页也能改；控制台只是更好用的入口。
`embedding_mode = kb` 时控制台不碰知识库绑定。

### 自测

`tools/` 里有两个自测脚本，都在 AstrBot 容器里跑、不碰线上配置：

```bash
# 用假 embedding 服务验证探测、维度/模型闸门、自动建库
docker run --rm \
  -v "$PWD/astrbot_plugin_memory_digest":/plugin:ro \
  -v "$PWD/tools":/tools:ro \
  soulter/astrbot:latest python /tools/selftest_console.py

# 用框架自己的加载函数验证 metadata / _conf_schema / i18n / views / 路由
# （需要 AstrBot >= 4.29 的代码；4.28 镜像只有 pages/，会失败）
docker run --rm \
  -v "$PWD/astrbot_plugin_memory_digest":/plugin:ro \
  -v "$PWD/tools":/tools:ro \
  -e PLUGIN_DIR=/plugin \
  <AstrBot-4.29-image> python /tools/selftest_plugin_docs.py
```

### 进度文件迁移

旧版本把进度写在 `data/memory_digest_state.json`。新版本按文档要求放到
`data/plugin_data/astrbot_plugin_memory_digest/state.json`；第一次加载时
如果新文件不存在、旧文件存在，会自动复制过去，不会重扫历史。

---

## 4. tools/ —— 复核台

**没有它，前面两个插件会留一个永远清不掉的队列。**
`memory_digest` 写库时把新条目标成 `status="new"`，`memory_recall` 见到就打 0.5 折；
来源对不上的条目会标 `suspect=1`，`memory_recall` 见到直接不注入。两处都要人过目才能清，
这个工具就是那个「过目」的入口。

跑在**宿主机**上（不是容器里），零第三方依赖，纯 stdlib。

```
tools/
  review.py     真正干活的：读/写知识库，走框架自己的 vec_db 接口
  review.sh     包装层：读操作在跑着的容器里执行；写操作先停容器再改
  webui.py      本地网页，给 CLI 套一个能点的界面
  selftest.sh   在知识库【副本】上验一遍读改写全流程
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

⚠️ **多个 sid 用空格分隔，不是逗号**（每个 sid 是独立的参数）。sid 是 4 位零填充的字符串、
精确匹配 —— `1` 匹配不上 `0001`。

### 为什么写操作要停容器

知识库是「doc.db + FTS 索引 + FAISS 向量」三份，框架自己有原子双写接口
（`vec_db.delete` / `vec_db.insert`）。两边同时写 `index.faiss` 会打架，所以写操作
= 停容器 → 用同一个镜像起一次性容器改 → 起回来。`review.sh` 挂了 `trap`，异常退出也会把容器起回来。

**一批多种操作要走 `batch`**：逐个跑 `keep`/`delete` 会各停起一次容器（每次约 40 秒），
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

界面上：顶部两个筛选（待审/存疑 · 全部）+ 刷新 + 一致性检查；每条右边四个按钮
（通过 / 存疑 / 取消存疑 / 删除）；底部「应用」把选中项合成一个 batch 计划提交。
写入是**异步任务**（返回 `job_id`，页面每 1.5 秒轮询进度）—— 因为一次写入要 30~60 秒，
写完才返回的话中途刷新就收不到回执，页面报错而库里其实已经改完了。
页面加载时会先探一次任务状态，所以刷新能接上。

### 环境变量

```
KB_ROOT    知识库根目录     默认 ~/astrbot/data/knowledge_base
KB_NAME    知识库名字       默认 长期记忆   ★ 要和两个插件的 KB_NAME 一致
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

## 安装

```bash
git clone https://github.com/Lansheen3096/astrbot-memory-layer
cd astrbot-memory-layer
git switch feature/embedding-console      # 控制台在这个分支
cp -r astrbot_plugin_* <AstrBot>/data/plugins/
docker compose restart astrbot
```

控制台需要 AstrBot >= 4.29（`views/`、`register_web_api`、`astrbot.api.web`）；
4.28 上核心提炼/注入仍可用，只是没有控制台。装好后：

1. 在 WebUI 里建好知识库（默认名 `长期记忆`，两个插件的 `KB_NAME` 常量要对得上）。
2. 打开「记忆层 · 写入侧」→「Embedding 控制台」，选云端或本机，测试、保存。
3. 给知识库配一个 rerank provider（控制台只管 embedding，不管 rerank）。

如果知识库不存在，控制台默认会自动创建；也可以在保存前取消勾选
「知识库不存在时自动创建」，自己先建好。

## 已知依赖与坑

- **rerank provider 是硬依赖。** 没有它，`recall` 每轮只注入状态层（会打
  `rerank provider 不可用`），偏好/事件层全部拿不到。控制台不会替你配 rerank。
- **切换 embedding 前先看维度。** 维度不同会被控制台直接拒绝；同维度换模型
  需要勾选「强制切换」，否则旧记忆会检索异常。最稳妥的做法是新建一个知识库再切。
- **控制台会写 AstrBot 全局配置。** 保存时会创建/更新 `memory_layer_embedding`
  这个 provider。之后卸载插件，provider 不会自动删除，需要到 WebUI 提供商页手动清理。
- **知识库条目没有「编辑」接口。** 框架的 WebUI API 只有 POST / DELETE。
  要改一条已写入的记忆，正确做法是走 `vec_db.delete(chunk_id)` +
  `vec_db.insert(new_text, metadata, id=chunk_id)` 双写 ——
  直接改 `doc.db` 里的 text 只会动三处中的一处（FTS 和 FAISS 还是旧的）。
- **进度状态文件损坏会重建**（打一行 warning），代价是那一轮重扫。
- **`conversations` 表是只读打开的**（`mode=ro`）。如果上游插件接管了历史写入，
  要确认它写进去的 role 是规整交替的 —— 尾巴上挂着两条连续的 `user`，
  提炼出来的记忆质量会明显变差。
- 本仓库代码来自一个长期在跑的自用部署，注释里保留了踩坑原因和实测数字，
  请当"经验笔记 + 可用代码"看。

## License

MIT
