from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps, ImageSequence


def validate_pdf(filepath: Path) -> tuple[bool, str]:
    try:
        with fitz.open(filepath) as document:
            if document.page_count < 1:
                return False, "PDF tidak memiliki halaman."
        return True, "OK"
    except Exception as exc:
        return False, f"PDF tidak dapat dibaca: {exc}"


def convert_image_to_pdf(
    image_path: Path,
    output_pdf_path: Path,
) -> tuple[bool, str]:
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
    for executable in (
        "libreoffice",
        "soffice",
        "libreoffice.exe",
        "soffice.exe",
    ):
        found = shutil.which(executable)
        if found:
            return found
    return None


def convert_word_to_pdf(
    word_path: Path,
    output_pdf_path: Path,
) -> tuple[bool, str]:
    """Konversi DOC/DOCX menggunakan LibreOffice headless."""
    executable = find_libreoffice_binary()

    if not executable:
        return (
            False,
            "LibreOffice tidak ditemukan. Instal libreoffice-writer "
            "di Ubuntu/WSL atau pasang LibreOffice di Windows server.",
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
                details = (
                    result.stderr
                    or result.stdout
                    or "Tidak ada detail dari LibreOffice"
                ).strip()
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


def convert_source_to_pdf(
    source_path: Path,
    pdf_path: Path,
) -> tuple[bool, str]:
    """Pilih metode konversi berdasarkan ekstensi sumber."""
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
