from __future__ import annotations

"""
AgentSession：面向应用层的会话编排对象。

职责：
1) 管理会话存储目录；
2) 把 agent_core 事件/消息写入持久层；
3) 提供稳定的 prompt / continue 调用入口；
4) 上下文溢出检测与 LLM 驱动压缩。
"""

from pathlib import Path
import asyncio
import inspect
import logging
import json
import re
import shlex
import uuid
from typing import Awaitable, Callable

from ai.overflow import estimate_context_tokens, is_context_overflow
from ai.stream import complete_simple
from ai.types import (
    AssistantMessage,
    Context,
    Message,
    SimpleStreamOptions,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from agent_core import Agent, AgentEvent, AgentMessage, AgentOptions
from agent_core.types import BeforeToolCallContext, BeforeToolCallResult

from .artifacts import ArtifactStore, ContextCompressor, ToolResultCompactor
from .extensions.types import ExtensionLifecycleContext, RiskLevel, SkillRouteCandidate, SkillRouteResult
from .memory import LLMMemoryReflector, MemoryReflector, MemoryStore
from .session_store import SessionStore, new_session_id
from .skill_embeddings import SkillEmbeddingRetriever
from .types import AgentSessionOptions

logger = logging.getLogger("codeclaw.coding_agent.session")

_COMPACTION_SYSTEM_PROMPT = """你是一个上下文压缩助手。请根据以下对话历史生成一份简明摘要。
要求：
1. 保留所有关键事实、决策和结论
2. 保留重要的文件路径、代码片段和技术细节
3. 保留用户的偏好和约束条件
4. 移除重复和冗余信息
5. 用简洁的要点形式输出
6. 使用中文"""

_SKILL_RERANK_SYSTEM_PROMPT = """你是 CodeClaw 的 Skill Router。
你的任务是根据用户请求和候选 Skill 摘要，选择最合适的 Skill。
只输出合法 JSON，不要输出 Markdown。
如果没有合适 Skill，primary_skill 输出 null。"""


class AgentSession:
    def __init__(self, options: AgentSessionOptions) -> None:
        workspace_dir = Path(options.workspace_dir)
        self.workspace_dir = workspace_dir
        self.session_id = options.session_id or new_session_id()
        self.storage_backend = options.storage_backend

        self.store = self._create_store(self.session_id)
        self.store.ensure_initialized(
            model_id=options.model.id,
            provider=options.model.provider,
            system_prompt=options.system_prompt,
        )
        self.artifact_store = ArtifactStore(self.workspace_dir, self.session_id)
        self.artifact_store.ensure_initialized()
        self.context_compressor = ContextCompressor(ToolResultCompactor(self.artifact_store))
        self.memory_store = MemoryStore(self.workspace_dir)
        self.memory_store.ensure_initialized()
        self.memory_reflector = MemoryReflector(self.memory_store)
        self.llm_memory_reflector = LLMMemoryReflector(self.memory_store)
        self._logged_context_artifacts: set[str] = set()
        self._active_memory_context: UserMessage | None = None

        persisted_messages = self.store.load_session_messages()
        if not persisted_messages:
            persisted_messages = self.store.load_context_messages()
        merged_messages = [*persisted_messages, *options.messages]

        agent_opts = AgentOptions(
            model=options.model,
            system_prompt=options.system_prompt,
            tools=options.tools,
            messages=merged_messages,
            thinking_level=options.thinking_level,
            tool_execution=options.tool_execution,
            prompt_cache=options.prompt_cache,
            prompt_cache_ttl=options.prompt_cache_ttl,
            transform_context=self._transform_context_for_llm,
            before_tool_call=options.before_tool_call,
            after_tool_call=options.after_tool_call,
            session_id=self.session_id,
            max_tool_iterations=options.max_tool_iterations,
        )
        if options.convert_to_llm is not None:
            agent_opts.convert_to_llm = options.convert_to_llm
        self.agent = Agent(agent_opts)
        self.max_context_messages = options.max_context_messages
        self.max_context_tokens = options.max_context_tokens
        self.retain_recent_messages = options.retain_recent_messages
        self.summary_builder = options.summary_builder
        self.auto_memory = options.auto_memory
        self.llm_memory_reflection = options.llm_memory_reflection
        self.max_memory_reflection_items = options.max_memory_reflection_items
        self.memory_prompt_limit = options.memory_prompt_limit
        self.memory_retrieval_limit = min(max(1, options.memory_prompt_limit), 5)
        self.memory_injection_char_budget = options.memory_injection_char_budget
        self.skills = list(options.skills)
        self.max_routed_skills = options.max_routed_skills
        self.skill_injection_char_budget = options.skill_injection_char_budget
        self.skill_embedding_recall = options.skill_embedding_recall
        self.skill_embedding_backend = options.skill_embedding_backend
        self.skill_embedding_model_path = options.skill_embedding_model_path
        self.skill_embedding_retriever = SkillEmbeddingRetriever(
            self.skills,
            backend=self.skill_embedding_backend,
            model_path=self.skill_embedding_model_path,
        )
        self.skill_llm_rerank = options.skill_llm_rerank
        self.skill_llm_rerank_min_confidence = options.skill_llm_rerank_min_confidence
        self.install_approval_callback = options.install_approval_callback
        self.tool_execution = options.tool_execution
        self.prompt_cache = options.prompt_cache
        self.prompt_cache_ttl = options.prompt_cache_ttl
        self.retry_enabled = options.retry_enabled
        self.max_retries = options.max_retries
        self.retry_base_delay_ms = options.retry_base_delay_ms
        self.max_tool_iterations = options.max_tool_iterations
        self.prompt_debug_sources = options.prompt_debug_sources
        self.mcp_servers = options.mcp_servers
        self.mcp_client = options.mcp_client
        self.extension_commands = dict(options.extension_commands)
        self.before_prompt_hooks = list(options.before_prompt_hooks)
        self.after_prompt_hooks = list(options.after_prompt_hooks)
        self._external_before_tool_call = options.before_tool_call
        self.before_tool_call = options.before_tool_call
        self.after_tool_call = options.after_tool_call
        self._unsubscribe = self.agent.subscribe(self._on_agent_event)
        self._install_artifact_read_guard()
        self._active_skill_context: UserMessage | None = None

    @property
    def messages(self) -> list[AgentMessage]:
        return self.agent.state.messages

    @property
    def last_usage(self) -> dict | None:
        """返回最近一次 AssistantMessage 的 usage 信息。"""
        for msg in reversed(self.agent.state.messages):
            if isinstance(msg, AssistantMessage):
                u = msg.usage
                return {
                    "input_tokens": u.input,
                    "output_tokens": u.output,
                    "total_tokens": u.total_tokens,
                    "cache_read": u.cache_read,
                    "cache_write": u.cache_write,
                    "cost": {
                        "input": u.cost.input,
                        "output": u.cost.output,
                        "cache_read": u.cost.cache_read,
                        "cache_write": u.cost.cache_write,
                        "total": u.cost.total,
                    },
                }
        return None

    @property
    def cumulative_usage(self) -> dict:
        """统计整个会话的累积 token 使用和成本。"""
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_tokens = 0
        total_cost = 0.0
        for msg in self.agent.state.messages:
            if isinstance(msg, AssistantMessage):
                total_input += msg.usage.input
                total_output += msg.usage.output
                total_cache_read += msg.usage.cache_read
                total_cache_write += msg.usage.cache_write
                total_tokens += msg.usage.total_tokens
                total_cost += msg.usage.cost.total
        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read": total_cache_read,
            "cache_write": total_cache_write,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
        }

    async def prompt(self, text: str, *, images: list[str] | None = None) -> list[AgentMessage]:
        await self._run_lifecycle_hooks(text=text, is_continue=False, hooks=self.before_prompt_hooks)
        await self._check_and_compact_before_prompt()
        await self._prepare_skill_context(text)
        self._prepare_memory_context(text)
        before_count = len(self.agent.state.messages)
        try:
            result = await self._run_with_retry(lambda: self.agent.prompt(text, images=images))
            await self._reflect_memory_from_new_messages(before_count)
            await self._compact_context_if_needed()
            await self._run_lifecycle_hooks(text=text, is_continue=False, hooks=self.after_prompt_hooks)
            return result
        finally:
            self._active_skill_context = None
            self._active_memory_context = None

    async def prompt_message(self, message: UserMessage) -> list[AgentMessage]:
        await self._check_and_compact_before_prompt()
        text = _full_text_from_user(message)
        await self._prepare_skill_context(text)
        self._prepare_memory_context(text)
        before_count = len(self.agent.state.messages)
        try:
            result = await self._run_with_retry(lambda: self.agent.prompt(message))
            await self._reflect_memory_from_new_messages(before_count)
            await self._compact_context_if_needed()
            return result
        finally:
            self._active_skill_context = None
            self._active_memory_context = None

    async def continue_run(self) -> list[AgentMessage]:
        await self._run_lifecycle_hooks(text="", is_continue=True, hooks=self.before_prompt_hooks)
        before_count = len(self.agent.state.messages)
        result = await self._run_with_retry(self.agent.continue_run)
        await self._reflect_memory_from_new_messages(before_count)
        await self._compact_context_if_needed()
        await self._run_lifecycle_hooks(text="", is_continue=True, hooks=self.after_prompt_hooks)
        return result

    def subscribe(self, listener: Callable[[AgentEvent], None]) -> Callable[[], None]:
        return self.agent.subscribe(listener)

    def close(self) -> None:
        self._unsubscribe()

    def list_entry_ids(self) -> list[str]:
        return self.store.list_entry_ids()

    def list_memories(self, *, kind: str | None = None, limit: int = 50) -> list[dict]:
        return [record.to_dict() for record in self.memory_store.list(kind=kind, limit=limit)]

    def search_memories(self, query: str, *, limit: int = 10) -> list[dict]:
        return [record.to_dict() for record in self.memory_store.search(query, limit=limit)]

    def reflect_memories(self) -> list[dict]:
        records = self.memory_reflector.reflect(list(self.agent.state.messages), session_id=self.session_id)
        self.store.append_event(
            {
                "type": "memory_reflection",
                "sessionId": self.session_id,
                "created": len(records),
                "memory_ids": [record.id for record in records],
                "source": "manual",
            }
        )
        return [record.to_dict() for record in records]

    def list_skills(self) -> list[dict]:
        return [
            {
                "name": skill.name,
                "command_name": skill.command_name,
                "description": skill.description,
                "tags": list(skill.tags),
                "triggers": list(skill.triggers),
                "examples": list(skill.examples),
                "disable_model_invocation": skill.disable_model_invocation,
                "source_path": skill.source_path,
            }
            for skill in self.skills
        ]

    def list_entries(self) -> list[dict]:
        return self.store.list_entries()

    def get_leaf_id(self) -> str | None:
        return self.store.get_leaf_id()

    def get_entry_path(self, entry_id: str) -> list[str]:
        return self.store.get_entry_path(entry_id)

    def get_session_tree(self) -> list[dict]:
        return self.store.get_session_tree()

    def fork_session(self, from_entry_id: str | None = None) -> "AgentSession":
        new_id = new_session_id()
        fork_store = self.store.fork_to(new_id, from_entry_id=from_entry_id)
        meta = fork_store.read_meta() or {}
        model = self.agent.state.model
        system_prompt = str(meta.get("system_prompt", self.agent.state.system_prompt))
        return AgentSession(
            AgentSessionOptions(
                model=model,
                workspace_dir=self.workspace_dir,
                storage_backend=self.storage_backend,
                system_prompt=system_prompt,
                tools=list(self.agent.state.tools),
                session_id=new_id,
                thinking_level=self.agent.state.thinking_level,
                tool_execution=self.tool_execution,
                prompt_cache=self.prompt_cache,
                prompt_cache_ttl=self.prompt_cache_ttl,
                max_context_messages=self.max_context_messages,
                max_context_tokens=self.max_context_tokens,
                retain_recent_messages=self.retain_recent_messages,
                summary_builder=self.summary_builder,
                auto_memory=self.auto_memory,
                llm_memory_reflection=self.llm_memory_reflection,
                max_memory_reflection_items=self.max_memory_reflection_items,
                memory_prompt_limit=self.memory_prompt_limit,
                memory_injection_char_budget=self.memory_injection_char_budget,
                skills=self.skills,
                max_routed_skills=self.max_routed_skills,
                skill_injection_char_budget=self.skill_injection_char_budget,
                skill_embedding_recall=self.skill_embedding_recall,
                skill_llm_rerank=self.skill_llm_rerank,
                skill_llm_rerank_min_confidence=self.skill_llm_rerank_min_confidence,
                install_approval_callback=self.install_approval_callback,
                retry_enabled=self.retry_enabled,
                max_retries=self.max_retries,
                retry_base_delay_ms=self.retry_base_delay_ms,
                max_tool_iterations=self.max_tool_iterations,
                prompt_debug_sources=self.prompt_debug_sources,
                mcp_servers=self.mcp_servers,
                mcp_client=self.mcp_client,
                extension_commands=self.extension_commands,
                before_prompt_hooks=self.before_prompt_hooks,
                after_prompt_hooks=self.after_prompt_hooks,
                before_tool_call=self._external_before_tool_call,
                after_tool_call=self.after_tool_call,
            )
        )

    def fork_from_entry(self, entry_id: str) -> "AgentSession":
        return self.fork_session(from_entry_id=entry_id)

    def switch_to_entry(self, entry_id: str) -> None:
        self.store.set_leaf(entry_id)
        restored = self.store.load_session_messages(leaf_id=entry_id)
        self.agent.set_messages(restored)
        self.store.append_event(
            {
                "type": "session_switch_entry",
                "session_id": self.session_id,
                "entry_id": entry_id,
            }
        )

    def switch_session(self, session_id: str) -> None:
        new_store = self._create_store(session_id)
        meta = new_store.read_meta()
        if not meta:
            raise ValueError(f"Session not found: {session_id}")

        self.session_id = session_id
        self.store = new_store
        self.artifact_store = ArtifactStore(self.workspace_dir, self.session_id)
        self.artifact_store.ensure_initialized()
        self.context_compressor = ContextCompressor(ToolResultCompactor(self.artifact_store))
        self.memory_store = MemoryStore(self.workspace_dir)
        self.memory_store.ensure_initialized()
        self.memory_reflector = MemoryReflector(self.memory_store)
        self.llm_memory_reflector = LLMMemoryReflector(self.memory_store)
        self._logged_context_artifacts = set()
        self._active_memory_context = None
        self._active_skill_context = None
        restored = new_store.load_session_messages()
        if not restored:
            restored = new_store.load_context_messages()
        self.agent.set_messages(restored)

    def _create_store(self, session_id: str):
        return SessionStore(self.workspace_dir, session_id)

    async def _reflect_memory_from_new_messages(self, start_index: int) -> None:
        if not self.auto_memory:
            return
        new_messages = list(self.agent.state.messages[start_index:])
        if not new_messages:
            return
        records = []
        source = "rules"
        error_message = None
        if self.llm_memory_reflection:
            try:
                records = await self.llm_memory_reflector.reflect(
                    model=self.agent.state.model,
                    messages=new_messages,
                    session_id=self.session_id,
                    max_items=self.max_memory_reflection_items,
                )
                source = "llm"
            except Exception as exc:
                error_message = str(exc)
                records = []
                source = "rules_fallback"
        rule_records = self.memory_reflector.reflect(new_messages, session_id=self.session_id)
        if rule_records:
            seen = {record.id for record in records}
            records.extend(record for record in rule_records if record.id not in seen)
            if source == "llm":
                source = "llm_with_rules"
        if not records:
            if source == "llm":
                source = "rules_empty_fallback"
        if not records:
            self.store.append_event(
                {
                    "type": "memory_reflection",
                    "sessionId": self.session_id,
                    "created": 0,
                    "memory_ids": [],
                    "source": source,
                    "error_message": error_message,
                }
            )
            return
        self.store.append_event(
            {
                "type": "memory_reflection",
                "sessionId": self.session_id,
                "created": len(records),
                "memory_ids": [record.id for record in records],
                "source": source,
                "error_message": error_message,
            }
        )

    async def _prepare_skill_context(self, query: str) -> None:
        query = query.strip()
        self._active_skill_context = None
        if not query or not self.skills or self.max_routed_skills <= 0:
            return

        route = await self._route_skill_result(query)
        if not route.primary_skill:
            self.store.append_event(
                {
                    "type": "skill_routed",
                    "sessionId": self.session_id,
                    "query": query[:240],
                    "candidates": [candidate.__dict__ for candidate in route.candidates],
                    "primary_skill": None,
                    "secondary_skills": [],
                    "confidence": route.confidence,
                    "risk_level": route.risk_level,
                    "auto_execute": route.auto_execute,
                    "reason": route.reason,
                    "injected_count": 0,
                    "user_corrected": None,
                    "success": None,
                }
            )
            return

        routed_skill_names = [route.primary_skill, *route.secondary_skills]
        routed_skills = [skill for name in routed_skill_names for skill in self.skills if skill.name == name]
        lines, injected_names = self._format_skill_context(routed_skills)
        if not injected_names:
            return
        self._active_skill_context = UserMessage(content=[TextContent(text="\n".join(lines))])
        self.store.append_event(
            {
                "type": "skill_routed",
                "sessionId": self.session_id,
                "query": query[:240],
                "candidates": [candidate.__dict__ for candidate in route.candidates],
                "primary_skill": route.primary_skill,
                "secondary_skills": route.secondary_skills,
                "confidence": route.confidence,
                "risk_level": route.risk_level,
                "auto_execute": route.auto_execute,
                "reason": route.reason,
                "injected_count": len(injected_names),
                "char_budget": self.skill_injection_char_budget,
                "user_corrected": None,
                "success": None,
            }
        )

    def _route_skills(self, query: str) -> list:
        route = self._route_skill_result_sync(query)
        by_name = {skill.name: skill for skill in self.skills}
        return [
            (candidate.score, by_name[candidate.name])
            for candidate in route.candidates[: self.max_routed_skills]
            if candidate.name in by_name
        ]

    async def _route_skill_result(self, query: str) -> SkillRouteResult:
        route = self._route_skill_result_sync(query)
        if not self._should_llm_rerank(route):
            return route
        return await self._llm_rerank_skill_route(query, route)

    def _route_skill_result_sync(self, query: str) -> SkillRouteResult:
        query_terms = set(_skill_terms(query))
        if not query_terms:
            return SkillRouteResult(
                primary_skill=None,
                secondary_skills=[],
                confidence=0.0,
                risk_level="low",
                auto_execute=False,
                reason="No meaningful query terms were found.",
            )
        keyword_candidates = self._keyword_recall(query, query_terms)
        embedding_candidates = self._embedding_recall(query) if self.skill_embedding_recall else []
        candidates = [
            candidate
            for candidate in _merge_skill_candidates(keyword_candidates, embedding_candidates)
            if candidate.score >= 2.0
        ]
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            return SkillRouteResult(
                primary_skill=None,
                secondary_skills=[],
                confidence=0.0,
                risk_level="low",
                auto_execute=False,
                reason="No skill candidate passed routing threshold.",
                candidates=[],
            )

        primary = candidates[0]
        secondary = [candidate.name for candidate in candidates[1 : max(1, self.max_routed_skills)]]
        confidence = _route_confidence(primary.score, candidates[1].score if len(candidates) > 1 else 0.0)
        return SkillRouteResult(
            primary_skill=primary.name,
            secondary_skills=secondary,
            confidence=confidence,
            risk_level=primary.risk_level,
            auto_execute=primary.auto_invoke and primary.risk_level != "high",
            reason=primary.reason,
            candidates=candidates[:5],
        )

    def _keyword_recall(self, query: str, query_terms: set[str]) -> list[SkillRouteCandidate]:
        query_lower = query.lower()
        candidates: list[SkillRouteCandidate] = []
        for skill in self.skills:
            if skill.disable_model_invocation:
                continue
            score = 0.0
            reasons: list[str] = []
            trigger_terms = [item.lower() for item in skill.triggers if item.strip()]
            negative_terms = [item.lower() for item in skill.negative_triggers if item.strip()]
            required_terms = [item.lower() for item in skill.requires if item.strip()]
            tag_terms = [item.lower() for item in skill.tags if item.strip()]
            example_terms = [item.lower() for item in skill.examples if item.strip()]
            meta_haystack = " ".join(
                [skill.name, skill.description, " ".join(skill.tags), " ".join(skill.triggers), " ".join(skill.examples)]
            ).lower()
            content_haystack = skill.content[:2000].lower()

            if any(_skill_phrase_matches(term, query_lower) for term in negative_terms):
                continue
            if required_terms and not all(_skill_phrase_matches(term, query_lower) for term in required_terms):
                continue

            for trigger in trigger_terms:
                if _skill_phrase_matches(trigger, query_lower) or query_lower in trigger:
                    score += 6
                    reasons.append(f"matched trigger `{trigger}`")
            for example in example_terms:
                if _skill_phrase_matches(example, query_lower) or query_lower in example:
                    score += 3
                    reasons.append(f"matched example `{example}`")
            for tag in tag_terms:
                if _skill_term_matches(tag, query_lower):
                    score += 3
                    reasons.append(f"matched tag `{tag}`")
            for term in query_terms:
                if _skill_term_matches(term, meta_haystack):
                    score += 2
                    reasons.append(f"matched metadata term `{term}`")
                elif _skill_term_matches(term, content_haystack):
                    score += 0.5
            if skill.command_name.lower() in query.lower() or skill.name.lower() in query.lower():
                score += 8
                reasons.append("matched skill name or command")
            if score >= 2:
                candidates.append(
                    SkillRouteCandidate(
                        name=skill.name,
                        score=round(score, 3),
                        risk_level=skill.risk_level,
                        auto_invoke=skill.auto_invoke,
                        reason="; ".join(reasons[:4]) or "matched weak content terms",
                    )
                )
        return candidates

    def _embedding_recall(self, query: str) -> list[SkillRouteCandidate]:
        return self.skill_embedding_retriever.recall(query)

    def _should_llm_rerank(self, route: SkillRouteResult) -> bool:
        if not self.skill_llm_rerank or not route.candidates or route.primary_skill is None:
            return False
        if len(route.candidates) < 2 and route.confidence >= self.skill_llm_rerank_min_confidence:
            return False
        return route.confidence < self.skill_llm_rerank_min_confidence or len(route.candidates) > 1

    async def _llm_rerank_skill_route(self, query: str, route: SkillRouteResult) -> SkillRouteResult:
        by_name = {skill.name: skill for skill in self.skills}
        candidates = [candidate for candidate in route.candidates if candidate.name in by_name][:5]
        if not candidates:
            return route
        prompt = _build_skill_rerank_prompt(query, candidates, by_name)
        try:
            response = await complete_simple(
                self.agent.state.model,
                Context(
                    system_prompt=_SKILL_RERANK_SYSTEM_PROMPT,
                    messages=[UserMessage(content=[TextContent(text=prompt)])],
                ),
                SimpleStreamOptions(max_tokens=500),
            )
        except Exception as exc:
            route.reason = f"{route.reason}; llm_rerank_failed={exc}"
            return route

        raw = _full_text_from_assistant(response).strip()
        payload = _parse_json_object(raw)
        selected = str(payload.get("primary_skill", "")).strip()
        if selected.lower() in {"", "none", "null"}:
            route.primary_skill = None
            route.secondary_skills = []
            route.confidence = 0.0
            route.auto_execute = False
            route.reason = str(payload.get("reason") or "LLM rerank selected no skill.")[:240]
            return route
        if selected not in by_name:
            route.reason = f"{route.reason}; llm_rerank_invalid_selection={selected}"
            return route

        primary = next((candidate for candidate in candidates if candidate.name == selected), None)
        if primary is None:
            skill = by_name[selected]
            primary = SkillRouteCandidate(
                name=skill.name,
                score=route.candidates[0].score,
                risk_level=skill.risk_level,
                auto_invoke=skill.auto_invoke,
                reason="selected by llm_rerank",
            )
        confidence = float(payload.get("confidence", route.confidence) or route.confidence)
        confidence = max(0.0, min(0.99, confidence))
        skill = by_name[selected]
        route.primary_skill = selected
        route.secondary_skills = [
            name for name in payload.get("secondary_skills", []) if isinstance(name, str) and name in by_name and name != selected
        ][: max(0, self.max_routed_skills - 1)]
        route.confidence = round(confidence, 3)
        route.risk_level = skill.risk_level
        route.auto_execute = bool(payload.get("auto_execute", skill.auto_invoke)) and skill.risk_level != "high"
        route.reason = f"llm_rerank: {str(payload.get('reason') or primary.reason)[:220]}"
        return route

    def _format_skill_context(self, skills: list) -> tuple[list[str], list[str]]:
        lines = [
            "[Relevant Skill]",
            (
                "以下 Skill 与当前任务相关，仅注入命中的技能内容。"
                "请优先按 Skill 流程执行；若与当前用户要求冲突，以当前用户要求为准。"
            ),
        ]
        injected: list[str] = []
        for skill in skills:
            allowed = ", ".join(skill.allowed_tools) if skill.allowed_tools else "(not restricted)"
            pre = ", ".join(skill.pre_skills) if skill.pre_skills else "(none)"
            post = ", ".join(skill.post_skills) if skill.post_skills else "(none)"
            header = (
                f"\n## {skill.name}\n"
                f"Description: {skill.description}\n"
                f"Command: /{skill.command_name}\n"
                f"Risk: {skill.risk_level}\n"
                f"Auto execute: {skill.auto_invoke}\n"
                f"Allowed tools: {allowed}\n"
                f"Pre skills: {pre}\n"
                f"Post skills: {post}"
            )
            current_chars = sum(len(line) + 1 for line in lines)
            remaining = self.skill_injection_char_budget - current_chars - len(header) - 2
            if remaining < 200:
                break
            body = _clip_text(skill.content, remaining)
            block = f"{header}\n\n{body}"
            if current_chars + len(block) > self.skill_injection_char_budget:
                break
            lines.append(block)
            injected.append(skill.name)
        return lines, injected

    def _prepare_memory_context(self, query: str) -> None:
        query = query.strip()
        self._active_memory_context = None
        if not query or self.memory_retrieval_limit <= 0:
            return
        records = self._retrieve_relevant_memories(query)
        if not records:
            self.store.append_event(
                {
                    "type": "memory_retrieved",
                    "sessionId": self.session_id,
                    "query": query[:240],
                    "matched_ids": [],
                    "injected_count": 0,
                }
            )
            return

        lines, injected_ids = self._format_memory_context(records)
        if not injected_ids:
            self._active_memory_context = None
            self.store.append_event(
                {
                    "type": "memory_retrieved",
                    "sessionId": self.session_id,
                    "query": query[:240],
                    "matched_ids": [],
                    "injected_count": 0,
                    "char_budget": self.memory_injection_char_budget,
                }
            )
            return
        self._active_memory_context = UserMessage(content=[TextContent(text="\n".join(lines))])
        self.memory_store.mark_used(injected_ids)
        self.store.append_event(
            {
                "type": "memory_retrieved",
                "sessionId": self.session_id,
                "query": query[:240],
                "matched_ids": injected_ids,
                "injected_count": len(injected_ids),
                "kinds": [record.kind for record in records if record.id in set(injected_ids)],
                "char_budget": self.memory_injection_char_budget,
            }
        )

    def _format_memory_context(self, records: list) -> tuple[list[str], list[str]]:
        lines = [
            "[Relevant Long-Term Memory]",
            (
                "以下是与当前任务相关的长期记忆，仅供内部参考。"
                "优先参考；若与当前用户明确要求冲突，以当前要求为准。"
                "除非用户明确询问记忆系统，否则不要主动提到或复述这些长期记忆。"
            ),
        ]
        injected_ids: list[str] = []
        labels = {
            "preference": "User Preferences",
            "error_fix": "Relevant Error Fixes",
            "procedural": "Procedural Experience",
            "project_fact": "Project Facts",
        }
        for kind in ("preference", "error_fix", "procedural", "project_fact"):
            bucket = [record for record in records if record.kind == kind and record.id not in injected_ids]
            if not bucket:
                continue
            header = f"\n## {labels[kind]}"
            if self._memory_context_chars(lines) + len(header) > self.memory_injection_char_budget:
                break
            lines.append(header)
            for record in bucket:
                tags = f" tags={','.join(record.tags[:4])}" if record.tags else ""
                next_line = f"- {record.id} {record.content}{tags}"
                if self._memory_context_chars(lines) + len(next_line) > self.memory_injection_char_budget:
                    break
                lines.append(next_line)
                injected_ids.append(record.id)
        return lines, injected_ids

    @staticmethod
    def _memory_context_chars(lines: list[str]) -> int:
        return sum(len(line) + 1 for line in lines)

    def _retrieve_relevant_memories(self, query: str) -> list:
        records = self.memory_store.search(query, limit=self.memory_retrieval_limit)
        seen = {record.id for record in records}

        # User preferences are lightweight and broadly useful, so keep a few as fallback.
        for record in self.memory_store.list(kind="preference", limit=3):
            if record.id in seen:
                continue
            records.append(record)
            seen.add(record.id)
            if len(records) >= self.memory_retrieval_limit:
                break

        if any(marker in query.lower() for marker in ("error", "traceback", "exception", "报错", "失败", "400")):
            for record in self.memory_store.list(kind="error_fix", limit=5):
                if record.id in seen:
                    continue
                records.append(record)
                seen.add(record.id)
                if len(records) >= self.memory_retrieval_limit:
                    break

        return records[: self.memory_retrieval_limit]

    async def _transform_context_for_llm(
        self,
        messages: list[AgentMessage],
        signal: object | None,
    ) -> list[AgentMessage]:
        result = self.context_compressor.compress(messages)
        transformed_messages = list(result.messages)
        if self._active_skill_context is not None:
            transformed_messages = self._inject_memory_context(transformed_messages, self._active_skill_context)
        if self._active_memory_context is not None:
            transformed_messages = self._inject_memory_context(transformed_messages, self._active_memory_context)
        for record in result.records:
            if record.artifact_id in self._logged_context_artifacts:
                continue
            self._logged_context_artifacts.add(record.artifact_id)
            self.store.append_event(
                {
                    "type": "context_compression",
                    "sessionId": self.session_id,
                    "artifact_id": record.artifact_id,
                    "toolCallId": record.tool_call_id,
                    "toolName": record.tool_name,
                    "original_chars": record.original_chars,
                    "preview_chars": record.preview_chars,
                    "strategy": "tool_result_artifact",
                }
            )
        return transformed_messages  # type: ignore[return-value]

    @staticmethod
    def _inject_memory_context(messages: list[AgentMessage], memory_context: UserMessage) -> list[AgentMessage]:
        if not messages:
            return [memory_context]
        insert_at = len(messages)
        for idx in range(len(messages) - 1, -1, -1):
            if isinstance(messages[idx], UserMessage):
                insert_at = idx
                break
        return [*messages[:insert_at], memory_context, *messages[insert_at:]]

    def _install_artifact_read_guard(self) -> None:
        user_before = self._external_before_tool_call

        async def _guard(ctx: BeforeToolCallContext, signal: object | None) -> BeforeToolCallResult | None:
            if user_before:
                result = user_before(ctx, signal)
                if inspect.isawaitable(result):
                    result = await result
                if result and result.block:
                    return result

            if ctx.tool_call.name == "bash":
                command = str(ctx.args.get("command", "")).strip()
                if _is_dependency_install_command(command):
                    if self.install_approval_callback is None:
                        return BeforeToolCallResult(
                            block=True,
                            reason=(
                                "Dependency installation requires explicit user approval. "
                                f"Blocked command: {command}"
                            ),
                        )
                    approved = self.install_approval_callback(command)
                    if inspect.isawaitable(approved):
                        approved = await approved
                    if not approved:
                        return BeforeToolCallResult(
                            block=True,
                            reason=(
                                "User declined dependency installation. "
                                "Use currently installed packages or a standard-library fallback."
                            ),
                        )
                    return None

            if ctx.tool_call.name != "read_artifact":
                return None

            artifact_id = str(ctx.args.get("artifact_id", "")).strip()
            if not artifact_id:
                return None

            prior_reads = 0
            for message in ctx.context.messages:
                if not isinstance(message, ToolResultMessage) or message.tool_name != "read_artifact":
                    continue
                details = message.details if isinstance(message.details, dict) else {}
                if details.get("artifact_id") == artifact_id:
                    prior_reads += 1

            if prior_reads >= 2:
                return BeforeToolCallResult(
                    block=True,
                    reason=(
                        f"read_artifact for {artifact_id} was blocked after {prior_reads} prior reads. "
                        "Use the existing preview/chunks to answer, or ask the user for a narrower target."
                    ),
                )
            return None

        self.before_tool_call = _guard
        self.agent._options.before_tool_call = _guard

    async def _on_agent_event(self, event: AgentEvent) -> None:
        self.store.append_event(event)
        if event["type"] == "message_end":
            message = event["message"]
            self.store.append_context_message(message)

    async def _run_lifecycle_hooks(
        self,
        *,
        text: str,
        is_continue: bool,
        hooks: list,
    ) -> None:
        if not hooks:
            return
        ctx = ExtensionLifecycleContext(
            session=self,
            text=text,
            is_continue=is_continue,
            message_count=len(self.agent.state.messages),
        )
        for hook in hooks:
            value = hook(ctx)
            if inspect.isawaitable(value):
                await value

    async def _check_and_compact_before_prompt(self) -> None:
        """调用 LLM 前检查上下文是否溢出，如溢出则先压缩。"""
        model = self.agent.state.model
        ctx = Context(
            messages=self.agent.state.messages,
            system_prompt=self.agent.state.system_prompt,
            tools=self.agent.state.tools,
        )
        if is_context_overflow(model, ctx):
            logger.warning(
                "context overflow detected before prompt, triggering compaction session_id=%s",
                self.session_id,
            )
            await self._compact_context_if_needed(force=True)

    async def _compact_context_if_needed(self, *, force: bool = False) -> None:
        max_messages = self.max_context_messages
        max_tokens = self.max_context_tokens
        over_message_limit = bool(max_messages and max_messages > 0 and len(self.agent.state.messages) > max_messages)
        estimated_tokens = estimate_context_tokens(self.agent.state.messages, self.agent.state.system_prompt)
        over_token_limit = bool(max_tokens and max_tokens > 0 and estimated_tokens > max_tokens)

        if not force and not over_message_limit and not over_token_limit:
            return

        messages = list(self.agent.state.messages)
        retain = max(2, min(self.retain_recent_messages, len(messages) - 1))
        if len(messages) <= retain:
            return

        older, recent = self._split_for_context_compaction(messages, retain)
        if not older:
            return

        if self.summary_builder:
            summary_text = self.summary_builder(older).strip()
        else:
            summary_text = self._structured_summary(older)

        if not summary_text:
            summary_text = self._fallback_summary(older)

        quality = self._assess_summary_coverage(older, summary_text)
        if quality["missing_total"] > 0:
            patch_text = self._build_summary_coverage_patch(quality)
            if patch_text:
                summary_text = f"{summary_text}\n\n{patch_text}".strip()
                quality = self._assess_summary_coverage(older, summary_text)

        summary_id = f"sum_{uuid.uuid4().hex[:10]}"
        summary_message = UserMessage(
            content=[
                TextContent(
                    text=(
                        "[Context Summary]\n"
                        f"summary_id={summary_id}\n"
                        f"covered_messages={len(older)}\n"
                        f"retained_recent={len(recent)}\n"
                        f"estimated_tokens_before={estimated_tokens}\n\n"
                        f"{summary_text}"
                    )
                )
            ],
        )
        compacted = [summary_message, *recent]

        self.agent.set_messages(compacted)
        self.store.rewrite_context_messages(compacted)
        self.store.append_session_summary(
            {
                "type": "session_summary",
                "summary_id": summary_id,
                "session_id": self.session_id,
                "reason": "overflow" if force else ("token_threshold" if over_token_limit else "message_threshold"),
                "covered_message_count": len(older),
                "retained_recent": len(recent),
                "estimated_tokens_before": estimated_tokens,
                "quality": quality,
                "summary": summary_text,
            }
        )
        self.store.append_event(
            {
                "type": "context_compacted",
                "sessionId": self.session_id,
                "summary_id": summary_id,
                "before_count": len(messages),
                "after_count": len(compacted),
                "retained_recent": len(recent),
                "estimated_tokens_before": estimated_tokens,
                "summary_quality": quality,
                "reason": "overflow" if force else ("token_threshold" if over_token_limit else "message_threshold"),
            }
        )
        logger.info(
            "context compacted session_id=%s before=%d after=%d",
            self.session_id, len(messages), len(compacted),
        )

    @staticmethod
    def _split_for_context_compaction(messages: list[Message], retain: int) -> tuple[list[Message], list[Message]]:
        cut = max(1, len(messages) - retain)
        # Do not keep orphan ToolResult messages without the Assistant ToolCall that produced them.
        while cut > 0 and isinstance(messages[cut], ToolResultMessage):
            cut -= 1
        return messages[:cut], messages[cut:]

    @staticmethod
    def _structured_summary(messages: list[Message]) -> str:
        user_items: list[str] = []
        assistant_items: list[str] = []
        tool_items: list[str] = []
        tool_names: dict[str, int] = {}

        for msg in messages:
            if isinstance(msg, UserMessage):
                text = _extract_text_from_user(msg)
                if text:
                    user_items.append(text)
            elif isinstance(msg, AssistantMessage):
                text = _extract_text_from_assistant(msg)
                tool_calls = [block for block in msg.content if isinstance(block, ToolCall)]
                if text:
                    assistant_items.append(text)
                for call in tool_calls:
                    tool_names[call.name] = tool_names.get(call.name, 0) + 1
            elif isinstance(msg, ToolResultMessage):
                tool_names[msg.tool_name] = tool_names.get(msg.tool_name, 0) + 1
                text = _extract_text_from_tool_result(msg)
                if text:
                    tool_items.append(f"{msg.tool_name}: {text}")

        sections = [
            "## User Goals",
            *_format_summary_items(user_items[-8:]),
            "",
            "## Assistant Decisions",
            *_format_summary_items(assistant_items[-8:]),
            "",
            "## Tool Evidence",
            *_format_summary_items(tool_items[-8:]),
            "",
            "## Tool Activity",
        ]
        if tool_names:
            sections.extend(f"- {name}: {count}" for name, count in sorted(tool_names.items()))
        else:
            sections.append("- none")
        summary = "\n".join(sections).strip()
        if len(summary) > 4000:
            summary = summary[:4000].rstrip() + "\n...<structured summary truncated>..."
        return summary

    @staticmethod
    def _assess_summary_coverage(messages: list[Message], summary_text: str) -> dict[str, object]:
        entities = _extract_summary_entities(messages)
        summary_lower = summary_text.lower()
        missing: dict[str, list[str]] = {}
        total = 0
        covered = 0

        for key, values in entities.items():
            total += len(values)
            key_missing: list[str] = []
            for value in values:
                if value.lower() in summary_lower:
                    covered += 1
                else:
                    key_missing.append(value)
            missing[key] = key_missing

        score = 1.0 if total == 0 else covered / total
        missing_total = total - covered
        return {
            "score": round(score, 4),
            "covered": covered,
            "total": total,
            "missing_total": missing_total,
            "missing": missing,
        }

    @staticmethod
    def _build_summary_coverage_patch(quality: dict[str, object]) -> str:
        missing_raw = quality.get("missing")
        if not isinstance(missing_raw, dict):
            return ""

        labels = {
            "paths": "Paths",
            "symbols": "Symbols",
            "artifacts": "Artifacts",
            "constraints": "Constraints",
            "tools": "Tools",
        }
        lines = ["## Coverage Patch"]
        added = False
        for key in ("paths", "symbols", "artifacts", "constraints", "tools"):
            values = missing_raw.get(key)
            if not isinstance(values, list) or not values:
                continue
            clean_values = [str(item) for item in values[:12] if str(item).strip()]
            if not clean_values:
                continue
            lines.append(f"- {labels[key]}: {', '.join(clean_values)}")
            added = True
        return "\n".join(lines) if added else ""

    async def _llm_summary(self, messages: list[Message]) -> str:
        """用 LLM 生成上下文摘要。"""
        formatted = self._format_messages_for_summary(messages)
        if not formatted.strip():
            return ""

        try:
            summary_context = Context(
                messages=[UserMessage(content=f"请压缩以下对话历史为简明摘要：\n\n{formatted}")],
                system_prompt=_COMPACTION_SYSTEM_PROMPT,
            )
            model = self.agent.state.model
            result = await complete_simple(
                model,
                summary_context,
                SimpleStreamOptions(max_tokens=2000),
            )
            text_parts = [b.text for b in result.content if isinstance(b, TextContent)]
            summary = "\n".join(text_parts).strip()
            if summary:
                logger.info("LLM compaction summary generated chars=%d", len(summary))
                return summary
        except Exception as exc:
            logger.warning("LLM compaction failed, using fallback: %s", exc)

        return ""

    @staticmethod
    def _format_messages_for_summary(messages: list[Message]) -> str:
        lines: list[str] = []
        for msg in messages[-40:]:
            if isinstance(msg, UserMessage):
                text = _extract_text_from_user(msg)
                if text:
                    lines.append(f"User: {text}")
            elif isinstance(msg, AssistantMessage):
                text = _extract_text_from_assistant(msg)
                if text:
                    lines.append(f"Assistant: {text}")
            elif isinstance(msg, ToolResultMessage):
                text = _extract_text_from_tool_result(msg)
                if text:
                    lines.append(f"ToolResult({msg.tool_name}): {text}")
        return "\n".join(lines)

    @staticmethod
    def _fallback_summary(messages: list[Message]) -> str:
        lines: list[str] = []
        for msg in messages[-20:]:
            if isinstance(msg, UserMessage):
                text = _extract_text_from_user(msg)
                if text:
                    lines.append(f"- User: {text}")
            elif isinstance(msg, AssistantMessage):
                text = _extract_text_from_assistant(msg)
                if text:
                    lines.append(f"- Assistant: {text}")
            elif isinstance(msg, ToolResultMessage):
                text = _extract_text_from_tool_result(msg)
                if text:
                    lines.append(f"- ToolResult({msg.tool_name}): {text}")
        merged = "\n".join(lines).strip()
        if len(merged) > 3000:
            merged = merged[:3000] + "\n...<summary truncated>..."
        return merged

    async def _run_with_retry(self, op: Callable[[], Awaitable[list[AgentMessage]]]) -> list[AgentMessage]:
        attempts = self.max_retries + 1 if self.retry_enabled else 1
        last: list[AgentMessage] | None = None

        for attempt in range(attempts):
            messages = await op()
            last = messages

            final_assistant = next((m for m in reversed(self.agent.state.messages) if isinstance(m, AssistantMessage)), None)
            should_retry = self._should_retry(final_assistant)
            if not should_retry or attempt >= attempts - 1:
                return messages

            delay_ms = int(self.retry_base_delay_ms * (2**attempt))
            self.store.append_event(
                {
                    "type": "auto_retry_start",
                    "attempt": attempt + 1,
                    "max_attempts": attempts,
                    "delay_ms": delay_ms,
                    "error_message": final_assistant.error_message if final_assistant else "",
                }
            )
            await asyncio.sleep(delay_ms / 1000.0)

        return last or []

    @staticmethod
    def _should_retry(message: AssistantMessage | None) -> bool:
        if message is None:
            return False
        if message.stop_reason not in {"error", "aborted"}:
            return False
        error_text = (message.error_message or "").lower()
        if "invalid_api_key" in error_text or "authentication" in error_text or "unauthorized" in error_text:
            return False
        return True


