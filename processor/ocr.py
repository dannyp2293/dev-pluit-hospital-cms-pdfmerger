from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageFilter, ImageOps
from rapidfuzz import fuzz

try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    PYTESSERACT_IMPORT_ERROR: str | None = None
except Exception as _ocr_import_exc:
    pytesseract = None
    TesseractOutput = None
    PYTESSERACT_IMPORT_ERROR = str(_ocr_import_exc)

from .database import get_ocr_cache, save_ocr_cache
from .identity import (
    advanced_name_similarity,
    extract_identity_fields,
    find_name_in_pdf_text,
    normalize_patient_identity_name,
)

OCR_ENABLED_DEFAULT = os.getenv("MCU_OCR_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off"
}


OCR_LANGUAGE = os.getenv("MCU_OCR_LANG", "ind+eng").strip() or "ind+eng"


OCR_DPI = max(150, min(400, int(os.getenv("MCU_OCR_DPI", "300"))))


OCR_MAX_PAGES = max(1, min(10, int(os.getenv("MCU_OCR_MAX_PAGES", "3"))))


OCR_MIN_TEXT_CHARS = max(10, int(os.getenv("MCU_OCR_MIN_TEXT_CHARS", "40")))


OCR_NAME_MIN_CONFIDENCE = max(0.0, min(100.0, float(os.getenv("MCU_OCR_NAME_MIN_CONFIDENCE", "65"))))


TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()


def extract_text_from_pdf(filepath: Path) -> str:
    """Ekstraksi text layer PDF tanpa OCR."""
    pieces: list[str] = []
    try:
        with fitz.open(filepath) as document:
            for page in document:
                pieces.append(page.get_text("text"))
    except Exception:
        return ""
    return "\n".join(pieces)


def _name_from_positioned_words(
    words: list[tuple], page_width: float, left_column_only: bool = False,
) -> str:
    """Ambil nama hanya dari kolom kiri IDENTITAS PASIEN pada tabel dua kolom.

    Setiap word: (x0, y0, x1, y1, text, block_id, line_id).
    Posisi DOKTER PENGIRIM dipakai sebagai batas kanan agar nama dokter tidak
    pernah ikut menjadi kandidat nama pasien.
    """
    normalized_words = []
    for raw in words:
        try:
            x0, y0, x1, y1, token = float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]), str(raw[4])
        except (TypeError, ValueError, IndexError):
            continue
        token_norm = re.sub(r"[^A-Z0-9]", "", normalize_patient_name(token))
        if token_norm:
            normalized_words.append((x0, y0, x1, y1, token, token_norm))
    if not normalized_words:
        return ""

    # Kelompokkan kata berdasarkan baris visual (toleran terhadap beda baseline kecil).
    sorted_words = sorted(normalized_words, key=lambda item: (item[1], item[0]))
    rows: list[list[tuple]] = []
    for word in sorted_words:
        if not rows:
            rows.append([word])
            continue
        row_y = sum(item[1] for item in rows[-1]) / len(rows[-1])
        if abs(word[1] - row_y) <= 8:
            rows[-1].append(word)
        else:
            rows.append([word])

    def token_matches(token: str, target: str, cutoff: float = 65.0) -> bool:
        return token == target or fuzz.ratio(token, target) >= cutoff

    patient_header = None
    for row in rows:
        tokens = [item[5] for item in row]
        has_identity = any(token_matches(token, "IDENTITAS", 68.0) for token in tokens)
        has_patient = any(token_matches(token, "PASIEN", 60.0) for token in tokens)
        if has_identity and has_patient:
            patient_header = row
            break
    if patient_header is None:
        return ""

    header_y = min(item[1] for item in patient_header)
    patient_x = min(item[0] for item in patient_header)
    doctor_x = None
    for row in rows:
        row_y = sum(item[1] for item in row) / len(row)
        tokens = {item[5] for item in row}
        has_doctor = any(token_matches(token, "DOKTER", 68.0) for token in tokens)
        has_sender = any(token_matches(token, "PENGIRIM", 65.0) for token in tokens)
        if abs(row_y - header_y) <= 24 and has_doctor and has_sender:
            doctor_x = min(item[0] for item in row if token_matches(item[5], "DOKTER", 68.0))
            break

    # Kolom kiri adalah area pasien. Fallback 52% hanya dipakai bila judul
    # kolom kanan tidak terbaca; ini tetap membatasi pembacaan ke paruh kiri.
    right_boundary = (
        (doctor_x - 4) if doctor_x is not None
        else (page_width * 0.98 if left_column_only else page_width * 0.52)
    )
    if right_boundary <= patient_x:
        right_boundary = page_width * 0.52

    stop_labels = {
        "MRN", "NO", "NOREG", "NOREGISTRASI", "NOTRANSAKSI", "TRANSAKSI",
        "NIK", "TANGGALLAHIR", "TGLLAHIR", "DOB", "JENISKELAMIN", "KELAMIN",
        "UMUR", "USIA", "ALAMAT", "POLIKLINIK", "DOKTER", "PEMERIKSAAN",
        "TANGGAL", "TGL", "JAM", "HASIL", "NAMA", "NAME", "PASIEN",
    }

    def clean_name_tokens(tokens: list[str]) -> str:
        # Buang punctuation dan berhenti di label field berikutnya.
        useful: list[str] = []
        for raw_token in tokens:
            token_norm = re.sub(r"[^A-Z0-9]", "", normalize_patient_name(raw_token))
            if not token_norm or token_norm in {"-", ":", "#"}:
                continue
            if token_norm in stop_labels and useful:
                break
            if token_norm in {"NAMA", "NAME", "PASIEN"}:
                continue
            useful.append(raw_token)
            if len(useful) >= 8:
                break
        candidate = normalize_patient_identity_name(" ".join(useful))
        if not candidate or re.search(r"\d", candidate):
            return ""
        invalid = {"DOKTER", "PENGIRIM", "MRN", "NIK", "TANGGAL", "LAHIR", "ALAMAT", "PASIEN", "NAMA", "NAME"}
        if set(candidate.split()) & invalid or len(candidate) < 3 or len(candidate.split()) > 8:
            return ""
        return candidate

    relevant_rows = [
        row for row in rows
        if header_y + 8 < sum(item[1] for item in row) / len(row) <= header_y + 280
    ]
    for row_index, row in enumerate(relevant_rows):
        left_words = [item for item in sorted(row, key=lambda item: item[0])
                      if patient_x - 18 <= item[0] < right_boundary]
        if not left_words:
            continue
        for index, item in enumerate(left_words):
            label_match = (
                item[5] in {"NAMA", "NAMAPASIEN", "NAMALENGKAP", "NAME", "PATIENTNAME"}
                or token_matches(item[5], "NAMA", 68.0)
                or token_matches(item[5], "NAME", 72.0)
            )
            if not label_match:
                continue
            values = [part[4] for part in left_words[index + 1:]]
            candidate = clean_name_tokens(values)
            if candidate:
                return candidate
            # Beberapa PDF memisahkan label Nama dan nilainya ke baris berikutnya.
            if row_index + 1 < len(relevant_rows):
                next_row = [part for part in sorted(relevant_rows[row_index + 1], key=lambda part: part[0])
                            if patient_x - 18 <= part[0] < right_boundary]
                candidate = clean_name_tokens([part[4] for part in next_row])
                if candidate:
                    return candidate
    return ""


def _generic_name_from_positioned_words(words: list[tuple], page_width: float) -> str:
    """Deteksi nama tanpa mengandalkan label/vendor tertentu.

    Fallback ini dipakai setelah parser label/layout terstruktur gagal. Nama
    harus berupa baris yang masuk akal sebagai nama orang dan didukung konteks
    visual di sekitarnya (mis. tanggal, umur, jenis kelamin, atau data formulir).
    MRN/REG/No. RM bukan syarat: petunjuk konteks boleh berupa informasi lain.
    Nama dokter, petugas, institusi, dan istilah pemeriksaan dikecualikan.
    """
    normalized = []
    heights = []
    for raw in words:
        try:
            x0, y0, x1, y1 = map(float, raw[:4])
            raw_text = str(raw[4]).strip()
        except (TypeError, ValueError, IndexError):
            continue
        token = re.sub(r"[^A-Z0-9]", "", normalize_patient_name(raw_text))
        if not token:
            continue
        line_identity = None
        if len(raw) >= 7:
            candidate_line_identity = (str(raw[5]), str(raw[6]))
            # Some OCR adapters/tests use (0, 0) as a placeholder rather than
            # a real block/line identifier. Do not let that merge every row.
            if candidate_line_identity not in {("0", "0"), ("None", "None")}:
                line_identity = candidate_line_identity
        normalized.append((x0, y0, x1, y1, raw_text, token, line_identity))
        heights.append(max(1.0, y1 - y0))

    if not normalized:
        return ""

    heights.sort()
    median_height = heights[len(heights) // 2]
    row_tolerance = max(4.0, median_height * 0.75)
    ordered = sorted(normalized, key=lambda item: (item[1], item[0]))
    rows: list[list[tuple]] = []
    for item in ordered:
        if not rows:
            rows.append([item])
            continue
        mean_y = sum(part[1] for part in rows[-1]) / len(rows[-1])
        same_extractor_line = (
            item[6] is not None
            and any(part[6] == item[6] for part in rows[-1])
        )
        if same_extractor_line or abs(item[1] - mean_y) <= row_tolerance:
            rows[-1].append(item)
        else:
            rows.append([item])

    doctor_x = None
    patient_header_y = None
    patient_header_x = None
    doctor_words = {"DOKTER", "DOCTOR", "PHYSICIAN", "PENGIRIM", "EXAMINER", "PRACTITIONER"}
    for row in rows:
        tokens = {part[5] for part in row}
        row_text = " ".join(part[5] for part in row)
        if (
            ("DOKTER" in tokens or "DOCTOR" in tokens or "PHYSICIAN" in tokens)
            or ("PENGIRIM" in tokens)
            or ("EXAMINER" in tokens)
            or ("PRACTITIONER" in tokens)
        ):
            doctor_candidates = [part[0] for part in row if part[5] in doctor_words]
            if doctor_candidates:
                doctor_x = min(doctor_candidates)
        if "IDENTITAS" in tokens and "PASIEN" in tokens:
            patient_header_y = sum(part[1] for part in row) / len(row)
            patient_header_x = min(part[0] for part in row)

    organization_or_form_words = {
        "RUMAH", "SAKIT", "HOSPITAL", "CLINIC", "KLINIK", "PUSKESMAS",
        "PLUIT", "JAKARTA", "UTARA", "SELATAN", "TIMUR", "BARAT", "PUSAT",
        "KOTA", "KABUPATEN", "PROVINSI", "INDONESIA", "JAWA", "JL", "JALAN", "RAYA",
        "LABORATORIUM", "LAB", "RADIOLOGI", "HASIL", "TEST",
        "PEMERIKSAAN", "MCU", "VISUS", "AUDIO", "AUDIOMETRI", "EKG",
        "SPIRO", "SPIROMETRI", "RONTGEN", "NORMAL", "ABNORMAL", "TOTAL",
        "PARTIAL", "BUTA", "WARNA", "ALAMAT", "JALAN", "RAYA", "PHONE",
        "TELP", "FAX", "WEBSITE", "WWW", "EMAIL", "TANGGAL", "DATE",
        "NO", "NOMOR", "REG", "REGNO", "MRN", "NIK", "DOB", "TTL",
        "UMUR", "USIA", "AGE", "SEX", "GENDER", "MALE", "FEMALE",
        "MAN", "WOMAN", "PRIA", "WANITA", "LAKI", "PEREMPUAN", "PASIEN",
        "NAMA", "NAME", "PATIENT", "IDENTITAS", "KELAMIN", "DOKTER",
        "DOCTOR", "PHYSICIAN", "PENGIRIM", "EXAMINER", "PRACTITIONER",
        "MR", "MRS", "MS", "MISS", "DR", "DRA", "DRS", "PROF", "IR",
        "SP", "PK", "PD", "OG", "KJ", "JP", "THT", "RAD", "AN", "MARS",
        "MD", "AP", "APT", "SPOG", "SPPK", "SPPD", "SPJP", "SPKJ",
    }
    metadata_words = {
        "DOB", "TTL", "BIRTH", "LAHIR", "TANGGAL", "DATE", "AGE", "UMUR",
        "USIA", "SEX", "GENDER", "MALE", "FEMALE", "MAN", "WOMAN", "PRIA",
        "WANITA", "LAKI", "PEREMPUAN", "ALAMAT", "ADDRESS", "REG", "ID",
        "NO", "NOMOR", "MRN", "NIK", "PATIENT", "PASIEN",
    }
    doctor_titles = {"DR", "DRA", "DRS", "DOKTER", "DOCTOR", "PROF", "IR", "PHYSICIAN"}
    doctor_credentials = {"SP", "PK", "PD", "OG", "KJ", "JP", "THT", "RAD", "AN", "MARS", "MD", "SPOG", "SPPK", "SPPD", "SPJP", "SPKJ"}

    def clean_person_row(row: list[tuple]) -> tuple[str, bool]:
        raw_tokens = [part[4] for part in sorted(row, key=lambda part: part[0])]
        tokens = [re.sub(r"[^A-Z0-9]", "", normalize_patient_name(value)) for value in raw_tokens]
        pairs = [(token, raw) for token, raw in zip(tokens, raw_tokens) if token]
        if not pairs:
            return "", False

        explicit_label = any(
            token in {"NAMA", "NAME", "PATIENTNAME", "NAMAPASIEN", "PASIEN"}
            for token, _ in pairs
        )
        if explicit_label:
            first_label = next(
                i for i, (token, _) in enumerate(pairs)
                if token in {"NAMA", "NAME", "PATIENTNAME", "NAMAPASIEN", "PASIEN"}
            )
            pairs = pairs[first_label + 1:]
            while pairs and pairs[0][0] in {"PASIEN", "PATIENT"}:
                pairs.pop(0)

        if not pairs:
            return "", explicit_label

        # A doctor title/credential invalidates the whole row. Do not extract
        # the doctor's given name by simply dropping the title token.
        pair_tokens = [token for token, _ in pairs]
        if any(token in doctor_titles or token in doctor_credentials for token in pair_tokens):
            return "", explicit_label

        # Split adjacent text into plausible name segments around form labels,
        # digits, and non-name content. This handles e.g. "HASIL TEST ACEP SURYAGANDA"
        # where the patient's sticker shares a baseline with the report heading.
        segments: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        for token, raw in pairs:
            is_web_or_provider = (
                token in {"WWW", "WWIVW", "WVVW", "COM", "NET", "ORG", "WEBSITE", "EMAIL"}
                or any(marker in token for marker in ("HOSPITAL", "CLINIC", "PLUIT", "PUSKESMAS"))
            )
            is_exam_or_form = (
                any(marker in token for marker in ("BUTAWARNA", "WARNA", "NORMAL", "ABNORMAL"))
                or (len(token) >= 5 and max(fuzz.ratio(token, term) for term in ("NORMAL", "ABNORMAL", "PEMERIKSAAN", "HASIL")) >= 88)
            )
            is_stop = (
                token in organization_or_form_words or token in metadata_words
                or token in {"UN", "IN", "OF", "TO", "AT", "BY", "ON", "AS", "IS", "OR"}
                or is_web_or_provider or is_exam_or_form
            )
            # One-letter OCR fragments are commonly punctuation/checkbox noise.
            # Ignore them without splitting a valid multi-word name around them.
            if len(token) == 1:
                continue
            if re.search(r"\d", token) or is_stop:
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append((token, raw))
        if current:
            segments.append(current)

        valid_segments: list[tuple[str, float, int]] = []
        for segment in segments:
            segment_tokens = [part[0] for part in segment]
            if not 2 <= len(segment_tokens) <= 5:
                continue
            if any(len(token) < 2 for token in segment_tokens[:-1]):
                continue
            candidate = normalize_patient_identity_name(" ".join(segment_tokens))
            if candidate and len(candidate.split()) >= 2 and len(candidate) >= 5:
                raw_letters = "".join(re.findall(r"[A-Za-z]", " ".join(part[1] for part in segment)))
                segment_upper_ratio = (
                    sum(1 for char in raw_letters if char.isupper()) / len(raw_letters)
                    if raw_letters else 0.0
                )
                valid_segments.append((candidate, segment_upper_ratio, len(candidate)))

        if not valid_segments:
            return "", explicit_label
        if len(valid_segments) > 1:
            valid_segments.sort(key=lambda item: (item[1], item[2]), reverse=True)
            best, best_upper, best_length = valid_segments[0]
            _second, second_upper, second_length = valid_segments[1]
            # If one segment is clearly styled as a proper uppercase name and
            # OCR debris is lowercase/shorter, prefer the stronger segment.
            if best_upper < 0.75 or best_upper - second_upper < 0.20:
                return "", explicit_label
            return best, explicit_label
        return valid_segments[0][0], explicit_label


    row_tokens = [{part[5] for part in row} for row in rows]
    metadata_row_indexes = []
    for index, row in enumerate(rows):
        tokens = row_tokens[index]
        row_has_digits = any(re.search(r"\d", part[4]) for part in row)
        if row_has_digits or (tokens & metadata_words):
            metadata_row_indexes.append(index)

    candidates: list[tuple[float, str]] = []
    for index, row in enumerate(rows):
        row_y = sum(part[1] for part in row) / len(row)
        row_x = min(part[0] for part in row)
        row_x1 = max(part[2] for part in row)
        tokens = row_tokens[index]
        row_text = " ".join(part[4] for part in row)

        # Exclude header/footer and strip the physician-side words even when
        # patient/doctor values share one visual baseline in a two-column table.
        if patient_header_y is not None and row_y <= patient_header_y + row_tolerance:
            continue
        candidate_row = list(row)
        if doctor_x is not None:
            candidate_row = [part for part in candidate_row if part[0] < doctor_x - 3]
        if patient_header_y is not None:
            if patient_header_x is not None:
                candidate_row = [
                    part for part in candidate_row
                    if part[0] >= patient_header_x - max(20.0, median_height * 3)
                ]
        if not candidate_row:
            continue

        candidate, explicit_label = clean_person_row(candidate_row)
        if not candidate:
            continue

        # Explicit name labels are strong evidence; otherwise require nearby
        # patient-form context, not any particular MRN/registration marker.
        nearby_distances = [abs(index - meta_index) for meta_index in metadata_row_indexes if meta_index != index]
        nearest_context = min(nearby_distances) if nearby_distances else 99
        has_patient_header_context = patient_header_y is not None and row_y > patient_header_y
        if not explicit_label and nearest_context > 3 and not has_patient_header_context:
            continue

        raw_letters = "".join(re.findall(r"[A-Za-z]", row_text))
        uppercase_ratio = (
            sum(1 for char in raw_letters if char.isupper()) / len(raw_letters)
            if raw_letters else 0.0
        )
        # Candidate confidence is a ranking score, not an OCR probability.
        score = 5.0 if explicit_label else 0.0
        if nearest_context <= 1:
            score += 4.0
        elif nearest_context == 2:
            score += 3.0
        elif nearest_context == 3:
            score += 2.0
        if has_patient_header_context:
            score += 2.0
        if uppercase_ratio >= 0.75:
            score += 1.5
        if 2 <= len(candidate.split()) <= 3:
            score += 1.0
        # Slightly penalize extremely long rows that may be report headings.
        if row_x1 - row_x > page_width * 0.65:
            score -= 1.0
        candidates.append((score, candidate))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_name = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == best_score and candidates[1][1] != best_name:
        return ""  # Ambiguous: send to manual verification rather than guessing.
    return best_name


def find_name_in_pdf_layout(filepath: Path) -> str:
    """Ekstrak nama dengan urutan: identitas terstruktur, lalu layout generik."""
    try:
        with fitz.open(filepath) as document:
            for page in document:
                words = page.get_text("words", sort=True)
                candidate = _name_from_positioned_words(words, float(page.rect.width))
                if candidate:
                    return candidate
                candidate = _generic_name_from_positioned_words(words, float(page.rect.width))
                if candidate:
                    return candidate
    except Exception:
        return ""
    return ""


def find_name_in_ocr_page_layout(page: fitz.Page) -> str:
    """Fallback OCR berbasis koordinat untuk halaman scan dengan tabel dua kolom."""
    if pytesseract is None or not get_tesseract_status().get("available"):
        return ""
    image = None
    gray = None
    try:
        scale = OCR_DPI / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        gray = ImageOps.autocontrast(ImageOps.grayscale(image))
        if gray.width < 1500:
            ratio = 1500 / max(1, gray.width)
            gray = gray.resize((int(gray.width * ratio), int(gray.height * ratio)), Image.Resampling.LANCZOS)
        language = _resolve_ocr_language(get_tesseract_status())
        data = pytesseract.image_to_data(
            gray, lang=language, config="--oem 3 --psm 6", output_type=TesseractOutput.DICT
        )
        words = []
        texts = data.get("text", [])
        for index, raw in enumerate(texts):
            word = str(raw or "").strip()
            if not word:
                continue
            try:
                confidence = float(data.get("conf", [])[index])
                if confidence < 0:
                    continue
                left = float(data.get("left", [])[index])
                top = float(data.get("top", [])[index])
                width = float(data.get("width", [])[index])
                height = float(data.get("height", [])[index])
                block = int(data.get("block_num", [0])[index]) * 1000 + int(data.get("par_num", [0])[index])
                line = int(data.get("line_num", [0])[index])
                words.append((left, top, left + width, top + height, word, block, line))
            except (ValueError, TypeError, IndexError):
                continue
        candidate = _name_from_positioned_words(words, float(gray.width))
        if not candidate:
            candidate = _generic_name_from_positioned_words(words, float(gray.width))
        if candidate:
            return candidate

        # Pass kedua: tingkatkan kontras/ketajaman dan pakai sparse-text mode.
        # Hanya dijalankan ketika pass pertama belum menemukan nama.
        enhanced = ImageOps.equalize(gray).filter(
            ImageFilter.UnsharpMask(radius=1.2, percent=160, threshold=3)
        )
        try:
            enhanced_data = pytesseract.image_to_data(
                enhanced, lang=language, config="--oem 3 --psm 11",
                output_type=TesseractOutput.DICT,
            )
            enhanced_words = []
            for index, raw in enumerate(enhanced_data.get("text", [])):
                word = str(raw or "").strip()
                if not word:
                    continue
                try:
                    confidence = float(enhanced_data.get("conf", [])[index])
                    if confidence < 0:
                        continue
                    left = float(enhanced_data.get("left", [])[index])
                    top = float(enhanced_data.get("top", [])[index])
                    width = float(enhanced_data.get("width", [])[index])
                    height = float(enhanced_data.get("height", [])[index])
                    block = int(enhanced_data.get("block_num", [0])[index]) * 1000 + int(enhanced_data.get("par_num", [0])[index])
                    line = int(enhanced_data.get("line_num", [0])[index])
                    enhanced_words.append((left, top, left + width, top + height, word, block, line))
                except (ValueError, TypeError, IndexError):
                    continue
            candidate = _name_from_positioned_words(enhanced_words, float(enhanced.width))
            if not candidate:
                candidate = _generic_name_from_positioned_words(enhanced_words, float(enhanced.width))
            if candidate:
                return candidate
        finally:
            enhanced.close()

        # OCR halaman penuh kadang menggabungkan dua kolom. Ulangi OCR hanya
        # pada 55% sisi kiri halaman agar nama dokter di kolom kanan tersisih.
        crop_width = max(1, int(gray.width * 0.55))
        patient_crop = gray.crop((0, 0, crop_width, gray.height))
        try:
            crop_data = pytesseract.image_to_data(
                patient_crop, lang=language, config="--oem 3 --psm 4",
                output_type=TesseractOutput.DICT,
            )
            crop_words = []
            for index, raw in enumerate(crop_data.get("text", [])):
                word = str(raw or "").strip()
                if not word:
                    continue
                try:
                    confidence = float(crop_data.get("conf", [])[index])
                    if confidence < 0:
                        continue
                    left = float(crop_data.get("left", [])[index])
                    top = float(crop_data.get("top", [])[index])
                    width = float(crop_data.get("width", [])[index])
                    height = float(crop_data.get("height", [])[index])
                    block = int(crop_data.get("block_num", [0])[index]) * 1000 + int(crop_data.get("par_num", [0])[index])
                    line = int(crop_data.get("line_num", [0])[index])
                    crop_words.append((left, top, left + width, top + height, word, block, line))
                except (ValueError, TypeError, IndexError):
                    continue
            candidate = _name_from_positioned_words(
                crop_words, float(patient_crop.width), left_column_only=True
            )
            if not candidate:
                candidate = _generic_name_from_positioned_words(
                    crop_words, float(patient_crop.width)
                )
            return candidate
        finally:
            patient_crop.close()
    except Exception:
        return ""
    finally:
        if gray is not None:
            gray.close()
        if image is not None:
            image.close()


@lru_cache(maxsize=4)
def get_tesseract_status() -> dict[str, Any]:
    """Periksa wrapper, binary Tesseract, versi, dan paket bahasa yang tersedia."""
    if pytesseract is None:
        return {
            "available": False,
            "version": "Tidak tersedia",
            "languages": [],
            "message": f"Library pytesseract belum terpasang: {PYTESSERACT_IMPORT_ERROR}",
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
    # Prefer coordinate-aware parsing of the left IDENTITAS PASIEN column.
    # This avoids reading "Nama" from the right DOKTER PENGIRIM column.
    text_candidate = find_name_in_pdf_layout(filepath) or find_name_in_pdf_text(text_layer)
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
    ocr_layout_candidate = ""

    if enable_ocr and ocr_required:
        try:
            with fitz.open(filepath) as document:
                page_limit = min(document.page_count, OCR_MAX_PAGES)
                for page_index in range(page_limit):
                    ocr_used = True
                    page = document.load_page(page_index)
                    page_text, confidence, message = run_tesseract_on_page(
                        page, file_hash, page_index + 1
                    )
                    if page_text:
                        ocr_text_parts.append(page_text)
                    if not ocr_layout_candidate:
                        # First try text parsing, then OCR word coordinates in the
                        # left patient column if the two-column line is ambiguous.
                        # Positional OCR takes priority: plain text can flatten
                        # side-by-side patient/doctor cells into one ambiguous line.
                        ocr_layout_candidate = find_name_in_ocr_page_layout(page)
                        if not ocr_layout_candidate:
                            ocr_layout_candidate = find_name_in_pdf_text(page_text)
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
    ocr_candidate = ocr_layout_candidate or find_name_in_pdf_text(ocr_text)
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
        extraction_source = "TEXT_LAYER"
        ocr_status = "OCR dinonaktifkan pada sesi ini."
    elif not get_tesseract_status()["available"]:
        extraction_source = "TEXT_LAYER"
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


if pytesseract is not None:
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    elif os.name == "nt":
        _default_tesseract = Path(
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
        if _default_tesseract.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(_default_tesseract)
