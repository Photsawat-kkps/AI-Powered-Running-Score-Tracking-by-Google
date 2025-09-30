# -*- coding: utf-8 -*-
"""
OCR Google Form uploads -> write result into Google Sheet (Working Sheet).

Workflow:
1) อ่านจากแท็บดิบ (Form Responses 1)
2) ถ้าแท็บทำงานยังไม่มี/ว่าง: สร้าง + เขียน "หัวตาราง + แถวข้อมูลทั้งหมด" แล้ว OCR "ทุกแถว" ครั้งแรก
3) ครั้งถัดไป:
   - มีแถวใหม่ -> คัดลอกเฉพาะแถวใหม่จากดิบ -> ทำ OCR เฉพาะ "แถวใหม่"
   - ไม่มีแถวใหม่ -> backfill เฉพาะแถวที่ Out_Status และ In_Status ยังค่าว่าง (ยังไม่เคยตัดสินผล)

Run-type switch:

- Outdoor:
  • ใช้ภาพหลัก (Outdoor smartwatch/mobile). ถ้าไม่เจอ ค่อยลอง Selfie
  • เขียน Out_Distance_km / Out_Duration_hms / Out_Status
  • ลำดับสรุปสถานะ (กันทับกัน):
      1) ระยะ < 2.00 และ เวลา > 02:00:00 → "All Condition Insufficient"
      2) ระยะ < 2.00 → "Distance Insufficient"
      3) เวลา > 02:00:00 → "Time Over"
      4) ไม่เข้าเงื่อนไขข้างบน → คง OK / Miss box / NG ตามเดิม

- Indoor:
  • จากภาพ Indoor (smartwatch/mobile) → digi_distance_km / digi_duration_hms
  • จากภาพเครื่องออกกำลังกาย → mach_distance_km / mach_duration_hms
  • In_Status:
      - ถ้าขาดข้อมูลช่องใดช่องหนึ่งใน {digi_duration_hms, mach_distance_km, mach_duration_hms} → "NG"
      - ถ้ามีครบทั้ง 3 ช่องแล้ว ให้ตัดสินตามลำดับ:
          1) (digi_dist < 2.00 และ mach_dist < 2.00) และ (digi_dur > 02:00:00 และ mach_dur > 02:00:00)
             → "All Condition Insufficient"
          2) (digi_dist < 2.00 และ mach_dist < 2.00) → "Distance Insufficient"
          3) (digi_dur > 02:00:00 และ mach_dur > 02:00:00) → "Time Over"
          4) นอกนั้น → "OK"

Idempotent:
- รอบแรก = ทุกแถว, รอบถัดไป = เฉพาะแถวใหม่ หรือแถวที่ยังไม่มีผลลัพธ์ (backfill)
"""

import os
import io
import re
import time
import logging
import datetime as dt
from typing import List, Optional, Tuple
from PIL import Image, UnidentifiedImageError

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
from google.cloud import vision
from google.cloud import logging as cloud_logging  # structured logging (Cloud Run)

# ---- Logging (1 line per run) ----
try:
    cloud_logging.Client().setup_logging()
except Exception:
    logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ocrsheet")
logger.setLevel(logging.INFO)

LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "7"))

# ========= CONFIG =========
SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID", "1-ht6PyQtynMG-dMpSGM4I3sVr7HBhb8y33xwl_QZvVA")
SHEET_NAME_RAW   = os.getenv("SHEET_NAME", "Form Responses 1")   # Raw tab
SHEET_NAME_WORK  = os.getenv("WORK_SHEET_NAME", f"{SHEET_NAME_RAW} (Working)")  # Working tab

# --- Outdoor image columns (prod names) ---
IMAGE_COL_NAME   = os.getenv("IMAGE_COL_NAME", "รูปถ่ายแสดงระยะทาง Outdoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)")
SELFIE_COL_NAME  = os.getenv("SELFIE_COL_NAME", "รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Outdoor (Selfie)")

# --- Indoor image columns (source) ---
INDOOR_DIGI_COL  = os.getenv("INDOOR_DIGI_COL", "รูปถ่ายแสดงระยะทาง Indoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)")
INDOOR_MACH_COL  = os.getenv("INDOOR_MACH_COL", "รูปถ่ายระยะทางจากเครื่องออกกำลังกาย (Photo of the distance display from the exercise machine.)")

# --- where/run-type column + keys ---
WHERE_COL_NAME   = os.getenv("WHERE_COL_NAME", "ลักษณะสถานที่วิ่ง (Where did you run?)")
OUTDOOR_KEYS     = ["กลางแจ้ง", "นอกบ้าน", "outdoor"]
INDOOR_KEYS      = ["ในร่ม", "indoor"]

# --- Date result column ---
PHOTO_DATE_COL = os.getenv("PHOTO_DATE_COL", "Shot_Date")

# --- Outdoor result columns (prod names) ---
STATUS_COL = os.getenv("STATUS_COL", "Out_Status")
DIST_COL   = os.getenv("DIST_COL", "Out_Distance_km")
DUR_COL    = os.getenv("DUR_COL", "Out_Duration_hms")   # HH:MM:SS

# --- Indoor result columns (new) ---
IN_STATUS_COL = os.getenv("IN_STATUS_COL", "In_Status")
DIGI_DIST_COL = os.getenv("DIGI_DIST_COL", "digi_distance_km")
DIGI_DUR_COL  = os.getenv("DIGI_DUR_COL",  "digi_duration_hms")
MACH_DIST_COL = os.getenv("MACH_DIST_COL", "mach_distance_km")
MACH_DUR_COL  = os.getenv("MACH_DUR_COL",  "mach_duration_hms")

# ranges (เผื่อถึง AZ)
RAW_RANGE  = os.getenv("RAW_RANGE",  f"{SHEET_NAME_RAW}!A:AZ")
WORK_RANGE = os.getenv("WORK_RANGE", f"{SHEET_NAME_WORK}!A:AZ")

# thresholds & new labels
TIME_OVER_HMS = os.getenv("TIME_OVER_HMS", "02:00:00")
DIST_MIN_KM   = float(os.getenv("DIST_MIN_KM", "2.0"))  # < 2.00 km
STATUS_COND_INSUFF = "All Condition Insufficient"
STATUS_DIST_INSUFF = "Distance Insufficient"
# =================================================

#-----------Only Photo files--------------
# ========= Helpers =========
ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "image/tiff", "image/bmp"  # เพิ่มได้ตามที่คุณรองรับจริง
}
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}

def looks_like_image_by_meta(filename: str, content_type: Optional[str]) -> bool:
    fn = (filename or "").lower()
    if content_type and content_type.startswith("image/"):
        return True
    if content_type in ALLOWED_IMAGE_MIMES:
        return True
    # กันกรณี content-type ว่าง: ใช้สกุลไฟล์ช่วยตัดสินคร่าว ๆ
    return any(fn.endswith(ext) for ext in ALLOWED_EXTS)

def bytes_is_valid_image(data: bytes) -> bool:
    # เปิดรูปแบบไม่โหลดทั้งภาพ เพื่อเช็คว่า “เป็นไฟล์รูปจริงไหม”
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()   # ถ้าไฟล์พังจะโยน error
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False

