# CodeClaw 开发文档

本文档用于说明 CodeClaw 当前的架构设计、核心模块、开发流程和后续优化方向。它更偏向开发者视角，适合用来快速理解项目、准备面试讲解，以及后续继续扩展能力。

## 项目目标

CodeClaw 的目标是实现一个轻量级 AI Agent 编程助手系统。核心执行链路是：

```text
用户输入
  -> 模型流式生成
  -> 模型决定是否调用工具
  -> Agent 执行工具
  -> 工具结果回填上下文
  -> 模型继续推理或输出最终回答
```

当前优先级不是一上来做复杂多 Agent，而是先保证单 Agent 基座稳定：

- 模型调用协议统一
- 工具调用链路可靠
- 事件流可观测
- 本地编程工具可控
- 会话可保存、恢复、分叉和切换
- 长上下文可以压缩和检索
- Prompt Cache 有统计口径
- 扩展、Skill、MCP 能作为后续能力入口
- Skill Router 已支持结构化元信息、关键词召回、BGE-M3 向量召回、LLM rerank 和按需注入

## 源码结构

```text
src/ai/
  模型接入层：统一模型类型、模型注册表、provider 适配、流式事件转换、token 估算。

src/agent_core/
  Agent 编排层：模型无关的运行循环、工具执行、状态管理、Hook、事件分发。

src/coding_agent/
  编程助手应用层：CLI、会话管理、本地工具、上下文压缩、资源加载、扩展、Skill、MCP 桥接。
```

可以按照“三层架构”理解整个项目：

```text
ai             负责把不同模型 API 统一起来
agent_core     负责让模型和工具形成可观测的执行闭环
coding_agent   负责把 Agent 能力包装成真正可用的编程助手
```

## 模型接入层

`src/ai/` 的职责是屏蔽不同模型 API 的差异，让上层不用关心 Anthropic、OpenAI Compatible、Kimi、GLM 等接口细节。

项目内部使用统一对象表达模型请求：

```text
Context
Message
Tool
ToolCall
ToolResultMessage
Usage
```

provider 适配器负责两件事：

1. 把 CodeClaw 内部消息转换成目标模型需要的请求格式。
2. 把模型返回的流式响应转换成统一事件，例如文本增量、工具调用增量、消息结束和用量统计。

这样上层的 Agent Loop 只依赖统一协议，不直接依赖具体厂商接口。

## Agent 编排内核

`src/agent_core/` 是项目最核心的运行时层。它负责把一次用户请求变成完整 Agent 执行过程。

基本流程如下：

```text
1. 把用户输入追加到上下文
2. 调用模型流式接口
3. 消费模型流式事件并更新当前 assistant message
4. 如果模型返回工具调用，则解析 ToolCall
5. 根据配置并行或串行执行工具
6. 把工具结果包装成 ToolResultMessage 回填上下文
7. 继续调用模型，直到模型不再请求工具
8. 输出最终回答并结束本轮
```

运行过程中会产生可观测事件：

```text
agent_start
turn_start
message_start
message_update
message_end
tool_execution_start
tool_execution_update
tool_execution_end
turn_end
agent_end
```

外部应用层可以订阅这些事件，用来做 CLI 流式输出、会话持久化、调试日志、运行状态展示和错误兜底。

### 工具调用解析

模型看到的是工具名称、工具描述和参数 schema。模型如果决定调用工具，会返回结构化 `ToolCall`，其中包含：

```text
tool_call_id
tool_name
arguments
```

Agent Loop 不直接相信模型文本，而是基于 provider 已经解析好的工具调用块进行处理。工具执行完成后，结果会被包装成 `ToolResultMessage`，并通过 `tool_call_id` 与原始工具调用一一对应。

### 并行与串行工具执行

工具执行模式由配置决定：

```text
parallel    并行执行同一轮里的多个工具调用
sequential  按顺序执行工具调用
```

