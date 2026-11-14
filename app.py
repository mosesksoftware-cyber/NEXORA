
from flask import (
    Flask, render_template, render_template_string,
    request, redirect, url_for, session, flash, abort, jsonify
)

import sqlite3
import os
import secrets
import hmac
import re

from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash


# ==================================================
# APPLICATION CONFIGURATION
# ==================================================

app = Flask(__name__)

app.secret_key = os.environ.get("MKS_SECRET_KEY")

ADMIN_USERNAME = os.environ.get("MKS_ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("MKS_ADMIN_PASSWORD")

AIRTEL_MERCHANT_CODE = os.environ.get(
    "MKS_AIRTEL_MERCHANT_CODE", ""
).strip()

if not app.secret_key:
    raise RuntimeError("MKS_SECRET_KEY is not configured.")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("Admin credentials are not configured.")


DB_NAME = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "moses_k_software.db"
)


# ==================================================
# PACKAGES
# ==================================================

NETWORKS = ["MTN", "Airtel", "Lyca"]

PACKAGES_BY_DURATION = {
    "Weekly": [
        {"gb": "12 GB", "price": 9000},
        {"gb": "15 GB", "price": 11000},
        {"gb": "17.5 GB", "price": 13000},
        {"gb": "19 GB", "price": 15000}
    ],

    "Monthly": [
        # 10 GB at UGX 7,000 is disabled.
        {"gb": "15 GB", "price": 10000},
        {"gb": "20 GB", "price": 15000},
        {"gb": "25 GB", "price": 20000},
        {"gb": "30 GB", "price": 25000}
    ]
}

PACKAGES = PACKAGES_BY_DURATION["Monthly"]


# ==================================================
# ORDER STATUSES
# ==================================================

STATUSES = [
    "Awaiting Payment",
    "Payment Verified",
    "Sent to Supplier",
    "Completed",
    "Rejected"
]


# ==================================================
# SESSION SECURITY
# ==================================================

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True
)


# ==================================================
# DATABASE CONNECTION
# ==================================================

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def generate_tracking_code():
    return "MKS-" + secrets.token_hex(5).upper()


# ==================================================
# DATABASE INITIALIZATION
# ==================================================

