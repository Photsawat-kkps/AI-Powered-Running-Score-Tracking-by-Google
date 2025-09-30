# -*- coding: utf-8 -*-
"""
Backfill + Recheck OCR เฉพาะช่วงเวลา (copy -> ocr)
- ซิงก์เฉพาะแถวในช่วงเวลาที่กำหนดจาก RAW -> WORK (ถ้าหายไป)
- Detect เฉพาะแถวในช่วงเวลา และเฉพาะแถวที่ยัง "ไม่มี Out_Status และ In_Status"
- ใช้ logic เดียวกับสคริปต์หลักล่าสุด (main):
  • OCR wrapper กันพัง (non-image/รูปพัง/Vision error)
  • Parser เวลา/ระยะ (กัน km/h, รูปแบบแปลก, packed digits, มีคะแนนใกล้ label, pace injection)
  • Outdoor/Indoor + All Condition Insufficient / Distance Insufficient / Time Over
  • เขียน Shot_Date จาก OCR เหมือน main

Entry point: backfill_window_http (Cloud Run / Functions Framework)
"""

import os
import io
import re
import time
import datetime as dt
from typing import List, Optional, Tuple

from flask import Request, make_response
import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
from google.cloud import vision
from PIL import Image, UnidentifiedImageError  # สำหรับตรวจไฟล์รูป

# -------- Window (แก้ได้ตามต้องการ หรือ map มาจาก env) --------
try:
    _tz = dt.timezone(dt.timedelta(hours=int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "7"))))
except Exception:
    _tz = dt.timezone(dt.timedelta(hours=7))
_yesterday = (dt.datetime.now(_tz) - dt.timedelta(days=1)).date()
DEFAULT_FROM = dt.datetime(_yesterday.year, _yesterday.month, _yesterday.day, 0, 0, 0, tzinfo=_tz).isoformat(timespec="seconds")
DEFAULT_TO   = dt.datetime(_yesterday.year, _yesterday.month, _yesterday.day, 23, 59, 59, tzinfo=_tz).isoformat(timespec="seconds")

# ---------------- คอนฟิก (ต้องตรงกับ main) ----------------
SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID", "1-ht6PyQtynMG-dMpSGM4I3sVr7HBhb8y33xwl_QZvVA")
SHEET_NAME_RAW   = os.getenv("SHEET_NAME", "Form Responses 1")
SHEET_NAME_WORK  = os.getenv("WORK_SHEET_NAME", f"{SHEET_NAME_RAW} (Working)")

# Outdoor images
IMAGE_COL_NAME   = os.getenv("IMAGE_COL_NAME", "รูปถ่ายแสดงระยะทาง Outdoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)")
SELFIE_COL_NAME  = os.getenv("SELFIE_COL_NAME", "รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Outdoor (Selfie)")

# Indoor images
INDOOR_DIGI_COL  = os.getenv("INDOOR_DIGI_COL", "รูปถ่ายแสดงระยะทาง Indoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)")
INDOOR_MACH_COL  = os.getenv("INDOOR_MACH_COL", "รูปถ่ายระยะทางจากเครื่องออกกำลังกาย (Photo of the distance display from the exercise machine.)")

# Where?
WHERE_COL_NAME   = os.getenv("WHERE_COL_NAME", "ลักษณะสถานที่วิ่ง (Where did you run?)")
OUTDOOR_KEYS     = ["กลางแจ้ง", "นอกบ้าน", "outdoor"]
INDOOR_KEYS      = ["ในร่ม", "indoor"]

# Outdoor results
STATUS_COL = os.getenv("STATUS_COL", "Out_Status")
DIST_COL   = os.getenv("DIST_COL",  "Out_Distance_km")
DUR_COL    = os.getenv("DUR_COL",   "Out_Duration_hms")

# Indoor results
IN_STATUS_COL = os.getenv("IN_STATUS_COL", "In_Status")
DIGI_DIST_COL = os.getenv("DIGI_DIST_COL", "digi_distance_km")
DIGI_DUR_COL  = os.getenv("DIGI_DUR_COL",  "digi_duration_hms")
MACH_DIST_COL = os.getenv("MACH_DIST_COL", "mach_distance_km")
MACH_DUR_COL  = os.getenv("MACH_DUR_COL",  "mach_duration_hms")

# Shot date (เหมือน main)
PHOTO_DATE_COL = os.getenv("PHOTO_DATE_COL", "Shot_Date")

# Ranges
RAW_RANGE  = os.getenv("RAW_RANGE",  f"{SHEET_NAME_RAW}!A:AZ")
WORK_RANGE = os.getenv("WORK_RANGE", f"{SHEET_NAME_WORK}!A:AZ")

# Timestamp column (ต้องมีใน RAW/WORK)
TIMESTAMP_COL_NAME     = os.getenv("TIMESTAMP_COL_NAME", "Timestamp")
LOCAL_TZ_OFFSET_HOURS  = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "7"))

# thresholds & labels (ให้ตรงกับ main)
TIME_OVER_HMS        = os.getenv("TIME_OVER_HMS", "02:00:00")
DIST_MIN_KM          = float(os.getenv("DIST_MIN_KM", "2.0"))  # < 2.00 km
STATUS_COND_INSUFF   = "All Condition Insufficient"
STATUS_DIST_INSUFF   = "Distance Insufficient"

# ---------------- Google clients ----------------
def _build_services():
    creds, _ = google.auth.default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive  = build("drive",  "v3", credentials=creds, cache_discovery=False)
    return sheets, drive

# ---------------- Sheets helpers ----------------
def _get_values(sheets, a1: str):
    return sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=a1
    ).execute().get("values", [])

def _update_values(sheets, a1: str, values):
    return sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=a1,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()

def _append_values(sheets, a1: str, values):
    return sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=a1,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()

def _list_sheet_titles(sheets):
    meta = sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return [sh["properties"]["title"] for sh in meta.get("sheets", [])]

def _ensure_sheet_exists(sheets, title: str):
    if title in _list_sheet_titles(sheets):
        return
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()

def _idx(header, name: str) -> Optional[int]:
    name = (name or "").lower().strip()
    for i, h in enumerate(header):
        if (h or "").lower().strip() == name:
            return i
    return None

def _ensure_col(header, rows, name: str) -> int:
    i = _idx(header, name)
    if i is None:
        header.append(name)
        i = len(header) - 1
        for r in rows:
            r.append("")
    return i

def _pad_row(row, target_len: int):
    if len(row) < target_len:
        return row + [""] * (target_len - len(row))
    return row[:target_len]

def _pad_all_rows(rows: List[List[str]], target_len: int):
    for j in range(len(rows)):
        if len(rows[j]) < target_len:
            rows[j] = rows[j] + [""] * (target_len - len(rows[j]))
        elif len(rows[j]) > target_len:
            rows[j] = rows[j][:target_len]

def _col_letter(n: int) -> str:
    s = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        s.append(chr(65 + r))
    return "".join(reversed(s))

def _range_for_row(sheet_name: str, row_1based: int, num_cols: int) -> str:
    last_col = _col_letter(num_cols)
    return f"{sheet_name}!A{row_1based}:{last_col}{row_1based}"

def get_cell(row: List[str], idx: Optional[int]) -> str:
    """อ่าน cell แบบปลอดภัย – ถ้า idx None หรือเลยความยาว ให้คืน "" """
    if idx is None:
        return ""
    return row[idx] if idx < len(row) else ""

# ---------------- Drive + OCR (เหมือน main) ----------------
ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "image/tiff", "image/bmp"
}
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}

