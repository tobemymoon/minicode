# CodeClaw

CodeClaw is a lightweight AI coding agent runtime inspired by Claude Code. It
combines a query loop, tool calling, session persistence, context compression,
prompt cache support, local coding tools, extension loading, and MCP tool
bridging into a small Python project.

The project is currently focused on building a stable single-agent foundation:
model access, deterministic agent orchestration, coding tools, session memory,
and layered context governance. Multi-agent collaboration is planned after the
single-agent context and safety layers are stable.

## Highlights

- Unified model access layer for Anthropic Messages and OpenAI-compatible APIs.
- Event-driven agent loop with streaming responses, tool calls, tool execution,
  hook points, retries, and runtime state updates.
- Coding-agent application layer with CLI modes, session persistence, session
  fork/switch, context compaction, prompt cache statistics, and local tools.
- Built-in tools for file read/write, precise edit, grep, find, ls, bash,
  artifact search, and artifact chunk recovery.
- Layered context compression using structured session summaries, recent message
  retention, tool-call/result boundary preservation, artifact externalization,
  summary quality checks, and on-demand artifact search.
- Extension and skill loading support for custom commands, hooks, prompt
  snippets, and tool registration.
- MCP bridge for adapting MCP tools into CodeClaw's internal AgentTool format.

## Architecture

```text
User / CLI
   |
   v
coding_agent
   |-- session lifecycle
   |-- local coding tools
   |-- context compression
   |-- extension / skill loading
   |
   v
agent_core
   |-- event-driven query loop
   |-- tool call parsing
   |-- parallel / sequential tool execution
   |-- hook interception
   |-- retry and runtime state
   |
   v
ai
   |-- unified Message / Tool / Context types
   |-- Anthropic Messages provider
   |-- OpenAI-compatible provider
   |-- streaming event normalization
```

## Install

Create and activate your Python environment first, then install the project in
editable mode:

```bash
cd /data4/slx/XingClaw
pip install -e .
```

For source-tree execution without installation:

```bash
PYTHONPATH=src python -m coding_agent --help
```

## Configure Models

CodeClaw reads API keys from environment variables.

For Anthropic-compatible models:

```bash
export ANTHROPIC_API_KEY="..."
```

For Kimi through the Anthropic Messages-compatible endpoint:

```bash
export MOONSHOT_API_KEY="..."
# or
export KIMI_API_KEY="..."
```

The model registry includes Claude, GLM, Kimi, and OpenAI-compatible models in
`src/ai/models.py`.

## Run

Interactive mode:

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace .
```

Single prompt mode:

```bash
PYTHONPATH=src python -m coding_agent \
  --mode print \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --prompt "解释 CodeClaw 的 Agent Loop 是怎么工作的"
```

Read-only mode:

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --read-only
```

## Context Compression Test

Use a low threshold to trigger compaction quickly:

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --max-context-messages 6 \
  --retain-recent-messages 2
```

Then ask:

```text
阅读 src/coding_agent/artifacts.py，告诉我 ArtifactStore 和 ContextCompressor 分别做什么
```

Inspect summary and event logs:

```bash
cat .codeclaw/sessions/*/session_summary.jsonl
grep -R '"type": "context_compacted"' .codeclaw/sessions/*/events.jsonl
find .codeclaw/artifacts -type f
```

## Runtime Data

Runtime state is written under `.codeclaw/` and is ignored by Git:

```text
.codeclaw/sessions/
.codeclaw/artifacts/
```

The project still supports reading legacy `.xingclaw/` data where needed, but
new runtime files are written to `.codeclaw/`.

## Development

Compile-check the source tree:

```bash
python -m compileall -q src
```

If tests are present in your checkout:

```bash
python -m pytest -q
```

See [docs/development.md](docs/development.md) for architecture notes,
development workflow, and the optimization roadmap.

