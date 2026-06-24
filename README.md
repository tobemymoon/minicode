# CodeClaw AI Agent 编程助手系统

CodeClaw 是一个轻量级 AI Coding Agent 项目，参考 Claude Code 的交互方式和架构思想，围绕 **Query Loop + Tool Use** 构建任务执行闭环。项目将大模型接入、Agent 编排、会话管理、本地编程工具、上下文压缩、Prompt Cache 统计、扩展加载和 MCP 工具桥接组织在一个 Python 工程中，方便学习、实验和持续演进。

当前项目重点是先打稳单 Agent 基座：统一模型调用协议、稳定工具调用链路、可观测事件流、会话持久化、分层上下文治理和基础安全控制。多 Agent 协作、Skill 路由和更强的记忆沉淀机制属于后续优化方向。

## 核心能力

- **统一模型接入层**：封装 Anthropic Messages、OpenAI Compatible 等不同 API 形态，支持 Claude、Kimi、GLM 等模型通过统一接口切换。
- **Agent 编排内核**：实现事件驱动的运行循环，支持模型流式生成、工具调用解析、并行/串行工具执行、Hook 拦截点、错误处理和运行状态管理。
- **编程助手应用层**：支持 CLI 交互、单轮运行、会话持久化、会话分叉/切换、上下文压缩、失败重试和 token/cache 用量统计。
- **本地编程工具**：提供文件读取、写入、精确编辑、目录查看、代码检索、Shell 执行、Artifact 检索与恢复等能力。
- **分层上下文压缩**：将长工具结果外置化为 Artifact，用摘要预览替换上下文，结合结构化 Session Summary、最近消息保留、工具调用组裁剪和摘要质量检查，降低长会话上下文压力。
- **Prompt Cache 支持**：保留稳定系统提示词、稳定工具列表和尽量稳定的上下文前缀，并记录 `cache_read`、`cache_write` 等统计信息，方便对比优化效果。
- **扩展与 MCP 桥接**：支持扩展加载、命令注册、Hook 注入、工具注册，并提供 MCP 工具到内部 `AgentTool` 的适配桥。

## 架构概览

```text
用户 / CLI
   |
   v
coding_agent 应用层
   |-- 会话生命周期管理
   |-- 本地编程工具
   |-- 上下文压缩与 Artifact
   |-- 扩展 / Skill / MCP 桥接
   |
   v
agent_core 编排层
   |-- 事件驱动 Query Loop
   |-- 工具调用解析
   |-- 并行 / 串行工具执行
   |-- Hook 拦截点
   |-- 重试与运行状态
   |
   v
ai 模型接入层
   |-- 统一 Message / Tool / Context 类型
   |-- Anthropic Messages 适配
   |-- OpenAI Compatible 适配
   |-- 流式事件归一化
```

## 安装

进入项目目录，并在你的 Python 环境中安装：

```bash
cd /data4/slx/XingClaw
pip install -e .
```

如果不想安装，也可以通过源码路径直接运行：

```bash
PYTHONPATH=src python -m coding_agent --help
```

## 模型配置

项目从环境变量读取 API Key。

Anthropic 或 Anthropic Messages 兼容接口：

```bash
export ANTHROPIC_API_KEY="你的 key"
```

Kimi 可通过 Anthropic Messages 兼容接口接入：

```bash
export MOONSHOT_API_KEY="你的 key"
# 或者
export KIMI_API_KEY="你的 key"
```

模型注册表位于：

```text
src/ai/models.py
```

这里维护了 Claude、Kimi、GLM、OpenAI Compatible 等模型的 `provider`、`api`、`base_url`、上下文窗口和最大输出长度等配置。

## 启动方式

交互式 CLI：

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace .
```

单轮执行：

```bash
PYTHONPATH=src python -m coding_agent \
  --mode print \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --prompt "解释 CodeClaw 的 Agent Loop 是怎么工作的"
```

只读模式：

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --read-only
```

## 常用命令

交互式 CLI 中可以使用以下命令：

```text
/help      查看可用命令
/session   查看当前会话和叶子节点
/tree      查看会话树
/new       创建新会话分支
/fork      从指定节点分叉
/switch    切换到指定叶子节点
/memory    管理长期记忆
/usage     查看最近一轮和累计 token/cache 用量
/clear     清空当前上下文
```

## 上下文压缩测试

可以用较小阈值快速触发压缩：

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --max-context-messages 6 \
  --retain-recent-messages 2
```

然后输入类似问题：

```text
阅读 src/coding_agent/artifacts.py，告诉我 ArtifactStore 和 ContextCompressor 分别做什么
```

查看压缩摘要、事件日志和外置化 Artifact：

```bash
cat .codeclaw/sessions/*/session_summary.jsonl
grep -R '"type": "context_compacted"' .codeclaw/sessions/*/events.jsonl
find .codeclaw/artifacts -type f
```

## 运行时数据

新的运行时数据默认写入 `.codeclaw/`：

```text
.codeclaw/sessions/
.codeclaw/artifacts/
```

这些目录用于保存会话树、事件日志、结构化摘要和长工具结果 Artifact，通常不提交到 Git。项目仍兼容读取部分历史 `.xingclaw/` 数据，但新数据会写入 `.codeclaw/`。

## 开发检查

编译检查：

```bash
python -m compileall -q src
```

如果当前分支包含测试：

```bash
python -m pytest -q
```

更多架构说明和开发路线见 [docs/development.md](docs/development.md)。
