from __future__ import annotations

"""
coding_agent 对外类型定义。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

from ai.models import get_model
from ai.types import Message, Model
from agent_core import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentMessage,
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    PromptCacheMode,
    ToolExecutionMode,
)
from .extensions.types import LifecycleHook, RegisteredCommand, SkillSpec

ConvertToLlmFn = Callable[[list[AgentMessage]], list[Message] | Awaitable[list[Message]]]
InstallApprovalFn = Callable[[str], bool | Awaitable[bool]]
SecurityApprovalFn = Callable[[dict[str, Any]], bool | Awaitable[bool]]


@dataclass
class AgentSessionOptions:
    """
    AgentSession 初始化参数。
    """

    model: Model
    workspace_dir: str | Path
    storage_backend: Literal["jsonl"] = "jsonl"
    system_prompt: str = ""
    tools: list[AgentTool] = field(default_factory=list)
    session_id: Optional[str] = None
    messages: list[AgentMessage] = field(default_factory=list)
    thinking_level: str = "off"
    tool_execution: ToolExecutionMode = "parallel"
    prompt_cache: Optional[PromptCacheMode] = None
    prompt_cache_ttl: Optional[Literal["5m", "1h"]] = None
    convert_to_llm: Optional[ConvertToLlmFn] = None
    max_context_messages: Optional[int] = None
    max_context_tokens: Optional[int] = None
    retain_recent_messages: int = 24
    summary_builder: Optional[Callable[[list[Message]], str]] = None
    auto_memory: bool = True
    llm_memory_reflection: bool = True
    max_memory_reflection_items: int = 3
    memory_prompt_limit: int = 12
    memory_injection_char_budget: int = 1600
    retry_enabled: bool = True
    max_retries: int = 2
    retry_base_delay_ms: int = 1200
    max_tool_iterations: int = 8
    read_only_mode: bool = False
    block_dangerous_bash: bool = True
    bash_allow_patterns: Optional[list[str]] = None
    bash_block_patterns: Optional[list[str]] = None
    edit_require_unique_match: bool = True
    prompt_guidelines: Optional[list[str]] = None
    append_system_prompt: Optional[str] = None
    tool_snippets: Optional[dict[str, str]] = None
    extension_paths: Optional[list[str]] = None
    skill_paths: Optional[list[str]] = None
    prompt_debug_sources: bool = False
    mcp_servers: Optional[list[dict[str, Any]]] = None
    mcp_client: Any | None = None
    extension_commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    skills: list[SkillSpec] = field(default_factory=list)
    max_routed_skills: int = 1
    skill_injection_char_budget: int = 3000
    skill_embedding_recall: bool = True
    skill_embedding_backend: Literal["auto", "local", "bge-m3", "off"] = "auto"
    skill_embedding_model_path: Optional[str] = "/data4/slx/models/bge-m3"
    skill_llm_rerank: bool = True
    skill_llm_rerank_min_confidence: float = 0.7
    install_approval_callback: Optional[InstallApprovalFn] = None
    security_approval_callback: Optional[SecurityApprovalFn] = None
    before_prompt_hooks: list[LifecycleHook] = field(default_factory=list)
    after_prompt_hooks: list[LifecycleHook] = field(default_factory=list)
    before_tool_call: Optional[
        Callable[[BeforeToolCallContext, Any | None], BeforeToolCallResult | None | Awaitable[BeforeToolCallResult | None]]
    ] = None
    after_tool_call: Optional[
        Callable[[AfterToolCallContext, Any | None], AfterToolCallResult | None | Awaitable[AfterToolCallResult | None]]
    ] = None


@dataclass
class CreateAgentSessionOptions:
    """
    更友好的会话创建参数：

    你可以二选一提供模型信息：
    1) 直接传 model；
    2) 传 provider + model_id（由工厂自动解析）。

    若传入已有 session_id，工厂会优先尝试从会话元数据恢复
    provider/model_id/system_prompt。
    """

    workspace_dir: str | Path
    storage_backend: Literal["jsonl"] = "jsonl"
    model: Optional[Model] = None
    provider: Optional[str] = None
    model_id: Optional[str] = None
    system_prompt: str = ""
    tools: list[AgentTool] = field(default_factory=list)
    session_id: Optional[str] = None
    messages: list[AgentMessage] = field(default_factory=list)
    thinking_level: str = "off"
    tool_execution: ToolExecutionMode = "parallel"
    prompt_cache: PromptCacheMode = "off"
    prompt_cache_ttl: Optional[Literal["5m", "1h"]] = None
    load_workspace_resources: bool = True
    enabled_builtin_tools: Optional[list[str]] = None
    max_context_messages: Optional[int] = None
    max_context_tokens: Optional[int] = None
    retain_recent_messages: int = 24
    summary_builder: Optional[Callable[[list[Message]], str]] = None
    auto_memory: bool = True
    llm_memory_reflection: bool = True
    max_memory_reflection_items: int = 3
    memory_prompt_limit: int = 12
    memory_injection_char_budget: int = 1600
    retry_enabled: bool = True
    max_retries: int = 2
    retry_base_delay_ms: int = 1200
    max_tool_iterations: int = 8
    read_only_mode: bool = False
    block_dangerous_bash: bool = True
    bash_allow_patterns: Optional[list[str]] = None
    bash_block_patterns: Optional[list[str]] = None
    edit_require_unique_match: bool = True
    prompt_guidelines: Optional[list[str]] = None
    append_system_prompt: Optional[str] = None
    tool_snippets: Optional[dict[str, str]] = None
    extension_paths: Optional[list[str]] = None
    skill_paths: Optional[list[str]] = None
    prompt_debug_sources: bool = False
    mcp_servers: Optional[list[dict[str, Any]]] = None
    mcp_client: Any | None = None
    extension_commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    skills: list[SkillSpec] = field(default_factory=list)
    max_routed_skills: int = 1
    skill_injection_char_budget: int = 3000
    skill_embedding_recall: bool = True
    skill_embedding_backend: Literal["auto", "local", "bge-m3", "off"] = "auto"
    skill_embedding_model_path: Optional[str] = "/data4/slx/models/bge-m3"
    skill_llm_rerank: bool = True
    skill_llm_rerank_min_confidence: float = 0.7
    install_approval_callback: Optional[InstallApprovalFn] = None
    security_approval_callback: Optional[SecurityApprovalFn] = None
    before_prompt_hooks: list[LifecycleHook] = field(default_factory=list)
    after_prompt_hooks: list[LifecycleHook] = field(default_factory=list)
    before_tool_call: Optional[
        Callable[[BeforeToolCallContext, Any | None], BeforeToolCallResult | None | Awaitable[BeforeToolCallResult | None]]
    ] = None
    after_tool_call: Optional[
        Callable[[AfterToolCallContext, Any | None], AfterToolCallResult | None | Awaitable[AfterToolCallResult | None]]
    ] = None

    def resolve_model(self) -> Model:
        if self.model is not None:
            return self.model
        if self.provider and self.model_id:
            return get_model(self.provider, self.model_id)
        raise ValueError("Model is required: provide model or provider+model_id")


RunMode = Literal["print", "interactive", "rpc"]
OutputFn = Callable[[str], None]
InputFn = Callable[[str], str]
