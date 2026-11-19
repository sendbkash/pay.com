"""bKash "Send Money" payment page with automatic SMS verification.

Flow
  1. Admin creates an order (amount) and gets a /pay/<ref> link.
  2. Customer sends money to RECEIVE_NUMBER, then enters their bKash number + TrxID.
  3. Your phone receives bKash's "You have received Tk ..." SMS; an SMS-forwarder
     app POSTs it to /webhook/sms.
  4. We match (customer number + TrxID + amount) against that SMS and mark the
     order paid. If the SMS has not arrived yet the order stays "pending" and is
     verified automatically the moment the SMS comes in.
"""
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, url_for)

# --------------------------------------------------------------------------
# Config (all from environment variables, nothing hard-coded)
# --------------------------------------------------------------------------
SHOP_NAME = os.environ.get("SHOP_NAME", "My Shop")
RECEIVE_NUMBER = os.environ.get("RECEIVE_NUMBER", "")   # your bKash number
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")   # shared with SMS forwarder
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
API_KEY = os.environ.get("API_KEY", "")                 # optional: create orders from your site
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "payments.db"))
ALLOWED_SENDERS = {
    s.strip().lower()
    for s in os.environ.get("ALLOWED_SENDERS", "bKash").split(",") if s.strip()
}
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "8"))  # submits per IP per 10 minutes

BDT = timezone(timedelta(hours=6))  # Bangladesh has no DST

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
if os.environ.get("TRUST_PROXY") == "1":  # set when behind nginx / a hosting proxy
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  ref             TEXT PRIMARY KEY,
  amount          REAL NOT NULL,
  note            TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'unpaid',  -- unpaid | pending | paid
  customer_number TEXT,
  trxid           TEXT,
  paid_amount     REAL,
  paid_via        TEXT,                            -- sms | manual
  created_at      TEXT NOT NULL,
  paid_at         TEXT
);
CREATE TABLE IF NOT EXISTS sms (
  trxid         TEXT PRIMARY KEY,                  -- one TrxID can only ever exist once
  sender_number TEXT NOT NULL,
  amount        REAL NOT NULL,
  raw           TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  order_ref     TEXT                               -- which order consumed this payment
);
CREATE TABLE IF NOT EXISTS sms_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  sender     TEXT,
  body       TEXT,
  reason     TEXT
);
"""


def now():
    return datetime.now(BDT).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def tx(db):
    """Write transaction. BEGIN IMMEDIATE makes concurrent verifications queue up,
    so one TrxID can never be claimed by two orders at once."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()


init_db()

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
BD_NUMBER = re.compile(r"^01[3-9]\d{8}$")
TRXID = re.compile(r"^[A-Z0-9]{8,12}$")

# bKash "Send Money received" SMS, e.g.
#   You have received Tk 500.00 from 01712345678. Fee Tk 0.00. Balance Tk 1,234.56.
#   TrxID 9BK1A2B3C4 at 19/11/2026 14:10
SMS_AMOUNT_FROM = re.compile(
    r"received\s+(?:payment\s+)?Tk\.?\s*([\d,]+(?:\.\d{1,2})?)\s+from\s+(\+?\d[\d\s-]{9,15}\d)",
    re.I)
SMS_TRXID = re.compile(r"TrxID\s*[:\-]?\s*([A-Za-z0-9]{8,12})", re.I)


