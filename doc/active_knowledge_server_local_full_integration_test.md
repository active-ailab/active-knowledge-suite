# Active Knowledge Server 本地全功能集成测试

本文给出一套面向本地开发机的完整集成测试流程，目标不是只验证 CLI 能启动，而是分层验证以下三件事：

- 真实本地工程是否能完成 init、validate、index、serve 和 live query smoke。
- server 的查询、性能、稳定性、可重复性 gate 是否满足当前实现契约。
- baseline / release 相关命令是否能在本地完成一轮可发布前演练。

结论先行：这三层不能混为一条命令。

- `init` / `validate` / `index` / `serve` / live query smoke 验证的是你当前配置下的真实工程与真实工作目录。
- `eval run --gate quality`、`perf run`、`stability run`、`eval run --gate reproducibility` 主要验证的是 server 自带的 synthetic benchmark 和稳定契约，不直接证明当前 ZeppOS 工程里的真实符号和真实文档已经被正确索引。
- `baseline publish`、`baseline validate`、`eval-baseline save|compare`、`release checklist` 验证的是发布物和回归门禁。

## 1. 前置条件

- 以下命令默认在 `active-knowledge-server/` 目录下执行。
- 默认配置使用 `../examples/local-single-user.yaml`。
- 如果你的本地工程不是配置文件里的 `project.workspace_root`，请给下面所有命令追加 `--workspace /path/to/your/workspace`。
- 仓库根目录下的 `knowledge-sources/` 当前允许为空骨架；如果没有实际文档，`docs_search` live smoke 应该跳过，而不是当作 server 故障。
- Phase C 会写入 `../.active-kb/baseline/`，需要本地可写权限。

建议先定义一组公共变量：

```bash
cd active-knowledge-server

CONFIG=../examples/local-single-user.yaml
WORKDIR=../.active-kb
LOCAL_ARTIFACTS="$WORKDIR/local/artifacts"
BASELINE_ARTIFACTS="$WORKDIR/baseline/artifacts"
BASELINE_ID="local-full-$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p \
  "$LOCAL_ARTIFACTS/mcp" \
  "$LOCAL_ARTIFACTS/eval" \
  "$LOCAL_ARTIFACTS/perf" \
  "$LOCAL_ARTIFACTS/stability" \
  "$BASELINE_ARTIFACTS/eval-baseline" \
  "$BASELINE_ARTIFACTS/stability" \
  "$BASELINE_ARTIFACTS/release"
```

如果你要补 `AR4-04` 的断点续建手测，建议再准备一组隔离变量，避免污染日常 `.active-kb/`：

```bash
RESUME_WORKSPACE=/home/gangan/ZeppOS
RESUME_WORKDIR=/tmp/active-kb-ar4-04-resume-smoke
RESUME_ARTIFACTS="$RESUME_WORKDIR/artifacts"
RESUME_CONFIG="$RESUME_WORKDIR/resume-smoke.yaml"
RESUME_JOB_ID="index:manual-resume-smoke"

mkdir -p "$RESUME_ARTIFACTS"
```

## 2. Phase A：真实本地工程闭环

这一阶段只验证当前配置下的真实 workspace、真实 workdir、真实 MCP wiring。

说明：`init` 和 `status` 现在只做 quick storage validation，避免在已有大索引上卡在全量一致性扫描；完整的 deep storage validation 仍然放在 `validate`，并会把当前校验阶段打印到 stderr，方便判断命令是否只是慢而不是卡死。

`index --format json` 的最终 JSON 仍只写 stdout；当 stderr 是交互式终端时，索引期间会把与 text 模式一致的 global progress、stage progress 和最近文件滚动区写到 stderr，不影响 `jq`、重定向或 CI 解析 stdout。

