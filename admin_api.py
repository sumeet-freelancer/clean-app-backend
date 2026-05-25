import os
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from firebase_admin import auth, firestore

from firebase_setup import init_firebase


SUPER_ADMIN_ROLES = {"host", "superadmin"}
TENANT_ADMIN_ROLES = {"tenantadmin", "companyadmin"}
ADMIN_ROLES = SUPER_ADMIN_ROLES | TENANT_ADMIN_ROLES
ALLOWED_ROLES = {"staff", "companyadmin", "tenantadmin"}
DEFAULT_ALLOWED_ORIGINS = {
    "https://seiso-app-5d532.web.app",
    "https://seiso-app-5d532.firebaseapp.com",
    "http://localhost:5000",
    "http://127.0.0.1:5000",
}

init_firebase()
db = firestore.client()
app = Flask(__name__)


def get_allowed_origins():
    raw = os.getenv("ALLOWED_ORIGINS", "")
    configured = {item.strip() for item in raw.split(",") if item.strip()}
    return configured or DEFAULT_ALLOWED_ORIGINS


def json_response(payload, status=200):
    response = jsonify(payload)
    origin = request.headers.get("Origin")
    if origin in get_allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response, status


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in get_allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def require_admin_context():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, ("missing_authorization", 401)

    token = header.split(" ", 1)[1].strip()
    if not token:
        return None, ("missing_authorization", 401)

    try:
        decoded = auth.verify_id_token(token)
    except Exception:
        return None, ("invalid_token", 401)

    role = str(decoded.get("role") or "").strip()
    tenant_id = str(decoded.get("tenantId") or "").strip() or None
    tenant_ids = decoded.get("tenantIds") if isinstance(decoded.get("tenantIds"), list) else []

    if role not in ADMIN_ROLES:
        return None, ("admin_required", 403)

    return {
        "uid": decoded.get("uid"),
        "email": decoded.get("email"),
        "role": role,
        "tenant_id": tenant_id,
        "tenant_ids": [str(item).strip() for item in tenant_ids if str(item).strip()],
        "is_superadmin": role in SUPER_ADMIN_ROLES,
    }, None


def normalize_email(value):
    return str(value or "").strip().lower()


def clean_string(value):
    return str(value or "").strip()


def unique_values(values):
    result = []
    seen = set()
    for value in values:
        clean = clean_string(value)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def invite_doc_id(tenant_id, role, email):
    return f"{tenant_id}__{role}__{email}"


def validate_target_tenant(admin_context, target_tenant_id):
    if admin_context["is_superadmin"]:
        if not target_tenant_id:
            return None, "target_tenant_required"
        return target_tenant_id, None

    allowed = unique_values([admin_context["tenant_id"], *admin_context["tenant_ids"]])
    if not allowed:
        return None, "admin_tenant_missing"
    if target_tenant_id and target_tenant_id not in allowed:
        return None, "tenant_not_allowed"
    return allowed[0], None


def validate_target_role(admin_context, target_role):
    if target_role not in ALLOWED_ROLES:
        return "invalid_role"
    if not admin_context["is_superadmin"] and target_role != "staff":
        return "tenant_admin_can_create_staff_only"
    return None


def build_claims(role, tenant_id):
    claims = {"role": role}
    if tenant_id:
        claims["tenantId"] = tenant_id
        claims["tenantIds"] = [tenant_id]
    return claims


def get_or_create_user(email, password, display_name):
    try:
        user = auth.get_user_by_email(email)
        updates = {}
        if display_name:
            updates["display_name"] = display_name
        if password:
            updates["password"] = password
        if updates:
            user = auth.update_user(user.uid, **updates)
        return user, False
    except auth.UserNotFoundError:
        return auth.create_user(
            email=email,
            password=password,
            display_name=display_name or None,
            email_verified=False,
        ), True


@app.route("/health", methods=["GET"])
def health():
    return json_response({"ok": True, "service": "clean-app-admin-api"})


@app.route("/api/staff-users", methods=["OPTIONS"])
def staff_users_options():
    return json_response({"ok": True})


@app.route("/api/staff-users", methods=["POST"])
def create_staff_user():
    admin_context, error = require_admin_context()
    if error:
        code, status = error
        return json_response({"ok": False, "error": code}, status)

    payload = request.get_json(silent=True) or {}
    email = normalize_email(payload.get("email"))
    password = clean_string(payload.get("password"))
    display_name = clean_string(payload.get("displayName") or payload.get("name"))
    target_role = clean_string(payload.get("role") or "staff")
    requested_tenant_id = clean_string(payload.get("tenantId")) or None

    if not email:
        return json_response({"ok": False, "error": "email_required"}, 400)
    if not password or len(password) < 6:
        return json_response({"ok": False, "error": "password_min_6_required"}, 400)

    role_error = validate_target_role(admin_context, target_role)
    if role_error:
        return json_response({"ok": False, "error": role_error}, 403)

    tenant_id, tenant_error = validate_target_tenant(admin_context, requested_tenant_id)
    if tenant_error:
        return json_response({"ok": False, "error": tenant_error}, 403)

    tenant_doc = db.collection("tenants").document(tenant_id).get()
    if not tenant_doc.exists:
        return json_response({"ok": False, "error": "tenant_not_found"}, 404)

    try:
        user, created = get_or_create_user(email, password, display_name)
        claims = build_claims(target_role, tenant_id)
        auth.set_custom_user_claims(user.uid, claims)

        now = firestore.SERVER_TIMESTAMP
        staff_payload = {
            "uid": user.uid,
            "email": email,
            "role": target_role,
            "tenantId": tenant_id,
            "tenantIds": [tenant_id],
            "disabled": False,
            "status": "active",
            "updatedAt": now,
        }
        if display_name:
            staff_payload["name"] = display_name
        if created:
            staff_payload["createdAt"] = now

        db.collection("staffs").document(user.uid).set(staff_payload, merge=True)

        invite_ref = db.collection("userInvites").document(invite_doc_id(tenant_id, target_role, email))
        invite_ref.set({
            "email": email,
            "displayName": display_name,
            "role": target_role,
            "tenantId": tenant_id,
            "status": "claims_applied",
            "resolvedUid": user.uid,
            "resolvedAt": now,
            "createdBy": admin_context["uid"],
            "updatedAt": now,
        }, merge=True)

        return json_response({
            "ok": True,
            "created": created,
            "uid": user.uid,
            "email": email,
            "role": target_role,
            "tenantId": tenant_id,
            "claims": claims,
            "processedAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        app.logger.exception("staff user registration failed")
        return json_response({"ok": False, "error": "registration_failed", "message": str(exc)}, 500)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
