# Active Knowledge Skill 路由说明与示例

> 适用范围：`active-knowledge-server` Phase 6 MCP query tools  
> 定位：当前仓库中 `question-intents.md` 的等价实施文档  
> 机器校验伴随物：[../active-knowledge-server/tests/fixtures/skill_routing_examples.yaml](../active-knowledge-server/tests/fixtures/skill_routing_examples.yaml)、[../active-knowledge-server/tests/contracts/test_skill_routing_examples.py](../active-knowledge-server/tests/contracts/test_skill_routing_examples.py)

## 1. 使用原则

这份文档面向 Skill 编写者，而不是面向最终回答话术。它只回答三件事：

1. 用户问题应该先打到哪个 MCP query tool。
2. 什么时候要串第二跳或第三跳，而不是让 `kb_search` 吞掉一切。
3. 低置信、歧义和安全边界下，Skill 应该怎样停在正确的位置。

结合行业里 Agent + RAG 的稳定实践，这里固定五条路由原则：

- Anchor-first：能先拿符号、路径、模块、profile 锚点，就不要先做宽召回。
- Docs-before-code：API、widget、product、project 问题先回到权威文档，再下钻实现。
- Narrow-then-expand：先做首跳定位，再决定是否补 `code_trace`、`workspace_view`、`evidence_bundle`。
- Evidence-first：高价值结论必须能回到 `evidence_refs`，不把派生理解包装成原始事实。
- Router-not-storage：Skill 只依赖 MCP query tools 和统一契约，不依赖 ops tools、SQLite、LanceDB 或内部 artifact 路径。

## 2. Skill 路由矩阵

| Intent / 问题类型 | 首跳工具 | 常见补跳 | Skill 规则 |
| --- | --- | --- | --- |
| `code_exact` 函数、宏、路径、文件定位 | `code_resolve` | `code_context`、`evidence_bundle` | 先把锚点收紧到 symbol/path，再决定是否扩展上下文。 |
| `code_concept` 模块职责、机制说明 | `code_context` | `kb_search`、`evidence_bundle` | 先拿模块级摘要，不要一开始就跨域全量召回。 |
| `call_trace` 调用链、谁调用了谁 | `code_resolve` | `code_trace`、`evidence_bundle` | 先定位起点/终点，再做 graph-aware trace。 |
| `runtime_flow` ISR、task、queue、startup 链 | `workspace_view` | `code_trace`、`evidence_bundle` | 先建立 feature/目录落点，再解释运行时流转。 |
| `profile_diff` 宏影响、profile 差异 | `config_impact` | `code_resolve`、`evidence_bundle` | 先给 profile matrix 和受影响模块，再补符号证据。 |
| `api_lookup` API 用法、参数、返回值 | `docs_search` | `code_resolve`、`evidence_bundle` | 先用 API 文档回答，再按需定位实现入口。 |
| `widget_lookup` 控件属性、绑定、UI 约束 | `docs_search` | `workspace_view`、`evidence_bundle` | 先给控件能力边界，再补 feature 落点。 |
| `workspace_nav` 去哪看、目录职责、层次边界 | `workspace_view` | `code_context`、`kb_search` | 先回答看哪里，再决定是否展开模块解释。 |
| `product_context` / `project_context` 范围、计划、风险 | `docs_search` | `workspace_view`、`evidence_bundle` | 先回到 product/project 原始文档。 |
| `evidence_lookup` 证据包、出处回溯 | `evidence_bundle` | `code_resolve`、`docs_search` | 直接回证据，不重复生成推断结论。 |
| `unknown` 模糊、缺上下文、跨域探索 | `kb_search` | `workspace_view`、`docs_search` | 只做探索性召回，并显式保留低置信语义。 |

## 3. 十个典型问题的 Tool Call 示例

以下示例全部使用当前真实 MCP wrapper 名称，而不是设计阶段的抽象名。

### 3.1 机制类问题先探索再收紧

问题：`Active 的运动数据缓存机制是怎么实现的？`

```text
kb_search(query="Active 的运动数据缓存机制是怎么实现的？")
code_context(query="运动数据缓存机制", granularity="module")
evidence_bundle(query="运动数据缓存机制")
```

适用原因：问题没有稳定 symbol/path 锚点，先用 `kb_search` 做混合召回，再收紧到模块摘要和证据。

### 3.2 API 用法先查文档再看实现

问题：`sensor.subscribe API 怎么用？`

```text
docs_search(query="sensor.subscribe API 怎么用？", doc_type="api")
code_resolve(query="sensor.subscribe")
evidence_bundle(query="sensor.subscribe API 怎么用？")
```

适用原因：先回到 API 文档给参数/返回值和适用范围，再补实现入口。

### 3.3 控件问题先查 widget 文档再落到 feature

问题：`Active Text widget 支持哪些属性？`

```text
docs_search(query="Active Text widget 支持哪些属性？", doc_type="widget")
workspace_view(view="feature", query="Active Text")
```

适用原因：控件问题优先回答能力边界和属性集合，必要时再看 feature 落点。

### 3.4 宏或路径定位直接打精确工具

问题：`CONFIG_HEALTH_SLEEP 在哪里定义？`

```text
code_resolve(query="CONFIG_HEALTH_SLEEP 在哪里定义？")
evidence_bundle(query="CONFIG_HEALTH_SLEEP 在哪里定义？")
```

适用原因：这是典型 `code_exact`，没有必要先走宽召回。

