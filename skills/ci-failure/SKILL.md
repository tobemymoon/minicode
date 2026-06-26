---
name: ci-failure
description: Use when the user asks why CI failed, why GitHub Actions failed, why tests pass locally but fail in CI, or provides a CI log.
tags: ci,github-actions,logs,test-failure,build,持续集成
triggers: CI失败,GitHub Actions失败,workflow failed,tests pass locally,ci log,构建失败
negative_triggers: git push,commit,提交代码
requires:
risk_level: high
auto_invoke: true
pre_skills:
post_skills: run-tests
allowed_tools: read,grep,find,ls,bash
examples: 帮我看CI为什么失败,这个GitHub Actions日志怎么修,本地过了CI不过
---

## Goal

Analyze CI failure logs, identify the failing stage, and propose or apply the smallest fix.

## Procedure

1. Identify the CI system: GitHub Actions, GitLab CI, Jenkins, CircleCI, or other.
2. Find the first real error, not just the final summary.
3. Identify the failing job, step, command, and exit code.
4. Classify the failure.
5. Determine whether it is a code issue, test issue, dependency issue, environment issue, permission issue, or flaky failure.
6. Propose the smallest fix.
7. If asked to fix, update the relevant code or CI config.
8. Recommend validation steps.

## Failure categories

- Test failure
- Lint failure
- Type-check failure
- Build failure
- Dependency installation failure
- Missing environment variable
- Permission or token issue
- Path or working-directory issue
- OS or version mismatch
- Network or cache issue
- Flaky test

## Constraints

- Do not assume the last line is the root cause.
- Do not modify CI config unless the failure is caused by CI configuration.
- Do not bypass tests to make CI pass.
- Do not remove failing checks unless explicitly requested and justified.

## Output format

CI system:
Failing job:
Failing command:
Key error:
Failure category:
Root cause:
Recommended fix:
Validation:
