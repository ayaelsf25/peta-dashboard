from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from dataset_store import (
    DatasetValidationError,
    connect_db,
    persist_dataset,
    validate_dataset,
)
from seed_data import (
    DEFAULT_UNIT_NAMES_WORKBOOK,
    DEFAULT_WORKBOOK,
    read_unit_names,
    save_unit_names,
    seed_if_empty,
)

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
SESSION_COOKIE = "peta_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_ATTEMPT_LIMIT = 8
IP_LOGIN_ATTEMPT_LIMIT = 60
MAX_ROWS = 50_000
MAX_SOAL_COUNT = 200
PASSWORD_MIN_LENGTH = 12
MAX_REQUEST_BYTES = 20 * 1024 * 1024


class LoginPayload(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=256)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=256)


class ImportPayload(BaseModel):
    file: str = Field(min_length=1, max_length=255)
    soalCount: int = Field(ge=1, le=MAX_SOAL_COUNT)
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_ROWS)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return "$scrypt$16384$8$1$%s$%s" % (
        salt.hex(),
        derived.hex(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        marker, algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if marker or algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def session_digest(token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def initialize_database(path: Path, admin_password: str) -> None:
    with connect_db(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE,
                username_key TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                nip TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                must_change_password INTEGER NOT NULL DEFAULT 0
                    CHECK (must_change_password IN (0, 1))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_admin_account
                ON accounts(role) WHERE role = 'admin';
            CREATE TABLE IF NOT EXISTS dataset_rows (
                id INTEGER PRIMARY KEY,
                nip TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS dataset_rows_nip_idx ON dataset_rows(nip);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                attempt_key TEXT NOT NULL,
                attempted_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_attempts_time_idx
                ON login_attempts(attempt_key, attempted_at);
            """
        )
        account_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
        }
        if "username_key" not in account_columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN username_key TEXT NOT NULL DEFAULT ''")
        stale_names = connection.execute(
            "SELECT id, username FROM accounts WHERE username_key = ''"
        ).fetchall()
        for row in stale_names:
            connection.execute(
                "UPDATE accounts SET username_key = ? WHERE id = ?",
                (row["username"].casefold(), row["id"]),
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS accounts_username_key_idx ON accounts(username_key)"
        )
        connection.execute(
            """
            INSERT INTO accounts (username, username_key, role, nip, password_hash, must_change_password)
            VALUES ('admin', 'admin', 'admin', NULL, ?, 0)
            ON CONFLICT(role) WHERE role = 'admin'
            DO UPDATE SET username = 'admin', username_key = 'admin',
                          password_hash = excluded.password_hash,
                          must_change_password = 0
            """,
            (hash_password(admin_password),),
        )


def create_app(
    *,
    db_path: Path | None = None,
    admin_password: str | None = None,
    session_secret: str | None = None,
    cookie_secure: bool | None = None,
    seed_workbook_path: Path | None = None,
    unit_names_workbook_path: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        password = admin_password if admin_password is not None else os.getenv("PETA_ADMIN_PASSWORD", "")
        secret = session_secret if session_secret is not None else os.getenv("PETA_SESSION_SECRET", "")
        secure = cookie_secure
        if secure is None:
            secure = os.getenv("PETA_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes"}
        if len(password) < 14:
            raise RuntimeError("PETA_ADMIN_PASSWORD wajib diatur dan minimal 14 karakter.")
        if len(secret) < 32:
            raise RuntimeError("PETA_SESSION_SECRET wajib diatur dan minimal 32 karakter.")
        path = db_path or Path(os.getenv("PETA_DB_PATH", str(ROOT / "server_data" / "prisma.sqlite3")))
        if not path.is_absolute():
            path = ROOT / path
        initialize_database(path, password)
        workbook = seed_workbook_path
        if workbook is None:
            workbook = Path(os.getenv("PETA_SEED_WORKBOOK", str(DEFAULT_WORKBOOK)))
        if not workbook.is_absolute():
            workbook = ROOT / workbook
        unit_names_workbook = unit_names_workbook_path
        if unit_names_workbook is None:
            unit_names_workbook = Path(
                os.getenv("PETA_UNIT_NAMES_WORKBOOK", str(DEFAULT_UNIT_NAMES_WORKBOOK))
            )
        if not unit_names_workbook.is_absolute():
            unit_names_workbook = ROOT / unit_names_workbook
        save_unit_names(path, read_unit_names(unit_names_workbook))
        if seed_if_empty(path, workbook):
            logger.info("Seed awal berhasil: %s", workbook.name)
        else:
            logger.info("Seed dilewati; database sudah memiliki data peserta.")
        application.state.db_path = path
        application.state.session_secret = secret
        application.state.cookie_secure = secure
        yield

    app = FastAPI(
        title="PETA Dashboard API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Origin tidak diizinkan."}, status_code=403)
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_REQUEST_BYTES:
                        return JSONResponse({"detail": "Ukuran permintaan terlalu besar."}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Content-Length tidak valid."}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def db() -> sqlite3.Connection:
        return connect_db(app.state.db_path)

    def read_session(request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        digest = session_digest(token, app.state.session_secret)
        now = int(time.time())
        with db() as connection:
            row = connection.execute(
                """
                SELECT a.id, a.username, a.role, a.nip, a.must_change_password, s.expires_at
                FROM sessions s JOIN accounts a ON a.id = s.account_id
                WHERE s.token_hash = ?
                """,
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                return None
        return dict(row)

    def require_account(request: Request) -> dict[str, Any]:
        account = read_session(request)
        if account is None:
            raise HTTPException(status_code=401, detail="Silakan masuk terlebih dahulu.")
        return account

    def require_ready_account(account: dict[str, Any] = Depends(require_account)) -> dict[str, Any]:
        if account["must_change_password"]:
            raise HTTPException(status_code=403, detail="Ganti kata sandi awal sebelum mengakses data.")
        return account

    @app.get("/")
    def dashboard():
        return FileResponse(ROOT / "dashboard.html", headers={"Cache-Control": "no-store"})

    @app.get("/peta.png")
    def logo():
        return FileResponse(ROOT / "peta.png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/auth/me")
    def auth_me(account: dict[str, Any] | None = Depends(read_session)):
        if account is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "role": account["role"],
            "name": account["username"],
            "nip": account["nip"],
            "must_change_password": bool(account["must_change_password"]),
        }

    @app.post("/api/auth/login")
    def login(payload: LoginPayload, request: Request, response: Response):
        username = payload.username.strip()
        if not username or not payload.password:
            raise HTTPException(status_code=401, detail="Username atau kata sandi salah.")
        now = int(time.time())
        client_ip = request.client.host if request.client else "unknown"
        attempt_key = hashlib.sha256(
            (client_ip + "\0" + username.casefold()).encode("utf-8")
        ).hexdigest()
        ip_attempt_key = hashlib.sha256(("ip\0" + client_ip).encode("utf-8")).hexdigest()

        with db() as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE attempted_at < ?",
                (now - LOGIN_WINDOW_SECONDS,),
            )
            limits = ((attempt_key, LOGIN_ATTEMPT_LIMIT), (ip_attempt_key, IP_LOGIN_ATTEMPT_LIMIT))
            for key, limit in limits:
                attempts = connection.execute(
                    "SELECT COUNT(*) AS total FROM login_attempts WHERE attempt_key = ? AND attempted_at >= ?",
                    (key, now - LOGIN_WINDOW_SECONDS),
                ).fetchone()["total"]
                if attempts >= limit:
                    raise HTTPException(status_code=429, detail="Terlalu banyak percobaan. Coba lagi dalam 15 menit.")

            candidates = connection.execute(
                "SELECT * FROM accounts WHERE username_key = ? ORDER BY role = 'admin' DESC",
                (username.casefold(),),
            ).fetchall()
            matched = None
            for candidate in candidates:
                if candidate["role"] == "user" and candidate["must_change_password"]:
                    valid = hmac.compare_digest(payload.password, candidate["nip"] or "")
                else:
                    valid = verify_password(payload.password, candidate["password_hash"])
                if valid and matched is None:
                    matched = candidate
            if not candidates:
                verify_password(payload.password, DUMMY_PASSWORD_HASH)
            if matched is None:
                connection.executemany(
                    "INSERT INTO login_attempts (attempt_key, attempted_at) VALUES (?, ?)",
                    ((key, now) for key, _ in limits),
                )
                raise HTTPException(status_code=401, detail="Username atau kata sandi salah.")

            connection.executemany(
                "DELETE FROM login_attempts WHERE attempt_key = ?",
                ((key,) for key, _ in limits),
            )
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO sessions (token_hash, account_id, expires_at) VALUES (?, ?, ?)",
                (session_digest(token, app.state.session_secret), matched["id"], now + SESSION_TTL_SECONDS),
            )
            response.set_cookie(
                SESSION_COOKIE,
                token,
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                secure=app.state.cookie_secure,
                samesite="strict",
                path="/",
            )
            return {
                "authenticated": True,
                "role": matched["role"],
                "name": matched["username"],
                "nip": matched["nip"],
                "must_change_password": bool(matched["must_change_password"]),
            }

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with db() as connection:
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash = ?",
                    (session_digest(token, app.state.session_secret),),
                )
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            secure=app.state.cookie_secure,
            samesite="strict",
        )
        return {"ok": True}

    @app.post("/api/auth/change-password")
    def change_password(
        payload: ChangePasswordPayload,
        request: Request,
        account: dict[str, Any] = Depends(require_account),
    ):
        if account["role"] != "user":
            raise HTTPException(status_code=403, detail="Perubahan ini hanya untuk akun peserta.")
        if payload.new_password == payload.current_password:
            raise HTTPException(status_code=400, detail="Kata sandi baru harus berbeda dari kata sandi lama.")
        with db() as connection:
            current = connection.execute(
                "SELECT password_hash, nip, must_change_password FROM accounts WHERE id = ?",
                (account["id"],),
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=400, detail="Kata sandi lama tidak benar.")
            if current["must_change_password"]:
                old_password_matches = hmac.compare_digest(
                    payload.current_password, current["nip"] or ""
                )
            else:
                old_password_matches = verify_password(payload.current_password, current["password_hash"])
            if not old_password_matches:
                raise HTTPException(status_code=400, detail="Kata sandi lama tidak benar.")
            connection.execute(
                """
                UPDATE accounts
                SET password_hash = ?, must_change_password = 0
                WHERE id = ?
                """,
                (hash_password(payload.new_password), account["id"]),
            )
            current_token = request.cookies.get(SESSION_COOKIE, "")
            current_token_hash = session_digest(current_token, app.state.session_secret)
            connection.execute(
                "DELETE FROM sessions WHERE account_id = ? AND token_hash != ?",
                (account["id"], current_token_hash),
            )
        return {"ok": True}

    @app.get("/api/data")
    def get_data(account: dict[str, Any] = Depends(require_ready_account)):
        with db() as connection:
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata").fetchall()
            }
            if account["role"] == "admin":
                rows = connection.execute("SELECT payload FROM dataset_rows ORDER BY id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM dataset_rows WHERE nip = ? ORDER BY id",
                    (account["nip"],),
                ).fetchall()
        return {
            "file": metadata.get("file", ""),
            "soalCount": int(metadata.get("soalCount", "0")),
            "unitNames": json.loads(metadata.get("unitNames", "{}")),
            "rows": [json.loads(row["payload"]) for row in rows],
        }

    @app.post("/api/admin/import")
    def import_data(
        payload: ImportPayload,
        account: dict[str, Any] = Depends(require_ready_account),
    ):
        if account["role"] != "admin":
            raise HTTPException(status_code=403, detail="Hanya admin yang dapat memuat data.")
        try:
            rows, participants = validate_dataset(payload.rows, payload.soalCount)
        except DatasetValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        filename = Path(payload.file).name
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persist_dataset(connection, filename, payload.soalCount, rows, participants)
        return {"ok": True, "file": filename, "rows": len(rows), "soalCount": payload.soalCount}

    return app


DUMMY_PASSWORD_HASH = hash_password("invalid-login-placeholder")
app = create_app()
