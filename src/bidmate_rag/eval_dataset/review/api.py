"""Loopback-only FastAPI surface for the local reviewer."""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from .db import ApprovalBlockedError, ReviewConflictError
from .repository import ReviewRepository
from .schemas import (
    DecisionRequest,
    DraftRequest,
    ResolveAnchorRequest,
    ResumeRequest,
)
from .sessions import ReviewSession

SESSION_COOKIE = "bidmate_review_session"
CSRF_COOKIE = "bidmate_review_csrf"
SESSION_SECONDS = 8 * 60 * 60


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, ReviewConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ApprovalBlockedError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (PermissionError, ValueError)):
        return HTTPException(status_code=422, detail=str(error))
    raise error


def create_review_app(repository: ReviewRepository) -> FastAPI:
    app = FastAPI(title="BidMate local evaluation reviewer", docs_url=None, redoc_url=None)

    def require_session(
        request: Request,
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> ReviewSession:
        session_token = request.cookies.get(SESSION_COOKIE)
        if not session_token:
            raise HTTPException(status_code=401, detail="local review session is required")
        csrf_cookie = request.cookies.get(CSRF_COOKIE)
        if (
            not csrf_cookie
            or not x_csrf_token
            or not hmac.compare_digest(csrf_cookie, x_csrf_token)
        ):
            raise HTTPException(status_code=403, detail="csrf token mismatch")
        try:
            return repository.sessions.validate(session_token, csrf_cookie)
        except PermissionError as error:
            status = 403 if "csrf" in str(error).lower() else 401
            raise HTTPException(status_code=status, detail=str(error)) from error

    @app.post("/api/session", status_code=201)
    def create_session(request: Request, response: Response) -> dict:
        session_token = request.cookies.get(SESSION_COOKIE)
        csrf_token = request.cookies.get(CSRF_COOKIE)
        session = None
        if session_token and csrf_token:
            try:
                session = repository.sessions.validate(session_token, csrf_token)
            except PermissionError:
                session = None
        if session is None:
            session = repository.sessions.create()
            response.set_cookie(
                SESSION_COOKIE,
                session.session_token,
                max_age=SESSION_SECONDS,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
            response.set_cookie(
                CSRF_COOKIE,
                session.csrf_token,
                max_age=SESSION_SECONDS,
                httponly=False,
                samesite="strict",
                secure=False,
                path="/",
            )
        return {"session_id": session.session_id, "expires_at": session.expires_at.isoformat()}

    @app.delete("/api/session", status_code=204)
    def delete_session(
        request: Request,
        response: Response,
        _: ReviewSession = Depends(require_session),
    ) -> None:
        repository.sessions.revoke(request.cookies[SESSION_COOKIE])
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")

    @app.get("/api/packages")
    @app.get("/api/packages/candidates", include_in_schema=False)
    def list_packages() -> list[dict]:
        return repository.discover_packages()

    @app.post("/api/packages/{package_id}/import")
    def import_package(
        package_id: str,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.import_discovered(package_id, actor_session_id=session.session_id)
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/datasets")
    def list_datasets() -> list[dict]:
        return repository.list_datasets()

    @app.get("/api/datasets/{dataset_id}/items")
    def list_items(
        dataset_id: str,
        status: str | None = None,
        sop_type: str | None = None,
        difficulty: str | None = None,
        document_id: str | None = None,
        blocking_reason: str | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict:
        try:
            return repository.query_items(
                dataset_id,
                status=status,
                sop_type=sop_type,
                difficulty=difficulty,
                document_id=document_id,
                blocking_reason=blocking_reason,
                page=page,
                page_size=page_size,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/datasets/{dataset_id}/resume")
    def get_resume(dataset_id: str) -> dict:
        try:
            return repository.get_resume(dataset_id)
        except Exception as error:
            raise _http_error(error) from error

    @app.put("/api/datasets/{dataset_id}/resume")
    def set_resume(
        dataset_id: str,
        request: ResumeRequest,
        _: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.set_resume(dataset_id, request.item_id, request.anchor_id)
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/items/{item_id}")
    def get_item(item_id: str) -> dict:
        try:
            return repository.get_item(item_id)
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/items/{item_id}/audit")
    def list_audit(item_id: str) -> list[dict]:
        try:
            return repository.list_audit(item_id)
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/items/{item_id}/snapshots")
    def list_snapshots(item_id: str) -> list[dict]:
        try:
            return repository.list_snapshots(item_id)
        except Exception as error:
            raise _http_error(error) from error

    @app.put("/api/items/{item_id}/draft")
    def save_draft(
        item_id: str,
        request: DraftRequest,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.save_draft(
                item_id,
                base_revision=request.base_revision,
                patch=request.patch,
                actor_session_id=session.session_id,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.post("/api/items/{item_id}/anchors/{anchor_id}/resolve")
    def resolve_anchor(
        item_id: str,
        anchor_id: str,
        request: ResolveAnchorRequest,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.resolve_anchor(
                item_id,
                anchor_id,
                base_revision=request.base_revision,
                method=request.method,
                bbox=request.bbox,
                selected_quote=request.selected_quote,
                page_number=request.page_number,
                actor_session_id=session.session_id,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.post("/api/items/{item_id}/anchors/{anchor_id}/auto-resolve")
    def auto_resolve_anchor(
        item_id: str,
        anchor_id: str,
        request: DecisionRequest,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.auto_resolve_anchor(
                item_id,
                anchor_id,
                base_revision=request.base_revision,
                actor_session_id=session.session_id,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/datasets/{dataset_id}/documents/{document_id}/pdf")
    def stream_pdf(dataset_id: str, document_id: str) -> FileResponse:
        try:
            path = repository.verified_pdf_path(dataset_id, document_id)
            return FileResponse(path, media_type="application/pdf", filename=path.name)
        except Exception as error:
            raise _http_error(error) from error

    @app.post("/api/items/{item_id}/approve")
    def approve(
        item_id: str,
        request: DecisionRequest,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.approve(
                item_id,
                base_revision=request.base_revision,
                actor_session_id=session.session_id,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.post("/api/items/{item_id}/fork")
    def fork(
        item_id: str,
        request: DecisionRequest,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.fork(
                item_id,
                base_revision=request.base_revision,
                actor_session_id=session.session_id,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.post("/api/items/{item_id}/reject")
    def reject(
        item_id: str,
        request: DecisionRequest,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            return repository.reject(
                item_id,
                base_revision=request.base_revision,
                reason=request.reason,
                actor_session_id=session.session_id,
            )
        except Exception as error:
            raise _http_error(error) from error

    @app.post("/api/datasets/{dataset_id}/exports", status_code=201)
    def export(
        dataset_id: str,
        session: ReviewSession = Depends(require_session),
    ) -> dict:
        try:
            exported = repository.export_legacy(
                dataset_id,
                actor_session_id=session.session_id,
            )
            return exported.as_dict()
        except Exception as error:
            raise _http_error(error) from error

    @app.get("/api/exports/{export_id}")
    def get_export(export_id: str) -> dict:
        try:
            return repository.get_export(export_id)
        except Exception as error:
            raise _http_error(error) from error

    return app


def _default_data_root() -> Path:
    return Path(__file__).resolve().parents[4].parent / "data"


app = create_review_app(
    ReviewRepository(
        os.environ.get(
            "BIDMATE_EVAL_REVIEW_DB",
            "artifacts/eval_dataset/rebuild/review/review.sqlite3",
        ),
        package_root=os.environ.get(
            "BIDMATE_EVAL_PACKAGE_ROOT",
            "artifacts/eval_dataset/rebuild/verification/n8n-mock/automation",
        ),
        pdf_root=os.environ.get("BIDMATE_EVAL_REVIEW_PDF_ROOT", _default_data_root() / "PDF1"),
        export_root=os.environ.get(
            "BIDMATE_EVAL_EXPORT_ROOT",
            "artifacts/eval_dataset/rebuild/review/exports",
        ),
    )
)