当前项目支持配置级切换。后续如果要更细粒度，可以根据工具元数据判断是否允许并行，例如写文件、执行 Shell 这类副作用工具更适合串行或加锁。

### Hook 拦截点

Hook 是 Agent Loop 预留的扩展点，典型用途包括：

```text
before_tool_call   工具执行前做权限检查、参数检查、人工确认
after_tool_call    工具执行后做结果审计、日志记录、错误改写
```

当前 Hook 机制已经具备流程入口，后续可以继续增强成真正的安全审批链，例如危险命令拦截、越界路径拦截和高风险写操作确认。

### 工具循环保护

如果模型连续多轮调用工具而不输出最终回答，Agent Loop 会触发工具迭代上限保护。触发后，系统会给模型追加一条内部约束，要求它停止调用工具，并基于已有上下文生成最终答复，避免陷入无限工具调用。

## 编程助手应用层

`src/coding_agent/` 把底层 Agent Runtime 包装成实际可用的编程助手。

主要能力包括：

- CLI 交互模式
- 单轮 print 模式
- 会话创建、保存、恢复
- 会话分叉与切换
- 上下文压缩
- 失败重试
- 本地编程工具
- 长期记忆入口
- 扩展和 Skill 加载
- MCP 工具桥接

应用层不是重新实现模型推理，而是负责把“模型 + 工具 + 会话 + 工作区”组织成一个面向开发任务的产品形态。

## 会话持久化

会话数据默认写入：

```text
.codeclaw/sessions/
```

会话中会保存：

- session id
- 会话树节点
- 当前 leaf id
- 用户消息
- assistant 消息
- 工具调用与工具结果
- Agent 事件日志
- token/cache 用量
- 上下文压缩摘要

会话持久化的核心价值是让 Agent 不只是一问一答，而是可以恢复历史、从某个节点分叉、对比不同解决路径，并保留执行过程证据。

## 内置工具

当前本地工具包括：

```text
read             读取文件片段，支持 offset 和 max_chars
write            写入文件
edit             精确文本替换
grep             正则搜索代码内容
find             查找文件
ls               查看目录
bash             执行 Shell 命令，并做基础危险命令拦截
search_artifact  搜索外置化的长工具结果
read_artifact    读取外置化 Artifact 的指定片段
```

这些工具会以 schema 形式暴露给模型，但真正执行发生在 CodeClaw 的 Python 工具分发器中。也就是说，模型只负责“选择工具和生成参数”，工具能力和权限边界由本地代码控制。

## 分层上下文压缩

长会话中，工具结果、代码片段和历史对话会不断膨胀。如果全部塞回模型上下文，会导致 token 成本上升、推理变慢，甚至超过上下文窗口。

当前压缩路线是：

```text
长 ToolResult
  -> 外置化保存到 ArtifactStore
  -> 在上下文中替换成摘要预览占位符
  -> 保留最近 N 条消息
  -> 对更旧历史生成结构化 Session Summary
  -> 裁剪时尽量保留 tool call/result 成组关系
  -> 检查摘要是否覆盖关键实体
  -> 需要细节时通过 search_artifact/read_artifact 按需恢复
```

### ArtifactStore

长工具结果会保存到：

```text
.codeclaw/artifacts/blobs/
```

元数据会记录到：

```text
.codeclaw/artifacts/artifacts.jsonl
```

模型上下文中不再直接放完整长文本，而是放一个包含 `artifact_id`、工具名、原始长度、行数和摘要预览的占位符。

### 结构化 Session Summary

上下文压缩触发后，较旧历史会被压缩成结构化摘要，通常包含：

```text
User Goals
Assistant Decisions
Tool Evidence
Tool Activity
Coverage Patch
```

摘要会持久化到：

```text
.codeclaw/sessions/<session_id>/session_summary.jsonl
```

这样后续会话不用完全依赖原始长历史，也可以保留任务目标、关键决策、工具证据和重要文件信息。

### 摘要质量检查

为了避免摘要“看起来很顺但漏掉关键信息”，项目会从被压缩历史中抽取关键实体：

