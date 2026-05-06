# RTOS 多仓嵌入式工程知识库方案（对话汇总版）

> 文档用途：作为进入真实工程环境之前的阶段性结论，用于后续需求拆分、工程调研、方案细化与实施排期。  
> 当前状态：**架构级设计已形成，工程细节待在真实项目环境中确认。**

---

## 1. 背景与目标

目标是为一个**基于 RTOS 的、多仓（repo 管理）的嵌入式工程**设计一套可被 Agent 高效利用的知识系统，使其能够：

- 快速理解跨层、跨模块的功能链路。
- 在多 build profile / 多仓 / 多语言（C/C++、汇编、脚本、Python）环境下进行准确检索。
- 将代码知识与文档知识统一组织起来。
- 通过 Skill 固化检索与回答工作流，让 Agent 更稳定地使用外部知识能力。

本轮对话形成的核心认识是：

**不要把 Skill 当知识库本体；要把 Skill 当“知识库调用与推理编排器”。**

---

## 2. 最终共识：推荐采用的整体架构

### 2.1 一句话定义

推荐架构为：

**外部工程知识库系统 + MCP 接口层 + 知识 Skill**

更精确地分层如下：

1. **离线构建 / 索引层**  
   负责扫描工程、解析构建上下文、抽取代码关系、抽取文档结构、构建图谱与索引。

2. **知识库服务层**  
   负责存储与查询：符号索引、引用关系、RTOS 语义图、文档库、向量索引、profile 元数据等。

3. **MCP 接口层**  
   将知识库能力暴露成可供 ChatGPT / Agent 调用的工具接口。

4. **Skill 层**  
   负责告诉 Agent：什么时候调用知识库、优先调用什么、如何组合证据、答案如何组织。

### 2.2 正确理解方式

可以将原先的想法：

- “构建一个外部知识库 MCP，MCP 负责构建多层次工程图和知识文档”
- “构建一个知识库的 Skill，让 Agent 自主使用 MCP”

修正为：

- **构建一个外部知识库系统**，由它负责构建多层次工程图和知识文档。
- **构建一个 MCP server / connector / app**，把这些查询能力暴露出去。
- **构建一个 Skill**，把“如何使用这个知识库”固化成稳定工作流。

因此：

- **MCP 不是知识库本体**；它是知识库的对外访问协议 / 接口层。
- **Skill 不是资料仓库**；它是 Agent 的工作流控制层。

---

## 3. Skill、知识库、MCP 的职责边界

### 3.1 Skill 负责什么

Skill 适合承载：

- 问题分类规则
- 检索路由策略
- 输出模板
- 质量检查清单
- 少量稳定的术语表 / 强约束规范
- 调用知识库 MCP 的步骤说明与优先级

Skill 最擅长的是：

- “这是调用链追踪还是配置影响分析？”
- “先查 symbol 还是先查文档？”
- “回答必须给出哪些证据和前提条件？”

### 3.2 知识库负责什么

知识库应该承载：

- 源码与结构化代码索引
- 文档、API 说明、组件说明、开发规范
- 构建 profile 信息
- RTOS 运行语义关系
- 多仓 manifest / revision / workspace snapshot
- 代码与文档之间的关联关系

知识库最擅长的是：

- “这个函数在哪些 profile 下定义/引用？”
- “这个 ISR 最终影响哪个任务/模块？”
- “这个宏关闭后影响哪些功能链路？”
- “这段 API 说明对应哪些代码实体？”

### 3.3 MCP 负责什么

MCP 层负责：

- 对外暴露知识库查询工具
- 统一调用入口
- 让外部 Agent / ChatGPT 可以通过工具方式访问知识能力

MCP 不应承担：

- 大规模离线图谱构建
- 长周期索引流水线管理
- 知识库本体存储与计算

---

## 4. 文档类知识（API 文档、控件/组件文档、开发规范）怎么处理

### 4.1 结论

**主体融入知识库；少量高价值规则放进 Skill。**

### 4.2 具体建议

