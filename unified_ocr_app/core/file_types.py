SUPPORTED_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
}

SUPPORTED_OFFICE_SUFFIXES = {".docx", ".odt", ".doc", ".odoc"}

SUPPORTED_INPUT_SUFFIXES = {".pdf", *SUPPORTED_IMAGE_SUFFIXES, *SUPPORTED_OFFICE_SUFFIXES}
