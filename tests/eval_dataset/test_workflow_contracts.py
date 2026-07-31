from __future__ import annotations

import json
from pathlib import Path
from runpy import run_path

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
        "Resume Stage",
        "Prepare Selector",
        "Selector Mode",
        "Mock Selector",
        "OpenAI Selector",
        "Normalize Selector",
        "Lock Evidence",
        "Evidence Gate",
        "Prepare Generator",
        "Generator Mode",
        "Mock Generator",
        "OpenAI Generator",
        "Normalize Generator",
        "Deterministic Gates",
        "Gate Result",
        "Prepare Reviewer",
        "Reviewer Mode",
        "Mock Reviewer",
        "OpenAI Reviewer",
        "Normalize Reviewer",
        "Reviewer Decision",
        "Record Terminal Outcome",
        "Classify Selector Provider Error",
        "Classify Selector Normalize Error",
        "Classify Generator Provider Error",
        "Classify Generator Normalize Error",
        "Classify Reviewer Provider Error",
        "Classify Reviewer Normalize Error",
        "Record Provider Failure",
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
        assert names[f"Prepare {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert names[f"OpenAI {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert f"Normalize {stage}" in workflow["connections"]
    serialized = json.dumps(workflow, ensure_ascii=False).lower()
    assert "/v1/testing/" not in serialized



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


def test_live_provider_path_prepares_before_responses_and_normalizes_after() -> None:
    workflow = _load("bidmate_eval_process_work_unit_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    for stage in ("Selector", "Generator", "Reviewer"):
        assert nodes[f"Prepare {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert nodes[f"OpenAI {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert nodes[f"OpenAI {stage}"]["parameters"]["url"] == "={{ $json.provider_request.url }}"
        assert nodes[f"Normalize {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert _targets(workflow, f"Prepare {stage}") == [f"OpenAI {stage}"]
        assert _targets(workflow, f"OpenAI {stage}") == [f"Normalize {stage}"]
        assert workflow["connections"][f"OpenAI {stage}"]["main"][0][0]["index"] == 0
    serialized = json.dumps(workflow, ensure_ascii=False).lower()
    assert "@n8n/n8n-nodes-langchain.openai" not in serialized
def test_live_provider_failure_graph_classifies_error_outputs_and_uses_stub_credential() -> None:
    workflow = _load("bidmate_eval_process_work_unit_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    for stage in ("Selector", "Generator", "Reviewer"):
        provider = nodes[f"OpenAI {stage}"]
        normalizer = nodes[f"Normalize {stage}"]
        assert provider["onError"] == "continueErrorOutput"
        assert "credentials" not in provider
        assert provider["parameters"]["sendHeaders"] is True
        headers = provider["parameters"]["headerParameters"]["parameters"]
        header_values = {header["name"]: header["value"] for header in headers}
        assert header_values["Idempotency-Key"] == "={{ $json.idempotency_key }}"
        assert normalizer["onError"] == "continueErrorOutput"
        assert _targets(workflow, f"OpenAI {stage}", 0) == [f"Normalize {stage}"]
        assert _targets(workflow, f"OpenAI {stage}", 1) == [f"Classify {stage} Provider Error"]
        assert _targets(workflow, f"Normalize {stage}", 1) == [f"Classify {stage} Normalize Error"]
        for phase in ("Provider", "Normalize"):
            classifier = nodes[f"Classify {stage} {phase} Error"]
            assert classifier["type"] == "n8n-nodes-base.set"
            if phase == "Provider":
                assignments = classifier["parameters"]["assignments"]["assignments"]
                failure_value = next(
                    assignment["value"]
                    for assignment in assignments
                    if assignment["name"] == "failure_class"
                )
                assert "transient_server" in failure_value
                assert ">= 500" in failure_value
                assert "< 600" in failure_value
            assert _targets(workflow, classifier["name"]) == ["Record Provider Failure"]
    recorder = nodes["Record Provider Failure"]
    assert recorder["parameters"]["url"].endswith("/v1/provider-calls/{{ $json.provider_call_id }}/failure")
    assert "failure_class" in recorder["parameters"]["jsonBody"]





def test_main_workflow_preserves_batch_input_and_runs_every_unit_before_finalize() -> None:
    workflow = _load("bidmate_eval_generate_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert "Batch Config" in nodes["Create or Resume Run"]["parameters"]["jsonBody"]
    assert nodes["Create or Resume Run"]["parameters"]["options"]["timeout"] >= 600_000
    assert "Create or Resume Run" in nodes["Batch Gate"]["parameters"]["url"]
    assert ".first()" in nodes["Batch Gate"]["parameters"]["url"]
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
    expected_mock_targets = {
        "Selector": "Lock Evidence",
        "Generator": "Deterministic Gates",
        "Reviewer": "Record Terminal Outcome",
    }
    for stage, mock_target in expected_mock_targets.items():
        assert nodes[f"Prepare {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert nodes[f"Normalize {stage}"]["type"] == "n8n-nodes-base.httpRequest"
        assert _targets(workflow, f"Mock {stage}") == [mock_target]
        assert _targets(workflow, f"Prepare {stage}") == [f"OpenAI {stage}"]
        assert _targets(workflow, f"OpenAI {stage}") == [f"Normalize {stage}"]
        assert workflow["connections"][f"OpenAI {stage}"]["main"][0][0]["index"] == 0
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
    values = {assignment["name"]: assignment["value"] for assignment in assignments}
    assert set(values) == {
        "batch_id",
        "target_count",
        "mode",
        "max_items_per_call",
        "campaign_key",
        "data_root",
        "cost_limit_microusd",
        "live_authorized",
    }
    assert values["mode"] == "={{ $env.BIDMATE_EVAL_MODE || 'mock' }}"
    assert values["campaign_key"] == "={{ $env.BIDMATE_EVAL_CAMPAIGN_KEY || null }}"
    assert values["data_root"] == "={{ $env.BIDMATE_EVAL_DATA_ROOT || '' }}"
    assert values["cost_limit_microusd"] == "={{ Number($env.BIDMATE_EVAL_COST_LIMIT_MICROUSD || 0) }}"
    assert values["live_authorized"] == "={{ $env.BIDMATE_EVAL_LIVE_AUTHORIZED === 'true' }}"


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



def test_tracked_operator_path_exports_runtime_live_values_to_batch_config() -> None:
    workflow = _load("bidmate_eval_generate_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assignments = nodes["Batch Config"]["parameters"]["assignments"]["assignments"]
    values = {assignment["name"]: assignment["value"] for assignment in assignments}

    assert values["mode"] == "={{ $env.BIDMATE_EVAL_MODE || 'mock' }}"
    assert values["campaign_key"] == "={{ $env.BIDMATE_EVAL_CAMPAIGN_KEY || null }}"
    assert values["data_root"] == "={{ $env.BIDMATE_EVAL_DATA_ROOT || '' }}"
    assert values["target_count"] == "={{ Number($env.BIDMATE_EVAL_TARGET_COUNT || 30) }}"
    assert values["cost_limit_microusd"] == "={{ Number($env.BIDMATE_EVAL_COST_LIMIT_MICROUSD || 0) }}"
    assert values["live_authorized"] == "={{ $env.BIDMATE_EVAL_LIVE_AUTHORIZED === 'true' }}"

    script = Path("scripts/start_eval_tools.ps1").read_text(encoding="utf-8")
    assert '$env:BIDMATE_EVAL_CAMPAIGN_KEY = $CampaignKey.Trim()' in script
    assert '$env:BIDMATE_EVAL_DATA_ROOT = $dataRoot' in script
    assert '$env:BIDMATE_EVAL_TARGET_COUNT = [string]$TargetItems' in script
    assert '$env:BIDMATE_EVAL_COST_LIMIT_MICROUSD = [string][int]($HardCapUsd * 1000000)' in script
    assert '$env:BIDMATE_EVAL_LIVE_AUTHORIZED = [string]$LiveAuthorized.IsPresent.ToString().ToLowerInvariant()' in script
    assert "N8N_BLOCK_ENV_ACCESS_IN_NODE" not in script
    assert '$runtimeWorkflowRoot = Join-Path $runtimeRoot "workflows"' in script
    assert "Batch Config" in script
    assert 'import:workflow "--input=$runtimeGeneratePath"' in script
    assert "function Test-N8nWorkflowSet" in script
    assert "$needsWorkflowRefresh = $RefreshWorkflows.IsPresent -or" in script
    assert "workflow bootstrap verification failed" in script
    assert "live runtime manifest PDF hash drift detected" in script
    assert "pdf_sha256" in script
    assert "live runtime manifest files do not match Batch_config" in script


def test_launcher_executes_workflow_database_probe_over_stdin() -> None:
    script = Path("scripts/start_eval_tools.ps1").read_text(encoding="utf-8")

    assert "$probeCode | & $PythonPath - $DatabasePath" in script
    assert "& $PythonPath -c $probeCode $DatabasePath" not in script


def test_launcher_reads_parsed_runtime_json_as_strict_utf8() -> None:
    script = Path("scripts/start_eval_tools.ps1").read_text(encoding="utf-8")

    assert (
        "[IO.File]::ReadAllText($parsedPath, [Text.UTF8Encoding]::new($false, $true)) "
        "| ConvertFrom-Json"
    ) in script
    assert "Get-Content -Raw -LiteralPath $parsedPath | ConvertFrom-Json" not in script


def test_stage_modes_receive_and_route_the_canonical_work_unit_payload() -> None:
    workflow = _load("bidmate_eval_process_work_unit_v1.json")

    assert _targets(workflow, "Claim Granted", 0) == ["Resume Stage"]
    assert _targets(workflow, "Resume Stage", 0) == ["Selector Mode"]
    assert _targets(workflow, "Resume Stage", 1) == ["Generator Mode"]
    assert _targets(workflow, "Resume Stage", 2) == ["Reviewer Mode"]
    assert _targets(workflow, "Evidence Gate", 0) == ["Generator Mode"]
    assert _targets(workflow, "Gate Result", 0) == ["Reviewer Mode"]
    for stage in ("Selector", "Generator", "Reviewer"):
        assert _targets(workflow, f"{stage} Mode", 0) == [f"Mock {stage}"]
        assert _targets(workflow, f"{stage} Mode", 1) == [f"Prepare {stage}"]

def test_live_stub_harness_uses_isolated_dummy_credential_and_graph_failures() -> None:
    script = Path("scripts/test_eval_automation_live_stub.ps1").read_text(encoding="utf-8")
    stub = Path("tests/eval_dataset/live_responses_stub.py").read_text(encoding="utf-8")

    assert "httpHeaderAuth" in script
    assert "genericCredentialType" in script
    assert "Bearer stub-only" in script
    assert "$stubCredentialJson = '['" in script
    assert "import:credentials" in script
    assert "publish:workflow" in script
    assert "Set-StubScenarioPlan" in script
    assert "Set-RetryRunId" in script
    assert "Invoke-GraphRetry $rateState.run_id" in script
    assert "Record-ProviderFailure" not in script
    assert "Invoke-StubScenario" not in script
    assert 'request.headers.get("authorization") != "Bearer stub-only"' in stub
    assert "/scenario-plan" in stub
    assert "StreamingResponse" in stub
    assert "transient_server" in stub
    assert "Invoke-GraphRetry $serverState.run_id" in script


def test_live_stub_selects_unique_documents_and_claims_both_for_type_b() -> None:
    stage_output = run_path("tests/eval_dataset/live_responses_stub.py")["_stage_output"]
    windows = [
        {"window_id": "w-1", "document_id": "d-1", "text": "DemoProject01 alpha"},
        {"window_id": "w-1b", "document_id": "d-1", "text": "DemoProject01 later"},
        {"window_id": "w-2", "document_id": "d-2", "text": "DemoProject02 beta"},
    ]

    selected = stage_output("selector", json.dumps({"windows": windows}))
    selected_ids = [entry["window_id"] for entry in selected["selected_windows"]]
    assert selected_ids == ["w-1", "w-2"]

    selected_windows = [window for window in windows if window["window_id"] in selected_ids]
    generated = stage_output(
        "generator",
        json.dumps(
            {
                "windows": selected_windows,
                "sop_type": "B",
                "difficulty": "low",
            }
        ),
    )
    assert [claim["window_id"] for claim in generated["evidence_claims"]] == ["w-1", "w-2"]
    assert "DemoProject01" in generated["question"]
    assert "DemoProject02" in generated["question"]


def test_delivery_verifier_reports_amendment_identity_and_current_graph() -> None:
    script = Path("scripts/verify_eval_dataset_delivery.ps1").read_text(encoding="utf-8")

    assert "build-bidmate-live-eval-poc-amendment-04-xhigh-v06" in script
    assert "b8bfa69619480c0f99007a1f4fa253f74b56988b34dbd40c28bea8733b84710e" in script
    assert "APPROVED_IMPLEMENTATION_PACKET.amendment-04.json" in script
    assert "$manualArtifactsPresentBefore = Test-Path -LiteralPath $manualRoot" in script
    assert "$manualArtifactsPresentBefore -and -not (Test-Path -LiteralPath $manualRoot)" in script
    assert r'Test-Path -LiteralPath (Join-Path $root "artifacts\eval_dataset\manual")' not in script
    assert "$env:N8N_BLOCK_ENV_ACCESS_IN_NODE = 'false'" in script
    assert script.index("N8N_BLOCK_ENV_ACCESS_IN_NODE") < script.index("test_eval_automation_mock.ps1")
    assert "$previousN8nBlockEnvAccess" in script
    assert "Remove-Item Env:N8N_BLOCK_ENV_ACCESS_IN_NODE" in script
    assert "full verifier requires n8n environment access to remain blocked" in script
    assert script.index("Remove-Item Env:N8N_BLOCK_ENV_ACCESS_IN_NODE") < script.index("test_eval_automation_live_stub.ps1")
    assert script.index("Remove-Item Env:N8N_BLOCK_ENV_ACCESS_IN_NODE") < script.index("scripts\\start_eval_tools.ps1")
    assert '"n8n/workflows/bidmate_eval_process_work_unit_v1.json" = 33' in script
    assert "process = 33" in script
    assert "expected_prompt_count" in script
    assert "max_prompt_count" in script
    assert "forbidden_extensions" in script
    assert "max_file_bytes" in script
    assert "workflow contains tracked credentials" in script
    assert '@("--root", ".", "--scope", "worktree")' in script
    assert '@("--root", ".", "--scope", "objects")' in script
    assert "$allowedImmutablePublicationFindings" in script
    assert "build-bidmate-eval-tools-rebuild-xhigh-v06" not in script
    assert "d9cfbcb577c462645aa3c6f853952980dc42b57c05348e581f8302efc18532a3" not in script



def test_reviewer_repair_routes_once_back_to_generator() -> None:
    workflow = _load("bidmate_eval_process_work_unit_v1.json")
    nodes = {node["name"]: node for node in workflow["nodes"]}

    assert nodes["Reviewer Decision"]["type"] == "n8n-nodes-base.if"
    assert _targets(workflow, "Normalize Reviewer") == ["Reviewer Decision"]
    assert _targets(workflow, "Reviewer Decision", 0) == ["Prepare Generator"]
    assert _targets(workflow, "Reviewer Decision", 1) == ["Record Terminal Outcome"]
