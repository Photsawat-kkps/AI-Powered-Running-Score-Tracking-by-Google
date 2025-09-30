# 🏃‍♀️ **Sports Day Reinvented**
## AI‑Powered Running Score Tracking (Google Cloud Vision)

> A story‑style README for both **non‑technical** and **technical** readers. It explains the *why*, shows the *how*, and gives you the *code path* to reproduce. Visual cues (🖼️) indicate where to place images/diagrams. Deep dives live in the **Appendices** so the main story stays crisp.

---

### TL;DR
We turned photos from **scoreboards, smartwatches, and treadmill panels** into **clean, validated results**—automatically. The system:
- Reads text with **Google Cloud Vision API**
- Extracts **time (HH:MM:SS)** and **distance (km)** with domain‑aware parsing
- Applies **event rules** (Outdoor/Indoor)
- Writes to a **Working Sheet**, then produces a **Daily Summary** (one sheet per date)
- Minimizes manual work; humans only check anomalies

🖼️ **Hero visual (top banner):** *Before → After* collage: raw photo → OCR text highlights → neat result table.

---

## 1) Project Overview (Main Story)
**Problem** — Sports Day results were recorded from mixed photo inputs and typed into spreadsheets by hand. It was **slow**, **error‑prone**, and **hard to audit**.

**Solution** — Use **Google Cloud Vision** (pre‑trained AI) to OCR uploaded images, then apply a **smart parser** to reliably extract running **time** and **distance**. For **Outdoor**, we read the primary photo (watch/mobile) and fall back to selfie if necessary. For **Indoor**, we read both **digital device** and **machine panel** and apply combined rules. Finally, we validate and write results into Google Sheets.

**Impact** — Quicker announcements, consistent scoring, audit trail (every decision links back to the source photo), fewer disputes.

🖼️ **Visual:** Swimlane diagram showing *Participant → Google Form → Drive → OCR Worker → Sheets → Summary/Dashboard*.

---

## 2) Event Context & OCR Inputs (Main Story)
We process three photo contexts:
- **Scoreboards** (handwritten/printed)  
- **Smartwatch screens** (Garmin/Apple/Android; Outdoor duration & distance)
- **Treadmill machine panels** (Indoor distance/time/pace)

**Collection Flow**
- Participants upload via **Google Forms** → files land in **Google Drive**, metadata in **Google Sheets** (`Form Responses 1`).
- Our worker ingests new rows, fetches images, OCRs them, parses values, then writes results to the **Working** tab.

🖼️ **Visual (3‑up grid):** exemplar *scoreboard / watch / treadmill* images with annotated areas the OCR relies on.

> Deep dive: [Appendix C — Sample Dataset & Annotations](#appendix-c-sample-dataset--annotations)

---

## 3) Tech Stack (Quick)
- **AI**: Google Cloud Vision API (Text Detection)
- **Runtime**: Python 3.11 (Cloud Run / Cloud Functions or local)
- **Data**: Google Sheets (Raw tab + Working tab + per‑day Summary tabs)
- **Storage**: Google Drive (form uploads)
- **Automation**: Cloud Scheduler (triggers worker and daily summary)
- **Auth**: Service Account (Sheets + Drive scopes)
- **Logging**: Google Cloud Logging (JSON)

```python
from google.cloud import vision
client = vision.ImageAnnotatorClient()
resp = client.text_detection(image=vision.Image(content=img_bytes))
text = resp.full_text_annotation.text if resp.full_text_annotation else ""
```

> Deep dive: [Appendix D — Setup & Deployment](#appendix-d-setup--deployment)

---

## 4) End‑to‑End Pipeline (Main Story)
```
Google Form → Google Drive (images) + Google Sheet (Form Responses 1)
                    ↓
               OCR Worker (ocr_sheet)
                    ↓
        Vision API → OCR Text → Smart Parsing
                    ↓
      Validation & Business Rules (Outdoor/Indoor)
                    ↓
           Working Sheet  (… (Working))
                    ↓
          Daily Summary (summarize_day → YYYY‑MM‑DD)
```

### 4.1 Worker Behavior (`ocr_sheet`)
**Idempotent ingest**
- **First run** → copy all raw rows to Working; OCR **every** row
- **Next runs** → only new rows; if no new rows, **backfill** rows that still have empty statuses

**Outdoor flow**
- Primary image: smartwatch/mobile (fallback: selfie only if primary is an image but parsing failed)
- Write `Out_Distance_km`, `Out_Duration_hms`, `Out_Status`
- **Status precedence** *(applies only if initial status is `OK`)*:
  1) dist < 2.00 **and** time > 02:00:00 → `All Condition Insufficient`
  2) dist < 2.00 → `Distance Insufficient`
  3) time > 02:00:00 → `Time Over`