def looks_like_image_by_meta(filename: str, content_type: Optional[str]) -> bool:
    fn = (filename or "").lower()
    if content_type and content_type.startswith("image/"):
        return True
    if content_type in ALLOWED_IMAGE_MIMES:
        return True
    return any(fn.endswith(ext) for ext in ALLOWED_EXTS)

def bytes_is_valid_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False

def _file_ids_from_cell(cell: str) -> List[str]:
    links = re.split(r"[, \n]+", (cell or "").strip())
    ids: List[str] = []
    for url in links:
        if not url:
            continue
        m = re.search(r"/d/([A-Za-z0-9_-]+)", url) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
        if m:
            ids.append(m.group(1))
    return ids

def _download_bytes_and_meta(drive, file_id: str) -> tuple[bytes, str, str]:
    meta = drive.files().get(fileId=file_id, fields="name,mimeType").execute()
    filename = meta.get("name") or ""
    mime = meta.get("mimeType") or ""
    req = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue(), filename, mime

def ocr_image_bytes_safe(data: bytes, filename: str, content_type: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """
    Return: (status, reason, text)
      - status: "OK" | "NG"
      - reason: non-image / corrupt-bad-image / vision-error / vision-exception
      - text:   OCR text (ถ้าสำเร็จ)
    """
    if not looks_like_image_by_meta(filename, content_type):
        return "NG", "non-image", None
    if not bytes_is_valid_image(data):
        return "NG", "corrupt/bad image data", None

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=data)
    try:
        resp = client.text_detection(image=image)
        if resp.error.message:
            return "NG", f"vision-error: {resp.error.message}", None
        text = (resp.full_text_annotation.text or "").strip() if resp.full_text_annotation else ""
        return ("OK", "", text) if text else ("OK", "", "")
    except Exception as e:
        return "NG", f"vision-exception: {e.__class__.__name__}", None

# ---------------- Smart parsers (เหมือน main) ----------------
DIST_LABEL  = re.compile(r"^\s*distance\s*$", re.I)
TIME_LABEL  = re.compile(r"^\s*elapsed\s*time\s*$", re.I)
PACE_LABEL  = re.compile(r"^\s*(avg(?:\.|erage)?\s*)?pace\s*$", re.I)

KM_RE       = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:k\s*m|km\.?|kilometers?\.?|Kilometers?\.?|กม\.?|กม|กิโลเมตร\.?)\b", re.I)
TIME_ANY_RE = re.compile(
    r"\b(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,]\d{1,3})?\b(?!\s*(?:AM|PM)\b)"
    r"|"
    r"\b(\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\b(?!\s*(?:AM|PM)\b)",
    re.I
)
PACE_RE     = re.compile(
    r"(\d{1,2})[:'’](\d{2})\s*(?:(?:min|mins|minute|minutes|นาที|น\.)\s*)?/\s*(?:k\s*m|km|kilometers?|kilometres?|กิโลเมตร|กม\.?|กม)\b",
    re.I
)
DECIMAL_RE   = re.compile(r"\b(\d+[.,]\d+)\b")
TWO_DEC_RE   = re.compile(r"\b(\d+[.,]\d{2})\b")

KEYWORDS = {
    "distance": ["distance", "dist", "Distance", "ระยะทาง", "ระยะ", "Kilometers", "kilometers", "Kilometres", "kilometres", "กิโลเมตร", "กม.", "Distance (km)", "Distance [km]"],
    "time":     ["elapsed time", "Elapsed time", "duration", "Duration", "time", "Time", "เวลาที่ใช้", "เวลา", "Workout Time", "Workout time", "Moving Time", "h:m:s", "H:M:S", "เวลาออกกำลังกาย", "Running Time"],
    "pace":     ["avg pace", "average pace", "Avg. pace", "pace", "Pace", "เพซ"],
}

def _norm_lines(text: str) -> List[str]:
    return [
        ln.strip().replace("’", ":").replace("′", ":")
        for ln in (text or "").splitlines()
        if ln and ln.strip()
    ]

def _label_idxs(lines: List[str], regex: re.Pattern, keys: List[str]) -> List[int]:
    idxs = []
    for i, l in enumerate(lines):
        s = l.lower().strip()
        if (regex.search(l) or any(k.lower() in s for k in keys)) and not KM_RE.search(s):
            idxs.append(i)
    return idxs

def _normalize_to_hhmmss(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().replace(",", ".")
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.\d{1,3})?", s)
    if m:
        h, mm, ss = map(int, m.groups())
        return f"{h:02d}:{mm:02d}:{ss:02d}"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\.\d{1,3})?", s)
    if m:
        mm, ss = map(int, m.groups())
        return f"00:{mm:02d}:{ss:02d}"
    return None

def _find_time(lines: List[str]) -> Optional[str]:
    # เวอร์ชันเต็ม (รวม ms-first + spoken + mixed + scoring) — เหมือน main
    HHMMSS_RE = re.compile(r"\b(\d{1,3}):(\d{2}):(\d{2})(?:\.\d{1,3})?\b")
    MMSS_RE   = re.compile(r"\b(\d{1,2}):(\d{2})(?:\.\d{1,3})?\b(?!\s*(?:AM|PM)\b)", re.I)
    MIXED_HHMMSS_RE = re.compile(
        r"(?<![0-9A-Za-z])(?P<h>\d{1,2})\s*(?P<sep1>[:.])\s*(?P<m>\d{2})\s*(?P<sep2>[:.])\s*(?P<s>\d{2})(?!\.\d)"
    )
    FRACT_HHMMSS_RE = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\b")
    FRACT_MMSS_RE   = re.compile(r"\b(\d{1,2}):(\d{2})[.,](\d{1,3})\b(?!\s*(?:AM|PM)\b)", re.I)

    DATE_SLASH_RE = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)")
    DATE_ISO_RE   = re.compile(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)")

    H_UNITS = r"(?:h|hr|hrs|hour|hours|ชั่วโมง|ชม\.?|ช\.ม\.?)"
    M_UNITS = r"(?:m|min|mins|minute|minutes|นาที|น\.?)"
    S_UNITS = r"(?:s|sec|secs|second|seconds|วินาที|วิ\.?|วิ)"
    HM_SPOKEN_RE = re.compile(
        rf"(?<!\d)(\d{{1,3}})\s*{H_UNITS}\s*(\d{{1,2}})\s*{M_UNITS}(?:\s*(\d{{1,2}})\s*{S_UNITS})?(?!\w)",
        re.I
    )
    MS_SPOKEN_RE = re.compile(
        rf"(?<!\d)(\d{{1,2}})\s*{M_UNITS}\s*(\d{{1,2}})\s*{S_UNITS}(?!\w)",
        re.I
    )

    PACE_QUOTES   = ("'", "’", "′", "“", "”", '"')
    NOISY_TOKENS  = ("pace", "bpm", "kcal", "steps", "avg hr", "average hr", "avg heart rate")
    label_idxs = _label_idxs(lines, TIME_LABEL, KEYWORDS["time"])

    def _is_datey_line(s: str) -> bool:
        low = (s or "").lower()
        return bool(DATE_SLASH_RE.search(s) or DATE_ISO_RE.search(s) or " be" in low or "พ.ศ" in low)

    def _is_noisy_line(s: str) -> bool:
        low = (s or "").lower()
        return any(t in low for t in NOISY_TOKENS)

    def _is_pace_like_around(s: str, start: int, end: int) -> bool:
        if s[max(0, start-1):start] in PACE_QUOTES or s[end:end+1] in PACE_QUOTES:
            return True
        if s[max(0, start-4):start].lower() == "pace":
            return True
        if s[end:end+4].lower() == "pace":
            return True
        return False

    # Phase 0: เวลาที่มี .ms ก่อน
    fract_cands: List[Tuple[str, int, int]] = []
    for j, s in enumerate(lines):
        if _is_datey_line(s):
            continue
        for m in FRACT_HHMMSS_RE.finditer(s):
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            h, mm, ss = map(int, m.groups()[:3])
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                fract_cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 4, j))
        for m in FRACT_MMSS_RE.finditer(s):
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            mm, ss = map(int, m.groups()[:2])
            if 0 <= mm <= 59 and 0 <= ss <= 59:
                fract_cands.append((f"00:{mm:02d}:{ss:02d}", 4, j))
    if fract_cands:
        def score_ms(hms: str, _kind: int, j: int) -> float:
            sc = 200.0
            if label_idxs:
                if any(abs(j - i) <= 2 for i in label_idxs):
                    sc += 120.0
                else:
                    dist = min(abs(j - i) for i in label_idxs)
                    sc += max(0.0, 60.0 - dist * 12.0)
            if j <= 2: sc -= 50.0
            if _is_noisy_line(lines[j]): sc -= 25.0
            return sc
        best = max(fract_cands, key=lambda t: score_ms(*t))
        return best[0]

    # Phase 1: candidate ปกติ
    cands: List[Tuple[str, int, int]] = []
    for j, s in enumerate(lines):
        if _is_datey_line(s):
            continue
        for m in HM_SPOKEN_RE.finditer(s):
            h = int(m.group(1)); mm = int(m.group(2)); ss = int(m.group(3)) if m.group(3) else 0
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 3, j))
        for m in MS_SPOKEN_RE.finditer(s):
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            mm = int(m.group(1)); ss = int(m.group(2))
            if 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"00:{mm:02d}:{ss:02d}", 3, j))
        for m in HHMMSS_RE.finditer(s):
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            h, mm, ss = map(int, m.groups()[:3])
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 3, j))
        for m in MIXED_HHMMSS_RE.finditer(s):
            token = s[m.start():m.end()]
            if ":." in token:
                continue
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            h = int(m.group("h")); mm = int(m.group("m")); ss = int(m.group("s"))
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 3, j))
    if not cands:
        for j, s in enumerate(lines):
            if _is_datey_line(s):
                continue
            for m in MMSS_RE.finditer(s):
                if _is_pace_like_around(s, m.start(), m.end()):
                    continue
                mm, ss = map(int, m.groups()[:2])
                if 0 <= mm <= 59 and 0 <= ss <= 59:
                    cands.append((f"00:{mm:02d}:{ss:02d}", 2, j))
    if not cands:
        return None

    def score(hms: str, kind: int, j: int) -> float:
        sc = 120.0 if kind == 3 else 60.0
        if label_idxs:
            if any(abs(j - i) <= 2 for i in label_idxs):
                sc += 120.0
            else:
                dist = min(abs(j - i) for i in label_idxs)
                sc += max(0.0, 60.0 - dist * 12.0)
        if j <= 2: sc -= 50.0
        if _is_noisy_line(lines[j]): sc -= 25.0
        return sc

    best = max(cands, key=lambda t: score(*t))
    return best[0]

def _find_pace_sec(lines: List[str]) -> Optional[int]:
    idxs = _label_idxs(lines, PACE_LABEL, KEYWORDS["pace"])
    for i in idxs:
        for j in range(1, 5):
            if i + j < len(lines):
                m = PACE_RE.search(lines[i + j])
                if m:
                    return int(m.group(1)) * 60 + int(m.group(2))
    m = PACE_RE.search(" ".join(lines))
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None

def _sec_from_timestr(t: Optional[str]) -> Optional[int]:
    if not t:
        return None
    h, mm, ss = map(int, t.split(":"))
    return h * 3600 + mm * 60 + ss

def _km_ok(v: float) -> bool:
    return 0.1 <= v <= 80.0

def parse_duration_and_km_smart(text: str) -> Tuple[Optional[str], Optional[float]]:
    lines = _norm_lines(text)

    time_hms = _find_time(lines)
    pace_sec = _find_pace_sec(lines)
    time_sec = _sec_from_timestr(time_hms) if time_hms else None

    if not time_hms:
        MIXED_HHMMSS_RE = re.compile(
            r"(?<![0-9A-Za-z])(?P<h>\d{1,2})\s*(?P<sep1>[:.])\s*(?P<m>\d{2})\s*(?P<sep2>[:.])\s*(?P<s>\d{2})(?!\.\d)"
        )
        for ln in lines:
            for m in MIXED_HHMMSS_RE.finditer(ln):
                sep1, sep2 = m.group("sep1"), m.group("sep2")
                if sep1 == ":" and sep2 == ".":
                    continue
                h = int(m.group("h")); mm_ = int(m.group("m")); ss_ = int(m.group("s"))
                if 0 <= h <= 1000 and 0 <= mm_ <= 59 and 0 <= ss_ <= 59:
                    time_hms = f"{h:02d}:{mm_:02d}:{ss_:02d}"
                    time_sec = _sec_from_timestr(time_hms)
                    break
            if time_hms:
                break

    if not time_hms:
        PACKED_TIME56_RE = re.compile(r"(?<![0-9A-Za-z.,:])(\d{5,6})(?![0-9A-Za-z.,:])")
        for ln in lines:
            for m in PACKED_TIME56_RE.finditer(ln):
                s = m.group(1)
                if len(s) == 6:
                    hh, mm_, ss_ = int(s[:2]), int(s[2:4]), int(s[4:6])
                else:
                    hh, mm_, ss_ = int(s[0]), int(s[1:3]), int(s[3:5])
                if 0 <= hh <= 1000 and 0 <= mm_ <= 59 and 0 <= ss_ <= 59:
                    time_hms = f"{hh:02d}:{mm_:02d}:{ss_:02d}"
                    time_sec = _sec_from_timestr(time_hms)
                    break
            if time_hms:
                break

    if not time_hms:
        PACKED_TIME78_RE = re.compile(r"(?<![0-9A-Za-z.,:])(\d{7,8})(?![0-9A-Za-z.,:])")
        for ln in lines:
            for m in PACKED_TIME78_RE.finditer(ln):
                s = m.group(1)
                if len(s) == 8:
                    hh, mm_, ss_ = int(s[:2]), int(s[2:4]), int(s[4:6])
                else:
                    hh, mm_, ss_ = int(s[0]), int(s[1:3]), int(s[3:5])
                if 0 <= hh <= 1000 and 0 <= mm_ <= 59 and 0 <= ss_ <= 59:
                    time_hms = f"{hh:02d}:{mm_:02d}:{ss_:02d}"
                    time_sec = _sec_from_timestr(time_hms)
                    break
            if time_hms:
                break

    # กัน speed เช่น km/h, กม/ชม, km/hr
    SPEED_AFTER_RE = r"(?:/\s*(?:h|hr|hour|ชม\.?|ชั่วโมง)\b)"
    unit_token_re = re.compile(
        rf"\b(?:k\s*m|km\.?|kilometers?|kilometres?|กิโลเมตร|กม\.?|กม)\b(?!\s*{SPEED_AFTER_RE})",
        re.I
    )
    dist_label_idxs = _label_idxs(lines, DIST_LABEL, KEYWORDS["distance"])
    anchor_lines = set(dist_label_idxs)
    for i, ln in enumerate(lines):
        if unit_token_re.search(ln):
            anchor_lines.add(i)

    candidates: List[Tuple[float, int]] = []

    KM_RE_NO_SPEED = re.compile(
        rf"\b(\d+(?:[.,]\d+)?)\s*(?:k\s*m|km\.?|kilometers?\.?|kilometres?\.?|กิโลเมตร|กม\.?|กม)\b(?!\s*{SPEED_AFTER_RE})",
        re.I
    )
    for i, ln in enumerate(lines):
        for m in KM_RE_NO_SPEED.finditer(ln):
            try:
                val = float(m.group(1).replace(",", "."))
                candidates.append((val, i))
            except Exception:
                pass

    SPEED_UNIT_AFTER = re.compile(
        rf"^\s*(?:k\s*m|km\.?|kilometers?|kilometres?|กิโลเมตร|กม\.?|กม)\b\s*{SPEED_AFTER_RE}",
        re.I
    )
    def _is_speed_value_after(line: str, end_idx: int) -> bool:
        return bool(SPEED_UNIT_AFTER.search(line[end_idx:]))

    for i in sorted(anchor_lines):
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines):
                ln = lines[j]
                for m in DECIMAL_RE.finditer(ln):
                    try:
                        if _is_speed_value_after(ln, m.end()):
                            continue
                        v = float(m.group(1).replace(",", "."))
                        if 0.1 <= v <= 100.0:
                            candidates.append((v, j))
                    except Exception:
                        pass

    SPACED_TWO_DEC_RE = re.compile(r"\b(\d+)\s*[.,]\s*(\d{2})\b")
    two_decimals_all: List[Tuple[float, int, str, Optional[Tuple[int,int]]]] = []

    for i, ln in enumerate(lines):
        seen_normals = set()
        for m in TWO_DEC_RE.finditer(ln):
            tok = m.group(0).replace(",", ".")
            if tok in seen_normals:
                continue
            if _is_speed_value_after(ln, m.end()):
                continue
            try:
                v = float(tok)
            except Exception:
                continue
            if 0.1 <= v <= 90.0:
                seen_normals.add(tok)
                mm_ss: Optional[Tuple[int,int]] = None
                if "." in tok:
                    mm_str, ss_str = tok.split(".", 1)
                    if mm_str.isdigit() and ss_str.isdigit():
                        mm, ss = int(mm_str), int(ss_str)
                        if 0 <= mm <= 59 and 0 <= ss <= 59:
                            mm_ss = (mm, ss)
                two_decimals_all.append((v, i, tok, mm_ss))
        for m in SPACED_TWO_DEC_RE.finditer(ln):
            if _is_speed_value_after(ln, m.end()):
                continue
            mm_str, ss_str = m.group(1), m.group(2)
            tok = f"{mm_str}.{ss_str}"
            if tok in seen_normals:
                continue
            try:
                v = float(tok)
            except Exception:
                continue
            if 0.1 <= v <= 90.0:
                seen_normals.add(tok)
                mm, ss = int(mm_str), int(ss_str)
                mm_ss: Optional[Tuple[int,int]] = None
                if 0 <= mm <= 59 and 0 <= ss <= 59:
                    mm_ss = (mm, ss)
                two_decimals_all.append((v, i, tok, mm_ss))

    # injection: มี pace + time → เลือก km ที่ใกล้ time/pace
    if pace_sec and time_sec and pace_sec > 0 and two_decimals_all:
        expect = time_sec / pace_sec
        best = None
        best_err = float("inf")
        for v, i, _tok, mmss in two_decimals_all:
            if mmss is not None:
                continue
            if not (0.2 <= v <= 80.0):
                continue
            err = abs(v - expect)
            if err < best_err:
                best_err = err
                best = (v, i)
        if best is not None:
            candidates.append(best)

    if not candidates:
        if len(two_decimals_all) >= 2:
            two_decimals_all_sorted = sorted(two_decimals_all, key=lambda x: x[0])
            v_small, i_small, _tok_small, _mmss_small = two_decimals_all_sorted[0]
            v_big,   i_big,   tok_big,   mmss_big    = two_decimals_all_sorted[-1]
            candidates.append((v_small, i_small))
            if not time_hms and mmss_big is not None:
                mm, ss = mmss_big
                time_hms = f"00:{mm:02d}:{ss:02d}"
                time_sec = _sec_from_timestr(time_hms)
        elif len(two_decimals_all) == 1:
            v1, i1, _tok1, _mmss1 = two_decimals_all[0]
            candidates.append((v1, i1))

    if not candidates:
        NUM34_RE = re.compile(r"(?<![0-9A-Za-z.,:])(\d{3,4})(?![0-9A-Za-z.,:])")
        for i, ln in enumerate(lines):
            nums = [m.group(1) for m in NUM34_RE.finditer(ln)]
            if len(nums) == 3:
                mid = nums[1]
                if mid.isdigit() and (3 <= len(mid) <= 4):
                    v = int(mid) / 100.0
                    if _km_ok(v):
                        candidates.append((v, i))
                        break

    PACKED_TIME3OR4_RE = re.compile(r"(?<![0-9A-Za-z.,:])(?P<n>\d{3,4})(?![0-9A-Za-z.,:])")
    PACKED_INT_3_RE    = re.compile(r"\b\d{3}\b")
    PACKED_INT_4_RE    = re.compile(r"\b\d{4}\b")

    def _time_from_3or4_digits(n: int) -> Optional[str]:
        if 100 <= n <= 999:
            m, ss = divmod(n, 100)
            if 0 <= m <= 59 and 0 <= ss <= 59:
                return f"00:{m:02d}:{ss:02d}"
        elif 1000 <= n <= 9999:
            mm, ss = divmod(n, 100)
            if 0 <= mm <= 59 and 0 <= ss <= 59:
                return f"00:{mm:02d}:{ss:02d}"
        return None

    maybe_time_hms = None
    n_lines = len(lines)
    have_regular_km = bool(candidates)

    if not time_hms and have_regular_km:
        best_sec = -1
        for i, ln in enumerate(lines):
            for m in PACKED_TIME3OR4_RE.finditer(ln):
                n_ = int(m.group("n"))
                hhmm = _time_from_3or4_digits(n_)
                if hhmm:
                    s = _sec_from_timestr(hhmm)
                    if s is not None and s > best_sec:
                        best_sec = s
                        maybe_time_hms = hhmm

    packed_km: List[Tuple[float, int]] = []
    have_regular_time = bool(time_hms or maybe_time_hms)
    if have_regular_time and not candidates:
        for i in sorted(anchor_lines):
            for j in (i - 1, i, i + 1):
                if 0 <= j < n_lines:
                    ln = lines[j]
                    if DECIMAL_RE.search(ln):
                        continue
                    for m in PACKED_INT_3_RE.finditer(ln):
                        v = int(m.group(0)) / 100.0
                        if _km_ok(v):
                            packed_km.append((v, j))
                    for m in PACKED_INT_4_RE.finditer(ln):
                        v = int(m.group(0)) / 100.0
                        if _km_ok(v):
                            packed_km.append((v, j))

    if (time_hms is None) and (maybe_time_hms is not None):
        time_hms = maybe_time_hms
        time_sec = _sec_from_timestr(time_hms)
    elif (time_hms is not None) and (not candidates) and packed_km:
        candidates.extend(packed_km)

    if (time_hms is None) and (not candidates):
        PACKED_34_CLEAN = re.compile(r"(?<![0-9A-Za-z.,:])(?P<n>\d{3,4})(?![%0-9A-Za-z.,:])")
        tokens = []
        for j, ln in enumerate(lines):
            for m in PACKED_34_CLEAN.finditer(ln):
                tokens.append((int(m.group('n')), j, m.start(), m.end()))
        uniq_vals = sorted(set(n for n,_,_,_ in tokens))
        if len(uniq_vals) == 2:
            small, big = uniq_vals[0], uniq_vals[1]
            t_big = _time_from_3or4_digits(big)
            if t_big:
                time_hms = t_big
                time_sec = _sec_from_timestr(time_hms)
            km_small = small / 100.0
            if _km_ok(km_small):
                candidates.append((km_small, -1))
        else:
            time_label_idxs = _label_idxs(lines, TIME_LABEL, KEYWORDS["time"])
            def _near_any(j: int, idxs, win: int) -> bool:
                return bool(idxs) and any(abs(j - i) <= win for i in idxs)
            time_bag = []
            dist_bag = []
            for n, j, s, e in tokens:
                t = _time_from_3or4_digits(n)
                if t and _near_any(j, time_label_idxs, 2):
                    d = min(abs(j - i) for i in time_label_idxs) if time_label_idxs else 99
                    time_bag.append((200 - d*60, t, j, (s, e)))
                if _near_any(j, list(anchor_lines), 1):
                    km = n / 100.0
                    if _km_ok(km) and not DECIMAL_RE.search(lines[j]):
                        d = min(abs(j - i) for i in anchor_lines) if anchor_lines else 99
                        dist_bag.append((200 - d*80, km, j, (s, e)))
            used = set()
            if time_bag:
                time_bag.sort(reverse=True)
                _sc, best_hms, tj, tsp = time_bag[0]
                time_hms = best_hms
                time_sec = _sec_from_timestr(time_hms)
                used.add((tj, tsp[0], tsp[1]))
            if dist_bag:
                dist_bag.sort(reverse=True)
                for _sc, km, dj, dsp in dist_bag:
                    key = (dj, dsp[0], dsp[1])
                    if key not in used:
                        candidates.append((km, dj))
                        break

    if not candidates:
        if time_hms:
            return time_hms, None
        return None, None

    def score_of(val: float, idx: int) -> float:
        if pace_sec and time_sec and pace_sec > 0:
            expect = time_sec / pace_sec
            if expect > 0:
                rel_err = abs(val - expect) / expect
                sc = 1000.0 * (1.0 - min(rel_err, 1.0))
                if dist_label_idxs:
                    dist = min(abs(idx - li) for li in dist_label_idxs)
                    sc += max(0.0, 20.0 - dist * 5.0)
                if 2.0 <= val <= 50.0:
                    sc += 2.0
                return sc
        sc = 0.0
        if dist_label_idxs:
            dist = min(abs(idx - li) for li in dist_label_idxs)
            sc += max(0.0, 100.0 - dist * 25.0)
        if 2.0 <= val <= 50.0:
            sc += 5.0
        return sc

    seen = set()
    uniq: List[Tuple[float, int]] = []
    for val, idx in candidates:
        key = (round(val, 3), idx)
        if key not in seen:
            seen.add(key)
            uniq.append((val, idx))
    candidates = uniq

    best_val, best_score = None, -1e9
    for val, idx in candidates:
        sc = score_of(val, idx)
        if sc > best_score:
            best_score, best_val = sc, val
    return time_hms, best_val

