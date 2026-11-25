from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pymupdf as fitz

from .database import DEFAULT_EXAMS, list_batch_documents
from .identity import (
    advanced_name_similarity,
    extract_name_from_filename,
    normalize_patient_identity_name,
    normalize_patient_name,
)
from .ocr import OCR_NAME_MIN_CONFIDENCE, extract_hybrid_text_from_pdf
from .utils import sha256_file

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(
    os.getenv("MCU_DATA_DIR", str(PROJECT_DIR / "DATA_SUMBER"))
).expanduser().resolve()

def detect_exam_codes(value: str) -> set[str]:
    """Deteksi kategori dari nama file/teks PDF. Hanya memakai petunjuk yang eksplisit."""
    text = normalize_patient_name(value)
    detected: set[str] = set()
    patterns = {
        "AUDIO": r"\b(AUDIO|AUDIOMETRI|AUDIOMETRIK)\b",
        "EKG": r"\b(EKG|ELECTROCARDIOGRAM|ELEKTROKARDIOGRAFI)\b",
        "VISUS": r"\b(VISUS|KETAJAMAN PENGELIHATAN)\b",
        "LAB": r"\b(LAB|LABORATORIUM)\b",
        "RONTGEN": r"\b(RONTGEN|RADIOLOGI|X RAY|XRAY)\b",
        "SPIRO": r"\b(SPIRO|SPIROMETRI)\b",
        "LPK": r"\b(LPK|LAPORAN PEMERIKSAAN KESEHATAN)\b",
    }
    for code, pattern in patterns.items():
        if re.search(pattern, text):
            detected.add(code)
    return detected