def _extract_text_from_user(message: UserMessage) -> str:
    if isinstance(message.content, str):
        return message.content[:180]
    text = "".join(block.text for block in message.content if isinstance(block, TextContent))
    return text[:180]


def _format_summary_items(items: list[str]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item}" for item in items]


def _extract_text_from_assistant(message: AssistantMessage) -> str:
    text = "".join(block.text for block in message.content if isinstance(block, TextContent))
    return text[:180]


def _extract_text_from_tool_result(message: ToolResultMessage) -> str:
    text = "".join(block.text for block in message.content if isinstance(block, TextContent))
    return text[:180]


def _extract_summary_entities(messages: list[Message]) -> dict[str, list[str]]:
    raw_texts: list[str] = []
    user_texts: list[str] = []
    tool_names: set[str] = set()

    for message in messages:
        if isinstance(message, UserMessage):
            text = _full_text_from_user(message)
            if text:
                raw_texts.append(text)
                user_texts.append(text)
        elif isinstance(message, AssistantMessage):
            text = _full_text_from_assistant(message)
            if text:
                raw_texts.append(text)
            for block in message.content:
                if isinstance(block, ToolCall):
                    tool_names.add(block.name)
                    raw_texts.append(f"{block.name} {block.arguments}")
        elif isinstance(message, ToolResultMessage):
            tool_names.add(message.tool_name)
            text = _full_text_from_tool_result(message)
            if text:
                raw_texts.append(text)
            if isinstance(message.details, dict):
                raw_texts.append(str(message.details))

    combined = "\n".join(raw_texts)
    paths = _unique_limited(
        re.findall(r"(?<![\w/.-])[\w./-]+\.(?:py|md|json|toml|txt|yaml|yml|csv|tsv)(?![\w/.-])", combined),
        limit=16,
    )
    symbols = _unique_limited(
        [
            *re.findall(r"\b(?:def|class)\s+([A-Za-z_]\w*)", combined),
            *re.findall(r"\b([A-Za-z_]\w*)\s*\(", combined),
        ],
        limit=16,
    )
    artifacts = _unique_limited(re.findall(r"\bart_[a-f0-9]{16}\b", combined), limit=8)
    constraints = _extract_user_constraints(user_texts)

    return {
        "paths": paths,
        "symbols": symbols,
        "artifacts": artifacts,
        "constraints": constraints,
        "tools": _unique_limited(sorted(tool_names), limit=12),
    }


