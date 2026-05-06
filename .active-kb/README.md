# Active Knowledge Local Store

This directory is the repository-level local-first storage root for Active Knowledge Server.

- `baseline/` stores distributable, reusable index baseline artifacts.
- `local/` stores machine-local overlays, jobs, cache, logs, temporary files, and locks.

Normal user indexing should write only to `local/`. The `baseline/` directory should be updated only by a release or `baseline publish` workflow.
