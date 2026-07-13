#!/usr/bin/env python3
"""
Web dashboard: current output power, rig status, and event log.

Auth: HTTP Basic Auth checked via PAM against your Pi's actual system
user accounts (the same username/password you SSH in with) -- no
separate password file to manage. Optionally restrict *which* system
users are allowed in via config.DASHBOARD_ALLOWED_USERS.

Note on privilege: PAM's password check goes through unix_chkpwd, a
setuid-root helper, so this generally works fine without running Flask
itself as root. If you get permission errors during login, run this
under systemd as root (see solar-dashboard.service) -- simplest fix on a
single-user Pi.
"""

from functools import wraps

import pam
from flask import Flask, Response, request, jsonify, send_from_directory

import config
import state_store

app = Flask(__name__, static_folder="static")


def check_auth(username: str, password: str) -> bool:
    allowed = getattr(config, "DASHBOARD_ALLOWED_USERS", None)
    if allowed and username not in allowed:
        return False
    return pam.pam().authenticate(username, password, service="login")


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="Solar Miner Dashboard"'},
            )
        return f(*args, **kwargs)
    return decorated


@app.route("/")
@requires_auth
def index():
    return send_from_directory("templates", "index.html")


@app.route("/static/<path:filename>")
@requires_auth
def static_files(filename):
    return send_from_directory("static", filename)


@app.route("/api/state")
@requires_auth
def api_state():
    return jsonify(state_store.read_state())


if __name__ == "__main__":
    # threaded=True so one slow request (e.g. a rig timing out) doesn't
    # block the dashboard for anyone else looking at it.
    app.run(host="0.0.0.0", port=getattr(config, "DASHBOARD_PORT", 8080), threaded=True)
