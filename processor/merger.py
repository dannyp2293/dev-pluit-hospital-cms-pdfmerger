from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pymupdf as fitz

from .utils import safe_folder_name, sha256_file

def merge_patient_documents(
    patient_name: str,
    file_list: list[dict[str, Any]],
    exam_order: list[str],
    output_batch_dir: Path,
) -> tuple[bool, str]:
    """Gabungkan PDF sesuai urutan kategori; sumber tidak diubah."""
    output_batch_dir.mkdir(parents=True, exist_ok=True)
    merged_pdf = fitz.open()
    pages_added = 0
    files_by_exam: dict[str, list[dict[str, Any]]] = {code: [] for code in exam_order}
    seen_files: set[str] = set()

    for item in file_list:
        path = Path(item["filepath"])
        code = item["exam_code"]
        if not path.is_file() or path.suffix.lower() != ".pdf" or code not in files_by_exam:
            continue
        try:
            digest = item.get("file_hash") or sha256_file(path)
        except Exception:
            continue
        if digest in seen_files:
            continue
        seen_files.add(digest)
        files_by_exam[code].append(item)

    safe_patient = safe_folder_name(patient_name).replace(" ", "_")
    output_path = output_batch_dir / f"{safe_patient}.pdf"
    temp_path = output_batch_dir / f"{safe_patient}.__TEMP__.pdf"

    try:
        for code in exam_order:
            exam_files = sorted(
                files_by_exam.get(code, []),
                key=lambda item: Path(item["filepath"]).name.casefold(),
            )
            for item in exam_files:
                pdf_path = Path(item["filepath"])
                with fitz.open(pdf_path) as source_pdf:
                    if source_pdf.page_count < 1:
                        continue
                    merged_pdf.insert_pdf(source_pdf)
                    pages_added += source_pdf.page_count

        if pages_added == 0:
            return False, "Tidak ada halaman PDF valid yang dapat digabungkan."

        merged_pdf.save(temp_path, garbage=4, deflate=True)
        merged_pdf.close()

        # Verifikasi jumlah halaman hasil sebelum mengganti hasil sebelumnya.
        with fitz.open(temp_path) as check_pdf:
            if check_pdf.page_count != pages_added:
                raise RuntimeError(
                    f"Verifikasi gagal: seharusnya {pages_added} halaman, "
                    f"hasil terbaca {check_pdf.page_count} halaman."
                )

        os.replace(temp_path, output_path)
        return True, str(output_path)

    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return False, f"Gagal menggabungkan PDF: {exc}"
    finally:
        if not merged_pdf.is_closed:
            merged_pdf.close()


def merge_all_patient_pdfs_into_master(file_paths: list[Path]) -> bytes:
    master = fitz.open()
    try:
        for file_path in file_paths:
            if file_path.exists() and file_path.suffix.lower() == ".pdf":
                with fitz.open(file_path) as document:
                    master.insert_pdf(document)
        return master.tobytes()
    finally:
        master.close()
