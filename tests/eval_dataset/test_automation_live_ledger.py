from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from bidmate_rag.eval_dataset.automation.ledger import AutomationLedger, CostLimitExceeded


def _live_run(ledger: AutomationLedger, *, run_key: str = "calibration") -> str:
    campaign_id = ledger.create_campaign(
        campaign_key="public-live-poc-v1",
        cost_limit_microusd=5_000_000,
    )
    return ledger.create_run(
        run_key=run_key,
        campaign_id=campaign_id,
        mode="live",
        cost_limit_microusd=5_000_000,
    )


def _reserve(ledger: AutomationLedger, run_id: str, *, unit: str, amount: int):
    return ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id=unit,
        stage="generator",
        attempt=1,
        model="gpt-stub",
        request_hash=(unit * 64)[:64],
        reserved_microusd=amount,
    )


def test_provider_reservation_blocks_before_campaign_limit_is_exceeded(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)

    first = _reserve(ledger, run_id, unit="a", amount=4_600_000)

    assert first.status == "reserved"
    with pytest.raises(CostLimitExceeded):
        _reserve(ledger, run_id, unit="b", amount=500_001)


def test_success_reconciles_reservation_to_actual_usage(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    call = _reserve(ledger, run_id, unit="a", amount=500_000)

    ledger.reconcile_provider_call(
        provider_call_id=call.provider_call_id,
        status="succeeded",
        actual_microusd=120_000,
        input_tokens=2_000,
        output_tokens=300,
        provider_response_id="resp_123",
    )

    totals = ledger.get_cost_totals(run_id)
    assert totals.actual_microusd == 120_000
    assert totals.open_reserved_microusd == 0


def test_ambiguous_failure_keeps_reservation_and_is_not_retryable(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    call = _reserve(ledger, run_id, unit="a", amount=500_000)

    ledger.mark_provider_call_unknown(call.provider_call_id, error_code="transport")

    totals = ledger.get_cost_totals(run_id)
    assert totals.open_reserved_microusd == 500_000
    assert ledger.provider_call_retryable(call.provider_call_id) is False


def test_same_provider_request_is_idempotent(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)

    first = _reserve(ledger, run_id, unit="a", amount=500_000)
    second = _reserve(ledger, run_id, unit="a", amount=500_000)

    assert second.provider_call_id == first.provider_call_id


def test_calibration_and_full_runs_share_one_campaign_cap(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    calibration_run = _live_run(ledger, run_key="calibration")
    campaign_id = ledger.connection.execute(
        "SELECT campaign_id FROM runs WHERE run_id=?", (calibration_run,)
    ).fetchone()["campaign_id"]
    full_run = ledger.create_run(
        run_key="full",
        campaign_id=campaign_id,
        mode="live",
        cost_limit_microusd=5_000_000,
    )
    calibration = _reserve(ledger, calibration_run, unit="a", amount=1_000_000)
    ledger.reconcile_provider_call(
        provider_call_id=calibration.provider_call_id,
        status="succeeded",
        actual_microusd=1_000_000,
        input_tokens=10,
        output_tokens=10,
        provider_response_id="resp_calibration",
    )

    with pytest.raises(CostLimitExceeded):
        _reserve(ledger, full_run, unit="b", amount=4_000_001)


def test_operational_cap_is_atomic_and_duplicate_reservation_remains_retrievable(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    first = ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id="first",
        stage="generator",
        attempt=1,
        model="gpt-stub",
        request_hash="a" * 64,
        reserved_microusd=4_500_000,
        operational_cap_microusd=4_500_000,
    )

    duplicate = ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id="first",
        stage="generator",
        attempt=1,
        model="gpt-stub",
        request_hash="a" * 64,
        reserved_microusd=4_500_000,
        operational_cap_microusd=4_500_000,
    )
    assert duplicate.provider_call_id == first.provider_call_id

    with pytest.raises(CostLimitExceeded, match="operational"):
        ledger.reserve_provider_call(
            run_id=run_id,
            work_unit_id="second",
            stage="generator",
            attempt=1,
            model="gpt-stub",
            request_hash="b" * 64,
            reserved_microusd=1,
            operational_cap_microusd=4_500_000,
        )


def test_concurrent_new_reservations_admit_only_one_before_operational_cap(tmp_path) -> None:
    database = tmp_path / "ledger.sqlite3"
    ledger = AutomationLedger(database)
    run_id = _live_run(ledger)

    def reserve(unit: str) -> str:
        isolated = AutomationLedger(database)
        try:
            isolated.reserve_provider_call(
                run_id=run_id,
                work_unit_id=unit,
                stage="generator",
                attempt=1,
                model="gpt-stub",
                request_hash=(unit * 64)[:64],
                reserved_microusd=3_000_000,
                operational_cap_microusd=4_500_000,
            )
            return "reserved"
        except CostLimitExceeded:
            return "cost_limited"
        finally:
            isolated.connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ("a", "b")))

    assert sorted(outcomes) == ["cost_limited", "reserved"]