```text
文件路径
函数名和类名
artifact id
用户约束
工具名称
```

如果摘要中缺少这些实体，会追加 `Coverage Patch`，把漏掉的关键信息补回摘要中。摘要质量结果会写入摘要日志和 `context_compacted` 事件。

### 最近消息保留和工具组裁剪

压缩时不会简单粗暴地只保留最后几条消息。如果裁剪点落在工具结果中间，系统会尽量往前移动边界，保留对应的 assistant tool call，避免留下“孤儿工具结果”。

## Prompt Cache 优化方向

Prompt Cache 的收益来自稳定上下文前缀。越稳定、越靠前、越重复的内容，越容易被模型服务端缓存复用。

推荐顺序是：

```text
稳定 system prompt
稳定且顺序固定的 tools
相对稳定的 memory / summary
动态用户输入和动态工具结果
```

当前项目已经记录以下字段，便于对比：

```text
input_tokens
output_tokens
cache_read
cache_write
total_tokens
```

交互式模式下可以使用：

```text
/usage
```

查看最近一轮和累计用量。

## 安全与权限方向

在继续扩展 Skill 或多 Agent 前，单 Agent 的安全边界需要继续加强：

- 限制工具访问工作区范围
- 阻断危险 Shell 命令
- 对高风险写操作增加人工确认
- 对工具调用做风险分级
- 保留完整事件日志作为审计依据
- 限制重复失败和无限工具调用

当前已经具备部分基础能力，例如工作区路径约束、危险命令基础拦截、Hook 入口和工具循环保护。后续可以把 Hook 发展成更完整的权限审批链。

## MCP 桥接

MCP 在项目中被视为“外部工具来源”。CodeClaw 的 MCP Bridge 会把 MCP 工具适配成内部 `AgentTool` 格式：

```text
MCP tool metadata
  -> CodeClaw AgentTool schema
  -> 模型可见的工具描述
  -> 工具调用参数转发给 MCP server
  -> MCP 执行结果转换成 ToolResultMessage
```

也就是说，模型看到的仍然是 CodeClaw 的统一工具协议，MCP 只是工具来源之一，不要求整个 Agent Runtime 都基于 MCP。

## 扩展与 Skill

扩展和 Skill 可以提供：

- 自定义工具
- 自定义命令
- before/after tool Hook
- before/after prompt Hook
- prompt 片段
- 领域能力说明

当前 Skill 已从“静态提示片段”升级为“动态路由能力”。Skill 可以放在：

```text
skills/<skill-name>/SKILL.md
.codeclaw/skills/<skill-name>.md
```

Skill frontmatter 支持：

```yaml
name:
description:
triggers:
negative_triggers:
requires:
risk_level:
auto_invoke:
pre_skills:
post_skills:
allowed_tools:
examples:
```

路由流程如下：

```text
用户输入
  -> keyword_recall 根据 triggers/tags/examples 等元信息召回
  -> bge_m3_recall 使用 BGE-M3 做语义召回
  -> candidates 合并与打分
  -> 低置信或多候选时调用 LLM rerank
  -> 只将命中的 Skill 正文动态注入当前轮上下文
```

这样可以避免所有 Skill 常驻 system prompt 导致召回空间过大、上下文噪声增加和 Prompt Cache 收益下降。

### BGE-M3 向量召回

Skill 向量召回由 `src/coding_agent/skill_embeddings.py` 负责。默认模型路径为：

```text
/data4/slx/models/bge-m3
```

CLI 可以通过以下参数控制：

```text
--skill-embedding-backend auto|local|bge-m3|off
--skill-embedding-model-path /data4/slx/models/bge-m3
```

`auto` 模式会优先加载 BGE-M3，如果 `sentence-transformers` 或模型目录不可用，会自动降级为本地轻量召回，保证主流程不被 embedding 环境阻塞。

### Skill 权限与审批

