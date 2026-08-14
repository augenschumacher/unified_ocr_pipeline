from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import customtkinter as ctk
import pytest

from core.review_service import ReviewQueueService
from ui import review_queue
from ui.review_queue import (
    ReviewQueueWindow,
    flagged_metadata_fields,
    grouped_review_reasons,
    review_item_sort_key,
)
from ui.pdf_preview import PDFPreviewFrame


def _review_item(item_id: int = 42, *, created_at: str = "2026-07-20T12:00:00+00:00") -> dict:
    return {
        "id": item_id,
        "job_id": f"job-{item_id}",
        "status": "staged",
        "kind": "ocr_quality",
        "source_name": f"Dokument-{item_id}.pdf",
        "created_at": created_at,
        "proposed_path": "Finanzen/Rechnungen",
        "metadata": {
            "document_date": "2026-07-20",
            "document_type": "Rechnung",
            "title": "Testrechnung",
            "issuer": "Beispiel GmbH",
            "recipient": "Testempfaenger",
            "amount": "19.99",
            "currency": "EUR",
            "tags": ["test", "rechnung"],
        },
        "payload": {"fused_text": "Erkannter Testtext"},
        "quality": {
            "quality_status": "review",
            "quality_score": 74,
            "metadata_evidence": {
                "fields": {"title": {"status": "unverified"}},
                "unverified_fields": ["amount"],
            },
            "review_reasons": [
                {"code": "metadata_values_unverified"},
                {"code": "missing_amount"},
            ],
        },
    }


class FakeService:
    def __init__(self, items: list[dict] | None = None):
        self.config = SimpleNamespace()
        self.items = list(items if items is not None else [_review_item()])
        self.resolve_calls: list[tuple[int, str, dict]] = []

    def list_open(self, limit: int = 200) -> list[dict]:
        return self.items[:limit]

    @staticmethod
    def review_readiness(_item: dict) -> tuple[bool, str]:
        return True, ""

    @staticmethod
    def known_paths() -> list[str]:
        return ["Finanzen/Rechnungen", "Sonstiges"]

    @staticmethod
    def preview_path(_item: dict) -> None:
        return None

    @staticmethod
    def original_path(_item: dict) -> None:
        return None

    def resolve(self, item_id: int, chosen_path: str, **kwargs) -> dict:
        self.resolve_calls.append((item_id, chosen_path, kwargs))
        return {"target_path": chosen_path}


class ImmediateThread:
    """Run the review worker synchronously while retaining the Thread API."""

    def __init__(self, *, target, daemon: bool = False, **_kwargs):
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.target()


class FakeControl:
    def __init__(self, value=""):
        self.value = value
        self.options = {}

    def get(self, *_args):
        return self.value

    def configure(self, **kwargs):
        self.options.update(kwargs)


@pytest.fixture(scope="module")
def ctk_root():
    try:
        root = ctk.CTk()
        root.withdraw()
        root.update_idletasks()
    except (tk.TclError, RuntimeError) as exc:
        pytest.skip(f"Kein nutzbares Tk-Display: {exc}")
    try:
        yield root
    finally:
        try:
            for child in root.winfo_children():
                child.destroy()
            root.update_idletasks()
            root.destroy()
        except tk.TclError:
            pass


def _open_window(root, service: FakeService) -> ReviewQueueWindow:
    return ReviewQueueWindow(
        root,
        service,
        sync_runner_factory=lambda: SimpleNamespace(
            gdrive_enabled=False,
            synology_enabled=False,
        ),
        sync_artifacts=lambda *_args: None,
    )


def _walk_widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk_widgets(child)


def test_review_helpers_group_reasons_flag_fields_and_sort_newest_first():
    item = _review_item()
    item["quality"]["review_reasons"].append({"code": "missing_amount"})

    assert flagged_metadata_fields(item) == {"title", "amount"}

    reasons = grouped_review_reasons(item)
    assert reasons[0] == "Markierte Angaben prüfen: Titel, Betrag"
    assert "Betrag konnte nicht sicher erkannt werden (2×)" in reasons
    assert not any("metadata_values_unverified" in reason for reason in reasons)

    items = [
        _review_item(2, created_at="2026-07-19T12:00:00+00:00"),
        _review_item(3, created_at="2026-07-20T12:00:00+00:00"),
        _review_item(4, created_at="2026-07-20T12:00:00+00:00"),
    ]
    ordered = sorted(items, key=review_item_sort_key, reverse=True)
    assert [item["id"] for item in ordered] == [4, 3, 2]


