import os

MOJIBAKE = [
    ('Ã¼', 'ü'), ('Ã¤', 'ä'), ('Ã¶', 'ö'), ('ÃŸ', 'ß'),
    ('Ãœ', 'Ü'), ('Ã„', 'Ä'), ('Ã–', 'Ö'),
    ('â€"', '–'), ('â†'', '→'), ('â"€', '─'),
]

targets = [
    'unified_ocr_app/core/llm/tasks.py',
    'unified_ocr_app/core/cache.py',
    'unified_ocr_app/core/llm/ollama_client.py',
    'unified_ocr_app/core/pipeline.py',
]

for fpath in targets:
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            text = f.read()
        original = text
        for bad, good in MOJIBAKE:
            text = text.replace(bad, good)
        if text != original:
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f'Fixed: {fpath}')
        else:
            print(f'Clean: {fpath}')
    except Exception as e:
        print(f'Error {fpath}: {e}')
