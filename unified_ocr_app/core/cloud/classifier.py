import json
import logging

from core.cloud.context_matcher import find_best_context_path, format_contexts_for_prompt, rank_context_paths

logger = logging.getLogger("UnifiedOCR")


def classify_document(
    fused_text: str,
    metadata: dict,
    known_paths: list,
    llm_client,
    valid_persons: list,
    path_contexts: dict | None = None,
    memory_candidates: list[dict] | None = None,
) -> dict:
    """Classify a document into a known folder path or a conservative new path proposal."""
    text_excerpt = (fused_text or "")[:3000]
    path_contexts = path_contexts or {}
    memory_candidates = memory_candidates or []

    candidates = _merge_candidates([
        *rank_context_paths(fused_text, metadata, known_paths, path_contexts),
        *memory_candidates,
    ], known_paths)

    context_match = find_best_context_path(fused_text, metadata, known_paths, path_contexts)
    if context_match:
        context_match["candidates"] = _merge_candidates(context_match.get("candidates", []) + candidates, known_paths)
        context_match.setdefault("confidence", context_match.get("score", 0))
        logger.info("Kontextbasierte Sortierung: %s", context_match.get("recommended_path"))
        return context_match

    memory_match = _best_auto_candidate(candidates)
    if memory_match:
        return _result_from_candidate(memory_match, candidates, reason=memory_match.get("reason", "memory"))

    paths_str = "\n".join(f"- {p}" for p in known_paths)
    persons_str = ", ".join(valid_persons) if valid_persons else "Sonstiges"
    contexts_str = format_contexts_for_prompt(path_contexts, known_paths)

    sys_prompt = (
        "Du bist ein präziser Dokumenten-Klassifikator. Deine Aufgabe ist es, das Dokument in eine passende Ordnerhierarchie einzusortieren.\n\n"
        f"Die erlaubten Hauptordner (erste Pfad-Stufe) lauten:\n{persons_str}\n\n"
        "STRIKTE REGELN:\n"
        "1. Bevorzuge existierende Pfade. Schlage neue Pfade nur vor, wenn kein bekannter Pfad passt.\n"
        "2. Nutze Sortier-Kontexte wie Aliase, Kennzeichen, Hobbys, Objektinfos und Stichworte bevorzugt, wenn sie im Dokument vorkommen.\n"
        "3. Erfinde niemals neue Hauptordner. Die erste Pfadstufe muss exakt aus der erlaubten Liste stammen.\n"
        "4. Neue Pfade dürfen differenziert sein, aber maximal vier Ebenen haben, z. B. Person/Auto/Golf/Service.\n"
        "5. Antworte ausschließlich als JSON:\n"
        '{"recommended_path": "Hauptordner/Unterordner", "is_new": true/false, "confidence": 0-100, "reason": "..."}'
    )

    user_prompt = (
        f"Bekannte Pfade:\n{paths_str}\n\n"
        f"Sortier-Kontexte:\n{contexts_str or '- keine'}\n\n"
        "Dokument-Metadaten:\n"
        f"- Datum: {metadata.get('date', 'unbekannt')}\n"
        f"- Titel/Inhalt: {metadata.get('subject') or metadata.get('title', 'unbekannt')}\n"
        f"- Typ: {metadata.get('document_type', 'unbekannt')}\n"
        f"- Tags: {metadata.get('tags', 'unbekannt')}\n\n"
        f"Dokument-Text (Auszug):\n{text_excerpt}\n\n"
        "Klassifiziere das Dokument und antworte im JSON-Format."
    )

    model = llm_client.analysis_model or llm_client.fusion_model
    if not model or model == "Keins":
        logger.warning("Kein Modell fuer Klassifikation konfiguriert.")
        return _fallback_result(candidates, known_paths)

    for attempt in range(2):
        try:
            logger.info("Fuehre Dokumentenklassifikation durch (Modell: %s)...", model)
            query = getattr(llm_client, "_query_with_privacy", llm_client.query)
            response = query(
                model,
                sys_prompt,
                user_prompt,
                think=False,
                max_tokens=512,
            )
            parsed = _parse_json_response(response)
            if not parsed:
                continue
            return _result_from_llm(parsed, candidates, known_paths, valid_persons)
        except Exception as exc:
            logger.error("Fehler bei Klassifikation (Versuch %s): %s", attempt + 1, exc)

    return _fallback_result(candidates, known_paths)


def _parse_json_response(response: str) -> dict | None:
    start = (response or "").find("{")
    end = (response or "").rfind("}") + 1
    if start == -1 or end <= start:
        return None
    return json.loads(response[start:end])


