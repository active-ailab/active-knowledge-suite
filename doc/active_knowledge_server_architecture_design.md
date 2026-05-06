# Active Knowledge Server 架构与方案设计

> 文档状态：Draft for Review  
> 调研日期：2026-04-27  
> 适用对象：`active-knowledge-server`  
> 相关文档：
> - [RTOS 多仓嵌入式工程知识库方案（对话汇总版）](./rtos_engineering_kb_plan_summary.md)
> - [面向大型 RTOS 项目的知识库 MCP + Skill 全案设计（正式版）](./rtos_engineering_kb_mcp_skill_full_design.md)
> - [Workspace Map](./active-knowledge-workspace-map.md)

---

## 1. 文档目标

本文面向 `active-knowledge-server` 给出一份可落地的工程设计。它不是重复上层全案，而是把“外部知识库系统 + FastMCP 接口层 + Skill 调用入口”具体下钻到 server 的目录、配置、数据库、索引流水线、查询接口和部署策略。

本文重点回答：

- 首次部署时如何配置 Active 工程路径
- 源码分发部署时 server 默认工作目录如何确定
- 如何在源码分发中携带可复用索引基线，避免用户首次部署从零建库
- 工作目录中应创建哪些数据库、缓存和索引文件
- 源码顶层如何统一管理需要进入知识库的源文档
- 如何同时索引完整项目代码、API 文档、控件文档，以及未来的产品和项目文档
- 代码查询如何支持不同颗粒度和不同理解视角
- FastMCP 工具、资源和未来 Skill 调用接口如何设计
- V1 如何在没有完整 `compile_commands.json` 的情况下启动，后续如何演进到编译感知代码 RAG

---

## 2. 行业实践调研结论

### 2.1 RAG 不是“向量库 + 切块”就够

2026 年的 RAG 实践已经非常明确：检索质量决定最终回答质量。行业和论文中的共同趋势是：

- 精确关键词检索、稀疏检索和向量检索要混合使用
- 初召回之后需要 rerank，提高证据精度
- 对带 ID、宏名、函数名、错误码、寄存器名、路径名的技术问题，BM25/FTS 往往比纯向量更可靠
- 对自然语言问题、同义表达、概念解释，向量检索仍然重要
- 评测需要覆盖 Recall、MRR、nDCG、证据命中率和端到端回答质量

Qdrant 和 LanceDB 的实践都把 hybrid search 与 reranking 作为高质量检索的重要形态。2026 年一篇 RAG 检索基准论文也将 sparse、dense、hybrid fusion、cross-encoder reranking、query expansion 等方法放在同一框架下评测，说明生产级 RAG 必须可实验、可度量，而不是只固定一种召回路径。

对 Active 的直接结论：

- 代码事实不能主要靠 embedding
- API、控件、产品、项目文档需要 hybrid retrieval
- 检索链路必须能解释“为什么这些证据被召回”
- 要从 V1 起保留评测集和查询日志

### 2.2 代码知识库必须以结构化索引为主

C/C++ 工程理解依赖编译上下文。clangd 文档明确指出，解释源代码需要 include path、宏、target、语言模式等编译参数；clangd index 也将 Symbol、Ref、Relation 作为核心数据对象。

Tree-sitter 的行业定位更适合多语言语法树抽取和轻量代码结构理解。它能构建代码语法树，适合脚本、配置、汇编、非完整编译单元等资产。

对 Active 的直接结论：

- `compile_commands.json` 或等价编译数据库是中长期目标
- V1 可以先用 `module.mk`、`Config.in`、`defconfig`、`.config`、路径规则、ctags、Tree-sitter 建立结构化底座
- C/C++ 的最终高置信符号和引用关系应由 clang/clangd-compatible index 提供
- 代码 embedding 只能用于“语义补召回”和“模块摘要召回”，不能取代 symbol/ref/graph 查询

### 2.3 MCP Server 应暴露稳定能力，不应暴露底层存储

MCP 官方 Python SDK 示例展示了 server 可以暴露 tools、resources、prompts，并支持 `streamable-http` transport。FastMCP 文档进一步说明：

- tools 适合包装可调用能力
- resources 适合暴露只读数据和动态内容
- type annotations / Pydantic schema 能让参数和返回值结构化
- tool annotations 可标记 read-only、destructive、idempotent 等行为
- structured output 能让客户端拿到机器可处理的 JSON

对 Active 的直接结论：

- 普通知识查询工具必须是 read-only
- 运维索引工具要与普通查询工具分组或默认关闭
- MCP 返回结构必须稳定，方便 Skill 固化路由
- 资源 URI 用于读取当前配置、snapshot、profile、doc section、evidence bundle 等只读上下文

### 2.4 本地源码分发更适合 local-first 存储

本项目通过源码分发和部署，并且要求默认工作目录可以放在源码顶层。行业上 local-first RAG 通常采用 SQLite、FTS5、LanceDB、Qdrant Local、Chroma、sqlite-vec 等嵌入式或本地文件型方案。

SQLite FTS5 是内置全文检索能力，适合本地元数据、文档块和代码文本检索。SQLite Vec1 在 2026 年已经提供 SQLite 内的 ANN 向量检索扩展，但仍处在较新阶段。LanceDB 支持本地目录数据库、全文检索、向量检索和 reranker，适合 Python 本地部署快速起步。

对 Active 的直接结论：

- V1 默认采用 `SQLite + FTS5 + LanceDB` 的本地文件型组合
- local-first 存储目录应位于源码仓库顶层，并支持随源码/release 分发一份只读索引基线
- 用户本机增量索引写入 local overlay，避免首次部署长时间全量建库
- 团队共享或大规模场景再切换到 `PostgreSQL/pgvector`、`Qdrant` 或独立搜索引擎
- 存储层必须抽象，不能把 MCP 工具直接绑定到某一种数据库实现

### 2.5 MCP 安全要求必须前置

MCP Streamable HTTP 规范要求 HTTP server 校验 `Origin`，本地运行建议只绑定 `127.0.0.1`，连接应有认证。MCP 社区也在推进 interceptors，用于在 tool call、resource read、prompt、sampling 等上下文操作点做校验、审计和转换。

对 Active 的直接结论：

- V1 本地 HTTP 默认只监听 `127.0.0.1`
- 远程 HTTP 必须启用 token 或 OAuth/OIDC 适配
- 所有工具调用写审计日志，但不记录大段源码全文
- 高风险运维工具默认不对 Skill 暴露

---

## 3. 需求收敛

### 3.1 必须满足的输入数据

V1 必须支持：

- 完整项目代码
- API 文档
- 控件使用文档
- 手工补充的知识源文档

V2+ 预留支持：

- 产品需求、功能说明、版本说明
- 项目计划、里程碑、风险、Issue、决策记录
- 设计规范、交互说明、screen flow、design token
- 测试说明、缺陷知识、发布和运维经验

### 3.2 代码查询颗粒度

必须支持以下颗粒度：

- workspace：顶层工程结构、子仓、主要 area
- directory：目录职责、目录下模块族
- build module：`module.mk`、`Config.in`、Kconfig、defconfig 关联
- file：文件职责、关键符号、所属 profile
- symbol：函数、宏、类型、变量、枚举、错误码、注册表项
- function/block：函数内核心逻辑、调用、条件编译、锁/队列/事件操作
- flow：调用链、消息链、初始化链、ISR/task/timer/queue 运行链路
- feature/domain：运动、健康、通知、UI、设备驱动等业务或技术域
- profile diff：不同 defconfig、`.config`、board、app 之间的差异

### 3.3 代码查询理解视角

必须支持以下视角：

- workspace 视角：它在哪、属于哪个 area、由哪个子仓或目录承载
- layer 视角：driver、engine、framework、uiframework、widget、service、app 的分层关系
- domain 视角：某个领域能力由哪些 service、engine、framework、UI 共同组成
- feature 视角：用户功能如何落到 app、presenter、screen、service、widget
- runtime 视角：任务、ISR、队列、timer、事件、启动链和 fault 传播
- profile 视角：某能力在哪些 defconfig/profile 下启用或裁剪
- evidence 视角：结论可追溯到哪些文件、符号、文档章节和配置

### 3.4 部署与目录要求

必须满足：

- 使用 Python 开发，兼顾 Linux/macOS/Windows
- 使用 FastMCP 作为 MCP 框架
- 首次部署可配置 Active 工程目录路径
- 源码分发部署
- 默认工作目录放在源码目录顶层，也允许配置其他路径
- 默认源文档目录和 local-first 存储目录均放在源码仓库顶层
- 分发包可携带已构建好的索引基线，用户只需配置本地项目路径并执行增量索引
- 工作目录中创建数据库、索引、缓存、日志等运行时文件
- 源码目录顶层创建统一的源文档目录，用于管理需要记忆的文档
- 预留稳定接口供 Skill 调用

---

## 4. 总体架构

### 4.1 一句话方案

`active-knowledge-server` 是一个本地优先、配置驱动、编译感知逐步增强的 Active 知识查询服务。它用 Python 实现核心索引和查询能力，用 FastMCP 暴露稳定的 MCP tools/resources，并在源码仓库顶层通过 `baseline + local overlay` 保存可分发索引基线、本机增量索引、缓存和索引任务状态。

### 4.2 架构图

```mermaid
flowchart TD
    A[Active Workspace<br/>source / configs / build outputs] --> B[Source Connectors]
    C[knowledge-sources<br/>api / widgets / product / project / design] --> B

    B --> D[Index Pipeline]
    D --> D1[Snapshot Collector]
    D --> D2[Profile Collector]
    D --> D3[Code Indexer]
    D --> D4[Doc Indexer]
    D --> D5[Relation Extractor]
    D --> D6[Embedding Builder]

    D1 --> E[Storage Layer]
    D2 --> E
    D3 --> E
    D4 --> E
    D5 --> E
    D6 --> E

    E --> E1[SQLite baseline + overlay<br/>entities / relations / FTS]
    E --> E2[LanceDB baseline + delta<br/>doc/card/module embeddings]
    E --> E3[Artifacts<br/>snapshots / reports / local cache]

    E --> F[Query Service]
    F --> F1[Query Router]
    F --> F2[Hybrid Retriever]
    F --> F3[Symbol Resolver]
    F --> F4[Graph Expander]
    F --> F5[Reranker]
    F --> F6[Evidence Packager]

    F --> G[FastMCP Facade]
    G --> G1[MCP Tools]
    G --> G2[MCP Resources]

    H[Active Skill] --> G
    I[Agent / IDE / ChatGPT MCP Client] --> G
```

### 4.3 核心分层

| 层级 | 职责 | 不做什么 |
| --- | --- | --- |
| Config Layer | 管理工程路径、工作目录、文档源、profile、存储后端 | 不直接执行查询 |
| Connector Layer | 接入源码、文档、build 输出、未来外部系统 | 不保存业务索引 |
| Index Pipeline | 抽取 snapshot、profile、代码结构、文档块、关系、embedding | 不直接服务 MCP |
| Storage Layer | 保存元数据、FTS、向量索引、任务状态和 evidence | 不暴露给 Skill |
| Query Service | 聚合 symbol/FTS/vector/graph，输出稳定证据包 | 不处理 MCP 协议细节 |
| FastMCP Facade | 暴露 tools/resources，做参数校验和结构化返回 | 不写复杂业务逻辑 |
| Skill Interface | 固化工具路由、问题分类、证据化回答模板 | 不保存大量动态事实 |

### 4.4 分层分模块展开设计

为了便于后续实现、评审和任务拆分，server 内部建议按“稳定领域服务 + 可替换适配器”的方式分层。每一层只依赖下方抽象接口，不反向依赖 MCP、CLI 或具体存储实现。

| 分层 | 模块 | 主要职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| 接入与配置层 | `config` | 加载/校验配置、合并 CLI/env/yaml、生成运行上下文 | CLI/env/yaml | `RuntimeConfig`、`ProjectConfig` |
| 接入与配置层 | `security/path_guard` | 路径 allowlist、symlink 检查、敏感路径拦截 | 用户参数、配置路径 | 安全的规范化路径 |
| Source 层 | `connectors/workspace` | 扫描 Active workspace、repo、目录、文件 | `workspace_root`、include/exclude | `FileInventory`、`RepoMap` |
| Source 层 | `connectors/source_docs` | 扫描 `knowledge-sources` 与外部 doc roots | 文档根目录 | `SourceDocManifest` |
| Source 层 | `connectors/build_outputs` | 发现 `.config`、defconfig、compile DB、map file | build 输出目录 | `BuildArtifactManifest` |
| 解析层 | `parsers/*` | 将源码、文档、配置转成统一 AST/section/item | 文件内容 | `ParseResult` |
| 抽取层 | `indexing/*` | 抽取实体、关系、chunk、evidence、embedding 任务 | `ParseResult`、profile | `EntityBatch`、`RelationBatch`、`ChunkBatch` |
| 持久化层 | `storage/*` | 保存元数据、FTS、向量、任务状态、迁移 | 批量写入请求 | 可查询索引和状态 |
| 查询层 | `query/*` | 意图分类、召回、融合、图扩展、证据打包 | 用户查询、profile | `QueryResult`、`EvidenceBundle` |
| 协议门面 | `mcp/*` | FastMCP tools/resources、schema 校验、tool annotation | MCP call | 结构化 MCP 响应 |
| 运维层 | `indexing/jobs`、`eval`、`security/audit` | 索引任务、评测回归、审计、健康检查 | ops 命令、日志 | job 状态、报告、指标 |

模块边界要求：

- `mcp/*` 只能调用 `query/*` 和受控的 `indexing/jobs.py`
- `query/*` 只能依赖 `storage/base.py` 抽象接口，不直接依赖 SQLite/LanceDB 细节
- `indexing/*` 可以依赖 parser、connector、storage，但不能依赖 MCP schema
- `parsers/*` 不写数据库，只产出可测试的中间结构
- `connectors/*` 不做复杂理解，只负责发现、读取和标准化 source manifest
- `models/*` 放跨层共享的数据契约，避免每层定义一套相似 JSON

### 4.5 模块依赖规则

```mermaid
flowchart TD
    CLI[CLI / Bootstrap] --> Config[Config Layer]
    CLI --> Jobs[Index Job Orchestrator]

    Config --> Connectors[Source Connectors]
    Config --> Storage[Storage Adapters]
    Config --> Query[Query Service]

    Connectors --> Parsers[Parsers]
    Parsers --> Extractors[Index Extractors]
    Extractors --> Storage
    Jobs --> Connectors
    Jobs --> Parsers
    Jobs --> Extractors
    Jobs --> Storage

    Storage --> Query
    Query --> MCP[FastMCP Facade]
    MCP --> Client[Skill / Agent / IDE]

    Security[Path Guard / Auth / Audit] -. cross-cutting .-> CLI
    Security -. cross-cutting .-> Jobs
    Security -. cross-cutting .-> Query
    Security -. cross-cutting .-> MCP
```

依赖方向应保持单向：

- `Config -> Connector -> Parser -> Extractor -> Storage -> Query -> MCP`
- CLI 可以编排 init/index/serve，但不承载核心业务逻辑
- Security、Audit、Eval 是横切能力，通过接口注入，不让业务层散落重复实现

### 4.6 关键运行边界

| 边界 | 约束 |
| --- | --- |
| Source of truth 边界 | 原始事实来自 Active workspace、build outputs、`knowledge-sources` 和已配置外部源；派生知识不能覆盖原始证据 |
| 写入边界 | 只有 indexing/jobs/migrations 写数据库；普通 query 和 MCP read tools 不写索引 |
| 证据边界 | MCP 默认返回短摘录、路径、行号和 hash，不返回大段源码全文 |
| Profile 边界 | 所有代码关系、模块可达性和配置影响分析都必须能绑定 snapshot/profile，缺失时给 warning |
| 存储边界 | SQLite/LanceDB 是 V1 默认实现，MCP schema 和 Skill 调用契约不能依赖具体后端 |
| 安全边界 | 任何文件读取都经过 path guard；远程 HTTP 默认不暴露 ops tools |

---

## 5. 源码目录与工作目录设计

### 5.1 源码目录建议

推荐源码顶层为 `active-knowledge/`，server 工程位于：

```text
active-knowledge/
  doc/
  knowledge-sources/
  .active-kb/
    baseline/
    local/
  active-knowledge-server/
  active-knowledge-graph/
  active-knowledge-workspace/
```

其中：

- `knowledge-sources/` 是随源码仓库分发的源文档目录，用于沉淀 API、控件、工程、产品、设计、项目等已整理资料
- `.active-kb/baseline/` 是随源码仓库或 release artifact 分发的 local-first 索引基线，用于让用户拿到分发后直接复用已有索引
- `.active-kb/local/` 是用户本机运行时 overlay，用于保存本地 workspace 路径绑定、增量索引、job 状态、cache、logs、locks 等本机数据
- `active-knowledge-server/` 保存 server 代码，不直接混入文档源或索引数据

这一布局的目标是让用户首次拿到分发后只需要配置本地 Active 项目路径，即可基于已存在的索引基线启动查询，再按需执行增量索引，而不是在首次部署时从零构建完整索引。

### 5.2 `active-knowledge-server/` 代码结构建议

```text
active-knowledge-server/
  pyproject.toml
  README.md
  src/
    active_knowledge_server/
      __init__.py
      cli.py
      server.py
      bootstrap.py
      config/
        loader.py
        schema.py
        defaults.py
      mcp/
        app.py
        tools.py
        resources.py
        schemas.py
        annotations.py
      indexing/
        pipeline.py
        jobs.py
        snapshot.py
        profile.py
        code_indexer.py
        doc_indexer.py
        relation_extractor.py
        embeddings.py
      connectors/
        workspace.py
        source_docs.py
        build_outputs.py
        future_issues.py
        future_design.py
      parsers/
        markdown.py
        api_docs.py
        widget_docs.py
        kconfig.py
        makefiles.py
        ctags.py
        tree_sitter.py
        clang_index.py
      storage/
        base.py
        sqlite_store.py
        lancedb_store.py
        migrations/
      query/
        service.py
        router.py
        retrievers.py
        symbol_resolver.py
        graph.py
        rerank.py
        evidence.py
      models/
        ids.py
        entities.py
        evidence.py
        query.py
        responses.py
      security/
        path_guard.py
        secret_scan.py
        auth.py
        audit.py
      eval/
        cases.py
        runner.py
        metrics.py
  tests/
  examples/
    active-kb.yaml
```

设计约束：

- `mcp/` 只包装 `query/` 和少量 `indexing/jobs.py` 能力
- `query/` 不依赖 FastMCP
- `storage/` 不依赖 MCP schema
- `indexing/` 能离线运行，也能由 ops 工具触发
- 所有路径统一通过 `config/` 和 `path_guard.py` 管控

### 5.3 默认 local-first 存储目录

源码分发部署时，默认 local-first 存储根目录为源码仓库顶层：

```text
active-knowledge/.active-kb/
```

该目录不应被理解为“纯临时运行目录”，而是同时承担两类职责：

| 子目录 | 职责 | 是否随分发携带 |
| --- | --- | --- |
| `.active-kb/baseline/` | 可复用索引基线、文档向量、workspace map、评测基线、索引报告 | 推荐携带 |
| `.active-kb/local/` | 本机配置 overlay、增量索引、jobs、cache、logs、locks、tmp | 不推荐携带 |

可通过以下方式覆盖：

- 环境变量：`ACTIVE_KB_WORKDIR=/path/to/workdir`
- 配置文件字段：`runtime.workdir`
- CLI 参数：`active-kb --workdir /path/to/workdir ...`

优先级：

```text
CLI 参数 > 环境变量 > 配置文件 > 源码顶层默认值
```

但即使允许覆盖，V1 推荐默认使用源码仓库顶层 `.active-kb/`，原因是：

- 分发包可以内置已构建好的 baseline index，用户无需首次长时间全量索引
- `knowledge-sources/` 与索引基线版本可以一起管理，避免“文档版本和索引版本不匹配”
- 用户本地只需要绑定 `project.workspace_root`，再对本地代码差异执行增量索引
- baseline 可作为团队统一评测和排障的共同基准

### 5.4 存储目录内容

```text
.active-kb/
  README.md
  baseline/
    manifest.json
    config/
      baseline.yaml
      profiles/
        mhs003_watch.yaml
        mhs003_sensorhub.yaml
    db/
      metadata.db
    vectors/
      lancedb/
    artifacts/
      snapshots/
      profile-manifests/
      workspace-maps/
      index-reports/
      eval-baseline/
  local/
    config/
      active-kb.local.yaml
    db/
      overlay.db
      jobs.db
    vectors/
      lancedb-delta/
    artifacts/
      local-index-reports/
    cache/
      parsed/
      embeddings/
      rerank/
    logs/
      server.log
      indexer.log
      audit.log
    tmp/
    locks/
```

说明：

- `baseline/manifest.json` 记录 baseline 的构建时间、源码/文档版本、schema version、embedding model version、snapshot/profile 清单
- `baseline/db/metadata.db` 保存可分发的结构化实体、关系、FTS 和 evidence 元数据
- `baseline/vectors/lancedb/` 保存可分发的向量集合
- `baseline/artifacts/` 保存可读的快照报告、profile 报告、workspace map 和评测基线
- `local/config/active-kb.local.yaml` 保存用户本机 Active workspace 路径、默认 profile、transport 等本地覆盖配置
- `local/db/overlay.db` 保存用户本机增量索引产生的新实体、关系、chunk 和 evidence
- `local/db/jobs.db` 保存索引任务、队列、进度和错误
- `local/vectors/lancedb-delta/` 保存增量向量，不直接改写 baseline 向量库
- `local/cache/`、`local/tmp/`、`local/locks/` 可删除后重建
- `local/logs/audit.log` 记录工具调用和敏感操作，不记录大段源码正文

查询时的读路径为：

```text
local overlay -> baseline index -> source files / source docs
```

写路径为：

```text
local overlay only
```

除非执行明确的 `publish-baseline` 或 release 构建流程，普通用户的增量索引不应直接修改 `.active-kb/baseline/`。

版本管理建议：

- `knowledge-sources/` 应进入源码仓库正常版本管理
- `.active-kb/baseline/manifest.json`、baseline 配置和小型报告建议进入源码仓库
- `.active-kb/baseline/db/metadata.db` 和 `.active-kb/baseline/vectors/` 如果体积较大，建议通过 Git LFS 或 release artifact 分发
- `.active-kb/local/` 应加入 `.gitignore`，只保留必要的 `.gitkeep` 或 README
- `.active-kb/local/logs/`、`tmp/`、`locks/` 永远不应进入源码仓库

### 5.5 源码顶层源文档目录

推荐在源码顶层创建：

```text
knowledge-sources/
  README.md
  api/
  widgets/
  engineering/
  product/
  design/
  project/
  qa/
  release/
  learned-seeds/
```

目录职责：

| 目录 | 用途 |
| --- | --- |
| `api/` | Active API 文档、SDK 文档、接口说明 |
| `widgets/` | 控件使用文档、UI 组件示例、约束说明 |
| `engineering/` | 架构说明、编码规范、调试指南、FAQ |
| `product/` | 产品需求、功能说明、版本差异 |
| `design/` | UI 规范、交互流、screen flow、design token 导出 |
| `project/` | 项目计划、里程碑、风险、决策记录 |
| `qa/` | 测试策略、缺陷归因、验证清单 |
| `release/` | 发布说明、兼容性、变更记录 |
| `learned-seeds/` | 人工整理的种子知识卡、历史专题图谱、回归样本 |

这部分文档属于“需要记忆的源文档”。它们不是 Skill 的静态正文，而是知识库的 source of truth 之一。

推荐将 `knowledge-sources/` 作为源码仓库的一等顶层目录进行版本管理，并随源码分发一起提供。这样用户拿到分发后可以直接使用已经整理好的 API、控件、工程 FAQ、产品/项目资料；当文档更新时，release 流程应同步重建 `.active-kb/baseline/`，保证“源文档版本”和“索引基线版本”一致。

---

## 6. 首次部署与配置方案

### 6.1 推荐首次部署流程

```bash
cd /path/to/active-knowledge
uv sync
uv run active-kb init \
  --workspace /home/gangan/Active \
  --workdir ./.active-kb \
  --source-docs ./knowledge-sources \
  --reuse-baseline

uv run active-kb index --incremental --profile auto
uv run active-kb serve --transport stdio
```

如果分发包已经包含 `.active-kb/baseline/`，`init` 阶段应优先校验并复用该基线。只有在 baseline 缺失、schema 不兼容或用户显式指定 `--full` 时，才需要执行全量索引。

如果接入本地 HTTP：

```bash
uv run active-kb serve --transport streamable-http --host 127.0.0.1 --port 8765
```

### 6.2 `init` 命令职责

`active-kb init` 应完成：

