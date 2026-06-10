from pathlib import Path
from unittest.mock import MagicMock, patch

from core.cloud.synology_client import SynologyWebDAVClient, is_private_webdav_url
from core.config import AppConfig
from core.ocr.page_extractor import order_text_blocks, split_text_into_packets
from core.pipeline import PipelineOrchestrator


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeWebDAVSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PROPFIND":
            return FakeResponse(404)
        if method == "MKCOL":
            return FakeResponse(201)
        if method == "PUT":
            return FakeResponse(201)
        return FakeResponse(500, "unexpected")


def test_synology_webdav_upload_creates_folders_and_puts_file(tmp_path):
    local_file = tmp_path / "bericht.pdf"
    local_file.write_bytes(b"pdf")
    session = FakeWebDAVSession()
    client = SynologyWebDAVClient(
        base_url="https://nas.local:5006",
        username="user",
        password="secret",
        root_path="OCR Archiv",
        session=session,
    )

    result = client.upload_file(local_file, "Fabio/Gesundheit")

    methods = [call[0] for call in session.calls]
    assert methods.count("MKCOL") == 2
    assert methods[-1] == "PUT"
    assert result["provider"] == "synology_webdav"
    assert result["folder_path"] == "Fabio/Gesundheit"
    assert result["remote_path"] == "Fabio/Gesundheit/bericht.pdf"
    assert "OCR%20Archiv/Fabio/Gesundheit/bericht.pdf" in session.calls[-1][1]


def test_private_webdav_url_detection():
    assert is_private_webdav_url("https://nas.local:5006")
    assert is_private_webdav_url("https://192.168.1.10:5006")
    assert is_private_webdav_url("https://diskstation:5006")
    assert not is_private_webdav_url("https://example.com/webdav")


def test_order_text_blocks_reads_two_columns_left_then_right():
    blocks = [
        {"x0": 40, "y0": 20, "x1": 560, "y1": 45, "text": "Kapitel 1"},
        {"x0": 45, "y0": 80, "x1": 250, "y1": 110, "text": "links oben"},
        {"x0": 45, "y0": 130, "x1": 250, "y1": 160, "text": "links unten"},
        {"x0": 330, "y0": 80, "x1": 550, "y1": 110, "text": "rechts oben"},
        {"x0": 330, "y0": 130, "x1": 550, "y1": 160, "text": "rechts unten"},
    ]

    ordered = order_text_blocks(blocks, page_width=600, page_height=800)

    assert [block["text"] for block in ordered] == [
        "Kapitel 1",
        "links oben",
        "links unten",
        "rechts oben",
        "rechts unten",
    ]


def test_split_text_into_packets_preserves_paragraphs():
    packets = split_text_into_packets("A\n\nB\n\nC\n\nD", 2)
    assert packets == ["A\n\nB", "C\n\nD"]


def test_pipeline_synology_upload_returns_audit_entries(tmp_path):
    pdf = tmp_path / "out.pdf"
    docx = tmp_path / "out.docx"
    report = tmp_path / "out.json"
    for path in (pdf, docx, report):
        path.write_text(path.suffix, encoding="utf-8")

    orch = PipelineOrchestrator(
        config=AppConfig(tmp_path),
        llm_client=MagicMock(),
        synology_enabled=True,
        synology_base_url="https://nas.local:5006",
        synology_username="user",
        synology_password="secret",
        synology_upload_pdf=True,
        synology_upload_docx=True,
        synology_upload_json=True,
    )

    client = MagicMock()
    client.is_configured = True
    client.upload_file.side_effect = [
        {"provider": "synology_webdav", "filename": "out.pdf", "remote_path": "Fabio/out.pdf"},
        {"provider": "synology_webdav", "filename": "out.docx", "remote_path": "Fabio/out.docx"},
        {"provider": "synology_webdav", "filename": "out.json", "remote_path": "Fabio/out.json"},
    ]

    with patch("core.cloud.synology_client.SynologyWebDAVClient", return_value=client):
        uploads = orch._stage_synology_upload(
            pdf_file=pdf,
            docx_file=docx,
            json_file=report,
            target_path="Fabio",
        )

    assert [entry["filename"] for entry in uploads] == ["out.pdf", "out.docx", "out.json"]
    assert all(entry["provider"] == "synology_webdav" for entry in uploads)
