import os
import asyncio
import logging
from telethon import TelegramClient, functions, types
from config import BOT_TOKEN, API_ID, API_HASH, bot

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

os.makedirs("sessions", exist_ok=True)

# Import plugins AFTER bot is created so they can use it or register handlers
from plugins import register_all_handlers
from database import initialize_runtime

from utils.health import start_health_server
from utils.auto_upi_verifier import start_auto_upi_verifier

async def register_bot_commands():
    await bot(functions.bots.SetBotCommandsRequest(
        scope=types.BotCommandScopeDefault(),
        lang_code="",
        commands=[types.BotCommand(command="start", description="Start Bot")],
    ))

async def main():
    initialize_runtime()
    start_auto_upi_verifier(bot)
    try:
        await start_health_server()
    except Exception as e:
        logger.error(f"⚠️ Health server startup failed: {e}")

    while True:
        try:
            if not bot.is_connected():
                await bot.connect()
            await register_bot_commands()
            print("✅ Numbott Modular (Telethon) STARTED SUCCESSFULLY", flush=True)
            await bot.run_until_disconnected()
        except Exception as err:
            logger.error(f"⚠️ Numbott disconnected: {err}. Reconnecting in 5s...")
            await asyncio.sleep(5)

if __name__ == '__main__':
    bot.start(bot_token=BOT_TOKEN)
    register_all_handlers(bot)

    from telethon import events
    @bot.on(events.CallbackQuery)
    async def debug_cb(e):
        logger.warning(f"CALLBACK DATA: {e.data}")

    @bot.on(events.NewMessage)
    async def debug_msg(e):
        logger.info(f"📩 INCOMING MSG from {e.sender_id}: {e.text}")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