**Indoor flow**
- From **digital** photo → `digi_distance_km`, `digi_duration_hms`
- From **machine** panel → `mach_distance_km`, `mach_duration_hms`
- `In_Status` decision:
  - If any of `{digi_duration_hms, mach_distance_km, mach_duration_hms}` missing → `NG`
  - Else evaluate in order:
    1) both dists < 2.00 **and** both durs > 02:00:00 → `All Condition Insufficient`
    2) both dists < 2.00 → `Distance Insufficient`
    3) both durs > 02:00:00 → `Time Over`
    4) otherwise → `OK`

**Smart date**
- Extract `Shot_Date` (e.g., `3/17/2025`) from OCR text (supports TH/EN month names, BE → CE).

🖼️ **Visual:** Architecture diagram with “decision boxes” for Outdoor/Indoor.

### 4.2 Parser Highlights (duration & km)
- Time patterns: `HH:MM:SS` / `MM:SS` / `MM:SS.ff` / spoken (`1h 20m 35s`, `32m 49s`)
- Mixed separators normalized: `01.13.52` → `01:13:52` (when safe)
- Pace guardrails: avoid treating `MM:SS / km` as **time**
- Distance candidates: decimals in **0.2–80.0** (prefer **2.0–50.0**), avoid `km/h`
- Anchor logic: tokens near *Distance/km* labels weigh more
- Packed tokens: infer from `3–4` digits near anchors (e.g., `905` → `9.05 km`) if needed
- Pace‑assisted inference: if pace + time exist, pick distance closest to `time / pace`

> Deep dive: [Appendix A — Parsing Rules](#appendix-a-parsing-rules-cheat-sheet)

---

## 5) Results (Main Story)
**What improved**
- **Speed**: OCR + parsing removes retyping
- **Accuracy**: domain heuristics filter out pace/date mix‑ups
- **Auditability**: every record links to images + parsing decision
- **Scalability**: handle more participants without more staff

🖼️ **Visual:** Split screen — left: raw OCR dump; right: final columns (`Time_HHMMSS`, `Distance_km`, `Validation_Status`, `Shot_Date`).

**Working Sheet columns (core)**
- `Out_Status`, `Out_Distance_km`, `Out_Duration_hms`
- `In_Status`, `digi_distance_km`, `digi_duration_hms`, `mach_distance_km`, `mach_duration_hms`
- `Shot_Date`

---

## 6) Daily Summary (One Sheet per Day)
`summary_day` builds a new sheet named `YYYY‑MM‑DD` with these derived fields:
- **Value condition** = first‑non‑empty(`Out_Status`, `In_Status`)
- **Distance** = first‑non‑empty(`Out_Distance_km`, `mach_distance_km`)
- **Duration** = Outdoor→`Out_Duration_hms`; Indoor→`min(digi_duration_hms, mach_duration_hms)`
- **Check distance with input distance** = `OK` / `Different` / `N/A`
- **Check Date** (from `Shot_Date` vs. `Timestamp`) = `OK` / `Different` / `N/A`
- **Summary** = `OK` iff all checks are `OK`, else `NG`

It **groups by Employee ID** and keeps the **latest Timestamp** per person.

🖼️ **Visual:** Example summary table showing merged decisions and checks.

---

## 7) Quickstart
### 7.1 Environment
Set via Cloud Run/Functions **env vars** (or `.env` when local):
```
SPREADSHEET_ID=...
SHEET_NAME=Form Responses 1
WORK_SHEET_NAME=Form Responses 1 (Working)
LOCAL_TZ_OFFSET_HOURS=7
TIME_OVER_HMS=02:00:00
DIST_MIN_KM=2.0
```
> Column names are localized (TH/EN). Keep your **Form column headers** identical to the script’s defaults or override them via env vars.

### 7.2 Permissions
- Enable **Vision API**, **Drive API**, **Sheets API**
- Service Account with scopes:
  - `spreadsheets` read/write
  - `drive.readonly`

### 7.3 Run Locally (pseudo)
```bash
export GOOGLE_APPLICATION_CREDENTIALS=path/to/key.json
python ocr_sheet.py      # process new rows → Working
SUMMARY_DATE=2025-09-29 \
python summarize_day.py  # write daily sheet named 2025-09-29
```

### 7.4 Deploy
- **Cloud Functions** (HTTP): `ocr_sheet`, `summarize_day`
- Trigger via **Cloud Scheduler** (e.g., every 5–10 min for worker; daily for summary)

---

## 8) Operational Notes
- **Idempotent**: safe to re‑run; only new rows or missing statuses are processed
- **Non‑image defense**: skips videos / non‑images; marks `NG`
- **Sorting**: optional RAW sort by timestamp (left commented in code)
- **Internationalization**: date parser supports EN/TH month names and BE→CE conversion
- **Performance**: batch updates to Sheets to reduce API calls

---

## 9) Limitations & Next Steps
**Known limits**
- Low‑light/blurred images, glare on glossy panels
- Rare watch UI layouts/fonts
- Ambiguous tokens that resemble dates/times/pace

**Roadmap**
- Reviewer UI (human‑in‑the‑loop)
- Confidence scoring & auto‑routing anomalies
- Face‑blurring (privacy) before public dashboards
- AutoML Vision for custom scoreboard templates

---

# Appendices (Deep Dives)

## Appendix A — Parsing Rules (Cheat Sheet)
- **Time**: `HH:MM:SS`, `MM:SS`, `MM:SS.ff`, `1h 20m [35s]`, `32m 49s`
- **Normalize**: `01.13.52` → `01:13:52` (safe contexts only)
- **Avoid**: pace tokens (`MM:SS / km`), AM/PM times, date strings
- **Distance (km)**: prefer **2.0–50.0**; ignore `km/h`
- **Anchors**: lines near *Distance/km* labels score higher
- **Packed**: infer `905` → `9.05 km` near anchors when no decimals exist
- **Pace‑assist**: if time + pace exist, choose distance ≈ `time / pace`

## Appendix B — Validation & Status Logic
- Outdoor precedence: `All Condition Insufficient` → `Distance Insufficient` → `Time Over`
- Indoor requirements: must have `digi_duration_hms`, `mach_distance_km`, `mach_duration_hms`
- Thresholds: `DIST_MIN_KM=2.0`, `TIME_OVER_HMS=02:00:00`

## Appendix C — Sample Dataset & Annotations
> Provide 10–20 example images and a small CSV of *OCR raw → parsed → validated*.

🖼️ **Grid:** scoreboard / watch / treadmill with bounding notes.

## Appendix D — Setup & Deployment
- Enable APIs, create Service Account, assign roles
- Grant Drive file access (uploads folder) & Sheets editor access
- (Optional) Cloud Run/Functions templates + Cloud Scheduler cron

## Appendix E — Working & Summary Schemas
**Working**
- Outdoor: `Out_Status`, `Out_Distance_km`, `Out_Duration_hms`
- Indoor: `In_Status`, `digi_*`, `mach_*`
- Common: `Shot_Date`

**Summary (per day)**
- `Value condition`, `Distance`, `Duration`, `Check distance with input distance`, `Check Date`, `Summary`

---

### Visual Map (Where to Put Images)
- Top: **Hero before/after**
- §2: **Input triptych** (scoreboard / watch / treadmill)
- §4: **Pipeline diagram** with Outdoor/Indoor branches
- §5: **Results table** + small dashboard snapshot

> Keep the **Main Story** flowing from §1→§6; push heavy regex, thresholds, env vars, and deployment steps to the **Appendices** for readers who want the nitty‑gritty.