# ZeppOS validate 检查记录

## 检查范围

本次检查基于以下信息：

- `uv run active-kb init --config ../examples/local-single-user.yaml`
- `uv run active-kb validate --config ../examples/local-single-user.yaml --format json`
- `.active-kb/local/artifacts/index-jobs/index:0641b70c2639480a84d67fb2e5cbd948/collect/code.json`
- `.active-kb/local/artifacts/index-jobs/index:0641b70c2639480a84d67fb2e5cbd948/collect/docs.json`
- 2026-06-25 本机磁盘与 `.active-kb` 占用情况

## 结论

这次 `validate` 的结论不是“存储彻底损坏”，而是：

- 当前 ZeppOS 索引处于 `partial_ready`
- 索引“可以查询”，但覆盖率和完整性都已经降级
- 不能把这次结果视为一次成功完成的 ZeppOS 全量索引

最直接的失败信号来自最近一次 index job：

- `job_id`: `index:0641b70c2639480a84d67fb2e5cbd948`
- `status`: `partial_ready`
- `last_task_error`: `database or disk is full`
- `tasks_failed`: `2`
- `tasks_applied`: `0`

这说明本次问题的主线不是 `validate` 命令本身异常，而是上一次 ZeppOS 索引在写入阶段失败，`validate` 只是把这个现状如实暴露出来。

## 关键信号

### 1. 当前索引状态不是 ready

`validate` 输出中的索引摘要为：

- `result_status = partial_ready`
- `message = The index is queryable with degraded coverage or warnings.`

这类状态在当前仓库实现里的语义就是：

- 可以返回查询结果
- 但结果不完整，或者底层存在警告/失败任务

因此，这份索引只能用于临时观察，不适合作为稳定 baseline，也不适合作为“ZeppOS 已成功建库”的验收结果。

### 2. 最近一次索引确实在写入阶段出错

最近 job 元数据里最关键的几项是：

- `started_at = 2026-06-18T07:58:54Z`
- `finished_at = 2026-06-18T10:09:57Z`
- `last_phase = done`
- `last_task_key = workspace:map`
- `last_task_error = database or disk is full`
- `changed_code_paths_count = 264853`

这说明：

- 索引任务跑了较长时间
- 最终没有形成完整成功结果
- 问题发生时，待处理代码量非常大

### 3. 当前本地 workdir 已经非常大

本机检查结果：

- 根分区可用空间约 `21G`
- `.active-kb` 总占用约 `68G`
- 其中几乎全部来自 `.active-kb/local`

这和 job 里的 `database or disk is full` 是互相印证的。  
也就是说，这不是单纯“历史日志里有一条失败信息”，而是当前磁盘压力和本地索引体量都已经明显异常。

### 4. 实际收集结果出现了不该进入索引的 `.repo`

这是这次检查里最值得单独关注的异常。

示例配置 [`examples/local-single-user.yaml`](/home/gangan/GANLab/ActiveTools/active-knowledge/examples/local-single-user.yaml:39) 已配置：

```yaml
paths:
  exclude:
    - .git
    - .repo
    - build/out
    - build/tmp
```

但本次 job 的 `collect/code.json` 里，实际收集到了大量：

- `.repo/project-objects/...`
- `.repo/projects/...`

而且 warning 里也能看到很多：

- `workspace.symlink_dir_skipped`
- 路径位于 `workspace:.repo/projects/...`

这说明至少在这次 ZeppOS 索引中，`.repo` 没有被有效挡住。  
这很可能才是 ZeppOS 建库体量爆炸、最终触发磁盘写满的根因之一。

### 5. docs 侧几乎没有真实内容

`collect/docs.json` 显示本次文档侧只收到了 9 个 `.gitkeep` 文件：

- `api/.gitkeep`
- `design/.gitkeep`
- `engineering/.gitkeep`
- `learned-seeds/.gitkeep`
- `product/.gitkeep`
- `project/.gitkeep`
- `qa/.gitkeep`
- `release/.gitkeep`
- `widgets/.gitkeep`