- 校验 workspace 路径存在
- 校验源码顶层、`knowledge-sources/` 和 `.active-kb/` 目录
- 创建或补齐 `.active-kb/local/` 目录结构
- 检查 `.active-kb/baseline/manifest.json`
- 校验 baseline schema version、embedding model version、source docs hash、snapshot/profile 清单
- 创建初始 `active-kb.yaml` 和 `local/config/active-kb.local.yaml`
- 校验 baseline SQLite schema
- 初始化 local overlay SQLite schema 和 LanceDB delta 目录
- 发现 `configs/`、`build/.config`、`build/out_hub/.config`
- 扫描 `knowledge-sources/` 并生成源文档清单
- 判断是否需要增量索引，并输出下一步建议命令

`init` 对 baseline 的处理策略：

| 场景 | 行为 |
| --- | --- |
| baseline 存在且兼容 | 直接复用 baseline，只创建 local overlay |
| baseline 存在但 schema 过旧 | 尝试 migration；失败时提示重新获取或重建 baseline |
| baseline 存在但 source docs hash 不匹配 | warning，并建议执行 `index --incremental --docs` |
| baseline 不存在 | 创建空 baseline 引用，提示执行 `index --full` 或下载 baseline artifact |
| 用户本地 workspace 与 baseline snapshot 不一致 | 允许启动，但后续查询返回 snapshot/profile warning |

### 6.3 配置文件位置

推荐主配置文件：

```text
.active-kb/baseline/config/baseline.yaml
```

推荐本机 overlay 配置：

```text
.active-kb/local/config/active-kb.local.yaml
```

允许源码顶层放一个轻量入口配置：

```text
active-kb.yaml
```

入口配置只保存仓库内目录位置和环境选择：

```yaml
runtime:
  workdir: ./.active-kb
  baseline_config: ./.active-kb/baseline/config/baseline.yaml
  local_config: ./.active-kb/local/config/active-kb.local.yaml
```

配置合并优先级：

```text
CLI 参数 > 环境变量 > local_config > baseline_config > 内置默认值
```

### 6.4 主配置草案

```yaml
deployment_mode: local_single_user

server:
  name: active-knowledge-server
  transport: stdio
  expose_ops_tools: false
  http:
    host: 127.0.0.1
    port: 8765
    mcp_path: /mcp
    require_auth: false
    auth_provider: none
    allowed_origins:
      - http://127.0.0.1
      - http://localhost
    trust_reverse_proxy: false

runtime:
  source_root: /home/gangan/GANLab/MCP/active-knowledge
  workdir: /home/gangan/GANLab/MCP/active-knowledge/.active-kb
  baseline_dir: ${runtime.workdir}/baseline
  local_dir: ${runtime.workdir}/local
  source_docs_root: /home/gangan/GANLab/MCP/active-knowledge/knowledge-sources
  log_level: info

project:
  id: active
  display_name: Active
  workspace_root: /home/gangan/Active
  branch_strategy: baseline-first
  baseline_branch: main
  default_snapshot: current
  default_profile: auto

paths:
  include:
    - application
    - components
    - configs
    - core
    - drivers
    - framework
    - packages
    - platform
    - resources
    - ui
    - uiframework
  exclude:
    - .git
    - build/out
    - build/tmp
    - "**/.cache/**"
    - "**/node_modules/**"
    - "**/__pycache__/**"

profiles:
  discovery:
    defconfig_roots:
      - configs
    dotconfig_candidates:
      - build/.config
      - build/out_hub/.config
  known:
    - id: mhs003_watch
      dotconfig: build/.config
      app: watch
      board: mhs003
    - id: mhs003_sensorhub
      dotconfig: build/out_hub/.config
      app: sensorhub
      board: mhs003

storage:
  baseline:
    manifest: ${runtime.baseline_dir}/manifest.json
  metadata:
    backend: sqlite
    path: ${runtime.baseline_dir}/db/metadata.db
    mode: readonly
  overlay:
    backend: sqlite
    path: ${runtime.local_dir}/db/overlay.db
  jobs:
    backend: sqlite
    path: ${runtime.local_dir}/db/jobs.db
  vector:
    backend: lancedb
    path: ${runtime.baseline_dir}/vectors/lancedb
    mode: readonly
  vector_delta:
    backend: lancedb
    path: ${runtime.local_dir}/vectors/lancedb-delta
  artifacts_root: ${runtime.baseline_dir}/artifacts
  local_artifacts_root: ${runtime.local_dir}/artifacts
  cache_root: ${runtime.local_dir}/cache

indexing:
  mode: local
  incremental: true
  reuse_baseline: true
  write_target: local_overlay
  workers: auto
  code:
    enable_full_code_scan: true
    enable_ctags: true
    enable_tree_sitter: true
    enable_clang_index: false
    compile_db_candidates:
      - build/compile_commands.json
      - build/out_hub/compile_commands.json
  docs:
    enable_markdown: true
    enable_html: true
    enable_pdf: false
  embeddings:
    enabled: true
    provider: local
    model: bge-m3
    batch_size: 32
  learned_cards:
    enabled: false
    require_review: true

query:
  default_top_k: 12
  max_evidence_items: 20
  hybrid:
    enable_fts: true
    enable_vector: true
    enable_symbol: true
    enable_graph_expand: true
    rerank: lightweight
  evidence_required: true

security:
  path_allowlist:
    - ${project.workspace_root}
    - ${runtime.source_docs_root}
    - ${runtime.workdir}
    - ${runtime.baseline_dir}
    - ${runtime.local_dir}
  secret_scan:
    enabled: true
    deny_patterns:
      - "AKIA[0-9A-Z]{16}"
      - "-----BEGIN PRIVATE KEY-----"
  audit:
    enabled: true
```

`profiles.discovery.dotconfig_candidates` 只定义候选来源，不代表自动选择。`default_profile=auto` 的确定性解析、`.config` 可信候选判定和多候选返回格式见附录 C。

### 6.5 部署模式与配置样式

`deployment_mode` 是启动安全策略的第一层开关，必须为以下枚举之一：

| mode | 使用场景 | 默认 transport | HTTP host 规则 | auth/origin/audit | ops tools |
| --- | --- | --- | --- | --- | --- |
| `local_single_user` | 单人本机 IDE/Agent 使用。 | `stdio` | 若启用 HTTP，只允许 `127.0.0.1`、`::1` 或 `localhost`。 | 本地 loopback HTTP 可不认证；audit 默认开启。 | 默认不暴露，可由本机配置显式打开。 |
| `remote_shared` | 团队共享服务、远程 Agent、网关后服务。 | `streamable-http` | 可绑定非 loopback，但必须通过 fail-safe 启动校验。 | `require_auth=true`；`allowed_origins` 必填且禁止 `*`；audit 必须开启。 | 默认不暴露；V1 不允许远程暴露 ops tools。 |

完整示例文件：

- `examples/local-single-user.yaml`
- `examples/remote-shared.yaml`

配置合并后再执行安全校验。CLI 参数可以覆盖 transport、host、port，但不能绕过 `deployment_mode` 的 fail-safe 规则。

### 6.6 Baseline 构建与发布流程

`baseline` 不应由每个普通用户各自维护，而应由维护者或 CI 在标准环境中构建并随源码/release 分发。

推荐流程：

```text
update knowledge-sources
  -> run full index on standard Active workspace
  -> run eval regression
  -> generate baseline manifest
  -> package .active-kb/baseline
  -> publish source/release artifact
```

推荐命令形态：

```bash
uv run active-kb index --full --target baseline --profile all
uv run active-kb eval run --baseline
uv run active-kb baseline publish \
  --manifest ./.active-kb/baseline/manifest.json
```

发布检查项：

- `knowledge-sources/` hash 已写入 baseline manifest
- SQLite schema、parser、extractor、embedding model 版本已写入 manifest
- baseline 中至少包含 V1 默认 profile 和默认文档域
- eval 关键指标未低于上一个发布基线
- `.active-kb/local/` 未被打入 release artifact
- 大体积 `metadata.db`、`vectors/` 根据团队策略走 Git LFS 或 release artifact

---

## 7. 数据存储设计

### 7.1 V1 默认存储选型

| 数据类型 | 默认方案 | 理由 |
| --- | --- | --- |
| 元数据基线 | `.active-kb/baseline/db/metadata.db` | 单文件、跨平台、可随源码/release 分发，避免首次全量建库 |
| 元数据增量 | `.active-kb/local/db/overlay.db` | 只记录用户本机增量，避免污染 baseline |
| 全文检索 | SQLite FTS5 | 内置全文检索，适合代码片段、文档块、API 文档；baseline 和 overlay 可分别维护 |
| 向量基线 | `.active-kb/baseline/vectors/lancedb/` | 本地目录数据库，可随分发携带文档和摘要向量 |
| 向量增量 | `.active-kb/local/vectors/lancedb-delta/` | 用户本机新增/变更 chunk 的向量增量 |
| 原始证据 | 源文件路径 + hash + 可选 normalized copy | 避免复制完整工程，保留 source of truth |
| 任务状态 | `.active-kb/local/db/jobs.db` | 方便恢复、查询和调试，属于本机运行状态 |
| 报告产物 | baseline artifacts + local artifacts | baseline 便于分发评审，local 便于用户排障 |

### 7.2 V2 可替换后端

| 场景 | 可选方案 |
| --- | --- |
| 团队共享服务 | PostgreSQL + pgvector |
| 大规模向量检索 | Qdrant |
| 搜索工程复杂化 | OpenSearch / Elasticsearch |
| 全部 local-first 且希望单文件向量 | SQLite Vec1 / sqlite-vec |
| 多租户权限治理 | PostgreSQL + 独立对象存储 |

必须通过 `storage/base.py` 定义存储接口，禁止 MCP tool 直接操作 SQLite SQL。

### 7.3 核心表设计

#### `source`

记录所有输入源。

| 字段 | 说明 |
| --- | --- |
| `source_id` | 稳定 ID |
| `source_type` | `workspace` / `source_doc` / `build_output` / `external` |
| `root_path` | 根路径 |
| `domain` | engineering/product/design/project 等 |
| `authority_level` | 权威级别 |
| `created_at` | 创建时间 |

#### `snapshot`

记录一次工程快照。

| 字段 | 说明 |
| --- | --- |
| `snapshot_id` | 快照 ID |
| `workspace_root` | Active 工程路径 |
| `baseline_branch` | 基线分支 |
| `git_head` | 顶层或主仓 commit |
| `repo_manifest_hash` | 子仓映射摘要 |
| `created_at` | 快照时间 |
| `status` | indexing/ready/failed |

#### `profile`

记录 defconfig/profile。完整 identity key、`auto` 选择和多候选行为见附录 C。

| 字段 | 说明 |
| --- | --- |
| `snapshot_id` | 所属 snapshot |
| `profile_id` | profile ID |
| `profile_record_id` | 基于 `snapshot_id + profile_id + defconfig_hash + dotconfig_hash` 的物理记录 ID |
| `board` | board |
| `app` | watch/sensorhub 等 |
| `defconfig_path` | defconfig 路径 |
| `defconfig_hash` | defconfig 内容 hash |
| `dotconfig_path` | `.config` 路径 |
| `dotconfig_hash` | `.config` 内容 hash |
| `config_hash` | defconfig、`.config` 和归一化宏摘要的综合 hash |
| `macro_summary_json` | 关键宏摘要 |
| `source` | explicit/local_config/dotconfig_scan/baseline_default 等来源 |
| `priority` | 多候选排序权重 |
| `last_used_at` | 最近使用时间，用于候选排序和诊断 |
| `status` | resolved/stale/invalid/unresolved 等状态 |

#### `file`

记录代码和文档文件。

| 字段 | 说明 |
| --- | --- |
| `file_id` | 文件 ID |
| `snapshot_id` | 所属快照 |
| `source_id` | 来源 |
| `path` | 绝对路径或 source-root 相对路径 |
| `rel_path` | 展示用相对路径 |
| `language` | c/cpp/h/asm/md/html/yaml 等 |
| `content_hash` | 内容 hash |
| `mtime` | 文件时间 |
| `workspace_area` | drivers/framework/packages/ui 等 |
| `module_id` | 构建模块 |

#### `chunk`

记录可检索块。

| 字段 | 说明 |
| --- | --- |
| `chunk_id` | chunk ID |
| `file_id` | 文件 |
| `chunk_type` | doc_section/code_symbol/code_block/module_summary |
| `title` | 标题 |
| `start_line` | 起始行 |
| `end_line` | 结束行 |
| `text` | 规范化文本 |
| `token_count` | 估算 token |
| `embedding_ref` | 向量库引用 |

#### `entity`

统一实体表。

| 字段 | 说明 |
| --- | --- |
| `entity_id` | 实体 ID |
| `entity_type` | Symbol/Module/Directory/API/Widget/Feature/Requirement 等 |
| `name` | 名称 |
| `qualified_name` | 全限定名 |
| `snapshot_id` | 快照 |
| `profile_scope` | all 或 profile 列表 |
| `file_id` | 主文件 |
| `chunk_id` | 主 chunk |
| `metadata_json` | 扩展属性 |

#### `relation`

统一关系表，承担轻量图数据库职责。

| 字段 | 说明 |
| --- | --- |
| `relation_id` | 关系 ID |
| `src_entity_id` | 起点 |
| `dst_entity_id` | 终点 |
| `relation_type` | calls/contains/defines/guarded_by_macro 等 |
| `snapshot_id` | 快照 |
| `profile_id` | 可空 |
| `condition_expr` | 宏条件或配置条件 |
| `confidence` | 置信度 |
| `evidence_id` | 证据 |

#### `evidence`

证据引用表。

| 字段 | 说明 |
| --- | --- |
| `evidence_id` | 证据 ID |
| `evidence_type` | code/doc/config/build |
| `file_id` | 文件 |
| `start_line` | 起始行 |
| `end_line` | 结束行 |
| `excerpt` | 短摘录 |
| `hash` | 证据内容 hash |
| `authority_level` | 权威级别 |

#### FTS 表

建议建立：

- `chunk_fts(chunk_id, title, text, rel_path, symbols, tags)`
- `entity_fts(entity_id, name, qualified_name, aliases, summary)`
- `doc_fts(chunk_id, doc_type, title, text)`
- `code_fts(chunk_id, symbol_names, comments, code_text)`

### 7.4 ID 设计

ID 要稳定、可复现，便于增量索引和 Skill 引用。

建议：

```text
file_id     = sha1(snapshot_id + rel_path)
chunk_id    = sha1(file_id + chunk_type + start_line + end_line + content_hash)
entity_id   = sha1(snapshot_id + entity_type + qualified_name + primary_location)
evidence_id = sha1(file_id + start_line + end_line + excerpt_hash)
```

### 7.5 Baseline + Overlay 读写模型

为了支持“分发即带索引基线”，V1 存储层应采用两段式读写模型。
完整一致性契约见附录 B；本节只描述主路径。

读路径：

```text
query -> local overlay -> baseline -> source evidence
```

写路径：

```text
incremental index -> local overlay
full baseline build / release pipeline -> baseline
```

合并规则：

- `overlay` 中同 ID 的实体、chunk、relation 优先于 `baseline`
- `overlay` 可以记录 tombstone，用于屏蔽本地已删除或已变更的 baseline 对象
- 查询结果需要标记 `source_index=baseline|overlay|merged`
- evidence 引用仍指向原始源码、源文档或 normalized copy，不指向临时缓存
- `baseline` 默认只读，只有发布人员或 CI 执行 `publish-baseline` 时才允许写入

`baseline/manifest.json` 建议字段：

```json
{
  "baseline_id": "active-main-20260427-bge-m3-v1",
  "created_at": "2026-04-27T00:00:00Z",
  "source_docs_hash": "sha256:...",
  "schema_version": "1.0.0",
  "parser_version": "1.0.0",
  "embedding_model": "bge-m3",
  "embedding_model_version": "1.0",
  "snapshots": ["current"],
  "profiles": ["mhs003_watch", "mhs003_sensorhub"],
  "artifacts": {
    "metadata": "db/metadata.db",
    "vectors": "vectors/lancedb",
    "workspace_map": "artifacts/workspace-maps/current.json"
  }
}
```

---

## 8. 索引流水线设计

### 8.1 流水线阶段

```text
discover -> snapshot -> profile -> scan -> parse -> extract -> chunk -> fts -> embed -> graph -> report
```

| 阶段 | 输入 | 输出 |
| --- | --- | --- |
| discover | 配置文件 | workspace、source docs、build outputs 清单 |
| snapshot | Git/repo/文件 hash | snapshot 记录、repo map |
| profile | defconfig/.config | profile 记录、宏摘要 |
| scan | include/exclude 规则 | file inventory |
| parse | 文件内容 | AST/heading/API/widget 结构 |
| extract | parse result | entities、relations、evidence |
| chunk | 文件结构 | code chunks、doc sections |
| fts | chunks/entities | FTS index |
| embed | selected chunks | vector collection |
| graph | relations | view projection |
| report | 全部状态 | index report、workspace map |

### 8.2 V1 无 compile DB 的索引策略

当前 Active 工作区尚不能假设有完整 `compile_commands.json`。因此 V1 采用“结构化优先、编译感知预留”的路线。

V1 先抽取：

- 顶层 area：`application`、`components`、`drivers`、`framework`、`packages`、`ui`、`uiframework` 等
- 子仓和目录 map
- `module.mk` 构建模块
- `Config.in` / Kconfig 选项
- `configs/*defconfig`
- `build/.config` 与 `build/out_hub/.config`
- C/C++/H 文件中的函数、宏、类型、include、注释
- UI、service、engine、framework、driver 的路径语义
- API 和控件文档的章节结构

V1 不承诺：

- 100% 精确 C/C++ 引用关系
- 全量跨 translation unit 类型推断
- 所有条件编译分支的真实可达性

V1 应通过 `warnings` 明确提示“未启用 clang index”或“缺少 compile DB”。

### 8.3 V2 编译感知增强

当 build 系统可以导出编译数据库时启用：

- `compile_commands.json`
- clang/clangd-compatible index
- include path、macro、target triple、toolchain 参数
- 头文件归属推断
- symbol definition/reference 归并
- profile 级可达性过滤

### 8.4 文档索引策略

#### API 文档

抽取：

- API 名称
- 所属模块
- 函数签名
- 参数
- 返回值
- 错误码
- 示例代码
- 版本和适用 profile
- 对应源码符号候选

#### 控件使用文档

抽取：

- 控件名
- 所属 UI 框架或 widget family
- 使用场景
- 属性、事件、生命周期
- 样式和资源限制
- 示例
- 对应 UI 代码路径候选

#### 产品和项目文档

预留抽取：

- requirement/feature ID
- 产品范围
- 版本范围
- 关联 UI screen
- 关联 service/domain
- 里程碑、风险、Issue
- 决策记录

### 8.5 Chunk 设计

代码 chunk：

- function chunk：函数定义为基本单位
- macro chunk：宏定义及附近注释
- type chunk：struct/enum/class/typedef
- file header chunk：文件头注释、include、关键宏
- module summary chunk：由索引器生成的模块摘要

文档 chunk：

- heading section：按 Markdown/HTML heading 切分
- API item：每个 API 单独 chunk
- widget item：每个控件单独 chunk
- table row group：参数表、属性表、错误码表按逻辑组切分
- learned card：人工或系统生成的知识卡

Chunk 原则：

- 小块用于精确召回
- 大块用于上下文补全
- 每个 chunk 必须能回到文件和行号
- 文档 chunk 要保留标题路径，例如 `API > Sensor > getHeartRate`

### 8.6 增量索引

增量判断依据：

- 文件 path
- content hash
- mtime
- snapshot_id
- profile config hash
- parser version
- embedding model version

重新索引规则：

- 文件 hash 变更：重建该文件 chunks/entities/relations
- `.config` 变更：重建 profile 和 profile-conditioned relations
- parser version 变更：重建对应语言解析结果
- embedding model 变更：重建向量集合
- source docs 变更：重建相关 doc chunks 和 doc vectors

`.config`/defconfig 变化的表级重算清单见附录 C.7；parser/extractor、build rule、compile DB、baseline snapshot 和 embedding model 等全量重算边界见附录 C.8。

在 baseline + overlay 模式下，增量索引默认只写 `.active-kb/local/`：

- baseline 中未变化的 chunk/entity/relation 直接复用
- 本地新增或变化的对象写入 `overlay.db` 和 `lancedb-delta`
- 本地删除的对象以 tombstone 形式写入 overlay，查询时屏蔽 baseline 旧对象
- source docs 变化时，优先写入 local overlay；只有 release 流程才重建 baseline
- 当用户希望把本机增量沉淀为团队基线时，走单独的 `publish-baseline` 或 CI release 流程

---

## 9. 查询与 RAG 设计

### 9.1 查询总流程

```text
normalize query
  -> classify intent
  -> select view and granularity
  -> retrieve candidates from symbol/FTS/vector/graph
  -> fuse and rerank
  -> expand evidence
  -> package result with warnings
```

### 9.2 查询意图分类

| intent | 典型问题 | 首选检索 |
| --- | --- | --- |
| `code_exact` | 函数、宏、错误码、文件路径在哪 | symbol + FTS |
| `code_concept` | 某机制如何实现 | FTS + module summary + graph |
| `call_trace` | A 到 B 怎么调用 | symbol + relation graph |
| `runtime_flow` | ISR/task/queue/timer 链路 | relation graph + pattern extractor |
| `profile_diff` | 两个 board 差异 | profile + macro + FTS |
| `api_lookup` | API 怎么用 | doc FTS + vector + symbol link |
| `widget_lookup` | 控件怎么用 | widget doc + UI path + examples |
| `workspace_nav` | 该去哪看 | workspace map + path role |
| `product_context` | 功能范围/需求背景 | product docs + feature map |
| `project_context` | 版本/风险/里程碑 | project docs + issue source |
| `evidence_lookup` | 结论依据、出处、证据包 | evidence graph + original source |
| `unknown` | 意图不足或上下文缺失 | hybrid recall + warning |

### 9.3 多路召回

必须实现四类召回器：

- `SymbolRetriever`：函数、宏、类型、文件、模块精确查找
- `FullTextRetriever`：SQLite FTS5，适合技术词、路径、错误码、API 名称
- `VectorRetriever`：LanceDB，适合自然语言描述、同义表达、文档语义召回
- `GraphRetriever`：从实体扩展 calls/defines/contains/guarded_by_macro 等关系

### 9.4 Fusion 与 rerank

V1 建议：

- 对代码精确问题，提高 symbol/FTS 权重
- 对 API/控件文档问题，FTS 和 vector 权重接近
- 对产品/项目问题，提高文档向量和 metadata 过滤权重
- 使用 RRF 或加权归一化做初步融合
- 使用轻量 reranker 或规则 reranker 做最终排序

V2 可选：

- cross-encoder reranker
- late interaction reranker
- query-specific adaptive weights
- 基于历史点击/采纳的重排

### 9.5 证据包设计

所有查询返回都必须包含 evidence。

```json
{
  "summary": "简短结论",
  "query_intent": "api_lookup",
  "snapshot_id": "current",
  "profile_id": "mhs003_watch",
  "entities": [],
  "relations": [],
  "evidence_refs": [
    {
      "evidence_id": "ev_...",
      "type": "doc",
      "path": "knowledge-sources/api/sensor.md",
      "start_line": 12,
      "end_line": 40,
      "authority_level": "source_doc",
      "excerpt": "短摘录"
    }
  ],
  "warnings": [],
  "next_queries": []
}
```

### 9.6 不同颗粒度查询的实现方式

| 颗粒度 | 实现 |
| --- | --- |
| workspace | snapshot + workspace map artifact + directory entities |
| directory | directory entity + child files/modules + doc links |
| module | build module entity + file set + Config.in/module.mk evidence |
| file | file entity + symbol list + FTS chunks |
| symbol | symbol entity + definition/declaration/ref relation |
| function | function chunk + calls/refs + profile conditions |
| flow | graph traversal + evidence bundle |
| feature | feature entity + domain/service/app/ui projection |
| profile | profile table + macro diff + guarded relations |

### 9.7 不同视角的投影视图

统一关系图中保留所有实体和关系，查询时投影为不同视图。

| 视图 | 主要节点 | 主要关系 |
| --- | --- | --- |
| workspace | WorkspaceArea/Directory/Repo/File | contains/belongs_to |
| layer | Driver/Engine/Framework/UIFramework/Widget/Service/App | belongs_to_layer/implements/consumes |
| domain | Domain/ServicePackage/EngineComponent/API | belongs_to_domain/depends_on |
| feature | Feature/AppPackage/UIScreen/Presenter/Widget | implements_feature/renders_widget |
| runtime | Task/ISR/Queue/Semaphore/Timer/Fault | posts_to/waits_on/triggers |
| profile | Profile/Macro/Module/File/Symbol | enabled_by/guarded_by_macro |
| evidence | Evidence/File/DocSection/Entity | supports/derived_from |

