# FMA_PRO – النسخة المستقرة قبل التسليم

## الملفات الأساسية
- `FMA_PRO.ino` : ملف firmware واحد فقط
- `app.py` : تطبيق المراقبة الخارجي
- `requirements.txt` : متطلبات app.py
- `data/index.html` : الواجهة الأصلية بعد نقلها إلى LittleFS لحل مشكلة الفلاش

## لماذا يوجد ملف index.html منفصل؟
لأن هذا هو **الإصلاح الحقيقي** لمشكلة امتلاء مساحة البرنامج.
الواجهة الأصلية لم تُحذف ولم يُعاد تصميمها، وإنما نُقلت من داخل الكود إلى LittleFS حتى لا تستهلك Flash الخاص بالسكيتش.

## إعداد Arduino IDE 2.x
- Board: DOIT ESP32 DEVKIT V1
- Partition Scheme: Huge APP
- Core Debug Level: None

## المكتبات المطلوبة
- LittleFS (ضمن ESP32 Core)
- WiFi / WebServer / Wire / Preferences (ضمن ESP32 Core)

## خطوات الرفع
1. افتح مجلد `FMA_PRO`
2. افتح الملف `FMA_PRO.ino`
3. اختر اللوحة DOIT ESP32 DEVKIT V1
4. ارفع السكيتش
5. ارفع محتوى `data/index.html` إلى LittleFS

## ملاحظة مهمة
إذا رفعت السكيتش فقط ولم ترفع LittleFS، فلن تظهر الواجهة الأصلية على `/`.

## app.py
لتشغيل التطبيق الخارجي:
```bash
pip install -r requirements.txt
streamlit run app.py
```
