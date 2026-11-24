from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

MEDICAL_KEYWORDS = [
    "AUDIOMETRIK", "AUDIOMETRI", "LABORATORIUM", "RADIOLOGI",
    "EKG", "RONTGEN", "SPIROMETRI", "SPIRO", "VISUS", "HASIL",
    "PEMERIKSAAN", "MCU", "LPK", "LAB", "AUDIO",
]


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


def _generic_name_from_text_lines(text: str) -> str:
    """Fallback line-based name detection for text-only PDFs without known labels."""
    lines = [re.sub(r"\s+", " ", line).strip(" \t|:;,.#-") for line in str(text or "").splitlines()]
    if not lines:
        return ""
    stop = {
        "RUMAH", "SAKIT", "HOSPITAL", "KLINIK", "CLINIC", "PUSKESMAS", "PLUIT",
        "JAKARTA", "UTARA", "SELATAN", "TIMUR", "BARAT", "PUSAT", "KOTA",
        "KABUPATEN", "PROVINSI", "INDONESIA", "JAWA", "JL", "JALAN", "RAYA",
        "HASIL", "TEST", "PEMERIKSAAN", "MCU", "VISUS", "AUDIO", "LAB", "EKG",
        "SPIRO", "RONTGEN", "NORMAL", "ABNORMAL", "TOTAL", "PARTIAL", "WARNA",
        "MRN", "REG", "NO", "NOMOR", "NIK", "DOB", "TTL", "TANGGAL", "LAHIR",
        "UMUR", "USIA", "AGE", "SEX", "GENDER", "MALE", "FEMALE", "MAN",
        "WOMAN", "PRIA", "WANITA", "DOKTER", "DOCTOR", "PHYSICIAN", "PENGIRIM",
        "DR", "DRA", "DRS", "PROF", "IR", "SP", "PK", "PD", "OG", "KJ", "JP",
        "THT", "RAD", "AN", "MARS", "MD", "NAMA", "NAME", "PASIEN", "PATIENT",
    }
    metadata_terms = {"DOB", "TTL", "TANGGAL", "LAHIR", "UMUR", "USIA", "AGE", "SEX", "GENDER", "PRIA", "WANITA", "MAN", "FEMALE", "MALE", "REG", "NO", "NIK", "MRN", "ID"}
    candidates = []
    for index, line in enumerate(lines):
        tokens = [re.sub(r"[^A-Z0-9]", "", normalize_patient_name(token)) for token in line.split()]
        tokens = [token for token in tokens if token]
        if not 2 <= len(tokens) <= 5 or any(re.search(r"\d", token) for token in tokens):
            continue
        if any(token in stop for token in tokens):
            continue
        if any(token in {"DOKTER", "DOCTOR", "DR", "DRA", "DRS", "PROF", "IR", "SP", "PK", "PD", "OG", "KJ", "JP", "THT", "RAD", "AN", "MARS", "MD"} for token in tokens):
            continue
        if any(len(token) < 2 for token in tokens[:-1]):
            continue
        context = []
        for neighbor_index in (index - 2, index - 1, index + 1, index + 2):
            if 0 <= neighbor_index < len(lines):
                neighbor = lines[neighbor_index]
                neighbor_tokens = set(re.sub(r"[^A-Z0-9]", "", normalize_patient_name(t)) for t in neighbor.split())
                if re.search(r"\d", neighbor) or (neighbor_tokens & metadata_terms):
                    context.append(neighbor_index)
        if not context:
            continue
        if any(re.search(r"(?i)DOKTER\s+PENGIRIM|DOCTOR|PHYSICIAN|EXAMINER", line) for line in lines[max(0, index-2):index+1]):
            continue
        candidates.append((min(abs(index - other) for other in context), normalize_patient_identity_name(" ".join(tokens))))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0] and candidates[0][1] != candidates[1][1]:
        return ""
    return candidates[0][1]


def find_name_in_pdf_text(text: str) -> str:
    """Ambil nama pasien dari blok IDENTITAS PASIEN, bukan blok dokter.

    Banyak laporan MCU memiliki dua kolom: IDENTITAS PASIEN dan DOKTER
    PENGIRIM. Keduanya dapat memiliki label "Nama". Karena itu pencarian
    diprioritaskan pada blok identitas pasien dan dihentikan sebelum blok dokter.
    Jika format dokumen tidak memiliki penanda blok tersebut, gunakan parser
    label umum sebagai fallback.
    """
    if not text:
        return ""

    raw_text = str(text).replace("\r", "\n")
    patient_marker = re.search(r"(?i)IDENTITAS\s+PASIEN", raw_text)
    if patient_marker:
        patient_block = raw_text[patient_marker.end():]
        end_markers = [
            r"DOKTER\s+PENGIRIM", r"IDENTITAS\s+DOKTER",
            r"DATA\s+DOKTER", r"INFORMASI\s+DOKTER",
        ]
        end_positions = [
            m.start() for pattern in end_markers
            if (m := re.search(pattern, patient_block, re.IGNORECASE))
        ]
        if end_positions:
            patient_block = patient_block[:min(end_positions)]
        search_text = patient_block
    else:
        # Jika dokumen menyebut kolom DOKTER PENGIRIM tetapi header pasien gagal
        # terbaca (termasuk salah baca OCR seperti DORTER), label "Nama" ambigu.
        # Jangan fallback ke nama dokter hanya karena heading pasien gagal dikenali.
        raw_tokens = re.findall(r"[A-Z]+", raw_text.upper())
        has_doctor_word = any(fuzz.ratio(token, "DOKTER") >= 68 for token in raw_tokens)
        has_sender_word = any(fuzz.ratio(token, "PENGIRIM") >= 65 for token in raw_tokens)
        if has_doctor_word and has_sender_word:
            return ""
        if re.search(r"(?i)IDENTITAS\s+DOKTER", raw_text):
            return ""
        search_text = raw_text

    raw_lines = [str(line).rstrip("\r\n") for line in search_text.splitlines()]
    lines = [re.sub(r"[ \t]+", " ", line).strip(" \t|") for line in raw_lines]

    name_label_re = re.compile(
        r"(?i)(?:^|[\s|])(?:nama(?:\s+pasien|\s+lengkap)?|patient\s+name|name)"
        r"\s*[:\-]?\s*(.*)$"
    )
    patient_label_re = re.compile(r"(?i)(?:^|[\s|])pasien\s*[:\-]\s*(.*)$")
    stop_re = re.compile(
        r"(?i)\s+(?=(?:no\.?\s*(?:transaksi|rm|rekam|registrasi|reg)|"
        r"nomor\s*(?:transaksi|rekam\s*medis)|tanggal\s*lahir|tgl\.?\s*lahir|"
        r"date\s*of\s*birth|dob|nik|n\.?i\.?k\.?|jenis\s*kelamin|kelamin|"
        r"umur|usia|alamat|tempat\s*lahir|poliklinik|dokter|jenis\s*pemeriksaan|"
        r"nama(?:\s+pasien|\s+lengkap)?|patient\s+name|name)\s*[:#\-])"
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
        "POLIKLINIK", "LABORATORIUM", "RADIOLOGI", "RONTGEN", "SPIRO",
        "VISUS", "AUDIO", "EKG", "LAB", "LPK",
    }

    def clean_candidate(raw_value: str) -> str:
        value = str(raw_value or "").strip(" \t:;,.|#-")
        value = stop_re.split(value, maxsplit=1)[0].strip(" \t:;,.|#-")
        value = re.sub(
            r"(?i)^(?:nama(?:\s+pasien|\s+lengkap)?|patient\s+name|name|pasien)\s*[:\-]?\s*",
            "", value,
        ).strip(" \t:;,.|#-")
        cleaned = normalize_patient_identity_name(value)
        if not cleaned or cleaned in invalid_values or re.search(r"\d", cleaned):
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
        matches = [m for pattern in (name_label_re, patient_label_re) if (m := pattern.search(line))]
        matches.sort(key=lambda match: match.start())
        for match in matches:
            value = clean_candidate(match.group(1))
            if value:
                return value

        label_only = re.fullmatch(
            r"(?i)(?:nama(?:\s+pasien|\s+lengkap)?|patient\s+name|name|pasien)\s*[:\-]?",
            line,
        )
        if label_only:
            for next_line in lines[index + 1:index + 4]:
                if not next_line:
                    continue
                if re.match(
                    r"(?i)^(?:no\.?\s*(?:transaksi|rm|rekam|registrasi|reg)|"
                    r"nomor\s*(?:transaksi|rekam\s*medis)|tanggal\s*lahir|"
                    r"tgl\.?\s*lahir|dob|nik|jenis\s*kelamin|kelamin|umur|usia|"
                    r"alamat|tempat\s*lahir|puasa|jenis\s*pemeriksaan)\b",
                    next_line,
                ):
                    break
                value = clean_candidate(next_line)
                if value:
                    return value
                break

        raw_line = raw_lines[index]
        table_match = re.match(
            r"(?i)^\s*(?:nama(?:\s+pasien|\s+lengkap)?|patient\s+name|name)\s{2,}(.+?)\s*$",
            raw_line,
        )
        if table_match:
            value = clean_candidate(table_match.group(1))
            if value:
                return value

    # Format vendor yang menempelkan nama di stiker tanpa label NAMA/PASIEN.
    # Konteks sekitar dipakai sebagai petunjuk, bukan MRN/REG sebagai syarat.
    return _generic_name_from_text_lines(search_text)


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


def extract_identity_fields(text: str) -> dict[str, str]:
    """Ekstrak pengenal pendukung; NIK/No. RM hanya disimpan sebagai hash dan mask."""
    nik_value = _extract_labeled_value(
        text,
        r"(?:NIK|N\.I\.K\.?|NO\.?\s*KTP|NOMOR\s*INDUK\s*KEPENDUDUKAN)",
    )
    nik_digits = re.sub(r"\D", "", nik_value)
    nik_digits = nik_digits[:16] if len(nik_digits) >= 16 else ""
    if nik_digits == "0" * 16:
        nik_digits = ""

    mr_value = _extract_labeled_value(
        text,
        r"(?:NO\.?\s*RM|NOMOR\s*REKAM\s*MEDIS|NO\.?\s*REG(?:ISTRASI)?|"
        r"MRN|MEDICAL\s*RECORD\s*(?:NO|NUMBER)|ID\s*PASIEN|PATIENT\s*ID)",
    )
    mr_normalized = _normalize_identifier(mr_value)
    if not (4 <= len(mr_normalized) <= 24):
        mr_normalized = ""

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
        "dob_iso": dob_iso,
    }


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
