"""MongoDB-only persistence facade for the Telegram bot."""

import os

from config import ADMIN_ID, SUPER_ADMINS, is_super_admin
from mongo_cursor import MongoCursor
from mongo_repository import MongoRepository
from utils.telegram_sessions import restore_all_sessions

repository = MongoRepository()
cur = MongoCursor(repository)


class MongoRuntime:
    repository = repository

    def execute(self, query, params=()):
        return cur.execute(query, params)

    def commit(self):
        return None

    def rollback(self):
        return None

    def upsert_stock_account(self, account):
        return self.repository.upsert_stock_account(account)


db = MongoRuntime()


def initialize_runtime():
    repository.ping()
    repository.ensure_indexes()
    restore_all_sessions(repository)
    for user_id in SUPER_ADMINS:
        if user_id:
            repository.db.admins.update_one(
                {"_id": int(user_id)},
                {"$set": {"user_id": int(user_id), "p_add_stock": 1,
                          "p_manage_stock": 1, "p_stats": 1, "p_bal": 1,
                          "p_settings": 1}},
                upsert=True,
            )


def _setting(key, default=None):
    row = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_setting(key, value):
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def is_bot_online(): return _setting("bot_status", "on") == "on"


def is_admin(uid):
    return is_super_admin(uid) or repository.db.admins.find_one({"_id": int(uid)}) is not None


def has_perm(uid, perm):
    if is_super_admin(uid): return True
    row = repository.db.admins.find_one({"_id": int(uid)}, {perm: 1})
    return bool(row and row.get(perm) == 1)


def ensure_user(uid, commit=True): repository.ensure_user(uid)


def get_usdt_rate():
    try: return float(_setting("usdt_rate", 94.0))
    except (TypeError, ValueError): return 94.0


def get_support_url():
    url = _setting("support_url") or "https://t.me/global_robots_chat"
    return url if url.startswith("http") else "https://" + url.replace("@", "t.me/")


def to_usd(inr): return round(inr / get_usdt_rate(), 2)


def is_user_banned(uid):
    user = repository.get_user(uid)
    return bool(user and user.get("banned") == 1)


def update_balance(uid, amount): repository.update_balance(uid, amount)
def approve_deposit(deposit_id, amount): return repository.approve_deposit(deposit_id, amount)


COUNTRY_CODES = {
    "1": ("USA/Canada", "🇺🇸"), "7": ("Russia", "🇷🇺"), "20": ("Egypt", "🇪🇬"),
    "27": ("South Africa", "🇿🇦"), "31": ("Netherlands", "🇳🇱"), "32": ("Belgium", "🇧🇪"),
    "33": ("France", "🇫🇷"), "34": ("Spain", "🇪🇸"), "39": ("Italy", "🇮🇹"),
    "44": ("UK", "🇬🇧"), "46": ("Sweden", "🇸🇪"), "48": ("Poland", "🇵🇱"),
    "49": ("Germany", "🇩🇪"), "51": ("Peru", "🇵🇪"), "52": ("Mexico", "🇲🇽"),
    "54": ("Argentina", "🇦🇷"), "55": ("Brazil", "🇧🇷"), "56": ("Chile", "🇨🇱"),
    "57": ("Colombia", "🇨🇴"), "58": ("Venezuela", "🇻🇪"), "60": ("Malaysia", "🇲🇾"),
    "61": ("Australia", "🇦🇺"), "62": ("Indonesia", "🇮🇩"), "63": ("Philippines", "🇵🇭"),
    "66": ("Thailand", "🇹🇭"), "84": ("Vietnam", "🇻🇳"), "86": ("China", "🇨🇳"),
    "90": ("Turkey", "🇹🇷"), "91": ("India", "🇮🇳"), "92": ("Pakistan", "🇵🇰"),
    "93": ("Afghanistan", "🇦🇫"), "94": ("Sri Lanka", "🇱🇰"), "95": ("Myanmar", "🇲🇲"),
    "98": ("Iran", "🇮🇷"), "212": ("Morocco", "🇲🇦"), "213": ("Algeria", "🇩🇿"),
    "234": ("Nigeria", "🇳🇬"), "254": ("Kenya", "🇰🇪"), "255": ("Tanzania", "🇹🇿"),
    "380": ("Ukraine", "🇺🇦"), "880": ("Bangladesh", "🇧🇩"), "964": ("Iraq", "🇮🇶"),
    "966": ("Saudi Arabia", "🇸🇦"), "971": ("UAE", "🇦🇪"), "998": ("Uzbekistan", "🇺🇿"),
}