Skill 可以声明 `risk_level`、`auto_invoke` 和 `allowed_tools`。当前已经实现：

- 高风险 Skill 不自动执行。
- `git-workflow` 可以通过 `disable-model-invocation` 避免被模型自动调用。
- 依赖安装类 bash 命令会触发人工确认，例如 `pip install pandas`。
- `allowed_tools` 已从提示约束升级为运行时强制拦截。
- 高风险 shell、敏感路径读写、git push/commit、curl/wget 等操作会进入统一安全确认链路。

在 Skill 路由、上下文治理和基础权限链路稳定后，项目已经继续实现中心化多 Agent 协作、Workflow DAG 和安全审查链路。

## 中心化多 Agent 协作

多 Agent 采用中心化架构：

```text
CoordinatorAgent
  -> run_subagent / run_agent_team
  -> Specialist Agent
  -> AgentResult
  -> SharedState / Trace
  -> Coordinator 汇总
```

子 Agent 不直接互相通信，所有结果统一回到主 Agent。当前 Specialist Agent 包括：

```text
RepoExplorerAgent
PlannerAgent
CodeEditorAgent
TestRunnerAgent
CodeReviewAgent
DependencyScanAgent
SecurityReviewAgent
```

每个子 Agent 通过 Agent Card 描述：

```text
name
description
role
risk_level
auto_invoke
allowed_tools
forbidden
max_context_tokens
```

执行结果统一为 `AgentResult`，包含：

```text
status
summary
evidence
changed_files
commands_run
artifacts
next_actions
risk_level
```

### Workflow DAG

`run_agent_team` 会生成动态 Workflow DAG，而不是固定自然语言流程。当前默认节点为：

```text
explore
dependency_scan
security_scan
plan
verify
review
```

其中 `explore / dependency_scan / security_scan` 属于只读扫描组：

```text
parallel_group = read_only_scan
```

运行数据持久化到：

```text
.codeclaw/multi_agent/<task_id>/workflow.json
.codeclaw/multi_agent/<task_id>/node_status.json
.codeclaw/multi_agent/<task_id>/shared_state.json
.codeclaw/multi_agent/<task_id>/trace.jsonl
```

Trace 记录事件包括：

```text
workflow_planned
parallel_group_started
node_started
node_finished
node_failed
parallel_group_finished
workflow_finished
```

当前默认采用“并行批次语义 + 安全顺序执行”，避免多个子 Agent 同时争用 git/subprocess；后续可以打开线程并行执行。

### Fork / Worktree 模式

`run_agent_team` 支持三种协作模式：

```text
team      当前工作区执行 Agent Team
fork      记录逻辑会话分叉计划
worktree  在干净 git 工作区中创建隔离 worktree
```

`worktree` 模式会检查主工作区是否 clean；如果存在未提交改动，会拒绝创建 worktree，避免从脏状态派生隔离目录。

## 权限与安全审查

安全链路由 `src/coding_agent/security.py` 提供，主要包括：

- `RiskClassifier`：对工具调用进行风险分类；
- 规则过滤：危险 shell 直接拦截；
- 敏感路径检测：`.env`、`.ssh`、私钥、credentials 等；
- 人工确认：依赖安装、git push/commit、curl/wget、高风险敏感写入；
- Prompt Injection 检测：工具输出中出现越权指令时标记为不可信；
- before/after tool hook：执行前审查，执行后标记可疑内容；
- 安全事件写入 session events。

示例分类：

```text
rm -rf /tmp/x               -> critical / block
pip install pandas          -> high / confirm
git push origin main        -> high / confirm
python -m compileall -q src -> low / allow
write .env                  -> high / confirm
```

如果工具输出中出现 `ignore previous instructions`、泄露系统提示词、泄露 key 等模式，系统会在工具结果前加入 `[Security Notice]`，要求模型将其视为不可信数据而不是指令。

## 开发流程

源码运行帮助：

```bash
PYTHONPATH=src python -m coding_agent --help
```

