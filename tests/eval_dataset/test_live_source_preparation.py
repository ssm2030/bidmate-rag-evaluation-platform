from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path("scripts/prepare_live_eval_poc.py")
    spec = importlib.util.spec_from_file_location("prepare_live_eval_poc", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolver_selects_named_pdf_attachment() -> None:
    module = _module()
    html = '<a href="/files/notice.pdf">Notice PDF</a><a href="/files/rfp.pdf">Request for proposal PDF</a>'

    resolved = module.resolve_attachment(
        base_url="https://example.go.kr/notices/1",
        html=html,
        expected_attachment_name="Request for proposal",
    )

    assert resolved == "https://example.go.kr/files/rfp.pdf"


def test_resolver_deduplicates_same_attachment_and_skips_preview_action() -> None:
    module = _module()
    html = """
    <a href="/common/board/Download.do?fileNo=2">제안요청서.pdf</a>
    <a href="https://www.nia.or.kr/common/board/Download.do?fileNo=2">제안요청서.pdf-다운로드</a>
    <a href="javascript:goPreview()">제안요청서.pdf-미리보기</a>
    """

    resolved = module.resolve_attachment(
        base_url="https://www.nia.or.kr/notices/1",
        html=html,
        expected_attachment_name="제안요청서",
    )

    assert resolved == "https://www.nia.or.kr/common/board/Download.do?fileNo=2"


def test_download_accepts_pdf_magic_with_generic_binary_content_type() -> None:
    module = _module()

    module.validate_pdf_payload(b"%PDF-1.7\npublic", "application/octet-stream")


def test_download_rejects_non_pdf_content() -> None:
    module = _module()

    with pytest.raises(module.InvalidPdfPayload):
        module.validate_pdf_payload(b"<html>login required</html>", "text/html")


def test_runtime_paths_are_under_artifacts_live_poc_source(tmp_path) -> None:
    module = _module()
    output_root = tmp_path / "artifacts" / "live_poc" / "source"

    paths = module.runtime_paths(output_root, repository_root=tmp_path)

    assert paths.pdf_root == output_root / "PDF1"
    assert paths.parsed_root == output_root / "Parsed"
    assert paths.manifest_path == output_root / "runtime-manifest.json"


def test_runtime_paths_reject_output_outside_repository_artifact_root(tmp_path) -> None:
    module = _module()
    outside = tmp_path / "other" / "artifacts" / "live_poc" / "source"

    with pytest.raises(ValueError, match="repository artifact path"):
        module.runtime_paths(outside, repository_root=tmp_path)


def test_runtime_manifest_keeps_page_text_and_contact_values_out(tmp_path) -> None:
    module = _module()
    record = module.runtime_manifest_record(
        source_id="example",
        organization="Public agency",
        source_page_url="https://example.go.kr/notices/1",
        resolved_attachment_url="https://example.go.kr/files/rfp.pdf",
        pdf_sha256="a" * 64,
        page_count=1,
        parsed_file="Parsed/example.json",
        pdf_file="PDF1/example.pdf",
    )

    assert "page_text" not in record
    assert "010-1234-5678" not in str(record)



def test_canonical_live_source_manifest_matches_confirmed_public_pages() -> None:
    payload = json.loads(
        Path("configs/eval_live_sources.json").read_text(encoding="utf-8")
    )
    sources = payload["sources"]

    assert len(sources) == 12
    assert [source["source_id"] for source in sources] == [
        "nia-ai-norms-2026",
        "nia-public-ai-governance-2026",
        "nia-public-data-survey-2026",
        "nia-digital-development-2026",
        "nia-digital-ethics-web-2026",
        "nia-ai-digital-competency-2025",
        "bok-cbdc-security-2022",
        "bok-fx-review-upgrade-2023",
        "bok-statistics-operations-2023",
        "bok-gyeonggi-pmo-2024",
        "nia-digital-ethics-instructors-2026",
        "nia-public-cloud-policy-2025",
    ]
    assert [source["source_page_url"] for source in sources] == [
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=29609&cbIdx=78336",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=29460&cbIdx=78336",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=29250&cbIdx=78336",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=29176&cbIdx=78336",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=28763&cbIdx=78336",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=28114&cbIdx=78336",
        "https://www.bok.or.kr/portal/bbs/P0000561/view.do?menuNo=200037&nttId=10070073",
        "https://www.bok.or.kr/portal/bbs/P0000561/view.do?menuNo=200037&nttId=10080379",
        "https://www.bok.or.kr/portal/bbs/P0000561/view.do?menuNo=200037&nttId=10081480",
        "https://www.bok.or.kr/portal/bbs/P0000561/view.do?menuNo=200037&nttId=10083157",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=29191&cbIdx=78336",
        "https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?bcIdx=28054&cbIdx=78336",
    ]
    assert all(source["expected_attachment_name"] == "제안요청서" for source in sources)
    assert all("source_page_url" in source for source in sources)
    assert all("direct_pdf_url" not in source for source in sources)
    assert all("requires_manual_provenance_check" not in source for source in sources)


def test_network_allowlist_matches_confirmed_manifest_hosts() -> None:
    module = _module()

    assert module.ALLOWED_HOSTS == {
        "www.nia.or.kr",
        "www.bok.or.kr",
        "file-cdn.bok.or.kr",
    }


def _write_source_config(tmp_path: Path, *, manual: bool = False) -> Path:
    sources = [
        {
            "source_id": f"source-{index:02d}",
            "organization": "Public agency",
            "source_page_url": f"https://www.nia.or.kr/notice/{index}",
            "expected_attachment_name": "Request for proposal",
            "document_kind": "RFP",
            **({"requires_manual_provenance_check": True} if manual and index == 0 else {}),
        }
        for index in range(12)
    ]
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps({"schema_version": 1, "sources": sources}),
        encoding="utf-8",
    )
    return path


