from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class SopSlot:
    ordinal: int
    sop_type: str
    task_kind: str
    difficulty: str
    document_count: int
    answerability: str
    evidence_mode: str
    requires_history: bool
    perturbation: str


def _largest_remainder(total: int, weights: tuple[int, ...]) -> list[int]:
    if total < 1:
        raise ValueError("target_count must be positive")
    denominator = sum(weights)
    quotas = [total * weight / denominator for weight in weights]
    counts = [floor(quota) for quota in quotas]
    remaining = total - sum(counts)
    order = sorted(range(len(weights)), key=lambda index: (-(quotas[index] - counts[index]), index))
    for index in order[:remaining]:
        counts[index] += 1
    return counts


def plan_sop_slots(target_count: int = 30) -> list[SopSlot]:
    type_names = ("A", "B", "C", "D", "E")
    type_counts = _largest_remainder(target_count, (9, 12, 3, 3, 3))
    difficulty_names = ("low", "medium", "high")
    difficulty_counts = _largest_remainder(target_count, (15, 9, 6))
    types = (
        ["A", "E", "B", "C", "D"]
        if target_count == len(type_names)
        else [
            name
            for name, count in zip(type_names, type_counts, strict=True)
            for _ in range(count)
        ]
    )
    difficulties = [
        name
        for name, count in zip(difficulty_names, difficulty_counts, strict=True)
        for _ in range(count)
    ]
    contracts = {
        "A": ("extract", 1, "answerable", "direct_quote", False, "none"),
        "B": ("compare", 2, "answerable", "multi_evidence", False, "none"),
        "C": ("follow_up", 1, "answerable", "direct_quote", True, "none"),
        "D": ("extract", 1, "unanswerable", "none", False, "none"),
        "E": ("extract", 1, "answerable", "direct_quote", False, "typo"),
    }
    slots: list[SopSlot] = []
    for index, (sop_type, difficulty) in enumerate(zip(types, difficulties, strict=True), start=1):
        task_kind, document_count, answerability, evidence_mode, history, perturbation = contracts[
            sop_type
        ]
        slots.append(
            SopSlot(
                index,
                sop_type,
                task_kind,
                difficulty,
                document_count,
                answerability,
                evidence_mode,
                history,
                perturbation,
            )
        )
    return slots


def plan_segments(segments: list[str]) -> list[dict[str, str]]:
    return [{"segment": segment} for segment in segments]