def _full_text_from_user(message: UserMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _skill_terms(text: str) -> list[str]:
    candidates = [
        *re.findall(r"[A-Za-z_][A-Za-z0-9_:-]{2,}", text),
        *re.findall(r"[\u4e00-\u9fff]{2,8}", text),
    ]
    stop = {"这个", "那个", "一下", "帮我", "现在", "当前", "进行", "分析", "文件"}
    terms: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        clean = item.strip().lower()
        if not clean or clean in stop or clean in seen:
            continue
        seen.add(clean)
        terms.append(clean)
    return terms[:40]


def _skill_term_matches(term: str, haystack: str) -> bool:
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9_:-]+", term):
        return re.search(rf"(?<![a-z0-9_:-]){re.escape(term)}(?![a-z0-9_:-])", haystack) is not None
    return term in haystack


def _skill_phrase_matches(phrase: str, haystack: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9_:-]+", phrase):
        return _skill_term_matches(phrase, haystack)
    return phrase in haystack.replace(" ", "")


def _route_confidence(top_score: float, second_score: float) -> float:
    if top_score <= 0:
        return 0.0
    base = min(0.95, top_score / 24.0)
    margin = max(0.0, min(0.2, (top_score - second_score) / 30.0))
    return round(min(0.99, base + margin), 3)


