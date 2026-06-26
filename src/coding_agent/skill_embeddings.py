from __future__ import annotations

"""
Skill 向量召回后端。

默认优先使用本地 BGE-M3；如果依赖或模型不可用，自动退回轻量本地向量，
保证 Skill Router 不会因为 embedding 环境问题影响主流程启动。
"""

from pathlib import Path
import math
import os
import re
from typing import Any

from .extensions.types import SkillRouteCandidate, SkillSpec

DEFAULT_BGE_M3_PATH = "/data4/slx/models/bge-m3"

_MODEL_CACHE: dict[str, Any] = {}


class SkillEmbeddingRetriever:
    def __init__(
        self,
        skills: list[SkillSpec],
        *,
        backend: str = "auto",
        model_path: str | Path | None = None,
        min_similarity: float = 0.55,
    ) -> None:
        self.skills = skills
        self.backend = backend
        self.model_path = str(model_path or os.environ.get("CODECLAW_SKILL_EMBEDDING_MODEL_PATH") or DEFAULT_BGE_M3_PATH)
        self.min_similarity = min_similarity
        self._skill_vectors: list[tuple[SkillSpec, Any]] | None = None
        self._backend_used: str | None = None
        self._load_error: str | None = None

    @property
    def backend_used(self) -> str | None:
        return self._backend_used

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def recall(self, query: str) -> list[SkillRouteCandidate]:
        if self.backend == "off":
            return []
        if self.backend == "local":
            return self._local_recall(query)
        if self.backend in {"auto", "bge-m3"}:
            try:
                return self._bge_recall(query)
            except Exception as exc:
                self._load_error = f"{type(exc).__name__}: {exc}"
                if self.backend == "bge-m3":
                    return []
                return self._local_recall(query)
        return self._local_recall(query)

    def _bge_recall(self, query: str) -> list[SkillRouteCandidate]:
        model = _load_sentence_transformer(self.model_path)
        self._backend_used = "bge-m3"
        query_vector = model.encode(query, normalize_embeddings=True)
        if self._skill_vectors is None or self._backend_used != "bge-m3":
            texts = [_skill_embedding_text(skill) for skill in self.skills if not skill.disable_model_invocation]
            eligible = [skill for skill in self.skills if not skill.disable_model_invocation]
            vectors = model.encode(texts, normalize_embeddings=True) if texts else []
            self._skill_vectors = list(zip(eligible, vectors))

        candidates: list[SkillRouteCandidate] = []
        for skill, vector in self._skill_vectors:
            similarity = _dense_cosine(query_vector, vector)
            if similarity < self.min_similarity:
                continue
            candidates.append(
                SkillRouteCandidate(
                    name=skill.name,
                    score=round(similarity * 10.0, 3),
                    risk_level=skill.risk_level,
                    auto_invoke=skill.auto_invoke,
                    reason=f"bge_m3_recall similarity={similarity:.3f}",
                )
            )
        return candidates

    def _local_recall(self, query: str) -> list[SkillRouteCandidate]:
        self._backend_used = "local"
        query_vector = _local_skill_embedding(query)
        if not query_vector:
            return []
        candidates: list[SkillRouteCandidate] = []
        for skill in self.skills:
            if skill.disable_model_invocation:
                continue
            similarity = _sparse_cosine(query_vector, _local_skill_embedding(_skill_embedding_text(skill)))
            if similarity < 0.12:
                continue
            candidates.append(
                SkillRouteCandidate(
                    name=skill.name,
                    score=round(similarity * 10.0, 3),
                    risk_level=skill.risk_level,
                    auto_invoke=skill.auto_invoke,
                    reason=f"local_embedding_recall similarity={similarity:.3f}",
                )
            )
        return candidates


def _load_sentence_transformer(model_path: str):
    path = str(Path(model_path).expanduser())
    cached = _MODEL_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("sentence-transformers is not installed. Run: pip install sentence-transformers") from exc
    if not Path(path).exists():
        raise RuntimeError(f"embedding model path does not exist: {path}")
    device = os.environ.get("CODECLAW_EMBEDDING_DEVICE", "cpu")
    model = SentenceTransformer(path, device=device)
    _MODEL_CACHE[path] = model
    return model


def _skill_embedding_text(skill: SkillSpec) -> str:
    return " ".join(
        [
            skill.name,
            skill.description,
            " ".join(skill.tags),
            " ".join(skill.triggers),
            " ".join(skill.examples),
            skill.content[:1800],
        ]
    )


def _local_skill_embedding(text: str) -> dict[str, float]:
    tokens = _skill_terms(text)
    grams = _chinese_char_grams(text)
    vector: dict[str, float] = {}
    for token in [*tokens, *grams]:
        vector[token] = vector.get(token, 0.0) + 1.0
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm <= 0:
        return {}
    return {key: value / norm for key, value in vector.items()}


def _skill_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}", text)
    stop = {"the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "with", "我", "你", "这个", "那个"}
    seen: set[str] = set()
    terms: list[str] = []
    for item in raw:
        clean = item.strip().lower()
        if not clean or clean in stop or clean in seen:
            continue
        seen.add(clean)
        terms.append(clean)
    return terms[:80]


def _chinese_char_grams(text: str, *, size: int = 2) -> list[str]:
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    if len(chars) < size:
        return []
    return ["".join(chars[i : i + size]) for i in range(len(chars) - size + 1)]


def _sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def _dense_cosine(left: Any, right: Any) -> float:
    left_values = _as_float_list(left)
    right_values = _as_float_list(right)
    if not left_values or not right_values or len(left_values) != len(right_values):
        return 0.0
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(a * a for a in left_values))
    right_norm = math.sqrt(sum(b * b for b in right_values))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _as_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(value) for value in vector]
