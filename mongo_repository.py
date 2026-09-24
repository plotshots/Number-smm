"""MongoDB persistence primitives used by the bot runtime."""

import os
import re
import hashlib
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
import gridfs


class _BannerFileStore:
    def __init__(self, database):
        try:
            self.store = gridfs.GridFS(database, collection="banner_files")
            self.fallback = None
        except TypeError:
            self.store = None
            self.fallback = database.banner_files

    def put(self, content, filename, content_type):
        if self.store:
            return self.store.put(content, filename=filename, content_type=content_type)
        identifier = uuid.uuid4().hex
        self.fallback.insert_one({"_id": identifier, "data": content, "filename": filename, "content_type": content_type})
        return identifier

    def get(self, identifier):
        if self.store:
            return self.store.get(identifier)
        row = self.fallback.find_one({"_id": identifier})
        if not row:
            raise KeyError(identifier)
        return type("StoredFile", (), {"read": lambda self: row["data"]})()

    def delete(self, identifier):
        if self.store:
            return self.store.delete(identifier)
        self.fallback.delete_one({"_id": identifier})


COLLECTIONS = (
    "users", "settings", "stock", "auto_prices", "spamfree_prices", "deposits",
    "upi_orders", "orders", "custom_payments", "admins", "custom_countries",
    "smm_orders", "source_codes", "panels", "redeemed_transactions", "telegram_sessions", "banners",
)


