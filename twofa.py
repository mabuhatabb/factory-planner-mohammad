"""Two-step sign-in codes (TOTP, RFC 6238), compatible with Google Authenticator, Microsoft Authenticator, Authy, 1Password."""
import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret, counter, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def verify(secret, code, last_counter=0, now=None, window=1):
    """Returns the accepted time step (store it to block reuse) or None."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    step = int((now if now is not None else time.time()) // 30)
    for c in range(step - window, step + window + 1):
        if c > last_counter and hmac.compare_digest(code_at(secret, c), code):
            return c
    return None


def uri(secret, username, issuer="Production Planner"):
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def qr_png(data):
    """PNG bytes of a QR code, or None if the optional 'segno' library is not installed."""
    try:
        import segno
    except Exception:
        return None
    buf = io.BytesIO()
    segno.make(data).save(buf, kind="png", scale=5, border=2)
    return buf.getvalue()


def new_recovery_codes(n=8):
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(n)]


def hash_code(code):
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