启动交互式会话：

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace .
```

快速触发上下文压缩：

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --max-context-messages 6 \
  --retain-recent-messages 2
```

查看运行日志：

```bash
cat .codeclaw/sessions/*/session_summary.jsonl
grep -R '"type": "context_compacted"' .codeclaw/sessions/*/events.jsonl
find .codeclaw/artifacts -type f
```

编译检查：

```bash
python -m compileall -q src
```

如果当前分支包含测试：

```bash
python -m pytest -q
```

## 后续路线

短期重点：

- 为安全审查链路补充确定性测试；
- 为 Workflow DAG、node_status 和 trace 增加 eval 用例；
- 完善 WorktreeRunner 的写入隔离与 patch 生成；
- 继续优化 Prompt Cache 的稳定前缀布局和统计对比。

中期重点：

- PatchManager：生成 patch、检查 patch 是否可应用；
- CandidateResult：比较多个候选方案；
- JudgeAgent：根据测试、diff 大小和风险评分选择候选；
- ResumeWorkflow：基于 workflow.json 和 node_status.json 断点续跑；
- EvalRunner：评估规划、并行安全、候选选择和恢复能力。

长期方向：

- Worktree 中真正并行写入；
- MergeManager 安全合并和失败回滚；
- 多候选 patch 自动验证与人工审批；
- 更完善的 Agent Team 质量评估体系。

## 工程日志

### 2026-06-26 Skill 路由与向量召回优化

本次优化将 Skill 系统从静态扩展提示升级为动态路由能力：

- 扩展 Skill frontmatter，支持 triggers、negative_triggers、requires、risk_level、auto_invoke、pre_skills、post_skills、allowed_tools 和 examples。
- 支持 `skills/*/SKILL.md` 和 `.codeclaw/skills/*.md` 两类 Skill 组织方式。
- 实现关键词召回、BGE-M3 向量召回和 LLM rerank 的组合路由链路。
- 只将命中的 Skill 注入当前轮上下文，避免全量 Skill 常驻 system prompt。
- 新增 `/skills` 命令查看已加载 Skill。
- 新增依赖安装审批 Hook，模型请求 `pip install` 等安装命令时需要用户确认。
- 新增 `skill_embeddings.py`，支持 `auto/local/bge-m3/off` 后端，并在 BGE-M3 不可用时自动降级。

当前 Skill V1/V2 已完成，可以支撑领域 Skill 扩展和后续中心化多 Agent 协作。

### 2026-06-28 多 Agent、安全审查与开发体验优化

本次优化将 CodeClaw 从单 Agent + Skill 路由进一步推进到工程化协作框架：

- 新增 `multi_agent.py`，实现中心化多 Agent 协作骨架。
- 新增 Agent Card、AgentResult、SharedState、FileWriteLock 和 MultiAgentStore。
- 新增 `run_subagent` 与 `run_agent_team` 工具。
- 支持 Coordinator 统一调度，子 Agent 以 Tool Call 方式受控执行。
- 新增 RepoExplorerAgent、PlannerAgent、CodeEditorAgent、TestRunnerAgent、CodeReviewAgent、DependencyScanAgent 和 SecurityReviewAgent。
- 支持 `team/fork/worktree` 三种协作模式。
- 新增 Workflow DAG、ParallelScheduler、workflow.json、node_status.json 和更细粒度 trace。
- 新增 `security.py`，实现 RiskClassifier、规则过滤、敏感路径检测、人工确认和 Prompt Injection 检测。
- 将 Skill `allowed_tools` 从提示约束升级为运行时强制门控。
- CLI 默认恢复最近 session，避免频繁重启产生大量新会话。
- 新增 `watch_compile.py` 和 `codeclaw-watch`，支持代码保存后自动编译。
- README 已重写为当前系统总览版。

当前项目已经具备统一模型接入、Agent 编排、编程工具、长期记忆、上下文压缩、Skill 路由、多 Agent 协作、Workflow DAG 和权限安全审查的完整主干。