这表示：

- 本次 `knowledge-sources/` 基本还是空壳目录
- 文档索引不是本次失败主因
- 即使索引成功，docs 侧当前也不会贡献太多检索价值

## 对 warnings 的判断

### 可以接受的非阻断项

- `baseline.manifest_missing`
  - 当前没有 baseline manifest
  - 对本地 overlay 模式是可接受的
  - 代表“依赖本地索引”，不代表这次失败

- `build_outputs.symlink_dir_skipped`
  - 构建产物中的符号链接目录被跳过
  - 属于预期防御性行为

- `compile_db.missing`
  - 没找到 `compile_commands.json`
  - 会降低跨编译单元精度
  - 但不是这次 job 失败的直接原因

- `profile.multiple_candidates`
  - 自动 profile 解析到了多个候选
  - 说明 ZeppOS 配置视角较多
  - 会影响 profile-sensitive 查询体验，但不是导致磁盘写满的核心原因

### 需要关注但更像“环境/状态信号”的项

- `storage.schema_mismatch`
  - 在 `init` 输出里出现过
  - 从上下文看更像 baseline 还没建立完成，或 baseline 目录本身为空
  - 目前没有看到它成为这次 `partial_ready` 的直接主因

## 综合判断

如果只回答“这次 validate 有没有发现问题”，答案是：有，而且问题不小。

但更准确地说，问题分成两层：

1. 表层现象
   - 当前索引是 `partial_ready`
   - 最近一次建库留下了 `database or disk is full`

2. 更可能的根因
   - ZeppOS 实际扫描范围异常扩大
   - `.repo` 内容被收进了 code collect
   - `.active-kb/local` 因此膨胀到 `68G`

所以，这次不应该继续围绕 `validate` 本身排查，而应该转去处理“ZeppOS workspace 扫描范围失控”。

## 建议下一步

### 优先级 P0

- 不要把当前索引结果当成成功建库结果使用
- 不要基于当前状态发布 baseline

### 优先级 P1

- 先确认为什么 `.repo` 没被排除
- 优先检查 workspace scan / collect 阶段是否真的按 `paths.exclude` 生效
- 临时规避时，可以把排除规则进一步收紧，例如同时覆盖：
  - `.repo`
  - `.repo/**`
  - `**/.repo/**`
  - `.repo/project-objects/**`
  - `.repo/projects/**`

### 优先级 P1

- 在重新索引前，先处理本地工作目录体量问题
- 仅靠 `--restart` 不能自动回收已经写出来的 `68G` 本地数据
- 更稳妥的方式是：
  - 备份需要的结果后清理当前 `.active-kb/local`
  - 或直接换一个新的 workdir 重新跑

### 优先级 P1

- 修正扫描范围后，再重新执行一次完整的本地增量索引

可参考：

```bash
cd active-knowledge-server
uv run active-kb index \
  --config ../examples/local-single-user.yaml \
  --restart
```

前提是：

- 已确认有足够磁盘空间
- 已确认 `.repo` 不会再次被扫入

### 优先级 P2

- 如果后续要提升 C/C++ 查询精度，再补 `compile_commands.json`
- 如果后续 profile 查询很多，建议显式指定 profile，而不是长期依赖 `auto`
- 如果希望 docs 检索也有价值，需要往 `knowledge-sources/` 放入真实文档，而不是只有 `.gitkeep`

## 最终判断

本次 `validate` 输出反映出的真实状态是：

- 本地 Active Knowledge 环境已初始化
- validate 存储检查本身没有暴露出“当前 overlay 已不可用”的致命错误
- 但 ZeppOS 索引结果并未成功完成
- 当前最需要处理的是索引范围异常扩大，尤其是 `.repo` 被错误收录

一句话总结：

当前不是 “validate 坏了”，而是 “ZeppOS 这次 index 构建实际上失败了，而且失败前把 `.repo` 也扫进来了，最终把本地索引盘写爆了”。

