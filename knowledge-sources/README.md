# Active Knowledge Sources

This directory is the default source-document root for `active-knowledge-server`.

Place documents here when they should be remembered by the RAG system but should not live inside a Skill body. Suggested subdirectories:

- `api/`: API and SDK documents
- `widgets/`: UI widget and component usage documents
- `engineering/`: architecture notes, coding rules, debug guides, FAQ
- `product/`: product requirements, feature briefs, release-scope notes
- `design/`: UI specs, screen flows, design tokens
- `project/`: plans, milestones, risks, decisions
- `qa/`: test strategy, defect analysis, validation checklists
- `release/`: release notes and compatibility records
- `learned-seeds/`: curated seed knowledge cards and regression samples

Recommended front matter:

```yaml
---
doc_type: api
domain: engineering
version: "1.0"
authority_level: official
owner: active
tags: []
---
```