def _merge_skill_candidates(
    keyword_candidates: list[SkillRouteCandidate],
    embedding_candidates: list[SkillRouteCandidate],
) -> list[SkillRouteCandidate]:
    merged: dict[str, SkillRouteCandidate] = {}
    for candidate in [*keyword_candidates, *embedding_candidates]:
        existing = merged.get(candidate.name)
        if existing is None:
            merged[candidate.name] = candidate
            continue
        existing.score = round(existing.score + candidate.score, 3)
        existing.reason = f"{existing.reason}; {candidate.reason}"
    return list(merged.values())


def _build_skill_rerank_prompt(query: str, candidates: list[SkillRouteCandidate], by_name: dict) -> str:
    lines = [
        "请根据用户请求选择最合适的 Skill。",
        "",
        f"用户请求：{query}",
        "",
        "候选 Skill：",
    ]
    for candidate in candidates:
        skill = by_name[candidate.name]
        lines.extend(
            [
                f"- name: {skill.name}",
                f"  description: {skill.description}",
                f"  triggers: {', '.join(skill.triggers[:8])}",
                f"  tags: {', '.join(skill.tags[:8])}",
                f"  risk_level: {skill.risk_level}",
                f"  auto_invoke: {skill.auto_invoke}",
                f"  recall_score: {candidate.score}",
                f"  recall_reason: {candidate.reason}",
            ]
        )
    lines.extend(
        [
            "",
            "输出 JSON：",
            '{"primary_skill": "skill-name-or-null", "secondary_skills": [], "confidence": 0.0, "auto_execute": true, "reason": "short reason"}',
        ]
    )
    return "\n".join(lines)


