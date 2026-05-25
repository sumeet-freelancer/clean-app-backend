import json
import os

import firebase_admin
from firebase_admin import credentials


def init_firebase():
    if firebase_admin._apps:
        return

    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    service_account_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    storage_bucket = os.getenv("FIREBASE_STORAGE_BUCKET", "seiso-app-5d532.firebasestorage.app")

    if service_account_json:
        cred = credentials.Certificate(json.loads(service_account_json))
    elif service_account_path:
        cred = credentials.Certificate(service_account_path)
    else:
        raise RuntimeError(
            "Set FIREBASE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS before starting."
        )

    firebase_admin.initialize_app(cred, {"storageBucket": storage_bucket})