```bash
uv sync --group dev

uv run active-kb init \
  --config "$CONFIG" \
  --reuse-baseline \
  --format json

uv run active-kb validate \
  --config "$CONFIG" \
  --strict \
  --format json

uv run active-kb status \
  --config "$CONFIG" \
  --format json

uv run active-kb index \
  --config "$CONFIG" \
  --incremental \
  --source all \
  --resume auto \
  --format json

uv run active-kb index \
  --config "$CONFIG" \
  --full \
  --target local \
  --source all \
  --format json

uv run active-kb rebuild \
  --config "$CONFIG" \
  --vectors \
  --target local \
  --source docs \
  --format json

uv run active-kb status \
  --config "$CONFIG" \
  --format json

uv run active-kb serve \
  --config "$CONFIG" \
  --transport stdio \
  --format json | tee "$LOCAL_ARTIFACTS/mcp/server-plan.json"
```

### 2.1 可恢复索引 smoke

这一步是可选慢测，目标不是覆盖全部索引功能，而是验证“真实工程 + 真实 SQLite jobs/checkpoint + 中断后重跑”这条恢复链路。

- 建议优先对真实工程 `/home/gangan/ZeppOS` 做 smoke，而不是只跑 synthetic fixture。
- 这一步不要塞进默认 `pytest` 或普通单测；它依赖真实 workspace、真实 I/O 和一次受控中断，更适合本地手测或单独 slow lane。
- 为了不影响日常索引数据，推荐单独使用 `RESUME_WORKDIR=/tmp/...`。

如果要严格按人工手测复现 `Ctrl+C -> 重跑 -> resumed=true`，直接执行下面两条命令：

```bash
rm -rf "$RESUME_WORKDIR"
mkdir -p "$RESUME_ARTIFACTS"
export CONFIG RESUME_WORKSPACE RESUME_WORKDIR RESUME_CONFIG

uv run python - <<'PY'
import os
from pathlib import Path

import yaml
from active_knowledge_server.config.loader import set_nested

config_path = Path(os.environ["CONFIG"])
resume_config = Path(os.environ["RESUME_CONFIG"])
resume_workdir = Path(os.environ["RESUME_WORKDIR"])
baseline_dir = resume_workdir / "baseline"
local_dir = resume_workdir / "local"
payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

set_nested(payload, ("runtime", "workdir"), str(resume_workdir))
set_nested(payload, ("runtime", "baseline_dir"), str(baseline_dir))
set_nested(payload, ("runtime", "local_dir"), str(local_dir))
set_nested(payload, ("project", "workspace_root"), os.environ["RESUME_WORKSPACE"])
set_nested(payload, ("storage", "baseline", "manifest"), str(baseline_dir / "manifest.json"))
set_nested(payload, ("storage", "metadata", "path"), str(baseline_dir / "db" / "metadata.db"))
set_nested(payload, ("storage", "overlay", "path"), str(local_dir / "db" / "overlay.db"))
set_nested(payload, ("storage", "jobs", "path"), str(local_dir / "db" / "jobs.db"))
set_nested(payload, ("storage", "vector", "path"), str(baseline_dir / "vectors" / "lancedb"))
set_nested(payload, ("storage", "vector_delta", "path"), str(local_dir / "vectors" / "lancedb-delta"))
set_nested(payload, ("storage", "artifacts_root"), str(baseline_dir / "artifacts"))
set_nested(payload, ("storage", "local_artifacts_root"), str(local_dir / "artifacts"))
set_nested(payload, ("storage", "cache_root"), str(local_dir / "cache"))

resume_config.write_text(
    yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
    encoding="utf-8",
)
print(resume_config)
PY

uv run active-kb index \
  --config "$RESUME_CONFIG" \
  --incremental \
  --source code \
  --no-resume \
  --job-id "$RESUME_JOB_ID" \
  --format json | tee "$RESUME_ARTIFACTS/first-run.json"
```

看到 stderr 已经出现至少一个 applied task 或明显进入 apply 阶段后，按一次 `Ctrl+C` 中断。随后立刻重跑：

```bash
uv run active-kb index \
  --config "$RESUME_CONFIG" \
  --incremental \
  --source code \
  --resume auto \
  --format json | tee "$RESUME_ARTIFACTS/resume-run.json"

uv run python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/active-kb-ar4-04-resume-smoke/artifacts/resume-run.json").read_text())
job = payload["job"]
assert payload["status"] == "ok", payload
assert job["resumed"] is True, job
assert (job["tasks_skipped"] or 0) > 0, job
print(json.dumps({
    "status": payload["status"],
    "job_id": job["job_id"],
    "resumed": job["resumed"],
    "tasks_skipped": job["tasks_skipped"],
}, ensure_ascii=False, indent=2))
PY
```

