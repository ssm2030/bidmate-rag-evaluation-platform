from __future__ import annotations

import inspect

from app import eval_ui


def test_streamlit_evaluation_ui_exposes_no_edit_upload_or_save_path() -> None:
    source = inspect.getsource(eval_ui)

    for forbidden in (
        "save_eval_set",
        "_render_edit_tab",
        "_render_question_form",
        "st.file_uploader",
        "eval_set_edited.csv",
        "upload_csv",
        "save_eval",
    ):
        assert forbidden not in source

    for retained in ("_render_run_tab", "_render_debug_tab", "_render_compare_tab"):
        assert retained in source
