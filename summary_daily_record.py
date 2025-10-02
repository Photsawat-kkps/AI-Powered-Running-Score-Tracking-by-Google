# -*- coding: utf-8 -*-
"""
Daily Summary (by date) from Working sheet.

Steps:
1) Filter rows by SUMMARY_DATE (from column "Timestamp").
2) Build output columns:
   base: Timestamp, เลือกทีมของตัวเอง (Select your team),
         รหัสพนักงาน (Employee ID), ระยะทาง หน่วยกิโลเมตร  (Distance in km unit),
         ลักษณะสถานที่วิ่ง (Where did you run?)
   images (pass-through): Outdoor/Indoor photos & selfies (5 cols)
   Derived:
     - Value condition : first-non-empty(Out_Status, In_Status)
     - Distance        : first-non-empty(Out_Distance_km, mach_distance_km)
     - Duration        : Outdoor -> Out_Duration_hms
                         Indoor  -> min(digi_duration_hms, mach_duration_hms)
     - Check distance with input distance : "OK" / "Different" / "N/A"
     - Check Date     : compare Shot_Date (M/D/YYYY) with date(Timestamp)
                        -> "N/A" if Shot_Date empty/invalid
                        -> "OK"  if same day
                        -> "NG"  otherwise
     - Summary        : "OK" iff [Value condition, Check distance with input distance, Check Date] are all "OK"; else "NG"
3) Group by Employee ID and pick the row with the latest Timestamp.
4) Sort by Timestamp ascending (configurable) and write into a sheet named YYYY-MM-DD.

Entry point: summarize_day(request)
"""

import os
import re
from typing import List, Optional, Dict
from datetime import datetime, date, timedelta, timezone

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# =============== TIME / CONFIG ===============
LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "7"))
_LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))
_DEFAULT_YESTERDAY = (datetime.now(_LOCAL_TZ) - timedelta(days=1)).date().isoformat()

SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID", "1-ht6PyQtynMG-dMpSGM4I3sVr7HBhb8y33xwl_QZvVA")
WORK_SHEET_NAME  = os.getenv("WORK_SHEET_NAME", "Form Responses 1 (Working)")
SUMMARY_DATE_STR = os.getenv("SUMMARY_DATE", _DEFAULT_YESTERDAY)   # YYYY-MM-DD
SORT_DESCENDING  = False  # False = old->new, True = newest first

# =============== COLUMN NAMES (TH / EN) ===============
# Base (from Working)
COL_TS    = "Timestamp"  # e.g. '9/17/2025 9:28:21'
COL_TEAM  = "เลือกทีมของตัวเอง (Select your team)"
COL_EID   = "รหัสพนักงาน (Employee ID)"
COL_MAN   = "ระยะทาง หน่วยกิโลเมตร  (Distance in km unit)"
COL_WHERE = "ลักษณะสถานที่วิ่ง (Where did you run?)"

# Images (pass-through)
COL_IMG_OUT     = "รูปถ่ายแสดงระยะทาง Outdoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)"
COL_SELFIE_OUT  = "รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Outdoor (Selfie)"
COL_IMG_IN_DIGI = "รูปถ่ายแสดงระยะทาง Indoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)"
COL_IMG_IN_MACH = "รูปถ่ายระยะทางจากเครื่องออกกำลังกาย (Photo of the distance display from the exercise machine.)"
COL_SELFIE_IN   = "รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Indoor (Selfie)"

# Inputs for merge (from Working)
OUT_STATUS = "Out_Status"
IN_STATUS  = "In_Status"

OUT_DIST   = "Out_Distance_km"
MACH_DIST  = "mach_distance_km"

OUT_DUR    = "Out_Duration_hms"
DIGI_DUR   = "digi_duration_hms"
MACH_DUR   = "mach_duration_hms"

# Shot date parsed from OCR (M/D/YYYY), e.g. '3/17/2025'
COL_SHOT_DATE = "Shot_Date"