def test_review_readiness_rejects_missing_artifacts(monkeypatch):
    service = object.__new__(ReviewQueueService)
    monkeypatch.setattr(service, "_artifact_items", lambda _item, require_all=True: [])

    ready, reason = service.review_readiness(_review_item())

    assert ready is False
    assert "keine wiederherstellbaren Dateien" in reason


def test_review_readiness_accepts_complete_matching_manifest(monkeypatch, tmp_path: Path):
    item = _review_item()
    pdf_path = tmp_path / "archival.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
    manifest_path = tmp_path / "test_job_manifest.json"
    manifest_path.write_text(json.dumps({"job_id": item["job_id"]}), encoding="utf-8")

    service = object.__new__(ReviewQueueService)
    monkeypatch.setattr(
        service,
        "_artifact_items",
        lambda _item, require_all=True: [
            ("archival_pdf", pdf_path),
            ("job_manifest", manifest_path),
        ],
    )
    monkeypatch.setattr(service, "_review_requires_manifest", lambda _item: True)

    assert service.review_readiness(item) == (True, "")


def test_ctk_queue_open_close_reopen_has_no_nested_or_blank_toplevel(ctk_root):
    service = FakeService()

    first = _open_window(ctk_root, service)
    ctk_root.update()
    toplevels = [
        widget for widget in _walk_widgets(ctk_root) if isinstance(widget, ctk.CTkToplevel)
    ]
    assert toplevels == [first]
    assert first.winfo_viewable()

    first.request_close()
    ctk_root.update_idletasks()
    assert not first.winfo_exists()
    assert not any(
        isinstance(widget, ctk.CTkToplevel) for widget in _walk_widgets(ctk_root)
    )

    second = _open_window(ctk_root, service)
    ctk_root.update()
    toplevels = [
        widget for widget in _walk_widgets(ctk_root) if isinstance(widget, ctk.CTkToplevel)
    ]
    assert toplevels == [second]
    assert second.winfo_viewable()
    second.request_close()
    ctk_root.update_idletasks()
    assert not second.winfo_exists()


