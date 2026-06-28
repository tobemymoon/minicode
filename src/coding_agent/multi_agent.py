from __future__ import annotations

"""
中心化多 Agent V1。

这一版先提供受控 Specialist 工具，而不是让多个 Agent 彼此自由对话：
- Coordinator 仍是主 Agent；
- 子 Agent 通过 run_subagent 工具被调用；
- 子 Agent 返回结构化 AgentResult；
- 写操作不在子 Agent 内直接执行，避免并发改文件冲突。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import json
import re
import shlex
import subprocess
import uuid
from typing import Any, Literal

from ai.types import TextContent
from agent_core import AgentTool, AgentToolResult

RiskLevel = Literal["low", "medium", "high"]
AgentStatus = Literal["success", "failed", "partial", "need_more_context"]
CollaborationMode = Literal["team", "fork", "worktree"]
TaskMode = Literal["read_only", "write", "verify", "review", "judge", "merge"]
WorkspaceMode = Literal["current", "worktree", "none"]


@dataclass
class AgentResult:
    agent_name: str
    status: AgentStatus
    summary: str
    evidence: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    next_actions: list[str] = field(default_factory=list)
    risk_level: RiskLevel = "low"


@dataclass
class AgentCard:
    name: str
    description: str
    role: str
    risk_level: RiskLevel
    auto_invoke: bool
    allowed_tools: list[str]
    requires: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    max_context_tokens: int = 12000


@dataclass
class AgentTask:
    id: str
    agent_name: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    mode: TaskMode = "read_only"
    parallel_group: str | None = None
    workspace_mode: WorkspaceMode = "current"
    required_context_keys: list[str] = field(default_factory=list)
    output_keys: list[str] = field(default_factory=list)
    risk_level: RiskLevel = "low"
    timeout_seconds: int = 300
    max_retries: int = 0
    command: str = ""


@dataclass
class AgentWorkflow:
    task_id: str
    user_request: str
    workflow_type: str
    nodes: list[AgentTask]
    max_parallel: int = 3
    max_write_parallel: int = 2
    max_fix_iterations: int = 2
    allow_write: bool = False
    allow_worktree: bool = False
    allow_auto_merge: bool = False
    budget_tokens: int | None = None
    budget_seconds: int | None = None


@dataclass
class SharedState:
    user_request: str
    task_id: str
    repo_summary: str | None = None
    relevant_files: list[str] = field(default_factory=list)
    implementation_plan: str | None = None
    changed_files: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    test_result: str | None = None
    review_result: str | None = None
    risks: list[str] = field(default_factory=list)
    final_summary: str | None = None


class FileWriteLock:
    def __init__(self) -> None:
        self.locked_files: set[str] = set()

    def acquire(self, agent_name: str, file_path: str) -> bool:
        _ = agent_name
        if file_path in self.locked_files:
            return False
        self.locked_files.add(file_path)
        return True

    def release(self, file_path: str) -> None:
        self.locked_files.discard(file_path)


class MultiAgentStore:
    def __init__(self, workspace: Path, task_id: str) -> None:
        self.workspace = workspace.resolve()
        self.task_id = task_id
        self.root = self.workspace / ".codeclaw" / "multi_agent" / task_id
        self.trace_file = self.root / "trace.jsonl"
        self.state_file = self.root / "shared_state.json"
        self.workflow_file = self.root / "workflow.json"
        self.node_status_file = self.root / "node_status.json"

    def ensure_initialized(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def write_state(self, state: SharedState) -> None:
        self.ensure_initialized()
        self.state_file.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_workflow(self, workflow: AgentWorkflow) -> None:
        self.ensure_initialized()
        self.workflow_file.write_text(json.dumps(asdict(workflow), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_node_status(self, status: dict[str, str]) -> None:
        self.ensure_initialized()
        self.node_status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_trace(
        self,
        *,
        event_type: str,
        agent_name: str,
        task: str,
        result: AgentResult | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_initialized()
        payload = {
            "type": event_type,
            "task_id": self.task_id,
            "agent_name": agent_name,
            "task": task,
            "timestamp": _now_ms(),
            "result": asdict(result) if result else None,
            "metadata": metadata or {},
        }
        with self.trace_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


class ParallelScheduler:
    def __init__(
        self,
        workspace: Path,
        store: MultiAgentStore,
        *,
        max_parallel: int = 3,
        enable_thread_parallel: bool = False,
    ) -> None:
        self.workspace = workspace.resolve()
        self.store = store
        self.max_parallel = max(1, max_parallel)
        self.enable_thread_parallel = enable_thread_parallel
        self.semaphore = asyncio.Semaphore(self.max_parallel)

    async def run(
        self,
        workflow: AgentWorkflow,
        state: SharedState,
        *,
        execution_workspace: Path | None = None,
    ) -> dict[str, AgentResult]:
        workspace = (execution_workspace or self.workspace).resolve()
        results: dict[str, AgentResult] = {}
        node_status: dict[str, str] = {node.id: "pending" for node in workflow.nodes}
        pending = {node.id: node for node in workflow.nodes}
        self.store.write_node_status(node_status)

        while pending:
            ready = [
                node
                for node in pending.values()
                if all(dep in results for dep in node.depends_on)
            ]
            if not ready:
                blocked = {node_id: node.depends_on for node_id, node in pending.items()}
                raise RuntimeError(f"Workflow has circular dependency or blocked nodes: {blocked}")

            runnable = self._select_runnable_batch(ready, workflow)
            group = runnable[0].parallel_group or runnable[0].mode
            self.store.append_trace(
                event_type="parallel_group_started",
                agent_name="ParallelScheduler",
                task=workflow.user_request,
                metadata={"group": group, "nodes": [node.id for node in runnable]},
            )
            if self.enable_thread_parallel:
                batch_results = await asyncio.gather(
                    *[self._run_node(node, workflow, state, workspace) for node in runnable],
                    return_exceptions=True,
                )
            else:
                # 默认采用“并行批次、顺序执行”的安全模式，避免多个子 Agent 同时争用 git/subprocess。
                batch_results = []
                for node in runnable:
                    try:
                        batch_results.append(await self._run_node(node, workflow, state, workspace))
                    except Exception as exc:  # pragma: no cover - defensive fallback
                        batch_results.append(exc)

            for node, raw_result in zip(runnable, batch_results):
                if isinstance(raw_result, Exception):
                    result = AgentResult(
                        agent_name=node.agent_name,
                        status="failed",
                        summary=f"Node {node.id} failed: {raw_result}",
                        evidence=[repr(raw_result)],
                        risk_level=node.risk_level,
                    )
                else:
                    result = raw_result
                results[node.id] = result
                pending.pop(node.id)
                node_status[node.id] = result.status
                _apply_result_to_state(state, result)
                self.store.write_state(state)
                self.store.write_node_status(node_status)
                self.store.append_trace(
                    event_type="node_finished" if result.status != "failed" else "node_failed",
                    agent_name=node.agent_name,
                    task=node.task,
                    result=result,
                    metadata={"node_id": node.id, "mode": node.mode, "workspace_mode": node.workspace_mode},
                )
            self.store.append_trace(
                event_type="parallel_group_finished",
                agent_name="ParallelScheduler",
                task=workflow.user_request,
                metadata={"group": group, "nodes": [node.id for node in runnable]},
            )
        return results

    def _select_runnable_batch(self, ready: list[AgentTask], workflow: AgentWorkflow) -> list[AgentTask]:
        read_only = [node for node in ready if node.mode == "read_only"]
        if read_only:
            return read_only[: workflow.max_parallel]

        write_nodes = [node for node in ready if node.mode == "write"]
        if write_nodes:
            worktree_writes = [node for node in write_nodes if node.workspace_mode == "worktree"]
            if worktree_writes and workflow.allow_worktree:
                return worktree_writes[: workflow.max_write_parallel]
            return write_nodes[:1]

        return ready[: workflow.max_parallel]

    async def _run_node(
        self,
        node: AgentTask,
        workflow: AgentWorkflow,
        state: SharedState,
        workspace: Path,
    ) -> AgentResult:
        self.store.append_trace(
            event_type="node_started",
            agent_name=node.agent_name,
            task=node.task,
            metadata={"node_id": node.id, "depends_on": node.depends_on, "mode": node.mode},
        )
        context = _context_for_node(state, node)
        if not self.enable_thread_parallel:
            return run_subagent(
                workspace,
                agent_name=node.agent_name,
                task=node.task,
                context=context,
                command=node.command,
            )
        async with self.semaphore:
            return await asyncio.to_thread(
                run_subagent,
                workspace,
                agent_name=node.agent_name,
                task=node.task,
                context=context,
                command=node.command,
            )


AGENT_CARDS: dict[str, AgentCard] = {
    "CoordinatorAgent": AgentCard(
        name="CoordinatorAgent",
        description="中心调度器，负责拆解任务、分配子 Agent、汇总结果。",
        role="coordinator",
        risk_level="medium",
        auto_invoke=True,
        allowed_tools=["run_subagent"],
        forbidden=["direct_file_write"],
    ),
    "RepoExplorerAgent": AgentCard(
        name="RepoExplorerAgent",
        description="只读分析项目结构、相关文件和可能修改点。",
        role="reader",
        risk_level="low",
        auto_invoke=True,
        allowed_tools=["find", "grep", "read", "ls"],
        forbidden=["write", "edit", "commit", "push"],
    ),
    "PlannerAgent": AgentCard(
        name="PlannerAgent",
        description="根据用户需求和探索结果生成最小实现计划。",
        role="planner",
        risk_level="low",
        auto_invoke=True,
        allowed_tools=[],
        requires=["repo_summary"],
    ),
    "CodeEditorAgent": AgentCard(
        name="CodeEditorAgent",
        description="根据计划给出最小改动建议；V1 不直接写文件。",
        role="writer",
        risk_level="medium",
        auto_invoke=True,
        allowed_tools=["read", "grep"],
        requires=["implementation_plan"],
        forbidden=["commit", "push", "delete_file"],
    ),
    "TestRunnerAgent": AgentCard(
        name="TestRunnerAgent",
        description="运行安全白名单内的验证命令并提取结果。",
        role="tester",
        risk_level="medium",
        auto_invoke=True,
        allowed_tools=["bash", "read"],
        forbidden=["rm", "reset", "commit", "push"],
    ),
    "CodeReviewAgent": AgentCard(
        name="CodeReviewAgent",
        description="只读审查当前 diff，识别风险和测试缺口。",
        role="reviewer",
        risk_level="low",
        auto_invoke=True,
        allowed_tools=["git diff", "read", "grep"],
        forbidden=["write", "edit", "commit", "push"],
    ),
    "DependencyScanAgent": AgentCard(
        name="DependencyScanAgent",
        description="只读扫描依赖与打包配置，识别依赖风险和安装入口。",
        role="dependency_scanner",
        risk_level="low",
        auto_invoke=True,
        allowed_tools=["read", "find", "grep"],
        forbidden=["install", "write", "commit", "push"],
    ),
    "SecurityReviewAgent": AgentCard(
        name="SecurityReviewAgent",
        description="只读扫描常见安全风险，例如危险命令、密钥文件和越界路径访问。",
        role="security_reviewer",
        risk_level="low",
        auto_invoke=True,
        allowed_tools=["read", "grep", "find"],
        forbidden=["write", "edit", "commit", "push"],
    ),
}


def create_multi_agent_tools(workspace_dir: str | Path) -> list[AgentTool]:
    workspace = Path(workspace_dir).resolve()

    async def run_subagent_tool(tool_call_id: str, params: dict[str, Any], signal=None, on_update=None) -> AgentToolResult:
        _ = tool_call_id, signal, on_update
        agent_name = str(params.get("agent_name", "")).strip()
        task = str(params.get("task", "")).strip()
        context = str(params.get("context", "")).strip()
        command = str(params.get("command", "")).strip()
        task_id = str(params.get("task_id", "")).strip() or _new_task_id()
        result = run_subagent(workspace, agent_name=agent_name, task=task, context=context, command=command)
        state = _state_from_result(user_request=task, task_id=task_id, result=result)
        store = MultiAgentStore(workspace, task_id)
        store.write_state(state)
        store.append_trace(event_type="subagent_result", agent_name=agent_name, task=task, result=result)
        payload = asdict(result)
        payload["task_id"] = task_id
        payload["trace_path"] = str(store.trace_file.relative_to(workspace))
        payload["shared_state_path"] = str(store.state_file.relative_to(workspace))
        return AgentToolResult(
            content=[TextContent(text=json.dumps(payload, ensure_ascii=False, indent=2))],
            details=payload,
        )

    async def run_agent_team_tool(tool_call_id: str, params: dict[str, Any], signal=None, on_update=None) -> AgentToolResult:
        _ = tool_call_id, signal, on_update
        task = str(params.get("task", "")).strip()
        test_command = str(params.get("test_command", "")).strip()
        mode = str(params.get("mode", "team")).strip() or "team"
        base_session_id = str(params.get("base_session_id", "")).strip()
        base_entry_id = str(params.get("base_entry_id", "")).strip()
        max_fix_iterations = int(params.get("max_fix_iterations", 0) or 0)
        result = await arun_agent_team(
            workspace,
            task=task,
            test_command=test_command,
            mode=_normalize_mode(mode),
            base_session_id=base_session_id or None,
            base_entry_id=base_entry_id or None,
            max_fix_iterations=max(0, min(max_fix_iterations, 2)),
        )
        payload = asdict(result)
        return AgentToolResult(
            content=[TextContent(text=json.dumps(payload, ensure_ascii=False, indent=2))],
            details=payload,
        )

    return [
        AgentTool(
            name="run_subagent",
            label="Run Specialist Agent",
            description=(
                "运行一个受控的专用子 Agent，并返回结构化 AgentResult。"
                "适合复杂任务中的代码探索、计划、测试和 diff review；子 Agent 不会彼此通信。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "子 Agent 名称：RepoExplorerAgent/PlannerAgent/CodeEditorAgent/TestRunnerAgent/CodeReviewAgent",
                    },
                    "task": {"type": "string", "description": "分配给子 Agent 的具体子任务"},
                    "context": {"type": "string", "description": "必要的上游摘要或约束，不要塞完整长日志"},
                    "command": {
                        "type": "string",
                        "description": "仅 TestRunnerAgent 使用的验证命令，必须命中安全白名单",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "可选，多 Agent 任务 id；不传则自动创建",
                    },
                },
                "required": ["agent_name", "task"],
                "additionalProperties": False,
            },
            execute=run_subagent_tool,
        ),
        AgentTool(
            name="run_agent_team",
            label="Run Agent Team",
            description=(
                "运行中心化 Agent Team 固定流程：RepoExplorer -> Planner -> TestRunner -> CodeReview，"
                "并持久化 shared_state 与 trace。适合需要探索、验证和审查的复杂任务。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "用户原始任务或复杂子任务"},
                    "test_command": {
                        "type": "string",
                        "description": "可选验证命令，例如 python -m compileall -q src；必须命中安全白名单",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["team", "fork", "worktree"],
                        "description": "协作模式：team 当前工作区串行协作；fork 记录会话分叉计划；worktree 创建隔离 git worktree",
                    },
                    "base_session_id": {
                        "type": "string",
                        "description": "fork 模式可选：父会话 id，用于 trace 记录",
                    },
                    "base_entry_id": {
                        "type": "string",
                        "description": "fork 模式可选：父会话节点 id，用于 trace 记录",
                    },
                    "max_fix_iterations": {
                        "type": "number",
                        "description": "预留字段，最大自动修复轮数，V1 仅记录不自动写代码，最大 2",
                    },
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            execute=run_agent_team_tool,
        ),
    ]


def run_agent_team(
    workspace: Path,
    *,
    task: str,
    test_command: str = "",
    mode: CollaborationMode = "team",
    base_session_id: str | None = None,
    base_entry_id: str | None = None,
    max_fix_iterations: int = 0,
) -> AgentResult:
    return asyncio.run(
        arun_agent_team(
            workspace,
            task=task,
            test_command=test_command,
            mode=mode,
            base_session_id=base_session_id,
            base_entry_id=base_entry_id,
            max_fix_iterations=max_fix_iterations,
        )
    )


async def arun_agent_team(
    workspace: Path,
    *,
    task: str,
    test_command: str = "",
    mode: CollaborationMode = "team",
    base_session_id: str | None = None,
    base_entry_id: str | None = None,
    max_fix_iterations: int = 0,
) -> AgentResult:
    workspace = workspace.resolve()
    task_id = _new_task_id()
    store = MultiAgentStore(workspace, task_id)
    state = SharedState(user_request=task, task_id=task_id)
    workflow = plan_agent_workflow(
        task_id=task_id,
        user_request=task,
        test_command=test_command,
        mode=mode,
        max_fix_iterations=max_fix_iterations,
    )
    return await execute_agent_workflow(
        workspace,
        workflow=workflow,
        state=state,
        store=store,
        mode=mode,
        base_session_id=base_session_id,
        base_entry_id=base_entry_id,
    )


def plan_agent_workflow(
    *,
    task_id: str,
    user_request: str,
    test_command: str = "",
    mode: CollaborationMode = "team",
    max_fix_iterations: int = 0,
) -> AgentWorkflow:
    nodes = [
        AgentTask(
            id="explore",
            agent_name="RepoExplorerAgent",
            task=f"分析与任务相关的项目结构和文件：{user_request}",
            mode="read_only",
            parallel_group="read_only_scan",
            output_keys=["repo_summary", "relevant_files"],
        ),
        AgentTask(
            id="dependency_scan",
            agent_name="DependencyScanAgent",
            task=f"扫描依赖配置并识别依赖风险：{user_request}",
            mode="read_only",
            parallel_group="read_only_scan",
            output_keys=["dependency_files"],
        ),
        AgentTask(
            id="security_scan",
            agent_name="SecurityReviewAgent",
            task=f"扫描安全风险和危险操作模式：{user_request}",
            mode="read_only",
            parallel_group="read_only_scan",
            output_keys=["risks"],
        ),
        AgentTask(
            id="plan",
            agent_name="PlannerAgent",
            task=f"基于探索、依赖和安全扫描结果生成执行计划：{user_request}",
            depends_on=["explore", "dependency_scan", "security_scan"],
            mode="read_only",
            required_context_keys=["repo_summary", "relevant_files", "risks"],
            output_keys=["implementation_plan"],
        ),
    ]
    if test_command or _looks_like_validation_task(user_request):
        nodes.append(
            AgentTask(
                id="verify",
                agent_name="TestRunnerAgent",
                task=f"运行相关验证：{user_request}",
                depends_on=["plan"],
                mode="verify",
                command=test_command,
                output_keys=["test_result"],
                risk_level="medium",
            )
        )
    review_deps = ["plan"] + (["verify"] if any(node.id == "verify" for node in nodes) else [])
    nodes.append(
        AgentTask(
            id="review",
            agent_name="CodeReviewAgent",
            task=f"审查当前 diff 和多 Agent 执行结果：{user_request}",
            depends_on=review_deps,
            mode="review",
            output_keys=["review_result", "risks"],
        )
    )
    return AgentWorkflow(
        task_id=task_id,
        user_request=user_request,
        workflow_type=f"agent_team:{mode}",
        nodes=nodes,
        max_parallel=3,
        max_write_parallel=2,
        max_fix_iterations=max_fix_iterations,
        allow_write=False,
        allow_worktree=mode == "worktree",
        allow_auto_merge=False,
    )


async def execute_agent_workflow(
    workspace: Path,
    *,
    workflow: AgentWorkflow,
    state: SharedState,
    store: MultiAgentStore,
    mode: CollaborationMode,
    base_session_id: str | None = None,
    base_entry_id: str | None = None,
) -> AgentResult:
    workspace = workspace.resolve()
    store.write_state(state)
    store.write_workflow(workflow)
    store.append_trace(
        event_type="workflow_planned",
        agent_name="WorkflowPlanner",
        task=workflow.user_request,
        metadata={"workflow": asdict(workflow)},
    )
    store.append_trace(
        event_type="team_start",
        agent_name="CoordinatorAgent",
        task=workflow.user_request,
        metadata={
            "mode": mode,
            "max_fix_iterations": workflow.max_fix_iterations,
            "base_session_id": base_session_id,
            "base_entry_id": base_entry_id,
        },
    )

    execution_workspace = workspace
    mode_artifacts: dict[str, Any] = {"mode": mode}
    if mode == "fork":
        mode_artifacts.update(
            _record_fork_plan(
                store,
                task=workflow.user_request,
                base_session_id=base_session_id,
                base_entry_id=base_entry_id,
            )
        )
    elif mode == "worktree":
        setup = _setup_worktree(workspace, workflow.task_id)
        mode_artifacts.update(setup)
        store.append_trace(
            event_type="worktree_setup",
            agent_name="CoordinatorAgent",
            task=workflow.user_request,
            metadata=setup,
        )
        if setup.get("status") == "success" and setup.get("worktree_path"):
            execution_workspace = Path(str(setup["worktree_path"])).resolve()

    scheduler = ParallelScheduler(workspace, store, max_parallel=workflow.max_parallel)
    results_by_id = await scheduler.run(workflow, state, execution_workspace=execution_workspace)
    results = list(results_by_id.values())
    state.final_summary = _summarize_team_results(results)
    store.write_state(state)

    final_status: AgentStatus = "success"
    if any(result.status == "failed" for result in results):
        final_status = "failed"
    elif any(result.status in {"partial", "need_more_context"} for result in results):
        final_status = "partial"
    final = AgentResult(
        agent_name="CoordinatorAgent",
        status=final_status,
        summary=state.final_summary or "Agent Team finished.",
        evidence=[f"{result.agent_name}: {result.summary}" for result in results],
        changed_files=state.changed_files,
        commands_run=state.test_commands,
        artifacts={
            "task_id": workflow.task_id,
            "workflow_path": str(store.workflow_file.relative_to(workspace)),
            "node_status_path": str(store.node_status_file.relative_to(workspace)),
            "trace_path": str(store.trace_file.relative_to(workspace)),
            "shared_state_path": str(store.state_file.relative_to(workspace)),
            **mode_artifacts,
            "results": [asdict(result) for result in results],
            "risks": state.risks,
        },
        next_actions=_team_next_actions(results),
        risk_level="medium" if state.risks else "low",
    )
    store.append_trace(event_type="workflow_finished", agent_name="CoordinatorAgent", task=workflow.user_request, result=final)
    store.append_trace(event_type="team_end", agent_name="CoordinatorAgent", task=workflow.user_request, result=final)
    return final


def run_subagent(
    workspace: Path,
    *,
    agent_name: str,
    task: str,
    context: str = "",
    command: str = "",
) -> AgentResult:
    workspace = workspace.resolve()
    if agent_name not in AGENT_CARDS:
        return AgentResult(
            agent_name=agent_name or "(missing)",
            status="failed",
            summary=f"Unknown agent: {agent_name}",
            evidence=[f"Available agents: {', '.join(AGENT_CARDS)}"],
            risk_level="low",
        )
    if agent_name == "RepoExplorerAgent":
        return _repo_explorer(workspace, task)
    if agent_name == "PlannerAgent":
        return _planner(task, context)
    if agent_name == "CodeEditorAgent":
        return _code_editor(task, context)
    if agent_name == "TestRunnerAgent":
        return _test_runner(workspace, task, command)
    if agent_name == "CodeReviewAgent":
        return _code_reviewer(workspace, task)
    if agent_name == "DependencyScanAgent":
        return _dependency_scanner(workspace, task)
    if agent_name == "SecurityReviewAgent":
        return _security_reviewer(workspace, task)
    return AgentResult(
        agent_name=agent_name,
        status="need_more_context",
        summary="This agent is registered but has no V1 executor yet.",
        next_actions=["Use one of the V1 specialist agents."],
        risk_level=AGENT_CARDS[agent_name].risk_level,
    )


def _repo_explorer(workspace: Path, task: str) -> AgentResult:
    terms = _query_terms(task)
    files = _list_repo_files(workspace)
    relevant: list[str] = []
    evidence: list[str] = []
    for file_path in files:
        score = _score_path(file_path, terms)
        if score > 0:
            relevant.append(file_path)
    if terms:
        grep_lines = _safe_rg(workspace, terms[:4])
        evidence.extend(grep_lines[:20])
        for line in grep_lines:
            path = line.split(":", 1)[0]
            if path and path not in relevant:
                relevant.append(path)
    relevant = relevant[:20]
    summary = "识别到可能相关文件。" if relevant else "未从文件名或内容中识别到明确相关文件。"
    return AgentResult(
        agent_name="RepoExplorerAgent",
        status="success" if relevant else "partial",
        summary=summary,
        evidence=evidence[:20],
        artifacts={"relevant_files": relevant, "query_terms": terms},
        next_actions=["Read the most relevant files before editing."] if relevant else ["Ask for a narrower target or inspect project tree."],
        risk_level="low",
    )


def _planner(task: str, context: str) -> AgentResult:
    steps = [
        "先读取相关文件，确认现有实现和约束。",
        "制定最小改动方案，避免无关重构。",
        "由主 Agent 使用 edit/write 执行修改。",
        "运行相关验证命令。",
        "审查 diff 并总结风险。",
    ]
    if "test" in task.lower() or "测试" in task:
        steps.insert(3, "优先定位已有测试入口，再决定是否新增最小测试。")
    summary = "生成了中心化执行计划。"
    if context:
        summary += " 已参考上游上下文摘要。"
    return AgentResult(
        agent_name="PlannerAgent",
        status="success",
        summary=summary,
        evidence=[f"{index + 1}. {step}" for index, step in enumerate(steps)],
        artifacts={"implementation_plan": steps, "context_preview": context[:800]},
        next_actions=["Coordinator should decide whether to edit directly or ask for clarification."],
        risk_level="low",
    )


def _code_editor(task: str, context: str) -> AgentResult:
    return AgentResult(
        agent_name="CodeEditorAgent",
        status="partial",
        summary="V1 CodeEditorAgent 不直接写文件，避免多 Agent 并发写入冲突；请由主 Agent 根据计划调用 edit/write。",
        evidence=[
            "写操作保持串行。",
            "同一文件修改前必须先 read 定位上下文。",
            "修改后交给 TestRunnerAgent 和 CodeReviewAgent 验证。",
        ],
        artifacts={"task": task, "context_preview": context[:1000]},
        next_actions=["Main agent should perform the minimal edit/write operation.", "Then run TestRunnerAgent."],
        risk_level="medium",
    )


def _test_runner(workspace: Path, task: str, command: str) -> AgentResult:
    command = command.strip() or _guess_test_command(task)
    if not command:
        return AgentResult(
            agent_name="TestRunnerAgent",
            status="need_more_context",
            summary="没有识别到安全验证命令。",
            next_actions=["Provide a whitelisted command such as `python -m compileall -q src` or `python -m pytest -q`."],
            risk_level="medium",
        )
    if not _is_safe_test_command(command):
        return AgentResult(
            agent_name="TestRunnerAgent",
            status="failed",
            summary="验证命令未通过安全白名单。",
            evidence=[f"Blocked command: {command}"],
            next_actions=["Use compileall, pytest, or unittest validation commands."],
            risk_level="medium",
        )
    completed = subprocess.run(
        command,
        cwd=workspace,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    error = completed.stderr.decode("utf-8", errors="replace")
    merged = "\n".join(part for part in [output.strip(), error.strip()] if part)
    status: AgentStatus = "success" if completed.returncode == 0 else "failed"
    summary = "验证通过。" if status == "success" else f"验证失败，exit_code={completed.returncode}。"
    return AgentResult(
        agent_name="TestRunnerAgent",
        status=status,
        summary=summary,
        evidence=[_clip(merged, 2000)] if merged else ["(no output)"],
        commands_run=[command],
        artifacts={"exit_code": completed.returncode},
        next_actions=[] if status == "success" else ["Inspect failure output and run a focused fix iteration."],
        risk_level="medium",
    )


def _code_reviewer(workspace: Path, task: str) -> AgentResult:
    diff_stat = _run_git(workspace, ["git", "diff", "--stat"])
    diff_name = _run_git(workspace, ["git", "diff", "--name-only"])
    changed_files = [line.strip() for line in diff_name.splitlines() if line.strip()]
    if not changed_files:
        return AgentResult(
            agent_name="CodeReviewAgent",
            status="success",
            summary="当前没有未提交 diff 可审查。",
            evidence=["git diff --name-only returned empty"],
            risk_level="low",
        )
    evidence = [diff_stat.strip() or "(no diff stat)"]
    risks = _basic_diff_risks(changed_files, task)
    return AgentResult(
        agent_name="CodeReviewAgent",
        status="success",
        summary=f"审查到 {len(changed_files)} 个变更文件。",
        evidence=evidence,
        changed_files=changed_files,
        artifacts={"risks": risks},
        next_actions=["Run focused tests before commit.", "Check high-risk files manually."] if risks else ["Run validation before final response."],
        risk_level="low" if not risks else "medium",
    )


def _dependency_scanner(workspace: Path, task: str) -> AgentResult:
    _ = task
    candidates = [
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "setup.cfg",
        "package.json",
        "pnpm-lock.yaml",
        "package-lock.json",
    ]
    found: list[str] = []
    evidence: list[str] = []
    for rel in candidates:
        path = workspace / rel
        if not path.exists():
            continue
        found.append(rel)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines()[:80]:
            clean = line.strip()
            if clean and any(marker in clean.lower() for marker in ("dependencies", "pytest", "httpx", "sentence", "torch")):
                evidence.append(f"{rel}: {clean}")
    summary = "识别到依赖配置文件。" if found else "未识别到常见依赖配置文件。"
    risks = []
    if any("torch" in item.lower() for item in evidence):
        risks.append("Large ML dependency detected; verify install size and runtime compatibility.")
    return AgentResult(
        agent_name="DependencyScanAgent",
        status="success" if found else "partial",
        summary=summary,
        evidence=evidence[:20],
        artifacts={"dependency_files": found, "risks": risks},
        next_actions=["Check optional dependencies before installing new packages."] if found else [],
        risk_level="low" if not risks else "medium",
    )


def _security_reviewer(workspace: Path, task: str) -> AgentResult:
    _ = task
    patterns = ["rm -rf", "git reset --hard", "subprocess.run", "shell=True", "Path escapes workspace", "API_KEY"]
    evidence: list[str] = []
    regexes = [(pattern, re.compile(re.escape(pattern), flags=re.I)) for pattern in patterns]
    for rel_path in _list_repo_files(workspace)[:400]:
        if not rel_path.endswith((".py", ".md", ".toml", ".txt", ".yaml", ".yml", ".json", ".sh")):
            continue
        path = workspace / rel_path
        try:
            if path.stat().st_size > 200_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern, regex in regexes:
                if regex.search(line):
                    evidence.append(f"{rel_path}:{line_no}: {line.strip()[:180]}")
                    break
            if len(evidence) >= 30:
                break
        if len(evidence) >= 30:
            break
    risks: list[str] = []
    if any("rm -rf" in line or "git reset --hard" in line for line in evidence):
        risks.append("Dangerous shell command pattern appears in repository.")
    if any("shell=True" in line for line in evidence):
        risks.append("shell=True usage found; verify command sanitization and allowlists.")
    summary = "完成只读安全风险扫描。"
    return AgentResult(
        agent_name="SecurityReviewAgent",
        status="success",
        summary=summary,
        evidence=evidence[:30],
        artifacts={"risks": risks, "patterns": patterns},
        next_actions=["Review highlighted risky patterns before enabling write/merge automation."] if risks else [],
        risk_level="low" if not risks else "medium",
    )


def _query_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,8}", text)
    stop = {"这个", "那个", "一下", "帮我", "现在", "当前", "进行", "实现", "优化"}
    terms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        clean = item.lower().strip()
        if not clean or clean in stop or clean in seen:
            continue
        seen.add(clean)
        terms.append(clean)
    return terms[:12]


def _list_repo_files(workspace: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            return [line.strip() for line in completed.stdout.decode().splitlines() if line.strip()]
    except Exception:
        pass
    return [str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file() and ".git" not in path.parts][:500]


def _score_path(path: str, terms: list[str]) -> int:
    lower = path.lower()
    return sum(1 for term in terms if term in lower)


def _safe_rg(workspace: Path, terms: list[str]) -> list[str]:
    if not terms:
        return []
    pattern = "|".join(re.escape(term) for term in terms[:4])
    try:
        completed = subprocess.run(
            ["git", "grep", "-n", "-E", pattern, "--", "."],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if completed.returncode in {0, 1}:
            return completed.stdout.decode("utf-8", errors="replace").splitlines()
    except Exception:
        pass
    try:
        completed = subprocess.run(
            ["rg", "-n", "--glob", "!*.pyc", "--glob", "!*.jsonl", pattern, "."],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return completed.stdout.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def _guess_test_command(task: str) -> str:
    text = task.lower()
    if "compile" in text or "编译" in task:
        return "python -m compileall -q src"
    if "pytest" in text or "测试" in task or "test" in text:
        return "python -m pytest -q"
    return ""


def _is_safe_test_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    normalized = " ".join(parts)
    allowed_prefixes = [
        "python -m compileall",
        "python3 -m compileall",
        "python -m pytest",
        "python3 -m pytest",
        "pytest",
        "python -m unittest",
        "python3 -m unittest",
    ]
    if any(normalized.startswith(prefix) for prefix in allowed_prefixes):
        return not any(token in normalized for token in [";", "&&", "||", "|", ">", "<", "`", "$("])
    return False


def _run_git(workspace: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            args,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        return completed.stdout.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"git command failed: {exc}"


def _basic_diff_risks(changed_files: list[str], task: str) -> list[str]:
    risks: list[str] = []
    if any(path.endswith((".toml", ".lock", "requirements.txt")) for path in changed_files):
        risks.append("Dependency or packaging files changed; verify install path.")
    if any("test" not in path.lower() for path in changed_files) and "test" not in task.lower():
        risks.append("Production files changed without an explicit test request.")
    if any(path.startswith(".codeclaw/") for path in changed_files):
        risks.append("Runtime/config files changed; check whether they should be committed.")
    return risks


def _normalize_mode(value: str) -> CollaborationMode:
    if value in {"fork", "worktree"}:
        return value  # type: ignore[return-value]
    return "team"


def _record_fork_plan(
    store: MultiAgentStore,
    *,
    task: str,
    base_session_id: str | None,
    base_entry_id: str | None,
) -> dict[str, Any]:
    fork_session_id = f"fork_{store.task_id}"
    payload = {
        "status": "planned",
        "fork_session_id": fork_session_id,
        "base_session_id": base_session_id,
        "base_entry_id": base_entry_id,
        "note": (
            "Fork mode records a logical session fork plan for Coordinator trace. "
            "Use AgentSession.fork_session/fork_from_entry to materialize a conversational branch."
        ),
    }
    store.append_trace(
        event_type="fork_plan",
        agent_name="CoordinatorAgent",
        task=task,
        metadata=payload,
    )
    return {"fork": payload}


def _setup_worktree(workspace: Path, task_id: str) -> dict[str, Any]:
    if not _is_git_repo(workspace):
        return {"status": "skipped", "reason": "workspace is not a git repository"}
    if _has_uncommitted_changes(workspace):
        return {
            "status": "skipped",
            "reason": "workspace has uncommitted changes; refusing to create worktree from a dirty base",
        }
    worktree_root = workspace / ".codeclaw" / "worktrees"
    worktree_path = worktree_root / task_id
    branch_name = f"codeclaw/{task_id}"
    worktree_root.mkdir(parents=True, exist_ok=True)
    command = ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        return {"status": "failed", "reason": str(exc), "command": " ".join(command)}
    out = completed.stdout.decode("utf-8", errors="replace").strip()
    err = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        return {
            "status": "failed",
            "reason": err or out or f"git worktree exited {completed.returncode}",
            "command": " ".join(command),
            "exit_code": completed.returncode,
        }
    return {
        "status": "success",
        "worktree_path": str(worktree_path),
        "branch": branch_name,
        "command": " ".join(command),
        "stdout": out,
    }


def _is_git_repo(workspace: Path) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.decode().strip() == "true"


def _has_uncommitted_changes(workspace: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return bool(completed.stdout.decode("utf-8", errors="replace").strip())


def _new_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"ma_{stamp}_{uuid.uuid4().hex[:8]}"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _state_from_result(*, user_request: str, task_id: str, result: AgentResult) -> SharedState:
    state = SharedState(user_request=user_request, task_id=task_id)
    _apply_result_to_state(state, result)
    state.final_summary = result.summary
    return state


def _apply_result_to_state(state: SharedState, result: AgentResult) -> None:
    if result.agent_name == "RepoExplorerAgent":
        state.repo_summary = result.summary
        relevant = result.artifacts.get("relevant_files") if isinstance(result.artifacts, dict) else None
        if isinstance(relevant, list):
            state.relevant_files = [str(item) for item in relevant]
    elif result.agent_name == "PlannerAgent":
        plan = result.artifacts.get("implementation_plan") if isinstance(result.artifacts, dict) else None
        if isinstance(plan, list):
            state.implementation_plan = "\n".join(f"- {item}" for item in plan)
        else:
            state.implementation_plan = result.summary
    elif result.agent_name == "TestRunnerAgent":
        state.test_result = result.summary
        for command in result.commands_run:
            if command not in state.test_commands:
                state.test_commands.append(command)
    elif result.agent_name == "CodeReviewAgent":
        state.review_result = result.summary
        for file_path in result.changed_files:
            if file_path not in state.changed_files:
                state.changed_files.append(file_path)
        risks = result.artifacts.get("risks") if isinstance(result.artifacts, dict) else None
        if isinstance(risks, list):
            for risk in risks:
                text = str(risk)
                if text not in state.risks:
                    state.risks.append(text)
    else:
        for file_path in result.changed_files:
            if file_path not in state.changed_files:
                state.changed_files.append(file_path)


def _context_for_node(state: SharedState, node: AgentTask) -> str:
    payload = {
        "task_id": state.task_id,
        "required_context_keys": node.required_context_keys,
        "repo_summary": state.repo_summary,
        "relevant_files": state.relevant_files[:20],
        "implementation_plan": state.implementation_plan,
        "changed_files": state.changed_files,
        "test_commands": state.test_commands,
        "test_result": state.test_result,
        "review_result": state.review_result,
        "risks": state.risks,
    }
    if not node.required_context_keys:
        return json.dumps(payload, ensure_ascii=False)[:1600]
    selected = {key: payload.get(key) for key in node.required_context_keys if key in payload}
    selected["task_id"] = state.task_id
    return json.dumps(selected, ensure_ascii=False)[:1600]


def _looks_like_validation_task(task: str) -> bool:
    lower = task.lower()
    return any(marker in lower for marker in ("test", "pytest", "compile", "验证", "测试", "编译"))


def _summarize_team_results(results: list[AgentResult]) -> str:
    parts = [f"{result.agent_name}={result.status}" for result in results]
    if any(result.status == "failed" for result in results):
        return "Agent Team completed with failures: " + ", ".join(parts)
    if any(result.status in {"partial", "need_more_context"} for result in results):
        return "Agent Team completed partially: " + ", ".join(parts)
    return "Agent Team completed successfully: " + ", ".join(parts)


def _team_next_actions(results: list[AgentResult]) -> list[str]:
    actions: list[str] = []
    for result in results:
        for action in result.next_actions:
            if action not in actions:
                actions.append(action)
    return actions[:8]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 24].rstrip() + "\n...<truncated>..."