def test_source_ids_must_be_filename_safe_slugs(tmp_path) -> None:
    module = _module()
    config = _write_source_config(tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["sources"][0]["source_id"] = "../escape"
    config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="filename-safe slugs"):
        module.load_sources(config)


def test_resolver_rejects_duplicate_named_pdf_attachments() -> None:
    module = _module()
    html = (
        '<a href="/files/rfp-one.pdf">Request for proposal PDF</a>'
        '<a href="/files/rfp-two.pdf">Request for proposal PDF</a>'
    )

    with pytest.raises(ValueError, match="exactly one PDF link"):
        module.resolve_attachment(
            base_url="https://www.nia.or.kr/notice/1",
            html=html,
            expected_attachment_name="Request for proposal",
        )


def test_malformed_pdf_is_rejected_during_parse() -> None:
    module = _module()

    with pytest.raises(module.InvalidPdfPayload, match="could not be parsed"):
        module._extract_pages(b"%PDF-1.4\nmalformed")


def test_prepare_sources_records_redirect_and_writes_atomic_manifest(
    tmp_path, monkeypatch
 ) -> None:
    module = _module()
    source_config = _write_source_config(tmp_path)
    output_root = tmp_path / "artifacts" / "live_poc" / "source"
    pdf_bytes = b"%PDF-1.4\nloopback"

    def fake_fetch(url: str):
        if "/notice/" in url:
            html = b'<a href="/files/rfp.pdf">Request for proposal PDF</a>'
            return html, "text/html", "https://www.nia.or.kr/final/notice"
        return pdf_bytes, "application/pdf", "https://www.nia.or.kr/cdn/final-rfp.pdf"

    monkeypatch.setattr(module, "_fetch", fake_fetch)
    monkeypatch.setattr(
        module,
        "_extract_pages",
        lambda payload: [{"page_num": 1, "text": "public page text"}],
    )

    paths = module.prepare_sources(
        source_config=source_config,
        output_root=output_root,
        target_count=12,
        repository_root=tmp_path,
    )

    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["sources"]) == 12
    assert manifest["sources"][0]["parsed_file"] == "Parsed/source_00.json"
    assert manifest["sources"][0]["pdf_file"] == "PDF1/source_00.pdf"
    batch_config = json.loads(paths.batch_config_path.read_text(encoding="utf-8"))
    assert batch_config[0]["files"][0] == "source_00.json"
    assert manifest["sources"][0]["resolved_attachment_url"] == (
        "https://www.nia.or.kr/cdn/final-rfp.pdf"
    )
    assert "public page text" not in json.dumps(manifest)
    assert all(record["public_provenance_checked"] for record in manifest["sources"])
    assert len(list(paths.pdf_root.glob("*.pdf"))) == 12
    assert len(list(paths.parsed_root.glob("*.json"))) == 12


