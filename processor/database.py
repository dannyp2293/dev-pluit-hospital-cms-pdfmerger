from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_DIR = PROJECT_DIR / "DATABASE"

DB_PATH = Path(
    os.getenv("MCU_DB_PATH", str(DB_DIR / "mcu_merger.db"))
).expanduser().resolve()

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_EXAMS = [
    ("LPK", "Laporan Pemeriksaan Kesehatan"),
    ("LAB", "Laboratorium"),
    ("RONTGEN", "Rontgen + film"),
    ("EKG", "Elektrokardiografi"),
    ("VISUS", "Visus"),
    ("AUDIO", "Audiometri"),
    ("SPIRO", "Spirometri"),
]


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