# ===== Smart Date Parser (returns M/D/YYYY) =====
from datetime import datetime, date, timedelta

_MONTHS = {
    # EN
    "january":1,"jan":1,"february":2,"feb":2,"march":3,"mar":3,"april":4,"apr":4,
    "may":5,"june":6,"jun":6,"july":7,"jul":7,"august":8,"aug":8,"september":9,"sep":9,"sept":9,
    "october":10,"oct":10,"november":11,"nov":11,"december":12,"dec":12,
    # TH (เต็ม/ย่อ)
    "มกราคม":1,"ม.ค.":1,"กุมภาพันธ์":2,"ก.พ.":2,"มีนาคม":3,"มี.ค.":3,"เมษายน":4,"เม.ย.":4,
    "พฤษภาคม":5,"พ.ค.":5,"มิถุนายน":6,"มิ.ย.":6,"กรกฎาคม":7,"ก.ค.":7,"สิงหาคม":8,"ส.ค.":8,
    "กันยายน":9,"ก.ย.":9,"ตุลาคม":10,"ต.ค.":10,"พฤศจิกายน":11,"พ.ย.":11,"ธันวาคม":12,"ธ.ค.":12,
}
_ORD = ("st","nd","rd","th")

_WEEKDAYS_TH = {"อา","จ","อ","พ","พฤ","ศ","ส","อาทิตย์","จันทร์","อังคาร","พุธ","พฤหัส","ศุกร์","เสาร์"}
_WEEKDAYS_EN = {"mon","monday","tue","tues","tuesday","wed","wednesday","thu","thur","thurs","thursday",
                "fri","friday","sat","saturday","sun","sunday"}

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$", re.I)

def _strip_ordinal(s: str) -> str:
    t = s.lower().strip().rstrip(",.")
    for suf in _ORD:
        if t.endswith(suf) and t[:-len(suf)].isdigit():
            return t[:-len(suf)]
    return t

def _as_int(x) -> int|None:
    try: return int(str(x))
    except: return None

def _year_fix(y: int) -> int:
    # รองรับ 2 หลัก & พ.ศ.
    if y < 100:         # 25 -> 2025, 99 -> 2099
        return 2000 + y
    if y > 2400:        # พ.ศ. -> ค.ศ.
        return y - 543
    return y