def test_prepare_sources_cleans_staging_on_partial_failure(tmp_path, monkeypatch) -> None:
    module = _module()
    source_config = _write_source_config(tmp_path)
    output_root = tmp_path / "artifacts" / "live_poc" / "source"
    calls = 0

    def failing_fetch(url: str):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise RuntimeError("deterministic fetch failure")
        if "/notice/" in url:
            html = b'<a href="/files/rfp.pdf">Request for proposal PDF</a>'
            return html, "text/html", url
        return b"%PDF-1.4\nloopback", "application/pdf", url

    monkeypatch.setattr(module, "_fetch", failing_fetch)
    monkeypatch.setattr(
        module,
        "_extract_pages",
        lambda payload: [{"page_num": 1, "text": "public page text"}],
    )

    with pytest.raises(RuntimeError, match="deterministic fetch failure"):
        module.prepare_sources(
            source_config=source_config,
            output_root=output_root,
            target_count=12,
            repository_root=tmp_path,
        )

    assert not output_root.exists()
    assert list(output_root.parent.glob(".source.staging-*")) == []


def test_manual_provenance_stops_before_any_fetch(tmp_path, monkeypatch) -> None:
    module = _module()
    source_config = _write_source_config(tmp_path, manual=True)
    output_root = tmp_path / "artifacts" / "live_poc" / "source"
    fetched = False

    def forbidden_fetch(url: str):
        nonlocal fetched
        fetched = True
        raise AssertionError("network fetch must not start")

    monkeypatch.setattr(module, "_fetch", forbidden_fetch)

    with pytest.raises(ValueError, match="manual provenance"):
        module.prepare_sources(
            source_config=source_config,
            output_root=output_root,
            target_count=12,
            repository_root=tmp_path,
        )

    assert fetched is False
    assert not output_root.exists()


def test_page_text_quality_enforces_empty_page_ratio() -> None:
    module = _module()
    accepted = [
        {"page_num": 1, "text": "content"},
        {"page_num": 2, "text": "content"},
        {"page_num": 3, "text": "content"},
        {"page_num": 4, "text": "content"},
        {"page_num": 5, "text": ""},
    ]
    empty_count, empty_ratio = module.validate_page_text_quality(accepted)
    assert empty_count == 1
    assert empty_ratio == pytest.approx(0.2)

    rejected = [*accepted[:-1], {"page_num": 5, "text": ""}, {"page_num": 6, "text": ""}]
    with pytest.raises(ValueError, match="empty-page ratio"):
        module.validate_page_text_quality(rejected)


def test_bok_official_cdn_redirect_is_allowed(monkeypatch) -> None:
    module = _module()
    followed: list[str] = []

    def would_follow(self, req, fp, code, msg, headers, newurl):
        followed.append(newurl)
        return object()

    monkeypatch.setattr(module.HTTPRedirectHandler, "redirect_request", would_follow)
    handler = module._AllowlistedRedirectHandler()
    request = module.Request("https://www.bok.or.kr/notices/1")
    cdn_url = "https://file-cdn.bok.or.kr/portal/example/proposal.pdf?token=public"

    result = handler.redirect_request(request, None, 302, "Found", {}, cdn_url)

    assert result is not None
    assert followed == [cdn_url]


def test_redirect_target_is_allowlisted_before_redirect_handler_follows(monkeypatch) -> None:
    module = _module()
    followed: list[str] = []

    def would_follow(self, req, fp, code, msg, headers, newurl):
        followed.append(newurl)
        return object()

    monkeypatch.setattr(module.HTTPRedirectHandler, "redirect_request", would_follow)
    handler = module._AllowlistedRedirectHandler()
    request = module.Request("https://www.nia.or.kr/notices/1")

    with pytest.raises(ValueError, match="approved HTTPS host"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.invalid/private.pdf",
        )
    assert followed == []