def test_editor_build_failure_destroys_partial_window(ctk_root, monkeypatch):
    original_label = review_queue.ctk.CTkLabel
    calls = {"count": 0}

    def fail_during_build(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulierter Widget-Fehler")
        return original_label(*args, **kwargs)

    monkeypatch.setattr(review_queue.ctk, "CTkLabel", fail_during_build)

    with pytest.raises(RuntimeError, match="simulierter Widget-Fehler"):
        _open_window(ctk_root, FakeService())

    ctk_root.update_idletasks()
    assert not any(
        isinstance(widget, ctk.CTkToplevel) for widget in _walk_widgets(ctk_root)
    )


def test_queue_has_exactly_one_primary_confirmation_and_no_checkbox(ctk_root):
    window = _open_window(ctk_root, FakeService())
    ctk_root.update_idletasks()
    widgets = list(_walk_widgets(window))

    assert not [widget for widget in widgets if isinstance(widget, ctk.CTkCheckBox)]
    confirmation_buttons = [
        widget
        for widget in widgets
        if isinstance(widget, ctk.CTkButton)
        and "bestätig" in str(widget.cget("text")).casefold()
    ]
    assert confirmation_buttons == [window._resolve_button]
    window.destroy()


def test_app_launcher_focuses_existing_queue_instead_of_opening_another():
    from app import App

    class ExistingWindow:
        presented = False

        @staticmethod
        def winfo_exists():
            return True

        def present(self):
            self.presented = True

    existing = ExistingWindow()
    owner = SimpleNamespace(_review_queue_window=existing)

    App._open_review_queue(owner)

    assert existing.presented is True
    assert owner._review_queue_window is existing


def test_primary_confirmation_resolves_with_explicit_quality_confirmation(
    ctk_root,
    monkeypatch,
):
    service = FakeService()
    monkeypatch.setattr(review_queue.threading, "Thread", ImmediateThread)
    window = _open_window(ctk_root, service)
    ctk_root.update_idletasks()

    window._resolve_button.invoke()
    ctk_root.update()

    assert len(service.resolve_calls) == 1
    item_id, chosen_path, kwargs = service.resolve_calls[0]
    assert item_id == 42
    assert chosen_path == "Finanzen/Rechnungen"
    assert kwargs["quality_confirmed"] is True
    assert kwargs["corrected_text"] == "Erkannter Testtext"
    assert kwargs["corrected_metadata"]["title"] == "Testrechnung"
    window.destroy()


def test_release_preview_documents_closes_all_nested_pdf_handles(monkeypatch):
    released: list[str] = []
    cancelled: list[str] = []

    class FakePreview:
        def __init__(self, name: str):
            self.name = name

        @staticmethod
        def winfo_children():
            return []

        def release_document(self):
            released.append(self.name)

    class FakeContainer:
        def __init__(self, children):
            self.children = list(children)

        def winfo_children(self):
            return list(self.children)

    original = FakePreview("original")
    ocr = FakePreview("ocr")
    owner = SimpleNamespace(
        _preview_host=FakeContainer([FakeContainer([original]), ocr]),
        _cancel_preview_jobs=lambda: cancelled.append("cancelled"),
    )
    monkeypatch.setattr(review_queue, "PDFPreviewFrame", FakePreview)

    ReviewQueueWindow._release_preview_documents(owner)

    assert cancelled == ["cancelled"]
    assert sorted(released) == ["ocr", "original"]


def test_pdf_preview_release_document_closes_real_handle_contract_idempotently():
    calls: list[object] = []

    class FakeDocument:
        is_closed = False

        def close(self):
            calls.append("doc.close")
            self.is_closed = True

    class FakeButton:
        def configure(self, **kwargs):
            calls.append(kwargs)

    owner = SimpleNamespace(
        _resize_job="resize-1",
        after_cancel=lambda job: calls.append(("after_cancel", job)),
        doc=FakeDocument(),
        _photo=object(),
        prev_btn=FakeButton(),
        next_btn=FakeButton(),
        search_status=FakeButton(),
    )
    owner._close_document = lambda: PDFPreviewFrame._close_document(owner)

    PDFPreviewFrame.release_document(owner)
    PDFPreviewFrame.release_document(owner)

    assert calls.count("doc.close") == 1
    assert ("after_cancel", "resize-1") in calls
    assert owner.doc is None
    assert owner._photo is None
    assert owner._resize_job is None


def test_primary_confirmation_releases_preview_before_service_resolve(
    ctk_root,
    monkeypatch,
):
    order: list[str] = []

    class OrderedService(FakeService):
        def resolve(self, item_id: int, chosen_path: str, **kwargs) -> dict:
            order.append("resolve")
            return super().resolve(item_id, chosen_path, **kwargs)

    service = OrderedService()
    monkeypatch.setattr(review_queue.threading, "Thread", ImmediateThread)
    window = _open_window(ctk_root, service)
    ctk_root.update_idletasks()
    monkeypatch.setattr(
        window,
        "_release_preview_documents",
        lambda: order.append("release"),
    )

    window._resolve_button.invoke()
    ctk_root.update()

    assert order[:2] == ["release", "resolve"]
    window.destroy()


def test_resolve_selected_headless_releases_preview_before_worker(monkeypatch):
    order: list[str] = []

    class OrderedService(FakeService):
        def resolve(self, item_id: int, chosen_path: str, **kwargs) -> dict:
            order.append("resolve")
            return {"target_path": chosen_path}

    service = OrderedService()
    owner = SimpleNamespace(
        _resolving=False,
        _selected_item=_review_item(),
        _readiness={42: (True, "")},
        _publish_to_root=False,
        _target_var=FakeControl("Finanzen/Rechnungen"),
        _text_box=FakeControl("Erkannter Testtext"),
        _note_box=FakeControl("geprüft"),
        _corrected_metadata=lambda: {"title": "Testrechnung"},
        _sync_runner_factory=lambda: SimpleNamespace(
            gdrive_enabled=False,
            synology_enabled=False,
        ),
        _sync_artifacts=lambda *_args: None,
        service=service,
        _original_fused_text="Erkannter Testtext",
        _resolve_button=FakeControl(),
        _reset_button=FakeControl(),
        _set_status=lambda *_args, **_kwargs: None,
        _release_preview_documents=lambda: order.append("release"),
        after=lambda _delay, callback, *args: callback(*args),
        winfo_exists=lambda: True,
        _resolve_button_text=lambda: "Bestätigen",
        refresh_items=lambda: order.append("refresh"),
        _dashboard_refresh=lambda: None,
    )
    monkeypatch.setattr(review_queue.threading, "Thread", ImmediateThread)

    ReviewQueueWindow.resolve_selected(owner)

    assert order == ["release", "resolve", "refresh"]
