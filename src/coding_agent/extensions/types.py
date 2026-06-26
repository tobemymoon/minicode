from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from agent_core import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
)

BeforeHook = Callable[[BeforeToolCallContext, Any | None], BeforeToolCallResult | None | Awaitable[BeforeToolCallResult | None]]
AfterHook = Callable[[AfterToolCallContext, Any | None], AfterToolCallResult | None | Awaitable[AfterToolCallResult | None]]


@dataclass
class ExtensionLifecycleContext:
    session: Any
    text: str
    is_continue: bool
    message_count: int


LifecycleHook = Callable[[ExtensionLifecycleContext], None | Awaitable[None]]


@dataclass
class ExtensionCommandContext:
    name: str
    args: list[str]
    raw_text: str
    session: Any
    message: Any


CommandHandler = Callable[[ExtensionCommandContext], str | None | Awaitable[str | None]]
RiskLevel = Literal["low", "medium", "high"]


@dataclass
class RegisteredCommand:
    name: str
    handler: CommandHandler
    description: str | None = None
    source: Literal["extension", "skill", "builtin", "prompt"] = "extension"


@dataclass
class SkillSpec:
    name: str
    command_name: str
    description: str
    content: str
    source_path: str
    tags: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    negative_triggers: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    risk_level: RiskLevel = "low"
    auto_invoke: bool = True
    pre_skills: list[str] = field(default_factory=list)
    post_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False


@dataclass
class SkillRouteCandidate:
    name: str
    score: float
    risk_level: RiskLevel
    auto_invoke: bool
    reason: str


@dataclass
class SkillRouteResult:
    primary_skill: str | None
    secondary_skills: list[str]
    confidence: float
    risk_level: RiskLevel
    auto_execute: bool
    reason: str
    candidates: list[SkillRouteCandidate] = field(default_factory=list)


@dataclass
class LoadedExtensions:
    tools: list[AgentTool] = field(default_factory=list)
    before_tool_hooks: list[BeforeHook] = field(default_factory=list)
    after_tool_hooks: list[AfterHook] = field(default_factory=list)
    prompt_guidelines: list[str] = field(default_factory=list)
    append_prompts: list[str] = field(default_factory=list)
    commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    before_prompt_hooks: list[LifecycleHook] = field(default_factory=list)
    after_prompt_hooks: list[LifecycleHook] = field(default_factory=list)
    skills: list[SkillSpec] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    loaded_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