以下内容应作为**知识库的一等内容**进入外部知识系统：

- API 文档
- 组件/控件文档
- 协议文档
- 开发规范、编码规范、架构约束、评审规范
- FAQ、版本说明、迁移说明

原因：

- 内容体量通常较大
- 会随着版本持续变化
- 往往与具体代码实体、模块、profile 有关联
- 需要按证据检索，而不是只作为固定提示词

### 4.3 为什么不能主要放在 Skill 里

如果把完整文档主要封装到 Skill 中，会出现这些问题：

- 上下文膨胀
- 难以版本化
- 更新成本高
- 不利于与代码实体建立双向关联
- 不利于按 profile / 版本 / 模块精确检索

### 4.4 最适合的双层处理方式

以“开发规范”为例：

- **完整规范正文**：放知识库
- **提炼出的强约束规则**：放 Skill

例如：

- “ISR 中不得阻塞等待” → 可作为 Skill 中的稳定检查规则
- “完整编码规范正文与例外说明” → 保存在知识库中，按需检索引用

---

## 5. 为什么知识库应在工程外部运行

### 5.1 结论

**知识库通常应在工程外部运行。**

更准确地说：

- 源码仓库仍然是 source of truth
- 知识库索引器、图谱构建器、查询服务是外部进程/服务
- 索引结果存放在独立数据库/缓存中
- Skill / Agent 通过 MCP 查询这些结果

### 5.2 原因

这样设计的主要收益：

- 不污染工程代码仓库
- 索引、图谱、文档处理可以独立演进
- 可按 workspace snapshot / build profile 生成多套索引
- 便于集中部署和远程访问
- 更容易支持大型多仓工程

### 5.3 对你们工程的意义

由于你们是：

- RTOS 项目
- repo 管理的多仓工程
- 多语言混合（C/C++、汇编、脚本、Python）
- 很可能存在多 board / 多 profile / 多 feature 宏

所以“外部知识库系统”几乎是更合理的默认选项。

---

## 6. 知识库技术路线：推荐分层技术栈

### 6.1 总体思路

知识库不应只做“代码切块 + 向量检索”，而应采用：

**编译感知索引 + 精确符号检索 + RTOS 语义图 + 文档知识关联 + 向量检索补充**

### 6.2 推荐技术分层

#### A. 工程快照与构建上下文层

输入建议包括：

- repo manifest / workspace snapshot
- 每个 project 的 revision / commit SHA
- local manifest / submanifest
- build profile 信息
- include path / macro defines / toolchain
- linker script / startup files / map file
- feature/config headers

这一层的核心目的不是检索，而是定义“这次知识库索引到底针对哪个代码宇宙”。

#### B. 代码解析与结构抽取层

建议组合：

- **C/C++：clang / libTooling / clangd-compatible 索引**
- **汇编、构建脚本、配置文件：tree-sitter + 自定义规则**
- **Python：AST + 依赖抽取 + 自定义规则**
- **兜底工具：ctags / ripgrep / 正则抽取**

##### 推荐原因

- C/C++ 的高质量关系抽取需要编译感知能力。
- 汇编和脚本通常更适合“语法 + 规则”路线。
- 多语言统一后更容易做跨文件、跨模块、跨仓关系映射。

#### C. 知识库存储层

建议至少拆成四类：

1. **原始证据库**  
   源码、文档、配置、构建产物、manifest、提交说明等。

2. **结构化索引库**  
   symbol、ref、宏、文件、模块、profile 元数据。

3. **图关系库**  
   调用关系、依赖关系、RTOS 语义关系、文档关联关系。

4. **向量索引库**  
   用于模块摘要、文档摘要、FAQ、经验总结等说明性内容。

> 重要建议：原始 C 代码不应主要依赖 embedding 做主检索；主检索应优先使用符号索引与结构图谱。

#### D. 查询服务层

建议提供可编排的查询 API，例如：

- `resolve_symbol()`
- `search_code()`
- `search_docs()`
- `trace_path()`
- `impact_of_macro()`
- `diff_profiles()`
- `get_neighbors()`
- `explain_module()`

