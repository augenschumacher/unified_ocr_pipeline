"""Local Ollama model recommendations for OCR pipeline roles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelRecommendation:
    vram_gb: int
    label: str
    glm_ocr: str
    vision: str
    fusion: str
    analysis: str
    notes: str
    estimated_download_gb: int = 0
    required_free_gb: int = 0

    def as_settings_models(self) -> dict[str, str]:
        return {
            "glm_ocr": self.glm_ocr,
            "vision": self.vision,
            "fusion": self.fusion,
            "analysis": self.analysis,
        }

    def as_llm_config_stages(self) -> dict[str, str]:
        return {key: f"ollama/{value}" for key, value in self.as_settings_models().items()}


_FALLBACK_RECOMMENDATIONS: tuple[ModelRecommendation, ...] = (
    ModelRecommendation(
        vram_gb=8,
        label="8 GB VRAM - fluessig und konservativ",
        glm_ocr="glm-ocr:bf16",
        vision="qwen3-vl:4b-instruct-q4_K_M",
        fusion="gemma4:e4b-it-qat",
        analysis="gemma4:e4b-it-qat",
        estimated_download_gb=18,
        required_free_gb=25,
        notes="Kleine Vision-Stufe, Gemma4 QAT fuer Text-Fusion/Analyse.",
    ),
    ModelRecommendation(
        vram_gb=12,
        label="12 GB VRAM - ausgewogen",
        glm_ocr="glm-ocr:bf16",
        vision="qwen3-vl:8b-instruct-q4_K_M",
        fusion="gemma4:12b-it-qat",
        analysis="gemma4:12b-it-qat",
        estimated_download_gb=30,
        required_free_gb=40,
        notes="Bessere Vision-Qualitaet und 12B-QAT fuer Textaufgaben.",
    ),
    ModelRecommendation(
        vram_gb=16,
        label="16 GB VRAM - stark fuer lange Dokumente",
        glm_ocr="glm-ocr:bf16",
        vision="qwen3-vl:8b-instruct-q4_K_M",
        fusion="gemma4:12b-it-qat",
        analysis="gemma4:12b-it-qat",
        estimated_download_gb=30,
        required_free_gb=40,
        notes="Gute Reserve fuer grosse Seitenbilder und 256K-Kontextmodelle.",
    ),
    ModelRecommendation(
        vram_gb=24,
        label="24 GB VRAM - grosse MoE/QAT-Stufe",
        glm_ocr="glm-ocr:bf16",
        vision="qwen3-vl:30b-a3b-instruct-q4_K_M",
        fusion="gemma4:26b-a4b-it-qat",
        analysis="gemma4:26b-a4b-it-qat",
        estimated_download_gb=65,
        required_free_gb=85,
        notes="MoE/QAT-Empfehlung; Modelle sollten nacheinander entladen werden.",
    ),
    ModelRecommendation(
        vram_gb=32,
        label="32 GB VRAM - maximale lokale Qualitaet",
        glm_ocr="glm-ocr:bf16",
        vision="qwen3-vl:32b-instruct-q4_K_M",
        fusion="gemma4:31b-it-qat",
        analysis="gemma4:31b-it-qat",
        estimated_download_gb=82,
        required_free_gb=105,
        notes="Sehr starke lokale Modelle; grosse Downloads und viel Plattenplatz.",
    ),
)


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "resources" / "ollama_model_recommendations.json"


def _recommendation_from_dict(item: dict) -> ModelRecommendation:
    return ModelRecommendation(
        vram_gb=int(item["vram_gb"]),
        label=str(item["label"]),
        glm_ocr=str(item["glm_ocr"]),
        vision=str(item["vision"]),
        fusion=str(item["fusion"]),
        analysis=str(item["analysis"]),
        notes=str(item.get("notes", "")),
        estimated_download_gb=int(item.get("estimated_download_gb", 0) or 0),
        required_free_gb=int(item.get("required_free_gb", 0) or 0),
    )


def load_recommendations(catalog_path: str | Path | None = None) -> tuple[ModelRecommendation, ...]:
    path = Path(catalog_path) if catalog_path else default_catalog_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tiers = data.get("tiers", [])
        recommendations = tuple(sorted((_recommendation_from_dict(item) for item in tiers), key=lambda item: item.vram_gb))
        if recommendations:
            return recommendations
    except Exception:
        pass
    return _FALLBACK_RECOMMENDATIONS


VRAM_RECOMMENDATIONS: tuple[ModelRecommendation, ...] = load_recommendations()


def recommendation_for_vram(vram_gb: int | str | None) -> ModelRecommendation:
    try:
        requested = int(vram_gb or 8)
    except (TypeError, ValueError):
        requested = 8

    selected = VRAM_RECOMMENDATIONS[0]
    for recommendation in VRAM_RECOMMENDATIONS:
        if requested >= recommendation.vram_gb:
            selected = recommendation
    return selected


def default_recommendation() -> ModelRecommendation:
    return recommendation_for_vram(8)
