from __future__ import annotations

from bidmate_rag.eval_dataset.automation.ledger import AutomationLedger


def test_ledger_reuses_run_only_for_the_same_complete_identity(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    identity = {
        "batch_id": 1,
        "mode": "mock",
        "slot_plan_hash": "slots-v1",
        "prompt_bundle_hash": "prompts-v2",
        "contract_version": "bidmate-eval-automation-v2",
        "provider_model": "local-deterministic-v1",
        "document_set_hash": "documents-a",
    }

    first = ledger.get_or_create_run_for_identity(
        dataset_id="batch-1",
        identity=identity,
        cost_limit_microusd=0,
    )
    same = ledger.get_or_create_run_for_identity(
        dataset_id="batch-1",
        identity=dict(identity),
        cost_limit_microusd=0,
    )
    changed = ledger.get_or_create_run_for_identity(
        dataset_id="batch-1",
        identity={**identity, "document_set_hash": "documents-b"},
        cost_limit_microusd=0,
    )

    assert same == first
    assert changed != first


def test_ledger_reuses_done_work_unit_and_preserves_terminal_result(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = ledger.create_run("dataset-1", cost_limit_microusd=5_000_000)
    first = ledger.create_work_unit(
        run_id,
        ordinal=1,
        plan={"sop_type": "A"},
        prompt_bundle_hash="p1",
    )
    ledger.mark_done(first.work_unit_id, result={"item_id": "item-1"})

    second = ledger.create_work_unit(
        run_id,
        ordinal=1,
        plan={"sop_type": "A"},
        prompt_bundle_hash="p1",
    )

    assert second.work_unit_id == first.work_unit_id
    assert second.status == "done"
    assert ledger.work_unit_result(first.work_unit_id) == {"item_id": "item-1"}


def test_retry_query_returns_only_retryable_failed_units_and_preserves_stage(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = ledger.create_run("dataset-2", cost_limit_microusd=100)
    retryable = ledger.create_work_unit(
        run_id, ordinal=1, plan={"sop_type": "A"}, prompt_bundle_hash="p1"
    )
    needs_review = ledger.create_work_unit(
        run_id, ordinal=2, plan={"sop_type": "D"}, prompt_bundle_hash="p1"
    )
    permanent = ledger.create_work_unit(
        run_id, ordinal=3, plan={"sop_type": "B"}, prompt_bundle_hash="p1"
    )
    done = ledger.create_work_unit(
        run_id, ordinal=4, plan={"sop_type": "C"}, prompt_bundle_hash="p1"
    )

    ledger.claim(retryable.work_unit_id)
    ledger.record_failure(
        retryable.work_unit_id,
        error="temporary provider error",
        retryable=True,
        failure_stage="generator",
    )
    ledger.mark_needs_review(needs_review.work_unit_id, reason="absence could not be proven")
    ledger.record_failure(
        permanent.work_unit_id,
        error="invalid provider response",
        retryable=False,
        failure_stage="reviewer",
    )
    ledger.mark_done(done.work_unit_id, result={"item_id": "item-4"})

    candidates = ledger.retryable_units(run_id)

    assert [(unit.work_unit_id, unit.status, unit.failure_stage) for unit in candidates] == [
        (retryable.work_unit_id, "retryable_failed", "generator")
    ]
    summary = ledger.summary(run_id)
    assert summary["terminal_count"] == 3
    assert summary["retryable_count"] == 1
    assert summary["needs_review_count"] == 1
    assert summary["permanent_failed_count"] == 1


def test_cost_authorization_stops_before_cap_without_recording_overspend(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = ledger.create_run("dataset-3", cost_limit_microusd=100)

    assert ledger.authorize_cost(run_id, estimated_microusd=60) is True
    ledger.record_cost(run_id, 60)
    assert ledger.authorize_cost(run_id, estimated_microusd=41) is False

    summary = ledger.summary(run_id)
    assert summary["status"] == "cost_paused"
    assert summary["cost_microusd"] == 60