def test_requeue_released_auth_failure_is_limited_and_keeps_stage(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    work_units = []
    for ordinal in (1, 2):
        unit = ledger.create_work_unit(
            run_id,
            ordinal=ordinal,
            plan={"ordinal": ordinal},
            prompt_bundle_hash="prompt-v1",
        )
        ledger.claim(unit.work_unit_id)
        call = ledger.reserve_provider_call(
            run_id=run_id,
            work_unit_id=unit.work_unit_id,
            stage="selector",
            attempt=1,
            model="gpt-stub",
            request_hash=str(ordinal) * 64,
            reserved_microusd=100_000,
        )
        ledger.reconcile_provider_call(
            provider_call_id=call.provider_call_id,
            status="released",
            error_code="selector_provider_http_401",
        )
        ledger.record_failure(
            unit.work_unit_id,
            error="selector_provider_http_401",
            retryable=False,
            failure_stage="selector",
        )
        work_units.append(unit)

    requeued = ledger.requeue_released_auth_failures(run_id, limit=1)

    assert [unit.work_unit_id for unit in requeued] == [work_units[0].work_unit_id]
    assert requeued[0].status == "retryable_failed"
    assert requeued[0].failure_stage == "selector"
    remaining = ledger.connection.execute(
        "SELECT status FROM work_units WHERE work_unit_id=?",
        (work_units[1].work_unit_id,),
    ).fetchone()
    assert remaining["status"] == "permanent_failed"
    totals = ledger.get_cost_totals(run_id)
    assert totals.actual_microusd == 0
    assert totals.open_reserved_microusd == 0



def test_recover_captured_selector_response_reconciles_cost_and_resumes_generator(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    unit = ledger.create_work_unit(
        run_id,
        ordinal=1,
        plan={"ordinal": 1},
        prompt_bundle_hash="prompt-v1",
    )
    ledger.claim(unit.work_unit_id)
    call = ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id=unit.work_unit_id,
        stage="selector",
        attempt=1,
        model="gpt-stub",
        request_hash="a" * 64,
        reserved_microusd=100_000,
    )
    ledger.mark_provider_call_unknown(call.provider_call_id, error_code="invalid_response")
    ledger.record_failure(
        unit.work_unit_id,
        error="provider_output_repair:invalid_provider_response:usage details",
        retryable=False,
        failure_stage="selector",
    )

    recovered = ledger.recover_captured_selector_response(
        provider_call_id=call.provider_call_id,
        actual_microusd=29_841,
        input_tokens=5_176,
        output_tokens=303,
        provider_response_id="resp_captured",
    )

    assert recovered.status == "retryable_failed"
    assert recovered.failure_stage == "generator"
    stored_call = ledger.connection.execute(
        "SELECT status, actual_microusd, provider_response_id FROM provider_calls "
        "WHERE provider_call_id=?",
        (call.provider_call_id,),
    ).fetchone()
    assert dict(stored_call) == {
        "status": "succeeded",
        "actual_microusd": 29_841,
        "provider_response_id": "resp_captured",
    }
    totals = ledger.get_cost_totals(run_id)
    assert totals.actual_microusd == 29_841
    assert totals.open_reserved_microusd == 0
    replayed = ledger.recover_captured_selector_response(
        provider_call_id=call.provider_call_id,
        actual_microusd=29_841,
        input_tokens=5_176,
        output_tokens=303,
        provider_response_id="resp_captured",
    )
    assert replayed == recovered
    assert ledger.get_cost_totals(run_id).actual_microusd == 29_841
    ledger.connection.execute(
        "UPDATE work_units SET status='permanent_failed', "
        "last_error='provider_output_repair:invalid_provider_response:evidence quote', "
        "failure_stage='generator', terminal_code='provider_permanent' "
        "WHERE work_unit_id=?",
        (unit.work_unit_id,),
    )
    ledger.connection.commit()

    rehydrated = ledger.recover_captured_selector_response(
        provider_call_id=call.provider_call_id,
        actual_microusd=29_841,
        input_tokens=5_176,
        output_tokens=303,
        provider_response_id="resp_captured",
    )

    assert rehydrated.status == "permanent_failed"
    assert rehydrated.failure_stage == "generator"
    assert ledger.get_cost_totals(run_id).actual_microusd == 29_841


def test_rollback_unstarted_captured_resume_restores_retryable_attempt_two(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    unit = ledger.create_work_unit(
        run_id,
        ordinal=1,
        plan={"ordinal": 1},
        prompt_bundle_hash="prompt-v1",
    )
    ledger.connection.execute(
        "UPDATE work_units SET status=?, attempts=?, last_error=?, failure_stage=? "
        "WHERE work_unit_id=?",
        (
            "running",
            3,
            "captured_provider_response_recovered",
            "generator",
            unit.work_unit_id,
        ),
    )
    ledger.connection.commit()

    rolled_back = ledger.rollback_unstarted_captured_resume(unit.work_unit_id)

    assert rolled_back.status == "retryable_failed"
    assert rolled_back.attempts == 2
    assert rolled_back.failure_stage == "generator"


def test_captured_generator_replay_preserves_one_cost_and_resumes_reviewer(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    unit = ledger.create_work_unit(
        run_id,
        ordinal=1,
        plan={"ordinal": 1},
        prompt_bundle_hash="prompt-v1",
    )
    ledger.claim(unit.work_unit_id)
    ledger.connection.execute(
        "UPDATE work_units SET attempts=3 WHERE work_unit_id=?",
        (unit.work_unit_id,),
    )
    ledger.connection.commit()
    call = ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id=unit.work_unit_id,
        stage="generator",
        attempt=3,
        model="gpt-stub",
        request_hash="b" * 64,
        reserved_microusd=100_000,
    )
    ledger.reconcile_provider_call(
        provider_call_id=call.provider_call_id,
        status="succeeded",
        actual_microusd=7_328,
        input_tokens=2_193,
        output_tokens=123,
        provider_response_id="resp_captured_generator",
        error_code="invalid_response",
    )
    ledger.record_failure(
        unit.work_unit_id,
        error="provider_output_repair:invalid_provider_response:evidence quote mismatch",
        retryable=False,
        failure_stage="generator",
    )

    reopened = ledger.reopen_captured_generator_response(
        provider_call_id=call.provider_call_id,
        actual_microusd=7_328,
        input_tokens=2_193,
        output_tokens=123,
        provider_response_id="resp_captured_generator",
    )

    assert reopened.status == "running"
    assert reopened.attempts == 3
    during_replay = ledger.get_cost_totals(run_id)
    assert during_replay.actual_microusd == 0
    assert during_replay.open_reserved_microusd == 100_000

    ledger.reconcile_provider_call(
        provider_call_id=call.provider_call_id,
        status="succeeded",
        actual_microusd=7_328,
        input_tokens=2_193,
        output_tokens=123,
        provider_response_id="resp_captured_generator",
    )
    resumed = ledger.complete_captured_generator_replay(call.provider_call_id)

    assert resumed.status == "retryable_failed"
    assert resumed.attempts == 2
    assert resumed.last_error == "captured_generator_response_recovered"
    assert resumed.failure_stage == "reviewer"
    final_totals = ledger.get_cost_totals(run_id)
    assert final_totals.actual_microusd == 7_328
    assert final_totals.open_reserved_microusd == 0
    stored_call = ledger.provider_call(call.provider_call_id)
    assert stored_call.status == "succeeded"


def test_requeue_post_auth_contract_failures_routes_only_two_safe_repairs(tmp_path) -> None:
    ledger = AutomationLedger(tmp_path / "ledger.sqlite3")
    run_id = _live_run(ledger)
    errors = (
        "provider_output_repair:candidate_contract_invalid:"
        "multi document_scope requires 2-3 unique documents",
        "provider_output_repair:invalid_provider_response:"
        "Type D requires zero evidence claims",
    )
    units = []
    for ordinal, error in enumerate(errors, start=1):
        unit = ledger.create_work_unit(
            run_id,
            ordinal=ordinal,
            plan={"ordinal": ordinal},
            prompt_bundle_hash="prompt-v1",
        )
        ledger.claim(unit.work_unit_id)
        ledger.connection.execute(
            "UPDATE work_units SET attempts=2 WHERE work_unit_id=?",
            (unit.work_unit_id,),
        )
        ledger.connection.commit()
        selector = ledger.reserve_provider_call(
            run_id=run_id,
            work_unit_id=unit.work_unit_id,
            stage="selector",
            attempt=1,
            model="gpt-stub",
            request_hash="c" * 64,
            reserved_microusd=100_000,
        )
        ledger.reconcile_provider_call(
            provider_call_id=selector.provider_call_id,
            status="released",
            error_code="selector_provider_http_401",
        )
        successful_selector = ledger.reserve_provider_call(
            run_id=run_id,
            work_unit_id=unit.work_unit_id,
            stage="selector",
            attempt=2,
            model="gpt-stub",
            request_hash="e" * 64,
            reserved_microusd=100_000,
        )
        ledger.reconcile_provider_call(
            provider_call_id=successful_selector.provider_call_id,
            status="succeeded",
            actual_microusd=5_000,
            input_tokens=50,
            output_tokens=10,
            provider_response_id=f"resp_selector_{ordinal}",
        )
        generator = ledger.reserve_provider_call(
            run_id=run_id,
            work_unit_id=unit.work_unit_id,
            stage="generator",
            attempt=2,
            model="gpt-stub",
            request_hash="d" * 64,
            reserved_microusd=100_000,
        )
        ledger.reconcile_provider_call(
            provider_call_id=generator.provider_call_id,
            status="succeeded",
            actual_microusd=10_000,
            input_tokens=100,
            output_tokens=20,
            provider_response_id=f"resp_{ordinal}",
        )
        ledger.record_failure(
            unit.work_unit_id,
            error=error,
            retryable=False,
            failure_stage="generator",
        )
        units.append(unit)

    before = ledger.get_cost_totals(run_id)
    requeued = ledger.requeue_post_auth_contract_failures(run_id, limit=2)

    assert [unit.work_unit_id for unit in requeued] == [
        unit.work_unit_id for unit in units
    ]
    assert [unit.status for unit in requeued] == [
        "retryable_failed",
        "retryable_failed",
    ]
    assert [unit.attempts for unit in requeued] == [1, 1]
    assert [unit.failure_stage for unit in requeued] == ["selector", "generator"]
    assert [unit.last_error.split(":", 2)[1] for unit in requeued] == [
        "multi_document_scope_requires_two_documents",
        "type_d_requires_zero_evidence_claims",
    ]
    after = ledger.get_cost_totals(run_id)
    assert after == before

    multi_unit = units[0]
    ledger.connection.execute(
        "UPDATE work_units SET status='permanent_failed', attempts=2, "
        "last_error='provider_output_repair:candidate_contract_invalid:"
        "multi document_scope requires 2-3 unique documents', "
        "failure_stage='generator', terminal_code='provider_permanent' "
        "WHERE work_unit_id=?",
        (multi_unit.work_unit_id,),
    )
    ledger.connection.commit()
    second_selector = ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id=multi_unit.work_unit_id,
        stage="selector",
        attempt=2,
        model="gpt-stub",
        request_hash="f" * 64,
        reserved_microusd=100_000,
    )
    ledger.reconcile_provider_call(
        provider_call_id=second_selector.provider_call_id,
        status="succeeded",
        actual_microusd=5_000,
        input_tokens=50,
        output_tokens=10,
        provider_response_id="resp_selector_multi_repair",
    )

    generator_repair = ledger.requeue_post_auth_contract_failures(run_id, limit=1)

    assert [unit.work_unit_id for unit in generator_repair] == [
        multi_unit.work_unit_id
    ]
    assert generator_repair[0].attempts == 1
    assert generator_repair[0].failure_stage == "generator"
    assert (
        generator_repair[0].last_error.split(":", 2)[1]
        == "multi_document_generator_requires_two_documents"
    )