def _format_mdy_no_pad(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"

def _now_th_date() -> date:
    return (datetime.utcnow() + timedelta(hours=7)).date()

def _fmt(y, m, d):
    try:
        return f"{m}/{d}/{y}" if date(y, m, d) else None
    except Exception:
        return None

def _normalize(s: str) -> str:
    # แปลง "_" เป็นช่องว่าง, ลบตัวล่องหน, ลดสัญลักษณ์กวน, lower ทั้งก้อน
    return (s.replace("_", " ")
             .replace("\u200b", "")
             .replace("\u200f", "")
             .replace("·", " ").replace("•", " ").replace("@", " ")
             .replace(" ", " ").replace(" ", " ").lower())

def _tokenize(blob: str):
    # ใส่ @/bullet เป็น token ด้วย
    return re.findall(r"[A-Za-zก-๙\.]+|\d{1,4}|[@,•·/:\-]|BE|พ\.ศ\.", blob, re.I)

def _is_weekday(t: str) -> bool:
    tt = t.lower().strip().rstrip(".")
    return (tt in _WEEKDAYS_TH) or (tt in _WEEKDAYS_EN)

def _resolve_day_month(a: int, b: int, prefer_dayfirst: bool) -> tuple[int,int] | None:
    if not (1 <= a <= 31 and 1 <= b <= 31):
        return None
    if a > 12 and b <= 12:  # 21/9 -> D/M
        return (a, b)
    if a <= 12 and b > 12:  # 9/21 -> M/D
        return (b, a)
    return (a, b) if prefer_dayfirst else (b, a)

def _pick_year_after_month_tokens(tok: list[str], i_month: int, after_day_idx: int|None) -> tuple[Optional[int], bool]:
    """
    หาเฉพาะ 'ปี 4 หลัก' หรือ 'พ.ศ./BE + ปี 4 หลัก'
    ข้าม , . @ · • / ชื่อวัน เวลา และ AM/PM
    return (year_fixed, explicit_year?)
    """
    n = len(tok)
    j = (after_day_idx + 1) if after_day_idx is not None else (i_month + 1)
    steps = 0
    while j < n and steps < 5:
        t = tok[j]
        tl = t.lower().strip().rstrip(".")
        if t in {",", ".", "@", "•", "·", "/"} or (tl in _WEEKDAYS_EN) or (t in _WEEKDAYS_TH):
            j += 1; steps += 1; continue
        if _TIME_RE.match(t) or tl in {"am", "pm"}:
            j += 1; steps += 1; continue
        if re.fullmatch(r"\d{4}", t):
            return _year_fix(int(t)), True
        if tl in {"พ.ศ.", "be"} and j + 1 < n and re.fullmatch(r"\d{4}", tok[j+1]):
            return _year_fix(int(tok[j+1])), True
        break
    return None, False

TODAY_LIKE_RE = re.compile(
    r"\b(?:t\W*o\W*d\W*a\W*y|morning|afternoon|evening|tonight|night)\b",
    re.I
)

def _parse_smart_date_from_text(text: str, default_year: int|None=None) -> str|None:
    """
    คืนค่า 'M/D/YYYY' หรือ None
    ครอบคลุม:
      • today/วันนี้ (ทน \W*) + morning/afternoon/evening/tonight/night
      • BE/พ.ศ. ยอมรับ 'B E' (มีช่องว่าง) และ '_' คั่น
      • สองส่วน M/D หรือ D/M → เติมปีอัตโนมัติ
      • เดือน EN/TH (เต็ม/ย่อ), ปีต้อง 4 หลัก
      • ISO YYYY-MM-DD
      • มี scoring ให้ตัวที่ครบ/น่าเชื่อถือชนะ
    """
    if not text or not text.strip():
        return None

    prefer_dayfirst = True  # บริบทไทย

    # ---- 0) Normalize + Today ----
    norm = _normalize(text)
    if TODAY_LIKE_RE.search(norm) or ("วันนี้" in norm) or ("วันนี" in norm):
        return _format_mdy_no_pad(_now_th_date())

    lines = [ln.strip() for ln in norm.splitlines() if ln.strip()]
    blob  = " ".join(lines)

    EN_FULL = {"january","february","march","april","may","june","july","august","september","october","november","december"}
    TH_FULL = {"มกราคม","กุมภาพันธ์","มีนาคม","เมษายน","พฤษภาคม","มิถุนายน","กรกฎาคม","สิงหาคม","กันยายน","ตุลาคม","พฤศจิกายน","ธันวาคม"}
    cands = []

    def _add(y, m, d, flags):
        out = _fmt(y, m, d)
        if not out:
            return
        for it in cands:
            if it["y"]==y and it["m"]==m and it["d"]==d:
                it["flags"].update(flags)
                return
        cands.append({"y":y, "m":m, "d":d, "flags":set(flags)})

    # 1) YYYY sep MM sep DD
    m = re.search(r"(?<!\d)(20\d{2})\s*([\/\-.])\s*(\d{1,2})\s*\2\s*(\d{1,2})(?:\b|[^0-9])", blob)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(3)), int(m.group(4))
        _add(y, mo, dd, {"has_year","year_four","month_numeric","numeric_sep","pattern_y_m_d"})

    # 2) D/M/Y or M/D/Y (+ BE/พ.ศ.)
    m = re.search(r"(?<!\d)(\d{1,2})\s*([\/\-.])\s*(\d{1,2})\s*\2\s*(\d{2,4})\s*(?:b\s*e|พ\.ศ\.)?\b", blob, re.I)
    if m:
        a, b, yraw = int(m.group(1)), int(m.group(3)), int(m.group(4))
        y = _year_fix(yraw)
        dm = _resolve_day_month(a, b, prefer_dayfirst)
        if dm:
            dd, mo = dm
            flags = {"has_year","month_numeric","numeric_sep","pattern_dmy_or_mdy"}
            flags.add("year_two" if yraw<100 else "year_four")
            _add(y, mo, dd, flags)

    # 2.5) two-part M/D หรือ D/M (ไม่มีปี)
    m = re.search(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\s*[\/\-.]\s*\d)", blob)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        dm = _resolve_day_month(a, b, prefer_dayfirst)
        if dm:
            dd, mo = dm
            yy = default_year or _now_th_date().year
            _add(yy, mo, dd, {"two_part","inferred_year","month_numeric","numeric_sep"})

    # 3) Month name (EN/TH)
    tok = _tokenize(blob)
    n = len(tok)
    for i in range(n):
        w = tok[i]; wl = w.lower().strip(); wl2 = wl.rstrip(".")
        if wl not in _MONTHS and wl2 not in _MONTHS:
            continue
        mm = _MONTHS[wl] if wl in _MONTHS else _MONTHS[wl2]
        month_full = (wl in EN_FULL or wl in TH_FULL or wl2 in TH_FULL)

        # A) Day Month [Year]
        dd = None
        if i-1 >= 0:
            t1 = tok[i-1]
            t1s = _strip_ordinal(t1)
            if _as_int(t1s) is not None:
                dd = _as_int(t1s)
            elif t1 in {",","."} and i-2 >= 0 and _as_int(_strip_ordinal(tok[i-2])) is not None:
                dd = _as_int(_strip_ordinal(tok[i-2]))
            elif (t1.lower().strip().rstrip(".") in _WEEKDAYS_EN or t1 in _WEEKDAYS_TH) and i-2 >= 0 and _as_int(_strip_ordinal(tok[i-2])) is not None:
                dd = _as_int(_strip_ordinal(tok[i-2]))
        if dd is not None and 1 <= dd <= 31:
            y_found, explicit = _pick_year_after_month_tokens(tok, i, None)
            if not y_found:
                y_found = default_year or _now_th_date().year
            flags = {"from_monthname"}
            flags.add("month_name_full" if month_full else "month_name_abbr")
            if explicit: flags.add("has_year")
            else:        flags.add("inferred_year")
            _add(y_found, mm, dd, flags)

        # B) Month Day [Year]
        day_idx = None; dd2 = None
        if i+1 < n:
            tday = _strip_ordinal(tok[i+1])
            if _as_int(tday) is not None:
                dd2 = _as_int(tday); day_idx = i+1
            elif tok[i+1] in {",","."} and i+2 < n:
                tday2 = _strip_ordinal(tok[i+2])
                if _as_int(tday2) is not None:
                    dd2 = _as_int(tday2); day_idx = i+2
        if dd2 is not None and 1 <= dd2 <= 31:
            y_found, explicit = _pick_year_after_month_tokens(tok, i, day_idx)
            if not y_found:
                y_found = default_year or _now_th_date().year
            flags = {"from_monthname"}
            flags.add("month_name_full" if month_full else "month_name_abbr")
            if explicit: flags.add("has_year")
            else:        flags.add("inferred_year")
            _add(y_found, mm, dd2, flags)

    # 4) ISO
    m = re.search(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", blob)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        _add(y, mo, dd, {"has_year","year_four","iso","month_numeric"})

    if not cands:
        return None

    # Scoring
    def _score(it):
        flags = it["flags"]
        sc = 0.0
        if "has_year" in flags:       sc += 100
        if "year_four" in flags:      sc += 25
        if "year_two" in flags:       sc -= 10
        if "inferred_year" in flags:  sc -= 35

        if "month_name_full" in flags: sc += 70
        if "month_name_abbr" in flags: sc += 50
        if "from_monthname" in flags:  sc += 10
        if "month_numeric" in flags:   sc += 20

        if "iso" in flags:             sc += 80
        if "numeric_sep" in flags:     sc += 10
        if "two_part" in flags:        sc += 15

        if "pattern_y_m_d" in flags:        sc += 15
        if "pattern_dmy_or_mdy" in flags:   sc += 10
        return sc

    best = max(cands, key=_score)
    return f"{best['m']}/{best['d']}/{best['y']}"

def parse_duration_km_date_smart(text: str, default_year: int|None=None):
    dur, dist = parse_duration_and_km_smart(text)
    date_str = _parse_smart_date_from_text(text, default_year=default_year)
    return dur, dist, date_str

# ---------------- Helpers: run-type & thresholds ----------------
def _where_category(s: str) -> Optional[str]:
    s = (s or "").strip().lower()
    if any(k in s for k in OUTDOOR_KEYS):
        return "outdoor"
    if any(k in s for k in INDOOR_KEYS):
        return "indoor"
    return None

def thr_hms_to_sec(hms: str) -> int:
    h, m, s = map(int, hms.split(":"))
    return h*3600 + m*60 + s

def _to_float(x) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", ".")
        return float(s)
    except Exception:
        return None

def is_small_distance_km(dist) -> bool:
    v = _to_float(dist)
    return (v is not None) and (v < DIST_MIN_KM)

# ---------------- Window helpers ----------------
def _parse_iso(ts: str) -> dt.datetime:
    if ts is None:
        raise ValueError("empty timestamp")
    s = str(ts).strip()

    # Google Sheets serial number
    if re.fullmatch(r"\d+(\.\d+)?", s):
        base = dt.datetime(1899, 12, 30, tzinfo=dt.timezone(dt.timedelta(hours=LOCAL_TZ_OFFSET_HOURS)))
        serial = float(s)
        days = int(serial)
        frac = serial - days
        seconds = round(frac * 86400)
        return base + dt.timedelta(days=days, seconds=seconds)

    # ISO8601 / "YYYY-mm-dd HH:MM[:SS]"
    try:
        iso = s.replace(" ", "T")
        if iso.endswith("Z"):
            return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt_obj = dt.datetime.fromisoformat(iso)
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=dt.timezone(dt.timedelta(hours=LOCAL_TZ_OFFSET_HOURS)))
        return dt_obj
    except Exception:
        pass

    # m/d/Y or d/m/Y
    patterns = [
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]
    for p in patterns:
        try:
            dt_naive = dt.datetime.strptime(s, p)
            return dt_naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=LOCAL_TZ_OFFSET_HOURS)))
        except Exception:
            continue

    raise ValueError(f"unrecognized timestamp format: {s}")

