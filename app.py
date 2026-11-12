
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
        {"gb": "7 GB", "price": 4000},
        {"gb": "8.5 GB", "price": 5500},
        {"gb": "10 GB", "price": 7000},
        {"gb": "12.5 GB", "price": 9000}
    ],

    "Monthly": [
        {"gb": "10 GB", "price": 7000},
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

    return render_template(
        "index.html",
        packages=PACKAGES,
        networks=NETWORKS,
        packages_by_duration=PACKAGES_BY_DURATION,
        merchant_code=AIRTEL_MERCHANT_CODE
    )


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
                duration
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            duration
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

    return render_template(
        "admin_dashboard.html",
        orders=orders,
        total_orders=total_orders,
        pending=pending,
        verified=verified,
        completed=completed,
        revenue=revenue,
        statuses=STATUSES
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

    flash(
        "Order status updated successfully.",
        "success"
    )

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
