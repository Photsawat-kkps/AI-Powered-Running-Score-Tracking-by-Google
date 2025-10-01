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


### Additional Project Details
### 4.3 Job Behavior (`ocr_sheet.py`)
**Running validation**
- Distance need at least 2.00 km --> If not in condition **Column "Value condition" = Distance Insufficient**
- Duration need not over 02:00:00 Hour --> If not in condition **Column "Value condition" = Time Over**

**Outdoor validation**
- This script will check photo both of running result 

  - at **1st column** "รูปถ่ายแสดงระยะทาง Outdoor และเวลาจากอุปกรณ์สมาร์ทวอทช์ หรือแอปพลิเคชันจากมือถือ  (Photo showing distance and time from a smartwatch or mobile application)"

  - and **2nd column** "รูปถ่ายตัวเองระหว่างร่วมกิจกรรมแบบ Outdoor (Selfie)"
- When It found **Distance and Duration** at **1st column** --> **Column "Value condition" =  OK**
- If It not found at **1st column**, It will continue checking at **2nd column**, and If it found **Distance and Duration** --> **Column "Value condition" =  Miss Box**
- If not found Distance and Duration --> **Column "Value condition" =  NG**

**Indoor validation**




