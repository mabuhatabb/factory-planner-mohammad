"""Logins and database (SQLAlchemy). SQLite locally, hosted Postgres when DATABASE_URL is set."""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

ITERATIONS = 200_000
MAX_FAILS = 5
LOCK_MINUTES = 10

_engine = None


def get_engine(url=None):
    global _engine
    if url is not None or _engine is None:
        url = url or os.environ.get("DATABASE_URL") or "sqlite:///planner_v2.db"
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        _engine = create_engine(url, pool_pre_ping=True)
        init_db(_engine)
    return _engine


def is_hosted(engine=None):
    return not (engine or get_engine()).url.drivername.startswith("sqlite")


def init_db(engine):
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if engine.url.drivername.startswith("sqlite") else "SERIAL PRIMARY KEY"
    with engine.begin() as c:
        c.execute(text(f"""CREATE TABLE IF NOT EXISTS users (
            id {pk}, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
            fails INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created TEXT NOT NULL)"""))
        for tbl in ("datasets", "plans", "feedback"):
            c.execute(text(f"""CREATE TABLE IF NOT EXISTS {tbl} (
                id {pk}, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                body TEXT NOT NULL, created TEXT NOT NULL)"""))


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def valid_username(u):
    return bool(re.fullmatch(r"[A-Za-z0-9_.@-]{3,60}", u or ""))


def create_user(username, password, engine=None):
    """Returns (user_id, error)."""
    e = engine or get_engine()
    username = (username or "").strip().lower()
    if not valid_username(username):
        return None, "Username: 3-60 characters, letters, numbers and _ . @ - only."
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters."
    salt = secrets.token_hex(16)
    try:
        with e.begin() as c:
            if c.execute(text("SELECT 1 FROM users WHERE username=:u"), {"u": username}).first():
                return None, "That username is taken."
            c.execute(text("INSERT INTO users (username, salt, pw_hash, created) VALUES (:u,:s,:h,:t)"),
                      {"u": username, "s": salt, "h": _hash(password, salt), "t": _now()})
            uid = c.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar()
        return uid, None
    except Exception:
        return None, "That username is taken."


def check_login(username, password, engine=None):
    """Returns (user_id, error)."""
    e = engine or get_engine()
    username = (username or "").strip().lower()
    with e.begin() as c:
        row = c.execute(text("SELECT id, salt, pw_hash, fails, locked_until FROM users WHERE username=:u"),
                        {"u": username}).first()
        if not row:
            _hash(password or "", "00" * 16)  # same time as a real check
            return None, "Wrong username or password."
        uid, salt, pw_hash, fails, locked = row
        if locked and datetime.fromisoformat(locked) > datetime.utcnow():
            return None, f"Too many wrong attempts. Try again in {LOCK_MINUTES} minutes."
        if hmac.compare_digest(_hash(password or "", salt), pw_hash):
            c.execute(text("UPDATE users SET fails=0, locked_until=NULL WHERE id=:i"), {"i": uid})
            return uid, None
        fails += 1
        lock = None
        if fails >= MAX_FAILS:
            lock = (datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
            fails = 0
        c.execute(text("UPDATE users SET fails=:f, locked_until=:l WHERE id=:i"),
                  {"f": fails, "l": lock, "i": uid})
        return None, "Wrong username or password."


def change_password(uid, old, new, engine=None):
    e = engine or get_engine()
    if len(new or "") < 8:
        return "New password must be at least 8 characters."
    with e.begin() as c:
        row = c.execute(text("SELECT salt, pw_hash FROM users WHERE id=:i"), {"i": uid}).first()
        if not row or not hmac.compare_digest(_hash(old or "", row[0]), row[1]):
            return "Current password is wrong."
        salt = secrets.token_hex(16)
        c.execute(text("UPDATE users SET salt=:s, pw_hash=:h WHERE id=:i"),
                  {"s": salt, "h": _hash(new, salt), "i": uid})
    return None


# ---------- per-user saved items ----------
def save_item(table, uid, name, data, engine=None):
    assert table in ("datasets", "plans", "feedback")
    e = engine or get_engine()
    body = json.dumps(data, default=str)
    with e.begin() as c:
        c.execute(text(f"INSERT INTO {table} (user_id, name, body, created) VALUES (:u,:n,:b,:t)"),
                  {"u": uid, "n": name, "b": body, "t": _now()})


def list_items(table, uid, engine=None):
    assert table in ("datasets", "plans", "feedback")
    e = engine or get_engine()
    with e.begin() as c:
        rows = c.execute(text(f"SELECT id, name, created FROM {table} WHERE user_id=:u ORDER BY id DESC"),
                         {"u": uid}).all()
    return [{"id": r[0], "name": r[1], "created": r[2]} for r in rows]


def load_item(table, uid, item_id, engine=None):
    assert table in ("datasets", "plans", "feedback")
    e = engine or get_engine()
    with e.begin() as c:
        row = c.execute(text(f"SELECT body FROM {table} WHERE id=:i AND user_id=:u"),
                        {"i": item_id, "u": uid}).first()
    return json.loads(row[0]) if row else None


def delete_item(table, uid, item_id, engine=None):
    assert table in ("datasets", "plans", "feedback")
    e = engine or get_engine()
    with e.begin() as c:
        c.execute(text(f"DELETE FROM {table} WHERE id=:i AND user_id=:u"), {"i": item_id, "u": uid})
