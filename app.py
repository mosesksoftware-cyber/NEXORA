# ==================================================
# DATABASE INITIALIZATION
# ==================================================

def init_db():

    with get_db() as conn:

        # ------------------------------------------
        # EXISTING ORDERS TABLE
        # ------------------------------------------

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

        # ------------------------------------------
        # FILL MISSING TRACKING CODES
        # ------------------------------------------

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

        # ------------------------------------------
        # EXISTING AIRTIME-TO-CASH TABLE
        # ------------------------------------------

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

        # ------------------------------------------
        # CUSTOMER ACCOUNTS
        # ------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                referral_code TEXT NOT NULL UNIQUE,
                referred_by INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (referred_by)
                    REFERENCES customers(id)
            )
        """)

        # ------------------------------------------
        # REFERRAL REWARDS
        # UGX 1,000 PER QUALIFYING CUSTOMER
        # ------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_customer_id INTEGER NOT NULL UNIQUE,
                order_id INTEGER NOT NULL UNIQUE,
                amount INTEGER NOT NULL DEFAULT 1000,
                status TEXT NOT NULL DEFAULT 'Available',
                created_at TEXT NOT NULL,

                FOREIGN KEY (referrer_id)
                    REFERENCES customers(id),

                FOREIGN KEY (referred_customer_id)
                    REFERENCES customers(id),

                FOREIGN KEY (order_id)
                    REFERENCES orders(id)
            )
        """)

        # ------------------------------------------
        # WITHDRAWAL REQUESTS
        # ------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                payout_phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY (customer_id)
                    REFERENCES customers(id)
            )
        """)

        # ------------------------------------------
        # DATABASE INDEXES
        # ------------------------------------------

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_customers_referral_code
            ON customers(referral_code)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_rewards_referrer
            ON referral_rewards(referrer_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_withdrawals_customer
            ON withdrawal_requests(customer_id)
        """)