## 安全清理 + 重建 index 步骤

这套流程适合：

- 你想先安全回收一部分运行时垃圾
- 尽量保留本地配置
- 再重新做一次完整重建

### 0. 先停止所有相关进程

在开始前，先确认没有正在运行的：

- `active-kb serve`
- `active-kb index`
- 任何正在占用 `.active-kb/local/db/*.db` 的 MCP/IDE 进程

### 1. 备份本地配置

最重要的是保留这个文件：

- [`.active-kb/local/config/active-kb.local.yaml`](/home/gangan/GANLab/ActiveTools/active-knowledge/.active-kb/local/config/active-kb.local.yaml:1)

建议先备份一份：

```bash
cp .active-kb/local/config/active-kb.local.yaml \
  .active-kb/local/config/active-kb.local.yaml.bak.$(date +%Y%m%d-%H%M%S)
```

### 2. 先做非破坏性清理

这一步只清理运行时垃圾和历史状态，不会删除当前 local config：

```bash
cd active-knowledge-server
uv run active-kb clean \
  --config ../examples/local-single-user.yaml \
  --cache \
  --tmp \
  --old-jobs \
  --keep 3 \
  --format json
```

如果你怀疑历史快照和失败 staging 也很多，可以继续：

```bash
uv run active-kb clean \
  --config ../examples/local-single-user.yaml \
  --old-snapshots \
  --staging-jobs \
  --old-live-versions \
  --keep 1 \
  --format json
```

说明：

- `--old-jobs` 只删旧的 terminal jobs
- `--old-snapshots` 清理旧 local overlay snapshots
- `--staging-jobs` 清理失败或 superseded 的 full-index staging
- `--old-live-versions` 清理旧 publish 版本
- `--compact-overlay` 只适合“保留现有 overlay 并做整理”，不适合你现在这种想彻底重建的场景

### 3. 检查空间是否已经回收

```bash
du -sh ../.active-kb ../.active-kb/local
df -h ..
```

如果 `.active-kb/local` 仍然非常大，说明仅靠 `clean` 不够，需要进入下一步“删除 local index 数据本体”。

### 4. 删除 local index 数据，但保留 config

从仓库根目录执行更直观：

```bash
rm -f .active-kb/local/db/overlay.db
rm -f .active-kb/local/db/jobs.db
rm -rf .active-kb/local/vectors/lancedb-delta
rm -rf .active-kb/local/artifacts/index-jobs
rm -rf .active-kb/local/cache
rm -rf .active-kb/local/tmp
```

如果你想把运行日志也清掉：

```bash
rm -f .active-kb/local/logs/*.log
rm -f .active-kb/local/logs/observability.json
```

不要删除：

- `.active-kb/local/config/active-kb.local.yaml`
- `.active-kb/baseline/`

### 5. 重新初始化本地工作目录

```bash
cd active-knowledge-server
uv run active-kb init --config ../examples/local-single-user.yaml
```

这一步的目标是：

- 重新补齐缺失目录
- 重新创建 `overlay.db` / `jobs.db`
- 保持你原来的 local-single-user 配置口径

### 6. 重建前先 validate 一次

```bash
uv run active-kb validate \
  --config ../examples/local-single-user.yaml \
  --format json
```

这时如果只剩 baseline 缺失、compile db 缺失之类 warning，就可以继续。

### 7. 重新做一次完整本地重建

既然目标是重建，不建议再走 `--resume auto`，直接从头跑：

```bash
uv run active-kb index \
  --config ../examples/local-single-user.yaml \
  --full \
  --target local \
  --source all \
  --no-resume \
  --format json
```

如果你明确还在使用 local overlay 语义，也可以用：

```bash
uv run active-kb index \
  --config ../examples/local-single-user.yaml \
  --incremental \
  --source all \
  --restart \
  --format json
```

两者区别：