#### E. MCP 接口层

把查询服务封装为 MCP 工具，供 Agent / ChatGPT 调用。

#### F. Skill 层

在 Skill 中固化：

- 何时使用哪些 MCP 工具
- 各问题类型的调用顺序
- 证据组合逻辑
- 回答模板与检查清单

---

## 7. 为什么不能只做普通 RAG

对你们的工程场景，普通“代码切块 + 向量检索”通常会在以下场景失效：

1. **同名函数/宏太多**  
   没有 build profile 和仓级上下文时，容易命中错误实体。

2. **RTOS 语义分散在多个层次**  
   功能链路通常跨：ISR → driver → queue / semaphore → task → service → app。

3. **宏和编译条件决定真实路径**  
   纯语义相似度难以区分条件分支。

4. **汇编、启动文件、linker script、map file 很关键**  
   普通 RAG 对 boot path、vector table、内存布局问题支持通常较弱。

因此推荐：

- **精确检索**：符号、宏、文件、寄存器、错误码
- **结构检索**：调用链、依赖链、RTOS 上下文链
- **语义检索**：模块摘要、文档、提交说明、FAQ

---

## 8. 编译感知是高质量代码理解的前提

### 8.1 核心原则

对 C/C++ 来说：

**没有编译上下文，就没有高质量工程理解。**

### 8.2 建议的 build profile 概念

建议为知识库定义统一的 `build profile`，至少包含：

- board
- chip / SoC
- toolchain
- optimization level
- RTOS config
- feature macros
- customer / product variant

### 8.3 为什么 profile 是主维度

只有带着 profile 进入索引和查询，Agent 才能回答：

- 这是在哪个板型/产品线下成立的？
- 某个函数为什么在本地看不到？
- 为什么 A 板和 B 板的初始化链不同？

---

## 9. 针对 repo 多仓工程的关键建议

### 9.1 索引单位不应是“单个仓库”

建议把知识库的“代码宇宙”定义为：

**manifest/workspace snapshot + build profile**

而不是简单的某个 repo。

### 9.2 建议保存的元数据

每次构建知识库时，应记录：

- manifest revision
- 每个 project 的 commit SHA
- local manifest / submanifest
- workspace path 映射
- profile 集合

### 9.3 为什么这样做

这样 Agent 才能对回答进行强约束：

- 结论适用于哪个代码快照
- 适用于哪个 build profile
- 与当前 workspace 是否一致

---

## 10. 图谱不是一般调用图，而应是“嵌入式语义图”

### 10.1 推荐实体类型

建议至少建模这些实体：

- ManifestSnapshot
- BuildProfile
- Repo
- Directory
- File
- Module
- Symbol
  - function
  - macro
  - variable
  - type / struct / enum
- Task
- ISR
- Queue / Semaphore / EventGroup
- Timer
- Peripheral
- Register
- FaultCode / EventCode
- MemorySection
- LinkerRegion
- Document
- APIEntry / Rule / Guideline

### 10.2 推荐关系类型

建议至少建这些关系：

- `contains`
- `defines`
- `declares`
- `implements`
- `calls`
- `references`
- `guarded_by_macro`
- `built_under_profile`
- `belongs_to_module`
- `runs_in_context`
- `creates_task`
- `posts_to_queue`
- `waits_on_queue`
- `signals_event`
- `triggered_by_interrupt`
- `mapped_to_vector`
- `located_in_section`
- `initializes_before`
- `depends_on`
- `reports_fault`
- `used_by_variant`
- `document_describes`
- `guideline_applies_to`

### 10.3 最关键的设计要求

**每条边都应该带条件。**

例如：

- profile_id
- 宏条件
- ISR / task / boot context
- board / arch
- 仓库来源

这样才能避免“无条件调用图”带来的误导。

---

## 11. RTOS 语义必须显式建模

### 11.1 为什么通用 parser 不够

通用 parser 能告诉你“谁调用了谁”，但不能天然理解：