def select_patient_name_candidate(
    filename_candidate: str,
    text_candidate: str,
    ocr_candidate: str,
    ocr_confidence: float | None,
    source_conflict: bool = False,
) -> dict[str, Any]:
    """Filename-first decision.

    Kebijakan operasional:
    - Nama file tetap menjadi acuan kelompok.
    - Nama PDF yang tidak terbaca atau hanya muncul dari OCR berkeyakinan
      rendah memakai nama file dan TIDAK menahan kelompok.
    - Nama yang terbaca jelas dan cocok menghasilkan COCOK.
    - Nama yang terbaca jelas tetapi berbeda menahan seluruh kelompok pasien.
    - Sumber text layer dan OCR yang saling bertentangan masuk PERLU VERIFIKASI.

    Catatan: ocr_confidence adalah confidence rata-rata halaman, bukan confidence
    khusus kata nama. Karena itu OCR-only mismatch hanya dianggap BERBEDA pada
    ambang yang lebih ketat; OCR yang meragukan kembali ke filename.
    """
    filename_name = normalize_patient_identity_name(filename_candidate)
    text_name = normalize_patient_identity_name(text_candidate)
    ocr_name = normalize_patient_identity_name(ocr_candidate)

    if filename_name:
        chosen_name = filename_name
        name_source = "FILENAME_PRIMARY"
    elif text_name:
        chosen_name = text_name
        name_source = "PDF_TEXT_LAYER_FALLBACK"
    elif ocr_name:
        chosen_name = ocr_name
        name_source = "PDF_OCR_FALLBACK"
    else:
        chosen_name = "NAMA_TIDAK_TERDETEKSI"
        name_source = "UNDETECTED"

    # Text layer/layout diutamakan karena tidak bergantung pada tebakan OCR.
    document_name = text_name or ocr_name
    name_not_detected = not bool(document_name)
    mismatch_score = (
        advanced_name_similarity(filename_name, document_name)["score"]
        if filename_name and document_name else None
    )

    confidence_value = None
    if ocr_confidence is not None:
        try:
            confidence_value = float(ocr_confidence)
        except (TypeError, ValueError):
            confidence_value = None

    effective_source_conflict = bool(source_conflict)
    text_score = (
        advanced_name_similarity(filename_name, text_name)["score"]
        if filename_name and text_name else None
    )
    ocr_score = (
        advanced_name_similarity(filename_name, ocr_name)["score"]
        if filename_name and ocr_name else None
    )

    # Jika text layer sudah cocok dengan filename, OCR yang berbeda dianggap
    # noise/hasil scan dan tidak boleh menahan kelompok.
    if text_score is not None and text_score >= 90:
        effective_source_conflict = False

    # Gunakan ambang lebih konservatif untuk OCR-only mismatch karena confidence
    # yang tersedia adalah rata-rata halaman, bukan confidence spesifik nama.
    ocr_mismatch_min_confidence = max(OCR_NAME_MIN_CONFIDENCE, 90.0)
    low_confidence_ocr_review = bool(
        not text_name and ocr_name and (
            confidence_value is None or confidence_value < ocr_mismatch_min_confidence
        )
    )

    if not filename_name:
        name_status = "NAMA FILE TIDAK TERDETEKSI"
    elif not document_name:
        # Tidak ada nama pasien yang dapat dipastikan dari PDF: gunakan filename.
        name_status = "NAMA FILE DIPAKAI"
    elif text_score is not None and text_score >= 90:
        # Nama dari text layer cocok; OCR buruk/berbeda tidak mengubah keputusan.
        name_status = "COCOK"
    elif text_name:
        # Text layer memberikan nama nyata. Jika sumber OCR bertentangan, tahan
        # untuk pemeriksaan; mismatch kuat dari text layer juga menahan kelompok.
        if effective_source_conflict:
            name_status = "PERLU VERIFIKASI"
        elif text_score is not None and text_score < 75:
            name_status = "BERBEDA"
        elif text_score is not None and text_score < 90:
            name_status = "PERLU VERIFIKASI"
        else:
            name_status = "COCOK"
    elif ocr_name:
        # Jika OCR tetap menghasilkan nama yang sangat cocok dengan filename,
        # kelompok lolos meskipun confidence halaman rendah.
        if ocr_score is not None and ocr_score >= 90:
            name_status = "COCOK"
        # OCR-only confidence rendah berarti nama tidak cukup terbaca: jangan
        # menganggapnya sebagai mismatch; gunakan nama file saja.
        elif low_confidence_ocr_review:
            name_status = "NAMA FILE DIPAKAI"
        elif ocr_score is not None and ocr_score < 75:
            # OCR-only perlu ambang ekstra ketat karena confidence adalah rata-rata
            # halaman. Nilai tinggi tetap bukan jaminan, maka mismatch ini ditahan.
            strong_ocr_threshold = ocr_mismatch_min_confidence
            if confidence_value is not None and confidence_value >= strong_ocr_threshold:
                name_status = "BERBEDA"
            else:
                name_status = "NAMA FILE DIPAKAI"
        elif ocr_score is not None and ocr_score < 90:
            name_status = "PERLU VERIFIKASI"
        else:
            name_status = "COCOK"
    else:
        name_status = "NAMA FILE DIPAKAI"

    name_mismatch = name_status == "BERBEDA"
    name_uncertain = name_status == "PERLU VERIFIKASI"
    # Hanya mismatch nyata atau nama yang terbaca tetapi ambigu yang menahan
    # seluruh kelompok. OCR gagal/buram tidak lagi memicu verifikasi per file.
    name_review_required = name_status in {
        "BERBEDA", "PERLU VERIFIKASI", "NAMA FILE TIDAK TERDETEKSI"
    }

    return {
        "chosen_name": chosen_name,
        "name_source": name_source,
        "document_name": document_name,
        "name_status": name_status,
        "name_mismatch": name_mismatch,
        "name_uncertain": name_uncertain,
        "name_not_detected": name_not_detected,
        "name_similarity_score": mismatch_score,
        "low_confidence_ocr_review": low_confidence_ocr_review,
        "source_conflict": effective_source_conflict,
        "name_review_required": name_review_required,
    }


