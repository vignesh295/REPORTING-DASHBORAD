"""
Simple session-based authentication with two roles: "ops" and "admin".

  ops   -> can only see the Upload page (and their upload confirmation).
  admin -> can see everything (dashboard, lane details, upload).

Users live in a JSON file (config.USERS_FILE); passwords are stored as salted
werkzeug hashes, never in plain text. Seed / manage accounts with
`python manage_users.py`. Set AUTH_ENABLED=false to open the app up during dev
— every request is then treated as a built-in "dev" admin.
"""
import functools
import json
import os

from flask import flash, redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config

ROLES = ("ops", "admin")


def _load_users():
    if not os.path.exists(config.USERS_FILE):
        return {}
    try:
        with open(config.USERS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_users(users):
    with open(config.USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def set_user(username, password, role):
    """Create or update a user (used by manage_users.py). Password is hashed."""
    username = username.strip()
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not username or not password:
        raise ValueError("username and password are required")
    users = _load_users()
    users[username] = {
        "password_hash": generate_password_hash(password),
        "role": role,
    }
    _save_users(users)


def set_role(username, role):
    """Change a user's role without touching their password."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    users = _load_users()
    if username not in users:
        raise ValueError("no such user")
    users[username]["role"] = role
    _save_users(users)


def admin_count():
    return sum(1 for rec in _load_users().values() if rec.get("role") == "admin")


def delete_user(username):
    users = _load_users()
    if username in users:
        del users[username]
        _save_users(users)
        return True
    return False


def list_users():
    return {u: rec.get("role", "ops") for u, rec in _load_users().items()}


def verify(username, password):
    """Return {'username','role'} on success, else None."""
    rec = _load_users().get((username or "").strip())
    if not rec or not check_password_hash(rec.get("password_hash", ""), password or ""):
        return None
    return {"username": username.strip(), "role": rec.get("role", "ops")}


def current_user():
    """The logged-in user dict {'username','role'}, or None."""
    if not config.AUTH_ENABLED:
        return {"username": "dev", "role": "admin"}
    return session.get("user")


def login_user(user):
    session["user"] = {"username": user["username"], "role": user["role"]}


def logout_user():
    session.pop("user", None)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    """Gate a view to the given role(s). ops hitting an admin page is bounced
    to their upload page rather than shown a scary error."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("You don't have access to that page.", "error")
                return redirect(url_for("upload"))
            return view(*args, **kwargs)
        return wrapped
    return decorator
