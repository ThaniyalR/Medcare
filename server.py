#!/usr/bin/env python3
"""Nutrix application API. Standard-library backend; provider secrets stay server-side."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("NUTRIX_DB", ROOT / "data" / "nutrix.sqlite3"))
APP_ENV = os.environ.get("APP_ENV", "development").lower()
PORT = int(os.environ.get("PORT", "8000"))
COOKIE_NAME = "nutrix_session"
SESSION_DAYS = 7
OTP_TTL = 5 * 60
MAX_BODY = 64 * 1024
CONSENT_VERSION = "nutrix-privacy-v1"
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
ROLES = {"patient", "nurse", "caretaker"}
PLANS = {
    "caregiver-4h": {"title": "Care assistant · 4 hours", "role": "caretaker", "amount": 120000},
    "caregiver-8h": {"title": "Care assistant · 8 hours", "role": "caretaker", "amount": 220000},
    "nurse-visit": {"title": "Registered nurse · home visit", "role": "nurse", "amount": 90000},
}
RATE_BUCKETS: dict[str, list[float]] = {}


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT NOT NULL,
              phone TEXT NOT NULL UNIQUE, password_hash BLOB NOT NULL,
              credential_type TEXT, credential_id TEXT,
              phone_verified INTEGER NOT NULL DEFAULT 0,
              consent_version TEXT NOT NULL DEFAULT 'nutrix-privacy-v1',
              consent_at INTEGER NOT NULL DEFAULT 0,
              review_status TEXT NOT NULL DEFAULT 'approved',
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registrations (
              id TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT NOT NULL,
              phone TEXT NOT NULL UNIQUE, password_hash BLOB NOT NULL,
              credential_type TEXT, credential_id TEXT,
              consent_version TEXT NOT NULL DEFAULT 'nutrix-privacy-v1',
              consent_at INTEGER NOT NULL DEFAULT 0,
              provider_sid TEXT, dev_code_hash BLOB,
              expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash BLOB PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bookings (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
              plan_key TEXT NOT NULL, service_title TEXT NOT NULL,
              required_role TEXT NOT NULL, scheduled_at TEXT NOT NULL,
              amount_paise INTEGER NOT NULL, currency TEXT NOT NULL,
              order_id TEXT UNIQUE, payment_id TEXT UNIQUE,
              status TEXT NOT NULL, assigned_staff_id TEXT REFERENCES users(id),
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS patient_profiles (
              user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              conditions TEXT NOT NULL DEFAULT '',
              allergies TEXT NOT NULL DEFAULT '',
              mobility TEXT NOT NULL DEFAULT '',
              care_notes TEXT NOT NULL DEFAULT '',
              emergency_contact TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS bookings_open ON bookings(status, required_role, scheduled_at);
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
            """
        )
        for table, columns in {
            "users": {
                "consent_version": "TEXT NOT NULL DEFAULT 'nutrix-privacy-v1'",
                "consent_at": "INTEGER NOT NULL DEFAULT 0",
            },
            "registrations": {
                "consent_version": "TEXT NOT NULL DEFAULT 'nutrix-privacy-v1'",
                "consent_at": "INTEGER NOT NULL DEFAULT 0",
            },
        }.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        conn.execute("DELETE FROM registrations WHERE expires_at < ?", (int(time.time()),))


def password_hash(password: str) -> bytes:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt + derived


def password_matches(password: str, stored: bytes) -> bool:
    salt, expected = stored[:16], stored[16:]
    actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, expected)