def get_flag_by_country_name(name):
    for country, flag in COUNTRY_CODES.values():
        if country == name: return flag
    row = repository.db.custom_countries.find_one({"name": name})
    return row.get("flag", "🌍") if row else "🌍"


def get_country_info(phone):
    phone = str(phone).replace(" ", "").replace("+", "")
    customs = sorted(repository.db.custom_countries.find({}), key=lambda x: len(str(x.get("code", ""))), reverse=True)
    for row in customs:
        if phone.startswith(str(row.get("code", ""))): return row.get("name"), row.get("flag", "🌍")
    for length in (3, 2, 1):
        if phone[:length] in COUNTRY_CODES: return COUNTRY_CODES[phone[:length]]
    return "Unknown", "🌍"


def get_bot_mode():
    mode = _setting("bot_mode")
    return mode if mode in ("manual", "panel", "hybrid") else os.getenv("BOT_MODE", "manual").lower()


def set_bot_mode(mode): _set_setting("bot_mode", mode)
def get_more_account_filters_enabled(): return _setting("more_account_filters", "1") != "0"
def set_more_account_filters_enabled(enabled): _set_setting("more_account_filters", "1" if enabled else "0")
def get_lzt_key(): return str(_setting("lzt_api_key") or os.getenv("LZT_API_KEY", "")).strip()
def set_lzt_key(key): _set_setting("lzt_api_key", key.strip())


def get_rub_rate():
    try: return float(_setting("rub_rate") or os.getenv("RUB_RATE", "1.15"))
    except (TypeError, ValueError): return 1.15


def set_rub_rate(rate): _set_setting("rub_rate", str(rate))


def get_lzt_margin():
    try: return float(_setting("lzt_margin") or os.getenv("LZT_MARGIN", "25.0"))
    except (TypeError, ValueError): return 25.0


def set_lzt_margin(margin): _set_setting("lzt_margin", str(margin))


def adjust_price_by_mode(base, mode):
    if mode == "spam": return max(int(base * 0.55), 15)
    if mode == "nonspam": return max(int(round(base * 1.25)), base + 8)
    if mode == "premium": return base + 150
    return base


def get_panel_price(country, year, lzt_price_rub=0, mode="bulk"):
    additions = {2026: 0, 2025: 25, 2024: 55, 2023: 85, 2022: 125, 2021: 175, 2020: 230, 2019: 290, 2018: 350, 2017: 420}
    try: year = int(year)
    except (TypeError, ValueError): year = 2026
    extra = additions.get(year, 0 if year >= 2026 else (2026 - year) * 60)
    if mode == "nonspam":
        row = repository.db.spamfree_prices.find_one({"_id": country})
        if row and row.get("price", 0) > 0: return int(row["price"]) + extra
    row = repository.db.auto_prices.find_one({"country": country, "year": str(year)})
    if mode == "spam" and not row:
        row = repository.db.auto_prices.find_one({"country": country, "year": {"$in": ["Common", "ALL"]}})
    if row and row.get("price", 0) > 0:
        value = int(row["price"]) + extra
        return max(int(value * 0.55), 15) if mode == "spam" else adjust_price_by_mode(value, mode)
    row = repository.db.auto_prices.find_one({"country": country, "year": {"$in": ["Common", "ALL"]}})
    if row and row.get("price", 0) > 0: return adjust_price_by_mode(int(row["price"]) + extra, mode)
    return adjust_price_by_mode(max(round(lzt_price_rub * get_rub_rate() + get_lzt_margin()), 25), mode)


def get_change_number_fee():
    try: return int(_setting("change_number_fee", 10))
    except (TypeError, ValueError): return 10


def set_change_number_fee(fee): _set_setting("change_number_fee", str(fee))
def get_fsub_status(): return str(_setting("fsub_status", "on")).lower()
def set_fsub_status(status): _set_setting("fsub_status", str(status).lower())


def get_fsub_channels():
    value = _setting("fsub_channels")
    return [x.strip() for x in value.split(",") if x.strip()] if value is not None else [x.strip() for x in os.getenv("CHECK_CHANNELS", "").split(",") if x.strip()]


