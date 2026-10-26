import os
import sys
import time
import asyncio
import logging
from datetime import datetime, timezone
from aiohttp import web, ClientSession, ClientTimeout
from dotenv import load_dotenv

# Setup base directory & env loading
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [RenderSupervisor]: %(message)s",
)
logger = logging.getLogger("RenderSupervisor")

# Import the discord bot instance
try:
    from bot import bot, DISCORD_TOKEN
except Exception as e:
    logger.error(f"Failed to import discord bot from bot.py: {e}", exc_info=True)
    sys.exit(1)

START_TIME = time.time()
PORT = int(os.environ.get("PORT", 10000))
SELF_PING_INTERVAL_SEC = 480  # 8 minutes (Render sleeps after 15 min of no HTTP traffic)


import math

async def handle_root(request: web.Request) -> web.Response:
    uptime_sec = int(time.time() - START_TIME)
    is_ready = bool(bot.is_ready())
    latency_val = None
    if is_ready and bot.latency is not None and not math.isnan(bot.latency):
        latency_val = round(bot.latency * 1000, 2)

    data = {
        "service": "Discord Bot 24/7 Hosting",
        "status": "healthy" if is_ready else "starting",
        "bot_user": str(bot.user) if is_ready else None,
        "guilds_count": len(bot.guilds) if is_ready else 0,
        "latency_ms": latency_val,
        "uptime_seconds": uptime_sec,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    return web.json_response(data)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "healthy",
            "bot_connected": bool(bot.is_ready()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        status=200,
    )


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="pong", content_type="text/plain")


async def run_bot_supervisor():
    """Runs the Discord bot in an auto-restart resilient loop."""
    logger.info("Starting Discord Bot Supervisor...")
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is missing! Bot cannot start.")
        return

    retry_delay = 5
    while True:
        try:
            logger.info("Connecting Discord bot to Gateway...")
            await bot.start(DISCORD_TOKEN)
        except asyncio.CancelledError:
            logger.info("Bot supervisor cancelled, closing bot...")
            if not bot.is_closed():
                await bot.close()
            break
        except Exception as e:
            logger.error(f"Discord bot encountered an error: {e}. Reconnecting in {retry_delay}s...")
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                pass
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)  # Exponential backoff up to 60s
        else:
            logger.warning("bot.start finished unexpectedly. Restarting in 5s...")
            await asyncio.sleep(5)


async def self_ping_worker():
    """Keeps the Render Web Service awake by self-pinging every few minutes."""
    await asyncio.sleep(30)  # Wait for web server to initialize
    while True:
        url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
        if url:
            target = url.rstrip("/") + "/api/health"
            try:
                timeout = ClientTimeout(total=10)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(target) as resp:
                        logger.info(f"[Keep-Alive Self-Ping] Status: {resp.status} for {target}")
            except Exception as e:
                logger.warning(f"[Keep-Alive Self-Ping] Ping failed: {e}")
        else:
            logger.debug("[Keep-Alive Self-Ping] No RENDER_EXTERNAL_URL or KEEP_ALIVE_URL defined.")
        await asyncio.sleep(SELF_PING_INTERVAL_SEC)


async def main():
    # 1. Setup aiohttp web server
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/ping", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"aiohttp health check web server is listening on 0.0.0.0:{PORT}")

    # 2. Launch supervisor and self-pinger as background tasks
    bot_task = asyncio.create_task(run_bot_supervisor(), name="discord_bot_supervisor")
    ping_task = asyncio.create_task(self_ping_worker(), name="keep_alive_self_ping")

    try:
        # Run until interrupted
        await asyncio.gather(bot_task, ping_task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown initiated...")
    finally:
        logger.info("Cleaning up web server and bot...")
        bot_task.cancel()
        ping_task.cancel()
        if not bot.is_closed():
            await bot.close()
        await runner.cleanup()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user.")
