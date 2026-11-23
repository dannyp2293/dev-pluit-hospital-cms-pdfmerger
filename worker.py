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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MCU PDF Merger Python CLI Worker"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Periksa kesiapan Python worker")

    args = parser.parse_args()

    if args.command == "health":
        result, exit_code = health()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