# ========= Main OCR wrapper =========
def ocr_image_bytes_safe(
    data: bytes,
    filename: str,
    content_type: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    """
    Return: (status, reason, text)
      - status: "OK" | "NG"
      - reason: สาเหตุถ้า NG (เช่น "non-image", "corrupt/bad image data", "vision-error")
      - text:   ผลลัพธ์ OCR ถ้า OK; otherwise None
    """
    # ชั้นที่ 1: เช็ค metadata
    if not looks_like_image_by_meta(filename, content_type):
        return "NG", "non-image", None

    # ชั้นที่ 2: เช็ค byte เป็นรูปได้จริงไหม
    if not bytes_is_valid_image(data):
        return "NG", "corrupt/bad image data", None

    # ชั้นที่ 3: เรียก Vision ใน try/except
    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=data)
    try:
        resp = client.text_detection(image=image)
        if resp.error.message:
            # Vision บอก error ชัดเจน
            return "NG", f"vision-error: {resp.error.message}", None

        # รวมข้อความหลัก (แบบง่าย)
        text = (resp.full_text_annotation.text or "").strip() if resp.full_text_annotation else ""
        return ("OK", "", text) if text else ("OK", "", "")
    except Exception as e:
        # กันตก: ไม่ให้พังทั้งแถว
        return "NG", f"vision-exception: {e.__class__.__name__}", None

# ---------- Google API clients ----------
def _build_services():
    creds, _ = google.auth.default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive  = build("drive",  "v3", credentials=creds, cache_discovery=False)
    vcli   = vision.ImageAnnotatorClient()
    return sheets, drive, vcli


# ---------- Sheets helpers ----------
def _get_values(sheets, a1: str) -> List[List[str]]:
    return sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=a1
    ).execute().get("values", [])

def _update_values(sheets, a1: str, values: List[List[str]]):
    return sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=a1,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()

def _append_values(sheets, a1: str, values: List[List[str]]):
    return sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=a1,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()

def _list_sheet_titles(sheets) -> List[str]:
    meta = sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return [sh["properties"]["title"] for sh in meta.get("sheets", [])]

def _ensure_sheet_exists(sheets, title: str):
    if title in _list_sheet_titles(sheets):
        return
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()

# --- Sort RAW by timestamp helpers ---
TS_HEADER_RE = re.compile(
    r"(timestamp|เวลาประทับ|ประทับเวลา|date.?time|datetime|time\s*submitted|เวลาที่ส่ง)",
    re.I
)

def _sheet_props_by_title(sheets, spreadsheet_id: str, title: str):
    """Return (sheetId, rowCount, columnCount) for a sheet title."""
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sh in meta.get("sheets", []):
        if sh["properties"]["title"] == title:
            p = sh["properties"].get("gridProperties", {})
            return sh["properties"]["sheetId"], p.get("rowCount", 100000), p.get("columnCount", 26)
    return None, None, None

def _find_timestamp_idx(header: List[str]) -> int:
    """Find timestamp column index from header; fallback to first column if not found."""
    for i, h in enumerate(header or []):
        if TS_HEADER_RE.search(h or ""):
            return i
    return 0  # fallback: first column

def _sort_raw_by_timestamp(sheets, header: List[str], ascending: bool = True):
    """Sort the RAW sheet by timestamp column (skip header row)."""
    sheet_id, _rows, cols = _sheet_props_by_title(sheets, SPREADSHEET_ID, SHEET_NAME_RAW)
    if sheet_id is None:
        return
    ts_idx = _find_timestamp_idx(header)
    body = {
        "requests": [{
            "sortRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,        # skip header
                    "startColumnIndex": 0,
                    "endColumnIndex": cols
                },
                "sortSpecs": [{
                    "dimensionIndex": ts_idx,
                    "sortOrder": "ASCENDING" if ascending else "DESCENDING"
                }]
            }
        }]
    }
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body=body
    ).execute()


def _idx(header: List[str], name: str) -> Optional[int]:
    name = (name or "").lower().strip()
    for i, h in enumerate(header):
        if (h or "").lower().strip() == name:
            return i
    return None

def _ensure_col(header: List[str], rows: List[List[str]], name: str) -> int:
    """Ensure column exists; if not, append to header and every row."""
    i = _idx(header, name)
    if i is None:
        header.append(name)
        i = len(header) - 1
        for r in rows:
            r.append("")
    return i

def _pad_row(row: List[str], target_len: int) -> List[str]:
    if len(row) < target_len:
        return row + [""] * (target_len - len(row))
    return row[:target_len]

def _col_letter(n: int) -> str:
    s = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        s.append(chr(65 + r))
    return "".join(reversed(s))

def _range_for_row(sheet_name: str, row_1based: int, num_cols: int) -> str:
    last_col = _col_letter(num_cols)
    return f"{sheet_name}!A{row_1based}:{last_col}{row_1based}"


# ---------- Drive helpers ----------
def _file_ids_from_cell(cell: str) -> List[str]:
    # รองรับ comma/space/newline และแพทเทิร์น /d/<id> หรือ ?id=<id>
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
    """
    return: (content_bytes, filename, mime_type)
    """
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

# ---------- Smart parsers ----------
DIST_LABEL  = re.compile(r"^\s*distance\s*$", re.I)
TIME_LABEL  = re.compile(r"^\s*elapsed\s*time\s*$", re.I)
PACE_LABEL  = re.compile(r"^\s*(avg(?:\.|erage)?\s*)?pace\s*$", re.I)

KM_RE       = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:k\s*m|km\.?|kilometers?\.?|Kilometers?\.?|กม\.?|กม|กิโลเมตร\.?)\b", re.I)
TIME_ANY_RE = re.compile(
    r"\b(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,]\d{1,3})?\b(?!\s*(?:AM|PM)\b)"  # อนุญาต .ms ต่อท้าย HH:MM:SS
    r"|"
    r"\b(\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\b(?!\s*(?:AM|PM)\b)",                   # MM:SS(.ms)
    re.I
)
PACE_RE = re.compile(
    r"(\d{1,2})[:'’](\d{2})\s*"                         # 24:07 หรือ 24'07
    r"(?:(?:min|mins|minute|minutes|นาที|น\.)\s*)?"     # อนุญาตมี/ไม่มีตัวบอก "นาที"
    r"/\s*"                                             # เครื่องหมาย /
    r"(?:k\s*m|km|kilometers?|kilometres?|กิโลเมตร|กม\.?|กม)\b",  # หน่วยระยะทาง (รวม "k m")
    re.I
)
DECIMAL_RE   = re.compile(r"\b(\d+[.,]\d+)\b")
TWO_DEC_RE   = re.compile(r"\b(\d+[.,]\d{2})\b")

KEYWORDS = {
    "distance": ["distance", "dist ", "dist: ", "Distance", "ระยะทาง", "ระยะ", "Kilometers", "kilometers", "Kilometres", "kilometres", "กิโลเมตร", "กม.", "Distance (km)", "Distance [km]"],
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

    # HH:MM:SS(.ms) → HH:MM:SS
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.\d{1,3})?", s)
    if m:
        h, mm, ss = map(int, m.groups())
        return f"{h:02d}:{mm:02d}:{ss:02d}"

    # MM:SS(.ms) → 00:MM:SS
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\.\d{1,3})?", s)
    if m:
        mm, ss = map(int, m.groups())
        return f"00:{mm:02d}:{ss:02d}"

    return None