def twilio_configured() -> bool:
    return all(setting(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID"))


def local_otp_allowed() -> bool:
    return APP_ENV == "development" and setting("ALLOW_LOCAL_OTP", "true").lower() == "true"


def phone_verification_skipped() -> bool:
    return APP_ENV in ("development", "demo") and setting("SKIP_PHONE_VERIFICATION").lower() == "true"


def provider_post(url: str, fields: dict[str, str], username: str, password: str) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    basic = base64.b64encode((username + ":" + password).encode()).decode()
    request.add_header("Authorization", "Basic " + basic)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read(256 * 1024))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(502, "The payment or messaging provider could not be reached. Please try again.") from exc
    return result


def twilio_url(suffix: str) -> str:
    return (
        "https://verify.twilio.com/v2/Services/"
        + urllib.parse.quote(setting("TWILIO_VERIFY_SERVICE_SID"), safe="")
        + suffix
    )


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def send_verification(phone: str) -> tuple[str | None, bytes | None]:
    twilio_values = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID")
    if any(setting(key) for key in twilio_values) and not twilio_configured():
        raise ApiError(503, "SMS verification settings are incomplete. Contact the Nutrix administrator.")
    if twilio_configured():
        result = provider_post(
            twilio_url("/Verifications"),
            {"To": phone, "Channel": "sms"},
            setting("TWILIO_ACCOUNT_SID"),
            setting("TWILIO_AUTH_TOKEN"),
        )
        sid = result.get("sid")
        if not sid or result.get("status") not in ("pending", "approved"):
            raise ApiError(502, "The SMS provider could not start a verification.")
        return str(sid), None
    if local_otp_allowed():
        code = f"{secrets.randbelow(1_000_000):06d}"
        print(f"[development OTP] {phone}: {code}", flush=True)
        return None, hashlib.sha256(code.encode()).digest()
    raise ApiError(503, "SMS verification is not configured. Contact the Nutrix administrator.")


def check_verification(phone: str, code: str, provider_sid: str | None, dev_hash: bytes | None) -> bool:
    if provider_sid and twilio_configured():
        result = provider_post(
            twilio_url("/VerificationCheck"),
            {"To": phone, "Code": code},
            setting("TWILIO_ACCOUNT_SID"),
            setting("TWILIO_AUTH_TOKEN"),
        )
        return result.get("status") == "approved"
    if APP_ENV == "development" and dev_hash:
        return hmac.compare_digest(hashlib.sha256(code.encode()).digest(), dev_hash)
    return False


def razorpay_configured() -> bool:
    return bool(setting("RAZORPAY_KEY_ID") and setting("RAZORPAY_KEY_SECRET"))


def razorpay_order(amount: int, receipt: str) -> dict:
    credentials = base64.b64encode(
        (setting("RAZORPAY_KEY_ID") + ":" + setting("RAZORPAY_KEY_SECRET")).encode()
    ).decode()
    payload = json.dumps(
        {"amount": amount, "currency": "INR", "receipt": receipt, "payment_capture": 1}
    ).encode()
    request = urllib.request.Request(
        "https://api.razorpay.com/v1/orders", data=payload, method="POST"
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Basic " + credentials)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read(256 * 1024))
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        print(f"Razorpay order request failed ({exc.code}): {detail}", flush=True)
        raise ApiError(502, "Razorpay could not create the payment order.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(502, "Razorpay could not be reached. Please try again.") from exc


def public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "name": row["name"],
        "phone": row["phone"],
        "phoneVerified": bool(row["phone_verified"]),
        "reviewStatus": row["review_status"],
        "credentialType": row["credential_type"],
        "credentialId": row["credential_id"],
    }


def money(amount: int) -> dict:
    return {"amountPaise": amount, "currency": "INR", "display": f"₹{amount / 100:,.2f}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Nutrix/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, status: int, data: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "Invalid request size.") from exc
        if length <= 0 or length > MAX_BODY:
            raise ApiError(413 if length > MAX_BODY else 400, "Request body is empty or too large.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "Request must contain valid JSON.") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "Request must be a JSON object.")
        return value

    def rate_limit(self, bucket: str, limit: int, window: int = 60) -> None:
        now = time.time()
        samples = [stamp for stamp in RATE_BUCKETS.get(bucket, []) if now - stamp < window]
        if len(samples) >= limit:
            raise ApiError(429, "Too many attempts. Wait a little and try again.")
        samples.append(now)
        RATE_BUCKETS[bucket] = samples

    def check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            parsed = urllib.parse.urlsplit(origin)
            if parsed.netloc != host or parsed.scheme not in ("http", "https"):
                raise ApiError(403, "Request origin was not allowed.")

    def current_user(self) -> sqlite3.Row:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(COOKIE_NAME)
        if not morsel:
            raise ApiError(401, "Please sign in to continue.")
        token_hash = hashlib.sha256(morsel.value.encode()).digest()
        with db() as conn:
            row = conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>?",
                (token_hash, int(time.time())),
            ).fetchone()
        if row is None:
            raise ApiError(401, "Your session has expired. Please sign in again.")
        return row

    def require_role(self, user: sqlite3.Row, *roles: str) -> None:
        if user["role"] not in roles:
            raise ApiError(403, "This action is not available for your account type.")

    def require_reviewed(self, user: sqlite3.Row) -> None:
        if user["role"] in ("nurse", "caretaker") and user["review_status"] != "approved":
            raise ApiError(403, "Your professional credentials are awaiting administrator review.")

    def do_GET(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/api/"):
                self.handle_api_get(path)
            else:
                self.serve_static(path)
        except ApiError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except (sqlite3.Error, OSError) as exc:
            print(f"GET {self.path} failed: {exc}", flush=True)
            self.send_json(500, {"error": "The request could not be completed. Please try again."})

    def do_POST(self) -> None:
        try:
            self.check_origin()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/payments/webhook":
                self.payment_webhook()
                return
            data = self.read_json()
            self.handle_api_post(path, data)
        except ApiError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except (sqlite3.Error, OSError) as exc:
            print(f"POST {self.path} failed: {exc}", flush=True)
            self.send_json(500, {"error": "The request could not be completed. Please try again."})

    def handle_api_get(self, path: str) -> None:
        if path == "/api/config":
            self.send_json(
                200,
                {
                    "razorpayKeyId": setting("RAZORPAY_KEY_ID") or None,
                    "smsReady": twilio_configured() or local_otp_allowed(),
                    "localOtp": local_otp_allowed(),
                    "phoneVerificationRequired": not phone_verification_skipped(),
                    "environment": APP_ENV,
                },
            )
            return
        if path == "/api/admin/reviews":
            if not setting("ADMIN_REVIEW_KEY") or not hmac.compare_digest(
                self.headers.get("X-Admin-Key", ""), setting("ADMIN_REVIEW_KEY")
            ):
                raise ApiError(403, "Administrator review access is not configured or authorized.")
            with db() as conn:
                rows = conn.execute(
                    "SELECT id, name, phone, role, credential_type, credential_id, created_at "
                    "FROM users WHERE review_status='pending' ORDER BY created_at"
                ).fetchall()
            self.send_json(200, {"reviews": [dict(r) for r in rows]})
            return
        user = self.current_user()
        if path == "/api/me":
            self.send_json(200, {"user": public_user(user)})
            return
        if path == "/api/dashboard":
            self.require_reviewed(user)
            self.dashboard(user)
            return
        raise ApiError(404, "API endpoint not found.")

    def handle_api_post(self, path: str, data: dict) -> None:
        if path == "/api/auth/register":
            self.register(data)
        elif path == "/api/auth/verify":
            self.verify_registration(data)
        elif path == "/api/auth/login":
            self.login(data)
        elif path == "/api/auth/logout":
            self.logout()
        elif path == "/api/patient/profile":
            self.save_patient_profile(data)
        elif path == "/api/payments/order":
            self.create_payment_order(data)
        elif path == "/api/payments/verify":
            self.verify_payment(data)
        elif path == "/api/bookings":
            self.create_payment_order(data)
        elif path.startswith("/api/bookings/") and path.endswith("/accept"):
            self.accept_booking(path.split("/")[-2])
        elif path.startswith("/api/admin/reviews/") and path.endswith("/decision"):
            self.review_staff(path.split("/")[-2], data)
        else:
            raise ApiError(404, "API endpoint not found.")

    def register(self, data: dict) -> None:
        self.rate_limit(self.client_address[0] + ":register", 5, 3600)
        role = str(data.get("role", "")).lower()
        name = str(data.get("name", "")).strip()
        phone = str(data.get("phone", "")).strip()
        password = str(data.get("password", ""))
        if role not in ROLES:
            raise ApiError(400, "Choose patient, nurse, or caretaker.")
        if len(name) < 2 or len(name) > 100:
            raise ApiError(400, "Enter your name (2–100 characters).")
        if not PHONE_RE.fullmatch(phone):
            raise ApiError(400, "Enter a valid mobile number in international format, e.g. +919876543210.")
        if len(password) < 10 or len(password) > 128:
            raise ApiError(400, "Choose a password between 10 and 128 characters.")
        if data.get("privacyConsent") is not True:
            raise ApiError(400, "Accept the privacy notice before creating your account.")
        credential_type = credential_id = None
        if role in ("nurse", "caretaker"):
            credential_type = str(data.get("credentialType", "")).strip()
            credential_id = str(data.get("credentialId", "")).strip()
            if len(credential_type) < 2 or len(credential_type) > 80:
                raise ApiError(400, "Enter your professional credential type.")
            if len(credential_id) < 3 or len(credential_id) > 100:
                raise ApiError(400, "Enter your professional credential or registration number.")
        now = int(time.time())
        with db() as conn:
            if conn.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
                raise ApiError(409, "An account with this mobile number already exists. Sign in instead.")
            pending = conn.execute("SELECT 1 FROM registrations WHERE phone=?", (phone,)).fetchone()
            if pending:
                self.rate_limit(phone + ":register", 3, 900)
            conn.execute("DELETE FROM registrations WHERE phone=?", (phone,))
        if phone_verification_skipped():
            review = "pending" if role in ("nurse", "caretaker") else "approved"
            user_id, token, expires = secrets.token_urlsafe(18), secrets.token_urlsafe(32), now + SESSION_DAYS * 86400
            with db() as conn:
                conn.execute(
                    "INSERT INTO users(id,role,name,phone,password_hash,credential_type,credential_id,"
                    "phone_verified,consent_version,consent_at,review_status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, role, name, phone, password_hash(password), credential_type,
                        credential_id, 0, CONSENT_VERSION, now, review, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (hashlib.sha256(token.encode()).digest(), user_id, expires, now),
                )
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            self.send_json(
                201,
                {"user": public_user(row), "message": "Account created. Mobile number is not verified."},
                {"Set-Cookie": self.session_cookie(token)},
            )
            return
        provider_sid, dev_hash = send_verification(phone)
        with db() as conn:
            conn.execute(
                "INSERT INTO registrations(id,role,name,phone,password_hash,credential_type,credential_id,"
                "consent_version,consent_at,provider_sid,dev_code_hash,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    secrets.token_urlsafe(18), role, name, phone, password_hash(password),
                    credential_type, credential_id, CONSENT_VERSION, now, provider_sid, dev_hash,
                    now + OTP_TTL, now,
                ),
            )
        self.send_json(201, {"message": "Verification code sent to your mobile number.", "expiresInSeconds": OTP_TTL})

    def verify_registration(self, data: dict) -> None:
        if phone_verification_skipped():
            raise ApiError(409, "Mobile verification is disabled in this local development environment.")
        self.rate_limit(self.client_address[0] + ":verify", 12, 900)
        phone, code = str(data.get("phone", "")).strip(), str(data.get("code", "")).strip()
        if not PHONE_RE.fullmatch(phone) or not re.fullmatch(r"\d{6}", code):
            raise ApiError(400, "Enter the registered mobile number and six-digit verification code.")
        with db() as conn:
            pending = conn.execute("SELECT * FROM registrations WHERE phone=?", (phone,)).fetchone()
            if pending is None:
                raise ApiError(400, "No pending signup was found. Please sign up again.")
            if pending["expires_at"] < int(time.time()):
                conn.execute("DELETE FROM registrations WHERE phone=?", (phone,))
                conn.commit()
                raise ApiError(410, "That verification code expired. Please sign up again.")
            if pending["attempts"] >= 5:
                conn.execute("DELETE FROM registrations WHERE phone=?", (phone,))
                conn.commit()
                raise ApiError(429, "Too many incorrect codes. Please start signup again.")
            valid = check_verification(phone, code, pending["provider_sid"], pending["dev_code_hash"])
            if not valid:
                conn.execute("UPDATE registrations SET attempts=attempts+1 WHERE phone=?", (phone,))
                conn.commit()
                raise ApiError(400, "The verification code is incorrect or expired.")
            review = "pending" if pending["role"] in ("nurse", "caretaker") else "approved"
            user_id, now = secrets.token_urlsafe(18), int(time.time())
            conn.execute(
                "INSERT INTO users(id,role,name,phone,password_hash,credential_type,credential_id,"
                "phone_verified,consent_version,consent_at,review_status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, pending["role"], pending["name"], phone, pending["password_hash"],
                    pending["credential_type"], pending["credential_id"], 1, pending["consent_version"],
                    pending["consent_at"], review, now,
                ),
            )
            conn.execute("DELETE FROM registrations WHERE phone=?", (phone,))
            token, expires = secrets.token_urlsafe(32), now + SESSION_DAYS * 86400
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).digest(), user_id, expires, now),
            )
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        self.send_json(
            201,
            {"user": public_user(row), "message": "Mobile number verified."},
            {"Set-Cookie": self.session_cookie(token)},
        )

    def login(self, data: dict) -> None:
        self.rate_limit(self.client_address[0] + ":login", 20, 900)
        phone, password = str(data.get("phone", "")).strip(), str(data.get("password", ""))
        if not PHONE_RE.fullmatch(phone) or not password:
            raise ApiError(400, "Enter your mobile number and password.")
        with db() as conn:
            row = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if row is None or not password_matches(password, row["password_hash"]):
            raise ApiError(401, "Mobile number or password is incorrect.")
        if not row["phone_verified"] and not phone_verification_skipped():
            raise ApiError(403, "Verify your mobile number before signing in.")
        token, expires = secrets.token_urlsafe(32), int(time.time()) + SESSION_DAYS * 86400
        with db() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).digest(), row["id"], expires, int(time.time())),
            )
        self.send_json(200, {"user": public_user(row)}, {"Set-Cookie": self.session_cookie(token)})

    def session_cookie(self, token: str) -> str:
        secure = "; Secure" if APP_ENV in ("demo", "production") else ""
        return f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_DAYS * 86400}{secure}"

    def logout(self) -> None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(COOKIE_NAME)
        if morsel:
            with db() as conn:
                conn.execute(
                    "DELETE FROM sessions WHERE token_hash=?",
                    (hashlib.sha256(morsel.value.encode()).digest(),),
                )
        secure = "; Secure" if APP_ENV == "production" else ""
        self.send_json(200, {"message": "Signed out."}, {"Set-Cookie": f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure}"})

    def dashboard(self, user: sqlite3.Row) -> None:
        with db() as conn:
            if user["role"] == "patient":
                rows = conn.execute(
                    "SELECT id,service_title,scheduled_at,amount_paise,currency,status,created_at "
                    "FROM bookings WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                    (user["id"],),
                ).fetchall()
                profile = conn.execute(
                    "SELECT conditions,allergies,mobility,care_notes,emergency_contact "
                    "FROM patient_profiles WHERE user_id=?",
                    (user["id"],),
                ).fetchone()
                self.send_json(
                    200,
                    {
                        "user": public_user(user),
                        "bookings": [dict(r) for r in rows],
                        "profile": dict(profile) if profile else None,
                    },
                )
                return
            rows = conn.execute(
                "SELECT b.id,b.service_title,b.scheduled_at,b.amount_paise,b.currency,b.status,"
                "CASE WHEN b.assigned_staff_id=? THEN u.name ELSE NULL END AS patient_name,"
                "CASE WHEN b.assigned_staff_id=? THEN p.conditions ELSE NULL END AS patient_conditions,"
                "CASE WHEN b.assigned_staff_id=? THEN p.allergies ELSE NULL END AS patient_allergies,"
                "CASE WHEN b.assigned_staff_id=? THEN p.mobility ELSE NULL END AS patient_mobility,"
                "CASE WHEN b.assigned_staff_id=? THEN p.care_notes ELSE NULL END AS patient_care_notes,"
                "CASE WHEN b.assigned_staff_id=? THEN p.emergency_contact ELSE NULL END AS patient_emergency_contact "
                "FROM bookings b JOIN users u ON u.id=b.user_id "
                "LEFT JOIN patient_profiles p ON p.user_id=u.id "
                "WHERE b.required_role=? AND ((b.status='paid' AND b.assigned_staff_id IS NULL) "
                "OR b.assigned_staff_id=?) ORDER BY b.scheduled_at LIMIT 50",
                (
                    user["id"], user["id"], user["id"], user["id"], user["id"],
                    user["id"], user["role"], user["id"],
                ),
            ).fetchall()
            self.send_json(200, {"user": public_user(user), "bookings": [dict(r) for r in rows]})

    def save_patient_profile(self, data: dict) -> None:
            user = self.current_user()
            self.require_role(user, "patient")
            fields = {
                "conditions": 1000,
                "allergies": 500,
                "mobility": 500,
                "care_notes": 3000,
                "emergency_contact": 300,
            }
            values = {}
            for key, max_length in fields.items():
                value = str(data.get(key, "")).strip()
                if len(value) > max_length:
                    raise ApiError(400, f"{key.replace('_', ' ').capitalize()} is too long.")
                values[key] = value
            now = int(time.time())
            with db() as conn:
                conn.execute(
                    "INSERT INTO patient_profiles(user_id,conditions,allergies,mobility,care_notes,"
                    "emergency_contact,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET conditions=excluded.conditions,"
                    "allergies=excluded.allergies,mobility=excluded.mobility,care_notes=excluded.care_notes,"
                    "emergency_contact=excluded.emergency_contact,updated_at=excluded.updated_at",
                    (
                        user["id"], values["conditions"], values["allergies"], values["mobility"],
                        values["care_notes"], values["emergency_contact"], now,
                    ),
                )
            self.send_json(200, {"message": "Your care record was saved."})

    def create_payment_order(self, data: dict) -> None:
        user = self.current_user()
        self.require_role(user, "patient")
        if not razorpay_configured():
            raise ApiError(503, "Online payments are not configured yet. Please contact Nutrix support.")
        plan_key = str(data.get("plan", ""))
        plan = PLANS.get(plan_key)
        if not plan:
            raise ApiError(400, "Choose one of the available care services.")
        scheduled = str(data.get("scheduledAt", "")).strip()
        try:
            scheduled_time = dt.datetime.fromisoformat(scheduled)
            if scheduled_time.tzinfo is None:
                scheduled_time = scheduled_time.astimezone()
            scheduled_epoch = scheduled_time.timestamp()
        except ValueError as exc:
            raise ApiError(400, "Choose a valid date and time for care.") from exc
        if scheduled_epoch < time.time() + 3600 or scheduled_epoch > time.time() + 90 * 86400:
            raise ApiError(400, "Care must be scheduled at least one hour ahead and within 90 days.")
        booking_id = secrets.token_urlsafe(18)
        order = razorpay_order(plan["amount"], booking_id[:30])
        if not order.get("id"):
            raise ApiError(502, "Razorpay returned an invalid payment order.")
        now = int(time.time())
        with db() as conn:
            conn.execute(
                "INSERT INTO bookings(id,user_id,plan_key,service_title,required_role,scheduled_at,"
                "amount_paise,currency,order_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    booking_id, user["id"], plan_key, plan["title"], plan["role"],
                    scheduled_time.isoformat(timespec="minutes"),
                    plan["amount"], "INR", order["id"], "payment_pending", now,
                ),
            )
        self.send_json(
            201,
            {
                "bookingId": booking_id,
                "orderId": order["id"],
                "amount": plan["amount"],
                "currency": "INR",
                "keyId": setting("RAZORPAY_KEY_ID"),
                "name": plan["title"],
            },
        )

    def verify_payment(self, data: dict) -> None:
        user = self.current_user()
        self.require_role(user, "patient")
        order_id = str(data.get("razorpay_order_id", ""))
        payment_id = str(data.get("razorpay_payment_id", ""))
        signature = str(data.get("razorpay_signature", ""))
        if not all((order_id, payment_id, signature)) or not razorpay_configured():
            raise ApiError(400, "Payment verification details are incomplete.")
        expected = hmac.new(
            setting("RAZORPAY_KEY_SECRET").encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ApiError(400, "Payment signature could not be verified.")
        with db() as conn:
            booking = conn.execute(
                "SELECT id,status FROM bookings WHERE order_id=? AND user_id=?",
                (order_id, user["id"]),
            ).fetchone()
            if booking is None:
                raise ApiError(404, "Payment order was not found.")
            if booking["status"] != "payment_pending":
                raise ApiError(409, "This payment has already been processed.")
            conn.execute(
                "UPDATE bookings SET payment_id=?,status='paid' WHERE id=?",
                (payment_id, booking["id"]),
            )
        self.send_json(200, {"message": "Payment verified. Your care request is confirmed.", "bookingId": booking["id"]})

    def payment_webhook(self) -> None:
        secret = setting("RAZORPAY_WEBHOOK_SECRET")
        if not secret:
            raise ApiError(503, "Payment webhook is not configured.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "Invalid webhook request.") from exc
        if length <= 0 or length > 1024 * 1024:
            raise ApiError(413, "Webhook request size is invalid.")
        body = self.rfile.read(length)
        signature = self.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise ApiError(400, "Webhook signature could not be verified.")
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "Invalid webhook payload.") from exc
        if event.get("event") == "payment.captured":
            entity = event.get("payload", {}).get("payment", {}).get("entity", {})
            order_id, payment_id = entity.get("order_id"), entity.get("id")
            if order_id and payment_id:
                with db() as conn:
                    conn.execute(
                        "UPDATE bookings SET payment_id=?,status='paid' "
                        "WHERE order_id=? AND status='payment_pending'",
                        (payment_id, order_id),
                    )
        self.send_json(200, {"received": True})

    def accept_booking(self, booking_id: str) -> None:
        user = self.current_user()
        self.require_role(user, "nurse", "caretaker")
        self.require_reviewed(user)
        with db() as conn:
            cursor = conn.execute(
                "UPDATE bookings SET assigned_staff_id=?,status='assigned' "
                "WHERE id=? AND required_role=? AND status='paid' AND assigned_staff_id IS NULL",
                (user["id"], booking_id, user["role"]),
            )
            if cursor.rowcount != 1:
                raise ApiError(409, "This care request is no longer available.")
        self.send_json(200, {"message": "Care request accepted."})

    def review_staff(self, user_id: str, data: dict) -> None:
        if not setting("ADMIN_REVIEW_KEY") or not hmac.compare_digest(
            self.headers.get("X-Admin-Key", ""), setting("ADMIN_REVIEW_KEY")
        ):
            raise ApiError(403, "Administrator review access is not configured or authorized.")
        decision = str(data.get("decision", ""))
        if decision not in ("approved", "rejected"):
            raise ApiError(400, "Choose approved or rejected.")
        with db() as conn:
            cursor = conn.execute(
                "UPDATE users SET review_status=? WHERE id=? AND role IN ('nurse','caretaker') "
                "AND review_status='pending'",
                (decision, user_id),
            )
            if cursor.rowcount != 1:
                raise ApiError(404, "Pending professional signup was not found.")
        self.send_json(200, {"message": "Professional signup " + decision + "."})

    def serve_static(self, path: str) -> None:
        requested = "index.html" if path in ("/", "") else urllib.parse.unquote(path.lstrip("/"))
        target = (STATIC / requested).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            raise ApiError(404, "Page not found.")
        if not target.is_file():
            raise ApiError(404, "Page not found.")
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://checkout.razorpay.com; "
            "connect-src 'self' https://api.razorpay.com; style-src 'self'; "
            "img-src 'self' data:; frame-src https://api.razorpay.com https://checkout.razorpay.com; "
            "base-uri 'self'; form-action 'self' https://api.razorpay.com",
        )
        self.end_headers()
        self.wfile.write(content)