# Output column names (Summary sheet)
OUT_STATUS_COL    = "Value condition"                      # (merged status)
OUT_DISTANCE_COL  = "Distance"
OUT_DURATION_COL  = "Duration"
OUT_CHECK_COL     = "Check distance with input distance"   # ("OK"/"Different"/"N/A")
OUT_CHECK_DATE    = "Check Date"                           # ("OK"/"NG"/"N/A")
OUT_SUMMARY       = "Summary"                              # ("OK"/"NG")


# =============== SHEETS HELPERS ===============
def _sheets():
    creds, _ = google.auth.default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

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

def _list_sheet_titles(sheets) -> List[str]:
    meta = sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return [sh["properties"]["title"] for sh in meta.get("sheets", [])]

def _ensure_sheet_exists(sheets, title: str):
    if title not in _list_sheet_titles(sheets):
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()


# =============== GENERIC HELPERS ===============
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def _idx(header: List[str], name: str) -> Optional[int]:
    want = _norm(name)
    for i, h in enumerate(header or []):
        if _norm(h) == want:
            return i
    return None

# Put US-style first to match your actual data patterns,
# then Thai-style, then ISO.
_DATE_FORMATS = [
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
]

def _parse_date_only(v) -> Optional[date]:
    """Return date from various formats (also supports Google serial date)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        # Google serial date
        try:
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=float(v))).date()
        except Exception:
            return None
    s = str(v).strip()
    # Trim milliseconds if present
    s2 = re.sub(r"\.\d+$", "", s)
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
        if s2 != s:
            try:
                return datetime.strptime(s2, f).date()
            except ValueError:
                pass
    # Tolerant: pull M/D/Y anywhere at head; ignore trailing time/noise
    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100: yy += 2000
        if yy > 2400: yy -= 543  # BE -> CE
        try:
            return date(yy, mm, dd)
        except Exception:
            return None
    return None

def _parse_datetime(v) -> Optional[datetime]:
    """Return datetime; supports Google serial date."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            base = datetime(1899, 12, 30)
            return base + timedelta(days=float(v))
        except Exception:
            return None
    s = str(v).strip()
    s2 = re.sub(r"\.\d+$", "", s)
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
        if s2 != s:
            try:
                return datetime.strptime(s2, f)
            except ValueError:
                pass
    # ISO-ish
    m = re.search(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = int(m.group(4)), int(m.group(5))
        ss = int(m.group(6) or "0")
        try:
            return datetime(y, mo, dd, hh, mm, ss)
        except Exception:
            return None
    return None

def _to_float(x) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        s = str(x).strip().replace(",", ".")
        return float(s)
    except Exception:
        return None

def _first_non_empty(*vals: str) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            return s
    return ""

def _min_duration_hms(a: Optional[str], b: Optional[str]) -> str:
    """Pick the smaller HH:MM:SS; return '' if both missing/invalid."""
    def to_sec(hms: Optional[str]) -> Optional[int]:
        if not hms:
            return None
        try:
            h, m, s = hms.strip().split(":")
            return int(h)*3600 + int(m)*60 + int(s)
        except Exception:
            return None
    sa, sb = to_sec(a), to_sec(b)
    if sa is None and sb is None:
        return ""
    if sa is None:
        return b or ""
    if sb is None:
        return a or ""
    return a if sa <= sb else b


# =============== CORE ===============
def summarize_day(request):
    if not SPREADSHEET_ID or SPREADSHEET_ID == "PUT_YOUR_SHEET_ID":
        return ("[CONFIG] SPREADSHEET_ID is missing.", 400)

    target_day = _parse_date_only(SUMMARY_DATE_STR)
    if not target_day:
        return (f'[CONFIG] SUMMARY_DATE "{SUMMARY_DATE_STR}" invalid (YYYY-MM-DD).', 400)

    try:
        sheets = _sheets()

        # Ensure working sheet exists
        titles = _list_sheet_titles(sheets)
        if WORK_SHEET_NAME not in titles:
            return (f"[ERROR] Sheet '{WORK_SHEET_NAME}' not found. Available: {titles}", 400)

        # Load working
        values = _get_values(sheets, f"{WORK_SHEET_NAME}!A:AZ")
        if not values:
            return (f"[DATA] Sheet '{WORK_SHEET_NAME}' is empty.", 200)

        header, rows = values[0], values[1:]

        # indexes
        idx_ts        = _idx(header, COL_TS)
        idx_team      = _idx(header, COL_TEAM)
        idx_eid       = _idx(header, COL_EID)
        idx_man       = _idx(header, COL_MAN)
        idx_where     = _idx(header, COL_WHERE)

        idx_out_sta   = _idx(header, OUT_STATUS)
        idx_in_sta    = _idx(header, IN_STATUS)

        idx_out_dist  = _idx(header, OUT_DIST)
        idx_mach_dist = _idx(header, MACH_DIST)

        idx_out_dur   = _idx(header, OUT_DUR)
        idx_digi_dur  = _idx(header, DIGI_DUR)
        idx_mach_dur  = _idx(header, MACH_DUR)

        idx_img_out     = _idx(header, COL_IMG_OUT)
        idx_selfie_out  = _idx(header, COL_SELFIE_OUT)
        idx_img_in_digi = _idx(header, COL_IMG_IN_DIGI)
        idx_img_in_mach = _idx(header, COL_IMG_IN_MACH)
        idx_selfie_in   = _idx(header, COL_SELFIE_IN)

        idx_shot_date   = _idx(header, COL_SHOT_DATE)

        required = [idx_ts, idx_team, idx_eid, idx_man, idx_where,
                    idx_out_sta, idx_in_sta, idx_out_dist, idx_mach_dist,
                    idx_out_dur, idx_digi_dur, idx_mach_dur]
        if any(x is None for x in required):
            return (f"[ERROR] Missing columns in Working. Header={header}", 400)

        # filter by day
        day_rows: List[List[str]] = []
        for r in rows:
            d = _parse_date_only(r[idx_ts] if idx_ts is not None and idx_ts < len(r) else "")
            if d == target_day:
                day_rows.append(r)

        # prepare destination header
        dest_title = target_day.isoformat()
        _ensure_sheet_exists(sheets, dest_title)

        out_header = [
            COL_TS, COL_TEAM, COL_EID, COL_MAN, COL_WHERE,
            COL_IMG_OUT, COL_SELFIE_OUT, COL_IMG_IN_DIGI, COL_IMG_IN_MACH, COL_SELFIE_IN,
            OUT_DISTANCE_COL, OUT_DURATION_COL, OUT_STATUS_COL, OUT_CHECK_COL,
            OUT_CHECK_DATE, OUT_SUMMARY
        ]

        if not day_rows:
            _update_values(sheets, f"{dest_title}!A1", [out_header])
            return (f"[RESULT] No rows for {dest_title}.", 200)

        # build rows with merged columns
        built: List[List[str]] = []
        for r in day_rows:
            ts     = r[idx_ts]    if idx_ts    is not None and idx_ts    < len(r) else ""
            team   = r[idx_team]  if idx_team  is not None and idx_team  < len(r) else ""
            eid    = r[idx_eid]   if idx_eid   is not None and idx_eid   < len(r) else ""
            man    = r[idx_man]   if idx_man   is not None and idx_man   < len(r) else ""
            wherev = r[idx_where] if idx_where is not None and idx_where < len(r) else ""

            # images
            img_out     = r[idx_img_out]     if idx_img_out     is not None and idx_img_out     < len(r) else ""
            selfie_out  = r[idx_selfie_out]  if idx_selfie_out  is not None and idx_selfie_out  < len(r) else ""
            img_in_digi = r[idx_img_in_digi] if idx_img_in_digi is not None and idx_img_in_digi < len(r) else ""
            img_in_mach = r[idx_img_in_mach] if idx_img_in_mach is not None and idx_img_in_mach < len(r) else ""
            selfie_in   = r[idx_selfie_in]   if idx_selfie_in   is not None and idx_selfie_in   < len(r) else ""

            # Value condition (merged status)
            out_sta = r[idx_out_sta] if idx_out_sta is not None and idx_out_sta < len(r) else ""
            in_sta  = r[idx_in_sta]  if idx_in_sta  is not None and idx_in_sta  < len(r) else ""
            value_condition = _first_non_empty(out_sta, in_sta)

            # Distance (merged)
            out_dist  = r[idx_out_dist]  if idx_out_dist  is not None and idx_out_dist  < len(r) else ""
            mach_dist = r[idx_mach_dist] if idx_mach_dist is not None and idx_mach_dist < len(r) else ""
            distance  = _first_non_empty(out_dist, mach_dist)

            # Duration (by where)
            where_lc = (wherev or "").strip().lower()
            if "indoor" in where_lc or "ในร่ม" in where_lc:
                digi_dur = r[idx_digi_dur] if idx_digi_dur is not None and idx_digi_dur < len(r) else ""
                mach_dur = r[idx_mach_dur] if idx_mach_dur is not None and idx_mach_dur < len(r) else ""
                duration = _min_duration_hms(digi_dur, mach_dur)
            else:
                duration = r[idx_out_dur] if idx_out_dur is not None and idx_out_dur < len(r) else ""

            # Check distance with input distance
            dist_num = _to_float(distance)
            man_num  = _to_float(man)
            if (dist_num is None) or (man_num is None):
                check_distance = "N/A"
            else:
                check_distance = "OK" if dist_num == man_num else "Different"

            # Check Date
            ts_date_only = _parse_date_only(ts)  # expects '9/17/2025 9:28:21' etc.
            shot_raw = r[idx_shot_date] if (idx_shot_date is not None and idx_shot_date < len(r)) else ""
            shot_raw_stripped = str(shot_raw).strip()
            if not shot_raw_stripped:
                check_date = "N/A"  # ว่าง = N/A
            else:
                shot_date_only = _parse_date_only(shot_raw_stripped)  # expects '3/17/2025'
                if (ts_date_only is not None) and (shot_date_only is not None) and (ts_date_only == shot_date_only):
                    check_date = "OK"
                elif (ts_date_only is not None) and (shot_date_only is not None):
                    check_date = "Different"   # <-- เปลี่ยนจากเดิมที่เป็น "NG"
                else:
                    check_date = "N/A"

            # Summary
            summary = "OK" if (value_condition == "OK" and check_distance == "OK" and check_date == "OK") else "NG"

            built.append([
                ts, team, eid, man, wherev,
                img_out, selfie_out, img_in_digi, img_in_mach, selfie_in,
                distance, duration, value_condition, check_distance,
                check_date, summary
            ])

        # group by Employee ID -> pick latest timestamp
        def _dt_for_sort(row: List[str]) -> datetime:
            return _parse_datetime(row[0]) or datetime(1970, 1, 1)

        groups: Dict[str, List[List[str]]] = {}
        for row in built:
            emp = (row[2] or "").strip()
            if not emp:
                continue
            groups.setdefault(emp, []).append(row)

        chosen: List[List[str]] = []
        for emp, items in groups.items():
            latest = max(items, key=_dt_for_sort)
            chosen.append(latest)

        chosen.sort(key=_dt_for_sort, reverse=SORT_DESCENDING)

        _update_values(sheets, f"{dest_title}!A1", [out_header] + chosen)
        return (f"[OK] summarized {len(chosen)} employees -> '{dest_title}'", 200)

    except HttpError as e:
        return (f"[Google API error] {str(e)}", 500)
    except Exception as e:
        return (f"[Unhandled error] {e}", 500)
