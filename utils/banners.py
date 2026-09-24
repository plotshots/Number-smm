import html
from io import BytesIO

from telethon import events, types

from database import repository
from config import logger


BANNER_SECTIONS = {
    "home": "🏠 Home / Dashboard",
    "buy": "🛒 Buy Account",
    "deposit": "💳 Deposit",
    "profile": "👤 Profile",
    "orders": "📦 My Orders",
    "balance": "💰 Balance",
    "smm": "📱 Social Media Services",
    "support": "🆘 Support",
}


async def send_bannered_message(bot, event, key, caption, buttons=None, enabled_only=True):
    banner = repository.get_banner(key, enabled_only=enabled_only)
    if not banner:
        return False
    try:
        if banner.get("url") and key != "home":
            await bot.send_message(
                event.chat_id,
                f"{caption}\n<a href='{html.escape(banner['url'], quote=True)}'>&#8203;</a>",
                buttons=buttons, parse_mode="html",
                link_preview=True,
            )
            return True
        if not banner.get("file_id"):
            return False
        if key == "home":
            home_media = banner["file_id"]
            if banner.get("access_hash") and banner.get("file_reference"):
                home_media = types.InputPhoto(
                    id=int(banner["file_id"]),
                    access_hash=int(banner["access_hash"]),
                    file_reference=bytes.fromhex(banner["file_reference"]),
                )
            if isinstance(event, events.CallbackQuery.Event):
                await event.edit(caption, file=home_media, buttons=buttons, parse_mode="html")
            else:
                await bot.send_file(
                    event.chat_id, home_media, caption=caption, buttons=buttons,
                    parse_mode="html", force_document=False,
                )
            return True
        if isinstance(event, events.CallbackQuery.Event):
            try:
                await event.edit(caption, buttons=buttons, parse_mode="html")
            except Exception:
                return True
            return True

        content = repository.get_banner_content(key)
        if not content:
            return False

        image = BytesIO(content)
        image.name = banner.get("filename") or f"{key}.jpg"
        uploaded = await bot.upload_file(image)
        media = types.InputMediaUploadedPhoto(file=uploaded)
        await bot.send_file(
            event.chat_id, media, caption=caption, buttons=buttons,
            parse_mode="html", force_document=False,
        )
        return True
    except Exception as ex:
        logger.error(f"Banner send failed for {key}: {ex}", exc_info=True)
        return False