- 某个 API 是在创建任务
- 某个 API 是 ISR 中发消息
- 某个 wrapper 最终落到 RTOS 原语上
- 某个 callback 实际运行在哪种上下文

### 11.2 推荐增加一层 RTOS 语义抽取器

建议针对 RTOS 原语和自定义 wrapper，抽取这些语义：

- 任务创建
- 队列 / 消息通道
- 信号量 / 互斥锁
- 事件标志
- ISR 与下半部 / 工作线程关系
- timer callback
- buffer / ringbuffer / message pool 生产消费关系

### 11.3 这一层的价值

这层能力是“跨层跨模块功能理解”的关键，也是 Agent 真正答对并发链路、事件传播链路的基础。

---

## 12. 文档知识与代码知识应统一组织

### 12.1 统一纳入知识库

推荐将以下知识统一纳入同一知识系统：

- 代码
- API 文档
- 组件/控件说明
- 开发规范
- 设计说明
- FAQ / 调试手册
- 缺陷说明 / 提交记录（后续可选）

### 12.2 推荐建立的跨域关联

例如：

- API 文档章节 → 关联到函数 / 宏 / 模块
- 组件说明 → 关联到模块、任务、队列、外设
- 开发规范 → 关联到适用模块、规则类型、例外说明
- 设计说明 → 关联到初始化顺序、消息链路、模块边界

---

## 13. 推荐的检索策略：Hybrid Retrieval + Graph Expansion

### 13.1 问题进入系统后的标准流程

1. **问题分类**  
   识别问题属于哪种类型：
   - 调用链追踪
   - 宏/配置影响分析
   - 初始化/启动序列
   - 并发上下文分析
   - 模块职责解释
   - 差异分析
   - fault / event 传播分析

2. **锚点提取**  
   提取：
   - 函数名
   - 宏
   - 文件
   - 模块
   - 中断名
   - 外设名
   - task 名
   - fault code

3. **检索路由**  
   根据问题类型决定优先访问哪些索引/图谱/文档。

4. **子图扩展**  
   围绕锚点实体扩展受条件约束的局部图。

5. **证据重排**  
   优先当前 profile、当前 manifest snapshot、定义处、业务代码等高置信来源。

6. **答案生成**  
   输出结论、前提条件、关键链路、证据来源、不确定点。

### 13.2 为什么这比纯向量检索更适合嵌入式工程

因为问题本质上往往是：

**锚点实体 + 关系类型 + 条件上下文**

而不是单纯“哪段文本和我问的问题最像”。

---

## 14. Skill 的推荐职责设计

### 14.1 Skill 应内置的内容

建议在 Skill 中固化：

- 问题分类规则
- 工程领域术语表
- 何时调用 `resolve_symbol()` / `trace_path()` / `search_docs()` 等 MCP 工具
- 先查代码还是先查文档的条件
- 回答格式模板
- 质量检查清单

### 14.2 回答输出模板建议

Skill 可要求 Agent 的回答至少包含：

1. 结论
2. 成立前提（profile / 板型 / 宏 / 上下文）
3. 关键链路
4. 证据文件 / 符号 / 文档章节
5. 不确定点 / 未覆盖分支

### 14.3 这样做的直接收益

- 降低 Agent 输出的偶然性
- 降低幻觉
- 强制证据化回答
- 让不同问题遵循一致的分析路径

---

## 15. 推荐的 MCP 工具集合（V1）

可优先设计以下 MCP 工具：

- `resolve_symbol(name, profile?)`
- `search_code(query, scope?, profile?)`
- `search_docs(query, doc_type?, module?, version?)`
- `trace_call_path(src, dst?, profile?, depth?)`
- `impact_of_macro(macro, profile?)`
- `get_module_summary(module, profile?)`
- `get_neighbors(entity, relation_types?, profile?)`
- `explain_context(symbol)`
- `diff_profiles(entity_or_module, profile_a, profile_b)`
- `find_init_sequence(module_or_peripheral, profile?)`
- `trace_fault(fault_code, profile?)`