- `--full --target local --no-resume` 更接近“完整重建”
- `--incremental --restart` 更接近“丢弃旧 job 后重新规划一次增量”

对你当前这个 ZeppOS 场景，我更建议优先用前者。

### 8. 重建完成后做验收

```bash
uv run active-kb validate \
  --config ../examples/local-single-user.yaml \
  --format json

uv run active-kb status \
  --config ../examples/local-single-user.yaml \
  --format json
```

理想结果：

- `index.result_status = ready`
- 最近 job 不再出现 `database or disk is full`
- `.repo` 不应再出现在 collect 结果中

## 彻底删除本地 index，完全重建步骤

这套流程更激进，适合：

- 你确认当前 local index 已经没有保留价值
- 你想彻底从零开始
- 你愿意丢弃当前所有 local overlay / jobs / vector delta / index artifacts

### 删除范围

这里的“彻底删除本地 index”，指删除 `.active-kb/local` 里的索引运行态数据，而不是删除仓库源码。

建议保留：

- `.active-kb/local/config/active-kb.local.yaml`
- `.active-kb/baseline/`
- `examples/local-single-user.yaml`

### 步骤 1：备份 local config

```bash
cp .active-kb/local/config/active-kb.local.yaml \
  /tmp/active-kb.local.yaml.backup.$(date +%Y%m%d-%H%M%S)
```

### 步骤 2：彻底删除 local 索引运行态

从仓库根目录执行：

```bash
rm -rf .active-kb/local/artifacts
rm -rf .active-kb/local/cache
rm -rf .active-kb/local/tmp
rm -rf .active-kb/local/vectors
rm -rf .active-kb/local/db
rm -rf .active-kb/local/locks
rm -rf .active-kb/local/logs
```

然后重建基础目录并把 config 放回去：

```bash
mkdir -p .active-kb/local/config
cp /tmp/active-kb.local.yaml.backup.* .active-kb/local/config/active-kb.local.yaml
```

如果你不想用通配符恢复，建议备份时就用固定文件名。

更稳的写法是：

```bash
cp .active-kb/local/config/active-kb.local.yaml /tmp/active-kb.local.yaml.backup
rm -rf .active-kb/local
mkdir -p .active-kb/local/config
cp /tmp/active-kb.local.yaml.backup .active-kb/local/config/active-kb.local.yaml
```

这一步做完后：

- 本地 overlay metadata 没了
- 本地 jobs 历史没了
- 本地 vector delta 没了
- 本地 index artifacts 没了
- 本地缓存、日志、锁也都没了

### 步骤 3：重新 init

```bash
cd active-knowledge-server
uv run active-kb init --config ../examples/local-single-user.yaml
```

### 步骤 4：先 validate

```bash
uv run active-kb validate \
  --config ../examples/local-single-user.yaml \
  --format json
```

这时看到“缺少 baseline manifest”一类 warning 是正常的；  
只要没有新的 blocked 级错误，就可以继续。

### 步骤 5：完全重建本地 index

```bash
uv run active-kb index \
  --config ../examples/local-single-user.yaml \
  --full \
  --target local \
  --source all \
  --no-resume \
  --format json
```

### 步骤 6：重建后复核

```bash
uv run active-kb validate \
  --config ../examples/local-single-user.yaml \
  --format json

uv run active-kb status \
  --config ../examples/local-single-user.yaml \
  --format json
```

### 特别提醒

在你当前 ZeppOS 场景里，如果不先解决 `.repo` 被扫入的问题，那么“彻底删除后重建”大概率还会再次把本地盘打满。

所以，完全重建前建议至少先做一件事：

- 修正 `paths.exclude` 的实际生效问题，确保 `.repo` 不再进入 collect

如果暂时只是为了先跑通，可以临时把排除规则写得更保守，例如同时加入：

```yaml
paths:
  exclude:
    - .git
    - .repo
    - .repo/**
    - '**/.repo/**'
    - .repo/project-objects/**
    - .repo/projects/**
    - build/out
    - build/tmp
```
