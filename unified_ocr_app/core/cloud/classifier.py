import json
import logging

logger = logging.getLogger("UnifiedOCR")

def classify_document(fused_text: str, metadata: dict, known_paths: list, llm_client, valid_persons: list) -> dict:
    """
    Klassifiziert das Dokument mithilfe des LLM in eine Ordnerhierarchie.
    Bevorzugt bestehende Pfade, schlägt neue Pfade nur sehr konservativ vor.
    """
    # Erste 3000 Zeichen des Textes nutzen, um den Kontext nicht zu sprengen
    text_excerpt = fused_text[:3000]

    # Bekannte Pfade als formatierte Liste für das LLM
    paths_str = "\n".join(f"- {p}" for p in known_paths)
    persons_str = ", ".join(valid_persons) if valid_persons else "Sonstiges"

    sys_prompt = (
        "Du bist ein präziser Dokumenten-Klassifikator. Deine Aufgabe ist es, das Dokument in eine passende Ordnerhierarchie einzusortieren.\n\n"
        f"Die erlaubten Hauptordner (erste Pfad-Stufe) lauten:\n"
        f"{persons_str}\n\n"
        "STRIKTE REGELN:\n"
        "1. BEVORZUGE EXISTIERENDE PFADE: Versuche das Dokument exakt einem der bekannten Pfade zuzuordnen. "
        "Schlage einen neuen Pfad nur vor, wenn das Dokument thematisch in keinen der bestehenden Pfade passt.\n"
        "2. KEINE NEUEN HAUPTORDNER: Du darfst NIEMALS einen neuen Hauptordner (erste Stufe) erfinden. "
        f"Die erste Pfadstufe MUSS exakt eine der folgenden sein: {persons_str}.\n"
        "3. FORMAT FÜR NEUE PFADE: Falls du einen neuen Pfad vorschlagen musst, verwende das Format 'Hauptordner/Unterordner' (oder tiefer, z. B. 'Hauptordner/Unterordner/Detailordner'). "
        "Der 'Hauptordner' (erste Pfad-Stufe) muss aus der obigen Liste stammen. "
        "Die Unterordner sollten einzelne, kurze deutsche Wörter mit Großbuchstaben am Anfang sein (z. B. 'Finanzen', 'Hobby').\n"
        "4. JSON-AUSGABE: Antworte AUSSCHLIESSLICH im folgenden JSON-Format:\n"
        '{"recommended_path": "Hauptordner/Unterordner", "is_new": true/false}\n'
        "Setze 'is_new' nur auf true, wenn der empfohlene Pfad NICHT in der Liste der bekannten Pfade steht."
    )

    user_prompt = (
        f"Hier sind die bekannten Pfade:\n{paths_str}\n\n"
        f"Dokument-Metadaten:\n"
        f"- Datum: {metadata.get('date', 'unbekannt')}\n"
        f"- Titel/Inhalt: {metadata.get('subject', 'unbekannt')}\n"
        f"- Typ: {metadata.get('document_type', 'unbekannt')}\n"
        f"- Tags: {metadata.get('tags', 'unbekannt')}\n\n"
        f"Dokument-Text (Auszug):\n{text_excerpt}\n\n"
        "Klassifiziere das Dokument und antworte im JSON-Format."
    )

    # Wir nutzen das analysis_model, da dieses für Strukturierung/Analyse zuständig ist.
    # Falls das nicht konfiguriert ist, fusion_model als Fallback.
    model = llm_client.analysis_model or llm_client.fusion_model
    if not model or model == "Keins":
        logger.warning("Kein Modell für Klassifikation konfiguriert, verwende 'Sonstiges'")
        return {"recommended_path": "Sonstiges", "is_new": False}

    for attempt in range(2):
        try:
            logger.info(f"Führe Dokumentenklassifikation durch (Modell: {model})...")
            res = llm_client.query(
                model=model,
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                think=False,
                max_tokens=512
            )
            
            # Robustes JSON-Parsing
            start = res.find("{")
            end = res.rfind("}") + 1
            if start != -1 and end > start:
                parsed = json.loads(res[start:end])
                path = parsed.get("recommended_path", "Sonstiges").strip().replace("\\", "/")
                
                # Prüfen, ob der Pfad in known_paths enthalten ist (unabhängig von Groß-/Kleinschreibung des LLM, aber wir korrigieren es)
                matched_path = next((kp for kp in known_paths if kp.lower() == path.lower()), None)
                if matched_path:
                    return {"recommended_path": matched_path, "is_new": False}
                
                # Komponenten bereinigen und leere Einträge filtern
                parts = [p.strip() for p in path.split("/") if p.strip()]
                if not parts:
                    parts = ["Sonstiges"]
                
                # Pfad-Validierung (erste Stufe muss eine bekannte Person/Gruppe sein, case-insensitives Mapping)
                person_matched = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
                if person_matched:
                    parts[0] = person_matched
                else:
                    if "Sonstiges" in valid_persons:
                        parts[0] = "Sonstiges"
                    elif valid_persons:
                        parts[0] = valid_persons[0]
                    else:
                        parts[0] = "Sonstiges"
                
                # Single-level expansion: if only Hauptordner was recommended (and it is not "Sonstiges"), expand with "Sonstiges"
                if len(parts) == 1 and parts[0].lower() != "sonstiges":
                    parts.append("Sonstiges")
                    
                # Deep path truncation: limit path to depth 2 (Hauptordner/Unterordner)
                if len(parts) > 2:
                    parts = parts[:2]
                
                # Normalisierten Pfad zusammensetzen
                path = "/".join(parts)
                
                # Erneuter Abgleich gegen known_paths nach Normalisierung
                matched_path = next((kp for kp in known_paths if kp.lower() == path.lower()), None)
                if matched_path:
                    return {"recommended_path": matched_path, "is_new": False}
                
                # is_new setzen
                is_new = path not in known_paths
                return {"recommended_path": path, "is_new": is_new}
        except Exception as e:
            logger.error(f"Fehler bei Klassifikation (Versuch {attempt+1}): {e}")
            
    return {"recommended_path": "Sonstiges", "is_new": False}
