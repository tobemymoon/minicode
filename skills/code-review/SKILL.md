---
name: code-review
description: Use when the user asks to review code, review a diff, check recent changes, inspect a pull request, or find potential issues before commit.
tags: review,diff,pr,quality,security,testing,代码审查
triggers: review,代码审查,检查diff,看看改动,提交前检查,pull request,PR
negative_triggers: 修复,直接改,执行
requires:
risk_level: low
auto_invoke: true
pre_skills:
post_skills: run-tests
allowed_tools: read,grep,find,ls,bash
examples: 帮我 review 当前 diff,提交前检查一下,看看这段代码有没有问题
---

## Goal

Review code changes for correctness, maintainability, security, performance, and test coverage.

## Procedure

1. Inspect the changed files or target files.
2. Understand the intent of the change.
3. Check for correctness issues.
4. Check edge cases and error handling.
5. Check API compatibility and side effects.
6. Check whether tests or docs should be updated.
7. Separate blocking issues from optional improvements.

## Review checklist

Correctness:
- Does the code satisfy the intended behavior?
- Are edge cases handled?
- Are errors handled properly?

Maintainability:
- Is the code readable?
- Is there unnecessary complexity?
- Is duplicated logic introduced?

Security:
- Are there injection risks?
- Are secrets exposed?
- Is user input validated?

Testing:
- Are relevant tests added or updated?
- Are important paths untested?

## Constraints

- Do not modify code unless explicitly asked.
- Do not nitpick formatting unless it affects readability or consistency.
- Prioritize real bugs over style comments.

## Output format

Summary:
Blocking issues:
Non-blocking suggestions:
Testing gaps:
Recommended action:
