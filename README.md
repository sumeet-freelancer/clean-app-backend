# Clean App Backend

Backend worker for processing cleaning report generation jobs from Firebase Firestore.

The frontend creates documents in the `reportJobs` collection. This worker reads queued jobs, downloads report photos from Firebase Storage, fills the selected report template, uploads the generated file back to Firebase Storage, and updates the job status.

## Files

- `report_worker.py`: Firestore job worker.
- `admin_api.py`: Admin API for creating staff/admin Firebase Auth users and applying custom claims.
- `report_generation_engine.py`: Excel template filling logic.
- `report_generation_pptx.py`: PowerPoint visual inspection report filling logic.
- `report-templates.json`: report template metadata used by the worker.
- `report_templates/regular-cleaning-template.xlsx`: Excel template file.
- `report_templates/visual-inspection-template.pptx`: PowerPoint template file.
- `requirements.txt`: Python dependencies.
- `render.yaml`: Render background worker configuration.

## Required Environment Variables

Set these in Render or the hosting environment:

- `FIREBASE_SERVICE_ACCOUNT_JSON`: full Firebase service account JSON as a single environment variable.
- `FIREBASE_STORAGE_BUCKET`: Firebase Storage bucket name, currently `seiso-app-5d532.firebasestorage.app`.
- `REPORT_TEMPLATES_CONFIG`: optional. Defaults to `report-templates.json`.

Do not commit Firebase service account JSON files to this repository.

## Supported Report Types

- `regular-cleaning-weekly` / `excel-weekly-7-photo`: Existing Excel report with up to 7 photos per weekly sheet.
- `visual-inspection-monthly-pptx` / `pptx-visual-inspection`: PowerPoint report using sectioned photos. Photos must include `reportSection` and `reportOrder` metadata in each record photo entry.

## Local Test

Install dependencies:

```bash
pip install -r requirements.txt
```

Run once:

```bash
python report_worker.py --once
```

Run a specific job:

```bash
python report_worker.py --job-id YOUR_REPORT_JOB_ID
```

Run continuously:

```bash
python report_worker.py --poll-interval 60
```

## Render

Recommended services:

- Web Service: staff/admin user creation API.
- Background Worker: report generation worker.

### Web Service

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn admin_api:app
```

Required environment variables:

- `FIREBASE_SERVICE_ACCOUNT_JSON`
- `FIREBASE_STORAGE_BUCKET`
- `ALLOWED_ORIGINS`

### Background Worker

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
python report_worker.py --poll-interval 60
```