### 9.8 RAG 标准章节补全清单

一份可评审的 RAG 方案不应只描述“切块、向量库、检索”。Active Knowledge Server 的 RAG 设计至少需要覆盖以下章节，并在后续实现文档或 README 中保持对应关系。

| 章节 | 本文对应位置 | 必须回答的问题 |
| --- | --- | --- |
| 知识范围与权威源 | 3.1、5.5、12.1 | 哪些内容进入知识库，哪个来源优先级最高 |
| 数据接入与同步 | 5、6、8.1、8.6 | 文档、源码、配置、build outputs 如何接入，如何增量更新 |
| 数据治理与元数据 | 7.3、12、13.4 | 每条知识的 domain、owner、version、authority、freshness 如何记录 |
| 文档解析与结构化抽取 | 8.4、12 | API、控件、产品、项目文档如何解析成结构化对象 |
| 代码解析与结构化索引 | 8.2、8.3、11 | 符号、模块、profile、运行链路如何索引 |
| Chunk 策略 | 8.5 | chunk 边界、大小、标题路径、行号、证据回溯如何设计 |
| Embedding 策略 | 6.4、8.1、9.3 | 哪些内容向量化，使用什么模型，如何版本化和重建 |
| 检索策略 | 9.1 至 9.4 | query 如何分类，多路召回如何融合和重排 |
| 上下文组装 | 9.9 | top evidence 如何去重、压缩、排序，并传给 Agent |
| 答案生成策略 | 9.10、10.6 | server 返回证据包，Skill/Agent 如何基于证据回答 |
| 引用与可追溯性 | 9.5、13.4 | 每个结论如何回到文件、章节、符号、配置和 profile |
| 冲突与不确定性处理 | 9.11 | 多来源冲突、缺索引、缺 compile DB 时如何给 warning |
| 验收门槛与回归 | 14 | 如何衡量 Recall、MRR、nDCG、Evidence Hit Rate、Warning Quality 和 release gate |
| 反馈闭环 | 9.12、14 | 用户采纳、误召回、漏召回如何转成评测样本或索引改进 |
| 安全与权限 | 13 | 路径、敏感信息、远程访问、审计如何控制 |
| 运维与成本 | 15、18 | 索引任务、缓存、日志、模型成本、降级策略如何管理 |

### 9.9 上下文组装策略

RAG 的上下文组装由 Query Service 完成第一阶段，Skill/Agent 完成第二阶段。

Query Service 负责：

- 对同一文件、同一 symbol、同一 doc section 的重复候选去重
- 按 `authority_level`、`retrieval_score`、`profile_match`、`freshness_ts` 综合排序
- 将代码、文档、配置证据分组，避免单一来源淹没其他关键证据
- 对相邻行号或同一标题下的证据做小范围合并
- 控制 evidence 数量和总文本长度，默认返回短摘录和引用，不返回完整长文
- 保留每条证据的召回来源，例如 `symbol`、`fts`、`vector`、`graph`、`manual_seed`

Skill/Agent 负责：

- 根据用户问题选择是否继续调用 `code_context`、`code_trace`、`docs_search`
- 基于 evidence bundle 组织最终回答
- 明确区分“证据中能确认的事实”和“基于证据的推断”
- 在证据不足时请求补充检索，而不是直接生成确定结论

建议的 evidence 排序规则：

```text
final_score =
  retrieval_score * intent_weight
  + authority_boost
  + profile_match_boost
  + recency_boost
  + graph_proximity_boost
  - conflict_penalty
  - stale_penalty
```

V1 可以用规则评分实现，V2 再引入学习型 reranker 或基于反馈的权重调整。

### 9.10 答案生成与引用策略

`active-knowledge-server` 的 V1 定位是“证据服务”和“查询服务”，不强制内置 LLM 生成最终长答案。最终回答通常由 Skill/Agent 生成，但 server 必须提供足够稳定的回答上下文。

回答策略建议：

- 代码定位类问题：先给确定位置，再给所属模块、profile 条件和相邻关系
- API/控件使用类问题：先给用法结论，再给参数/约束/示例证据，最后给对应代码候选
- 机制解释类问题：先给高层链路，再列关键节点和证据，不把推断写成事实
- profile 差异类问题：必须列出比较对象、宏差异、受影响模块和 warning
- 产品/项目上下文类问题：必须展示文档版本、状态和权威源，避免把过期计划当当前事实

引用要求：

- 每段关键结论至少绑定一个 `evidence_ref`
- 生成回答中的文件路径应使用相对路径和行号
- 引用学习后知识卡时，也必须同时引用其 `derived_from` 原始证据
- 当证据之间冲突时，回答应保留冲突并说明权威源排序

### 9.11 不确定性、冲突与降级策略

常见不确定性需要显式返回 `warnings`：

| 场景 | warning | 降级策略 |
| --- | --- | --- |
| 缺少 `compile_commands.json` | C/C++ 引用关系为启发式结果 | 使用 symbol/FTS/Tree-sitter，降低 relation confidence |
| 未指定 profile | profile 过滤可能不准确 | 使用 default profile，并返回可选 profile 列表 |
| 文档版本缺失 | 文档时效无法判断 | 降低 freshness 分，优先 official/front matter 完整文档 |
| 多个符号同名 | 需要 disambiguation | 返回候选列表和所属模块 |
| 文档与代码冲突 | source conflict | 代码事实优先，文档作为意图或用法参考 |
| embedding 不可用 | semantic recall disabled | 退化为 symbol + FTS + graph |
| reranker 不可用 | rerank disabled | 使用 RRF 或规则融合 |

### 9.12 反馈闭环

为了让 RAG 质量能持续提升，V1 起就应记录轻量反馈数据：

- 查询原文、intent、profile、top evidence、耗时、warning
- 用户或 Skill 标记的“有用/无用证据”
- 未命中的目标文件、symbol、doc section
- 多轮追问中最终被采纳的 evidence
- 被人工整理进 `learned-seeds/` 的知识卡来源

反馈数据的用途：

- 生成或更新 `eval/cases.yaml`
- 调整 query intent 分类规则
- 调整 hybrid fusion 权重
- 发现需要补充 front matter 或人工知识卡的文档
- 识别 parser/extractor 的系统性漏抽取

---

## 10. MCP 与 Skill 接口设计

### 10.1 MCP 设计原则

- 默认工具只读
- 工具数量控制在 LLM 可稳定使用的范围内
- 运维工具带 `ops_` 前缀，默认可通过配置关闭
- 返回 Pydantic 结构化对象
- 每个工具都有 `warnings` 和 `evidence_refs`
- 工具描述中明确何时使用，避免 Skill 路由混乱

### 10.2 FastMCP Server 初始化

推荐结构：

```python
from fastmcp import FastMCP

mcp = FastMCP(
    name="active-knowledge-server",
    strict_input_validation=False,
)
```

工具标注建议：

- 普通查询工具：`readOnlyHint=true`、`destructiveHint=false`
- 索引触发工具：`readOnlyHint=false`、`idempotentHint=false`
- 状态查询工具：`readOnlyHint=true`、`idempotentHint=true`

### 10.3 V1 Tools

#### `kb_search`

统一 RAG 检索入口，供 Skill 在不确定问题类型时调用。

```python
kb_search(
  query: str,
  domain: str | None = None,
  view: str | None = None,
  granularity: str | None = None,
  profile_id: str | None = None,
  top_k: int = 12
)
```

适用：

- 初步探索
- 文档和代码混合问题
- Skill 需要先建立候选证据

#### `code_resolve`

解析代码实体。

```python
code_resolve(
  name: str,
  entity_type: str | None = None,
  profile_id: str | None = None,
  snapshot_id: str | None = None
)
```

返回：

- 候选符号
- 定义/声明位置
- 所属模块
- profile 条件
- 歧义提示

#### `code_context`

获取代码实体上下文。

```python
code_context(
  entity: str,
  granularity: str = "symbol",
  view: str = "code",
  profile_id: str | None = None,
  include_neighbors: bool = True
)
```

支持 `granularity`：

- `file`
- `symbol`
- `function`
- `module`
- `feature`

#### `code_trace`

追踪调用链或运行链路。

```python
code_trace(
  source: str,
  target: str | None = None,
  trace_type: str = "call",
  profile_id: str | None = None,
  max_depth: int = 5
)
```

支持 `trace_type`：

- `call`
- `init`
- `event`
- `isr`
- `task`
- `queue`
- `timer`
- `fault`

#### `config_impact`

分析宏或 profile 的影响。

```python
config_impact(
  macro_or_config: str,
  profile_id: str | None = None,
  compare_to: str | None = None,
  scope: str | None = None
)
```

#### `docs_search`

文档检索。

```python
docs_search(
  query: str,
  doc_type: str | None = None,
  domain: str | None = None,
  module: str | None = None,
  version: str | None = None,
  top_k: int = 12
)
```

支持 `doc_type`：

- `api`
- `widget`
- `engineering`
- `product`
- `design`
- `project`
- `qa`
- `release`

#### `workspace_view`

返回工程视图。

```python
workspace_view(
  view: str = "workspace",
  topic: str | None = None,
  profile_id: str | None = None,
  depth: int = 2
)
```

支持 `view`：

- `workspace`
- `layer`
- `domain`
- `feature`
- `runtime`
- `profile`

#### `evidence_bundle`

根据实体或查询打包证据。

```python
evidence_bundle(
  entity_or_query: str,
  profile_id: str | None = None,
  max_items: int = 20
)
```

### 10.4 Ops Tools

默认只在 `server.expose_ops_tools=true` 时暴露。
`deployment_mode=remote_shared` 下 V1 即使配置了 `server.expose_ops_tools=true` 也必须拒绝启动，避免远程共享服务暴露索引、配置和运维入口。

| Tool | 作用 |
| --- | --- |
| `ops_get_config` | 查看当前配置摘要 |
| `ops_validate_setup` | 校验 workspace、workdir、source docs、数据库 |
| `ops_index_status` | 查看索引任务状态 |
| `ops_start_index` | 启动索引任务 |
| `ops_cancel_index` | 取消索引任务 |
| `ops_list_profiles` | 枚举 profile |
| `ops_list_sources` | 枚举文档源 |

### 10.5 Resources

资源 URI 建议：

```text
active://config/current
active://snapshot/current
active://snapshot/{snapshot_id}
active://profile/{profile_id}
active://workspace/current/summary
active://workspace/current/tree
active://view/{view_name}/{topic}
active://doc/{doc_id}
active://doc-section/{section_id}
active://entity/{entity_id}
active://evidence/{evidence_id}
active://index/status
```

资源用于只读上下文展示，不用于触发索引或修改状态。

### 10.6 给 Skill 的稳定调用契约

Skill 只应依赖以下稳定能力：

- `kb_search`
- `code_resolve`
- `code_context`
- `code_trace`
- `config_impact`
- `docs_search`
- `workspace_view`
- `evidence_bundle`

Skill 不应依赖：

- SQLite 表结构
- LanceDB collection 名称
- 内部 parser 名称
- ops 工具
- 临时 artifact 路径

### 10.7 Skill 路由建议

本节只给出常见问题的简表。完整、可测试的工具选路契约见附录 A.10 至 A.14。

| 用户问题 | Skill 首选工具 |
| --- | --- |
| “这个 API 怎么用” | `docs_search(doc_type=api)` -> `code_resolve` |
| “这个控件怎么用” | `docs_search(doc_type=widget)` -> `workspace_view(view=feature)` |
| “这个函数在哪” | `code_resolve` |
| “这个模块负责什么” | `code_context(granularity=module)` |
| “某功能链路怎么走” | `workspace_view(view=feature)` -> `code_trace` |
| “宏开关影响什么” | `config_impact` |
| “两个 profile 有什么差异” | `config_impact(compare_to=...)` |
| “我该去哪看” | `workspace_view(view=workspace/topic)` |
| “需求对应哪些代码” | `kb_search(domain=product)` -> `workspace_view(view=feature)` |

---

## 11. 代码查询的具体方案

### 11.1 Symbol-first 查询

对函数名、宏名、类型名、错误码、路径片段，优先走：

```text
entity_fts -> symbol table -> file/chunk evidence -> graph neighbors
```

返回时标记：

- exact match
- fuzzy match
- alias match
- doc mention only
- generated summary only

### 11.2 Module-first 查询

对“某模块负责什么”“某目录怎么理解”，走：

```text
directory/module entity -> module.mk/Config.in evidence -> child files -> summaries -> docs
```

输出：

- 模块职责
- 关键文件
- 关键符号
- 配置入口
- 文档入口
- 不确定点

### 11.3 Feature-first 查询

对“运动记录功能怎么实现”“通知 UI 链路在哪里”，走：

```text
domain/feature lexical match
  -> packages/services
  -> packages/apps
  -> ui
  -> uiframework/widgets
  -> framework/engine
  -> code trace
```

通过 Active 路径规则建立初始 mapping：

- `packages/services/<domain>` -> Domain/ServicePackage
- `packages/apps/<feature>` -> AppPackage/Feature
- `ui/<Feature>` -> UI screen family
- `uiframework/.../widget` -> Widget/UIFrameworkComponent
- `framework/engine/<domain>Engine` -> EngineComponent

### 11.4 Runtime-first 查询

对启动链、ISR、任务、队列、timer、fault：

```text
pattern extractor -> relation graph -> profile filter -> evidence bundle
```

V1 pattern：

- `*_init`
- `task_create` wrapper
- queue send/receive wrapper
- semaphore/event wrapper
- timer create/start/callback
- ISR/vector table pattern
- fault/error code report pattern

V2 引入 clang AST 后提升置信度。

### 11.5 Profile-aware 查询

所有代码和关系查询都接受 `profile_id`。
完整 profile 规范、`auto` 选择算法、多 profile 展示和重算边界见附录 C。

如果用户没有指定：

- 使用 `project.default_profile`
- 如果 default_profile 为 `auto`，优先当前 `.config`
- 如果仍无法判断，返回 `warnings`，并给出可选 profile 列表

Profile 过滤依据：

- defconfig 宏
- `.config` 宏
- Config.in/Kconfig 条件
- `#ifdef/#if` 条件
- build module 可达性
- compile DB 中实际编译文件

---

## 12. 文档知识方案

### 12.1 文档作为一等 source

API 文档、控件文档、产品文档、项目文档不进入 Skill 正文，而进入 `knowledge-sources/` 和外部配置的 doc roots。

每个文档至少记录：

- source path
- doc type
- domain
- version
- owner
- authority level
- freshness timestamp

### 12.2 API 文档和代码关联

关联策略：

- API 名称 exact match 到 symbol
- 函数签名 match 到 declaration/definition
- 模块名 match 到 directory/module
- 示例代码中的 symbol match
- 文档 front matter 显式声明 `code_symbols`

推荐 API 文档 front matter：

```yaml
---
doc_type: api
module: sensor
version: "1.0"
code_symbols:
  - sensor_get_hr
  - sensor_subscribe
profiles:
  - all
authority_level: official
---
```

### 12.3 控件文档和 UI 代码关联

推荐控件文档 front matter：

```yaml
---
doc_type: widget
widget: Picker
ui_framework: active
code_paths:
  - uiframework/
  - ui/
tags:
  - input
  - list
authority_level: official
---
```

### 12.4 未来产品/项目文档接入

产品文档 front matter：

```yaml
---
doc_type: product
feature_id: sport_record
version: "4.0"
status: approved
related_domains:
  - sport
related_ui:
  - SportRecord
authority_level: prd
---
```

项目文档 front matter：

```yaml
---
doc_type: project
project_id: active_4_0
milestone: M2
status: active
related_features:
  - sport_record
authority_level: project_plan
---
```

---

## 13. 安全与治理

### 13.1 路径安全

所有文件读取必须经过 `path_guard`：

- 只能读取 allowlist 内路径
- 禁止 `..` 逃逸
- 禁止通过 symlink 跳出 allowlist，除非配置显式允许
- 返回给 MCP 的路径尽量使用相对路径

### 13.2 敏感信息过滤

索引前扫描：

- 私钥
- token
- 密码
- 云访问凭证
- 内部证书

命中后策略：

- 默认跳过 embedding
- evidence excerpt 脱敏
- index report 中记录被过滤的文件和原因

### 13.3 远程服务安全

远程安全必须 fail-safe：配置不满足安全条件时，server 拒绝启动，而不是降级为“带 warning 继续运行”。

启动校验发生在配置合并后、MCP server 绑定端口前。失败返回 `result_status=blocked`，写入 `security.log` 和 `audit.log`，并且不得暴露任何 tool。

#### 13.3.1 `local_single_user`

本地单机模式规则：

- 默认 `transport=stdio`。
- `server.expose_ops_tools=false`；用户可在本机配置中显式设为 `true`。
- `security.audit.enabled=true`。
- 若启用 `streamable-http`，`server.http.host` 只能是 `127.0.0.1`、`::1` 或 `localhost`。
- loopback HTTP 可允许 `require_auth=false`，但仍必须校验 `Origin`；默认只允许 `http://127.0.0.1` 和 `http://localhost`。
- local mode 下如果 host 是 `0.0.0.0` 或非 loopback，按远程暴露风险处理，必须拒绝启动。

#### 13.3.2 `remote_shared`

远程共享模式规则：

- 默认 `transport=streamable-http`。
- `server.http.require_auth=true`。
- `server.http.auth_provider` 必须是 `token` 或 `oidc`。
- `server.http.allowed_origins` 必须非空，且不得包含 `*`、`http://*`、`https://*` 或空字符串。
- `security.audit.enabled=true`。
- `server.expose_ops_tools=false`；V1 不允许 remote_shared 暴露 ops tools。
- `server.http.host` 可以是 `0.0.0.0` 或非 loopback，但只有在认证、Origin、audit 和 ops 暴露规则全部通过时才允许启动。
- 推荐在可信网关或 HTTPS 终止后部署；`trust_reverse_proxy=true` 时必须记录代理来源和原始客户端摘要。

#### 13.3.3 fail-safe 启动校验

| 条件 | 失败 code | 级别 | 行为 |
| --- | --- | --- | --- |
| `deployment_mode` 不在枚举内 | `schema.invalid_request` | `blocked` | 拒绝启动。 |
| `local_single_user` 绑定 `0.0.0.0` 或非 loopback host | `security.remote_insecure_config` | `blocked` | 拒绝启动，提示切换 loopback 或 remote_shared。 |
| 非 loopback HTTP 且 `require_auth=false` | `security.auth_required` | `blocked` | 拒绝启动。 |
| remote_shared 缺 `allowed_origins` 或包含通配 | `security.origin_blocked` | `blocked` | 拒绝启动。 |
| remote_shared 未启用 audit | `security.audit_required` | `blocked` | 拒绝启动。 |
| remote_shared 设置 `expose_ops_tools=true` | `security.ops_exposure_blocked` | `blocked` | 拒绝启动。 |
| `auth_provider=token` 但 token 来源不可用 | `security.auth_required` | `blocked` | 拒绝启动。 |
| `auth_provider=oidc` 但 issuer/audience/JWKS 缺失 | `security.auth_required` | `blocked` | 拒绝启动。 |

启动失败报告示例：

```json
{
  "result_status": "blocked",
  "warnings": [
    {
      "level": "blocked",
      "code": "security.auth_required",
      "message": "remote_shared requires authenticated HTTP transport.",
      "actionable": true,
      "suggested_action": "Set server.http.require_auth=true and configure auth_provider."
    }
  ]
}
```

#### 13.3.4 远程认证抽象

V1 支持 token provider：

```yaml
server:
  http:
    require_auth: true
    auth_provider: token
    token:
      env: ACTIVE_KB_AUTH_TOKEN
      header: Authorization
      scheme: Bearer
```

要求：

- token 从环境变量或 secret file 读取，不写入仓库配置。
- 比对使用常量时间比较。
- audit 只记录 token 是否存在、认证结果和调用方摘要，不记录 token 内容。

V2 预留 OIDC provider：

```yaml
server:
  http:
    require_auth: true
    auth_provider: oidc
    oidc:
      issuer: https://issuer.example.com
      audience: active-kb
      jwks_url: https://issuer.example.com/.well-known/jwks.json
      required_scopes:
        - active-kb:read
```

OIDC provider 必须校验 issuer、audience、签名、过期时间和 scope；失败统一返回 `security.auth_required` 或后续细分的 auth code。

### 13.4 派生知识治理

派生知识卡必须保存：

- 生成时间
- 生成器版本
- 模型名
- evidence_refs
- 适用 snapshot/profile
- review_status

未经审核的知识卡不能作为最高权威证据。

---

## 14. 验收门槛与回归策略

本节把评测集、补充需求和发布检查统一为 release gate。所有 gate 必须可在本地 release 流程和 CI 中复用同一份配置，默认配置文件为 `eval/gates/v1.yaml`。

推荐命令：

```bash
active-kb eval run --gate v1 --cases eval/cases.yaml --report .active-kb/local/artifacts/eval/v1-gate.json
active-kb validate --gate v1 --report .active-kb/local/artifacts/validate/v1-gate.json
active-kb perf run --gate v1 --report .active-kb/local/artifacts/perf/v1-gate.json
```

gate 输出必须包含：

```json
{
  "gate_id": "v1",
  "status": "pass",
  "started_at": "2026-05-06T00:00:00Z",
  "baseline_manifest": ".active-kb/baseline/manifest.json",
  "cases_file": "eval/cases.yaml",
  "metrics": {},
  "failures": [],
  "warnings": [],
  "artifacts": []
}
```

### 14.1 阻断级别

| 级别 | 语义 | release 行为 |
| --- | --- | --- |
| `blocker` | 安全、schema、契约、数据一致性或核心质量阈值失败。 | 阻断 release；必须修复或显式降级 gate 配置。 |
| `warning` | 非核心指标低于目标，或性能、稳定性存在可接受风险。 | 不默认阻断，但必须进入 release note 和后续任务。 |
| `advisory` | 未来增强项、观察项或低风险偏差。 | 不阻断；记录趋势。 |

安全测试、schema compliance、MCP contract、path guard、warning registry、storage consistency、profile resolution 契约测试必须 100% 通过，失败一律为 `blocker`。

### 14.2 V1 评测集

`eval/cases.yaml` 至少覆盖：

| 类别 | 最低数量 | 必须覆盖 |
| --- | ---: | --- |
| 符号定位 | 10 | 函数、宏、类型、文件路径、同名候选。 |
| API 文档查证 | 10 | 参数、返回值、错误码、示例、版本。 |
| 控件使用 | 10 | 属性、事件、生命周期、样式、示例。 |
| workspace 导航 | 10 | area、module、目录职责、feature 入口。 |
| 配置/profile 影响 | 10 | `CONFIG_*`、defconfig、`.config`、multi-profile diff。 |
| feature/domain 跨层 | 10 | 产品/设计/工程/代码证据链。 |
| warning 与降级 | 10 | 零结果、多结果、歧义、低置信、profile unresolved。 |
| 存储与增量 | 10 | baseline/overlay/tombstone/FTS/vector/profile 变更。 |

每个 case 必须声明 `intent`、输入工具、期望 evidence、profile/snapshot 要求、允许 warning、阻断级别和是否计入 release gate。

### 14.3 质量阈值

V1 gate 的质量阈值：

| 指标 | 计算方式 | 目标 | 阻断级别 |
| --- | --- | ---: | --- |
| Evidence Hit Rate | 返回 evidence 是否包含人工标注目标。 | >= 0.85 | `blocker` |
| Top-k Recall | top 10 是否召回目标 chunk/entity/evidence。 | >= 0.90 | `blocker` |
| MRR | 目标证据排序位置的 mean reciprocal rank。 | >= 0.65 | `blocker` |
| nDCG@10 | 标注相关证据在 top 10 的排序质量。 | >= 0.75 | `warning` |
| Profile Correctness | profile 解析、过滤和 multi-profile 展示是否正确。 | >= 0.95 | `blocker` |
| Warning Quality | warning code、level、actionable、suggested_action 是否正确。 | >= 0.90 | `blocker` |
| schema compliance | QueryResult、Warning、Evidence、ProfileResolution 等结构校验。 | 1.00 | `blocker` |
| Answer Faithfulness | 如果启用 `answer_draft`，草稿是否只基于 evidence。 | >= 0.95 | `warning` |

质量指标不得只看平均值。任一 P0 case 失败都应标记为 `blocker`；P1 case 失败按指标阈值聚合；P2 case 默认为 `warning` 或 `advisory`。

### 14.4 性能阈值

性能 gate 使用固定评测集和固定本地 baseline。除 incremental index 外，每个工具至少运行 30 次，报告 P50/P95。

