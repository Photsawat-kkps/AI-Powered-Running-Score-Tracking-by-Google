# Sports Day Reinvented: AI‑Powered Running Score Tracking by Google

> **Who is this for?**
> - **Non‑technical readers**: See how we turned messy photos into clean, trustworthy running results.
> - **Technical readers**: Dive into the data flow, parsing rules, and code snippets (with links to deep‑dives).
>
> **How to read**
> - The **Main Story** runs from **1 → 5**.
> - Sidebars marked **(Deep‑dive)** can be skimmed now and read later.
> - Look for **🖼️ Image here** to know where to drop screenshots/diagrams.

---

## 1) Project Overview (Main Story)
**Goal:** Automate running‑result collection on Sports Day—**from photos → structured data → verified results**—to reduce manual tallying, speed up publishing, and cut human errors.

- **Problem**: Results were captured as phone photos (scoreboards, watch screens, treadmill panels). Manual typing was slow, error‑prone, and hard to audit.
- **Solution**: Use **Google Cloud Vision** to read text (OCR), then apply **domain‑specific parsing rules** to extract **time (HH:MM:SS)** and **distance (km)** reliably, auto‑validate, and export to a results sheet/dashboard.
- **Outcome**: Faster result turnaround, traceable errors, consistent scoring—**with human oversight minimized to exceptions**.

🖼️ **Image here:** A before/after collage (raw scoreboard photo → highlighted OCR text → clean table of results)

**Key highlights**
- Images → Vision API → text lines → smart parser (time vs. pace vs. random numbers) → **validated rows**
- Outdoor vs. Indoor flows handled (watch photo vs. treadmill panel)
- Automatic anomaly checks (e.g., impossible distance/time, mixed punctuation “01.13.52”)

