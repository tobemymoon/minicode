from __future__ import annotations

"""
自进化长期记忆 V2。

设计目标：
1. LLM 反思优先，规则反思兜底；
2. 分类存储、准入过滤、去重合并和索引更新；
3. 按需检索、使用次数更新和预算化注入。
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ai.stream import complete_simple
from ai.types import AssistantMessage, Context, Message, Model, SimpleStreamOptions, TextContent, ToolCall, ToolResultMessage, UserMessage

MemoryKind = Literal["preference", "procedural", "error_fix", "project_fact"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(text: str) -> str:
    return "mem_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _fingerprint(kind: str, content: str) -> str:
    normalized = re.sub(r"\s+", " ", content.strip().lower())
    return hashlib.sha1(f"{kind}:{normalized}".encode("utf-8")).hexdigest()


@dataclass
class MemoryRecord:
    id: str
    kind: MemoryKind
    content: str
    tags: list[str]
    source: dict[str, Any]
    confidence: float = 0.7
    use_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        return cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", "project_fact")),  # type: ignore[arg-type]
            content=str(data.get("content", "")),
            tags=[str(x) for x in data.get("tags", []) if str(x).strip()],
            source=dict(data.get("source", {})) if isinstance(data.get("source"), dict) else {},
            confidence=float(data.get("confidence", 0.7)),
            use_count=int(data.get("use_count", 0)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            last_used_at=str(data.get("last_used_at", "")),
            fingerprint=str(data.get("fingerprint", "")),
        )


class MemoryStore:
    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.root = self.workspace_dir / ".codeclaw" / "memory"
        self.memories_file = self.root / "memories.jsonl"
        self.index_file = self.root / "index.json"

    def ensure_initialized(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.memories_file.exists():
            self.memories_file.write_text("", encoding="utf-8")
        if not self.index_file.exists():
            self.index_file.write_text(
                json.dumps({"version": 1, "keywords": {}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def list(self, *, kind: str | None = None, limit: int = 50) -> list[MemoryRecord]:
        self.ensure_initialized()
        records: list[MemoryRecord] = []
        for line in self.memories_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = MemoryRecord.from_dict(json.loads(line))
            if kind and record.kind != kind:
                continue
            records.append(record)
        return records[-max(limit, 1):]

    def add(
        self,
        *,
        kind: MemoryKind,
        content: str,
        tags: list[str],
        source: dict[str, Any],
        confidence: float = 0.7,
    ) -> MemoryRecord | None:
        self.ensure_initialized()
        clean = re.sub(r"\s+", " ", content).strip()
        if len(clean) < 8:
            return None
        fp = _fingerprint(kind, clean)
        existing = self._by_fingerprint(fp)
        if existing:
            return self._merge_existing(existing.id, tags=tags, source=source, confidence=confidence)

        similar = self._find_similar(kind, clean)
        if similar:
            return self._merge_existing(similar.id, tags=tags, source=source, confidence=confidence)

        now = _utc_now_iso()
        record = MemoryRecord(
            id=_stable_id(fp),
            kind=kind,
            content=clean[:1000],
            tags=_unique(tags, limit=12),
            source=source,
            confidence=round(float(confidence), 3),
            created_at=now,
            updated_at=now,
            fingerprint=fp,
        )
        with self.memories_file.open("a", encoding="utf-8") as fp_out:
            fp_out.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        self.rebuild_index()
        return record

    def search(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        terms = _keywords(query)
        if not terms:
            return self.list(limit=limit)

        scored: list[tuple[float, MemoryRecord]] = []
        for record in self.list(limit=1000):
            haystack = " ".join([record.content, *record.tags]).lower()
            score = sum(_term_score(term, haystack) for term in terms)
            if score:
                score += record.confidence
                score += min(record.use_count, 5) * 0.2
                if record.kind == "preference":
                    score += 0.5
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [record for _, record in scored[:limit]]

    def mark_used(self, ids: list[str]) -> list[MemoryRecord]:
        if not ids:
            return []
        id_set = set(ids)
        now = _utc_now_iso()
        updated: list[MemoryRecord] = []
        records = self.list(limit=10000)
        for record in records:
            if record.id not in id_set:
                continue
            record.use_count += 1
            record.last_used_at = now
            record.updated_at = now
            updated.append(record)
        if updated:
            self._write_all(records)
            self.rebuild_index()
        return updated

    def render_for_prompt(self, *, limit: int = 12) -> str:
        records = self.list(limit=200)
        if not records:
            return ""
        priority = {"preference": 0, "project_fact": 1, "procedural": 2, "error_fix": 3}
        records.sort(key=lambda item: (priority.get(item.kind, 9), -item.confidence, item.updated_at))
        lines: list[str] = []
        for record in records[:limit]:
            tags = f" tags={','.join(record.tags[:4])}" if record.tags else ""
            lines.append(f"- [{record.kind}] {record.content}{tags}")
        return "\n".join(lines)

    def rebuild_index(self) -> None:
        records = self.list(limit=10000)
        index: dict[str, list[str]] = {}
        for record in records:
            for keyword in _keywords(" ".join([record.content, *record.tags])):
                index.setdefault(keyword, [])
                if record.id not in index[keyword]:
                    index[keyword].append(record.id)
        payload = {
            "version": 1,
            "updated_at": _utc_now_iso(),
            "memory_count": len(records),
            "keywords": {k: v[:50] for k, v in sorted(index.items())},
        }
        self.index_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        for record in self.list(limit=10000):
            if record.fingerprint == fingerprint:
                return record
        return None

    def _find_similar(self, kind: MemoryKind, content: str) -> MemoryRecord | None:
        content_keys = set(_keywords(content))
        if not content_keys:
            return None
        for record in self.list(kind=kind, limit=10000):
            record_keys = set(_keywords(record.content))
            if not record_keys:
                continue
            overlap = len(content_keys & record_keys) / max(1, min(len(content_keys), len(record_keys)))
            if overlap >= 0.75 or content in record.content or record.content in content:
                return record
        return None

    def _merge_existing(
        self,
        record_id: str,
        *,
        tags: list[str],
        source: dict[str, Any],
        confidence: float,
    ) -> MemoryRecord | None:
        records = self.list(limit=10000)
        merged: MemoryRecord | None = None
        now = _utc_now_iso()
        for record in records:
            if record.id != record_id:
                continue
            record.tags = _unique([*record.tags, *tags], limit=16)
            record.confidence = round(max(record.confidence, float(confidence)), 3)
            record.updated_at = now
            sources = record.source.get("merged_sources") if isinstance(record.source, dict) else None
            if not isinstance(sources, list):
                sources = []
            sources.append(source)
            record.source["merged_sources"] = sources[-8:]
            merged = record
            break
        if merged:
            self._write_all(records)
            self.rebuild_index()
        return merged

    def _write_all(self, records: list[MemoryRecord]) -> None:
        self.ensure_initialized()
        text = "\n".join(json.dumps(record.to_dict(), ensure_ascii=False) for record in records)
        self.memories_file.write_text(text + ("\n" if text else ""), encoding="utf-8")


class MemoryReflector:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def reflect(self, messages: list[Message], *, session_id: str | None = None) -> list[MemoryRecord]:
        if not messages:
            return []
        user_texts = [_text_from_user(m) for m in messages if isinstance(m, UserMessage)]
        assistant_texts = [_text_from_assistant(m) for m in messages if isinstance(m, AssistantMessage)]
        tool_results = [m for m in messages if isinstance(m, ToolResultMessage)]
        tool_names = _tool_names(messages)
        source = {"session_id": session_id, "message_count": len(messages)}

        added: list[MemoryRecord] = []
        for text in user_texts:
            added.extend(self._extract_preferences(text, source=source))

        procedural = self._extract_procedural(user_texts, assistant_texts, tool_results, tool_names, source=source)
        if procedural:
            added.append(procedural)

        error_fix = self._extract_error_fix(user_texts, assistant_texts, tool_results, source=source)
        if error_fix:
            added.append(error_fix)

        return added

    def _extract_preferences(self, text: str, *, source: dict[str, Any]) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        markers = ("我希望", "我想", "我喜欢", "以后", "后续", "默认", "不要", "不能", "必须", "优先", "用中文")
        for chunk in re.split(r"[\n。！？!?；;]", text):
            clean = re.sub(r"\s+", " ", chunk).strip()
            if not clean or len(clean) > 180:
                continue
            if _looks_like_question(clean):
                continue
            if any(marker in clean for marker in markers):
                record = self.store.add(
                    kind="preference",
                    content=f"用户偏好/约束：{clean}",
                    tags=["user_preference", *_keywords(clean)[:5]],
                    source={**source, "extractor": "preference_rules"},
                    confidence=0.82,
                )
                if record:
                    records.append(record)
        return records

    def _extract_procedural(
        self,
        user_texts: list[str],
        assistant_texts: list[str],
        tool_results: list[ToolResultMessage],
        tool_names: list[str],
        *,
        source: dict[str, Any],
    ) -> MemoryRecord | None:
        if not tool_names or not assistant_texts:
            return None
        final = assistant_texts[-1]
        if not any(marker in final for marker in ("完成", "通过", "已修复", "成功", "已经", "验证")):
            return None
        goal = user_texts[-1] if user_texts else "未记录用户目标"
        paths = _paths("\n".join([goal, final, *[_text_from_tool_result(r) for r in tool_results]]))
        content = (
            f"可复用执行经验：处理“{goal[:120]}”时，使用工具链 {', '.join(tool_names[:8])}"
            f"{'，涉及文件 ' + ', '.join(paths[:6]) if paths else ''}，最终结果显示任务完成或验证通过。"
        )
        return self.store.add(
            kind="procedural",
            content=content,
            tags=["execution_pattern", *tool_names[:5], *paths[:5]],
            source={**source, "extractor": "procedural_rules"},
            confidence=0.68,
        )

    def _extract_error_fix(
        self,
        user_texts: list[str],
        assistant_texts: list[str],
        tool_results: list[ToolResultMessage],
        *,
        source: dict[str, Any],
    ) -> MemoryRecord | None:
        errors = [r for r in tool_results if r.is_error or _looks_like_error_tool_result(r)]
        combined_assistant = "\n".join(assistant_texts)
        err_text = _text_from_tool_result(errors[-1]) if errors else _extract_error_excerpt(combined_assistant)
        if not err_text:
            return None
        if not any(marker in combined_assistant for marker in ("修复", "解决", "原因", "处理", "下次", "错误", "排查", "FileNotFoundError")):
            return None
        goal = user_texts[-1] if user_texts else "未知任务"
        content = f"错误修复经验：任务“{goal[:100]}”中出现错误“{err_text[:160]}”，后续处理结论：{_clip(combined_assistant, 220)}"
        return self.store.add(
            kind="error_fix",
            content=content,
            tags=["error_fix", *_keywords(goal)[:4], *_keywords(err_text)[:4]],
            source={**source, "extractor": "error_fix_rules"},
            confidence=0.72,
        )


class LLMMemoryReflector:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def reflect(
        self,
        *,
        model: Model,
        messages: list[Message],
        session_id: str | None = None,
        max_items: int = 3,
    ) -> list[MemoryRecord]:
        formatted = _format_messages_for_llm_reflection(messages)
        if not formatted:
            return []

        prompt = f"""请从下面这轮 AI 编程助手执行轨迹中提炼长期记忆候选。