| 操作 | 测量条件 | P50 目标 | P95 目标 | 阻断级别 |
| --- | --- | ---: | ---: | --- |
| `init --reuse-baseline` | baseline 已存在，只做配置、manifest、schema 校验。 | <= 5s | <= 15s | `warning` |
| server startup | stdio 或本地 HTTP 启动到 tools ready。 | <= 2s | <= 5s | `warning` |
| `docs_search` | top_k=12，API/widget/docs 混合 case。 | <= 800ms | <= 2500ms | `warning` |
| `code_resolve` | symbol/path/macro exact/fuzzy case。 | <= 500ms | <= 1500ms | `warning` |
| `kb_search` | hybrid recall + fusion + lightweight rerank。 | <= 1200ms | <= 3500ms | `warning` |
| `evidence_bundle` | 5 到 20 条 evidence_refs 打包。 | <= 300ms | <= 1000ms | `warning` |
| incremental index | <= 100 个文件变化，无 embedding 重建。 | <= 60s | <= 180s | `warning` |
| incremental index with embedding | <= 100 个文档 chunk 变化，允许本地 embedding。 | <= 180s | <= 600s | `advisory` |

性能失败默认不阻断 V1 release，但如果退化导致工具超时、partial_ready 不可用或 CI 无法稳定完成，则升级为 `blocker`。

### 14.5 稳定性阈值

稳定性 gate 分为 release 必跑和 nightly 长跑。

| 场景 | 门槛 | 阻断级别 |
| --- | --- | --- |
| 长时间运行 | server 连续运行 8 小时；错误率为 0；内存增长不超过 20%；无文件句柄泄漏。 | `warning`，连续两次失败升为 `blocker` |
| 并发只读查询 | 16 并发、30 分钟、混合 query；成功率 >= 99.5%；无写锁等待扩散。 | `blocker` |
| 索引中断恢复 | indexing 期间终止进程，重启后 job 可恢复或可安全重试，`validate` 通过。 | `blocker` |
| migration 幂等 | 同一 migration 连续执行两次，schema/data checksum 无异常变化。 | `blocker` |
| partial_ready 可用性 | parser/embedding 单项失败时，可用索引仍可查询并返回明确 warning。 | `blocker` |
| baseline/overlay 只读隔离 | 普通增量索引不写 baseline；overlay 可清理和重建。 | `blocker` |

nightly 长跑最近一次通过时间超过 7 天时，release gate 至少返回 `warning`；超过 14 天时升级为 `blocker`。

### 14.6 失败回归门槛

- 安全、schema、契约、存储一致性测试必须 100% 通过。
- Evidence Hit Rate、Top-k Recall、Profile Correctness、Warning Quality 不得低于阈值，也不得相对上一发布基线下降超过 2 个百分点。
- MRR 不得低于阈值，也不得相对上一发布基线下降超过 0.03。
- P95 延迟相对上一发布基线退化超过 20% 时至少为 `warning`；导致超时或工具不可用时为 `blocker`。
- 新增 bug 必须补充最小复现 case 到 `eval/cases.yaml` 或 `tests/regression/`，否则修复不能关闭。
- 修复召回、路由、profile、warning 或存储一致性问题时，必须同时更新对应 gate 样本或 fixture。

### 14.7 补充需求 Gate 映射

原第 19 节补充需求不再作为建议列表维护，统一映射为 gate：

| 需求 | Gate 检查 | 阻断级别 |
| --- | --- | --- |
| 可重复索引 | 同一 snapshot/profile 连续索引两次，entity/relation/chunk/evidence stable ID 和 checksum 一致。 | `blocker` |
| 可复用 baseline | `init --reuse-baseline` 能校验 manifest、schema、source docs hash、embedding model，并只写 local overlay。 | `blocker` |
| 可解释检索 | 每条 evidence 必须包含 retriever、score、rank、source、path、line 或 section。 | `blocker` |
| 查询审计 | audit log 记录 tool、query hash、profile、snapshot、耗时、命中数、warning，不记录大段源码和敏感值。 | `blocker` |
| 评测闭环 | parser、extractor、retriever、reranker、router、profile 规则变更必须运行相关 eval subset。 | `blocker` |
| 权限分级 | domain/audience metadata 存在；远程共享模式下权限未启用时返回安全 warning。 | `warning` |
| 离线 embedding | 无外网环境下可使用本地 embedding，或禁用 vector 后通过 FTS/symbol/graph 降级。 | `blocker` |
| 存储迁移 | SQLite migration 可前进、幂等、失败不破坏已有可用索引。 | `blocker` |
| 数据清理 | `clean --cache`、`clean --old-snapshots`、`rebuild --vectors` 不删除 active baseline/profile。 | `blocker` |
| 锁机制 | 索引写入持有 job lock；并发只读查询不阻塞；并发写入明确拒绝或排队。 | `blocker` |
| front matter | API、widget、product、project 文档 front matter schema 校验，缺关键字段返回 warning。 | `warning` |
| 证据最小化 | MCP 默认返回短摘录和引用；大段源码、敏感路径和 secret 必须被拦截或脱敏。 | `blocker` |

### 14.8 V1 最低发布门槛

V1 release 至少必须满足：

- 可通过 init 配置 `/home/gangan/Active` 或其他工程路径。
- 可复用或生成 `.active-kb/baseline/db/metadata.db`。
- 可生成 `.active-kb/local/db/overlay.db` 和 `.active-kb/local/db/jobs.db`。
- 可索引 `knowledge-sources/api` 和 `knowledge-sources/widgets`。
- 可返回带 evidence 的 API/控件查询结果。
- 可对代码文件、符号、宏、目录做 FTS 和结构化查询。
- 缺少 compile DB 时返回明确 warning。
- Skill 可通过稳定工具完成基础问答。
- `active-kb eval run --gate v1`、`active-kb validate --gate v1` 和 `active-kb perf run --gate v1` 可在本地和 CI 使用同一份 gate 配置。

---

## 15. 主要业务流程设计

### 15.1 端到端数据流

```mermaid
flowchart LR
    BASE[Distributed Baseline<br/>.active-kb/baseline] --> QS
    WS[Active Workspace] --> INV[Source Inventory]
    DOC[knowledge-sources / external doc roots] --> INV
    BUILD[defconfig / .config / compile DB / map files] --> INV

    INV --> PARSE[Parse Results]
    PARSE --> EXTRACT[Entities / Relations / Chunks / Evidence]
    BUILD --> PROFILE[Snapshot / Profile Manifests]
    PROFILE --> EXTRACT

    EXTRACT --> META[(Local overlay.db)]
    EXTRACT --> FTS[(Local overlay FTS5)]
    EXTRACT --> EMBED[Embedding Jobs]
    EMBED --> VEC[(Local lancedb-delta)]
    EXTRACT --> ART[Local Artifacts / Reports]

    USER[Skill / Agent Query] --> MCP[FastMCP Tools]
    MCP --> QS[Query Service]
    QS --> META
    QS --> FTS
    QS --> VEC
    QS --> ART
    QS --> EVID[Evidence Bundle]
    EVID --> MCP
    MCP --> USER
```

数据流约束：

- 原始源码和原始文档仍留在 source of truth，不默认复制完整正文
- `.active-kb/baseline/` 保存随分发携带的可复用索引基线
- `.active-kb/local/` 保存用户本机配置、增量索引、cache、logs 和 locks
- `metadata.db` 和 `overlay.db` 共同组成可查询结构、证据引用和 FTS
- 向量库只保存被选中的 doc chunk、module summary、knowledge card 向量，并区分 baseline 和 delta
- artifacts 面向人工评审和调试，不能成为唯一机器查询来源
- Query Service 聚合多类索引后只返回结构化结果和 evidence bundle

### 15.2 首次初始化流程

```mermaid
sequenceDiagram
    participant User
    participant CLI as active-kb CLI
    participant Config as Config Loader
    participant Guard as Path Guard
    participant Base as Baseline Manifest
    participant Store as Storage Migrator
    participant Disc as Source Discovery

    User->>CLI: active-kb init --workspace --reuse-baseline
    CLI->>Config: build initial config
    Config->>Guard: validate workspace/workdir/source_docs
    Guard-->>Config: normalized safe paths
    Config->>Base: load and validate baseline manifest
    Base-->>Config: baseline status and warnings
    Config->>Store: create local overlay and run migrations
    Store-->>Config: overlay.db/jobs.db ready
    Config->>Disc: discover configs, .config, source docs
    Disc-->>Config: manifests and warnings
    Config->>CLI: write local config
    CLI-->>User: baseline reuse status and next commands
```

失败处理：

- workspace 不存在：阻断 init
- workdir 不可写：阻断 init
- source docs 不存在：允许创建空目录并 warning，但无法复用完整文档索引基线
- baseline manifest 缺失：允许继续，但提示执行全量索引或下载 baseline artifact
- baseline schema 不兼容：尝试 migration，失败则阻断 baseline 复用
- local SQLite migration 失败：阻断 init，并保留错误日志
- 找不到 `.config`：允许继续，但 default profile 标记为 unresolved

### 15.3 全量索引流程

全量索引有两种目标：

- 普通用户本机全量索引：写入 `.active-kb/local/`，用于没有 baseline 或本地工程差异很大时
- 发布/CI 基线构建：写入 `.active-kb/baseline/`，用于生成可分发的索引基线

```mermaid
sequenceDiagram
    participant CLI as CLI / Ops Tool
    participant Jobs as Job Orchestrator
    participant Conn as Connectors
    participant Parser as Parsers
    participant Ext as Extractors
    participant Store as Storage
    participant Vec as Vector Store
    participant Report as Artifacts

    CLI->>Jobs: start full index(snapshot=current, profile=auto, target=local/baseline)
    Jobs->>Store: create job(indexing)
    Jobs->>Conn: discover workspace/docs/build outputs
    Conn-->>Jobs: source manifests
    Jobs->>Ext: collect snapshot and profiles
    Ext->>Store: upsert snapshot/profile/file inventory

    loop each changed or selected file
        Jobs->>Parser: parse file
        Parser-->>Ext: parse result
        Ext->>Store: upsert entities/relations/chunks/evidence/FTS
        Ext->>Vec: enqueue or write embeddings
    end

    Vec-->>Store: embedding refs
    Jobs->>Report: write index report/workspace map
    Jobs->>Store: mark job ready or failed
    Store-->>CLI: status summary
```

全量索引的关键状态：

| 状态 | 说明 |
| --- | --- |
| `pending` | job 已创建，等待执行 |
| `discovering` | 正在收集 workspace、docs、build outputs |
| `parsing` | 正在解析文件 |
| `extracting` | 正在抽取实体、关系、chunk、evidence |
| `embedding` | 正在构建或更新向量 |
| `reporting` | 正在生成报告和 artifacts |
| `ready` | 索引可查询 |
| `failed` | 失败，需要查看 job error |
| `partial_ready` | 部分 source 失败，但可降级查询 |

### 15.4 增量索引流程

```mermaid
flowchart TD
    A[Start incremental index] --> B[Load previous snapshot/index manifest]
    B --> C[Scan source manifests]
    C --> D{File hash / mtime changed?}
    D -- no --> E[Reuse previous chunks/entities]
    D -- yes --> F[Parse changed file]
    F --> G[Delete stale chunks/entities/relations for file]
    G --> H[Insert new chunks/entities/relations/evidence]
    H --> I{Needs embedding rebuild?}
    I -- yes --> J[Rebuild vectors for affected chunks]
    I -- no --> K[Keep embedding refs]
    J --> L[Update FTS and reports]
    K --> L
    E --> L
    L --> M{Profile config hash changed?}
    M -- yes --> N[Recompute profile-conditioned relations]
    M -- no --> O[Finalize job]
    N --> O
```

增量索引必须保证：

- 对 overlay 中的旧 chunk 同步删除相关 FTS、向量引用和 relation
- 对 baseline 中被本地变更覆盖的 chunk/entity/relation，不直接删除 baseline，而是在 overlay 写 tombstone 或 replacement
- parser version 或 embedding model version 变化时，即使文件 hash 不变也要重建相关产物
- 对单文件失败应尽量进入 `partial_ready`，并在查询时返回 warning
- 多进程写入通过 `.active-kb/locks/` 或 SQLite job lock 串行化

### 15.5 查询与 RAG 流程

```mermaid
sequenceDiagram
    participant Skill
    participant MCP as FastMCP Tool
    participant Router as Query Router
    participant Ret as Retrievers
    participant Fuse as Fusion/Rerank
    participant Graph as Graph Expander
    participant Ev as Evidence Packager
    participant Store as Storage

    Skill->>MCP: kb_search / code_context / docs_search
    MCP->>Router: validate schema and normalize query
    Router->>Router: classify intent/view/granularity/profile
    Router->>Ret: run symbol + FTS + vector retrieval
    Ret->>Store: query metadata/FTS/vector refs
    Store-->>Ret: candidates
    Ret-->>Fuse: scored candidates
    Fuse->>Graph: expand neighbors if needed
    Graph->>Store: relation traversal
    Store-->>Graph: related entities/evidence
    Graph-->>Fuse: expanded candidates
    Fuse-->>Ev: ranked candidates
    Ev->>Store: load evidence excerpts and metadata
    Ev-->>MCP: QueryResult with warnings/evidence_refs
    MCP-->>Skill: structured response
```

查询流程的降级顺序：

1. `symbol + FTS + graph + vector + rerank`
2. `symbol + FTS + graph + rules`
3. `FTS + path/module heuristics`
4. `workspace map + warning`

### 15.6 典型业务场景流程

| 场景 | 入口工具 | 流程 |
| --- | --- | --- |
| 查 API 用法 | `docs_search(doc_type=api)` | 文档召回 -> API item 抽取 -> 示例和参数证据 -> 关联源码符号 |
| 查控件用法 | `docs_search(doc_type=widget)` | 控件文档召回 -> 属性/事件/生命周期证据 -> UI 代码候选 |
| 查函数在哪 | `code_resolve` | symbol exact/fuzzy -> 定义声明候选 -> profile 条件 -> evidence |
| 查模块职责 | `code_context(granularity=module)` | module entity -> module.mk/Config.in -> 子文件和摘要 -> 相关文档 |
| 查功能链路 | `workspace_view(view=feature)` + `code_trace` | feature/domain 召回 -> service/app/ui 投影 -> 调用或事件链路 |
| 查宏影响 | `config_impact` | macro/profile 解析 -> guarded relations -> 受影响模块/文件/symbol |
| 查两个 profile 差异 | `config_impact(compare_to=...)` | profile 宏 diff -> module 可达性差异 -> 证据包 |
| 建库健康检查 | `ops_validate_setup` | 路径校验 -> schema 校验 -> source manifest -> warning/report |

### 15.7 错误处理与恢复流程

```mermaid
flowchart TD
    A[Job or Query Error] --> B{Error Type}
    B -- config/path --> C[Fail fast with actionable message]
    B -- parser single file --> D[Record file warning and continue]
    B -- storage write --> E[Rollback current transaction]
    B -- embedding provider --> F[Disable vector path and continue FTS]
    B -- compile DB missing --> G[Use V1 heuristic code index]
    B -- MCP schema --> H[Return validation error]

    D --> I[partial_ready if enough sources indexed]
    E --> J[mark job failed]
    F --> I
    G --> I
    C --> K[User fixes config and reruns]
    H --> L[Skill adjusts tool call]
```

错误处理原则：

- 配置错误和越权路径必须 fail fast
- 单文件解析错误不应阻断整个知识库可用性
- 存储写入以批次事务为边界，失败后 job 可重试
- 查询时发现索引不完整必须返回 warning，而不是静默给低置信答案

---

## 16. 分阶段实施计划

### Phase 0：工程骨架

产出：

- Python package
- FastMCP server 空壳
- CLI：`init`、`serve`、`index`、`status`
- config schema
- workdir 初始化
- SQLite migration 框架

### Phase 1：文档 RAG MVP

产出：

- `knowledge-sources/` 接入
- Markdown/HTML 文档解析
- API/widget front matter
- SQLite FTS5
- LanceDB embedding
- `docs_search`、`kb_search`、`evidence_bundle`

### Phase 2：Workspace 和代码结构索引

产出：

- workspace scan
- repo/path/module map
- `module.mk`、`Config.in`、defconfig、`.config` 解析
- C/H/CPP 基础 symbol 抽取
- `workspace_view`、`code_resolve`、`code_context`

### Phase 3：Active 视角图谱

产出：

- workspace/layer/domain/feature/profile 投影视图
- Active path mapping 规则
- feature/domain 查询
- profile-aware filtering 初版

### Phase 4：Runtime 和影响分析

产出：

- task/isr/queue/timer/fault pattern extractor
- `code_trace`
- `config_impact`
- graph expansion

### Phase 5：编译感知增强

产出：

- compile DB 接入
- clang/clangd-compatible index
- definition/ref 高置信索引
- profile 级可达性提升

### Phase 6：多角色知识扩展

产出：

- product/design/project connectors
- requirement-to-code/design trace
- 权威源冲突报告
- audience-aware 查询参数

---

## 17. 关键设计决策

### 17.1 V1 默认 local-first，而不是先上 PostgreSQL

原因：

- 本项目源码分发
- 首次部署要求简单
- 默认工作目录在源码顶层
- 源码分发可以内置 `.active-kb/baseline/`，用户只需配置本地项目路径并执行增量索引
- 单机索引和本地 Agent 使用是第一阶段主场景

保留升级路径：

- storage adapter
- config backend switch
- MCP schema 不变

### 17.2 代码事实以结构化索引为主

原因：

- Active 是大型 RTOS 工程，宏、profile、构建模块非常关键
- 纯向量无法可靠处理符号、宏、路径、条件编译
- 文档 embedding 可以补充语义，但不能替代代码图谱

### 17.3 Skill 只依赖稳定 tools

原因：

- Skill 是工作流，不是数据库客户端
- 稳定工具可让后端存储和索引策略自由演进
- 防止 Skill 固化错误的动态工程事实

### 17.4 源文档和索引基线集中在源码顶层

原因：

- 源码分发时文档和 server 一起移动
- 用户可以直观看到哪些文档会被知识库记忆
- `.active-kb/baseline/` 可以随分发携带，避免首次部署长时间初始化索引
- `.active-kb/local/` 承接用户本机差异，保护团队基线不被普通增量写入污染
- 未来产品/项目/设计文档可以渐进接入

### 17.5 主方案选择矩阵

| 方案项 | V1 选择 | 备选方案 | 选择理由 | 何时切换 |
| --- | --- | --- | --- | --- |
| MCP 框架 | FastMCP | 官方 MCP Python SDK 直写 | 快速实现 tools/resources/schema，适合 Python 工程 | FastMCP 能力不满足协议或部署需求时 |
| Transport | stdio 默认，streamable-http 可选 | SSE、独立 REST API | 本地 Agent/IDE 优先，HTTP 用于共享服务 | 团队共享和远程访问成为主场景 |
| 元数据存储 | SQLite baseline + overlay | PostgreSQL | local-first、源码分发、可复用索引基线、部署简单 | 多用户共享、并发写、权限治理增强时 |
| 全文检索 | SQLite FTS5 | OpenSearch、PostgreSQL FTS | 对符号、路径、宏、API 名称更可靠，部署轻 | 搜索语法、规模、权限需求复杂化时 |
| 向量存储 | LanceDB | Qdrant、pgvector、sqlite-vec | Python 本地体验好，目录型部署简单 | 大规模 ANN、服务化、多租户时 |
| 图谱存储 | SQLite relation table | Neo4j、Kuzu、graph DB | V1 关系规模和查询复杂度可由关系表承担 | 图遍历复杂度和可视化需求显著提升时 |
| C/C++ 索引 | ctags + Tree-sitter + path/build 规则 | clang index | 缺 compile DB 时先启动，保留编译感知升级 | compile DB 稳定可生成后 |
| Rerank | RRF + 规则 rerank | cross-encoder reranker | V1 无需额外模型依赖，行为可解释 | 评测证明排序瓶颈明显时 |
| Embedding | 文档和摘要优先 | 全量代码 embedding | 控制成本，避免代码事实依赖向量 | 文档语义召回不足或模块摘要成熟后 |
| 派生知识 | 默认关闭，人工审核 | 自动写入知识卡 | 避免未审核摘要污染权威事实 | evidence 和 review 工作流成熟后 |

### 17.6 关键子方案选择

#### 17.6.1 Baseline 分发方案

推荐：源码仓库或 release artifact 内置 `knowledge-sources/` 和 `.active-kb/baseline/`。

用户侧流程：

1. 拉取或解压 `active-knowledge` 分发包
2. 配置本地 `project.workspace_root`
3. `init --reuse-baseline` 校验 baseline
4. `index --incremental` 只补齐本机代码、配置或文档差异
5. 查询时自动合并 baseline 和 local overlay

发布侧流程：

1. 整理或更新 `knowledge-sources/`
2. 在标准 Active workspace 上执行全量索引
3. 运行 eval 回归
4. 生成 `.active-kb/baseline/manifest.json`
5. 将 baseline 作为 release artifact 或 Git LFS 大文件随源码分发

这一方案可以显著降低首次部署成本，同时让已整理文档和已验证索引成为团队共享资产。

#### 17.6.2 Snapshot 主键方案

推荐：`baseline branch snapshot + defconfig profile`

不推荐把 repo manifest 或单个子仓 commit 作为最高层主键。Active 的真实差异更多由 baseline 代码宇宙和 profile 配置共同决定，repo/submodule 更适合作为 snapshot 的组成部分。

需要保留：

- 子仓 commit map
- baseline branch name/head
- defconfig hash
- `.config` hash
- compile DB hash

#### 17.6.3 Chunk 粒度方案

推荐：代码以 symbol/function/type/file header 为主，文档以 heading/API item/widget item 为主。

不推荐统一固定 token 数切块。固定 token 切块会破坏 API 参数表、控件属性表、函数体、宏条件上下文，导致 evidence 难以解释。

#### 17.6.4 代码关系置信度方案

V1 关系必须带 `confidence` 和 `extractor`：

| 关系来源 | 置信度建议 |
| --- | --- |
| clang index | high |
| compile DB + AST | high |
| Tree-sitter 结构 | medium |
| ctags | medium |
| FTS/path 规则 | low to medium |
| LLM/派生卡推断 | low，必须 evidence + review |

#### 17.6.5 文档权威源方案

推荐权威顺序：

1. official API/widget docs
2. 源码与构建配置
3. 已审核 engineering notes
4. 产品/设计/项目文档中各自 domain 的权威源
5. learned cards
6. 历史聊天、临时笔记和未审核摘要

不同 domain 的权威不能简单混排。例如代码行为以源码为准，产品意图以 PRD 为准，UI 规范以设计文档为准。

#### 17.6.6 索引任务执行方案

V1 推荐单进程 job orchestrator + 文件级增量 + SQLite job lock。

暂不引入分布式任务队列。原因是本地源码分发和单机索引是第一阶段主场景，先把数据契约、增量正确性和恢复能力做稳。

升级条件：

- 索引时间无法接受
- 多 profile 并行索引成为刚需
- 团队共享服务需要后台任务调度

#### 17.6.7 生成式能力方案

V1 server 不内置最终回答生成，只提供证据包、摘要候选、warnings 和 next queries。

原因：

- 不绑定具体 LLM provider
- 降低部署复杂度和成本
- Skill/Agent 更适合根据上下文组织最终回答
- 便于把 server 做成稳定、可测试、可复用的 evidence service

未来可以增加可选 `answer_draft` tool，但必须明确其输出是草稿，不是最高权威事实。

---

## 18. 运维、可观测性与数据生命周期

### 18.1 运行状态与健康检查

`ops_validate_setup` 和 `ops_index_status` 至少应检查：

- 配置文件是否可读、schema 是否通过
- workspace/source docs/workdir 是否存在且权限正确
- baseline manifest 是否存在，是否与 source docs hash/schema/parser/embedding 版本匹配
- baseline SQLite schema version 是否匹配
- local overlay SQLite schema version 是否匹配
- baseline/overlay FTS 表和 metadata 表是否一致
- baseline LanceDB collection 和 local delta collection 是否存在，embedding model version 是否匹配
- 最近一次 snapshot/profile/job 状态
- 是否存在 `partial_ready`、parser errors、embedding errors
- HTTP transport 是否满足 host/auth/origin 配置要求
- `deployment_mode` 是否为 `local_single_user` 或 `remote_shared`
- local HTTP 是否只绑定 loopback
- remote_shared 是否满足 `require_auth=true`、`allowed_origins` 非通配、audit enabled、ops tools 不暴露
- token/OIDC provider 必需字段是否存在且 secret 来源可读取
- 安全启动失败是否返回 `blocked` 并写入 audit/security log

### 18.2 日志与指标

建议日志拆分：

| 日志 | 内容 |
| --- | --- |
| `server.log` | MCP server 启停、transport、tool call 摘要、异常 |
| `indexer.log` | job 状态、文件扫描、解析错误、批次写入、耗时 |
| `audit.log` | tool、query、profile、调用方、耗时、结果数量、ops 操作 |
| `security.log` | 启动安全校验、auth/origin/path 拒绝、远程暴露风险摘要 |
| `eval.log` | 评测 case、命中情况、指标变化 |

