from __future__ import annotations

"""
Artifact-backed context compression.

V1 focuses on keeping long tool results out of the LLM context while preserving
recoverability through stable artifact ids and JSONL metadata.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai.types import Message, TextContent, ToolResultMessage


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    path: str
    sha256: str
    chars: int
    lines: int
    summary: str
    session_id: str | None
    source: dict[str, Any]
    created_at: str
    created: bool = False


class ArtifactStore:
    """Filesystem + JSONL artifact store under `.codeclaw/artifacts`."""

    _ID_RE = re.compile(r"^art_[a-f0-9]{16}$")

    def __init__(self, workspace_dir: str | Path, session_id: str | None = None) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.session_id = session_id
        self.root = self.workspace_dir / ".codeclaw" / "artifacts"
        self.legacy_root = self.workspace_dir / ".xingclaw" / "artifacts"
        self.blob_dir = self.root / "blobs"
        self.legacy_blob_dir = self.legacy_root / "blobs"
        self.index_file = self.root / "artifacts.jsonl"

    def ensure_initialized(self) -> None:
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self.index_file.write_text("", encoding="utf-8")

    def put_text(self, text: str, *, source: dict[str, Any] | None = None) -> ArtifactRecord:
        self.ensure_initialized()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        artifact_id = f"art_{digest[:16]}"
        blob_path = self.blob_dir / f"{artifact_id}.txt"
        created = not blob_path.exists()
        if created:
            blob_path.write_text(text, encoding="utf-8")

        record = ArtifactRecord(
            artifact_id=artifact_id,
            path=blob_path.relative_to(self.workspace_dir).as_posix(),
            sha256=digest,
            chars=len(text),
            lines=text.count("\n") + (1 if text else 0),
            summary=_build_artifact_summary(text),
            session_id=self.session_id,
            source=source or {},
            created_at=_utc_now_iso(),
            created=created,
        )
        if created:
            self._append_record(record)
        return record

    def read_text(self, artifact_id: str, *, offset: int = 0, max_chars: int = 4000) -> tuple[str, dict[str, Any]]:
        blob_path = self._blob_path(artifact_id)
        if not blob_path.exists():
            raise FileNotFoundError(f"Artifact not found: {artifact_id}")
        text = blob_path.read_text(encoding="utf-8", errors="replace")
        safe_offset = max(0, int(offset))
        safe_max = max(1, int(max_chars))
        chunk = text[safe_offset : safe_offset + safe_max]
        return chunk, {
            "artifact_id": artifact_id,
            "offset": safe_offset,
            "max_chars": safe_max,
            "returned_chars": len(chunk),
            "total_chars": len(text),
            "has_more": safe_offset + len(chunk) < len(text),
        }

    def search_text(
        self,
        artifact_id: str,
        query: str,
        *,
        max_results: int = 5,
        context_chars: int = 120,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        blob_path = self._blob_path(artifact_id)
        if not blob_path.exists():
            raise FileNotFoundError(f"Artifact not found: {artifact_id}")
        text = blob_path.read_text(encoding="utf-8", errors="replace")
        terms = [term.lower() for term in re.findall(r"\w+", query) if term.strip()]
        safe_max = max(1, min(int(max_results), 20))
        safe_context = max(20, min(int(context_chars), 500))
        results: list[dict[str, Any]] = []
        line_start = 0
        for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
            line_text = line.rstrip("\n")
            haystack = line_text.lower()
            if terms and not all(term in haystack for term in terms):
                line_start += len(line)
                continue
            if not terms and not line_text.strip():
                line_start += len(line)
                continue
            match_index = 0
            if terms:
                positions = [haystack.find(term) for term in terms if haystack.find(term) >= 0]
                match_index = min(positions) if positions else 0
            offset = line_start + match_index
            snippet_start = max(0, match_index - safe_context)
            snippet_end = min(len(line_text), match_index + safe_context)
            results.append(
                {
                    "line": line_number,
                    "offset": offset,
                    "snippet": line_text[snippet_start:snippet_end],
                }
            )
            if len(results) >= safe_max:
                break
            line_start += len(line)
        return results, {
            "artifact_id": artifact_id,
            "query": query,
            "matches": len(results),
            "max_results": safe_max,
            "total_chars": len(text),
        }

    def _blob_path(self, artifact_id: str) -> Path:
        if not self._ID_RE.match(artifact_id):
            raise ValueError("Invalid artifact_id")
        primary = self.blob_dir / f"{artifact_id}.txt"
        legacy = self.legacy_blob_dir / f"{artifact_id}.txt"
        return legacy if legacy.exists() and not primary.exists() else primary

    def _append_record(self, record: ArtifactRecord) -> None:
        payload = {
            "artifact_id": record.artifact_id,
            "path": record.path,
            "sha256": record.sha256,
            "chars": record.chars,
            "lines": record.lines,
            "summary": record.summary,
            "session_id": record.session_id,
            "source": record.source,
            "created_at": record.created_at,
        }
        with self.index_file.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class CompressionRecord:
    artifact_id: str
    tool_call_id: str
    tool_name: str
    original_chars: int
    preview_chars: int
    source: dict[str, Any]


@dataclass(frozen=True)
class CompressionResult:
    messages: list[Message]
    records: list[CompressionRecord]


class ToolResultCompactor:
    """Externalize long ToolResultMessage text blocks into ArtifactStore."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        max_inline_chars: int = 6000,
        preview_chars: int = 600,
    ) -> None:
        self.artifact_store = artifact_store
        self.max_inline_chars = max_inline_chars
        self.preview_chars = preview_chars

    def compact(self, message: ToolResultMessage) -> tuple[ToolResultMessage, CompressionRecord | None]:
        if self._already_compacted(message):
            return message, None

        full_text = "\n".join(block.text for block in message.content if isinstance(block, TextContent))
        if len(full_text) <= self.max_inline_chars:
            return message, None

        source = {
            "kind": "tool_result",
            "tool_name": message.tool_name,
            "tool_call_id": message.tool_call_id,
            "is_error": message.is_error,
        }
        record = self.artifact_store.put_text(full_text, source=source)
        preview = _build_artifact_summary(full_text, max_chars=self.preview_chars)
        placeholder = self._build_placeholder(
            artifact_id=record.artifact_id,
            tool_name=message.tool_name,
            original_chars=len(full_text),
            line_count=record.lines,
            preview=preview,
        )
        details = message.details if isinstance(message.details, dict) else {"original_details": message.details}
        details = {
            **details,
            "artifact": {
                "artifact_id": record.artifact_id,
                "path": record.path,
                "original_chars": len(full_text),
                "lines": record.lines,
                "preview_chars": len(preview),
            },
        }
        compacted = ToolResultMessage(
            role=message.role,
            tool_call_id=message.tool_call_id,
            tool_name=message.tool_name,
            content=[TextContent(text=placeholder)],
            is_error=message.is_error,
            details=details,
            timestamp=message.timestamp,
        )
        return compacted, CompressionRecord(
            artifact_id=record.artifact_id,
            tool_call_id=message.tool_call_id,
            tool_name=message.tool_name,
            original_chars=len(full_text),
            preview_chars=len(preview),
            source=source,
        )

    @staticmethod
    def _already_compacted(message: ToolResultMessage) -> bool:
        if isinstance(message.details, dict) and isinstance(message.details.get("artifact"), dict):
            return True
        text = "\n".join(block.text for block in message.content if isinstance(block, TextContent))
        return "[Artifact Placeholder]" in text and "artifact_id=" in text

    @staticmethod
    def _build_placeholder(
        *,
        artifact_id: str,
        tool_name: str,
        original_chars: int,
        line_count: int,
        preview: str,
    ) -> str:
        return (
            "[Artifact Placeholder]\n"
            f"tool_name={tool_name}\n"
            f"artifact_id={artifact_id}\n"
            f"original_chars={original_chars}\n"
            f"line_count={line_count}\n"
            "The full tool result was externalized to keep the LLM context compact.\n"
            "Use search_artifact first to find relevant offsets, then read_artifact only for small exact chunks.\n\n"
            "[Summary Preview]\n"
            f"{preview}"
            "\n[/Summary Preview]"
        )


class ContextCompressor:
    """Apply V1 compression rules to a message list before LLM conversion."""

    def __init__(self, tool_result_compactor: ToolResultCompactor) -> None:
        self.tool_result_compactor = tool_result_compactor

    def compress(self, messages: list[Message]) -> CompressionResult:
        out: list[Message] = []
        records: list[CompressionRecord] = []
        for message in messages:
            if isinstance(message, ToolResultMessage):
                compacted, record = self.tool_result_compactor.compact(message)
                out.append(compacted)
                if record:
                    records.append(record)
            else:
                out.append(message)
        return CompressionResult(messages=out, records=records)


def _build_artifact_summary(text: str, *, max_chars: int = 600) -> str:
    lines = text.splitlines()
    non_empty = [line.strip() for line in lines if line.strip()]
    parts = [
        f"chars={len(text)}",
        f"lines={len(lines)}",
    ]
    if non_empty:
        parts.append("sample:")
        for line in non_empty[:8]:
            clean = re.sub(r"\s+", " ", line)
            parts.append(f"- {clean[:160]}")
    summary = "\n".join(parts)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + "\n...<summary truncated>..."
    return summary
