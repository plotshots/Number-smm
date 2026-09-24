from telethon import events, Button

from database import get_start_image_url, is_admin, has_perm, repository
from utils.banners import BANNER_SECTIONS
from utils.banners import send_bannered_message
from utils.keyboards import style_btn
from utils.states import admin_state


def _allowed(uid):
    return is_admin(uid) and has_perm(uid, "p_settings")


def _banner_for_menu(key):
    banner = repository.get_banner(key)
    if banner or key != "home":
        return banner
    return {"enabled": bool(get_start_image_url()), "url": get_start_image_url()}


async def banner_menu(event):
    if not _allowed(event.sender_id):
        return await event.answer("Access denied.", alert=True)
    buttons = []
    for key, label in BANNER_SECTIONS.items():
        banner = _banner_for_menu(key)
        status = "🟢 ON" if banner and banner.get("enabled") else "🔴 OFF"
        buttons.append([style_btn(f"{label}: {status}", f"banner_section|{key}", "primary")])
    buttons.append([style_btn("🔙 Back to Admin", "adm_adminmain", "danger")])
    await event.edit("<b>🖼️ Banner Management</b>\n\nSelect a section:", buttons=buttons)


async def banner_section(event, key):
    if not _allowed(event.sender_id) or key not in BANNER_SECTIONS:
        return await event.answer("Access denied.", alert=True)
    banner = _banner_for_menu(key)
    enabled = bool(banner and banner.get("enabled"))
    buttons = [
        [style_btn("🔴 Turn OFF" if enabled else "🟢 Turn ON", f"banner_toggle|{key}", "danger" if enabled else "success")],
        [style_btn("🖼️ Change Banner", f"banner_change|{key}", "primary"), style_btn("👁️ Preview", f"banner_preview|{key}", "primary")],
        [style_btn("🔙 All Sections", "banner_manage", "danger")],
    ]
    await event.edit(f"<b>{BANNER_SECTIONS[key]}</b>\nStatus: {'🟢 ON' if enabled else '🔴 OFF'}", buttons=buttons)


def register_banner_management(bot):
    @bot.on(events.CallbackQuery(pattern=b"^banner_manage$"))
    async def cb_banner_manage(e):
        await banner_menu(e)

    @bot.on(events.CallbackQuery(pattern=r"^banner_section\|([^|]+)$"))
    async def cb_banner_section(e):
        await banner_section(e, e.pattern_match.group(1).decode())

    @bot.on(events.CallbackQuery(pattern=r"^banner_toggle\|([^|]+)$"))
    async def cb_banner_toggle(e):
        key = e.pattern_match.group(1).decode()
        if not _allowed(e.sender_id) or key not in BANNER_SECTIONS:
            return await e.answer("Access denied.", alert=True)
        banner = repository.get_banner(key)
        if not banner:
            if key == "home":
                repository.save_banner_url(key, get_start_image_url())
                repository.set_banner_enabled(key, False)
                return await banner_section(e, key)
            return await e.answer("Upload a banner first.", alert=True)
        repository.set_banner_enabled(key, not bool(banner.get("enabled")))
        await banner_section(e, key)

    @bot.on(events.CallbackQuery(pattern=r"^banner_change\|([^|]+)$"))
    async def cb_banner_change(e):
        key = e.pattern_match.group(1).decode()
        if not _allowed(e.sender_id) or key not in BANNER_SECTIONS:
            return await e.answer("Access denied.", alert=True)
        admin_state[e.sender_id] = {"action": "banner_upload", "key": key}
        prompt = "Send the new Home/Dashboard banner image." if key == "home" else "Send the Telegram photo"
        prompt = prompt if key == "home" else f"{prompt} for <b>{BANNER_SECTIONS[key]}</b>."
        await e.edit(prompt, buttons=[[Button.inline("Cancel", f"banner_section|{key}")]])

    @bot.on(events.CallbackQuery(pattern=r"^banner_preview\|([^|]+)$"))
    async def cb_banner_preview(e):
        key = e.pattern_match.group(1).decode()
        if not _allowed(e.sender_id) or key not in BANNER_SECTIONS:
            return await e.answer("Access denied.", alert=True)
        banner = _banner_for_menu(key)
        if not banner or not (banner.get("file_id") or banner.get("url")):
            return await e.answer("No banner uploaded.", alert=True)
        if key == "home" and not repository.get_banner(key) and banner.get("url"):
            repository.save_banner_url(key, banner["url"])
        if not await send_bannered_message(
            bot, e, key, BANNER_SECTIONS[key], enabled_only=False,
        ):
            return await e.answer("Preview could not be sent.", alert=True)
        await e.answer("Preview sent.")

    @bot.on(events.NewMessage(func=lambda e: e.is_private and isinstance(admin_state.get(e.sender_id), dict) and admin_state[e.sender_id].get("action") == "banner_upload"))
    async def msg_banner_upload(e):
        if not _allowed(e.sender_id) or not e.photo:
            return
        state = admin_state.pop(e.sender_id)
        try:
            if state["key"] == "home":
                file_id = getattr(e.media.photo, "id", None)
                if not file_id:
                    raise ValueError("Telegram photo has no file id")
                repository.save_banner_file_id(state["key"], file_id)
                return await e.reply("✅ Home banner saved. It is OFF until you turn it ON.")
            content = await e.download_media(file=bytes)
            filename = getattr(e.file, "name", None) or f"{state['key']}.jpg"
            content_type = getattr(e.file, "mime_type", None) or "image/jpeg"
            saved = repository.save_banner(
                state["key"], content, str(e.media.photo.id),
                filename=filename, content_type=content_type,
            )
            await e.reply("✅ Banner uploaded. It is OFF until you turn it ON.")
        except Exception:
            await e.reply("❌ Banner upload failed.")