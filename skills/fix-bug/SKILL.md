---
name: fix-bug
description: Use when the user reports an error, stack trace, failing test, runtime exception, wrong output, or asks to fix a bug.
tags: bug,error,traceback,exception,debug,fix,报错,修复
triggers: 报错,修bug,修复错误,traceback,exception,失败,不对,wrong output,failing test
negative_triggers: review,只看,不要改
requires:
risk_level: medium
auto_invoke: true
pre_skills:
post_skills: run-tests
allowed_tools: read,grep,find,ls,edit,write,bash
examples: 这里报错了帮我修,这个测试失败了,为什么输出不对,根据这个 traceback 定位问题
---

## Goal

Find the root cause of the bug, apply the smallest safe fix, and verify the result.

## Procedure

1. Read the error message, stack trace, failing test, or user description.
2. Identify the related files and functions.
3. Reproduce the issue if possible.
4. Determine the root cause.
5. Make the smallest necessary code change.
6. Run the most relevant test or validation command.
7. If validation fails, analyze the new failure and continue once.
8. Summarize the fix and remaining risks.

## Constraints

- Do not rewrite unrelated code.
- Do not hide errors by broad try/except unless justified.
- Do not delete tests to make the bug disappear.
- Do not claim the bug is fixed unless validation was attempted.
- If validation cannot be run, explain why.

## Output format

Problem:
Root cause:
Changed files:
Fix:
Validation:
Remaining risks:
