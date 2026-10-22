"""SQLite persistence for OfferBot. Uses DATABASE_PATH; no PostgreSQL dependency."""
from contextlib import contextmanager
from typing import Any, Optional, Dict
from pathlib import Path
import os
import re
import sqlite3


def _sqlite_sql(sql: str) -> str:
    """Translate the small set of PostgreSQL syntax used by the original DB layer."""
    sql = re.sub(r"\bBIGSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", sql, flags=re.I)
    sql = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", sql, flags=re.I)
    sql = re.sub(r"\bBOOLEAN\b", "INTEGER", sql, flags=re.I)
    sql = re.sub(r"\bBIGINT\b", "INTEGER", sql, flags=re.I)
    sql = re.sub(r"\bDOUBLE\s+PRECISION\b", "REAL", sql, flags=re.I)
    sql = re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.I)
    sql = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)::date", r"date(\1)", sql, flags=re.I)
    sql = re.sub(r"ADD COLUMN IF NOT EXISTS", "ADD COLUMN", sql, flags=re.I)
    sql = sql.replace("%s", "?")
    return sql

class _SQLiteCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        translated = _sqlite_sql(sql)
        try:
            self._cursor.execute(translated, params)
        except sqlite3.OperationalError as exc:
            # SQLite has no "ADD COLUMN IF NOT EXISTS"; ignore only duplicate-column migrations.
            if "duplicate column name" in str(exc).lower() and "ALTER TABLE" in translated.upper():
                return self
            raise
        return self

    def executemany(self, sql, params):
        self._cursor.executemany(_sqlite_sql(sql), params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._cursor.close()


class _SQLiteConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return _SQLiteCursor(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


class Database:
    def __init__(self, database_path: str):
        if not database_path:
            raise RuntimeError("DATABASE_PATH is required")
        self.database_path = database_path
        self.database_url = database_path  # Compatibility with older code.
        if database_path != ":memory:" and not database_path.startswith("file:"):
            Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.database_path, timeout=30, uri=self.database_path.startswith("file:"))
        con.row_factory = lambda cursor, row: {column[0]: row[index] for index, column in enumerate(cursor.description)}
        wrapped = _SQLiteConnection(con)
        try:
            yield wrapped
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init_schema(self):
        statements = [
        """CREATE TABLE IF NOT EXISTS bot_users (
            user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            is_blocked BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS offer_business_connections (
            connection_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, username TEXT, first_name TEXT, last_name TEXT,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE, is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE TABLE IF NOT EXISTS offer_access_users (user_id BIGINT PRIMARY KEY)",
        "CREATE TABLE IF NOT EXISTS offer_blocked_users (user_id BIGINT PRIMARY KEY)",
        "CREATE TABLE IF NOT EXISTS offer_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS offer_user_profiles (
            user_id BIGINT PRIMARY KEY, name TEXT, url TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS gift_offers (
            id BIGSERIAL PRIMARY KEY, seller_id BIGINT NOT NULL, seller_username TEXT, buyer_id BIGINT NOT NULL,
            buyer_username TEXT, buyer_url TEXT, gift_name TEXT NOT NULL, gift_number BIGINT NOT NULL,
            gift_url TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL, currency TEXT NOT NULL,
            locale TEXT NOT NULL DEFAULT 'ru', status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ, accepted_at TIMESTAMPTZ,
            message_chat_id BIGINT, message_id BIGINT, business_connection_id TEXT, transfer_url TEXT)""",
        """CREATE TABLE IF NOT EXISTS payments (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, memo TEXT NOT NULL UNIQUE,
            amount DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), paid_at TIMESTAMPTZ,
            provider TEXT NOT NULL DEFAULT 'ton_wallet', provider_invoice_id TEXT, pay_url TEXT)""",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'ton_wallet'",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider_invoice_id TEXT",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS pay_url TEXT",
        """CREATE TABLE IF NOT EXISTS referrals (
            user_id BIGINT PRIMARY KEY, referred_by BIGINT, balance DOUBLE PRECISION NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS referral_rewards (
            id BIGSERIAL PRIMARY KEY, referrer_id BIGINT NOT NULL, referred_id BIGINT NOT NULL UNIQUE,
            amount DOUBLE PRECISION NOT NULL DEFAULT 3, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS idx_offers_status ON gift_offers(status)",
        "CREATE INDEX IF NOT EXISTS idx_offers_seller ON gift_offers(seller_id)",
        "CREATE INDEX IF NOT EXISTS idx_offers_buyer ON gift_offers(buyer_id)",
        "CREATE INDEX IF NOT EXISTS idx_payments_memo ON payments(memo)",
        "CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_bot_users_updated ON bot_users(updated_at)"
        ]
        with self.connect() as con:
            with con.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

    def register_user(self, user_id: int, username: Optional[str] = None,
                      first_name: Optional[str] = None, last_name: Optional[str] = None):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("""INSERT INTO bot_users(user_id,username,first_name,last_name,updated_at)
                VALUES(%s,%s,%s,%s,NOW()) ON CONFLICT(user_id) DO UPDATE SET
                username=EXCLUDED.username, first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name, updated_at=NOW()""",
                (user_id, username, first_name, last_name))

    def list_broadcast_users(self):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT user_id FROM bot_users WHERE is_blocked=FALSE ORDER BY user_id")
            return [int(r['user_id']) for r in cur.fetchall()]

    def mark_user_blocked(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE bot_users SET is_blocked=TRUE, updated_at=NOW() WHERE user_id=%s", (user_id,))

    def upsert_business_connection(self, connection_id: str, user_id: int, username: Optional[str],
                                   first_name: Optional[str], last_name: Optional[str], enabled: bool):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("""INSERT INTO offer_business_connections
                (connection_id,user_id,username,first_name,last_name,is_enabled,is_blocked,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,FALSE,NOW()) ON CONFLICT(connection_id) DO UPDATE SET
                user_id=EXCLUDED.user_id,username=EXCLUDED.username,first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name,is_enabled=EXCLUDED.is_enabled,updated_at=NOW()""",
                (connection_id,user_id,username,first_name,last_name,enabled))

    def get_business_connection(self, connection_id: str):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM offer_business_connections WHERE connection_id=%s", (connection_id,))
            return cur.fetchone()

    def get_connection_for_user(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM offer_business_connections WHERE user_id=%s AND is_enabled=TRUE ORDER BY updated_at DESC LIMIT 1", (user_id,))
            return cur.fetchone()

    def set_connection_blocked(self, connection_id: str, blocked: bool):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE offer_business_connections SET is_blocked=%s,updated_at=NOW() WHERE connection_id=%s", (blocked,connection_id))

    def add_access(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("INSERT INTO offer_access_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,))

    def remove_access(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM offer_access_users WHERE user_id=%s", (user_id,))

    def unblock_offer_user(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM offer_blocked_users WHERE user_id=%s", (user_id,))

    def block_offer_user(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("INSERT INTO offer_blocked_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,))

    def is_blocked(self, user_id: int) -> bool:
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT 1 FROM offer_blocked_users WHERE user_id=%s", (user_id,))
            return cur.fetchone() is not None

    def has_access(self, user_id: int, owner_id: int, mode: str) -> bool:
        if self.is_blocked(user_id): return False
        if user_id == owner_id or mode == "all": return True
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT 1 FROM offer_access_users WHERE user_id=%s", (user_id,))
            return cur.fetchone() is not None

    def set_profile(self, user_id: int, name: Optional[str], url: Optional[str]):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("""INSERT INTO offer_user_profiles(user_id,name,url,updated_at) VALUES(%s,%s,%s,NOW())
                ON CONFLICT(user_id) DO UPDATE SET name=EXCLUDED.name,url=EXCLUDED.url,updated_at=NOW()""", (user_id,name,url))

    def get_profile(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM offer_user_profiles WHERE user_id=%s", (user_id,))
            return cur.fetchone()

    def create_offer(self, data: Dict[str, Any]) -> int:
        keys = ('seller_id','seller_username','buyer_id','buyer_username','buyer_url','gift_name','gift_number','gift_url','amount','currency','locale','status','created_at','expires_at','message_chat_id','message_id','business_connection_id','transfer_url')
        values = [data[k] for k in keys]
        with self.connect() as con, con.cursor() as cur:
            cur.execute("""INSERT INTO gift_offers(seller_id,seller_username,buyer_id,buyer_username,buyer_url,gift_name,gift_number,gift_url,amount,currency,locale,status,created_at,expires_at,message_chat_id,message_id,business_connection_id,transfer_url)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", values)
            return int(cur.fetchone()['id'])

    def set_offer_message(self, offer_id: int, chat_id: int, message_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE gift_offers SET message_chat_id=%s,message_id=%s WHERE id=%s", (chat_id,message_id,offer_id))

    def delete_offer(self, offer_id: int):
        with self.connect() as con, con.cursor() as cur: cur.execute("DELETE FROM gift_offers WHERE id=%s", (offer_id,))

    def get_offer(self, offer_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM gift_offers WHERE id=%s", (offer_id,)); return cur.fetchone()

    def update_offer_status(self, offer_id: int, status: str, transfer_url: Optional[str] = None):
        with self.connect() as con, con.cursor() as cur:
            if status == 'accepted':
                cur.execute("UPDATE gift_offers SET status=%s,transfer_url=COALESCE(%s,transfer_url),accepted_at=NOW() WHERE id=%s", (status,transfer_url,offer_id))
            else:
                cur.execute("UPDATE gift_offers SET status=%s,transfer_url=COALESCE(%s,transfer_url) WHERE id=%s", (status,transfer_url,offer_id))

    def list_connections(self):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM offer_business_connections ORDER BY updated_at DESC"); return cur.fetchall()


    def find_user_by_username(self, username: str):
        username = username.lstrip("@").lower()
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT user_id,username,first_name,last_name FROM bot_users WHERE LOWER(username)=%s ORDER BY updated_at DESC LIMIT 1", (username,))
            return cur.fetchone()

    def user_profile_stats(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT username,first_name,last_name,created_at FROM bot_users WHERE user_id=%s", (user_id,))
            user = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status='paid') AS paid, COALESCE(SUM(amount) FILTER (WHERE status='paid'),0) AS spent FROM payments WHERE user_id=%s", (user_id,))
            payments = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS total FROM offer_business_connections WHERE user_id=%s AND is_enabled=TRUE", (user_id,))
            connections = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS total FROM referral_rewards WHERE referrer_id=%s", (user_id,))
            referrals = cur.fetchone()
            return {'user': user, 'payments': payments, 'connections': int(connections['total'] or 0), 'referrals': int(referrals['total'] or 0)}

    def total_users(self) -> int:
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM bot_users")
            return int(cur.fetchone()['c'] or 0)

    def register_referral(self, user_id: int, referrer_id: int | None):
        if referrer_id == user_id: referrer_id = None
        with self.connect() as con, con.cursor() as cur:
            cur.execute("INSERT INTO referrals(user_id,referred_by) VALUES(%s,%s) ON CONFLICT DO NOTHING", (user_id,referrer_id))
            if referrer_id:
                cur.execute("UPDATE referrals SET referred_by=%s WHERE user_id=%s AND referred_by IS NULL", (referrer_id,user_id))

    def referral_info(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT balance FROM referrals WHERE user_id=%s", (user_id,)); row=cur.fetchone()
            cur.execute("SELECT COUNT(*) AS count FROM referral_rewards WHERE referrer_id=%s", (user_id,)); count=cur.fetchone()['count']
            return {'balance': float(row['balance']) if row else 0.0, 'count': int(count)}

    def reward_referrer_for_payment(self, referred_id: int, amount: float = 3.0):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT referred_by FROM referrals WHERE user_id=%s", (referred_id,)); row=cur.fetchone()
            if not row or not row['referred_by'] or row['referred_by'] == referred_id: return None
            referrer_id = int(row['referred_by'])
            cur.execute("INSERT INTO referral_rewards(referrer_id,referred_id,amount) VALUES(%s,%s,%s) ON CONFLICT(referred_id) DO NOTHING RETURNING id", (referrer_id,referred_id,amount))
            if cur.fetchone():
                cur.execute("INSERT INTO referrals(user_id,balance) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET balance=referrals.balance+EXCLUDED.balance", (referrer_id,amount))
                return referrer_id
            return None

    def cleanup_expired_offers(self):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM gift_offers WHERE expires_at IS NOT NULL AND expires_at<=NOW() AND status='pending'")
            return cur.rowcount

    def daily_offer_stats(self):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status='accepted') AS accepted FROM gift_offers WHERE created_at::date=CURRENT_DATE")
            row=cur.fetchone(); total=int(row['total'] or 0); accepted=int(row['accepted'] or 0)
            return total, accepted, (accepted / total * 100 if total else 0.0)

    def stats(self):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT status,COUNT(*) AS c FROM gift_offers GROUP BY status")
            return {r['status']: r['c'] for r in cur.fetchall()}

    def create_payment(self, user_id: int, memo: str, amount: float, provider: str = 'ton_wallet') -> int:
        with self.connect() as con, con.cursor() as cur:
            cur.execute("INSERT INTO payments(user_id,memo,amount,status,created_at,provider) VALUES(%s,%s,%s,'pending',NOW(),%s) RETURNING id", (user_id,memo,amount,provider))
            return int(cur.fetchone()['id'])

    def set_payment_provider_data(self, payment_id: int, provider_invoice_id: str, pay_url: str):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE payments SET provider_invoice_id=%s,pay_url=%s WHERE id=%s", (str(provider_invoice_id), pay_url, payment_id))

    def fail_payment(self, payment_id: int):
        """Mark an invoice-creation failure; never overwrite a confirmed payment."""
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE payments SET status='failed' WHERE id=%s AND status='pending' AND provider_invoice_id IS NULL", (payment_id,))

    def get_payment_by_memo(self, memo: str):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE memo=%s", (memo,)); return cur.fetchone()

    def get_pending_payment(self, user_id: int):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE user_id=%s AND status='pending' ORDER BY created_at DESC LIMIT 1", (user_id,)); return cur.fetchone()

    def mark_payment_paid(self, payment_id: int) -> bool:
        """Atomically mark a payment paid. True only for the first successful transition."""
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE id=%s AND status='pending' RETURNING id", (payment_id,))
            return cur.fetchone() is not None

    def confirm_payment_and_grant_access(self, payment_id: int, user_id: int) -> bool:
        """Confirm once and grant access in the same PostgreSQL transaction."""
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE id=%s AND user_id=%s AND status='pending' RETURNING id", (payment_id, user_id))
            if cur.fetchone() is None:
                return False
            cur.execute("INSERT INTO offer_access_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,))
            cur.execute("DELETE FROM offer_blocked_users WHERE user_id=%s", (user_id,))
            cur.execute("UPDATE offer_business_connections SET is_blocked=FALSE,updated_at=NOW() WHERE user_id=%s", (user_id,))
            return True

    def confirm_payment_access_and_reward_referrer(self, payment_id: int, user_id: int, reward_amount: float = 3.0):
        """Atomically confirm first payment, grant access and credit a referrer once.

        Returns the referrer's Telegram ID when a reward was newly credited, otherwise None.
        A unique referral_rewards.referred_id prevents duplicate rewards across retries.
        """
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE id=%s AND user_id=%s AND status='pending' RETURNING id", (payment_id, user_id))
            if cur.fetchone() is None:
                return None
            cur.execute("INSERT INTO offer_access_users(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,))
            cur.execute("DELETE FROM offer_blocked_users WHERE user_id=%s", (user_id,))
            cur.execute("UPDATE offer_business_connections SET is_blocked=FALSE,updated_at=NOW() WHERE user_id=%s", (user_id,))
            cur.execute("SELECT referred_by FROM referrals WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if not row or not row['referred_by'] or int(row['referred_by']) == user_id:
                return None
            referrer_id = int(row['referred_by'])
            cur.execute("INSERT INTO referral_rewards(referrer_id,referred_id,amount) VALUES(%s,%s,%s) ON CONFLICT(referred_id) DO NOTHING RETURNING id", (referrer_id, user_id, reward_amount))
            if cur.fetchone() is None:
                return None
            cur.execute("INSERT INTO referrals(user_id,balance) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET balance=referrals.balance+EXCLUDED.balance", (referrer_id, reward_amount))
            return referrer_id

    def payment_history(self, limit: int = 10, offset: int = 0):
        limit = max(1, min(int(limit), 20))
        offset = max(0, int(offset))
        with self.connect() as con, con.cursor() as cur:
            cur.execute("""SELECT p.id,p.user_id,p.memo,p.amount,p.status,p.created_at,p.paid_at,
                p.provider,p.provider_invoice_id,p.pay_url,u.username,u.first_name FROM payments p LEFT JOIN bot_users u ON u.user_id=p.user_id
                ORDER BY p.created_at DESC LIMIT %s OFFSET %s""", (limit, offset))
            return cur.fetchall()

    def payment_stats(self):
        with self.connect() as con, con.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='paid') AS paid,
                COUNT(*) FILTER (WHERE status='pending') AS pending,
                COALESCE(SUM(amount) FILTER (WHERE status='paid'),0) AS paid_amount,
                COUNT(*) FILTER (WHERE status='paid' AND paid_at::date=CURRENT_DATE) AS paid_today,
                COALESCE(SUM(amount) FILTER (WHERE status='paid' AND paid_at::date=CURRENT_DATE),0) AS amount_today
                FROM payments""")
            row = cur.fetchone()
            return {k: (float(v) if k in {'paid_amount','amount_today'} else int(v or 0)) for k,v in row.items()}