def init_db():

    with get_db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                network TEXT NOT NULL,
                package TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Awaiting Payment',
                created_at TEXT NOT NULL
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(orders)"
            ).fetchall()
        }

        migrations = {

            "payment_method": """
                ALTER TABLE orders
                ADD COLUMN payment_method TEXT
                NOT NULL DEFAULT 'Airtel Money'
            """,

            "payment_phone": """
                ALTER TABLE orders
                ADD COLUMN payment_phone TEXT
            """,

            "payment_reference": """
                ALTER TABLE orders
                ADD COLUMN payment_reference TEXT
            """,

            "payment_verified_at": """
                ALTER TABLE orders
                ADD COLUMN payment_verified_at TEXT
            """,

            "tracking_code": """
                ALTER TABLE orders
                ADD COLUMN tracking_code TEXT
            """,

            "duration": """
                ALTER TABLE orders
                ADD COLUMN duration TEXT NOT NULL DEFAULT 'Monthly'
            """,

            "payment_submitted_at": """
                ALTER TABLE orders
                ADD COLUMN payment_submitted_at TEXT
            """,

            "customer_id": """
                ALTER TABLE orders
                ADD COLUMN customer_id INTEGER
            """
        }

        for column, sql in migrations.items():

            if column not in columns:
                conn.execute(sql)

        existing_orders = conn.execute("""
            SELECT id
            FROM orders
            WHERE tracking_code IS NULL
            OR tracking_code = ''
        """).fetchall()

        for record in existing_orders:

            conn.execute("""
                UPDATE orders
                SET tracking_code = ?
                WHERE id = ?
            """, (
                generate_tracking_code(),
                record["id"]
            ))

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_orders_tracking_code
            ON orders(tracking_code)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                referral_code TEXT NOT NULL UNIQUE,
                referred_by INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (referred_by) REFERENCES customers(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_customer_id INTEGER NOT NULL UNIQUE,
                order_id INTEGER NOT NULL UNIQUE,
                amount INTEGER NOT NULL DEFAULT 1000,
                status TEXT NOT NULL DEFAULT 'Available',
                created_at TEXT NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES customers(id),
                FOREIGN KEY (referred_customer_id) REFERENCES customers(id),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                payout_phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_referral_code ON customers(referral_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer ON referral_rewards(referrer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_customer ON withdrawal_requests(customer_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS airtime_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL UNIQUE,
                network TEXT NOT NULL,
                airtime_phone TEXT NOT NULL,
                airtime_amount INTEGER NOT NULL,
                payout_phone TEXT NOT NULL,
                payout_amount INTEGER,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


# ==================================================
# CUSTOMER ACCOUNT AND REFERRAL HELPERS
# ==================================================

REFERRAL_REWARD_AMOUNT = 1000

def make_referral_code():
    return "NX" + secrets.token_hex(4).upper()

def current_customer():
    customer_id = session.get("customer_id")
    if not customer_id:
        return None
    with get_db() as conn:
        return conn.execute("SELECT id, name, email, referral_code, created_at FROM customers WHERE id = ?", (customer_id,)).fetchone()

def customer_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("customer_id"):
            return redirect(url_for("customer_login", next=request.path))
        return function(*args, **kwargs)
    return wrapper


# ==================================================
# CSRF SECURITY
# ==================================================

def csrf_token():

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


def check_csrf():

    submitted = request.form.get("csrf_token", "")
    saved = session.get("csrf_token", "")

    if not saved or not hmac.compare_digest(submitted, saved):
        abort(400, "Invalid form security token.")


# ==================================================
# ADMIN LOGIN PROTECTION
# ==================================================

def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))

        return function(*args, **kwargs)

    return wrapper


# ==================================================
# VALIDATION HELPERS
# ==================================================

def valid_uganda_phone(phone):

    return bool(
        re.fullmatch(r"(?:07\d{8}|2567\d{8})", phone)
    )


def is_ajax_request():

    return (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"

        or request.headers.get("Accept", "").startswith(
            "application/json"
        )
    )


# ==================================================
# HOME PAGE
# ==================================================

@app.route("/")
def home():

    referral_code = request.args.get("ref", "").strip().upper()
    if referral_code:
        with get_db() as conn:
            valid_referrer = conn.execute("SELECT id FROM customers WHERE referral_code = ?", (referral_code,)).fetchone()
        if valid_referrer:
            session["pending_referral_code"] = referral_code

    return render_template(
        "index.html",
        packages=PACKAGES,
        networks=NETWORKS,
        packages_by_duration=PACKAGES_BY_DURATION,
        merchant_code=AIRTEL_MERCHANT_CODE
    )


# ==================================================
# CUSTOMER REGISTRATION, LOGIN, REFERRALS AND WALLET
# ==================================================

@app.route("/register", methods=["GET", "POST"])
def customer_register():
    if request.method == "POST":
        check_csrf()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or len(name) > 80:
            flash("Enter a valid name.", "error")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            referral_code = session.get("pending_referral_code", "")
            with get_db() as conn:
                referrer = conn.execute("SELECT id FROM customers WHERE referral_code = ?", (referral_code,)).fetchone() if referral_code else None
                try:
                    code = make_referral_code()
                    while conn.execute("SELECT 1 FROM customers WHERE referral_code = ?", (code,)).fetchone():
                        code = make_referral_code()
                    cur = conn.execute("""INSERT INTO customers (name,email,password_hash,referral_code,referred_by,created_at) VALUES (?,?,?,?,?,?)""", (name,email,generate_password_hash(password),code,referrer["id"] if referrer else None,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    session.clear()
                    session["customer_id"] = cur.lastrowid
                    session["csrf_token"] = secrets.token_urlsafe(32)
                    flash("Account created successfully.", "success")
                    return redirect(url_for("customer_account"))
                except sqlite3.IntegrityError:
                    flash("An account with that email already exists.", "error")
    return render_template_string(ACCOUNT_AUTH_TEMPLATE, page_title="Create NEXORA Account", action=url_for("customer_register"), show_name=True)

@app.route("/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db() as conn:
            customer = conn.execute("SELECT * FROM customers WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if customer and check_password_hash(customer["password_hash"], password):
            session.clear()
            session["customer_id"] = customer["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("customer_account"))
        flash("Email or password is incorrect.", "error")
    return render_template_string(ACCOUNT_AUTH_TEMPLATE, page_title="Customer Login", action=url_for("customer_login"), show_name=False)

@app.route("/logout", methods=["POST"])
@customer_required
def customer_logout():
    check_csrf()
    session.clear()
    return redirect(url_for("home"))

@app.route("/account")
@customer_required
def customer_account():
    customer = current_customer()
    with get_db() as conn:
        earned = conn.execute("SELECT COALESCE(SUM(amount),0) FROM referral_rewards WHERE referrer_id=? AND status='Available'", (customer["id"],)).fetchone()[0]
        pending = conn.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE customer_id=? AND status IN ('Pending','Approved')", (customer["id"],)).fetchone()[0]
        rewards = conn.execute("SELECT rr.amount,rr.status,rr.created_at,o.tracking_code FROM referral_rewards rr JOIN orders o ON o.id=rr.order_id WHERE rr.referrer_id=? ORDER BY rr.id DESC", (customer["id"],)).fetchall()
        withdrawals = conn.execute("SELECT * FROM withdrawal_requests WHERE customer_id=? ORDER BY id DESC", (customer["id"],)).fetchall()
        referrals = conn.execute("SELECT COUNT(*) FROM customers WHERE referred_by=?", (customer["id"],)).fetchone()[0]
    balance = max(0, earned - pending)
    referral_link = request.url_root.rstrip("/") + "/?ref=" + customer["referral_code"]
    return render_template_string(ACCOUNT_PAGE_TEMPLATE, customer=customer,balance=balance,earned=earned,pending=pending,rewards=rewards,withdrawals=withdrawals,referrals=referrals,referral_link=referral_link)

@app.route("/withdraw", methods=["POST"])
@customer_required
def request_withdrawal():
    check_csrf()
    phone=request.form.get("payout_phone","").strip()
    try: amount=int(request.form.get("amount","0"))
    except ValueError: amount=0
    if not valid_uganda_phone(phone) or amount < 1000:
        flash("Enter a valid Ugandan payout number and a withdrawal of at least UGX 1,000.","error")
        return redirect(url_for("customer_account"))
    customer=current_customer()
    with get_db() as conn:
        earned=conn.execute("SELECT COALESCE(SUM(amount),0) FROM referral_rewards WHERE referrer_id=? AND status='Available'",(customer["id"],)).fetchone()[0]
        reserved=conn.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawal_requests WHERE customer_id=? AND status IN ('Pending','Approved','Paid')",(customer["id"],)).fetchone()[0]
        available=max(0,earned-reserved)
        if amount > available:
            flash("Your available referral balance is not enough for that withdrawal.","error")
        else:
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO withdrawal_requests (customer_id,amount,payout_phone,status,created_at,updated_at) VALUES (?,?,?,'Pending',?,?)",(customer["id"],amount,phone,now,now))
            flash("Withdrawal request submitted. It will be reviewed by NEXORA; this is not an instant payment.","success")
    return redirect(url_for("customer_account"))

@app.route("/admin/referrals/withdrawals")
@admin_required
def admin_withdrawals():
    with get_db() as conn:
        rows=conn.execute("SELECT w.*,c.name,c.email FROM withdrawal_requests w JOIN customers c ON c.id=w.customer_id ORDER BY w.id DESC").fetchall()
    return render_template_string(WITHDRAWAL_ADMIN_TEMPLATE, rows=rows)

@app.route("/admin/referrals/withdrawals/<int:withdrawal_id>", methods=["POST"])
@admin_required
def admin_update_withdrawal(withdrawal_id):
    check_csrf()
    status=request.form.get("status","")
    if status not in ("Approved","Paid","Rejected"):
        abort(400)
    with get_db() as conn:
        row=conn.execute("SELECT * FROM withdrawal_requests WHERE id=?",(withdrawal_id,)).fetchone()
        if row is None: abort(404)
        if status == "Paid" and row["status"] not in ("Approved","Paid"):
            flash("Approve a withdrawal before marking it paid.","error")
            return redirect(url_for("admin_withdrawals"))
        conn.execute("UPDATE withdrawal_requests SET status=?,updated_at=? WHERE id=?",(status,datetime.now().strftime("%Y-%m-%d %H:%M:%S"),withdrawal_id))
    flash("Withdrawal status updated. Complete the actual payout separately before choosing Paid.","success")
    return redirect(url_for("admin_withdrawals"))

ACCOUNT_AUTH_TEMPLATE = """<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>{{ page_title }}</title><style>body{font-family:Arial;background:#f2f5fb;color:#10213c;padding:20px}.card{max-width:430px;margin:35px auto;background:white;padding:25px;border-radius:15px}input,button{box-sizing:border-box;width:100%;padding:13px;margin:8px 0;border:1px solid #ccd3e0;border-radius:8px}button{background:#2454dc;color:white;border:0;font-weight:bold}.msg{padding:10px;background:#fff0f0;margin:8px 0}</style></head><body><div class='card'><h2>NEXORA</h2><h3>{{ page_title }}</h3>{% for category,message in get_flashed_messages(with_categories=true) %}<p class='msg'>{{ message }}</p>{% endfor %}<form method='post'><input type='hidden' name='csrf_token' value='{{ csrf_token() }}'>{% if show_name %}<label>Full name</label><input name='name' maxlength='80' required>{% endif %}<label>Email</label><input type='email' name='email' required><label>Password (minimum 8 characters)</label><input type='password' name='password' minlength='8' required><button type='submit'>{{ 'CREATE ACCOUNT' if show_name else 'LOGIN' }}</button></form><p><a href='/login'>Login</a> · <a href='/register'>Register</a> · <a href='/'>Home</a></p></div></body></html>"""

ACCOUNT_PAGE_TEMPLATE = """<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>NEXORA Account</title><style>body{font-family:Arial;background:#f2f5fb;color:#10213c;padding:18px}.card{max-width:720px;margin:20px auto;background:white;padding:22px;border-radius:14px}input,button{padding:12px;margin:6px 0;border:1px solid #ccd3e0;border-radius:7px}button{background:#2454dc;color:white;border:0}.link{overflow-wrap:anywhere;background:#edf2ff;padding:12px}</style></head><body><div class='card'><h2>NEXORA Account</h2><p>Welcome, {{ customer['name'] }} · {{ customer['email'] }}</p><h3>Referral wallet</h3><p><b>Available balance: UGX {{ '{:,}'.format(balance) }}</b></p><p>Total earned: UGX {{ '{:,}'.format(earned) }} · Referred accounts: {{ referrals }}</p><p>Earn UGX 1,000 when a person you refer registers and completes an order whose payment NEXORA verifies.</p><p>Your referral code: <b>{{ customer['referral_code'] }}</b></p><div class='link'>{{ referral_link }}</div><button onclick='navigator.clipboard.writeText({{ referral_link|tojson }})'>Copy referral link</button><h3>Request withdrawal</h3><form method='post' action='/withdraw'><input type='hidden' name='csrf_token' value='{{ csrf_token() }}'><input type='number' name='amount' min='1000' max='{{ balance }}' placeholder='Amount in UGX' required><input name='payout_phone' placeholder='07XXXXXXXX' pattern='07[0-9]{8}|2567[0-9]{8}' required><button>SUBMIT REQUEST</button></form><p>Withdrawal requests are reviewed manually. No automatic Mobile Money payout is connected.</p><h3>Reward history</h3>{% for r in rewards %}<p>UGX {{ '{:,}'.format(r['amount']) }} · {{ r['status'] }} · {{ r['tracking_code'] }}</p>{% else %}<p>No rewards yet.</p>{% endfor %}<h3>Withdrawal history</h3>{% for w in withdrawals %}<p>UGX {{ '{:,}'.format(w['amount']) }} · {{ w['status'] }} · {{ w['payout_phone'] }}</p>{% else %}<p>No withdrawals yet.</p>{% endfor %}<p><a href='/'>Home</a></p><form method='post' action='/logout'><input type='hidden' name='csrf_token' value='{{ csrf_token() }}'><button>LOG OUT</button></form></div></body></html>"""

WITHDRAWAL_ADMIN_TEMPLATE = """<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>NEXORA withdrawals</title><style>body{font-family:Arial;background:#f2f5fb;padding:20px;color:#10213c}.card{background:white;padding:16px;margin:12px auto;max-width:800px;border-radius:10px}button,select{padding:10px;margin:5px}</style></head><body><h2>NEXORA Withdrawal Requests</h2><p><a href='/admin'>Back to dashboard</a></p>{% for r in rows %}<div class='card'><b>{{ r['name'] }}</b> ({{ r['email'] }})<p>UGX {{ '{:,}'.format(r['amount']) }} · {{ r['payout_phone'] }} · {{ r['status'] }}</p><form method='post' action='/admin/referrals/withdrawals/{{ r['id'] }}'><input type='hidden' name='csrf_token' value='{{ csrf_token() }}'><select name='status'><option>Approved</option><option>Paid</option><option>Rejected</option></select><button>Update</button></form></div>{% else %}<p>No withdrawal requests.</p>{% endfor %}</body></html>"""


# ==================================================
# AIRTIME-TO-CASH CUSTOMER REQUESTS
# ==================================================

@app.route("/airtime/request", methods=["POST"])
def airtime_request():

    submitted = request.form.get("csrf_token", "")
    saved = session.get("csrf_token", "")

    if not saved or not hmac.compare_digest(submitted, saved):
        return jsonify({"success": False, "message": "Security token expired. Refresh the page and try again."}), 400

    network = request.form.get("network", "").strip()
    airtime_phone = request.form.get("airtime_phone", "").strip()
    payout_phone = request.form.get("payout_phone", "").strip()
    amount_raw = request.form.get("airtime_amount", "").strip()

    try:
        amount = int(amount_raw)
    except (TypeError, ValueError):
        amount = 0

    if network not in NETWORKS:
        error = "Choose MTN, Airtel or Lyca."
    elif not valid_uganda_phone(airtime_phone) or not valid_uganda_phone(payout_phone):
        error = "Enter valid Ugandan phone numbers for both fields."
    elif amount < 1000 or amount > 2000000:
        error = "Enter an airtime amount between UGX 1,000 and UGX 2,000,000."
    else:
        error = None

    if error:
        return jsonify({"success": False, "message": error}), 400

    reference = "NXA-" + secrets.token_hex(4).upper()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute("""
            INSERT INTO airtime_requests (
                reference, network, airtime_phone, airtime_amount,
                payout_phone, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?)
        """, (reference, network, airtime_phone, amount, payout_phone, now, now))

    return jsonify({
        "success": True,
        "message": "Your request has been received. NEXORA will verify the airtime before confirming any payout.",
        "reference": reference
    })


# ==================================================
# CUSTOMER ORDER SUBMISSION
# ==================================================

@app.route("/order", methods=["POST"])
def order():

    submitted = request.form.get("csrf_token", "")
    saved = session.get("csrf_token", "")

    if not saved or not hmac.compare_digest(submitted, saved):

        return jsonify({
            "success": False,
            "message": "Security token expired. Refresh the page and try again."
        }), 400

    name = request.form.get(
        "customer_name", ""
    ).strip()

    phone = request.form.get(
        "phone", ""
    ).strip()

    network = request.form.get(
        "network", ""
    ).strip()

    duration = request.form.get(
        "duration", ""
    ).strip()

    package = request.form.get(
        "package", ""
    ).strip()

    available_packages = PACKAGES_BY_DURATION.get(
        duration, []
    )

    selected = next(
        (p for p in available_packages if p["gb"] == package),
        None
    )

    error = None

    if not name or len(name) > 80:

        error = "Enter a valid customer name."

    elif not valid_uganda_phone(phone):

        error = "Enter a valid data recipient phone number."

    elif network not in NETWORKS or selected is None:

        error = "Select a valid network, duration and package."

    if error:

        return jsonify({
            "success": False,
            "message": error
        }), 400

    tracking_code = generate_tracking_code()

    with get_db() as conn:

        cursor = conn.execute("""
            INSERT INTO orders (
                customer_name,
                phone,
                network,
                package,
                amount,
                status,
                created_at,
                payment_method,
                tracking_code,
                duration,
                customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            phone,
            network,
            package,
            selected["price"],
            "Awaiting Payment",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Airtel Money Merchant",
            tracking_code,
            duration,
            session.get("customer_id")
        ))

        order_id = cursor.lastrowid

    session["customer_order_id"] = order_id

    return jsonify({

        "success": True,

        "message": "Your order has been received.",

        "order": {
            "id": order_id,
            "tracking_code": tracking_code,
            "network": network,
            "duration": duration,
            "package": package,
            "amount": selected["price"],
            "status": "Awaiting Payment",
            "phone": phone
        },

        "payment_url": url_for(
            "payment_page",
            order_id=order_id
        ),

        "success_url": url_for(
            "order_success",
            order_id=order_id
        ),

        "tracking_url": url_for(
            "track_order",
            tracking_code=tracking_code
        )
    })


# ==================================================
# ORDER CONFIRMATION
# LOADS templates/success.html
# ==================================================

@app.route("/order-success/<int:order_id>")
def order_success(order_id):

    if session.get("customer_order_id") != order_id:
        abort(404)

    with get_db() as conn:

        record = conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,)
        ).fetchone()

    if record is None:
        abort(404)

    return render_template(
        "success.html",
        order=record
    )


# ==================================================
# MERCHANT PAYMENT PAGE
# ==================================================

@app.route("/payment/<int:order_id>")
def payment_page(order_id):

    if session.get("customer_order_id") != order_id:
        abort(404)

    with get_db() as conn:

        record = conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,)
        ).fetchone()

    if record is None:
        abort(404)

    return render_template_string("""

    <!DOCTYPE html>
    <html lang="en">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <title>Airtel Merchant Payment | NEXORA</title>

        <style>

            body {
                margin: 0;
                padding: 20px;
                font-family: Arial;
                background: #f1f4f9;
                color: #091936;
            }

            .card {
                max-width: 450px;
                margin: 35px auto;
                padding: 25px;
                background: white;
                border-radius: 16px;
            }

            h2 {
                text-align: center;
            }

            .code {
                padding: 18px;
                background: #edf2ff;
                color: #2454dc;
                border-radius: 10px;
                text-align: center;
                font-size: 27px;
                font-weight: bold;
                overflow-wrap: anywhere;
            }

            .amount {
                text-align: center;
                font-size: 24px;
                font-weight: bold;
                margin: 18px;
            }

            .notice {
                padding: 15px;
                background: #fff4d6;
                border-radius: 9px;
                line-height: 1.7;
                font-size: 14px;
            }

            .btn {
                display: block;
                padding: 15px;
                margin-top: 13px;
                text-align: center;
                text-decoration: none;
                background: #2454dc;
                color: white;
                border-radius: 9px;
                font-weight: bold;
            }

        </style>

    </head>

    <body>

        <div class="card">

            <h2>NEXORA</h2>

            <h3>Airtel Money Merchant Payment</h3>

            <p>
                Tracking reference:
                <strong>{{ record["tracking_code"] }}</strong>
            </p>

            <p>Network: {{ record["network"] }}</p>

            <p>Package: {{ record["package"] }}</p>

            <div class="amount">
                UGX {{ "{:,}".format(record["amount"]) }}
            </div>

            {% if merchant_code %}

                <p>Use this merchant code:</p>

                <div class="code">
                    {{ merchant_code }}
                </div>

                <div class="notice">

                    1. Open Airtel Money.<br>
                    2. Select the merchant payment option.<br>
                    3. Enter the merchant code above.<br>
                    4. Enter the exact amount shown.<br>
                    5. Confirm the merchant details before paying.<br>
                    6. Return to NEXORA and choose COMPLETE MY ORDER.<br><br>

                    Never share your Mobile Money PIN or OTP.

                </div>

            {% else %}

                <div class="notice">

                    Merchant payment is not configured.
                    Please contact NEXORA.

                </div>

            {% endif %}

            <a class="btn" href="/">
                RETURN TO NEXORA
            </a>

            <a class="btn"
               href="{{ url_for(
                   'track_order',
                   tracking_code=record['tracking_code']
               ) }}">

                TRACK MY ORDER

            </a>

        </div>

    </body>

    </html>

    """, record=record, merchant_code=AIRTEL_MERCHANT_CODE)


# ==================================================
# CUSTOMER COMPLETE MY ORDER
# ==================================================

@app.route("/api/complete-paid-order", methods=["POST"])
def complete_paid_order():

    submitted = request.form.get("csrf_token", "")
    saved = session.get("csrf_token", "")

    if not saved or not hmac.compare_digest(submitted, saved):

        return jsonify({
            "success": False,
            "message": "Security token expired. Refresh the page and try again."
        }), 400

    tracking_code = request.form.get(
        "tracking_code", ""
    ).strip().upper()

    phone = request.form.get(
        "phone", ""
    ).strip()

    if not tracking_code or not valid_uganda_phone(phone):

        return jsonify({
            "success": False,
            "message": "Enter your tracking reference and the phone number used when ordering."
        }), 400

    if not AIRTEL_MERCHANT_CODE:

        return jsonify({
            "success": False,
            "message": "Merchant payment is not configured. Please contact the administrator."
        }), 400

    with get_db() as conn:

        record = conn.execute("""
            SELECT id, status, payment_submitted_at
            FROM orders
            WHERE tracking_code = ? AND phone = ?
        """, (
            tracking_code,
            phone
        )).fetchone()

        if record is None:

            return jsonify({
                "success": False,
                "message": "Order not found. Check your tracking reference and phone number."
            }), 404

        if record["status"] != "Awaiting Payment":

            return jsonify({
                "success": False,
                "message": "This order has already been updated."
            }), 400

        if record["payment_submitted_at"]:

            session["customer_order_id"] = record["id"]

            return jsonify({
                "success": True,
                "message": "Your order has already been submitted for payment verification.",
                "tracking_code": tracking_code,
                "status": record["status"]
            })

        cursor = conn.execute("""
            UPDATE orders
            SET payment_submitted_at = ?
            WHERE id = ?
              AND status = 'Awaiting Payment'
              AND payment_submitted_at IS NULL
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            record["id"]
        ))

        if cursor.rowcount != 1:

            return jsonify({
                "success": False,
                "message": "Could not submit your order. Please try again."
            }), 409

        session["customer_order_id"] = record["id"]

    return jsonify({

        "success": True,

        "message": "Your order has been submitted. NEXORA will verify payment after checking the merchant account.",

        "tracking_code": tracking_code,

        "status": "Awaiting Payment"
    })


# ==================================================
# PAYMENT STATUS
# ==================================================

@app.route("/api/payment-status", methods=["POST"])
def api_payment_status():

    order_id = session.get("customer_order_id")

    tracking_code = request.form.get(
        "tracking_code", ""
    ).strip().upper()

    if not order_id or not tracking_code:

        return jsonify({
            "success": False,
            "message": "Order session expired. Please use Track Order."
        }), 403

    with get_db() as conn:

        record = conn.execute("""
            SELECT id, tracking_code, network, duration, package,
                   amount, status, payment_submitted_at, created_at
            FROM orders
            WHERE id = ? AND tracking_code = ?
        """, (
            order_id,
            tracking_code
        )).fetchone()

    if record is None:

        return jsonify({
            "success": False,
            "message": "Order not found."
        }), 404

    return jsonify({

        "success": True,

        "order": {
            "id": record["id"],
            "tracking_code": record["tracking_code"],
            "network": record["network"],
            "duration": record["duration"],
            "package": record["package"],
            "amount": record["amount"],
            "status": record["status"],
            "payment_submitted_at": record["payment_submitted_at"],
            "created_at": record["created_at"]
        }
    })


# ==================================================
# CUSTOMER ORDER TRACKING
# ==================================================

@app.route("/track", methods=["GET", "POST"])
def track_order():

    order = None
    error = None

    prefill_tracking_code = request.args.get(
        "tracking_code", ""
    ).strip().upper()

    if request.method == "POST":

        tracking_code = request.form.get(
            "tracking_code", ""
        ).strip().upper()

        phone = request.form.get(
            "phone", ""
        ).strip()

        prefill_tracking_code = tracking_code

        if not tracking_code or not valid_uganda_phone(phone):

            error = "Enter a valid tracking reference and phone number."

        else:

            with get_db() as conn:

                order = conn.execute("""
                    SELECT tracking_code, network, duration, package,
                           amount, status, created_at, payment_submitted_at
                    FROM orders
                    WHERE tracking_code = ? AND phone = ?
                """, (
                    tracking_code,
                    phone
                )).fetchone()

            if order is None:

                error = "Order not found. Check your tracking reference and phone number."

        if is_ajax_request():

            if error:

                return jsonify({
                    "success": False,
                    "message": error
                }), 400

            return jsonify({

                "success": True,

                "order": {
                    "tracking_code": order["tracking_code"],
                    "network": order["network"],
                    "duration": order["duration"],
                    "package": order["package"],
                    "amount": order["amount"],
                    "status": order["status"],
                    "created_at": order["created_at"],
                    "payment_submitted_at": order["payment_submitted_at"]
                }
            })

    return render_template_string("""

    <!DOCTYPE html>
    <html lang="en">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <title>Track Order | NEXORA</title>

        <style>

            body {
                margin: 0;
                padding: 20px;
                font-family: Arial;
                background: #f3f5fa;
                color: #091936;
            }

            .container {
                max-width: 480px;
                margin: 35px auto;
                background: white;
                padding: 25px;
                border-radius: 15px;
            }

            input {
                width: 100%;
                padding: 13px;
                margin: 9px 0 17px;
                border: 1px solid #ccd3e0;
                border-radius: 7px;
                box-sizing: border-box;
            }

            button {
                width: 100%;
                padding: 14px;
                background: #2454dc;
                color: white;
                border: 0;
                border-radius: 7px;
                font-size: 15px;
            }

            .error {
                background: #ffe9e9;
                color: #a00000;
                padding: 12px;
                border-radius: 7px;
            }

            .result, .step {
                background: #edf2ff;
                padding: 13px;
                border-radius: 8px;
                margin-top: 10px;
            }

            .current {
                background: #d8f3df;
            }

            .rejected {
                background: #ffe5e5;
                color: #a00000;
            }

            .service-announcement {
                background: #fff0f6;
                border: 1px solid #f3b6d0;
                border-left: 5px solid #e83e83;
                color: #17213b;
                padding: 15px;
                border-radius: 10px;
                margin: 18px 0;
                text-align: left;
                line-height: 1.6;
                font-size: 14px;
            }

            .service-announcement h4 {
                color: #d52d76;
                margin: 0 0 9px;
                font-size: 16px;
            }

            .service-announcement p {
                margin: 8px 0;
            }

            a {
                display: block;
                text-align: center;
                margin-top: 20px;
                color: #2454dc;
            }

        </style>

    </head>

    <body>

        <div class="container">

            <h2>NEXORA</h2>

            <h3>Track Your Order</h3>

            <div class="service-announcement">
                <h4>📢 NEXORA Service Announcement</h4>

                <p><strong>Dear Valued Customer,</strong></p>

                <p>
                    We sincerely apologize for the inconvenience caused
                    while delivering your requested data packages.
                </p>

                <p>
                    We are currently improving our services to provide
                    a better and more reliable customer experience.
                </p>

                <p>
                    We will notify you as soon as our services return
                    to normal operation.
                </p>

                <p>
                    Thank you for your patience, understanding and
                    continued support.
                </p>

                <strong>NEXORA — Your Digital World, Simplified. 🚀</strong>
            </div>

            <p>
                Enter your tracking reference and the phone number
                used when ordering.
            </p>

            {% if error %}

                <div class="error">
                    {{ error }}
                </div>

            {% endif %}

            <form method="POST">

                <label>Tracking Reference</label>

                <input
                    name="tracking_code"
                    value="{{ prefill_tracking_code }}"
                    placeholder="MKS-XXXXXXXXXX"
                    required
                >

                <label>Order Phone Number</label>

                <input
                    type="tel"
                    name="phone"
                    placeholder="07XXXXXXXX"
                    pattern="07[0-9]{8}|2567[0-9]{8}"
                    required
                >

                <button type="submit">
                    TRACK ORDER
                </button>

            </form>

            {% if order %}

                <div class="result">

                    <p>
                        <strong>Reference:</strong>
                        {{ order["tracking_code"] }}
                    </p>

                    <p>
                        <strong>Network:</strong>
                        {{ order["network"] }}
                    </p>

                    <p>
                        <strong>Duration:</strong>
                        {{ order["duration"] }}
                    </p>

                    <p>
                        <strong>Package:</strong>
                        {{ order["package"] }}
                    </p>

                    <p>
                        <strong>Amount:</strong>
                        UGX {{ "{:,}".format(order["amount"]) }}
                    </p>

                    <p>
                        <strong>Order date:</strong>
                        {{ order["created_at"] }}
                    </p>

                    <p>
                        <strong>Status:</strong>
                        {{ order["status"] }}
                    </p>

                    {% if order["status"] == "Rejected" %}

                        <div class="step rejected">
                            Order rejected. Please contact NEXORA.
                        </div>

                    {% else %}

                        {% set steps = [
                            "Awaiting Payment",
                            "Payment Verified",
                            "Sent to Supplier",
                            "Completed"
                        ] %}

                        {% set current = steps.index(order["status"])
                            if order["status"] in steps else -1 %}

                        {% for step in steps %}

                            <div class="step {% if current >= loop.index0 %}current{% endif %}">

                                {% if current >= loop.index0 %}
                                    ✓
                                {% else %}
                                    ○
                                {% endif %}

                                {{ step }}

                            </div>

                        {% endfor %}

                    {% endif %}

                </div>

            {% endif %}

            <a href="/">Back to NEXORA</a>

        </div>

    </body>

    </html>

    """,
    order=order,
    error=error,
    prefill_tracking_code=prefill_tracking_code)


# ==================================================
# ADMIN LOGIN
# ==================================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":

        check_csrf()

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        username_ok = hmac.compare_digest(
            username,
            ADMIN_USERNAME
        )

        password_ok = hmac.compare_digest(
            password,
            ADMIN_PASSWORD
        )

        if username_ok and password_ok:

            session.clear()

            session["admin_logged_in"] = True

            session["csrf_token"] = secrets.token_urlsafe(32)

            return redirect(url_for("admin_dashboard"))

        flash(
            "Incorrect username or password.",
            "error"
        )

    return render_template("admin_login.html")


# ==================================================
# ADMIN DASHBOARD
# ==================================================

@app.route("/admin")
@admin_required
def admin_dashboard():

    with get_db() as conn:

        orders = conn.execute("""
            SELECT *
            FROM orders
            ORDER BY id DESC
        """).fetchall()

        total_orders = conn.execute("""
            SELECT COUNT(*)
            FROM orders
        """).fetchone()[0]

        pending = conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = 'Awaiting Payment'
        """).fetchone()[0]

        verified = conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status IN (
                'Payment Verified',
                'Sent to Supplier',
                'Completed'
            )
        """).fetchone()[0]

        completed = conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = 'Completed'
        """).fetchone()[0]

        revenue = conn.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM orders
            WHERE status IN (
                'Payment Verified',
                'Sent to Supplier',
                'Completed'
            )
        """).fetchone()[0]

        airtime_requests = conn.execute("""
            SELECT * FROM airtime_requests ORDER BY id DESC
        """).fetchall()

    return render_template(
        "admin_dashboard.html",
        orders=orders,
        total_orders=total_orders,
        pending=pending,
        verified=verified,
        completed=completed,
        revenue=revenue,
        statuses=STATUSES,
        airtime_requests=airtime_requests,
        airtime_statuses=["Pending", "Verifying", "Airtime Verified", "Payout Sent", "Rejected"]
    )


# ==================================================
# UPDATE ORDER STATUS
# ==================================================

@app.route("/admin/update/<int:order_id>", methods=["POST"])
@admin_required
def update_order(order_id):

    check_csrf()

    new_status = request.form.get(
        "status", ""
    ).strip()

    if new_status not in STATUSES:

        flash("Invalid order status.", "error")

        return redirect(url_for("admin_dashboard"))

    with get_db() as conn:

        current = conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,)
        ).fetchone()

        if current is None:
            abort(404)

        if new_status == "Completed" and not current["payment_verified_at"]:
            flash("Verify the customer's payment before completing this order.", "error")
            return redirect(url_for("admin_dashboard"))

        if new_status == "Payment Verified":

            if not current["payment_submitted_at"]:

                flash(
                    "Customer has not submitted the order for payment verification.",
                    "error"
                )

                return redirect(url_for("admin_dashboard"))

            conn.execute("""
                UPDATE orders
                SET status = ?, payment_verified_at = ?
                WHERE id = ?
            """, (
                new_status,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order_id
            ))

        else:

            conn.execute("""
                UPDATE orders
                SET status = ?
                WHERE id = ?
            """, (
                new_status,
                order_id
            ))

        if new_status == "Completed":
            linked = conn.execute("""
                SELECT c.id, c.referred_by
                FROM orders o JOIN customers c ON c.id = o.customer_id
                WHERE o.id = ?
            """, (order_id,)).fetchone()
            if linked and linked["referred_by"]:
                conn.execute("""
                    INSERT OR IGNORE INTO referral_rewards
                    (referrer_id,referred_customer_id,order_id,amount,status,created_at)
                    VALUES (?,?,?,?,'Available',?)
                """, (linked["referred_by"],linked["id"],order_id,REFERRAL_REWARD_AMOUNT,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    flash(
        "Order status updated successfully.",
        "success"
    )

    return redirect(url_for("admin_dashboard"))


# ==================================================
# UPDATE AIRTIME-TO-CASH REQUEST
# ==================================================

@app.route("/admin/airtime/update/<int:request_id>", methods=["POST"])
@admin_required
def update_airtime_request(request_id):

    check_csrf()
    new_status = request.form.get("status", "").strip()
    payout_raw = request.form.get("payout_amount", "").strip()
    allowed = ["Pending", "Verifying", "Airtime Verified", "Payout Sent", "Rejected"]

    if new_status not in allowed:
        flash("Invalid airtime request status.", "error")
        return redirect(url_for("admin_dashboard"))

    payout_amount = None
    if payout_raw:
        try:
            payout_amount = int(payout_raw)
            if payout_amount < 1 or payout_amount > 2000000:
                raise ValueError
        except ValueError:
            flash("Enter a valid payout amount in UGX.", "error")
            return redirect(url_for("admin_dashboard"))

    with get_db() as conn:
        current = conn.execute(
            "SELECT * FROM airtime_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if current is None:
            abort(404)

        if new_status == "Payout Sent":
            if not payout_amount:
                payout_amount = current["payout_amount"]
            if not payout_amount:
                flash("Enter and save the actual payout amount before marking payout sent.", "error")
                return redirect(url_for("admin_dashboard"))
            if current["status"] not in ("Airtime Verified", "Payout Sent"):
                flash("Verify receipt of the airtime before marking a payout as sent.", "error")
                return redirect(url_for("admin_dashboard"))

        conn.execute("""
            UPDATE airtime_requests
            SET status = ?, payout_amount = COALESCE(?, payout_amount), updated_at = ?
            WHERE id = ?
        """, (new_status, payout_amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), request_id))

    flash("Airtime request updated.", "success")
    return redirect(url_for("admin_dashboard"))


# ==================================================
# ADMIN LOGOUT
# ==================================================

@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():

    check_csrf()

    session.clear()

    return redirect(url_for("admin_login"))


# ==================================================
# INITIALIZE DATABASE
# ==================================================

init_db()


# ==================================================
# RUN APPLICATION
# ==================================================

if __name__ == "__main__":

    app.run(debug=False)