建议指标：

- `index_files_total`
- `index_files_failed`
- `index_duration_seconds`
- `query_latency_seconds`
- `retrieval_candidates_total`
- `evidence_items_returned`
- `warnings_total`
- `embedding_queue_size`
- `storage_size_bytes`

### 18.3 数据生命周期

数据分级：

| 数据 | 是否可删除 | 重建来源 |
| --- | --- | --- |
| `knowledge-sources/` | 不建议删除 | 人工整理和版本管理的源文档 |
| `.active-kb/baseline/manifest.json` | 不建议删除 | release baseline 构建流程 |
| `.active-kb/baseline/db/metadata.db` | 可从标准 workspace 重建，但成本高 | workspace、source docs、build outputs |
| `.active-kb/baseline/vectors/lancedb/` | 可重建 | baseline chunks + embedding model |
| `.active-kb/baseline/artifacts/` | 可重建或随 release 归档 | baseline 索引结果 |
| `.active-kb/local/config/active-kb.local.yaml` | 不建议删除 | 用户本机配置 |
| `.active-kb/local/db/overlay.db` | 可删除，删除后回到 baseline 状态 | 用户本机增量索引 |
| `.active-kb/local/db/jobs.db` | 可清理历史 job | 当前和历史 job |
| `.active-kb/local/vectors/lancedb-delta/` | 可重建 | overlay chunks + embedding model |
| `.active-kb/local/cache/` | 可删除 | parser/embedding 中间产物 |
| `.active-kb/local/logs/` | 可轮转 | 运行过程 |

必须提供的运维命令：

- `active-kb status`
- `active-kb validate`
- `active-kb index --incremental`
- `active-kb index --full`
- `active-kb clean --cache`
- `active-kb clean --old-snapshots --keep N`
- `active-kb rebuild --vectors`
- `active-kb migrate`
- `active-kb baseline validate`
- `active-kb baseline publish`

### 18.4 兼容性与迁移

所有可持久化产物都应记录版本：

- config schema version
- SQLite schema version
- parser version
- extractor version
- embedding model version
- vector collection version
- MCP schema version
- baseline manifest version
- source docs hash/version

迁移原则：

- 小版本升级自动迁移
- 大版本升级先备份再迁移
- migration 必须幂等
- migration 失败不能破坏已有可用索引
- MCP tool 返回中应包含 `schema_version`，方便 Skill 适配

### 18.5 常见风险与缓解

| 风险 | 缓解 |
| --- | --- |
| baseline 与源文档不一致 | manifest 记录 source docs hash，init 时校验，不一致则 warning 并建议增量重建 |
| 用户增量污染团队基线 | baseline 默认只读，普通索引只写 local overlay |
| 文档过期 | front matter 记录 version/freshness，查询时降权并 warning |
| 代码索引低置信 | relation confidence + extractor 标记，缺 compile DB 时 warning |
| 向量召回漂移 | FTS/symbol 优先，评测集跟踪 Evidence Hit Rate |
| source path 泄漏 | 返回相对路径，路径经过 allowlist 和脱敏 |
| 索引中断 | job 状态可恢复，批次事务写入 |
| 多进程写冲突 | job lock，普通查询只读 |
| Skill 依赖内部实现 | 只暴露稳定 tools/resources，不暴露数据库细节 |

---

## 附录 A：查询契约

本附录定义 Query Router 在检索前对用户问题进行分类的稳定契约。它面向实现、MCP schema、Skill 路由和评测 fixture，不面向最终回答话术。

### A.1 设计原则

- **单一主 intent**：V1 每次分类只输出一个 `intent`。多个信号并存时通过置信度、`matched_signals` 和 warning 暴露不确定性，避免 Skill 同时面对多套主路由。
- **规则优先，模型可选**：符号、路径、宏名、错误码、profile、API、控件、运行时关键词等确定信号必须由规则识别。LLM 分类器只能作为低置信或歧义时的可替换补充，输出仍必须约束到本附录枚举。
- **精确锚点优先**：对函数、宏、路径、错误码、寄存器、配置项等技术查询，精确检索信号优先于纯语义向量信号。
- **用户显式参数优先**：`domain`、`view`、`granularity`、`profile_id`、`snapshot_id`、`caller_tool` 是调用方已经给出的上下文，优先级高于从自然语言中猜测。
- **可解释路由**：分类结果必须返回 `matched_signals`，每个信号说明来源、权重和触发原因，便于审计、调权和构造回归用例。
- **低置信可降级**：无法稳定分类时不阻塞查询，默认进入 `kb_search` 的 hybrid recall，但必须带 `low_confidence` warning 和 `next_queries`。

### A.2 intent 枚举

V1 固定支持以下 `intent`，Skill、MCP tool 描述、评测集和查询日志必须复用同一组术语。

```text
code_exact
code_concept
call_trace
runtime_flow
profile_diff
api_lookup
widget_lookup
workspace_nav
product_context
project_context
evidence_lookup
unknown
```

### A.3 分类输入

Query Router 的分类输入来自 `QueryRequest`。所有字段都应进入 route trace，即使为空。

```json
{
  "query": "CONFIG_BT 在 mhs003 watch profile 下影响哪些模块？",
  "domain": "auto",
  "view": "auto",
  "granularity": "auto",
  "profile_id": "auto",
  "snapshot_id": "current",
  "caller_tool": "kb_search",
  "client_context": {
    "cwd": "packages/services/bluetooth",
    "active_file": "packages/services/bluetooth/bt_service.c",
    "selected_text": "CONFIG_BT",
    "selected_symbol": "CONFIG_BT",
    "recent_paths": ["packages/services/bluetooth/bt_service.c"],
    "user_role": "firmware_engineer",
    "previous_intent": null,
    "previous_entities": []
  }
}
```

字段含义：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `query` | `str` | 用户原始问题，必须保留原文，并额外生成 normalized query。 |
| `domain` | `auto | code | docs | api | widget | product | project | design | engineering` | 调用方提供的知识域提示。`auto` 表示由 router 判断。 |
| `view` | `auto | workspace | layer | domain | feature | runtime | profile | evidence | code` | 用户或 Skill 期望的投影视图。 |
| `granularity` | `auto | workspace | directory | module | file | symbol | function | flow | feature | profile | doc_section` | 用户或 Skill 期望的结果颗粒度。 |
| `profile_id` | `auto | current | <profile_id> | null` | profile 上下文。`auto` 触发 profile resolution；`null` 表示调用方明确不需要 profile。 |
| `snapshot_id` | `current | baseline | <snapshot_id> | null` | snapshot 上下文。为空时默认 `current`。 |
| `caller_tool` | `kb_search | code_resolve | code_context | code_trace | config_impact | docs_search | workspace_view | evidence_bundle | skill | client` | 调用入口，用于提升对应 intent 的先验权重。 |
| `client_context` | `object` | IDE 或 Skill 附带的上下文，不得覆盖显式 query，但可作为 tie-breaker。 |

### A.4 分类输出

分类输出是 Query Router 的稳定中间结果，后续工具选择、retriever 权重、warning 和审计日志都引用它。

```json
{
  "intent": "profile_diff",
  "confidence": 0.88,
  "matched_signals": [
    {
      "type": "macro_name",
      "value": "CONFIG_BT",
      "span": [0, 9],
      "weight": 0.25,
      "source": "query",
      "reason": "matches CONFIG_* macro pattern"
    },
    {
      "type": "profile_keyword",
      "value": "mhs003 watch profile",
      "span": [12, 30],
      "weight": 0.30,
      "source": "query",
      "reason": "explicit profile context"
    }
  ],
  "selected_view": "profile",
  "selected_granularity": "profile",
  "profile_resolution": {
    "requested": "auto",
    "status": "resolved",
    "resolved_profile_id": "mhs003_watch",
    "candidates": []
  },
  "warnings": []
}
```

字段含义：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `intent` | enum | 本附录定义的单一主 intent。 |
| `confidence` | `float` | `0.0-1.0` 的分类置信度。V1 推荐按规则权重归一化计算，后续可替换为校准后的分类器。 |
| `matched_signals` | `list[MatchedSignal]` | 触发分类的可解释信号。必须足以复现分类结果。 |
| `selected_view` | enum | Router 选择的默认投影视图。显式输入非 `auto` 时通常沿用调用方值。 |
| `selected_granularity` | enum | Router 选择的默认结果颗粒度。 |
| `profile_resolution` | object | profile 解析状态。无 profile 相关性时为 `not_required`。 |
| `warnings` | list | 分类阶段 warning。warning 的完整分级由附录 A 后续章节统一定义。 |

`MatchedSignal` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | enum | 例如 `symbol_like`、`path_like`、`macro_name`、`error_code`、`runtime_keyword`、`profile_keyword`、`api_keyword`、`widget_keyword`、`product_keyword`、`project_keyword`、`evidence_keyword`、`caller_tool_hint`、`client_context_hint`。 |
| `value` | `str` | 命中的原始文本或规范化值。 |
| `span` | `[int, int] | null` | 在 `query` 中的字符区间。来自 `client_context` 时为 `null`。 |
| `weight` | `float` | 对最终分类的贡献权重。 |
| `source` | `query | explicit_param | caller_tool | client_context | history` | 信号来源。 |
| `reason` | `str` | 简短触发说明，供 route trace 和调试使用。 |

### A.5 触发信号与默认路由

| intent | 定义 | 主要触发信号 | 默认 view | 默认 granularity | 首选召回偏置 |
| --- | --- | --- | --- | --- | --- |
| `code_exact` | 定位确定代码实体或路径。 | 函数/类型/变量形态，如 `foo_bar()`、`Foo::bar`；宏名，如 `CONFIG_*`、全大写下划线；路径，如 `drivers/foo/bar.c`；错误码、寄存器名；“在哪”“定义”“声明”“谁引用”。 | `code` | `symbol` 或 `file` | symbol + FTS，必要时 graph neighbors。 |
| `code_concept` | 解释某个代码机制、模块职责或实现思路。 | “如何实现”“机制”“原理”“负责什么”“为什么这样做”；模块名或领域词但没有明确 source-target 链路；自然语言描述多于精确锚点。 | `domain` 或 `layer` | `module` 或 `feature` | FTS + module summary + vector + graph。 |
| `call_trace` | 追踪函数、模块或事件从 A 到 B 的调用关系。 | “调用链”“谁调用”“被谁调用”“A 到 B”“入口到”“trace call”；同时出现 source/target 符号或函数形态。 | `code` | `flow` | symbol resolve + relation graph + evidence bundle。 |
| `runtime_flow` | 分析 RTOS 运行时链路和并发语义。 | `ISR`、`task`、`thread`、`queue`、`timer`、`semaphore`、`mutex`、`event`、`fault`、`boot`、`init`、`startup`、“消息链”“事件链”“中断”“任务切换”。 | `runtime` | `flow` | relation graph + pattern extractor + FTS。 |
| `profile_diff` | 分析 profile、board、defconfig、`.config` 或宏开关差异和影响。 | `profile`、`board`、`defconfig`、`.config`、`CONFIG_*`、`Kconfig`、“启用”“裁剪”“影响哪些模块”“对比”“差异”。 | `profile` | `profile` | profile tables + macro relations + FTS。 |
| `api_lookup` | 查询 API 文档、参数、返回值、约束、示例和代码链接。 | “API”“接口”“怎么用”“参数”“返回值”“示例”“调用方式”；API 名称、SDK/JS/Native 接口名；`domain=api` 或 `docs_search(doc_type=api)`。 | `evidence` 或 `domain` | `doc_section` | docs FTS + vector + symbol link。 |
| `widget_lookup` | 查询 Active 控件、UI 组件、属性、事件、样式和示例。 | “控件”“组件”“widget”“属性”“样式”“布局”“事件”“示例”；UI 路径、控件名、`doc_type=widget`。 | `feature` | `doc_section` 或 `feature` | widget docs + UI paths + examples + vector。 |
| `workspace_nav` | 回答该去哪里看、目录/仓/模块职责和工程结构。 | “在哪看”“去哪里改”“目录结构”“哪个仓”“哪个模块”“路径职责”“新人入口”；路径片段、workspace area 名称。 | `workspace` 或 `layer` | `directory` 或 `module` | workspace map + path role + directory entities。 |
| `product_context` | 查询功能范围、需求背景、PRD、用户故事、验收口径。 | “需求”“功能范围”“PRD”“用户故事”“验收”“产品定义”“为什么要做”；`domain=product`。 | `feature` | `feature` 或 `doc_section` | product docs + feature map + metadata filtering。 |
| `project_context` | 查询项目计划、版本、风险、里程碑、Issue、Owner、交付状态。 | “里程碑”“排期”“风险”“进度”“版本计划”“release”“issue”“owner”“阻塞”；`domain=project`。 | `evidence` | `doc_section` | project docs + issue/release sources + metadata filtering。 |
| `evidence_lookup` | 用户主要要求证据、出处、引用、原文或证据包。 | “依据”“证据”“出处”“引用”“原文”“证明”“evidence bundle”；`caller_tool=evidence_bundle`。如果这些词只是“请带证据”的修饰，则保持原业务 intent。 | `evidence` | `doc_section` 或 `symbol` | evidence graph + original source + authority sorting。 |
| `unknown` | 缺少可稳定分类的查询。 | 过短、代词过多、无锚点、纯闲聊、上下文缺失；最高 intent 分数低于阈值。 | `evidence` | `doc_section` | `kb_search` hybrid recall + warning + next queries。 |

### A.6 分类优先级与冲突处理

分类器按以下顺序合成分数：

1. 显式参数：非 `auto` 的 `domain`、`view`、`granularity`、`profile_id`、`snapshot_id`。
2. 调用入口：`caller_tool=code_trace` 提升 `call_trace`，`caller_tool=config_impact` 提升 `profile_diff`，`caller_tool=docs_search` 根据 `doc_type` 提升文档类 intent。
3. 精确锚点：符号、路径、宏、错误码、寄存器、defconfig、`.config`。
4. 领域关键词：API、控件、运行时、profile、产品、项目、证据关键词。
5. 自然语言句式：“怎么用”“如何实现”“调用链”“影响哪些”“去哪看”等。
6. IDE/Skill 上下文：active file、selected symbol、recent paths、previous entities。

冲突处理规则：

- `evidence_lookup` 只有在用户主要目标是“找出处/证据包/原文”时作为主 intent；如果问题是“这个 API 怎么用，请给出处”，主 intent 仍为 `api_lookup`。
- 单个 `CONFIG_*` 加“在哪定义”归为 `code_exact`；加“影响哪些模块/哪些 profile 启用/差异”归为 `profile_diff`。
- 函数名加“在哪/定义/声明”归为 `code_exact`；两个符号之间加“怎么调用/A 到 B”归为 `call_trace`。
- `init/startup/boot` 只问入口符号位置时归为 `code_exact`；问启动顺序、任务/事件链路时归为 `runtime_flow`。
- 路径加“这个目录负责什么”归为 `workspace_nav`；路径加“这个机制如何实现”可归为 `code_concept`，并把路径作为 scope。
- 产品、项目词同时出现时，用户问功能/需求/验收归 `product_context`；问排期/风险/owner/release 归 `project_context`。

推荐置信度规则：

| 条件 | 行为 |
| --- | --- |
| `confidence >= 0.80` | 直接使用主 intent 路由。 |
| `0.50 <= confidence < 0.80` | 使用主 intent 路由，但返回 `router.medium_confidence` 或 `router.ambiguous_intent` warning。 |
| `confidence < 0.50` | intent 置为 `unknown`，走 `kb_search` hybrid recall，并返回 `router.low_confidence` warning。 |
| top1 与 top2 分差 `< 0.15`，且 top1 `< 0.80` | 保留 top1，但返回歧义 warning，并在 `next_queries` 中提示补充上下文。 |

### A.7 unknown 与低置信降级

当 intent 为 `unknown` 或低置信时，Query Service 不应生成确定结论。默认行为：

- 调用 `kb_search` 的 hybrid recall，启用 FTS、vector、workspace metadata 的宽召回。
- `result_status` 使用 A.15 定义的统一状态枚举，通常为 `low_confidence` 或 `zero_result`。
- `summary` 只说明“找到的候选线索”，不得写成确定事实。
- `warnings` 至少包含 `router.low_confidence`。
- `next_queries` 必须给出 2 到 5 个可执行追问或补充过滤条件，例如补充 `profile_id`、模块名、符号名、doc_type、路径。
- 若 query 过短或只有代词，优先建议用户提供 active file、选中符号或目标模块。

示例返回片段：

```json
{
  "query_intent": "unknown",
  "confidence": 0.34,
  "warnings": [
    {
      "level": "caution",
      "code": "router.low_confidence",
      "message": "Query intent is unclear; returned hybrid recall candidates only.",
      "actionable": true,
      "suggested_action": "Provide a module, symbol, doc_type, or profile_id."
    }
  ],
  "next_queries": [
    "请补充要查看的模块、文件路径或符号名",
    "如果是 API 用法，请指定 API 名称或 doc_type=api",
    "如果是 profile 差异，请指定 profile_id 或 compare_to"
  ]
}
```

### A.8 正反例

以下用例必须可以一比一转换为 `tests/fixtures/query_intents.yaml`。正例要求分类为目标 intent；反例要求不得分类为目标 intent，并应落到表中说明的其他 intent。

| intent | 正例 | 反例 |
| --- | --- | --- |
| `code_exact` | `CONFIG_HEALTH_SLEEP 在哪里定义？`<br>`drivers/hr/ppg_sensor.c 这个文件属于哪个模块？`<br>`app_manager_start() 的声明和实现在哪？` | `CONFIG_HEALTH_SLEEP 影响哪些 profile？` -> `profile_diff`<br>`app_manager_start() 到 screen_create() 的调用链怎么走？` -> `call_trace` |
| `code_concept` | `Active 的运动数据缓存机制是怎么实现的？`<br>`packages/services/notification 主要负责什么？`<br>`健康引擎如何把传感器数据交给上层？` | `notification_service.c 在哪？` -> `code_exact`<br>`sensor API 怎么用？` -> `api_lookup` |
| `call_trace` | `app_manager_start() 到 screen_create() 的调用链怎么走？`<br>`谁调用了 health_service_publish_event？`<br>`GNSS 初始化入口到驱动 open 的链路是什么？` | `启动阶段有哪些 task？` -> `runtime_flow`<br>`app_manager_start() 在哪定义？` -> `code_exact` |
| `runtime_flow` | `PPG 中断之后事件如何进入 health task？`<br>`启动时 timer service 的初始化顺序是什么？`<br>`fault 发生后 Active 的处理链路怎么走？` | `timer_create API 怎么用？` -> `api_lookup`<br>`timer_service_init 在哪里？` -> `code_exact` |
| `profile_diff` | `watch@mhs003 和 sensorhub@mhs003 的 CONFIG_BT 差异是什么？`<br>`CONFIG_GNSS 打开后影响哪些模块？`<br>`这个功能在哪些 defconfig 下启用？` | `CONFIG_BT 在哪里定义？` -> `code_exact`<br>`蓝牙服务的实现机制是什么？` -> `code_concept` |
| `api_lookup` | `sensor.subscribe API 怎么用？`<br>`Active 蓝牙 API 的参数和返回值是什么？`<br>`如何调用 file system API 读写文件？` | `sensor_service.c 里 subscribe 在哪定义？` -> `code_exact`<br>`传感器数据从 ISR 到 task 的链路是什么？` -> `runtime_flow` |
| `widget_lookup` | `Active Text widget 支持哪些属性？`<br>`progress ring 控件怎么绑定数据？`<br>`UI Button 组件的点击事件示例在哪？` | `button_driver.c 在哪？` -> `code_exact`<br>`按钮点击后事件如何进入 app task？` -> `runtime_flow` |
| `workspace_nav` | `新同事要看蓝牙功能应该从哪些目录开始？`<br>`ui 和 uiframework 的职责边界是什么？`<br>`packages/services 下各模块大概负责什么？` | `蓝牙扫描 API 怎么用？` -> `api_lookup`<br>`notification_service_start 调用链怎么走？` -> `call_trace` |
| `product_context` | `睡眠功能 V1 的产品范围是什么？`<br>`这个运动目标提醒需求为什么要做？`<br>`PRD 里对离线语音功能的验收标准是什么？` | `睡眠算法 task 的运行链路是什么？` -> `runtime_flow`<br>`睡眠功能本周风险有哪些？` -> `project_context` |
| `project_context` | `mhs003 本周有哪些交付风险？`<br>`下一版 Active release 计划包含哪些事项？`<br>`蓝牙重构 issue 的 owner 和当前状态是什么？` | `蓝牙重构后的 API 怎么用？` -> `api_lookup`<br>`蓝牙功能的用户价值是什么？` -> `product_context` |
| `evidence_lookup` | `给我 app_manager_start 的证据包。`<br>`上面结论的出处是什么？`<br>`睡眠功能范围有哪些原文依据？` | `app_manager_start 怎么调用 screen_create？` -> `call_trace`<br>`睡眠功能范围是什么？请带证据。` -> `product_context` |
| `unknown` | `这个为什么不行？`<br>`帮我看看这里。`<br>`mhs003 那个东西呢？` | `CONFIG_BT 影响哪些模块？` -> `profile_diff`<br>`Text widget 怎么用？` -> `widget_lookup` |

### A.9 单元测试 fixture 格式

每个正反例应落成一条可执行 fixture。推荐 schema：

```yaml
- id: intent_profile_diff_pos_001
  input:
    query: "CONFIG_GNSS 打开后影响哪些模块？"
    domain: auto
    view: auto
    granularity: auto
    profile_id: auto
    snapshot_id: current
    caller_tool: kb_search
    client_context: {}
  expect:
    intent: profile_diff
    min_confidence: 0.80
    selected_view: profile
    selected_granularity: profile
    required_signal_types:
      - macro_name
      - profile_keyword
    forbidden_intents:
      - code_exact
  notes:
    - "CONFIG_* alone is not enough for profile_diff; the phrase '影响哪些模块' is the differentiator."
```

测试要求：

- 正例：`intent` 必须等于期望值，`confidence` 必须大于 `min_confidence`，`required_signal_types` 必须全部出现在 `matched_signals`。
- 反例：不得落入 `forbidden_intents`；若最高分低于阈值，应落入 `unknown` 并返回 `router.low_confidence`。
- 回归：新增 intent、signal type 或 tie-breaker 时，必须新增 fixture；修改权重时，必须说明受影响 fixture。
- 日志：测试快照应保留 `matched_signals`，避免只检查最终 intent 导致不可解释回归。

### A.10 V1 工具定位

工具选路发生在 intent 分类之后。Router 必须把 intent、显式参数、profile resolution、工具可用性和最近一次返回状态合成为一个 `tool_plan`。`kb_search` 是探索和降级入口，不是所有问题的默认首选。

推荐内部 `tool_plan` 结构：

```json
{
  "route_mode": "direct",
  "primary_tool": "config_impact",
  "primary_args": {
    "macro_or_config": "CONFIG_BT",
    "profile_id": "mhs003_watch",
    "compare_to": "mhs003_sensorhub"
  },
  "fallback_tools": ["kb_search", "code_resolve"],
  "chain": [
    {
      "tool": "config_impact",
      "on_status": ["ok", "multi_result", "partial_ready"],
      "stop_if_evidence_sufficient": true
    },
    {
      "tool": "evidence_bundle",
      "when": "summary_has_entities_without_source_refs"
    }
  ],
  "prohibited_dependencies": ["ops_tools", "sqlite_tables", "lancedb_collections", "artifact_paths"]
}
```

V1 工具定位：

