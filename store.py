"""Logins and database (SQLAlchemy). SQLite locally, hosted Postgres when DATABASE_URL is set."""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect, text

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
    # An older version of the app used tables with the same names but different columns.
    # Move those aside (kept, not deleted) so the new tables can be created.
    insp = inspect(engine)
    for tbl in ("datasets", "plans", "feedback"):
        if insp.has_table(tbl):
            cols = {c["name"] for c in insp.get_columns(tbl)}
            if "body" not in cols or "user_id" not in cols:
                with engine.begin() as c:
                    c.execute(text(f"ALTER TABLE {tbl} RENAME TO {tbl}_old_{datetime.utcnow():%Y%m%d%H%M%S}"))
    with engine.begin() as c:
        c.execute(text(f"""CREATE TABLE IF NOT EXISTS users (
            id {pk}, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
            fails INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created TEXT NOT NULL)"""))
        for tbl in ("datasets", "plans", "feedback"):
            c.execute(text(f"""CREATE TABLE IF NOT EXISTS {tbl} (
                id {pk}, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                body TEXT NOT NULL, created TEXT NOT NULL)"""))
    _migrate_users(engine)


def _migrate_users(engine):
    """Older databases have no role/owner columns on users."""
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    with engine.begin() as c:
        if "role" not in cols:
            c.execute(text("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"))
        if "owner_id" not in cols:
            c.execute(text("ALTER TABLE users ADD COLUMN owner_id INTEGER"))
        c.execute(text("UPDATE users SET owner_id=id WHERE owner_id IS NULL"))
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if engine.url.drivername.startswith("sqlite") else "SERIAL PRIMARY KEY"
        c.execute(text(f"""CREATE TABLE IF NOT EXISTS audit (
            id {pk}, owner_id INTEGER NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL,
            detail TEXT, created TEXT NOT NULL)"""))


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
            c.execute(text("UPDATE users SET owner_id=:i, role='admin' WHERE id=:i"), {"i": uid})
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


def update_item(table, uid, item_id, data, engine=None):
    assert table in ("datasets", "plans", "feedback")
    e = engine or get_engine()
    with e.begin() as c:
        c.execute(text(f"UPDATE {table} SET body=:b WHERE id=:i AND user_id=:u"),
                  {"b": json.dumps(data, default=str), "i": item_id, "u": uid})


# ---------- approval links (manager decides without a login) ----------
APPROVAL_DAYS = 14


def init_approvals(engine):
    with engine.begin() as c:
        c.execute(text("""CREATE TABLE IF NOT EXISTS approvals (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
            email TEXT NOT NULL, created TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0)"""))


def create_approval(uid, plan_id, email, engine=None):
    e = engine or get_engine()
    init_approvals(e)
    token = secrets.token_urlsafe(24)
    with e.begin() as c:
        c.execute(text("INSERT INTO approvals (token, user_id, plan_id, email, created) VALUES (:t,:u,:p,:e,:c)"),
                  {"t": token, "u": uid, "p": plan_id, "e": email, "c": _now()})
    return token


def get_approval(token, engine=None):
    """Returns dict or None. Adds 'error' when used or expired."""
    e = engine or get_engine()
    init_approvals(e)
    if not token or len(token) > 100:
        return None
    with e.begin() as c:
        r = c.execute(text("SELECT user_id, plan_id, email, created, used FROM approvals WHERE token=:t"),
                      {"t": token}).first()
    if not r:
        return None
    out = {"uid": r[0], "plan_id": r[1], "email": r[2], "created": r[3], "used": bool(r[4]), "error": None}
    if out["used"]:
        out["error"] = "This decision link was already used."
    elif datetime.fromisoformat(r[3]) + timedelta(days=APPROVAL_DAYS) < datetime.utcnow():
        out["error"] = f"This link expired after {APPROVAL_DAYS} days. Ask for a new one."
    return out


