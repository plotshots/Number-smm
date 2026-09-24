import os
import asyncio
from unittest.mock import patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("MONGODB_URI", "mongomock://banner-tests")

import mongomock

from mongo_repository import MongoRepository
from utils.banners import BANNER_SECTIONS, send_bannered_message
from plugins import buy


def test_all_banner_sections_are_independent_and_persistent():
    client = mongomock.MongoClient()
    repository = MongoRepository(client=client, database_name="banner-tests")
    repository.ensure_indexes()

    for key in BANNER_SECTIONS:
        saved = repository.save_banner(key, f"{key}-image".encode(), f"telegram-{key}")
        assert saved["key"] == key
        assert saved["enabled"] is False
        assert saved["file_id"] == f"telegram-{key}"
        assert saved["created_at"]
        assert saved["updated_at"]
        assert repository.get_banner_content(key) is None
        assert repository.get_banner_content(key, enabled_only=False) == f"{key}-image".encode()

    repository.set_banner_enabled("buy", True)
    repository.set_banner_enabled("support", True)
    assert repository.get_banner_content("buy") == b"buy-image"
    assert repository.get_banner_content("support") == b"support-image"
    assert repository.get_banner_content("deposit") is None

    repository.save_banner("buy", b"replacement", "telegram-buy-new")
    assert repository.get_banner("buy")["enabled"] is True
    assert repository.get_banner("buy")["file_id"] == "telegram-buy-new"
    assert repository.get_banner_content("buy") == b"replacement"

    restarted = MongoRepository(client=client, database_name="banner-tests")
    assert restarted.get_banner("buy")["enabled"] is True
    assert restarted.get_banner_content("buy") == b"replacement"
    assert restarted.get_banner("deposit")["enabled"] is False
    assert restarted.get_banner_content("missing") is None


def test_home_file_id_only_banner_survives_restart_without_document_storage():
    client = mongomock.MongoClient()
    repository = MongoRepository(client=client, database_name="banner-file-id-tests")
    repository.ensure_indexes()

    saved = repository.save_banner_file_id("home", 123456789)
    assert saved["file_id"] == "123456789"
    assert "gridfs_id" not in saved
    assert repository.get_banner_content("home", enabled_only=False) is None

    restarted = MongoRepository(client=client, database_name="banner-file-id-tests")
    assert restarted.get_banner("home")["file_id"] == "123456789"


def test_buy_account_uses_one_uploaded_photo_message_with_caption_and_buttons():
    class BannerRepository:
        def get_banner(self, key, enabled_only=False):
            return {"key": key, "file_id": "telegram-id", "filename": "buy.png"}

        def get_banner_content(self, key, enabled_only=True):
            return b"image-bytes"

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def upload_file(self, image):
            assert image.name == "buy.png"
            return b"uploaded-photo"

        async def send_file(self, chat_id, media, caption=None, buttons=None, parse_mode=None, force_document=None):
            self.sent.append((chat_id, media, caption, buttons, force_document))

    class FakeEvent:
        chat_id = 100

        async def respond(self, *args, **kwargs):
            raise AssertionError("existing text path must not run when banner is enabled")

        async def edit(self, *args, **kwargs):
            raise AssertionError("existing text path must not run when banner is enabled")

    bot = FakeBot()
    event = FakeEvent()
    with patch("utils.banners.repository", BannerRepository()), patch.object(buy, "bot", bot):
        asyncio.run(buy.show_buy_menu(event))

    assert len(bot.sent) == 1
    chat_id, media, caption, buttons, force_document = bot.sent[0]
    assert chat_id == 100
    assert media.__class__.__name__ == "InputMediaUploadedPhoto"
    assert "𝐒ᴇʟᴇᴄᴛ 𝐀ᴄᴄᴏᴜɴᴛ 𝐂ᴀᴛᴇɢᴏʀʏ" in caption
    assert buttons
    assert force_document is False


def test_home_banner_sends_stored_telegram_file_id_directly():
    class BannerRepository:
        def get_banner(self, key, enabled_only=False):
            return {"key": key, "enabled": True, "file_id": "telegram-home-id"}

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_file(self, chat_id, file_id, **kwargs):
            self.sent.append((chat_id, file_id, kwargs))

    class FakeEvent:
        chat_id = 100

    from utils.banners import send_bannered_message

    bot = FakeBot()
    with patch("utils.banners.repository", BannerRepository()):
        assert asyncio.run(send_bannered_message(bot, FakeEvent(), "home", "Dashboard"))

    assert bot.sent[0][0:2] == (100, "telegram-home-id")
    assert bot.sent[0][2]["force_document"] is False
