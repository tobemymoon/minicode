---
name: git-workflow
description: Use when the user asks for git status, diff summary, commit message, branch creation, staging guidance, push guidance, or pull request description.
disable-model-invocation: true
tags: git,commit,push,branch,pr,diff,提交
triggers: git status,git diff,commit,push,提交,分支,PR,拉取请求
negative_triggers:
requires:
risk_level: high
auto_invoke: false
pre_skills: run-tests
post_skills:
allowed_tools: bash
examples: 帮我写提交信息,总结当前diff,帮我push到github
---

## Goal

Help prepare safe git commits and pull request descriptions.

## Procedure

1. Inspect git status.
2. Inspect git diff.
3. Summarize changed files.
4. Decide whether changes are coherent enough for one commit.
5. Generate a concise commit message.
6. Generate a PR description if requested.
7. Recommend tests to run before pushing.

## Commit message format

Use one of:

- feat: ...
- fix: ...
- refactor: ...
- docs: ...
- test: ...
- chore: ...

## PR description format

## Summary
- ...

## Changes
- ...

## Tests
- ...

## Risks
- ...

## Constraints

- Do not commit unless the user explicitly asks.
- Do not push unless the user explicitly asks.
- Do not include unrelated files in a commit.
- Warn if secrets, large files, generated files, or environment files appear in the diff.

## Output format

Changed files:
Diff summary:
Suggested commit message:
Suggested PR description:
Pre-push checklist:
