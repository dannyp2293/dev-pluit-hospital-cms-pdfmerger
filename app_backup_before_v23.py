from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image, ImageOps, ImageSequence
from rapidfuzz import fuzz

# pytesseract hanya wrapper Python. Binary Tesseract tetap harus dipasang
# terpisah pada komputer/server yang menjalankan aplikasi.
try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    PYTESSERACT_IMPORT_ERROR: str | None = None
except Exception as _ocr_import_exc:
    pytesseract = None
    TesseractOutput = None
    PYTESSERACT_IMPORT_ERROR = str(_ocr_import_exc)


# ============================================================
# 1. KONFIGURASI APLIKASI
# ============================================================

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "DATA_SUMBER"
OUTPUT_DIR = APP_DIR / "HASIL_PDF"
REPORT_DIR = APP_DIR / "LAPORAN"
# Ruang penyimpanan converter dibuat terpisah dari sumber Validasi & Merge.
CONVERSION_DIR = APP_DIR / "HASIL_KONVERSI"
DB_DIR = APP_DIR / "DATABASE"
DB_PATH = DB_DIR / "mcu_merger.db"

DEFAULT_EXAMS = [
    ("LPK", "Laporan Pemeriksaan Kesehatan"),
    ("LAB", "Laboratorium"),
    ("RONTGEN", "Rontgen + film"),
    ("EKG", "Elektrokardiografi"),
    ("VISUS", "Visus"),
    ("AUDIO", "Audiometri"),
    ("SPIRO", "Spirometri"),
]

ALLOWED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".doc", ".docx"
}
MAX_FILE_COUNT = 50
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

MEDICAL_KEYWORDS = [
    "AUDIOMETRIK", "AUDIOMETRI", "LABORATORIUM", "RADIOLOGI",
    "EKG", "RONTGEN", "SPIROMETRI", "SPIRO", "VISUS", "HASIL",
    "PEMERIKSAAN", "MCU", "LPK", "LAB", "AUDIO",
]

# OCR HYBRID: text layer PyMuPDF tetap menjadi sumber pertama. Tesseract hanya
# dipanggil jika nama tidak ditemukan atau text layer terlalu sedikit.
OCR_ENABLED_DEFAULT = os.getenv("MCU_OCR_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
OCR_LANGUAGE = os.getenv("MCU_OCR_LANG", "ind+eng").strip() or "ind+eng"
OCR_DPI = max(150, min(400, int(os.getenv("MCU_OCR_DPI", "300"))))
OCR_MAX_PAGES = max(1, min(10, int(os.getenv("MCU_OCR_MAX_PAGES", "3"))))
OCR_MIN_TEXT_CHARS = max(10, int(os.getenv("MCU_OCR_MIN_TEXT_CHARS", "40")))
# OCR nama pasien hanya mengambil alih nama file jika confidence memadai.
OCR_NAME_MIN_CONFIDENCE = max(0.0, min(100.0, float(os.getenv("MCU_OCR_NAME_MIN_CONFIDENCE", "65"))))
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()
if pytesseract is not None:
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    elif os.name == "nt":
        _default_tesseract = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if _default_tesseract.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(_default_tesseract)

for directory in (DATA_DIR, OUTPUT_DIR, REPORT_DIR, CONVERSION_DIR, DB_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# Folder mandiri untuk fitur Normalisasi Nama; tidak dipakai Validasi & Merge.
NAME_NORMALIZATION_DIR = APP_DIR / "NORMALISASI_NAMA"
NAME_NORMALIZATION_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="MCU PDF Merger", page_icon="📄", layout="wide")


# ============================================================
# 2. DATABASE
# ============================================================


@contextmanager
def get_conn():
    """Buka koneksi SQLite dan selalu tutup setelah blok selesai."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                batch_name TEXT NOT NULL UNIQUE,
                exam_codes TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Persiapan',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exam_types (
                code TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_name TEXT NOT NULL,
                exam_code TEXT NOT NULL,
                original_name TEXT NOT NULL,
                original_name_key TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                pdf_path TEXT,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                uploaded_at TEXT NOT NULL,
                merge_enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(batch_name, exam_code, original_name_key, file_hash)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_batch_exam "
            "ON documents(batch_name, exam_code)"
        )
        # Antrean merge: dokumen lama tetap tersimpan, tetapi dapat dikeluarkan
        # dari analisis/merge tanpa menghapus file sumber atau catatan database.
        document_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "merge_enabled" not in document_columns:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN merge_enabled INTEGER NOT NULL DEFAULT 1"
            )
        # Kolom pengelolaan batch ditambahkan secara aman (migrasi aditif).
        # Batch lama tetap ada; nilai NULL berarti belum diarsipkan/dibuang.
        batch_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(batches)").fetchall()
        }
        if "archived_at" not in batch_columns:
            conn.execute("ALTER TABLE batches ADD COLUMN archived_at TEXT")
        if "deleted_at" not in batch_columns:
            conn.execute("ALTER TABLE batches ADD COLUMN deleted_at TEXT")
        # Manifest converter terpisah; tidak dibaca oleh Validasi & Merge MCU.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversion_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_name TEXT NOT NULL,
                exam_code TEXT NOT NULL,
                original_name TEXT NOT NULL,
                original_name_key TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                pdf_path TEXT,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                uploaded_at TEXT NOT NULL,
                UNIQUE(batch_name, exam_code, original_name_key, file_hash)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversion_documents_batch_exam "
            "ON conversion_documents(batch_name, exam_code)"
        )
        for code, label in DEFAULT_EXAMS:
            conn.execute(
                "INSERT OR IGNORE INTO exam_types(code, label, active) VALUES (?, ?, 1)",
                (code, label),
            )

        # Cache OCR dipisahkan dari manifest dokumen. Kunci berdasarkan hash
        # PDF + halaman + bahasa + versi engine agar OCR tidak diulang tanpa sebab.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ocr_cache (
                file_hash TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                language TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                confidence REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(file_hash, page_number, language, engine_version)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_name TEXT NOT NULL,
                action TEXT NOT NULL,
                source_names TEXT,
                target_name TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identity_audit_batch "
            "ON identity_audit_log(batch_name, created_at DESC)"
        )


# ============================================================
# 3. UTILITAS FILE DAN VALIDASI
# ============================================================


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
    target_dir: Path, original_stem: str, current_path: Path | None = None
) -> Path:
    """Gunakan nama PDF bersih; tambahkan nomor hanya jika nama memang bentrok."""
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
    pdf_path: Path, original_name: str, ready_dir: Path
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

    # Perlindungan ekstra: jangan pernah menimpa file yang sudah ada.
    return pdf_path


def validate_upload(uploaded_file: Any) -> str | None:
    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return f"Format {extension or '(tanpa ekstensi)'} tidak didukung."
    if uploaded_file.size <= 0:
        return "File kosong."
    if uploaded_file.size > MAX_FILE_SIZE_BYTES:
        size_mb = uploaded_file.size / (1024 * 1024)
        return f"Ukuran {size_mb:.2f} MB melebihi batas {MAX_FILE_SIZE_MB} MB."
    return None


def validate_pdf(filepath: Path) -> tuple[bool, str]:
    try:
        with fitz.open(filepath) as document:
            if document.page_count < 1:
                return False, "PDF tidak memiliki halaman."
        return True, "OK"
    except Exception as exc:
        return False, f"PDF tidak dapat dibaca: {exc}"


# ============================================================
# 4. KONVERSI FILE MENJADI PDF
# ============================================================


def convert_image_to_pdf(image_path: Path, output_pdf_path: Path) -> tuple[bool, str]:
    """Konversi gambar, termasuk TIFF multi-halaman, menjadi PDF."""
    try:
        frames: list[Image.Image] = []
        with Image.open(image_path) as image:
            for frame in ImageSequence.Iterator(image):
                frame_copy = ImageOps.exif_transpose(frame.copy())
                if frame_copy.mode != "RGB":
                    frame_copy = frame_copy.convert("RGB")
                frames.append(frame_copy)

        if not frames:
            return False, "File gambar tidak memiliki frame yang dapat dibaca."

        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        first, remaining = frames[0], frames[1:]
        first.save(
            output_pdf_path,
            "PDF",
            resolution=150.0,
            save_all=bool(remaining),
            append_images=remaining,
        )
        for frame in frames:
            frame.close()
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def find_libreoffice_binary() -> str | None:
    for executable in ("libreoffice", "soffice", "libreoffice.exe", "soffice.exe"):
        found = shutil.which(executable)
        if found:
            return found
    return None


def convert_word_to_pdf(word_path: Path, output_pdf_path: Path) -> tuple[bool, str]:
    """Konversi DOC/DOCX menggunakan LibreOffice headless (Linux/WSL/Windows)."""
    executable = find_libreoffice_binary()
    if not executable:
        return (
            False,
            "LibreOffice tidak ditemukan. Instal libreoffice-writer di Ubuntu/WSL "
            "atau pasang LibreOffice di Windows server.",
        )

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="mcu_lo_") as temp_root:
            temp_root_path = Path(temp_root)
            temp_output = temp_root_path / "output"
            temp_profile = temp_root_path / "profile"
            temp_output.mkdir(parents=True, exist_ok=True)
            temp_profile.mkdir(parents=True, exist_ok=True)

            command = [
                executable,
                "--headless",
                f"-env:UserInstallation={temp_profile.as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(temp_output),
                str(word_path),
            ]
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )

            generated_pdf = temp_output / f"{word_path.stem}.pdf"
            if result.returncode != 0 or not generated_pdf.exists():
                details = (result.stderr or result.stdout or "Tidak ada detail dari LibreOffice").strip()
                return False, details[:1200]

            shutil.copy2(generated_pdf, output_pdf_path)

        valid, message = validate_pdf(output_pdf_path)
        if not valid:
            output_pdf_path.unlink(missing_ok=True)
            return False, message
        return True, "OK"
    except subprocess.TimeoutExpired:
        return False, "Konversi Word melewati batas waktu 180 detik."
    except Exception as exc:
        return False, str(exc)


# ============================================================
# 5. PENYIMPANAN UPLOAD, ANTI-DUPLIKASI, DAN MANIFEST DATABASE
# ============================================================


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


def get_document_record_by_hash(batch_name: str, file_hash: str) -> sqlite3.Row | None:
    """Cari konten identik di seluruh kategori pada batch yang sama."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM documents WHERE batch_name=? AND file_hash=? ORDER BY id LIMIT 1",
            (batch_name, file_hash),
        ).fetchone()


def get_document_record(
    batch_name: str,
    exam_code: str,
    original_name_key: str,
    file_hash: str,
) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM documents
            WHERE batch_name=? AND exam_code=? AND original_name_key=? AND file_hash=?
            """,
            (batch_name, exam_code, original_name_key, file_hash),
        ).fetchone()


def store_uploaded_documents(
    uploaded_files: list[Any], batch_name: str, exam_code: str
) -> dict[str, Any]:
    """Simpan file asli dan hasil PDF di folder terpisah; cegah upload identik berulang."""
    result: dict[str, Any] = {
        "pdf_paths": [],
        "saved_count": 0,
        "duplicate_count": 0,
        "reactivated_count": 0,
        "converted_count": 0,
        "pdf_source_count": 0,
        "errors": [],
    }

    if len(uploaded_files) > MAX_FILE_COUNT:
        result["errors"].append(f"Maksimal {MAX_FILE_COUNT} file sekali upload.")
        return result

    exam_dir = DATA_DIR / batch_name / exam_code
    source_dir = exam_dir / "ASLI"
    ready_dir = exam_dir / "PDF_SIAP"
    source_dir.mkdir(parents=True, exist_ok=True)
    ready_dir.mkdir(parents=True, exist_ok=True)

    seen_in_current_upload: set[tuple[str, str]] = set()

    for uploaded in uploaded_files:
        validation_error = validate_upload(uploaded)
        if validation_error:
            result["errors"].append(f"{uploaded.name}: {validation_error}")
            continue

        original_name = safe_filename(uploaded.name)
        extension = Path(original_name).suffix.lower()
        original_name_key = original_name.casefold()

        try:
            file_bytes = uploaded.getvalue()
            if not file_bytes:
                result["errors"].append(f"{original_name}: File kosong.")
                continue
            file_hash = sha256_bytes(file_bytes)
        except Exception as exc:
            result["errors"].append(f"{original_name}: Gagal membaca upload ({exc}).")
            continue

        current_key = (original_name_key, file_hash)
        if current_key in seen_in_current_upload:
            result["duplicate_count"] += 1
            continue
        seen_in_current_upload.add(current_key)

        # Cegah satu konten yang sama didaftarkan lagi, walaupun nama file atau
        # kategori pilihan berbeda. File sumber yang sudah ada tidak disentuh.
        same_content = get_document_record_by_hash(batch_name, file_hash)
        if same_content:
            existing_exam = same_content["exam_code"]
            if existing_exam != exam_code:
                result["duplicate_count"] += 1
                result["errors"].append(
                    f"{original_name}: konten identik sudah tercatat pada kategori "
                    f"{existing_exam}; tidak dimasukkan ke kategori {exam_code}."
                )
                continue

            existing_pdf_value = same_content["pdf_path"]
            existing_pdf_path = Path(existing_pdf_value) if existing_pdf_value else None
            if (
                same_content["status"] == "BERHASIL"
                and existing_pdf_path is not None
                and existing_pdf_path.is_file()
            ):
                if not bool(same_content["merge_enabled"]):
                    # Upload eksplisit ulang pada kategori yang sama memasukkan
                    # kembali dokumen tersebut ke antrean proses yang baru.
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE documents SET merge_enabled=1 WHERE id=?",
                            (same_content["id"],),
                        )
                    result["reactivated_count"] += 1
                else:
                    result["duplicate_count"] += 1
                result["pdf_paths"].append(existing_pdf_path)
                continue
            # Untuk record gagal atau PDF yang hilang, lanjutkan ke jalur
            # retry existing di bawah; jangan menambah record duplikat.

        existing = get_document_record(
            batch_name, exam_code, original_name_key, file_hash
        )

        if existing:
            source_path = Path(existing["source_path"])
            pdf_value = existing["pdf_path"]
            pdf_path = Path(pdf_value) if pdf_value else None

            if not source_path.exists():
                atomic_write(source_path, file_bytes)

            if pdf_path and pdf_path.exists():
                valid, _ = validate_pdf(pdf_path)
                if valid:
                    clean_pdf_path = normalize_existing_pdf_name(
                        pdf_path, existing["original_name"], ready_dir
                    )
                    if clean_pdf_path != pdf_path:
                        with get_conn() as conn:
                            conn.execute(
                                "UPDATE documents SET pdf_path=? WHERE id=?",
                                (str(clean_pdf_path), existing["id"]),
                            )
                    result["duplicate_count"] += 1
                    result["pdf_paths"].append(clean_pdf_path)
                    continue

            # Jika hasil PDF hilang/rusak, proses ulang dengan nama PDF yang bersih.
            if pdf_path is None:
                pdf_path = get_unique_pdf_output_path(
                    ready_dir, Path(existing["original_name"]).stem
                )
            success, message = convert_source_to_pdf(source_path, pdf_path)
            if success:
                clean_pdf_path = normalize_existing_pdf_name(
                    pdf_path, existing["original_name"], ready_dir
                )
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE documents SET pdf_path=?, status='BERHASIL', error_message=NULL, merge_enabled=1 WHERE id=?",
                        (str(clean_pdf_path), existing["id"]),
                    )
                result["pdf_paths"].append(clean_pdf_path)
                result["duplicate_count"] += 1
            else:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE documents SET status='GAGAL', error_message=? WHERE id=?",
                        (message, existing["id"]),
                    )
                result["errors"].append(f"{original_name}: {message}")
            continue

        safe_stem = safe_filename(Path(original_name).stem)
        # Suffix hash hanya dipakai pada file sumber internal agar tidak tertimpa.
        # PDF siap unduh memakai nama pasien/pemeriksaan yang bersih.
        source_filename = f"{safe_stem}__{file_hash[:12]}{extension}"
        source_path = source_dir / source_filename
        pdf_path = get_unique_pdf_output_path(ready_dir, safe_stem)

        try:
            if not source_path.exists():
                atomic_write(source_path, file_bytes)

            success, message = convert_source_to_pdf(source_path, pdf_path)
            if success:
                # Kategori mengikuti dropdown yang dipilih petugas.
                # Hanya nama file yang secara eksplisit menyebut kategori lain
                # yang ditandai konflik. Jangan scan seluruh isi PDF untuk kategori,
                # karena isi laporan MCU dapat menyebut LAB/LPK sebagai istilah umum.
                detected_codes = detect_exam_codes(original_name)
                conflicting_codes = detected_codes - {exam_code}
                if conflicting_codes:
                    pdf_path.unlink(missing_ok=True)
                    success = False
                    message = (
                        f"Kategori yang dipilih {exam_code}, tetapi dokumen terindikasi "
                        f"sebagai {', '.join(sorted(conflicting_codes))}. "
                        "Periksa kategori upload sebelum mencoba lagi."
                    )
            status = "BERHASIL" if success else "GAGAL"
            stored_pdf_path = str(pdf_path) if success else None

            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO documents(
                        batch_name, exam_code, original_name, original_name_key,
                        file_hash, source_path, pdf_path, extension, size_bytes,
                        status, error_message, uploaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_name,
                        exam_code,
                        original_name,
                        original_name_key,
                        file_hash,
                        str(source_path),
                        stored_pdf_path,
                        extension,
                        len(file_bytes),
                        status,
                        None if success else message,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )

            if success:
                result["saved_count"] += 1
                result["pdf_paths"].append(pdf_path)
                if extension == ".pdf":
                    result["pdf_source_count"] += 1
                else:
                    result["converted_count"] += 1
            else:
                result["saved_count"] += 1  # Sumber tetap tersimpan untuk percobaan ulang.
                result["errors"].append(f"{original_name}: {message}")

        except sqlite3.IntegrityError:
            # Mengantisipasi upload bersamaan dengan file yang sama.
            duplicate = get_document_record(
                batch_name, exam_code, original_name_key, file_hash
            )
            if duplicate and duplicate["pdf_path"]:
                duplicate_pdf = Path(duplicate["pdf_path"])
                if duplicate_pdf.exists():
                    result["duplicate_count"] += 1
                    result["pdf_paths"].append(duplicate_pdf)
            else:
                result["errors"].append(
                    f"{original_name}: Catatan dokumen sudah ada tetapi hasil PDF belum tersedia."
                )
        except Exception as exc:
            result["errors"].append(f"{original_name}: Gagal menyimpan/proses ({exc}).")

    # Jangan tampilkan PDF yang sama dua kali pada daftar unduhan.
    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for path in result["pdf_paths"]:
        resolved = str(path.resolve())
        if resolved not in seen_paths and path.exists():
            seen_paths.add(resolved)
            unique_paths.append(path)
    result["pdf_paths"] = unique_paths
    return result


def convert_source_to_pdf(source_path: Path, pdf_path: Path) -> tuple[bool, str]:
    extension = source_path.suffix.lower()
    if extension == ".pdf":
        valid, message = validate_pdf(source_path)
        if not valid:
            return False, message
        try:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            if source_path.resolve() != pdf_path.resolve():
                shutil.copy2(source_path, pdf_path)
            return True, "OK"
        except Exception as exc:
            return False, str(exc)

    if extension in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        return convert_image_to_pdf(source_path, pdf_path)

    if extension in {".doc", ".docx"}:
        return convert_word_to_pdf(source_path, pdf_path)

    return False, f"Ekstensi {extension} tidak didukung untuk konversi."


def list_batch_documents(batch_name: str, exam_code: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM documents WHERE batch_name=?"
    params: list[Any] = [batch_name]
    if exam_code:
        query += " AND exam_code=?"
        params.append(exam_code)
    query += " ORDER BY exam_code, original_name"
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def list_conversion_documents(
    batch_name: str, exam_code: str | None = None
) -> list[sqlite3.Row]:
    """Daftar riwayat converter; terpisah dari dokumen Validasi & Merge."""
    query = "SELECT * FROM conversion_documents WHERE batch_name=?"
    params: list[Any] = [batch_name]
    if exam_code:
        query += " AND exam_code=?"
        params.append(exam_code)
    query += " ORDER BY exam_code, original_name"
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def _get_conversion_by_hash(
    batch_name: str, file_hash: str
) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM conversion_documents "
            "WHERE batch_name=? AND file_hash=? ORDER BY id LIMIT 1",
            (batch_name, file_hash),
        ).fetchone()


def _get_conversion_exact(
    batch_name: str,
    exam_code: str,
    original_name_key: str,
    file_hash: str,
) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM conversion_documents
            WHERE batch_name=? AND exam_code=? AND original_name_key=? AND file_hash=?
            """,
            (batch_name, exam_code, original_name_key, file_hash),
        ).fetchone()


def store_conversion_documents(
    uploaded_files: list[Any], batch_name: str, exam_code: str
) -> dict[str, Any]:
    """Konversi ke HASIL_KONVERSI; tidak menulis ke manifest Validasi & Merge."""
    result: dict[str, Any] = {
        "pdf_paths": [],
        "saved_count": 0,
        "duplicate_count": 0,
        "converted_count": 0,
        "pdf_source_count": 0,
        "errors": [],
    }

    if len(uploaded_files) > MAX_FILE_COUNT:
        result["errors"].append(f"Maksimal {MAX_FILE_COUNT} file sekali upload.")
        return result

    exam_dir = CONVERSION_DIR / batch_name / exam_code
    source_dir = exam_dir / "ASLI"
    ready_dir = exam_dir / "PDF_SIAP"
    source_dir.mkdir(parents=True, exist_ok=True)
    ready_dir.mkdir(parents=True, exist_ok=True)
    seen_current: set[str] = set()

    for uploaded in uploaded_files:
        validation_error = validate_upload(uploaded)
        if validation_error:
            result["errors"].append(f"{uploaded.name}: {validation_error}")
            continue

        original_name = safe_filename(uploaded.name)
        extension = Path(original_name).suffix.lower()
        original_name_key = original_name.casefold()

        try:
            file_bytes = uploaded.getvalue()
            if not file_bytes:
                result["errors"].append(f"{original_name}: File kosong.")
                continue
            file_hash = sha256_bytes(file_bytes)
        except Exception as exc:
            result["errors"].append(f"{original_name}: Gagal membaca upload ({exc}).")
            continue

        if file_hash in seen_current:
            result["duplicate_count"] += 1
            continue
        seen_current.add(file_hash)

        existing_exact = _get_conversion_exact(
            batch_name, exam_code, original_name_key, file_hash
        )
        if existing_exact:
            old_source = Path(existing_exact["source_path"])
            old_pdf = Path(existing_exact["pdf_path"]) if existing_exact["pdf_path"] else None

            if not old_source.is_file():
                atomic_write(old_source, file_bytes)

            if old_pdf and old_pdf.is_file():
                valid, _ = validate_pdf(old_pdf)
                if valid and existing_exact["status"] == "BERHASIL":
                    result["duplicate_count"] += 1
                    result["pdf_paths"].append(old_pdf)
                    continue

            if old_pdf is None:
                old_pdf = get_unique_pdf_output_path(
                    ready_dir, Path(original_name).stem
                )
            else:
                old_pdf.parent.mkdir(parents=True, exist_ok=True)

            success, message = convert_source_to_pdf(old_source, old_pdf)
            if success:
                name_hints = detect_exam_codes(original_name)
                conflicts = name_hints - {exam_code}
                if conflicts:
                    old_pdf.unlink(missing_ok=True)
                    success = False
                    message = (
                        f"Nama file mengindikasikan kategori "
                        f"{', '.join(sorted(conflicts))}, bukan {exam_code}. "
                        "Periksa kategori sebelum menyimpan."
                    )

            with get_conn() as conn:
                conn.execute(
                    "UPDATE conversion_documents SET pdf_path=?, status=?, "
                    "error_message=? WHERE id=?",
                    (
                        str(old_pdf) if success else None,
                        "BERHASIL" if success else "GAGAL",
                        None if success else message,
                        existing_exact["id"],
                    ),
                )

            if success:
                result["duplicate_count"] += 1
                result["pdf_paths"].append(old_pdf)
            else:
                result["errors"].append(f"{original_name}: {message}")
            continue

        # Hash yang sudah berhasil disimpan pada kategori lain tidak boleh disalin.
        # Akan tetapi, catatan GAGAL akibat salah pilih kategori boleh diunggah ulang
        # ke kategori yang benar tanpa menghapus catatan riwayat sebelumnya.
        same_content = _get_conversion_by_hash(batch_name, file_hash)
        if same_content:
            previous_error = (same_content["error_message"] or "").lower()
            retry_after_category_error = (
                same_content["status"] == "GAGAL"
                and "kategori" in previous_error
            )
            if not retry_after_category_error:
                result["duplicate_count"] += 1
                result["errors"].append(
                    f"{original_name}: konten identik sudah tersimpan pada kategori "
                    f"{same_content['exam_code']}; tidak dibuat salinan baru."
                )
                continue

        safe_stem = safe_filename(Path(original_name).stem)
        source_filename = f"{safe_stem}__{file_hash[:12]}{extension}"
        source_path = source_dir / source_filename
        pdf_path = get_unique_pdf_output_path(ready_dir, safe_stem)

        try:
            if not source_path.exists():
                atomic_write(source_path, file_bytes)

            success, message = convert_source_to_pdf(source_path, pdf_path)
            if success:
                name_hints = detect_exam_codes(original_name)
                conflicts = name_hints - {exam_code}
                if conflicts:
                    pdf_path.unlink(missing_ok=True)
                    success = False
                    message = (
                        f"Nama file mengindikasikan kategori "
                        f"{', '.join(sorted(conflicts))}, bukan {exam_code}. "
                        "Periksa kategori sebelum menyimpan."
                    )

            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO conversion_documents(
                        batch_name, exam_code, original_name, original_name_key,
                        file_hash, source_path, pdf_path, extension, size_bytes,
                        status, error_message, uploaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_name,
                        exam_code,
                        original_name,
                        original_name_key,
                        file_hash,
                        str(source_path),
                        str(pdf_path) if success else None,
                        extension,
                        len(file_bytes),
                        "BERHASIL" if success else "GAGAL",
                        None if success else message,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )

            if success:
                result["saved_count"] += 1
                result["pdf_paths"].append(pdf_path)
                if extension == ".pdf":
                    result["pdf_source_count"] += 1
                else:
                    result["converted_count"] += 1
            else:
                # File asli tetap berada di folder converter untuk pemeriksaan/percobaan ulang.
                result["saved_count"] += 1
                result["errors"].append(f"{original_name}: {message}")

        except sqlite3.IntegrityError:
            duplicate = _get_conversion_exact(
                batch_name, exam_code, original_name_key, file_hash
            )
            if duplicate and duplicate["pdf_path"]:
                duplicate_pdf = Path(duplicate["pdf_path"])
                if duplicate_pdf.is_file():
                    result["duplicate_count"] += 1
                    result["pdf_paths"].append(duplicate_pdf)
            else:
                result["errors"].append(
                    f"{original_name}: Catatan konversi sudah ada, tetapi PDF belum tersedia."
                )
        except Exception as exc:
            result["errors"].append(f"{original_name}: Gagal menyimpan/konversi ({exc}).")

    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for path in result["pdf_paths"]:
        resolved = str(path.resolve())
        if resolved not in seen_paths and path.is_file():
            seen_paths.add(resolved)
            unique_paths.append(path)
    result["pdf_paths"] = unique_paths
    return result


def migrate_legacy_converter_records() -> int:
    """Pindahkan riwayat DOC/DOCX/gambar lama dari manifest Validasi ke manifest converter.

    File PDF lama tidak dimigrasikan karena versi sebelumnya memakai satu tabel
    untuk PDF converter dan PDF yang sengaja di-upload pada menu Validasi.
    Asal PDF lama tidak dapat dibedakan dengan aman.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE LOWER(extension) != '.pdf' ORDER BY id"
        ).fetchall()

    migrated = 0
    for row in rows:
        old_source = Path(row["source_path"])
        old_pdf = Path(row["pdf_path"]) if row["pdf_path"] else None
        category_dir = CONVERSION_DIR / row["batch_name"] / row["exam_code"]
        new_source_dir = category_dir / "ASLI"
        new_pdf_dir = category_dir / "PDF_SIAP"
        new_source_dir.mkdir(parents=True, exist_ok=True)
        new_pdf_dir.mkdir(parents=True, exist_ok=True)

        moved_source = old_source
        moved_pdf = old_pdf
        try:
            if old_source.is_file():
                source_target = new_source_dir / old_source.name
                if source_target.exists():
                    source_target = get_unique_filepath(new_source_dir, old_source.name)
                shutil.move(str(old_source), str(source_target))
                moved_source = source_target

            if old_pdf and old_pdf.is_file():
                pdf_target = new_pdf_dir / old_pdf.name
                if pdf_target.exists():
                    pdf_target = get_unique_filepath(new_pdf_dir, old_pdf.name)
                shutil.move(str(old_pdf), str(pdf_target))
                moved_pdf = pdf_target

            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO conversion_documents(
                        batch_name, exam_code, original_name, original_name_key,
                        file_hash, source_path, pdf_path, extension, size_bytes,
                        status, error_message, uploaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["batch_name"], row["exam_code"], row["original_name"],
                        row["original_name_key"], row["file_hash"], str(moved_source),
                        str(moved_pdf) if moved_pdf else None, row["extension"],
                        row["size_bytes"], row["status"], row["error_message"],
                        row["uploaded_at"],
                    ),
                )
                conn.execute("DELETE FROM documents WHERE id=?", (row["id"],))
            migrated += 1
        except Exception:
            # Jangan buang data jika migrasi satu record gagal; kembalikan file ke asal.
            try:
                if moved_pdf and old_pdf and moved_pdf != old_pdf and moved_pdf.exists():
                    old_pdf.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(moved_pdf), str(old_pdf))
                if moved_source != old_source and moved_source.exists():
                    old_source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(moved_source), str(old_source))
            except Exception:
                pass
            continue

    return migrated


def migrate_legacy_pdf_filenames() -> int:
    """Hilangkan suffix hash dari PDF lama yang tercatat di manifest DB."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, original_name, pdf_path FROM documents "
            "WHERE status='BERHASIL' AND pdf_path IS NOT NULL ORDER BY id"
        ).fetchall()

    renamed_count = 0
    for row in rows:
        old_path = Path(row["pdf_path"])
        if not old_path.is_file():
            continue

        new_path = normalize_existing_pdf_name(
            old_path,
            row["original_name"],
            old_path.parent,
        )
        if new_path.resolve() != old_path.resolve():
            with get_conn() as conn:
                conn.execute(
                    "UPDATE documents SET pdf_path=? WHERE id=?",
                    (str(new_path), row["id"]),
                )
            renamed_count += 1

    return renamed_count


# ============================================================
# 6. IDENTIFIKASI KANDIDAT NAMA PASIEN, OCR HYBRID, DAN MATCHING LANJUTAN
# ============================================================


def normalize_patient_name(value: str) -> str:
    value = str(value or "").upper().strip()
    value = re.sub(r"[^A-Z0-9À-ÖØ-öø-ÿ ]+", " ", value)
    return " ".join(value.split())


def normalize_patient_identity_name(value: str) -> str:
    """Normalisasi identitas tanpa mengubah nama file asli atau data sumber.

    Menghapus gelar umum pada awalan/akhiran, tetapi mempertahankan kata
    penghubung nama seperti BIN dan BINTI.
    """
    cleaned = normalize_patient_name(value)
    if not cleaned:
        return ""

    # Gelar awalan yang umum tercantum di dokumen formal.
    cleaned = re.sub(r"^(?:(?:PROF|PROFESOR|DR|DRS|DRA|IR|HJ|HJH)\s+)+", "", cleaned)

    # Gelar akademik/profesi yang lazim menempel pada nama di Indonesia.
    suffixes = (
        "S KOM", "M KOM", "S KED", "M KES", "S KM", "M KM", "S FARM",
        "APT", "S GZ", "M GZ", "S PSI", "M PSI", "S SOS", "M SOS",
        "S T", "M T", "S E", "M M", "M SI", "S SI", "S H", "M H",
        "S IP", "M IP", "S PD", "M PD", "S P", "M P", "S DS",
        "S KEP", "N S", "S TR KES", "S TR KEB", "S TR AK",
    )
    changed = True
    while changed and cleaned:
        changed = False
        for suffix in sorted(suffixes, key=len, reverse=True):
            if cleaned == suffix:
                cleaned = ""
                changed = True
                break
            if cleaned.endswith(" " + suffix):
                cleaned = cleaned[: -(len(suffix) + 1)].strip()
                changed = True
                break

    return " ".join(cleaned.split())


def extract_name_from_filename(filename: str, exam_code: str = "") -> str:
    stem = Path(filename).stem
    stem = re.sub(r"(?:[_ -]+\d+|\(\d+\))$", "", stem, flags=re.IGNORECASE)
    if exam_code:
        stem = re.sub(
            rf"(?:^|[ _-]){re.escape(exam_code)}(?:$|[ _-])",
            " ", stem, flags=re.IGNORECASE
        )
    for keyword in sorted(MEDICAL_KEYWORDS, key=len, reverse=True):
        stem = re.sub(rf"\b{re.escape(keyword)}\b", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\b(?:COPY|SALINAN|SCAN|SCANNED|COPYAN)\b", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-.()]+", " ", stem)
    return normalize_patient_identity_name(stem)


def extract_text_from_pdf(filepath: Path) -> str:
    """Ekstraksi text layer PDF dengan urutan baca yang lebih konsisten."""
    pieces: list[str] = []
    try:
        with fitz.open(filepath) as document:
            for page in document:
                try:
                    page_text = page.get_text("text", sort=True)
                except TypeError:
                    # Kompatibilitas dengan versi PyMuPDF yang belum mendukung sort.
                    page_text = page.get_text("text")
                pieces.append(page_text)
    except Exception:
        return ""
    return "\n".join(pieces)


def find_name_in_pdf_text(text: str) -> str:
    """Ekstrak nama dari field identitas, termasuk label/nilai yang terpecah.

    Mendukung format seperti:
      Nama : UCOK FADILAH
      Nama :\nUCOK FADILAH
      Nama\n:\nUCOK FADILAH
      Nama\nUCOK FADILAH
      Nama Pasien    UCOK FADILAH

    Baris delimiter tunggal (':', '-', '#') dilewati. Pencarian dihentikan
    ketika field identitas lain dimulai agar nilai MRN/registrasi tidak dibaca
    sebagai nama. Frasa bebas seperti "Pasien Puasa 13 Jam" tetap ditolak.
    """
    if not text:
        return ""

    raw_lines = [str(line).rstrip("\r\n") for line in str(text).splitlines()]
    lines = [re.sub(r"[ \t]+", " ", line).strip(" \t|") for line in raw_lines]

    name_label = r"(?:nama(?:\s+(?:pasien|lengkap))?|patient\s+name|name)"
    inline_name_re = re.compile(
        rf"(?i)^\s*{name_label}\s*[:\-]\s*(.*?)\s*$"
    )
    label_only_re = re.compile(rf"(?i)^\s*{name_label}\s*[:\-]?\s*$")
    table_name_re = re.compile(
        rf"(?i)^\s*{name_label}\s{{2,}}(.+?)\s*$"
    )

    # Label berikut menandakan bahwa baris sesudahnya bukan nilai nama.
    next_field_re = re.compile(
        r"(?i)^\s*(?:MRN|NO\.?\s*(?:TRANSAKSI|RM|REKAM(?:\s*MEDIS)?|"
        r"REGISTRASI|REG)|NOMOR\s*(?:TRANSAKSI|REKAM\s*MEDIS)|"
        r"TANGGAL\s*LAHIR|TGL\.?\s*LAHIR|DATE\s*OF\s*BIRTH|DOB|"
        r"N\.?I\.?K\.?|NIK|JENIS\s*KELAMIN|KELAMIN|UMUR|USIA|"
        r"ALAMAT|TEMPAT\s*LAHIR|POLIKLINIK|DOKTER|DOKTER\s*PEMERIKSA|"
        r"PEMERIKSA|NAMA\s*(?:DOKTER|PEMERIKSA|PENGUJI))\b"
    )
    stop_re = re.compile(
        r"(?i)\s+(?=(?:MRN|NO\.?\s*(?:TRANSAKSI|RM|REKAM|REGISTRASI|REG)|"
        r"NOMOR\s*(?:TRANSAKSI|REKAM\s*MEDIS)|TANGGAL\s*LAHIR|"
        r"TGL\.?\s*LAHIR|DATE\s*OF\s*BIRTH|DOB|N\.?I\.?K\.?|NIK|"
        r"JENIS\s*KELAMIN|KELAMIN|UMUR|USIA|ALAMAT|TEMPAT\s*LAHIR|"
        r"POLIKLINIK|DOKTER|PEMERIKSA)\s*[:#\-])"
    )
    invalid_values = {
        "NAMA", "NAMA PASIEN", "NAMA LENGKAP", "NAME", "PATIENT NAME",
        "PASIEN", "NO", "NIK", "NO RM", "NOMOR REKAM MEDIS",
    }
    invalid_name_tokens = {
        "PUASA", "JAM", "MENIT", "HARI", "FASTING", "HOUR", "HOURS",
        "PEMERIKSAAN", "PEMERIKSAANMCU", "MCU", "HASIL", "NORMAL",
        "ABNORMAL", "NEGATIF", "POSITIF", "LAPORAN", "PASIEN",
        "LAKI", "PEREMPUAN", "PRIA", "WANITA", "JENIS", "KELAMIN",
        "UMUR", "USIA", "TANGGAL", "LAHIR", "ALAMAT", "DOKTER",
        "PEMERIKSA", "PENGUJI", "POLIKLINIK", "LABORATORIUM", "RADIOLOGI",
        "RONTGEN", "SPIRO", "VISUS", "AUDIO", "EKG", "LAB", "LPK",
    }

    def clean_candidate(raw_value: str) -> str:
        value = str(raw_value or "").strip(" \t:;,.|#-")
        value = stop_re.split(value, maxsplit=1)[0].strip(" \t:;,.|#-")
        value = re.sub(
            rf"(?i)^{name_label}\s*[:\-]?\s*", "", value
        ).strip(" \t:;,.|#-")
        cleaned = normalize_patient_identity_name(value)
        if not cleaned or cleaned in invalid_values:
            return ""
        if re.search(r"\d", cleaned):
            return ""
        if set(cleaned.split()) & invalid_name_tokens:
            return ""
        if re.fullmatch(r"[\d\s./-]+", cleaned):
            return ""
        if re.search(r"\b(?:LAC|TRX|TRANSAKSI)\b", cleaned, re.IGNORECASE):
            return ""
        if len(cleaned) < 3 or len(cleaned.split()) > 8:
            return ""
        return cleaned

    for index, line in enumerate(lines):
        if not line:
            continue

        # 1. Nama dan nilai pada satu baris dengan delimiter eksplisit.
        match = inline_name_re.match(line)
        if match:
            candidate = clean_candidate(match.group(1))
            if candidate:
                return candidate

        # 2. Label sendiri atau label diikuti delimiter pada baris terpisah.
        if label_only_re.match(line):
            for next_line in lines[index + 1:index + 7]:
                candidate_line = next_line.strip()
                if not candidate_line or re.fullmatch(r"[:#|./\\-]+", candidate_line):
                    continue
                if next_field_re.match(candidate_line):
                    break
                candidate = clean_candidate(candidate_line)
                if candidate:
                    return candidate
                # Baris delimiter sudah dilewati di atas. Jika baris berikutnya
                # bukan field lain dan juga bukan kandidat nama, hentikan pencarian
                # agar teks lain (misalnya nama perusahaan) tidak terambil.
                break

        # 3. PDF tabel: label dan nilai dipisahkan oleh beberapa spasi/tab.
        raw_line = raw_lines[index]
        table_match = table_name_re.match(raw_line)
        if table_match:
            candidate = clean_candidate(table_match.group(1))
            if candidate:
                return candidate

    return ""

def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _hash_identity_value(value: str) -> str:
    normalized = _normalize_identifier(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _mask_identifier(value: str) -> str:
    normalized = _normalize_identifier(value)
    if not normalized:
        return ""
    return "*" * max(0, len(normalized) - 4) + normalized[-4:]


def _extract_labeled_value(text: str, labels: str) -> str:
    pattern = re.compile(
        rf"(?:{labels})\s*[:#\-]?\s*([A-Z0-9./ -]{{3,}})",
        re.IGNORECASE,
    )
    match = pattern.search(text or "")
    if not match:
        return ""
    value = re.split(
        r"\s{2,}|\s+(?:NAMA|NAME|TANGGAL\s+LAHIR|TGL\s+LAHIR|DOB|"
        r"JENIS\s+KELAMIN|UMUR|USIA|ALAMAT|NIK|NO\.?\s*RM|MRN)\s*[:#\-]",
        match.group(1),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return value.strip(" .:/-\t\r\n")


def _extract_mrn_value(text: str) -> tuple[str, str, str]:
    """Ambil MRN/No. RM dari label yang eksplisit.

    No. Registrasi, No. Transaksi, dan Patient ID tidak dianggap sebagai MRN.
    Jika lebih dari satu nomor MRN berbeda ditemukan dalam satu PDF, hasil
    dibuat ambigu agar aplikasi tidak menyatakan dua pasien cocok secara keliru.
    """
    if not text:
        return "", "", "NOT_FOUND"

    lines = [str(line).strip() for line in str(text).splitlines()]
    inline_pattern = re.compile(
        r"(?i)(?<![A-Z0-9])(?P<label>MRN|NO\.?\s*RM|NO\.?\s*REKAM\s*MEDIS|"
        r"NOMOR\s*REKAM\s*MEDIS)(?![A-Z0-9])\s*[:#\-]?\s*"
        r"(?P<value>[A-Z0-9][A-Z0-9./-]{3,23})(?![A-Z0-9])"
    )
    label_only_pattern = re.compile(
        r"(?i)^\s*(?P<label>MRN|NO\.?\s*RM|NO\.?\s*REKAM\s*MEDIS|"
        r"NOMOR\s*REKAM\s*MEDIS)\s*[:#\-]?\s*$"
    )
    value_only_pattern = re.compile(r"(?i)^\s*[:#\-]?\s*([A-Z0-9][A-Z0-9./-]{3,23})\s*$")
    candidates: dict[str, set[str]] = {}

    for index, line in enumerate(lines):
        if not line:
            continue
        for match in inline_pattern.finditer(line):
            label = re.sub(r"\s+", " ", match.group("label").upper()).strip()
            value = _normalize_identifier(match.group("value"))
            if 4 <= len(value) <= 24:
                candidates.setdefault(value, set()).add(label)

        label_match = label_only_pattern.match(line)
        if label_match:
            label = re.sub(r"\s+", " ", label_match.group("label").upper()).strip()
            # Bentuk ekstraksi PDF tertentu memisahkan label, titik dua, dan nilai.
            for next_line in lines[index + 1:index + 4]:
                candidate_line = next_line.strip()
                if not candidate_line:
                    continue
                if candidate_line in {":", "-", "#"}:
                    continue
                value_match = value_only_pattern.match(candidate_line)
                if value_match:
                    value = _normalize_identifier(value_match.group(1))
                    if 4 <= len(value) <= 24:
                        candidates.setdefault(value, set()).add(label)
                break

    if not candidates:
        return "", "", "NOT_FOUND"
    if len(candidates) > 1:
        return "", "AMBIGUOUS", "AMBIGUOUS"

    value, labels = next(iter(candidates.items()))
    preferred = next((label for label in sorted(labels) if label == "MRN"), None)
    source_label = preferred or sorted(labels)[0]
    return value, source_label, "OK"


def extract_identity_fields(text: str) -> dict[str, str]:
    """Ekstrak identitas. MRN hanya dari label MRN/No. RM eksplisit."""
    nik_value = _extract_labeled_value(
        text,
        r"(?:NIK|N\.I\.K\.?|NO\.?\s*KTP|NOMOR\s*INDUK\s*KEPENDUDUKAN)",
    )
    nik_digits = re.sub(r"\D", "", nik_value)
    nik_digits = nik_digits[:16] if len(nik_digits) >= 16 else ""
    if nik_digits == "0" * 16:
        nik_digits = ""

    mr_value, mr_source_label, mr_extraction_status = _extract_mrn_value(text)
    mr_normalized = _normalize_identifier(mr_value)

    dob_value = _extract_labeled_value(
        text,
        r"(?:TANGGAL\s*LAHIR|TGL\.?\s*LAHIR|DATE\s*OF\s*BIRTH|DOB)",
    )
    dob_iso = ""
    date_match = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b", dob_value)
    if date_match:
        raw_date = date_match.group(1)
        date_formats = ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d")
        for date_format in date_formats:
            try:
                parsed = datetime.strptime(raw_date, date_format)
                if 1900 <= parsed.year <= datetime.now().year:
                    dob_iso = parsed.strftime("%Y-%m-%d")
                    break
            except ValueError:
                continue

    return {
        "nik_hash": _hash_identity_value(nik_digits),
        "nik_masked": _mask_identifier(nik_digits),
        "mr_hash": _hash_identity_value(mr_normalized),
        "mr_masked": _mask_identifier(mr_normalized),
        "mr_source_label": mr_source_label,
        "mr_extraction_status": mr_extraction_status,
        "dob_iso": dob_iso,
    }


def get_ocr_cache(file_hash: str, page_number: int, language: str, engine_version: str) -> dict[str, Any] | None:
    try:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT extracted_text, confidence FROM ocr_cache
                   WHERE file_hash=? AND page_number=? AND language=? AND engine_version=?""",
                (file_hash, page_number, language, engine_version),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def save_ocr_cache(
    file_hash: str, page_number: int, language: str,
    engine_version: str, extracted_text: str, confidence: float | None,
) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO ocr_cache
                   (file_hash, page_number, language, engine_version, extracted_text, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    file_hash, page_number, language, engine_version,
                    extracted_text, confidence,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
    except sqlite3.Error:
        # OCR tetap dapat berjalan walaupun cache tidak tersedia.
        pass


@lru_cache(maxsize=4)
def get_tesseract_status() -> dict[str, Any]:
    """Periksa wrapper, binary Tesseract, versi, dan paket bahasa yang tersedia."""
    if pytesseract is None:
        return {
            "available": False,
            "version": "Tidak tersedia",
            "languages": [],
            "message": (
                "Modul Python pytesseract belum terpasang. Jalankan "
                "'python -m pip install pytesseract' pada environment yang menjalankan Streamlit. "
                "Binary Tesseract OCR juga harus terpasang terpisah. "
                f"Detail: {PYTESSERACT_IMPORT_ERROR}"
            ),
        }
    try:
        version = str(pytesseract.get_tesseract_version()).splitlines()[0]
        languages = list(pytesseract.get_languages(config=""))
        requested = [item.strip() for item in OCR_LANGUAGE.split("+") if item.strip()]
        missing = [item for item in requested if item not in languages]
        message = "Tesseract siap digunakan."
        if missing:
            message = (
                "Paket bahasa belum lengkap: " + ", ".join(missing)
                + ". Sistem akan mencoba bahasa Inggris jika tersedia."
            )
        return {
            "available": True,
            "version": version,
            "languages": languages,
            "message": message,
        }
    except Exception as exc:
        return {
            "available": False,
            "version": "Tidak terdeteksi",
            "languages": [],
            "message": f"Binary Tesseract belum terdeteksi atau gagal dijalankan: {exc}",
        }


def _resolve_ocr_language(status: dict[str, Any]) -> str:
    requested = [item.strip() for item in OCR_LANGUAGE.split("+") if item.strip()]
    available = set(status.get("languages", []))
    if requested and all(item in available for item in requested):
        return "+".join(requested)
    if "eng" in available:
        return "eng"
    return requested[0] if requested else "eng"


def run_tesseract_on_page(
    page: fitz.Page, file_hash: str, page_number: int,
) -> tuple[str, float | None, str]:
    """Render satu halaman, preprocessing ringan, OCR, lalu cache hasilnya."""
    status = get_tesseract_status()
    if not status["available"] or pytesseract is None:
        return "", None, status["message"]

    language = _resolve_ocr_language(status)
    engine_version = status["version"]
    cached = get_ocr_cache(file_hash, page_number, language, engine_version)
    if cached is not None:
        return str(cached.get("extracted_text") or ""), cached.get("confidence"), "CACHE"

    try:
        scale = OCR_DPI / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        gray = ImageOps.grayscale(image)
        gray = ImageOps.autocontrast(gray)

        # Naikkan resolusi kecil secara proporsional agar karakter hasil scan lebih terbaca.
        if gray.width < 1500:
            ratio = 1500 / max(1, gray.width)
            gray = gray.resize((int(gray.width * ratio), int(gray.height * ratio)), Image.Resampling.LANCZOS)

        data = pytesseract.image_to_data(
            gray,
            lang=language,
            config="--oem 3 --psm 6",
            output_type=TesseractOutput.DICT,
        )
        line_parts: list[str] = []
        current_line: tuple[int, int, int] | None = None
        current_words: list[str] = []
        confidences: list[float] = []
        for index, raw_word in enumerate(data.get("text", [])):
            word = str(raw_word).strip()
            if not word:
                continue
            line_key = (
                int(data.get("block_num", [0])[index]),
                int(data.get("par_num", [0])[index]),
                int(data.get("line_num", [0])[index]),
            )
            if current_line is not None and line_key != current_line:
                if current_words:
                    line_parts.append(" ".join(current_words))
                current_words = []
            current_line = line_key
            current_words.append(word)
            try:
                confidence = float(data.get("conf", [])[index])
                if confidence >= 0:
                    confidences.append(confidence)
            except (ValueError, TypeError, IndexError):
                pass
        if current_words:
            line_parts.append(" ".join(current_words))

        extracted_text = "\n".join(line_parts).strip()
        confidence_avg = round(sum(confidences) / len(confidences), 1) if confidences else None
        save_ocr_cache(
            file_hash, page_number, language, engine_version,
            extracted_text, confidence_avg,
        )
        gray.close()
        image.close()
        return extracted_text, confidence_avg, "OCR"
    except Exception as exc:
        return "", None, f"OCR gagal: {exc}"



def extract_hybrid_text_from_pdf(
    filepath: Path,
    file_hash: str,
    enable_ocr: bool = True,
    filename_candidate: str = "",
) -> dict[str, Any]:
    """Utamakan text layer; OCR fallback atau cross-check bila sumber nama berbeda."""
    text_layer = extract_text_from_pdf(filepath)
    text_candidate = find_name_in_pdf_text(text_layer)
    text_is_sufficient = len(text_layer.strip()) >= OCR_MIN_TEXT_CHARS and bool(text_candidate)

    filename_name = normalize_patient_identity_name(filename_candidate)
    text_name = normalize_patient_identity_name(text_candidate)
    crosscheck_required = bool(
        filename_name
        and text_name
        and advanced_name_similarity(filename_name, text_name)["score"] < 90
    )
    ocr_required = (not text_is_sufficient) or crosscheck_required

    ocr_text_parts: list[str] = []
    ocr_confidences: list[float] = []
    ocr_messages: list[str] = []
    ocr_used = False
    ocr_cache_used = False

    if enable_ocr and ocr_required:
        try:
            with fitz.open(filepath) as document:
                page_limit = min(document.page_count, OCR_MAX_PAGES)
                for page_index in range(page_limit):
                    ocr_used = True
                    page_text, confidence, message = run_tesseract_on_page(
                        document.load_page(page_index), file_hash, page_index + 1
                    )
                    if page_text:
                        ocr_text_parts.append(page_text)
                    if message == "CACHE":
                        ocr_cache_used = True
                    if confidence is not None:
                        ocr_confidences.append(float(confidence))
                    if message and message not in {"OCR", "CACHE"}:
                        ocr_messages.append(message)
        except Exception as exc:
            ocr_messages.append(f"Gagal membuka halaman untuk OCR: {exc}")

    ocr_text = "\n".join(ocr_text_parts).strip()
    combined_text = "\n".join(part for part in (text_layer, ocr_text) if part).strip()
    ocr_candidate = find_name_in_pdf_text(ocr_text)
    all_text_candidate = text_candidate or ocr_candidate or find_name_in_pdf_text(combined_text)
    identities = extract_identity_fields(combined_text)

    if text_candidate and ocr_candidate:
        source_score = advanced_name_similarity(text_candidate, ocr_candidate)["score"]
        source_conflict = source_score < 90
    else:
        source_score = None
        source_conflict = False

    if ocr_text and text_candidate and crosscheck_required:
        extraction_source = "TEXT_LAYER + OCR CROSS-CHECK"
        ocr_status = (
            "OCR cache digunakan untuk memeriksa perbedaan nama."
            if ocr_cache_used else "OCR berhasil dijalankan untuk memeriksa perbedaan nama."
        )
    elif text_is_sufficient and not ocr_used:
        extraction_source = "TEXT_LAYER"
        ocr_status = "Tidak diperlukan; nama ditemukan di text layer."
    elif ocr_text:
        extraction_source = "TEXT_LAYER + OCR" if text_layer.strip() else "OCR"
        ocr_status = "Cache OCR digunakan." if ocr_cache_used else "OCR berhasil."
    elif not enable_ocr:
        extraction_source = "TEXT_LAYER (nama tidak ditemukan)" if not text_candidate else "TEXT_LAYER"
        ocr_status = "OCR dinonaktifkan pada sesi ini."
    elif not get_tesseract_status()["available"]:
        extraction_source = "TEXT_LAYER (nama tidak ditemukan)" if not text_candidate else "TEXT_LAYER"
        ocr_status = get_tesseract_status()["message"]
    else:
        extraction_source = "TEXT_LAYER"
        ocr_status = "; ".join(ocr_messages) if ocr_messages else (
            "OCR tidak menghasilkan teks. Periksa kualitas scan dan batas halaman OCR."
        )

    return {
        "text_layer": text_layer,
        "ocr_text": ocr_text,
        "combined_text": combined_text,
        "name_from_text": text_candidate,
        "name_from_ocr": ocr_candidate,
        "name_candidate": all_text_candidate,
        "source_conflict": source_conflict,
        "source_similarity_score": source_score,
        "identity_fields": identities,
        "extraction_source": extraction_source,
        "ocr_confidence": round(sum(ocr_confidences) / len(ocr_confidences), 1) if ocr_confidences else None,
        "ocr_status": ocr_status,
        "ocr_attempted": ocr_used,
    }



def select_patient_name_candidate(
    filename_candidate: str,
    text_candidate: str,
    ocr_candidate: str,
    ocr_confidence: float | None,
    source_conflict: bool = False,
) -> dict[str, Any]:
    """Gunakan nama dalam PDF sebagai hint utama; filename hanya fallback.

    OCR di bawah ambang tetap ditampilkan sebagai kandidat nama dan ditandai
    wajib diperiksa, bukan diabaikan sehingga sistem diam-diam memakai filename.
    """
    filename_name = normalize_patient_identity_name(filename_candidate)
    text_name = normalize_patient_identity_name(text_candidate)
    ocr_name = normalize_patient_identity_name(ocr_candidate)

    low_confidence_ocr_review = bool(
        ocr_name
        and not text_name
        and (
            ocr_confidence is None
            or float(ocr_confidence) < OCR_NAME_MIN_CONFIDENCE
        )
    )

    if text_name:
        chosen_name = text_name
        name_source = "PDF_TEXT_LAYER"
    elif ocr_name:
        chosen_name = ocr_name
        name_source = (
            "PDF_OCR_LOW_CONFIDENCE"
            if low_confidence_ocr_review else "PDF_OCR"
        )
    elif filename_name:
        chosen_name = filename_name
        name_source = "FILENAME_FALLBACK"
    else:
        chosen_name = "NAMA_TIDAK_TERDETEKSI"
        name_source = "UNDETECTED"

    document_name = text_name or ocr_name
    # Jangan pernah menganggap nama filename sebagai identitas yang sudah terbukti.
    # Jika nama di dalam PDF tidak berhasil dibaca, persetujuan wajib ditahan.
    name_not_detected_review = not bool(document_name)
    mismatch_score = (
        advanced_name_similarity(filename_name, document_name)["score"]
        if filename_name and document_name else None
    )
    name_mismatch = bool(mismatch_score is not None and mismatch_score < 90)

    return {
        "chosen_name": chosen_name,
        "name_source": name_source,
        "document_name": document_name,
        "name_mismatch": name_mismatch,
        "name_similarity_score": mismatch_score,
        "low_confidence_ocr_review": low_confidence_ocr_review,
        "name_not_detected_review": name_not_detected_review,
        "source_conflict": bool(source_conflict),
    }


def analyze_batch_documents(
    batch_name: str, enable_ocr: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Analisis antrean aktif; text layer diprioritaskan dan OCR menjadi fallback."""
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

        # Nama yang dibaca dari field identitas di dalam PDF menjadi dasar utama.
        # Filename hanya menjadi fallback/pembanding agar salah judul PDF tidak
        # menyeret dokumen ke kelompok pasien yang keliru.
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
            "name_mismatch": name_decision["name_mismatch"],
            "name_similarity_score": name_decision["name_similarity_score"],
            "low_confidence_ocr_review": name_decision["low_confidence_ocr_review"],
            "name_not_detected_review": name_decision["name_not_detected_review"],
            "source_conflict": name_decision["source_conflict"],
            "source_similarity_score": extracted.get("source_similarity_score"),
            "name_review_required": (
                name_decision["name_mismatch"]
                or name_decision["low_confidence_ocr_review"]
                or name_decision["name_not_detected_review"]
                or name_decision["source_conflict"]
            ),
            "extraction_source": extracted["extraction_source"],
            "ocr_confidence": extracted["ocr_confidence"],
            "ocr_status": extracted["ocr_status"],
            "ocr_attempted": extracted["ocr_attempted"],
            "nik_hash": identity_fields["nik_hash"],
            "nik_masked": identity_fields["nik_masked"],
            "mr_hash": identity_fields["mr_hash"],
            "mr_masked": identity_fields["mr_masked"],
            "mr_source_label": identity_fields.get("mr_source_label", ""),
            "mr_extraction_status": identity_fields.get("mr_extraction_status", "NOT_FOUND"),
            "dob_iso": identity_fields["dob_iso"],
            "extension": ".pdf",
        }
        candidates.setdefault(group_key, []).append(info)

    return candidates


def advanced_name_similarity(name_a: str, name_b: str) -> dict[str, Any]:
    """Skor nama gabungan dari beberapa algoritma RapidFuzz."""
    a = normalize_patient_identity_name(name_a)
    b = normalize_patient_identity_name(name_b)
    if not a or not b:
        return {"score": 0.0, "algorithms": {}}
    if a == b:
        return {
            "score": 100.0,
            "algorithms": {
                "ratio": 100.0, "partial_ratio": 100.0,
                "token_sort_ratio": 100.0, "token_set_ratio": 100.0,
                "WRatio": 100.0, "QRatio": 100.0,
            },
        }

    scores = {
        "ratio": float(fuzz.ratio(a, b)),
        "partial_ratio": float(fuzz.partial_ratio(a, b)),
        "token_sort_ratio": float(fuzz.token_sort_ratio(a, b)),
        "token_set_ratio": float(fuzz.token_set_ratio(a, b)),
        "WRatio": float(fuzz.WRatio(a, b)),
        "QRatio": float(fuzz.QRatio(a, b)),
    }
    weighted = (
        scores["ratio"] * 0.15
        + scores["partial_ratio"] * 0.10
        + scores["token_sort_ratio"] * 0.20
        + scores["token_set_ratio"] * 0.20
        + scores["WRatio"] * 0.25
        + scores["QRatio"] * 0.10
    )
    # Mengurangi false positive saat satu nama hanya berupa potongan sangat pendek.
    tokens_a = a.split()
    tokens_b = b.split()
    shorter = min(len(a), len(b))
    longer = max(len(a), len(b))
    if longer and shorter / longer < 0.45:
        weighted *= 0.85
    if min(len(tokens_a), len(tokens_b)) == 1 and shorter < 6:
        weighted *= 0.90
    return {"score": round(weighted, 1), "algorithms": {k: round(v, 1) for k, v in scores.items()}}


def _patient_identity_summary(patient: dict[str, Any] | None) -> dict[str, Any]:
    """Ringkas identitas dan simpan No. RM menurut kategori pemeriksaan.

    Nomor RM dapat berasal dari sistem vendor yang berbeda. Karena itu, nilai
    dari VISUS tidak langsung dibandingkan sebagai konflik dengan AUDIO/LPK.
    """
    summary: dict[str, Any] = {
        "nik": set(),
        "mr": set(),
        "mr_by_exam": {},
        "mr_masks_by_exam": {},
        "dob": set(),
    }
    if not patient:
        return summary

    for file_info in patient.get("files", []):
        if file_info.get("nik_hash"):
            summary["nik"].add(file_info["nik_hash"])
        if file_info.get("mr_hash"):
            exam_code = str(file_info.get("exam_code") or "TANPA_KATEGORI").upper()
            summary["mr"].add(file_info["mr_hash"])
            summary["mr_by_exam"].setdefault(exam_code, set()).add(file_info["mr_hash"])
            if file_info.get("mr_masked"):
                summary["mr_masks_by_exam"].setdefault(exam_code, set()).add(file_info["mr_masked"])
        if file_info.get("dob_iso"):
            summary["dob"].add(file_info["dob_iso"])
    return summary


def find_patient_mr_variations(patient: dict[str, Any]) -> dict[str, Any] | None:
    """Cari variasi No. RM tanpa langsung menganggapnya sebagai salah identitas.

    Variasi lintas kategori dapat terjadi karena vendor pemeriksaan berbeda.
    Variasi dalam kategori yang sama juga ditampilkan untuk pemeriksaan petugas,
    tetapi tetap bukan konflik keras selama NIK/tanggal lahir tidak bertentangan.
    """
    values_by_exam: dict[str, set[str]] = {}
    masks_by_exam: dict[str, set[str]] = {}
    for file_info in patient.get("files", []):
        if not file_info.get("mr_hash"):
            continue
        exam_code = str(file_info.get("exam_code") or "TANPA_KATEGORI").upper()
        values_by_exam.setdefault(exam_code, set()).add(file_info["mr_hash"])
        masked = file_info.get("mr_masked")
        if masked:
            masks_by_exam.setdefault(exam_code, set()).add(masked)

    all_values = set().union(*values_by_exam.values()) if values_by_exam else set()
    if len(all_values) <= 1:
        return None

    same_exam_variations = {
        exam: sorted(masks_by_exam.get(exam, set()))
        for exam, values in values_by_exam.items()
        if len(values) > 1
    }
    category_summaries = {
        exam: sorted(masks_by_exam.get(exam, set()))
        for exam in sorted(values_by_exam)
    }
    return {
        "values_by_exam": values_by_exam,
        "same_exam_variations": same_exam_variations,
        "category_summaries": category_summaries,
        "cross_category": len(values_by_exam) > 1,
    }


def _compare_identity_values(values_a: set[str], values_b: set[str]) -> str:
    """Bandingkan satu jenis identitas tanpa menganggap data parsial sebagai kecocokan."""
    if not values_a and not values_b:
        return "missing"
    if not values_a or not values_b:
        return "incomplete"
    # Jika satu kelompok memuat beberapa nomor/tanggal berbeda, jangan simpulkan cocok
    # hanya karena salah satu nilai kebetulan beririsan.
    if len(values_a) > 1 or len(values_b) > 1:
        return "inconsistent"
    return "same" if values_a == values_b else "different"


def _compare_mr_by_exam(
    evidence_a: dict[str, Any],
    evidence_b: dict[str, Any],
) -> str:
    """Bandingkan No. RM pada kategori yang sama, bukan lintas vendor."""
    mr_a = evidence_a.get("mr_by_exam", {})
    mr_b = evidence_b.get("mr_by_exam", {})
    if not mr_a and not mr_b:
        return "missing"
    if not mr_a or not mr_b:
        return "incomplete"

    common_exams = set(mr_a) & set(mr_b)
    if not common_exams:
        # Kedua kelompok memiliki RM, tetapi hanya pada kategori yang berbeda.
        return "different_categories"

    if any(len(mr_a[exam]) > 1 or len(mr_b[exam]) > 1 for exam in common_exams):
        return "inconsistent"
    if any(mr_a[exam] & mr_b[exam] for exam in common_exams):
        return "same"
    return "different"


def compare_patient_groups(
    name_a: str, name_b: str,
    patient_a: dict[str, Any] | None = None,
    patient_b: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bandingkan kemiripan nama dan identitas pendukung secara terpisah.

    Skor nama tidak pernah dinaikkan oleh data No. RM/NIK agar persentase
    yang ditampilkan tidak disalahartikan sebagai peluang identitas sama.
    """
    name_result = advanced_name_similarity(name_a, name_b)
    evidence_a = _patient_identity_summary(patient_a)
    evidence_b = _patient_identity_summary(patient_b)

    identity_status = {
        "nik": _compare_identity_values(evidence_a["nik"], evidence_b["nik"]),
        "mr": _compare_mr_by_exam(evidence_a, evidence_b),
        "dob": _compare_identity_values(evidence_a["dob"], evidence_b["dob"]),
    }
    nik_match = identity_status["nik"] == "same"
    mr_match = identity_status["mr"] == "same"
    dob_match = identity_status["dob"] == "same"
    nik_conflict = identity_status["nik"] in {"different", "inconsistent"}
    # Perbedaan No. RM hanya menjadi petunjuk pemeriksaan, bukan konflik keras:
    # tiap kategori MCU dapat dikerjakan vendor dengan sistem RM yang berbeda.
    mr_conflict = identity_status["mr"] in {"different", "inconsistent", "different_categories"}
    dob_conflict = identity_status["dob"] in {"different", "inconsistent"}

    # Skor ini hanya mengukur kemiripan tulisan nama. Tidak dinaikkan oleh No. RM/NIK.
    name_score = float(name_result["score"])
    conflict = nik_conflict or dob_conflict

    if conflict:
        recommendation = (
            "Jangan gabungkan dulu. NIK atau tanggal lahir berbeda/tidak konsisten. "
            "Cocokkan kembali dokumen sumber."
        )
    elif identity_status["mr"] in {"different", "inconsistent", "different_categories"}:
        recommendation = (
            "Nomor RM berbeda. Ini bisa terjadi jika pemeriksaan memakai vendor berbeda. "
            "Cocokkan nama, NIK, dan tanggal lahir sebelum mengambil keputusan."
        )
    elif nik_match or mr_match:
        recommendation = (
            "Ada data identitas yang cocok. Periksa dokumen sumber untuk memastikan "
            "kedua kelompok memang milik orang yang sama."
        )
    elif name_score >= 90:
        recommendation = (
            "Nama sangat mirip, tetapi belum ada bukti identitas pendukung yang cukup. "
            "Periksa No. RM, NIK, atau tanggal lahir."
        )
    elif name_score >= 75:
        recommendation = (
            "Nama agak mirip. Periksa dokumen hanya jika ada kemungkinan kedua kelompok terkait."
        )
    else:
        recommendation = (
            "Nama berbeda cukup jauh dan belum ada data pendukung yang cukup untuk menyatakan sama."
        )

    status_reasons = {
        "same": "sama pada kedua kelompok",
        "different": "berbeda pada kategori pemeriksaan yang sama",
        "different_categories": "berbeda kategori pemeriksaan; mungkin karena vendor berbeda",
        "inconsistent": "tidak konsisten pada kategori pemeriksaan yang sama",
        "incomplete": "belum lengkap pada kedua kelompok",
        "missing": "tidak tersedia",
    }
    reasons = [f"Kemiripan tulisan nama {name_score:.1f}%"]
    reasons.append(f"NIK: {status_reasons[identity_status['nik']]}")
    reasons.append(f"No. RM: {status_reasons[identity_status['mr']]}")
    reasons.append(f"Tanggal lahir: {status_reasons[identity_status['dob']]}")

    return {
        # score dipertahankan untuk kompatibilitas pemanggil lama, tetapi kini
        # selalu sama dengan skor kemiripan nama dan bukan skor identitas gabungan.
        "score": round(name_score, 1),
        "name_score": round(name_score, 1),
        "algorithms": name_result["algorithms"],
        "identity_status": identity_status,
        "mr_values_a": {
            exam: sorted(values)
            for exam, values in evidence_a.get("mr_masks_by_exam", {}).items()
        },
        "mr_values_b": {
            exam: sorted(values)
            for exam, values in evidence_b.get("mr_masks_by_exam", {}).items()
        },
        "nik_match": nik_match,
        "mr_match": mr_match,
        "dob_match": dob_match,
        "identity_conflict": conflict,
        "reasons": reasons,
        "recommendation": recommendation,
    }


def find_internal_identity_conflicts(patient: dict[str, Any]) -> list[str]:
    """Konflik keras hanya untuk identitas utama; variasi No. RM ditangani terpisah."""
    evidence = _patient_identity_summary(patient)
    conflicts: list[str] = []
    if len(evidence["nik"]) > 1:
        conflicts.append("Lebih dari satu NIK terdeteksi dalam satu kelompok dokumen")
    if len(evidence["dob"]) > 1:
        conflicts.append("Tanggal lahir berbeda terdeteksi dalam satu kelompok dokumen")
    return conflicts


def find_similar_patient_pairs(
    patient_names: list[str],
    patients_data: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, str, float]]:
    """Temukan kandidat pasangan dengan beberapa algoritma dan bukti identitas."""
    similar_pairs: list[tuple[str, str, float]] = []
    for index, name1 in enumerate(patient_names):
        normalized1 = normalize_patient_identity_name(name1)
        if not normalized1:
            continue
        for other_index in range(index + 1, len(patient_names)):
            name2 = patient_names[other_index]
            normalized2 = normalize_patient_identity_name(name2)
            if not normalized2 or normalized1 == normalized2:
                continue
            evidence = compare_patient_groups(
                name1, name2,
                (patients_data or {}).get(name1),
                (patients_data or {}).get(name2),
            )
            if evidence["score"] >= 75 or evidence["nik_match"] or evidence["mr_match"]:
                similar_pairs.append((name1, name2, evidence["score"]))
    return sorted(similar_pairs, key=lambda item: item[2], reverse=True)


def log_identity_action(
    batch_name: str, action: str, source_names: list[str] | None = None,
    target_name: str = "", details: dict[str, Any] | None = None,
) -> None:
    """Catat keputusan petugas tanpa memasukkan NIK/nomor RM mentah ke audit log."""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO identity_audit_log
                   (batch_name, action, source_names, target_name, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch_name,
                    action,
                    json.dumps(source_names or [], ensure_ascii=False),
                    target_name,
                    json.dumps(details or {}, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
    except sqlite3.Error:
        pass


def list_identity_audit(batch_name: str, limit: int = 100) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT action, source_names, target_name, details, created_at
               FROM identity_audit_log WHERE batch_name=?
               ORDER BY id DESC LIMIT ?""",
            (batch_name, max(1, min(int(limit), 500))),
        ).fetchall()


def find_unresolved_final_name_collisions(
    patients_data: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    """Return candidate pairs whose final names match but identity choice is unresolved."""
    items = list(patients_data.items())
    collisions: list[tuple[str, str]] = []
    for index, (key_a, patient_a) in enumerate(items):
        name_a = normalize_patient_identity_name(patient_a.get("final_name", ""))
        if not name_a:
            continue
        for key_b, patient_b in items[index + 1:]:
            name_b = normalize_patient_identity_name(patient_b.get("final_name", ""))
            if not name_b or name_a != name_b:
                continue
            resolution_a = patient_a.get("identity_resolution", {})
            resolution_b = patient_b.get("identity_resolution", {})
            kept_separate = (
                resolution_a.get("type") == "kept_separate"
                and resolution_a.get("with_key") == key_b
                and resolution_a.get("name_key") == name_a
            ) or (
                resolution_b.get("type") == "kept_separate"
                and resolution_b.get("with_key") == key_a
                and resolution_b.get("name_key") == name_b
            )
            if not kept_separate:
                collisions.append((key_a, key_b))
    return collisions


def get_ocr_hashes_from_document_rows(rows: list[sqlite3.Row]) -> list[str]:
    """Cache memakai hash PDF siap proses, bukan selalu hash file upload asli."""
    hashes: set[str] = set()
    for row in rows:
        pdf_value = row["pdf_path"] if "pdf_path" in row.keys() else None
        if pdf_value:
            pdf_path = Path(pdf_value)
            if pdf_path.is_file():
                try:
                    hashes.add(sha256_file(pdf_path))
                    continue
                except OSError:
                    pass
        if row["file_hash"]:
            hashes.add(row["file_hash"])
    return sorted(hashes)


def delete_ocr_cache_for_hashes(file_hashes: list[str]) -> int:
    unique_hashes = sorted({item for item in file_hashes if item})
    if not unique_hashes:
        return 0
    deleted = 0
    with get_conn() as conn:
        for offset in range(0, len(unique_hashes), 400):
            chunk = unique_hashes[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"DELETE FROM ocr_cache WHERE file_hash IN ({placeholders})",
                chunk,
            )
            deleted += max(0, cursor.rowcount)
    return deleted


# ============================================================
# 7. PDF MERGE, ZIP, DAN LAPORAN DOWNLOAD
# ============================================================


def unique_output_path(directory: Path, filename: str) -> Path:
    return get_unique_filepath(directory, filename)



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


def create_zip_from_files(file_paths: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in file_paths:
            if file_path.exists() and file_path.is_file():
                archive.write(file_path, arcname=file_path.name)
    return buffer.getvalue()


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


        
def delete_batch_merge_outputs(batch_name: str) -> int:
    """Hapus hanya PDF hasil merge pada batch terpilih."""
    target_dir = OUTPUT_DIR / batch_name

    if not target_dir.exists():
        return 0

    # Pastikan folder benar-benar berada di dalam OUTPUT_DIR.
    if target_dir.resolve().parent != OUTPUT_DIR.resolve():
        raise ValueError("Lokasi folder output tidak aman.")

    deleted_count = 0

    for pdf_path in target_dir.glob("*.pdf"):
        if pdf_path.is_file():
            pdf_path.unlink()
            deleted_count += 1

    return deleted_count


def clear_batch_session_state(batch_name: str) -> None:
    """Bersihkan analisis, persetujuan, dan hasil merge dalam session."""
    prefixes = (
        f"validated_patients_{batch_name}",
        f"merged_files_{batch_name}",
        f"patient_approval_{batch_name}_",
        f"patient_name_{batch_name}_",
        f"patient_search_{batch_name}",
        f"merge_order_{batch_name}",
    )

    for key in list(st.session_state.keys()):
        if any(key.startswith(prefix) for prefix in prefixes):
            del st.session_state[key]


def get_merge_queue_counts(batch_name: str) -> tuple[int, int]:
    """Return (eligible documents, excluded documents) for a batch."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN merge_enabled=1 AND status='BERHASIL'
                         AND pdf_path IS NOT NULL THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN merge_enabled=0 THEN 1 ELSE 0 END) AS excluded
            FROM documents WHERE batch_name=?
            """,
            (batch_name,),
        ).fetchone()
    return int(row["queued"] or 0), int(row["excluded"] or 0)


def reset_analysis_keep_queue(batch_name: str) -> int:
    """Clear analysis and merged output but keep current documents eligible."""
    deleted_outputs = delete_batch_merge_outputs(batch_name)
    clear_batch_session_state(batch_name)
    return deleted_outputs


def start_new_merge_cycle(batch_name: str) -> tuple[int, int]:
    """Exclude all existing validation documents from the next analysis/merge.

    Original source files and PDF_SIAP copies are intentionally retained.
    New uploads will be added with merge_enabled=1 by the database default.
    """
    with get_conn() as conn:
        queued_before = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE batch_name=? AND merge_enabled=1",
            (batch_name,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE documents SET merge_enabled=0 WHERE batch_name=? AND merge_enabled=1",
            (batch_name,),
        )
    deleted_outputs = delete_batch_merge_outputs(batch_name)
    clear_batch_session_state(batch_name)
    return int(queued_before), deleted_outputs


def reset_one_exam_category(batch_name: str, exam_code: str) -> int:
    """Hapus dokumen satu kategori saja; batch dan kategori lain tetap ada."""
    # Validasi nama folder agar penghapusan tidak bisa keluar dari DATA_DIR/batch.
    batch_dir = (DATA_DIR / batch_name).resolve()
    category_dir = (DATA_DIR / batch_name / exam_code).resolve()

    if category_dir.parent != batch_dir:
        raise ValueError("Lokasi folder kategori tidak aman.")

    rows = list_batch_documents(batch_name, exam_code)
    deleted_count = len(rows)
    delete_ocr_cache_for_hashes(get_ocr_hashes_from_document_rows(rows))

    # Hapus folder kategori, termasuk ASLI dan PDF_SIAP.
    if category_dir.exists():
        shutil.rmtree(category_dir)

    # Hapus manifest database hanya untuk kategori yang dipilih.
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM documents WHERE batch_name=? AND exam_code=?",
            (batch_name, exam_code),
        )

    # Hasil merge lama menjadi tidak valid setelah isi kategori berubah.
    delete_batch_merge_outputs(batch_name)
    clear_batch_session_state(batch_name)

    # Kosongkan uploader dan hasil upload terakhir untuk kategori ini.
    direct_version_key = f"direct_pdf_uploader_version_{batch_name}"
    st.session_state[direct_version_key] = (
        st.session_state.get(direct_version_key, 0) + 1
    )
    st.session_state.pop(f"direct_upload_result_{batch_name}", None)

    return deleted_count


def reset_all_batch_documents(batch_name: str) -> None:
    """Hapus seluruh dokumen batch, tetapi pertahankan data master batch."""

    # Hitung hash PDF siap proses sebelum folder batch dihapus agar cache OCR
    # untuk hasil konversi gambar/Word juga ikut dibersihkan.
    with get_conn() as conn:
        batch_document_rows = conn.execute(
            "SELECT file_hash, pdf_path FROM documents WHERE batch_name=?", (batch_name,)
        ).fetchall()
    batch_ocr_hashes = get_ocr_hashes_from_document_rows(batch_document_rows)

    # Hapus folder hanya jika lokasinya tepat di dalam folder utama.
    folders = [
        (DATA_DIR, DATA_DIR / batch_name),
        (OUTPUT_DIR, OUTPUT_DIR / batch_name),
        (REPORT_DIR, REPORT_DIR / batch_name),
    ]

    for base_dir, target_dir in folders:
        if target_dir.exists():
            if target_dir.resolve().parent != base_dir.resolve():
                raise ValueError(
                    f"Lokasi folder tidak aman: {target_dir}"
                )

            shutil.rmtree(target_dir)

    # Hapus cache OCR terkait dokumen batch, lalu catatan manifest.
    delete_ocr_cache_for_hashes(batch_ocr_hashes)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM documents WHERE batch_name=?",
            (batch_name,),
        )
        conn.execute("DELETE FROM identity_audit_log WHERE batch_name=?", (batch_name,))

    # Bersihkan analisis dan hasil merge.
    clear_batch_session_state(batch_name)

    # Ganti key uploader supaya pilihan file lama ikut kosong.
    st.session_state["conversion_uploader_version"] = (
        st.session_state.get("conversion_uploader_version", 0) + 1
    )

    direct_version_key = f"direct_pdf_uploader_version_{batch_name}"
    st.session_state[direct_version_key] = (
        st.session_state.get(direct_version_key, 0) + 1
    )

    # Bersihkan ringkasan konversi jika berasal dari batch ini.
    latest_result = st.session_state.get("latest_conversion_result")

    if latest_result and latest_result.get("batch") == batch_name:
        st.session_state.pop("latest_conversion_result", None)


def _safe_batch_child(base_dir: Path, batch_name: str) -> Path:
    """Resolve folder batch dan pastikan tetap berada langsung di bawah base_dir."""
    base = base_dir.resolve()
    target = (base_dir / batch_name).resolve()
    if target.parent != base:
        raise ValueError(f"Lokasi batch tidak aman: {target}")
    return target


def archive_batch(batch_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE batches SET archived_at=? WHERE batch_name=? AND deleted_at IS NULL",
            (datetime.now().isoformat(timespec="seconds"), batch_name),
        )


def unarchive_batch(batch_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE batches SET archived_at=NULL WHERE batch_name=? AND deleted_at IS NULL",
            (batch_name,),
        )


def move_batch_to_trash(batch_name: str) -> None:
    """Soft delete: hanya tandai batch; file dan catatan belum dihapus."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE batches SET deleted_at=? WHERE batch_name=? AND deleted_at IS NULL",
            (datetime.now().isoformat(timespec="seconds"), batch_name),
        )
    clear_batch_session_state(batch_name)


def restore_batch_from_trash(batch_name: str) -> None:
    """Pulihkan batch dari Tong Sampah ke daftar batch aktif."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE batches SET deleted_at=NULL, archived_at=NULL WHERE batch_name=?",
            (batch_name,),
        )


def permanently_delete_batch(batch_name: str) -> dict[str, int]:
    """Hapus permanen batch yang sudah ada di Tong Sampah setelah konfirmasi petugas."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM batches WHERE batch_name=?", (batch_name,)
        ).fetchone()
    if row is None or row["deleted_at"] is None:
        raise ValueError("Batch harus berada di Tong Sampah sebelum dihapus permanen.")

    with get_conn() as conn:
        permanent_delete_rows = conn.execute(
            "SELECT file_hash, pdf_path FROM documents WHERE batch_name=?", (batch_name,)
        ).fetchall()
    permanent_ocr_hashes = get_ocr_hashes_from_document_rows(permanent_delete_rows)

    deleted_files = 0
    for base_dir in (DATA_DIR, OUTPUT_DIR, REPORT_DIR, CONVERSION_DIR):
        target_dir = _safe_batch_child(base_dir, batch_name)
        if target_dir.exists():
            deleted_files += sum(1 for item in target_dir.rglob("*") if item.is_file())
            shutil.rmtree(target_dir)

    with get_conn() as conn:
        validation_count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE batch_name=?", (batch_name,)
        ).fetchone()[0]
        conversion_count = conn.execute(
            "SELECT COUNT(*) FROM conversion_documents WHERE batch_name=?", (batch_name,)
        ).fetchone()[0]
        conn.execute("DELETE FROM documents WHERE batch_name=?", (batch_name,))
        conn.execute("DELETE FROM conversion_documents WHERE batch_name=?", (batch_name,))
        conn.execute("DELETE FROM identity_audit_log WHERE batch_name=?", (batch_name,))
        conn.execute("DELETE FROM batches WHERE batch_name=?", (batch_name,))

    delete_ocr_cache_for_hashes(permanent_ocr_hashes)
    clear_batch_session_state(batch_name)
    latest_result = st.session_state.get("latest_conversion_result")
    if latest_result and latest_result.get("batch") == batch_name:
        st.session_state.pop("latest_conversion_result", None)

    return {
        "files": deleted_files,
        "validation_records": validation_count,
        "conversion_records": conversion_count,
    }


def make_processing_report(batch_name: str, patients: dict[str, Any]) -> bytes:
    lines = [
        f"LAPORAN PEMROSESAN MCU - {batch_name}",
        f"Waktu dibuat: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Jumlah kandidat pasien: {len(patients)}",
        "",
    ]
    for name, patient in patients.items():
        status = "DISETUJUI" if patient.get("is_validated") else "BELUM DISETUJUI"
        lines.append(f"{patient.get('final_name', name)} | {status} | {len(patient.get('files', []))} dokumen")
        for item in patient.get("files", []):
            lines.append(f"  - {item['exam_code']}: {item.get('original_filename', item['filename'])}")
    return "\n".join(lines).encode("utf-8")


# ============================================================
# 8. LOGIN DAN INISIALISASI
# ============================================================


def check_login() -> None:
    try:
        expected_password = st.secrets["app"]["password"]
    except Exception:
        st.error(
            "Konfigurasi password belum dibuat. Pastikan file "
            ".streamlit/secrets.toml tersedia dan memiliki [app] password."
        )
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.title("MCU PDF Merger")
    st.caption("Akses internal jaringan rumah sakit")
    with st.form("login_form"):
        password = st.text_input("Password aplikasi", type="password")
        submitted = st.form_submit_button("Masuk", use_container_width=True)
    if submitted:
        if password and password == expected_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Password tidak sesuai.")
    st.stop()


init_db()
check_login()

# Pisahkan riwayat DOC/DOCX/gambar converter lama dari Validasi & Merge.
if not st.session_state.get("legacy_converter_records_migrated", False):
    migrated_count = migrate_legacy_converter_records()
    st.session_state["legacy_converter_records_migrated"] = True
    if migrated_count:
        st.toast(
            f"{migrated_count} riwayat dokumen non-PDF dipisahkan ke menu Converter.",
            icon="✅",
        )

# Migrasikan PDF hasil versi sebelumnya yang masih memakai suffix hash.
# Proses ini hanya mengganti nama file; isi PDF tidak diubah dan file tidak ditimpa.
if not st.session_state.get("legacy_pdf_names_migrated", False):
    renamed_legacy_pdfs = migrate_legacy_pdf_filenames()
    st.session_state["legacy_pdf_names_migrated"] = True
    if renamed_legacy_pdfs:
        st.toast(
            f"{renamed_legacy_pdfs} nama PDF lama berhasil dirapikan.",
            icon="✅",
        )


# ============================================================
# 8A. UTILITAS KHUSUS NORMALISASI NAMA (TERPISAH DARI MODUL LAMA)
# ============================================================


def suggest_normalized_pdf_name(original_filename: str) -> tuple[str, str]:
    """Buat saran nama dari nama file saja; isi PDF tidak dibaca atau diubah.

    Aturan konservatif:
    - mengubah underscore/tanda pemisah menjadi spasi;
    - menghapus token angka di awal/akhir nama (misalnya ID/tahun);
    - menghapus angka yang menempel di depan kata pertama atau belakang kata terakhir;
    - menghapus kata kategori/pemeriksaan yang berdiri sendiri;
    - mempertahankan kata nama di bagian tengah (termasuk DWI).
    """
    stem = Path(original_filename).stem.strip()
    working = re.sub(r"[_\-.()]+", " ", stem)
    working = re.sub(r"\s+", " ", working).strip()
    tokens = working.split()
    notes: list[str] = []

    # Hapus token angka utuh di awal dan akhir, misalnya 6646 atau 2026 10.
    while tokens and re.fullmatch(r"\d+", tokens[0]):
        tokens.pop(0)
        notes.append("angka di depan dihapus")
    while tokens and re.fullmatch(r"\d+", tokens[-1]):
        tokens.pop()
        notes.append("angka di belakang dihapus")

    # Bersihkan angka yang menempel pada kata paling depan/paling belakang.
    if tokens:
        cleaned_first = re.sub(r"^\d+(?=[A-Za-zÀ-ÖØ-öø-ÿ])", "", tokens[0])
        if cleaned_first != tokens[0]:
            tokens[0] = cleaned_first
            notes.append("angka awalan yang menempel dihapus")

    if tokens:
        cleaned_last = re.sub(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\d+$", "", tokens[-1])
        if cleaned_last != tokens[-1]:
            tokens[-1] = cleaned_last
            notes.append("angka akhiran yang menempel dihapus")

    # Buang token kosong/angka yang mungkin tersisa setelah pembersihan.
    tokens = [token for token in tokens if token and not re.fullmatch(r"\d+", token)]

    # Hapus kode pemeriksaan hanya jika berdiri sebagai kata tersendiri.
    removable_words = {word.upper() for word in MEDICAL_KEYWORDS}
    removable_words.update({"COPY", "SALINAN"})
    filtered_tokens = [token for token in tokens if token.upper() not in removable_words]
    if len(filtered_tokens) != len(tokens):
        notes.append("kata/kode pemeriksaan yang berdiri sendiri dihapus")
    tokens = filtered_tokens

    suggested = " ".join(tokens).upper().strip()
    suggested = re.sub(r"\s+", " ", suggested)
    if not suggested:
        suggested = "NAMA_TIDAK_TERDETEKSI"
        notes.append("nama perlu diperiksa manual")

    return suggested, ("; ".join(dict.fromkeys(notes)) if notes else "format dirapikan")


def safe_normalization_stem(value: str) -> str:
    """Sanitasi nama tujuan agar tidak dapat membuat path/folder lain."""
    value = re.sub(r"[\\/:*?\"<>|]+", " ", str(value))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    value = re.sub(r"[^\w -]+", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value or "NAMA_TIDAK_TERDETEKSI"


def find_normalization_similar_names(names: list[str]) -> list[tuple[str, str, float]]:
    """Tampilkan pasangan nama mirip sebagai peringatan, bukan penggabungan."""
    unique_names = list(dict.fromkeys(name.strip().upper() for name in names if name.strip()))
    pairs: list[tuple[str, str, float]] = []
    for index, name_a in enumerate(unique_names):
        for name_b in unique_names[index + 1:]:
            score = fuzz.token_sort_ratio(name_a, name_b)
            if 78 <= score < 100:
                pairs.append((name_a, name_b, round(score, 1)))
    return sorted(pairs, key=lambda item: item[2], reverse=True)


# ============================================================
# 9. SIDEBAR DAN DATA BATCH
# ============================================================

with st.sidebar:
    st.title("MCU PDF Merger")
    st.caption("Pengelolaan dokumen Medical Check Up")
    page = st.radio(
        "Navigasi Utama",
        [
            "Dashboard",
            "Buat Batch MCU",
            "Kelola Batch MCU",
            "Paket Pemeriksaan",
            "Convert Word, JPG to PDF",
            "Validasi & Merge MCU",
            "Normalisasi Nama",
            "Tong Sampah",
            "Panduan",
        ],
    )
    st.divider()
    if st.button("Keluar", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()

st.title(page)


# ============================================================
# 10. DASHBOARD
# ============================================================

if page == "Dashboard":
    with get_conn() as conn:
        total_batches = conn.execute("SELECT COUNT(*) FROM batches WHERE deleted_at IS NULL").fetchone()[0]
        active_batches = conn.execute(
            "SELECT COUNT(*) FROM batches "
            "WHERE deleted_at IS NULL AND archived_at IS NULL AND status != 'Selesai'"
        ).fetchone()[0]
        active_exams = conn.execute(
            "SELECT COUNT(*) FROM exam_types WHERE active=1"
        ).fetchone()[0]
        recent_batches = conn.execute(
            "SELECT * FROM batches WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 10"
        ).fetchall()
        total_documents = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE batch_name IN (SELECT batch_name FROM batches WHERE deleted_at IS NULL)"
        ).fetchone()[0]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total batch MCU", total_batches)
    col2.metric("Batch berjalan", active_batches)
    col3.metric("Jenis pemeriksaan aktif", active_exams)
    col4.metric("Dokumen tercatat", total_documents)

    st.subheader("Batch terbaru")
    if recent_batches:
        st.dataframe(
            [
                {
                    "Perusahaan": row["company"],
                    "Batch": row["batch_name"],
                    "Pemeriksaan": row["exam_codes"].replace(",", ", "),
                    "Status": (
                        "Diarsipkan" if row["archived_at"] else row["status"]
                    ),
                    "Dibuat": row["created_at"],
                }
                for row in recent_batches
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Belum ada batch. Buat batch terlebih dahulu.")

    st.info(
        "Gunakan data dummy selama pengembangan. Aplikasi ini masih menggunakan "
        "satu password bersama dan belum menjadi sistem produksi untuk data medis."
    )


# ============================================================
# 11. BUAT BATCH MCU
# ============================================================

elif page == "Buat Batch MCU":
    st.write("Buat ruang kerja terpisah untuk setiap perusahaan/periode MCU.")
    with get_conn() as conn:
        exam_rows = conn.execute(
            "SELECT code, label FROM exam_types WHERE active=1 ORDER BY code"
        ).fetchall()

    exam_labels = {row["code"]: row["label"] for row in exam_rows}
    with st.form("create_batch_form"):
        company = st.text_input("Nama perusahaan", placeholder="Contoh: JICT")
        period = st.text_input("Periode / tahun", placeholder="Contoh: 2026")
        selected_exams = st.multiselect(
            "Pilih pemeriksaan dalam paket ini",
            options=list(exam_labels.keys()),
            default=list(exam_labels.keys()),
            format_func=lambda code: f"{code} — {exam_labels.get(code, code)}",
        )
        submitted = st.form_submit_button("Buat batch", use_container_width=True)

    if submitted:
        if not company.strip() or not period.strip():
            st.error("Nama perusahaan dan periode wajib diisi.")
        elif not selected_exams:
            st.error("Pilih minimal satu jenis pemeriksaan.")
        else:
            batch_name = f"{safe_folder_name(company)}_{safe_folder_name(period)}"
            try:
                with get_conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO batches(company, batch_name, exam_codes, status, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            company.strip(),
                            batch_name,
                            ",".join(selected_exams),
                            "Persiapan",
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                for code in selected_exams:
                    (DATA_DIR / batch_name / code).mkdir(parents=True, exist_ok=True)
                (OUTPUT_DIR / batch_name).mkdir(parents=True, exist_ok=True)
                (REPORT_DIR / batch_name).mkdir(parents=True, exist_ok=True)
                st.success(f"Batch {batch_name} berhasil dibuat.")
                st.code(str(DATA_DIR / batch_name), language="text")
            except sqlite3.IntegrityError:
                st.error("Batch tersebut sudah ada. Gunakan periode atau nama yang berbeda.")


# ============================================================
# 12. PAKET PEMERIKSAAN
# ============================================================

elif page == "Paket Pemeriksaan":
    st.write("Kelola daftar jenis pemeriksaan untuk batch MCU.")
    with get_conn() as conn:
        exam_rows = conn.execute(
            "SELECT code, label, active FROM exam_types ORDER BY code"
        ).fetchall()

    st.dataframe(
        [
            {
                "Kode": row["code"],
                "Nama pemeriksaan": row["label"],
                "Aktif": "Ya" if row["active"] else "Tidak",
            }
            for row in exam_rows
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Tambah jenis pemeriksaan")
    with st.form("add_exam_form"):
        code = st.text_input("Kode unik", placeholder="Contoh: TREADMILL").strip().upper()
        label = st.text_input("Nama pemeriksaan", placeholder="Contoh: Treadmill").strip()
        add_exam = st.form_submit_button("Tambah pemeriksaan")

    if add_exam:
        if not code or not label:
            st.error("Kode dan nama pemeriksaan wajib diisi.")
        elif not code.replace("_", "").isalnum():
            st.error("Kode hanya boleh berisi huruf, angka, dan underscore.")
        else:
            try:
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO exam_types(code, label, active) VALUES (?, ?, 1)",
                        (code, label),
                    )
                st.success("Jenis pemeriksaan berhasil ditambahkan.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Kode pemeriksaan sudah digunakan.")


# ============================================================
# 13. MENU KONVERSI WORD, GAMBAR, DAN PDF
# ============================================================

elif page == "Convert Word, JPG to PDF":
    st.subheader("Konversi dan penyimpanan dokumen pemeriksaan")
    st.write(
        "Hasil konversi dan riwayatnya tersimpan terpisah di HASIL_KONVERSI. "
        "Dokumen di menu ini tidak otomatis masuk ke Validasi & Merge MCU. "
        "Unduh PDF di sini, lalu unggah kembali secara manual ke menu Validasi & Merge bila akan digabung."
    )

    with get_conn() as conn:
        batch_rows = conn.execute(
            "SELECT batch_name, company, exam_codes FROM batches "
            "WHERE deleted_at IS NULL AND archived_at IS NULL ORDER BY id DESC"
        ).fetchall()
        all_exam_rows = conn.execute(
            "SELECT code, label FROM exam_types WHERE active=1 ORDER BY code"
        ).fetchall()

    if not batch_rows:
        st.warning("Belum ada batch MCU. Buat batch terlebih dahulu.")
    else:
        batch_map = {row["batch_name"]: row for row in batch_rows}
        selected_batch = st.selectbox(
            "Pilih perusahaan / batch MCU",
            options=list(batch_map.keys()),
            format_func=lambda name: f"{batch_map[name]['company']} — {name}",
            key="conversion_batch_select",
        )
        selected_batch_row = batch_map[selected_batch]
        included_codes = [
            item.strip()
            for item in selected_batch_row["exam_codes"].split(",")
            if item.strip()
        ]
        labels = {row["code"]: row["label"] for row in all_exam_rows}
        available_codes = [code for code in included_codes if code in labels]

        if not available_codes:
            st.warning("Batch ini belum memiliki jenis pemeriksaan aktif.")
        else:
            selected_exam = st.selectbox(
                "Pilih kategori pemeriksaan",
                options=available_codes,
                format_func=lambda code: f"{code} — {labels.get(code, code)}",
                key="conversion_exam_select",
            )
            st.caption(
                "Format: PDF, JPG/JPEG, PNG, TIFF, DOC, DOCX. Maksimal "
                "50 file per upload dan 50 MB per file."
            )
            # Versi key berubah saat tombol reset ditekan agar file_uploader benar-benar kosong.
            if "conversion_uploader_version" not in st.session_state:
                st.session_state["conversion_uploader_version"] = 0

            uploader_key = (
                f"conversion_file_uploader_{st.session_state['conversion_uploader_version']}"
            )

            uploads = st.file_uploader(
                "Pilih dokumen pemeriksaan",
                type=["pdf", "jpg", "jpeg", "png", "tif", "tiff", "doc", "docx"],
                accept_multiple_files=True,
                key=uploader_key,
            )

            if uploads:
                preview_rows = []
                for uploaded in uploads:
                    validation_error = validate_upload(uploaded)
                    preview_rows.append(
                        {
                            "Nama file": uploaded.name,
                            "Format": Path(uploaded.name).suffix.upper(),
                            "Ukuran (MB)": round(uploaded.size / (1024 * 1024), 2),
                            "Status": validation_error or "Siap diproses",
                        }
                    )
                st.write(f"Jumlah file dipilih: {len(uploads)}")
                st.dataframe(preview_rows, use_container_width=True, hide_index=True)

                if len(uploads) > MAX_FILE_COUNT:
                    st.error(f"Maksimal {MAX_FILE_COUNT} file sekali upload.")
                elif st.button(
                    "Konversi dan simpan dokumen",
                    type="primary",
                    use_container_width=True,
                    key="run_conversion_button",
                ):
                    with st.spinner("Menyimpan dan memproses dokumen..."):
                        stats = store_conversion_documents(uploads, selected_batch, selected_exam)
                    st.session_state["latest_conversion_result"] = {
                        "batch": selected_batch,
                        "exam": selected_exam,
                        "pdf_paths": [str(path) for path in stats["pdf_paths"]],
                        "stats": {
                            key: value for key, value in stats.items() if key != "pdf_paths" and key != "errors"
                        },
                        "errors": stats["errors"],
                    }
                    st.rerun()

    conversion_result = st.session_state.get("latest_conversion_result")
    if conversion_result:
        st.divider()
        st.subheader("Hasil proses terakhir")
        st.caption(
            f"Batch: {conversion_result['batch']} | Pemeriksaan: {conversion_result['exam']}"
        )
        stats = conversion_result["stats"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Sumber baru disimpan", stats["saved_count"])
        c2.metric("Upload identik terdeteksi", stats["duplicate_count"])
        c3.metric("PDF siap diunduh", len(conversion_result["pdf_paths"]))
        for message in conversion_result["errors"]:
            st.error(message)

        pdf_paths = [Path(value) for value in conversion_result["pdf_paths"] if Path(value).exists()]
        if pdf_paths:
            if len(pdf_paths) > 1:
                st.download_button(
                    "Download semua PDF (.ZIP)",
                    data=create_zip_from_files(pdf_paths),
                    file_name=f"PDF_{conversion_result['batch']}_{conversion_result['exam']}.zip",
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                )
            st.write("PDF individual:")
            for index, pdf_path in enumerate(pdf_paths):
                st.download_button(
                    label=f"Save As PDF — {pdf_path.name}",
                    data=pdf_path.read_bytes(),
                    file_name=pdf_path.name,
                    mime="application/pdf",
                    key=f"conversion_pdf_download_{index}_{pdf_path.name}",
                )

        st.divider()
        if st.button(
            "🔄 Reset / Upload Dokumen Lain",
            use_container_width=True,
            key="reset_conversion_button",
        ):
            # Hapus ringkasan hasil di layar, tetapi jangan menghapus file/database.
            st.session_state.pop("latest_conversion_result", None)
            st.session_state["conversion_uploader_version"] += 1
            st.rerun()

    with st.expander("Riwayat konversi pada batch dan pemeriksaan terpilih"):
        if "selected_batch" in locals() and "selected_exam" in locals():
            rows = list_conversion_documents(selected_batch, selected_exam)
            if rows:
                st.dataframe(
                    [
                        {
                            "Nama sumber": row["original_name"],
                            "Status": row["status"],
                            "Ukuran (MB)": round(row["size_bytes"] / (1024 * 1024), 2),
                            "Waktu upload": row["uploaded_at"],
                            "Keterangan": row["error_message"] or "-",
                        }
                        for row in rows
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("Belum ada dokumen tercatat untuk pemeriksaan ini.")


# ============================================================
# 14. VALIDASI MANUAL DAN MERGE PDF
# ============================================================

elif page == "Validasi & Merge MCU":
    st.subheader("Analisis, validasi identitas, dan penggabungan PDF pasien")
    st.warning(
        "OCR hybrid dan matching lanjutan membantu menemukan kandidat dokumen pasien. "
        "Skor kemiripan atau kecocokan identitas bukan persetujuan merge; petugas tetap "
        "wajib memeriksa dokumen sumber dan menyetujui identitas secara manual."
    )

    with get_conn() as conn:
        batch_rows = conn.execute(
            "SELECT batch_name, company, exam_codes FROM batches "
            "WHERE deleted_at IS NULL AND archived_at IS NULL ORDER BY id DESC"
        ).fetchall()

    if not batch_rows:
        st.warning("Belum ada batch MCU. Buat batch terlebih dahulu.")
    else:
        batch_map = {row["batch_name"]: row for row in batch_rows}
        selected_batch = st.selectbox(
            "Pilih batch MCU yang diproses",
            options=list(batch_map.keys()),
            format_func=lambda name: f"{batch_map[name]['company']} — {name}",
            key="validation_batch_select",
        )
        selected_batch_row = batch_map[selected_batch]
        exam_order_default = [
            item.strip()
            for item in selected_batch_row["exam_codes"].split(",")
            if item.strip()
        ]
        batch_state_key = f"validated_patients_{selected_batch}"
        analysis_rules_version = "20261107_flexible_mrn_v1"
        analysis_version_key = f"identity_analysis_version_{selected_batch}"
        if st.session_state.get(analysis_version_key) != analysis_rules_version:
            # Hasil grouping lama bisa berisi kandidat keliru (misalnya PUASA 13 JAM).
            # Buang hanya hasil analisis dalam session; file sumber dan database tetap utuh.
            st.session_state.pop(batch_state_key, None)
            st.session_state.pop(f"merged_files_{selected_batch}", None)
            stale_prefixes = (
                f"patient_approval_{selected_batch}_",
                f"patient_name_{selected_batch}_",
                f"patient_name_source_review_{selected_batch}_",
                f"patient_mr_variation_review_{selected_batch}_",
                f"same_name_target_{selected_batch}_",
                f"verify_same_identity_{selected_batch}_",
                f"split_document_{selected_batch}_",
                f"split_name_{selected_batch}_",
                f"split_submit_{selected_batch}_",
                f"merge_same_name_{selected_batch}_",
                f"keep_separate_{selected_batch}_",
            )
            stale_exact_keys = {
                f"patient_search_{selected_batch}",
                f"identity_merge_a_{selected_batch}",
                f"identity_merge_b_{selected_batch}",
                f"identity_merge_final_{selected_batch}",
                f"identity_merge_confirm_{selected_batch}",
                f"identity_merge_submit_{selected_batch}",
            }
            for old_key in list(st.session_state.keys()):
                if old_key in stale_exact_keys or old_key.startswith(stale_prefixes):
                    del st.session_state[old_key]
            st.session_state[analysis_version_key] = analysis_rules_version
            st.info(
                "Aturan pembacaan nama diperbarui. Hasil pengelompokan sebelumnya "
                "dibersihkan dari sesi; klik 'Analisis ulang dokumen dan perbarui identitas pasien'."
            )

        # ----------------------------------------------------
        # KONTROL RESET/ANTREAN DI HALAMAN VALIDASI & MERGE
        # ----------------------------------------------------
        queued_count, excluded_count = get_merge_queue_counts(selected_batch)
        with st.container(border=True):
            st.markdown("#### Kontrol sesi pemrosesan")
            st.caption(
                "Reset analisis hanya menghapus hasil analisis dan PDF gabungan. "
                "Untuk mulai dengan dokumen baru saja, keluarkan dokumen lama dari antrean; "
                "file sumber tetap disimpan."
            )
            q1, q2 = st.columns(2)
            q1.metric("Dokumen aktif untuk merge", queued_count)
            q2.metric("Dokumen lama di luar antrean", excluded_count)

            reset_col, fresh_col = st.columns(2)
            with reset_col:
                reset_analysis_key = f"validation_reset_analysis_{selected_batch}"
                if st.button(
                    "Reset Analisis & Hasil Merge",
                    use_container_width=True,
                    key=f"request_{reset_analysis_key}",
                ):
                    st.session_state[reset_analysis_key] = True

                if st.session_state.get(reset_analysis_key, False):
                    st.warning(
                        "Hasil PDF gabungan dan analisis/persetujuan akan dihapus. "
                        "Dokumen yang ada tetap aktif di antrean, sehingga analisis berikutnya "
                        "akan membaca dokumen yang sama lagi."
                    )
                    c_yes, c_no = st.columns(2)
                    with c_yes:
                        if st.button(
                            "Konfirmasi reset analisis",
                            type="primary",
                            use_container_width=True,
                            key=f"confirm_{reset_analysis_key}",
                        ):
                            deleted = reset_analysis_keep_queue(selected_batch)
                            st.session_state.pop(reset_analysis_key, None)
                            st.success(
                                f"Analisis direset. {deleted} PDF gabungan dihapus; "
                                "dokumen masih berada dalam antrean."
                            )
                            st.rerun()
                    with c_no:
                        if st.button(
                            "Batal",
                            use_container_width=True,
                            key=f"cancel_{reset_analysis_key}",
                        ):
                            st.session_state.pop(reset_analysis_key, None)
                            st.rerun()

            with fresh_col:
                new_cycle_key = f"validation_new_cycle_{selected_batch}"
                if st.button(
                    "Mulai Data Baru (Keluarkan Dokumen Lama)",
                    use_container_width=True,
                    key=f"request_{new_cycle_key}",
                    disabled=(queued_count == 0),
                ):
                    st.session_state[new_cycle_key] = True

                if st.session_state.get(new_cycle_key, False):
                    st.warning(
                        f"Sebanyak {queued_count} dokumen akan dikeluarkan dari antrean analisis/merge. "
                        "File sumber, PDF siap proses, dan catatan database TIDAK dihapus. "
                        "Upload baru setelah tindakan ini akan masuk ke antrean baru."
                    )
                    understand = st.checkbox(
                        "Saya paham dokumen lama tetap tersimpan tetapi tidak ikut merge berikutnya.",
                        key=f"understand_{new_cycle_key}",
                    )
                    c_yes, c_no = st.columns(2)
                    with c_yes:
                        if st.button(
                            "Konfirmasi mulai data baru",
                            type="primary",
                            use_container_width=True,
                            disabled=not understand,
                            key=f"confirm_{new_cycle_key}",
                        ):
                            excluded, deleted = start_new_merge_cycle(selected_batch)
                            st.session_state.pop(new_cycle_key, None)
                            st.success(
                                f"Sesi baru dimulai. {excluded} dokumen lama dikeluarkan dari antrean; "
                                f"{deleted} PDF hasil merge dihapus. Dokumen sumber tetap aman."
                            )
                            st.rerun()
                    with c_no:
                        if st.button(
                            "Batal",
                            use_container_width=True,
                            key=f"cancel_{new_cycle_key}",
                        ):
                            st.session_state.pop(new_cycle_key, None)
                            st.rerun()

        with st.expander("Upload PDF langsung", expanded=False):
            valid_codes = exam_order_default

            if valid_codes:
                with get_conn() as conn:
                    upload_exam_rows = conn.execute(
                        "SELECT code, label FROM exam_types"
                    ).fetchall()

                exam_labels_for_upload = {
                    row["code"]: row["label"]
                    for row in upload_exam_rows
                }

                # PILIH KATEGORI PEMERIKSAAN
                selected_pdf_exam = st.selectbox(
                    "Pilih kategori pemeriksaan yang akan di-upload",
                    options=valid_codes,
                    format_func=lambda code: (
                        f"{code} — "
                        f"{exam_labels_for_upload.get(code, code)}"
                    ),
                    key=f"direct_pdf_exam_{selected_batch}",
                )

                # ==================================================
                # DOKUMEN YANG SUDAH TERSIMPAN PADA KATEGORI TERPILIH
                # ==================================================

                st.markdown(
                    f"#### Dokumen yang sudah tersimpan — {selected_pdf_exam}"
                )

                st.caption(
                    f"Kategori aktif: {selected_pdf_exam} — "
                    f"{exam_labels_for_upload.get(selected_pdf_exam, selected_pdf_exam)}. "
                    "Daftar ini hanya menampilkan dokumen pada kategori tersebut."
                )

                selected_category_rows = list_batch_documents(
                    selected_batch,
                    selected_pdf_exam,
                )

                patient_groups = {}

                for doc_row in selected_category_rows:
                    patient_candidate = extract_name_from_filename(
                        doc_row["original_name"],
                        selected_pdf_exam,
                    ) or "NAMA BELUM TERDETEKSI"

                    group = patient_groups.setdefault(
                        patient_candidate,
                        {
                            "files": [],
                            "pages": 0,
                            "statuses": [],
                        },
                    )

                    group["files"].append(doc_row["original_name"])
                    group["statuses"].append(doc_row["status"])

                    if (
                        doc_row["pdf_path"]
                        and Path(doc_row["pdf_path"]).is_file()
                    ):
                        try:
                            with fitz.open(doc_row["pdf_path"]) as check_doc:
                                group["pages"] += check_doc.page_count
                        except Exception:
                            pass

                if patient_groups:
                    selected_rows = []

                    for patient_name, details in sorted(patient_groups.items()):
                        selected_rows.append({
                            "Kategori": selected_pdf_exam,
                            "Nama kandidat pasien": patient_name,
                            "Jumlah file": len(details["files"]),
                            "Total halaman": details["pages"],
                            "Nama file": "\n".join(details["files"]),
                            "Status": (
                                "BERHASIL"
                                if all(
                                    status == "BERHASIL"
                                    for status in details["statuses"]
                                )
                                else "PERLU DIPERIKSA"
                            ),
                        })

                    st.dataframe(
                        selected_rows,
                        use_container_width=True,
                        hide_index=True,
                    )

                    st.caption(
                        "Nama kandidat diambil dari nama file, bukan verifikasi "
                        "identitas. Tetap periksa nama pasien pada tahap "
                        "Validasi & Merge MCU."
                    )

                else:
                    st.info(
                        f"Belum ada dokumen tersimpan untuk kategori "
                        f"{selected_pdf_exam}."
                    )

                # ==================================================
                # RINGKASAN SELURUH KATEGORI + RESET PER KATEGORI
                # ==================================================

                with st.expander(
                    "Lihat ringkasan seluruh kategori pemeriksaan",
                    expanded=True,
                ):
                    overview_rows = []

                    for exam_code in valid_codes:
                        rows_for_exam = list_batch_documents(
                            selected_batch,
                            exam_code,
                        )

                        names_for_exam = sorted({
                            extract_name_from_filename(
                                row["original_name"],
                                exam_code,
                            ) or "NAMA BELUM TERDETEKSI"
                            for row in rows_for_exam
                        })

                        overview_rows.append({
                            "Kategori": exam_code,
                            "Nama pemeriksaan": exam_labels_for_upload.get(
                                exam_code,
                                exam_code,
                            ),
                            "Jumlah file": len(rows_for_exam),
                            "Kandidat pasien": (
                                ", ".join(names_for_exam)
                                if names_for_exam
                                else "Belum ada"
                            ),
                        })

                    st.dataframe(
                        overview_rows,
                        use_container_width=True,
                        hide_index=True,
                    )

                    # --------------------------------------------------
                    # RESET SATU KATEGORI DARI RINGKASAN
                    # --------------------------------------------------
                    st.divider()
                    st.markdown("#### Reset kategori pemeriksaan")
                    st.caption(
                        "Reset hanya menghapus dokumen Validasi & Merge pada kategori yang dipilih. "
                        "Batch, kategori lain, dan seluruh data Converter tetap dipertahankan. "
                        "File sumber dan PDF siap proses kategori terpilih ikut dihapus."
                    )

                    reset_exam_code = st.selectbox(
                        "Pilih kategori yang ingin di-reset",
                        options=valid_codes,
                        format_func=lambda code: (
                            f"{code} — "
                            f"{exam_labels_for_upload.get(code, code)}"
                        ),
                        key=f"direct_reset_exam_{selected_batch}",
                    )
                    reset_category_rows = list_batch_documents(
                        selected_batch,
                        reset_exam_code,
                    )
                    reset_category_count = len(reset_category_rows)
                    st.write(
                        f"Dokumen kategori **{reset_exam_code}**: "
                        f"**{reset_category_count} file**"
                    )

                    reset_request_key = (
                        f"direct_reset_request_{selected_batch}_{reset_exam_code}"
                    )
                    reset_nonce_key = (
                        f"direct_reset_nonce_{selected_batch}_{reset_exam_code}"
                    )
                    if reset_nonce_key not in st.session_state:
                        st.session_state[reset_nonce_key] = 0
                    reset_confirm_key = (
                        f"direct_reset_confirm_{selected_batch}_{reset_exam_code}_"
                        f"{st.session_state[reset_nonce_key]}"
                    )
                    reset_flash_key = (
                        f"direct_reset_flash_{selected_batch}_{reset_exam_code}"
                    )

                    if st.session_state.get(reset_flash_key):
                        st.success(st.session_state.pop(reset_flash_key))

                    if st.button(
                        f"Reset kategori {reset_exam_code}",
                        use_container_width=True,
                        disabled=(reset_category_count == 0),
                        key=f"direct_reset_button_{selected_batch}_{reset_exam_code}",
                    ):
                        st.session_state[reset_request_key] = True

                    if st.session_state.get(reset_request_key, False):
                        st.error(
                            f"Konfirmasi penghapusan kategori {reset_exam_code}: "
                            f"{reset_category_count} catatan dokumen, file sumber ASLI, "
                            "dan PDF_SIAP kategori ini akan dihapus. Hasil merge lama "
                            "untuk batch ini juga akan dihapus. Kategori lain dan Converter "
                            "tidak akan dihapus. Tindakan ini tidak dapat dibatalkan."
                        )
                        confirm_reset_category = st.checkbox(
                            f"Saya yakin ingin menghapus seluruh dokumen kategori {reset_exam_code}.",
                            key=reset_confirm_key,
                        )
                        confirm_col, cancel_col = st.columns(2)
                        with confirm_col:
                            if st.button(
                                "Ya, reset kategori ini",
                                type="primary",
                                use_container_width=True,
                                disabled=not confirm_reset_category,
                                key=f"direct_reset_confirm_button_{selected_batch}_{reset_exam_code}",
                            ):
                                try:
                                    removed_count = reset_one_exam_category(
                                        selected_batch,
                                        reset_exam_code,
                                    )
                                    st.session_state[reset_flash_key] = (
                                        f"Reset {reset_exam_code} selesai. "
                                        f"{removed_count} catatan dokumen dihapus. "
                                        "Batch dan kategori lain tetap ada."
                                    )
                                    st.session_state.pop(reset_request_key, None)
                                    st.session_state[reset_nonce_key] += 1
                                    st.rerun()
                                except Exception as exc:
                                    st.error(
                                        f"Reset kategori gagal: {exc}. "
                                        "Tidak ada tindakan lanjutan yang dilakukan."
                                    )
                        with cancel_col:
                            if st.button(
                                "Batal",
                                use_container_width=True,
                                key=f"direct_reset_cancel_button_{selected_batch}_{reset_exam_code}",
                            ):
                                st.session_state.pop(reset_request_key, None)
                                st.session_state[reset_nonce_key] += 1
                                st.rerun()

                st.divider()

                # ==================================================
                # UPLOAD PDF
                # ==================================================

                st.markdown("### Upload PDF ke kategori terpilih")

                st.info(
                    f"File yang dipilih akan disimpan sebagai kategori "
                    f"**{selected_pdf_exam} — "
                    f"{exam_labels_for_upload.get(selected_pdf_exam, selected_pdf_exam)}**. "
                    "Pastikan kategori sudah benar sebelum menyimpan."
                )

                # KEY UPLOADER
                direct_version_key = (
                    f"direct_pdf_uploader_version_{selected_batch}"
                )

                if direct_version_key not in st.session_state:
                    st.session_state[direct_version_key] = 0

                direct_pdfs = st.file_uploader(
                    "Pilih file PDF",
                    type=["pdf"],
                    accept_multiple_files=True,
                    key=(
                        f"direct_pdf_uploader_{selected_batch}_"
                        f"{st.session_state[direct_version_key]}"
                    ),
                )

                # TAMPILKAN NAMA FILE YANG DIPILIH
                if direct_pdfs:
                    st.markdown("**Daftar file yang akan diunggah:**")

                    for uploaded in direct_pdfs:
                        st.write(f"- {uploaded.name}")

                    st.caption(
                        f"Total: {len(direct_pdfs)} file dipilih."
                    )

                    if len(direct_pdfs) > MAX_FILE_COUNT:
                        st.error(
                            f"Maksimal {MAX_FILE_COUNT} file sekali upload."
                        )

                # TOMBOL SIMPAN DAN RESET
                col_save, col_reset = st.columns(2)

                with col_save:
                    save_clicked = st.button(
                        "Simpan PDF langsung",
                        type="primary",
                        use_container_width=True,
                        disabled=(
                            not bool(direct_pdfs)
                            or len(direct_pdfs) > MAX_FILE_COUNT
                        ),
                        key=f"save_direct_pdf_{selected_batch}",
                    )

                with col_reset:
                    reset_clicked = st.button(
                        "Reset pilihan file",
                        use_container_width=True,
                        disabled=not bool(direct_pdfs),
                        key=f"reset_direct_pdf_{selected_batch}",
                    )

                # RESET PILIHAN FILE SAJA
                if reset_clicked:
                    st.session_state[direct_version_key] += 1
                    st.rerun()

                # SIMPAN PDF
                if save_clicked:
                    with st.spinner(
                        "Menyimpan dan memvalidasi PDF..."
                    ):
                        stats = store_uploaded_documents(
                            direct_pdfs,
                            selected_batch,
                            selected_pdf_exam,
                        )

                    st.session_state[
                        f"direct_upload_result_{selected_batch}"
                    ] = {
                        "exam": selected_pdf_exam,
                        "saved": stats["saved_count"],
                        "duplicates": stats["duplicate_count"],
                        "errors": stats["errors"],
                    }

                    # Kosongkan pilihan file setelah proses simpan.
                    st.session_state[direct_version_key] += 1
                    st.rerun()

                # HASIL UPLOAD TERAKHIR
                last_upload = st.session_state.get(
                    f"direct_upload_result_{selected_batch}"
                )

                if last_upload:
                    st.divider()
                    st.markdown("**Hasil upload terakhir**")

                    st.caption(
                        f"Kategori: {last_upload['exam']}"
                    )

                    st.success(
                        f"{last_upload['saved']} file sumber baru disimpan. "
                        f"{last_upload['duplicates']} upload identik terdeteksi."
                    )

                    for message in last_upload["errors"]:
                        st.error(message)

            else:
                st.info(
                    "Batch tidak memiliki kategori pemeriksaan."
                )

        with st.expander("Pengaturan identifikasi nama dan OCR Tesseract", expanded=False):
            st.markdown(
                "**Metode:** text layer PDF dibaca lebih dahulu. Tesseract hanya dijalankan "
                "ketika nama tidak ditemukan atau text layer terlalu sedikit. Hasil OCR "
                "disimpan dalam cache lokal SQLite agar analisis ulang tidak selalu mengulang OCR."
            )
            tesseract_status = get_tesseract_status()
            if tesseract_status["available"]:
                st.success(
                    f"Tesseract aktif — versi {tesseract_status['version']}. "
                    f"Bahasa tersedia: {', '.join(tesseract_status['languages']) or 'tidak diketahui'}."
                )
                st.caption(tesseract_status["message"])
            else:
                st.error(tesseract_status["message"])
                st.caption(
                    "Instal binary Tesseract di komputer/server, instal paket bahasa ind dan eng, "
                    "lalu instal wrapper Python pytesseract. Aplikasi masih dapat membaca text layer PDF."
                )
            enable_ocr = st.checkbox(
                "Aktifkan OCR fallback untuk PDF hasil scan/foto",
                value=OCR_ENABLED_DEFAULT,
                key=f"enable_hybrid_ocr_{selected_batch}",
                help="OCR dibatasi ke maksimal halaman awal sesuai konfigurasi MCU_OCR_MAX_PAGES.",
            )
            st.caption(
                f"Bahasa konfigurasi: {OCR_LANGUAGE} | Resolusi: {OCR_DPI} DPI | "
                f"Maksimal halaman OCR: {OCR_MAX_PAGES}."
            )

        if st.button(
            "Analisis ulang dokumen dan perbarui identitas pasien",
            type="primary",
            use_container_width=True,
            key=f"analyze_batch_{selected_batch}",
        ):
            with st.spinner("Membaca PDF dan menyusun kandidat pasien..."):
                candidates = analyze_batch_documents(selected_batch, enable_ocr=enable_ocr)
            # Analisis ulang wajib meminta persetujuan ulang; hapus state widget lama.
            for old_key in list(st.session_state.keys()):
                if (
                    old_key.startswith(f"patient_approval_{selected_batch}_")
                    or old_key.startswith(f"patient_name_{selected_batch}_")
                    or old_key.startswith(f"patient_name_source_review_{selected_batch}_")
                    or old_key.startswith(f"patient_mr_variation_review_{selected_batch}_")
                ):
                    del st.session_state[old_key]
            new_patients: dict[str, dict[str, Any]] = {}
            for candidate_name, files in candidates.items():
                patient_key = candidate_name or "NAMA_TIDAK_TERDETEKSI"
                new_patients[patient_key] = {
                    "final_name": patient_key,
                    "is_validated": False,
                    "files": files,
                    "identity_resolution": {},
                }
            st.session_state[batch_state_key] = new_patients
            st.success(f"Analisis selesai. Ditemukan {len(new_patients)} kandidat pasien.")

        patients_data = st.session_state.get(batch_state_key)
        if patients_data is not None:
            # Bersihkan key widget sebelum komponen identitas dibuat ulang.
            # Marker ini dipasang oleh aksi gabung/pisah di dalam daftar pasien.
            reset_identity_widgets_key = f"reset_identity_widgets_{selected_batch}"
            if st.session_state.pop(reset_identity_widgets_key, False):
                widget_prefixes = (
                    f"patient_approval_{selected_batch}_",
                    f"patient_name_{selected_batch}_",
                    f"patient_mr_variation_review_{selected_batch}_",
                    f"same_name_target_{selected_batch}_",
                    f"verify_same_identity_{selected_batch}_",
                    f"split_document_{selected_batch}_",
                    f"split_name_{selected_batch}_",
                    f"split_submit_{selected_batch}_",
                )
                widget_exact_keys = {
                    f"identity_merge_a_{selected_batch}",
                    f"identity_merge_b_{selected_batch}",
                    f"identity_merge_final_{selected_batch}",
                    f"identity_merge_confirm_{selected_batch}",
                }
                for old_key in list(st.session_state.keys()):
                    if old_key in widget_exact_keys or any(
                        old_key.startswith(prefix) for prefix in widget_prefixes
                    ):
                        st.session_state.pop(old_key, None)

            if not patients_data:
                st.info("Belum ada PDF yang dapat dianalisis pada batch ini.")
            else:
                total_patients = len(patients_data)
                total_docs = sum(len(item["files"]) for item in patients_data.values())
                approved_count = sum(
                    1 for item in patients_data.values()
                    if item["is_validated"] and not find_internal_identity_conflicts(item)
                )
                patients_by_name = {
                    item["final_name"]: item for item in patients_data.values()
                }
                similar_pairs = find_similar_patient_pairs(
                    [item["final_name"] for item in patients_data.values()],
                    patients_by_name,
                )

                col1, col2, col3 = st.columns(3)
                col1.metric("Kandidat pasien", total_patients)
                col2.metric("Dokumen PDF", total_docs)
                col3.metric("Disetujui", f"{approved_count}/{total_patients}")

                st.caption(
                    "Nama dari dalam PDF (text layer/OCR) dipakai sebagai petunjuk utama; "
                    "nama filename hanya fallback dan pembanding. OCR confidence rendah tetap "
                    "ditampilkan sebagai hint tetapi wajib diverifikasi. Nama mirip tidak digabung otomatis."
                )

                # Diagnostik ditampilkan di atas kartu pasien agar sumber nama setiap file
                # dapat dilihat tanpa harus membuka satu per satu kelompok pasien.
                identity_diagnostic_rows = []
                for group_name, group_patient in patients_data.items():
                    for doc_info in group_patient.get("files", []):
                        identity_diagnostic_rows.append({
                            "Kelompok sementara": group_name,
                            "File": doc_info.get("original_filename", doc_info.get("filename", "")),
                            "Kategori": doc_info.get("exam_code", ""),
                            "Nama dari file": doc_info.get("name_from_file") or "—",
                            "Nama di text layer": doc_info.get("name_from_text") or "Tidak terbaca",
                            "Nama hasil OCR": doc_info.get("name_from_ocr") or "Tidak terbaca",
                            "Nama yang dipakai": doc_info.get("candidate_name") or "—",
                            "Sumber nama": doc_info.get("name_source") or "—",
                            "Confidence OCR": (
                                f"{doc_info.get('ocr_confidence'):.1f}%"
                                if doc_info.get("ocr_confidence") is not None else "—"
                            ),
                            "Status OCR": doc_info.get("ocr_status", "—"),
                            "No. RM (4 digit akhir)": doc_info.get("mr_masked") or "Tidak terbaca",
                            "Label No. RM": doc_info.get("mr_source_label") or "—",
                            "Status pembacaan RM": {
                                "OK": "Terbaca",
                                "NOT_FOUND": "Tidak ditemukan",
                                "AMBIGUOUS": "Lebih dari satu nomor — perlu cek",
                            }.get(doc_info.get("mr_extraction_status"), "Tidak diketahui"),
                            "Perlu verifikasi": "YA" if doc_info.get("name_review_required") else "Tidak",
                        })
                with st.expander(
                    "Diagnostik: nama yang terbaca dari setiap PDF",
                    expanded=any(row["Perlu verifikasi"] == "YA" for row in identity_diagnostic_rows),
                ):
                    st.caption(
                        "Kolom ini menunjukkan bukti deteksi nama pada setiap file. Jika nama di text layer "
                        "dan OCR tertulis 'Tidak terbaca', aplikasi belum berhasil mendapatkan nama dari isi PDF. "
                        "Periksa posisi halaman identitas, kualitas scan, dan status Tesseract."
                    )
                    st.dataframe(identity_diagnostic_rows, use_container_width=True, hide_index=True)

                if similar_pairs:
                    st.warning(
                        "Ada beberapa kelompok nama yang perlu diperiksa. "
                        "Ini belum berarti dokumennya milik orang yang sama."
                    )
                    st.caption(
                        "Cara membaca: kemiripan nama hanya membandingkan tulisan nama, "
                        "bukan memastikan identitas orang. Cocokkan No. RM, NIK, dan tanggal "
                        "lahir pada PDF sebelum memutuskan untuk menggabungkan."
                    )
                    status_labels = {
                        "same": "Sama pada kategori pemeriksaan yang sama",
                        "different": "Berbeda pada kategori pemeriksaan yang sama",
                        "different_categories": "Ditemukan pada kategori berbeda — tidak dibandingkan langsung",
                        "inconsistent": "Tidak konsisten — ada beberapa nilai dalam kelompok",
                        "incomplete": "Belum lengkap — hanya tersedia pada salah satu kelompok",
                        "missing": "Belum ditemukan di dokumen",
                    }
                    for name1, name2, _pair_score in similar_pairs:
                        evidence = compare_patient_groups(
                            name1, name2,
                            patients_by_name.get(name1),
                            patients_by_name.get(name2),
                        )
                        name_score = evidence["name_score"]
                        if name_score >= 90:
                            name_level = "Sangat mirip"
                        elif name_score >= 75:
                            name_level = "Agak mirip"
                        else:
                            name_level = "Tidak terlalu mirip"

                        with st.expander(f"{name1} ↔ {name2}", expanded=False):
                            st.write(f"**Kemiripan nama:** {name_level} ({name_score:.1f}%)")
                            st.write(
                                "**Nomor rekam medis (MRN):** "
                                + status_labels[evidence["identity_status"]["mr"]]
                            )
                            mr_a_text = ", ".join(
                                f"{exam}: {' / '.join(values)}"
                                for exam, values in evidence.get("mr_values_a", {}).items()
                            ) or "Tidak terbaca"
                            mr_b_text = ", ".join(
                                f"{exam}: {' / '.join(values)}"
                                for exam, values in evidence.get("mr_values_b", {}).items()
                            ) or "Tidak terbaca"
                            st.caption(
                                f"MRN yang dipakai — {name1}: {mr_a_text} | "
                                f"{name2}: {mr_b_text}"
                            )
                            st.write(
                                "**NIK:** "
                                + status_labels[evidence["identity_status"]["nik"]]
                            )
                            st.write(
                                "**Tanggal lahir:** "
                                + status_labels[evidence["identity_status"]["dob"]]
                            )
                            if evidence["identity_conflict"]:
                                st.error("**Kesimpulan:** " + evidence["recommendation"])
                            elif evidence["nik_match"] or evidence["mr_match"]:
                                st.info("**Kesimpulan:** " + evidence["recommendation"])
                            else:
                                st.caption("**Kesimpulan:** " + evidence["recommendation"])
                    st.caption(
                        "Persentase kemiripan bukan peluang bahwa dua dokumen milik orang yang sama. "
                        "Keputusan gabung atau pisah tetap harus berdasarkan identitas pada dokumen."
                    )

                # ----------------------------------------------------
                # PENYELESAIAN MANUAL NAMA YANG MIRIP
                # ----------------------------------------------------
                if len(patients_data) >= 2:
                    with st.expander(
                        "Penyelesaian nama mirip — gabungkan jika sudah terverifikasi",
                        expanded=bool(similar_pairs),
                    ):
                        st.warning(
                            "Gabungkan hanya jika kedua kelompok sudah dipastikan milik orang yang sama. "
                            "Cocokkan nama, No. RM, NIK, dan tanggal lahir pada PDF. Jika ada data "
                            "yang berbeda atau meragukan, jangan gabungkan dulu."
                        )
                        patient_options = list(patients_data.keys())
                        left_col, right_col = st.columns(2)
                        with left_col:
                            merge_name_a = st.selectbox(
                                "Kandidat pertama",
                                options=patient_options,
                                key=f"identity_merge_a_{selected_batch}",
                            )
                        with right_col:
                            other_options = [name for name in patient_options if name != merge_name_a]
                            merge_name_b = st.selectbox(
                                "Kandidat kedua",
                                options=other_options,
                                key=f"identity_merge_b_{selected_batch}",
                            )

                        suggested_final_name = merge_name_a
                        final_identity_name = st.text_input(
                            "Nama pasien final untuk kedua kelompok",
                            value=suggested_final_name,
                            key=f"identity_merge_final_{selected_batch}",
                        ).strip().upper()
                        identity_confirmed = st.checkbox(
                            "Saya sudah memeriksa identitas pendukung dan memastikan kedua kelompok adalah orang yang sama.",
                            key=f"identity_merge_confirm_{selected_batch}",
                        )

                        if st.button(
                            "Gabungkan dua kandidat terpilih",
                            type="primary",
                            disabled=not identity_confirmed or not final_identity_name,
                            key=f"identity_merge_submit_{selected_batch}",
                        ):
                            if merge_name_a == merge_name_b:
                                st.error("Pilih dua kandidat yang berbeda.")
                            else:
                                selected_keys = {merge_name_a, merge_name_b}
                                collision = (
                                    final_identity_name in patients_data
                                    and final_identity_name not in selected_keys
                                )
                                if collision:
                                    st.error(
                                        "Nama final sudah digunakan kandidat lain. "
                                        "Pilih nama final yang tidak bertabrakan."
                                    )
                                else:
                                    first_patient = patients_data[merge_name_a]
                                    second_patient = patients_data[merge_name_b]
                                    combined_files = []
                                    seen_file_ids = set()
                                    for file_item in first_patient["files"] + second_patient["files"]:
                                        unique_id = file_item.get("file_hash") or file_item.get("filepath")
                                        if unique_id in seen_file_ids:
                                            continue
                                        seen_file_ids.add(unique_id)
                                        combined_files.append(file_item)

                                    # Hapus dua grup lama, lalu buat satu grup baru.
                                    del patients_data[merge_name_a]
                                    del patients_data[merge_name_b]
                                    patients_data[final_identity_name] = {
                                        "final_name": final_identity_name,
                                        "is_validated": False,
                                        "files": combined_files,
                                        "identity_resolution": {
                                            "type": "manual_merge",
                                            "merged_from": [merge_name_a, merge_name_b],
                                            "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                                        },
                                    }
                                    log_identity_action(
                                        selected_batch, "MANUAL_MERGE",
                                        [merge_name_a, merge_name_b], final_identity_name,
                                        {"document_count": len(combined_files)},
                                    )

                                    # Hasil merge lama tidak boleh dipakai setelah identitas berubah.
                                    deleted_outputs = delete_batch_merge_outputs(selected_batch)
                                    st.session_state.pop(f"merged_files_{selected_batch}", None)

                                    # Bersihkan key widget berbasis indeks agar persetujuan lama
                                    # tidak berpindah ke pasien lain setelah daftar berubah.
                                    for old_key in list(st.session_state.keys()):
                                        if (
                                            old_key.startswith(f"patient_approval_{selected_batch}_")
                                            or old_key.startswith(f"patient_name_{selected_batch}_")
                                            or old_key.startswith(f"patient_mr_variation_review_{selected_batch}_")
                                            or old_key in {
                                                f"identity_merge_a_{selected_batch}",
                                                f"identity_merge_b_{selected_batch}",
                                                f"identity_merge_final_{selected_batch}",
                                                f"identity_merge_confirm_{selected_batch}",
                                            }
                                        ):
                                            del st.session_state[old_key]

                                    st.session_state[batch_state_key] = patients_data
                                    st.success(
                                        f"Berhasil menggabungkan {merge_name_a} dan {merge_name_b} "
                                        f"menjadi {final_identity_name}. Persetujuan pasien direset; "
                                        f"silakan periksa ulang dokumennya. {deleted_outputs} hasil PDF lama dihapus."
                                    )
                                    st.rerun()

                # Warna tombol mengikuti konvensi lampu lalu lintas.
                # Selector dibatasi ke container masing-masing agar tombol lain tidak berubah.
                st.markdown(
                    """
                    <style>
                    .st-key-bulk_approve_button div[data-testid="stButton"] > button {
                        background-color: #198754 !important;
                        border: 1px solid #198754 !important;
                        color: #ffffff !important;
                    }
                    .st-key-bulk_approve_button div[data-testid="stButton"] > button:hover {
                        background-color: #157347 !important;
                        border-color: #146c43 !important;
                        color: #ffffff !important;
                    }
                    .st-key-bulk_approve_button div[data-testid="stButton"] > button:focus:not(:active) {
                        box-shadow: 0 0 0 0.2rem rgba(25, 135, 84, 0.25) !important;
                    }
                    .st-key-bulk_cancel_button div[data-testid="stButton"] > button {
                        background-color: #dc3545 !important;
                        border: 1px solid #dc3545 !important;
                        color: #ffffff !important;
                    }
                    .st-key-bulk_cancel_button div[data-testid="stButton"] > button:hover {
                        background-color: #bb2d3b !important;
                        border-color: #b02a37 !important;
                        color: #ffffff !important;
                    }
                    .st-key-bulk_cancel_button div[data-testid="stButton"] > button:focus:not(:active) {
                        box-shadow: 0 0 0 0.2rem rgba(220, 53, 69, 0.25) !important;
                    }
                    </style>
                    """,
                    unsafe_allow_html=True,
                )
                with st.container(key="bulk_approve_button"):
                    if st.button(
                        "Setujui semua kandidat",
                        key=f"approve_all_{selected_batch}",
                    ):
                        # Proses setiap kandidat secara terpisah. Konflik pada satu
                        # pasien tidak menghalangi kandidat lain yang sudah aman.
                        unresolved_pairs = find_unresolved_final_name_collisions(patients_data)
                        unresolved_keys = {key for pair in unresolved_pairs for key in pair}
                        approved_now = 0
                        skipped_candidates: list[tuple[str, list[str]]] = []

                        for patient_index, (patient_key, patient) in enumerate(
                            patients_data.items(), start=1
                        ):
                            blockers: list[str] = []

                            if patient_key in unresolved_keys:
                                blockers.append("nama final sama dengan kandidat lain dan belum diputuskan")

                            blockers.extend(find_internal_identity_conflicts(patient))

                            mismatch_files = [
                                f for f in patient.get("files", [])
                                if f.get("name_review_required")
                            ]
                            for mismatch_index, file_item in enumerate(mismatch_files, start=1):
                                review_key = (
                                    f"patient_name_source_review_{selected_batch}_{patient_index}_"
                                    f"{(file_item.get('file_hash') or str(mismatch_index))[:12]}"
                                )
                                if not bool(st.session_state.get(review_key, False)):
                                    blockers.append(
                                        "perbedaan nama file dan isi PDF belum diverifikasi"
                                    )
                                    break

                            mr_variation = find_patient_mr_variations(patient)
                            if mr_variation and mr_variation["same_exam_variations"]:
                                mr_review_key = f"patient_mr_variation_review_{selected_batch}_{patient_index}"
                                if not bool(st.session_state.get(mr_review_key, False)):
                                    blockers.append(
                                        "lebih dari satu No. RM pada kategori yang sama belum diperiksa/dikonfirmasi"
                                    )

                            if not str(patient.get("final_name", "")).strip():
                                blockers.append("nama pasien final masih kosong")

                            approval_key = f"patient_approval_{selected_batch}_{patient_index}"
                            if blockers:
                                patient["is_validated"] = False
                                st.session_state[approval_key] = False
                                skipped_candidates.append(
                                    (patient.get("final_name", patient_key), list(dict.fromkeys(blockers)))
                                )
                            else:
                                patient["is_validated"] = True
                                st.session_state[approval_key] = True
                                approved_now += 1

                        if approved_now:
                            st.success(
                                f"{approved_now} kandidat disetujui. "
                                f"{len(skipped_candidates)} kandidat dilewati karena masih perlu pemeriksaan."
                            )
                        else:
                            st.warning(
                                "Belum ada kandidat yang dapat disetujui. "
                                "Periksa alasan kandidat yang masih tertahan."
                            )

                        if skipped_candidates:
                            with st.expander(
                                f"Lihat {len(skipped_candidates)} kandidat yang dilewati",
                                expanded=True,
                            ):
                                for candidate_name, reasons in skipped_candidates:
                                    st.markdown(f"**{candidate_name}**")
                                    for reason in reasons:
                                        st.caption(f"• {reason}")

                with st.container(key="bulk_cancel_button"):
                    if st.button(
                        "Batalkan semua persetujuan",
                        key=f"unapprove_all_{selected_batch}",
                    ):
                        for patient_index, patient in enumerate(patients_data.values(), start=1):
                            patient["is_validated"] = False
                            st.session_state[f"patient_approval_{selected_batch}_{patient_index}"] = False
                        st.rerun()

                search_query = st.text_input(
                    "Cari nama pasien",
                    key=f"patient_search_{selected_batch}",
                ).strip().upper()

                for index, (original_key, patient) in enumerate(patients_data.items(), start=1):
                    if search_query and search_query not in patient["final_name"].upper():
                        continue

                    with st.expander(
                        f"Pasien {index}: {patient['final_name']} "
                        f"({len(patient['files'])} dokumen) — "
                        f"{'Disetujui' if patient['is_validated'] else 'Belum disetujui'}",
                        expanded=not patient["is_validated"],
                    ):
                        name_key = f"patient_name_{selected_batch}_{index}"
                        approval_key = f"patient_approval_{selected_batch}_{index}"
                        if name_key not in st.session_state:
                            st.session_state[name_key] = patient["final_name"]
                        internal_conflicts = find_internal_identity_conflicts(patient)
                        mr_variation = find_patient_mr_variations(patient)
                        mr_review_pending = False
                        mr_review_key = f"patient_mr_variation_review_{selected_batch}_{index}"
                        if mr_variation:
                            same_exam_variations = mr_variation["same_exam_variations"]
                            if same_exam_variations:
                                variation_parts = [
                                    f"{exam}: {', '.join(masks) if masks else 'lebih dari satu nomor'}"
                                    for exam, masks in same_exam_variations.items()
                                ]
                                st.warning(
                                    "Ada beberapa No. RM berbeda pada kategori pemeriksaan yang sama: "
                                    + "; ".join(variation_parts)
                                    + ". Periksa sumber dokumen sebelum menyetujui."
                                )
                                reviewed_mr = st.checkbox(
                                    "Saya sudah memeriksa perbedaan No. RM pada kategori ini dan memastikan dokumennya milik pasien yang sama.",
                                    key=mr_review_key,
                                )
                                if not reviewed_mr:
                                    mr_review_pending = True
                            elif mr_variation["cross_category"]:
                                categories = ", ".join(mr_variation["category_summaries"].keys())
                                st.info(
                                    "No. RM berbeda ditemukan pada kategori pemeriksaan yang berbeda "
                                    f"({categories}). Hal ini dapat terjadi karena vendor VISUS, AUDIOMETRI, "
                                    "atau pemeriksaan lain menggunakan sistem pendaftaran masing-masing. "
                                    "Perbedaan lintas kategori tidak menghalangi persetujuan."
                                )

                        if approval_key not in st.session_state:
                            st.session_state[approval_key] = patient["is_validated"]

                        patient["final_name"] = st.text_input(
                            "Nama pasien final (koreksi bila diperlukan)",
                            key=name_key,
                        ).strip().upper()

                        # Deteksi nama final yang sama dengan kandidat lain.
                        # Nama sama tidak pernah otomatis menyatukan dokumen.
                        current_final_key = normalize_patient_identity_name(patient["final_name"])
                        matching_candidate_keys = [
                            other_key
                            for other_key, other_patient in patients_data.items()
                            if other_key != original_key
                            and current_final_key
                            and normalize_patient_identity_name(other_patient.get("final_name", "")) == current_final_key
                        ]
                        unresolved_collision = False

                        if matching_candidate_keys:
                            collision_target_key = st.selectbox(
                                "Nama ini sudah digunakan kandidat lain",
                                options=matching_candidate_keys,
                                format_func=lambda key: patients_data[key]["final_name"],
                                key=f"same_name_target_{selected_batch}_{index}",
                            )
                            collision_target = patients_data[collision_target_key]
                            current_resolution = patient.get("identity_resolution", {})
                            target_resolution = collision_target.get("identity_resolution", {})
                            kept_separate = (
                                current_resolution.get("type") == "kept_separate"
                                and current_resolution.get("with_key") == collision_target_key
                                and current_resolution.get("name_key") == current_final_key
                            ) or (
                                target_resolution.get("type") == "kept_separate"
                                and target_resolution.get("with_key") == original_key
                                and target_resolution.get("name_key") == current_final_key
                            )

                            st.warning(
                                "Nama tersebut sudah dimiliki kandidat lain. Sistem menemukan "
                                "dua kelompok dokumen dengan nama final yang sama. Periksa "
                                "identitas pendukung sebelum menentukan tindakan."
                            )
                            with st.container(border=True):
                                st.markdown("**Koreksi identitas pasien**")
                                st.caption(f"Nama awal: {original_key}")
                                st.caption(f"Nama yang dikoreksi: {patient['final_name']}")
                                st.caption(
                                    "Nama tersebut sudah dimiliki kandidat: "
                                    f"{collision_target['final_name']}"
                                )
                                st.caption(
                                    "Dokumen kedua kelompok belum digabungkan. Pilih salah satu "
                                    "tindakan di bawah ini."
                                )
                                if kept_separate:
                                    st.info(
                                        "Kedua kelompok saat ini dipertahankan sebagai kandidat "
                                        "terpisah. Persetujuan identitas masing-masing tetap diperlukan."
                                    )

                                verify_same_key = f"verify_same_identity_{selected_batch}_{index}"
                                verified_same = st.checkbox(
                                    "Saya sudah memeriksa identitas pendukung dan memastikan kedua kelompok milik orang yang sama.",
                                    key=verify_same_key,
                                )
                                action_merge, action_separate = st.columns(2)

                                with action_merge:
                                    merge_to_existing = st.button(
                                        f"Gabungkan ke {collision_target['final_name']}",
                                        type="primary",
                                        use_container_width=True,
                                        disabled=not verified_same,
                                        key=f"merge_same_name_{selected_batch}_{index}",
                                    )

                                with action_separate:
                                    keep_separate = st.button(
                                        "Simpan sebagai kandidat terpisah",
                                        use_container_width=True,
                                        key=f"keep_separate_{selected_batch}_{index}",
                                    )

                                if merge_to_existing:
                                    target_files = collision_target["files"]
                                    source_files = patient["files"]
                                    combined_files = []
                                    seen_document_ids = set()
                                    for file_item in target_files + source_files:
                                        unique_id = (
                                            file_item.get("file_hash")
                                            or file_item.get("filepath")
                                            or file_item.get("filename")
                                        )
                                        if unique_id in seen_document_ids:
                                            continue
                                        seen_document_ids.add(unique_id)
                                        combined_files.append(file_item)

                                    collision_target["files"] = combined_files
                                    collision_target["final_name"] = collision_target["final_name"].strip().upper()
                                    collision_target["is_validated"] = False
                                    log_identity_action(
                                        selected_batch, "MANUAL_MERGE_NAME_COLLISION",
                                        [collision_target_key, original_key],
                                        collision_target["final_name"],
                                        {"document_count": len(combined_files)},
                                    )
                                    collision_target["identity_resolution"] = {
                                        "type": "manual_merge",
                                        "merged_from": [collision_target_key, original_key],
                                        "source_aliases": [original_key, patient["final_name"]],
                                        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                                    }
                                    del patients_data[original_key]

                                    deleted_outputs = delete_batch_merge_outputs(selected_batch)
                                    st.session_state.pop(f"merged_files_{selected_batch}", None)
                                    st.session_state[batch_state_key] = patients_data

                                    # Key widget dibersihkan pada rerun berikutnya,
                                    # sebelum widget pasien dibuat ulang.
                                    for old_key in list(st.session_state.keys()):
                                        if (
                                            old_key.startswith(f"patient_approval_{selected_batch}_")
                                            or old_key.startswith(f"patient_name_{selected_batch}_")
                                            or old_key.startswith(f"patient_mr_variation_review_{selected_batch}_")
                                        ):
                                            del st.session_state[old_key]
                                    st.session_state[f"reset_identity_widgets_{selected_batch}"] = True
                                    st.success(
                                        f"Dokumen {original_key} berhasil digabung ke "
                                        f"{collision_target['final_name']}. Persetujuan identitas "
                                        f"direset. {deleted_outputs} hasil PDF lama dihapus."
                                    )
                                    st.rerun()

                                if keep_separate:
                                    resolution = {
                                        "type": "kept_separate",
                                        "with_key": collision_target_key,
                                        "name_key": current_final_key,
                                        "recorded_at": datetime.now().isoformat(timespec="seconds"),
                                    }
                                    patient["identity_resolution"] = dict(resolution)
                                    log_identity_action(
                                        selected_batch, "KEEP_SEPARATE",
                                        [original_key, collision_target_key],
                                        patient["final_name"],
                                        {"reason": "Petugas mempertahankan kandidat sebagai dua pasien berbeda."},
                                    )
                                    collision_target["identity_resolution"] = {
                                        "type": "kept_separate",
                                        "with_key": original_key,
                                        "name_key": current_final_key,
                                        "recorded_at": resolution["recorded_at"],
                                    }
                                    patient["is_validated"] = False
                                    collision_target["is_validated"] = False
                                    st.session_state[batch_state_key] = patients_data
                                    st.session_state.pop(f"merged_files_{selected_batch}", None)
                                    deleted_outputs = delete_batch_merge_outputs(selected_batch)
                                    # Bersihkan widget pada rerun berikutnya sebelum
                                    # widget pasien dirender ulang.
                                    for old_key in list(st.session_state.keys()):
                                        if (
                                            old_key.startswith(f"patient_approval_{selected_batch}_")
                                            or old_key.startswith(f"patient_name_{selected_batch}_")
                                            or old_key.startswith(f"patient_mr_variation_review_{selected_batch}_")
                                        ):
                                            del st.session_state[old_key]
                                    st.session_state[f"reset_identity_widgets_{selected_batch}"] = True
                                    st.success(
                                        "Kedua kelompok disimpan sebagai kandidat terpisah. "
                                        "Setujui identitas masing-masing setelah diverifikasi. "
                                        f"{deleted_outputs} hasil PDF lama dihapus."
                                    )
                                    st.rerun()

                            unresolved_collision = not kept_separate

                        # Perbedaan nama filename vs isi PDF harus dikonfirmasi
                        # satu per satu. Persetujuan massal tidak boleh melewatinya.
                        name_review_pending = False
                        mismatch_files = [
                            file_item for file_item in patient.get("files", [])
                            if file_item.get("name_review_required")
                        ]
                        for mismatch_index, file_item in enumerate(mismatch_files, start=1):
                            review_key = (
                                f"patient_name_source_review_{selected_batch}_{index}_"
                                f"{(file_item.get('file_hash') or str(mismatch_index))[:12]}"
                            )
                            file_title = file_item.get(
                                "original_filename", file_item.get("filename", "Dokumen")
                            )
                            filename_name = file_item.get("name_from_file") or "(tidak terbaca)"
                            inside_name = file_item.get("name_from_document") or file_item.get("name_from_ocr") or "(tidak terbaca)"
                            if file_item.get("name_mismatch"):
                                st.warning(
                                    f"Perbedaan identitas nama: judul file = {filename_name}; "
                                    f"nama di dalam PDF = {inside_name}. "
                                    f"Kelompok sementara menggunakan nama dari isi dokumen "
                                    f"({file_item.get('name_source', 'sumber dokumen')})."
                                )
                            elif file_item.get("low_confidence_ocr_review"):
                                st.warning(
                                    f"Nama OCR pada {file_title} memiliki confidence di bawah "
                                    f"{OCR_NAME_MIN_CONFIDENCE:.0f}%. Periksa PDF sumber sebelum menyetujui."
                                )
                            elif file_item.get("name_not_detected_review"):
                                st.warning(
                                    f"Nama pasien belum berhasil dibaca dari isi {file_title}. "
                                    f"Sistem sementara memakai nama filename ({filename_name}); "
                                    "buka PDF dan verifikasi nama pasien secara manual. "
                                    "Persetujuan tetap ditahan sampai verifikasi dilakukan."
                                )
                            review_label = (
                                f"Saya sudah membuka dan memverifikasi isi {file_title}; "
                                f"nama pasien di dalam dokumen benar."
                            )
                            if review_key not in st.session_state:
                                st.session_state[review_key] = False
                            reviewed_name = st.checkbox(review_label, key=review_key)
                            if not reviewed_name:
                                name_review_pending = True

                        if approval_key not in st.session_state:
                            st.session_state[approval_key] = patient["is_validated"]
                        if (
                            unresolved_collision
                            or internal_conflicts
                            or name_review_pending
                            or mr_review_pending
                        ):
                            st.session_state[approval_key] = False
                            patient["is_validated"] = False

                        patient["is_validated"] = st.checkbox(
                            "Saya sudah memeriksa dan menyetujui identitas pasien ini",
                            key=approval_key,
                            disabled=(
                                unresolved_collision
                                or bool(internal_conflicts)
                                or name_review_pending
                                or mr_review_pending
                            ),
                        )
                        if name_review_pending:
                            st.caption(
                                "Persetujuan pasien dinonaktifkan sampai setiap perbedaan "
                                "nama file dan nama di dalam PDF diverifikasi."
                            )
                        if internal_conflicts:
                            st.caption(
                                "Persetujuan dinonaktifkan karena identitas pendukung di dalam kelompok belum konsisten. "
                                "Pisahkan dokumen atau periksa sumber identitas sebelum melanjutkan."
                            )
                        if mr_review_pending:
                            st.caption(
                                "Persetujuan menunggu konfirmasi karena terdapat lebih dari satu No. RM "
                                "pada kategori pemeriksaan yang sama."
                            )
                        if unresolved_collision:
                            st.caption(
                                "Persetujuan dinonaktifkan sampai lu memilih untuk "
                                "menggabungkan kandidat atau menyimpannya secara terpisah."
                            )
                        if patient.get("identity_resolution", {}).get("type") == "manual_merge":
                            merged_from = patient["identity_resolution"].get("merged_from", [])
                            st.info(
                                "Kelompok ini merupakan hasil penggabungan manual dari: "
                                + " dan ".join(merged_from)
                                + ". Pastikan identitas dan seluruh dokumen benar sebelum menyetujui."
                            )

                        if internal_conflicts:
                            st.error(
                                "Ada ketidaksesuaian identitas pendukung dalam kelompok dokumen ini: "
                                + "; ".join(internal_conflicts)
                            )
                            st.caption(
                                "Periksa apakah satu nama dipakai oleh lebih dari satu orang atau "
                                "apakah OCR salah membaca identitas. Dokumen dapat dipisahkan "
                                "menjadi kandidat baru untuk diperiksa secara terpisah."
                            )
                            if len(patient["files"]) >= 2:
                                split_labels = [
                                    f"{f.get('original_filename', f.get('filename', 'Dokumen'))} "
                                    f"| {f.get('exam_code', '?')} "
                                    f"| NIK {f.get('nik_masked') or '-'} "
                                    f"| RM {f.get('mr_masked') or '-'}"
                                    for f in patient["files"]
                                ]
                                split_index = st.selectbox(
                                    "Pilih satu dokumen yang akan dipisahkan",
                                    options=list(range(len(patient["files"]))),
                                    format_func=lambda i: split_labels[i],
                                    key=f"split_document_{selected_batch}_{index}",
                                )
                                split_default = patient["final_name"]
                                split_final_name = st.text_input(
                                    "Nama untuk kandidat baru",
                                    value=split_default,
                                    key=f"split_name_{selected_batch}_{index}",
                                ).strip().upper()
                                if st.button(
                                    "Pisahkan dokumen menjadi kandidat baru",
                                    key=f"split_submit_{selected_batch}_{index}",
                                    use_container_width=True,
                                    disabled=not split_final_name,
                                ):
                                    moved_file = patient["files"].pop(split_index)
                                    patient["is_validated"] = False
                                    new_key = split_final_name
                                    suffix = 2
                                    while new_key in patients_data:
                                        new_key = f"{split_final_name} [{suffix}]"
                                        suffix += 1
                                    patients_data[new_key] = {
                                        "final_name": split_final_name,
                                        "is_validated": False,
                                        "files": [moved_file],
                                        "identity_resolution": {},
                                    }
                                    shared_name_key = normalize_patient_identity_name(split_final_name)
                                    timestamp = datetime.now().isoformat(timespec="seconds")
                                    patient["identity_resolution"] = {
                                        "type": "kept_separate",
                                        "with_key": new_key,
                                        "name_key": shared_name_key,
                                        "recorded_at": timestamp,
                                    }
                                    patients_data[new_key]["identity_resolution"] = {
                                        "type": "kept_separate",
                                        "with_key": original_key,
                                        "name_key": shared_name_key,
                                        "recorded_at": timestamp,
                                    }
                                    log_identity_action(
                                        selected_batch, "SPLIT_DOCUMENT",
                                        [original_key], split_final_name,
                                        {
                                            "filename": moved_file.get("original_filename", moved_file.get("filename", "")),
                                            "exam_code": moved_file.get("exam_code", ""),
                                        },
                                    )
                                    patient["is_validated"] = False
                                    patients_data[new_key]["is_validated"] = False
                                    st.session_state[batch_state_key] = patients_data
                                    st.session_state.pop(f"merged_files_{selected_batch}", None)
                                    delete_batch_merge_outputs(selected_batch)
                                    for old_key in list(st.session_state.keys()):
                                        if (
                                            old_key.startswith(f"patient_approval_{selected_batch}_")
                                            or old_key.startswith(f"patient_name_{selected_batch}_")
                                            or old_key.startswith(f"patient_mr_variation_review_{selected_batch}_")
                                        ):
                                            del st.session_state[old_key]
                                    st.session_state[f"reset_identity_widgets_{selected_batch}"] = True
                                    st.success(
                                        "Dokumen dipisahkan. Kedua kandidat tetap belum disetujui; "
                                        "periksa masing-masing identitas sebelum merge."
                                    )
                                    st.rerun()

                        files_by_exam: dict[str, list[dict[str, Any]]] = {}
                        for file_item in patient["files"]:
                            files_by_exam.setdefault(file_item["exam_code"], []).append(file_item)

                        missing_exams = [code for code in exam_order_default if code not in files_by_exam]
                        if missing_exams:
                            st.warning(
                                "Pemeriksaan belum ditemukan untuk pasien ini: "
                                + ", ".join(missing_exams)
                                + ". Periksa kelengkapan sebelum menyetujui."
                            )

                        for exam_code, exam_files in files_by_exam.items():
                            st.markdown(f"**{exam_code} — {len(exam_files)} file**")
                            for file_item in exam_files:
                                st.write(
                                    f"- {file_item.get('original_filename', file_item['filename'])} "
                                    f"| Kategori: {exam_code} "
                                    f"| Halaman: {file_item.get('page_count', '?')}"
                                )
                                if file_item.get("name_mismatch"):
                                    st.warning(
                                        "Nama dari judul file berbeda dengan nama pasien di dalam PDF. "
                                        "Sistem mengelompokkan dokumen memakai nama dari dalam PDF; "
                                        "verifikasi sumber wajib dilakukan."
                                    )
                                if file_item.get("name_not_detected_review"):
                                    st.warning(
                                        "Nama pasien belum terbaca dari isi PDF. Nama file hanya dipakai "
                                        "sebagai nama sementara; verifikasi dokumen sumber wajib dilakukan."
                                    )
                                if file_item.get("source_conflict"):
                                    st.error(
                                        "Text layer dan OCR menghasilkan nama berbeda. "
                                        "Periksa halaman identitas PDF secara langsung sebelum menyetujui."
                                    )
                                if file_item.get("name_from_text"):
                                    st.caption(
                                        f"Nama terbaca di PDF (text layer): {file_item['name_from_text']}"
                                    )
                                if file_item.get("name_from_file"):
                                    st.caption(
                                        f"Nama dari filename (pembanding/fallback): {file_item['name_from_file']}"
                                    )
                                if file_item.get("name_source"):
                                    st.caption(
                                        f"Dasar pengelompokan: {file_item.get('name_source')} "
                                        f"— {file_item.get('candidate_name', '')}"
                                    )
                                if file_item.get("name_from_ocr"):
                                    st.caption(
                                        f"Nama hasil OCR: {file_item['name_from_ocr']}"
                                    )
                                st.caption(
                                    f"Sumber ekstraksi: {file_item.get('extraction_source', 'TEXT_LAYER')} | "
                                    f"Status OCR: {file_item.get('ocr_status', 'Belum dijalankan')}"
                                    + (
                                        f" | Confidence OCR: {file_item['ocr_confidence']:.1f}%"
                                        if file_item.get("ocr_confidence") is not None else ""
                                    )
                                )
                                identity_labels = []
                                if file_item.get("nik_masked"):
                                    identity_labels.append(f"NIK {file_item['nik_masked']}")
                                if file_item.get("mr_masked"):
                                    identity_labels.append(f"No. RM {file_item['mr_masked']}")
                                if file_item.get("dob_iso"):
                                    identity_labels.append(f"Tgl. lahir {file_item['dob_iso']}")
                                if identity_labels:
                                    st.caption("Identitas pendukung terbaca: " + " | ".join(identity_labels))

                with st.expander("Riwayat keputusan identitas (audit log)", expanded=False):
                    audit_rows = list_identity_audit(selected_batch, limit=100)
                    if audit_rows:
                        audit_view = []
                        for audit in audit_rows:
                            try:
                                source_names = json.loads(audit["source_names"] or "[]")
                            except Exception:
                                source_names = []
                            audit_view.append({
                                "Waktu": audit["created_at"],
                                "Aksi": audit["action"],
                                "Kandidat sumber": " | ".join(source_names),
                                "Nama hasil": audit["target_name"],
                                "Detail": audit["details"],
                            })
                        st.dataframe(audit_view, use_container_width=True, hide_index=True)
                    else:
                        st.caption("Belum ada keputusan merge/pisah identitas yang tercatat.")

                st.divider()
                st.subheader("Penggabungan PDF per pasien")
                st.caption(
                    "Susun urutan kategori pemeriksaan. Urutan teratas menjadi bagian awal PDF pasien."
                )
                with get_conn() as conn:
                    all_exam_rows = conn.execute(
                        "SELECT code, label FROM exam_types WHERE active=1"
                    ).fetchall()
                exam_labels = {row["code"]: row["label"] for row in all_exam_rows}
                # Urutan dibuat eksplisit dengan nomor agar tidak bergantung pada
                # urutan chip multiselect atau urutan pilihan widget.
                available_merge_codes = [
                    code for code in exam_order_default if code in exam_labels
                ]
                st.caption(
                    "Isi nomor urutan untuk setiap kategori. Nomor 1 akan menjadi "
                    "halaman paling awal pada PDF gabungan."
                )
                merge_ranks: dict[str, int] = {}
                rank_columns = st.columns(2)
                for rank_index, code in enumerate(available_merge_codes):
                    with rank_columns[rank_index % 2]:
                        merge_ranks[code] = st.number_input(
                            f"Urutan {exam_labels.get(code, code)} ({code})",
                            min_value=1,
                            max_value=max(1, len(available_merge_codes)),
                            value=rank_index + 1,
                            step=1,
                            key=f"merge_rank_{selected_batch}_{code}",
                        )

                ordered_codes = sorted(
                    available_merge_codes,
                    key=lambda code: (merge_ranks[code], available_merge_codes.index(code)),
                )
                duplicate_ranks = len(set(merge_ranks.values())) != len(merge_ranks)
                st.write(
                    "Urutan merge: "
                    + " → ".join(
                        f"{code} ({exam_labels.get(code, code)})"
                        for code in ordered_codes
                    )
                )
                if duplicate_ranks:
                    st.warning(
                        "Ada nomor urutan yang sama. Ubah agar setiap kategori "
                        "memiliki nomor urutan berbeda sebelum menjalankan merge."
                    )

                unresolved_name_collisions = find_unresolved_final_name_collisions(patients_data)
                approved_patients = [
                    patient for patient in patients_data.values()
                    if patient["is_validated"]
                    and patient["final_name"].strip()
                    and not find_internal_identity_conflicts(patient)
                ]
                st.info(
                    f"Pasien yang disetujui: {len(approved_patients)} dari {len(patients_data)}"
                )
                if unresolved_name_collisions:
                    st.warning(
                        f"Ada {len(unresolved_name_collisions)} pasangan nama final yang sama "
                        "belum diputuskan. Selesaikan di bagian koreksi identitas sebelum merge."
                    )

                if st.button(
                    "Proses merge PDF pasien yang disetujui",
                    type="primary",
                    use_container_width=True,
                    key=f"merge_patients_{selected_batch}",
                ):
                    if unresolved_name_collisions:
                        st.error(
                            "Merge dibatalkan: masih ada nama final yang sama dan belum "
                            "diputuskan sebagai satu pasien atau kandidat terpisah."
                        )
                    elif not approved_patients:
                        st.error("Belum ada pasien yang disetujui.")
                    elif not ordered_codes:
                        st.error("Batch ini belum memiliki urutan pemeriksaan.")
                    elif duplicate_ranks:
                        st.error("Perbaiki nomor urutan yang duplikat terlebih dahulu.")
                    else:
                        output_dir = OUTPUT_DIR / selected_batch
                        merged_paths: list[Path] = []
                        failures: list[str] = []
                        with st.spinner("Menggabungkan dokumen pasien..."):
                            # Jika dua orang berbeda memiliki nama final identik,
                            # beri akhiran pada nama file output agar tidak saling menimpa.
                            duplicate_name_totals: dict[str, int] = {}
                            for patient in approved_patients:
                                name_key_value = normalize_patient_identity_name(patient["final_name"])
                                duplicate_name_totals[name_key_value] = (
                                    duplicate_name_totals.get(name_key_value, 0) + 1
                                )

                            duplicate_name_seen: dict[str, int] = {}
                            for patient in approved_patients:
                                patient_final_name = patient["final_name"].strip()
                                name_key_value = normalize_patient_identity_name(patient_final_name)
                                duplicate_name_seen[name_key_value] = (
                                    duplicate_name_seen.get(name_key_value, 0) + 1
                                )
                                output_patient_name = patient_final_name
                                if duplicate_name_totals.get(name_key_value, 0) > 1:
                                    output_patient_name = (
                                        f"{patient_final_name} "
                                        f"(KANDIDAT {duplicate_name_seen[name_key_value]})"
                                    )

                                success, message = merge_patient_documents(
                                    output_patient_name,
                                    patient["files"],
                                    ordered_codes,
                                    output_dir,
                                )
                                if success:
                                    merged_paths.append(Path(message))
                                else:
                                    failures.append(
                                        f"{patient_final_name}: {message}"
                                    )

                        st.session_state[f"merged_files_{selected_batch}"] = [
                            str(path) for path in merged_paths
                        ]
                        if merged_paths:
                            st.success(
                                f"{len(merged_paths)} PDF pasien berhasil dibuat."
                            )
                        for failure in failures:
                            st.error(failure)

                merged_values = st.session_state.get(f"merged_files_{selected_batch}", [])
                merged_files = [Path(value) for value in merged_values if Path(value).exists()]
                if merged_files:
                    st.divider()
                    st.subheader("Download hasil penggabungan")
                    left, right = st.columns(2)
                    with left:
                        st.download_button(
                            f"Download ZIP ({len(merged_files)} PDF pasien)",
                            data=create_zip_from_files(merged_files),
                            file_name=f"Hasil_Merge_MCU_{selected_batch}.zip",
                            mime="application/zip",
                            use_container_width=True,
                            type="primary",
                        )
                    with right:
                        st.download_button(
                            "Download Master PDF",
                            data=merge_all_patient_pdfs_into_master(merged_files),
                            file_name=f"Master_MCU_{selected_batch}.pdf",
                            mime="application/pdf",
                            use_container_width=True,
                        )

                    st.download_button(
                        "Download laporan daftar kandidat (.TXT)",
                        data=make_processing_report(selected_batch, patients_data),
                        file_name=f"Laporan_MCU_{selected_batch}.txt",
                        mime="text/plain",
                    )


# ============================================================
# 15. KELOLA BATCH MCU
# ============================================================

elif page == "Kelola Batch MCU":
    st.subheader("Kelola Batch MCU")
    st.caption(
        "Pilih batch pada panel di samping untuk mengubah status atau memindahkannya "
        "ke Tong Sampah. Data tidak dihapus permanen saat dipindahkan."
    )

    with get_conn() as conn:
        batch_rows = conn.execute(
            "SELECT * FROM batches WHERE deleted_at IS NULL ORDER BY id DESC"
        ).fetchall()

    if not batch_rows:
        st.info("Belum ada batch. Buat batch terlebih dahulu.")
    else:
        batch_map = {row["batch_name"]: row for row in batch_rows}

        # Tabel dan panel tindakan ditampilkan berdampingan.
        table_col, action_col = st.columns([1.8, 1], gap="large")

        with table_col:
            st.markdown("#### Daftar Batch")
            overview = []
            for row in batch_rows:
                overview.append({
                    "Perusahaan": row["company"],
                    "Batch": row["batch_name"],
                    "Status proses": row["status"],
                    "Status": "Nonaktif" if row["archived_at"] else "Aktif",
                    "Dokumen Validasi": len(list_batch_documents(row["batch_name"])),
                    "Dokumen Converter": len(list_conversion_documents(row["batch_name"])),
                    "Dibuat": row["created_at"],
                })
            st.dataframe(
                overview,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Aktif: tersedia untuk upload dan Validasi & Merge. "
                "Nonaktif: disimpan, tetapi disembunyikan dari proses operasional."
            )

        with action_col:
            with st.container(border=True):
                st.markdown("#### Tindakan Batch")
                selected_batch = st.selectbox(
                    "Pilih batch",
                    options=list(batch_map.keys()),
                    format_func=lambda name: f"{batch_map[name]['company']} — {name}",
                    key="manage_batch_select",
                )
                selected_row = batch_map[selected_batch]
                is_inactive = bool(selected_row["archived_at"])

                st.markdown(f"**{selected_row['company']}**")
                st.caption(selected_batch)
                if is_inactive:
                    st.warning("Status saat ini: NONAKTIF")
                else:
                    st.success("Status saat ini: AKTIF")

                count_a, count_b = st.columns(2)
                count_a.metric(
                    "Dokumen Validasi",
                    len(list_batch_documents(selected_batch)),
                )
                count_b.metric(
                    "Dokumen Converter",
                    len(list_conversion_documents(selected_batch)),
                )

                toggle_key = f"confirm_batch_status_{selected_batch}"
                toggle_label = "Aktifkan Batch" if is_inactive else "Nonaktifkan Batch"
                if st.button(
                    toggle_label,
                    use_container_width=True,
                    type="primary" if is_inactive else "secondary",
                    key=f"toggle_status_request_{selected_batch}",
                ):
                    st.session_state[toggle_key] = True

                if st.session_state.get(toggle_key, False):
                    target_status = "Aktif" if is_inactive else "Nonaktif"
                    st.warning(
                        f"Batch **{selected_batch}** akan diubah menjadi **{target_status}**. "
                        "Dokumen dan hasil PDF tidak dihapus."
                    )
                    yes, no = st.columns(2)
                    with yes:
                        if st.button(
                            "Konfirmasi",
                            type="primary",
                            use_container_width=True,
                            key=f"toggle_status_confirm_{selected_batch}",
                        ):
                            if is_inactive:
                                unarchive_batch(selected_batch)
                            else:
                                archive_batch(selected_batch)
                            st.session_state.pop(toggle_key, None)
                            st.success(f"Status batch sekarang {target_status}.")
                            st.rerun()
                    with no:
                        if st.button(
                            "Batal",
                            use_container_width=True,
                            key=f"toggle_status_cancel_{selected_batch}",
                        ):
                            st.session_state.pop(toggle_key, None)
                            st.rerun()

                st.divider()
                trash_key = f"confirm_move_trash_{selected_batch}"
                if st.button(
                    "Pindahkan ke Tong Sampah",
                    use_container_width=True,
                    key=f"move_trash_request_{selected_batch}",
                ):
                    st.session_state[trash_key] = True

                if st.session_state.get(trash_key, False):
                    st.error(
                        "Batch akan disembunyikan dari daftar aktif dan masuk Tong Sampah. "
                        "File serta catatan database belum dihapus dan masih dapat dipulihkan."
                    )
                    trash_confirm = st.checkbox(
                        "Saya yakin ingin memindahkan batch ini ke Tong Sampah.",
                        key=f"move_trash_checkbox_{selected_batch}",
                    )
                    yes, no = st.columns(2)
                    with yes:
                        if st.button(
                            "Ya, Pindahkan",
                            type="primary",
                            disabled=not trash_confirm,
                            use_container_width=True,
                            key=f"move_trash_confirm_{selected_batch}",
                        ):
                            move_batch_to_trash(selected_batch)
                            st.session_state.pop(trash_key, None)
                            st.success("Batch dipindahkan ke Tong Sampah. Data masih dapat dipulihkan.")
                            st.rerun()
                    with no:
                        if st.button(
                            "Batal",
                            use_container_width=True,
                            key=f"move_trash_cancel_{selected_batch}",
                        ):
                            st.session_state.pop(trash_key, None)
                            st.rerun()

        # Reset tetap tersedia, tetapi ditempatkan dalam satu bagian ringkas.
        st.divider()
        with st.expander("Reset dokumen batch (opsional)", expanded=False):
            st.caption(
                "Reset hanya untuk mengulang proses Validasi & Merge. "
                "Data Converter tidak ikut diubah."
            )

            if not bool(selected_row["archived_at"]):
                with st.expander("Reset analisis dan hasil merge", expanded=False):
                    st.write(
                        "Menghapus hasil PDF gabungan dan status analisis/persetujuan. "
                        "Dokumen sumber Validasi & Merge dan seluruh data Converter tetap tersimpan."
                    )
                    key = f"manage_reset_analysis_{selected_batch}"
                    if st.button("Minta reset analisis", key=f"req_{key}"):
                        st.session_state[key] = True
                    if st.session_state.get(key):
                        confirm = st.checkbox(
                            "Saya yakin ingin mereset analisis dan hasil merge.",
                            key=f"check_{key}",
                        )
                        if st.button(
                            "Konfirmasi reset analisis",
                            type="primary",
                            disabled=not confirm,
                            key=f"do_{key}",
                        ):
                            deleted = delete_batch_merge_outputs(selected_batch)
                            clear_batch_session_state(selected_batch)
                            st.session_state.pop(key, None)
                            st.success(
                                f"Reset selesai. {deleted} file hasil merge dihapus; "
                                "dokumen sumber tetap ada."
                            )
                            st.rerun()

                with st.expander("Reset satu kategori pemeriksaan", expanded=False):
                    with get_conn() as conn:
                        exam_rows = conn.execute(
                            "SELECT code, label FROM exam_types ORDER BY code"
                        ).fetchall()
                    exam_map = {row["code"]: row["label"] for row in exam_rows}
                    batch_codes = [
                        item.strip()
                        for item in selected_row["exam_codes"].split(",")
                        if item.strip()
                    ]
                    reset_codes = [item for item in batch_codes if item in exam_map]
                    if reset_codes:
                        reset_exam = st.selectbox(
                            "Kategori yang akan di-reset",
                            reset_codes,
                            format_func=lambda code: f"{code} — {exam_map.get(code, code)}",
                            key=f"manage_reset_exam_{selected_batch}",
                        )
                        reset_count = len(list_batch_documents(selected_batch, reset_exam))
                        st.write(
                            f"Dokumen Validasi & Merge pada {reset_exam}: **{reset_count} file**"
                        )
                        key = f"manage_reset_category_{selected_batch}_{reset_exam}"
                        if st.button(
                            f"Minta reset {reset_exam}",
                            disabled=reset_count == 0,
                            key=f"req_{key}",
                        ):
                            st.session_state[key] = True
                        if st.session_state.get(key):
                            st.warning(
                                "Dokumen sumber dan PDF siap proses pada kategori ini akan dihapus. "
                                "Kategori lain dan Converter tetap aman. Hasil merge batch juga dihapus."
                            )
                            confirm = st.checkbox(
                                f"Saya yakin ingin menghapus dokumen kategori {reset_exam}.",
                                key=f"check_{key}",
                            )
                            if st.button(
                                "Konfirmasi reset kategori",
                                type="primary",
                                disabled=not confirm,
                                key=f"do_{key}",
                            ):
                                deleted = reset_one_exam_category(selected_batch, reset_exam)
                                st.session_state.pop(key, None)
                                st.success(
                                    f"Reset {reset_exam} selesai. {deleted} catatan dokumen dihapus."
                                )
                                st.rerun()
                    else:
                        st.info("Batch ini tidak memiliki kategori pemeriksaan yang dapat di-reset.")

                with st.expander("Reset seluruh dokumen Validasi & Merge", expanded=False):
                    st.warning(
                        "Ini menghapus seluruh dokumen sumber Validasi & Merge, PDF siap proses, "
                        "catatan dokumen Validasi, laporan, dan hasil merge. "
                        "Data Converter serta master batch tetap ada."
                    )
                    key = f"manage_reset_all_{selected_batch}"
                    if st.button(
                        "Minta reset seluruh dokumen Validasi & Merge",
                        key=f"req_{key}",
                    ):
                        st.session_state[key] = True
                    if st.session_state.get(key):
                        typed = st.text_input(
                            "Ketik nama batch persis untuk konfirmasi",
                            key=f"type_{key}",
                        )
                        if st.button(
                            "Konfirmasi hapus dokumen Validasi & Merge",
                            type="primary",
                            disabled=(typed != selected_batch),
                            key=f"do_{key}",
                        ):
                            reset_all_batch_documents(selected_batch)
                            st.session_state.pop(key, None)
                            st.success(
                                "Dokumen Validasi & Merge batch berhasil direset. "
                                "Converter tetap terpisah."
                            )
                            st.rerun()
            else:
                st.info(
                    "Batch sedang Nonaktif. Aktifkan kembali batch sebelum menjalankan reset operasional."
                )


# ============================================================
# 15A. NORMALISASI NAMA (FITUR MANDIRI)
# ============================================================

elif page == "Normalisasi Nama":
    st.subheader("Normalisasi Nama File PDF")
    st.caption(
        "Bersihkan nama file PDF secara massal. Fitur ini berdiri sendiri dan "
        "tidak mengubah dokumen, database, antrean, atau hasil Validasi & Merge MCU."
    )

    st.info(
        "Fitur ini hanya membaca nama file, bukan isi PDF. PDF asli disimpan terpisah; "
        "hasil normalisasi adalah salinan PDF dengan nama file baru. "
        "Tidak ada dokumen yang digabungkan."
    )
    st.caption(
        f"Batas upload: maksimal {MAX_FILE_COUNT} file PDF sekaligus, "
        f"maksimal {MAX_FILE_SIZE_MB} MB per file."
    )

    upload_version_key = "name_normalization_upload_version"
    if upload_version_key not in st.session_state:
        st.session_state[upload_version_key] = 0

    uploaded_pdfs = st.file_uploader(
        "Pilih file PDF",
        type=["pdf"],
        accept_multiple_files=True,
        key=f"name_normalization_uploader_{st.session_state[upload_version_key]}",
        help="Pilih maksimal 50 PDF. File sumber Validasi & Merge tidak akan disentuh.",
    )

    analyze_col, reset_col = st.columns([3, 1])
    with analyze_col:
        analyze_clicked = st.button(
            "Analisis nama PDF",
            type="primary",
            use_container_width=True,
            key="name_normalization_analyze",
        )
    with reset_col:
        reset_clicked = st.button(
            "Reset tampilan",
            use_container_width=True,
            key="name_normalization_reset",
        )

    if reset_clicked:
        for state_key in (
            "name_normalization_rows",
            "name_normalization_job_id",
            "name_normalization_zip_path",
            "name_normalization_report_path",
        ):
            st.session_state.pop(state_key, None)
        st.session_state[upload_version_key] += 1
        st.rerun()

    if analyze_clicked:
        if not uploaded_pdfs:
            st.error("Pilih minimal satu file PDF terlebih dahulu.")
        elif len(uploaded_pdfs) > MAX_FILE_COUNT:
            st.error(f"Maksimal {MAX_FILE_COUNT} file dalam satu proses.")
        else:
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
            job_dir = NAME_NORMALIZATION_DIR / f"PROSES_{job_id}"
            source_dir = job_dir / "FILE_ASLI"
            output_dir = job_dir / "HASIL_NORMALISASI"
            report_dir = job_dir / "LAPORAN"
            source_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            report_dir.mkdir(parents=True, exist_ok=True)

            accepted_rows: list[dict[str, Any]] = []
            upload_errors: list[str] = []

            for uploaded in uploaded_pdfs:
                original_name = safe_filename(uploaded.name)
                extension = Path(original_name).suffix.lower()

                if extension != ".pdf":
                    upload_errors.append(f"{uploaded.name}: format bukan PDF.")
                    continue
                if uploaded.size <= 0:
                    upload_errors.append(f"{uploaded.name}: file kosong.")
                    continue
                if uploaded.size > MAX_FILE_SIZE_BYTES:
                    size_mb = uploaded.size / (1024 * 1024)
                    upload_errors.append(
                        f"{uploaded.name}: ukuran {size_mb:.2f} MB melebihi batas "
                        f"{MAX_FILE_SIZE_MB} MB."
                    )
                    continue

                try:
                    file_bytes = uploaded.getvalue()
                    if not file_bytes:
                        upload_errors.append(f"{uploaded.name}: file kosong/tidak terbaca.")
                        continue

                    source_path = get_unique_filepath(source_dir, original_name)
                    atomic_write(source_path, file_bytes)
                    is_valid_pdf, validation_message = validate_pdf(source_path)
                    if not is_valid_pdf:
                        source_path.unlink(missing_ok=True)
                        upload_errors.append(f"{uploaded.name}: {validation_message}")
                        continue

                    suggested_name, cleaning_note = suggest_normalized_pdf_name(original_name)
                    accepted_rows.append(
                        {
                            "Nama file asli": original_name,
                            "Saran nama": suggested_name,
                            "Nama final": suggested_name,
                            "Catatan deteksi": cleaning_note,
                            "source_path": str(source_path),
                        }
                    )
                except Exception as exc:
                    upload_errors.append(f"{uploaded.name}: gagal diproses ({exc}).")

            if upload_errors:
                with st.expander(f"Detail file yang tidak diproses ({len(upload_errors)})", expanded=True):
                    for error in upload_errors:
                        st.warning(error)

            if accepted_rows:
                st.session_state["name_normalization_rows"] = accepted_rows
                st.session_state["name_normalization_job_id"] = job_id
                st.session_state.pop("name_normalization_zip_path", None)
                st.session_state.pop("name_normalization_report_path", None)
                st.success(
                    f"Analisis selesai. {len(accepted_rows)} PDF valid siap diperiksa."
                )
            elif not upload_errors:
                st.error("Tidak ada PDF valid yang dapat diproses.")

    normalization_rows = st.session_state.get("name_normalization_rows", [])
    current_job_id = st.session_state.get("name_normalization_job_id")

    if normalization_rows and current_job_id:
        st.divider()
        st.subheader("Review hasil normalisasi")
        st.write(
            "Periksa kolom **Nama final**. Kamu bisa mengubahnya langsung sebelum "
            "menyiapkan salinan PDF."
        )

        editor_rows = [
            {
                "Nama file asli": row["Nama file asli"],
                "Saran nama": row["Saran nama"],
                "Nama final": row["Nama final"],
                "Catatan deteksi": row["Catatan deteksi"],
            }
            for row in normalization_rows
        ]
        edited_table = st.data_editor(
            editor_rows,
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            disabled=["Nama file asli", "Saran nama", "Catatan deteksi"],
            column_config={
                "Nama file asli": st.column_config.TextColumn("Nama file asli", width="large"),
                "Saran nama": st.column_config.TextColumn("Saran sistem", width="large"),
                "Nama final": st.column_config.TextColumn("Nama final (boleh diedit)", width="large"),
                "Catatan deteksi": st.column_config.TextColumn("Catatan", width="medium"),
            },
            key=f"name_normalization_editor_{current_job_id}",
        )

        if hasattr(edited_table, "to_dict"):
            reviewed_rows = edited_table.to_dict("records")
        else:
            reviewed_rows = list(edited_table)

        final_names = [str(row.get("Nama final", "")).strip() for row in reviewed_rows]
        blank_names = [index + 1 for index, value in enumerate(final_names) if not value]
        if blank_names:
            st.error(
                "Nama final tidak boleh kosong. Periksa baris: "
                + ", ".join(str(number) for number in blank_names)
            )

        # Kelompok nama identik dan nama yang mirip hanya berupa saran pemeriksaan.
        name_counts: dict[str, int] = {}
        for value in final_names:
            normalized_value = re.sub(r"\s+", " ", value).strip().upper()
            if normalized_value:
                name_counts[normalized_value] = name_counts.get(normalized_value, 0) + 1
        repeated_names = {name: count for name, count in name_counts.items() if count > 1}
        if repeated_names:
            st.warning(
                "Ada beberapa file dengan nama final yang sama. File tetap akan "
                "disimpan terpisah; sistem memberi nomor pada nama file output agar "
                "tidak saling menimpa. Kesamaan nama bukan bukti identitas orang yang sama."
            )
            st.dataframe(
                [
                    {"Nama final": name, "Jumlah file": count}
                    for name, count in repeated_names.items()
                ],
                use_container_width=True,
                hide_index=True,
            )

        similar_pairs = find_normalization_similar_names(final_names)
        if similar_pairs:
            with st.expander("Saran pemeriksaan nama yang mirip", expanded=False):
                st.caption(
                    "Kemiripan hanya ditampilkan sebagai petunjuk. Fitur ini tidak "
                    "menggabungkan pasien atau isi PDF."
                )
                st.dataframe(
                    [
                        {
                            "Nama pertama": name_a,
                            "Nama kedua": name_b,
                            "Kemiripan (%)": score,
                        }
                        for name_a, name_b, score in similar_pairs
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

        if any("DWI" in str(row.get("Nama final", "")).upper() for row in reviewed_rows):
            st.caption(
                "Catatan: kata seperti DWI dipertahankan karena bisa merupakan bagian "
                "nama seseorang. Hapus atau ubah kata tersebut sendiri pada kolom Nama final "
                "bila sudah dipastikan bukan bagian nama."
            )

        st.warning(
            "Sebelum menyimpan, pastikan nama final sudah diperiksa. Proses ini hanya "
            "mengganti nama salinan file PDF; tidak menggabungkan dokumen dan tidak "
            "mengubah file sumber."
        )
        name_review_confirmed = st.checkbox(
            "Saya sudah memeriksa seluruh nama final dan menyetujui pembuatan salinan PDF.",
            key=f"name_normalization_confirm_{current_job_id}",
        )

        save_clicked = st.button(
            "Simpan hasil normalisasi & buat ZIP",
            type="primary",
            use_container_width=True,
            disabled=(not name_review_confirmed or bool(blank_names)),
            key=f"name_normalization_save_{current_job_id}",
        )

        if save_clicked:
            job_dir = NAME_NORMALIZATION_DIR / f"PROSES_{current_job_id}"
            output_dir = job_dir / "HASIL_NORMALISASI"
            report_dir = job_dir / "LAPORAN"
            output_dir.mkdir(parents=True, exist_ok=True)
            report_dir.mkdir(parents=True, exist_ok=True)

            # Bersihkan hanya output dari proses normalisasi aktif ini, bukan file asli.
            for old_output in output_dir.iterdir():
                if old_output.is_file():
                    old_output.unlink()
            for old_archive in job_dir.glob("*.zip"):
                if old_archive.is_file():
                    old_archive.unlink()

            manifest_rows: list[dict[str, Any]] = []
            output_paths: list[Path] = []
            output_errors: list[str] = []

            for source_row, edited_row in zip(normalization_rows, reviewed_rows):
                final_name = str(edited_row.get("Nama final", "")).strip()
                clean_stem = safe_normalization_stem(final_name)
                source_path = Path(source_row["source_path"])
                if not source_path.is_file():
                    output_errors.append(
                        f"{source_row['Nama file asli']}: file sumber pada penyimpanan sementara tidak ditemukan."
                    )
                    continue

                try:
                    output_path = get_unique_filepath(output_dir, f"{clean_stem}.pdf")
                    shutil.copy2(source_path, output_path)
                    if sha256_file(source_path) != sha256_file(output_path):
                        output_path.unlink(missing_ok=True)
                        raise RuntimeError("Verifikasi salinan gagal; isi PDF tidak identik.")

                    output_paths.append(output_path)
                    manifest_rows.append(
                        {
                            "Nama file asli": source_row["Nama file asli"],
                            "Saran nama sistem": source_row["Saran nama"],
                            "Nama final": final_name,
                            "Nama file output": output_path.name,
                        }
                    )
                except Exception as exc:
                    output_errors.append(f"{source_row['Nama file asli']}: {exc}")

            if output_errors:
                with st.expander(f"File yang gagal disiapkan ({len(output_errors)})", expanded=True):
                    for error in output_errors:
                        st.error(error)

            if output_paths:
                report_path = report_dir / "laporan_normalisasi.csv"
                with report_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
                    fieldnames = [
                        "Nama file asli",
                        "Saran nama sistem",
                        "Nama final",
                        "Nama file output",
                    ]
                    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(manifest_rows)

                zip_path = job_dir / "HASIL_NORMALISASI.zip"
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for output_path in output_paths:
                        archive.write(output_path, arcname=output_path.name)
                    archive.write(report_path, arcname="laporan_normalisasi.csv")

                st.session_state["name_normalization_zip_path"] = str(zip_path)
                st.session_state["name_normalization_report_path"] = str(report_path)
                st.success(
                    f"Selesai. {len(output_paths)} salinan PDF berhasil dibuat. "
                    "PDF asli tetap tersimpan tanpa perubahan."
                )
            else:
                st.error("Tidak ada hasil PDF yang berhasil dibuat.")

        saved_zip_value = st.session_state.get("name_normalization_zip_path")
        saved_report_value = st.session_state.get("name_normalization_report_path")
        if saved_zip_value and Path(saved_zip_value).is_file():
            st.divider()
            st.subheader("Download hasil")
            zip_path = Path(saved_zip_value)
            st.download_button(
                "Download seluruh PDF hasil normalisasi (ZIP)",
                data=zip_path.read_bytes(),
                file_name=zip_path.name,
                mime="application/zip",
                type="primary",
                use_container_width=True,
                key=f"download_normalized_zip_{current_job_id}",
            )
            if saved_report_value and Path(saved_report_value).is_file():
                report_path = Path(saved_report_value)
                st.download_button(
                    "Download laporan perubahan nama (CSV)",
                    data=report_path.read_bytes(),
                    file_name=report_path.name,
                    mime="text/csv",
                    use_container_width=True,
                    key=f"download_normalized_report_{current_job_id}",
                )
            st.caption(
                f"Folder proses tersimpan di: {NAME_NORMALIZATION_DIR / f'PROSES_{current_job_id}'}"
            )


# ============================================================
# 17. TONG SAMPAH
# ============================================================

elif page == "Tong Sampah":
    st.subheader("Tong Sampah Batch MCU")
    st.caption("Batch yang dipindahkan ke sini belum dihapus permanen dan dapat dipulihkan.")

    with get_conn() as conn:
        trash_rows = conn.execute(
            "SELECT * FROM batches WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
        ).fetchall()

    if not trash_rows:
        st.success("Tong Sampah kosong.")
    else:
        st.dataframe(
            [
                {
                    "Perusahaan": row["company"],
                    "Nama batch": row["batch_name"],
                    "Status proses": row["status"],
                    "Tanggal dipindahkan": row["deleted_at"],
                    "Dokumen Validasi": len(list_batch_documents(row["batch_name"])),
                    "Dokumen Converter": len(list_conversion_documents(row["batch_name"])),
                }
                for row in trash_rows
            ],
            use_container_width=True,
            hide_index=True,
        )

        trash_map = {row["batch_name"]: row for row in trash_rows}
        selected_trash = st.selectbox(
            "Pilih batch di Tong Sampah",
            options=list(trash_map.keys()),
            format_func=lambda name: f"{trash_map[name]['company']} — {name}",
            key="trash_batch_select",
        )
        st.info("Pulihkan akan mengembalikan batch ke daftar aktif. Hapus permanen akan menghapus seluruh dokumen Converter, Validasi & Merge, output, laporan, dan catatan database.")

        restore_col, delete_col = st.columns(2)
        with restore_col:
            if st.button("Pulihkan Batch", type="primary", use_container_width=True, key=f"restore_trash_{selected_trash}"):
                restore_batch_from_trash(selected_trash)
                st.success("Batch dipulihkan ke daftar batch aktif.")
                st.rerun()
        with delete_col:
            if st.button("Hapus Permanen", use_container_width=True, key=f"request_permanent_delete_{selected_trash}"):
                st.session_state[f"confirm_permanent_delete_{selected_trash}"] = True

        permanent_key = f"confirm_permanent_delete_{selected_trash}"
        if st.session_state.get(permanent_key):
            st.error("Tindakan ini tidak dapat dibatalkan. Semua data batch, termasuk file sumber dan hasil Converter, akan dihapus permanen.")
            checkbox = st.checkbox("Saya memahami penghapusan ini permanen dan sudah memastikan backup tersedia.", key=f"check_permanent_delete_{selected_trash}")
            typed = st.text_input("Ketik nama batch persis", key=f"type_permanent_delete_{selected_trash}")
            yes, no = st.columns(2)
            with yes:
                if st.button("Ya, hapus permanen", type="primary", disabled=(not checkbox or typed != selected_trash), use_container_width=True, key=f"do_permanent_delete_{selected_trash}"):
                    result = permanently_delete_batch(selected_trash)
                    st.session_state.pop(permanent_key, None)
                    st.success(
                        f"Batch dihapus permanen. {result['files']} file dan "
                        f"{result['validation_records'] + result['conversion_records']} catatan dokumen dihapus."
                    )
                    st.rerun()
            with no:
                if st.button("Batal", use_container_width=True, key=f"cancel_permanent_delete_{selected_trash}"):
                    st.session_state.pop(permanent_key, None)
                    st.rerun()


# ============================================================
# 17. PANDUAN
# ============================================================

elif page == "Panduan":
    st.subheader("Alur kerja aplikasi")
    st.markdown(
        """
1. **Buat Batch MCU:** buat ruang kerja untuk perusahaan dan periode MCU.
2. **Kelola Batch MCU:** ubah status batch Aktif/Nonaktif, pindahkan batch ke Tong Sampah, dan akses reset dokumen melalui pengaturan lanjutan.
3. **Paket Pemeriksaan:** periksa daftar pemeriksaan atau tambahkan kategori baru.
4. **Convert Word, JPG to PDF:** pilih batch dan kategori, lalu unggah PDF, gambar, DOC, atau DOCX.
5. **Penyimpanan Converter:** file sumber dan hasil PDF converter berada terpisah di `HASIL_KONVERSI/<batch>/<kategori>/`. Hasil converter hanya untuk diunduh dan tidak otomatis masuk ke menu Validasi & Merge.
6. **Validasi & Merge MCU:** unggah PDF yang sudah disiapkan melalui bagian Upload PDF Langsung, lalu analisis, periksa nama kandidat, koreksi nama jika perlu, dan setujui identitas pasien secara manual.
7. **Merge:** atur urutan pemeriksaan dan gabungkan hanya pasien yang sudah disetujui.

**Aturan anti-duplikasi:** sistem memeriksa SHA-256 pada seluruh kategori dalam satu batch. Konten yang sama tidak akan didaftarkan dua kali meskipun nama file atau kategori upload berbeda. Petunjuk kategori yang eksplisit pada nama/isi PDF juga diperiksa; dokumen yang terindikasi salah kategori tidak ikut dianalisis atau di-merge. File sumber tidak dihapus oleh proses analisis maupun merge.

**Identifikasi nama pasien:** pengelompokan awal menggunakan nama filename yang dinormalisasi. Text layer PDF dibaca terlebih dahulu; ketika nama tidak ditemukan atau text layer tidak memadai, Tesseract dijalankan sebagai OCR fallback. Pencocokan nama memakai beberapa algoritma RapidFuzz (ratio, partial ratio, token sort, token set, WRatio, dan QRatio). NIK, nomor rekam medis, serta tanggal lahir menjadi bukti pendukung. Data NIK dan nomor RM tidak ditampilkan utuh; sistem menyimpan hash untuk pencocokan dan menampilkan empat karakter terakhir saja. Nama yang mirip tidak digabung otomatis.

**OCR Tesseract:** pasang binary Tesseract OCR dan paket bahasa `ind` serta `eng`, lalu pasang Python wrapper dengan `pip install pytesseract`. Jika binary tidak berada di PATH, set environment variable `TESSERACT_CMD` ke lokasi `tesseract.exe`. OCR dibatasi ke maksimal halaman awal sesuai `MCU_OCR_MAX_PAGES` dan hasilnya di-cache secara lokal di SQLite berdasarkan hash PDF, halaman, bahasa, dan versi engine.

**Batasan:** OCR dapat salah membaca karakter, angka, nama, atau tanggal pada scan buram, miring, bertanda tangan/stempel, atau tabel padat. Confidence OCR bukan jaminan kebenaran. Jika satu kelompok memuat lebih dari satu NIK/No. RM/tanggal lahir, persetujuan dinonaktifkan dan petugas dapat memisahkan dokumen menjadi kandidat baru. Semua kandidat tetap harus diperiksa dan disetujui petugas sebelum merge.

**Tong Sampah:** batch yang dipindahkan ke Tong Sampah dapat dipulihkan. Penghapusan permanen memerlukan konfirmasi dengan mengetik nama batch dan memastikan backup tersedia.

**Keamanan:** gunakan data dummy selama pengembangan. Sebelum penggunaan operasional rumah sakit, tambahkan akun individual dan role, audit log, backup terenkripsi, pembatasan akses LAN, serta persetujuan keamanan informasi rumah sakit. Jangan unggah dokumen pasien atau password ke GitHub.
        """
    )