| Tool | 适用 | 不适用 | 回退 |
| --- | --- | --- | --- |
| `kb_search` | 探索性问题；混合代码/文档/产品域；低上下文或低置信 intent；先召回候选证据再决定下一步。 | 已有明确符号、路径、宏、API、profile 或 trace 锚点且可直接调用专用工具；需要确定调用链或 profile diff 的问题。 | `code_resolve`、`docs_search`、`workspace_view` 或 `config_impact`，由候选实体和 intent 再路由。 |
| `code_resolve` | 函数、宏、类型、变量、错误码、路径、模块名等代码实体定位；同名实体消歧。 | 机制解释、调用链、API 用法、产品/项目文档问题；没有任何代码锚点的宽泛问题。 | 零结果时 `kb_search(domain=code)`；多结果时返回候选并可接 `code_context` 或 `evidence_bundle`。 |
| `code_context` | 已知实体的上下文、文件/模块职责、邻近符号、profile 条件；需要从“在哪”推进到“它周围是什么”。 | 未解析实体；跨 source-target 调用链；API/控件纯文档用法。 | 先 `code_resolve`；上下文不足时 `kb_search(domain=code)`；需要链路时 `code_trace`。 |
| `code_trace` | 调用链、初始化链、事件链、ISR/task/queue/timer/fault 链路；已有 source 或 target 锚点。 | 单纯定位符号；没有 source/topic 的宽泛机制问题；profile 宏影响分析。 | 先 `code_resolve` 解析锚点；relation 不完整时补 `kb_search(domain=code, view=runtime)` 或 `docs_search(doc_type=engineering)`。 |
| `config_impact` | `CONFIG_*`、Kconfig、defconfig、`.config`、profile 差异、board/app 启用裁剪、宏影响范围。 | 宏定义位置的纯定位；普通 API/控件用法；无 profile 相关性的代码解释。 | 宏未解析时 `code_resolve(entity_type=macro)`；profile 未解析时返回 warning 和候选；需要原始证据时 `evidence_bundle`。 |
| `docs_search` | API、widget、engineering、product、design、project、qa、release 文档检索；参数、约束、示例、需求、计划。 | 纯代码符号定位；调用链或 profile diff 的主分析；需要直接读取数据库内部结构。 | 文档提到代码实体时接 `code_resolve`；零结果时 `kb_search(domain=docs)`；需要证据闭包时 `evidence_bundle`。 |
| `workspace_view` | 工程导航、目录职责、workspace/layer/domain/feature/runtime/profile 结构视图；“该去哪看”。 | 单个符号精确定位；API 参数细节；宏差异计算。 | 需要代码细节时接 `code_context`；需要链路时接 `code_trace`；主题不清时 `kb_search`。 |
| `evidence_bundle` | 根据实体或查询汇总证据、出处、短摘录、authority、derived_from；最终回答前补证据。 | 第一次探索未知问题；执行 trace/diff/搜索本身；替代专用分析工具。 | entity 未解析时先 `code_resolve`、`docs_search` 或 `kb_search`；证据不足时返回 `next_queries`。 |

### A.11 intent 到工具的决策表

Skill 和 MCP facade 必须复用下表的术语。表中的 `首选工具` 是有足够锚点时的默认选择；`备选/回退` 是零结果、多结果、低置信、索引降级或证据不足时的下一步。

| intent | 用户问题类型 | 首选工具 | 备选/回退 | 何时回退 |
| --- | --- | --- | --- | --- |
| `code_exact` | 函数、宏、类型、错误码、路径在哪；谁定义/声明/引用。 | `code_resolve` | `code_context`、`evidence_bundle`、`kb_search(domain=code)` | `code_resolve` 零结果；同名候选过多；只命中文档提及但无代码实体。 |
| `code_concept` | 某机制如何实现；模块职责；代码设计解释。 | 有明确实体时 `code_context`；无明确实体时 `kb_search(domain=code)` | `workspace_view(view=domain/layer/feature)`、`docs_search(doc_type=engineering)`、`evidence_bundle` | 候选模块不足；自然语言问题没有锚点；代码证据和文档证据冲突。 |
| `call_trace` | A 到 B 怎么调用；谁调用谁；入口到目标链路。 | `code_resolve` -> `code_trace(trace_type=call)` | `code_context`、`kb_search(domain=code)`、`evidence_bundle` | source/target 未解析；多候选未消歧；relation graph `partial_ready`。 |
| `runtime_flow` | ISR/task/queue/timer/event/fault/startup 运行链路。 | 有锚点时 `code_trace(trace_type=...)`；无锚点时 `workspace_view(view=runtime)` | `docs_search(doc_type=engineering)`、`kb_search(view=runtime)`、`evidence_bundle` | runtime 抽取器缺失；缺 compile DB；只找到静态符号没有运行时关系。 |
| `profile_diff` | profile、board、defconfig、`.config`、宏开关影响和差异。 | `config_impact` | `code_resolve(entity_type=macro)`、`workspace_view(view=profile)`、`evidence_bundle` | profile 未解析；宏不存在；比较对象缺失；影响范围缺证据。 |
| `api_lookup` | API 用法、参数、返回值、约束、示例。 | `docs_search(doc_type=api)` | `code_resolve`、`kb_search(domain=api)`、`evidence_bundle` | 文档零结果；API 名称可能是代码符号；需要链接到实现或示例。 |
| `widget_lookup` | 控件/组件属性、事件、样式、生命周期、示例。 | `docs_search(doc_type=widget)` | `workspace_view(view=feature)`、`code_resolve`、`evidence_bundle` | 控件文档不完整；需要 UI 代码示例；控件名和代码实体同名。 |
| `workspace_nav` | 去哪里看、哪个目录/仓/模块负责、工程结构解释。 | `workspace_view` | `code_context(granularity=module/file)`、`kb_search(view=workspace)`、`evidence_bundle` | topic 不清；目录职责缺摘要；用户追问具体符号或实现。 |
| `product_context` | 需求背景、功能范围、PRD、验收标准、用户故事。 | `docs_search(doc_type=product)` 或 `kb_search(domain=product)` | `workspace_view(view=feature)`、`evidence_bundle` | 需求需要映射到代码/feature；文档版本或 authority 不足。 |
| `project_context` | 版本计划、里程碑、风险、owner、issue、release 状态。 | `docs_search(doc_type=project/release)` 或 `kb_search(domain=project)` | `evidence_bundle`、`workspace_view(view=feature)` | 项目文档零结果；状态信息过期；需要关联功能范围。 |
| `evidence_lookup` | 出处、依据、原文、证据包。 | `evidence_bundle` | `code_resolve`、`docs_search`、`kb_search` | entity/query 无法直接解析；证据少于最低要求；只命中 learned card 无原始来源。 |
| `unknown` | 上下文不足、代词过多、意图过短。 | `kb_search` | 根据候选再路由到专用工具 | `confidence < 0.50`；top1/top2 intent 分差过小；用户没有给锚点。 |

### A.12 工具串联规则

工具串联必须是有界、可解释、可停止的。Query Service 不应在一次请求中无限循环“搜索 -> 追问 -> 再搜索”。

通用规则：

- 同一条链路必须传递相同的 `snapshot_id` 和已解析 `profile_id`。不能静默混用不同 snapshot/profile 的证据。
- 一次普通查询默认最多执行 3 个 read-only query tools；`call_trace`、`runtime_flow`、`profile_diff` 可放宽到 5 个，但必须记录 route trace。
- 专用工具返回 `ok` 且 evidence 足够时停止，不再为了“保险”额外调用 `kb_search`。
- `evidence_bundle` 只在需要证据闭包、补引用或用户显式要出处时调用，不能替代 `code_trace`、`config_impact`、`docs_search` 的分析职责。
- 任一工具返回零结果、多结果、歧义、低置信或 `partial_ready` 时，下一步必须优先补上下文或缩小范围，而不是生成确定结论。
- 串联结果必须保留每一步的 `matched_signals`、`primary_tool`、`fallback_reason` 和 warning code，方便评测和审计。

推荐串联模板：

| 场景 | 串联 |
| --- | --- |
| API 用法并关联实现 | `docs_search(doc_type=api)` -> `code_resolve` -> `evidence_bundle` |
| 控件用法并找 UI 示例 | `docs_search(doc_type=widget)` -> `workspace_view(view=feature)` -> `code_resolve` -> `evidence_bundle` |
| 符号定位并解释上下文 | `code_resolve` -> `code_context` -> `evidence_bundle` |
| 调用链追踪 | `code_resolve(source)` + `code_resolve(target?)` -> `code_trace` -> `evidence_bundle` |
| 运行时链路 | `workspace_view(view=runtime)` -> `code_trace(trace_type=event/isr/task/queue/timer/fault)` -> `docs_search(doc_type=engineering)` -> `evidence_bundle` |
| 宏影响分析 | `config_impact` -> `code_resolve(entity_type=macro)` -> `evidence_bundle` |
| profile 对比 | `config_impact(compare_to=...)` -> `workspace_view(view=profile)` -> `evidence_bundle` |
| 工程导航到模块解释 | `workspace_view(view=workspace/layer/domain)` -> `code_context(granularity=module)` -> `evidence_bundle` |
| 需求映射到代码 | `docs_search(doc_type=product)` 或 `kb_search(domain=product)` -> `workspace_view(view=feature)` -> `code_context(granularity=feature)` -> `evidence_bundle` |
| 项目状态查证 | `docs_search(doc_type=project/release)` -> `evidence_bundle` |
| 低置信探索 | `kb_search` -> 根据 top candidate 的 `entity_type`、`doc_type`、`intent_hint` 再调用一个专用工具 |

### A.13 `kb_search` 的边界

`kb_search` 的职责是“先召回候选证据和候选实体”，不是“把所有查询都吞掉”。以下情况应优先使用 `kb_search`：

- 用户问题跨越代码、文档、产品或项目多个 domain，且没有明显首选专用工具。
- intent 为 `unknown` 或 `confidence < 0.50`。
- 用户只给自然语言主题，没有符号、路径、API 名称、profile 或 doc_type。
- Skill 需要先建立候选范围，再决定调用 `code_resolve`、`docs_search`、`workspace_view` 或 `config_impact`。
- 专用工具返回零结果，但 query 中仍有可用于宽召回的自然语言或路径片段。

以下情况不应先用 `kb_search`：

- 明确问“函数/宏/路径在哪”，直接 `code_resolve`。
- 明确问“API/控件怎么用”，直接 `docs_search(doc_type=api|widget)`。
- 明确问“调用链/运行链路”，先 `code_resolve` 再 `code_trace`。
- 明确问“宏/profile/board 差异或影响”，直接 `config_impact`。
- 明确问“目录职责/去哪看”，直接 `workspace_view`。
- 明确问“给我证据包/出处”，直接 `evidence_bundle`，解析失败时再回退。

### A.14 禁止路由和依赖边界

以下行为在 V1 中禁止，必须进入契约测试或 lint 检查：

- Skill 不得把 ops tools 当作查询链路的一部分，例如为了回答用户问题调用 `ops_start_index`、`ops_cancel_index` 或直接依赖 `ops_list_profiles`。
- Skill 和 MCP query tools 不得直接依赖 SQLite 表名、SQL 查询、LanceDB collection 名称、embedding 文件路径或 `.active-kb` 内部 artifact 路径。
- Query Service 不得通过未受控路径读取源码全文来绕过 evidence packager；返回给 Skill 的源码必须是受限短摘录和 evidence ref。
- 不得在普通查询中触发索引写入、迁移、clean、baseline publish 等有副作用操作。
- 不得在 profile 未解析时静默使用随机 profile；必须返回 warning、候选或按附录 C 的 `auto` 规则处理。
- 不得把 learned card、摘要或缓存当作唯一权威证据；若用于回答，必须能追溯到 `derived_from` 原始 evidence。
- 不得为了补齐证据而无界串联工具；达到最大步数后返回 partial/low confidence warning 和 `next_queries`。
- 不得向 Skill 暴露绝对路径、用户主目录、临时缓存路径或大段源码全文。

### A.15 QueryResult 统一外壳

所有 V1 query tools 都必须返回统一的 `QueryResult` 外壳。专用工具可以在 `items` 中放自己的结果类型，但不得改变 `schema_version`、`result_status`、`confidence`、`warnings`、`evidence_refs`、`next_queries` 的语义。

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "code_resolve",
  "result_status": "ok",
  "confidence": 0.91,
  "confidence_band": "high",
  "query_intent": "code_exact",
  "snapshot_id": "current",
  "profile_id": "mhs003_watch",
  "summary": "Found one high-confidence definition candidate.",
  "items": [],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [],
  "warnings": [],
  "next_queries": [],
  "suggested_filters": [],
  "diagnostics": {
    "route_mode": "direct",
    "primary_tool": "code_resolve",
    "retrieval_sources": ["symbol", "fts"],
    "elapsed_ms": 42
  }
}
```

字段要求：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定为 `query_result.v1`，schema 变化必须进入 contract snapshot。 |
| `tool_name` | 是 | 实际返回该结果的 MCP tool。串联查询以最终聚合 tool 为准，并在 `diagnostics.route_trace` 记录步骤。 |
| `result_status` | 是 | A.16 定义的状态枚举。 |
| `confidence` | 是 | `0.0-1.0`，表示本次结果可作为回答依据的整体置信度，不等同于单条候选 score。 |
| `confidence_band` | 是 | `high`、`medium`、`low`，由 A.17 分段得到。 |
| `query_intent` | 是 | A.2 定义的 intent。低置信时可为 `unknown`。 |
| `snapshot_id` | 是 | 实际查询的 snapshot。未知时用 `current` 或返回 warning，不得为空。 |
| `profile_id` | 是 | 实际使用的 profile；无关查询为 `not_required`；未解析为 `unresolved` 并返回 warning。 |
| `summary` | 是 | 简短机器可读结论。异常和不确定状态不得写确定结论。 |
| `items` | 是 | 工具主结果。零结果时必须为 `[]`。 |
| `candidates` | 是 | 多结果、歧义或低置信候选。没有候选时为 `[]`。 |
| `entities` | 是 | 涉及的实体摘要。没有时为 `[]`。 |
| `relations` | 是 | 涉及的关系摘要。没有时为 `[]`。 |
| `evidence_refs` | 是 | 可追溯证据短引用。除 `zero_result`、`blocked`、严重 `error` 外，高价值查询至少 1 条。 |
| `warnings` | 是 | A.21 至 A.26 定义的统一 warning 对象列表。没有 warning 时为 `[]`。 |
| `next_queries` | 是 | 可执行追问或下一步检索建议。没有时为 `[]`。 |
| `suggested_filters` | 是 | 建议的 profile、module、domain、doc_type、entity_type 等过滤项。没有时为 `[]`。 |
| `diagnostics` | 是 | 轻量诊断。不得包含大段源码、绝对敏感路径、token 或数据库内部细节。 |

### A.16 `result_status` 枚举

| status | 含义 | `summary` 规则 | evidence 规则 | Skill 行为 |
| --- | --- | --- | --- | --- |
| `ok` | 结果足够明确，可以作为回答依据。 | 可以给确定结论。 | 高价值查询至少 1 条；纯结构列表也应尽量有 evidence。 | 可生成答案，但仍需引用 evidence。 |
| `zero_result` | 按当前参数没有找到候选。 | 不得生成“目标不存在”的确定结论，只能说当前索引未命中。 | `evidence_refs=[]`。 | 不生成确定答案，优先展示 `next_queries` 或 `suggested_filters`。 |
| `multi_result` | 找到多个有效候选，需要选择或并列展示。 | 说明存在多个候选，不得默认选一个当唯一答案。 | 每个主要候选应有 evidence 或 match reason。 | 列候选，必要时向用户请求 disambiguation。 |
| `ambiguous` | 问题或上下文歧义，无法判断用户要哪一类结果。 | 明确缺什么上下文。 | 可有候选 evidence，但不得作结论。 | 先补问或用 `suggested_filters` 缩小范围。 |
| `low_confidence` | 有候选线索，但整体置信度低。 | 必须使用不确定措辞，如“可能”“线索显示”。 | 除同时为零结果的场景外至少 1 条。 | 可以给线索，不给确定结论；建议补查。 |
| `partial_ready` | 索引部分可用，部分 source/job 失败或降级。 | 说明可用范围和缺失范围。 | 仅引用可用索引中的 evidence。 | 可以回答可确认部分，必须显式说明降级。 |
| `blocked` | 安全、权限、路径、认证、配置或 schema 阻断。 | 说明被阻断的原因类别，不泄露敏感路径或密钥。 | 通常为空。 | 不生成答案，要求用户修配置或授权。 |
| `error` | 非预期错误。 | 说明查询失败，不暴露 stack trace。 | 通常为空；若有部分安全结果也应优先用 `partial_ready`。 | 不生成答案，展示 request_id 和建议重试/上报。 |

状态选择规则：

- `blocked` 优先级最高。只要安全或权限不满足，必须 fail fast，不继续检索。
- `error` 只用于非预期失败；可降级返回部分结果时优先用 `partial_ready`。
- `zero_result` 表示检索执行成功但无候选，不是异常。
- `multi_result` 用于候选都合理但需要选择；`ambiguous` 用于问题本身缺上下文。
- `low_confidence` 可与候选一起返回，但主 `result_status` 只能有一个；同时存在多候选且低置信时，优先按用户下一步需要选择 `multi_result` 或 `ambiguous`。

### A.17 confidence 分段

默认分段：

| band | 范围 | 说明 |
| --- | --- | --- |
| `high` | `confidence >= 0.80` | 可作为主要回答依据。 |
| `medium` | `0.50 <= confidence < 0.80` | 可作为候选线索或需补证据的回答依据。 |
| `low` | `confidence < 0.50` | 只能作为线索，不可写确定结论。 |

工具可覆盖阈值，但必须写入 tool contract 和测试 fixture。例如：

- `code_resolve` 的 exact symbol match 可在 `0.75` 以上视为高可信，但同名多候选必须降为 `multi_result`。
- `code_trace` 在缺 compile DB 且只靠启发式 relation 时，即使路径完整，也不得超过 `medium`，并应返回 `compile_db.missing` 或等价 warning。
- `docs_search` 命中过期文档或缺 front matter 时应降低 freshness 分，必要时降为 `medium`。
- `config_impact` 在 profile 未解析时不得返回 `ok/high`。

### A.18 不确定状态的强制格式

| status | 强制字段和约束 |
| --- | --- |
| `zero_result` | `items=[]`、`candidates=[]`、`evidence_refs=[]`；`summary` 不得断言目标不存在；`next_queries` 或 `suggested_filters` 至少一个非空；warning 至少包含 `retrieval.zero_result`。 |
| `multi_result` | `candidates` 非空；每个候选必须包含 `disambiguation_key`、`entity_type`、`path` 或 `module` 或 `profile_id`、`match_reason`、`score`；`summary` 说明候选数量和选择依据。 |
| `ambiguous` | `diagnostics.required_context` 非空，明确需要补充 `profile_id`、`entity_type`、`module`、`domain`、`doc_type`、`source` 或 `target` 中的哪些字段；`next_queries` 必须非空。 |
| `low_confidence` | `confidence_band=low`；`summary` 必须是不确定措辞；除零结果外 `evidence_refs` 至少 1 条；`warnings` 至少包含 `router.low_confidence` 或 `retrieval.low_confidence`。 |
| `partial_ready` | `diagnostics.index_status` 必须包含 `ready_sources`、`missing_sources`、`failed_jobs`、`degradation_chain`；warning 至少包含 `index.partial_ready`；结果只能来自 `ready_sources`。 |
| `blocked` | `items=[]`、`candidates=[]`、`evidence_refs=[]`；warning level 必须为 `blocked`；`diagnostics.blocked_reason` 必须使用安全枚举，不返回敏感细节。 |
| `error` | `items=[]`；必须有 `diagnostics.request_id` 和 `diagnostics.error_kind`；不得返回 traceback、绝对敏感路径或密钥。 |

### A.19 `result_status` JSON 示例

`ok`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "code_resolve",
  "result_status": "ok",
  "confidence": 0.92,
  "confidence_band": "high",
  "query_intent": "code_exact",
  "snapshot_id": "current",
  "profile_id": "mhs003_watch",
  "summary": "Found one definition for app_manager_start.",
  "items": [
    {"entity_id": "sym:app_manager_start", "entity_type": "function", "path": "uiframework/appmanager/app_manager.c", "start_line": 42}
  ],
  "candidates": [],
  "entities": [{"entity_id": "sym:app_manager_start", "name": "app_manager_start"}],
  "relations": [],
  "evidence_refs": [
    {"evidence_id": "ev_app_mgr_42", "type": "code", "path": "uiframework/appmanager/app_manager.c", "start_line": 42, "end_line": 58, "authority_level": "source_code", "excerpt": "short excerpt"}
  ],
  "warnings": [],
  "next_queries": [],
  "suggested_filters": [],
  "diagnostics": {"retrieval_sources": ["symbol", "fts"], "elapsed_ms": 38}
}
```

`zero_result`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "code_resolve",
  "result_status": "zero_result",
  "confidence": 0.0,
  "confidence_band": "low",
  "query_intent": "code_exact",
  "snapshot_id": "current",
  "profile_id": "not_required",
  "summary": "No candidate was found in the current index for the requested symbol.",
  "items": [],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [],
  "warnings": [{"level": "caution", "code": "retrieval.zero_result", "message": "No candidate matched the query.", "actionable": true, "suggested_action": "Relax filters or try exact symbol, module, or path."}],
  "next_queries": ["Try the exact symbol spelling.", "Search by containing module or file path."],
  "suggested_filters": [{"field": "domain", "value": "code"}, {"field": "entity_type", "value": "function"}],
  "diagnostics": {"retrieval_sources": ["symbol", "fts"], "elapsed_ms": 31}
}
```

`multi_result`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "code_resolve",
  "result_status": "multi_result",
  "confidence": 0.72,
  "confidence_band": "medium",
  "query_intent": "code_exact",
  "snapshot_id": "current",
  "profile_id": "not_required",
  "summary": "Found 3 plausible symbols named init_service; choose a module or path to disambiguate.",
  "items": [],
  "candidates": [
    {"disambiguation_key": "service:init_service", "entity_type": "function", "path": "packages/services/core/init.c", "module": "services/core", "profile_id": "all", "match_reason": "exact symbol name", "score": 0.88},
    {"disambiguation_key": "ui:init_service", "entity_type": "function", "path": "ui/services/init.c", "module": "ui/services", "profile_id": "all", "match_reason": "exact symbol name", "score": 0.84}
  ],
  "entities": [],
  "relations": [],
  "evidence_refs": [],
  "warnings": [{"level": "caution", "code": "retrieval.multi_result", "message": "Multiple candidates require disambiguation.", "actionable": true, "suggested_action": "Specify module, path, entity_type, or profile_id."}],
  "next_queries": ["Specify the module, for example services/core or ui/services."],
  "suggested_filters": [{"field": "module", "value": "services/core"}, {"field": "module", "value": "ui/services"}],
  "diagnostics": {"candidate_count": 3}
}
```

`ambiguous`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "kb_search",
  "result_status": "ambiguous",
  "confidence": 0.56,
  "confidence_band": "medium",
  "query_intent": "unknown",
  "snapshot_id": "current",
  "profile_id": "unresolved",
  "summary": "The query could refer to an API, a widget, or a runtime event; more context is required.",
  "items": [],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [],
  "warnings": [{"level": "caution", "code": "router.ambiguous_intent", "message": "Intent is ambiguous.", "actionable": true, "suggested_action": "Specify domain, entity_type, doc_type, or profile_id."}],
  "next_queries": ["Specify whether this is an API, widget, or runtime flow question.", "Provide a symbol, module, or doc_type."],
  "suggested_filters": [{"field": "doc_type", "value": "api"}, {"field": "view", "value": "runtime"}],
  "diagnostics": {"required_context": ["domain", "entity_type", "profile_id"]}
}
```

`low_confidence`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "kb_search",
  "result_status": "low_confidence",
  "confidence": 0.41,
  "confidence_band": "low",
  "query_intent": "code_concept",
  "snapshot_id": "current",
  "profile_id": "mhs003_watch",
  "summary": "Possible related evidence was found, but it is not enough to confirm the mechanism.",
  "items": [{"title": "possible module", "path": "packages/services/notification"}],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [
    {"evidence_id": "ev_notify_summary", "type": "doc", "path": "knowledge-sources/engineering/notification.md", "start_line": 10, "end_line": 18, "authority_level": "source_doc", "excerpt": "short excerpt"}
  ],
  "warnings": [{"level": "caution", "code": "retrieval.low_confidence", "message": "Evidence is weak or incomplete.", "actionable": true, "suggested_action": "Provide a narrower module, symbol, or source path."}],
  "next_queries": ["Ask for a specific module or symbol.", "Run workspace_view(view=domain, topic=notification)."],
  "suggested_filters": [{"field": "module", "value": "packages/services/notification"}],
  "diagnostics": {"retrieval_sources": ["vector"], "top_score": 0.41}
}
```