def analyze_batch_documents(
    batch_name: str, enable_ocr: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Analisis antrean aktif; nama file utama, text layer/OCR sebagai pembanding."""
    batch_dir = DATA_DIR / batch_name
    if not batch_dir.exists():
        return {}

    documents_to_analyze: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for row in list_batch_documents(batch_name):
        if not bool(row["merge_enabled"]):
            continue
        pdf_value = row["pdf_path"]
        if not pdf_value or row["status"] != "BERHASIL":
            continue
        pdf_path = Path(pdf_value)
        if not pdf_path.is_file():
            continue
        resolved = str(pdf_path.resolve())
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        documents_to_analyze.append({
            "filepath": str(pdf_path),
            "filename": pdf_path.name,
            "original_filename": row["original_name"],
            "exam_code": row["exam_code"],
        })
    return analyze_documents(documents_to_analyze, enable_ocr=enable_ocr)



def analyze_documents(
    documents_to_analyze: list[dict[str, Any]],
    enable_ocr: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Mesin analisis bersama untuk dokumen dari SQLite atau manifest."""
    candidates: dict[str, list[dict[str, Any]]] = {}
    seen_content_batch: set[str] = set()

    for item in documents_to_analyze:
        pdf_path = Path(item["filepath"])
        original_name = item["original_filename"]
        exam_code = item["exam_code"]
        try:
            with fitz.open(pdf_path) as pdf:
                if pdf.page_count < 1:
                    continue
                page_count = pdf.page_count
            file_hash = sha256_file(pdf_path)
        except Exception:
            continue

        if file_hash in seen_content_batch:
            continue
        seen_content_batch.add(file_hash)

        category_hints = detect_exam_codes(original_name)
        conflicting_categories = category_hints - {exam_code}
        if conflicting_categories:
            continue

        filename_candidate = extract_name_from_filename(original_name, exam_code)
        extracted = extract_hybrid_text_from_pdf(
            pdf_path, file_hash, enable_ocr=enable_ocr,
            filename_candidate=filename_candidate,
        )
        text_candidate = extracted["name_from_text"]
        ocr_candidate = extracted["name_from_ocr"]
        identity_fields = extracted["identity_fields"]

        # Nama file menjadi dasar pengelompokan. Nama dari PDF hanya dipakai
        # sebagai pembanding agar nama dokter/OCR tidak mengubah kelompok pasien.
        name_decision = select_patient_name_candidate(
            filename_candidate,
            text_candidate,
            ocr_candidate,
            extracted["ocr_confidence"],
            source_conflict=extracted.get("source_conflict", False),
        )
        chosen_name = name_decision["chosen_name"]
        group_key = normalize_patient_identity_name(chosen_name) or "NAMA_TIDAK_TERDETEKSI"

        info = {
            "filename": item["filename"],
            "original_filename": original_name,
            "filepath": str(pdf_path),
            "exam_code": exam_code,
            "name_from_file": filename_candidate,
            "name_from_text": text_candidate,
            "name_from_ocr": ocr_candidate,
            "candidate_name": chosen_name,
            "name_from_document": name_decision["document_name"],
            "name_source": name_decision["name_source"],
            "page_count": page_count,
            "file_hash": file_hash,
            "name_status": name_decision["name_status"],
            "name_mismatch": name_decision["name_mismatch"],
            "name_uncertain": name_decision["name_uncertain"],
            "name_not_detected": name_decision["name_not_detected"],
            "name_similarity_score": name_decision["name_similarity_score"],
            "low_confidence_ocr_review": name_decision["low_confidence_ocr_review"],
            "source_conflict": name_decision["source_conflict"],
            "source_similarity_score": extracted.get("source_similarity_score"),
            "name_review_required": name_decision["name_review_required"],
            "extraction_source": extracted["extraction_source"],
            "ocr_confidence": extracted["ocr_confidence"],
            "ocr_status": extracted["ocr_status"],
            "ocr_attempted": extracted["ocr_attempted"],
            "nik_hash": identity_fields["nik_hash"],
            "nik_masked": identity_fields["nik_masked"],
            "mr_hash": identity_fields["mr_hash"],
            "mr_masked": identity_fields["mr_masked"],
            "dob_iso": identity_fields["dob_iso"],
            "extension": ".pdf",
        }
        if item.get("document_id") is not None:
            info["document_id"] = item["document_id"]
        candidates.setdefault(group_key, []).append(info)

    return candidates

def analyze_manifest_documents(
    manifest: dict[str, Any],
    enable_ocr: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Validasi JSON manifest Laravel dan analisis dokumennya."""

    if not isinstance(manifest, dict):
        raise ValueError("Manifest harus berupa object JSON.")

    batch_name = manifest.get("batch_name")

    if (
        not isinstance(batch_name, str)
        or not batch_name.strip()
        or batch_name != batch_name.strip()
        or batch_name in {".", ".."}
        or any(separator in batch_name for separator in ("/", "\\", "\x00"))
    ):
        raise ValueError("Nama batch tidak valid.")

    data_root = DATA_DIR.resolve()
    batch_dir = (data_root / batch_name).resolve()

    if batch_dir == data_root or data_root not in batch_dir.parents:
        raise ValueError("Lokasi batch berada di luar DATA_DIR.")

    if not batch_dir.is_dir():
        raise ValueError("Direktori batch tidak ditemukan.")

    raw_documents = manifest.get("documents")

    if not isinstance(raw_documents, list) or not raw_documents:
        raise ValueError("Manifest harus berisi daftar dokumen.")

    valid_exam_codes = {code for code, _ in DEFAULT_EXAMS}

    documents_to_analyze: list[dict[str, Any]] = []
    seen_document_ids: set[int] = set()
    seen_paths: set[str] = set()

    for index, item in enumerate(raw_documents):
        if not isinstance(item, dict):
            raise ValueError(
                f"Dokumen indeks {index} harus berupa object."
            )

        document_id = item.get("document_id")

        if type(document_id) is not int or document_id <= 0:
            raise ValueError(
                f"document_id pada indeks {index} tidak valid."
            )

        if document_id in seen_document_ids:
            raise ValueError(f"document_id duplikat: {document_id}")

        seen_document_ids.add(document_id)

        if item.get("status") != "BERHASIL":
            raise ValueError(
                f"Dokumen {document_id} belum berstatus BERHASIL."
            )

        if item.get("merge_enabled") is not True:
            raise ValueError(
                f"Dokumen {document_id} tidak diaktifkan untuk analisis."
            )

        exam_code = item.get("exam_code")

        if not isinstance(exam_code, str):
            raise ValueError(
                f"exam_code dokumen {document_id} tidak valid."
            )

        exam_code = exam_code.strip().upper()

        if exam_code not in valid_exam_codes:
            raise ValueError(
                f"Kode pemeriksaan tidak dikenal: {exam_code}"
            )

        filepath = item.get("filepath")

        if not isinstance(filepath, str) or not filepath.strip():
            raise ValueError(
                f"filepath dokumen {document_id} tidak valid."
            )

        pdf_path = Path(filepath).expanduser().resolve()

        try:
            pdf_path.relative_to(batch_dir)
        except ValueError:
            raise ValueError(
                f"File dokumen {document_id} berada di luar direktori batch."
            ) from None

        if not pdf_path.is_file():
            raise ValueError(
                f"File PDF dokumen {document_id} tidak ditemukan."
            )

        if pdf_path.suffix.lower() != ".pdf":
            raise ValueError(
                f"Dokumen {document_id} bukan file PDF."
            )

        resolved_path = str(pdf_path)

        if resolved_path in seen_paths:
            raise ValueError(
                f"Path dokumen duplikat: {pdf_path.name}"
            )

        seen_paths.add(resolved_path)

        original_filename = item.get("original_filename")

        if (
            not isinstance(original_filename, str)
            or not original_filename.strip()
            or Path(original_filename).name != original_filename
            or any(
                separator in original_filename
                for separator in ("/", "\\", "\x00")
            )
        ):
            raise ValueError(
                f"original_filename dokumen {document_id} tidak valid."
            )

        documents_to_analyze.append({
            "document_id": document_id,
            "filepath": resolved_path,
            "filename": pdf_path.name,
            "original_filename": original_filename,
            "exam_code": exam_code,
        })

    return analyze_documents(
        documents_to_analyze,
        enable_ocr=enable_ocr,
    )