def mark_approval_used(token, engine=None):
    e = engine or get_engine()
    with e.begin() as c:
        c.execute(text("UPDATE approvals SET used=1 WHERE token=:t"), {"t": token})


# ---------- team, roles and audit trail ----------
ROLES = ("admin", "planner", "manager", "viewer")
ROLE_TEXT = {"admin": "Admin: everything, including the team",
             "planner": "Planner: edit data, make and save plans, send for approval",
             "manager": "Manager: view everything, approve or reject plans",
             "viewer": "Viewer: view only"}


def get_user(login_id, engine=None):
    e = engine or get_engine()
    with e.begin() as c:
        r = c.execute(text("SELECT id, username, role, owner_id FROM users WHERE id=:i"), {"i": login_id}).first()
    return {"id": r[0], "username": r[1], "role": r[2], "owner_id": r[3] or r[0]} if r else None


def can(role, action):
    allowed = {"edit": ("admin", "planner"), "decide": ("admin", "manager"),
               "team": ("admin",), "audit": ("admin", "manager")}
    return role in allowed[action]


def add_member(owner_id, username, password, role, engine=None):
    """Create a login that shares the owner's data. Returns error text or None."""
    e = engine or get_engine()
    if role not in ROLES or role == "admin":
        return "Choose planner, manager or viewer."
    username = (username or "").strip().lower()
    if not valid_username(username):
        return "Username: 3-60 characters, letters, numbers and _ . @ - only."
    if len(password or "") < 8:
        return "Password must be at least 8 characters."
    salt = secrets.token_hex(16)
    with e.begin() as c:
        if c.execute(text("SELECT 1 FROM users WHERE username=:u"), {"u": username}).first():
            return "That username is taken."
        c.execute(text("INSERT INTO users (username, salt, pw_hash, created, role, owner_id) VALUES (:u,:s,:h,:t,:r,:o)"),
                  {"u": username, "s": salt, "h": _hash(password, salt), "t": _now(), "r": role, "o": owner_id})
    return None


def list_members(owner_id, engine=None):
    e = engine or get_engine()
    with e.begin() as c:
        rows = c.execute(text("SELECT id, username, role, created FROM users WHERE owner_id=:o ORDER BY id"),
                         {"o": owner_id}).all()
    return [{"id": r[0], "username": r[1], "role": r[2], "created": r[3]} for r in rows]


def remove_member(owner_id, member_id, engine=None):
    e = engine or get_engine()
    with e.begin() as c:
        c.execute(text("DELETE FROM users WHERE id=:i AND owner_id=:o AND role<>'admin'"), {"i": member_id, "o": owner_id})


def reset_member_password(owner_id, member_id, new_password, engine=None):
    e = engine or get_engine()
    if len(new_password or "") < 8:
        return "Password must be at least 8 characters."
    salt = secrets.token_hex(16)
    with e.begin() as c:
        c.execute(text("UPDATE users SET salt=:s, pw_hash=:h, fails=0, locked_until=NULL WHERE id=:i AND owner_id=:o AND role<>'admin'"),
                  {"s": salt, "h": _hash(new_password, salt), "i": member_id, "o": owner_id})
    return None


def log(owner_id, username, action, detail="", engine=None):
    """Write one line to the audit trail. Never raises."""
    try:
        e = engine or get_engine()
        with e.begin() as c:
            c.execute(text("INSERT INTO audit (owner_id, username, action, detail, created) VALUES (:o,:u,:a,:d,:t)"),
                      {"o": owner_id, "u": username, "a": action, "d": (detail or "")[:500], "t": _now()})
    except Exception:
        pass


def list_audit(owner_id, limit=300, engine=None):
    e = engine or get_engine()
    with e.begin() as c:
        rows = c.execute(text("SELECT created, username, action, detail FROM audit WHERE owner_id=:o ORDER BY id DESC LIMIT :l"),
                         {"o": owner_id, "l": limit}).all()
    return [{"when (UTC)": r[0].replace("T", " "), "who": r[1], "action": r[2], "detail": r[3]} for r in rows]