class MongoRepository:
    """Small repository for Mongo-native operations used by the bot runtime."""

    def __init__(self, uri=None, database_name=None, client=None):
        self.uri = uri or os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
        self.database_name = database_name or os.getenv("MONGODB_DB_NAME", "numbott")
        if client is None:
            if not self.uri:
                raise RuntimeError("MONGODB_URI is required")
            if self.uri.startswith("mongomock://"):
                try:
                    import mongomock
                except ImportError as exc:
                    raise RuntimeError("mongomock is required for mongomock:// test URIs") from exc
                client = mongomock.MongoClient()
            else:
                client = MongoClient(self.uri, serverSelectionTimeoutMS=10000)
        self.client = client
        self.db = client[self.database_name]
        self.banner_files = _BannerFileStore(self.db)

    def ping(self):
        return self.client.admin.command("ping")

    def ensure_indexes(self):
        indexes = {
            "users": [("referred_by", ASCENDING)],
            "stock": [("available", ASCENDING), ("country_name", ASCENDING),
                      ("account_year", DESCENDING), ("category", ASCENDING),
                      ("twofa", ASCENDING)],
            "deposits": [("user_id", ASCENDING), ("status", ASCENDING), ("utr", ASCENDING)],
            "orders": [("user_id", ASCENDING), ("date", DESCENDING)],
            "upi_orders": [("user_id", ASCENDING), ("status", ASCENDING)],
            "smm_orders": [("user_id", ASCENDING), ("status", ASCENDING)],
            "redeemed_transactions": [("utr", ASCENDING), ("txn_id", ASCENDING)],
            "auto_prices": [("country", ASCENDING), ("year", ASCENDING)],
            "spamfree_prices": [("country", ASCENDING)],
            "custom_countries": [("name", ASCENDING)],
            "custom_payments": [("name", ASCENDING)],
        }
        for collection, fields in indexes.items():
            for field, direction in fields:
                self.db[collection].create_index([(field, direction)])
        self.db.deposits.create_index([("source_key", ASCENDING)], unique=True, sparse=True)
        self.db.telegram_sessions.create_index([("account_key", ASCENDING)], unique=True, sparse=True)
        self.db.banners.create_index([("key", ASCENDING)], unique=True)

    def get_banner(self, key, enabled_only=False):
        query = {"key": str(key)}
        if enabled_only:
            query["enabled"] = True
        return self.db.banners.find_one(query)

    def save_banner(
        self, key, content, file_id, filename=None, content_type=None,
        access_hash=None, file_reference=None,
    ):
        now = self._now()
        existing = self.get_banner(key)
        filename = filename or f"{key}.jpg"
        content_type = content_type or "image/jpeg"
        gridfs_id = self.banner_files.put(content, filename=filename, content_type=content_type)
        document = {
            "key": str(key), "enabled": bool(existing.get("enabled", False)) if existing else False,
            "file_id": str(file_id), "gridfs_id": gridfs_id,
            "filename": filename, "content_type": content_type,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        if access_hash is not None:
            document["access_hash"] = str(access_hash)
        if file_reference is not None:
            document["file_reference"] = bytes(file_reference).hex()
        self.db.banners.replace_one({"key": str(key)}, document, upsert=True)
        if existing and existing.get("gridfs_id"):
            try:
                self.banner_files.delete(existing["gridfs_id"])
            except Exception:
                pass
        return self.get_banner(key)

    def save_banner_file_id(self, key, file_id):
        now = self._now()
        existing = self.get_banner(key)
        document = {
            "key": str(key),
            "enabled": bool(existing.get("enabled", False)) if existing else False,
            "file_id": str(file_id),
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        self.db.banners.replace_one({"key": str(key)}, document, upsert=True)
        return self.get_banner(key)

    def save_banner_url(self, key, url):
        now = self._now()
        existing = self.get_banner(key)
        document = {
            "key": str(key),
            "enabled": bool(existing.get("enabled", False)) if existing else False,
            "url": str(url).strip(),
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        self.db.banners.replace_one({"key": str(key)}, document, upsert=True)
        return self.get_banner(key)

    def set_banner_enabled(self, key, enabled):
        return self.db.banners.find_one_and_update(
            {"key": str(key)}, {"$set": {"enabled": bool(enabled), "updated_at": self._now()}},
            return_document=ReturnDocument.AFTER,
        )

    def get_banner_content(self, key, enabled_only=True):
        banner = self.get_banner(key, enabled_only=enabled_only)
        if not banner or not banner.get("gridfs_id"):
            return None
        try:
            return self.banner_files.get(banner["gridfs_id"]).read()
        except Exception:
            return None

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def ensure_user(self, user_id):
        self.db.users.update_one(
            {"_id": int(user_id)},
            {"$setOnInsert": {"user_id": int(user_id), "balance": 0,
                              "total_deposited": 0, "banned": 0, "discount": 0,
                              "terms_accepted": 0, "joined_date": self._now()}},
            upsert=True,
        )

    def get_user(self, user_id):
        return self.db.users.find_one({"_id": int(user_id)})

    def get_orders_for_user(self, user_id, limit=10):
        normalized_user_id = int(user_id)
        return list(
            self.db.orders.find(
                {"user_id": {"$in": [normalized_user_id, str(normalized_user_id)]}}
            ).sort([("id", DESCENDING), ("date", DESCENDING)]).limit(limit)
        )

    def update_balance(self, user_id, amount):
        result = self.db.users.find_one_and_update(
            {"_id": int(user_id)}, {"$inc": {"balance": amount}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise ValueError(f"user {user_id} does not exist")
        return result

    def debit_balance(self, user_id, amount):
        return self.db.users.find_one_and_update(
            {"_id": int(user_id), "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}},
            return_document=ReturnDocument.AFTER,
        )

    def upsert_stock_account(self, account):
        """Persist one manual account directly in the Mongo stock collection."""
        document = dict(account)
        document["_id"] = document["phone"]
        existing = self.db.stock.find_one({"_id": document["_id"]}, {"session_id": 1})
        document.setdefault("session_id", (existing or {}).get("session_id") or self.session_id_for_account(document["phone"]))
        document.setdefault("added_date", self._now())
        self.db.stock.replace_one({"_id": document["_id"]}, document, upsert=True)
        return document

    @staticmethod
    def session_id_for_account(account_key):
        """Return a stable, non-sensitive identifier for one Telegram account."""
        normalized = str(account_key).strip().lstrip("+")
        return "tg-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get_telegram_session(self, session_id):
        return self.db.telegram_sessions.find_one({"_id": str(session_id)})

    @contextmanager
    def _telegram_session_lock(self, session_id, timeout=30):
        """Serialize writes across processes using a Mongo lease document."""
        session_id = str(session_id)
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        while True:
            now = self._now()
            try:
                lock = self.db.telegram_session_locks.find_one_and_update(
                    {"_id": session_id, "$or": [
                        {"lock_until": {"$lte": now}}, {"lock_until": {"$exists": False}},
                        {"lock_owner": owner},
                    ]},
                    {"$set": {"lock_owner": owner, "lock_until": datetime.fromtimestamp(now.timestamp() + 60, timezone.utc)}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                lock = None
            if lock and lock.get("lock_owner") == owner:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out acquiring Telegram session lock")
            time.sleep(0.05)
        try:
            yield
        finally:
            self.db.telegram_session_locks.delete_one({"_id": session_id, "lock_owner": owner})

    def persist_telegram_session(self, session_id, source_path, account_key=None):
        """Atomically upsert a Telethon session and its runtime sidecar files."""
        session_id = str(session_id)
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            raise FileNotFoundError(source_path)
        with open(source_path, "rb") as source:
            stored_files = {"session": source.read()}
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = source_path + suffix
            if os.path.isfile(sidecar):
                stored_files[suffix[1:]] = open(sidecar, "rb").read()
        document = {
            "_id": session_id,
            "files": stored_files,
            "updated_at": self._now(),
            "format": "telethon-sqlite",
        }
        if account_key is not None:
            document["account_key"] = str(account_key)
        with self._telegram_session_lock(session_id):
            self.db.telegram_sessions.replace_one({"_id": session_id}, document, upsert=True)
        return document

    def restore_telegram_sessions(self):
        """Return persisted session records for startup materialization."""
        return list(self.db.telegram_sessions.find({}, {"_id": 1, "files": 1, "account_key": 1}))

    def claim_stock_account(self, mode="bulk", country=None, year=None):
        query = {"available": 1}
        if country is not None:
            query["country_name"] = country
        if year is not None:
            query["account_year"] = int(year)
        if mode == "aged":
            query["account_year"] = {"$ne": None}
        elif mode == "nonspam":
            query["category"] = {"$exists": True, "$not": re.compile("^spam$", re.IGNORECASE)}
        elif mode == "spam":
            query["category"] = re.compile("^spam$", re.IGNORECASE)
        elif mode == "no_2fa":
            query["$or"] = [{"twofa": None}, {"twofa": ""}, {"twofa": re.compile("^none$", re.IGNORECASE)}]
        elif mode == "with_2fa":
            query["twofa"] = {"$nin": [None, "", "None", "none"]}
        elif mode != "bulk":
            return None
        account = self.db.stock.find_one_and_update(
            query, {"$set": {"available": 0}},
            sort=[("added_date", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.BEFORE,
        )
        return account

    def release_stock_account(self, account):
        """Return a claimed account to available stock without changing its mapping."""
        if not account or "_id" not in account:
            return False
        result = self.db.stock.update_one(
            {"_id": account["_id"], "available": 0},
            {"$set": {"available": 1}},
        )
        return result.modified_count == 1

    @contextmanager
    def transaction(self):
        """Require a deployment that supports transactions for money operations."""
        with self.client.start_session() as session:
            with session.start_transaction():
                yield session

    def approve_deposit(self, deposit_id, amount):
        try:
            with self.transaction() as session:
                return self._approve_deposit(deposit_id, amount, session=session)
        except NotImplementedError as exc:
            if "sessions" not in str(exc).lower():
                raise
            return self._approve_deposit_without_session(deposit_id, amount)

    def _approve_deposit(self, deposit_id, amount, session=None):
        find_kwargs = {"session": session} if session is not None else {}
        deposit_query = {"_id": int(deposit_id), "status": "pending"}
        deposit = self.db.deposits.find_one(deposit_query, **find_kwargs)
        if deposit is None:
            existing = self.db.deposits.find_one({"_id": int(deposit_id)}, **find_kwargs)
            if existing:
                return {"approved": False, "already_processed": True,
                        "user_id": existing.get("user_id"), "amount": existing.get("amount")}
            raise ValueError(f"deposit {deposit_id} does not exist")

        user_id = deposit.get("user_id")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError(f"invalid user ID {user_id!r}")
        amount = int(amount)
        if amount <= 0:
            raise ValueError(f"invalid deposit amount {amount!r}")
        user = self.db.users.find_one_and_update(
            {"_id": user_id}, {"$inc": {"balance": amount, "total_deposited": amount}},
            session=session, return_document=ReturnDocument.AFTER,
        )
        if user is None:
            raise ValueError(f"user {user_id} does not exist")
        status_result = self.db.deposits.update_one(
            deposit_query, {"$set": {"status": "approved", "amount": amount}}, **find_kwargs,
        )
        if status_result.matched_count != 1:
            raise RuntimeError(f"deposit {deposit_id} status update was not applied")
        return {"approved": True, "already_processed": False, "user_id": user_id,
                "previous_balance": user["balance"] - amount,
                "balance": user["balance"], "amount": amount, "status": "approved"}

    def _approve_deposit_without_session(self, deposit_id, amount):
        """Support clients without sessions while keeping the production path transactional."""
        deposit = self.db.deposits.find_one_and_update(
            {"_id": int(deposit_id), "status": "pending"},
            {"$set": {"status": "processing"}},
            return_document=ReturnDocument.BEFORE,
        )
        if deposit is None:
            existing = self.db.deposits.find_one({"_id": int(deposit_id)})
            if existing:
                return {"approved": False, "already_processed": True,
                        "user_id": existing.get("user_id"), "amount": existing.get("amount")}
            raise ValueError(f"deposit {deposit_id} does not exist")
        user_id = deposit.get("user_id")
        amount = int(amount)
        try:
            if not isinstance(user_id, int) or user_id <= 0:
                raise ValueError(f"invalid user ID {user_id!r}")
            if amount <= 0:
                raise ValueError(f"invalid deposit amount {amount!r}")
            user = self.db.users.find_one_and_update(
                {"_id": user_id}, {"$inc": {"balance": amount, "total_deposited": amount}},
                return_document=ReturnDocument.AFTER,
            )
            if user is None:
                raise ValueError(f"user {user_id} does not exist")
            status_result = self.db.deposits.update_one(
                {"_id": int(deposit_id), "status": "processing"},
                {"$set": {"status": "approved", "amount": amount}},
            )
            if status_result.matched_count != 1:
                raise RuntimeError(f"deposit {deposit_id} status update was not applied")
            return {"approved": True, "already_processed": False, "user_id": user_id,
                    "previous_balance": user["balance"] - amount,
                    "balance": user["balance"], "amount": amount, "status": "approved"}
        except Exception:
            self.db.deposits.update_one(
                {"_id": int(deposit_id), "status": "processing"},
                {"$set": {"status": "pending"}},
            )
            raise

    def get_deposit(self, deposit_id):
        """Find a deposit by its integer callback ID."""
        try:
            normalized_id = int(deposit_id)
        except (TypeError, ValueError):
            return None
        return self.db.deposits.find_one({"_id": normalized_id})

    def reject_deposit(self, deposit_id):
        with self.transaction() as session:
            deposit = self.db.deposits.find_one_and_update(
                {"_id": int(deposit_id), "status": "pending"},
                {"$set": {"status": "rejected"}},
                session=session, return_document=ReturnDocument.BEFORE,
            )
            if deposit is None:
                existing = self.db.deposits.find_one({"_id": int(deposit_id)}, session=session)
                if existing:
                    return {"rejected": False, "already_processed": True,
                            "user_id": existing.get("user_id"), "amount": existing.get("amount")}
                raise ValueError(f"deposit {deposit_id} does not exist")
            return {"rejected": True, "already_processed": False,
                    "user_id": deposit.get("user_id"), "amount": deposit.get("amount"),
                    "status": "rejected"}

    def create_manual_deposit(self, user_id, amount, method, screenshot_file_id,
                              source_chat_id, source_message_id):
        """Create one pending manual deposit, deduplicated by Telegram update."""
        source_key = f"{int(source_chat_id)}:{int(source_message_id)}"
        existing = self.db.deposits.find_one({"source_key": source_key})
        if existing is not None:
            return existing, False

        document = {
            "_id": self.next_id("deposits"),
            "id": None,
            "user_id": int(user_id),
            "amount": int(amount),
            "method_name": method,
            "payment_method": method,
            "screenshot_file_id": screenshot_file_id,
            "source_chat_id": int(source_chat_id),
            "source_message_id": int(source_message_id),
            "source_key": source_key,
            "status": "pending",
            "created_at": self._now(),
            "date": self._now(),
        }
        document["id"] = document["_id"]
        try:
            self.db.deposits.insert_one(document)
        except DuplicateKeyError:
            return self.db.deposits.find_one({"source_key": source_key}), False
        return document, True

    def set_setting(self, key, value):
        self.db.settings.update_one({"_id": key}, {"$set": {"key": key, "value": value}}, upsert=True)

    def get_setting(self, key, default=None):
        row = self.db.settings.find_one({"_id": key})
        return row.get("value", default) if row else default

    def next_id(self, collection):
        result = self.db["_sequences"].find_one_and_update(
            {"_id": collection}, {"$inc": {"value": 1}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        return result["value"]