这些工具的设计目标是：

- 覆盖高频问题类型
- 保持工具语义清晰
- 便于 Skill 组合编排

---

## 16. 为什么建议先做“代码图”，再做“文档增强图”

第一阶段建议先做：

- 编译感知的代码索引
- RTOS 语义图
- profile 感知查询

然后第二阶段再增强：

- 设计文档
- API 文档
- 开发规范
- 提交记录 / 缺陷单 / FAQ

原因：

- 代码关系很多是可以通过 parser / 构建系统确定性提取的
- 文档更适合在第二阶段通过摘要、链接、图增强方式接入
- 这样更稳、更省算力、更利于评估效果

---

## 17. 最小可用版本（MVP）建议

### 17.1 覆盖范围建议

第一版尽量收敛到：

- 1 个主产品线
- 1~2 个 board/profile
- 3~5 个核心 repo
- 20~30 个真实问题样本

### 17.2 优先支持的 4 类问题

建议先做：

1. 跨层调用链追踪
2. 宏 / 配置影响分析
3. 初始化 / 启动序列分析
4. ISR / task / queue 并发链路分析

### 17.3 MVP 成功标准

- 能命中正确 profile
- 能给出较完整的链路
- 能提供代码/文档证据
- 延迟可接受
- 相比纯语义检索有明显准确率提升

---

## 18. 建议的实施阶段

### Phase 0：边界与建模

输出内容：

- manifest snapshot 规范
- build profile 规范
- 模块边界定义
- RTOS 原语映射表
- 问题类型 taxonomy

### Phase 1：编译感知索引

输出内容：

- compile DB / 等价编译数据库
- C/C++ symbol/ref 索引
- repo/path/commit/profile 元数据
- 基础代码证据库

### Phase 2：RTOS 语义图

输出内容：

- task / isr / queue / semaphore / timer / fault 关系图
- startup / vector / linker / map 关系图
- 带 profile 条件的边

### Phase 3：查询服务与 MCP

输出内容：

- 查询 API
- MCP tool schema
- 统一返回格式
- 证据定位接口

### Phase 4：Skill 接入

输出内容：

- 问题分类规则
- 工具调用编排
- 回答模板
- 质量检查清单

### Phase 5：文档与经验增强

输出内容：

- API 文档索引
- 规范索引
- FAQ / issue / commit / debug note 接入
- 代码图与文档图的跨域关联

---

## 19. 当前已明确的关键原则

1. **Skill 管“怎么做”，知识库管“知道什么”。**
2. **知识库应在工程外部运行。**
3. **MCP 是接口层，不是知识库本体。**
4. **文档主体进入知识库，规则摘要进入 Skill。**
5. **索引单位应是 manifest/workspace snapshot + build profile。**
6. **编译感知是 C/C++ 工程理解的基础。**
7. **图谱应是 RTOS/嵌入式语义图，而不只是调用图。**
8. **主检索应以精确索引与结构图谱为核心，向量检索仅作补充。**
9. **先做高价值 MVP，再逐步扩展到更多仓、更多 profile、更多文档资产。**

---

## 20. 进入真实工程环境后优先确认的事项

进入工程后，建议优先确认以下内容：

### A. 工程与构建

- repo manifest 结构
- 仓库数量与职责划分
- build 系统类型（CMake / Make / SCons / 自研脚本 / IDE 工程）
- 是否能稳定导出 compile_commands.json 或等价编译命令清单
- 主要 build profile 数量与命名规则

### B. RTOS 与运行模型

- RTOS 类型（FreeRTOS / ThreadX / RTX / 自研 RTOS 等）
- 是否有统一 wrapper 层
- 任务 / 队列 / 信号量 / 事件 / timer 的封装方式
- ISR 与工作线程 / 回调的组织习惯

### C. 代码组织方式

- 模块边界如何划分
- driver / middleware / service / app 的层级约定
- generated/vendor/third-party 代码比例
- 启动文件 / linker script / map file 的获取方式

### D. 文档资产