`partial_ready`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "code_trace",
  "result_status": "partial_ready",
  "confidence": 0.63,
  "confidence_band": "medium",
  "query_intent": "runtime_flow",
  "snapshot_id": "current",
  "profile_id": "mhs003_watch",
  "summary": "A partial event chain is available, but runtime relations are incomplete because some sources failed indexing.",
  "items": [{"trace_id": "trace_ppg_event_partial", "nodes": ["ppg_isr", "health_queue", "health_task"]}],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [
    {"evidence_id": "ev_ppg_isr", "type": "code", "path": "drivers/ppg/ppg_irq.c", "start_line": 80, "end_line": 96, "authority_level": "source_code", "excerpt": "short excerpt"}
  ],
  "warnings": [{"level": "degraded", "code": "index.partial_ready", "message": "Some sources are missing from the runtime relation index.", "actionable": true, "suggested_action": "Re-run indexing for failed runtime sources."}],
  "next_queries": ["Re-run index for failed runtime sources.", "Ask for a narrower source or target symbol."],
  "suggested_filters": [{"field": "trace_type", "value": "isr"}],
  "diagnostics": {
    "index_status": {
      "ready_sources": ["symbol", "fts", "tree_sitter_relations"],
      "missing_sources": ["compile_db_relations"],
      "failed_jobs": ["job_20260506_runtime_extract"],
      "degradation_chain": ["compile_db missing", "graph traversal used heuristic relations"]
    }
  }
}
```

`blocked`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "docs_search",
  "result_status": "blocked",
  "confidence": 0.0,
  "confidence_band": "low",
  "query_intent": "api_lookup",
  "snapshot_id": "current",
  "profile_id": "not_required",
  "summary": "The request was blocked by the path guard.",
  "items": [],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [],
  "warnings": [{"level": "blocked", "code": "security.path_blocked", "message": "Requested path is outside the configured allowlist.", "actionable": true, "suggested_action": "Use an allowlisted path or update the path allowlist."}],
  "next_queries": ["Use a path under the configured workspace or source docs root."],
  "suggested_filters": [],
  "diagnostics": {"blocked_reason": "path_outside_allowlist", "request_id": "qry_20260506_001"}
}
```

`error`：

```json
{
  "schema_version": "query_result.v1",
  "tool_name": "kb_search",
  "result_status": "error",
  "confidence": 0.0,
  "confidence_band": "low",
  "query_intent": "unknown",
  "snapshot_id": "current",
  "profile_id": "unresolved",
  "summary": "The query failed due to an unexpected retriever error.",
  "items": [],
  "candidates": [],
  "entities": [],
  "relations": [],
  "evidence_refs": [],
  "warnings": [{"level": "degraded", "code": "retrieval.error", "message": "Retriever failed before producing usable results.", "actionable": true, "suggested_action": "Retry the query or run validate/status if it persists."}],
  "next_queries": ["Retry the query.", "Run status or validate if the error persists."],
  "suggested_filters": [],
  "diagnostics": {"error_kind": "retriever_exception", "request_id": "qry_20260506_002"}
}
```

### A.20 契约测试要求

D0-03 对应的 contract tests 至少覆盖：

- 每个 `result_status` 的 JSON schema snapshot。
- `zero_result` 不允许出现非空 `items` 或 `evidence_refs`。
- `multi_result` 每个 candidate 必须有 `disambiguation_key`、`entity_type`、`match_reason`、`score`，且至少有 `path`、`module`、`profile_id` 之一。
- `ambiguous` 必须有 `diagnostics.required_context` 和非空 `next_queries`。
- `low_confidence` 除零结果外必须有 evidence，且 `summary` 不得使用确定措辞。
- `partial_ready` 必须包含 `index_status.ready_sources`、`missing_sources`、`failed_jobs`、`degradation_chain`。
- `blocked` 必须 fail fast：不得继续执行 retriever，不得返回 evidence。
- `error` 不得包含 traceback、绝对敏感路径、token、SQL、LanceDB collection 内部名或大段源码。
- 所有 V1 tools 共享 `warnings`、`evidence_refs`、`next_queries`、`suggested_filters` 字段，不允许局部改名。

### A.21 Warning 对象

`warnings` 是所有 V1 query tools 共享的结构化数组。warning 用于表达假设、不确定性、降级、阻断和可恢复动作，不用于塞入长日志、源码全文或内部异常栈。

标准对象：

```json
{
  "level": "degraded",
  "code": "compile_db.missing",
  "message": "Compile database is missing; relation results use heuristic extraction.",
  "details": {
    "affected_relation_types": ["calls", "refs"],
    "fallback": "tree_sitter_and_fts"
  },
  "actionable": true,
  "suggested_action": "Generate compile_commands.json or configure build_outputs.compile_db_candidates.",
  "affected_sources": ["code.relations", "runtime.graph"],
  "evidence_refs": []
}
```

字段定义：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `level` | 是 | A.22 定义的 `info`、`caution`、`degraded`、`blocked`。 |
| `code` | 是 | A.23 命名规则和 A.24 registry 中的稳定 warning code。 |
| `message` | 是 | 面向 Skill 和用户的短说明，不包含敏感路径、token、SQL 或大段源码。 |
| `details` | 否 | 机器可读补充信息，只放短结构化字段。没有时为 `{}` 或省略。 |
| `actionable` | 是 | 是否有明确恢复动作。 |
| `suggested_action` | 否 | 可执行恢复建议。`actionable=true` 时必须非空。 |
| `affected_sources` | 否 | 受影响的 source、index、retriever、profile 或 tool 能力。没有时为 `[]` 或省略。 |
| `evidence_refs` | 否 | 与 warning 本身相关的 evidence，如冲突文档、过期 source、失败 source 摘要。没有时为 `[]` 或省略。 |

约束：

- `warnings=[]` 表示没有已知不确定性，不表示结果一定正确。
- 同一 `code` 在一次 `QueryResult` 中最多出现一次；多个来源合并到 `affected_sources` 或 `details`。
- warning 不改变 `result_status` 的枚举，但可驱动 `result_status`。例如 `security.path_blocked` 必须对应 `blocked`。
- warning code 是稳定契约，不能在模块内临时发明。新增 code 必须更新 A.24 registry 和 contract tests。

### A.22 warning level

| level | 含义 | 是否影响可信度 | 是否可继续回答 | Skill 行为 |
| --- | --- | --- | --- | --- |
| `info` | 假设说明、默认选择、轻微信息缺省，不影响当前结果可信度。 | 通常不影响。 | 可以继续回答。 | 可在回答中省略，除非用户问前提或配置。 |
| `caution` | 上下文不足、候选较多、歧义、低置信或需要 disambiguation。 | 影响确定性。 | 可以列候选或线索，不应给唯一确定结论。 | 优先补查、列候选或询问用户补上下文。 |
| `degraded` | 缺索引、缺 compile DB、embedding/rerank 不可用、partial_ready、文档过期等导致结果可用但降级。 | 明显影响覆盖率或排序质量。 | 可以回答可确认部分。 | 必须在回答中说明降级范围和影响。 |
| `blocked` | 安全、权限、schema、路径、认证或配置错误导致不能返回结果。 | 结果不可用。 | 不得回答业务问题。 | 停止生成答案，只说明阻断原因和恢复动作。 |

level 与 `result_status` 的关系：

| `result_status` | 允许 level | 要求 |
| --- | --- | --- |
| `ok` | `info`、少量 `caution` | 若有 `degraded`，通常应改为 `partial_ready` 或降低 confidence。 |
| `zero_result` | `caution`、`degraded` | 至少包含 `retrieval.zero_result`。 |
| `multi_result` | `caution` | 至少包含 `retrieval.multi_result`。 |
| `ambiguous` | `caution` | 至少包含 `router.ambiguous_intent` 或上下文类 code。 |
| `low_confidence` | `caution`、`degraded` | 至少包含 `router.low_confidence` 或 `retrieval.low_confidence`。 |
| `partial_ready` | `degraded` | 至少包含 `index.partial_ready`。 |
| `blocked` | `blocked` | 至少包含一个 `blocked` level warning，且不继续 retriever。 |
| `error` | `degraded` 或 `blocked` | 非安全异常用 `degraded`；安全/权限错误用 `blocked` 并转为 `blocked` status。 |

### A.23 warning code 命名规则

命名格式：

```text
<namespace>.<reason>
```

规则：

- namespace 使用小写 snake 或短词，只能来自 registry：`router`、`retrieval`、`profile`、`snapshot`、`index`、`compile_db`、`embedding`、`rerank`、`docs`、`source`、`evidence`、`security`、`schema`、`storage`、`tool`。
- reason 使用小写 snake，表达稳定原因，不包含动态值。例如使用 `profile.unresolved`，不要使用 `profile.mhs003_missing`。
- code 不编码 level。level 可随上下文变化，但 code 语义必须稳定。
- code 不编码工具名。相同原因跨工具复用，例如 `retrieval.zero_result` 可出现在 `kb_search`、`docs_search`、`code_resolve`。
- 新增 code 必须同时补 registry、测试 fixture、审计字段说明和至少一个触发场景。

### A.24 warning code registry

V1 初始 registry：

| code | 默认 level | 触发场景 | Skill 处理 | 测试要求 |
| --- | --- | --- | --- | --- |
| `router.default_assumed` | `info` | Router 使用默认 view、granularity、snapshot 或非敏感默认 profile。 | 可正常回答，必要时说明默认值。 | default assumption fixture。 |
| `router.medium_confidence` | `caution` | intent 分类为中等置信但仍可路由。 | 继续检索，但回答需保留不确定性。 | medium confidence fixture。 |
| `router.low_confidence` | `caution` | intent 分类低于阈值。 | 走候选线索或补问，不给确定结论。 | intent 低置信 fixture。 |
| `router.ambiguous_intent` | `caution` | top intents 接近或 query 可解释为多类问题。 | 请求 domain/entity/profile/doc_type 等上下文。 | ambiguous fixture。 |
| `router.max_steps_reached` | `caution` | 工具串联达到最大步数。 | 停止自动串联，展示 next_queries。 | tool chain fixture。 |
| `retrieval.zero_result` | `caution` | 检索成功但无候选。 | 不断言不存在，展示 next_queries/suggested_filters。 | zero_result fixture。 |
| `retrieval.multi_result` | `caution` | 多个候选都合理。 | 列候选，要求 disambiguation。 | multi_result fixture。 |
| `retrieval.low_confidence` | `caution` | 候选存在但分数或证据不足。 | 只给线索，建议补查。 | low_confidence fixture。 |
| `retrieval.error` | `degraded` | retriever 非预期失败且没有可用部分结果。 | 不回答，建议 retry/status/validate。 | error fixture。 |
| `profile.unresolved` | `caution` | `profile_id=auto` 无法确定唯一 profile。 | 展示 profile 候选或要求用户指定。 | profile auto fixture。 |
| `profile.multiple_candidates` | `caution` | 多个 `.config`、defconfig 或 profile 候选。 | 列候选，不随机选择。 | multi profile fixture。 |
| `profile.mismatch` | `degraded` | 请求 profile 与 snapshot/build output 不匹配。 | 明确 profile 可能不准确。 | profile mismatch fixture。 |
| `snapshot.mismatch` | `degraded` | workspace 与 baseline snapshot/manifest 不一致。 | 说明结果可能与当前源码不同。 | snapshot mismatch fixture。 |
| `index.partial_ready` | `degraded` | 部分 source/job 失败但可降级查询。 | 回答可确认部分，说明缺失范围。 | partial_ready fixture。 |
| `index.stale` | `degraded` | 索引时间或 source hash 落后于 workspace/source docs。 | 建议增量索引。 | stale index fixture。 |
| `index.source_missing` | `degraded` | 某类 source docs、workspace path 或 build output 缺失。 | 说明覆盖范围缺失。 | source missing fixture。 |
| `compile_db.missing` | `degraded` | 缺少 compile DB，C/C++ relation 使用启发式结果。 | 必须说明调用/引用关系降级。 | compile DB missing fixture。 |
| `compile_db.stale` | `degraded` | compile DB 与当前 snapshot/profile 不匹配。 | 降低 relation confidence。 | stale compile DB fixture。 |
| `embedding.unavailable` | `degraded` | embedding 服务或本地模型不可用。 | 说明 semantic recall 关闭，FTS/symbol 继续。 | embedding unavailable fixture。 |
| `embedding.version_mismatch` | `degraded` | vector index embedding model 与配置不一致。 | 建议重建 vectors。 | embedding version fixture。 |
| `rerank.unavailable` | `degraded` | reranker 不可用。 | 使用 RRF/规则排序并说明排序降级。 | rerank unavailable fixture。 |
| `docs.version_missing` | `caution` | 文档缺 version/front matter freshness。 | 降低 freshness，说明时效未知。 | docs metadata fixture。 |
| `docs.stale` | `degraded` | 文档已过期或版本低于查询目标。 | 不把旧文档当当前事实。 | stale docs fixture。 |
| `source.conflict` | `caution` | 多来源证据冲突。 | 保留冲突，按 authority 排序。 | conflict fixture。 |
| `evidence.insufficient` | `caution` | 高价值查询 evidence 少于要求。 | 不生成确定结论，建议补查。 | evidence minimum fixture。 |
| `evidence.derived_only` | `caution` | 只命中 learned card/摘要，缺原始来源。 | 要求追溯 `derived_from`。 | derived-only fixture。 |
| `security.path_blocked` | `blocked` | 路径越权或 symlink 逃逸。 | 不回答，提示使用 allowlist 内路径。 | blocked/security fixture。 |
| `security.auth_required` | `blocked` | 远程或受保护操作缺认证。 | 要求认证或 token。 | auth fixture。 |
| `security.origin_blocked` | `blocked` | HTTP Origin 不在允许列表。 | 不回答，提示配置 Origin。 | origin fixture。 |
| `security.remote_insecure_config` | `blocked` | 本地模式绑定非 loopback 或远程 HTTP 未满足 fail-safe。 | 拒绝启动，切换 loopback 或 remote_shared 并补齐安全配置。 | remote startup fixture。 |
| `security.audit_required` | `blocked` | remote_shared 未启用 audit。 | 启用 `security.audit.enabled=true`。 | audit required fixture。 |
| `security.ops_exposure_blocked` | `blocked` | remote_shared 试图暴露 ops tools。 | 关闭 `server.expose_ops_tools` 或改用本地模式。 | ops exposure fixture。 |
| `schema.invalid_request` | `blocked` | MCP 参数 schema 不合法。 | 要求修正参数。 | schema request fixture。 |
| `schema.incompatible_version` | `blocked` | 客户端/Skill schema 版本不兼容。 | 要求升级或适配。 | schema version fixture。 |
| `storage.unavailable` | `degraded` | FTS/vector/metadata 某后端不可用但可部分降级。 | 说明缺失 retriever。 | storage unavailable fixture。 |
| `tool.not_exposed` | `blocked` | 调用未暴露或被配置禁用的 tool。 | 不绕过，提示配置或使用可用 tool。 | tool gating fixture。 |

registry 维护要求：

- registry 是唯一 code 来源；实现中建议生成 `WarningCode` enum 或常量文件。
- 未登记 code 在 contract tests 中失败。
- registry 中每个 code 至少有一个单测或契约测试覆盖；无法本地触发的安全/远程场景用 schema fixture 覆盖。
- audit log 记录 `level`、`code`、`tool_name`、`result_status`、`query_hash` 或短 query、`profile_id`、`snapshot_id`，不得记录大段源码、密钥、绝对敏感路径或完整异常栈。

### A.25 Skill 处理规则

Skill/Agent 在生成最终回答前必须先扫描 warnings：

| 最高 warning level | Skill 行为 |
| --- | --- |
| `blocked` | 不生成业务答案；只说明阻断类别、可恢复动作和必要的配置/权限上下文。 |
| `degraded` | 可以回答可确认部分，但必须在“前提/限制”中说明降级来源、影响范围和建议恢复动作。 |
| `caution` | 优先补查、列候选或向用户请求 disambiguation；如果继续回答，必须使用不确定措辞。 |
| `info` | 可正常回答；仅在前提重要或用户要求时说明。 |

多 warning 合并规则：

- 最高 level 决定回答策略：`blocked > degraded > caution > info`。
- 同 namespace 多 warning 可合并展示，但不得丢失 code；例如多个 source 缺失合并成一个 `index.source_missing`。
- `source.conflict` 不能被静默吞掉，即使同时有高置信代码证据，也要说明冲突和 authority 排序。
- `profile.unresolved` 出现在 profile-sensitive intent 中时，不得输出 profile-specific 结论。

### A.26 warning 契约测试

D0-04 对应的 contract tests 至少覆盖：

- Warning 对象 schema：`level`、`code`、`message`、`actionable` 必填；其他字段类型稳定。
- `level` 只能取 `info`、`caution`、`degraded`、`blocked`。
- `code` 必须存在于 A.24 registry，未登记 code 失败。
- `actionable=true` 时 `suggested_action` 必须非空。
- `blocked` level 必须对应 `result_status=blocked`，且 `items=[]`、`evidence_refs=[]`。
- `partial_ready` 必须包含 `index.partial_ready`。
- `zero_result` 必须包含 `retrieval.zero_result`。
- `multi_result` 必须包含 `retrieval.multi_result`。
- `low_confidence` 必须包含 `router.low_confidence` 或 `retrieval.low_confidence`。
- audit log 只记录 warning code/level 和摘要字段，不记录大段源码、token、SQL 或 traceback。

---

## 附录 B：存储一致性

本附录定义 baseline 与 local overlay 的合并、删除、替换、跨库 relation、FTS/vector 候选过滤、迁移、清理和校验规则。它是 `storage/*`、`indexing/jobs`、`query/*` 和 `ops_validate_setup` 的共同契约。

### B.1 设计目标

- **baseline 可复用**：随源码或 release artifact 分发，作为团队共享只读索引基线。
- **overlay 可恢复**：本机增量索引只写 local overlay，删除 overlay 后可回到 baseline 状态。
- **查询看 logical view**：Query Service 只读 logical entity/chunk/relation/evidence view，不直接判断 baseline/overlay 细节。
- **删除和替换可解释**：本机删除或替换 baseline 对象时不改 baseline，用 overlay tombstone/replacement 表表达。
- **FTS/vector 与 metadata 一致**：全文、向量候选必须经过 tombstone/replacement 过滤，不得返回已被本机删除或替换的旧对象。
- **失败可回滚**：migration、index、compact、clean 失败不能破坏已有可查询索引。

### B.2 baseline 只读原则

普通命令的写入边界：

| 操作 | baseline | overlay | 说明 |
| --- | --- | --- | --- |
| `init --reuse-baseline` | 只读校验 | 创建/迁移 | 不写 baseline。 |
| `index --incremental` | 只读 | 写入 | 增量实体、relation、FTS、vector delta 都写 overlay。 |
| `index --full` 普通用户 | 只读或空引用 | 写入 | 没有 baseline 时也默认写 local full overlay。 |
| query/MCP read tools | 只读 | 只读 | 不触发索引、migration、compact。 |
| `clean --cache`、`clean --old-snapshots` | 不删除 | 可清理受控 local 数据 | baseline 不由普通 clean 删除。 |
| `migrate` | 按策略只读或备份后迁移 | 备份后迁移 | 大版本 baseline migration 需人工确认。 |
| `baseline publish` 或 CI release | 可写 | 不打包 local | 唯一允许生成/改写 baseline 的路径。 |

实现要求：

- `.active-kb/baseline/` 默认以只读模式打开；写 baseline 必须显式 `write_target=baseline` 且 `mode=baseline_publish`。
- 普通用户的 local 配置不得把 `write_target` 静默提升为 baseline。
- baseline manifest、SQLite schema、vector collection version 不兼容时，查询可选择阻断 baseline 复用或降级到 overlay，但不得半写 baseline。

### B.3 overlay 优先 logical view

所有查询必须从 logical view 读取。logical view 的合并顺序：

1. 如果 overlay 有同 ID 对象且未被 tombstone 屏蔽，使用 overlay 版本。
2. 如果 baseline 对象被 overlay tombstone 命中，不返回该 baseline 对象。
3. 如果 baseline 对象被 overlay replacement 命中，查询旧 ID 时解析到新 ID 或返回 replacement warning。
4. 不同 ID 对象按 `snapshot_id`、`profile_id`、`source_scope` 合并。
5. 返回候选必须标记 `source_index=baseline|overlay|merged`。

伪 SQL：

```sql
WITH tombstoned AS (
  SELECT object_type, object_id, baseline_id
  FROM overlay.tombstone
  WHERE active = 1
),
overlay_entities AS (
  SELECT 'overlay' AS source_index, e.*
  FROM overlay.entity e
  LEFT JOIN tombstoned t
    ON t.object_type = 'entity'
   AND t.object_id = e.entity_id
  WHERE t.object_id IS NULL
),
baseline_entities AS (
  SELECT 'baseline' AS source_index, b.*
  FROM baseline.entity b
  LEFT JOIN overlay.entity oe
    ON oe.entity_id = b.entity_id
  LEFT JOIN tombstoned t
    ON t.object_type = 'entity'
   AND (t.object_id = b.entity_id OR t.baseline_id = b.entity_id)
  WHERE oe.entity_id IS NULL
    AND t.object_id IS NULL
)
SELECT * FROM overlay_entities
UNION ALL
SELECT * FROM baseline_entities;
```

接口语义：

```python
StorageReader.logical_entities(scope: QueryScope) -> Iterable[LogicalEntity]
StorageReader.logical_chunks(scope: QueryScope) -> Iterable[LogicalChunk]
StorageReader.logical_relations(scope: QueryScope) -> Iterable[LogicalRelation]
StorageReader.logical_evidence(scope: QueryScope) -> Iterable[LogicalEvidence]
StorageReader.resolve_replacement(object_type: str, object_id: str, scope: QueryScope) -> ReplacementResolution
StorageReader.is_tombstoned(object_type: str, object_id: str, scope: QueryScope) -> bool
```

### B.4 tombstone 表

overlay 必须提供 tombstone 表，用于屏蔽 baseline 或 overlay 中已经删除、失效、被更细粒度对象替换的对象。

建议字段：

| 字段 | 说明 |
| --- | --- |
| `tombstone_id` | 稳定 ID，例如 `sha1(object_type + object_id + scope + reason)`。 |
| `object_type` | `source`、`file`、`chunk`、`entity`、`relation`、`evidence`、`fts_row`、`vector_ref`。 |
| `object_id` | 被屏蔽对象的 logical ID。 |
| `baseline_id` | 对应 baseline 对象 ID；屏蔽 overlay-only 对象时可为空。 |
| `snapshot_id` | 适用 snapshot；全局 tombstone 可用 `all`，但 V1 尽量避免。 |
| `profile_id` | 适用 profile；非 profile-sensitive 对象为 `all`。 |
| `source_scope` | 适用 source 或 path scope，如 `workspace`、`knowledge-sources/api`。 |
| `reason` | `deleted`、`changed`、`replaced`、`source_removed`、`parser_version_changed`、`privacy_blocked`。 |
| `created_by_job` | 创建 tombstone 的 job ID。 |
| `created_at` | 创建时间。 |
| `active` | 是否生效，compact 后可置为 0 或归档。 |

规则：

- 删除 baseline 对象时只写 tombstone，不删除 baseline row。
- 文件级删除必须级联写入 file、chunk、entity、relation、evidence、fts/vector ref 的 tombstone 或由 logical view 推导屏蔽。
- tombstone 必须参与 metadata、FTS、vector、relation、evidence 所有查询路径。
- 安全阻断不应通过 tombstone 表表达，使用 path guard 和 `security.path_blocked` warning。

### B.5 replacement 表

replacement 用于表达“旧对象被新对象替代”，比 tombstone 更适合文件变更、chunk 重切、符号移动和 API 文档章节重排。

建议字段：

| 字段 | 说明 |
| --- | --- |
| `replacement_id` | 稳定 ID。 |
| `object_type` | `file`、`chunk`、`entity`、`relation`、`evidence`、`vector_ref`。 |
| `old_object_id` | 被替换的旧 logical ID。 |
| `new_object_id` | 新 logical ID。 |
| `baseline_id` | 旧 baseline 对象 ID，可空。 |
| `scope` | JSON，包含 `snapshot_id`、`profile_id`、`source_scope`、`path_scope`。 |
| `reason` | `content_changed`、`chunk_rebuilt`、`symbol_moved`、`doc_section_rekeyed`、`profile_projection_changed`。 |
| `created_by_job` | 创建 replacement 的 job ID。 |
| `created_at` | 创建时间。 |
| `active` | 是否生效。 |

规则：

- replacement 与 tombstone 可以同时存在：旧 ID 先 tombstone，再通过 replacement 指向新 ID。
- 用户或 Skill 通过旧 ID 查询时，Query Service 应解析 replacement，并在 `diagnostics` 或 `warnings` 中标记已替换。
- relation endpoint 指向旧 ID 时，logical relation view 必须解析到新 ID；如果无法解析，返回 dangling relation 检查错误。
- compact 后可以将 replacement 物化到 overlay 新表，但 compact 前不能删除 active replacement。

### B.6 relation 跨库解析

relation endpoint 可指向 baseline 或 overlay entity。查询时必须通过 logical entity view 解析 endpoint，避免 dangling edge。

relation logical view 规则：

1. 读取 overlay relation 和 baseline relation。
2. 对 relation 自身应用 tombstone/replacement。
3. 对 `src_entity_id`、`dst_entity_id` 分别执行 `resolve_replacement`。
4. 如果任一 endpoint tombstoned 且无 replacement，过滤该 relation，并在 validate 中计为 `dangling_relation`。
5. 如果 endpoint 指向 overlay 新实体，`source_index=merged`。
6. profile-conditioned relation 还需按 profile scope 过滤。

伪代码：

