from __future__ import annotations

import json
from pathlib import Path

WORKFLOWS = {
    "bidmate_eval_generate_v1.json": [
        "Manual Trigger",
        "Batch Config",
        "Worker Health",
        "Worker Compatible",
        "Create or Resume Run",
        "Run Authorized",
        "Inventory Documents",
        "Input Gate",
        "Plan Work Units",
        "Expand Work Units",
        "Loop One Unit",
        "Execute Process Unit",
        "Batch Gate",
        "Batch Pass",
        "Finalize Package",
        "Sanitized Summary",
        "Done",
    ],
    "bidmate_eval_process_work_unit_v1.json": [
        "Execute Workflow Trigger",
        "Validate Unit Contract",
        "Claim Work Unit",
        "Claim Granted",
        "Build Selector Request",
        "Selector Mode",
        "Mock Selector",
        "Live Selector",
        "Normalize Selector",
        "Lock Evidence",
        "Evidence Gate",
        "Build Generator Request",
        "Generator Mode",
        "Mock Generator",
        "Live Generator",
        "Normalize Generator",
        "Deterministic Gates",
        "Gate Result",
        "Build Reviewer Request",
        "Reviewer Mode",
        "Mock Reviewer",
        "Live Reviewer",
        "Normalize Reviewer",
        "Record Terminal Outcome",
    ],
    "bidmate_eval_retry_failed_v1.json": [
        "Manual Trigger",
        "Retry Settings",
        "Worker Health",
        "Find Retryable Units",
        "Retry Eligibility Gate",
        "Loop Retry Unit",
        "Execute Process Unit",
        "Retry Summary",
    ],
}


def _load(name: str) -> dict:
    return json.loads((Path("n8n/workflows") / name).read_text(encoding="utf-8"))


def test_n8n_workflows_have_exact_approved_node_manifests() -> None:
    for name, expected_names in WORKFLOWS.items():
        workflow = _load(name)
        assert workflow["settings"]["executionOrder"] == "v1"
        actual_names = [node["name"] for node in workflow["nodes"]]
        assert actual_names == expected_names
        assert set(workflow["connections"]) <= set(actual_names)