def _find_time(lines: List[str]) -> Optional[str]:
    """
    Step 0: ถ้ามีเวลาที่มี milliseconds (เช่น 04:53.79 / 1:02:03.5) ให้พิจารณากลุ่มนี้ก่อน
    Step 1: เก็บ candidate เวลาทั้งเอกสาร (ให้ HH:MM:SS > MM:SS), กัน date/pace
    Step 2: เพิ่มคะแนนถ้าอยู่ใกล้คีย์เวิร์ดเวลา (±2 บรรทัด), ลดคะแนน noise/top-lines
    เลือกคะแนนสูงสุดคืนค่าเป็น HH:MM:SS หรือ 00:MM:SS
    """
    # --- patterns ---
    HHMMSS_RE = re.compile(r"\b(\d{1,3}):(\d{2}):(\d{2})(?:\.\d{1,3})?\b")
    MMSS_RE   = re.compile(r"\b(\d{1,2}):(\d{2})(?:\.\d{1,3})?\b(?!\s*(?:AM|PM)\b)", re.I)

    # รูปแบบคั่นผิด เช่น 01.13.52 / 01.13:52 (ไม่รับ HH:MM.SS)
    MIXED_HHMMSS_RE = re.compile(
        r"(?<![0-9A-Za-z])(?P<h>\d{1,2})\s*(?P<sep1>[:.])\s*(?P<m>\d{2})\s*(?P<sep2>[:.])\s*(?P<s>\d{2})(?!\.\d)(?!\d)"
    )

    # แบบมี milliseconds ชัดเจน
    FRACT_HHMMSS_RE = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\b")
    FRACT_MMSS_RE   = re.compile(r"\b(\d{1,2}):(\d{2})[.,](\d{1,3})\b(?!\s*(?:AM|PM)\b)", re.I)

    DATE_SLASH_RE = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)")
    DATE_ISO_RE   = re.compile(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)")

    # >>> รูปแบบภาษา (spoken) เช่น 1h 20m [35s] / 1 ชม. 20 นาที [35 วิ]
    H_UNITS = r"(?:h|hr|hrs|hour|hours|ชั่วโมง|ชม\.?|ช\.ม\.?)"
    M_UNITS = r"(?:m|min|mins|minute|minutes|นาที|น\.?)"
    S_UNITS = r"(?:s|sec|secs|second|seconds|วินาที|วิ\.?|วิ)"
    HM_SPOKEN_RE = re.compile(
        rf"(?<!\d)(\d{{1,3}})\s*{H_UNITS}\s*(\d{{1,2}})\s*{M_UNITS}(?:\s*(\d{{1,2}})\s*{S_UNITS})?(?!\w)",
        re.I
    )
    # NEW: รองรับ "32m 49s"
    MS_SPOKEN_RE = re.compile(
        rf"(?<!\d)(\d{{1,2}})\s*{M_UNITS}\s*(\d{{1,2}})\s*{S_UNITS}(?!\w)",
        re.I
    )    
    # <<<

    PACE_QUOTES   = ("'", "’", "′", "“", "”", '"')
    NOISY_TOKENS  = ("pace", "bpm", "kcal", "steps", "avg hr", "average hr", "avg heart rate")

    # indices ของ label เวลา จาก KEYWORDS["time"]
    label_idxs = _label_idxs(lines, TIME_LABEL, KEYWORDS["time"])

    def _is_datey_line(s: str) -> bool:
        low = (s or "").lower()
        return bool(DATE_SLASH_RE.search(s) or DATE_ISO_RE.search(s) or " be" in low or "พ.ศ" in low)

    def _is_noisy_line(s: str) -> bool:
        low = (s or "").lower()
        return any(t in low for t in NOISY_TOKENS)

    def _is_pace_like_around(s: str, start: int, end: int) -> bool:
        # 1) กันเฉพาะกรณี quote ติดกับตัวเลข (ไม่มีเว้นวรรคคั่น)
        if s[max(0, start-1):start] in PACE_QUOTES or s[end:end+1] in PACE_QUOTES:
            return True

        # 2) กันเฉพาะกรณี 'pace' ติดกับแมตช์โดยไม่มีช่องว่าง
        if s[max(0, start-4):start].lower() == "pace":
            return True
        if s[end:end+4].lower() == "pace":
            return True

        # ถ้าแค่ "อยู่บรรทัดเดียวกัน" แต่มี space คั่น → ไม่กัน ให้พิจารณาได้
        return False

    # ------------------ Phase 0: ให้โอกาสเวลาที่มี milliseconds ก่อน ------------------
    fract_cands: List[Tuple[str, int, int]] = []  # (hms, kind, line_idx), kind: 4=มี ms
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
        # ให้คะแนนเฉพาะกลุ่ม .ms และเลือกตัวที่ดีที่สุด → ตัด 16:29 ออกไปโดยสิ้นเชิง
        def score_ms(hms: str, _kind: int, j: int) -> float:
            sc = 0.0
            sc += 200.0  # base สูงเพราะมี ms
            if label_idxs:
                if any(abs(j - i) <= 2 for i in label_idxs):
                    sc += 120.0
                else:
                    dist = min(abs(j - i) for i in label_idxs)
                    sc += max(0.0, 60.0 - dist * 12.0)
            if j <= 2:
                sc -= 50.0
            if _is_noisy_line(lines[j]):
                sc -= 25.0
            return sc

        best = max(fract_cands, key=lambda t: score_ms(*t))
        return best[0]

    # ------------------ Phase 1: เก็บ candidate ปกติ ------------------
    # cands = [(hms, kind, line_index)], kind: 3 = HH:MM:SS, 2 = MM:SS
    cands: List[Tuple[str, int, int]] = []

    for j, s in enumerate(lines):
        if _is_datey_line(s):
            continue

        # Spoken H/M[/S] → แปลงเป็น HH:MM:SS (priority สูงเท่า HH:MM:SS)
        for m in HM_SPOKEN_RE.finditer(s):
            h = int(m.group(1))
            mm = int(m.group(2))
            ss = int(m.group(3)) if m.group(3) else 0
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 3, j))

        # Spoken M/S → "00:MM:SS" (priority เท่า HH:MM:SS)
        for m in MS_SPOKEN_RE.finditer(s):
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            mm = int(m.group(1))
            ss = int(m.group(2))
            if 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"00:{mm:02d}:{ss:02d}", 3, j))

        # 1) HH:MM:SS
        for m in HHMMSS_RE.finditer(s):
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            h, mm, ss = map(int, m.groups()[:3])
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 3, j))

        # 1.5) MIXED HH.MM.SS / HH.MM:SS → normalize เป็น HH:MM:SS
        for m in MIXED_HHMMSS_RE.finditer(s):
            token = s[m.start():m.end()]
            if ":." in token:  # HH:MM.SS → ปล่อยให้ logic อื่นจัดการเป็น MM:SS.ms
                continue
            if _is_pace_like_around(s, m.start(), m.end()):
                continue
            h = int(m.group("h")); mm = int(m.group("m")); ss = int(m.group("s"))
            if 0 <= h <= 1000 and 0 <= mm <= 59 and 0 <= ss <= 59:
                cands.append((f"{h:02d}:{mm:02d}:{ss:02d}", 3, j))

    # ถ้ายังไม่เจอเลย ค่อยเก็บ MM:SS เป็น candidate
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

    # ------------------ Phase 2: ให้คะแนน + เลือกดีที่สุด ------------------
    def score(hms: str, kind: int, j: int) -> float:
        sc = 0.0
        sc += 120.0 if kind == 3 else 60.0            # ชนิดเวลา
        if label_idxs:
            if any(abs(j - i) <= 2 for i in label_idxs):
                sc += 120.0
            else:
                dist = min(abs(j - i) for i in label_idxs)
                sc += max(0.0, 60.0 - dist * 12.0)
        if j <= 2:
            sc -= 50.0                                 # เลี่ยง status bar/top
        if _is_noisy_line(lines[j]):
            sc -= 25.0
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

    # ========== 1) หาเวลา + pace ปกติ (+ fallback 5–6 หลัก) ==========
    time_hms = _find_time(lines)           # "HH:MM:SS" หรือ "MM:SS"
    pace_sec = _find_pace_sec(lines)
    print(f"[DEBUG] pace_sec={pace_sec}", flush=True)
    time_sec = _sec_from_timestr(time_hms) if time_hms else None

    # ถ้ายังไม่เจอ "เวลาแบบมี :" ให้ลองกรณีคั่นผิด (01.13.52 / 01.13:52)
    # และตามด้วยแบบแพ็ค 5–6 หลัก (HHMMSS / HMMSS) + 7–8 หลัก (HHMMSSff / HMMSSff)
    if not time_hms:
        MIXED_HHMMSS_RE = re.compile(
            r"(?<![0-9A-Za-z])"
            r"(?P<h>\d{1,2})\s*(?P<sep1>[:.])\s*(?P<m>\d{2})\s*(?P<sep2>[:.])\s*(?P<s>\d{2})"
            r"(?!\.\d)"
        )
        for ln in lines:
            for m in MIXED_HHMMSS_RE.finditer(ln):
                sep1, sep2 = m.group("sep1"), m.group("sep2")
                if sep1 == ":" and sep2 == ".":  # เช่น 01:13.52 → ให้ logic MM:SS.ms จัดการ
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
        found = False
        for ln in lines:
            for m in PACKED_TIME56_RE.finditer(ln):
                s = m.group(1)
                if len(s) == 6:   # HHMMSS
                    hh, mm_, ss_ = int(s[:2]), int(s[2:4]), int(s[4:6])
                else:             # HMMSS
                    hh, mm_, ss_ = int(s[0]), int(s[1:3]), int(s[3:5])
                if 0 <= hh <= 1000 and 0 <= mm_ <= 59 and 0 <= ss_ <= 59:
                    time_hms = f"{hh:02d}:{mm_:02d}:{ss_:02d}"
                    time_sec = _sec_from_timestr(time_hms)
                    found = True
                    break
            if found:
                break

    if not time_hms:
        PACKED_TIME78_RE = re.compile(r"(?<![0-9A-Za-z.,:])(\d{7,8})(?![0-9A-Za-z.,:])")
        for ln in lines:
            for m in PACKED_TIME78_RE.finditer(ln):
                s = m.group(1)
                if len(s) == 8:   # HHMMSSff
                    hh, mm_, ss_ = int(s[:2]), int(s[2:4]), int(s[4:6])
                else:             # 7 หลัก → HMMSSff
                    hh, mm_, ss_ = int(s[0]), int(s[1:3]), int(s[3:5])
                if 0 <= hh <= 1000 and 0 <= mm_ <= 59 and 0 <= ss_ <= 59:
                    time_hms = f"{hh:02d}:{mm_:02d}:{ss_:02d}"
                    time_sec = _sec_from_timestr(time_hms)
                    break
            if time_hms:
                break

    # ========== 2) หา anchor/label ของระยะ ==========
    # กัน speed: km/h, km/hr, กม./ชม., กิโลเมตร/ชั่วโมง, และ kph
    UNIT_CORE      = r"(?:k\s*m|km|kilometers?|kilometres?|กิโลเมตร|กม)"
    SPEED_AFTER_RE = r"(?:\.?\s*/\s*(?:h|hr|hour|ชม\.?|ชั่วโมง)\b)"  # รองรับจุดก่อน '/'

    # ใช้หา "บรรทัด" ที่บอกระยะ (ยกเว้นเป็น speed)
    unit_token_re = re.compile(
        rf"\b{UNIT_CORE}\b(?!\s*{SPEED_AFTER_RE})\.?",
        re.I
    )

    # บรรทัดที่เป็น speed ให้ตัดทิ้งจาก anchor (ทั้ง kph และ km/.../ชม.)
    SPEED_LINE_RE = re.compile(
        rf"(?:\bkph\b)|(?:\b{UNIT_CORE}\b\s*{SPEED_AFTER_RE}\.?)",
        re.I
    )

    dist_label_idxs = _label_idxs(lines, DIST_LABEL, KEYWORDS["distance"])
    anchor_lines = set(dist_label_idxs)
    for i, ln in enumerate(lines):
        if unit_token_re.search(ln) and not SPEED_LINE_RE.search(ln):
            anchor_lines.add(i)

    # ========== 3) ผู้สมัคร km แบบปกติ ==========
    candidates: List[Tuple[float, int]] = []

    # 3.a NEW: มีหน่วย km ชัดเจน แต่ "ไม่ยอมรับ speed"
    KM_RE_NO_SPEED = re.compile(
        rf"\b(\d+(?:[.,]\d+)?)\s*{UNIT_CORE}\b\.?(?!\s*{SPEED_AFTER_RE})",
        re.I
    )
    for i, ln in enumerate(lines):
        for m in KM_RE_NO_SPEED.finditer(ln):
            try:
                val = float(m.group(1).replace(",", "."))
                candidates.append((val, i))
            except Exception:
                pass

    # helper: token นี้คือค่าความเร็วหรือไม่ (เช็คหน่วยหลังเลข)
    # helper: token นี้คือค่าความเร็วหรือไม่ (เช็คหน่วยหลังเลข)
    SPEED_UNIT_AFTER = re.compile(
        rf"^\s*(?:{UNIT_CORE}\b\s*{SPEED_AFTER_RE}\.?|kph\.?\b)",
        re.I
    )
    def _is_speed_value_after(line: str, end_idx: int) -> bool:
        return bool(SPEED_UNIT_AFTER.search(line[end_idx:]))

    # helper: token หลักพันแบบคอมมา เช่น 9,500 / 12,345
    THOUSANDS_TOKEN_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")

    # 3.b ทศนิยมใกล้ anchor (±1) — NEW: ข้ามถ้าทันทีหลังเลขเป็นหน่วย km/h
    for i in sorted(anchor_lines):
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines):
                ln = lines[j]
                for m in DECIMAL_RE.finditer(ln):  # x.xx, x,xx
                    try:
                        raw = m.group(1)

                        # ข้ามเลขรูปแบบหลักพันคั่นด้วยคอมมา เช่น 9,500
                        if THOUSANDS_TOKEN_RE.match(raw):
                            continue
                        if _is_speed_value_after(ln, m.end()):
                            continue  # ข้าม 9.0 km/h

                        v = float(raw.replace(",", "."))
                        if 0.1 <= v <= 100.0:
                            candidates.append((v, j))

                    except Exception:
                        pass

    # 3.c ทศนิยม 2 ตำแหน่งทั้งข้อความ (+กติกาเล็ก=km ใหญ่=time-like)
    SPACED_TWO_DEC_RE = re.compile(r"\b(\d+)\s*[.,]\s*(\d{2})\b")

    two_decimals_all: List[Tuple[float, int, str, Optional[Tuple[int,int]]]] = []

    for i, ln in enumerate(lines):
        seen_normals = set()

        # 3.c.1 แบบติดกัน: 20.59, 4.05 — NEW: ข้ามถ้าต่อด้วย km/h
        for m in TWO_DEC_RE.finditer(ln):
            tok = m.group(0).replace(",", ".")
            if tok in seen_normals:
                continue
            if _is_speed_value_after(ln, m.end()):
                continue  # ข้าม 9.00 km/h
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

        # 3.c.2 แบบมีช่องว่าง: 20 .59 — NEW: ข้ามถ้าต่อด้วย km/h
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

    # <<< ใส่บล็อค injection ตรงนี้ (นอกลูปทั้งหมด) >>>
    if pace_sec and time_sec and pace_sec > 0 and two_decimals_all:
        expect = time_sec / pace_sec
        best = None
        best_err = float("inf")
        for v, i, _tok, mmss in two_decimals_all:
            if mmss is not None:
                continue  # อันนี้เป็นรูป mm:ss ไม่ใช่ระยะ
            if not (0.2 <= v <= 80.0):
                continue
            err = abs(v - expect)
            if err < best_err:
                best_err = err
                best = (v, i)
        if best is not None:
            candidates.append(best)
    # >>> จบ injection

    # ใช้กติกา "ตัวเล็ก = km", "ตัวใหญ่ = เวลา"
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

    # 3.d "เลข 3 ตัวเรียงกันในบรรทัดเดียว" → ตัวกลางเป็น km
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

    # ========== 4) helpers/regex ภายใน ==========
    PACKED_TIME4_RE    = re.compile(r"(?<![0-9A-Za-z.,:])(?P<n>\d{4})(?![0-9A-Za-z.,:])")  # MMSS
    PACKED_TIME3OR4_RE = re.compile(r"(?<![0-9A-Za-z.,:])(?P<n>\d{3,4})(?![0-9A-Za-z.,:])")
    PACKED_INT_3_RE    = re.compile(r"\b\d{3}\b")
    PACKED_INT_4_RE    = re.compile(r"\b\d{4}\b")

    def _hhmm_ok_from_MMSS(n: int) -> Optional[str]:
        if not (0 <= n <= 5959):
            return None
        mm, ss = divmod(n, 100)
        if 0 <= mm <= 59 and 0 <= ss <= 59:
            return f"00:{mm:02d}:{ss:02d}"
        return None

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

    def _near_anchor(idx: int, n: int) -> bool:
        return any(k in anchor_lines for k in (idx - 1, idx, idx + 1) if 0 <= k < n)

    # ========== 5) เดาเวลา 3–4 หลัก (เมื่อมี km ปกติช่วยยืนยัน) ==========
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

    # ========== 6) เดา km แบบ packed ใกล้ anchor (เมื่อมีเวลาแล้ว) ==========
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

    # ========== 7) กติกากลางทาง ==========
    if (time_hms is None) and (maybe_time_hms is not None):
        time_hms = maybe_time_hms
        time_sec = _sec_from_timestr(time_hms)
    elif (time_hms is not None) and (not candidates) and packed_km:
        candidates.extend(packed_km)

    # ========== 8) ไพ่สุดท้าย (packed 3–4 หลักแบบฉลาด) ==========
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

    # ========== 9) ถ้ายังไม่มีผู้สมัคร km ==========
    if not candidates:
        if time_hms:
            return time_hms, None
        return None, None
    
    # print check pace
    if pace_sec and time_sec:
        expect = time_sec / pace_sec
    else:
        expect = None
    
    # ========== 10) ลบซ้ำ ==========
    seen = set()
    uniq: List[Tuple[float, int]] = []
    for val, idx in candidates:
        key = (round(val, 3), idx)
        if key not in seen:
            seen.add(key)
            uniq.append((val, idx))
    candidates = uniq
    
    # ========== 11) ให้คะแนนและเลือก best ==========
    def score_of(val: float, idx: int) -> float:
        # แยกจุดอ้างอิงเป็น 2 กลุ่ม
        label_pts = sorted(set(dist_label_idxs))                 # keyword: Distance / Kilometers ฯลฯ
        unit_only_pts = sorted(set(anchor_lines) - set(label_pts))  # แค่บรรทัดหน่วย

        def _mindist(i: int, pts: list[int]) -> int | None:
            if not pts:
                return None
            return min(abs(i - p) for p in pts)

        dL = _mindist(idx, label_pts)       # ระยะห่างจาก keyword
        dU = _mindist(idx, unit_only_pts)   # ระยะห่างจากบรรทัดหน่วย

        # เก็บ component ไว้พิมพ์
        pace_comp = 0.0
        kw_bonus = 0.0
        #unit_bonus = 0.0
        range_bonus = 0.0
        decimal_bonus = 0.0

        # 1) มี pace/time → pace เป็นแกนหลัก (มากสุด)
        if pace_sec and time_sec and pace_sec > 0:
            expect = time_sec / pace_sec
            if expect > 0:
                rel_err = abs(val - expect) / expect
                pace_comp = 1000.0 * (1.0 - min(rel_err, 1.0))    # ค่าหลักจาก pace

                # tie-breaker: keyword > unit
                if dL is not None:
                    kw_bonus = max(0.0, 30.0 - 8.0 * dL)
                #if dU is not None:
                    #unit_bonus = max(0.0, 10.0 - 3.0 * dU)

                # โบนัสเล็ก ๆ
                if 2.0 <= val <= 50.0:
                    range_bonus = 2.0
                if not float(val).is_integer():
                    decimal_bonus = 5.0  # 2.10 ชนะ 2

                sc = pace_comp + kw_bonus + range_bonus + decimal_bonus
                # print(f"[SCORE] v={val} line#{idx} pace={pace_comp:.2f} kw={kw_bonus:.2f} "
                    # f"range={range_bonus:.2f} dec={decimal_bonus:.2f}  total={sc:.2f}", flush=True)
                return sc

        # 2) ไม่มี pace/time → keyword รองลงมา, unit น้อยสุด
        if dL is not None:
            kw_bonus = max(0.0, 120.0 - 40.0 * dL)   # keyword เข้ม
        #if dU is not None:
            #unit_bonus = max(0.0,  20.0 - 10.0 * dU) # unit อ่อน
        if 2.0 <= val <= 50.0:
            range_bonus = 5.0
        if not float(val).is_integer():
            decimal_bonus = 5.0

        sc = kw_bonus + range_bonus + decimal_bonus
        # print(f"[SCORE] v={val} line#{idx} kw={kw_bonus:.2f} "
                # f"range={range_bonus:.2f} dec={decimal_bonus:.2f} total={sc:.2f}", flush=True)
        return sc

    best_val, best_score = None, -1e9
    for val, idx in candidates:
        sc = score_of(val, idx)
        if sc > best_score:
            best_score, best_val = sc, val

    return time_hms, best_val