如果你不想手工卡时机，可以直接跑仓库自带的 smoke harness。它会在首个 applied checkpoint 之后自动发送 `SIGTERM`，然后执行 `--resume auto`、`validate --strict` 和 `status` 检查：

```bash
uv run python scripts/manual_resume_smoke.py \
  --config "$CONFIG" \
  --workspace "$RESUME_WORKSPACE" \
  --workdir "$RESUME_WORKDIR" \
  --source code \
  --job-id "$RESUME_JOB_ID" \
  --clean \
  --output "$RESUME_ARTIFACTS/report.json"
```

通过标准：

- 第一轮命令返回 `130`，并留下 `job_id` 对应的 interrupted job/checkpoint。
- 第二轮 JSON 满足 `status=ok`、`job.resumed=true`、`job.tasks_skipped > 0`。
- `uv run active-kb validate --config "$RESUME_CONFIG" --strict --format json` 返回 `status=ok`。

上面的 `serve --format json` 只验证 runtime wiring，不会阻塞当前终端。接着补一段 live query smoke，直接调用与 MCP 同一套 tool handler：

```bash
uv run python - <<'PY' | tee "$LOCAL_ARTIFACTS/mcp/live-smoke.json"
import json
from pathlib import Path

from active_knowledge_server.config.loader import resolve_config
from active_knowledge_server.server import build_server_app

root = Path.cwd()
resolved = resolve_config(config_path=Path("../examples/local-single-user.yaml"))
app = build_server_app(resolved, cwd=root)
handlers = {tool.name: tool.handler for tool in app.inventory.tools}

payload = {
    "server_info": handlers["server_info"]().model_dump(mode="json"),
    "workspace_view": handlers["workspace_view"](
        view="workspace",
        query="platform/mcu/mhs003",
    ).model_dump(mode="json"),
    "code_resolve": handlers["code_resolve"](
        "configs/mhs003/mhs003_geneva_defconfig",
        granularity="file",
    ).model_dump(mode="json"),
    "evidence_bundle": handlers["evidence_bundle"](
        query="platform/mcu/mhs003/module.mk",
    ).model_dump(mode="json"),
}

docs_root = root.parent / "knowledge-sources"
has_docs = any(path.is_file() and path.name != ".gitkeep" for path in docs_root.rglob("*"))
if has_docs:
    payload["docs_search"] = handlers["docs_search"](
        "sensor",
        domain="api",
        doc_type="api",
        view="evidence",
        granularity="doc_section",
    ).model_dump(mode="json")
else:
    payload["docs_search"] = {
        "skipped": True,
        "reason": "knowledge-sources has no indexed source docs yet",
    }

print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
```

如果你还要验证真实 MCP stdio 会话，而不是只验证 handler，可以另开一终端执行：

```bash
uv run active-kb serve --config "$CONFIG" --transport stdio
```

然后用 VS Code / Copilot / MCP Inspector 调 `ping`、`server_info`、`workspace_view`、`code_resolve`。

Phase A 通过标准：

- `validate` 返回码为 `0`。
- `index --incremental`、`index --full --target local`、`rebuild --vectors` 都返回码为 `0`。
- 两条 `index` 命令的 `result.result_status` 都不是 `blocked` 或 `error`。
- `server_info` 至少能返回 2 个 bootstrap tools 和 8 个 query tools。
- `workspace_view`、`code_resolve`、`evidence_bundle` 的 `result_status` 不应为 `blocked` 或 `error`。
- `docs_search` 只有在 `knowledge-sources/` 已经放入实际文档时才算必测项。

## 3. Phase B：Synthetic Gate

这一阶段验证 server 的质量、性能、稳定性和可重复性契约。它依赖 synthetic benchmark，不依赖你当前 ZeppOS 目录里必须存在某个特定符号。