- API 文档格式与存放位置
- 组件/控件文档来源
- 开发规范是否版本化
- 是否有设计说明 / FAQ / issue / commit 记录可接入

### E. 业务问题优先级

- 最希望 Agent 优先解决的前 10~20 个问题是什么
- 哪些问题当前最耗人工排查时间
- 哪些问题最需要“跨层跨模块理解”

---

## 21. 建议的下一轮需求拆分方式

在进入真实工程环境后，下一轮需求拆分建议按以下顺序进行：

1. **工程盘点**  
   识别仓库结构、build profile、文档资产、RTOS 封装方式。

2. **高价值问题归类**  
   收集真实问题样本，建立问题 taxonomy。

3. **索引与图谱 MVP 边界确定**  
   选择 1 条产品线、1~2 个 profile、3~5 个核心 repo。

4. **MCP 工具定义**  
   先定义 V1 工具清单与 I/O 模式。

5. **Skill 工作流设计**  
   固化问题分类、工具调用顺序、证据模板。

6. **评测集与验收指标**  
   用真实问题衡量命中率、链路完整率、证据质量、延迟表现。

---

## 22. OpenAI 相关产品假设（供后续实施时复核）

以下是本轮对话中涉及的、与 OpenAI 产品集成相关的**当前假设**；在真正落地前应再次核对官方文档，因为平台能力可能变化：

1. **Skills 的定位**：Skills 是可复用、可共享的工作流，可包含说明、示例和代码，并可在适当时被自动使用。  
   参考：[Skills in ChatGPT](https://help.openai.com/ko-kr/articles/20001066-skills-in-chatgpt)

2. **MCP / 自定义 connector 的定位**：MCP 用于把外部数据源或能力接入 ChatGPT / Agent 工作流。  
   参考：[MCP Overview](https://platform.openai.com/docs/mcp/overview)

3. **当前接入限制（需复核）**：ChatGPT 中的自定义 MCP / apps / connectors 涉及 developer mode、计划权限、远程 server 支持等限制；某些模式下对 agent mode / deep research 的支持存在差异。  
   参考：
   - [Developer mode, apps and full MCP connectors in ChatGPT [beta]](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta)
   - [Connectors in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt)

> 建议：在真正实施“知识库 MCP + Skill”接入前，先确认你们实际使用的 OpenAI 产品形态、账号计划、部署模式与权限模型。

---

## 23. 当前阶段的最终结论

本轮对话后，适合作为后续工程设计输入的最终结论如下：

### 结论 1

应该建设的是：

**外部工程知识库系统 + MCP 接口层 + 知识 Skill**

而不是把大量工程知识直接塞进 Skill。

### 结论 2

知识库应同时覆盖：

- 代码知识
- RTOS 运行语义
- API / 组件 / 开发规范等文档知识
- profile / manifest / workspace 维度的版本化信息

### 结论 3

Skill 的职责是：

- 识别问题类型
- 决定检索顺序
- 调用 MCP 工具
- 组织证据化回答

### 结论 4

知识库应优先采用：

**编译感知索引 + 精确检索 + 语义图谱 + 文档关联 + 向量补充**

而不是只做普通 RAG。

### 结论 5

在进入真实工程环境后，应优先围绕：

- manifest/workspace snapshot
- build profile
- RTOS wrapper / 运行语义
- 高价值真实问题样本

继续做细化设计。

---

## 24. 附：本次对话中形成的高价值设计口号

可直接作为后续方案文档中的 guiding principles：

- **Skill 管“怎么做”，知识库管“知道什么”。**
- **不要把 Skill 当知识库；要把 Skill 当知识库查询与推理编排器。**
- **MCP 是知识库能力的接口层，不是知识库本体。**
- **先做编译感知代码图，再做文档增强图。**
- **索引单位不是单仓库，而是 manifest/workspace snapshot + build profile。**
- **对 RTOS 工程，真正决定效果的是：代码快照是否可复现、编译上下文是否完整、RTOS 语义是否被显式建模。**