```python
def logical_relation(row, scope):
    if is_tombstoned("relation", row.relation_id, scope):
        return None
    src = resolve_replacement("entity", row.src_entity_id, scope)
    dst = resolve_replacement("entity", row.dst_entity_id, scope)
    if is_tombstoned("entity", src.id, scope) or is_tombstoned("entity", dst.id, scope):
        return None
    if not entity_exists(src.id, scope) or not entity_exists(dst.id, scope):
        return DanglingRelation(row.relation_id, src.id, dst.id)
    return row.with_endpoints(src.id, dst.id)
```

### B.7 FTS 合并规则

FTS 查询必须等价于：

```text
baseline FTS + overlay FTS - active tombstones - replaced old rows
```

规则：

- baseline 和 overlay 可维护独立 FTS 表，查询时进行候选合并和去重。
- overlay 同 ID FTS row 覆盖 baseline row。
- tombstoned chunk/entity/doc row 不得进入候选。
- replacement 后旧 row 不得进入候选；如需要兼容旧 ID，候选应指向 `new_object_id`。
- FTS 结果必须包含 `source_index`、`match_source=fts`、`logical_object_id`、`physical_object_id`、`score`。
- 排序时先在各自 index 内打分，再做归一化或 RRF；不得因为 overlay 表较小而天然压过 baseline，除非同 ID 覆盖。

FTS validate 必须检查：

- 每个 live chunk/entity/doc 都有对应 FTS row，除非明确 `fts_disabled`。
- 每个 FTS row 指向 live logical object。
- tombstoned/replaced old object 不出现在 FTS 查询结果中。
- baseline/overlay FTS schema version 与 metadata schema version 兼容。

### B.8 vector 合并规则

向量查询必须等价于：

```text
baseline LanceDB + local delta - active tombstones - replaced old vector refs
```

规则：

- baseline vector store 与 local delta vector store 分开维护。
- local delta 只保存 overlay 新增或变化 chunk/module summary/knowledge card 的向量。
- candidate payload 必须包含 `logical_object_id`、`chunk_id`、`source_index`、`embedding_model_version`、`content_hash`。
- vector 初召回后必须回 metadata logical view 过滤 tombstone/replacement/profile/snapshot/source_scope。
- embedding model version 不匹配时返回 `embedding.version_mismatch`，并禁用不兼容 collection 或建议 rebuild。
- replacement/tombstone 不要求立即物理删除 vector row，但必须在 query filter 中屏蔽。
- `rebuild --vectors` 可重建 delta 或 baseline vectors；普通 rebuild 不写 baseline，除非 `baseline publish` 流程。

vector validate 必须检查：

- live vector payload 能解析到 live chunk/entity/evidence。
- payload embedding model version 与 manifest 匹配。
- tombstoned/replaced old vector 不会通过 logical filter 返回。
- delta collection 与 overlay metadata 中的 embedding refs 一致。

### B.9 migration 原则

所有持久化产物迁移必须满足：

- **幂等**：同一 migration 重复执行不会造成重复列、重复索引、重复数据或状态错乱。
- **先备份**：迁移 SQLite、manifest、LanceDB metadata 前先创建本地备份或可恢复 snapshot。
- **先校验后切换**：新 schema 写入成功并通过 validate 后，再更新 manifest version。
- **失败不破坏旧索引**：迁移失败时保留旧 DB/vector 可查询，返回 `schema.incompatible_version` 或 `storage.unavailable`。
- **大版本人工确认**：baseline major schema migration 需要 `--confirm` 或 CI release，不由普通 init 静默执行。
- **overlay 优先迁移**：local overlay 可自动迁移；baseline 默认只读，除非 baseline publish/migrate 流程。
- **跨库同步**：metadata schema、FTS schema、vector payload schema 和 manifest version 必须作为一组兼容性检查。

migration 记录表建议字段：

| 字段 | 说明 |
| --- | --- |
| `migration_id` | 稳定 migration ID。 |
| `from_version` | 迁移前版本。 |
| `to_version` | 迁移后版本。 |
| `target` | `baseline_metadata`、`overlay_metadata`、`jobs`、`fts`、`vectors`、`manifest`。 |
| `status` | `pending`、`running`、`applied`、`failed`、`rolled_back`。 |
| `backup_ref` | 备份路径或 snapshot 引用。 |
| `started_at` / `finished_at` | 时间。 |
| `error_summary` | 短错误摘要，不含敏感路径或 stack trace。 |

### B.10 clean、compact 与 rebuild

清理策略：

| 命令 | 可清理 | 不可清理 | 要求 |
| --- | --- | --- | --- |
| `clean --cache` | `.active-kb/local/cache/`、tmp、过期 parser/embedding 中间产物 | baseline、overlay metadata、active vectors | 可直接清；下次按需重建。 |
| `clean --old-snapshots --keep N` | local artifacts、old local snapshots、old jobs | active snapshot/profile、baseline release artifacts | 需保留 manifest 引用的 active 数据。 |
| `clean --logs` | 轮转后的 local logs | 当前 job logs、audit/security 必需保留窗口 | 遵守审计保留策略。 |
| `compact --overlay` | 已失效 tombstone/replacement、被新版本覆盖的 overlay rows | active tombstone/replacement、baseline | compact 前后 validate 必须通过。 |
| `rebuild --fts` | FTS 表 | metadata source of truth | 从 logical live chunks/entities/doc 重建。 |
| `rebuild --vectors` | local delta vectors | baseline vectors，除非 baseline publish | 从 live overlay chunks 重建 delta。 |

compact 规则：

- 只有当 replacement 已物化、旧 ID 不再被 active relation/evidence/query history 引用时，才能清理对应 tombstone/replacement。
- compact 必须生成 report，列出删除数量、保留 active tombstone/replacement 数、validate 结果。
- compact 失败不得影响查询；失败后保留原 overlay。
- baseline compact 只允许在 CI/release baseline build 中执行。

### B.11 一致性检查

`active-kb validate`、`ops_validate_setup` 和 `baseline validate` 必须覆盖以下检查：

| 检查项 | 失败含义 | 默认严重性 |
| --- | --- | --- |
| manifest/schema mismatch | manifest、SQLite、FTS、vector schema 不兼容。 | blocked 或 degraded，取决于是否可降级。 |
| source hash mismatch | source docs/workspace hash 与 manifest 不一致。 | degraded |
| orphan relation | relation endpoint 指向不存在 entity，且无 replacement。 | degraded |
| dangling evidence | evidence 指向不存在 file/chunk 或越权 path。 | degraded 或 blocked |
| tombstone leak | tombstoned object 仍可被 metadata/FTS/vector 查询返回。 | blocked for contract |
| replacement loop | replacement A->B->A 或链路超过阈值。 | blocked for affected object |
| FTS/chunk mismatch | live chunk/entity 与 FTS row 不一致。 | degraded |
| vector ref missing | live embedding_ref 找不到 vector payload。 | degraded |
| vector stale | vector payload content_hash 或 embedding model 不匹配。 | degraded |
| relation profile mismatch | profile-conditioned relation 不在目标 profile 可见范围。 | caution/degraded |
| baseline write drift | 普通 job 修改了 baseline manifest/db/vector mtime/hash。 | blocked |
| overlay lock stale | job lock 长时间未释放。 | caution |

validate 输出必须包含：

```json
{
  "schema_version": "validate_report.v1",
  "status": "ok|degraded|blocked",
  "checked_at": "2026-05-06T00:00:00Z",
  "baseline_id": "active-main-...",
  "overlay_id": "local-...",
  "checks": [
    {
      "check_code": "storage.tombstone_leak",
      "severity": "blocked",
      "message": "A tombstoned chunk is still returned by FTS.",
      "affected_objects": ["chunk:..."],
      "suggested_action": "Run rebuild --fts or compact --overlay after fixing metadata."
    }
  ]
}
```

### B.12 增量删除和替换测试用例

V1 至少需要以下 fixture：

| case | 初始状态 | 操作 | 期望 |
| --- | --- | --- | --- |
| delete baseline file | baseline 有 file/chunk/entity/relation/evidence/FTS/vector | 本机删除文件并增量索引 | overlay 写 tombstone；logical view 不返回旧对象；baseline 物理未变。 |
| replace baseline chunk | baseline doc section 行号或 hash 变化 | 重新 chunk | overlay 写 replacement；旧 chunk FTS/vector 被过滤；旧 ID 可解析到新 ID。 |
| move symbol | baseline entity 在文件 A，本机移动到文件 B | 增量索引文件 A/B | old entity tombstone 或 replacement；relation endpoint 指向新 entity。 |
| delete overlay object | overlay-only 文件被删除 | 增量索引 | overlay row 和 FTS/vector ref 可物理删除或 tombstone；query 不返回。 |
| relation dangling prevention | baseline relation 指向被删除 entity | tombstone entity | logical relation 被过滤，validate 报告 dangling_relation 或 tombstone filtered count。 |
| vector tombstone filter | baseline vector 命中旧 chunk | tombstone chunk | vector 初召回可命中物理 row，但 logical filter 必须移除。 |
| FTS tombstone filter | baseline FTS 命中旧 doc | tombstone doc chunk | FTS 结果不返回旧 chunk。 |
| compact overlay | overlay 有 inactive tombstone/replacement | compact | active 行保留，inactive 安全清理，validate 通过。 |

### B.13 实现边界

- MCP tools、Skill 和 Query Router 不得读取 SQLite 表名、LanceDB collection 名称或 artifact 内部路径。
- `query/*` 只能通过 `storage/base.py` logical read API 访问数据。
- `indexing/*` 和 `storage/*` 负责物理表、FTS、vector、manifest 的一致性维护。
- `ops_validate_setup` 可以调用 storage validate API，但输出仍使用稳定 report schema。
- audit log 记录 storage warning code、validate summary 和 job ID，不记录大段源码或绝对敏感路径。

---

## 附录 C：Profile 规范

本附录定义 profile 的标识、发现、`auto` 选择、多 profile 展示、重算边界和 unresolved 行为。它是 `connectors/build_outputs`、`indexing/profile_collector`、`query/router`、`config_impact`、`workspace_view(view=profile)` 和 Skill 路由共同依赖的契约。

### C.1 设计目标

- **确定性**：同一 workspace、config 和参数输入下，`profile_id=auto` 必须得到同一结果或同一候选列表。
- **可解释**：profile 选择结果必须返回来源、hash、候选、warning 和 route trace。
- **不随机**：多个 `.config` 或 profile 候选时不得静默随机选择。
- **最小重算**：`.config` 变化只重算 profile 相关投影，不重算无关文档 chunk 和无关向量。
- **profile-sensitive 明确**：代码可达性、宏影响、调用关系、runtime flow、config impact 必须绑定 profile 或显式返回 unresolved warning。

### C.2 profile 标识和主键

profile 记录必须同时有用户可读 ID 和可复现主键。

```text
profile_identity_key = snapshot_id + profile_id + defconfig_hash + dotconfig_hash
profile_record_id    = sha1(profile_identity_key)
```

字段要求：

| 字段 | 说明 |
| --- | --- |
| `snapshot_id` | profile 所属代码宇宙。 |
| `profile_id` | 稳定可读 ID，推荐 `<board>_<app>` 或 `<app>@<board>` 规范化形式，例如 `mhs003_watch`。 |
| `profile_record_id` | 基于 identity key 的物理记录 ID，用于区分同名 profile 的配置变化版本。 |
| `board` | 从 defconfig、`.config` 或配置映射解析出的 board。 |
| `app` | watch、sensorhub 等 app/target 维度。 |
| `defconfig_path` | source-root 相对路径，可为空但必须有 warning。 |
| `defconfig_hash` | defconfig 内容 hash；缺失时为 `missing`。 |
| `dotconfig_path` | `.config` source-root 相对路径。 |
| `dotconfig_hash` | `.config` 内容 hash；缺失时为 `missing`。 |
| `config_hash` | `defconfig_hash + dotconfig_hash + normalized_macro_summary` 的综合 hash。 |
| `macro_summary_json` | 关键宏、enabled/disabled/unknown 摘要。 |
| `source` | `explicit`、`cli_env`、`local_config`、`dotconfig_scan`、`baseline_default`、`manual_seed`。 |
| `priority` | 可选排序权重，数值越小越优先或按配置约定。 |
| `last_used_at` | 最近被显式选中的时间。 |
| `status` | `resolved`、`candidate`、`stale`、`unresolved`、`invalid`。 |

约束：

- `profile_id` 用于用户和 Skill 展示；`profile_record_id` 用于存储版本区分。
- 同一 `profile_id` 在同一 `snapshot_id` 下如果 hash 变化，必须生成新的 `profile_record_id`，并保留或归档旧记录。
- 查询返回中可展示 `profile_id`，诊断中可返回 `profile_record_id`。

### C.3 `default_profile=auto` 选择顺序

`profile_id` 解析输入：

```json
{
  "requested_profile_id": "auto",
  "snapshot_id": "current",
  "caller_tool": "config_impact",
  "client_context": {
    "active_file": "packages/services/bluetooth/bt_service.c",
    "cwd": "packages/services/bluetooth"
  }
}
```

选择顺序：

1. **显式参数**：tool 参数 `profile_id=<id>`、`compare_to=<id>`。
2. **CLI/env**：`--profile`、`ACTIVE_KB_PROFILE`、启动参数。
3. **local config**：`.active-kb/local/config/active-kb.local.yaml` 中的 `project.default_profile` 或 tool-specific profile。
4. **当前 `.config` 可信候选**：按 C.4 扫描并判断可信。
5. **baseline 默认 profile**：baseline manifest 中标记的 default profile。
6. **unresolved**：无法唯一确定，返回候选和 `profile.unresolved` 或 `profile.multiple_candidates` warning。

解析输出：

```json
{
  "requested": "auto",
  "status": "resolved",
  "resolved_profile_id": "mhs003_watch",
  "profile_record_id": "prof_...",
  "source": "dotconfig_scan",
  "confidence": 0.91,
  "candidates": [],
  "warnings": []
}
```

状态枚举：

| status | 含义 |
| --- | --- |
| `not_required` | 查询不依赖 profile，例如纯产品文档检索。 |
| `resolved` | 唯一 profile 已确定。 |
| `multiple_candidates` | 多个可信候选，需要用户或 Skill 选择。 |
| `unresolved` | 没有可信候选。 |
| `stale` | 有候选，但 hash/mtime/manifest 显示可能过期。 |
| `invalid` | 显式 profile 不存在或与 snapshot 不兼容。 |

### C.4 `.config` 可信候选判定

`.config` 候选来源：

- 配置文件中的 `profiles.auto.dotconfig_candidates`。
- 默认路径：`build/.config`、`build/out_hub/.config`。
- build output connector 发现的 `.config`。
- IDE/client context 指向的 build output。

可信候选必须同时满足：

| 条件 | 判定 |
| --- | --- |
| 文件存在 | 路径在 allowlist 内，且不是坏 symlink。 |
| hash 可读 | 能计算 `dotconfig_hash`，读取失败则 invalid。 |
| 可解析 app/board | 能从宏、路径、front matter 或 local profile mapping 中解析 `app` 和 `board`。 |
| mtime 可信 | mtime 新于最近 profile scan，或与 manifest 记录一致。 |
| macro parse 成功 | 能解析关键 `CONFIG_*`，失败则 stale/invalid。 |
| snapshot 兼容 | `.config` 对应 workspace/snapshot 与当前查询 snapshot 不冲突。 |

可信度建议：

| 情况 | status | confidence |
| --- | --- | --- |
| hash、app、board、mtime、manifest 全匹配 | `candidate` | `>= 0.90` |
| hash 可读且 app/board 可解析，但 mtime 旧 | `stale` | `0.60-0.79` |
| 只能从路径猜 app/board | `stale` | `0.50-0.69` |
| hash 不可读或 app/board 不可解析 | `invalid` | `< 0.50` |

### C.5 多 `.config` 和多 profile 候选

当存在两个以上可信候选：

- 不得静默选择第一个或随机选择。
- 返回 `profile_resolution.status=multiple_candidates`。
- warning 至少包含 `profile.multiple_candidates`。
- `candidates` 必须稳定排序。

排序规则：

1. 显式 `profile.priority`。
2. `last_used_at` 越新越靠前。
3. 与 `client_context.active_file` 或 `cwd` 的 module/domain 相关度。
4. `.config` mtime 越新越靠前。
5. `profile_id` 字典序，作为最终稳定 tie-breaker。

候选结构：

```json
{
  "profile_id": "mhs003_watch",
  "profile_record_id": "prof_...",
  "app": "watch",
  "board": "mhs003",
  "defconfig_path": "configs/mhs003/watch_defconfig",
  "dotconfig_path": "build/.config",
  "defconfig_hash": "sha256:...",
  "dotconfig_hash": "sha256:...",
  "source": "dotconfig_scan",
  "priority": 10,
  "last_used_at": "2026-05-06T00:00:00Z",
  "match_reason": "dotconfig candidate from configured path",
  "confidence": 0.93
}
```

### C.6 multi-profile 查询展示

profile diff、config impact、feature availability、workspace profile view 等 multi-profile 查询必须按 profile 分组展示。

每个 profile 的结果必须包含：

| 字段 | 说明 |
| --- | --- |
| `profile_id` | profile 可读 ID。 |
| `profile_record_id` | profile 记录 ID。 |
| `status` | `enabled`、`disabled`、`unknown`、`not_applicable`。 |
| `macro_diff` | 与比较对象或 baseline default 的关键宏差异。 |
| `affected_modules` | 受影响 build module 列表。 |
| `affected_files` | 可选，受影响文件列表。 |
| `affected_symbols` | 可选，受影响符号列表。 |
| `condition_expr` | 相关 Kconfig/`#if` 条件表达式。 |
| `evidence_refs` | defconfig、`.config`、Config.in、module.mk、源码条件的证据。 |
| `warnings` | profile-specific warning。 |

示例：

```json
{
  "result_status": "ok",
  "query_intent": "profile_diff",
  "profile_id": "multi",
  "items": [
    {
      "profile_id": "mhs003_watch",
      "status": "enabled",
      "macro_diff": [{"macro": "CONFIG_BT", "value": "y", "compare_to": "n"}],
      "affected_modules": ["packages/services/bluetooth"],
      "evidence_refs": ["ev_config_bt_watch"]
    },
    {
      "profile_id": "mhs003_sensorhub",
      "status": "disabled",
      "macro_diff": [{"macro": "CONFIG_BT", "value": "n", "compare_to": "y"}],
      "affected_modules": [],
      "evidence_refs": ["ev_config_bt_sensorhub"]
    }
  ]
}
```

展示规则：

- 不同 profile 的 `enabled/disabled/unknown` 不得混成一个结论。
- `unknown` 必须说明是宏缺失、条件无法解析、profile 未索引还是 source 缺失。
- 证据至少包含 defconfig 或 `.config`；影响模块还应引用 Config.in/module.mk 或源码条件。

### C.7 profile 变更的增量重算边界

只变 `.config` 或 defconfig 展开结果时，必须重算：

- `profile` 记录和 `profile_record_id`。
- `macro_summary_json`。
- macro diff index。
- profile-conditioned relations，例如 `guarded_by_macro`、`enabled_by`、`disabled_by`。
- module/file/symbol 可达性投影。
- feature/domain/profile view materialization。
- `config_impact` 缓存。
- profile-specific evidence bundle index。
- 与 profile 相关的 FTS metadata 字段或 filter payload。

不得重算，除非源文件或版本也变化：

- 无关 source docs chunks。
- 文档向量。
- 与 profile 无关的 code chunks。
- 与 profile 无关的 learned cards。
- workspace snapshot inventory。

可能需要重算：

| 变化 | 重算范围 |
| --- | --- |
| `.config` hash 变化 | profile、macro summary、profile projections。 |
| defconfig hash 变化 | profile、macro summary、profile projections。 |
| Config.in/Kconfig 文件变化 | affected macro definitions、dependency graph、profile projections。 |
| module.mk/build rule 变化 | build module reachability、file/module projection。 |
| compile DB profile variant 变化 | relation confidence、actual compiled files、include/macro context。 |

### C.8 触发全量重算边界

以下变化触发全量或大范围重算：

| 变化 | 最小动作 |
| --- | --- |
| parser version 变化 | 重建对应语言 parse result、entities、chunks、relations。 |
| extractor version 变化 | 重建受影响 extractor 产物和 relation。 |
| build module 规则变化 | 重建 module graph、profile reachability。 |
| Config.in/Kconfig parser version 变化 | 重建 macro dependency graph 和 profile projections。 |
| compile DB schema/version 变化 | 重建 compile-aware symbol/ref/relation。 |
| baseline snapshot 变化 | 新 snapshot 全量索引或复用兼容 baseline 后增量。 |
| embedding model 变化 | 重建对应 vector collections。 |
| chunking 策略变化 | 重建 chunks、FTS、vectors、evidence refs。 |
| source docs hash 变化 | 重建受影响 doc chunks、FTS、vectors。 |
| storage schema major 变化 | 按附录 B migration 规则执行。 |

判断原则：

- 影响文本切块或 embedding payload 的变化必须重建 vector。
- 影响实体 ID 或 relation endpoint 的变化必须重建 relation 和 evidence mapping。
- 只影响 profile 可达性的变化不重建无关 doc chunks 和 doc vectors。

### C.9 unresolved profile 行为

当 profile 无法解析：

- profile-insensitive 查询可以继续，例如 `docs_search(doc_type=api)`、`product_context`、`project_context`、纯 `workspace_nav`。
- profile-sensitive 查询不得输出 profile-specific 结论，例如 `profile_diff`、`config_impact`、`runtime_flow`、带宏条件的 `code_trace`。
- QueryResult 必须包含 `profile_id="unresolved"` 或 `profile_resolution.status="unresolved"`。
- warnings 必须包含 `profile.unresolved`；多个候选时使用 `profile.multiple_candidates`。
- `next_queries` 或 `suggested_filters` 必须提示可选 profile。

示例：

```json
{
  "profile_resolution": {
    "requested": "auto",
    "status": "multiple_candidates",
    "resolved_profile_id": null,
    "candidates": [
      {"profile_id": "mhs003_watch", "dotconfig_path": "build/.config", "confidence": 0.93},
      {"profile_id": "mhs003_sensorhub", "dotconfig_path": "build/out_hub/.config", "confidence": 0.91}
    ]
  },
  "warnings": [
    {
      "level": "caution",
      "code": "profile.multiple_candidates",
      "message": "Multiple profile candidates were found; no profile was selected automatically.",
      "actionable": true,
      "suggested_action": "Specify profile_id=mhs003_watch or profile_id=mhs003_sensorhub."
    }
  ]
}
```

### C.10 契约测试要求

D0-06 对应的 tests 至少覆盖：

- 显式 `profile_id` 优先于所有 auto 来源。
- CLI/env 优先于 local config。
- local config 优先于 `.config` scan。
- 单个可信 `.config` 可解析为 `resolved`。
- 两个可信 `.config` 返回 `multiple_candidates`，排序稳定。
- `.config` 缺 hash、缺 app/board 或 mtime 过期时返回 stale/invalid。
- baseline default profile 只在无可信本地候选时使用。
- profile-sensitive 查询遇到 unresolved 必须返回 warning 且不生成 profile-specific 结论。
- profile-insensitive 查询遇到 unresolved 可以继续，但必须标记 `profile_id=unresolved` 或 `not_required`。
- `.config` hash 变化只触发 C.7 列出的 profile 重算范围。
- parser/extractor/build rule/compile DB/baseline snapshot/embedding model 变化触发 C.8 列出的全量或大范围重算。

---

## 20. 参考资料

- FastMCP Tools：https://fastmcp.wiki/en/servers/tools
- FastMCP Resources：https://fastmcp.wiki/en/servers/resources
- MCP Python SDK：https://py.sdk.modelcontextprotocol.io/
- MCP Transport 规范：https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- MCP Authorization 规范：https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- MCP Interceptors Charter：https://modelcontextprotocol.io/community/interceptors/charter
- clangd compile commands：https://clangd.llvm.org/design/compile-commands
- clangd index：https://clangd.llvm.org/design/indexing
- Tree-sitter：https://github.com/tree-sitter/tree-sitter
- Qdrant Hybrid Search with Reranking：https://qdrant.tech/documentation/tutorials-search-engineering/reranking-hybrid-search/
- LanceDB Hybrid Search：https://docs.lancedb.com/search/hybrid-search
- SQLite FTS5：https://www.sqlite.org/fts5.html
- SQLite Vec1：https://sqlite.org/vec1
- LlamaIndex Routing：https://developers.llamaindex.ai/python/framework/module_guides/querying/router/
- Microsoft Advanced RAG：https://learn.microsoft.com/en-us/azure/developer/ai/advanced-retrieval-augmented-generation
- From BM25 to Corrective RAG, arXiv 2604.01733：https://arxiv.org/abs/2604.01733
