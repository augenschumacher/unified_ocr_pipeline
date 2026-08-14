"""Tests fuer die Plausibilitaetspruefung von Dateinamen-Titeln.

Das Korpus stammt aus einem echten Archiv: die guten Namen aus final/, die
schlechten aus Ordnern, die eine aeltere Version erzeugt hatte.
"""
import pytest

from core.filename_quality import (
    looks_like_ocr_noise,
    score_filename_title,
    usable_filename_title,
)
from core.pipeline import PipelineOrchestrator


OCR_NOISE_TITLES = [
    "A. .... rrext Betrag EUR Jahreswerte BZV_ .- usatzverso",
    "BRP•r`` Pflegeversicherungsbrutto 5512,50 66150,00 PGS ",
    "ILA _ rText 1.' Betrag EUR 1Jahreswerte PA9 _; Pflegeve",
    "Verdienstabrechnung LA rrext 1Betrag EUR 1Jahreswerte P",
]

VALID_TITLES = [
    "Team Leistungsrecht",
    "Katholisches Klinikum Bochum",
    "Mrtversicherung ab Geburt",
    "Post muss nach Hause geschickt werden Katholisches Klin",
    "Teilnahmebescheinigung_IVOM_Therapie",
    "Datenschutzinformation_Vita34",
    "Kassenbon_Herdweg-Apotheke_18052026_Rechnung",
    "Geburtsurkunde_Charlotte_Maria_Schumacher",
    "Antrag_elektronischer_Arztausweis",
    "Charlottenklinik für Augenheilkunde",
    "Lohnabrechnung Mai 2026",
    "Rechnung 2024-05-12",
    "Vertrag Nr 4711",
]


@pytest.mark.parametrize("title", OCR_NOISE_TITLES)
def test_ocr_noise_titles_are_detected(title):
    score, reasons = score_filename_title(title)
    assert looks_like_ocr_noise(title), f"{title!r} -> {score} {reasons}"


@pytest.mark.parametrize("title", VALID_TITLES)
def test_valid_titles_are_kept(title):
    score, reasons = score_filename_title(title)
    assert not looks_like_ocr_noise(title), f"{title!r} -> {score} {reasons}"
    assert usable_filename_title(title)[0] == title


def test_empty_title_is_rejected():
    assert usable_filename_title("")[0] == ""


def test_final_name_drops_noise_and_reports_it():
    rejected = []
    name = PipelineOrchestrator._final_name_from_metadata(
        {
            "document_date": "19052026",
            "filename_title": "BRP•r`` Pflegeversicherungsbrutto 5512,50 66150,00 PGS ",
            "document_type": "Lohnabrechnung",
        },
        rejected=rejected,
    )

    assert name == "19052026_Lohnabrechnung"
    assert len(rejected) == 1
    assert rejected[0]["reasons"]


def test_final_name_keeps_a_good_title():
    rejected = []
    name = PipelineOrchestrator._final_name_from_metadata(
        {
            "document_date": "12-05-2026",
            "filename_title": "Rechnung Stammzelldepot",
            "document_type": "Rechnung",
        },
        rejected=rejected,
    )

    assert name == "12-05-2026_Rechnung_Stammzelldepot_Rechnung"
    assert rejected == []


def test_noise_title_without_document_type_falls_back():
    rejected = []
    name = PipelineOrchestrator._final_name_from_metadata(
        {"document_date": "19052026", "filename_title": "A. .... rrext BZV_ .- usatzverso"},
        rejected=rejected,
    )

    assert name == "19052026_dokument"
    assert len(rejected) == 1
