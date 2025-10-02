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

## 3) Prepare Tech Stack
- **Cloud**: Google cloud platform
- **AI**: Google Cloud Vision API (Text Detection)
- **Runtime**: Python 3.11 (Cloud Run / Cloud Functions or local)
- **Intake (Form UI)**: Google Forms
- **Data**: Google Sheets 
- **Storage**: Google Drive (form uploads)
- **Automation**: Cloud Scheduler (triggers worker) ******Additional!!******
- **Auth**: Service Account (Sheets + Drive scopes)

---

## 4) End‑to‑End Pipeline (Simplified)

### 4.1 OCR operation
- A python script - **ocr_sheet.py** on **Cloud run** calls the **Cloud Vision API** for text detection ( all on **Google Cloud** ).
- Detection results are written to a new column in the worksheet, and validation logic checks the values.

  How to use?
  1. Create a function **ocr_sheet** at cloud run functions service on Google cloud platform.
      1. Search "cloud run functions"
      2. Click "Write a function"
      3. Select
          - Choice : Use an lnline editor to create a function
          - Service name : ocr-sheet
          - Region : asia-southeast1 (Singapore)
          - Runtime : Python 3.13
          - Authentication : Require authentication
          - Containers, Volumes, Networking, Security --> Security
              - Service account : ocr-sheet
      4. Create
  
    ![Cloud run function](image/Cloud_run_function.png "Cloud run function")

  2. Edit the **sheet_id** in the script.
      - SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID", **"....INPUT YOUR SHEET-ID...."**)

  How to find sheet-id?
  Go to your sheet and copy your sheet id following the below picture.

    ![Sheet id](image/sheet_id.png "Sheet id")

  3. **Click Run** 

![OCR process](image/OCR_process.png "OCR process")

### 4.2 Summary result
- Summary daily datas and merge the distance and duration columns for indoor and outdoor runs using script - **summary_daily.py** 
- **Let the committee review and validate the data --> Finish !!**

  How to use?
  1. Create a function **summary_daily** at cloud run functions service on Google cloud platform.

      ** Same method with step "Create a function **ocr_sheet**".
  2. Edit the **sheet_id** in the script.
      - SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID", **"....INPUT YOUR SHEET-ID...."**)
      
      ** Same method with step "Edit the **sheet_id**" of OCR operation.
  3. **Click Run** 

![Summary step](image/Summary_step.png "Summary step")


### - Script logic and detail -  
### 4.3 Script Behavior (`ocr_sheet.py`)
**Result**
1. Distance at **column "Out_Distance_km"**
2. Duration at **column "Out_Duration_hms"**
3. Date on running record photos **column "Shot_Date"**

**Running validation**
- Distance need at least 2.00 km --> If not in condition **Column "Out_Status" = Distance Insufficient**
- Duration need not over 02:00:00 Hour --> If not in condition **Column "Out_Status" = Time Over**
- if Distance and Duration aren't in condition **Column "Out_Status" = All Cindition Insufficient**

**Outdoor validation**
- For person that runs at outdoor **("ลักษณะสถานที่วิ่ง (Where did you run?)" = กลางแจ้ง/นอกบ้าน (Outdoor))** script will check photo both of running result 

  - at **1st column** "รูปถ่ายแสดงระยะทาง Outdoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)"

  - and **2nd column** "รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Outdoor (Selfie)"
- When It found **Distance and Duration** at **1st column** --> **Column "Out_Status" =  OK**
- If It not found at **1st column**, It will continue checking at **2nd column**, and If it found **Distance and Duration** --> **Column "Out_Status" =  Miss Box**
- If not found Distance and Duration --> **Column "Out_Status" =  NG**

**Indoor validation**
- For person that runs at indoor **("ลักษณะสถานที่วิ่ง (Where did you run?)" = ในร่ม (Indoor))** 
    - Script will check **Distance** at **1st column** "รูปถ่ายระยะทางจากเครื่องออกกำลังกาย (Photo of the distance display from the exercise machine.)"
    - Script will check **Duration** at **both of 2nd column** "รูปถ่ายแสดงระยะทาง Indoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)" and 1st column "รูปถ่ายระยะทางจากเครื่องออกกำลังกาย (Photo of the distance display from the exercise machine.)" 
- If It found **Duration** at both of 1st column and 2nd column --> Choose minimum duration for answer
- If It not found **Distance and Duration** --> **Column "In_Status" =  OK**
- If It not found **Distance and Duration** --> **Column "In_Status" =  NG**

![ocrscipt_result](image/indoor_outdoor_result.png "ocrscipt_result")

### 4.4 Script Behavior (`summary_daily.py`)
**Result**
1. Column **"Distance"** : Value from merging between Distance of Outdoor and Indoor.
2. Column **"Duration"** : Value from merging between Duration of Outdoor and Indoor.
3. Column **"Value condition"** : Value from merging between "Out_Status" and "In_Status" of Outdoor and Indoor.
4. Column **"Check distance** : with input distance" : If it equal --> "OK", not equal --> "Different" , not have to compare --> "N/A"
5. Column **"Check Date"** : If it equal --> "OK", not equal --> "Different" , not have to compare --> "N/A"
6. Column **"Summary"** : if column "Value condition" = OK, "Check distance with input distance" = OK and "Check Date" = OK --> answer "OK", if not answer "NG"

![Summary result](image/summary_result.png "Summary result")

---

## 5) If you want to improve parser logic
1. Improve detecting "Distance" --> can improve at block **"Smart parsers"**
2. Improve detecting "Duration" --> can improve at block **"Smart parsers"**
3. Improve detecting "Date" --> can improve at block **Smart Date Parser (returns M/D/YYYY)"**











