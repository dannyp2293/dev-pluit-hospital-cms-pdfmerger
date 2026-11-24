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

    args = parser.parse_args()

    if args.command == "health":
        result, exit_code = health()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    if args.command == "extract":
        result, exit_code = extract_pdf(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
