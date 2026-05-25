import re
import logging

logger = logging.getLogger("UnifiedOCR")

class QualityChecker:
    @staticmethod
    def normalize_amount(amount_str: str) -> str:
        """Normalisiert einen Geldbetrag für den Vergleich (z.B. 1.234,56 -> 1234,56)"""
        s = amount_str.replace(".", "").replace(" ", "").replace("−", "-")
        if s.startswith("+"):
            s = s[1:]
        return s

    @classmethod
    def extract_amounts(cls, text: str) -> set:
        """Extrahiert Geldbeträge im Format 123,45 oder 1.234,56"""
        pattern = r"-?\b\d{1,3}(?:\.\d{3})*,\d{2}\b"
        found = re.findall(pattern, text)
        return {cls.normalize_amount(a) for a in found}

    @staticmethod
    def extract_dates(text: str) -> set:
        """Extrahiert Datumsangaben im Format DD.MM.YYYY"""
        pattern = r"\b\d{2}\.\d{2}\.\d{4}\b"
        return set(re.findall(pattern, text))

    @staticmethod
    def extract_la_codes(text: str) -> set:
        """Extrahiert typische Lohnarten-Codes (3-4 stellige Ziffern am Zeilenanfang oder isoliert)"""
        # Wir suchen nach 3-4 stelligen Ziffern, die oft als Lohnarten- oder Buchungsschlüssel dienen
        pattern = r"\b\d{3,4}\b"
        # Um Rauschen (wie Jahreszahlen) zu reduzieren, filtern wir Werte im Bereich 1900-2100 heraus
        candidates = re.findall(pattern, text)
        return {c for c in candidates if not (1900 <= int(c) <= 2100)}

    @classmethod
    def run_quality_check(cls, ocr_text: str, docling_markdown: str, vision_markdown: str, fused_text: str) -> dict:
        warnings = []
        missing_values = []
        severity = "info"

        # 1. Beträge vergleichen
        ocr_amounts = cls.extract_amounts(ocr_text)
        docling_amounts = cls.extract_amounts(docling_markdown)
        vision_amounts = cls.extract_amounts(vision_markdown)
        
        # Alle Beträge aus den Quellkanälen zusammenführen
        source_amounts = ocr_amounts.union(docling_amounts).union(vision_amounts)
        fused_amounts = cls.extract_amounts(fused_text)
        
        missing_amounts = source_amounts - fused_amounts
        # Rück-Übersetzung der normalisierten Beträge in lesbare Beträge zur Anzeige
        all_found_raw = re.findall(r"-?\b\d{1,3}(?:\.\d{3})*,\d{2}\b", ocr_text + " " + docling_markdown + " " + vision_markdown)
        raw_map = {cls.normalize_amount(a): a for a in all_found_raw}

        if missing_amounts:
            for ma in sorted(missing_amounts):
                readable = raw_map.get(ma, ma)
                warnings.append(f"Geldbetrag fehlt im Enddokument: {readable} EUR")
                missing_values.append({"type": "amount", "value": readable})
            severity = "warning"

        # 2. Datumsangaben vergleichen
        source_dates = cls.extract_dates(ocr_text).union(cls.extract_dates(docling_markdown)).union(cls.extract_dates(vision_markdown))
        fused_dates = cls.extract_dates(fused_text)
        
        missing_dates = source_dates - fused_dates
        if missing_dates:
            for md in sorted(missing_dates):
                warnings.append(f"Datum fehlt im Enddokument: {md}")
                missing_values.append({"type": "date", "value": md})
            if severity != "error":
                severity = "warning"

        # 3. Lohnarten-Codes (LA-Codes) vergleichen
        source_la = cls.extract_la_codes(ocr_text).union(cls.extract_la_codes(docling_markdown))
        fused_la = cls.extract_la_codes(fused_text)
        
        missing_la = source_la - fused_la
        if missing_la:
            # Nur als Warnung, falls es sich um Gehaltsabrechnungen handelt (heuristisch geprüft)
            is_payroll = any(kw in (ocr_text + fused_text).lower() for kw in ["abrechnung", "gehalt", "lohn", "verdienst"])
            if is_payroll:
                for mla in sorted(missing_la):
                    warnings.append(f"Möglicher Lohnarten-Code (LA) fehlt: {mla}")
                    missing_values.append({"type": "la_code", "value": mla})
                severity = "warning"

        # 4. Ziffern-Dichte vergleichen
        source_digits_count = sum(c.isdigit() for c in ocr_text)
        fused_digits_count = sum(c.isdigit() for c in fused_text)
        
        digit_ratio = 1.0
        if source_digits_count > 0:
            digit_ratio = fused_digits_count / source_digits_count
            if digit_ratio < 0.80:
                warnings.append(f"Hoher Ziffernverlust im Enddokument! Nur {digit_ratio:.1%} der Ziffern aus der OCR erhalten ({fused_digits_count} von {source_digits_count}).")
                severity = "error"

        # 5. Tabellenzeilen-Verlust prüfen
        source_table_lines = sum(1 for line in docling_markdown.splitlines() if "|" in line)
        fused_table_lines = sum(1 for line in fused_text.splitlines() if "|" in line)
        
        if source_table_lines > 5 and fused_table_lines < (source_table_lines * 0.7):
            warnings.append(f"Verlust von Tabellenzeilen vermutet: Quell-Markdown hat {source_table_lines} Zeilen mit Tabellen-Trennern '|', das fusionierte Dokument hat nur {fused_table_lines}.")
            if severity != "error":
                severity = "warning"

        logger.info(f"Qualitätskontrolle abgeschlossen. Severity: {severity}, Warnungen: {len(warnings)}")
        
        return {
            "severity": severity,
            "warnings": warnings,
            "missing_values": missing_values,
            "metrics": {
                "source_digits": source_digits_count,
                "fused_digits": fused_digits_count,
                "digit_ratio": round(digit_ratio, 3),
                "source_table_lines": source_table_lines,
                "fused_table_lines": fused_table_lines
            }
        }