```bash
uv run active-kb eval run \
  --config "$CONFIG" \
  --gate quality \
  --report "$LOCAL_ARTIFACTS/eval/quality.json" \
  --format json

uv run active-kb perf run \
  --config "$CONFIG" \
  --gate performance \
  --report "$LOCAL_ARTIFACTS/perf/performance.json" \
  --format json

uv run active-kb stability run \
  --config "$CONFIG" \
  --gate stability \
  --soak-seconds 60 \
  --mixed-query-count 500 \
  --report "$LOCAL_ARTIFACTS/stability/stability-dev.json" \
  --format json

uv run active-kb eval run \
  --config "$CONFIG" \
  --gate reproducibility \
  --report "$LOCAL_ARTIFACTS/eval/reproducibility.json" \
  --format json
```

Phase B 通过标准：

- `quality`、`performance`、`reproducibility` 返回 `status=pass`。
- `stability run` 在开发机本地允许 `status=partial_ready`，因为默认 60 秒 soak 不满足 release 级 8 小时窗口。
- 如果 `stability run` 已经是 `fail`，不要继续进入 release 流程。

## 4. Phase C：Baseline / Release 演练

这一阶段是“可发布前演练”，不是每次日常开发都必须跑。

### 4.1 首次建立本地基线

```bash
uv run active-kb baseline publish \
  --config "$CONFIG" \
  --source all \
  --baseline-id "$BASELINE_ID" \
  --publish-mode build \
  --format json

uv run active-kb baseline validate \
  --config "$CONFIG" \
  --format json

uv run active-kb eval-baseline save \
  --config "$CONFIG" \
  --baseline-id "$BASELINE_ID" \
  --quality-report "$LOCAL_ARTIFACTS/eval/quality.json" \
  --performance-report "$LOCAL_ARTIFACTS/perf/performance.json" \
  --stability-report "$LOCAL_ARTIFACTS/stability/stability-dev.json" \
  --format json
```

这里建议本地演练先用 `--publish-mode build`，只有真的要产出 release 基线时再改成 `publish`。

### 4.2 后续回归比较

以后每次重新跑完 Phase B，可以直接比较当前结果与最近一次保存的 baseline：

```bash
uv run active-kb eval-baseline compare \
  --config "$CONFIG" \
  --quality-report "$LOCAL_ARTIFACTS/eval/quality.json" \
  --performance-report "$LOCAL_ARTIFACTS/perf/performance.json" \
  --stability-report "$LOCAL_ARTIFACTS/stability/stability-dev.json" \
  --report "$BASELINE_ARTIFACTS/eval-baseline/compare-latest.json" \
  --format json
```

### 4.3 Release 级稳定性与 checklist

`release checklist` 要求的是 release 级 stability window，而不是开发机 60 秒 smoke。发布前需要重新跑一份 8 小时 soak 报告：

```bash
uv run active-kb stability run \
  --config "$CONFIG" \
  --gate stability \
  --soak-seconds 28800 \
  --mixed-query-count 500 \
  --report "$BASELINE_ARTIFACTS/stability/release-gate.json" \
  --format json

uv run active-kb release checklist \
  --config "$CONFIG" \
  --quality-report "$LOCAL_ARTIFACTS/eval/quality.json" \
  --performance-report "$LOCAL_ARTIFACTS/perf/performance.json" \
  --stability-report "$BASELINE_ARTIFACTS/stability/release-gate.json" \
  --report "$BASELINE_ARTIFACTS/release/checklist.json" \
  --format json
```

Phase C 通过标准：

- `baseline validate` 返回 `status=ok`。
- `eval-baseline save` 首次建立成功，后续 `eval-baseline compare` 返回 `status=pass`。
- `release checklist` 只有在 8 小时 stability report 已生成时才应该期待 `status=pass`。

## 5. 建议的日常使用方式

- 日常本地联调：跑 Phase A。
- 提交前自测：跑 Phase A + Phase B。
- 准备发版或冻结 baseline：跑 Phase A + Phase B + Phase C。

这套分层方式的核心价值是：

- 不把真实本地工程问题和 synthetic gate 失败混在一起排查。
- 不把开发机 60 秒 stability smoke 误判成 release 已就绪。
- 在没有独立 CLI query 命令的前提下，仍然可以通过 MCP 同源 handler 做 live query smoke。
