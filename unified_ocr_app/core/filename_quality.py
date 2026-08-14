"""Plausibilitaetspruefung fuer LLM-erzeugte Dateinamen-Titel.

Der Analyseschritt liefert gelegentlich keinen Titel, sondern rohen
OCR-Auswurf aus Tabellenspalten.  Solche Zeichenketten wurden bisher nur von
unzulaessigen Windows-Zeichen befreit und danach als dauerhafter Archivname
festgeschrieben, zum Beispiel::

    19052026_BRP<bullet>r`` Pflegeversicherungsbrutto 5512,50 66150,00 PGS
    19052026_ILA _ rText 1.' Betrag EUR 1Jahreswerte PA9 _; Pflegeve

Die Heuristik erkennt genau dieses Muster.  Sie ist bewusst konservativ: sie
verwirft nur bei mehreren unabhaengigen Auffaelligkeiten, damit gueltige Titel
wie ``Kassenbon_Herdweg-Apotheke_18052026_Rechnung`` erhalten bleiben.
"""

from __future__ import annotations

import re


# Geldbetraege gehoeren in den Dokumentinhalt, nicht in den Titel.
_AMOUNT = re.compile(r"\d{1,3}(?:[.\s]\d{3})*[.,]\d{2}\b")
# Zeichen, die praktisch nur aus fehlerhafter Glyphenerkennung stammen.
_SUSPICIOUS_CHARACTERS = frozenset("•`´^~|<>\\;")
_PUNCTUATION_RUN = re.compile(r"[.\-_,;:]{3,}")
_DIGIT_LETTER_BREAK = re.compile(r"(?:\d[A-Za-zÄÖÜäöüß]|[A-Za-zÄÖÜäöüß]\d)")
# "1Betrag", "1Jahreswerte": typischer Spalten-Bleed aus Tabellen-OCR.
_LEADING_DIGIT_WORD = re.compile(r"(?<![\w])\d[A-Za-zÄÖÜäöüß]{3,}")

NOISE_THRESHOLD = 3


def score_filename_title(title: str) -> tuple[int, list[str]]:
    """Bewertet einen Titel; je hoeher der Wert, desto wahrscheinlicher OCR-Muell."""
    text = str(title or "").strip()
    if not text:
        return NOISE_THRESHOLD, ["Titel ist leer."]

    score = 0
    reasons: list[str] = []

    if _AMOUNT.search(text):
        score += 2
        reasons.append("Enthaelt einen Geldbetrag.")
    if any(character in _SUSPICIOUS_CHARACTERS for character in text):
        score += 2
        reasons.append("Enthaelt Zeichen, die typisch fuer Fehlerkennung sind.")
    if _PUNCTUATION_RUN.search(text):
        score += 2
        reasons.append("Enthaelt eine Kette aus Satzzeichen.")

    leading_digit_words = _LEADING_DIGIT_WORD.findall(text)
    if leading_digit_words:
        score += 2
        reasons.append(
            f"Ziffer direkt vor Wortanfang ({len(leading_digit_words)}x), "
            "typisch fuer verrutschte Tabellenspalten."
        )

    letters = sum(character.isalpha() for character in text)
    dense = [character for character in text if not character.isspace()]
    ratio = letters / len(dense) if dense else 0.0
    if ratio < 0.72:
        score += 1
        reasons.append(f"Nur {ratio:.0%} Buchstaben.")

    if len(_DIGIT_LETTER_BREAK.findall(text)) >= 2:
        score += 1
        reasons.append("Mehrfach Ziffern und Buchstaben ohne Trennung.")

    tokens = [token for token in re.split(r"[\s_]+", text) if token]
    fragments = sum(1 for token in tokens if len(token) <= 2 and not token.isdigit())
    if fragments >= 2:
        score += 1
        reasons.append(f"{fragments} zusammenhanglose Kurzfragmente.")

    return score, reasons


def looks_like_ocr_noise(title: str) -> bool:
    """True, wenn der Titel nicht als Archivname taugt."""
    return score_filename_title(title)[0] >= NOISE_THRESHOLD


def usable_filename_title(title: str) -> tuple[str, list[str]]:
    """Gibt den Titel zurueck, oder einen leeren String samt Begruendungen."""
    score, reasons = score_filename_title(title)
    if score >= NOISE_THRESHOLD:
        return "", reasons
    return str(title or "").strip(), []