def norm_number(raw):
    """'+8801712-345678' / '8801712345678' / '01712345678' -> '01712345678' (or None)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("880"):
        digits = "0" + digits[3:]
    return digits if BD_NUMBER.match(digits) else None


def parse_bkash_sms(body):
    m, t = SMS_AMOUNT_FROM.search(body or ""), SMS_TRXID.search(body or "")
    if not (m and t):
        return None
    number = norm_number(m.group(2))
    if not number:
        return None
    return {"amount": float(m.group(1).replace(",", "")),
            "number": number, "trxid": t.group(1).upper()}


def parse_amount(raw):
    try:
        value = round(float(str(raw).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 500000 else None


def safe_eq(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())


@app.template_filter("money")
def money(value):
    s = f"{float(value or 0):,.2f}"
    return s[:-3] if s.endswith(".00") else s


_attempts = defaultdict(deque)


def rate_limited(key, limit=None, window=600):
    limit = limit or MAX_ATTEMPTS
    t = time.time()
    q = _attempts[key]
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


MESSAGES = {
    "invalid_number": "সঠিক বিকাশ নম্বর দিন (যেমন 01712345678)।",
    "invalid_trxid": "TrxID ঠিক নেই। বিকাশের SMS দেখে আবার লিখুন (৮–১২ অক্ষর, ইংরেজি অক্ষর ও সংখ্যা)।",
    "number_mismatch": "এই TrxID-এর টাকা অন্য নম্বর থেকে এসেছে। যে নম্বর থেকে Send Money করেছেন সেই নম্বরটি দিন।",
    "amount_low": "আপনি ৳{got} পাঠিয়েছেন, কিন্তু ৳{need} দরকার। সাপোর্টে যোগাযোগ করুন।",
    "used": "এই TrxID আগেই অন্য পেমেন্টে ব্যবহার হয়েছে।",
    "rate_limited": "অনেকবার চেষ্টা হয়েছে। ১০ মিনিট পরে আবার চেষ্টা করুন।",
    "not_found": "অর্ডার পাওয়া যায়নি।",
}

# --------------------------------------------------------------------------
# Core: match an order against received SMS
# --------------------------------------------------------------------------


def _fail(db, ref, code, **extra):
    # Reset so the customer can correct a typo and try again.
    db.execute("UPDATE orders SET status='unpaid', customer_number=NULL, trxid=NULL "
               "WHERE ref=? AND status!='paid'", (ref,))
    return {"status": "failed", "code": code, **extra}


def verify_order(db, ref):
    """Match one order's submitted number + TrxID against the SMS table.
    Must be called inside tx(). Returns {"status": paid|pending|unpaid|failed, ...}."""
    o = db.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()
    if o is None:
        return {"status": "failed", "code": "not_found"}
    if o["status"] == "paid":
        return {"status": "paid"}
    if o["status"] != "pending":
        return {"status": "unpaid"}

    sms = db.execute("SELECT * FROM sms WHERE trxid=?", (o["trxid"],)).fetchone()
    if sms is None:
        return {"status": "pending"}                       # SMS not here yet
    if sms["order_ref"] not in (None, ref):
        return _fail(db, ref, "used")                      # already consumed by another order
    if sms["sender_number"] != o["customer_number"]:
        return _fail(db, ref, "number_mismatch")
    if sms["amount"] + 0.005 < o["amount"]:
        return _fail(db, ref, "amount_low", got=sms["amount"], need=o["amount"])

    db.execute("UPDATE sms SET order_ref=? WHERE trxid=?", (ref, o["trxid"]))
    db.execute("UPDATE orders SET status='paid', paid_amount=?, paid_via='sms', paid_at=? "
               "WHERE ref=?", (sms["amount"], now(), ref))
    return {"status": "paid"}


def public_result(db, ref, result):
    out = {"status": result["status"]}
    if result["status"] == "paid":
        o = db.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()
        out.update(ref=ref, trxid=o["trxid"] or "", paid_at=o["paid_at"],
                   amount=money(o["paid_amount"] or o["amount"]))
    elif result["status"] == "failed":
        code = result["code"]
        out["code"] = code
        out["message"] = MESSAGES[code].format(
            got=money(result.get("got")), need=money(result.get("need")))
    return out


def create_order(db, amount, note=""):
    ref = secrets.token_hex(5).upper()
    db.execute("INSERT INTO orders (ref, amount, note, created_at) VALUES (?,?,?,?)",
               (ref, amount, (note or "")[:200], now()))
    return ref


# --------------------------------------------------------------------------
# Auth / hardening
# --------------------------------------------------------------------------


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_PASS:
            abort(503, "ADMIN_PASS set kora hoy nai.")
        auth = request.authorization
        if not (auth and safe_eq(auth.username or "", ADMIN_USER)
                and safe_eq(auth.password or "", ADMIN_PASS)):
            return Response("Login required", 401,
                            {"WWW-Authenticate": 'Basic realm="Admin"'})
        return fn(*args, **kwargs)
    return wrapper


@app.before_request
def csrf_guard():
    # Admin uses HTTP Basic auth, which browsers re-send automatically, so refuse
    # cross-site form posts.
    if request.method == "POST" and request.path.startswith("/admin"):
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            abort(403)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith(("/api/", "/pay/", "/admin")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(_e):
    return Response("পেজ পাওয়া যায়নি / Not found", 404, mimetype="text/plain")


# --------------------------------------------------------------------------
# Customer-facing
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return redirect(url_for("admin"))


@app.get("/pay/<ref>")
def pay(ref):
    receive = norm_number(RECEIVE_NUMBER)
    if not receive:
        abort(503, "RECEIVE_NUMBER set kora hoy nai.")
    order = get_db().execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()
    if order is None:
        abort(404)
    return render_template("pay.html", shop=SHOP_NAME, order=order,
                           receive=receive, receive_fmt=f"{receive[:5]}-{receive[5:]}")


@app.post("/api/submit")
def api_submit():
    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref", ""))[:40]
    number = norm_number(str(data.get("number", "")))
    trx = str(data.get("trxid", "")).strip().upper()

    if rate_limited(request.remote_addr or "?"):
        return jsonify(status="failed", code="rate_limited",
                       message=MESSAGES["rate_limited"]), 429
    if number is None:
        return jsonify(status="failed", code="invalid_number",
                       message=MESSAGES["invalid_number"]), 400
    if not TRXID.match(trx):
        return jsonify(status="failed", code="invalid_trxid",
                       message=MESSAGES["invalid_trxid"]), 400

    db = get_db()
    with tx(db):
        o = db.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()
        if o is None:
            result = {"status": "failed", "code": "not_found"}
        elif o["status"] == "paid":
            result = {"status": "paid"}
        else:
            db.execute("UPDATE orders SET status='pending', customer_number=?, trxid=? "
                       "WHERE ref=?", (number, trx, ref))
            result = verify_order(db, ref)
        body = public_result(db, ref, result)
    return jsonify(body)


@app.get("/api/status/<ref>")
def api_status(ref):
    db = get_db()
    with tx(db):
        result = verify_order(db, ref[:40])
        body = public_result(db, ref[:40], result)
    return jsonify(body)


# --------------------------------------------------------------------------
# SMS webhook (called by the SMS-forwarder app on your phone)
# --------------------------------------------------------------------------


def _pick(data, *keys):
    for k in keys:
        if data.get(k):
            return data[k]
    return ""


def log_sms(db, sender, body, reason):
    db.execute("INSERT INTO sms_log (created_at, sender, body, reason) VALUES (?,?,?,?)",
               (now(), sender[:60], body[:600], reason))
    db.execute("DELETE FROM sms_log WHERE id NOT IN "
               "(SELECT id FROM sms_log ORDER BY id DESC LIMIT 500)")


@app.post("/webhook/sms")
def webhook_sms():
    if not WEBHOOK_SECRET:
        abort(503, "WEBHOOK_SECRET set kora hoy nai.")
    supplied = request.headers.get("X-Webhook-Secret") or request.args.get("key") or ""
    if not safe_eq(supplied, WEBHOOK_SECRET):
        abort(401)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    sender = str(_pick(data, "from", "sender", "address", "number")).strip()
    body = str(_pick(data, "message", "body", "text", "content", "sms")).strip()
    if not body:
        return jsonify(ok=True, ignored="empty")

    db = get_db()
    # Anyone can text your phone from a normal number, so only trust the bKash sender ID.
    if sender.lower() not in ALLOWED_SENDERS:
        log_sms(db, sender, body, "ignored: sender is not in ALLOWED_SENDERS")
        return jsonify(ok=True, ignored="sender")

    parsed = parse_bkash_sms(body)
    if parsed is None:
        log_sms(db, sender, body, "ignored: not a bKash 'received' SMS (or format not recognised)")
        return jsonify(ok=True, ignored="unparsed")

    matched = []
    with tx(db):
        cur = db.execute(
            "INSERT OR IGNORE INTO sms (trxid, sender_number, amount, raw, received_at) "
            "VALUES (?,?,?,?,?)",
            (parsed["trxid"], parsed["number"], parsed["amount"], body[:600], now()))
        inserted = cur.rowcount == 1
        if inserted:
            rows = db.execute("SELECT ref FROM orders WHERE status='pending' AND trxid=? "
                              "ORDER BY rowid", (parsed["trxid"],)).fetchall()
            for row in rows:
                if verify_order(db, row["ref"])["status"] == "paid":
                    matched.append(row["ref"])
    return jsonify(ok=True, duplicate=not inserted, matched=matched)


# --------------------------------------------------------------------------
# Admin + order-creation API
# --------------------------------------------------------------------------


@app.get("/admin")
@admin_required
def admin():
    db = get_db()
    return render_template(
        "admin.html", shop=SHOP_NAME, new_ref=request.args.get("new"),
        orders=db.execute("SELECT * FROM orders ORDER BY rowid DESC LIMIT 50").fetchall(),
        sms=db.execute("SELECT * FROM sms ORDER BY rowid DESC LIMIT 50").fetchall(),
        logs=db.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT 30").fetchall())


@app.post("/admin/orders")
@admin_required
def admin_create_order():
    amount = parse_amount(request.form.get("amount"))
    if amount is None:
        abort(400, "Amount 1 theke 500000 er moddhe hote hobe.")
    db = get_db()
    ref = create_order(db, amount, request.form.get("note", ""))
    return redirect(url_for("admin", new=ref))


@app.post("/admin/orders/<ref>/mark-paid")
@admin_required
def admin_mark_paid(ref):
    """Fallback when the SMS never arrives but you saw the money in your bKash app."""
    db = get_db()
    with tx(db):
        db.execute("UPDATE orders SET status='paid', paid_via='manual', paid_at=?, "
                   "paid_amount=amount WHERE ref=? AND status!='paid'", (now(), ref))
    return redirect(url_for("admin"))


@app.post("/api/orders")
def api_create_order():
    """For your own website/backend: create an order, get the pay link back."""
    if not API_KEY:
        abort(503, "API_KEY set kora hoy nai.")
    if not safe_eq(request.headers.get("X-API-Key", ""), API_KEY):
        abort(401)
    data = request.get_json(silent=True) or {}
    amount = parse_amount(data.get("amount"))
    if amount is None:
        return jsonify(error="invalid amount"), 400
    ref = create_order(get_db(), amount, str(data.get("note", "")))
    return jsonify(ref=ref, pay_url=url_for("pay", ref=ref, _external=True)), 201


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