# ===== Smart Date Parser (returns M/D/YYYY) =====
from datetime import datetime, date, timedelta
import re

# เดือน (อังกฤษ/ไทย/ตัวย่อ)
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
    # เพิ่ม: แปลง "_" เป็นช่องว่าง, ลบตัวล่องหน, ลดสัญลักษณ์กวน
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

def _pick_year_after_month(i_month: int, after_day_idx: int|None, tok: list[str]) -> int|None:
    """
    หาเฉพาะ 'ปี 4 หลัก' หรือ 'พ.ศ./BE + ปี 4 หลัก'
    ข้าม , . @ · • / ชื่อวัน เวลา และ AM/PM
    """
    n = len(tok)
    j = (after_day_idx + 1) if after_day_idx is not None else (i_month + 1)
    steps = 0
    while j < n and steps < 5:
        t = tok[j]
        tl = t.lower().strip().rstrip(".")
        if t in {",", ".", "@", "•", "·", "/"} or _is_weekday(t):
            j += 1; steps += 1; continue
        if _TIME_RE.match(t) or tl in {"am", "pm"}:
            j += 1; steps += 1; continue

        # ปี 4 หลัก
        if re.fullmatch(r"\d{4}", t):
            return _year_fix(int(t))
        # พ.ศ./BE + ปี 4 หลัก (BE อนุญาตเว้นวรรค)
        if tl in {"พ.ศ.", "be"} and j + 1 < n and re.fullmatch(r"\d{4}", tok[j+1]):
            return _year_fix(int(tok[j+1]))
        # เลข 2 หลักใกล้เดือนไม่ใช่ปี (กัน 22=2022 / 21=2021)
        break
    return None

