---
name: run-tests
description: Use after code changes or when the user asks to test, validate, run checks, run unit tests, run lint, or confirm that a change works.
tags: tests,validation,pytest,lint,mypy,ci,测试,验证
triggers: 跑测试,验证一下,run tests,pytest,lint,检查是否通过,confirm change works
negative_triggers: 删除,清空,reset,rm -rf
requires:
risk_level: medium
auto_invoke: true
pre_skills:
post_skills:
allowed_tools: read,grep,find,ls,bash
examples: 跑一下测试,验证刚才的改动,检查这个修改有没有问题
---

## Goal

Run the most relevant validation commands for the current project and summarize the result.

## Procedure

1. Identify the project language and package manager.
2. Look for test commands in README, package.json, pyproject.toml, Makefile, tox.ini, noxfile.py, pytest.ini, Cargo.toml, go.mod, pom.xml, or CI config.
3. Prefer targeted tests when a specific file or module changed.
4. If no targeted test is obvious, run the smallest safe general validation command.
5. Capture command, exit code, and important output.
6. If tests fail, summarize the failure and likely cause.

## Common commands

Python:
- pytest
- python -m pytest
- ruff check .
- mypy .

Node:
- npm test
- npm run test
- pnpm test
- yarn test
- npm run lint

Go:
- go test ./...

Rust:
- cargo test
- cargo clippy

Java:
- mvn test
- gradle test

## Constraints

- Do not install dependencies unless the user requested it or the project instructions require it.
- Do not run destructive commands.
- Do not ignore failing tests.
- Do not claim success if only lint passed but tests were not run.

## Output format

Detected project type:
Command run:
Result:
Key output:
Failure reason:
Next action:
