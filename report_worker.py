import argparse
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, storage

from report_generation_engine import generate_report

ROOT = Path(__file__).resolve().parent
SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
TEMPLATE_CONFIG_PATH = Path(os.getenv("REPORT_TEMPLATES_CONFIG", ROOT / "report-templates.json"))
WORK_ROOT = ROOT / "_report_jobs"
BUCKET_NAME = os.getenv("FIREBASE_STORAGE_BUCKET", "seiso-app-5d532.firebasestorage.app")
JST = timezone(timedelta(hours=9))
MAX_WEEKS = 4
MAX_PHOTOS_PER_WEEK = 7


def parse_args():
    parser = argparse.ArgumentParser(description="Process queued report jobs from Firestore.")
    parser.add_argument("--job-id", help="Only process the specified Firestore reportJobs document ID.")
    parser.add_argument("--once", action="store_true", help="Process available jobs once and exit.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum queued jobs to process in one run.")
    parser.add_argument("--poll-interval", type=int, default=60, help="Seconds to wait between polling cycles.")
    return parser.parse_args()


def load_templates():
    import json

    with TEMPLATE_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return {item["id"]: item for item in data}


def month_range(target_month: str):
    start_local = datetime.strptime(f"{target_month}-01", "%Y-%m-%d").replace(tzinfo=JST)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def to_jst_date(timestamp_value):
    if hasattr(timestamp_value, "to_datetime"):
        dt = timestamp_value.to_datetime()
    else:
        dt = timestamp_value
    return dt.astimezone(JST).date()


def init_firebase():
    if firebase_admin._apps:
        return
    if SERVICE_ACCOUNT_JSON:
        cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
    elif SERVICE_ACCOUNT_PATH:
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    else:
        raise RuntimeError(
            "Set FIREBASE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS before starting the worker."
        )
    firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})


def fetch_jobs(db, args):
    if args.job_id:
        doc = db.collection("reportJobs").document(args.job_id).get()
        return [doc] if doc.exists else []

    query = db.collection("reportJobs").where("status", "==", "queued").limit(args.limit)
    return list(query.stream())


def resolve_template(job_data, templates):
    template = templates.get(job_data.get("reportType"))
    if not template:
        raise RuntimeError(f"unknown reportType: {job_data.get('reportType')}")
    path = Path(template.get("templatePath", ""))
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path


def fetch_records(db, job_data):
    start, end = month_range(job_data["targetMonth"])
    query = (
        db.collection("records")
        .where("tenantId", "==", job_data["tenantId"])
        .where("staffId", "==", job_data["staffId"])
        .where("date", ">=", start)
        .where("date", "<", end)
    )
    return list(query.stream())


def build_week_entries(records, job_data):
    grouped = {}
    matched_records = 0

    for doc in records:
        data = doc.to_dict() or {}
        if data.get("locationId") != job_data["locationId"]:
            continue
        if not data.get("reportRequired"):
            continue

        report_type = str(data.get("reportType") or "").strip()
        if report_type and report_type != job_data["reportType"]:
            continue

        work_date = to_jst_date(data["date"])
        bucket = grouped.setdefault(work_date, {"work_date": work_date, "photo_paths": []})
        matched_records += 1

        for photo in data.get("photos", []):
          path = photo.get("path")
          if path and len(bucket["photo_paths"]) < MAX_PHOTOS_PER_WEEK:
              bucket["photo_paths"].append(path)

    week_entries = sorted(grouped.values(), key=lambda item: item["work_date"])[:MAX_WEEKS]
    return week_entries, matched_records


def download_photos(bucket, photo_paths, photo_dir):
    photo_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for index, path in enumerate(photo_paths):
        blob = bucket.blob(path)
        suffix = Path(path).suffix or ".jpg"
        dest = photo_dir / f"{index:03d}{suffix}"
        blob.download_to_filename(str(dest))
        downloaded.append(dest)
    return downloaded


def materialize_week_entries(bucket, week_entries, job_dir):
    materialized = []
    for index, entry in enumerate(week_entries, start=1):
        photo_dir = job_dir / f"week_{index}"
        photos = download_photos(bucket, entry["photo_paths"][:MAX_PHOTOS_PER_WEEK], photo_dir)
        materialized.append({
            "work_date": entry["work_date"],
            "photos": photos,
            "bottom_left_label": "",
            "bottom_right_label": ""
        })
    return materialized


def upload_output(bucket, tenant_id, target_month, job_id, output_path):
    storage_path = f"tenants/{tenant_id}/reports/{target_month}/{job_id}/{output_path.name}"
    blob = bucket.blob(storage_path)
    blob.upload_from_filename(str(output_path))
    return storage_path


def process_job(db, bucket, templates, doc):
    job_data = doc.to_dict() or {}
    doc.reference.update({
        "status": "processing",
        "startedAt": firestore.SERVER_TIMESTAMP,
        "errorMessage": ""
    })

    job_dir = WORK_ROOT / doc.id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    output_dir = job_dir / "output"

    try:
        template_path = resolve_template(job_data, templates)
        records = fetch_records(db, job_data)
        grouped_entries, matched_records = build_week_entries(records, job_data)
        if not grouped_entries:
            raise RuntimeError("report job matched no report-enabled records")

        week_entries = materialize_week_entries(bucket, grouped_entries, job_dir)
        photo_count = sum(len(entry["photos"]) for entry in week_entries)
        output_file = generate_report(
            property_name=job_data.get("locationName") or "物件",
            property_code=job_data.get("propertyCode") or "",
            target_month=job_data["targetMonth"],
            template_path=template_path,
            output_dir=output_dir,
            week_entries=week_entries,
        )
        storage_path = upload_output(bucket, job_data["tenantId"], job_data["targetMonth"], doc.id, output_file)

        doc.reference.update({
            "status": "completed",
            "completedAt": firestore.SERVER_TIMESTAMP,
            "outputPath": storage_path,
            "outputFileName": output_file.name,
            "photoCount": photo_count,
            "recordCount": matched_records
        })
        print(f"[OK] {doc.id} -> {storage_path}")
    except Exception as error:
        doc.reference.update({
            "status": "failed",
            "completedAt": firestore.SERVER_TIMESTAMP,
            "errorMessage": str(error)
        })
        print(f"[FAIL] {doc.id}: {error}")


def process_available_jobs(db, bucket, templates, args):
    jobs = fetch_jobs(db, args)
    if not jobs:
        print("No queued report jobs found.")
        return 0

    for doc in jobs:
        process_job(db, bucket, templates, doc)
    return len(jobs)


def main():
    args = parse_args()
    init_firebase()
    db = firestore.client()
    bucket = storage.bucket()
    templates = load_templates()
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    if args.once or args.job_id:
        process_available_jobs(db, bucket, templates, args)
        return

    while True:
        process_available_jobs(db, bucket, templates, args)
        time.sleep(max(args.poll_interval, 10))


if __name__ == "__main__":
    main()