def validate_startup() -> None:
    if APP_ENV not in ("development", "demo", "production"):
        raise SystemExit("APP_ENV must be development, demo, or production.")
    if APP_ENV == "demo":
        if setting("SKIP_PHONE_VERIFICATION").lower() != "true":
            raise SystemExit("Demo mode requires SKIP_PHONE_VERIFICATION=true.")
        print("WARNING: Public demo mode is for fictional data only. Do not enter real patient information.", flush=True)
    if APP_ENV == "production":
        if setting("SKIP_PHONE_VERIFICATION").lower() == "true":
            raise SystemExit("Phone verification cannot be skipped in production.")
        missing = [
            key for key in (
                "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID",
                "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
                "ADMIN_REVIEW_KEY",
            ) if not setting(key)
        ]
        if missing:
            raise SystemExit("Missing required production configuration: " + ", ".join(missing))
        if local_otp_allowed():
            raise SystemExit("Local OTP must not be enabled in production.")
    if APP_ENV != "production" and local_otp_allowed():
        print("WARNING: Development OTPs are printed to this terminal. Do not use real patient information.", flush=True)
    if phone_verification_skipped():
        print("WARNING: Phone verification is disabled in demo mode. Do not use real accounts or patient information.", flush=True)


if __name__ == "__main__":
    load_local_env()
    APP_ENV = setting("APP_ENV", "development").lower()
    PORT = int(setting("PORT", "8000"))
    DB_PATH = Path(setting("NUTRIX_DB", str(ROOT / "data" / "nutrix.sqlite3")))
    validate_startup()
    init_db()
    print(f"Nutrix is ready at http://127.0.0.1:{PORT} ({APP_ENV})", flush=True)
    ThreadingHTTPServer((setting("HOST", "127.0.0.1"), PORT), Handler).serve_forever()
