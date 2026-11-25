"""Deterministic ranking of work experiences against a job (US-2.2).

Keyword overlap only — no embeddings, no LLM. Experiences with zero
overlap are omitted. Ties break on company name then experience id.
"""

from __future__ import annotations

from typing import Any

from src.matching.semantic import _tokenize

# Career-generic words that would otherwise match every "Engineer" title.
_RANK_STOP_WORDS = frozenset(
    {
        "engineer",
        "engineering",
        "software",
        "developer",
        "development",
        "experience",
        "experienced",
        "need",
        "needed",
        "require",
        "required",
        "requirement",
        "requirements",
        "join",
        "team",
        "role",
        "staff",
        "senior",
        "junior",
        "lead",
        "principal",
        "manager",
        "platform",
        "frontend",
        "backend",
        "fullstack",
        "full",
        "stack",
        "job",
        "jobs",
        "work",
        "working",
        "description",
        "including",
        "using",
        "use",
        "used",
        "you",
        "will",
        "our",
        "we",
        "us",
        "your",
        "position",
        "opportunity",
        "strong",
        "ability",
        "skills",
        "skill",
    }
)


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in _tokenize(text):
        cleaned = token.strip(".")
        if len(cleaned) > 1 and cleaned not in _RANK_STOP_WORDS:
            terms.add(cleaned)
    return terms


def _collect_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        parts: list[str] = []
        for item in value.values():
            parts.extend(_collect_strings(item))
        return parts
    if isinstance(value, list | tuple | set):
        parts = []
        for item in value:
            parts.extend(_collect_strings(item))
        return parts
    return [str(value)]


def _job_text(job: dict[str, Any]) -> str:
    parts = [
        job.get("title"),
        job.get("company"),
        job.get("description"),
        job.get("snippet"),
    ]
    parts.extend(_collect_strings(job.get("requirements")))
    raw = job.get("raw_data")
    if isinstance(raw, dict):
        parts.append(raw.get("description"))
        parts.extend(_collect_strings(raw.get("requirements")))
    return " ".join(str(part) for part in parts if part)


def _experience_text(experience: dict[str, Any]) -> str:
    parts = [
        experience.get("company") or experience.get("client"),
        experience.get("problem"),
        " ".join(_collect_strings(experience.get("skills"))),
        " ".join(_collect_strings(experience.get("domains"))),
        " ".join(_collect_strings(experience.get("actions"))),
        " ".join(_collect_strings(experience.get("outcomes"))),
    ]
    for bullet in experience.get("bullets") or []:
        if isinstance(bullet, dict):
            parts.append(bullet.get("text") or bullet.get("content"))
            parts.extend(_collect_strings(bullet.get("tags")))
        elif bullet:
            parts.append(str(bullet))
    return " ".join(str(part) for part in parts if part)


def rank_experiences(
    experiences: list[Any],
    job: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return overlapping experiences, highest score first.

    Score is ``|overlap| / |job terms|`` rounded to 6 decimals. Zero
    overlap is omitted rather than ranked at the bottom.
    """
    job_terms = _terms(_job_text(job if isinstance(job, dict) else {}))
    if not job_terms:
        return []

    ranked: list[dict[str, Any]] = []
    for experience in experiences:
        if not isinstance(experience, dict):
            continue
        overlap = sorted(job_terms & _terms(_experience_text(experience)))
        if not overlap:
            continue
        ranked.append(
            {
                "id": str(experience.get("id") or ""),
                "company": str(
                    experience.get("company") or experience.get("client") or ""
                ),
                "title": str(
                    experience.get("title") or experience.get("role") or ""
                ),
                "score": round(len(overlap) / len(job_terms), 6),
                "matched_terms": overlap,
            }
        )

    ranked.sort(key=lambda row: (-row["score"], row["company"].lower(), row["id"]))
    return ranked