def _row_in_window(row_ts: str, start: dt.datetime, end: dt.datetime) -> bool:
    try:
        t = _parse_iso(row_ts)
        return (t >= start) and (t <= end)
    except Exception:
        return False

# ---------------- Core backfill + detect ----------------
def run_backfill_window(start_iso: str, end_iso: str) -> dict:
    t0 = time.monotonic()
    sheets, drive = _build_services()

    # RAW
    raw_vals = _get_values(sheets, RAW_RANGE)
    if not raw_vals:
        return {"result": "success", "detail": "no raw", "duration_sec": round(time.monotonic()-t0, 3)}
    raw_header, raw_rows = raw_vals[0], raw_vals[1:]

    # WORK ensure exists
    _ensure_sheet_exists(sheets, SHEET_NAME_WORK)
    work_vals = _get_values(sheets, WORK_RANGE)
    if not work_vals:
        # create header only
        work_header = list(raw_header)
        for col in [STATUS_COL, DIST_COL, DUR_COL, IN_STATUS_COL, DIGI_DIST_COL, DIGI_DUR_COL, MACH_DIST_COL, MACH_DUR_COL, PHOTO_DATE_COL]:
            _ensure_col(work_header, [], col)
        _update_values(sheets, f"{SHEET_NAME_WORK}!A1", [work_header])
        work_vals = _get_values(sheets, WORK_RANGE)

    work_header, work_rows = work_vals[0], work_vals[1:]
    _pad_all_rows(work_rows, len(work_header))

    # ensure result cols in header & rows
    header_before = list(work_header)
    for col in [STATUS_COL, DIST_COL, DUR_COL, IN_STATUS_COL, DIGI_DIST_COL, DIGI_DUR_COL, MACH_DIST_COL, MACH_DUR_COL, PHOTO_DATE_COL]:
        _ensure_col(work_header, work_rows, col)
    if work_header != header_before:
        _update_values(sheets, f"{SHEET_NAME_WORK}!A1", [work_header])
        work_vals = _get_values(sheets, WORK_RANGE)
        work_header, work_rows = work_vals[0], work_vals[1:]
        _pad_all_rows(work_rows, len(work_header))

    # indexes
    idx_ts_raw   = _idx(raw_header,  TIMESTAMP_COL_NAME)
    idx_ts_work  = _idx(work_header, TIMESTAMP_COL_NAME)
    if idx_ts_raw is None or idx_ts_work is None:
        raise RuntimeError(f"Missing '{TIMESTAMP_COL_NAME}' in RAW or WORK.")

    idx_img    = _idx(work_header, IMAGE_COL_NAME)
    idx_selfie = _idx(work_header, SELFIE_COL_NAME)
    idx_where  = _idx(work_header, WHERE_COL_NAME)
    idx_digi   = _idx(work_header, INDOOR_DIGI_COL)
    idx_mach   = _idx(work_header, INDOOR_MACH_COL)
    if any(v is None for v in [idx_img, idx_where, idx_digi, idx_mach]):
        raise RuntimeError("Missing required image/where columns in Working sheet.")

    idx_sta   = _idx(work_header, STATUS_COL)
    idx_dist  = _idx(work_header, DIST_COL)
    idx_dur   = _idx(work_header, DUR_COL)
    idx_insta = _idx(work_header, IN_STATUS_COL)
    idx_ddist = _idx(work_header, DIGI_DIST_COL)
    idx_ddur  = _idx(work_header, DIGI_DUR_COL)
    idx_mdist = _idx(work_header, MACH_DIST_COL)
    idx_mdur  = _idx(work_header, MACH_DUR_COL)
    idx_photo_date = _idx(work_header, PHOTO_DATE_COL)

    # window
    start_dt = _parse_iso(start_iso)
    end_dt   = _parse_iso(end_iso)

    # RAW rows in window
    raw_in_window = []
    for r in raw_rows:
        ts = get_cell(r, idx_ts_raw)
        if _row_in_window(ts, start_dt, end_dt):
            raw_in_window.append(r)

    # existing keys in WORK (by timestamp only)
    def key_of_row(header: List[str], row: List[str]) -> str:
        return get_cell(row, _idx(header, TIMESTAMP_COL_NAME))

    work_keys_in_window = set()
    for r in work_rows:
        ts = get_cell(r, idx_ts_work)
        if _row_in_window(ts, start_dt, end_dt):
            work_keys_in_window.add(key_of_row(work_header, r))

    # append missing rows
    to_append = []
    for r in raw_in_window:
        key = key_of_row(raw_header, r)
        if key not in work_keys_in_window:
            to_append.append(_pad_row(list(r), len(work_header)))

    if to_append:
        _append_values(sheets, WORK_RANGE, to_append)
        work_vals = _get_values(sheets, WORK_RANGE)
        work_header, work_rows = work_vals[0], work_vals[1:]
        _pad_all_rows(work_rows, len(work_header))
        # re-index after append
        idx_ts_work  = _idx(work_header, TIMESTAMP_COL_NAME)
        idx_img      = _idx(work_header, IMAGE_COL_NAME)
        idx_selfie   = _idx(work_header, SELFIE_COL_NAME)
        idx_where    = _idx(work_header, WHERE_COL_NAME)
        idx_digi     = _idx(work_header, INDOOR_DIGI_COL)
        idx_mach     = _idx(work_header, INDOOR_MACH_COL)
        idx_sta      = _idx(work_header, STATUS_COL)
        idx_dist     = _idx(work_header, DIST_COL)
        idx_dur      = _idx(work_header, DUR_COL)
        idx_insta    = _idx(work_header, IN_STATUS_COL)
        idx_ddist    = _idx(work_header, DIGI_DIST_COL)
        idx_ddur     = _idx(work_header, DIGI_DUR_COL)
        idx_mdist    = _idx(work_header, MACH_DIST_COL)
        idx_mdur     = _idx(work_header, MACH_DUR_COL)
        idx_photo_date = _idx(work_header, PHOTO_DATE_COL)

    # targets: in-window AND Out_Status=="" AND In_Status=="" (ถือว่า NG เป็นสถานะแล้ว)
    targets = []
    for i, r in enumerate(work_rows):
        ts = get_cell(r, idx_ts_work)
        if not _row_in_window(ts, start_dt, end_dt):
            continue
        out_empty = not (get_cell(r, idx_sta).strip())
        in_empty  = not (get_cell(r, idx_insta).strip())
        if out_empty and in_empty:
            targets.append(i)

    # OCR + parse แบบเดียวกับ main
    def ocr_and_parse_safe(cell_text: str) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[str]]:
        """
        return: (duration_hms, distance_km, shot_date_mdy, ng_reason)
        - ถ้า non-image/วิดีโอ/รูปพัง/Vision error → ng_reason ไม่ว่าง
        """
        file_ids = _file_ids_from_cell(cell_text)
        if not file_ids:
            return None, None, None, None

        pieces: List[str] = []
        for fid in file_ids:
            content, filename, mime = _download_bytes_and_meta(drive, fid)
            status, reason, text = ocr_image_bytes_safe(content, filename, mime)
            if status == "NG":
                return None, None, None, reason or "non-image"
            if text:
                pieces.append(text)

        text_all = "\n\n---\n\n".join(pieces)
        if not text_all.strip():
            return None, None, None, None

        default_year = dt.datetime.now(dt.timezone(dt.timedelta(hours=LOCAL_TZ_OFFSET_HOURS))).year
        dur, dist, date_str = parse_duration_km_date_smart(text_all, default_year=default_year)
        return dur, dist, date_str, None

    def success(d, k) -> bool:
        return (d is not None) and (k is not None)

    # comparator ปลอดภัยแบบ main
    def _is_time_over_safe(hms: Optional[str]) -> bool:
        if not hms:
            return False
        t = _sec_from_timestr(hms)
        return (t is not None) and (t > thr_hms_to_sec(TIME_OVER_HMS))

    batch_updates = []

    for i in targets:
        r = work_rows[i]
        where_val = get_cell(r, idx_where).strip()
        cat = _where_category(where_val)
        if cat is None:
            raise RuntimeError(f"Unknown value in '{WHERE_COL_NAME}' at working row {i+2}: {where_val!r}")

        changed = False

        if cat == "outdoor":
            out_dur, out_dist, shot_date, ng_reason = ocr_and_parse_safe(get_cell(r, idx_img))

            if ng_reason:  # ช่องหลัก non-image/วิดีโอ/รูปพัง → NG
                r[idx_dur]  = ""
                r[idx_dist] = ""
                r[idx_sta]  = "NG"
                if shot_date:
                    r[idx_photo_date] = shot_date
                changed = True
            else:
                if shot_date:
                    r[idx_photo_date] = shot_date

                if success(out_dur, out_dist):
                    r[idx_dur]  = out_dur
                    r[idx_dist] = out_dist
                    r[idx_sta]  = "OK"
                    changed = True
                else:
                    if idx_selfie is not None:
                        s_dur, s_dist, s_date, ng2 = ocr_and_parse_safe(get_cell(r, idx_selfie))
                        if ng2:
                            r[idx_dur]  = ""
                            r[idx_dist] = ""
                            r[idx_sta]  = "NG"
                            if s_date:
                                r[idx_photo_date] = s_date
                            changed = True
                        elif success(s_dur, s_dist):
                            r[idx_dur]  = s_dur
                            r[idx_dist] = s_dist
                            r[idx_sta]  = "Miss box"
                            if s_date:
                                r[idx_photo_date] = s_date
                            changed = True
                        else:
                            r[idx_dur]  = ""
                            r[idx_dist] = ""
                            r[idx_sta]  = "NG"
                            changed = True
                    else:
                        r[idx_dur]  = ""
                        r[idx_dist] = ""
                        r[idx_sta]  = "NG"
                        changed = True

            # precedence overrides เหมือน main (ทำเฉพาะเมื่อเริ่มต้นเป็น OK)
            initial_status = (get_cell(r, idx_sta) or "").strip()
            if initial_status == "OK":
                new_status = None
                if is_small_distance_km(get_cell(r, idx_dist)) and _is_time_over_safe(get_cell(r, idx_dur)):
                    new_status = STATUS_COND_INSUFF
                elif is_small_distance_km(get_cell(r, idx_dist)):
                    new_status = STATUS_DIST_INSUFF
                elif _is_time_over_safe(get_cell(r, idx_dur)):
                    new_status = "Time Over"
                if new_status and new_status != initial_status:
                    r[idx_sta] = new_status
                    changed = True

        else:  # indoor
            digi_dur,  digi_dist, digi_date,  ng_digi  = ocr_and_parse_safe(get_cell(r, idx_digi))
            mach_dur,  mach_dist, mach_date,  ng_mach  = ocr_and_parse_safe(get_cell(r, idx_mach))

            r[idx_ddur]  = digi_dur or ""
            r[idx_ddist] = digi_dist if (digi_dist is not None) else ""
            r[idx_mdur]  = mach_dur or ""
            r[idx_mdist] = mach_dist if (mach_dist is not None) else ""
            if digi_date:
                r[idx_photo_date] = digi_date
            changed = True

            if ng_digi or ng_mach:
                r[idx_insta] = "NG"
            else:
                has_digi_dur  = bool(digi_dur)
                has_mach_dur  = bool(mach_dur)
                has_mach_dist = (mach_dist is not None)

                if not (has_digi_dur and has_mach_dur and has_mach_dist):
                    r[idx_insta] = "NG"
                else:
                    small_by_machine_only = is_small_distance_km(mach_dist)
                    over_both  = _is_time_over_safe(digi_dur) and _is_time_over_safe(mach_dur)

                    if small_by_machine_only and over_both:
                        r[idx_insta] = STATUS_COND_INSUFF
                    elif small_by_machine_only:
                        r[idx_insta] = STATUS_DIST_INSUFF
                    elif over_both:
                        r[idx_insta] = "Time Over"
                    else:
                        r[idx_insta] = "OK"

        if changed:
            batch_updates.append({
                "range": _range_for_row(SHEET_NAME_WORK, i + 2, len(work_header)),
                "values": [_pad_row(r, len(work_header))]
            })

    if batch_updates:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "RAW", "data": batch_updates},
        ).execute()

    return {
        "result": "success",
        "window": {"from": start_iso, "to": end_iso},
        "updated_rows": len(batch_updates),
        "duration_sec": round(time.monotonic() - t0, 3),
    }

# ---------------- HTTP entry ----------------
def backfill_window_http(request: Request):
    try:
        summary = run_backfill_window(DEFAULT_FROM, DEFAULT_TO)
        return make_response((summary, 200))
    except HttpError as e:
        try:
            detail = e.content.decode() if hasattr(e, "content") else str(e)
        except Exception:
            detail = str(e)
        return make_response(({"result": "error", "reason": detail}, 500))
    except Exception as e:
        return make_response(({"result": "error", "reason": str(e)}, 500))
