# Third-Party Licenses

This project depends on third-party open-source packages. Keep their license
texts and notices intact when redistributing the application.

## Key Runtime Dependencies

| Dependency | License | Notes |
| --- | --- | --- |
| PyMuPDF | AGPL-3.0 or commercial Artifex license | Strong copyleft dependency; this is the main reason this project is released under AGPL-3.0. |
| OCRmyPDF | MPL-2.0 | Used for OCR PDF processing. |
| Docling | MIT | Used for document parsing/extraction. |
| CustomTkinter | CC0-1.0 | Used for the desktop UI. |
| LiteLLM | MIT | Used for LLM provider routing. |
| Google API Python Client | Apache-2.0 | Used for Google Drive integration. |
| python-docx | MIT | Used for DOCX export. |
| pydantic | MIT | Used for data validation. |
| pystray | LGPL-3.0 | Used for system tray integration. |
| pillow-heif | BSD-3-Clause | Used for HEIF image support. |

This file is a practical notice, not a substitute for the full license texts
of each dependency. For a release bundle, include dependency metadata generated
from the exact installed package versions.
