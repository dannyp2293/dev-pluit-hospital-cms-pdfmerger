from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def safe_filename(filename: str) -> str:
    """Hilangkan path traversal dan karakter yang tidak aman dari nama file."""
    basename = Path(filename).name.strip()
    cleaned = re.sub(r"[^\w.() -]+", "_", basename, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "dokumen_tanpa_nama"


def safe_folder_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return cleaned.strip("_") or "TANPA_NAMA"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(filepath: Path) -> str:
    digest = hashlib.sha256()

    with filepath.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def atomic_write(filepath: Path, data: bytes) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_path = filepath.with_name(filepath.name + ".part")

    try:
        with temp_path.open("wb") as file_obj:
            file_obj.write(data)

        os.replace(temp_path, filepath)

    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def get_unique_filepath(target_dir: Path, filename: str) -> Path:
    """Menghasilkan nama file unik; file lama tidak pernah ditimpa."""
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / safe_filename(filename)

    if not candidate.exists():
        return candidate

    stem, suffix = candidate.stem, candidate.suffix
    counter = 1

    while True:
        new_path = target_dir / f"{stem}_{counter}{suffix}"

        if not new_path.exists():
            return new_path

        counter += 1


def get_unique_pdf_output_path(
    target_dir: Path,
    original_stem: str,
    current_path: Path | None = None,
) -> Path:
    """Gunakan nama PDF bersih; tambahkan nomor jika nama bentrok."""
    target_dir.mkdir(parents=True, exist_ok=True)
    clean_stem = safe_filename(original_stem)

    candidate = target_dir / f"{clean_stem}.pdf"

    if current_path is not None and candidate.resolve() == current_path.resolve():
        return candidate

    if not candidate.exists():
        return candidate

    counter = 2

    while True:
        candidate = target_dir / f"{clean_stem} ({counter}).pdf"

        if current_path is not None and candidate.resolve() == current_path.resolve():
            return candidate

        if not candidate.exists():
            return candidate

        counter += 1


def normalize_existing_pdf_name(
    pdf_path: Path,
    original_name: str,
    ready_dir: Path,
) -> Path:
    """Migrasikan nama PDF lama yang memakai hash ke nama yang mudah dibaca."""
    clean_path = get_unique_pdf_output_path(
        ready_dir,
        Path(original_name).stem,
        current_path=pdf_path,
    )

    if clean_path.resolve() == pdf_path.resolve():
        return pdf_path

    if not clean_path.exists():
        pdf_path.replace(clean_path)
        return clean_path

    return pdf_path
