# RVI Home Assignment – Post-Processing של לוג Test Track (BLF)

משימה זו מדמה תהליך Post-Processing של נתוני מסלול ניסוי: פענוח BLF+DBC, מיזוג על ציר זמן, ויזואליזציה והפקת תובנות.

## מבנה הפרויקט

```
data/                         # BLF + DBC (+ PDF הנחיות)
process_blf.py                # צינור עיבוד מלא
requirements.txt
output/
  dgps_frameid_export.csv     # ייצוא ממוזג (חובה)
  analysis_summary.json       # סיכום מספרי לניתוח
  plots/                      # גרפים HTML/PNG + דשבורד אינטראקטיבי
README.md
```

## הרצה

### CLI (כמו קודם – לא נמחק)

```bash
pip install -r requirements.txt
python process_blf.py
```

```bash
python process_blf.py --blf data/Logging_2026-07-10_12-01-57.blf --out-dir output
```

### אתר מקומי להעלאת קבצים (במקביל ל-CLI)

```bash
pip install -r requirements.txt
python web_app.py
```

Then open: [http://127.0.0.1:5000](http://127.0.0.1:5000)

1. Upload BLF + two DBC files (or re-process sample files from `data/`)
2. After processing you are redirected to `/results/<job_id>` with dashboard, plots, CSV
3. Precomputed assignment results: [http://127.0.0.1:5000/assignment-results](http://127.0.0.1:5000/assignment-results) (serves existing `output/`)
4. New web runs are stored under `web_runs/` (CLI `output/` is unchanged)

## לפני הגשה (אריזה)

- להגיש לפחות: `process_blf.py`, `requirements.txt`, `README.md`, `output/dgps_frameid_export.csv`, `output/plots/` (HTML+PNG).
- קובץ ה-BLF (~45MB) אפשר לשלוח בקישור Drive (כמו במייל המקורי) במקום לצרף למייל — אלא אם התבקש במפורש לצרף את הקובץ.
- לוודא שהבדיקה רואה את ההערה הבולטת על **fallback של TargetPosLocalX/Y**.

## חלק א' – חילוץ ומיזוג

### מקורות DBC

| עמודה | DBC | CAN ID | הודעה |
|-------|-----|--------|-------|
| frameID | ISR_mqttToCan_dbc_251210.dbc | 0x400 (1024) | mqtt_to_can_bridge_... |
| VUT_Velocity | ESP_TT_dbc_250427.dbc | 1539 | Velocity |
| Target2_Velocity | ESP_TT_dbc_250427.dbc | 1795 | TargetVelocity |
| Target2_FW_Distance, RangeTimeToCollisionForward | ESP_TT_dbc_250427.dbc | 1968 | RangeForward |
| VUT_Lat_Meter, VUT_Lng_Meter | ESP_TT_dbc_250427.dbc | 1548 | PosLocal |
| TargetPosLocalX, TargetPosLocalY | ESP_TT_dbc_250427.dbc | 1804 (primary) / 1972 (fallback) | TargetPosLocal / RangeTargetPosLocal |

> **הערה בולטת:** בלוג זה `TargetPosLocal` (1804) חסר לחלוטין. העמודות `TargetPosLocalX/Y` ב-CSV מולאו מ-`Target2_Lng_Meter` / `Target2_Lat_Meter` (1972). שמות העמודות נשמרו כנדרש במפרט.

### timestamp_sec – יחסי או מוחלט?

**יחסי.** `timestamp_sec = timestamp_abs_CAN - t_first`, כאשר `t_first` הוא חותמת הזמן של פריים ה-CAN הראשון בלוג. הזמן המוחלט נשמר פנימית בלבד למיזוג.

### גישת Time Alignment

נבחר **`pandas.merge_asof(..., direction="backward")` על גריד ~10 ms**:

1. בונים ציר זמן צפוף (חותמות FrameID + גריד סינתטי 10 ms).
2. לכל סיגנל מחברים את **הערך האחרון הידוע** בזמן ≤ לנקודת הגריד.
3. מסירים שורות ריקות לחלוטין, ואז גוזמים שורות פתיחה שבהן יש רק FrameID בלי DGPS (ייצוא נקי יותר). `timestamp_sec` נשאר יחסי לתחילת הלוג.

### מקרי קצה שטופלו בקוד

- payload קצר / כשל פענוח → נספר ומדולג
- CAN IDs לא רלוונטיים → מדולגים (סטרימינג)
- כפילויות על אותה חותמת זמן → ערך אחרון
- סיגנל חסר → NaN / fallback מתועד
- `frameID == 0` → נשמר ומדווח
- קפיצות FrameID → מזוהות עם **זמן יחסי מדויק** ומסומנות בגרף
- `frameID` מיוצא כמספר שלם (Int) ב-CSV

## חלק ב' – ויזואליזציה

בתיקייה `output/plots/`:

1. `01_velocities.html/.png` – מהירות VUT מול Target (+ סימון קטעי parallel*)
2. `02_forward_distance.html/.png` – מרחק קדמי
3. `03_ttc_forward.html/.png` – TTC קדמי
4. `04_frameid_monotonicity.html/.png` – מונוטוניות FrameID (+ annotation לקפיצות)
5. `dashboard.html/.png` – ארבעת הגרפים יחד (Plotly Zoom/Pan)
6. `05_trajectories.html` – בונוס: מסלולי XY

הסימון `parallel*` בגרפים מבוסס על **הנחת יסוד** (ראו למטה), לא על דרישה מהמפרט.

## ניתוח וממצאים

- משך ההקלטה הכולל: **866.42 s** (~14.44 דקות). קצב הגריד הממוזג: **~100.0 Hz**. קצב FrameID הגולמי בלוג: **~28.6 Hz** — נמוך מהנומינלי 100 Hz שב-DBC/PDF (ממצא חשוב לבדיקת sync/logging).
- טווח מהירות VUT: **0.000 … 28.420 m/s (ממוצע 4.006)**.
- טווח מהירות Target: **0.000 … 27.920 m/s (ממוצע 3.689)**.
- נסיעה במקביל: כן (לפי **הנחת יסוד** לזיהוי מקביליות – לא הגדרה מהמפרט). קטעים ≥2.0s:
  - 107.5s–114.0s (משך 6.5s, VUT≈10.91, Target≈11.11 m/s, הפרש צידי ממוצע≈0.10 m)
  - 771.1s–775.6s (משך 4.5s, VUT≈27.82, Target≈27.77 m/s, הפרש צידי ממוצע≈0.39 m)
- FrameID מונוטוני לא-יורד? **כן**. קפיצות לאחור=0, קפיצות גדולות(>5)=1, ערכי 0=0. חורים/קפיצות: ב־t≈0.1s: 297729→298007 (Δ=278). טווח: 297655 → 323999 (26068 דגימות גולמיות).
- **TargetPosLocalX/Y (חשוב להגשה):** הודעת CAN הראשית `0x70C` / `TargetPosLocal` **לא הופיעה בלוג** (0 פריימים). העמודות מולאו ב-fallback מתועד מ-`0x7B4` (`fallback:0x7B4/Target2_Lng_Meter`). זה פתרון הנדסי לשמירת שלמות ה-CSV — לא הסיגנל הראשי מהמפרט.
- שגיאות פענוח: 0 מתוך 466987 פריימים רלוונטיים.


## הנחות יסוד (לא דרישות מהמפרט)

> כל סעיף כאן הוא **הנחה שלנו לצורך ניתוח/יישום**. זה **אינו** מופיע כדרישה מחייבת ב-PDF.

1. **נסיעה במקביל** – הנחה: הפרש מהירות אופקית ≤ `0.5 m/s`, מהירות מינימלית `1.0 m/s`, הפרש צידי מקומי `|VUT_Lat_Meter - TargetPosLocalY| ≤ 2.0 m`, ומשך רציף ≥ `2.0 s`. המפרט שואל אם יש קטעים כאלה אך לא מגדיר סף מספרי.
2. **סף קפיצת FrameID** – הנחה: `Δ > 5` נחשב חור/sync-drop לדיווח. המפרט מציין שקפיצות גדולות חשודות, בלי סף מספרי.
3. **גריד 10 ms** – הנחה יישומית התואמת את התדר הנומינלי ~100 Hz שבמפרט FrameID; בחירת המיזוג עצמה (`merge_asof`) היא אחת מהאפשרויות שהמפרט מציע במפורש.
4. **fallback ל-TargetPosLocal** – הנחה הנדסית כשהודעת 1804 חסרה בלוג: שימוש ב-1972 תחת אותם שמות עמודות נדרשים. הדרישה היא שמות העמודות ב-CSV; מקור ה-CAN המדויק ל-1804 לא היה זמין בלוג זה.
5. **גזירת שורות פתיחה ללא DGPS** – הנחה לייצוא נקי יותר; לא נדרש במפרט.

### הערות לפרשנות

- TTC יכול להיות שלילי/רווי כשאין התקרבות אמיתית — לבחון יחד עם המרחק הקדמי.
- קצב FrameID הגולמי (~עשרות Hz) נמוך מהנומינלי 100 Hz — ייתכן דילול בלוגר/שידור; הגריד הממוזג נשאר ~100 Hz.

## תלויות

ראה `requirements.txt` (python-can, cantools, pandas, numpy, plotly, matplotlib).