只保留未来可复用的信息，忽略一次性闲聊、临时文件名、无价值细节。
允许的 kind 只有：preference、procedural、error_fix、project_fact。
最多输出 {max_items} 条。

输出必须是 JSON，不要 Markdown：
{{
  "memories": [
    {{
      "kind": "preference",
      "content": "用户希望解释时偏面试话术",
      "tags": ["interview", "preference"],
      "confidence": 0.86,
      "reason": "用户明确表达长期偏好"
    }}
  ]
}}

执行轨迹：
{formatted}
"""
        result = await complete_simple(
            model,
            Context(messages=[UserMessage(content=prompt)], system_prompt=_LLM_MEMORY_SYSTEM_PROMPT),
            SimpleStreamOptions(max_tokens=1200),
        )
        raw = "\n".join(block.text for block in result.content if isinstance(block, TextContent)).strip()
        payload = _parse_json_object(raw)
        items = payload.get("memories") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        records: list[MemoryRecord] = []
        for item in items[:max_items]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()
            if kind not in {"preference", "procedural", "error_fix", "project_fact"}:
                continue
            content = str(item.get("content", "")).strip()
            if not _memory_content_allowed(content):
                continue
            tags = [str(x) for x in item.get("tags", []) if str(x).strip()] if isinstance(item.get("tags"), list) else []
            confidence = float(item.get("confidence", 0.75))
            record = self.store.add(
                kind=kind,  # type: ignore[arg-type]
                content=content,
                tags=[*tags, *_keywords(content)[:4]],
                source={
                    "session_id": session_id,
                    "extractor": "llm_reflection",
                    "reason": str(item.get("reason", ""))[:240],
                },
                confidence=max(0.0, min(1.0, confidence)),
            )
            if record:
                records.append(record)
        return records


def _text_from_user(message: UserMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _text_from_assistant(message: AssistantMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _text_from_tool_result(message: ToolResultMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _looks_like_error_tool_result(message: ToolResultMessage) -> bool:
    details = message.details if isinstance(message.details, dict) else {}
    exit_code = details.get("exit_code")
    try:
        if exit_code is not None and int(exit_code) != 0:
            return True
    except (TypeError, ValueError):
        pass
    if details.get("error") or details.get("timeout") or details.get("blocked"):
        return True
    text = _text_from_tool_result(message).lower()
    markers = (
        "traceback",
        "filenotfounderror",
        "no such file",
        "no such file or directory",
        "command failed",
        "error:",
        "[stderr]",
        "报错",
        "错误",
        "失败",
        "不存在",
    )
    return any(marker in text for marker in markers)


def _extract_error_excerpt(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"python: can't open file [^\n]+",
        r"FileNotFoundError[^\n]*",
        r"No such file or directory[^\n]*",
        r"没有那个文件或目录[^\n]*",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return _clip(match.group(0), 220)
    lowered = text.lower()
    if any(marker in lowered for marker in ("filenotfounderror", "no such file", "不存在", "文件路径错误")):
        return _clip(text, 220)
    return ""


def _tool_names(messages: list[Message]) -> list[str]:
    names: list[str] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    names.append(block.name)
        elif isinstance(message, ToolResultMessage):
            names.append(message.tool_name)
    return _unique(names, limit=12)


_LLM_MEMORY_SYSTEM_PROMPT = """你是 CodeClaw 的长期记忆反思器。
你的任务是把一轮执行轨迹提炼为未来可复用的长期记忆。
记忆必须简洁、准确、可复用；不要保存敏感信息、一次性命令输出或无价值临时细节。
只输出合法 JSON。"""


def _format_messages_for_llm_reflection(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages[-20:]:
        if isinstance(message, UserMessage):
            text = _text_from_user(message)
            if text:
                lines.append(f"User: {_clip(text, 700)}")
        elif isinstance(message, AssistantMessage):
            text = _text_from_assistant(message)
            tool_calls = [block.name for block in message.content if isinstance(block, ToolCall)]
            if text:
                lines.append(f"Assistant: {_clip(text, 700)}")
            if tool_calls:
                lines.append(f"AssistantToolCalls: {', '.join(tool_calls)}")
        elif isinstance(message, ToolResultMessage):
            text = _text_from_tool_result(message)
            prefix = "ToolError" if message.is_error else "ToolResult"
            if text:
                lines.append(f"{prefix}({message.tool_name}): {_clip(text, 500)}")
    return "\n".join(lines).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _memory_content_allowed(content: str) -> bool:
    clean = re.sub(r"\s+", " ", content).strip()
    if len(clean) < 8 or len(clean) > 1000:
        return False
    blocked = ("api_key", "secret", "password", "token=", "sk-")
    return not any(item in clean.lower() for item in blocked)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "...<truncated>"


def _paths(text: str) -> list[str]:
    return _unique(
        re.findall(r"(?<![\w/.-])[\w./-]+\.(?:py|md|json|toml|txt|yaml|yml|csv|tsv)(?![\w/.-])", text),
        limit=12,
    )


def _keywords(text: str) -> list[str]:
    candidates = [
        *re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text),
        *re.findall(r"[\u4e00-\u9fff]{2,8}", text),
        *_paths(text),
    ]
    stop = {"the", "and", "for", "with", "from", "this", "that", "用户", "任务", "工具", "文件"}
    return [item for item in _unique(candidates, limit=40) if item.lower() not in stop]


def _term_score(term: str, haystack: str) -> int:
    term_lower = term.lower()
    if term_lower in haystack:
        return 3
    if re.search(r"[\u4e00-\u9fff]", term):
        grams = _chinese_grams(term, size=2)
        hits = sum(1 for gram in grams if gram in haystack)
        if hits:
            return min(2, hits)
    return 0


def _chinese_grams(text: str, *, size: int) -> list[str]:
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    if len(chars) < size:
        return []
    return ["".join(chars[i : i + size]) for i in range(len(chars) - size + 1)]


def _looks_like_question(text: str) -> bool:
    question_markers = ("吗", "么", "怎么", "为什么", "是什么", "咋", "如何", "能不能", "可不可以", "会不会")
    return any(marker in text for marker in question_markers)


def _unique(values: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip().strip("'\"`")
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
        if len(out) >= limit:
            break
    return out
