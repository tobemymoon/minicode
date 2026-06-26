---
name: plan-task
description: Use before implementing a non-trivial coding task, refactor, bug fix, feature request, or repository-wide change.
tags: planning,implementation,refactor,feature,bugfix,计划,改代码
triggers: 制定计划,先计划,实现方案,改代码前,重构,新增功能,修改多个文件
negative_triggers: 直接改,马上执行
requires:
risk_level: low
auto_invoke: true
pre_skills:
post_skills:
allowed_tools: read,grep,find,ls
examples: 先给我一个实现计划,这个功能怎么改比较稳,改代码前先规划一下
---

## Goal

Create a concrete implementation plan before editing code.

## Procedure

1. Restate the user's goal in engineering terms.
2. Identify relevant files and modules.
3. Determine what needs to be changed.
4. Break the task into small implementation steps.
5. Define validation steps.
6. List risks and assumptions.

## Constraints

- Do not edit files in this skill.
- Do not produce vague plans.
- Do not over-engineer the solution.
- Prefer the smallest safe change that satisfies the request.

## Output format

Task goal:
Relevant files:
Implementation plan:
Validation plan:
Risks:
Assumptions:
