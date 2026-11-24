from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys

from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent

REQUIRED_PACKAGES = {
    "streamlit": "streamlit",
    "PyMuPDF": "fitz",
    "rapidfuzz": "rapidfuzz",
    "Pillow": "PIL",
    "pandas": "pandas",
    "pytesseract": "pytesseract",
}


def check_packages() -> dict:
    results = {}

    for package, module in REQUIRED_PACKAGES.items():
        try:
            installed_version = version(package)
            module_available = find_spec(module) is not None

            results[package] = {
                "installed": True,
                "module_available": module_available,
                "version": installed_version,
            }

        except (PackageNotFoundError, ValueError):
            results[package] = {
                "installed": False,
                "module_available": False,
                "version": None,
            }

    return results


def run_command(command: str, arguments: list[str]) -> dict:
    executable = shutil.which(command)

    if not executable:
        return {
            "available": False,
            "executable": None,
            "version": None,
        }

    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        output = result.stdout.strip() or result.stderr.strip()
        first_line = output.splitlines()[0] if output else None

        return {
            "available": result.returncode == 0,
            "executable": executable,
            "version": first_line,
        }

    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "available": False,
            "executable": executable,
            "version": None,
            "error": str(exc),
        }


def check_tesseract() -> dict:
    info = run_command("tesseract", ["--version"])

    if not info["available"]:
        return info

    language_result = run_command("tesseract", ["--list-langs"])

    languages = []

    if language_result["available"]:
        output = subprocess.run(
            [info["executable"], "--list-langs"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        if output.returncode == 0:
            languages = output.stdout.strip().splitlines()[1:]

    languages = [language.strip() for language in languages if language.strip()]

    required = ["eng", "ind"]
    missing = [language for language in required if language not in languages]

    info["languages"] = languages
    info["required_languages"] = required
    info["missing_languages"] = missing
    info["ok"] = len(missing) == 0

    return info


def health() -> tuple[dict, int]:
    packages = check_packages()
    tesseract = check_tesseract()

    libreoffice = run_command("libreoffice", ["--version"])

    packages_ok = all(
        item["installed"] and item["module_available"]
        for item in packages.values()
    )

    overall_ok = (
        packages_ok
        and tesseract.get("ok", False)
        and libreoffice["available"]
    )

    result = {
        "ok": overall_ok,
        "command": "health",
        "project_dir": str(PROJECT_DIR),
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "packages": packages,
        "system": {
            "tesseract": tesseract,
            "libreoffice": libreoffice,
        },
    }

    return result, 0 if overall_ok else 1



def extract_pdf(args: argparse.Namespace) -> tuple[dict, int]:
    """Ekstrak teks, identitas, dan OCR dari satu file PDF."""
    filepath = args.file.expanduser().resolve()

    if not filepath.is_file():
        return {
            "ok": False,
            "command": "extract",
            "error": "File PDF tidak ditemukan.",
        }, 2

    if filepath.suffix.lower() != ".pdf":
        return {
            "ok": False,
            "command": "extract",
            "error": "Format file harus PDF.",
        }, 2

    try:
        from processor.database import init_db
        from processor.identity import extract_name_from_filename
        from processor.ocr import extract_hybrid_text_from_pdf
        from processor.utils import sha256_file

        init_db()

        file_hash = sha256_file(filepath)
        filename_candidate = extract_name_from_filename(
            filepath.name,
            args.exam_code,
        )

        extraction = extract_hybrid_text_from_pdf(
            filepath,
            file_hash,
            enable_ocr=not args.no_ocr,
            filename_candidate=filename_candidate,
        )

        return {
            "ok": True,
            "command": "extract",
            "file_name": filepath.name,
            "file_hash": file_hash,
            "filename_candidate": filename_candidate,
            "extraction": extraction,
        }, 0

    except Exception as exc:
        return {
            "ok": False,
            "command": "extract",
            "error": str(exc),
        }, 1




def analyze_batch(args: argparse.Namespace) -> tuple[dict, int]:
    """Analisis seluruh dokumen dalam satu batch."""
    batch_name = args.batch_name.strip()

    if not batch_name:
        return {
            "ok": False,
            "command": "analyze",
            "error": "Nama batch tidak boleh kosong.",
        }, 2

    try:
        from processor.database import init_db
        from processor.analyzer import DATA_DIR, analyze_batch_documents

        data_root = DATA_DIR.resolve()
        batch_dir = (data_root / batch_name).resolve()

        # Cegah batch_name mengarah keluar dari DATA_DIR.
        if batch_dir == data_root or data_root not in batch_dir.parents:
            return {
                "ok": False,
                "command": "analyze",
                "error": "Lokasi batch tidak valid.",
            }, 2

        if not batch_dir.is_dir():
            return {
                "ok": False,
                "command": "analyze",
                "batch_name": batch_name,
                "error": "Direktori batch tidak ditemukan.",
            }, 2

        init_db()

        patients = analyze_batch_documents(
            batch_name,
            enable_ocr=not args.no_ocr,
        )

        document_count = sum(
            len(files) for files in patients.values()
        )

        return {
            "ok": True,
            "command": "analyze",
            "batch_name": batch_name,
            "patient_count": len(patients),
            "document_count": document_count,
            "patients": patients,
        }, 0

    except Exception as exc:
        return {
            "ok": False,
            "command": "analyze",
            "batch_name": batch_name,
            "error": str(exc),
        }, 1



def merge_approved_batch(args: argparse.Namespace) -> tuple[dict, int]:
    """Merge hanya pasien yang telah disetujui melalui manifest Laravel."""
    manifest_path = args.manifest.expanduser().resolve()

    if not manifest_path.is_file():
        return {
            "ok": False,
            "command": "merge",
            "error": "Manifest tidak ditemukan.",
        }, 2

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        if not isinstance(manifest, dict):
            raise ValueError("Format manifest harus berupa object JSON.")

        batch_name = manifest.get("batch_name")
        exam_order = manifest.get("exam_order")
        patients = manifest.get("patients")

        if not isinstance(batch_name, str) or not batch_name.strip():
            raise ValueError("batch_name tidak valid.")

        batch_name = batch_name.strip()

        if (
            batch_name in {".", ".."}
            or "/" in batch_name
            or "\\" in batch_name
        ):
            raise ValueError("Nama batch tidak valid.")

        if (
            not isinstance(exam_order, list)
            or not exam_order
            or any(not isinstance(code, str) or not code.strip()
                   for code in exam_order)
            or len(set(exam_order)) != len(exam_order)
        ):
            raise ValueError("exam_order tidak valid atau memiliki duplikasi.")

        if not isinstance(patients, list) or not patients:
            raise ValueError("Tidak ada pasien yang disetujui dalam manifest.")

        from processor.analyzer import DATA_DIR
        from processor.database import DEFAULT_EXAMS
        from processor.identity import normalize_patient_identity_name
        from processor.merger import merge_patient_documents
        from processor.utils import sha256_file

        valid_exam_codes = {code for code, _ in DEFAULT_EXAMS}

        if not set(exam_order).issubset(valid_exam_codes):
            raise ValueError("Manifest berisi kategori pemeriksaan tidak dikenal.")

        data_root = DATA_DIR.resolve()
        batch_dir = (data_root / batch_name).resolve()

        if batch_dir == data_root or data_root not in batch_dir.parents:
            raise ValueError("Lokasi batch berada di luar DATA_DIR.")

        if not batch_dir.is_dir():
            raise ValueError("Direktori batch tidak ditemukan.")

        output_root = args.output_dir.expanduser().resolve()
        output_batch_dir = (output_root / batch_name).resolve()

        if (
            output_batch_dir == output_root
            or output_root not in output_batch_dir.parents
        ):
            raise ValueError("Lokasi output batch tidak valid.")

        # Validasi seluruh manifest sebelum file apa pun digabungkan.
        validated_patients = []

        for patient in patients:
            if not isinstance(patient, dict):
                raise ValueError("Data pasien dalam manifest tidak valid.")

            if patient.get("approved") is not True:
                raise ValueError(
                    "Manifest hanya boleh berisi pasien yang telah disetujui."
                )

            final_name = patient.get("final_name")

            if not isinstance(final_name, str) or not final_name.strip():
                raise ValueError("Nama final pasien tidak valid.")

            files = patient.get("files")

            if not isinstance(files, list) or not files:
                raise ValueError(
                    f"Tidak ada dokumen untuk pasien {final_name}."
                )

            validated_files = []

            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("Data dokumen dalam manifest tidak valid.")

                filepath = Path(str(item.get("filepath", ""))).expanduser().resolve()
                exam_code = item.get("exam_code")

                if exam_code not in exam_order:
                    raise ValueError(
                        f"Kategori dokumen tidak sesuai exam_order: {exam_code}"
                    )

                try:
                    filepath.relative_to(batch_dir)
                except ValueError:
                    raise ValueError(
                        f"File berada di luar direktori batch: {filepath}"
                    )

                if not filepath.is_file() or filepath.suffix.lower() != ".pdf":
                    raise ValueError(f"File PDF tidak valid: {filepath}")

                actual_hash = sha256_file(filepath)
                expected_hash = item.get("file_hash")

                if (
                    not isinstance(expected_hash, str)
                    or len(expected_hash) != 64
                    or any(
                        character not in "0123456789abcdefABCDEF"
                        for character in expected_hash
                    )
                ):
                    raise ValueError(
                        f"Hash dokumen tidak valid atau tidak tersedia: {filepath.name}"
                    )

                if expected_hash.lower() != actual_hash:
                    raise ValueError(
                        f"Hash dokumen berubah sejak analisis: {filepath.name}"
                    )

                validated_files.append({
                    "filepath": str(filepath),
                    "exam_code": exam_code,
                    "file_hash": actual_hash,
                })

            validated_patients.append({
                "final_name": final_name.strip(),
                "files": validated_files,
            })

    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "command": "merge",
            "error": str(exc),
        }, 2

    try:
        name_totals = {}
        for patient in validated_patients:
            key = (
                normalize_patient_identity_name(patient["final_name"])
                or patient["final_name"].casefold()
            )
            name_totals[key] = name_totals.get(key, 0) + 1

        name_seen = {}
        merged_files = []
        failures = []

        for patient in validated_patients:
            final_name = patient["final_name"]
            name_key = (
                normalize_patient_identity_name(final_name)
                or final_name.casefold()
            )

            name_seen[name_key] = name_seen.get(name_key, 0) + 1

            output_name = final_name
            if name_totals[name_key] > 1:
                output_name = f"{final_name} (KANDIDAT {name_seen[name_key]})"

            success, message = merge_patient_documents(
                output_name,
                patient["files"],
                exam_order,
                output_batch_dir,
            )

            if success:
                merged_files.append({
                    "patient_name": final_name,
                    "output_name": output_name,
                    "filepath": message,
                })
            else:
                failures.append({
                    "patient_name": final_name,
                    "error": message,
                })

        result = {
            "ok": bool(merged_files) and not failures,
            "command": "merge",
            "batch_name": batch_name,
            "exam_order": exam_order,
            "approved_patient_count": len(validated_patients),
            "merged_patient_count": len(merged_files),
            "merged_files": merged_files,
            "failures": failures,
        }

        return result, 0 if result["ok"] else 1

    except Exception as exc:
        return {
            "ok": False,
            "command": "merge",
            "batch_name": batch_name,
            "error": str(exc),
        }, 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MCU PDF Merger Python CLI Worker"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Periksa kesiapan Python worker")

    extract_parser = subparsers.add_parser(
        "extract",
        help="Ekstrak teks dan identitas dari PDF",
    )
    extract_parser.add_argument("--file", required=True, type=Path)
    extract_parser.add_argument("--exam-code", default="")
    extract_parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Nonaktifkan OCR Tesseract",
    )

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Analisis dan kelompokkan dokumen dalam satu batch",
    )
    analyze_parser.add_argument("--batch-name", required=True)
    analyze_parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Nonaktifkan OCR Tesseract",
    )

    merge_parser = subparsers.add_parser(
        "merge",
        help="Gabungkan PDF pasien yang telah disetujui",
    )
    merge_parser.add_argument("--manifest", required=True, type=Path)
    merge_parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_DIR / "HASIL_PDF",
    )

    args = parser.parse_args()

    if args.command == "health":
        result, exit_code = health()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    if args.command == "extract":
        result, exit_code = extract_pdf(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    if args.command == "analyze":
        result, exit_code = analyze_batch(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    if args.command == "merge":
        result, exit_code = merge_approved_batch(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
