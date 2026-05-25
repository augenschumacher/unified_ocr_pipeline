"""
docling_runner.py – Docling PDF-Konvertierung mit HybridChunker

Verantwortlichkeiten:
    - PDF via Docling in Markdown konvertieren
    - Chunks mit HybridChunker erzeugen und seitenweise gruppieren
"""

from pathlib import Path


def run_docling_by_page_with_chunks(pdf_path: Path) -> tuple[str, dict[int, str]]:
    """
    Konvertiert eine PDF mit Docling und gibt zwei Ergebnisse zurück:

    Returns:
        full_markdown   – Gesamtes Markdown-Dokument als String
        page_markdowns  – Dictionary {Seitennummer: Markdown-Text der Chunks}

    Verwendet HybridChunker für semantisch sinnvolle Chunk-Grenzen.
    Chunks werden anhand ihrer Seitenreferenz (prov.page_no) gruppiert.
    Chunks ohne Seitenreferenz werden Seite 1 zugeordnet.
    """
    try:
        from docling.document_converter import DocumentConverter
        from docling.chunking import HybridChunker

        converter  = DocumentConverter()
        conversion = converter.convert(str(pdf_path))
        dl_doc     = conversion.document

        # Gesamtes Markdown
        export_fn     = getattr(dl_doc, "export_to_markdown", None)
        full_markdown = str(export_fn()) if callable(export_fn) else str(dl_doc)

        # Tokenizer-Warnung unterdrücken (> 512 Token)
        chunker = HybridChunker()
        try:
            hf_tok = getattr(getattr(chunker, "tokenizer", None), "tokenizer", None)
            if hf_tok and hasattr(hf_tok, "model_max_length"):
                hf_tok.model_max_length = 100_000
        except Exception:
            pass

        # Chunks seitenweise gruppieren
        page_chunks: dict[int, list[str]] = {}
        for chunk in chunker.chunk(dl_doc):
            pages: set[int] = set()
            if hasattr(chunk, "meta") and hasattr(chunk.meta, "doc_items") and chunk.meta.doc_items:
                for item in chunk.meta.doc_items:
                    if hasattr(item, "prov") and item.prov:
                        for p in item.prov:
                            page_no = getattr(p, "page_no", None)
                            if page_no is not None:
                                pages.add(page_no)
            if not pages:
                pages.add(1)

            chunk_text = (
                chunker.contextualize(chunk) if hasattr(chunker, "contextualize") else chunk.text
            )
            for page_no in pages:
                page_chunks.setdefault(page_no, []).append(chunk_text)

        page_markdowns = {p: "\n\n".join(txts) for p, txts in page_chunks.items()}
        return full_markdown, page_markdowns

    except Exception as e:
        raise RuntimeError(f"Docling/HybridChunker fehlgeschlagen: {e}")