def _parse_json_object(text: str) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _is_dependency_install_command(command: str) -> bool:
    if not command:
        return False
    # Shell chains are common in model-generated commands; inspect each segment conservatively.
    segments = re.split(r"\s*(?:&&|\|\||;)\s*", command)
    return any(_is_dependency_install_segment(segment) for segment in segments if segment.strip())


def _is_dependency_install_segment(segment: str) -> bool:
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = segment.split()
    if len(parts) < 2:
        return False

    first = Path(parts[0]).name.lower()
    if first in {"pip", "pip3", "pipx"} and len(parts) >= 2:
        return parts[1] == "install"

    if first in {"python", "python3", "python.exe"} and len(parts) >= 4:
        return parts[1:4] == ["-m", "pip", "install"]

    if first == "uv" and len(parts) >= 3:
        return parts[1:3] == ["pip", "install"]

    return False


def _clip_text(text: str, limit: int) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 40)].rstrip() + "\n...<skill content truncated>..."


def _full_text_from_assistant(message: AssistantMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _full_text_from_tool_result(message: ToolResultMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _extract_user_constraints(user_texts: list[str]) -> list[str]:
    constraints: list[str] = []
    keywords = ("要求", "必须", "不要", "不能", "需要", "希望", "优先", "禁止", "保留", "删除")
    for text in user_texts:
        chunks = re.split(r"[\n。！？!?；;]", text)
        for chunk in chunks:
            clean = re.sub(r"\s+", " ", chunk).strip()
            if not clean or len(clean) > 140:
                continue
            if any(keyword in clean for keyword in keywords):
                constraints.append(clean)
    return _unique_limited(constraints, limit=12)


def _unique_limited(values: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip().strip("'\"`")
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= limit:
            break
    return out
