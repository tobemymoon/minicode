# CodeClaw Development Guide

This document records the current architecture, development workflow, and
optimization direction for CodeClaw.

## Project Goal

CodeClaw aims to become a compact AI coding agent runtime inspired by Claude
Code. The core loop is:

```text
user query -> model streaming response -> optional tool calls -> tool results
-> model continues -> final answer
```

The current priority is a strong single-agent foundation:

- stable model access
- reliable tool calling
- observable event flow
- safe local coding tools
- session persistence and fork/switch
- layered context compression
- prompt cache measurement
- extension, skill, and MCP integration points

Multi-agent collaboration should be added only after context governance and
safety hooks are stable.

## Source Layout

```text
src/ai/
  Unified model types, provider registry, streaming adapters, token estimation.

src/agent_core/
  Provider-independent agent runtime: event loop, tool execution, state, hooks.

src/coding_agent/
  Application layer: CLI, sessions, local tools, compression, resources,
  extensions, skills, MCP bridge.
```

## Key Concepts

### Unified AI Layer

The `ai` package normalizes provider-specific APIs into common types:

- `Message`
- `Context`
- `Tool`
- `ToolCall`
- `ToolResultMessage`
- `Usage`

Providers convert CodeClaw messages into their API-specific payloads and convert
streaming responses back into internal events.

### Agent Loop

The `agent_core` package owns the deterministic runtime workflow:

1. Append user prompt to context.
2. Call the model through `stream_simple`.
3. Emit streaming message events.
4. Parse assistant `ToolCall` blocks.
5. Execute tools in parallel or sequential mode.
6. Append `ToolResultMessage` values.
7. Continue until the model stops calling tools.

The loop emits observable events such as:

- `agent_start`
- `turn_start`
- `message_start`
- `message_update`
- `message_end`
- `tool_execution_start`
- `tool_execution_update`
- `tool_execution_end`
- `turn_end`
- `agent_end`

The loop also has a maximum tool-iteration guard. If the model keeps calling
tools, CodeClaw appends a guard result and asks the model for a final no-tool
answer using the available context.

### Coding Agent Layer

The `coding_agent` package adds product-level capabilities:

- interactive CLI
- single-prompt execution
- session persistence
- session fork/switch
- context compaction
- retry on transient model errors
- local coding tools
- workspace resource loading
- extension and skill loading
- MCP tool bridge

## Built-in Tools

Current local tools include:

- `read`: read file chunks with `offset` and `max_chars`
- `write`: write files
- `edit`: precise text replacement
- `grep`: regex search
- `find`: file discovery
- `ls`: directory listing
- `bash`: shell execution with basic dangerous-command blocking
- `search_artifact`: keyword search inside externalized artifacts
- `read_artifact`: recover small chunks from externalized artifacts

The tools are exposed to the model through JSON-schema-like parameter
definitions, but execution happens inside CodeClaw's Python tool dispatcher.

## Layered Context Compression

The current design is:

```text
long tool result
  -> externalize to artifact store
  -> replace context content with Summary Preview placeholder
  -> preserve recent N messages
  -> summarize older history into structured session summary
  -> preserve tool call/result group boundaries
  -> validate summary coverage
  -> recover exact details with search_artifact/read_artifact only when needed
```

### Artifact Store

Long `ToolResultMessage` content is written under:

```text
.codeclaw/artifacts/blobs/
```

Metadata is written to:

```text
.codeclaw/artifacts/artifacts.jsonl
```

The prompt sees only a compact placeholder containing:

- `artifact_id`
- tool name
- original character count
- line count
- structured summary preview

### Structured Session Summary

When context compaction triggers, older history is summarized into a structured
message with sections:

- `User Goals`
- `Assistant Decisions`
- `Tool Evidence`
- `Tool Activity`
- optional `Coverage Patch`

Each summary is persisted to:

```text
.codeclaw/sessions/<session_id>/session_summary.jsonl
```

### Summary Quality Check

CodeClaw extracts key entities from compressed history:

- file paths
- function and class names
- artifact IDs
- user constraints
- tool names

It checks whether the generated summary contains those entities. Missing items
are appended as a `Coverage Patch`, and the quality result is stored in both the
summary log and compaction event.

### Tool Group Boundary

When keeping recent messages, CodeClaw avoids retaining orphan tool results. If
the cut point lands on a `ToolResultMessage`, it moves the boundary backward so
the corresponding assistant tool call remains in context.

## Prompt Cache Direction

Prompt cache optimization should keep stable context at the front:

1. Stable system prompt.
2. Stable, consistently ordered tool definitions.
3. Stable memory or summary blocks where possible.
4. Dynamic user/task content later in the message list.

The project already records usage fields:

- `input_tokens`
- `output_tokens`
- `cache_read`
- `cache_write`
- `total_tokens`

Use `/usage` in interactive mode to compare runs.

## Safety Direction

Before adding multi-agent collaboration, strengthen safety in the single-agent
runtime:

- enforce workspace path boundaries
- block destructive shell patterns
- add approval hooks for risky tools
- classify AI/tool risk levels
- preserve audit logs in event streams
- avoid uncontrolled repeated tool calls

## MCP Bridge

The MCP bridge adapts MCP-style tools into CodeClaw `AgentTool` objects. The
model still sees a normal CodeClaw tool schema; the bridge handles remote tool
metadata, parameter forwarding, and result conversion.

This project currently treats MCP as a tool source. It does not require the
entire agent runtime to be MCP-based.

## Extensions and Skills

Extensions and skills can add:

- tools
- command handlers
- before/after tool hooks
- before/after prompt hooks
- prompt snippets

The recommended direction is a layered skill system:

```text
raw tools -> high-level skills -> skill directory / metadata -> skill router
```

Do not add multi-agent orchestration before this routing and safety layer is
clear enough.

## Development Workflow

Compile-check:

```bash
python -m compileall -q src
```

Run the CLI from source:

```bash
PYTHONPATH=src python -m coding_agent --help
```

Run a quick interactive session:

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace .
```

Trigger compaction quickly:

```bash
PYTHONPATH=src python -m coding_agent \
  --mode interactive \
  --provider anthropic \
  --model-id kimi-k2.5 \
  --workspace . \
  --max-context-messages 6 \
  --retain-recent-messages 2
```

Inspect runtime logs:

```bash
cat .codeclaw/sessions/*/session_summary.jsonl
grep -R '"type": "context_compacted"' .codeclaw/sessions/*/events.jsonl
find .codeclaw/artifacts -type f
```

## Roadmap

### Short Term

- Stabilize layered context compression with real CLI testing.
- Add more deterministic tests for summary quality and tool loop guards.
- Improve safety hooks for risky file and shell operations.
- Improve prompt cache measurement and stable-prefix layout.

### Medium Term

- Build a skill router for domain-specific capabilities.
- Add self-evolving memory extraction from successful fixes and user
  preferences.
- Add artifact search upgrades based on ripgrep or structured indexes.

### Later

- Add center-led multi-agent collaboration.
- Use a main agent for planning and review.
- Use sub-agents as controlled tools with minimal permissions.
- Add worktree/fork/team modes only after safety controls are mature.