### 3.5 模块职责解释直接走上下文工具

问题：`packages/services/notification 主要负责什么？`

```text
code_context(query="packages/services/notification 主要负责什么？", granularity="module")
evidence_bundle(query="packages/services/notification 主要负责什么？")
```

适用原因：路径已经给出，目标是模块职责，不是精确符号定位。

### 3.6 调用链先解锚点再 trace

问题：`app_manager_start() 到 screen_create() 的调用链怎么走？`

```text
code_resolve(query="app_manager_start")
code_trace(query="app_manager_start() 到 screen_create() 的调用链怎么走？", view="runtime")
evidence_bundle(query="app_manager_start() 到 screen_create() 的调用链怎么走？")
```

适用原因：行业实践里链路问题最怕同名 symbol，先解起点锚点更稳。

### 3.7 ISR 到 task 的运行时流先建 feature 落点

问题：`PPG 中断之后事件如何进入 health task？`

```text
workspace_view(view="feature", query="PPG health")
code_trace(query="PPG 中断之后事件如何进入 health task？", view="runtime")
evidence_bundle(query="PPG 中断之后事件如何进入 health task？")
```

适用原因：先知道相关 feature/目录，再解释 ISR、queue、task 的链路会更可控。

### 3.8 Profile 差异优先走配置影响

问题：`CONFIG_BT 在 watch 和 sensorhub 的差异是什么？`

```text
config_impact(
  query="CONFIG_BT 在 watch 和 sensorhub 的差异是什么？",
  profile_id="mhs003_watch",
  compare_to="mhs003_sensorhub"
)
evidence_bundle(query="CONFIG_BT 在 watch 和 sensorhub 的差异是什么？")
```

适用原因：差异分析的首要输出应是 profile matrix、差异宏和影响范围，而不是一开始列源码片段。

### 3.9 工程导航先回答去哪里看

问题：`新同事要看蓝牙功能应该从哪些目录开始？`

```text
workspace_view(view="workspace", query="蓝牙")
code_context(query="蓝牙功能", granularity="module")
```

适用原因：导航类问题先给目录和职责，再补模块说明。

### 3.10 产品范围问题先回到 product 文档

问题：`睡眠功能 V1 的产品范围是什么？`

```text
docs_search(query="睡眠功能 V1 的产品范围是什么？", doc_type="product")
workspace_view(view="feature", query="睡眠")
evidence_bundle(query="睡眠功能 V1 的产品范围是什么？")
```

适用原因：先回答产品范围，再把 feature 落点和证据挂上，避免把代码现状直接等价成产品定义。

### 3.11 证据回溯直接请求 evidence bundle

问题：`给我 app_manager_start 的证据包。`

```text
evidence_bundle(query="给我 app_manager_start 的证据包。")
code_resolve(query="app_manager_start")
```

适用原因：证据回溯的目标是 evidence refs，不是重新生成解释。

## 4. 低置信与歧义处理示例

### 4.1 歧义：API 还是 widget

问题：`这个控件 API 怎么用？`

- 预期 warning：`router.ambiguous_intent`
- 首跳工具：`docs_search`
- Skill 动作：先说明它可能是 API 文档，也可能是 widget 文档；要求用户补充控件名、模块名或文档域。必要时只列候选，不下确定结论。

推荐输出骨架：

```text
我目前把它识别成 API / widget 混合歧义问题。
如果你补充控件名或模块名，我会直接收紧到 docs_search(doc_type=widget|api)。
在上下文不足前，我不会把某一类文档结论当成确定答案。
```

### 4.2 低置信：缺少任何锚点

问题：`这个为什么不行？`

- 预期 warning：`router.low_confidence`
- 首跳工具：`kb_search`
- Skill 动作：不直接给原因；先索要 symbol、路径、报错、profile、最近变更。若用户暂时给不出，只返回 `next_queries` 和低置信候选线索。

推荐输出骨架：

```text
当前缺少可验证锚点，我只能做低置信探索，不能直接判断根因。
请至少补充以下之一：函数/模块名、文件路径、报错文本、profile、最近改动。
如果要我先粗查，我会用 kb_search 给出候选方向，并明确标注 low confidence。
```

## 5. 禁止依赖

Skill 允许依赖的只有稳定 MCP query tools 与统一 `QueryResult` 契约。Skill 不应直接依赖：

- ops tools：`ops_get_config`、`ops_validate_setup`、`ops_index_status`、`ops_start_index`、`ops_cancel_index`、`ops_list_profiles`、`ops_list_sources`
- 内部存储：SQLite 表、LanceDB collection 名称
- 内部路径：`local/db`、`local/vectors`、`local/artifacts`、`baseline/db`
- 内部实现细节：parser 名称、临时 job 文件、未承诺对外暴露的 artifact 目录

## 6. 机器校验方式

这份文档对应的机器校验输入和测试位于：

- [../active-knowledge-server/tests/fixtures/skill_routing_examples.yaml](../active-knowledge-server/tests/fixtures/skill_routing_examples.yaml)
- [../active-knowledge-server/tests/contracts/test_skill_routing_examples.py](../active-knowledge-server/tests/contracts/test_skill_routing_examples.py)

当前 smoke test 校验三件事：

1. 路由矩阵和示例使用的 tool 名称都存在于当前 MCP inventory。
2. 低置信/歧义示例与 `QueryRouter` 的 warning 行为一致。
3. 示例不会漂移到 ops tools 或内部存储术语。