def test_n8n_process_graph_exposes_three_isomorphic_mode_branches() -> None:
    workflow = _load("bidmate_eval_process_work_unit_v1.json")
    names = {node["name"]: node for node in workflow["nodes"]}
    for stage in ("Selector", "Generator", "Reviewer"):
        assert names[f"{stage} Mode"]["type"] == "n8n-nodes-base.switch"
        assert names[f"Mock {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert names[f"Live {stage}"]["type"] != "n8n-nodes-base.noOp"
        assert f"Normalize {stage}" in workflow["connections"]
    serialized = json.dumps(workflow, ensure_ascii=False).lower()
    assert "/v1/testing/" not in serialized
    assert "credentials" not in serialized


def test_n8n_workflows_are_loopback_only_and_have_no_fake_content() -> None:
    for name in WORKFLOWS:
        serialized = json.dumps(_load(name), ensure_ascii=False)
        lowered = serialized.lower()
        assert "http://127.0.0.1:8121" in serialized
        assert "mock question" not in lowered
        assert "mock/rfp.pdf" not in lowered
        assert "https://" not in lowered


def _targets(workflow: dict, node_name: str, output_index: int = 0) -> list[str]:
    outputs = workflow["connections"].get(node_name, {}).get("main", [])
    if output_index >= len(outputs):
        return []
    return [connection["node"] for connection in outputs[output_index]]


def test_main_workflow_preserves_batch_input_and_runs_every_unit_before_finalize() -> None:
    workflow = _load("bidmate_eval_generate_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert "Batch Config" in nodes["Create or Resume Run"]["parameters"]["jsonBody"]
    assert nodes["Create or Resume Run"]["parameters"]["options"]["timeout"] >= 600_000
    assert nodes["Loop One Unit"]["typeVersion"] == 3
    assert _targets(workflow, "Loop One Unit", 0) == ["Batch Gate"]
    assert _targets(workflow, "Loop One Unit", 1) == ["Execute Process Unit"]
    assert _targets(workflow, "Execute Process Unit") == ["Loop One Unit"]
    assert (
        nodes["Execute Process Unit"]["parameters"]["workflowId"]["value"]
        == "bidmate-eval-process-v1"
    )


def test_process_workflow_has_executable_mock_stage_chain_and_failure_terminals() -> None:
    workflow = _load("bidmate_eval_process_work_unit_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    expected_urls = {
        "Claim Work Unit": "/v1/workflow/claim-work-unit",
        "Mock Selector": "/v1/workflow/mock-selector",
        "Lock Evidence": "/v1/workflow/lock-evidence",
        "Mock Generator": "/v1/workflow/mock-generator",
        "Deterministic Gates": "/v1/workflow/deterministic-gates",
        "Mock Reviewer": "/v1/workflow/mock-reviewer",
        "Record Terminal Outcome": "/v1/workflow/record-terminal-outcome",
    }
    for name, suffix in expected_urls.items():
        assert nodes[name]["parameters"]["url"].endswith(suffix)
    for stage in ("Selector", "Generator", "Reviewer"):
        assert nodes[f"Build {stage} Request"]["type"] == "n8n-nodes-base.noOp"
        assert nodes[f"Normalize {stage}"]["type"] == "n8n-nodes-base.noOp"
        assert _targets(workflow, f"Mock {stage}") == [f"Normalize {stage}"]
        assert _targets(workflow, f"Live {stage}") == [f"Normalize {stage}"]
    assert "Record Terminal Outcome" in _targets(workflow, "Claim Granted", 1)
    assert "Record Terminal Outcome" in _targets(workflow, "Evidence Gate", 1)
    assert "Record Terminal Outcome" in _targets(workflow, "Gate Result", 1)


def test_retry_workflow_keeps_run_id_and_reuses_fixed_process_workflow() -> None:
    workflow = _load("bidmate_eval_retry_failed_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert "Retry Settings" in nodes["Find Retryable Units"]["parameters"]["url"]
    assert nodes["Loop Retry Unit"]["typeVersion"] == 3
    assert (
        nodes["Execute Process Unit"]["parameters"]["workflowId"]["value"]
        == "bidmate-eval-process-v1"
    )
    assert _targets(workflow, "Execute Process Unit") == ["Loop Retry Unit"]


def test_set_nodes_use_the_assignments_compatible_n8n_version() -> None:
    for name in WORKFLOWS:
        workflow = _load(name)
        for node in workflow["nodes"]:
            if node["type"] == "n8n-nodes-base.set":
                assert node["typeVersion"] >= 3


def test_control_flow_nodes_use_parameter_schema_compatible_versions() -> None:
    required_versions = {
        "n8n-nodes-base.if": 2.3,
        "n8n-nodes-base.switch": 3.4,
        "n8n-nodes-base.executeWorkflow": 1.3,
    }
    for name in WORKFLOWS:
        workflow = _load(name)
        for node in workflow["nodes"]:
            if node["type"] in required_versions:
                assert node["typeVersion"] == required_versions[node["type"]]


def test_main_batch_config_contains_only_worker_run_contract_fields() -> None:
    workflow = _load("bidmate_eval_generate_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assignments = nodes["Batch Config"]["parameters"]["assignments"]["assignments"]
    assert {assignment["name"] for assignment in assignments} == {
        "batch_id",
        "target_count",
        "mode",
        "max_items_per_call",
    }


def test_mock_runner_uses_real_batch_retry_finalize_and_idempotence_contract() -> None:
    script = Path("scripts/test_eval_automation_mock.ps1").read_text(encoding="utf-8")
    lowered = script.lower()
    assert "artifacts\\eval_dataset\\rebuild" in lowered
    assert "artifacts\\eval_dataset\\test" not in lowered
    assert "bidmate_eval_simulate_transient_failure" in lowered
    assert "batch_config.json" in lowered
    assert "publish:workflow" in lowered
    assert "--id=bidmate-eval-generate-v1" in lowered
    assert "--id=bidmate-eval-retry-v1" in lowered
    assert "retry_count" in lowered
    assert "manifesthashstable" in lowered
    assert "duplicateprovidercalls" in lowered
    assert "foreach ($id in $ids)" not in script
