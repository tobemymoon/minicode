---
name: repo-understanding
description: Use when the user asks to understand a repository, explain project structure, find entry points, identify modules, or determine how the project works.
tags: repository,architecture,entrypoint,structure,onboarding,repo,项目结构,入口,模块
triggers: 项目结构,怎么运行,功能在哪,熟悉仓库,理解项目,entry point,repository structure
negative_triggers: 修改,修复,删除,提交
requires:
risk_level: low
auto_invoke: true
pre_skills:
post_skills:
allowed_tools: read,grep,find,ls,bash
examples: 这个项目结构怎么看,这个项目怎么运行,这个功能在哪,帮我熟悉一下这个仓库
---

## Goal

Understand the repository structure and explain how the project is organized.

## Procedure

1. Inspect the top-level files and directories.
2. Identify the project type, language, framework, and package manager.
3. Locate entry points, configuration files, dependency files, and test directories.
4. Identify major modules and their responsibilities.
5. Find run, build, test, and lint commands from README, package files, Makefile, pyproject, setup files, or CI config.
6. Summarize the architecture without changing files.

## Constraints

- Do not modify files.
- Do not run destructive commands.
- Do not guess commands if they are not found; mark them as unknown.
- Prefer evidence from repository files.

## Output format

Project type:
Entry points:
Main directories:
Important config files:
Dependency files:
Run/build/test commands:
Architecture summary:
Potential risks:
Recommended next step:
