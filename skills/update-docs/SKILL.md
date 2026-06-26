---
name: update-docs
description: Use when code changes affect public APIs, CLI usage, configuration, installation steps, environment variables, examples, or when the user asks to update documentation.
tags: docs,readme,documentation,cli,config,文档
triggers: 更新文档,README,docs,文档同步,配置说明,CLI用法,安装步骤
negative_triggers: 不改文档,先不用改文档
requires:
risk_level: low
auto_invoke: true
pre_skills:
post_skills:
allowed_tools: read,grep,find,ls,edit,write
examples: 更新README,把这次改动写进文档,同步CLI参数说明
---

## Goal

Update documentation so it matches the current code behavior.

## Procedure

1. Identify what changed in code.
2. Determine whether README, docs, examples, comments, or changelog need updates.
3. Update only the relevant documentation.
4. Keep documentation concise and accurate.
5. Include examples when behavior or usage changed.
6. Check that commands, paths, options, and environment variables match the code.

## Documentation targets

- README.md
- docs/
- CHANGELOG.md
- API documentation
- CLI usage
- configuration examples
- environment variable examples
- code comments
- examples/

## Constraints

- Do not document behavior that does not exist.
- Do not rewrite unrelated documentation.
- Do not add marketing language.
- Do not remove important setup steps.
- Keep examples runnable where possible.

## Output format

Docs updated:
Reason:
Changed sections:
Validation:
Remaining gaps:
