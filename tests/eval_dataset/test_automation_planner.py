from __future__ import annotations

from collections import Counter

from bidmate_rag.eval_dataset.automation import planner


def test_default_sop_slots_have_exact_type_and_difficulty_distribution() -> None:
    assert hasattr(planner, "plan_sop_slots"), "SOP slot planner is missing"
    slots = planner.plan_sop_slots(30)
    assert [slot.ordinal for slot in slots] == list(range(1, 31))
    assert Counter(slot.sop_type for slot in slots) == {"A": 9, "B": 12, "C": 3, "D": 3, "E": 3}
    assert Counter(slot.difficulty for slot in slots) == {"low": 15, "medium": 9, "high": 6}
    assert all(slot.document_count == 2 for slot in slots if slot.sop_type == "B")
    assert all(slot.requires_history for slot in slots if slot.sop_type == "C")
    assert all(
        slot.answerability == "unanswerable" and slot.document_count == 1
        for slot in slots
        if slot.sop_type == "D"
    )


def test_non_default_slot_allocation_is_deterministic_and_sums_to_target() -> None:
    assert hasattr(planner, "plan_sop_slots"), "SOP slot planner is missing"
    first = planner.plan_sop_slots(17)
    second = planner.plan_sop_slots(17)
    assert first == second
    assert len(first) == 17
    assert sum(Counter(slot.sop_type for slot in first).values()) == 17
    assert sum(Counter(slot.difficulty for slot in first).values()) == 17


def test_five_item_calibration_uses_each_type_in_api_compatible_order() -> None:
    slots = planner.plan_sop_slots(5)

    assert Counter(slot.sop_type for slot in slots) == {
        "A": 1,
        "B": 1,
        "C": 1,
        "D": 1,
        "E": 1,
    }
    assert [slot.sop_type for slot in slots] == ["A", "E", "B", "C", "D"]