TODAY_LIKE_RE = re.compile(
    r"(?:\b(?:t\W*o\W*d\W*a\W*y|morning|afternoon|evening|tonight|night)\b|บ่าย)",
    re.I
)

def _parse_smart_date_from_text(text: str, default_year: int|None=None) -> str|None:
    """
    คืนค่า 'M/D/YYYY' หรือ None
    ครอบคลุม logic เดิม + เพิ่ม:
      • today/วันนี้ ทน \W*
      • BE/พ.ศ. ยอมรับ 'B E' (มีช่องว่าง) และ '_' คั่น
      • แบบ 'สองส่วน' M/D หรือ D/M (+ เวลา/weekday ต่อท้าย) -> เติมปีอัตโนมัติ
      • เดือน EN/TH: เลขสองหลักใกล้เดือนเป็น 'วัน' เท่านั้น, ปีต้อง 4 หลัก/พ.ศ.
    """
    if not text or not text.strip():
        return None

    prefer_dayfirst = True  # บริบทไทย

    # ---- 0) Normalize + Today/วันนี้ ----
    norm = _normalize(text)
    if TODAY_LIKE_RE.search(norm) or ("วันนี้" in norm) or ("วันนี" in norm):
        return _format_mdy_no_pad(_now_th_date())

    lines = [ln.strip() for ln in norm.splitlines() if ln.strip()]
    blob  = " ".join(lines)

    # --- ชุดข้อมูลช่วยตัดสิน "ชื่อเดือนเต็ม" vs "ย่อ"
    EN_FULL = {"january","february","march","april","may","june","july","august","september","october","november","december"}
    TH_FULL = {"มกราคม","กุมภาพันธ์","มีนาคม","เมษายน","พฤษภาคม","มิถุนายน","กรกฎาคม","สิงหาคม","กันยายน","ตุลาคม","พฤศจิกายน","ธันวาคม"}
    cands = []

    def _add(y, m, d, flags):
        out = _fmt(y, m, d)
        if not out:
            return
        # กันซ้ำ (y,m,d) เดิม
        for it in cands:
            if it["y"]==y and it["m"]==m and it["d"]==d:
                # รวมธงเพื่อการให้คะแนน
                it["flags"].update(flags)
                return
        cands.append({"y":y, "m":m, "d":d, "flags":set(flags)})

    # ---- 1) YYYY sep MM sep DD ----
    m = re.search(r"(?<!\d)(20\d{2})\s*([\/\-.])\s*(\d{1,2})\s*\2\s*(\d{1,2})(?:\b|[^0-9])", blob)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(3)), int(m.group(4))
        _add(y, mo, dd, {"has_year","year_four","month_numeric","numeric_sep","pattern_y_m_d"})

    # ---- 2) D/M/Y หรือ M/D/Y (+ BE/พ.ศ.) ----
    #  เพิ่ม BE แบบเว้นวรรคได้: (?:b\s*e|พ\.ศ\.)
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

    # ---- 2.5) รูป 'สองส่วน' M/D หรือ D/M (ไม่มีปี) + อาจมีเวลา/weekday/สัญลักษณ์ต่อท้าย ----
    #  ตัวกันเวลา: ไม่ให้สับสนกับ 9:14 (มี ':')
    m = re.search(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\s*[\/\-.]\s*\d)", blob)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        dm = _resolve_day_month(a, b, prefer_dayfirst)
        if dm:
            dd, mo = dm
            yy = default_year or _now_th_date().year
            _add(yy, mo, dd, {"two_part","inferred_year","month_numeric","numeric_sep"})

    # ---- 3) มีชื่อเดือน (อังกฤษ/ไทย) ----
    tok = _tokenize(blob)
    n = len(tok)

    def _pick_year_after_month(i_month: int, after_day_idx: int|None):
        yy = None
        j = (after_day_idx + 1) if after_day_idx is not None else (i_month + 1)
        steps = 0
        while j < n and steps < 5:
            t = tok[j]
            tl = t.lower().strip().rstrip(".")
            if t in {",",".","@","•","·","/"} or _is_weekday(t):
                j += 1; steps += 1; continue
            if _TIME_RE.match(t) or tl in {"am","pm"}:
                j += 1; steps += 1; continue
            if re.fullmatch(r"\d{4}", t):
                return _year_fix(int(t)), True  # (year, explicit?)
            if tl in {"พ.ศ.","be"} and j+1 < n and re.fullmatch(r"\d{4}", tok[j+1]):
                return _year_fix(int(tok[j+1])), True
            break
        return None, False  # (year, explicit?)

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
            elif _is_weekday(t1) and i-2 >= 0 and _as_int(_strip_ordinal(tok[i-2])) is not None:
                dd = _as_int(_strip_ordinal(tok[i-2]))
        if dd is not None and 1 <= dd <= 31:
            y_found, explicit = _pick_year_after_month(i, None)
            if not y_found:
                y_found = default_year or _now_th_date().year
            flags = {"from_monthname"}
            flags.add("month_name_full" if month_full else "month_name_abbr")
            if explicit: flags.add("has_year"); 
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
            y_found, explicit = _pick_year_after_month(i, day_idx)
            if not y_found:
                y_found = default_year or _now_th_date().year
            flags = {"from_monthname"}
            flags.add("month_name_full" if month_full else "month_name_abbr")
            if explicit: flags.add("has_year"); 
            else:        flags.add("inferred_year")
            _add(y_found, mm, dd2, flags)

    # ---- 4) ISO YYYY-MM-DD (มี/ไม่มีเวลา) ----
    m = re.search(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", blob)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        _add(y, mo, dd, {"has_year","year_four","iso","month_numeric"})

    if not cands:
        return None

    # === Scoring ===
    def _score(it):
        flags = it["flags"]
        sc = 0.0
        # ความครบถ้วน
        if "has_year" in flags:       sc += 100
        if "year_four" in flags:      sc += 25
        if "year_two" in flags:       sc -= 10
        if "inferred_year" in flags:  sc -= 35

        # รูปแบบเดือน
        if "month_name_full" in flags: sc += 70    # ชื่อเดือนเต็ม = ชัดเจน
        if "month_name_abbr" in flags: sc += 50
        if "from_monthname" in flags:  sc += 10
        if "month_numeric" in flags:   sc += 20

        # รูปแบบโดยรวม
        if "iso" in flags:             sc += 80
        if "numeric_sep" in flags:     sc += 10
        if "two_part" in flags:        sc += 15    # มีแต่วัน/เดือน ให้คะแนนน้อยกว่า

        # เล็ก ๆ เพื่อ tie-break
        if "pattern_y_m_d" in flags:        sc += 15
        if "pattern_dmy_or_mdy" in flags:   sc += 10

        return sc

    best = max(cands, key=_score)
    return f"{best['m']}/{best['d']}/{best['y']}"

def parse_duration_km_date_smart(text: str, default_year: int|None=None):
    """
    Wrapper: ใช้ตัวเดิมดึง duration/distance + ดึง 'date_str' เพิ่ม (M/D/YYYY)
    return: (duration_hms: Optional[str], distance_km: Optional[float], date_mdy: Optional[str])
    """
    dur, dist = parse_duration_and_km_smart(text)  # คงของเดิม
    date_str = _parse_smart_date_from_text(text, default_year=default_year)
    return dur, dist, date_str

# === Run-type helpers ===
def _where_category(s: str) -> Optional[str]:
    s = (s or "").strip().lower()
    if any(k in s for k in OUTDOOR_KEYS):
        return "outdoor"
    if any(k in s for k in INDOOR_KEYS):
        return "indoor"
    return None

def is_time_over(hms: Optional[str], thr_hms: str) -> bool:
    if not hms:
        return False
    t = _sec_from_timestr(hms)
    thr = _sec_from_timestr(thr_hms)
    return (t is not None) and (thr is not None) and (t > thr_hms_to_sec(thr_hms))

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


# ---------- Main HTTP entry ----------
def ocr_sheet(request):
    if not SPREADSHEET_ID or SPREADSHEET_ID == "PUT_YOUR_SHEET_ID_HERE":
        return ("SPREADSHEET_ID is not set", 400)

    run_ts = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    t0 = time.monotonic()
    current_phase = "init"

    try:
        current_phase = "build_services"
        sheets, drive, vcli = _build_services()

        current_phase = "read_raw"
        raw_vals = _get_values(sheets, RAW_RANGE)
        if not raw_vals:
            dur = round(time.monotonic() - t0, 3)
            logger.info({"event":"summary","result":"success","run_ts":run_ts,"duration_sec":dur})
            return ("No data in raw sheet", 200)
        raw_header, raw_rows = raw_vals[0], raw_vals[1:]

        # NEW: sort RAW by timestamp so new rows are truly at the bottom
        # current_phase = "sort_raw_by_timestamp"
        # try:
        #     _sort_raw_by_timestamp(sheets, raw_header, ascending=True)
        # except Exception as e:
        #     logger.warning({"event":"warn","where":"sort_raw_by_timestamp","reason":str(e)})

        # Re-read RAW after sorting
        # current_phase = "read_raw_after_sort"
        # raw_vals = _get_values(sheets, RAW_RANGE)
        # raw_header, raw_rows = raw_vals[0], raw_vals[1:]

        current_phase = "ensure_working_sheet"
        _ensure_sheet_exists(sheets, SHEET_NAME_WORK)

        current_phase = "load_working"
        work_vals = _get_values(sheets, WORK_RANGE)
        first_time = False

        if not work_vals:
            current_phase = "first_time_copy_all"
            first_time = True
            work_header = list(raw_header)
            to_copy = [list(r) for r in raw_rows]

            # Outdoor result cols
            _ensure_col(work_header, to_copy, STATUS_COL)
            _ensure_col(work_header, to_copy, DIST_COL)
            _ensure_col(work_header, to_copy, DUR_COL)
            # Indoor result cols
            _ensure_col(work_header, to_copy, IN_STATUS_COL)
            _ensure_col(work_header, to_copy, DIGI_DIST_COL)
            _ensure_col(work_header, to_copy, DIGI_DUR_COL)
            _ensure_col(work_header, to_copy, MACH_DIST_COL)
            _ensure_col(work_header, to_copy, MACH_DUR_COL)

            to_copy = [_pad_row(r, len(work_header)) for r in to_copy]
            _update_values(sheets, f"{SHEET_NAME_WORK}!A1", [work_header] + to_copy)

            current_phase = "reload_after_first_copy"
            work_vals = _get_values(sheets, WORK_RANGE)

        current_phase = "prepare_header_pointers"
        work_header, work_rows = work_vals[0], work_vals[1:]

        header_before = list(work_header)
        # Outdoor result cols
        idx_sta  = _ensure_col(work_header, work_rows, STATUS_COL)
        idx_dist = _ensure_col(work_header, work_rows, DIST_COL)
        idx_dur  = _ensure_col(work_header, work_rows, DUR_COL)
        # Indoor result cols
        idx_insta = _ensure_col(work_header, work_rows, IN_STATUS_COL)
        idx_ddist = _ensure_col(work_header, work_rows, DIGI_DIST_COL)
        idx_ddur  = _ensure_col(work_header, work_rows, DIGI_DUR_COL)
        idx_mdist = _ensure_col(work_header, work_rows, MACH_DIST_COL)
        idx_mdur  = _ensure_col(work_header, work_rows, MACH_DUR_COL)
        # Date result cols
        idx_photo_date = _ensure_col(work_header, work_rows, PHOTO_DATE_COL)

        if work_header != header_before:
            _update_values(sheets, f"{SHEET_NAME_WORK}!A1", [work_header])

        current_phase = "copy_new_rows"
        new_count = max(0, len(raw_rows) - len(work_rows))
        if new_count > 0:
            to_copy = [_pad_row(r, len(work_header)) for r in raw_rows[-new_count:]]
            _append_values(sheets, WORK_RANGE, to_copy)

            current_phase = "reload_after_append"
            work_vals = _get_values(sheets, WORK_RANGE)
            work_header, work_rows = work_vals[0], work_vals[1:]
            # re-index (หลัง append)
            idx_sta  = _idx(work_header, STATUS_COL)
            idx_dist = _idx(work_header, DIST_COL)
            idx_dur  = _idx(work_header, DUR_COL)
            idx_insta = _idx(work_header, IN_STATUS_COL)
            idx_ddist = _idx(work_header, DIGI_DIST_COL)
            idx_ddur  = _idx(work_header, DIGI_DUR_COL)
            idx_mdist = _idx(work_header, MACH_DIST_COL)
            idx_mdur  = _idx(work_header, MACH_DUR_COL)
            idx_photo_date = _idx(work_header, PHOTO_DATE_COL)

        # --- index important columns ---
        current_phase = "index_important_cols"
        idx_img    = _idx(work_header, IMAGE_COL_NAME)     # Outdoor image
        idx_selfie = _idx(work_header, SELFIE_COL_NAME)    # Outdoor selfie
        idx_where  = _idx(work_header, WHERE_COL_NAME)     # Where did you run?
        idx_digi   = _idx(work_header, INDOOR_DIGI_COL)    # Indoor digital source
        idx_mach   = _idx(work_header, INDOOR_MACH_COL)    # Indoor machine source

        # Required columns (form is required; fail hard if mismatch)
        if idx_where is None:
            dur = round(time.monotonic() - t0, 3)
            reason = f"Missing column '{WHERE_COL_NAME}'"
            logger.info({"event":"summary","result":"error","run_ts":run_ts,"where":"index_important_cols","reason":reason,"duration_sec":dur})
            return (reason, 400)
        if idx_img is None:
            dur = round(time.monotonic() - t0, 3)
            reason = f"Missing column '{IMAGE_COL_NAME}'"
            logger.info({"event":"summary","result":"error","run_ts":run_ts,"where":"index_photo_cols","reason":reason,"duration_sec":dur})
            return (reason, 400)
        if idx_digi is None or idx_mach is None:
            dur = round(time.monotonic() - t0, 3)
            reason = f"Missing indoor columns: '{INDOOR_DIGI_COL}' or '{INDOOR_MACH_COL}'"
            logger.info({"event":"summary","result":"error","run_ts":run_ts,"where":"index_indoor_cols","reason":reason,"duration_sec":dur})
            return (reason, 400)

        # --- pick targets (รองรับ backfill) ---
        current_phase = "pick_targets"
        if first_time:
            target_indices = list(range(0, len(work_rows)))  # รอบแรก OCR ทุกแถว
            logger.info({"event":"pick_targets","mode":"first_time","count":len(target_indices)})
        else:
            if new_count > 0:
                start_new = len(work_rows) - new_count
                target_indices = list(range(start_new, len(work_rows)))  # OCR เฉพาะแถวใหม่
                logger.info({"event":"pick_targets","mode":"new_rows","new_count":new_count})
            else:
                # ไม่มีแถวใหม่ -> backfill เฉพาะแถวที่ยังไม่เคยตั้งสถานะ (Out_Status และ In_Status ว่างทั้งคู่)
                target_indices = []
                for i, r in enumerate(work_rows):
                    out_empty = (idx_sta is None) or not (r[idx_sta] or "").strip()
                    in_empty  = (idx_insta is None) or not (r[idx_insta] or "").strip()
                    if out_empty and in_empty:
                        target_indices.append(i)
                logger.info({"event":"pick_targets","mode":"backfill","count":len(target_indices)})
                if not target_indices:
                    dur = round(time.monotonic() - t0, 3)
                    logger.info({"event":"summary","result":"success","run_ts":run_ts,"detail":"no_new_rows_and_no_backfill","duration_sec":dur})
                    return ("OK (no new rows)", 200)

        current_phase = "process_rows"
        batch_updates = []

        def row_range_a1(i0: int) -> str:
            # i0: index 0-based ใน work_rows -> แถวจริงในชีต = i0 + 2 (มี header)
            return _range_for_row(SHEET_NAME_WORK, i0 + 2, len(work_header))

        def ocr_and_parse_safe(cell_text: str, *, fail_ng_on_non_image: bool = True) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[str]]:
            """
            return: (duration_hms, distance_km, shot_date_mdy, ng_reason)
                - ถ้าไฟล์ในช่องนี้เป็น non-image หรือรูปพัง → ng_reason = "non-image" / "corrupt-bad-image" / ฯลฯ
                - ถ้าอ่านได้ปกติ → ng_reason = None
            """
            file_ids = _file_ids_from_cell(cell_text)
            if not file_ids:
                return None, None, None, None

            pieces: List[str] = []
            for fid in file_ids:
                content, filename, mime = _download_bytes_and_meta(drive, fid)

                status, reason, text = ocr_image_bytes_safe(content, filename, mime)
                # print(f"===== OCR {filename} ({mime}) | status={status} | reason={reason or '-'} =====\n{text or '<<NO TEXT>>'}\n===== END OCR =====", flush=True)
                if status == "NG":
                    # non-image / รูปพัง / vision error → ถือเป็น NG สำหรับ field นี้
                    return None, None, None, reason or "non-image"
                if text:
                    pieces.append(text)

            text_all = "\n\n---\n\n".join(pieces)
            if not text_all.strip():
                return None, None, None, None

            default_year = dt.datetime.now(dt.timezone(dt.timedelta(hours=LOCAL_TZ_OFFSET_HOURS))).year
            dur, dist, date_str = parse_duration_km_date_smart(text_all, default_year=default_year)
            print(f"[OCR-PARSE RESULT] duration_hms={dur!r}, distance_km={dist!r}, shot_date_mdy={date_str!r}, ng_reason=None", flush=True)

            return dur, dist, date_str, None

            # print(text_all, flush=True)
            # print({
            #     "parsed_duration": dur,
            #     "parsed_distance": dist,
            #     "parsed_date": date_str,
            #     "seen_time_token": bool(TIME_ANY_RE.search(text_all)),
            #     "seen_km_token": bool(KM_RE.search(text_all)),
            #     "seen_decimal": bool(DECIMAL_RE.search(text_all)),
            # }, flush=True)

        def success(d, k) -> bool:
            return (d is not None) and (k is not None)

        # helper: compare time over safely (reuse)
        def _is_time_over_safe(hms: Optional[str]) -> bool:
            if not hms:
                return False
            t = _sec_from_timestr(hms)
            return (t is not None) and (t > thr_hms_to_sec(TIME_OVER_HMS))

        for i in target_indices:
            r = work_rows[i]
            if len(r) < len(work_header):
                r[:] = _pad_row(r, len(work_header))

            where_val = (r[idx_where] or "").strip()
            cat = _where_category(where_val)
            if cat is None:
                dur = round(time.monotonic() - t0, 3)
                reason = f"Unknown value in '{WHERE_COL_NAME}' at working row {i+2}: {where_val!r}"
                logger.error({"event":"summary","result":"error","run_ts":run_ts,"where":"process_rows","reason":reason,"duration_sec":dur})
                return (reason, 400)

            # ----------------- Outdoor -----------------
            if cat == "outdoor":
                changed = False

                out_dur, out_dist, shot_date, ng_reason = ocr_and_parse_safe(r[idx_img])
                if ng_reason:  # ช่องหลักเป็นวิดีโอ/ไม่ใช่รูป → NG ทันที
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
                        # ลองจาก Selfie เฉพาะกรณีที่ช่องหลัก "เป็นรูป" แต่ parse ไม่สำเร็จ
                        if idx_selfie is not None:
                            s_dur, s_dist, s_date, ng_reason2 = ocr_and_parse_safe(r[idx_selfie])
                            if ng_reason2:  # selfie เป็นวิดีโอ/ไม่ใช่รูป → NG
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

                # ---- Outdoor: precedence overrides (apply only if initial is OK) ----
                initial_status = (r[idx_sta] or "").strip()

                if initial_status == "OK":
                    new_status = None

                    # 1) ระยะน้อย + เวลาเกิน -> All Condition Insufficient
                    if is_small_distance_km(r[idx_dist]) and _is_time_over_safe(r[idx_dur]):
                        new_status = STATUS_COND_INSUFF
                    # 2) ระยะน้อย -> Distance Insufficient
                    elif is_small_distance_km(r[idx_dist]):
                        new_status = STATUS_DIST_INSUFF
                    # 3) เวลาเกิน -> Time Over
                    elif _is_time_over_safe(r[idx_dur]):
                        new_status = "Time Over"

                    if new_status and new_status != initial_status:
                        r[idx_sta] = new_status
                        changed = True

                # หมายเหตุ: ถ้าเริ่มเป็น "Miss box" หรือ "NG" จะไม่โดนทับด้วยเงื่อนไขด้านบน

                if changed:
                    batch_updates.append({
                        "range": row_range_a1(i),
                        "values": [_pad_row(r, len(work_header))]
                    })

            # ----------------- Indoor -----------------
            elif cat == "indoor":
                changed = False

                # Digi (smartwatch/mobile)
                digi_dur,  digi_dist, digi_date,  ng_digi  = ocr_and_parse_safe(r[idx_digi])
                # Machine (exercise machine)
                mach_dur,  mach_dist, mach_date,  ng_mach  = ocr_and_parse_safe(r[idx_mach])

                # เขียนค่าที่อ่านได้ (ไม่มีก็เขียน "")
                r[idx_ddur]  = digi_dur or ""
                r[idx_ddist] = digi_dist if (digi_dist is not None) else ""
                r[idx_mdur]  = mach_dur or ""
                r[idx_mdist] = mach_dist if (mach_dist is not None) else ""
                changed = True

                shot_date = digi_date or None
                if shot_date:
                    r[idx_photo_date] = shot_date

                # --- In_Status rules ---
                if ng_digi or ng_mach:
                    # ช่องใดช่องหนึ่งเป็นวิดีโอ/ไม่ใช่รูป → NG ทันที
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
                        "range": row_range_a1(i),
                        "values": [_pad_row(r, len(work_header))]
                    })

        current_phase = "batch_update"
        if batch_updates:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "RAW", "data": batch_updates},
            ).execute()

        dur = round(time.monotonic() - t0, 3)
        logger.info({"event":"summary","result":"success","run_ts":run_ts,"duration_sec":dur})
        return ("OK", 200)

    except HttpError as e:
        try:
            detail = e.content.decode() if hasattr(e, "content") else str(e)
        except Exception:
            detail = str(e)
        dur = round(time.monotonic() - t0, 3)
        logger.error({"event":"summary","result":"error","run_ts":run_ts,"where":current_phase,"reason":detail,"duration_sec":dur})
        return (f"Google API error: {detail}", 500)

    except Exception as e:
        dur = round(time.monotonic() - t0, 3)
        logger.error({"event":"summary","result":"error","run_ts":run_ts,"where":current_phase,"reason":str(e),"duration_sec":dur})
        return (f"Unhandled error: {e}", 500)
