"""
API Key System
--------------
Keys keys.json file mein store hoti hain. Format:

{
  "DARK-VIP": {
    "owner": "Rahul",
    "active": true,
    "expires": "2026-12-31",      # null = kabhi expire nahi
    "daily_limit": 50,            # null = unlimited
    "regions": ["IND", "BD"]      # null ya [] = saare regions allowed
  }
}

Usage (daily count) usage.json mein save hota hai.
Vercel par filesystem read-only hota hai, isliye wahan /tmp use hota hai
(wo temporary hai - daily limit best-effort rahegi).
"""

import json
import os
import secrets
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.environ.get("KEYS_FILE", os.path.join(BASE_DIR, "keys.json"))
USAGE_FILE = os.environ.get(
    "USAGE_FILE",
    "/tmp/ff_usage.json" if os.environ.get("VERCEL") else os.path.join(BASE_DIR, "usage.json"),
)

_lock = threading.Lock()


# ---------------------------------------------------------------- helpers
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_keys():
    data = _read_json(KEYS_FILE, {})
    return data if isinstance(data, dict) else {}


def _load_usage():
    data = _read_json(USAGE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_usage(usage):
    try:
        _write_json(USAGE_FILE, usage)
    except Exception:
        pass  # read-only filesystem - ignore, in-memory/best effort


def _used_today(usage, key):
    entry = usage.get(key)
    if entry and entry.get("date") == _today():
        return int(entry.get("count", 0))
    return 0


def _is_expired(expires):
    if not expires:
        return False
    try:
        end = datetime.strptime(expires, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # galat date = expired maano (safe side)
    # expiry date ke poore din tak valid
    return datetime.now(timezone.utc).date() > end.date()


# ------------------------------------------------------- request validation
def check_and_consume(key, region):
    """
    Returns (ok, error_message, http_status, info)
    ok=True hone par ek use consume ho jata hai (fail hone par refund() call karo).
    """
    if not key:
        return False, "API key required. Use &key=YOUR_KEY", 401, None

    with _lock:
        keys = load_keys()
        cfg = keys.get(key)
        if cfg is None:
            return False, "Invalid API key", 403, None
        if not cfg.get("active", True):
            return False, "API key is disabled", 403, None
        if _is_expired(cfg.get("expires")):
            return False, "API key expired", 403, None

        regions = cfg.get("regions")
        if regions and region not in [r.upper() for r in regions]:
            return False, f"This key is not allowed for region {region}", 403, None

        usage = _load_usage()
        used = _used_today(usage, key)
        limit = cfg.get("daily_limit")
        if limit is not None and used >= int(limit):
            return False, "Daily limit reached for this key", 429, None

        usage[key] = {"date": _today(), "count": used + 1}
        _save_usage(usage)

        info = {
            "owner": cfg.get("owner", ""),
            "expires": cfg.get("expires"),
            "daily_limit": limit,
            "remaining_today": None if limit is None else max(int(limit) - (used + 1), 0),
        }
        return True, None, 200, info


def refund(key):
    """Request fail ho gayi to use wapas kar do."""
    with _lock:
        usage = _load_usage()
        entry = usage.get(key)
        if entry and entry.get("date") == _today() and entry.get("count", 0) > 0:
            entry["count"] -= 1
            _save_usage(usage)


# ----------------------------------------------------------- admin helpers
def generate_key(prefix="DARK"):
    return f"{prefix}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"


def add_key(key=None, owner="", days=None, daily_limit=None, regions=None, active=True):
    from datetime import timedelta

    key = key or generate_key()
    expires = None
    if days:
        expires = (datetime.now(timezone.utc) + timedelta(days=int(days))).strftime("%Y-%m-%d")
    with _lock:
        keys = load_keys()
        keys[key] = {
            "owner": owner,
            "active": active,
            "expires": expires,
            "daily_limit": None if daily_limit in (None, "") else int(daily_limit),
            "regions": [r.upper() for r in regions] if regions else None,
        }
        _write_json(KEYS_FILE, keys)
    return key, keys[key]


def remove_key(key):
    with _lock:
        keys = load_keys()
        if key not in keys:
            return False
        del keys[key]
        _write_json(KEYS_FILE, keys)
    return True


def set_active(key, active):
    with _lock:
        keys = load_keys()
        if key not in keys:
            return False
        keys[key]["active"] = bool(active)
        _write_json(KEYS_FILE, keys)
    return True


def list_keys():
    with _lock:
        keys = load_keys()
        usage = _load_usage()
    out = {}
    for k, cfg in keys.items():
        out[k] = dict(cfg, used_today=_used_today(usage, k))
    return out