> **Deep‑dive references**: [Appendix A. Parsing Rules](#appendix-a-parsing-rules-cheat-sheet), [Appendix B. Data Validation](#appendix-b-data-validation--error-handling)

---

## 2) What We OCR (Contexts & Inputs) (Main Story)
**Event Context:** School/office **Sports Day** with **running events** (e.g., 100m, 5km fun run, relay). Data sources include:

- **Scoreboards** (handwritten/printed; photos from phones)
- **Watch screenshots** (Garmin/Apple/Android) showing **elapsed time** and **distance**
- **Treadmill panels** (Indoor) showing **time, distance, pace**

**Input artifacts**
- Photo uploads via **Google Form** → saved in **Google Drive**
- Form metadata (team, runner ID, outdoor/indoor, optional notes) → **Google Sheet** (`Form Responses 1`)

🖼️ **Image here:** Example scoreboard photo + watch screenshot + treadmill panel (3‑up grid)

> **Deep‑dive:** [Appendix C. Sample Dataset & Annotations](#appendix-c-sample-dataset--annotations)

---

## 3) Tech Stack (Quick) (Main Story)
- **Cloud OCR**: **Google Cloud Vision API** (Text Detection)
- **Compute**: Python 3.11 (local or Cloud Run/Functions)
- **Data**: Google Sheets (working tab + output tab), pandas for transforms
- **Automation (optional)**: Cloud Scheduler → trigger runner; Cloud Storage for images
- **Auth/Access**: Service Account (JSON key or Workload Identity)
- **Logging**: Python logging (JSON), optional export to a dashboard

```
# Minimal client stub (Python)
from google.cloud import vision

client = vision.ImageAnnotatorClient()
response = client.text_detection(image=vision.Image(content=img_bytes))
text = response.full_text_annotation.text
```

> **Deep‑dive:** [Appendix D. Setup & Deployment](#appendix-d-setup--deployment)

---

## 4) Pipeline (End‑to‑End) (Main Story)

**At a glance**
```
Phone Photo → Google Form → Google Drive
                  ↓
            Form Responses (Sheet)
                  ↓  (trigger)
           OCR Worker (Python)
                  ↓
         Vision API → text blocks
                  ↓
  Smart Parser (time, km, pace; rules)
                  ↓
     Validation & Anomaly detection
                  ↓
  Working Sheet  →  Results Sheet/Dashboard
```

🖼️ **Image here:** Clean architecture diagram (boxes/arrows). Keep this near the top for non‑tech readers.

**Detailed steps**
1. **Ingest**: New form rows detected (or batch backfill for empty results).
2. **Fetch media**: Download photo from Drive; preprocess (resize, grayscale if needed).
3. **OCR**: Vision API → full text + per‑block coordinates (if needed later).
4. **Parsing**:
   - Prefer real **time patterns** `HH:MM:SS` or `MM:SS`.
   - Accept `MM:SS.ff` (e.g., `04:53.79`) as **valid time**; map `.ff` to fractional seconds.
   - Resolve **mixed punctuation**: `01.13.52` / `01.13:52` → normalize to `01:13:52` only if unambiguous.
   - **Distance candidates**: decimals in **2.0–35.0 km** (configurable) to filter out calories/randoms.
   - **Anchor logic**: if a clear distance is found near keywords (“km”, “distance”), remaining time‑like tokens are treated as **time**, not pace.
   - Avoid false dates like `23.09.2025` when extracting time.
5. **Validation**: cross‑check time↔distance ranges (e.g., 2 km cannot have 2 hours), indoor vs. outdoor heuristics.
6. **Write‑back**: Structured row → **Working Sheet** (with flags) → **Results Sheet**.
7. **Notify (optional)**: Slack/Email for anomalies or low OCR confidence.

> **Deep‑dives**: [Appendix A](#appendix-a-parsing-rules-cheat-sheet), [Appendix B](#appendix-b-data-validation--error-handling)

---

## 5) Results (Main Story)
**What improved?**
- **Turnaround time**: Faster publishing (no manual retyping)
- **Accuracy**: Domain rules eliminate common OCR slips (e.g., pace mistaken as time)
- **Auditability**: Every row links back to the original image + parser decision
- **Scalability**: Add more devices/photos without adding staff

🖼️ **Image here:**
- **Before/After table**: Left = raw OCR text; Right = final columns (Time, Distance, Validity, Event, Team)
- **Dashboard snapshot**: Topline stats (finishers, avg pace, fastest times) per color/team

**Example output columns**
- `Timestamp` (from form)
- `Runner ID`, `Team`
- `Event Type` (Outdoor/Indoor)
- `Time_HHMMSS`, `Distance_km`
- `Validation_Status` (`OK / Different / N/A`)
- `Notes` (auto or reviewer)

> **Deep‑dive**: [Appendix E. Result Schema](#appendix-e-result-schema)

---

## (Optional) 6) Why This Works (For Both Audiences)
- **For organizers**: Less chaos on event day, faster announcements, fewer disputes.
- **For engineers**: Domain‑aware parsing + layered validation beats generic OCR for numeric panels/scoreboards.

---

## (Optional) 7) Limitations & Next Steps
**Known limits**
- Blurry/angled photos, reflective panels, overlapped digits
- Mixed language UIs; unusual fonts
- Edge cases: split screens with multiple times; long‑run times > 2h with tiny text

**Next steps**
- Add **AutoML / custom prompts** for scoreboard layouts
- Confidence scoring + human‑in‑the‑loop review UI
- **Privacy**: face blurring for public reports; consent checklist

---

## (Optional) 8) Quickstart (Tech)
> Keep this short in README; link to the appendix for detailed steps.

1) **Prereqs**
- Python 3.11, `pip install -r requirements.txt`
- Google Cloud project + Vision API enabled
- Service account key available

2) **Run locally**
```
export GOOGLE_APPLICATION_CREDENTIALS=path/to/key.json
python ocr_runner.py --sheet "Form Responses 1" --out "Working"
```

3) **Deploy (one‑liner idea)**
- Cloud Run/Functions with scheduler trigger; see [Appendix D](#appendix-d-setup--deployment)

---

# Appendices (Deep‑dives)

## Appendix A. Parsing Rules (Cheat Sheet)
- **Time**
  - Prefer strict `HH:MM:SS` or `MM:SS`
  - Accept `MM:SS.ff` (e.g., `04:53.79`) → treat `.ff` as fractional seconds
  - Reject likely **dates** (e.g., `23.09.2025`)
  - Normalize mixed separators: `01.13.52` → `01:13:52` only if context supports
- **Distance (km)**
  - Accept decimals **2.0–35.0** (tunable)
  - Favor tokens near `km`, `KM`, `Distance`, or typical device labels
- **Anchor precedence**
  - If a plausible distance is anchored, remaining time‑like tokens → **time** (not pace)
- **Indoor vs. Outdoor**
  - Indoor: prefer treadmill’s panel values (time, distance) when present
  - Outdoor: prefer watch elapsed time; avoid misreading pace `MM:SS /km` as time

## Appendix B. Data Validation & Error Handling
- **Consistency checks**
  - Unlikely pairs flagged (e.g., 2.0 km with 02:00:00 → `Different`)
  - Missing either time or distance → `N/A`
- **Confidence**
  - Low OCR confidence or conflicting tokens → require review
- **Logging**
  - JSON logs (`timestamp`, `runner_id`, `decision`, `reason`) for traceability

## Appendix C. Sample Dataset & Annotations
- Curate a small set (10–20 images): scoreboard, watch, treadmill
- Provide: original image, OCR text dump, parser decision, validation status
- **🖼️ Image grid here** with short captions

## Appendix D. Setup & Deployment
- Enable Vision API; create service account
- Grant Drive/Sheets read/write scopes (if using gspread/Google API client)
- Environment vars: credentials path, sheet names, image bucket/folder
- (Optional) Cloud Run/Functions template and scheduler cron

## Appendix E. Result Schema
| Column | Type | Notes |
|---|---|---|
| Timestamp | datetime | From Google Form |
| Runner ID | string | From form |
| Team | string | e.g., Blue / Green |
| Event Type | enum | Outdoor / Indoor |
| Time_HHMMSS | string | Normalized time |
| Distance_km | float | 2.00–35.00 |
| Validation_Status | enum | OK / Different / N/A |
| Notes | string | Parser comment or reviewer |

---

## Credits & Ethics
- Built with **Google Cloud Vision** (pre‑trained AI for text detection)
- Respect **privacy**: obtain consent; blur faces before public sharing
- Acknowledge volunteers who submitted images and helped label edge cases

---

### Visual Placement Guide (Quick)
- **Top of README**: Before/after collage (hook non‑tech readers)
- **Section 4**: Architecture diagram (one clear graphic)
- **Section 5**: Table snapshot + dashboard screenshot
- **Appendix C**: Multi‑image grid with captions

> Keep the main story brisk. Push regexes, full code, and cost/quotas to the **appendices** to keep momentum for mixed audiences.