def _result_from_llm(parsed: dict, candidates: list[dict], known_paths: list, valid_persons: list) -> dict:
    path = str(parsed.get("recommended_path", "Sonstiges")).strip().replace("\\", "/")
    confidence = _parse_confidence(parsed, default=78)

    matched_path = next((kp for kp in known_paths if kp.lower() == path.lower()), None)
    if matched_path:
        candidate = _candidate(matched_path, confidence, "llm", False, parsed.get("reason") or parsed.get("begruendung"))
        merged = _merge_candidates([candidate, *candidates], known_paths)
        return {
            "recommended_path": matched_path,
            "is_new": False,
            "reason": "llm",
            "confidence": confidence,
            "candidates": merged[:5],
        }

    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        parts = ["Sonstiges"]

    person_matched = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
    if person_matched:
        parts[0] = person_matched
    elif "Sonstiges" in valid_persons:
        parts[0] = "Sonstiges"
    elif valid_persons:
        parts[0] = valid_persons[0]
    else:
        parts[0] = "Sonstiges"

    if len(parts) == 1 and parts[0].lower() != "sonstiges":
        parts.append("Sonstiges")
    if len(parts) > 4:
        parts = parts[:4]

    path = "/".join(parts)
    matched_path = next((kp for kp in known_paths if kp.lower() == path.lower()), None)
    if matched_path:
        candidate = _candidate(matched_path, confidence, "llm", False, parsed.get("reason") or parsed.get("begruendung"))
        merged = _merge_candidates([candidate, *candidates], known_paths)
        return {
            "recommended_path": matched_path,
            "is_new": False,
            "reason": "llm",
            "confidence": confidence,
            "candidates": merged[:5],
        }

    is_new = path not in known_paths
    score = 52 if is_new else confidence
    candidate = _candidate(path, score, "llm", is_new, parsed.get("reason") or parsed.get("begruendung"))
    merged = _merge_candidates([candidate, *candidates], known_paths)
    return {
        "recommended_path": path,
        "is_new": is_new,
        "reason": "llm",
        "confidence": score,
        "candidates": merged[:5],
    }


def _fallback_result(candidates: list[dict], known_paths: list) -> dict:
    if candidates:
        return _result_from_candidate(candidates[0], candidates, reason="fallback")
    return {
        "recommended_path": "Sonstiges",
        "is_new": "Sonstiges" not in known_paths,
        "reason": "fallback",
        "confidence": 0,
        "candidates": [],
    }


def _result_from_candidate(candidate: dict, candidates: list[dict], reason: str) -> dict:
    return {
        "recommended_path": candidate["path"],
        "is_new": bool(candidate.get("is_new", False)),
        "reason": reason,
        "score": candidate.get("score", 0),
        "confidence": candidate.get("score", 0),
        "evidence": candidate.get("evidence", []),
        "candidates": candidates[:5],
    }


def _candidate(path: str, score: int, reason: str, is_new: bool, evidence=None) -> dict:
    if isinstance(evidence, str):
        evidence = [evidence]
    return {
        "path": path,
        "score": max(0, min(int(score or 0), 100)),
        "reason": reason,
        "is_new": bool(is_new),
        "evidence": evidence or [],
    }


def _parse_confidence(parsed: dict, default: int = 78) -> int:
    for key in ("confidence", "score", "wahrscheinlichkeit"):
        value = parsed.get(key)
        if value is None:
            continue
        try:
            return max(0, min(int(float(value)), 100))
        except (TypeError, ValueError):
            continue
    return default


def _merge_candidates(candidates: list[dict], known_paths: list) -> list[dict]:
    known_lower = {str(path).lower(): path for path in known_paths or []}
    merged = {}
    for candidate in candidates or []:
        path = str(candidate.get("path") or candidate.get("recommended_path") or "").strip().replace("\\", "/")
        if not path:
            continue
        path = known_lower.get(path.lower(), path)
        score = max(0, min(int(candidate.get("score") or candidate.get("confidence") or 0), 100))
        existing = merged.get(path)
        if existing and existing["score"] >= score:
            existing["evidence"].extend(candidate.get("evidence") or [])
            existing["evidence"] = list(dict.fromkeys(existing["evidence"]))[:8]
            continue
        merged[path] = {
            "path": path,
            "score": score,
            "reason": candidate.get("reason", "candidate"),
            "is_new": path not in (known_paths or []),
            "evidence": list(dict.fromkeys(candidate.get("evidence") or []))[:8],
        }
    return sorted(merged.values(), key=lambda item: (item["score"], item["path"].count("/")), reverse=True)


def _best_auto_candidate(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    best = candidates[0]
    runner_up = candidates[1]["score"] if len(candidates) > 1 else 0
    if best["score"] >= 86 and best["score"] - runner_up >= 10 and not best.get("is_new"):
        return best
    return None
