"""Browser-extension user accounts, auth tokens and shared presets.

Tables (created lazily):
  ext_users   - username/password login accounts (role: admin|user)
  ext_tokens  - login session tokens (X-Auth-Token header)
  ext_presets - shared scraping presets (config JSON)
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from . import db

TOKEN_TTL = 30 * 24 * 3600  # 30 days


def ensure_tables() -> None:
    if db.q_one("SELECT name FROM sqlite_master WHERE type='table' AND name='ext_users'"):
        return
    db.execute(
        """CREATE TABLE IF NOT EXISTS ext_users (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             username TEXT UNIQUE NOT NULL,
             password_hash TEXT NOT NULL,
             display_name TEXT DEFAULT '',
             role TEXT DEFAULT 'user',
             enabled INTEGER DEFAULT 1,
             created_at TEXT DEFAULT (datetime('now','localtime'))
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ext_tokens (
             token TEXT PRIMARY KEY,
             user_id INTEGER NOT NULL,
             created_at TEXT DEFAULT (datetime('now','localtime')),
             expires_at INTEGER NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ext_presets (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             name TEXT NOT NULL,
             owner_id INTEGER NOT NULL,
             config TEXT NOT NULL,
             is_public INTEGER DEFAULT 1,
             created_at TEXT DEFAULT (datetime('now','localtime')),
             updated_at TEXT DEFAULT (datetime('now','localtime'))
           )"""
    )
    _tables_ready = True


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return secrets.compare_digest(calc, digest)


# ---------------------------------------------------------------- admin boot

def ensure_admin() -> None:
    """Create the initial admin account (password = web admin password)."""
    ensure_tables()
    from .config import settings

    row = db.q_one("SELECT id FROM ext_users WHERE username = ?", (settings.admin_username,))
    if not row:
        db.execute(
            "INSERT INTO ext_users (username, password_hash, display_name, role) VALUES (?, ?, ?, 'admin')",
            (settings.admin_username, hash_password(settings.admin_password), "管理员"),
        )


# -------------------------------------------------------------------- tokens

def login(username: str, password: str) -> dict | None:
    ensure_tables()
    row = db.q_one("SELECT * FROM ext_users WHERE username = ?", (username.strip(),))
    if not row or not row["enabled"] or not verify_password(password, row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO ext_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, row["id"], int(time.time()) + TOKEN_TTL),
    )
    # opportunistic cleanup of expired tokens
    db.execute("DELETE FROM ext_tokens WHERE expires_at < ?", (int(time.time()),))
    return {"token": token, "user": _public_user(row)}


def logout(token: str) -> None:
    ensure_tables()
    db.execute("DELETE FROM ext_tokens WHERE token = ?", (token,))


def auth(token: str) -> dict | None:
    """Return the user dict for a valid token, else None."""
    if not token:
        return None
    ensure_tables()
    row = db.q_one(
        """SELECT u.* FROM ext_tokens t JOIN ext_users u ON u.id = t.user_id
           WHERE t.token = ? AND t.expires_at > ? AND u.enabled = 1""",
        (token, int(time.time())),
    )
    return dict(row) if row else None


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"] or row["username"],
        "role": row["role"],
    }


# ------------------------------------------------------------------- presets

def list_presets(user_id: int, is_admin: bool) -> list[dict]:
    ensure_tables()
    rows = db.q_all(
        """SELECT p.*, u.username AS owner_name FROM ext_presets p
           LEFT JOIN ext_users u ON u.id = p.owner_id
           ORDER BY p.updated_at DESC"""
    )
    out = []
    for r in rows:
        if not is_admin and not r["is_public"] and r["owner_id"] != user_id:
            continue
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "owner": r["owner_name"] or f"user{r['owner_id']}",
                "mine": r["owner_id"] == user_id,
                "isPublic": bool(r["is_public"]),
                "updatedAt": r["updated_at"],
            }
        )
    return out


def get_preset(preset_id: int) -> dict | None:
    ensure_tables()
    row = db.q_one("SELECT * FROM ext_presets WHERE id = ?", (preset_id,))
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "ownerId": row["owner_id"],
        "config": json.loads(row["config"] or "{}"),
        "isPublic": bool(row["is_public"]),
    }


def save_preset(user_id: int, name: str, config: dict, is_public: bool, preset_id: int | None = None) -> int:
    ensure_tables()
    cfg = json.dumps(config, ensure_ascii=False)
    if preset_id:
        row = db.q_one("SELECT owner_id FROM ext_presets WHERE id = ?", (preset_id,))
        if not row:
            raise ValueError("预设不存在")
        urow = db.q_one("SELECT role FROM ext_users WHERE id = ?", (user_id,))
        if row["owner_id"] != user_id and (not urow or urow["role"] != "admin"):
            raise ValueError("只能修改自己的预设")
        db.execute(
            "UPDATE ext_presets SET name = ?, config = ?, is_public = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (name, cfg, 1 if is_public else 0, preset_id),
        )
        return preset_id
    cur = db.execute(
        "INSERT INTO ext_presets (name, owner_id, config, is_public) VALUES (?, ?, ?, ?)",
        (name, user_id, cfg, 1 if is_public else 0),
    )
    row = db.q_one("SELECT last_insert_rowid() AS id")
    return row["id"]


def delete_preset(preset_id: int, user_id: int, is_admin: bool) -> bool:
    ensure_tables()
    row = db.q_one("SELECT owner_id FROM ext_presets WHERE id = ?", (preset_id,))
    if not row:
        return False
    if row["owner_id"] != user_id and not is_admin:
        return False
    db.execute("DELETE FROM ext_presets WHERE id = ?", (preset_id,))
    return True


# ------------------------------------------------------------- user admin

def list_users() -> list[dict]:
    ensure_tables()
    rows = db.q_all("SELECT * FROM ext_users ORDER BY id")
    out = []
    for r in rows:
        d = _public_user(r)
        d["enabled"] = bool(r["enabled"])
        d["createdAt"] = r["created_at"]
        out.append(d)
    return out


def create_user(username: str, password: str, display_name: str = "", role: str = "user") -> dict:
    ensure_tables()
    username = username.strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    if role not in ("user", "admin"):
        role = "user"
    if db.q_one("SELECT id FROM ext_users WHERE username = ?", (username,)):
        raise ValueError(f"用户名已存在: {username}")
    db.execute(
        "INSERT INTO ext_users (username, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), display_name.strip(), role),
    )
    row = db.q_one("SELECT * FROM ext_users WHERE username = ?", (username,))
    return _public_user(row)


def update_user(user_id: int, password: str = "", display_name: str = "", role: str = "", enabled: bool | None = None) -> dict | None:
    ensure_tables()
    row = db.q_one("SELECT * FROM ext_users WHERE id = ?", (user_id,))
    if not row:
        return None
    if password:
        db.execute("UPDATE ext_users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    if display_name:
        db.execute("UPDATE ext_users SET display_name = ? WHERE id = ?", (display_name.strip(), user_id))
    if role in ("user", "admin"):
        db.execute("UPDATE ext_users SET role = ? WHERE id = ?", (role, user_id))
    if enabled is not None:
        db.execute("UPDATE ext_users SET enabled = ? WHERE id = ?", (1 if enabled else 0, user_id))
    row = db.q_one("SELECT * FROM ext_users WHERE id = ?", (user_id,))
    d = _public_user(row)
    d["enabled"] = bool(row["enabled"])
    return d


def delete_user(user_id: int) -> bool:
    ensure_tables()
    row = db.q_one("SELECT role FROM ext_users WHERE id = ?", (user_id,))
    if not row or row["role"] == "admin":
        return False  # never delete admin accounts
    db.execute("DELETE FROM ext_users WHERE id = ?", (user_id,))
    db.execute("DELETE FROM ext_tokens WHERE user_id = ?", (user_id,))
    return True


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    ensure_tables()
    row = db.q_one("SELECT * FROM ext_users WHERE id = ?", (user_id,))
    if not row or not verify_password(old_password, row["password_hash"]):
        return False
    db.execute("UPDATE ext_users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))
    return True
