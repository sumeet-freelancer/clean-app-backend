import argparse
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import firestore, storage

from firebase_setup import init_firebase
from report_generation_engine import generate_report
from report_generation_pptx import generate_visual_inspection_report

ROOT = Path(__file__).resolve().parent
TEMPLATE_CONFIG_PATH = Path(os.getenv("REPORT_TEMPLATES_CONFIG", ROOT / "report-templates.json"))
WORK_ROOT = ROOT / "_report_jobs"
JST = timezone(timedelta(hours=9))
MAX_WEEKS = 4
MAX_PHOTOS_PER_WEEK = 7
PPTX_VISUAL_INSPECTION_FORMAT = "pptx-visual-inspection"


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
    return template, path


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


def record_matches_report(data, job_data):
    if data.get("locationId") != job_data["locationId"]:
        return False
    if not data.get("reportRequired"):
        return False

    report_type = str(data.get("reportType") or "").strip()
    return not report_type or report_type == job_data["reportType"]


def build_week_entries(records, job_data):
    grouped = {}
    matched_records = 0

    for doc in records:
        data = doc.to_dict() or {}
        if not record_matches_report(data, job_data):
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


def build_section_entries(records, job_data, template):
    section_configs = [
        section for section in template.get("photoSections", [])
        if section.get("id")
    ]
    sections = {
        section["id"]: {
            "id": section["id"],
            "label": section.get("label") or section["id"],
            "required": bool(section.get("required")),
            "max_photos": int(section.get("maxPhotos") or 1),
            "items": [],
            "work_dates": set(),
        }
        for section in section_configs
    }
    matched_records = 0

    for doc in records:
        data = doc.to_dict() or {}
        if not record_matches_report(data, job_data):
            continue

        work_date = to_jst_date(data["date"])
        matched_records += 1

        for photo_index, photo in enumerate(data.get("photos", []), start=1):
            section_id = str(photo.get("reportSection") or "").strip()
            if section_id not in sections:
                continue

            photo_report_type = str(photo.get("reportType") or "").strip()
            if photo_report_type and photo_report_type != job_data["reportType"]:
                continue

            path = photo.get("path")
            if not path:
                continue

            try:
                report_order = int(photo.get("reportOrder") or photo_index)
            except (TypeError, ValueError):
                report_order = photo_index

            sections[section_id]["work_dates"].add(work_date)
            sections[section_id]["items"].append({
                "path": path,
                "work_date": work_date,
                "order": report_order,
                "photo_index": photo_index,
            })

    section_entries = []
    for section in sections.values():
        items = sorted(
            section["items"],
            key=lambda item: (item["work_date"], item["order"], item["photo_index"], item["path"])
        )[:section["max_photos"]]
        section_entries.append({
            "id": section["id"],
            "label": section["label"],
            "required": section["required"],
            "photo_paths": [item["path"] for item in items],
            "work_dates": sorted(section["work_dates"]),
        })

    return section_entries, matched_records


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


def materialize_section_entries(bucket, section_entries, job_dir):
    materialized = []
    for entry in section_entries:
        photo_dir = job_dir / f"section_{entry['id']}"
        photos = download_photos(bucket, entry["photo_paths"], photo_dir)
        materialized.append({
            "id": entry["id"],
            "label": entry["label"],
            "required": entry["required"],
            "photos": photos,
            "work_dates": entry.get("work_dates", []),
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
        template, template_path = resolve_template(job_data, templates)
        template_format = str(job_data.get("templateFormat") or template.get("format") or "").strip()
        records = fetch_records(db, job_data)

        if template_format == PPTX_VISUAL_INSPECTION_FORMAT:
            section_entries, matched_records = build_section_entries(records, job_data, template)
            if not matched_records:
                raise RuntimeError("report job matched no report-enabled records")
            if not any(entry["photo_paths"] for entry in section_entries):
                raise RuntimeError("report job matched no sectioned report photos")

            materialized_sections = materialize_section_entries(bucket, section_entries, job_dir)
            photo_count = sum(len(entry["photos"]) for entry in materialized_sections)
            output_file = generate_visual_inspection_report(
                property_name=job_data.get("locationName") or "物件",
                target_month=job_data["targetMonth"],
                template_path=template_path,
                output_dir=output_dir,
                section_entries=materialized_sections,
            )
        else:
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
            "templateFormat": template_format,
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
