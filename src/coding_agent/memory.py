from __future__ import annotations

"""
自进化长期记忆 V1。

设计目标：先把“执行-反思-提炼-分类存储-索引更新-按需复用”闭环跑通。
存储使用文件系统 + JSONL，方便直接检查和面试讲解。
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ai.types import AssistantMessage, Message, TextContent, ToolCall, ToolResultMessage, UserMessage

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
            return None

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

        scored: list[tuple[int, MemoryRecord]] = []
        for record in self.list(limit=1000):
            haystack = " ".join([record.content, *record.tags]).lower()
            score = sum(_term_score(term, haystack) for term in terms)
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [record for _, record in scored[:limit]]

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
        errors = [r for r in tool_results if r.is_error]
        if not errors:
            return None
        final = assistant_texts[-1] if assistant_texts else ""
        if not any(marker in final for marker in ("修复", "解决", "原因", "通过", "成功")):
            return None
        goal = user_texts[-1] if user_texts else "未知任务"
        err_text = _text_from_tool_result(errors[-1])
        content = f"错误修复经验：任务“{goal[:100]}”中出现错误“{err_text[:120]}”，后续处理结论：{final[:180]}"
        return self.store.add(
            kind="error_fix",
            content=content,
            tags=["error_fix", *_keywords(goal)[:4]],
            source={**source, "extractor": "error_fix_rules"},
            confidence=0.72,
        )


def _text_from_user(message: UserMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _text_from_assistant(message: AssistantMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _text_from_tool_result(message: ToolResultMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


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
