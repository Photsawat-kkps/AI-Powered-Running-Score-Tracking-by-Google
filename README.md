# 🏃‍♀️ **Sports Day Reinvented**
## AI‑Powered Running Score Tracking (Google Cloud Vision)

**Why this exists**  
Sports Day results come as **photos** — scoreboards and watch/treadmill screens. Typing them into a sheet is slow and easy to get wrong. We use **Google Cloud Vision** to read the text, simple rules to pick out **time** and **distance**, and send the results to **Google Sheets** each day. Every number can be checked with its original photo. Read the main story for what we did; details and code are at the end. 

![Running watch screen](image/running_watch.png "Watch sample")

---

## 1) Project Overview (Main Story)
**- Problem -**  
In Sports Day’s running events, participants record their own results and submit a captured image via Google Form so the judges can aggregate and analyze the results. However, there are about **700 participants**, and the competition runs **every day for one week**. New images keep coming in across all 7 days, creating a huge volume to review. Doing this purely by people may be overwhelming and can lead to mistakes.

![Tired](image/Tired.jpg "Tried")

**- Solution -**  
We looked for a way to reduce judges’ manual work, improve accuracy, and cut human error caused by the massive workload. We use **Google Cloud Vision** to detect text in images and turn it into the **distance** and **running time**. These values are then checked against the distance entered by the submitter to verify whether it’s truly correct.

![Detection](image/Detection.png "Detection")

**- Impact !! -**  
This significantly reduces the judges’ workload, enables **daily result announcements**, and makes the competition more engaging because each day’s results are available quickly.

---

## 2) Event Context & Submitted data
We process three photo contexts:
- **Scoreboards** (handwritten/printed)  
- **Smartwatch screens** (Garmin/Apple/Android; Outdoor duration & distance)
- **Treadmill machine panels** (Indoor distance/time/pace)

![Data source](image/datasource.png "Data source")

**Collection Flow**
- Participants submit via **Google Forms** --> Photos are saved to **Google Drive**, and responses data to **Google Sheets**.

![Submit](image/Submit_data.png "Submit")

**Response Schema**
- เลือกทีมของตัวเอง (Select your team)
- รหัสพนักงาน (Employee ID)
- ระยะทาง หน่วยกิโลเมตร  (Distance in km unit)
- ลักษณะสถานที่วิ่ง (Where did you run?)
- รูปถ่ายแสดงระยะทาง Outdoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)
- รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Outdoor (Selfie)
- รูปถ่ายแสดงระยะทาง Indoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)
- รูปถ่ายระยะทางจากเครื่องออกกำลังกาย (Photo of the distance display from the exercise machine.)
- รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Indoor (Selfie)

---

## 3) Tech Stack List
- **Cloud**: Google cloud platform
- **AI**: Google Cloud Vision API (Text Detection)
- **Runtime**: Python 3.11 (Cloud Run / Cloud Functions or local)
- **Intake (Form UI)**: Google Forms
- **Data**: Google Sheets 
- **Storage**: Google Drive (form uploads)
- **Automation**: Cloud Scheduler (triggers worker)
- **Auth**: Service Account (Sheets + Drive scopes)

---

## 4) End‑to‑End Pipeline (Simplified)

### 4.1 OCR operation
- A python script - **ocr_sheet.py** on **Cloud run** calls the **Cloud Vision API** for text detection ( all on **Google Cloud** ).
- Detection results are written to a new column in the worksheet, and validation logic checks the values.

![OCR process](image/OCR_process.png "OCR process")

### 4.2 Summary result
- Summary daily datas and merge the distance and duration columns for indoor and outdoor runs using script - **summary_daily.py** 
- **Let the committee review and validate the data --> Finish !!**

![Summary step](image/Summary_step.png "Summary step")



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