def get_fsub_urls():
    from utils.helpers import format_join_url
    value = _setting("fsub_urls")
    raw = value.split(",") if value is not None else os.getenv("JOIN_URLS", "").split(",")
    return [format_join_url(x) for x in raw if x.strip()]


def set_fsub_data(channels, urls):
    from utils.helpers import format_join_url
    _set_setting("fsub_channels", ",".join(str(x).strip() for x in channels if str(x).strip()))
    _set_setting("fsub_urls", ",".join(format_join_url(x) for x in urls if format_join_url(x)))


def add_fsub_channel(channel_id, join_url):
    channels, urls = get_fsub_channels(), get_fsub_urls()
    item = str(channel_id).strip()
    if item in channels and channels.index(item) < len(urls): urls[channels.index(item)] = join_url
    elif item not in channels: channels.append(item); urls.append(join_url)
    set_fsub_data(channels, urls)


def remove_fsub_channel(index):
    channels, urls = get_fsub_channels(), get_fsub_urls()
    if 0 <= index < len(channels):
        channels.pop(index)
        if index < len(urls): urls.pop(index)
        set_fsub_data(channels, urls)


def get_log_channels_db():
    value = _setting("log_channels")
    if value is None:
        from config import LOG_CHANNELS
        return LOG_CHANNELS
    return [int(x) if x.strip().lstrip("-").isdigit() else x.strip() for x in value.split(",") if x.strip()]


def set_log_channels_db(channels): _set_setting("log_channels", ",".join(str(x) for x in channels))
def add_log_channel_db(channel_id):
    channels = get_log_channels_db()
    if str(channel_id) not in [str(x) for x in channels]: set_log_channels_db(channels + [channel_id])
def remove_log_channel_db(channel_id): set_log_channels_db([x for x in get_log_channels_db() if str(x) != str(channel_id)])
def get_start_image_url(): return str(_setting("start_image") or "https://ibb.co/BVPMtFyk").strip()
def set_start_image_url(url): _set_setting("start_image", url.strip())


def _catalog(collection, defaults):
    rows = list(repository.db[collection].find({"available": 1}))
    if not rows:
        for values in defaults:
            identifier = repository.next_id(collection)
            content_field = "file_content" if collection == "source_codes" else "panel_content"
            repository.db[collection].insert_one({"_id": identifier, "id": identifier, "title": values[0], "description": values[1], "price": values[2], content_field: values[3], "available": 1})
        rows = list(repository.db[collection].find({"available": 1}))
    return rows


def get_source_codes():
    rows = _catalog("source_codes", [("Main Store Bot (Full Modular Code)", "Complete full modular bot source code with OTP buying, SMM services, panels & instant delivery.", 500.0, "https://github.com/SUDEEPBOTS/Numbott"), ("OTP & SMM Bot Source Code", "Complete Python Telethon based automated OTP & SMM bot source code.", 299.0, "https://github.com/SUDEEPBOTS/Numbott")])
    return [(r["id"], r["title"], r["description"], r["price"], r["file_content"], r["available"]) for r in rows]


def get_panels():
    rows = _catalog("panels", [("VIP SMM Panel (Server 1)", "High-speed VIP SMM panel with instant order delivery and auto balance top-up.", 399.0, "https://fathersmm.com"), ("Budget SMM Panel (Server 2 - Cheap)", "Cheapest global SMM reseller panel.", 399.0, "https://best-smm.com")])
    return [(r["id"], r["title"], r["description"], r["price"], r["panel_content"], r["available"]) for r in rows]


def is_payment_redeemed_db(email_msg_id=None, utr=None, txn_id=None):
    if email_msg_id and repository.db.redeemed_transactions.find_one({"_id": str(email_msg_id).strip()}): return True
    for field, value in (("utr", utr), ("txn_id", txn_id)):
        if value and (repository.db.redeemed_transactions.find_one({field: str(value).strip()}) or repository.db.deposits.find_one({"utr": str(value).strip(), "status": "approved"})): return True
    return False


def record_redeemed_payment_db(email_msg_id, utr, txn_id, amount, user_id):
    document = {"_id": email_msg_id or "", "email_msg_id": email_msg_id or "", "utr": utr or "", "txn_id": txn_id or "", "amount": amount, "user_id": user_id}
    repository.db.redeemed_transactions.replace_one({"_id": document["_id"]}, document, upsert=True)
