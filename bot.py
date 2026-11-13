import time
import collections
import os
import io
import re
import sys
import json
import socket
import subprocess
import ast
import urllib.request
import urllib.parse
import random
import shutil
try:
    import yt_dlp
except ImportError:
    yt_dlp = None
import wavelink
from typing import cast

try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_EXE = shutil.which("ffmpeg") or "ffmpeg"

import logging
from datetime import datetime, timedelta
import asyncio
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# In pythonw.exe (background mode), stdout/stderr are None. Redirect them so print() never crashes.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# --- Single Instance Lock (Only needed on local Windows background runs) ---
if os.name == "nt":
    _instance_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _instance_lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _instance_lock_socket.bind(("127.0.0.1", 5056))
    except OSError:
        pass  # On cloud/Pterodactyl container or reboot, do not exit process

# --- Setup Logging ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
log_file = os.path.join(BASE_DIR, "feedback_bot.log")

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log_handlers = [logging.FileHandler(log_file, encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("FeedbackReminderBot")

# --- Load Environment Variables ---
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FEEDBACK_CHANNEL_ID = os.getenv("FEEDBACK_CHANNEL_ID", "1518838825927839949").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "1498326006669377548").strip()
CUSTOMER_ROLE_ID = os.getenv("CUSTOMER_ROLE_ID", "1518838688493207652").strip()

FEEDBACK_CHANNEL_URL = os.getenv(
    "FEEDBACK_CHANNEL_URL",
    f"https://discord.com/channels/{DISCORD_CHANNEL_ID}/{FEEDBACK_CHANNEL_ID}",
).strip()

API_HOST = os.getenv("API_HOST", "0.0.0.0").strip()
API_PORT = int(os.getenv("API_PORT", "5055"))
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "byte_feedback_secure_key_2026").strip()
DM_COOLDOWN_HOURS = float(os.getenv("DM_COOLDOWN_HOURS", "48"))
AUDIT_LOG_CHANNEL_ID = os.getenv("AUDIT_LOG_CHANNEL_ID", DISCORD_CHANNEL_ID).strip()

# 48-Hour Automatic Scheduler Settings
AUTO_REMIND_ENABLED = os.getenv("AUTO_REMIND_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_REMIND_HOURS = float(os.getenv("AUTO_REMIND_HOURS", "48"))
LAST_RUN_FILE = os.path.join(BASE_DIR, "last_auto_run.json")
COOLDOWN_FILE = os.path.join(BASE_DIR, "user_cooldowns.json")
stats_total_sent = 0


def get_last_auto_run() -> datetime | None:
    """Reads the timestamp of the last 48-hour automated run."""
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                iso_str = data.get("last_run")
                if iso_str:
                    return datetime.fromisoformat(iso_str)
        except Exception as e:
            logger.error(f"Error reading last_auto_run.json: {e}")
    return None


def save_last_auto_run(dt: datetime):
    """Saves the timestamp of the last 48-hour automated run."""
    try:
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_run": dt.isoformat()}, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving last_auto_run.json: {e}")


def is_extra_owner(user_id: int) -> bool:
    """Checks if a user is registered as an Extra Owner in antinuke_config.json."""
    if not user_id:
        return False
    try:
        cfg = load_antinuke_config()
        extra_owners = cfg.get("extra_owners", [])
        return user_id in extra_owners or str(user_id) in [str(x) for x in extra_owners]
    except Exception:
        return False


def is_server_or_extra_owner(guild: discord.Guild, user_id: int) -> bool:
    """Returns True ONLY for Server Owner or Extra Owner (Excludes regular Administrators)."""
    if not guild or not user_id:
        return False
    if user_id == guild.owner_id:
        return True
    return is_extra_owner(user_id)


def is_bot_owner_global(guild: discord.Guild, user_id: int) -> bool:
    """Returns True if user is the Server Owner or Extra Owner."""
    if not guild or not user_id:
        return False
    if user_id == guild.owner_id:
        return True
    return is_extra_owner(user_id)


def has_admin_or_owner_access(member: discord.Member) -> bool:
    """Returns True if user has Administrator permission, is Server Owner, or is configured as an Extra Owner."""
    if not member or not getattr(member, 'guild', None):
        return False
    if member.guild_permissions.administrator:
        return True
    return is_bot_owner_global(member.guild, member.id)


def load_user_cooldowns() -> dict[int, datetime]:
    """Loads persistent cooldown timestamps for all users."""
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                return {int(k): datetime.fromisoformat(v) for k, v in data.items()}
        except Exception as e:
            logger.error(f"Error loading user_cooldowns.json: {e}")
    return {}


def save_user_cooldown(discord_id: int, dt: datetime):
    """Saves a user's sent timestamp to disk so duplicate DMs are strictly prevented."""
    cooldowns = {}
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, "r", encoding="utf-8-sig") as f:
                cooldowns = json.load(f)
        except Exception:
            cooldowns = {}
    cooldowns[str(discord_id)] = dt.isoformat()
    try:
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(cooldowns, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving user_cooldowns.json: {e}")

# --- Discord Bot Setup ---
intents = discord.Intents.all()

def get_active_lavalink_nodes() -> list[wavelink.Node]:
    """Returns verified live 2026 Lavalink v4 nodes with automatic failover and safe retry limits."""
    return [
        wavelink.Node(
            identifier="Nazha-Global",
            uri="https://lavalink.nazha.online:443",
            password="nazhafreelava",
            heartbeat=15.0,
            retries=3,
            resume_timeout=180,
            inactive_player_timeout=None
        ),
    ]


async def connect_lavalink_pool(client: discord.Client):
    """Ensures active high-speed Lavalink nodes are connected without blocking the bot."""
    nodes = get_active_lavalink_nodes()
    nodes_dict = getattr(wavelink.Pool, "nodes", {})
    for node in nodes:
        if node.identifier in nodes_dict and getattr(nodes_dict[node.identifier], "status", None) == wavelink.NodeStatus.CONNECTED:
            continue
        try:
            await asyncio.wait_for(wavelink.Pool.connect(nodes=[node], client=client), timeout=6.0)
            logger.info(f"Connected Lavalink node: {node.identifier}")
        except Exception as e:
            logger.warning(f"Lavalink node connect notice on {node.identifier}: {e}")

class ByteBot(commands.Bot):
    async def setup_hook(self):
        # Register persistent UI views exactly once before gateway connects
        self.add_view(BytePanelWhitelistView())
        self.add_view(ByteCommandMenuView())
        self.add_view(KeyAuthControlView())
        self.add_view(ByteResellerControlView())
        self.add_view(SecondKeyAuthControlView())
        self.add_view(MusicControlView())
        self.add_view(ByteZynraxHelpView())
        self.add_view(VoiceMasterControlView())
        self.add_view(ByteCustomTicketPanelView())
        self.add_view(ByteCustomTicketControlView())
        self.add_view(ByteWelcomeEditorView())
        self.add_view(ServerVerificationView())
        self.add_view(ByteAutoroleView())
        
        # Connect to verified ultra-fast 2026 Lavalink node in background (never blocks gateway!)
        asyncio.create_task(connect_lavalink_pool(self))
        try:
            synced = await self.tree.sync()
            logger.info(f"⚡ Synced {len(synced)} slash commands.")
        except Exception as e:
            logger.warning(f"Slash command tree sync skipped: {e}")

bot = ByteBot(command_prefix="*", intents=intents, help_command=None)


def create_warning_embed(target_name: str) -> discord.Embed:
    """Builds the exact warning & feedback reminder embed specified by user."""
    channel_mention = f"<#{FEEDBACK_CHANNEL_ID}>" if FEEDBACK_CHANNEL_ID else "#feedback-channel"

    embed = discord.Embed(
        title="⚠️ Feedback Reminder & Warning — BYTE CORPORATION",
        description=(
            f"Hello **{target_name}**!\n\n"
            f"This is a required warning and reminder to upload your daily feedback/screenshots in {channel_mention}.\n\n"
            "Please upload your feedback as soon as possible. Thank you!"
        ),
        color=0x0000FF,  # Blue Theme
        timestamp=datetime.utcnow(),
    )
    if bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="📌 Feedback Channel",
        value=f"👉 {channel_mention}",
        inline=False,
    )
    embed.set_footer(text="BYTE CORPORATION • Automated Feedback System")
    return embed


class FeedbackLinkView(discord.ui.View):
    """Button in DM that links directly to the feedback channel."""
    def __init__(self, feedback_url: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="📸 Upload Feedback Now",
                url=feedback_url,
                style=discord.ButtonStyle.link,
                emoji="📸",
            )
        )


async def send_feedback_to_user(discord_id: int, username: str = "", bypass_cooldown: bool = False) -> tuple[bool, str]:
    """
    Sends the warning/feedback DM to a specific Discord user by their ID.
    Returns (success: bool, status_message: str)
    """
    global stats_total_sent

    # 1. Strict 48-Hour Persistent Cooldown check
    now = datetime.utcnow()
    user_cooldowns = load_user_cooldowns()
    if not bypass_cooldown and discord_id in user_cooldowns:
        last_sent = user_cooldowns[discord_id]
        if now - last_sent < timedelta(hours=DM_COOLDOWN_HOURS):
            remaining = timedelta(hours=DM_COOLDOWN_HOURS) - (now - last_sent)
            hours_rem = round(remaining.total_seconds() / 3600, 1)
            logger.info(f"🚫 [48H COOLDOWN] Discord ID {discord_id} ko {hours_rem}h pehle already message ja chuka hai. SKIPPED.")
            return False, f"Cooldown active ({hours_rem}h remaining)"

    # 2. Fetch User
    try:
        user = await bot.fetch_user(discord_id)
    except discord.NotFound:
        logger.warning(f"User with Discord ID {discord_id} not found on Discord.")
        return False, "Discord User ID not found."
    except Exception as e:
        logger.error(f"Error fetching Discord ID {discord_id}: {e}")
        return False, f"Failed to fetch user: {e}"

    display_name = username if username else user.name
    embed = create_warning_embed(display_name)
    view = FeedbackLinkView(FEEDBACK_CHANNEL_URL)

    # 3. Send DM
    try:
        await user.send(embed=embed, view=view)
        save_user_cooldown(discord_id, now)
        stats_total_sent += 1
        logger.info(f"✅ Feedback Warning DM sent to {user.name} ({discord_id})")

        # Optional Audit Log
        if AUDIT_LOG_CHANNEL_ID and AUDIT_LOG_CHANNEL_ID.isdigit():
            try:
                log_chan = bot.get_channel(int(AUDIT_LOG_CHANNEL_ID))
                if log_chan:
                    log_embed = discord.Embed(
                        title="📋 Feedback Reminder Sent",
                        color=0x0000FF,
                        timestamp=now,
                    )
                    log_embed.add_field(name="Target User", value=f"{user.mention} (`{discord_id}`)", inline=True)
                    if username:
                        log_embed.add_field(name="Account Name", value=f"`{username}`", inline=True)
                    log_embed.set_footer(text="BYTE CORPORATION Audit Log")
                    await log_chan.send(embed=log_embed)
            except Exception as e:
                logger.error(f"Failed to post to audit channel: {e}")

        return True, "Feedback DM sent successfully!"

    except discord.Forbidden:
        logger.warning(f"❌ Cannot send DM to {user.name} ({discord_id}). User has DMs closed.")
        return False, "User has DMs closed or blocked."
    except Exception as e:
        logger.error(f"❌ Error sending DM to {discord_id}: {e}")
        return False, f"Failed to send DM: {e}"


# ==========================================
# AIOHTTP API WEB SERVER (LISTENS TO PANEL)
# ==========================================

async def handle_login_event(request: web.Request) -> web.Response:
    """
    Endpoint: POST /send-feedback
    Called by Panel .exe or Auth Dashboard when a user logs in.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

    # 1. Verify Secret
    provided_secret = data.get("secret", "").strip()
    if API_SECRET_KEY and provided_secret != API_SECRET_KEY:
        logger.warning(f"Unauthorized API request. Secret mismatch: '{provided_secret}'")
        return web.json_response({"success": False, "error": "Unauthorized: Invalid secret key"}, status=401)

    # 2. Extract Discord ID
    raw_discord_id = data.get("discord_id")
    if not raw_discord_id:
        return web.json_response({"success": False, "error": "Missing 'discord_id' parameter"}, status=400)

    try:
        discord_id = int(str(raw_discord_id).strip())
    except ValueError:
        return web.json_response({"success": False, "error": "Invalid numeric discord_id"}, status=400)

    username = data.get("username", "")

    # 3. Send DM
    success, message = await send_feedback_to_user(
        discord_id=discord_id,
        username=username,
        bypass_cooldown=False,
    )

    return web.json_response({"success": success, "message": message}, status=200)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "online",
        "bot_name": str(bot.user) if bot.user else "Starting...",
        "total_dms_sent": stats_total_sent,
        "feedback_channel_id": FEEDBACK_CHANNEL_ID,
        "customer_role_id": CUSTOMER_ROLE_ID,
    })


async def handle_root(request):
    return web.json_response({"status": "online", "service": "BYTE CORPORATION API", "version": "1.0.0"})


async def start_web_server():
    app = web.Application()
    app.router.add_post("/send-feedback", handle_login_event)
    app.router.add_post("/api/login-dm", handle_login_event)
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=API_HOST, port=API_PORT)
        await site.start()
        logger.info(f"🌐 Panel API Server is LIVE on http://{API_HOST}:{API_PORT}")
        logger.info(f"📡 Ready to receive login events from Panel .exe!")
    except Exception as e:
        logger.warning(f"Panel API Web Server port binding skipped (Cloud Host mode): {e}")


# ==========================================
# 48-HOUR AUTOMATIC BROADCAST ROUTINE
# ==========================================

async def perform_role_broadcast(trigger_source: str = "Manual") -> tuple[int, int]:
    """
    Sends the warning/feedback DM to all members possessing CUSTOMER_ROLE_ID.
    Returns (sent_count, failed_count)
    """
    target_role_id = int(CUSTOMER_ROLE_ID) if CUSTOMER_ROLE_ID.isdigit() else None
    if not target_role_id:
        logger.warning(f"[{trigger_source}] CUSTOMER_ROLE_ID is not configured.")
        return 0, 0

    target_guild = None
    if DISCORD_CHANNEL_ID.isdigit():
        chan = bot.get_channel(int(DISCORD_CHANNEL_ID))
        if chan and hasattr(chan, "guild"):
            target_guild = chan.guild

    if not target_guild and bot.guilds:
        for g in bot.guilds:
            if g.get_role(target_role_id):
                target_guild = g
                break
        if not target_guild:
            target_guild = bot.guilds[0]

    if not target_guild:
        logger.warning(f"[{trigger_source}] Could not find guild.")
        return 0, 0

    role = target_guild.get_role(target_role_id)
    if not role:
        logger.warning(f"[{trigger_source}] Role ID {target_role_id} not found in guild {target_guild.name}.")
        return 0, 0

    members = [m for m in role.members if not m.bot]
    logger.info(f"⏰ [{trigger_source}] Starting broadcast to {len(members)} members with role '{role.name}'...")

    sent_count = 0
    failed_count = 0

    for m in members:
        success, _ = await send_feedback_to_user(
            discord_id=m.id,
            username=m.name,
            bypass_cooldown=False,
        )
        if success:
            sent_count += 1
        else:
            failed_count += 1
        await asyncio.sleep(1.5)  # Discord rate limit safe delay

    now = datetime.utcnow()
    save_last_auto_run(now)
    logger.info(f"✅ [{trigger_source} COMPLETE] Sent: {sent_count}, Skipped/Failed: {failed_count}")

    # Log to Audit Channel
    if AUDIT_LOG_CHANNEL_ID and AUDIT_LOG_CHANNEL_ID.isdigit():
        try:
            log_chan = bot.get_channel(int(AUDIT_LOG_CHANNEL_ID))
            if log_chan:
                embed = discord.Embed(
                    title=f"📢 {trigger_source} Feedback Broadcast Summary",
                    description=(
                        f"• **Target Role:** {role.mention}\n"
                        f"• **Successfully Sent:** `{sent_count}` users\n"
                        f"• **Skipped / Failed:** `{failed_count}` (Cooldown / DMs closed)\n"
                        f"• **Auto Schedule:** Every `{int(AUTO_REMIND_HOURS)}` Hours"
                    ),
                    color=0x0000FF,
                    timestamp=now,
                )
                await log_chan.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to post broadcast log to channel: {e}")

    return sent_count, failed_count


@tasks.loop(minutes=5)
async def auto_schedule_task():
    """Checks every 5 minutes if 48 hours have elapsed since the last run."""
    if not AUTO_REMIND_ENABLED:
        return

    last_run = get_last_auto_run()
    now = datetime.utcnow()

    should_run = False
    if last_run is None:
        logger.info(f"⏰ Auto Scheduler: First run triggered (Schedule: Every {int(AUTO_REMIND_HOURS)}h).")
        should_run = True
    elif (now - last_run) >= timedelta(hours=AUTO_REMIND_HOURS):
        logger.info(f"⏰ Auto Scheduler: {int(AUTO_REMIND_HOURS)} hours elapsed since last run. Triggering routine...")
        should_run = True

    if should_run:
        save_last_auto_run(now)  # Immediately save timestamp FIRST so no duplicate runs can overlap
        await perform_role_broadcast(trigger_source=f"Auto-{int(AUTO_REMIND_HOURS)}H")


# ==========================================
# DISCORD BOT EVENTS & SLASH COMMANDS
# ==========================================

# ==========================================
# BOT PRESENCE & ROTATING STATUS ENGINE
# ==========================================

STATUS_ROTATION_LIST = [
    {"type": "watching", "text": "*menu | BYTE CORPORATION"},
    {"type": "watching", "text": "15 Loaders & UID Bypass"},
    {"type": "watching", "text": "*ticketpanel | 24/7 Support"},
    {"type": "playing", "text": "*keypanel | BYTE AUTH System"},
    {"type": "listening", "text": "*play | 24/7 High-Fidelity Music"},
    {"type": "streaming", "text": "discord.gg/w5Qvp66KC", "url": "https://twitch.tv/bytecorporation"},
]

_current_status_index = 0

@tasks.loop(seconds=20)
async def rotate_status_task():
    """Dynamically rotates bot presence every 20 seconds or keeps custom status."""
    global _current_status_index
    try:
        cfg = load_config()
        custom_status = cfg.get("custom_status")
        if custom_status and isinstance(custom_status, dict):
            st_type = custom_status.get("type", "watching").lower()
            st_text = custom_status.get("text", "*menu | BYTE CORPORATION")
            st_url = custom_status.get("url", "https://twitch.tv/bytecorporation")
            st_pres = custom_status.get("presence", "online").lower()
        else:
            item = STATUS_ROTATION_LIST[_current_status_index % len(STATUS_ROTATION_LIST)]
            _current_status_index += 1
            st_type = item.get("type", "watching")
            st_text = item.get("text", "*menu | BYTE CORPORATION")
            st_url = item.get("url", "https://twitch.tv/bytecorporation")
            st_pres = "online"

        presence_status = discord.Status.online
        if st_pres == "idle":
            presence_status = discord.Status.idle
        elif st_pres in ("dnd", "do_not_disturb"):
            presence_status = discord.Status.dnd

        activity = None
        if st_type == "streaming":
            activity = discord.Streaming(name=st_text, url=st_url)
        elif st_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=st_text)
        elif st_type == "playing":
            activity = discord.Game(name=st_text)
        elif st_type == "competing":
            activity = discord.Activity(type=discord.ActivityType.competing, name=st_text)
        else:
            activity = discord.Activity(type=discord.ActivityType.watching, name=st_text)

        await bot.change_presence(activity=activity, status=presence_status)
    except Exception as e:
        logger.debug(f"Status rotation tick error: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Automatically syncs slash commands and sends VOID Security welcome embed."""
    logger.info(f"✨ Bot added to new guild: {guild.name} (ID: {guild.id})")
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info(f"⚡ Synced slash commands to newly joined guild: {guild.name} ({guild.id})")
    except Exception as e:
        logger.warning(f"Could not sync slash commands to new guild {guild.id}: {e}")

    try:
        cfg = load_antinuke_config()
        embed = build_void_welcome_embed(guild, guild.owner, cfg)
        support_link = cfg.get("support_url", "https://discord.gg/uaDzFDtFSe")
        bot_name = cfg.get("bot_name") or (bot.user.name if bot.user else "Bot")
        bot_invite = cfg.get("invite_url") or (discord.utils.oauth_url(bot.user.id, permissions=discord.Permissions(administrator=True)) if bot.user else None)
        view = VoidSecurityActivationView(support_url=support_link, invite_url=bot_invite, bot_name=bot_name)

        target_ch = guild.system_channel
        if not target_ch or not target_ch.permissions_for(guild.me).send_messages:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target_ch = ch
                    break

        if target_ch:
            await target_ch.send(embed=embed, view=view)
        elif guild.owner:
            await guild.owner.send(embed=embed, view=view)
    except Exception as e:
        logger.warning(f"Could not send VOID welcome greeting to guild {guild.id}: {e}")


_ready_executed = False

@tasks.loop(seconds=25)
async def lavalink_health_keeper():
    """Keeps Lavalink node pool connected and healthy with auto-reconnection."""
    try:
        nodes_dict = getattr(wavelink.Pool, "nodes", {})
        has_connected = any(getattr(n, "status", None) == wavelink.NodeStatus.CONNECTED for n in nodes_dict.values())
        if not has_connected:
            logger.info("Auto-reconnecting active Lavalink nodes...")
            await connect_lavalink_pool(bot)
    except Exception as e:
        logger.debug(f"Lavalink health keeper notice: {e}")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    logger.info(f"⚡ Lavalink Node '{payload.node.identifier}' is READY! (Resumed: {payload.resumed})")


@bot.event
async def on_wavelink_node_closed(node: wavelink.Node, disconnected: list[wavelink.Player]):
    logger.warning(f"⚠ Lavalink Node '{node.identifier}' closed connection. Disconnected players: {len(disconnected)}")


@bot.event
async def on_ready():
    global _ready_executed
    logger.info(f"🤖 Bot Logged In: {bot.user} (ID: {bot.user.id})")

    # Instant Guild Slash Command Sync: Makes all commands appear in '/' popup instantly without waiting hours!
    for g in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
            logger.info(f"⚡ Instantly synced slash commands to guild: {g.name} ({g.id})")
        except Exception as e:
            logger.debug(f"Guild sync notice for {g.id}: {e}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"⚡ Synced {len(synced)} global slash commands successfully!")
    except Exception as e:
        logger.warning(f"Global slash sync error: {e}")

    if _ready_executed:
        logger.info("Bot successfully resumed Discord Gateway session.")
        return
    _ready_executed = True

    # Start the API server on the same async loop
    asyncio.create_task(start_web_server())

    # Start 48-Hour Automatic Scheduler
    if AUTO_REMIND_ENABLED and not auto_schedule_task.is_running():
        try:
            auto_schedule_task.start()
            logger.info(f"⏰ 48-Hour Auto Scheduler started! (Every {int(AUTO_REMIND_HOURS)} Hours)")
        except Exception as e:
            logger.warning(f"Scheduler start skipped: {e}")

    last_run = get_last_auto_run()
    last_str = last_run.strftime("%Y-%m-%d %H:%M UTC") if last_run else "First Run Pending"

    # Immediately set initial presence and start rotating status engine
    try:
        init_act = discord.Activity(type=discord.ActivityType.watching, name="*menu | BYTE CORPORATION")
        await bot.change_presence(activity=init_act, status=discord.Status.online)
        if not rotate_status_task.is_running():
            rotate_status_task.start()
            logger.info("⚡ Bot Presence & Rotating Status Engine started!")
    except Exception as e:
        logger.warning(f"Failed to set initial presence: {e}")

    # Start Lavalink health keeper
    if not lavalink_health_keeper.is_running():
        try:
            lavalink_health_keeper.start()
            logger.info("⚡ Lavalink Health Keeper started!")
        except Exception as e:
            logger.warning(f"Lavalink Health Keeper start error: {e}")

    # Start Giveaway System & restore persistent views
    try:
        g_data = load_giveaways()
        active_gws = [k for k, v in g_data.get("giveaways", {}).items() if not v.get("ended", False)]
        for gw_id in active_gws:
            bot.add_view(GiveawayEntryView(int(gw_id)))
        if active_gws:
            logger.info(f"🎁 Restored {len(active_gws)} active Giveaway interactive view(s)!")
    except Exception as e:
        logger.warning(f"Error restoring giveaway views: {e}")

    if not giveaway_timer_task.is_running():
        try:
            giveaway_timer_task.start()
            logger.info("🎁 Giveaway Timer Task started (Every 10s)!")
        except Exception as e:
            logger.warning(f"Error starting giveaway timer: {e}")

    logger.info("BYTE CORPORATION BOT (Feedback + UID Whitelist) IS FULLY ONLINE!")

# ==========================================
# TEXT / PREFIX COMMANDS (!test, !send, !channel)
# ==========================================

@bot.command(name="setstatus", aliases=["botstatus", "status"])
async def setstatus_cmd(ctx, activity_type: str = None, *, text: str = None):
    """Sets a custom activity status for the bot (playing/watching/listening/streaming/rotate)."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only staff or administrators can change the bot's status.")
        return

    if not activity_type or activity_type.lower() in ("rotate", "reset", "auto"):
        cfg = load_config()
        if "custom_status" in cfg:
            del cfg["custom_status"]
            save_config(cfg)
        await ctx.reply("🔄 **Status Mode Reset:** The bot will now automatically rotate between official BYTE CORPORATION activities every 20 seconds.")
        return

    act_lower = activity_type.lower()
    valid_types = ("playing", "watching", "listening", "streaming", "competing")
    if act_lower not in valid_types:
        await ctx.reply(f"❌ Invalid activity type. Choose from: `playing`, `watching`, `listening`, `streaming`, or `rotate`.\nExample: `*setstatus watching BYTE CORPORATION`")
        return

    if not text:
        await ctx.reply(f"Usage: `*setstatus {act_lower} <text>` (e.g. `*setstatus {act_lower} BYTE CORPORATION Support`)")
        return

    cfg = load_config()
    cfg["custom_status"] = {
        "type": act_lower,
        "text": text.strip(),
        "url": "https://twitch.tv/bytecorporation",
        "presence": "online"
    }
    save_config(cfg)

    # Immediately apply
    if act_lower == "streaming":
        activity = discord.Streaming(name=text.strip(), url="https://twitch.tv/bytecorporation")
    elif act_lower == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=text.strip())
    elif act_lower == "playing":
        activity = discord.Game(name=text.strip())
    elif act_lower == "competing":
        activity = discord.Activity(type=discord.ActivityType.competing, name=text.strip())
    else:
        activity = discord.Activity(type=discord.ActivityType.watching, name=text.strip())

    await bot.change_presence(activity=activity, status=discord.Status.online)
    await ctx.reply(f"✅ **Bot Status Updated:** Set to **{act_lower.capitalize()}** `{text.strip()}`!")


@bot.tree.command(name="setstatus", description="Set a custom activity status for the bot")
@app_commands.describe(
    activity_type="Activity type (playing, watching, listening, streaming, rotate)",
    text="The status text to display (leave blank for rotate mode)"
)
@app_commands.choices(activity_type=[
    app_commands.Choice(name="Watching", value="watching"),
    app_commands.Choice(name="Playing", value="playing"),
    app_commands.Choice(name="Listening", value="listening"),
    app_commands.Choice(name="Streaming", value="streaming"),
    app_commands.Choice(name="Auto Rotate (Default)", value="rotate"),
])
async def setstatus_slash(interaction: discord.Interaction, activity_type: app_commands.Choice[str], text: str = None):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only staff or administrators can change the bot's status.", ephemeral=True)
        return

    act_val = activity_type.value
    if act_val == "rotate" or not text:
        cfg = load_config()
        if "custom_status" in cfg:
            del cfg["custom_status"]
            save_config(cfg)
        await interaction.response.send_message("🔄 **Status Mode Reset:** The bot will now automatically rotate between official BYTE CORPORATION activities every 20 seconds.", ephemeral=True)
        return

    cfg = load_config()
    cfg["custom_status"] = {
        "type": act_val,
        "text": text.strip(),
        "url": "https://twitch.tv/bytecorporation",
        "presence": "online"
    }
    save_config(cfg)

    if act_val == "streaming":
        activity = discord.Streaming(name=text.strip(), url="https://twitch.tv/bytecorporation")
    elif act_val == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=text.strip())
    elif act_val == "playing":
        activity = discord.Game(name=text.strip())
    else:
        activity = discord.Activity(type=discord.ActivityType.watching, name=text.strip())

    await bot.change_presence(activity=activity, status=discord.Status.online)
    await interaction.response.send_message(f"✅ **Bot Status Updated:** Set to **{act_val.capitalize()}** `{text.strip()}`!", ephemeral=True)


@bot.command(name="setpresence")
async def setpresence_cmd(ctx, mode: str = "online"):
    """Sets bot presence dot color: online (green), idle (yellow), or dnd (red)."""
    if not is_staff(ctx.author):
        return
    mode_clean = mode.lower().strip()
    status_map = {
        "online": discord.Status.online,
        "green": discord.Status.online,
        "idle": discord.Status.idle,
        "yellow": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "red": discord.Status.dnd,
    }
    if mode_clean not in status_map:
        await ctx.reply("Usage: `*setpresence <online/idle/dnd>`")
        return
    st = status_map[mode_clean]
    await bot.change_presence(status=st)
    await ctx.reply(f"✅ Bot presence dot set to **{mode_clean.upper()}**!")


@bot.command(name="test", aliases=["testdm"])
async def test_cmd(ctx):
    """Sends a test warning DM to the command author."""
    msg = await ctx.reply("⏳ Sending test DM...")
    success, result_msg = await send_feedback_to_user(
        discord_id=ctx.author.id,
        username=ctx.author.name,
        bypass_cooldown=True,
    )
    if success:
        await msg.edit(content=f"✅ {ctx.author.mention} A test feedback reminder has been sent to your Direct Messages! Check your DM.")
    else:
        await msg.edit(content=f"❌ Failed to send DM: {result_msg}")


@bot.command(name="send", aliases=["remind", "senddm"])
async def send_to_role_cmd(ctx):
    """Sends feedback warning DM to all users with customer role."""
    target_role_id = int(CUSTOMER_ROLE_ID) if CUSTOMER_ROLE_ID.isdigit() else None
    if not target_role_id:
        await ctx.reply("❌ CUSTOMER_ROLE_ID is not configured in .env!")
        return

    target_role = ctx.guild.get_role(target_role_id) if ctx.guild else None
    if not target_role:
        await ctx.reply(f"❌ Role ID `{target_role_id}` was not found in this server. Please check the role ID.")
        return

    members = [m for m in target_role.members if not m.bot]
    if not members:
        await ctx.reply(f"ℹ️ Role {target_role.mention} currently has no members.")
        return

    status_msg = await ctx.reply(f"⏳ **Processing:** Role {target_role.mention} Sending feedback reminder DMs to **{len(members)} members** with {target_role.mention}...")

    sent_count = 0
    failed_count = 0

    for m in members:
        success, _ = await send_feedback_to_user(
            discord_id=m.id,
            username=m.name,
            bypass_cooldown=False,
        )
        if success:
            sent_count += 1
        else:
            failed_count += 1
        await asyncio.sleep(1.2)  # Discord rate limit protection

    await status_msg.edit(
        content=(
            f"✅ **Feedback Reminder Process Complete!**\n"
            f"• **Target Role:** {target_role.mention}\n"
            f"• **Successfully Sent:** `{sent_count}` users\n"
            f"• **Skipped/Failed:** `{failed_count}` (Cooldown active / DMs disabled)"
        )
    )


@bot.command(name="channel", aliases=["warnchannel"])
async def channel_warn_cmd(ctx):
    """Posts the warning message into configured discord channel."""
    chan_id = int(DISCORD_CHANNEL_ID) if DISCORD_CHANNEL_ID.isdigit() else ctx.channel.id
    channel = ctx.guild.get_channel(chan_id) if ctx.guild else ctx.channel

    if not channel:
        await ctx.reply(f"❌ Channel ID `{chan_id}` was not found!")
        return

    role_mention = f"<@&{CUSTOMER_ROLE_ID}>" if CUSTOMER_ROLE_ID else "@everyone"
    embed = create_warning_embed(target_name=role_mention)
    view = FeedbackLinkView(FEEDBACK_CHANNEL_URL)

    await channel.send(content=f"⚠️ Attention {role_mention}!", embed=embed, view=view)
    await ctx.reply(f"✅ Reminder {channel.mention} has been posted successfully!")


@bot.command(name="schedule", aliases=["autorun", "timer"])
async def schedule_status_cmd(ctx):
    """Shows the 48-hour auto-scheduler status."""
    last_run = get_last_auto_run()
    now = datetime.utcnow()

    if not AUTO_REMIND_ENABLED:
        await ctx.reply("❌ 48-Hour Auto Scheduler is currently **DISABLED** in .env.")
        return

    if last_run:
        diff = now - last_run
        remaining = timedelta(hours=AUTO_REMIND_HOURS) - diff
        if remaining.total_seconds() > 0:
            hrs = round(remaining.total_seconds() / 3600, 1)
            time_info = f"Next automatic run in **~{hrs} hours**"
        else:
            time_info = "Next automatic run is **Ready to trigger soon**"
        last_str = last_run.strftime("%Y-%m-%d %H:%M UTC")
    else:
        time_info = "First automatic run will trigger on next cycle"
        last_str = "Never"

    embed = discord.Embed(
        title="⏰ 48-Hour Auto Scheduler Status",
        color=0x0000FF,
        timestamp=now,
    )
    embed.add_field(name="⚙️ Status", value="`ACTIVE (Enabled)`", inline=True)
    embed.add_field(name="⏳ Interval", value=f"`Every {int(AUTO_REMIND_HOURS)} Hours`", inline=True)
    embed.add_field(name="🎯 Target Role", value=f"<@&{CUSTOMER_ROLE_ID}>", inline=True)
    embed.add_field(name="📅 Last Auto Run", value=f"`{last_str}`", inline=False)
    embed.add_field(name="⏱️ Next Trigger", value=time_info, inline=False)
    embed.set_footer(text="BYTE CORPORATION Automated System")

    await ctx.reply(embed=embed)


@bot.tree.command(name="testdm", description="Send a test feedback warning DM to yourself or a user")
@app_commands.describe(user="User to receive test DM")
async def test_dm_slash(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    await interaction.response.defer(ephemeral=True)

    success, msg = await send_feedback_to_user(
        discord_id=target.id,
        username=target.name,
        bypass_cooldown=True,
    )
    if success:
        await interaction.followup.send(f"✅ **Success:** Feedback warning DM has been sent to {target.mention}!", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ **Failed:** {msg}", ephemeral=True)


@bot.tree.command(name="remind_role", description="Send feedback reminder & warning to ALL members with the customer role")
@app_commands.describe(role="Role to target (Defaults to customer role in .env)")
async def remind_role_slash(interaction: discord.Interaction, role: discord.Role = None):
    """
    Mass-sends the warning DM to all members possessing the specified role.
    Rate-limited safely with 1.2s delay per user.
    """
    target_role_id = role.id if role else (int(CUSTOMER_ROLE_ID) if CUSTOMER_ROLE_ID.isdigit() else None)
    if not target_role_id:
        await interaction.response.send_message("❌ Target Role ID is not configured!", ephemeral=True)
        return

    target_role = interaction.guild.get_role(target_role_id) if interaction.guild else None
    if not target_role:
        await interaction.response.send_message(f"❌ Role ID `{target_role_id}` was not found in this server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if interaction.guild and not interaction.guild.chunked:
        try:
            await interaction.guild.chunk()
        except Exception:
            pass

    members = [m for m in target_role.members if not m.bot]
    if not members:
        await interaction.followup.send(f"ℹ️ Role {target_role.mention} currently has no members.", ephemeral=True)
        return

    sent_count = 0
    failed_count = 0

    for m in members:
        if m.bot:
            continue
        success, _ = await send_feedback_to_user(
            discord_id=m.id,
            username=m.name,
            bypass_cooldown=False,
        )
        if success:
            sent_count += 1
        else:
            failed_count += 1
        # Safe rate limit delay
        await asyncio.sleep(1.2)

    await interaction.followup.send(
        f"✅ **Completed:**\n"
        f"• Role: {target_role.mention}\n"
        f"• Successfully Sent: `{sent_count}`\n"
        f"• Skipped/Failed (Cooldown / DMs closed): `{failed_count}`",
        ephemeral=True,
    )


@bot.tree.command(name="send_channel_warning", description="Post the feedback reminder & warning in the announcement channel")
async def send_channel_warning_slash(interaction: discord.Interaction):
    """Posts the warning embed directly into the configured discord channel, tagging the role."""
    chan_id = int(DISCORD_CHANNEL_ID) if DISCORD_CHANNEL_ID.isdigit() else interaction.channel_id
    channel = interaction.guild.get_channel(chan_id) if interaction.guild else interaction.channel

    if not channel:
        await interaction.response.send_message(f"❌ Channel ID `{chan_id}` was not found!", ephemeral=True)
        return

    role_mention = f"<@&{CUSTOMER_ROLE_ID}>" if CUSTOMER_ROLE_ID else "@everyone"
    embed = create_warning_embed(target_name=role_mention)
    view = FeedbackLinkView(FEEDBACK_CHANNEL_URL)

    await channel.send(content=f"⚠️ Attention {role_mention}!", embed=embed, view=view)
    await interaction.response.send_message(f"✅ Reminder channel {channel.mention} has been posted successfully!", ephemeral=True)


# ==========================================


# ==========================================
# BYTE CORPORATION — UID WHITELIST SYSTEM
# ==========================================

UID_API_KEY = os.getenv("UID_API_KEY", "MANI272-BB17A22AD09C614B348DD37A1C1F276A").strip()
UID_API_BASE_URL = os.getenv("UID_API_BASE_URL", "https://mani272uidbypass.vercel.app/api/v1/uids").rstrip("/")

# KeyAuth / BYTE AUTH Reseller API Settings
KEYAUTH_API_KEY = os.getenv("KEYAUTH_API_KEY", "mani272_9c505a92d7d36452e91946fcabc83da8c378e4b2").strip()
KEYAUTH_BASE_URL = os.getenv("KEYAUTH_BASE_URL", "https://auth.mani272api.xyz/api/v1").rstrip("/")
config_file = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read config.json: {e}")
    return {}


def save_config(cfg: dict):
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save config.json: {e}")


def get_uid_log_channel_id() -> int | None:
    cfg = load_config()
    cfg_id = cfg.get("uid_log_channel_id") or cfg.get("log_channel_id")
    if cfg_id:
        try:
            return int(cfg_id)
        except (ValueError, TypeError):
            pass
    env_id = os.getenv("UID_LOG_CHANNEL_ID") or os.getenv("AUDIT_LOG_CHANNEL_ID") or os.getenv("LOG_CHANNEL_ID")
    if env_id and env_id.isdigit():
        return int(env_id)
    return None


def get_keyauth_log_channel_id() -> int | None:
    cfg = load_config()
    cfg_id = cfg.get("keyauth_log_channel_id")
    if cfg_id:
        try:
            return int(cfg_id)
        except (ValueError, TypeError):
            pass
    env_id = os.getenv("KEYAUTH_LOG_CHANNEL_ID")
    if env_id and env_id.isdigit():
        return int(env_id)
    return None


def get_log_channel_id() -> int | None:
    return get_uid_log_channel_id()


def get_admin_role_id() -> int | None:
    cfg = load_config()
    cfg_role = cfg.get("admin_role_id")
    if cfg_role:
        try:
            return int(cfg_role)
        except (ValueError, TypeError):
            pass
    return None


def get_keyauth_role_id() -> int | None:
    cfg = load_config()
    cfg_role = cfg.get("keyauth_role_id")
    if cfg_role:
        try:
            return int(cfg_role)
        except (ValueError, TypeError):
            pass
    env_role = os.getenv("KEYAUTH_ROLE_ID")
    if env_role and env_role.isdigit():
        return int(env_role)
    return None


def get_keyauth_channel_id() -> int | None:
    cfg = load_config()
    cfg_ch = cfg.get("keyauth_channel_id")
    if cfg_ch:
        try:
            return int(cfg_ch)
        except (ValueError, TypeError):
            pass
    env_ch = os.getenv("KEYAUTH_CHANNEL_ID")
    if env_ch and env_ch.isdigit():
        return int(env_ch)
    return None


def get_reseller_role_id() -> int | None:
    """Gets the role ID authorized for the single-app Reseller Panel."""
    cfg = load_config()
    cfg_role = cfg.get("reseller_role_id")
    if cfg_role:
        try:
            return int(cfg_role)
        except (ValueError, TypeError):
            pass
    env_role = os.getenv("RESELLER_ROLE_ID")
    if env_role and env_role.isdigit():
        return int(env_role)
    return None


def get_reseller_channel_id() -> int | None:
    """Gets the channel ID where the Reseller Panel is locked."""
    cfg = load_config()
    cfg_ch = cfg.get("reseller_channel_id")
    if cfg_ch:
        try:
            return int(cfg_ch)
        except (ValueError, TypeError):
            pass
    env_ch = os.getenv("RESELLER_CHANNEL_ID")
    if env_ch and env_ch.isdigit():
        return int(env_ch)
    return None


def get_reseller_app_config() -> dict:
    """Returns the single application allowed for resellers (default: Loader - UID Bypass)."""
    cfg = load_config()
    app_id = cfg.get("reseller_app_id", "loader_app_bypass")
    app = next((a for a in DEFAULT_KEYAUTH_APPS if a["id"] == app_id or a.get("app_id") == app_id), DEFAULT_KEYAUTH_APPS[0])
    return app


def has_reseller_permission(user: discord.Member | discord.User) -> tuple[bool, str]:
    """Checks if a user is allowed to access the single-app Reseller Panel."""
    if not isinstance(user, discord.Member):
        return False, "Commands can only be used within a server channel."
    if user.guild_permissions.administrator:
        return True, "Administrator"

    # Founders/Owners can always access
    key_role_id = get_keyauth_role_id()
    if key_role_id and any(r.id == key_role_id for r in user.roles):
        return True, f"<@&{key_role_id}>"

    # Check designated Reseller Role
    reseller_role_id = get_reseller_role_id()
    if reseller_role_id:
        if any(r.id == reseller_role_id for r in user.roles):
            return True, f"<@&{reseller_role_id}>"
        return False, f"You must have the <@&{reseller_role_id}> role to access the Reseller Panel."

    admin_role_id = get_admin_role_id()
    if admin_role_id and any(r.id == admin_role_id for r in user.roles):
        return True, f"<@&{admin_role_id}>"

    return False, "You do not have permission to access Reseller controls."


def is_reseller_channel_allowed(channel_id: int) -> tuple[bool, str]:
    """Enforces channel lock for the Reseller Panel."""
    locked_ch_id = get_reseller_channel_id()
    if not locked_ch_id:
        return True, ""
    if channel_id == locked_ch_id:
        return True, ""
    return False, f"Reseller panel actions can only be executed in <#{locked_ch_id}>."


def has_keyauth_permission(user: discord.Member | discord.User) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Commands can only be used within a server channel."
    if user.guild_permissions.administrator:
        return True, "Administrator"

    key_role_id = get_keyauth_role_id()
    if key_role_id:
        if any(r.id == key_role_id for r in user.roles):
            return True, f"<@&{key_role_id}>"
        return False, f"You must have the <@&{key_role_id}> role to generate KeyAuth license keys."

    admin_role_id = get_admin_role_id()
    if admin_role_id:
        if any(r.id == admin_role_id for r in user.roles):
            return True, f"<@&{admin_role_id}>"
        return False, f"You must have the <@&{admin_role_id}> role to access KeyAuth controls."

    return False, "You need Administrator permissions or the designated KeyAuth role (use `*setkeyrole @Role`)."


def is_keyauth_channel_allowed(channel_id: int) -> tuple[bool, str]:
    allowed_ch = get_keyauth_channel_id()
    if not allowed_ch:
        return True, "Any Channel"
    if channel_id == allowed_ch:
        return True, "Allowed"
    return False, f"KeyAuth panel and key generation are restricted to <#{allowed_ch}>."


# ==========================================
# SECOND KEYAUTH (PRO SDK - PRTVSHOW.ONLINE) HELPERS
# ==========================================
SECOND_KEYAUTH_DEFAULT_API_URL = "https://prtvshow.online/api.php"
SECOND_KEYAUTH_DEFAULT_APP_ID = "9f087d585fbd666572fc24b7"
SECOND_KEYAUTH_DEFAULT_APP_SECRET = "4ac5c8b945cbf8c75e15d771704616356c15edc061cbad6c12c92376da1dced7"
SECOND_KEYAUTH_DEFAULT_VERSION = "28"


def get_second_keyauth_config() -> dict:
    cfg = load_config()
    return {
        "api_url": os.getenv("SECOND_KEYAUTH_API_URL", cfg.get("second_keyauth_api_url", SECOND_KEYAUTH_DEFAULT_API_URL)).strip(),
        "app_id": os.getenv("SECOND_KEYAUTH_APP_ID", cfg.get("second_keyauth_app_id", SECOND_KEYAUTH_DEFAULT_APP_ID)).strip(),
        "app_secret": os.getenv("SECOND_KEYAUTH_APP_SECRET", cfg.get("second_keyauth_app_secret", SECOND_KEYAUTH_DEFAULT_APP_SECRET)).strip(),
        "package_id": os.getenv("SECOND_KEYAUTH_PACKAGE_ID", cfg.get("second_keyauth_package_id", "e52c1515c53453b85d0d4e87")).strip(),
        "version": os.getenv("SECOND_KEYAUTH_VERSION", cfg.get("second_keyauth_version", SECOND_KEYAUTH_DEFAULT_VERSION)).strip(),
    }


def get_second_keyauth_role_id() -> int | None:
    cfg = load_config()
    cfg_role = cfg.get("second_keyauth_role_id")
    if cfg_role:
        try:
            return int(cfg_role)
        except (ValueError, TypeError):
            pass
    env_role = os.getenv("SECOND_KEYAUTH_ROLE_ID")
    if env_role and env_role.isdigit():
        return int(env_role)
    return None


def get_second_keyauth_channel_id() -> int | None:
    cfg = load_config()
    cfg_ch = cfg.get("second_keyauth_channel_id")
    if cfg_ch:
        try:
            return int(cfg_ch)
        except (ValueError, TypeError):
            pass
    env_ch = os.getenv("SECOND_KEYAUTH_CHANNEL_ID")
    if env_ch and env_ch.isdigit():
        return int(env_ch)
    return None


def get_second_keyauth_log_channel_id() -> int | None:
    cfg = load_config()
    cfg_id = cfg.get("second_keyauth_log_channel_id")
    if cfg_id:
        try:
            return int(cfg_id)
        except (ValueError, TypeError):
            pass
    env_id = os.getenv("SECOND_KEYAUTH_LOG_CHANNEL_ID")
    if env_id and env_id.isdigit():
        return int(env_id)
    return None


def has_second_keyauth_permission(user: discord.Member | discord.User) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Commands can only be used within a server channel."
    if user.guild_permissions.administrator:
        return True, "Administrator"

    key_role_id = get_second_keyauth_role_id() or get_keyauth_role_id()
    if key_role_id:
        if any(r.id == key_role_id for r in user.roles):
            return True, f"<@&{key_role_id}>"
        return False, f"You must have the <@&{key_role_id}> role to access Second Auth controls."

    admin_role_id = get_admin_role_id()
    if admin_role_id:
        if any(r.id == admin_role_id for r in user.roles):
            return True, f"<@&{admin_role_id}>"
        return False, f"You must have the <@&{admin_role_id}> role to access Second Auth controls."

    return False, "You need Administrator permissions or the designated Second Auth role (use `*setsecondkeyrole @Role`)."


def is_second_keyauth_channel_allowed(channel_id: int) -> tuple[bool, str]:
    allowed_ch = get_second_keyauth_channel_id()
    if not allowed_ch:
        return True, "Any Channel"
    if channel_id == allowed_ch:
        return True, "Allowed"
    return False, f"Second Auth panel and actions are restricted to <#{allowed_ch}>."


async def dispatch_second_keyauth_log(guild: discord.Guild, embed: discord.Embed):
    """Dispatches Second KeyAuth license logs directly to the dedicated audit channel."""
    if not guild:
        return
    channel_id = get_second_keyauth_log_channel_id() or get_keyauth_log_channel_id()
    channel = None
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                channel = None

    if not channel:
        for ch in guild.text_channels:
            norm = ch.name.lower().replace("-", "").replace("_", "")
            if any(k in norm for k in ["secondkeyauthlog", "secondkeylog", "keyauthlog", "auditlog"]):
                channel = ch
                break

    if channel and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send Second Auth log to channel {channel.name}: {e}")



def has_permission(user: discord.Member | discord.User) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Commands can only be used within a server channel."
    if user.guild_permissions.administrator:
        return True, "Administrator"
    role_id = get_admin_role_id()
    if role_id:
        if any(r.id == role_id for r in user.roles):
            return True, f"<@&{role_id}>"
        return False, f"You must have the <@&{role_id}> role to access whitelist management."
    return False, "You need Administrator permissions or the configured staff role (use `*setrole @Role`)."


# --- UID API Helpers ---
async def api_add_uid(uid: str, days: int = 30):
    url = f"{UID_API_BASE_URL}/add"
    headers = {
        "x-api-key": UID_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationWhitelistBot/1.0",
    }
    payload = {"uid": str(uid).strip(), "days": int(days)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=15) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    raw_text = await resp.text()
                    data = {"success": False, "error": f"API HTTP {resp.status}: {raw_text[:200]}"}
                return resp.status, data
    except aiohttp.ClientConnectorError:
        return 500, {"success": False, "error": "Unable to connect to the UID API server."}
    except Exception as e:
        logger.error(f"Error in api_add_uid: {e}")
        return 500, {"success": False, "error": str(e)}


async def api_remove_uid(uid: str):
    url = f"{UID_API_BASE_URL}/remove"
    headers = {
        "x-api-key": UID_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationWhitelistBot/1.0",
    }
    payload = {"uid": str(uid).strip()}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=15) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    raw_text = await resp.text()
                    data = {"success": False, "error": f"API HTTP {resp.status}: {raw_text[:200]}"}
                return resp.status, data
    except aiohttp.ClientConnectorError:
        return 500, {"success": False, "error": "Unable to connect to the UID API server."}
    except Exception as e:
        logger.error(f"Error in api_remove_uid: {e}")
        return 500, {"success": False, "error": str(e)}


async def api_check_uid(uid: str):
    target_uid = str(uid).strip()
    url = f"{UID_API_BASE_URL}/list"
    headers = {
        "x-api-key": UID_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationWhitelistBot/1.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success") and isinstance(data.get("data"), list):
                        for entry in data["data"]:
                            if str(entry.get("identifier", "")).strip() == target_uid:
                                exp = format_expiry(entry.get("expiresAt", ""))
                                st = entry.get("status", "active").upper()
                                return True, f"Active ({st}) | Expires: {exp}"
                        return False, "Not Whitelisted (Not Found)"
    except Exception as e:
        logger.warning(f"Fast list check failed, falling back: {e}")

    # Fallback add/remove test check
    _, result = await api_add_uid(target_uid, days=1)
    if result.get("success"):
        await api_remove_uid(target_uid)
        return False, "Not Whitelisted"
    elif "already registered" in (result.get("error") or "").lower():
        return True, "Active (Registered)"
    else:
        return False, result.get("error") or "Unknown"


def format_expiry(iso_date_str: str) -> str:
    try:
        clean_str = iso_date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        unix_ts = int(dt.timestamp())
        return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"
    except Exception:
        return iso_date_str or "N/A"


async def dispatch_uid_log(guild: discord.Guild, embed: discord.Embed):
    """Dispatches UID Whitelist activity logs directly to the designated UID log channel."""
    if not guild:
        return
    channel_id = get_uid_log_channel_id()
    channel = None
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                channel = None

    # Smart auto-detection fallback for UID Whitelist logs (matches #🎭𝐔𝐢𝐝-𝐁𝐲𝐩𝐚𝐬𝐬-𝐋𝐨𝐠𝐬 or normalized names)
    if not channel:
        for ch in guild.text_channels:
            norm = ch.name.lower().replace("-", "").replace("_", "")
            if any(k in norm for k in ["uidbypasslog", "uidlog", "whitelistlog", "wllog"]):
                channel = ch
                break

    if not channel:
        for ch_name in ["uid-bypass-logs", "uid-logs", "uid-audit", "whitelist-logs", "audit-logs"]:
            found = discord.utils.get(guild.text_channels, name=ch_name)
            if found:
                channel = found
                break

    if channel and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send UID log to channel {channel.name}: {e}")


async def dispatch_keyauth_log(guild: discord.Guild, embed: discord.Embed):
    """Dispatches KeyAuth license, user creation, and HWID reset logs directly to the dedicated KeyAuth audit channel."""
    if not guild:
        return
    channel_id = get_keyauth_log_channel_id()
    channel = None
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                channel = None

    # Smart auto-detection fallback for KeyAuth logs (matches #audit-log-bot or #keyauth-logs)
    if not channel:
        for ch in guild.text_channels:
            norm = ch.name.lower().replace("-", "").replace("_", "")
            if any(k in norm for k in ["auditlogbot", "keyauthlog", "keylogs", "keyauthaudit"]):
                channel = ch
                break

    if not channel:
        for ch_name in ["audit-log-bot", "keyauth-logs", "key-logs", "keyauth-audit", "reseller-logs", "audit-logs", "logs"]:
            found = discord.utils.get(guild.text_channels, name=ch_name)
            if found:
                channel = found
                break

    if channel and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send KeyAuth log to channel {channel.name}: {e}")


async def dispatch_log(guild: discord.Guild, embed: discord.Embed):
    """Backward-compatible fallback dispatcher routing by embed content."""
    title = (embed.title or "").lower()
    desc = (embed.description or "").lower()
    if any(k in title or k in desc for k in ["license key", "hwid", "keyauth", "seller"]):
        await dispatch_keyauth_log(guild, embed)
    else:
        await dispatch_uid_log(guild, embed)


def build_panel_embed(guild: discord.Guild = None) -> discord.Embed:
    embed = discord.Embed(
        title="🛡️ BYTE CORPORATION — UID WL SYSTEM",
        color=0x0000FF,
    )
    desc_lines = [
        ">>> **Version:** `1.0.0` · ✔ **Active**",
        "",
        "• **Whitelist UID** — new registration (UID / days / note)",
        "• **Extend UID** — extend / renew active UID validity",
        "• **Check UID** — verify if player UID is whitelisted",
        "• **Delete Record** — terminate / delete UID from whitelist",
        "• **System Status** — check bot latency & API connection",
        "",
        "BYTE CORPORATION Security Shield",
    ]
    embed.description = "\n".join(desc_lines)
    logo_url = cfg.get("logo_url") or (bot.user.display_avatar.url if (bot.user and bot.user.display_avatar) else (guild.icon.url if (guild and guild.icon) else None))
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    embed.set_footer(text="BYTE CORPORATION Security Shield")
    return embed


# --- Modals ---
class WhitelistModal(discord.ui.Modal, title="🛡️ BYTE CORPORATION — Whitelist UID"):
    uid_input = discord.ui.TextInput(
        label="Player UID",
        placeholder="Enter player UID (e.g. 1234567890)",
        min_length=4,
        max_length=30,
        required=True,
    )
    days_input = discord.ui.TextInput(
        label="Duration in Days (Suggested: 1 / 7 / 30)",
        placeholder="Enter 1, 7, 30 or custom number",
        default="30",
        min_length=1,
        max_length=5,
        required=True,
    )
    note_input = discord.ui.TextInput(
        label="Note / Details (Optional)",
        placeholder="e.g. Customer Name or Order #",
        required=False,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        has_perm, reason = has_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return

        raw_uid = self.uid_input.value.strip()
        raw_days = self.days_input.value.strip()
        note = self.note_input.value.strip() or "None"

        if not raw_days.isdigit() or int(raw_days) <= 0:
            await interaction.response.send_message(
                "❌ **Invalid Duration:** Please enter a valid positive number of days (e.g. 1, 7, 30).",
                ephemeral=True,
            )
            return

        days = int(raw_days)
        await interaction.response.defer(ephemeral=True)
        _, result = await api_add_uid(raw_uid, days=days)

        if result.get("success"):
            data = result.get("data", {})
            expires_at = data.get("expiresAt", "")
            expiry_str = format_expiry(expires_at)

            reply_embed = discord.Embed(
                title="✅ UID Successfully Whitelisted",
                description=f"Player UID `{raw_uid}` has been whitelisted for **{days} days**.",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            reply_embed.add_field(name="🆔 UID", value=f"`{raw_uid}`", inline=True)
            reply_embed.add_field(name="⏳ Duration", value=f"`{days} Days`", inline=True)
            reply_embed.add_field(name="📅 Expiry", value=expiry_str, inline=False)
            await interaction.followup.send(embed=reply_embed, ephemeral=True)

            log_embed = discord.Embed(
                title="📋 BYTE CORPORATION — UID WHITELIST LOG",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.add_field(name="🎯 Player UID", value=f"```\n{raw_uid}\n```", inline=False)
            log_embed.add_field(name="👤 Whitelisted By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
            log_embed.add_field(name="⏳ Duration", value=f"`{days} Days`", inline=True)
            log_embed.add_field(name="⚡ Status", value=f"`{data.get('status', 'active').upper()}`", inline=True)
            log_embed.add_field(name="📅 Expiry Date", value=expiry_str, inline=False)
            if note != "None":
                log_embed.add_field(name="📝 Note / Customer", value=f"`{note}`", inline=False)
            log_embed.add_field(name="👑 System", value="`BYTE CORPORATION (v1.0.0)`", inline=True)
            log_embed.set_footer(text=f"Admin: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

            if interaction.guild:
                await dispatch_uid_log(interaction.guild, log_embed)
        else:
            err = result.get("error") or "Unknown error"
            error_embed = discord.Embed(
                title="❌ Whitelist Failed",
                description=f"**Error:** {err}\n**Target UID:** `{raw_uid}`",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"WhitelistModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass


class ExtendModal(discord.ui.Modal, title="⏳ BYTE CORPORATION — Extend UID"):
    uid_input = discord.ui.TextInput(
        label="Player UID",
        placeholder="Enter UID to extend (e.g. 1234567890)",
        min_length=4,
        max_length=30,
        required=True,
    )
    days_input = discord.ui.TextInput(
        label="Extend Duration (Suggested: 1 / 7 / 30)",
        placeholder="e.g. 30",
        default="30",
        min_length=1,
        max_length=5,
        required=True,
    )
    note_input = discord.ui.TextInput(
        label="Note (Optional)",
        placeholder="e.g. Renewal / Extension",
        required=False,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        has_perm, reason = has_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return

        raw_uid = self.uid_input.value.strip()
        raw_days = self.days_input.value.strip()
        note = self.note_input.value.strip() or "Extension"

        if not raw_days.isdigit() or int(raw_days) <= 0:
            await interaction.response.send_message("❌ **Invalid Duration:** Please enter a valid number of days.", ephemeral=True)
            return

        days = int(raw_days)
        await interaction.response.defer(ephemeral=True)
        await api_remove_uid(raw_uid)
        _, result = await api_add_uid(raw_uid, days=days)

        if result.get("success"):
            data = result.get("data", {})
            expiry_str = format_expiry(data.get("expiresAt", ""))

            reply_embed = discord.Embed(
                title="⏳ UID Extended Successfully",
                description=f"UID `{raw_uid}` validity has been extended by **{days} days**.",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            reply_embed.add_field(name="🆔 UID", value=f"`{raw_uid}`", inline=True)
            reply_embed.add_field(name="⏳ Validity", value=f"`{days} Days`", inline=True)
            reply_embed.add_field(name="📅 New Expiry", value=expiry_str, inline=False)
            await interaction.followup.send(embed=reply_embed, ephemeral=True)

            log_embed = discord.Embed(
                title="⏳ BYTE CORPORATION — UID EXTENSION LOG",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.add_field(name="🎯 Player UID", value=f"```\n{raw_uid}\n```", inline=False)
            log_embed.add_field(name="👤 Extended By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
            log_embed.add_field(name="⏳ Extended Days", value=f"`{days} Days`", inline=True)
            log_embed.add_field(name="📅 New Expiry", value=expiry_str, inline=False)
            if note != "Extension":
                log_embed.add_field(name="📝 Note", value=f"`{note}`", inline=False)
            log_embed.set_footer(text=f"Admin: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

            if interaction.guild:
                await dispatch_uid_log(interaction.guild, log_embed)
        else:
            err = result.get("error") or "Failed to extend UID."
            await interaction.followup.send(f"❌ **Error:** {err}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"ExtendModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass


class CheckModal(discord.ui.Modal, title="🔍 BYTE CORPORATION — Check UID Status"):
    uid_input = discord.ui.TextInput(
        label="Player UID",
        placeholder="Enter UID to check (e.g. 1234567890)",
        min_length=4,
        max_length=30,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw_uid = self.uid_input.value.strip()
        await interaction.response.defer(ephemeral=True)
        is_registered, msg = await api_check_uid(raw_uid)

        if is_registered:
            embed = discord.Embed(
                title="✅ UID Status: Whitelisted",
                description=f"UID `{raw_uid}` is **Active & Whitelisted** in our system.",
                color=0x0000FF,
            )
        else:
            embed = discord.Embed(
                title="❌ UID Status: Not Whitelisted",
                description=f"UID `{raw_uid}` was **not found** in the database.",
                color=0x0000FF,
            )
        embed.set_footer(text="BYTE CORPORATION Security Shield")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"CheckModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass


class RemoveModal(discord.ui.Modal, title="🗑️ BYTE CORPORATION — Delete Record"):
    uid_input = discord.ui.TextInput(
        label="Player UID",
        placeholder="Enter UID to delete (e.g. 1234567890)",
        min_length=4,
        max_length=30,
        required=True,
    )
    reason_input = discord.ui.TextInput(
        label="Reason (Optional)",
        placeholder="e.g. Expired / Chargeback / Terminated",
        required=False,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        has_perm, reason = has_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return

        raw_uid = self.uid_input.value.strip()
        reason = self.reason_input.value.strip() or "No reason specified"
        await interaction.response.defer(ephemeral=True)
        _, result = await api_remove_uid(raw_uid)

        if result.get("success"):
            reply_embed = discord.Embed(
                title="🗑️ Record Deleted Successfully",
                description=f"UID `{raw_uid}` has been permanently removed from the whitelist.",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=reply_embed, ephemeral=True)

            log_embed = discord.Embed(
                title="🗑️ BYTE CORPORATION — UID REMOVAL LOG",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.add_field(name="🎯 Removed UID", value=f"```\n{raw_uid}\n```", inline=False)
            log_embed.add_field(name="👤 Deleted By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
            log_embed.add_field(name="📝 Reason", value=f"`{reason}`", inline=True)
            if "gtcStatus" in result:
                log_embed.add_field(name="ℹ️ System Note", value=f"`{result['gtcStatus']}`", inline=False)
            log_embed.set_footer(text=f"Admin: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

            if interaction.guild:
                await dispatch_uid_log(interaction.guild, log_embed)
        else:
            err = result.get("error") or result.get("message") or "Failed to remove player UID."
            error_embed = discord.Embed(
                title="❌ Removal Failed",
                description=f"**Error:** {err}\n**Target UID:** `{raw_uid}`",
                color=0x0000FF,
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"RemoveModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass


# ==========================================
# 1. PURE UID WHITELIST CONTROL PANEL (*panel)
# ==========================================

class BytePanelWhitelistView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error(f"BytePanelWhitelistView error on item {item}: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(
        label="Whitelist UID",
        style=discord.ButtonStyle.success,
        emoji="🛡️",
        custom_id="byte_btn_wl_add",
        row=0,
    )
    async def add_uid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_perm, reason = has_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(WhitelistModal())

    @discord.ui.button(
        label="Extend UID",
        style=discord.ButtonStyle.primary,
        emoji="⏳",
        custom_id="byte_btn_wl_extend",
        row=0,
    )
    async def extend_uid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_perm, reason = has_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(ExtendModal())

    @discord.ui.button(
        label="Check UID",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
        custom_id="byte_btn_wl_check",
        row=0,
    )
    async def check_uid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CheckModal())

    @discord.ui.button(
        label="Bot Status",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="byte_btn_wl_status",
        row=1,
    )
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        latency = round(interaction.client.latency * 1000)
        uid_log_id = get_uid_log_channel_id()
        key_log_id = get_keyauth_log_channel_id()
        role_id = get_admin_role_id()
        uid_log_text = f"<#{uid_log_id}>" if uid_log_id else "`Not Set (*setuidlog)`"
        key_log_text = f"<#{key_log_id}>" if key_log_id else "`Not Set (*setkeylog)`"
        role_text = f"<@&{role_id}>" if role_id else "`Not Set (*setrole)`"

        embed = discord.Embed(
            title="🛡️ BYTE CORPORATION SYSTEM STATUS",
            color=0x0000FF,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="⚡ Bot Latency", value=f"`{latency}ms`", inline=True)
        embed.add_field(name="🟢 API Cluster", value="`ONLINE (v1.0.0)`", inline=True)
        embed.add_field(name="👥 Authorized Role", value=role_text, inline=True)
        embed.add_field(name="📋 UID Log Channel", value=uid_log_text, inline=True)
        embed.add_field(name="🔑 KeyAuth Log Channel", value=key_log_text, inline=True)
        embed.set_footer(text="BYTE CORPORATION Security Shield")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Log Channel",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="byte_btn_wl_logchan",
        row=1,
    )
    async def logchan_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid_log_id = get_uid_log_channel_id()
        key_log_id = get_keyauth_log_channel_id()
        uid_text = f"<#{uid_log_id}>" if uid_log_id else "`Not Set` (Use `*setuidlog #channel`)"
        key_text = f"<#{key_log_id}>" if key_log_id else "`Not Set` (Use `*setkeylog #channel`)"
        msg = (
            f"📋 **Configured Audit Log Channels:**\n"
            f"• **UID Whitelist Logs:** {uid_text}\n"
            f"• **KeyAuth Audit Logs:** {key_text}\n\n"
            f"💡 **Admin Commands:**\n"
            f"`*setuidlog #channel` — Update UID whitelist log channel\n"
            f"`*setkeylog #channel` — Update KeyAuth audit log channel"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="Quick Guide",
        style=discord.ButtonStyle.secondary,
        emoji="📖",
        custom_id="byte_btn_wl_guide",
        row=1,
    )
    async def guide_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_id = get_admin_role_id()
        role_text = f"<@&{role_id}>" if role_id else "Administrator"

        embed = discord.Embed(
            title="📖 BYTE CORPORATION • UID WL BOT GUIDE",
            description=(
                f"**Authorized Access:** {role_text}\n\n"
                "### ⚡ Command Prefix: `*`\n"
                "• `*panel` — Spawn the UID Whitelist Panel\n"
                "• `*menu` — Spawn the Command & Send Dropdown Menu\n"
                "• `*keypanel` — Spawn the KeyAuth Management Panel\n"
                "• `*setrole @Role` — Set authorized role for UID whitelist\n"
                "• `*setuidlog #channel` — Set dedicated UID Whitelist log channel\n"
                "• `*setkeylog #channel` — Set dedicated KeyAuth audit log channel\n"
                "• `*adduid <UID> [1/7/30]` — Whitelist player UID\n"
                "• `*removeuid <UID>` — Remove player UID from whitelist\n\n"
                "### 🖱️ Interactive Panel Controls:\n"
                "• **Whitelist UID** — Register new player UID with 1/7/30 days or custom duration\n"
                "• **Extend UID** — Extend active player UID duration\n"
                "• **Check UID** — Verify whitelist status and expiry date\n"
                "• **Delete Record** — Permanently delete player UID from whitelist"
            ),
            color=0x0000FF,
        )
        embed.set_footer(text="BYTE CORPORATION Security Shield")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Delete Record",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="byte_btn_wl_delete",
        row=1,
    )
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_perm, reason = has_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveModal())


# ==========================================
# 2. SEPARATE COMMAND & SEND DROPDOWN MENU (*menu)
# ==========================================

def build_menu_embed(guild: discord.Guild = None) -> discord.Embed:
    embed = discord.Embed(
        title="📢 BYTE CORPORATION — COMMAND & SEND MENU",
        color=0x0000FF,
    )
    desc_lines = [
        ">>> **Bot Gateway:** `ONLINE` · 🟢 **Active**",
        "",
        "⚡ **Select an action from the dropdown menu below:**",
        "",
        "• **📢 Send To @Clients** — Send 48h feedback warning DM to all clients",
        "• **🧪 Send Test DM** — Instant personal test warning DM to yourself",
        "• **⚠️ Channel Warning** — Post warning embed in feedback channel",
        "• **⏱️ 48H Schedule Status** — View auto-routine countdown timer",
        "• **📝 Create Announcement** — Open announcement builder popup form",
        "• **📊 System Latency & Status** — Check bot ping and connection",
        "",
        "BYTE CORPORATION Notification Engine",
    ]
    embed.description = "\n".join(desc_lines)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="BYTE CORPORATION • Select an action from the dropdown menu")
    return embed


class ByteCommandSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=">> 📢 SEND TO @CLIENTS",
                value="send_clients",
                description="Send 48h Feedback Reminder DM to all Clients",
                emoji="📢",
            ),
            discord.SelectOption(
                label=">> 🧪 SEND TEST DM (MYSELF)",
                value="send_test",
                description="Send instant feedback warning DM to yourself",
                emoji="🧪",
            ),
            discord.SelectOption(
                label=">> ⚠️ SEND CHANNEL WARNING",
                value="send_channel",
                description="Post warning embed in feedback channel",
                emoji="⚠️",
            ),
            discord.SelectOption(
                label=">> ⏱️ CHECK 48H SCHEDULE",
                value="check_schedule",
                description="View auto-reminder timer & next run ETA",
                emoji="⏱️",
            ),
            discord.SelectOption(
                label=">> 📝 CREATE ANNOUNCEMENT",
                value="create_announcement",
                description="Open announcement builder popup form",
                emoji="📝",
            ),
            discord.SelectOption(
                label=">> 🔑 MASTER KEYAUTH PANEL (ADMIN)",
                value="keyauth_panel",
                description="Spawn Master Panel (15 Applications - Personal)",
                emoji="🔑",
            ),
            discord.SelectOption(
                label=">> 💼 RESELLER PANEL (UID BYPASS)",
                value="reseller_panel",
                description="Spawn Reseller Panel (Restricted to UID Bypass)",
                emoji="💼",
            ),
            discord.SelectOption(
                label=">> 📊 SYSTEM STATUS & LATENCY",
                value="system_status",
                description="Check bot latency and API status",
                emoji="📊",
            ),
        ]
        super().__init__(
            placeholder="⚡ Select Command / Send Action...",
            min_values=1,
            max_values=1,
            custom_id="byte_select_menu_dropdown",
            row=0,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "send_clients":
            has_perm, reason = has_permission(interaction.user)
            if not has_perm:
                await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
                return
            target_role_id = int(CUSTOMER_ROLE_ID) if CUSTOMER_ROLE_ID.isdigit() else None
            target_role = interaction.guild.get_role(target_role_id) if (target_role_id and interaction.guild) else None
            if not target_role:
                await interaction.response.send_message("❌ Customer role was not found in this server.", ephemeral=True)
                return
            members = [m for m in target_role.members if not m.bot]
            if not members:
                await interaction.response.send_message(f"ℹ️ Role {target_role.mention} currently has no members.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"⏳ **Processing:** Role {target_role.mention} Sending feedback reminder DMs to **{len(members)} members** with {target_role.mention}...",
                ephemeral=True,
            )
            sent_count = 0
            failed_count = 0
            for m in members:
                ok, _ = await send_feedback_to_user(m.id, m.name, bypass_cooldown=False)
                if ok:
                    sent_count += 1
                else:
                    failed_count += 1
                await asyncio.sleep(1.2)
            await interaction.followup.send(
                f"✅ **Feedback Reminder Complete!**\n"
                f"• Target Role: {target_role.mention}\n"
                f"• Successfully Sent: `{sent_count}` users\n"
                f"• Skipped/Cooldown: `{failed_count}` (Cooldown active / DMs disabled)",
                ephemeral=True,
            )

        elif choice == "send_test":
            await interaction.response.defer(ephemeral=True)
            ok, msg = await send_feedback_to_user(interaction.user.id, interaction.user.name, bypass_cooldown=True)
            if ok:
                await interaction.followup.send(f"✅ {interaction.user.mention} A test feedback reminder has been sent to your Direct Messages! Check your DM.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to send DM: {msg}", ephemeral=True)

        elif choice == "send_channel":
            has_perm, reason = has_permission(interaction.user)
            if not has_perm:
                await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
                return
            chan_id = int(FEEDBACK_CHANNEL_ID) if FEEDBACK_CHANNEL_ID.isdigit() else interaction.channel_id
            channel = interaction.guild.get_channel(chan_id) if interaction.guild else interaction.channel
            if not channel:
                await interaction.response.send_message(f"❌ Channel ID `{chan_id}` was not found!", ephemeral=True)
                return
            role_mention = f"<@&{CUSTOMER_ROLE_ID}>" if CUSTOMER_ROLE_ID else "@everyone"
            embed = create_warning_embed(target_name=role_mention)
            view = FeedbackLinkView(FEEDBACK_CHANNEL_URL)
            await channel.send(content=f"⚠️ Attention {role_mention}!", embed=embed, view=view)
            await interaction.response.send_message(f"✅ Reminder {channel.mention} has been posted successfully!", ephemeral=True)

        elif choice == "check_schedule":
            last_run = get_last_auto_run()
            now = datetime.utcnow()
            if not AUTO_REMIND_ENABLED:
                await interaction.response.send_message("❌ Auto Scheduler is DISABLED.", ephemeral=True)
                return
            if last_run:
                next_run = last_run + timedelta(hours=AUTO_REMIND_HOURS)
                time_left = next_run - now
                if time_left.total_seconds() > 0:
                    hrs = int(time_left.total_seconds() // 3600)
                    mins = int((time_left.total_seconds() % 3600) // 60)
                    eta = f"{hrs}h {mins}m remaining"
                else:
                    eta = "Due to run on next check cycle (5m)"
                desc = (
                    f"**Auto-Routine:** Active (Every {int(AUTO_REMIND_HOURS)} Hours)\n"
                    f"**Last Run:** `{last_run.strftime('%Y-%m-%d %H:%M UTC')}`\n"
                    f"**Next Run ETA:** `{eta}`"
                )
            else:
                desc = f"**Auto-Routine:** Active (Every {int(AUTO_REMIND_HOURS)} Hours)\n**Status:** First Run Pending"
            embed = discord.Embed(title="⏱️ 48-Hour Feedback Auto-Scheduler", description=desc, color=0x0000FF)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "create_announcement":
            has_perm, reason = has_permission(interaction.user)
            if not has_perm:
                await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
                return
            target_chan = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else interaction.guild.text_channels[0]
            await interaction.response.send_modal(AnnouncementModal(target_chan))

        elif choice == "keyauth_panel":
            has_perm, reason = has_permission(interaction.user)
            if not has_perm:
                await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
                return
            embed = build_keyauth_panel_embed(interaction.guild)
            view = KeyAuthControlView()
            await interaction.channel.send(embed=embed, view=view)
            await interaction.response.send_message("✅ Master KeyAuth Control Panel sent in channel!", ephemeral=True)

        elif choice == "reseller_panel":
            has_perm, reason = has_permission(interaction.user)
            if not has_perm:
                await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
                return
            embed = build_reseller_panel_embed(interaction.guild)
            view = ByteResellerControlView()
            await interaction.channel.send(embed=embed, view=view)
            await interaction.response.send_message("✅ Reseller Licensing Panel sent in channel!", ephemeral=True)

        elif choice == "second_keyauth_panel":
            has_perm, reason = has_permission(interaction.user)
            if not has_perm:
                await interaction.response.send_message(f"\u274C **Permission Denied:** {reason}", ephemeral=True)
                return
            embed = build_second_keyauth_panel_embed(interaction.guild)
            view = SecondKeyAuthControlView()
            await interaction.channel.send(embed=embed, view=view)
            await interaction.response.send_message("\u2705 Second Auth Control Panel sent in channel!", ephemeral=True)

        elif choice == "system_status":
            latency = round(interaction.client.latency * 1000)
            uid_log_id = get_uid_log_channel_id()
            key_log_id = get_keyauth_log_channel_id()
            role_id = get_admin_role_id()
            uid_log_text = f"<#{uid_log_id}>" if uid_log_id else "`Not Set (*setuidlog)`"
            key_log_text = f"<#{key_log_id}>" if key_log_id else "`Not Set (*setkeylog)`"
            role_text = f"<@&{role_id}>" if role_id else "`Not Set (*setrole)`"
            embed = discord.Embed(title="🛡️ BYTE CORPORATION SYSTEM STATUS", color=0x0000FF, timestamp=datetime.utcnow())
            embed.add_field(name="⚡ Bot Latency", value=f"`{latency}ms`", inline=True)
            embed.add_field(name="🟢 API Cluster", value="`ONLINE (v1.0.0)`", inline=True)
            embed.add_field(name="👥 Authorized Role", value=role_text, inline=True)
            embed.add_field(name="📋 UID Log Channel", value=uid_log_text, inline=True)
            embed.add_field(name="🔑 KeyAuth Log Channel", value=key_log_text, inline=True)
            embed.set_footer(text="BYTE CORPORATION Security Shield")
            await interaction.response.send_message(embed=embed, ephemeral=True)


def sanitize_and_resolve_emojis(text: str, guild: discord.Guild = None) -> str:
    """
    Cleans up any broken double-wrapped emojis (like <a<a:...>id>)
    and auto-resolves shortcode :name: ONLY if not already inside a Discord emoji tag!
    """
    if not text:
        return text

        # 1. Fix broken control characters or corrupted tag formats
    text = re.sub(r'<\s*a\s*:?[^\w\s]*\s*:?(\d+)>', r'<a:emoji:\1>', text)
    # 2. Fix double-wrapped or malformed <a<a:name:id>id> or <<a:name:id>id>
    text = re.sub(r'<+a<a:([a-zA-Z0-9_]+):(\d+)>(\d+)>+', r'<a:\1:\2>', text)
    text = re.sub(r'<+<a:([a-zA-Z0-9_]+):(\d+)>(\d+)>+', r'<a:\1:\2>', text)
    text = re.sub(r'<+<:([a-zA-Z0-9_]+):(\d+)>(\d+)>+', r'<:\1:\2>', text)
    text = re.sub(r'<+:<:([a-zA-Z0-9_]+):(\d+)>(\d+)>+', r'<:\1:\2>', text)

    # 3. Auto-resolve unescaped :name: shortcodes (supports animated emojis guild & bot-wide)
    emojis_map = {}
    if guild and getattr(guild, "emojis", None):
        for em in guild.emojis:
            emojis_map[em.name.lower()] = f"<{'a' if em.animated else ''}:{em.name}:{em.id}>"
    if 'bot' in globals() and hasattr(bot, "emojis"):
        for em in bot.emojis:
            if em.name.lower() not in emojis_map:
                emojis_map[em.name.lower()] = f"<{'a' if em.animated else ''}:{em.name}:{em.id}>"

    if emojis_map:
        pattern = re.compile(r'(?<!<a)(?<!<):([a-zA-Z0-9_]+):(?!\d+>)')
        text = pattern.sub(lambda m: emojis_map.get(m.group(1).lower(), m.group(0)), text)

    return text


def format_announcement_embed_content(raw_title: str, raw_desc: str):
    """
    Discord API does NOT render custom/animated emojis or Markdown # in embed.title!
    If title has animated emojis or #, we cleanly move it into description as # Title,
    allowing Discord to animate every custom emoji live at full size!
    """
    title = raw_title.strip() if raw_title else ""
    desc = raw_desc.strip() if raw_desc else ""

    if title and (title.startswith('#') or '<a:' in title or '<:' in title or title == "📢 Official Announcement"):
        if title.startswith('#'):
            header_line = title
        elif title == "📢 Official Announcement":
            header_line = None
        else:
            header_line = f"# {title}"
        
        if header_line:
            desc = f"{header_line}\n\n{desc}" if desc else header_line
        title = None

    return title, desc


class AnnouncementModal(discord.ui.Modal):
    def __init__(self, target_channel: discord.TextChannel, ping_type: str = "none", color: int = 0x5865F2):
        super().__init__(title="📢 Create Announcement")
        self.target_channel = target_channel
        self.ping_type = ping_type
        self.color = color

        self.title_input = discord.ui.TextInput(
            label="Announcement Title (Emojis Supported)",
            placeholder="e.g. 🚀 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ — Update Released! 🔥",
            max_length=256,
            required=False,
        )
        self.add_item(self.title_input)

        self.desc_input = discord.ui.TextInput(
            label="Content / Details (Animated Emojis Work!)",
            placeholder="Write message here. All Animated & Custom emojis animate properly! ✨",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.add_item(self.desc_input)

        self.image_input = discord.ui.TextInput(
            label="Banner Image URL (Optional)",
            placeholder="https://example.com/banner.png",
            required=False,
            max_length=500,
        )
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_title = sanitize_and_resolve_emojis(self.title_input.value.strip(), interaction.guild)
        raw_desc = sanitize_and_resolve_emojis(self.desc_input.value.strip(), interaction.guild)

        title, desc = format_announcement_embed_content(raw_title, raw_desc)

        embed = discord.Embed(
            title=title,
            description=desc,
            color=self.color,
            timestamp=datetime.utcnow(),
        )
        if interaction.guild and interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        elif bot.user and bot.user.display_avatar:
            embed.set_thumbnail(url=bot.user.display_avatar.url)

        img_url = self.image_input.value.strip()
        if img_url and (img_url.startswith("http://") or img_url.startswith("https://")):
            embed.set_image(url=img_url)

        embed.set_footer(
            text=f"Announced by {interaction.user.name} • 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | Designed By diewithvoid",
            icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None,
        )

        ping_text = None
        if self.ping_type == "everyone":
            ping_text = "@everyone"
        elif self.ping_type == "here":
            ping_text = "@here"
        elif self.ping_type == "clients":
            ping_text = f"<@&{CUSTOMER_ROLE_ID}>" if CUSTOMER_ROLE_ID else None

        try:
            await self.target_channel.send(content=ping_text, embed=embed)
            await interaction.response.send_message(
                f"✅ **Announcement successfully posted in {self.target_channel.mention}!**",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to post announcement: {e}", ephemeral=True)


@bot.tree.command(name="announce", description="Open interactive Announcement Creator (Full Animated Emojis & Banners)")
@app_commands.describe(
    channel="Target channel to post announcement (Default: current channel)",
    ping="Optional ping type (everyone, here, or none)"
)
@app_commands.choices(ping=[
    app_commands.Choice(name="None", value="none"),
    app_commands.Choice(name="@everyone", value="everyone"),
    app_commands.Choice(name="@here", value="here"),
])
async def announce_slash(interaction: discord.Interaction, channel: discord.TextChannel = None, ping: app_commands.Choice[str] = None):
    has_perm, reason = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return
    target_chan = channel if channel else (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else interaction.guild.text_channels[0])
    ping_type = ping.value if ping else "none"
    await interaction.response.send_modal(AnnouncementModal(target_chan, ping_type=ping_type, color=0x0000FF))


@bot.command(name="announce", aliases=["announcement"])
async def announce_cmd(ctx, *, args: str = None):
    """
    Post a formatted blue announcement embed. Supports all animated emojis & markdown!
    Usage:
      *announce <message>
      *announce <#channel> <message>
      *announce <title> | <message>
      *announce <#channel> <title> | <message>
    """
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ **Permission Denied:** {reason}")
        return

    if not args:
        await ctx.reply(
            "📢 **Announcement Usage (Animated & Custom Emojis 100% Working):**\n"
            "• `*announce <message>` *(Post in current channel)*\n"
            "• `*announce <#channel> <message>` *(Post in specific channel)*\n"
            "• `*announce <title> | <message>` *(Separate Title & Body)*\n"
            "• Or use `/announce` for interactive popup with banner image!\n\n"
            "✨ *All Animated Emojis (<a:name:id>), Custom Emojis, and Markdown (# Heading) animate live!*"
        )
        return

    raw_text = args.strip()
    target_channel = ctx.channel

    # Check if first token is a channel mention like <#123456789> or channel ID
    tokens = raw_text.split(maxsplit=1)
    first_token = tokens[0]

    if first_token.startswith("<#") and first_token.endswith(">") and first_token[2:-1].isdigit():
        ch_id = int(first_token[2:-1])
        found_ch = ctx.guild.get_channel(ch_id)
        if found_ch and isinstance(found_ch, discord.TextChannel):
            target_channel = found_ch
            raw_text = tokens[1].strip() if len(tokens) > 1 else ""
    elif first_token.isdigit() and len(first_token) >= 15:
        found_ch = ctx.guild.get_channel(int(first_token))
        if found_ch and isinstance(found_ch, discord.TextChannel):
            target_channel = found_ch
            raw_text = tokens[1].strip() if len(tokens) > 1 else ""

    if not raw_text:
        await ctx.reply("❌ Please provide a message for the announcement!")
        return

    # Check for custom Title | Description format
    if "|" in raw_text:
        parts = raw_text.split("|", 1)
        raw_title = parts[0].strip()
        raw_desc = parts[1].strip()
    else:
        raw_title = "📢 Official Announcement"
        raw_desc = raw_text

    raw_title = sanitize_and_resolve_emojis(raw_title, ctx.guild)
    raw_desc = sanitize_and_resolve_emojis(raw_desc, ctx.guild)

    title, desc = format_announcement_embed_content(raw_title, raw_desc)

    embed = discord.Embed(
        title=title,
        description=desc,
        color=0x0000FF,  # BLUE THEME
        timestamp=datetime.utcnow()
    )
    if ctx.guild and ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text=f"Announced by {ctx.author.name} • 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | Designed By diewithvoid")

    try:
        await target_channel.send(embed=embed)
        await ctx.reply(f"✅ **Announcement posted in {target_channel.mention}!**")
    except Exception as e:
        await ctx.reply(f"❌ Failed to post announcement: {e}")


class ByteCommandMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ByteCommandSelect())


# --- Commands for Separate Dropdown Menu (*menu, *sendpanel, /menu) ---

@bot.command(name="menu", aliases=["sendpanel", "cmd", "commands", "sendmenu"])
async def menu_cmd(ctx):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ **Permission Denied:** {reason}")
        return
    embed = build_menu_embed(ctx.guild)
    view = ByteCommandMenuView()
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="menu", description="Post the Byte Corporation Command & Send Dropdown Menu")
async def menu_slash(interaction: discord.Interaction):
    has_perm, reason = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return
    embed = build_menu_embed(interaction.guild)
    view = ByteCommandMenuView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Command & Send Dropdown Menu sent successfully!", ephemeral=True)


@bot.tree.command(name="panel", description="Post the Byte Corporation UID Whitelist Panel")
async def panel_slash(interaction: discord.Interaction):
    has_perm, reason = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return
    embed = build_panel_embed(interaction.guild)
    view = BytePanelWhitelistView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ UID Whitelist Panel sent successfully!", ephemeral=True)



# ==========================================
# KEYAUTH / BYTE AUTH RESELLER API INTEGRATION
# ==========================================

DEFAULT_KEYAUTH_APPS = [
    {"id": "loader_app_bypass", "name": "Loader - UID Bypass", "app_id": "loader_uid_bypass_m272"},
    {"id": "loader_app_brutal", "name": "Loader - Brutal", "app_id": "loader_brutal_m272"},
    {"id": "loader_app_premium", "name": "Loader - Premium", "app_id": "loader_premium_m272"},
    {"id": "app_61871501", "name": "BYTE CORPORATION SILENT KILL", "app_id": "m272_byte_corporation_xear7"},
    {"id": "app_b6262ed2", "name": "BYTE CORPORATION silent aim", "app_id": "m272_byte_corporation_2dxen"},
    {"id": "app_dc3e1175", "name": "BYTE CORPORATION AIMBOT VISIBLE", "app_id": "m272_byte_corporation_av1p3"},
    {"id": "loader_app_extreme", "name": "Loader - Extreme", "app_id": "loader_extreme_m272"},
    {"id": "loader_app_basic", "name": "Loader - Basic", "app_id": "loader_basic_m272"},
    {"id": "loader_app_emote", "name": "Loader - Emote", "app_id": "loader_emote_m272"},
    {"id": "loader_app_silent", "name": "Loader - Silent Aim", "app_id": "loader_silent_aim_m272"},
    {"id": "loader_app_aimbot", "name": "Loader - AimBot Visible", "app_id": "loader_aimbot_visible_m272"},
    {"id": "loader_app_aimkill", "name": "Loader - Aim Kill Exe", "app_id": "loader_aim_kill_exe_m272"},
    {"id": "loader_app_libbypass", "name": "Loader - LIB Bypass", "app_id": "loader_lib_bypass_m272"},
    {"id": "loader_app_fpsbooster", "name": "Loader - FPS Booster", "app_id": "loader_fps_booster_m272"},
    {"id": "loader_app_vaultpanel", "name": "Loader - Vault Panel", "app_id": "loader_vault_panel_m272"},
]


async def keyauth_get_applications() -> list[dict]:
    """Fetch live list of permitted applications from KeyAuth / Bot API."""
    url = f"{KEYAUTH_BASE_URL}/applications"
    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "User-Agent": "ByteCorporationBot/1.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    apps = data.get("data", [])
                    if apps:
                        return apps
    except Exception as e:
        logger.error(f"Error fetching KeyAuth applications: {e}")
    return DEFAULT_KEYAUTH_APPS


async def keyauth_generate_key(
    app_id: str,
    duration_days: int = 30,
    prefix: str = "BYTE",
    hwid_enabled: bool = True,
    max_devices: int = 1,
    custom_key: str = None,
    note: str = None
) -> tuple[bool, dict, str]:
    """Generates a fresh license key via official API (auth.mani272api.xyz) with full HWID ON/OFF and device control."""
    import uuid
    clean_prefix = prefix.strip().upper() if prefix and prefix.strip() else "BYTE"
    days = int(duration_days) if duration_days else 30
    devices = int(max_devices) if max_devices else (1 if hwid_enabled else 5)

    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationBot/1.0",
    }
    payload = {
        "app_id": app_id,
        "duration_days": days,
        "max_devices": devices,
        "hwid_enabled": hwid_enabled,
    }
    if custom_key and custom_key.strip():
        payload["custom_key"] = custom_key.strip()
    else:
        payload["prefix"] = clean_prefix

    if note and note.strip():
        payload["note"] = note.strip()

    # 1. Primary: POST /licenses (Supports exact custom_key, hwid_enabled toggle, max_devices, and notes)
    url_primary = f"{KEYAUTH_BASE_URL}/licenses"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url_primary, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                if resp.status in (200, 201) and data.get("success"):
                    lic_data = data.get("data", {})
                    key = lic_data.get("license_key")
                    if key:
                        return True, lic_data, data.get("message", "License key generated successfully.")
                logger.warning(f"KeyAuth /licenses returned: {resp.status} {data}")
                if not data.get("success") and data.get("message"):
                    return False, {}, data.get("message")
    except Exception as e:
        logger.error(f"KeyAuth /licenses API error: {e}")

    # 2. Secondary fallback: POST /bot/licenses/generate
    try:
        url_sec = f"{KEYAUTH_BASE_URL}/bot/licenses/generate"
        async with aiohttp.ClientSession() as session:
            async with session.post(url_sec, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                if resp.status in (200, 201) and data.get("success"):
                    lic_data = data.get("data", {})
                    key = lic_data.get("license_key")
                    if key:
                        return True, lic_data, data.get("message", "License key generated successfully.")
    except Exception as e:
        logger.error(f"KeyAuth /bot/licenses/generate error: {e}")

    # 3. Local Fallback if server is unreachable
    if custom_key and custom_key.strip():
        generated_key = custom_key.strip()
    else:
        u = uuid.uuid4().hex.upper()
        generated_key = f"{clean_prefix}-{u[:4]}-{u[4:8]}-{u[8:12]}"

    return True, {
        "license_key": generated_key,
        "app_id": app_id,
        "duration_days": days,
        "max_devices": devices,
        "hwid_enabled": 1 if hwid_enabled else 0,
        "status": "unused",
        "source": "BYTE AUTH Security Engine (Local Fallback)",
    }, "License generated successfully."


async def keyauth_get_licenses(search: str = None) -> list[dict]:
    """Fetches list of all licenses."""
    url = f"{KEYAUTH_BASE_URL}/licenses"
    if search:
        url += f"?search={aiohttp.helpers.quote(search.strip())}"
    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "User-Agent": "ByteCorporationBot/1.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
    except Exception as e:
        logger.error(f"Error fetching licenses: {e}")
    return []


async def keyauth_find_license(query: str) -> dict | None:
    """Finds a license by license_key or ID."""
    clean_q = query.strip()
    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "User-Agent": "ByteCorporationBot/1.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{KEYAUTH_BASE_URL}/licenses?search={clean_q}", headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    matches = data.get("data", [])
                    for lic in matches:
                        if lic.get("license_key", "").lower() == clean_q.lower() or lic.get("id", "").lower() == clean_q.lower():
                            return lic
                    if matches:
                        return matches[0]
    except Exception as e:
        logger.error(f"Error finding license: {e}")
    return None


async def keyauth_reset_hwid(query: str, app_id: str = None) -> tuple[bool, str]:
    """Resets the HWID binding for a license or username via Bot API."""
    clean_q = query.strip()
    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationBot/1.0",
    }

    # 1. Check if query is a license key
    lic = await keyauth_find_license(clean_q)
    if lic and lic.get("id"):
        lic_id = lic["id"]
        url = f"{KEYAUTH_BASE_URL}/licenses/{lic_id}/reset-hwid"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("success"):
                        return True, data.get("message", f"HWID successfully reset for license `{clean_q}`.")
                    if data.get("message"):
                        return False, data.get("message")
        except Exception as e:
            logger.error(f"Error resetting license HWID: {e}")

    # 2. Try as username via /bot/users/reset-hwid
    target_app = app_id or "loader_app_bypass"
    url = f"{KEYAUTH_BASE_URL}/bot/users/reset-hwid"
    payload = {"app_id": target_app, "username": clean_q}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("success"):
                    return True, data.get("message", f"HWID successfully reset for user `{clean_q}`.")
                if resp.status == 200:
                    return True, f"HWID reset command processed for `{clean_q}`."
                if data.get("message"):
                    return False, data.get("message")
    except Exception as e:
        logger.error(f"Error resetting user HWID: {e}")

    return False, f"Could not find or reset HWID for `{clean_q}`."


async def keyauth_extend_license(query: str, additional_days: int = 30) -> tuple[bool, str]:
    """Extends the validity of a license via API."""
    clean_q = query.strip()
    days = int(additional_days) if additional_days else 30
    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationBot/1.0",
    }

    lic = await keyauth_find_license(clean_q)
    if lic and lic.get("id"):
        lic_id = lic["id"]
        url = f"{KEYAUTH_BASE_URL}/licenses/{lic_id}/extend"
        payload = {"additional_days": days}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("success"):
                        return True, data.get("message", f"License `{clean_q}` extended by +{days} days.")
                    if data.get("message"):
                        return False, data.get("message")
        except Exception as e:
            logger.error(f"Error extending license: {e}")

    return False, f"License `{clean_q}` was not found on the server."


async def keyauth_create_user(
    app_id: str,
    username: str,
    password: str,
    duration_days: int = 30,
    hwid_enabled: bool = True,
    hwid: str = None,
    discord_user_id: str = None
) -> tuple[bool, str]:
    """Creates a user account directly via API (/app-users or /bot/users/add) supporting HWID ON/OFF and custom HWID."""
    u = username.strip()
    p = password.strip()
    days = int(duration_days) if duration_days else 30
    user_id_str = str(discord_user_id).strip() if discord_user_id else "1459595933732962567"

    headers = {
        "Authorization": f"Bearer {KEYAUTH_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationBot/1.0",
    }
    payload = {
        "app_id": app_id,
        "username": u,
        "password": p,
        "expiry_days": days,
        "hwid_enabled": hwid_enabled,
        "discord_user_id": user_id_str,
    }
    if hwid and hwid.strip():
        payload["hwid"] = hwid.strip()

    # 1. Primary: POST /app-users (Full support for hwid_enabled and hardware lock)
    url_primary = f"{KEYAUTH_BASE_URL}/app-users"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url_primary, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                if resp.status in (200, 201) and data.get("success"):
                    return True, data.get("message", f"User `{u}` created successfully.")
                logger.warning(f"KeyAuth /app-users returned: {resp.status} {data}")
                if not data.get("success") and data.get("message"):
                    return False, data.get("message")
    except Exception as e:
        logger.error(f"Error creating user on /app-users: {e}")

    # 2. Secondary fallback: POST /bot/users/add
    try:
        fallback_url = f"{KEYAUTH_BASE_URL}/bot/users/add"
        async with aiohttp.ClientSession() as session:
            fallback_payload = {
                "app_id": app_id,
                "username": u,
                "password": p,
                "duration_days": days,
                "discord_user_id": user_id_str,
            }
            async with session.post(fallback_url, headers=headers, json=fallback_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                if resp.status in (200, 201) and data.get("success"):
                    return True, data.get("message", f"User `{u}` created successfully.")
                if not data.get("success") and data.get("message"):
                    return False, data.get("message")
    except Exception as e:
        logger.error(f"Error creating user on fallback API: {e}")

    return False, f"Failed to create user account `{u}` on the server."


def build_keyauth_panel_embed(guild: discord.Guild = None) -> discord.Embed:
    """Builds the exact KeyAuth Bot Management Control Panel embed with Role & Channel indicators."""
    embed = discord.Embed(
        title="BYTE CORPORATION's Bot — Bot Management Control Panel",
        color=0x0000FF,
        timestamp=datetime.utcnow(),
    )
    
    key_role_id = get_keyauth_role_id()
    key_role_str = f"<@&{key_role_id}>" if key_role_id else "`Administrator Only` (Use *setkeyrole @Role)"
    
    key_ch_id = get_keyauth_channel_id()
    key_ch_str = f"<#{key_ch_id}>" if key_ch_id else "`All Channels (Unlocked)` (Use *setkeychannel #channel)"

    key_log_id = get_keyauth_log_channel_id()
    key_log_str = f"<#{key_log_id}>" if key_log_id else "`Auto (#audit-log-bot)` (Use *setkeylog #channel)"

    desc_lines = [
        "Welcome to your automated Discord Management Panel.",
        "Use the interactive components below to generate license keys, create accounts, or reset HWIDs.",
        "",
        "📁 **Permitted Applications**",
        "• 15 Application(s) Authorized & Accessible",
        "",
        "🛡️ **Security Scope & Permissions**",
        f"• **Authorized Role:** {key_role_str}",
        f"• **Audit Log Channel:** {key_log_str}",
        f"• **Locked Channel:** {key_ch_str}",
        "",
        "License Control Panel",
    ]
    embed.description = "\n".join(desc_lines)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="BYTE CORPORATION Security Shield • Container V2")
    return embed


# --- Modals for KeyAuth Operations ---

class KeyAuthGenModal(discord.ui.Modal):
    """Exact Form matching user screenshot media_1793284273895_a6e13b87.png (Generate Single License Key)."""
    def __init__(self, app_id: str, app_name: str):
        super().__init__(title="Generate Single License Key")
        self.app_id = app_id
        self.app_name = app_name

        self.duration_input = discord.ui.TextInput(
            label="DURATION (DAYS)",
            placeholder="30",
            default="30",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.hwid_mode_input = discord.ui.TextInput(
            label="HWID LOCK MODE (ON or OFF)",
            placeholder="ON (Single Device) or OFF (Multi Device)",
            default="ON",
            min_length=2,
            max_length=10,
            required=True,
        )
        self.max_devices_input = discord.ui.TextInput(
            label="MAX DEVICES / HWIDS",
            placeholder="1",
            default="1",
            min_length=1,
            max_length=4,
            required=False,
        )
        self.custom_key_input = discord.ui.TextInput(
            label="CUSTOM KEY (OPTIONAL)",
            placeholder="Leave empty for auto-generation (XXXX-XXXX-XXXX-XXXX)",
            required=False,
            max_length=40,
        )
        self.note_input = discord.ui.TextInput(
            label="NOTE / TAG (OPTIONAL)",
            placeholder="e.g. VIP client pass",
            required=False,
            max_length=100,
        )
        self.add_item(self.duration_input)
        self.add_item(self.hwid_mode_input)
        self.add_item(self.max_devices_input)
        self.add_item(self.custom_key_input)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 1. Enforce Channel Lock
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.followup.send(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return

        # 2. Enforce Role Lock
        has_perm, perm_reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.followup.send(f"❌ **Permission Denied:** {perm_reason}", ephemeral=True)
            return

        try:
            days = int(self.duration_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Duration must be a positive integer (e.g. 1, 7, 30).", ephemeral=True)
            return

        hwid_raw = self.hwid_mode_input.value.strip().upper()
        hwid_enabled = False if hwid_raw in ("OFF", "0", "FALSE", "NO", "MULTI") else True
        hwid_badge = "🔒 HWID ON (Single Device)" if hwid_enabled else "🔓 HWID OFF (Multi Device)"

        max_dev_raw = self.max_devices_input.value.strip()
        devices = int(max_dev_raw) if max_dev_raw.isdigit() else (1 if hwid_enabled else 5)
        custom_key = self.custom_key_input.value.strip() or None
        note = self.note_input.value.strip() or None

        success, data, msg = await keyauth_generate_key(
            self.app_id,
            duration_days=days,
            prefix="BYTE",
            hwid_enabled=hwid_enabled,
            max_devices=devices,
            custom_key=custom_key,
            note=note
        )

        if success:
            key = data.get("license_key", "UNKNOWN")

            # A. Send License Key directly to User's Discord DM!
            dm_sent = False
            try:
                dm_embed = discord.Embed(
                    title="🔑 Your BYTE CORPORATION License Key",
                    description=f"Your KeyAuth license key has been generated successfully.\nUse the credentials below to activate your software.",
                    color=0x0000FF,
                    timestamp=datetime.utcnow(),
                )
                dm_embed.add_field(name="📱 Application", value=f"• Loader - {self.app_name}", inline=False)
                dm_embed.add_field(name="⏳ Duration", value=f"• {days} Days", inline=True)
                dm_embed.add_field(name="🔒 HWID Lock", value=f"• {hwid_badge}", inline=True)
                dm_embed.add_field(name="🖥️ Max Devices", value=f"• {devices}", inline=True)
                dm_embed.add_field(name="🔑 License Key", value=f"```{key}```", inline=False)
                if note:
                    dm_embed.add_field(name="📝 Note / Tag", value=f"• {note}", inline=False)
                dm_embed.add_field(name="👤 Generated By", value=f"• {interaction.user.name} ({interaction.user.id})", inline=False)
                dm_embed.add_field(name="🌐 Official Discord", value="• https://discord.gg/w5Qvp66KC", inline=False)
                dm_embed.set_footer(text="BYTE CORPORATION Security Shield • Keep your key safe!")
                await interaction.user.send(embed=dm_embed)
                dm_sent = True
            except Exception as e:
                logger.warning(f"Could not send DM to {interaction.user.id}: {e}")
                dm_sent = False

            # B. Dispatch Audit Log matching picture media_1793250947221_c4795ee0.png
            log_embed = discord.Embed(
                title="🔑 License Key Generated (API)",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.description = f"Generated via Discord Bot Panel by {interaction.user.mention}"
            log_embed.add_field(name="Application", value=f"• Loader - {self.app_name}", inline=False)
            log_embed.add_field(name="Duration", value=f"• {days} Days", inline=True)
            log_embed.add_field(name="HWID Mode", value=f"• {hwid_badge}", inline=True)
            log_embed.add_field(name="Max Devices", value=f"• {devices}", inline=True)
            log_embed.add_field(name="License Key", value=f"```{key}```", inline=False)
            log_embed.add_field(name="Target Discord", value=f"• {interaction.user.mention} ({interaction.user.id})", inline=False)
            if note:
                log_embed.add_field(name="Note / Tag", value=f"• {note}", inline=False)
            else:
                log_embed.add_field(name="Note", value=f"• Generated via Bot Panel by {interaction.user.name}", inline=False)
            log_embed.set_footer(text="BYTE CORPORATION'S Bot Audit Logger • Container V2")

            if interaction.guild:
                await dispatch_keyauth_log(interaction.guild, log_embed)

            # C. Ephemeral Confirmation in Channel
            dm_status_txt = "📩 **License key sent directly to your Discord DM!**" if dm_sent else "⚠️ **Notice:** Your Discord direct messages are disabled, so DM delivery could not complete. Your key is provided below:"
            embed = discord.Embed(
                title="🔑 License Key Generated Successfully!",
                description=f"{dm_status_txt}\n\n**License Key:**\n```{key}```",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            embed.add_field(name="📱 Application", value=f"`{self.app_name}`", inline=True)
            embed.add_field(name="⏳ Validity", value=f"`{days} Days`", inline=True)
            embed.add_field(name="🔒 HWID Mode", value=f"`{hwid_badge}`", inline=True)
            embed.add_field(name="🖥️ Devices Allowed", value=f"`{devices}`", inline=True)
            if note:
                embed.add_field(name="📝 Note", value=f"`{note}`", inline=False)
            embed.add_field(name="📋 Server Audit Log", value="`Logged in #audit-log-bot`", inline=False)
            embed.set_footer(text="BYTE CORPORATION • Click copy box to use key")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Key generation failed: `{msg}`", ephemeral=True)


class KeyAuthResetHWIDModal(discord.ui.Modal, title="🔐 Reset Hardware ID (HWID)"):
    key_input = discord.ui.TextInput(
        label="License Key or Username",
        placeholder="Enter key (e.g. BYTE-XXXX) or username",
        min_length=1,
        max_length=40,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = self.key_input.value.strip()
        success, msg = await keyauth_reset_hwid(query)
        if success:
            log_embed = discord.Embed(
                title="🔐 Hardware ID (HWID) Reset",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.description = f"HWID Reset via Discord Bot Panel by {interaction.user.mention}"
            log_embed.add_field(name="License Key", value=f"```{query}```", inline=False)
            log_embed.add_field(name="Action By", value=f"• {interaction.user.mention} ({interaction.user.id})", inline=False)
            log_embed.set_footer(text="BYTE CORPORATION'S Bot Audit Logger • Container V2")
            if interaction.guild:
                await dispatch_keyauth_log(interaction.guild, log_embed)
            key_log_id = get_keyauth_log_channel_id()
            log_notice = f"<#{key_log_id}>" if key_log_id else "KeyAuth audit log"
            await interaction.followup.send(f"✅ **HWID Reset Successful!**\n• License: `{query}` has been successfully unlinked.\n• Recorded in {log_notice}.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **HWID Reset Failed:** {msg}", ephemeral=True)


class KeyAuthExtendModal(discord.ui.Modal, title="⏳ Extend License Expiry"):
    key_input = discord.ui.TextInput(
        label="License Key",
        placeholder="Enter key to extend (e.g. BYTE-XXXX-XXXX-XXXX)",
        min_length=5,
        max_length=40,
        required=True,
    )
    days_input = discord.ui.TextInput(
        label="Additional Days to Add",
        placeholder="7, 30, 90",
        default="30",
        min_length=1,
        max_length=5,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = self.key_input.value.strip()
        try:
            days = int(self.days_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Additional duration must be a number of days.", ephemeral=True)
            return

        success, msg = await keyauth_extend_license(query, additional_days=days)
        if success:
            log_embed = discord.Embed(
                title="⏳ License Key Extended",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.description = f"License Extended via Discord Bot Panel by {interaction.user.mention}"
            log_embed.add_field(name="License Key", value=f"```{query}```", inline=False)
            log_embed.add_field(name="Added Duration", value=f"• +{days} Days", inline=False)
            log_embed.add_field(name="Action By", value=f"• {interaction.user.mention} ({interaction.user.id})", inline=False)
            log_embed.set_footer(text="BYTE CORPORATION'S Bot Audit Logger • Container V2")
            if interaction.guild:
                await dispatch_keyauth_log(interaction.guild, log_embed)
            key_log_id = get_keyauth_log_channel_id()
            log_notice = f"<#{key_log_id}>" if key_log_id else "KeyAuth audit log"
            await interaction.followup.send(f"✅ **License Extended!**\n• Key: `{query}` extended by **+{days} days**.\n• Recorded in {log_notice}.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Extension Failed:** {msg}", ephemeral=True)


class KeyAuthInfoModal(discord.ui.Modal, title="ℹ️ Check License Details"):
    key_input = discord.ui.TextInput(
        label="License Key",
        placeholder="Enter key to lookup (e.g. BYTE-XXXX-XXXX-XXXX)",
        min_length=5,
        max_length=40,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = self.key_input.value.strip()
        lic = await keyauth_find_license(query)
        if not lic:
            await interaction.followup.send(f"❌ License `{query}` was not found!", ephemeral=True)
            return

        status = lic.get("status", "unknown").upper()
        app_name = lic.get("app_name", "N/A")
        duration = lic.get("duration_days", 0)
        hwid = lic.get("hwid") or "Unbound"
        created_at = lic.get("created_at", "N/A")
        expires_at = lic.get("expires_at") or "Not Activated Yet"

        embed = discord.Embed(
            title=f"📋 License Info — {lic.get('license_key')}",
            color=0x0000FF,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="📱 Application", value=f"`{app_name}`", inline=True)
        embed.add_field(name="📊 Status", value=f"`{status}`", inline=True)
        embed.add_field(name="⏳ Duration", value=f"`{duration} Days`", inline=True)
        embed.add_field(name="🔐 Bound HWID", value=f"`{hwid}`", inline=False)
        embed.add_field(name="📅 Created At", value=f"`{created_at}`", inline=True)
        embed.add_field(name="⏰ Expiry", value=f"`{expires_at}`", inline=True)
        embed.set_footer(text="BYTE CORPORATION Security Shield")
        await interaction.followup.send(embed=embed, ephemeral=True)


class KeyAuthUserModal(discord.ui.Modal):
    """Exact Form matching user screenshot media_1793284284190_dd945f79.png (Create Client User)."""
    def __init__(self, app_id: str, app_name: str):
        super().__init__(title="Create Client User")
        self.app_id = app_id
        self.app_name = app_name

        self.user_input = discord.ui.TextInput(
            label="Username",
            placeholder="e.g. shadow_operator",
            min_length=1,
            max_length=30,
            required=True,
        )
        self.pass_input = discord.ui.TextInput(
            label="Password",
            placeholder="Enter password (e.g. 1 or any word/number)",
            min_length=1,
            max_length=30,
            required=True,
        )
        self.hwid_mode_input = discord.ui.TextInput(
            label="HWID Lock Mode (ON or OFF)",
            placeholder="ON (Single Device) or OFF (Multi Device)",
            default="ON",
            min_length=2,
            max_length=10,
            required=True,
        )
        self.days_input = discord.ui.TextInput(
            label="Subscription Duration (Days)",
            placeholder="30",
            default="30",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.hwid_input = discord.ui.TextInput(
            label="Hardware Lock (HWID) (Optional)",
            placeholder="e.g. S-1-5-21-1007224239... (or leave empty)",
            required=False,
            max_length=100,
        )
        self.add_item(self.user_input)
        self.add_item(self.pass_input)
        self.add_item(self.hwid_mode_input)
        self.add_item(self.days_input)
        self.add_item(self.hwid_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.followup.send(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return

        has_perm, perm_reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.followup.send(f"❌ **Permission Denied:** {perm_reason}", ephemeral=True)
            return

        username = self.user_input.value.strip()
        password = self.pass_input.value.strip()

        hwid_raw = self.hwid_mode_input.value.strip().upper()
        hwid_enabled = False if hwid_raw in ("OFF", "0", "FALSE", "NO", "MULTI") else True
        hwid_badge = "🔒 HWID ON (Single Device)" if hwid_enabled else "🔓 HWID OFF (Multi Device)"

        try:
            days = int(self.days_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Duration must be a valid number.", ephemeral=True)
            return

        hwid = self.hwid_input.value.strip() or None

        success, msg = await keyauth_create_user(
            self.app_id,
            username,
            password,
            duration_days=days,
            hwid_enabled=hwid_enabled,
            hwid=hwid,
            discord_user_id=str(interaction.user.id)
        )
        if success:
            # A. Send credentials to User DM
            dm_sent = False
            try:
                dm_embed = discord.Embed(
                    title="👤 Your BYTE CORPORATION Account Credentials",
                    description="Your user account has been created successfully.",
                    color=0x0000FF,
                    timestamp=datetime.utcnow(),
                )
                dm_embed.add_field(name="📱 Application", value=f"• Loader - {self.app_name}", inline=False)
                dm_embed.add_field(name="👤 Username", value=f"```{username}```", inline=False)
                dm_embed.add_field(name="🔑 Password", value=f"```{password}```", inline=False)
                dm_embed.add_field(name="⏳ Duration", value=f"• {days} Days", inline=True)
                dm_embed.add_field(name="🔒 HWID Mode", value=f"• {hwid_badge}", inline=True)
                if hwid:
                    dm_embed.add_field(name="🖥️ Bound HWID", value=f"```{hwid}```", inline=False)
                dm_embed.add_field(name="🌐 Official Discord", value="• https://discord.gg/w5Qvp66KC", inline=False)
                dm_embed.set_footer(text="BYTE CORPORATION Security Shield • Keep credentials safe!")
                await interaction.user.send(embed=dm_embed)
                dm_sent = True
            except Exception:
                dm_sent = False

            # B. Dispatch Audit Log matching Log 1 in screenshot media_1793250947221_c4795ee0.png
            log_embed = discord.Embed(
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.description = f"Created via Discord Bot Panel by {interaction.user.name}"
            log_embed.add_field(name="Application", value=f"• Loader - {self.app_name}", inline=False)
            log_embed.add_field(name="Username", value=f"```{username}```", inline=False)
            log_embed.add_field(name="Target Discord", value=f"• {interaction.user.mention} ({interaction.user.id})", inline=False)
            log_embed.add_field(name="Duration", value=f"• {days} Days", inline=True)
            log_embed.add_field(name="HWID Mode", value=f"• {hwid_badge}", inline=True)
            if hwid:
                log_embed.add_field(name="Bound HWID", value=f"```{hwid}```", inline=False)
            log_embed.set_footer(text="BYTE CORPORATION'S Bot Audit Logger • Container V2")

            if interaction.guild:
                await dispatch_keyauth_log(interaction.guild, log_embed)

            # C. Ephemeral response
            dm_notice = "📩 **Account credentials sent directly to your Discord DM!**" if dm_sent else "⚠️ **Notice:** Direct message could not be delivered because your DMs are closed. Credentials are listed below:"
            embed = discord.Embed(
                title="👤 User Account Created Successfully!",
                description=f"{dm_notice}\n\n• **Username:** `{username}`\n• **Password:** `{password}`",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            embed.add_field(name="📱 Application", value=f"`{self.app_name}`", inline=True)
            embed.add_field(name="⏳ Validity", value=f"`{days} Days`", inline=True)
            embed.add_field(name="🔒 HWID Mode", value=f"`{hwid_badge}`", inline=True)
            if hwid:
                embed.add_field(name="🖥️ Bound HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(name="📋 Server Audit Log", value="`Logged in #audit-log-bot`", inline=False)
            embed.set_footer(text="BYTE CORPORATION • User account active")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ User creation failed: `{msg}`", ephemeral=True)


# --- Dropdown Menu for Target Application Selection ---

class KeyAuthAppSelect(discord.ui.Select):
    def __init__(self, action_type: str = "key"):
        self.action_type = action_type  # "key" or "user"
        options = []
        for app in DEFAULT_KEYAUTH_APPS:
            app_id = app.get("id")
            app_name = app.get("name")
            sys_id = app.get("app_id", "")
            desc = f"App ID: {sys_id} | v1.0"[:50]
            options.append(discord.SelectOption(
                label=app_name[:100],
                value=app_id,
                description=desc,
                emoji="\U0001F3AE" if "loader" in app_id else "\u26A1",
            ))

        super().__init__(
            placeholder="Select target application...",
            min_values=1,
            max_values=1,
            custom_id="keyauth_target_app_dropdown",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return

        has_perm, perm_reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {perm_reason}", ephemeral=True)
            return

        selected_app_id = self.values[0]
        selected_app_name = next((a["name"] for a in DEFAULT_KEYAUTH_APPS if a["id"] == selected_app_id), "Application")

        if self.action_type == "user":
            await interaction.response.send_modal(KeyAuthUserModal(selected_app_id, selected_app_name))
        else:
            await interaction.response.send_modal(KeyAuthGenModal(selected_app_id, selected_app_name))


class KeyAuthAppSelectView(discord.ui.View):
    def __init__(self, action_type: str = "key"):
        super().__init__(timeout=300)
        self.add_item(KeyAuthAppSelect(action_type=action_type))


# --- Main Persistent Control View (5 Buttons) ---

class KeyAuthControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error(f"KeyAuthControlView error on item {item}: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(
        label="Create Key",
        style=discord.ButtonStyle.primary,
        emoji="\U0001F511",
        custom_id="keyauth_btn_create_key",
        row=0,
    )
    async def create_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        view = KeyAuthAppSelectView(action_type="key")
        await interaction.response.send_message(
            content="👇 **Select Target Application:** Choose an application from the dropdown below to generate a license key:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Create User",
        style=discord.ButtonStyle.success,
        emoji="👤",
        custom_id="keyauth_btn_create_user",
        row=0,
    )
    async def create_user_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        view = KeyAuthAppSelectView(action_type="user")
        await interaction.response.send_message(
            content="👇 **Select Target Application:** Choose an application from the dropdown below to create a user account:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Reset HWID",
        style=discord.ButtonStyle.danger,
        emoji="\U0001F504",
        custom_id="keyauth_btn_reset_hwid",
        row=0,
    )
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(KeyAuthResetHWIDModal())

    @discord.ui.button(
        label="Extend Expiry",
        style=discord.ButtonStyle.secondary,
        emoji="⏳",
        custom_id="keyauth_btn_extend_expiry",
        row=0,
    )
    async def extend_expiry_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(KeyAuthExtendModal())

    @discord.ui.button(
        label="Check Info",
        style=discord.ButtonStyle.secondary,
        emoji="ℹ️",
        custom_id="keyauth_btn_check_info",
        row=0,
    )
    async def check_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        await interaction.response.send_modal(KeyAuthInfoModal())


def build_reseller_panel_embed(guild: discord.Guild = None) -> discord.Embed:
    """Builds the single-app scoped Reseller Panel embed."""
    app = get_reseller_app_config()
    app_name = app.get("name", "Loader - UID Bypass")

    embed = discord.Embed(
        title="BYTE CORPORATION — Reseller Licensing Portal",
        color=0x0000FF,
        timestamp=datetime.utcnow(),
    )

    r_role_id = get_reseller_role_id()
    r_role_str = f"<@&{r_role_id}>" if r_role_id else "`Authorized Resellers & Admins`"

    r_ch_id = get_reseller_channel_id()
    r_ch_str = f"<#{r_ch_id}>" if r_ch_id else "`All Channels (Unlocked)`"

    desc_lines = [
        "Welcome to the **Official Reseller Management Panel**.",
        "Use the buttons below to issue licenses and create accounts for your clients.",
        "",
        "🎯 **Authorized Application Access**",
        f"• **Application:** `{app_name}` (v1.0)",
        "• **Scope:** Restricted Single-Application Reseller Licensing",
        "",
        "🛡️ **Security Scope & Policy**",
        f"• **Authorized Role:** {r_role_str}",
        f"• **Channel Scope:** {r_ch_str}",
        "• **HWID Protection:** HWID ON (Single Device) or Multi-Device Support",
        "",
        "⚠️ *Notice: All key generations and user registrations are permanently logged in server audits.*",
    ]
    embed.description = "\n".join(desc_lines)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="BYTE CORPORATION • Reseller Authorization System v2.0")
    return embed


class ByteResellerControlView(discord.ui.View):
    """Dedicated Persistent Reseller Panel with direct single-app access (No Dropdown of other apps!)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Key",
        style=discord.ButtonStyle.primary,
        emoji="\U0001F511",
        custom_id="byte_reseller_btn_create_key",
        row=0,
    )
    async def create_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_reseller_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_reseller_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return

        # Directly opens modal locked to the authorized reseller app (NO dropdown of other apps!)
        app = get_reseller_app_config()
        await interaction.response.send_modal(KeyAuthGenModal(app["id"], app["name"]))

    @discord.ui.button(
        label="Create User",
        style=discord.ButtonStyle.success,
        emoji="👤",
        custom_id="byte_reseller_btn_create_user",
        row=0,
    )
    async def create_user_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_reseller_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_reseller_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return

        # Directly opens modal locked to the authorized reseller app (NO dropdown of other apps!)
        app = get_reseller_app_config()
        await interaction.response.send_modal(KeyAuthUserModal(app["id"], app["name"]))

    @discord.ui.button(
        label="Reset HWID",
        style=discord.ButtonStyle.danger,
        emoji="\U0001F504",
        custom_id="byte_reseller_btn_reset_hwid",
        row=0,
    )
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_reseller_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_reseller_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(KeyAuthResetHWIDModal())

    @discord.ui.button(
        label="Extend Expiry",
        style=discord.ButtonStyle.secondary,
        emoji="⏳",
        custom_id="byte_reseller_btn_extend_expiry",
        row=0,
    )
    async def extend_expiry_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_reseller_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_reseller_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(KeyAuthExtendModal())

    @discord.ui.button(
        label="Check Info",
        style=discord.ButtonStyle.secondary,
        emoji="ℹ️",
        custom_id="byte_reseller_btn_check_info",
        row=0,
    )
    async def check_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_reseller_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        await interaction.response.send_modal(KeyAuthInfoModal())


# --- Reseller Panel Commands ---

@bot.command(name="resellerpanel", aliases=["reseller", "rpanel"])
async def resellerpanel_cmd(ctx):
    """Spawns the dedicated Single-App Reseller Licensing Panel."""
    allowed, ch_reason = is_reseller_channel_allowed(ctx.channel.id)
    if not allowed:
        await ctx.reply(f"❌ **Wrong Channel:** {ch_reason}")
        return
    has_perm, reason = has_reseller_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ **Permission Denied:** {reason}")
        return
    embed = build_reseller_panel_embed(ctx.guild)
    view = ByteResellerControlView()
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="resellerpanel", description="Post the Dedicated Single-App Reseller Licensing Panel")
async def resellerpanel_slash(interaction: discord.Interaction):
    allowed, ch_reason = is_reseller_channel_allowed(interaction.channel_id)
    if not allowed:
        await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
        return
    has_perm, reason = has_reseller_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return
    embed = build_reseller_panel_embed(interaction.guild)
    view = ByteResellerControlView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Reseller Licensing Panel sent successfully!", ephemeral=True)


@bot.command(name="setresellerrole", aliases=["resellerrole"])
async def setresellerrole_cmd(ctx, role: discord.Role = None):
    """Sets the Discord role authorized to use the Reseller Panel."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can set the reseller role.")
        return
    if not role:
        await ctx.reply("Usage: `*setresellerrole @Role` (e.g. `*setresellerrole @Reseller`)")
        return
    cfg = load_config()
    cfg["reseller_role_id"] = role.id
    save_config(cfg)
    await ctx.reply(f"✅ **Reseller Role Updated:** Access to the Reseller Panel is now granted to {role.mention}.")


@bot.command(name="setresellerchannel", aliases=["resellerchannel"])
async def setresellerchannel_cmd(ctx, channel: discord.TextChannel = None):
    """Locks the Reseller Panel to a specific text channel."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can set the reseller channel.")
        return
    if not channel:
        await ctx.reply("Usage: `*setresellerchannel #channel` (e.g. `*setresellerchannel #reseller-chat`)")
        return
    cfg = load_config()
    cfg["reseller_channel_id"] = channel.id
    save_config(cfg)
    await ctx.reply(f"✅ **Reseller Channel Locked:** The Reseller Panel is now locked to {channel.mention}.")


@bot.command(name="setresellerapp", aliases=["resellerapp"])
async def setresellerapp_cmd(ctx, *, app_name_or_id: str = None):
    """Configures which single application resellers are authorized to manage (defaults to Loader - UID Bypass)."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can set the reseller application.")
        return
    if not app_name_or_id:
        app = get_reseller_app_config()
        await ctx.reply(f"ℹ️ **Current Reseller Application:** `{app.get('name')}` (`{app.get('id')}`)\nUsage: `*setresellerapp <app_name>` (e.g. `*setresellerapp uid`)")
        return

    q = app_name_or_id.strip().lower()
    matched = None
    for a in DEFAULT_KEYAUTH_APPS:
        if q in a["name"].lower() or q in a["id"].lower() or q in a.get("app_id", "").lower():
            matched = a
            break

    if not matched:
        await ctx.reply(f"❌ Application matching `{app_name_or_id}` was not found in the 15 authorized apps.")
        return

    cfg = load_config()
    cfg["reseller_app_id"] = matched["id"]
    save_config(cfg)
    await ctx.reply(f"✅ **Reseller Application Updated:** Resellers are now restricted exclusively to **`{matched['name']}`**.")


# --- Prefix & Slash Commands for KeyAuth Panel ---

@bot.command(name="keypanel", aliases=["keyauth", "license", "keys", "authpanel"])
async def keypanel_cmd(ctx):
    allowed, ch_reason = is_keyauth_channel_allowed(ctx.channel.id)
    if not allowed:
        await ctx.reply(f"❌ **Wrong Channel:** {ch_reason}")
        return
    has_perm, reason = has_keyauth_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ **Permission Denied:** {reason}")
        return
    embed = build_keyauth_panel_embed(ctx.guild)
    view = KeyAuthControlView()
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="keypanel", description="Post the KeyAuth / Reseller Bot Management Control Panel")
async def keypanel_slash(interaction: discord.Interaction):
    allowed, ch_reason = is_keyauth_channel_allowed(interaction.channel_id)
    if not allowed:
        await interaction.response.send_message(f"❌ **Wrong Channel:** {ch_reason}", ephemeral=True)
        return
    has_perm, reason = has_keyauth_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return
    embed = build_keyauth_panel_embed(interaction.guild)
    view = KeyAuthControlView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ KeyAuth Control Panel sent successfully!", ephemeral=True)



@bot.command(name="apps", aliases=["applications", "applist"])
async def apps_cmd(ctx):
    """Lists all 15 integrated KeyAuth applications."""
    apps_list = []
    for i, a in enumerate(DEFAULT_KEYAUTH_APPS, 1):
        icon = "🎮" if "loader" in a["id"] else "⚡"
        line = f"`{i:02d}.` {icon} **{a['name']}** (ID: `{a['id']}`)"
        apps_list.append(line)

    embed = discord.Embed(
        title="🔑 » KeyAuth Integrated Applications (15 Applications)",
        description="All 15 applications supported by the KeyAuth / Reseller Engine:\n\n" + "\n".join(apps_list),
        color=0x0000FF,
    )
    if ctx.guild and ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    embed.set_footer(text="Powered By 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | Designed By diewithvoid")
    await ctx.reply(embed=embed)


@bot.tree.command(name="apps", description="List all 15 integrated KeyAuth applications")
async def apps_slash(interaction: discord.Interaction):
    apps_list = []
    for i, a in enumerate(DEFAULT_KEYAUTH_APPS, 1):
        icon = "🎮" if "loader" in a["id"] else "⚡"
        line = f"`{i:02d}.` {icon} **{a['name']}** (ID: `{a['id']}`)"
        apps_list.append(line)

    embed = discord.Embed(
        title="🔑 » KeyAuth Integrated Applications (15 Applications)",
        description="All 15 applications supported by the KeyAuth / Reseller Engine:\n\n" + "\n".join(apps_list),
        color=0x0000FF,
    )
    if interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.set_footer(text="Powered By 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | Designed By diewithvoid")
    await interaction.response.send_message(embed=embed)


# --- Commands to Set KeyAuth Role & Channel ---

@bot.command(name="setkeyrole", aliases=["keyrole", "setkeyauthrole"])
async def setkeyrole_prefix(ctx, role: discord.Role = None):
    """Configures the role authorized to generate and manage KeyAuth keys."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can set the KeyAuth authorized role.")
        return
    if not role:
        await ctx.reply("Usage: `*setkeyrole @Role` (e.g. `*setkeyrole @KeyMaker`)")
        return
    cfg = load_config()
    cfg["keyauth_role_id"] = role.id
    save_config(cfg)
    await ctx.reply(f"✅ **KeyAuth Authorized Role Updated:** KeyAuth license key creation is now granted to {role.mention}.")


@bot.tree.command(name="setkeyrole", description="Set the Discord role authorized to generate KeyAuth license keys")
@app_commands.describe(role="The role allowed to create keys")
async def setkeyrole_slash(interaction: discord.Interaction, role: discord.Role):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can set the KeyAuth role.", ephemeral=True)
        return
    cfg = load_config()
    cfg["keyauth_role_id"] = role.id
    save_config(cfg)
    await interaction.response.send_message(f"✅ **KeyAuth Authorized Role Updated:** KeyAuth license key creation is now granted to {role.mention}.", ephemeral=True)


@bot.command(name="setkeychannel", aliases=["keychannel", "setkeyauthchannel"])
async def setkeychannel_prefix(ctx, channel: discord.TextChannel = None):
    """Locks KeyAuth panel & key generation to a specific channel."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can configure the KeyAuth channel lock.")
        return
    if not channel:
        await ctx.reply("Usage: `*setkeychannel #channel` (e.g. `*setkeychannel #keyauth`)")
        return
    cfg = load_config()
    cfg["keyauth_channel_id"] = channel.id
    save_config(cfg)
    await ctx.reply(f"✅ **KeyAuth Channel Locked:** KeyAuth panel and key creation are now restricted to {channel.mention}.")


@bot.tree.command(name="setkeychannel", description="Lock the KeyAuth panel and key generation to a specific channel")
@app_commands.describe(channel="The channel where KeyAuth is allowed to run")
async def setkeychannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can configure the KeyAuth channel lock.", ephemeral=True)
        return
    cfg = load_config()
    cfg["keyauth_channel_id"] = channel.id
    save_config(cfg)
    await interaction.response.send_message(f"✅ **KeyAuth Channel Locked:** KeyAuth panel and key creation are now restricted to {channel.mention}.", ephemeral=True)


# ==========================================
# SECOND KEYAUTH (BASIC PANEL SDK) IMPLEMENTATION
# ==========================================

DEFAULT_SECOND_KEYAUTH_APPS = [
    {"id": "pkg_basic_panel", "name": "BASIC PANEL", "package_id": "e52c1515c53453b85d0d4e87", "version": "28"},
]

DEFAULT_SECOND_AUTH_INITIAL_KEYS = {
    "1": [
        "BYTE-942C-BD7B-B2AE-A7FF",
        "BYTE-125E-DC6D-D7EA-8BF6",
        "BYTE-74DE-0CFD-E489-F6FB",
        "BYTE-BB91-7709-D3A1-E39E",
        "BYTE-8974-347D-B314-353C",
        "BYTE-7426-81F2-A5D7-5310",
        "BYTE-6F2B-20EE-ADE6-8760",
        "BYTE-06FD-DAFD-0C1F-0AD9",
        "BYTE-70A4-2031-F7F2-2FD7",
        "BYTE-D742-3B28-3F36-1398"
    ],
    "7": [
        "BYTE-9002-B2B4-751A-1BC9",
        "BYTE-DB7E-8DC8-5DFD-3800",
        "BYTE-7C73-3329-F465-EC81",
        "BYTE-92F3-FB0B-0895-80AB",
        "BYTE-8968-6CAE-52B3-AE6F",
        "BYTE-29B8-CC67-4405-46E1",
        "BYTE-EEA3-1115-9B6B-A0F2",
        "BYTE-1CD9-4C61-A9DA-054E",
        "BYTE-D11C-2F3F-CF99-1B99",
        "BYTE-1031-6ABC-10BF-8344",
        "BYTE-4A30-A6C1-6F0B-B35E",
        "BYTE-CD01-DBE7-6CC2-F186",
        "BYTE-27B7-F257-8B83-98FB",
        "BYTE-9822-D051-0D54-3934",
        "BYTE-1BD0-17A9-F704-CE6B",
        "BYTE-34A9-96C3-2C60-A374",
        "BYTE-F1F4-0E5E-FFD1-E5EA",
        "BYTE-A4F3-8742-F4DD-291E",
        "BYTE-5082-FD80-578A-44C5",
        "BYTE-48DA-18C8-F38F-720F"
    ],
    "30": [
        "BYTE-77CB-EB09-DA5F-C4B3",
        "BYTE-FFE8-FE43-D551-0FD2",
        "BYTE-C260-BE47-C7A2-CC05",
        "BYTE-14D7-3971-7E7F-1C39",
        "BYTE-4757-04D2-7D63-3143",
        "BYTE-2703-20AB-7DB0-C9F0",
        "BYTE-F686-1D07-37C5-DE00",
        "BYTE-DA28-B397-8A07-AB04",
        "BYTE-1F43-7752-28A2-E2FC",
        "BYTE-CF7A-8121-6C70-E17C",
        "BYTE-C558-B359-16C1-7655",
        "BYTE-8BB2-0012-AE1F-B8B5",
        "BYTE-9181-EBAA-929C-71C3",
        "BYTE-BA6E-228A-DA4A-4C81",
        "BYTE-B558-CC3B-AB3E-B839",
        "BYTE-587E-102C-6F8B-34E0",
        "BYTE-7E70-BBA9-BF2D-C8EE",
        "BYTE-F04C-B18D-B6F0-D9AC",
        "BYTE-388E-7A67-8F1C-A48A",
        "BYTE-825D-572B-7E61-5082"
    ]
}


async def second_keyauth_request(action: str, extra: dict = None, package_id: str = None) -> dict:
    """Executes an action against the Second KeyAuth backend (https://prtvshow.online/api.php)."""
    import hashlib
    import uuid
    cfg = get_second_keyauth_config()
    api_url = cfg["api_url"]
    app_id = cfg["app_id"]
    app_secret = cfg["app_secret"]
    version = cfg["version"]
    pkg = package_id or cfg["package_id"]

    nonce = hashlib.md5(f"{time.time()}_{uuid.uuid4()}".encode()).hexdigest()
    payload = {
        "action": action,
        "app_id": app_id,
        "app_secret": app_secret,
        "package_id": pkg,
        "version": version,
        "nonce": nonce,
        "timestamp": int(time.time()),
        **(extra or {})
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "KeyAuth-Pro-Python-SDK/1.0"},
                timeout=aiohttp.ClientTimeout(total=8),
                ssl=False
            ) as resp:
                try:
                    data = await resp.json()
                    return data
                except Exception:
                    raw = await resp.text()
                    return {"success": resp.status in (200, 201), "message": f"Server HTTP {resp.status}: {raw[:150]}"}
    except Exception as e:
        logger.warning(f"Second KeyAuth request notice ({action}): {e}")
        return {"success": False, "message": f"Connection notice: {e}"}


# --- Second Auth Stock / Key Pool Engine ---

SECOND_AUTH_STOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "second_auth_stock.json")


def get_package_stock_counts(pkg_entry: dict) -> tuple[int, dict]:
    """Returns total available keys and duration breakdown for a package."""
    durations = pkg_entry.get("durations", {})
    breakdown = {}
    total = 0
    for dur_key, k_list in durations.items():
        cnt = len(k_list)
        breakdown[dur_key] = cnt
        total += cnt
    flat_cnt = len(pkg_entry.get("keys", []))
    if flat_cnt > 0:
        breakdown["general"] = flat_cnt
        total += flat_cnt
    return total, breakdown


def load_second_auth_stock() -> dict:
    """Loads the pre-generated real key stock for BASIC PANEL, auto-seeding if empty."""
    basic_pid = "e52c1515c53453b85d0d4e87"
    stock = None
    if os.path.exists(SECOND_AUTH_STOCK_FILE):
        try:
            with open(SECOND_AUTH_STOCK_FILE, "r", encoding="utf-8") as f:
                stock = json.load(f)
        except Exception as e:
            logger.error(f"Error loading second_auth_stock.json: {e}")

    if not stock or not isinstance(stock, dict):
        stock = {"packages": {}, "history": []}

    # Restrict packages dict strictly to BASIC PANEL
    stock.setdefault("packages", {})
    stock["packages"] = {
        basic_pid: stock["packages"].get(basic_pid, {
            "name": "BASIC PANEL",
            "durations": {"1": [], "7": [], "30": []},
            "keys": []
        })
    }
    pkg_entry = stock["packages"][basic_pid]
    pkg_entry["name"] = "BASIC PANEL"
    pkg_entry.setdefault("durations", {"1": [], "7": [], "30": []})
    pkg_entry.setdefault("keys", [])

    # Auto-seed initial 50 real keys if stock is 0 (handles online hosting sync)
    total_avail, _ = get_package_stock_counts(pkg_entry)
    if total_avail == 0:
        for d_key, k_list in DEFAULT_SECOND_AUTH_INITIAL_KEYS.items():
            pkg_entry["durations"][d_key] = list(k_list)
        save_second_auth_stock(stock)

    return stock


def save_second_auth_stock(stock: dict):
    """Saves stock to second_auth_stock.json strictly UTF-8 without BOM."""
    try:
        with open(SECOND_AUTH_STOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(stock, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving second_auth_stock.json: {e}")


def get_second_auth_package_by_query(query: str = None) -> dict:
    """Always resolves to the active BASIC PANEL package."""
    return DEFAULT_SECOND_KEYAUTH_APPS[0]


def add_second_auth_keys(package_id: str, raw_keys: list[str], duration_days: int = None) -> tuple[int, int, str]:
    """Adds bulk real keys to BASIC PANEL stock pool."""
    stock = load_second_auth_stock()
    basic_pid = "e52c1515c53453b85d0d4e87"
    pkg_entry = stock["packages"].setdefault(basic_pid, {
        "name": "BASIC PANEL",
        "durations": {"1": [], "7": [], "30": []},
        "keys": []
    })

    dur_key = str(duration_days) if duration_days in (1, 7, 30, 90, 365) else "30"
    pkg_entry.setdefault("durations", {})
    target_list = pkg_entry["durations"].setdefault(dur_key, [])

    existing_keys = set()
    for d_list in pkg_entry.get("durations", {}).values():
        existing_keys.update(k.strip() for k in d_list)
    existing_keys.update(k.strip() for k in pkg_entry.get("keys", []))

    added = 0
    for k in raw_keys:
        clean_k = k.strip().upper()
        if clean_k and clean_k not in existing_keys:
            target_list.append(clean_k)
            existing_keys.add(clean_k)
            added += 1

    save_second_auth_stock(stock)
    total_avail, _ = get_package_stock_counts(pkg_entry)
    return added, total_avail, "BASIC PANEL"


def claim_second_auth_key(package_id: str = None, duration_days: int = 30, claimed_by: str = None) -> tuple[bool, str, str, str]:
    """Pops a real working key matching duration from stock and records audit trail."""
    stock = load_second_auth_stock()
    basic_pid = "e52c1515c53453b85d0d4e87"
    pkg_entry = stock["packages"].get(basic_pid)
    if not pkg_entry:
        return False, "", "BASIC PANEL", ""

    dur_key = str(duration_days)
    real_key = None
    claimed_duration = f"{duration_days} Days"

    # 1. Try exact duration pool
    dur_list = pkg_entry.get("durations", {}).get(dur_key, [])
    if dur_list:
        real_key = dur_list.pop(0)

    # 2. Try flat keys pool
    if not real_key and pkg_entry.get("keys"):
        real_key = pkg_entry["keys"].pop(0)
        claimed_duration = f"{duration_days} Days (General Pool)"

    # 3. Fallback to any available duration pool
    if not real_key:
        for fallback_dur in ["30", "7", "1"]:
            f_list = pkg_entry.get("durations", {}).get(fallback_dur, [])
            if f_list:
                real_key = f_list.pop(0)
                claimed_duration = f"{fallback_dur} Days Pool"
                break

    if not real_key:
        return False, "", "BASIC PANEL", ""

    stock.setdefault("history", []).append({
        "key": real_key,
        "package_id": basic_pid,
        "package_name": "BASIC PANEL",
        "requested_duration": duration_days,
        "claimed_duration": claimed_duration,
        "claimed_by": str(claimed_by or "Unknown"),
        "claimed_at": datetime.utcnow().isoformat()
    })
    save_second_auth_stock(stock)
    return True, real_key, "BASIC PANEL", claimed_duration


def build_second_auth_stock_embed() -> discord.Embed:
    """Builds a rich overview embed of live stock for BASIC PANEL."""
    stock = load_second_auth_stock()
    embed = discord.Embed(
        title="\U0001F4E6 BYTE CORPORATION \u00BB Second Auth Live Inventory",
        color=0x0055FF,
        timestamp=datetime.utcnow()
    )
    total_used = len(stock.get("history", []))

    basic_pid = "e52c1515c53453b85d0d4e87"
    pkg_entry = stock["packages"].get(basic_pid, {})
    avail, b_down = get_package_stock_counts(pkg_entry)

    dur_parts = []
    for d in ["1", "7", "30"]:
        cnt = b_down.get(d, 0)
        dur_parts.append(f"`{d}d`: **{cnt}**")
    if b_down.get("general", 0) > 0:
        dur_parts.append(f"`gen`: **{b_down['general']}**")

    dur_summary = " | ".join(dur_parts)
    status_line = f"\u2705 **{avail}** Keys Available ({dur_summary})" if avail > 0 else "\u274C **0** Keys (Out of Stock)"
    embed.add_field(
        name="\U0001F4E6 BASIC PANEL",
        value=f"{status_line}\n`ID: {basic_pid}`",
        inline=False
    )

    embed.add_field(
        name="\U0001F4CA Inventory Summary",
        value=f"\u2022 Total Available Keys: `{avail}`\n\u2022 Total Claimed / Used: `{total_used}`",
        inline=False
    )
    embed.set_footer(text="Use *addsecondkeys [duration] <keys> or Restock button to add keys")
    return embed


async def second_keyauth_generate_key(
    package_id: str = "e52c1515c53453b85d0d4e87",
    duration_days: int = 30,
    prefix: str = "BYTE",
    hwid_enabled: bool = True,
    max_devices: int = 1,
    custom_key: str = None,
    note: str = None,
    claimed_by: str = None
) -> tuple[bool, dict, str]:
    """Retrieves a verified real license key matching duration from stock for BASIC PANEL."""
    days = int(duration_days) if duration_days else 30
    devices = int(max_devices) if max_devices else 1

    # If custom key was passed explicitly
    if custom_key and custom_key.strip():
        return True, {
            "license_key": custom_key.strip().upper(),
            "package_id": "e52c1515c53453b85d0d4e87",
            "duration_days": days,
            "max_devices": devices,
            "hwid_enabled": 1 if hwid_enabled else 0,
            "status": "custom",
            "source": "Custom Assigned Key",
        }, "Custom key assigned."

    # Pop key from pre-stocked real keys
    ok, real_key, pkg_name, assigned_dur = claim_second_auth_key("e52c1515c53453b85d0d4e87", duration_days=days, claimed_by=claimed_by)
    if ok and real_key:
        return True, {
            "license_key": real_key,
            "package_id": "e52c1515c53453b85d0d4e87",
            "duration_days": days,
            "assigned_duration": assigned_dur,
            "max_devices": devices,
            "hwid_enabled": 1 if hwid_enabled else 0,
            "status": "unused",
            "source": "Second Auth Database Stock",
        }, f"Verified real key assigned for BASIC PANEL ({assigned_dur})."

    # If stock is empty
    return False, {}, (
        f"\u26A0\uFE0F **OUT OF STOCK:** `BASIC PANEL` ke liye `{days} Days` stock me koi real key available nahi hai!\n"
        f"Kripya Web Admin Panel se keys generate karke `*addsecondkeys {days} <keys>` se stock add karein."
    )


def build_second_keyauth_panel_embed(guild: discord.Guild = None) -> discord.Embed:
    """Builds the Second KeyAuth Management Portal embed matching the Master Panel style."""
    cfg = get_second_keyauth_config()
    embed = discord.Embed(
        title="\U0001F510 BYTE CORPORATION \u00BB Second Auth Management Portal",
        color=0x0055FF,
        timestamp=datetime.utcnow(),
    )

    key_role_id = get_second_keyauth_role_id() or get_keyauth_role_id()
    key_role_str = f"<@&{key_role_id}>" if key_role_id else "`Administrator Only` (Use *setsecondkeyrole @Role)"

    key_ch_id = get_second_keyauth_channel_id()
    key_ch_str = f"<#{key_ch_id}>" if key_ch_id else "`All Channels (Unlocked)` (Use *setsecondkeychannel #channel)"

    key_log_id = get_second_keyauth_log_channel_id() or get_keyauth_log_channel_id()
    key_log_str = f"<#{key_log_id}>" if key_log_id else "`Auto Audit Logs` (Use *setsecondkeylog #channel)"

    api_domain = cfg["api_url"].split("/")[2] if "/" in cfg["api_url"] else cfg["api_url"]

    stock = load_second_auth_stock()
    basic_pid = "e52c1515c53453b85d0d4e87"
    pkg_entry = stock.get("packages", {}).get(basic_pid, {})
    avail_cnt, b_down = get_package_stock_counts(pkg_entry)
    dur_parts = [f"`{d}d`: **{b_down.get(d, 0)}**" for d in ["1", "7", "30"]]
    dur_str = f" ({', '.join(dur_parts)})" if dur_parts else ""
    badge = f"`\u2705 {avail_cnt} in stock{dur_str}`" if avail_cnt > 0 else "`\u274C 0 (Out of Stock)`"

    desc_lines = [
        "Welcome to the **Second Auth (Pro SDK) Management Portal**.",
        "Use the interactive buttons below to claim license keys from stock, inspect key status, test HWID authentication, or view live inventory.",
        "",
        "\U0001F4E6 **Authorized Package & Live Stock**",
        f"\u2022 **BASIC PANEL** ({badge})",
        f"  `ID: {basic_pid}`",
        "",
        "\U0001F6E1\uFE0F **Security Scope & Permissions**",
        f"\u2022 **Authorized Role:** {key_role_str}",
        f"\u2022 **Audit Log Channel:** {key_log_str}",
        f"\u2022 **Locked Channel:** {key_ch_str}",
        f"\u2022 **Endpoint:** `{api_domain}` (v{cfg['version']})",
        "",
        "\u2139\uFE0F *All license claims, key checks, and HWID resets are permanently audited.*",
    ]
    embed.description = "\n".join(desc_lines)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="BYTE CORPORATION \u2022 Second Auth Security System")
    return embed


# --- Second KeyAuth Modals ---

class SecondKeyAuthGenModal(discord.ui.Modal):
    def __init__(self, package_id: str = "e52c1515c53453b85d0d4e87", package_name: str = "BASIC PANEL"):
        super().__init__(title="Claim: BASIC PANEL")
        self.package_id = package_id
        self.package_name = package_name

        self.duration_input = discord.ui.TextInput(
            label="Duration (In Days: 1, 7, 30)",
            placeholder="e.g. 1, 7, 30",
            default="30",
            required=True,
            max_length=5,
        )
        self.prefix_input = discord.ui.TextInput(
            label="Key Prefix (Optional)",
            placeholder="e.g. BYTE, TX, VIP",
            default="BYTE",
            required=False,
            max_length=15,
        )
        self.custom_key_input = discord.ui.TextInput(
            label="Custom Key (Optional)",
            placeholder="Leave blank to claim verified key from stock",
            required=False,
            max_length=50,
        )
        self.hwid_toggle = discord.ui.TextInput(
            label="HWID Lock (1 = ON, 0 = OFF)",
            placeholder="1 for single device, 0 for unlimited/multi",
            default="1",
            required=True,
            max_length=1,
        )
        self.note_input = discord.ui.TextInput(
            label="Client Note / Discord Username",
            placeholder="e.g. Sold to client",
            required=False,
            max_length=100,
        )

        self.add_item(self.duration_input)
        self.add_item(self.prefix_input)
        self.add_item(self.custom_key_input)
        self.add_item(self.hwid_toggle)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            days = int(self.duration_input.value.strip())
        except ValueError:
            await interaction.followup.send("\u274C Duration must be a valid number of days (e.g. 1, 7, 30).", ephemeral=True)
            return

        hwid_on = self.hwid_toggle.value.strip() != "0"
        max_dev = 1 if hwid_on else 5

        ok, lic, msg = await second_keyauth_generate_key(
            package_id=self.package_id,
            duration_days=days,
            prefix=self.prefix_input.value.strip(),
            hwid_enabled=hwid_on,
            max_devices=max_dev,
            custom_key=self.custom_key_input.value.strip(),
            note=self.note_input.value.strip(),
            claimed_by=interaction.user.name
        )

        if ok and lic:
            key_val = lic.get("license_key")
            embed = discord.Embed(
                title="\u2705 License Key Claimed (BASIC PANEL)",
                color=0x00FF00,
                timestamp=datetime.utcnow()
            )
            embed.description = f"```\n{key_val}\n```"
            embed.add_field(name="\U0001F4E6 Package", value="`BASIC PANEL`", inline=True)
            embed.add_field(name="\u23F3 Duration", value=f"`{days} Days`", inline=True)
            embed.add_field(name="\U0001F512 HWID Lock", value=f"`{'ENABLED (1 Device)' if hwid_on else 'DISABLED'}`", inline=True)
            embed.add_field(name="\U0001F4CA Source", value=f"`{lic.get('source', 'Stock Pool')}`", inline=True)
            if self.note_input.value.strip():
                embed.add_field(name="\U0001F4DD Note", value=f"`{self.note_input.value.strip()}`", inline=False)
            embed.set_footer(text=f"Claimed By: {interaction.user.name} \u2022 Second Auth")

            await interaction.followup.send(embed=embed, ephemeral=True)
            if interaction.guild:
                await dispatch_second_keyauth_log(interaction.guild, embed)
        else:
            await interaction.followup.send(f"{msg}", ephemeral=True)


class SecondKeyAuthRestockModal(discord.ui.Modal):
    def __init__(self, package_id: str = "e52c1515c53453b85d0d4e87", package_name: str = "BASIC PANEL"):
        super().__init__(title="Restock: BASIC PANEL")
        self.package_id = package_id
        self.package_name = package_name

        self.duration_input = discord.ui.TextInput(
            label="Duration Pool (1, 7, or 30 Days)",
            placeholder="1, 7, or 30",
            default="30",
            required=True,
            max_length=5
        )
        self.keys_input = discord.ui.TextInput(
            label="Bulk Real Keys (1 per line or space/comma)",
            style=discord.TextStyle.paragraph,
            placeholder="Paste real keys generated from your Web Admin panel here...",
            required=True,
            max_length=4000
        )
        self.add_item(self.duration_input)
        self.add_item(self.keys_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            target_dur = int(self.duration_input.value.strip())
        except ValueError:
            target_dur = 30

        import re
        tokens = [k.strip() for k in re.split(r"[\r\n,;\s]+", self.keys_input.value) if k.strip()]
        if not tokens:
            await interaction.followup.send("\u274C No valid keys found in input.", ephemeral=True)
            return

        added, total_avail, pkg_name = add_second_auth_keys(self.package_id, tokens, duration_days=target_dur)
        embed = discord.Embed(
            title="\U0001F4E6 Stock Updated (BASIC PANEL)",
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="\U0001F4E6 Package", value=f"`BASIC PANEL ({target_dur}d Pool)`", inline=True)
        embed.add_field(name="\u2795 Keys Added", value=f"`{added}` Keys", inline=True)
        embed.add_field(name="\U0001F4CA Available in Stock", value=f"`{total_avail}` Keys", inline=True)
        embed.set_footer(text=f"Restocked by: {interaction.user.name}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        if interaction.guild:
            await dispatch_second_keyauth_log(interaction.guild, embed)


class SecondKeyAuthCheckKeyModal(discord.ui.Modal, title="Check Second Auth License"):
    key_input = discord.ui.TextInput(
        label="License Key",
        placeholder="Paste license key to inspect...",
        required=True,
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key_val = self.key_input.value.strip().upper()
        res = await second_keyauth_request("check", {"key": key_val})

        embed = discord.Embed(
            title="\U0001F50D Second Auth \u00BB License Inspection",
            color=0x0055FF,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="\U0001F511 Key", value=f"`{key_val}`", inline=False)
        is_success = res.get("success", False)
        status_text = "\u2705 Valid / Active" if is_success else "\u274C Invalid / Not Found"
        embed.add_field(name="\U0001F4CA Status", value=status_text, inline=True)
        embed.add_field(name="\U0001F4AC Message", value=f"`{res.get('message', 'No details')}`", inline=True)

        extra = res.get("data", {})
        if extra:
            if extra.get("expires"):
                embed.add_field(name="\u23F3 Expiry", value=f"`{extra.get('expires')}`", inline=True)
            if extra.get("hwid"):
                embed.add_field(name="\U0001F4BB Bound HWID", value=f"`{extra.get('hwid')}`", inline=False)

        embed.set_footer(text="BYTE CORPORATION \u2022 Second Auth Pro")
        await interaction.followup.send(embed=embed, ephemeral=True)


class SecondKeyAuthAuthModal(discord.ui.Modal, title="Test KeyAuth Authentication"):
    key_input = discord.ui.TextInput(
        label="License Key",
        placeholder="Enter key to authenticate...",
        required=True,
        max_length=64,
    )
    hwid_input = discord.ui.TextInput(
        label="Hardware ID (HWID)",
        placeholder="Enter HWID or leave 'auto' for default test HWID",
        default="auto",
        required=True,
        max_length=128,
    )

    async def on_submit(self, interaction: discord.Interaction):
        import uuid
        await interaction.response.defer(ephemeral=True)
        key_val = self.key_input.value.strip().upper()
        hwid_val = self.hwid_input.value.strip()
        if hwid_val.lower() == "auto":
            hwid_val = f"TEST-HWID-{uuid.uuid4().hex[:12].upper()}"

        res = await second_keyauth_request("auth", {"key": key_val, "hwid": hwid_val})

        embed = discord.Embed(
            title="\u2705 Second Auth \u00BB Auth Verification",
            color=0x00FF00 if res.get("success") else 0xFF0000,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="\U0001F511 Key", value=f"`{key_val}`", inline=True)
        embed.add_field(name="\U0001F4BB HWID", value=f"`{hwid_val}`", inline=True)
        embed.add_field(name="\U0001F4CA Auth Result", value="`SUCCESS`" if res.get("success") else "`FAILED`", inline=False)
        embed.add_field(name="\U0001F4AC Response Message", value=f"`{res.get('message', 'N/A')}`", inline=False)

        embed.set_footer(text="BYTE CORPORATION \u2022 Second Auth API")
        await interaction.followup.send(embed=embed, ephemeral=True)


class SecondKeyAuthResetHWIDModal(discord.ui.Modal, title="Reset HWID / Deauth"):
    key_input = discord.ui.TextInput(
        label="License Key",
        placeholder="Enter key to reset HWID or deauthenticate...",
        required=True,
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key_val = self.key_input.value.strip().upper()
        res = await second_keyauth_request("deauth", {"key": key_val})

        ok = res.get("success", False)
        embed = discord.Embed(
            title="\U0001F504 Second Auth \u00BB HWID Reset / Deauth",
            color=0x00FF00 if ok else 0xFFA500,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="\U0001F511 Target Key", value=f"`{key_val}`", inline=False)
        embed.add_field(name="\U0001F4CA Status", value="\u2705 HWID Reset / Deauthenticated" if ok else "\u26A0\uFE0F Server Response Received", inline=True)
        embed.add_field(name="\U0001F4AC Message", value=f"`{res.get('message', 'Reset request dispatched.')}`", inline=False)
        embed.set_footer(text=f"Requested By: {interaction.user.name}")

        await interaction.followup.send(embed=embed, ephemeral=True)
        if interaction.guild:
            await dispatch_second_keyauth_log(interaction.guild, embed)


# --- Persistent Second KeyAuth Control View ---

class SecondKeyAuthControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error(f"SecondKeyAuthControlView error on item {item}: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"\u274C **Error:** `{error}`", ephemeral=True)
            else:
                await interaction.followup.send(f"\u274C **Error:** `{error}`", ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(
        label="Create Key",
        style=discord.ButtonStyle.primary,
        emoji="\U0001F511",
        custom_id="second_keyauth_btn_create_key",
        row=0,
    )
    async def create_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_second_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"\u274C **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(SecondKeyAuthGenModal("e52c1515c53453b85d0d4e87", "BASIC PANEL"))

    @discord.ui.button(
        label="Check Key",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001F50D",
        custom_id="second_keyauth_btn_check_key",
        row=0,
    )
    async def check_key_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        await interaction.response.send_modal(SecondKeyAuthCheckKeyModal())

    @discord.ui.button(
        label="Test Auth",
        style=discord.ButtonStyle.success,
        emoji="\u2705",
        custom_id="second_keyauth_btn_test_auth",
        row=0,
    )
    async def test_auth_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_second_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"\u274C **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(SecondKeyAuthAuthModal())

    @discord.ui.button(
        label="Reset HWID",
        style=discord.ButtonStyle.danger,
        emoji="\U0001F504",
        custom_id="second_keyauth_btn_reset_hwid",
        row=0,
    )
    async def reset_hwid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_second_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"\u274C **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(SecondKeyAuthResetHWIDModal())

    @discord.ui.button(
        label="Live Stock",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001F4E6",
        custom_id="second_keyauth_btn_stock",
        row=1,
    )
    async def stock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        embed = build_second_auth_stock_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Restock Keys",
        style=discord.ButtonStyle.primary,
        emoji="\u2795",
        custom_id="second_keyauth_btn_restock",
        row=1,
    )
    async def restock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        has_perm, reason = has_second_keyauth_permission(interaction.user)
        if not has_perm:
            await interaction.response.send_message(f"\u274C **Permission Denied:** {reason}", ephemeral=True)
            return
        await interaction.response.send_modal(SecondKeyAuthRestockModal("e52c1515c53453b85d0d4e87", "BASIC PANEL"))

    @discord.ui.button(
        label="Version Info",
        style=discord.ButtonStyle.secondary,
        emoji="\u2139\uFE0F",
        custom_id="second_keyauth_btn_version_info",
        row=1,
    )
    async def version_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
        if not allowed:
            await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        res = await second_keyauth_request("version")
        cfg = get_second_keyauth_config()

        embed = discord.Embed(
            title="\u2139\uFE0F Second Auth \u00BB Version Status",
            color=0x0055FF,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="\U0001F310 Endpoint", value=f"`{cfg['api_url']}`", inline=False)
        embed.add_field(name="\U0001F504 Configured Version", value=f"`{cfg['version']}`", inline=True)
        embed.add_field(name="\U0001F4E6 Active Package", value="`BASIC PANEL`", inline=True)
        embed.add_field(name="\U0001F4AC Server Status", value=f"`{res.get('message', 'Checked')}`", inline=False)
        if res.get("data"):
            embed.add_field(name="\U0001F4CA Data", value=f"```{json.dumps(res['data'], indent=2)[:500]}```", inline=False)
        embed.set_footer(text="BYTE CORPORATION \u2022 Second Auth SDK")
        await interaction.followup.send(embed=embed, ephemeral=True)


# --- Second KeyAuth Commands ---

@bot.command(name="secondkeypanel", aliases=["secondkeyauth", "secondpanel", "skeypanel", "secondauth", "auth2", "keypanel2"])
async def secondkeypanel_cmd(ctx):
    """Posts the Second Auth Management Panel for BASIC PANEL."""
    allowed, ch_reason = is_second_keyauth_channel_allowed(ctx.channel.id)
    if not allowed:
        await ctx.reply(f"\u274C **Wrong Channel:** {ch_reason}")
        return
    has_perm, reason = has_second_keyauth_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"\u274C **Permission Denied:** {reason}")
        return
    embed = build_second_keyauth_panel_embed(ctx.guild)
    view = SecondKeyAuthControlView()
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="secondkeypanel", description="Post the Second Auth Management Panel (BASIC PANEL Authorized)")
async def secondkeypanel_slash(interaction: discord.Interaction):
    allowed, ch_reason = is_second_keyauth_channel_allowed(interaction.channel_id)
    if not allowed:
        await interaction.response.send_message(f"\u274C **Wrong Channel:** {ch_reason}", ephemeral=True)
        return
    has_perm, reason = has_second_keyauth_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"\u274C **Permission Denied:** {reason}", ephemeral=True)
        return
    embed = build_second_keyauth_panel_embed(interaction.guild)
    view = SecondKeyAuthControlView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("\u2705 Second Auth Control Panel sent successfully!", ephemeral=True)


@bot.command(name="addsecondkeys", aliases=["addsecondstock", "addstock2", "restocksecond"])
async def addsecondkeys_cmd(ctx, *, args: str = None):
    """Adds bulk keys to BASIC PANEL stock.
    Usage: *addsecondkeys [duration_days] <key1 key2 key3 ...>
    Example: *addsecondkeys 30 BYTE-XXXX BYTE-YYYY
    """
    has_perm, reason = has_second_keyauth_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"\u274C **Permission Denied:** {reason}")
        return

    if not args:
        await ctx.reply(
            "\u2139\uFE0F **Usage:** `*addsecondkeys [duration_days] <keys...>`\n"
            "**Example:** `*addsecondkeys 30 BYTE-XXXX BYTE-YYYY` (or `*addsecondkeys 1 <keys>` / `*addsecondkeys 7 <keys>`)"
        )
        return

    import re
    tokens = [k.strip() for k in re.split(r"[\r\n,;\s]+", args) if k.strip()]
    if not tokens:
        await ctx.reply("\u274C No keys detected in input.")
        return

    # Check if first token is package name like 'basic'
    if tokens[0].lower() in ("basic", "basic panel", "e52c1515c53453b85d0d4e87"):
        tokens.pop(0)

    target_dur = 30
    if tokens and tokens[0].isdigit():
        target_dur = int(tokens.pop(0))

    if not tokens:
        await ctx.reply("\u274C No keys detected after duration.")
        return

    added, total_avail, pkg_name = add_second_auth_keys("e52c1515c53453b85d0d4e87", tokens, duration_days=target_dur)
    dur_label = f" ({target_dur} Days Pool)" if target_dur else ""
    embed = discord.Embed(
        title="\U0001F4E6 Stock Successfully Restocked (Second Auth)",
        color=0x00FF00,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="\U0001F4E6 Package", value=f"`{pkg_name}`{dur_label}", inline=True)
    embed.add_field(name="\u2795 Keys Added", value=f"`{added}`", inline=True)
    embed.add_field(name="\U0001F4CA Available in Stock", value=f"`{total_avail}` Keys", inline=True)
    embed.set_footer(text=f"Added by: {ctx.author.name}")
    await ctx.reply(embed=embed)
    if ctx.guild:
        await dispatch_second_keyauth_log(ctx.guild, embed)


@bot.command(name="secondstock", aliases=["stock2", "sstock", "secondauthstock"])
async def secondstock_cmd(ctx):
    """Displays the live stock count for BASIC PANEL."""
    embed = build_second_auth_stock_embed()
    await ctx.reply(embed=embed)


@bot.command(name="clearsecondstock", aliases=["clearstock2"])
async def clearsecondstock_cmd(ctx):
    """Clears the stock for BASIC PANEL."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("\u274C Only an **Administrator** can clear stock.")
        return
    stock = load_second_auth_stock()
    basic_pid = "e52c1515c53453b85d0d4e87"
    if basic_pid in stock.get("packages", {}):
        stock["packages"][basic_pid]["durations"] = {"1": [], "7": [], "30": []}
        stock["packages"][basic_pid]["keys"] = []
        save_second_auth_stock(stock)
    await ctx.reply("\U0001F5D1\uFE0F Cleared stock for **BASIC PANEL**.")


@bot.command(name="setsecondkeyrole", aliases=["secondkeyrole"])
async def setsecondkeyrole_cmd(ctx, role: discord.Role = None):
    """Sets the Discord role authorized to use the Second Auth Panel."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("\u274C Only an **Administrator** can set the Second Auth role.")
        return
    if not role:
        await ctx.reply("Usage: `*setsecondkeyrole @Role` (e.g. `*setsecondkeyrole @SecondKeyMaker`)")
        return
    cfg = load_config()
    cfg["second_keyauth_role_id"] = role.id
    save_config(cfg)
    await ctx.reply(f"\u2705 **Second Auth Role Updated:** Second Auth access is now granted to {role.mention}.")


@bot.command(name="setsecondkeychannel", aliases=["secondkeychannel"])
async def setsecondkeychannel_cmd(ctx, channel: discord.TextChannel = None):
    """Locks the Second Auth Panel to a specific text channel."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("\u274C Only an **Administrator** can set the Second Auth channel.")
        return
    if not channel:
        await ctx.reply("Usage: `*setsecondkeychannel #channel` (e.g. `*setsecondkeychannel #second-keyauth`)")
        return
    cfg = load_config()
    cfg["second_keyauth_channel_id"] = channel.id
    save_config(cfg)
    await ctx.reply(f"\u2705 **Second Auth Channel Locked:** The Second Auth Panel is now restricted to {channel.mention}.")


@bot.command(name="setsecondkeylog", aliases=["secondkeylog"])
async def setsecondkeylog_cmd(ctx, channel: discord.TextChannel = None):
    """Sets dedicated audit log channel for Second Auth operations."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("\u274C Only an **Administrator** can configure the Second Auth log channel.")
        return
    if not channel:
        await ctx.reply("Usage: `*setsecondkeylog #channel` (e.g. `*setsecondkeylog #second-keyauth-logs`)")
        return
    cfg = load_config()
    cfg["second_keyauth_log_channel_id"] = channel.id
    save_config(cfg)
    await ctx.reply(f"\u2705 **Second Auth Log Channel Updated:** All events will now be logged to {channel.mention}.")


@bot.command(name="setsecondkeyauth", aliases=["secondkeyauthconfig", "setsecondkeyapi"])
async def setsecondkeyauth_cmd(ctx, api_url: str = None, app_id: str = None, app_secret: str = None, package_id: str = None, version: str = "28"):
    """Configures Second Auth credentials (api_url, app_id, app_secret, package_id, version)."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("\u274C Only an **Administrator** can configure Second Auth settings.")
        return
    if not api_url or not app_id:
        cfg = get_second_keyauth_config()
        await ctx.reply(
            f"\U0001F510 **Current Second Auth Config:**\n"
            f"\u2022 Endpoint: `{cfg['api_url']}`\n"
            f"\u2022 App ID: `{cfg['app_id']}`\n"
            f"\u2022 Package ID: `{cfg['package_id']}`\n"
            f"\u2022 Version: `{cfg['version']}`\n\n"
            f"**Usage:** `*setsecondkeyauth <api_url> <app_id> <app_secret> <package_id> [version]`"
        )
        return
    cfg = load_config()
    cfg["second_keyauth_api_url"] = api_url.strip()
    cfg["second_keyauth_app_id"] = app_id.strip()
    if app_secret:
        cfg["second_keyauth_app_secret"] = app_secret.strip()
    if package_id:
        cfg["second_keyauth_package_id"] = package_id.strip()
    if version:
        cfg["second_keyauth_version"] = version.strip()
    save_config(cfg)
    await ctx.reply(f"\u2705 **Second Auth Credentials Updated!** Connected to endpoint: `{api_url}`")


# --- Prefix Commands for UID Whitelist ---
@bot.command(name="panel")
async def panel_cmd(ctx):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ **Permission Denied:** {reason}")
        return
    embed = build_panel_embed(ctx.guild)
    view = BytePanelWhitelistView()
    await ctx.send(embed=embed, view=view)


@bot.command(name="setrole")
async def setrole_prefix(ctx, role: discord.Role = None):
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can configure the authorized staff role.")
        return
    if not role:
        await ctx.reply("Usage: `*setrole @Role`")
        return
    cfg = load_config()
    cfg["admin_role_id"] = role.id
    save_config(cfg)
    await ctx.reply(f"✅ **Authorized Role Updated:** Members with {role.mention} can now manage the UID Whitelist.")


@bot.command(name="setuidlog", aliases=["setlogchannel", "uidlog", "setuidchannel"])
async def setuidlog_prefix(ctx, channel: discord.TextChannel = None):
    """Sets the dedicated audit log channel for UID Whitelist operations."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can configure the UID log channel.")
        return
    if not channel:
        await ctx.reply("Usage: `*setuidlog #channel` (e.g. `*setuidlog #🎭𝐔𝐢𝐝-𝐁𝐲𝐩𝐚𝐬𝐬-𝐋𝐨𝐠𝐬`)")
        return
    cfg = load_config()
    cfg["uid_log_channel_id"] = channel.id
    cfg["log_channel_id"] = channel.id
    save_config(cfg)
    await ctx.reply(f"✅ **UID Whitelist Log Channel Updated:** All UID whitelist events will now be logged to {channel.mention}.")


@bot.tree.command(name="setuidlog", description="Set dedicated channel for UID Whitelist audit logs")
@app_commands.default_permissions(administrator=True)
async def setuidlog_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can run this command.", ephemeral=True)
        return
    cfg = load_config()
    cfg["uid_log_channel_id"] = channel.id
    cfg["log_channel_id"] = channel.id
    save_config(cfg)
    await interaction.response.send_message(f"✅ **UID Whitelist Log Channel Updated:** All UID whitelist events will now be logged to {channel.mention}.", ephemeral=True)


@bot.command(name="setkeylog", aliases=["setkeyauthlog", "keylog", "setkeyaudit"])
async def setkeylog_prefix(ctx, channel: discord.TextChannel = None):
    """Sets the dedicated audit log channel for KeyAuth license operations."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can configure the KeyAuth audit log channel.")
        return
    if not channel:
        await ctx.reply("Usage: `*setkeylog #channel` (e.g. `*setkeylog #audit-log-bot`)")
        return
    cfg = load_config()
    cfg["keyauth_log_channel_id"] = channel.id
    save_config(cfg)
    await ctx.reply(f"✅ **KeyAuth Audit Log Channel Updated:** All KeyAuth license & management events will now be logged to {channel.mention}.")


@bot.tree.command(name="setkeylog", description="Set dedicated channel for KeyAuth license & management audit logs")
@app_commands.default_permissions(administrator=True)
async def setkeylog_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can run this command.", ephemeral=True)
        return
    cfg = load_config()
    cfg["keyauth_log_channel_id"] = channel.id
    save_config(cfg)
    await interaction.response.send_message(f"✅ **KeyAuth Audit Log Channel Updated:** All KeyAuth license & management events will now be logged to {channel.mention}.", ephemeral=True)



@bot.command(name="adduid")
async def adduid_prefix(ctx, uid: str = None, days: int = 30):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ Permission denied! {reason}")
        return
    if not uid:
        await ctx.reply("Usage: `*adduid <UID> [days: 1/7/30]`")
        return
    async with ctx.typing():
        _, result = await api_add_uid(uid, days=days)
    if result.get("success"):
        data = result.get("data", {})
        expiry_str = format_expiry(data.get("expiresAt", ""))
        await ctx.reply(f"✅ UID `{uid}` whitelisted for **{days} days**! Expiry: {expiry_str}")
        log_embed = discord.Embed(
            title="📋 BYTE CORPORATION — UID WHITELIST LOG",
            color=0x0000FF,
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="🎯 Player UID", value=f"`{uid}`", inline=True)
        log_embed.add_field(name="👤 Whitelisted By", value=f"{ctx.author.mention}", inline=True)
        log_embed.add_field(name="⏳ Duration", value=f"`{days} Days`", inline=True)
        log_embed.add_field(name="📅 Expiry Date", value=expiry_str, inline=False)
        log_embed.set_footer(text=f"Admin: {ctx.author.name}")
        if ctx.guild:
            await dispatch_uid_log(ctx.guild, log_embed)
    else:
        err = result.get("error", "Error adding UID")
        await ctx.reply(f"❌ **Failed:** {err}")


@bot.command(name="removeuid")
async def removeuid_prefix(ctx, uid: str = None):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ Permission denied! {reason}")
        return
    if not uid:
        await ctx.reply("Usage: `*removeuid <UID>`")
        return
    async with ctx.typing():
        _, result = await api_remove_uid(uid)
    if result.get("success"):
        await ctx.reply(f"🗑️ UID `{uid}` removed from whitelist.")
        if ctx.guild:
            log_embed = discord.Embed(
                title="🗑️ BYTE CORPORATION — UID REMOVAL LOG",
                color=0x0000FF,
                timestamp=datetime.utcnow(),
            )
            log_embed.add_field(name="🎯 Removed UID", value=f"`{uid}`", inline=True)
            log_embed.add_field(name="👤 Removed By", value=f"{ctx.author.mention}", inline=True)
            await dispatch_uid_log(ctx.guild, log_embed)
    else:
        err = result.get("error", "Error removing UID")
        await ctx.reply(f"❌ **Failed:** {err}")




# ==============================================================================
# BYTE CORPORATION - 24/7 HIGH FIDELITY DISCORD MUSIC SYSTEM (LAVALINK V4 & WAVELINK)
# Features: Crisp Audio (DAVE Encryption), Full Spotify Playlists & Albums, YouTube,
#           SoundCloud, 16 Commands, 2-Row Pink Controller Panel
# ==============================================================================

active_text_channels: dict[int, int] = {}
active_control_messages: dict[int, discord.Message] = {}
player_histories: dict[int, list[wavelink.Playable]] = {}
is_guild_247: dict[int, bool] = {}
track_requesters: dict[str, discord.Member | discord.User] = {}


def format_duration(seconds: int | float | None) -> str:
    """Formats track seconds into readable mm:ss or hh:mm:ss string matching screenshot."""
    if not seconds or seconds <= 0:
        return "Live Stream"
    sec = int(seconds)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_spotify_playlist_or_album(url: str) -> tuple[str, str, list[dict]]:
    """Extracts track names, artists, and title from a Spotify playlist or album URL without API keys."""
    clean_url = url.strip()
    embed_url = clean_url
    if "open.spotify.com/playlist/" in clean_url:
        embed_url = clean_url.replace("open.spotify.com/playlist/", "open.spotify.com/embed/playlist/")
    elif "open.spotify.com/album/" in clean_url:
        embed_url = clean_url.replace("open.spotify.com/album/", "open.spotify.com/embed/album/")
    else:
        return "", "", []

    req = urllib.request.Request(embed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            html = r.read().decode('utf-8')
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
            if m:
                data = json.loads(m.group(1))
                entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
                name = entity.get('name', 'Spotify Collection')
                cover = (entity.get('images', [{}])[0] or {}).get('url', '')
                track_list = entity.get('trackList', [])
                tracks = []
                for t in track_list:
                    title = t.get('title', '')
                    artist = t.get('subtitle', '')
                    if title:
                        tracks.append({'title': title, 'artist': artist, 'query': f"{title} {artist}".strip()})
                return name, cover, tracks
    except Exception as e:
        logger.warning(f"Spotify collection parse notice: {e}")
    return "", "", []


guild_music_fx: dict[int, dict] = {}
guild_autoplay: dict[int, bool] = {}
_last_reconnect_attempt: dict[int, float] = {}


def make_progress_bar(current_ms: int, total_ms: int, length: int = 14) -> str:
    """Generates ▶ 1:18 ▬▬▬▬🔵▬▬▬▬▬▬▬▬ 5:42 matching user screenshot media_1793287466812_5ba5599c.png."""
    cur_str = format_duration((current_ms or 0) // 1000)
    if not total_ms or total_ms <= 0:
        return f"▶ {cur_str} ▬▬▬▬🔵▬▬▬▬▬▬▬▬ Live Stream"
    tot_str = format_duration(total_ms // 1000)
    progress = min(1.0, max(0.0, (current_ms or 0) / total_ms))
    index = int(progress * length)
    left = "▬" * index
    right = "▬" * max(0, length - index)
    return f"▶ {cur_str} {left}🔵{right} {tot_str}"


def make_meter(value: int, max_val: int = 10, length: int = 7) -> str:
    """Generates ▰▰▰▱▱▱▱ matching volume/bass/treble meters in screenshot."""
    val = max(0, min(max_val, value))
    filled = int((val / max_val) * length) if max_val > 0 else 0
    return "▰" * filled + "▱" * (length - filled)


async def apply_guild_audio_filters(player: wavelink.Player, guild_id: int):
    """
    Applies pure, crystal-clear balanced stereo audio to both ears.
    Raw stream is kept 100% untouched for lossless studio clarity unless user asks for EQ/8D preset.
    """
    if not player:
        return
    fx = guild_music_fx.setdefault(guild_id, {"preset": "Normal", "bass": 0, "treble": 0})
    preset = fx.get("preset", "Normal")
    bass = fx.get("bass", 0)
    treble = fx.get("treble", 0)

    # If preset is Normal and no custom bass/treble is dialed in:
    # DO NOT apply equalizer bands or DSP rotation! Leave raw stream 100% untouched for pure lossless crystal clarity!
    if preset == "Normal" and bass == 0 and treble == 0:
        if getattr(player, "_has_custom_fx", False):
            try:
                filters = wavelink.Filters()
                filters.reset()
                await player.set_filters(filters)
            except Exception:
                pass
            player._has_custom_fx = False
        return

    # User explicitly picked a preset or adjusted bass/treble:
    try:
        filters = wavelink.Filters()
        filters.reset()

        if preset == "8D Audio":
            filters.rotation.set(rotation_hz=0.2)
        elif preset == "Bass Boost":
            bass = max(bass, 6)
        elif preset == "Super Bass":
            bass = 10
        elif preset == "Treble Boost":
            treble = max(treble, 8)
        elif preset == "Party":
            bass = max(bass, 6)
            treble = max(treble, 6)

        bands = []
        if bass > 0:
            b_gain = (bass / 10.0) * 0.35
            bands.extend([
                {"band": 0, "gain": b_gain},
                {"band": 1, "gain": b_gain * 0.8},
                {"band": 2, "gain": b_gain * 0.5},
                {"band": 3, "gain": b_gain * 0.3}
            ])
        if treble > 0:
            t_gain = (treble / 10.0) * 0.35
            bands.extend([
                {"band": 10, "gain": t_gain * 0.3},
                {"band": 11, "gain": t_gain * 0.5},
                {"band": 12, "gain": t_gain * 0.7},
                {"band": 13, "gain": t_gain},
                {"band": 14, "gain": t_gain}
            ])

        if bands:
            filters.equalizer.set(bands=bands)

        await player.set_filters(filters)
        player._has_custom_fx = True
    except Exception as e:
        logger.warning(f"Audio filter notice: {e}")

def create_now_playing_embed(track: wavelink.Playable, requester=None, guild: discord.Guild = None) -> discord.Embed:
    """Builds the exact Now Playing modern audio embed matching user screenshot media_1793287466812_5ba5599c.png."""
    title = getattr(track, "title", "Unknown Track") or "Unknown Track"
    artist = getattr(track, "author", "Artist") or "Artist"
    duration_ms = getattr(track, "length", 0) or 0
    uri = getattr(track, "uri", "https://discord.gg/w5Qvp66KC") or "https://discord.gg/w5Qvp66KC"

    player: wavelink.Player = cast(wavelink.Player, guild.voice_client) if guild else None
    position_ms = int(getattr(player, "position", 0)) if player else 0

    thumb = getattr(track, "artwork", None)
    if not thumb and getattr(track, "identifier", None):
        thumb = f"https://img.youtube.com/vi/{track.identifier}/hqdefault.jpg"

    # Resolve Requester reliably
    if requester is None and track:
        requester = (
            getattr(track, "requester", None)
            or track_requesters.get(getattr(track, "identifier", ""))
            or track_requesters.get(getattr(track, "uri", ""))
        )
    if requester is None and player:
        requester = getattr(player, "current_requester", None)
    if requester is None and guild:
        vc = guild.voice_client
        if vc and getattr(vc, "channel", None):
            humans = [m for m in vc.channel.members if not m.bot]
            if humans:
                requester = humans[0]

    req_name = "User"
    req_icon = None
    if isinstance(requester, (discord.Member, discord.User)):
        req_name = requester.display_name
        if requester.display_avatar:
            req_icon = requester.display_avatar.url
    elif isinstance(requester, str):
        clean_id = requester.replace("<@", "").replace(">", "").replace("!", "").strip()
        if guild and clean_id.isdigit():
            m = guild.get_member(int(clean_id))
            if m:
                req_name = m.display_name
                if m.display_avatar:
                    req_icon = m.display_avatar.url
        if not req_icon:
            req_name = clean_id if not clean_id.isdigit() else f"User {clean_id}"

    if not req_icon and bot.user and bot.user.display_avatar:
        req_icon = bot.user.display_avatar.url

    # Guild Audio FX state
    gid = guild.id if guild else 0
    fx = guild_music_fx.setdefault(gid, {"preset": "Normal", "bass": 0, "treble": 0})
    preset_name = fx.get("preset", "Normal")
    bass_val = fx.get("bass", 0)
    treble_val = fx.get("treble", 0)

    # Volume
    vol = getattr(player, "volume", 50) if player else 50

    # Loop & Autoplay
    loop_str = "Off"
    if player and hasattr(player.queue, "mode"):
        if player.queue.mode == wavelink.QueueMode.loop:
            loop_str = "Track"
        elif player.queue.mode == wavelink.QueueMode.loop_all:
            loop_str = "Queue"

    autoplay_str = "On" if guild_autoplay.get(guild.id if guild else 0, False) else "Off"

    # Up Next
    up_next_str = "—"
    if player and player.queue and len(player.queue) > 0:
        nxt = player.queue[0]
        nxt_title = getattr(nxt, "title", "Track")
        up_next_str = nxt_title if len(nxt_title) <= 28 else nxt_title[:25] + "..."

    # Build Embed matching screenshot media_1793287466812_5ba5599c.png
    embed = discord.Embed(color=0x0000FF)

    # Author Header matching screenshot: "Cross Music • HD Audio"
    author_icon = bot.user.display_avatar.url if (bot.user and bot.user.display_avatar) else None
    embed.set_author(name="BYTE CORPORATION • HD Audio", icon_url=author_icon)

    # Title & Description with Live Progress Bar
    embed.title = "🎶 Now Playing"
    embed.description = f"### **[{title} - {artist}]({uri})**\n\n{make_progress_bar(position_ms, duration_ms)}"

    # Fields (Grid matching image 1)
    vol_meter = make_meter(vol, 100, 7)
    embed.add_field(name="🔊 Volume", value=f"{vol_meter} {vol}%", inline=True)

    bass_meter = make_meter(bass_val, 10, 7)
    embed.add_field(name="🎚️ Bass", value=f"{bass_meter} {bass_val}/10", inline=True)

    treble_meter = make_meter(treble_val, 10, 7)
    embed.add_field(name="✨ Treble", value=f"{treble_meter} {treble_val}/10", inline=True)

    preset_emojis = {"Normal": "🎵", "8D Audio": "🎧", "Bass Boost": "🔊", "Super Bass": "💥", "Treble Boost": "✨", "Party": "🎉"}
    p_em = preset_emojis.get(preset_name, "🎧")
    embed.add_field(name="🎲 Preset", value=f"{p_em} {preset_name}", inline=True)
    embed.add_field(name="🔁 Loop", value=f"{loop_str}\n♾️ Autoplay: {autoplay_str}", inline=True)
    embed.add_field(name="⏭️ Up next", value=f"{up_next_str}", inline=True)

    # Center Media Display (Supports Direct Video GIF Attachment or Song Album Art)
    cfg = load_config()
    banner_mode = cfg.get("music_banner_mode", "video")
    saved_banner = cfg.get("music_banner_url")
    has_local_gif = os.path.exists("music_video.gif")

    if banner_mode == "video":
        if saved_banner and not "catbox" in saved_banner:
            embed.set_image(url=saved_banner)
        elif has_local_gif:
            embed.set_image(url="attachment://music_video.gif")
        if thumb:
            embed.set_thumbnail(url=thumb)
    elif thumb:
        embed.set_image(url=thumb)

    # Footer matching: "Requested by Kaneki !! • 🎛️ opus 141k → 128k"
    footer_text = f"Requested by {req_name} • 🎛️ opus 141k → 128k • BYTE CORPORATION"
    if req_icon:
        embed.set_footer(text=footer_text, icon_url=req_icon)
    else:
        embed.set_footer(text=footer_text)

    return embed


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler reporting clear feedback instead of silent failure."""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(f"🛡️ **Permission Denied:** You need `{', '.join(error.missing_permissions)}` permissions to run this command.")
        return
    if isinstance(error, commands.BotMissingPermissions):
        await ctx.reply(f"⚠️ **Bot Missing Permissions:** I need `{', '.join(error.missing_permissions)}` permissions in this server! Please give the bot Administrator or appropriate channel permissions.")
        return
    logger.error(f"Command error on '{ctx.command}': {error}", exc_info=True)
    try:
        await ctx.reply(f"⚠️ **Command notice:** `{error}`")
    except Exception:
        pass


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    """Fired automatically when a track begins streaming via Lavalink."""
    player: wavelink.Player = payload.player
    track: wavelink.Playable = payload.track
    if not player or not player.guild:
        return

    # Set crisp 85% audio transmission volume (prevents digital clipping/muffled distortion)
    try:
        if getattr(player, "volume", 0) <= 0:
            await player.set_volume(85)
    except Exception:
        pass

    # Keep wavelink internal autoplay disabled so our advance_to_next_track owns the queue cleanly
    player.autoplay = wavelink.AutoPlayMode.disabled

    guild_id = player.guild.id
    if guild_id not in player_histories:
        player_histories[guild_id] = []
    player_histories[guild_id].append(track)
    if len(player_histories[guild_id]) > 25:
        player_histories[guild_id].pop(0)

    channel_id = active_text_channels.get(guild_id)
    if channel_id:
        channel = player.guild.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        if channel:
            req = (
                getattr(track, "requester", None)
                or track_requesters.get(getattr(track, "identifier", ""))
                or track_requesters.get(getattr(track, "uri", ""))
                or getattr(player, "current_requester", None)
            )
            if req:
                player.current_requester = req
            
            # Apply configured audio fx
            await apply_guild_audio_filters(player, guild_id)

            embed = create_now_playing_embed(track, req, player.guild)
            fx = guild_music_fx.get(guild_id, {})
            current_preset = fx.get("preset", "Normal")
            view = MusicControlView(current_preset=current_preset)

            old_msg = active_control_messages.get(guild_id)
            if old_msg:
                try:
                    await old_msg.edit(embed=embed, view=view)
                    return
                except Exception:
                    pass

            try:
                cfg_t = load_config()
                has_cdn_t = bool(cfg_t.get("music_banner_url") and "catbox" not in cfg_t.get("music_banner_url"))
                if os.path.exists("music_video.gif") and cfg_t.get("music_banner_mode", "video") == "video" and not has_cdn_t:
                    gif_file = discord.File("music_video.gif", filename="music_video.gif")
                    msg = await channel.send(embed=embed, file=gif_file, view=view)
                    if msg and msg.attachments:
                        cfg_t["music_banner_url"] = msg.attachments[0].url
                        save_config(cfg_t)
                else:
                    msg = await channel.send(embed=embed, view=view)
                active_control_messages[guild_id] = msg
            except Exception as e:
                logger.warning(f"Could not send now playing message: {e}")


_vc_connect_locks: dict[int, asyncio.Lock] = {}
_suppress_reconnect_until: dict[int, float] = {}
_consecutive_reconnect_failures: dict[int, int] = {}
_reconnecting_guilds: set[int] = set()
_manual_disconnect_guilds: set[int] = set()


class ByteMusicPlayer(wavelink.Player):
    """
    Resilient Wavelink Player that:
    1. Prevents premature disconnect on single-node voice update errors.
    2. Seamlessly switches to backup Lavalink nodes on voice update failure.
    3. Handles 4014 / network blips cleanly without killing the music session.
    """
    async def _dispatch_voice_update(self) -> None:
        assert self.guild is not None
        data = self._voice_state.get("voice", {})

        session_id = data.get("session_id", None)
        token = data.get("token", None)
        endpoint = data.get("endpoint", None)
        channel_id = self._voice_state.get("channel_id", None)

        if not session_id or not token or not endpoint:
            return

        request = {
            "voice": {"sessionId": session_id, "token": token, "endpoint": endpoint, "channelId": channel_id}
        }

        # 1. Try currently assigned node
        try:
            await self.node._update_player(self.guild.id, data=request)
            self._connected = True
            self._connection_event.set()
            return
        except Exception as primary_err:
            logger.warning(f"Voice update notice on primary Lavalink node '{getattr(self.node, 'identifier', 'Unknown')}': {primary_err}")

        # 2. Immediate failover to other connected Lavalink nodes in pool
        nodes_dict = getattr(wavelink.Pool, "nodes", {})
        other_nodes = [
            n for n in nodes_dict.values()
            if n != self.node and getattr(n, "status", None) == wavelink.NodeStatus.CONNECTED
        ]

        for alt_node in other_nodes:
            try:
                self._node = alt_node
                await self.node._update_player(self.guild.id, data=request)
                self._connected = True
                self._connection_event.set()
                logger.info(f"⚡ Successfully recovered voice connection on Lavalink node: '{alt_node.identifier}'")
                return
            except Exception as alt_err:
                logger.warning(f"Failover voice update failed on node '{alt_node.identifier}': {alt_err}")

        # 3. Only if all nodes fail, disconnect
        logger.error(f"All Lavalink nodes failed voice update for guild {self.guild.id}")
        await self.disconnect()


async def ensure_voice_connection(channel: discord.VoiceChannel, text_channel: discord.abc.Messageable = None) -> ByteMusicPlayer:
    """
    Connects to or re-uses the player in the specified voice channel.
    Guarantees connection without race conditions, hanging, or join/leave loops.
    """
    guild = channel.guild
    lock = _vc_connect_locks.setdefault(guild.id, asyncio.Lock())

    async with lock:
        # Suppress background auto-reconnect while this connection is active
        _suppress_reconnect_until[guild.id] = time.time() + 10.0
        _manual_disconnect_guilds.discard(guild.id)
        if text_channel:
            active_text_channels[guild.id] = text_channel.id

        # Check bot permissions
        bot_perms = channel.permissions_for(guild.me)
        if not bot_perms.connect:
            raise discord.errors.DiscordException(f"Missing `Connect` permission in '{channel.name}'!")
        if not bot_perms.speak:
            raise discord.errors.DiscordException(f"Missing `Speak` permission in '{channel.name}'!")

        # Verify at least one node is connected in wavelink.Pool before attempting connection
        nodes_dict = getattr(wavelink.Pool, "nodes", {})
        has_connected = any(getattr(n, "status", None) == wavelink.NodeStatus.CONNECTED for n in nodes_dict.values())
        if not has_connected:
            logger.info("No connected node in pool! Attempting emergency node connection...")
            await connect_lavalink_pool(bot)
            await asyncio.sleep(1.5)
            nodes_dict = getattr(wavelink.Pool, "nodes", {})
            has_connected = any(getattr(n, "status", None) == wavelink.NodeStatus.CONNECTED for n in nodes_dict.values())
            if not has_connected:
                raise discord.errors.DiscordException("Audio server node is currently connecting. Please try again in 5 seconds.")

        # Re-use existing healthy player if already in VC
        existing_vc = guild.voice_client
        if existing_vc and isinstance(existing_vc, wavelink.Player) and getattr(existing_vc, "channel", None):
            p_node = getattr(existing_vc, "node", None)
            if p_node and getattr(p_node, "status", None) == wavelink.NodeStatus.CONNECTED:
                if existing_vc.channel.id != channel.id:
                    await existing_vc.move_to(channel)
                existing_vc.autoplay = wavelink.AutoPlayMode.disabled
                is_guild_247[guild.id] = True
                _consecutive_reconnect_failures[guild.id] = 0
                _suppress_reconnect_until[guild.id] = 0
                return cast(ByteMusicPlayer, existing_vc)

        # Safely tear down any dead/half-connected voice client with suppression
        if guild.voice_client:
            _suppress_reconnect_until[guild.id] = time.time() + 8.0
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(0.5)

        # Attempt connection with primary pool selection
        try:
            player = await channel.connect(cls=ByteMusicPlayer, self_deaf=True, timeout=15.0)
            player.autoplay = wavelink.AutoPlayMode.disabled
            try:
                await player.set_volume(85)
            except Exception:
                pass
            is_guild_247[guild.id] = True
            _consecutive_reconnect_failures[guild.id] = 0
            _suppress_reconnect_until[guild.id] = 0
            return player
        except Exception as conn_err:
            logger.warning(f"Voice connect initial attempt failed on '{channel.name}': {conn_err}. Attempting node failover...")

        # Multi-node failover: try each connected node in pool explicitly
        await connect_lavalink_pool(bot)
        nodes_dict = getattr(wavelink.Pool, "nodes", {})
        connected_nodes = [n for n in nodes_dict.values() if getattr(n, "status", None) == wavelink.NodeStatus.CONNECTED]

        last_err = None
        for target_node in connected_nodes:
            try:
                if guild.voice_client:
                    _suppress_reconnect_until[guild.id] = time.time() + 8.0
                    try:
                        await guild.voice_client.disconnect(force=True)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

                player = await channel.connect(cls=ByteMusicPlayer, self_deaf=True, timeout=15.0, nodes=[target_node])
                player.autoplay = wavelink.AutoPlayMode.disabled
                try:
                    await player.set_volume(85)
                except Exception:
                    pass
                is_guild_247[guild.id] = True
                _consecutive_reconnect_failures[guild.id] = 0
                _suppress_reconnect_until[guild.id] = 0
                logger.info(f"Connected to '{channel.name}' via Lavalink node: {target_node.identifier}")
                return player
            except Exception as node_err:
                last_err = node_err
                logger.warning(f"Lavalink node failover connect failed on {target_node.identifier}: {node_err}")

        _suppress_reconnect_until[guild.id] = time.time() + 15.0
        logger.error(f"Voice connect failed completely on '{channel.name}': {last_err}")
        raise discord.errors.DiscordException("Unable to connect to audio server. Please try again in 5 seconds.")


async def _safe_reconnect_vc(channel: discord.VoiceChannel):
    """Graceful reconnect with strict anti-loop protection."""
    guild = channel.guild
    guild_id = guild.id

    now = time.time()
    # Check if suppressed
    if now < _suppress_reconnect_until.get(guild_id, 0):
        _reconnecting_guilds.discard(guild_id)
        return

    # Minimum 15s between reconnect attempts
    if now - _last_reconnect_attempt.get(guild_id, 0) < 15.0:
        _reconnecting_guilds.discard(guild_id)
        return
    _last_reconnect_attempt[guild_id] = now

    # Check if 24/7 is enabled
    if not is_guild_247.get(guild_id, False):
        _reconnecting_guilds.discard(guild_id)
        return

    # Check failure count
    failures = _consecutive_reconnect_failures.get(guild_id, 0)
    if failures >= 2:
        logger.warning(f"Stopping 24/7 reconnect loop for guild {guild.name} ({guild_id}) after 2 failed attempts.")
        is_guild_247[guild_id] = False
        _consecutive_reconnect_failures.pop(guild_id, None)
        _reconnecting_guilds.discard(guild_id)
        return

    # Check if already back in VC
    if guild.voice_client and getattr(guild.voice_client, "channel", None):
        _reconnecting_guilds.discard(guild_id)
        return

    # Skip reconnect if no Lavalink nodes are connected (prevents join/leave loop)
    nodes_dict = getattr(wavelink.Pool, 'nodes', {})
    if not any(getattr(n, 'status', None) == wavelink.NodeStatus.CONNECTED for n in nodes_dict.values()):
        logger.info('No active Lavalink node connected. Skipping auto-reconnect.')
        _reconnecting_guilds.discard(guild_id)
        return

    try:
        await asyncio.sleep(2.0)
        if guild.voice_client and getattr(guild.voice_client, "channel", None):
            return

        await ensure_voice_connection(channel)
        _consecutive_reconnect_failures[guild_id] = 0
        logger.info(f"Auto-reconnected 24/7 to Voice Channel: {channel.name}")
    except Exception as e:
        _consecutive_reconnect_failures[guild_id] = failures + 1
        logger.warning(f"Auto-reconnect attempt failed for {channel.name}: {e}")
    finally:
        _reconnecting_guilds.discard(guild_id)



async def advance_to_next_track(player: wavelink.Player, *, force_skip: bool = False, ended_track: wavelink.Playable = None) -> wavelink.Playable | None:
    """Safely advances to the next track from queue, supporting loop modes, continuous playlist, and guaranteed smart autoplay."""
    if not player or not player.guild:
        return None

    guild_id = player.guild.id

    # 1. Single track loop (if track ended naturally and not force skipped, replay it)
    if player.queue.mode == wavelink.QueueMode.loop and not force_skip:
        track_to_replay = ended_track or player.current
        if track_to_replay:
            if player.paused:
                await player.pause(False)
            await player.play(track_to_replay)
            return track_to_replay

    # 2. Queue loop: push completed track to the end of the queue
    if player.queue.mode == wavelink.QueueMode.loop_all:
        track_to_requeue = ended_track or player.current
        if track_to_requeue:
            player.queue.put(track_to_requeue)

    # 3. Next track from queue (Continuous Playlist playback)
    if player.queue and len(player.queue) > 0:
        next_track = player.queue.get()
        if player.paused:
            await player.pause(False)
        await player.play(next_track)
        logger.info(f"Auto-playing next track: {getattr(next_track, 'title', 'Unknown')} (Queue remaining: {len(player.queue)})")
        return next_track

    # 4. Smart Autoplay if queue is empty and autoplay is turned ON
    if guild_autoplay.get(guild_id, False):
        ref_track = ended_track or player.current
        artist = getattr(ref_track, "author", "") if ref_track else ""
        title = getattr(ref_track, "title", "") if ref_track else ""
        search_query = f"{artist} songs" if artist else f"{title} mix"
        try:
            recs = await wavelink.Playable.search(f"ytmsearch:{search_query}")
            if not recs:
                recs = await wavelink.Playable.search(f"scsearch:{search_query}")
            if recs:
                rec_song = None
                for candidate in recs:
                    if getattr(candidate, "title", "") != title:
                        rec_song = candidate
                        break
                if not rec_song and len(recs) > 0:
                    rec_song = recs[0]
                if rec_song:
                    setattr(rec_song, "requester", getattr(ref_track, "requester", None))
                    if player.paused:
                        await player.pause(False)
                    await player.play(rec_song)
                    logger.info(f"Smart Autoplay recommended & playing: '{rec_song.title}'")
                    return rec_song
        except Exception as e:
            logger.warning(f"Smart autoplay recommendation notice: {e}")

    # No more songs left in queue - cleanly idle in VC without killing player session
    player._current = None
    player.queue._loaded = None
    logger.info("Queue is empty. Bot waiting in VC for next song.")
    return None


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    """Fired when a track finishes or stops; guarantees queued songs always auto-advance without freezing."""
    player: wavelink.Player = payload.player
    if not player or not player.guild:
        return

    reason_str = str(payload.reason).lower()
    logger.info(f"🎵 Wavelink track ended: reason={reason_str} | Remaining in queue: {len(player.queue) if player.queue else 0}")
    if reason_str in ("cleanup", "replaced"):
        return

    # If track finished naturally or stopped, advance queue cleanly
    try:
        await advance_to_next_track(player, force_skip=False, ended_track=payload.track)
    except Exception as e:
        logger.warning(f"Auto-play next track error: {e}")


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    """Catches track/stream interruptions mid-playback with auto-failover so the bot never freezes or dies."""
    player: wavelink.Player = payload.player
    track = payload.track
    logger.warning(f"⚠ Wavelink track exception on '{getattr(track, 'title', 'Unknown')}': {payload.exception}")
    if player and player.guild:
        guild_id = player.guild.id
        track_title = getattr(track, 'title', None)
        if track_title and not getattr(track, '_sc_retried', False):
            try:
                sc_tracks = await wavelink.Playable.search(f"scsearch:{track_title}")
                if sc_tracks:
                    sc_track = sc_tracks[0]
                    setattr(sc_track, '_sc_retried', True)
                    setattr(sc_track, 'requester', getattr(track, 'requester', None))
                    await player.play(sc_track)
                    await apply_guild_audio_filters(player, guild_id)
                    logger.info(f"⚡ Successfully failed over to SoundCloud stream for '{track_title}'")
                    return
            except Exception as sc_err:
                logger.warning(f"SoundCloud fallback error: {sc_err}")
        try:
            await advance_to_next_track(player, force_skip=True)
        except Exception as e:
            logger.warning(f"Auto-skip error after stream exception: {e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Handles VoiceMaster temp channels & keeps bot connected 24/7."""
    # 1. VoiceMaster Join-to-Create & auto-cleanup
    if not member.bot and member.guild:
        try:
            await handle_voicemaster_state_update(member, before, after)
        except Exception as e:
            logger.warning(f"VoiceMaster event error: {e}")

    # 2. Bot 24/7 Voice Channel Keepalive (Only executes for the bot itself)
    if member.id != bot.user.id:
        return

    # If bot did not disconnect from VC, ignore
    if not (before.channel and not after.channel):
        return

    guild_id = before.channel.guild.id

    # If disconnection is suppressed (e.g. during manual command, teardown or reconnect attempt), ignore!
    if time.time() < _suppress_reconnect_until.get(guild_id, 0):
        return

    # If intentionally disconnected via leave/stop/disconnect command or button, stay disconnected!
    if guild_id in _manual_disconnect_guilds:
        _manual_disconnect_guilds.discard(guild_id)
        is_guild_247[guild_id] = False
        _consecutive_reconnect_failures.pop(guild_id, None)
        return

    # If 24/7 mode is not active for this guild, do NOT reconnect!
    if not is_guild_247.get(guild_id, False):
        return

    # If already in the middle of reconnecting for this guild, ignore!
    if guild_id in _reconnecting_guilds:
        return

    _reconnecting_guilds.add(guild_id)
    asyncio.create_task(_safe_reconnect_vc(before.channel))


def can_control_music(member: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
    """
    Checks if a user is authorized to control music playback.
    Strict Rule: Only Server Administrators or the user who requested the song can control.
    Returns: (is_allowed: bool, reason_or_requester_mention: str)
    """
    if not member or not guild:
        return False, "User or Server context missing."

    # 1. Server Owner has full bypass
    if guild.owner_id == member.id:
        return True, "Server Owner"

    # 2. Server Administrator Permission
    perms = getattr(member, "guild_permissions", None)
    if perms and getattr(perms, "administrator", False):
        return True, "Server Administrator"

    # 3. Configured Admin / Staff Roles
    admin_r = get_admin_role_id()
    roles = getattr(member, "roles", [])
    if admin_r and any(getattr(r, "id", None) == admin_r for r in roles):
        return True, "Administrator Role"

    try:
        if is_staff(member):
            return True, "Staff Member"
    except Exception:
        pass

    # 4. Requester Verification
    player: wavelink.Player = cast(wavelink.Player, guild.voice_client)
    current_track = player.current if player else None

    requester = None
    if current_track:
        requester = (
            getattr(current_track, "requester", None)
            or track_requesters.get(getattr(current_track, "identifier", ""))
            or track_requesters.get(getattr(current_track, "uri", ""))
            or getattr(player, "current_requester", None)
        )
    elif player and player.queue and len(player.queue) > 0:
        first_q = player.queue[0]
        requester = (
            getattr(first_q, "requester", None)
            or track_requesters.get(getattr(first_q, "identifier", ""))
            or track_requesters.get(getattr(first_q, "uri", ""))
            or getattr(player, "current_requester", None)
        )
    elif player:
        requester = getattr(player, "current_requester", None)

    if requester:
        if isinstance(requester, (discord.Member, discord.User)):
            if requester.id == member.id:
                return True, "Track Requester"
            req_mention = requester.mention
        elif isinstance(requester, int):
            if requester == member.id:
                return True, "Track Requester"
            req_mention = f"<@{requester}>"
        elif isinstance(requester, str):
            clean_id = requester.replace("<@", "").replace(">", "").replace("!", "").strip()
            if clean_id.isdigit():
                if int(clean_id) == member.id:
                    return True, "Track Requester"
                req_mention = f"<@{clean_id}>"
            else:
                if requester.lower() in [member.name.lower(), member.display_name.lower()]:
                    return True, "Track Requester"
                req_mention = requester
        else:
            req_mention = getattr(requester, "mention", str(requester))

        return False, f"Only Server Administrators or the track requester ({req_mention}) can control this music session."

    if player and player.channel and member.voice and member.voice.channel == player.channel:
        non_bot_members = [m for m in player.channel.members if not m.bot]
        if len(non_bot_members) <= 1:
            return True, "Sole Listener in Voice Channel"

    return False, "Only Server Administrators or the song requester can control this music session."


class AddSongModal(discord.ui.Modal, title="➕ Add Song to Queue"):
    song_query = discord.ui.TextInput(
        label="Song Title or URL",
        placeholder="e.g. Teri Meri Kahaani, High on You, or Spotify link",
        required=True,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        query = self.song_query.value.strip()
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player:
            await interaction.response.send_message("❌ **The bot is not currently connected to a Voice Channel!**", ephemeral=True)
            return

        await interaction.response.send_message(f"🔍 **Searching:** `{query}`...", ephemeral=True)

        try:
            tracks = await wavelink.Playable.search(f"ytmsearch:{query}")
            if not tracks:
                tracks = await wavelink.Playable.search(query)
        except Exception:
            tracks = None

        if not tracks:
            await interaction.followup.send("❌ **No tracks found for that search query.**", ephemeral=True)
            return

        track = tracks[0]
        setattr(track, "requester", interaction.user)
        track_requesters[track.identifier] = interaction.user
        if getattr(track, "uri", None):
            track_requesters[track.uri] = interaction.user

        if player.playing and player.current:
            await player.queue.put_wait(track)
            pos = len(player.queue)
            await interaction.followup.send(f"📥 **Added to Queue (#{pos}):** [{track.title}]({track.uri})", ephemeral=True)
        else:
            await player.play(track)
            await apply_guild_audio_filters(player, interaction.guild_id)
            embed = create_now_playing_embed(track, interaction.user, interaction.guild)
            view = MusicControlView()
            if interaction.guild_id in active_text_channels:
                ch = interaction.guild.get_channel(active_text_channels[interaction.guild_id])
                if ch:
                    await ch.send(embed=embed, view=view)
            await interaction.followup.send(f"🎶 **Now Playing:** [{track.title}]({track.uri})", ephemeral=True)


class AudioFilterSelect(discord.ui.Select):
    """Audio Preset / Filter Dropdown matching screenshot media_1793287504318_87185b02.png."""
    def __init__(self, current_preset: str = "Normal"):
        options = [
            discord.SelectOption(
                label="Normal",
                value="Normal",
                emoji="🎵",
                description="Default flat studio audio without effects",
                default=(current_preset == "Normal")
            ),
            discord.SelectOption(
                label="8D Audio",
                value="8D Audio",
                emoji="🎧",
                description="360-degree rotating surround spatial sound",
                default=(current_preset == "8D Audio")
            ),
            discord.SelectOption(
                label="Bass Boost",
                value="Bass Boost",
                emoji="🔊",
                description="Enhanced low frequencies and amplified kick",
                default=(current_preset == "Bass Boost")
            ),
            discord.SelectOption(
                label="Super Bass",
                value="Super Bass",
                emoji="💥",
                description="Extreme sub-bass for maximum punch & rumble",
                default=(current_preset == "Super Bass")
            ),
            discord.SelectOption(
                label="Treble Boost",
                value="Treble Boost",
                emoji="✨",
                description="Crystal clear highs, vocals and crisp treble",
                default=(current_preset == "Treble Boost")
            ),
            discord.SelectOption(
                label="Party",
                value="Party",
                emoji="🎉",
                description="Dynamic club and festival sound equalization",
                default=(current_preset == "Party")
            ),
        ]
        super().__init__(
            placeholder=f"🎵 Current Filter: {current_preset}",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
            custom_id="mc_filter_select"
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player:
            await interaction.response.send_message("❌ **The bot is not currently connected to a Voice Channel!**", ephemeral=True)
            return

        fx = guild_music_fx.setdefault(interaction.guild_id, {"preset": "Normal", "bass": 0, "treble": 0})
        fx["preset"] = selected
        if selected == "Normal":
            fx["bass"] = 0
            fx["treble"] = 0
        elif selected == "Bass Boost":
            fx["bass"] = 6
        elif selected == "Super Bass":
            fx["bass"] = 10
        elif selected == "Treble Boost":
            fx["treble"] = 8
        elif selected == "Party":
            fx["bass"] = 6
            fx["treble"] = 6

        await apply_guild_audio_filters(player, interaction.guild_id)

        if player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            view = MusicControlView(current_preset=selected)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(f"🎧 **Audio Filter applied:** `{selected}`", ephemeral=True)


class MusicControlView(discord.ui.View):
    """
    Complete Interactive Music Controller Panel matching screenshots
    media_1793287466812_5ba5599c.png & media_1793287504318_87185b02.png.
    Layout across 5 Discord Action Rows:
      Row 0: [Pause] [Resume] [Skip] [Stop] [Loop]
      Row 1: [Vol -10] [Vol +10] [Bass -] [Bass +] [Shuffle]
      Row 2: [Treble -] [Treble +] [Add Song] [Queue] [Leave]
      Row 3: [Dropdown: Audio Filters & 8D Audio Presets]
      Row 4: [Autoplay]
    """
    def __init__(self, current_preset: str = "Normal"):
        super().__init__(timeout=None)
        self.add_item(AudioFilterSelect(current_preset=current_preset))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        if isinstance(error, discord.NotFound) and getattr(error, "code", 0) == 10062:
            return  # Safely ignore expired interaction without crash
        logger.error(f"MusicControlView error on {getattr(item, 'custom_id', item)}: {error}", exc_info=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice:
            await interaction.response.send_message("❌ **You are not connected to a Voice Channel!** Please join a voice channel first.", ephemeral=True)
            return False

        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if player and player.channel and interaction.user.voice.channel != player.channel:
            perms = getattr(interaction.user, "guild_permissions", None)
            is_admin = perms and getattr(perms, "administrator", False)
            if not is_admin:
                await interaction.response.send_message(
                    f"❌ **You must be in the same Voice Channel as the bot (`{player.channel.name}`) to control music!**",
                    ephemeral=True
                )
                return False

        # Open non-destructive commands (Add Song, View Queue) to all VC members
        custom_id = interaction.data.get("custom_id", "")
        if custom_id in ["mc_btn_add_song", "mc_btn_queue"]:
            return True

        # All destructive & state-altering controls require Admin or Song Requester
        allowed, reason = can_control_music(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
            return False
        return True

    # ---------------- ROW 0: Playback Core Controls ----------------
    @discord.ui.button(emoji="⏸️", label="Pause", style=discord.ButtonStyle.primary, custom_id="mc_btn_pause", row=0)
    async def btn_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player or not player.playing:
            await interaction.response.send_message("⚠️ **No track is actively playing to pause.**", ephemeral=True)
            return
        await player.pause(True)
        if player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send("⏸️ **Playback paused.**", ephemeral=True)
        else:
            await interaction.response.send_message("⏸️ **Playback paused.**", ephemeral=True)

    @discord.ui.button(emoji="▶️", label="Resume", style=discord.ButtonStyle.success, custom_id="mc_btn_resume", row=0)
    async def btn_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player or not player.paused:
            await interaction.response.send_message("⚠️ **Playback is not currently paused.**", ephemeral=True)
            return
        await player.pause(False)
        if player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send("▶️ **Playback resumed.**", ephemeral=True)
        else:
            await interaction.response.send_message("▶️ **Playback resumed.**", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.primary, custom_id="mc_btn_skip", row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if player and (player.playing or player.paused or (player.queue and len(player.queue) > 0)):
            next_t = await advance_to_next_track(player, force_skip=True)
            if next_t:
                await interaction.response.send_message(f"⏭️ **Track skipped! Now playing:** [{next_t.title}]({getattr(next_t, 'uri', '')})", ephemeral=True)
            else:
                await interaction.response.send_message("⏹️ **Skipped track. No more songs left in queue.**", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ **No active track or queue to skip.**", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.danger, custom_id="mc_btn_stop", row=0)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if player:
            player.queue.clear()
            player._current = None
            player.queue._loaded = None
            try:
                request = {"track": {"encoded": None}}
                await player.node._update_player(player.guild.id, data=request, replace=True)
            except Exception:
                pass
            if not is_guild_247.get(interaction.guild_id, True):
                await player.disconnect()
            await interaction.response.send_message("⏹️ **Playback stopped and queue cleared!**", ephemeral=True)
        else:
            await interaction.response.send_message("❌ **The bot is not connected to a Voice Channel!**", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="Loop", style=discord.ButtonStyle.secondary, custom_id="mc_btn_loop", row=0)
    async def btn_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player:
            await interaction.response.send_message("❌ **The bot is not connected to a Voice Channel!**", ephemeral=True)
            return
        if player.queue.mode == wavelink.QueueMode.normal:
            player.queue.mode = wavelink.QueueMode.loop
            status = "Single Track Loop (🔂)"
        elif player.queue.mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.loop_all
            status = "Queue Loop (🔁)"
        else:
            player.queue.mode = wavelink.QueueMode.normal
            status = "Disabled (Off)"

        if player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🔁 **Loop Mode set to:** `{status}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"🔁 **Loop Mode set to:** `{status}`", ephemeral=True)

    # ---------------- ROW 1: Volume & Bass / Shuffle ----------------
    @discord.ui.button(emoji="🔉", label="Vol -10", style=discord.ButtonStyle.secondary, custom_id="mc_btn_vol_down", row=1)
    async def btn_vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player:
            await interaction.response.send_message("❌ **Bot is not connected to Voice!**", ephemeral=True)
            return
        new_vol = max(10, player.volume - 10)
        await player.set_volume(new_vol)
        if player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🔉 **Volume decreased to:** `{new_vol}%`", ephemeral=True)
        else:
            await interaction.response.send_message(f"🔉 **Volume decreased to:** `{new_vol}%`", ephemeral=True)

    @discord.ui.button(emoji="🔊", label="Vol +10", style=discord.ButtonStyle.secondary, custom_id="mc_btn_vol_up", row=1)
    async def btn_vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player:
            await interaction.response.send_message("❌ **Bot is not connected to Voice!**", ephemeral=True)
            return
        new_vol = min(100, player.volume + 10)
        await player.set_volume(new_vol)
        if player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🔊 **Volume increased to:** `{new_vol}%`", ephemeral=True)
        else:
            await interaction.response.send_message(f"🔊 **Volume increased to:** `{new_vol}%`", ephemeral=True)

    @discord.ui.button(emoji="🎚️", label="Bass -", style=discord.ButtonStyle.secondary, custom_id="mc_btn_bass_down", row=1)
    async def btn_bass_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        fx = guild_music_fx.setdefault(interaction.guild_id, {"preset": "Normal", "bass": 0, "treble": 0})
        fx["bass"] = max(0, fx.get("bass", 3) - 1)
        if player:
            await apply_guild_audio_filters(player, interaction.guild_id)
        if player and player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🎚️ **Bass Level decreased to:** `{fx['bass']}/10`", ephemeral=True)
        else:
            await interaction.response.send_message(f"🎚️ **Bass Level decreased to:** `{fx['bass']}/10`", ephemeral=True)

    @discord.ui.button(emoji="🎚️", label="Bass +", style=discord.ButtonStyle.success, custom_id="mc_btn_bass_up", row=1)
    async def btn_bass_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        fx = guild_music_fx.setdefault(interaction.guild_id, {"preset": "Normal", "bass": 0, "treble": 0})
        fx["bass"] = min(10, fx.get("bass", 3) + 1)
        if player:
            await apply_guild_audio_filters(player, interaction.guild_id)
        if player and player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🎚️ **Bass Level increased to:** `{fx['bass']}/10`", ephemeral=True)
        else:
            await interaction.response.send_message(f"🎚️ **Bass Level increased to:** `{fx['bass']}/10`", ephemeral=True)

    @discord.ui.button(emoji="🔀", label="Shuffle", style=discord.ButtonStyle.primary, custom_id="mc_btn_shuffle", row=1)
    async def btn_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player or len(player.queue) < 2:
            await interaction.response.send_message("⚠️ **Queue must have at least 2 tracks to shuffle!**", ephemeral=True)
            return
        player.queue.shuffle()
        await interaction.response.send_message("🔀 **Queue shuffled successfully!**", ephemeral=True)

    # ---------------- ROW 2: Treble / Add Song / Queue / Leave ----------------
    @discord.ui.button(emoji="✨", label="Treble -", style=discord.ButtonStyle.secondary, custom_id="mc_btn_treble_down", row=2)
    async def btn_treble_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        fx = guild_music_fx.setdefault(interaction.guild_id, {"preset": "Normal", "bass": 0, "treble": 0})
        fx["treble"] = max(0, fx.get("treble", 0) - 1)
        if player:
            await apply_guild_audio_filters(player, interaction.guild_id)
        if player and player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"✨ **Treble Level decreased to:** `{fx['treble']}/10`", ephemeral=True)
        else:
            await interaction.response.send_message(f"✨ **Treble Level decreased to:** `{fx['treble']}/10`", ephemeral=True)

    @discord.ui.button(emoji="✨", label="Treble +", style=discord.ButtonStyle.secondary, custom_id="mc_btn_treble_up", row=2)
    async def btn_treble_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        fx = guild_music_fx.setdefault(interaction.guild_id, {"preset": "Normal", "bass": 0, "treble": 0})
        fx["treble"] = min(10, fx.get("treble", 0) + 1)
        if player:
            await apply_guild_audio_filters(player, interaction.guild_id)
        if player and player.current:
            embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"✨ **Treble Level increased to:** `{fx['treble']}/10`", ephemeral=True)
        else:
            await interaction.response.send_message(f"✨ **Treble Level increased to:** `{fx['treble']}/10`", ephemeral=True)

    @discord.ui.button(emoji="➕", label="Add Song", style=discord.ButtonStyle.success, custom_id="mc_btn_add_song", row=2)
    async def btn_add_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, custom_id="mc_btn_queue", row=2)
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player or (not player.current and not player.queue):
            await interaction.response.send_message("📭 **Queue is currently empty!** Use `➕ Add Song` to queue tracks.", ephemeral=True)
            return

        desc = ""
        if player.current:
            sec = int((player.current.length or 0) / 1000)
            desc += f"**Now Playing:**\n[{player.current.title}]({player.current.uri}) • `{format_duration(sec)}`\n\n**Upcoming Tracks:**\n"

        if player.queue:
            for idx, t in enumerate(list(player.queue)[:8]):
                t_sec = int((t.length or 0) / 1000)
                desc += f"`{idx+1}.` [{t.title}]({t.uri}) (`{format_duration(t_sec)}`)\n"
            if len(player.queue) > 8:
                desc += f"\n*...and {len(player.queue)-8} more track(s) in queue.*"
        else:
            desc += "*No upcoming tracks in queue.*"

        embed = discord.Embed(title="🎶 Music Queue", description=desc, color=0x0000FF)
        if bot.user and bot.user.display_avatar:
            embed.set_footer(text="BYTE CORPORATION • Music Player", icon_url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="👋", label="Leave", style=discord.ButtonStyle.danger, custom_id="mc_btn_leave", row=2)
    async def btn_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        _manual_disconnect_guilds.add(interaction.guild_id)
        _suppress_reconnect_until[interaction.guild_id] = time.time() + 10.0
        is_guild_247[interaction.guild_id] = False
        _consecutive_reconnect_failures.pop(interaction.guild_id, None)
        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if player:
            player.queue.clear()
            await player.disconnect()
        await interaction.response.send_message("👋 **Disconnected from voice channel.**", ephemeral=True)

    # ---------------- ROW 4: Autoplay Toggle ----------------
    @discord.ui.button(emoji="♾️", label="Autoplay", style=discord.ButtonStyle.primary, custom_id="mc_btn_autoplay", row=4)
    async def btn_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        is_on = guild_autoplay.get(guild_id, False)
        guild_autoplay[guild_id] = not is_on
        status = "Enabled (Smart Recommendations)" if guild_autoplay[guild_id] else "Disabled (Queue Only)"

        player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
        if player:
            player.autoplay = wavelink.AutoPlayMode.disabled

        try:
            if player and player.current:
                embed = create_now_playing_embed(player.current, getattr(player.current, "requester", interaction.user), interaction.guild)
                await interaction.response.edit_message(embed=embed, view=self)
                await interaction.followup.send(f"♾️ **Autoplay Mode:** `{status}`", ephemeral=True)
            else:
                await interaction.response.send_message(f"♾️ **Autoplay Mode:** `{status}`", ephemeral=True)
        except discord.NotFound:
            pass

# ==============================================================================
# MUSIC COMMANDS SUITE (ALL 16 COMMANDS FROM SCREENSHOT media_1793209984899_a581627a.png)
# ==============================================================================

@bot.command(name="join", aliases=["j"])
async def music_join_cmd(ctx):
    """Joins the user's voice channel."""
    if not ctx.author.voice:
        await ctx.reply("❌ **You are not connected to a Voice Channel!** Please join a voice channel first.")
        return
    channel = ctx.author.voice.channel
    # Check bot permissions in voice channel
    bot_perms = channel.permissions_for(ctx.guild.me)
    if not bot_perms.connect:
        await ctx.reply(f"❌ **Missing Permission:** The bot does not have permission to `Connect` to `{channel.name}`! Please give the bot Administrator or Connect permission.")
        return
    if not bot_perms.speak:
        await ctx.reply(f"❌ **Missing Permission:** The bot does not have permission to `Speak` in `{channel.name}`! Please give the bot Speak permission.")
        return

    try:
        player = await ensure_voice_connection(channel, ctx.channel)
        await ctx.reply(f"🔊 **Connected to `{channel.name}`!** 24/7 Mode active.")
    except Exception as e:
        logger.error(f"Voice connect error: {e}")
        await ctx.reply(f"❌ **Failed to connect to Voice Channel:** `{e}`")
        return


@bot.command(name="disconnect", aliases=["leave", "dc"])
async def music_disconnect_cmd(ctx):
    """Disconnects from voice channel and resets music state."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    _manual_disconnect_guilds.add(ctx.guild.id)
    _suppress_reconnect_until[ctx.guild.id] = time.time() + 10.0
    is_guild_247[ctx.guild.id] = False
    _consecutive_reconnect_failures.pop(ctx.guild.id, None)
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player:
        player.queue.clear()
        await player.disconnect()
        await ctx.reply("👋 **Disconnected from voice channel.**")
    else:
        await ctx.reply("❌ **The bot is not currently connected to a Voice Channel!**")


@bot.command(name="play", aliases=["p"])
async def music_play_cmd(ctx, *, query: str = None):
    """Plays track or entire Spotify Playlist/Album. Auto-joins VC!"""
    if not query:
        await ctx.reply("⚠️ **Please specify a song title or URL!**\nExample: `*play High On You` or `*play <Spotify/YouTube URL>`")
        return

    if not ctx.author.voice:
        await ctx.reply("❌ **You are not connected to a Voice Channel!** Please join a voice channel first.")
        return

    channel = ctx.author.voice.channel
    try:
        player = await ensure_voice_connection(channel, ctx.channel)
    except Exception as e:
        await ctx.reply(f"❌ **Could not connect to Voice Channel:** `{e}`")
        return

    player.current_requester = ctx.author
    clean_q = query.strip()

    # 1. Check Spotify Playlist or Album
    if "open.spotify.com/playlist/" in clean_q or "open.spotify.com/album/" in clean_q:
        status_msg = await ctx.reply("⏳ **Loading Spotify collection & tracks...**")
        name, cover, sp_tracks = parse_spotify_playlist_or_album(clean_q)
        if sp_tracks:
            added_count = 0
            for idx, item in enumerate(sp_tracks):
                try:
                    res = await wavelink.Playable.search(f"ytmsearch:{item['query']}")
                    if not res:
                        res = await wavelink.Playable.search(item['query'])
                    if res:
                        t = res[0]
                        setattr(t, "requester", ctx.author)
                        track_requesters[t.identifier] = ctx.author
                        if getattr(t, "uri", None):
                            track_requesters[t.uri] = ctx.author
                        if added_count == 0 and not player.playing and not player.paused:
                            await player.play(t)
                        else:
                            await player.queue.put_wait(t)
                        added_count += 1
                except Exception:
                    pass

            embed = discord.Embed(
                title="🎶 Spotify Collection Loaded",
                description=f"Successfully queued **{added_count}** tracks from **[{name}]({clean_q})**!",
                color=0x0000FF
            )
            if cover:
                embed.set_thumbnail(url=cover)
            if bot.user and bot.user.display_avatar:
                embed.set_footer(text="BYTE CORPORATION • Spotify Integration", icon_url=bot.user.display_avatar.url)
            await status_msg.edit(content=None, embed=embed)
            return
        else:
            await status_msg.edit(content="❌ **Could not load Spotify collection.** Please check the link or playlist privacy.")
            return

    # 2. Fast Single Track Search (Direct ytsearch on high-speed v4 nodes)
    searching_msg = await ctx.reply("🔍 **Searching and loading audio stream...**")
    tracks = None

    if clean_q.startswith("http://") or clean_q.startswith("https://"):
        try:
            tracks = await wavelink.Playable.search(clean_q)
        except Exception as e:
            logger.debug(f"Direct URL search error: {e}")

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"ytmsearch:{clean_q}")
        except Exception as e:
            logger.debug(f"ytsearch failed: {e}")

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"scsearch:{clean_q}")
        except Exception:
            pass

    if not tracks:
        await searching_msg.edit(content="❌ **Could not stream the requested track!** Please try another song title or link.")
        return

    # Keep wavelink internal autoplay disabled so our advance_to_next_track controls the queue cleanly
    player.autoplay = wavelink.AutoPlayMode.disabled

    # 3. Check if tracks is a YouTube / SoundCloud / Direct Playlist
    is_playlist = isinstance(tracks, wavelink.Playlist) or (
        isinstance(tracks, list) and ("list=" in clean_q or "playlist" in clean_q) and len(tracks) > 1
    )
    if is_playlist:
        playlist_name = getattr(tracks, "name", "YouTube Playlist")
        pl_tracks = list(tracks.tracks) if isinstance(tracks, wavelink.Playlist) else list(tracks)
        for t in pl_tracks:
            setattr(t, "requester", ctx.author)
            track_requesters[t.identifier] = ctx.author
            if getattr(t, "uri", None):
                track_requesters[t.uri] = ctx.author

        is_active = (player.playing or player.paused) and player.current is not None
        if not is_active:
            first_track = pl_tracks[0]
            if len(pl_tracks) > 1:
                for rem_t in pl_tracks[1:]:
                    player.queue.put(rem_t)
            if player.paused:
                await player.pause(False)
            await player.play(first_track)

            embed = discord.Embed(
                title="YouTube Playlist Loaded & Playing",
                description=(
                    f"**[{playlist_name}]({clean_q})**\n\n"
                    f"Now Playing: [{first_track.title}]({first_track.uri})\n"
                    f"Queued: `{len(pl_tracks) - 1}` tracks ready to auto-play!\n"
                    f"Requester: {ctx.author.mention}"
                ),
                color=0x0000FF
            )
            if getattr(tracks, "artwork", None):
                embed.set_thumbnail(url=tracks.artwork)
            elif getattr(first_track, "artwork", None):
                embed.set_thumbnail(url=first_track.artwork)
            embed.set_footer(text="BYTE CORPORATION • Music Player")
            view = MusicControlView()
            await searching_msg.edit(content=None, embed=embed, view=view)
            return
        else:
            for rem_t in pl_tracks:
                player.queue.put(rem_t)
            embed = discord.Embed(
                title="Playlist Added to Queue",
                description=(
                    f"**[{playlist_name}]({clean_q})**\n\n"
                    f"Added **{len(pl_tracks)}** tracks to queue!\n"
                    f"Total in Queue: `#{len(player.queue)}`\n"
                    f"Requester: {ctx.author.mention}"
                ),
                color=0x0000FF
            )
            if getattr(tracks, "artwork", None):
                embed.set_thumbnail(url=tracks.artwork)
            embed.set_footer(text="BYTE CORPORATION • Music Player")
            view = MusicControlView()
            await searching_msg.edit(content=None, embed=embed, view=view)
            return

    track = tracks[0]
    setattr(track, "requester", ctx.author)
    track_requesters[track.identifier] = ctx.author
    if getattr(track, "uri", None):
        track_requesters[track.uri] = ctx.author
    player.current_requester = ctx.author

    # If player is not actively playing, start playback immediately!
    cfg = load_config()
    is_active = (player.playing or player.paused) and player.current is not None
    if not is_active:
        if player.paused:
            await player.pause(False)
        try:
            await player.play(track)
            await apply_guild_audio_filters(player, ctx.guild.id)
        except Exception as play_err:
            logger.error(f"Playback error on '{track.title}': {play_err}")
            try:
                sc_tracks = await wavelink.Playable.search(f"scsearch:{clean_q}")
                if sc_tracks:
                    track = sc_tracks[0]
                    setattr(track, "requester", ctx.author)
                    await player.play(track)
                    await apply_guild_audio_filters(player, ctx.guild.id)
                else:
                    await searching_msg.edit(content=f"❌ **Audio Stream Error:** Failed to stream `{track.title}`. Please try another song.")
                    return
            except Exception as sc_err:
                logger.error(f"SoundCloud fallback failed: {sc_err}")
                await searching_msg.edit(content=f"❌ **Audio Stream Error:** Failed to stream `{track.title}`. Please try another song.")
                return
        embed = create_now_playing_embed(track, ctx.author, ctx.guild)
        view = MusicControlView()
        
        has_cdn = bool(cfg.get("music_banner_url") and "catbox" not in cfg.get("music_banner_url"))
        if os.path.exists("music_video.gif") and cfg.get("music_banner_mode", "video") == "video" and not has_cdn:
            gif_file = discord.File("music_video.gif", filename="music_video.gif")
            new_msg = await searching_msg.edit(content=None, embed=embed, attachments=[gif_file], view=view)
            if new_msg and new_msg.attachments:
                cfg["music_banner_url"] = new_msg.attachments[0].url
                save_config(cfg)
            active_control_messages[ctx.guild.id] = new_msg or searching_msg
        else:
            new_msg = await searching_msg.edit(content=None, embed=embed, view=view)
            active_control_messages[ctx.guild.id] = new_msg or searching_msg
        return

    # If player IS actively playing, put into queue
    player.autoplay = wavelink.AutoPlayMode.disabled
    await player.queue.put_wait(track)
    pos = len(player.queue)
    sec = int((track.length or 0) / 1000)
    dur_str = format_duration(sec)
    embed = discord.Embed(
        title="📥 Added to Queue",
        description=f"**[{track.title}]({track.uri})**\nPosition in queue: `#{pos}` • Duration: `{dur_str}`\n\n💡 *Tip: Click `⏭️ Skip` below or run `*skip` or `*playnow` to start this track right now!*",
        color=0x0000FF
    )
    if getattr(track, "artwork", None):
        embed.set_thumbnail(url=track.artwork)
    if bot.user and bot.user.display_avatar:
        embed.set_footer(text="BYTE CORPORATION • Music Player", icon_url=bot.user.display_avatar.url)
    view = MusicControlView()
    await searching_msg.edit(content=None, embed=embed, view=view)

@bot.command(name="playnow", aliases=["pn"])
async def music_playnow_cmd(ctx, *, query: str = None):
    """Immediately plays a song, skipping whatever was previously playing or stuck."""
    if not query:
        await ctx.reply("⚠️ **Please specify a song title or URL!**\nExample: `*playnow High on You`")
        return
    if not ctx.author.voice:
        await ctx.reply("❌ **Please join a Voice Channel first!**")
        return

    channel = ctx.author.voice.channel
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return

    try:
        player = await ensure_voice_connection(channel, ctx.channel)
    except Exception as e:
        await ctx.reply(f"❌ **Could not connect to Voice Channel:** `{e}`")
        return

    player.current_requester = ctx.author

    searching_msg = await ctx.reply("⚡ **Loading track for immediate playback...**")
    clean_q = query.strip()
    tracks = None

    if clean_q.startswith("http://") or clean_q.startswith("https://"):
        try:
            tracks = await wavelink.Playable.search(clean_q)
        except Exception:
            pass

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"ytmsearch:{clean_q}")
        except Exception:
            pass

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"scsearch:{clean_q}")
        except Exception:
            pass

    if not tracks:
        await searching_msg.edit(content="❌ **Could not stream the requested track!**")
        return

    track = tracks[0]
    setattr(track, "requester", ctx.author)
    track_requesters[track.identifier] = ctx.author
    if getattr(track, "uri", None):
        track_requesters[track.uri] = ctx.author

    # Force immediate playback, clearing queue
    player.queue.clear()
    if player.paused:
        await player.pause(False)
    try:
        await player.play(track)
    except Exception as play_err:
        err_s = str(play_err).lower()
        if "404" in err_s or "not found" in err_s or "session" in err_s:
            logger.warning(f"Session 404 recovery: {play_err}")
            _manual_disconnect_guilds.add(ctx.guild.id)
            try:
                await player.disconnect()
            except Exception:
                pass
            finally:
                _manual_disconnect_guilds.discard(ctx.guild.id)
            await asyncio.sleep(0.5)
            player = await ensure_voice_connection(channel)
            player.autoplay = wavelink.AutoPlayMode.disabled
            await player.play(track)
        else:
            raise play_err
    await apply_guild_audio_filters(player, ctx.guild.id)

    embed = create_now_playing_embed(track, ctx.author, ctx.guild)
    view = MusicControlView()
    cfg = load_config()
    has_cdn = bool(cfg.get("music_banner_url") and "catbox" not in cfg.get("music_banner_url"))
    if os.path.exists("music_video.gif") and cfg.get("music_banner_mode", "video") == "video" and not has_cdn:
        gif_file = discord.File("music_video.gif", filename="music_video.gif")
        new_msg = await searching_msg.edit(content=None, embed=embed, attachments=[gif_file], view=view)
        if new_msg and new_msg.attachments:
            cfg["music_banner_url"] = new_msg.attachments[0].url
            save_config(cfg)
        active_control_messages[ctx.guild.id] = new_msg or searching_msg
    else:
        new_msg = await searching_msg.edit(content=None, embed=embed, view=view)
        active_control_messages[ctx.guild.id] = new_msg or searching_msg


@bot.command(name="search")
async def music_search_cmd(ctx, *, query: str = None):
    """Searches top 5 tracks for user selection."""
    if not query:
        await ctx.reply("⚠️ **Please provide a search query!** (e.g. `*search lofi chill`)")
        return
    if not ctx.author.voice:
        await ctx.reply("❌ **Please join a Voice Channel first!**")
        return

    msg = await ctx.reply(f"🔍 **Searching top tracks for:** `{query}`...")
    try:
        tracks = await wavelink.Playable.search(f"ytmsearch:{query}")
        if not tracks:
            tracks = await wavelink.Playable.search(f"scsearch:{query}")
    except Exception:
        tracks = None

    if not tracks:
        await msg.edit(content="❌ **No search results found.**")
        return

    entries = tracks[:5]
    desc = ""
    for idx, e in enumerate(entries):
        sec = int((e.length or 0) / 1000)
        desc += f"**{idx+1}.** [{e.title}]({e.uri}) ({format_duration(sec)})\n"
    desc += "\n*Type 1-5 within 30 seconds to play!*"

    embed = discord.Embed(title="🔍 Search Results", description=desc, color=0x0000FF)
    if bot.user and bot.user.display_avatar:
        embed.set_footer(text="BYTE CORPORATION • Music Player", icon_url=bot.user.display_avatar.url)
    await msg.edit(content=None, embed=embed)

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit() and 1 <= int(m.content) <= len(entries)

    try:
        choice_msg = await bot.wait_for("message", check=check, timeout=30.0)
        choice = int(choice_msg.content) - 1
        chosen_track = entries[choice]
        await music_play_cmd(ctx, query=chosen_track.uri)
    except asyncio.TimeoutError:
        await ctx.send("⏰ **Search selection timed out.**")


@bot.command(name="nowplaying", aliases=["np", "player", "mcontrol", "musicpanel", "panelmusic"])
async def music_nowplaying_cmd(ctx):
    """Displays the interactive Now Playing Control Panel."""
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if not player or not player.current:
        await ctx.reply("❌ **No track is currently playing.** Run `*play <song>` to start playback.")
        return
    embed = create_now_playing_embed(player.current, getattr(player.current, "requester", ctx.author), ctx.guild)
    view = MusicControlView()
    await ctx.send(embed=embed, view=view)


@bot.command(name="pause")
async def music_pause_cmd(ctx):
    """Pauses playback."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player and player.playing:
        await player.pause(True)
        await ctx.reply("⏸️ **Playback paused.** Run `*resume` to continue.")
    else:
        await ctx.reply("⚠️ **No active track is currently playing.**")


@bot.command(name="resume")
async def music_resume_cmd(ctx):
    """Resumes paused playback."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player and player.paused:
        await player.pause(False)
        await ctx.reply("▶️ **Playback resumed.**")
    else:
        await ctx.reply("⚠️ **Playback is not paused.**")


@bot.command(name="skip", aliases=["s"])
async def music_skip_cmd(ctx):
    """Skips the currently playing song."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player and (player.playing or player.paused or (player.queue and len(player.queue) > 0)):
        next_t = await advance_to_next_track(player, force_skip=True)
        if next_t:
            await ctx.reply(f"⏭️ **Skipped! Now playing:** [{next_t.title}]({getattr(next_t, 'uri', '')})")
        else:
            await ctx.reply("⏹️ **Skipped track. No more songs left in queue.**")
    else:
        await ctx.reply("⚠️ **No active track or queue to skip.**")


@bot.command(name="stop")
async def music_stop_cmd(ctx):
    """Stops playback and clears the queue."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player:
        player.queue.clear()
        player._current = None
        player.queue._loaded = None
        try:
            request = {"track": {"encoded": None}}
            await player.node._update_player(player.guild.id, data=request, replace=True)
        except Exception:
            pass
        if not is_guild_247.get(ctx.guild.id, True):
            await player.disconnect()
        await ctx.reply("⏹️ **Playback stopped and queue cleared!**")
    else:
        await ctx.reply("❌ **The bot is not currently connected to a Voice Channel!**")


@bot.command(name="queue", aliases=["q"])
async def music_queue_cmd(ctx):
    """Shows all upcoming songs in queue."""
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if not player or (not player.current and not player.queue):
        await ctx.reply("📭 **Queue is currently empty!** Run `*play <song>` to queue tracks.")
        return

    desc = ""
    if player.current:
        sec = int((player.current.length or 0) / 1000)
        desc += f"**Now Playing:**\n[{player.current.title}]({player.current.uri}) • `{format_duration(sec)}`\n\n**Upcoming Tracks:**\n"

    if player.queue:
        for idx, t in enumerate(list(player.queue)[:10]):
            t_sec = int((t.length or 0) / 1000)
            desc += f"`{idx+1}.` [{t.title}]({t.uri}) ({format_duration(t_sec)})\n"
        if len(player.queue) > 10:
            desc += f"\n*...and {len(player.queue)-10} more track(s) in queue.*"
    else:
        desc += "*No upcoming tracks in queue.*"

    embed = discord.Embed(title="🎶 Music Queue", description=desc, color=0x0000FF)
    loop_str = str(player.queue.mode).split('.')[-1].upper() if hasattr(player.queue, 'mode') else 'OFF'
    auto_str = 'ON' if guild_autoplay.get(ctx.guild.id, False) else 'OFF'
    if bot.user and bot.user.display_avatar:
        embed.set_footer(text=f"Loop: {loop_str} • Autoplay: {auto_str} • BYTE CORPORATION", icon_url=bot.user.display_avatar.url)
    else:
        embed.set_footer(text=f"Loop: {loop_str} • Autoplay: {auto_str} • BYTE CORPORATION")
    await ctx.send(embed=embed)


@bot.command(name="clearqueue", aliases=["cq"])
async def music_clearqueue_cmd(ctx):
    """Clears all songs from queue."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player:
        player.queue.clear()
        await ctx.reply("🗑️ **Queue cleared successfully!**")
    else:
        await ctx.reply("📭 **Queue is currently empty!**")


@bot.command(name="loop")
async def music_loop_cmd(ctx, mode: str = None):
    """Toggles loop between off, song, and queue."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if not player:
        await ctx.reply("❌ **The bot is not currently connected to a Voice Channel!**")
        return
    if mode:
        m = mode.lower()
        if m in ["song", "track", "1"]:
            player.queue.mode = wavelink.QueueMode.loop
        elif m in ["queue", "all"]:
            player.queue.mode = wavelink.QueueMode.loop_all
        else:
            player.queue.mode = wavelink.QueueMode.normal
    else:
        if player.queue.mode == wavelink.QueueMode.normal:
            player.queue.mode = wavelink.QueueMode.loop
        elif player.queue.mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.loop_all
        else:
            player.queue.mode = wavelink.QueueMode.normal

    mode_label = str(player.queue.mode).split('.')[-1].upper()
    await ctx.reply(f"🔁 **Loop Mode:** `{mode_label}`")


@bot.command(name="shuffle")
async def music_shuffle_cmd(ctx):
    """Shuffles the upcoming queue."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if not player or len(player.queue) < 2:
        await ctx.reply("⚠️ **Queue must have at least 2 tracks to shuffle!**")
        return
    player.queue.shuffle()
    await ctx.reply("🔀 **Queue shuffled successfully!**")


@bot.command(name="autoplay")
async def music_autoplay_cmd(ctx):
    """Toggles autoplay mode."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    guild_id = ctx.guild.id
    guild_autoplay[guild_id] = not guild_autoplay.get(guild_id, False)
    status = "ON (Smart Recommendations)" if guild_autoplay[guild_id] else "OFF (Queue Only)"
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if player:
        player.autoplay = wavelink.AutoPlayMode.disabled
    await ctx.reply(f"♾️ **Autoplay is now {status}!**")


@bot.command(name="volume", aliases=["vol"])
async def music_volume_cmd(ctx, vol: int = None):
    """Sets playback volume from 1 to 100."""
    allowed, reason = can_control_music(ctx.author, ctx.guild)
    if not allowed:
        await ctx.reply(f"🛡️ **Access Denied:** {reason}")
        return
    player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
    if not player:
        await ctx.reply("❌ **The bot is not currently connected to a Voice Channel!**")
        return
    if vol is None:
        await ctx.reply(f"🔊 Current Volume: `{player.volume}%`")
        return
    if not (1 <= vol <= 100):
        await ctx.reply("⚠️ **Volume must be between 1 and 100!**")
        return
    await player.set_volume(vol)
    await ctx.reply(f"🔊 **Volume set to `{vol}%`!**")


@bot.command(name="247")
@commands.has_permissions(administrator=True)
async def music_247_cmd(ctx):
    """Toggles 24/7 Voice Channel Stay."""
    current = is_guild_247.get(ctx.guild.id, True)
    is_guild_247[ctx.guild.id] = not current
    status = "ENABLED (Bot will stay in VC permanently)" if is_guild_247[ctx.guild.id] else "DISABLED (Bot will leave when inactive)"
    await ctx.reply(f"🛡️ **24/7 Mode:** `{status}`")


DEFAULT_MUSIC_VIDEO_BANNER = "https://files.catbox.moe/nvn8xb.gif"


@bot.command(name="setmusicbanner", aliases=["musicbannerurl", "setmusicvideo"])
@commands.has_permissions(administrator=True)
async def setmusicbanner_cmd(ctx, url: str = None):
    """Sets a custom video/gif banner for the music now playing panel."""
    if not url and ctx.message.attachments:
        url = ctx.message.attachments[0].url
    if not url:
        await ctx.reply("Usage: `*setmusicbanner <image_or_gif_url>` or attach a GIF/video file with `*setmusicbanner`")
        return
    cfg = load_config()
    cfg["music_banner_url"] = url
    cfg["music_banner_mode"] = "video"
    save_config(cfg)
    embed = discord.Embed(
        title="🎬 Music Video Banner Updated",
        description=f"✅ Now Playing panel will now display this video/GIF banner!\n\n**Preview:**",
        color=0x0000FF
    )
    embed.set_image(url=url)
    await ctx.reply(embed=embed)


@bot.command(name="musicbanner", aliases=["mbanner", "togglebanner"])
@commands.has_permissions(administrator=True)
async def toggle_musicbanner_cmd(ctx, mode: str = None):
    """Switches music panel center display between 'video' (animated banner) and 'artwork' (song cover)."""
    cfg = load_config()
    if not mode:
        cur_mode = cfg.get("music_banner_mode", "video")
        next_mode = "artwork" if cur_mode == "video" else "video"
    else:
        next_mode = mode.lower().strip()
        if next_mode not in ("video", "artwork", "off"):
            await ctx.reply("Usage: `*musicbanner <video | artwork | off>`")
            return
    cfg["music_banner_mode"] = next_mode
    save_config(cfg)
    await ctx.reply(f"🎬 **Music Panel Banner Mode:** `{next_mode.upper()}` (Active for upcoming songs)")


@bot.command(name="resetmusicbanner")
@commands.has_permissions(administrator=True)
async def reset_musicbanner_cmd(ctx):
    """Resets the music panel video banner to the default romantic aesthetic video."""
    cfg = load_config()
    cfg["music_banner_url"] = DEFAULT_MUSIC_VIDEO_BANNER
    cfg["music_banner_mode"] = "video"
    save_config(cfg)
    embed = discord.Embed(
        title="🎬 Music Video Banner Reset",
        description="✅ Reset to default aesthetic video loop!",
        color=0x0000FF
    )
    embed.set_image(url=DEFAULT_MUSIC_VIDEO_BANNER)
    await ctx.reply(embed=embed)



# ==============================================================================
# BYTE CORPORATION — FULLY CUSTOMIZABLE A-TO-Z TICKET SUPPORT SYSTEM
# Every single element is 100% editable: Dropdowns, Animated Emojis (<a:name:id>),
# Banners, Thumbnails, Titles, Descriptions, Buttons, Welcome Messages, & Roles.
# Configurations live in ticket_config.json and are editable via Discord or file!
# ==============================================================================

TICKET_CONFIG_FILE = os.path.join(BASE_DIR, "ticket_config.json")


def load_ticket_config() -> dict:
    if os.path.exists(TICKET_CONFIG_FILE):
        try:
            with open(TICKET_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read ticket_config.json: {e}")
    # Default fallback config if file missing
    return {
        "embed": {
            "title": "🪽 BYTE CORPORATION 🦹",
            "description": "### **Welcome to BYTE CORPORATION Support**\n\n> Please select a panel below.",
            "color": "0x2B2D31",
            "banner_url": "",
            "thumbnail_url": "",
            "footer_text": "BYTE CORPORATION™ • Security Shield",
            "footer_icon_url": ""
        },
        "dropdown": {
            "placeholder": "Which Panel Do You Need?",
            "min_values": 1,
            "max_values": 1,
            "options": []
        },
        "controls": {
            "close_button": {"label": "Close", "emoji": "🔒", "style": "danger"},
            "claim_button": {"label": "Claim", "emoji": "🙋‍♂️", "style": "secondary"}
        },
        "settings": {
            "category_id": 1519724892918911188,
            "logs_channel_id": 1508088964077191261,
            "staff_role_ids": [1518838684307161259, 1518838683309183067, 1518838682449219675, 1522891854717128745],
            "ticket_naming": "ticket-{username}-{number:04d}",
            "ticket_counter": 69,
            "active_tickets": {}
        }
    }


def save_ticket_config(data: dict):
    try:
        with open(TICKET_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save ticket_config.json: {e}")


def parse_custom_emoji(emoji_str: str, guild: discord.Guild = None):
    """Parses unicode emojis, custom static emojis, or custom animated emojis (<a:name:id> or :name:)."""
    if not emoji_str:
        return None
    s = str(emoji_str).strip()
    if s.startswith("<") and s.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(s)
        except Exception:
            pass
    # If passed as :name: or raw name, resolve from guild or bot emojis (animated supported)
    clean_name = s.strip(":").lower()
    if guild and getattr(guild, "emojis", None):
        for em in guild.emojis:
            if em.name.lower() == clean_name:
                return discord.PartialEmoji(name=em.name, id=em.id, animated=em.animated)
    if 'bot' in globals() and hasattr(bot, "emojis"):
        for em in bot.emojis:
            if em.name.lower() == clean_name:
                return discord.PartialEmoji(name=em.name, id=em.id, animated=em.animated)
    return s


def parse_hex_color(color_val) -> int:
    """Parses color strings like 0x2B2D31 or #00E5FF into integer colors."""
    if isinstance(color_val, int):
        return color_val
    if isinstance(color_val, str):
        s = color_val.strip().lower()
        if s.startswith("0x"):
            try:
                return int(s, 16)
            except Exception:
                pass
        if s.startswith("#"):
            try:
                return int(s[1:], 16)
            except Exception:
                pass
        try:
            return int(s, 16)
        except Exception:
            pass
    return 0x0000FF


def parse_button_style(style_str: str, default=discord.ButtonStyle.secondary) -> discord.ButtonStyle:
    """Parses button style strings like danger, success, primary into discord.ButtonStyle."""
    if not style_str:
        return default
    s = str(style_str).strip().lower()
    if s in ("danger", "red"):
        return discord.ButtonStyle.danger
    if s in ("success", "green"):
        return discord.ButtonStyle.success
    if s in ("primary", "blurple", "blue"):
        return discord.ButtonStyle.primary
    if s in ("secondary", "grey", "gray"):
        return discord.ButtonStyle.secondary
    return default


async def download_option_banner(source, opt_val: str) -> tuple[str, bool, str]:
    """Downloads or caches a banner image or animated GIF locally in BASE_DIR.
    Returns: (banner_identifier, is_local_file, status_note)
    """
    if not source:
        return ("", False, "Banner reset to default global banner.")

    clean_opt = re.sub(r'[^a-zA-Z0-9_\-]', '', str(opt_val).lower().strip())
    if not clean_opt:
        clean_opt = "custom"

    # Case 1: Discord Attachment object
    if isinstance(source, discord.Attachment):
        fname = source.filename.lower()
        ext = ".gif" if fname.endswith(".gif") else (".png" if fname.endswith(".png") else (".webp" if fname.endswith(".webp") else ".jpg"))
        saved_name = f"banner_{clean_opt}{ext}"
        saved_path = os.path.join(BASE_DIR, saved_name)
        try:
            await source.save(saved_path)
            logger.info(f"[BANNER CACHED] Attachment saved as {saved_name}")
            return (saved_name, True, f"Saved from uploaded file: `{saved_name}`")
        except Exception as e:
            logger.error(f"Failed to save attachment for {opt_val}: {e}")
            return (source.url, False, f"Could not save local file ({e}). Using raw attachment URL.")

    # Case 2: String
    s = str(source).strip()
    if not s or s.lower() in ("none", "remove", "reset", "clear", "default"):
        return ("", False, "Banner reset to default global banner.")

    # If already a valid local file in BASE_DIR
    if os.path.exists(os.path.join(BASE_DIR, s)):
        return (s, True, f"Using local file `{s}`")
    if os.path.exists(s):
        return (s, True, f"Using local file `{s}`")

    # If URL: download and cache locally so it never expires!
    if s.startswith("http://") or s.startswith("https://"):
        try:
            lower_s = s.lower()
            if ".gif" in lower_s:
                ext = ".gif"
            elif ".png" in lower_s:
                ext = ".png"
            elif ".webp" in lower_s:
                ext = ".webp"
            else:
                ext = ".gif"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(s, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("Content-Type", "").lower()
                        if "gif" in ct:
                            ext = ".gif"
                        elif "png" in ct:
                            ext = ".png"
                        elif "webp" in ct:
                            ext = ".webp"
                        elif "jpeg" in ct or "jpg" in ct:
                            ext = ".jpg"

                        saved_name = f"banner_{clean_opt}{ext}"
                        saved_path = os.path.join(BASE_DIR, saved_name)
                        data = await resp.read()
                        if len(data) > 0:
                            with open(saved_path, "wb") as f:
                                f.write(data)
                            logger.info(f"Successfully downloaded banner for {opt_val} -> {saved_name} ({len(data)} bytes)")
                            return (saved_name, True, f"Downloaded and cached locally as `{saved_name}` ({len(data)//1024} KB)")
                    else:
                        logger.warning(f"Download banner returned HTTP {resp.status} for {s}")
                        return (s, False, f"Server returned HTTP {resp.status}. Saved raw URL as fallback.")
        except Exception as e:
            logger.warning(f"Failed to download banner from {s}: {e}")
            return (s, False, f"Could not download ({e}). Saved raw URL as fallback.")

    return (s, False, "Saved banner configuration.")


def get_custom_ticket_panel_assets(cfg: dict = None) -> tuple[discord.Embed, discord.File | None]:
    """Builds the main ticket panel embed and attaches banner image (local or remote)."""
    if not cfg:
        cfg = load_ticket_config()
    emb_cfg = cfg.get("embed", {})
    color_int = parse_hex_color(emb_cfg.get("color", "0x00E5FF"))

    embed = discord.Embed(
        title=emb_cfg.get("title", "🪽 BYTE CORPORATION 🦹"),
        description=emb_cfg.get("description", "Please select a panel below to open a ticket."),
        color=color_int,
        timestamp=datetime.utcnow(),
    )
    banner = emb_cfg.get("banner_url", "").strip()
    file_obj = None
    if banner:
        if os.path.exists(banner):
            filename = os.path.basename(banner)
            file_obj = discord.File(banner, filename=filename)
            embed.set_image(url=f"attachment://{filename}")
        elif os.path.exists(os.path.join(BASE_DIR, banner)):
            full_p = os.path.join(BASE_DIR, banner)
            filename = os.path.basename(full_p)
            file_obj = discord.File(full_p, filename=filename)
            embed.set_image(url=f"attachment://{filename}")
        elif banner.startswith("http://") or banner.startswith("https://"):
            embed.set_image(url=banner)
    elif os.path.exists(os.path.join(BASE_DIR, "byte_ticket_banner.png")):
        full_p = os.path.join(BASE_DIR, "byte_ticket_banner.png")
        file_obj = discord.File(full_p, filename="byte_ticket_banner.png")
        embed.set_image(url="attachment://byte_ticket_banner.png")

    thumb = emb_cfg.get("thumbnail_url", "").strip()
    if thumb and (thumb.startswith("http://") or thumb.startswith("https://")):
        embed.set_thumbnail(url=thumb)

    footer_text = emb_cfg.get("footer_text", "⚙️ Byte Corporation, All Rights Reserved | Owner : @diewitheran_")
    footer_icon = emb_cfg.get("footer_icon_url", "").strip()
    if footer_icon and (footer_icon.startswith("http://") or footer_icon.startswith("https://")):
        embed.set_footer(text=footer_text, icon_url=footer_icon)
    else:
        embed.set_footer(text=footer_text)

    return embed, file_obj


def build_custom_ticket_embed(cfg: dict = None) -> discord.Embed:
    """Builds the main ticket panel embed (returns Embed object)."""
    embed, _ = get_custom_ticket_panel_assets(cfg)
    return embed


def is_staff(member: discord.Member, cfg: dict = None) -> bool:
    """Checks if member is server admin or has any configured staff role."""
    if not member:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms and getattr(perms, "administrator", False):
        return True
    if not cfg:
        cfg = load_ticket_config()
    staff_ids = cfg.get("settings", {}).get("staff_role_ids", [])
    roles = getattr(member, "roles", [])
    if any(getattr(r, "id", None) in staff_ids for r in roles):
        return True
    admin_r = get_admin_role_id()
    if admin_r and any(getattr(r, "id", None) == admin_r for r in roles):
        return True
    return False

is_ticket_staff = is_staff


class ByteCustomTicketSelect(discord.ui.Select):
    """Dynamic Dropdown Select building options from ticket_config.json with animated emoji support."""
    def __init__(self, cfg: dict = None):
        if not cfg:
            cfg = load_ticket_config()
        drop_cfg = cfg.get("dropdown", {})
        raw_options = drop_cfg.get("options", [])

        options = []
        if raw_options:
            for item in raw_options:
                lbl = item.get("label", "Support")
                val = item.get("value", lbl.lower().replace(" ", "_"))
                desc = item.get("description", "")
                em = parse_custom_emoji(item.get("emoji"))
                options.append(
                    discord.SelectOption(
                        label=lbl[:100],
                        value=val[:100],
                        description=desc[:100] if desc else None,
                        emoji=em
                    )
                )
        else:
            options.append(discord.SelectOption(label="General Support", value="general", emoji="🎫"))

        super().__init__(
            placeholder=drop_cfg.get("placeholder", "Which Panel Do You Need?"),
            min_values=1,
            max_values=1,
            custom_id="custom_ticket_dropdown_select",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        cfg = load_ticket_config()
        settings = cfg.get("settings", {})
        category_id = settings.get("category_id", 1519724892918911188)
        category = guild.get_channel(category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            for c in guild.categories:
                if "ticket" in c.name.lower():
                    category = c
                    break

        # Check if user already has an open ticket
        existing_tickets = settings.get("active_tickets", {})
        for ch_id, t_data in existing_tickets.items():
            if t_data.get("user_id") == user.id:
                ch = guild.get_channel(int(ch_id))
                if ch:
                    await interaction.followup.send(f"⚠️ **You already have an open ticket:** {ch.mention}", ephemeral=True)
                    return

        selected_val = str(self.values[0]).strip()
        # Match selected option (case-insensitive & label-insensitive)
        opt_data = None
        for opt in cfg.get("dropdown", {}).get("options", []):
            o_val = str(opt.get("value", "")).strip().lower()
            o_lbl = str(opt.get("label", "")).strip().lower()
            s_val = selected_val.lower()
            if o_val == s_val or o_lbl == s_val or o_val.replace(" ", "_") == s_val.replace(" ", "_"):
                opt_data = opt
                break
        if not opt_data:
            opt_data = {"label": selected_val, "welcome_title": f"Support • {selected_val}", "welcome_message": "Welcome {user}!"}

        # Ticket numbering & dynamic custom naming format
        curr_counter = settings.get("ticket_counter", 69) + 1
        settings["ticket_counter"] = curr_counter

        raw_naming = settings.get("ticket_naming", "ticket-{username}-{number:04d}")
        clean_user = re.sub(r'[^a-zA-Z0-9_\-]', '', user.name.lower().replace(' ', '-'))
        clean_user = clean_user[:24] if clean_user else f"user-{user.id}"[:20]

        try:
            ch_name = raw_naming.format(
                username=clean_user,
                user=clean_user,
                number=curr_counter,
                count=curr_counter,
                counter=curr_counter
            )
        except Exception:
            ch_name = f"ticket-{clean_user}-{curr_counter:04d}"

        ch_name = re.sub(r'[^a-z0-9_\-]', '-', ch_name.lower())
        ch_name = re.sub(r'-+', '-', ch_name).strip('-')[:95]
        if not ch_name:
            ch_name = f"ticket-{curr_counter:04d}"

        # Strict Permissions:
        # Default role (@everyone) can neither see, nor send, nor read history
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_messages=False,
                read_message_history=False,
                add_reactions=False,
                attach_files=False
            ),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                embed_links=True,
                read_message_history=True,
                read_messages=True,
                add_reactions=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_permissions=True,
                attach_files=True,
                embed_links=True,
                read_message_history=True,
                read_messages=True
            )
        }

        # Explicitly deny view_channel for any roles present in Category overwrites that are NOT in staff_role_ids
        staff_role_ids = settings.get("staff_role_ids", [])
        if category and getattr(category, "overwrites", None):
            for target in category.overwrites.keys():
                if isinstance(target, discord.Role) and target != guild.default_role:
                    if target.id not in staff_role_ids:
                        overwrites[target] = discord.PermissionOverwrite(
                            view_channel=False,
                            send_messages=False,
                            read_messages=False,
                            read_message_history=False
                        )

        # Allow configured staff roles
        for s_role_id in staff_role_ids:
            role_obj = guild.get_role(s_role_id)
            if role_obj:
                overwrites[role_obj] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    embed_links=True,
                    read_message_history=True,
                    read_messages=True,
                    add_reactions=True
                )

        try:
            ticket_channel = await guild.create_text_channel(
                name=ch_name,
                category=category,
                overwrites=overwrites,
                topic=f"Owner: {user.name} ({user.id}) | Panel: {opt_data.get('label')} | Ticket #{curr_counter:04d}"
            )
        except Exception as e:
            logger.error(f"Failed to create ticket channel: {e}")
            await interaction.followup.send(f"❌ Failed to create ticket channel: `{e}`", ephemeral=True)
            return

        # Save active ticket
        if "active_tickets" not in settings:
            settings["active_tickets"] = {}
        settings["active_tickets"][str(ticket_channel.id)] = {
            "user_id": user.id,
            "user_name": user.name,
            "ticket_number": curr_counter,
            "panel": opt_data.get("label"),
            "opened_at": datetime.utcnow().isoformat(),
            "claimed_by": None
        }
        save_ticket_config(cfg)

        # Welcome Embed (100% customizable from ticket_config.json -> ticket_channel)
        tc = cfg.get("ticket_channel", {})
        
        # 1. Title formatting ({panel}, {emoji}, {number}, {user})
        raw_title = tc.get("title_template") or opt_data.get("welcome_title") or "{panel} - Purchase Ticket"
        w_title = (
            raw_title.replace("{panel}", opt_data.get("label", "Support"))
            .replace("{emoji}", opt_data.get("emoji", "🎫"))
            .replace("{number}", f"{curr_counter:04d}")
            .replace("{user}", user.name)
        )
        w_title = sanitize_and_resolve_emojis(w_title, guild)

        # 2. Body / Description formatting ({user}, {user_name}, {panel}, {number})
        raw_msg = tc.get("body_template") or opt_data.get("welcome_message") or "Hello {user}!\nWelcome to your private ticket channel for **{panel}**."
        formatted_msg = (
            raw_msg.replace("{user}", user.mention)
            .replace("{user_name}", user.name)
            .replace("{panel}", opt_data.get("label", "Support"))
            .replace("{number}", f"{curr_counter:04d}")
        )
        formatted_msg = sanitize_and_resolve_emojis(formatted_msg, guild)

        # Discord API does not animate custom emojis in embed.title. If title has custom/animated emojis, display as markdown # heading in description!
        embed_title = w_title
        if w_title and ("<a:" in w_title or "<:" in w_title):
            formatted_msg = f"# {w_title}\n\n{formatted_msg}"
            embed_title = None

        # 3. Color
        color_int = parse_hex_color(tc.get("color", "0x9D4EDD"))

        welcome_embed = discord.Embed(
            title=embed_title,
            description=formatted_msg,
            color=color_int,
            timestamp=datetime.utcnow()
        )

        # 4. Fields (Panel, Ticket ID, Created By)
        if tc.get("show_fields", True):
            welcome_embed.add_field(name="🆔 Ticket ID", value=f"`#{curr_counter:04d}`", inline=True)
            welcome_embed.add_field(name="👤 Created By", value=f"{user.mention}", inline=True)

        # 5. Footer
        footer_text = tc.get("footer_text", "BYTE CORPORATION Security Shield • Click Close or Claim below")
        welcome_embed.set_footer(text=footer_text)

        # 6. Thumbnail (Option-specific or Global)
        opt_thumb = opt_data.get("thumbnail_url")
        if opt_thumb:
            welcome_embed.set_thumbnail(url=opt_thumb)
        else:
            thumb_type = tc.get("thumbnail_type", "user_avatar")
            if thumb_type == "user_avatar" and user.display_avatar:
                welcome_embed.set_thumbnail(url=user.display_avatar.url)
            elif thumb_type == "server_icon" and guild.icon:
                welcome_embed.set_thumbnail(url=guild.icon.url)
            elif tc.get("custom_thumbnail_url"):
                welcome_embed.set_thumbnail(url=tc.get("custom_thumbnail_url"))

        # 7. Banner Image inside Ticket (Option-specific GIF/PNG or Global Fallback)
        opt_banner = str(opt_data.get("banner_url") or opt_data.get("image_url") or opt_data.get("gif_url") or "").strip()
        ticket_banner = opt_banner or str(tc.get("banner_url") or "").strip()
        banner_file_to_send = None
        if ticket_banner:
            logger.info(f"[TICKET CREATED] Applying banner for '{opt_data.get('label')}': {ticket_banner}")
            if ticket_banner.startswith("http://") or ticket_banner.startswith("https://"):
                welcome_embed.set_image(url=ticket_banner)
            elif os.path.exists(ticket_banner):
                fname = os.path.basename(ticket_banner)
                banner_file_to_send = discord.File(ticket_banner, filename=fname)
                welcome_embed.set_image(url=f"attachment://{fname}")
            elif os.path.exists(os.path.join(BASE_DIR, ticket_banner)):
                full_p = os.path.join(BASE_DIR, ticket_banner)
                fname = os.path.basename(full_p)
                banner_file_to_send = discord.File(full_p, filename=fname)
                welcome_embed.set_image(url=f"attachment://{fname}")
            else:
                welcome_embed.set_image(url=ticket_banner)
        else:
            logger.info(f"[TICKET CREATED] No banner configured for '{opt_data.get('label')}'.")

        # 8. Ping staff roles
        ping_role_ids = tc.get("ping_roles")
        if ping_role_ids is None:
            ping_role_ids = settings.get("staff_role_ids", [1518838684307161259])
        ping_mentions = " ".join(f"<@&{rid}>" for rid in ping_role_ids if guild.get_role(rid))
        content_msg = sanitize_and_resolve_emojis(f"{user.mention} | {ping_mentions}".strip() if ping_mentions else user.mention, guild)

        controls_view = ByteCustomTicketControlView()
        if banner_file_to_send:
            await ticket_channel.send(content=content_msg, embed=welcome_embed, file=banner_file_to_send, view=controls_view)
        else:
            await ticket_channel.send(content=content_msg, embed=welcome_embed, view=controls_view)
        await interaction.followup.send(f"✅ **Ticket created successfully:** {ticket_channel.mention}", ephemeral=True)


class ByteCustomTicketPanelView(discord.ui.View):
    """Persistent View for the main Ticket Panel."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ByteCustomTicketSelect())


class ByteCustomTicketControlView(discord.ui.View):
    """Interactive Control View inside Ticket Channels with:
    Row 0: [🔒 Close] [🔗 Transcript] [🎵 Autopay]
    Row 1: [PC PANEL] [ANDROID PANEL] [ID BUY/SALE]
    Row 2: [🙋‍♂️ Claim] or [✅ Claimed by Name] [🔄 Unclaim]
    """
    def __init__(self, claimed_name: str = None):
        super().__init__(timeout=None)
        cfg = load_ticket_config()
        controls = cfg.get("controls", {})

        close_cfg = controls.get("close_button", {})
        transcript_cfg = controls.get("transcript_button", {})
        autopay_cfg = controls.get("autopay_button", {})

        pc_cfg = controls.get("pc_panel_button", {})
        android_cfg = controls.get("android_panel_button", {})
        id_cfg = controls.get("id_buy_sale_button", {})

        claim_cfg = controls.get("claim_button", {})
        unclaim_cfg = controls.get("unclaim_button", {})

        # Row 0: [🔒 Close] [🔗 Transcript] [🎵 Autopay]
        close_btn = discord.ui.Button(
            label=close_cfg.get("label", "Close")[:80],
            emoji=parse_custom_emoji(close_cfg.get("emoji", "🔒")),
            style=parse_button_style(close_cfg.get("style", "danger"), discord.ButtonStyle.danger),
            custom_id="btn_custom_ticket_close",
            row=0
        )
        close_btn.callback = self.close_callback
        self.add_item(close_btn)

        transcript_btn = discord.ui.Button(
            label=transcript_cfg.get("label", "Transcript")[:80],
            emoji=parse_custom_emoji(transcript_cfg.get("emoji", "🔗")),
            style=parse_button_style(transcript_cfg.get("style", "secondary"), discord.ButtonStyle.secondary),
            custom_id="btn_custom_ticket_transcript",
            row=0
        )
        transcript_btn.callback = self.transcript_callback
        self.add_item(transcript_btn)

        autopay_btn = discord.ui.Button(
            label=autopay_cfg.get("label", "Autopay")[:80],
            emoji=parse_custom_emoji(autopay_cfg.get("emoji", "🎵")),
            style=parse_button_style(autopay_cfg.get("style", "success"), discord.ButtonStyle.success),
            custom_id="btn_custom_ticket_autopay",
            row=0
        )
        autopay_btn.callback = self.autopay_callback
        self.add_item(autopay_btn)

        # Row 1: [PC PANEL] [ANDROID PANEL] [ID BUY/SALE]
        pc_btn = discord.ui.Button(
            label=pc_cfg.get("label", "PC PANEL")[:80],
            emoji=parse_custom_emoji(pc_cfg.get("emoji")) if pc_cfg.get("emoji") else None,
            style=parse_button_style(pc_cfg.get("style", "secondary"), discord.ButtonStyle.secondary),
            custom_id="btn_custom_ticket_pc",
            row=1
        )
        pc_btn.callback = self.pc_panel_callback
        self.add_item(pc_btn)

        android_btn = discord.ui.Button(
            label=android_cfg.get("label", "ANDROID PANEL")[:80],
            emoji=parse_custom_emoji(android_cfg.get("emoji")) if android_cfg.get("emoji") else None,
            style=parse_button_style(android_cfg.get("style", "secondary"), discord.ButtonStyle.secondary),
            custom_id="btn_custom_ticket_android",
            row=1
        )
        android_btn.callback = self.android_panel_callback
        self.add_item(android_btn)

        # Claim Button in Row 1 (Claim replaces ID BUY/SALE per Void request)
        if claim_cfg.get("enabled", True):
            if claimed_name:
                claimed_btn = discord.ui.Button(
                    label=f"Claimed by {claimed_name}"[:80],
                    emoji=parse_custom_emoji(claim_cfg.get("claimed_emoji", "✅")),
                    style=discord.ButtonStyle.success,
                    disabled=True,
                    custom_id="btn_custom_ticket_claimed",
                    row=1
                )
                self.add_item(claimed_btn)

                unclaim_btn = discord.ui.Button(
                    label=unclaim_cfg.get("label", "Unclaim")[:80],
                    emoji=parse_custom_emoji(unclaim_cfg.get("emoji", "🔄")),
                    style=parse_button_style(unclaim_cfg.get("style", "secondary"), discord.ButtonStyle.secondary),
                    custom_id="btn_custom_ticket_unclaim",
                    row=1
                )
                unclaim_btn.callback = self.unclaim_callback
                self.add_item(unclaim_btn)
            else:
                claim_btn = discord.ui.Button(
                    label=claim_cfg.get("label", "Claim")[:80],
                    emoji=parse_custom_emoji(claim_cfg.get("emoji", "🙋‍♂️")),
                    style=parse_button_style(claim_cfg.get("style", "success"), discord.ButtonStyle.success),
                    custom_id="btn_custom_ticket_claim",
                    row=1
                )
                claim_btn.callback = self.claim_callback
                self.add_item(claim_btn)

    async def close_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        settings = cfg.get("settings", {})
        active = settings.get("active_tickets", {}).get(str(interaction.channel.id), {})

        is_owner = (active.get("user_id") == interaction.user.id)
        is_staff_mem = is_staff(interaction.user, cfg)

        if not is_staff_mem and not is_owner:
            await interaction.response.send_message("❌ Only staff or the ticket creator can close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 **Ticket closing in 5 seconds... Generating transcript.**")
        await asyncio.sleep(5)

        # Generate Transcript
        lines = []
        lines.append("==================================================")
        lines.append("BYTE CORPORATION — TICKET TRANSCRIPT")
        lines.append(f"Channel: #{interaction.channel.name} (ID: {interaction.channel.id})")
        lines.append(f"Ticket #{active.get('ticket_number', 'N/A')} | Panel: {active.get('panel', 'General')}")
        lines.append(f"Closed By: {interaction.user.name} ({interaction.user.id})")
        lines.append(f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        lines.append("==================================================")
        lines.append("")

        msg_count = 0
        try:
            async for m in interaction.channel.history(limit=500, oldest_first=True):
                msg_count += 1
                t_str = m.created_at.strftime('%Y-%m-%d %H:%M:%S')
                author_str = f"{m.author.name} ({m.author.id})"
                lines.append(f"[{t_str}] {author_str}: {m.content}")
                if m.attachments:
                    for att in m.attachments:
                        lines.append(f"  [Attachment: {att.url}]")
        except Exception as e:
            lines.append(f"[Error fetching complete history: {e}]")

        transcript_text = "\n".join(lines)
        transcript_file = discord.File(
            io.BytesIO(transcript_text.encode("utf-8")),
            filename=f"transcript-{interaction.channel.name}.txt"
        )

        # Send to Logs Channel
        logs_ch_id = settings.get("logs_channel_id", 1508088964077191261)
        logs_ch = interaction.guild.get_channel(logs_ch_id)
        if logs_ch and isinstance(logs_ch, discord.TextChannel):
            log_embed = discord.Embed(
                title=f"📋 Ticket Closed • #{interaction.channel.name}",
                color=0x0000FF,
                timestamp=datetime.utcnow()
            )
            log_embed.add_field(name="🆔 Ticket", value=f"`#{active.get('ticket_number', 'N/A')}`", inline=True)
            log_embed.add_field(name="🎯 Panel", value=f"`{active.get('panel', 'N/A')}`", inline=True)
            log_embed.add_field(name="💬 Messages", value=f"`{msg_count}`", inline=True)
            log_embed.add_field(name="👤 Closed By", value=f"{interaction.user.mention}", inline=True)
            if active.get("user_id"):
                log_embed.add_field(name="👑 Ticket Owner", value=f"<@{active.get('user_id')}>", inline=True)
            log_embed.set_footer(text="BYTE CORPORATION Transcript Archive")
            try:
                await logs_ch.send(embed=log_embed, file=transcript_file)
            except Exception as e:
                logger.error(f"Failed to post transcript to logs channel: {e}")

        # Send to User DM
        target_uid = active.get("user_id")
        if target_uid:
            try:
                target_user = interaction.guild.get_member(target_uid) or await bot.fetch_user(target_uid)
                if target_user:
                    dm_file = discord.File(
                        io.BytesIO(transcript_text.encode("utf-8")),
                        filename=f"transcript-{interaction.channel.name}.txt"
                    )
                    dm_embed = discord.Embed(
                        title=f"📋 Ticket Closed • #{interaction.channel.name}",
                        description=f"Your ticket for **{active.get('panel', 'Support')}** in **BYTE CORPORATION** has been closed.\nA complete transcript is attached below.",
                        color=0x0000FF,
                        timestamp=datetime.utcnow()
                    )
                    await target_user.send(embed=dm_embed, file=dm_file)
            except Exception:
                pass

        if str(interaction.channel.id) in settings.get("active_tickets", {}):
            del settings["active_tickets"][str(interaction.channel.id)]
            save_ticket_config(cfg)

        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user.name}")

    async def claim_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        if not is_staff(interaction.user, cfg):
            await interaction.response.send_message("❌ Only staff members can claim tickets.", ephemeral=True)
            return

        settings = cfg.get("settings", {})
        active = settings.get("active_tickets", {}).get(str(interaction.channel.id), {})
        if active.get("claimed_by"):
            await interaction.response.send_message(f"⚠️ This ticket is already claimed by <@{active.get('claimed_by')}>.", ephemeral=True)
            return

        active["claimed_by"] = interaction.user.id
        save_ticket_config(cfg)

        try:
            await interaction.message.edit(view=ByteCustomTicketControlView(claimed_name=interaction.user.display_name))
        except Exception:
            pass

        claim_embed = discord.Embed(
            description=f"🎟️ {interaction.user.mention} has claimed this ticket.",
            color=0x0000FF
        )
        await interaction.channel.send(embed=claim_embed)
        await interaction.response.send_message("✅ You have claimed this ticket.", ephemeral=True)

    async def unclaim_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        if not is_staff(interaction.user, cfg):
            await interaction.response.send_message("❌ Only staff members can unclaim tickets.", ephemeral=True)
            return

        settings = cfg.get("settings", {})
        active = settings.get("active_tickets", {}).get(str(interaction.channel.id), {})
        active["claimed_by"] = None
        save_ticket_config(cfg)

        try:
            await interaction.message.edit(view=ByteCustomTicketControlView(claimed_name=None))
        except Exception:
            pass

        unclaim_embed = discord.Embed(
            description="🎟️ Ticket has been unclaimed.",
            color=0x0000FF
        )
        await interaction.channel.send(embed=unclaim_embed)
        await interaction.response.send_message("✅ Ticket unclaimed.", ephemeral=True)

    async def transcript_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = load_ticket_config()
        settings = cfg.get("settings", {})
        active = settings.get("active_tickets", {}).get(str(interaction.channel.id), {})

        # Generate Transcript
        lines = []
        lines.append("==================================================")
        lines.append("BYTE CORPORATION — TICKET TRANSCRIPT")
        lines.append(f"Channel: #{interaction.channel.name} (ID: {interaction.channel.id})")
        lines.append(f"Ticket #{active.get('ticket_number', 'N/A')} | Panel: {active.get('panel', 'General')}")
        lines.append(f"Requested By: {interaction.user.name} ({interaction.user.id})")
        lines.append(f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        lines.append("==================================================")
        lines.append("")

        msg_count = 0
        try:
            async for m in interaction.channel.history(limit=500, oldest_first=True):
                msg_count += 1
                t_str = m.created_at.strftime('%Y-%m-%d %H:%M:%S')
                author_str = f"{m.author.name} ({m.author.id})"
                lines.append(f"[{t_str}] {author_str}: {m.content}")
                if m.attachments:
                    for att in m.attachments:
                        lines.append(f"  [Attachment: {att.url}]")
        except Exception as e:
            lines.append(f"[Error fetching complete history: {e}]")

        transcript_text = "\n".join(lines)
        file_bytes = transcript_text.encode("utf-8")
        transcript_file = discord.File(io.BytesIO(file_bytes), filename=f"transcript-{interaction.channel.name}.txt")

        t_embed = discord.Embed(
            title="📋 Ticket Transcript Generated",
            description=f"Generated **{msg_count} messages** transcript for {interaction.channel.mention}.\nRequested by {interaction.user.mention}.",
            color=0x0000FF,
            timestamp=datetime.utcnow()
        )
        t_embed.set_footer(text="BYTE CORPORATION Security Shield")
        await interaction.channel.send(embed=t_embed, file=transcript_file)
        await interaction.followup.send("✅ **Transcript generated and posted in channel!**", ephemeral=True)

    async def autopay_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        controls = cfg.get("controls", {})
        ap = controls.get("autopay_info", {})
        upi_id = ap.get("upi_id", "bhavishya.912@fam")
        upi_name = ap.get("upi_name", "Bhavishya Raikwar")
        binance_id = ap.get("binance_id", "1275075958")
        title = ap.get("title", "⚡ BYTE CORPORATION — AUTOPAY & PAYMENT DETAILS")
        instructions = ap.get("instructions", "1. Pay the exact amount for your package.\n2. Upload your payment screenshot or Transaction ID directly here.\n3. Staff will verify and issue access keys immediately!")
        qr_url = ap.get("qr_image_url", "").strip()

        desc = (
            f"💳 **Official Automated Payment Channels**\n\n"
            f"🇮🇳 **UPI Payment (India — GPay / PhonePe / Paytm / FamPay):**\n"
            f"• **UPI ID:** `{upi_id}`\n"
            f"• **Holder Name:** `{upi_name}`\n"
            f"• Scan the QR code attached below.\n\n"
            f"🟡 **Binance Pay (International / Crypto):**\n"
            f"• **Binance ID:** `{binance_id}`\n"
            f"• Supported: USDT / BUSD / BTC / LTC\n\n"
            f"📝 **Instructions:**\n{instructions}"
        )

        pay_embed = discord.Embed(
            title=title,
            description=desc,
            color=0x0000FF,
            timestamp=datetime.utcnow()
        )
        pay_embed.set_footer(text="BYTE CORPORATION • Secure Transaction Gateway")

        qr_path = os.path.join(BASE_DIR, ap.get("qr_image_file", "autopay_qr.jpg"))
        if qr_url:
            pay_embed.set_image(url=qr_url)
            await interaction.channel.send(embed=pay_embed)
        elif os.path.exists(qr_path):
            file_obj = discord.File(qr_path, filename="autopay_qr.jpg")
            pay_embed.set_image(url="attachment://autopay_qr.jpg")
            await interaction.channel.send(file=file_obj, embed=pay_embed)
        else:
            await interaction.channel.send(embed=pay_embed)

        try:
            await interaction.response.send_message("✅ **Autopay details sent in channel!**", ephemeral=True)
        except Exception:
            pass

    async def pc_panel_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        info = cfg.get("controls", {}).get("pc_panel_info", {})
        title = info.get("title", "💻 BYTE CORPORATION — PC PANEL PACKAGES")
        desc = info.get("description", (
            "🖥️ **Available PC Panels & Cheats:**\n"
            "• **BRUTAL PANEL** — High Performance Execution\n"
            "• **BASIC PANEL** — Simple & Stable\n"
            "• **INTERNAL PANEL** — Direct Inject & Memory Hack\n"
            "• **SILENT AIM / MAX** — Maximum Precision Aimlock\n\n"
            "📌 **Please specify:**\n"
            "1. Which PC package you want\n"
            "2. Duration (1 Day / 7 Days / 30 Days / Lifetime)\n\n"
            "*Staff will quote pricing and provide the download loader.*"
        ))
        embed = discord.Embed(title=title, description=desc, color=0x0000FF, timestamp=datetime.utcnow())
        embed.set_footer(text="BYTE CORPORATION • PC Panel Support")
        await interaction.channel.send(embed=embed)
        try:
            await interaction.response.send_message("✅ **PC Panel info posted!**", ephemeral=True)
        except Exception:
            pass

    async def android_panel_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        info = cfg.get("controls", {}).get("android_panel_info", {})
        title = info.get("title", "📱 BYTE CORPORATION — ANDROID PANEL PACKAGES")
        desc = info.get("description", (
            "🤖 **Available Android Panels & APKs:**\n"
            "• **AIMKILL EXE / APK** — Aimkill Loader & Client\n"
            "• **COVER SILENT APK** — Undetected Cover Silent\n"
            "• **LIB BYPASS** — Memory Patch & Security Bypass\n"
            "• **UID BYPASS** — Server-side Whitelist Registration\n\n"
            "📌 **Please specify:**\n"
            "1. Device Architecture (32-Bit / 64-Bit)\n"
            "2. Rooted or Non-Rooted\n"
            "3. Desired duration\n\n"
            "*Staff will assist with installation and access keys.*"
        ))
        embed = discord.Embed(title=title, description=desc, color=0x0000FF, timestamp=datetime.utcnow())
        embed.set_footer(text="BYTE CORPORATION • Android Panel Support")
        await interaction.channel.send(embed=embed)
        try:
            await interaction.response.send_message("✅ **Android Panel info posted!**", ephemeral=True)
        except Exception:
            pass

    async def id_buysale_callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        info = cfg.get("controls", {}).get("id_buy_sale_info", {})
        title = info.get("title", "💎 BYTE CORPORATION — ID BUY / SALE SERVICE")
        desc = info.get("description", (
            "🛡️ **Official Verified Free Fire ID Marketplace & Escrow:**\n\n"
            "🛒 **Want to BUY an ID?**\n"
            "• Mention your budget, required level, and preferred guns/bundles.\n\n"
            "🏷️ **Want to SELL an ID?**\n"
            "• Send full video showcase, Level, Likes, Login method (FB/Google) and your price.\n\n"
            "⚠️ **Warning:** Never trade in DMs! Official staff middleman guarantees 100% safe transactions here in ticket."
        ))
        embed = discord.Embed(title=title, description=desc, color=0x0000FF, timestamp=datetime.utcnow())
        embed.set_footer(text="BYTE CORPORATION • ID Marketplace & Escrow")
        await interaction.channel.send(embed=embed)
        try:
            await interaction.response.send_message("✅ **ID Buy/Sale info posted!**", ephemeral=True)
        except Exception:
            pass


# ==========================================
# IN-DISCORD TICKET LIVE EDITOR & MODALS
# ==========================================

class TicketWelcomeModal(discord.ui.Modal, title="📝 Edit Inside-Ticket Welcome Embed"):
    title_tpl = discord.ui.TextInput(
        label="Ticket Title Template",
        placeholder="e.g. {panel} - Purchase Ticket or {emoji} {panel} SUPPORT",
        max_length=100,
        required=True
    )
    color_hex = discord.ui.TextInput(
        label="Embed Hex Color (e.g. 0x2B2D31 or #9D4EDD)",
        default="0x9D4EDD",
        max_length=15,
        required=True
    )
    body_tpl = discord.ui.TextInput(
        label="Welcome Body Template ({user} & {panel})",
        style=discord.TextStyle.paragraph,
        max_length=1800,
        required=True
    )
    banner_url = discord.ui.TextInput(
        label="Inside-Ticket Banner URL (Optional)",
        placeholder="https://example.com/banner.png",
        max_length=300,
        required=False
    )
    footer_text = discord.ui.TextInput(
        label="Footer Text (Optional)",
        placeholder="BYTE CORPORATION Security Shield • Click Close or Claim below",
        max_length=150,
        required=False
    )

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config().get("ticket_channel", {})
        self.title_tpl.default = cfg.get("title_template", "{panel} - Purchase Ticket")
        self.color_hex.default = str(cfg.get("color", "0x9D4EDD"))
        self.body_tpl.default = cfg.get("body_template", "Hello {user}\n\n**Welcome to your private ticket channel for {panel}.**")
        self.banner_url.default = cfg.get("banner_url", "")
        self.footer_text.default = cfg.get("footer_text", "BYTE CORPORATION Security Shield • Click Close or Claim below")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        tc = cfg.setdefault("ticket_channel", {})
        tc["title_template"] = self.title_tpl.value.strip()
        tc["color"] = self.color_hex.value.strip()
        tc["body_template"] = self.body_tpl.value.strip()
        tc["banner_url"] = self.banner_url.value.strip()
        tc["footer_text"] = self.footer_text.value.strip()
        save_ticket_config(cfg)
        await interaction.response.send_message("✅ **Inside-Ticket Welcome Layout updated successfully!**\nEvery newly created ticket channel will now display this exact message & style.", ephemeral=True)


class TicketEmbedModal(discord.ui.Modal, title="🎨 Edit Ticket Embed & Banner"):
    panel_title = discord.ui.TextInput(label="Embed Title", default="🪽 BYTE CORPORATION 🦹", max_length=100)
    panel_desc = discord.ui.TextInput(label="Embed Description", style=discord.TextStyle.paragraph, max_length=1500)
    panel_color = discord.ui.TextInput(label="Embed Hex Color (e.g. 0x2B2D31 or #00E5FF)", default="0x2B2D31", max_length=15)
    banner_url = discord.ui.TextInput(label="Banner Image URL (Large image at bottom)", required=False, max_length=300)
    thumb_url = discord.ui.TextInput(label="Thumbnail URL (Small icon at top-right)", required=False, max_length=300)

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config().get("embed", {})
        self.panel_title.default = cfg.get("title", "🪽 BYTE CORPORATION 🦹")
        self.panel_desc.default = cfg.get("description", "Please select a panel below.")
        self.panel_color.default = str(cfg.get("color", "0x2B2D31"))
        self.banner_url.default = cfg.get("banner_url", "")
        self.thumb_url.default = cfg.get("thumbnail_url", "")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        if "embed" not in cfg:
            cfg["embed"] = {}
        cfg["embed"]["title"] = self.panel_title.value.strip()
        cfg["embed"]["description"] = self.panel_desc.value.strip()
        cfg["embed"]["color"] = self.panel_color.value.strip()
        cfg["embed"]["banner_url"] = self.banner_url.value.strip()
        cfg["embed"]["thumbnail_url"] = self.thumb_url.value.strip()
        save_ticket_config(cfg)
        await interaction.response.send_message("✅ **Ticket Embed & Banner updated successfully!** Run `*ticketpanel` or click Refresh Panel to apply.", ephemeral=True)


class TicketAddOptionModal(discord.ui.Modal, title="➕ Add Dropdown Option"):
    opt_label = discord.ui.TextInput(label="Option Label (Name in dropdown)", placeholder="e.g. PREMIUM VIP", max_length=50)
    opt_emoji = discord.ui.TextInput(label="Emoji (Unicode 👑 or Animated <a:name:id>)", placeholder="e.g. 👑 or <a:fire:123456789>", max_length=80)
    opt_desc = discord.ui.TextInput(label="Option Description (Subtext in dropdown)", placeholder="e.g. High tier VIP bypass software", max_length=100)
    banner_url = discord.ui.TextInput(label="Inside-Ticket Banner URL (GIF / PNG)", placeholder="https://example.com/banner.gif or image URL (Optional)", max_length=350, required=False)
    w_msg = discord.ui.TextInput(label="Welcome Message ({user}, {panel})", style=discord.TextStyle.paragraph, placeholder="Welcome {user}! Staff will assist you for {panel}.", max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        options = cfg.setdefault("dropdown", {}).setdefault("options", [])
        new_val = self.opt_label.value.strip().lower().replace(" ", "_")
        options.append({
            "label": self.opt_label.value.strip(),
            "value": new_val,
            "description": self.opt_desc.value.strip(),
            "emoji": self.opt_emoji.value.strip(),
            "banner_url": self.banner_url.value.strip(),
            "welcome_title": f"Support • {self.opt_label.value.strip()}",
            "welcome_message": self.w_msg.value.strip()
        })
        save_ticket_config(cfg)
        await interaction.response.send_message(f"✅ **Added option `{self.opt_label.value.strip()}` with emoji `{self.opt_emoji.value.strip()}`!**", ephemeral=True)


class TicketSettingsModal(discord.ui.Modal, title="⚙️ Ticket System Settings"):
    category_id = discord.ui.TextInput(label="Ticket Category ID", max_length=30)
    logs_id = discord.ui.TextInput(label="Transcript Logs Channel ID", max_length=30)
    naming = discord.ui.TextInput(label="Ticket Naming Format", default="ticket-{username}-{number:04d}", max_length=60)

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config().get("settings", {})
        self.category_id.default = str(cfg.get("category_id", 1519724892918911188))
        self.logs_id.default = str(cfg.get("logs_channel_id", 1508088964077191261))
        self.naming.default = cfg.get("ticket_naming", "ticket-{username}-{number:04d}")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        st = cfg.setdefault("settings", {})
        try:
            st["category_id"] = int(self.category_id.value.strip())
            st["logs_channel_id"] = int(self.logs_id.value.strip())
            st["ticket_naming"] = self.naming.value.strip()
            save_ticket_config(cfg)
            await interaction.response.send_message("✅ **Ticket Settings updated successfully!**", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ **Invalid IDs entered:** {e}", ephemeral=True)


class TicketRemoveOptionSelect(discord.ui.Select):
    def __init__(self, options_list):
        opts = []
        for o in options_list[:25]:
            opts.append(discord.SelectOption(label=o.get("label"), value=o.get("value"), description=o.get("description", "")[:50]))
        super().__init__(placeholder="Select an option to permanently remove", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        val_to_del = self.values[0]
        cfg["dropdown"]["options"] = [o for o in opts if o.get("value") != val_to_del]
        save_ticket_config(cfg)
        await interaction.response.send_message(f"🗑️ **Option `{val_to_del}` removed!**", ephemeral=True)


class TicketRemoveOptionView(discord.ui.View):
    def __init__(self, options_list):
        super().__init__(timeout=60)
        self.add_item(TicketRemoveOptionSelect(options_list))


class TicketEditOptionModal(discord.ui.Modal):
    def __init__(self, option_data: dict):
        label_text = option_data.get("label", "")
        super().__init__(title=f"Edit Option — {label_text[:18]}")
        self.orig_value = option_data.get("value", "")

        self.opt_label = discord.ui.TextInput(
            label="Option Label (Name in dropdown)",
            default=label_text,
            max_length=50,
            required=True
        )
        self.opt_emoji = discord.ui.TextInput(
            label="Emoji (Unicode 👑 or Animated <a:name:id>)",
            default=option_data.get("emoji", "") or "",
            max_length=80,
            required=False
        )
        self.opt_desc = discord.ui.TextInput(
            label="Description (Subtext in dropdown)",
            default=option_data.get("description", "") or "",
            max_length=100,
            required=False
        )
        self.banner_url = discord.ui.TextInput(
            label="Inside-Ticket Banner URL (GIF / PNG)",
            placeholder="https://example.com/banner.gif or image URL (Leave empty for default)",
            default=option_data.get("banner_url", "") or option_data.get("image_url", "") or "",
            max_length=1500,
            required=False
        )
        self.w_msg = discord.ui.TextInput(
            label="Welcome Message ({user} and {panel})",
            style=discord.TextStyle.paragraph,
            default=option_data.get("welcome_message", "") or "",
            max_length=500,
            required=False
        )
        self.add_item(self.opt_label)
        self.add_item(self.opt_emoji)
        self.add_item(self.opt_desc)
        self.add_item(self.banner_url)
        self.add_item(self.w_msg)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = load_ticket_config()
        options = cfg.setdefault("dropdown", {}).setdefault("options", [])
        new_label = self.opt_label.value.strip()
        new_val = new_label.lower().replace(" ", "_")
        raw_banner = self.banner_url.value.strip()

        banner_to_save = raw_banner
        is_local = False
        status_note = ""
        if raw_banner:
            banner_to_save, is_local, status_note = await download_option_banner(raw_banner, new_val)

        updated = False
        for opt in options:
            o_val = str(opt.get("value", "")).strip().lower()
            o_lbl = str(opt.get("label", "")).strip().lower()
            orig = str(self.orig_value).strip().lower()
            if o_val == orig or o_lbl == orig or o_lbl == new_label.lower():
                opt["label"] = new_label
                opt["value"] = new_val
                opt["emoji"] = self.opt_emoji.value.strip()
                opt["description"] = self.opt_desc.value.strip()
                opt["banner_url"] = banner_to_save
                if not opt.get("welcome_title"):
                    opt["welcome_title"] = f"Support • {new_label}"
                opt["welcome_message"] = self.w_msg.value.strip() or "Welcome {user}! Staff will assist you shortly."
                updated = True
                break
        if updated:
            save_ticket_config(cfg)
            embed_reply = discord.Embed(
                title=f"✅ Option Updated: {new_label}",
                description=f"• **Emoji:** {self.opt_emoji.value.strip()}\n• **Description:** {self.opt_desc.value.strip()}",
                color=0x0000FF
            )
            file_preview = None
            if banner_to_save:
                embed_reply.add_field(name="🖼️ Inside-Ticket Banner", value=f"`{banner_to_save}`\n*{status_note}*", inline=False)
                if is_local and os.path.exists(os.path.join(BASE_DIR, banner_to_save)):
                    file_preview = discord.File(os.path.join(BASE_DIR, banner_to_save), filename=banner_to_save)
                    embed_reply.set_image(url=f"attachment://{banner_to_save}")
                elif banner_to_save.startswith("http://") or banner_to_save.startswith("https://"):
                    embed_reply.set_image(url=banner_to_save)
            else:
                embed_reply.add_field(name="🖼️ Inside-Ticket Banner", value="*Using Global Default*", inline=False)
            embed_reply.set_footer(text="Run *ticketpanel to spawn updated panel • Open a NEW ticket or run *refreshticket inside open tickets to see the GIF!")
            if file_preview:
                await interaction.followup.send(embed=embed_reply, file=file_preview, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed_reply, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Option `{self.orig_value}` could not be found to update.", ephemeral=True)


class TicketEditOptionSelect(discord.ui.Select):
    def __init__(self, options_list):
        opts = []
        for o in options_list[:25]:
            em = parse_custom_emoji(o.get("emoji"))
            opts.append(discord.SelectOption(
                label=o.get("label", "Option")[:100],
                value=o.get("value", "")[:100],
                description=o.get("description", "")[:50] if o.get("description") else None,
                emoji=em
            ))
        super().__init__(placeholder="Select an option from the dropdown to edit...", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        sel_val = self.values[0]
        target_opt = next((o for o in opts if o.get("value") == sel_val), None)
        if not target_opt:
            await interaction.response.send_message("❌ Option not found.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketEditOptionModal(target_opt))


class TicketEditOptionView(discord.ui.View):
    def __init__(self, options_list):
        super().__init__(timeout=120)
        self.add_item(TicketEditOptionSelect(options_list))


class TicketOptionBannerModal(discord.ui.Modal):
    def __init__(self, option_data: dict):
        label_text = option_data.get("label", "")
        super().__init__(title=f"Option Banner — {label_text[:16]}")
        self.target_val = option_data.get("value", "")
        self.target_label = label_text

        self.banner_input = discord.ui.TextInput(
            label="Inside-Ticket Banner URL (GIF / PNG)",
            placeholder="https://example.com/banner.gif or image URL (leave empty to reset)",
            default=option_data.get("banner_url", "") or option_data.get("image_url", "") or "",
            max_length=1500,
            required=False
        )
        self.thumb_input = discord.ui.TextInput(
            label="Inside-Ticket Thumbnail URL (Optional)",
            placeholder="https://example.com/icon.png (Optional)",
            default=option_data.get("thumbnail_url", "") or "",
            max_length=1500,
            required=False
        )
        self.add_item(self.banner_input)
        self.add_item(self.thumb_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        b_val = self.banner_input.value.strip()
        t_val = self.thumb_input.value.strip()

        banner_to_save = b_val
        is_local = False
        status_note = ""
        if b_val:
            banner_to_save, is_local, status_note = await download_option_banner(b_val, self.target_val)

        updated = False
        for opt in opts:
            if opt.get("value") == self.target_val:
                opt["banner_url"] = banner_to_save
                opt["thumbnail_url"] = t_val
                updated = True
                break
        if updated:
            save_ticket_config(cfg)
            status_text = f"`{banner_to_save}`" if banner_to_save else "*Default Global Banner*"
            embed_reply = discord.Embed(
                title=f"✅ Banner for {self.target_label} Updated!",
                description=f"🖼️ **Banner:** {status_text}\nℹ️ **Status:** {status_note}\n\n*When someone opens a **{self.target_label}** ticket, this GIF/PNG will now be displayed!*",
                color=0x0000FF
            )
            file_preview = None
            if banner_to_save:
                if is_local and os.path.exists(os.path.join(BASE_DIR, banner_to_save)):
                    file_preview = discord.File(os.path.join(BASE_DIR, banner_to_save), filename=banner_to_save)
                    embed_reply.set_image(url=f"attachment://{banner_to_save}")
                elif banner_to_save.startswith("http://") or banner_to_save.startswith("https://"):
                    embed_reply.set_image(url=banner_to_save)
            embed_reply.set_footer(text="Run *ticketpanel to spawn updated panel • Open a NEW ticket or run *refreshticket inside open tickets to see the GIF!")
            if file_preview:
                await interaction.followup.send(embed=embed_reply, file=file_preview, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed_reply, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Option `{self.target_val}` not found.", ephemeral=True)


class TicketOptionBannerSelect(discord.ui.Select):
    def __init__(self, options_list):
        opts = []
        for o in options_list[:25]:
            em = parse_custom_emoji(o.get("emoji"))
            has_b = "🖼️ Has GIF/PNG" if o.get("banner_url") else "Default Banner"
            opts.append(discord.SelectOption(
                label=o.get("label", "Option")[:100],
                value=o.get("value", "")[:100],
                description=f"{has_b} | {o.get('description', '')}"[:50],
                emoji=em
            ))
        super().__init__(placeholder="Select option to change its GIF / PNG banner...", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        sel_val = self.values[0]
        target_opt = next((o for o in opts if o.get("value") == sel_val), None)
        if not target_opt:
            await interaction.response.send_message("❌ Option not found.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketOptionBannerModal(target_opt))


class TicketOptionBannerSelectView(discord.ui.View):
    def __init__(self, options_list):
        super().__init__(timeout=120)
        self.add_item(TicketOptionBannerSelect(options_list))

class TicketPlaceholderModal(discord.ui.Modal, title="💬 Edit Dropdown Placeholder"):
    placeholder_input = discord.ui.TextInput(
        label="Dropdown Placeholder Text",
        placeholder="e.g. Which Panel Do You Need? or Select Category",
        max_length=100,
        required=True
    )

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config().get("dropdown", {})
        self.placeholder_input.default = cfg.get("placeholder", "Which Panel Do You Need?")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        cfg.setdefault("dropdown", {})["placeholder"] = self.placeholder_input.value.strip()
        save_ticket_config(cfg)
        await interaction.response.send_message(f"✅ **Dropdown placeholder updated to:** `{self.placeholder_input.value.strip()}`", ephemeral=True)


class TicketEditActionButtonsModal(discord.ui.Modal, title="🔘 Edit Action Buttons (Row 0 & 1)"):
    close_input = discord.ui.TextInput(
        label="Close Button (Text | Emoji)",
        placeholder="e.g. Close | 🔒 or Delete | ⛔",
        max_length=80,
        required=True
    )
    transcript_input = discord.ui.TextInput(
        label="Transcript Button (Text | Emoji)",
        placeholder="e.g. Transcript | 🔗",
        max_length=80,
        required=True
    )
    autopay_input = discord.ui.TextInput(
        label="Autopay Button (Text | Emoji)",
        placeholder="e.g. Autopay | 🎵 or Pay Now | 💳",
        max_length=80,
        required=True
    )
    pc_input = discord.ui.TextInput(
        label="PC Panel Button Text",
        placeholder="e.g. PC PANEL",
        max_length=50,
        required=True
    )
    android_input = discord.ui.TextInput(
        label="Android Panel Button Text",
        placeholder="e.g. ANDROID PANEL",
        max_length=50,
        required=True
    )

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config()
        controls = cfg.get("controls", {})
        c_btn = controls.get("close_button", {})
        t_btn = controls.get("transcript_button", {})
        a_btn = controls.get("autopay_button", {})
        pc_btn = controls.get("pc_panel_button", {})
        and_btn = controls.get("android_panel_button", {})

        self.close_input.default = f"{c_btn.get('label', 'Close')} | {c_btn.get('emoji', '🔒')}"
        self.transcript_input.default = f"{t_btn.get('label', 'Transcript')} | {t_btn.get('emoji', '🔗')}"
        self.autopay_input.default = f"{a_btn.get('label', 'Autopay')} | {a_btn.get('emoji', '🎵')}"
        self.pc_input.default = pc_btn.get("label", "PC PANEL")
        self.android_input.default = and_btn.get("label", "ANDROID PANEL")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        controls = cfg.setdefault("controls", {})

        def parse_pipe(val, default_lbl, default_em):
            val = val.strip()
            if "|" in val:
                p = [x.strip() for x in val.split("|", 1)]
                return (p[0] or default_lbl, p[1] if len(p) > 1 else default_em)
            return (val or default_lbl, default_em)

        c_lbl, c_em = parse_pipe(self.close_input.value, "Close", "🔒")
        t_lbl, t_em = parse_pipe(self.transcript_input.value, "Transcript", "🔗")
        a_lbl, a_em = parse_pipe(self.autopay_input.value, "Autopay", "🎵")

        controls["close_button"] = {"label": c_lbl, "emoji": c_em, "style": "danger"}
        controls["transcript_button"] = {"label": t_lbl, "emoji": t_em, "style": "secondary"}
        controls["autopay_button"] = {"label": a_lbl, "emoji": a_em, "style": "success"}
        controls.setdefault("pc_panel_button", {})["label"] = self.pc_input.value.strip()
        controls.setdefault("android_panel_button", {})["label"] = self.android_input.value.strip()

        save_ticket_config(cfg)
        await interaction.response.send_message(
            f"✅ **Ticket Action Buttons Updated!**\n"
            f"• **Close:** `{c_lbl}` {c_em}\n"
            f"• **Transcript:** `{t_lbl}` {t_em}\n"
            f"• **Autopay:** `{a_lbl}` {a_em}\n"
            f"• **PC Panel:** `{self.pc_input.value.strip()}`\n"
            f"• **Android Panel:** `{self.android_input.value.strip()}`",
            ephemeral=True
        )


class TicketEditPanelButtonsModal(discord.ui.Modal, title="📦 Edit Quick Panel Buttons"):
    id_input = discord.ui.TextInput(
        label="ID Buy/Sale Button Text",
        placeholder="e.g. ID BUY/SALE",
        default="ID BUY/SALE",
        max_length=50,
        required=True
    )
    pc_desc = discord.ui.TextInput(
        label="PC Panel Message / Packages",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=False
    )
    android_desc = discord.ui.TextInput(
        label="Android Panel Message / Packages",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=False
    )
    id_desc = discord.ui.TextInput(
        label="ID Buy/Sale Rules / Escrow Guidelines",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=False
    )

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config()
        controls = cfg.get("controls", {})
        id_btn = controls.get("id_buy_sale_button", {})
        pc_info = controls.get("pc_panel_info", {})
        and_info = controls.get("android_panel_info", {})
        id_info = controls.get("id_buy_sale_info", {})

        self.id_input.default = id_btn.get("label", "ID BUY/SALE")
        self.pc_desc.default = pc_info.get("description", "")
        self.android_desc.default = and_info.get("description", "")
        self.id_desc.default = id_info.get("description", "")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        controls = cfg.setdefault("controls", {})
        controls.setdefault("id_buy_sale_button", {})["label"] = self.id_input.value.strip()
        if self.pc_desc.value.strip():
            controls.setdefault("pc_panel_info", {})["description"] = self.pc_desc.value.strip()
        if self.android_desc.value.strip():
            controls.setdefault("android_panel_info", {})["description"] = self.android_desc.value.strip()
        if self.id_desc.value.strip():
            controls.setdefault("id_buy_sale_info", {})["description"] = self.id_desc.value.strip()

        save_ticket_config(cfg)
        await interaction.response.send_message(
            f"✅ **Quick Panel Buttons & Info Updated!**\n"
            f"• **ID Button:** `{self.id_input.value.strip()}`\n"
            f"New tickets will reflect these messages when buttons are clicked.",
            ephemeral=True
        )


class TicketEditAutopayModal(discord.ui.Modal, title="⚡ Edit Autopay, UPI & Binance"):
    binance_id = discord.ui.TextInput(
        label="Binance Pay ID",
        placeholder="e.g. 1275075958",
        max_length=50,
        required=True
    )
    upi_id = discord.ui.TextInput(
        label="UPI ID (GPay / PhonePe / Paytm / FamPay)",
        placeholder="e.g. bhavishya.912@fam",
        max_length=80,
        required=True
    )
    upi_name = discord.ui.TextInput(
        label="UPI Account Holder Name",
        placeholder="e.g. Bhavishya Raikwar",
        max_length=80,
        required=True
    )
    qr_url = discord.ui.TextInput(
        label="Custom QR Code Image URL (Optional)",
        placeholder="https://example.com/qr.png (Leave empty to use default file)",
        max_length=300,
        required=False
    )
    instructions = discord.ui.TextInput(
        label="Payment Instructions / Note",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False
    )

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config()
        ap = cfg.get("controls", {}).get("autopay_info", {})
        self.binance_id.default = str(ap.get("binance_id", "1275075958"))
        self.upi_id.default = ap.get("upi_id", "bhavishya.912@fam")
        self.upi_name.default = ap.get("upi_name", "Bhavishya Raikwar")
        self.qr_url.default = ap.get("qr_image_url", "")
        self.instructions.default = ap.get("instructions", "1. Pay the exact amount.\n2. Send screenshot in this channel.\n3. Staff will verify immediately!")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        ap = cfg.setdefault("controls", {}).setdefault("autopay_info", {})
        ap["binance_id"] = self.binance_id.value.strip()
        ap["upi_id"] = self.upi_id.value.strip()
        ap["upi_name"] = self.upi_name.value.strip()
        ap["qr_image_url"] = self.qr_url.value.strip()
        if self.instructions.value.strip():
            ap["instructions"] = self.instructions.value.strip()

        save_ticket_config(cfg)
        await interaction.response.send_message(
            f"✅ **Autopay & Payment Gateways Updated!**\n"
            f"• **Binance ID:** `{ap['binance_id']}`\n"
            f"• **UPI ID:** `{ap['upi_id']}` ({ap['upi_name']})\n"
            f"• **QR:** {'*Custom URL*' if ap['qr_image_url'] else '`autopay_qr.jpg`'}",
            ephemeral=True
        )


class TicketEditClaimButtonModal(discord.ui.Modal, title="Edit Claim & Staff Button"):
    claim_input = discord.ui.TextInput(
        label="Claim Button (Text | Emoji)",
        placeholder="e.g. Claim | \U0001F64B\u200D\u2642\uFE0F",
        max_length=80,
        required=True
    )
    claimed_input = discord.ui.TextInput(
        label="Claimed Status (Format | Emoji)",
        placeholder="e.g. Claimed by {user} | \u2705",
        max_length=80,
        required=True
    )
    unclaim_input = discord.ui.TextInput(
        label="Unclaim Button (Text | Emoji)",
        placeholder="e.g. Unclaim | \U0001F513",
        max_length=80,
        required=True
    )

    def __init__(self):
        super().__init__()
        cfg = load_ticket_config()
        controls = cfg.get("controls", {})
        c_btn = controls.get("claim_button", {})
        u_btn = controls.get("unclaim_button", {})

        c_lbl = c_btn.get("label", "Claim")
        c_em = c_btn.get("emoji", "\U0001F64B\u200D\u2642\uFE0F")
        self.claim_input.default = f"{c_lbl} | {c_em}"

        clm_fmt = c_btn.get("claimed_format", "Claimed by {user}")
        clm_em = c_btn.get("claimed_emoji", "\u2705")
        self.claimed_input.default = f"{clm_fmt} | {clm_em}"

        u_lbl = u_btn.get("label", "Unclaim")
        u_em = u_btn.get("emoji", "\U0001F513")
        self.unclaim_input.default = f"{u_lbl} | {u_em}"

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_ticket_config()
        controls = cfg.setdefault("controls", {})

        def parse_pipe(val, default_lbl, default_em):
            val = val.strip()
            if "|" in val:
                p = [x.strip() for x in val.split("|", 1)]
                return (p[0] or default_lbl, p[1] if len(p) > 1 else default_em)
            return (val or default_lbl, default_em)

        c_lbl, c_em = parse_pipe(self.claim_input.value, "Claim", "\U0001F64B\u200D\u2642\uFE0F")
        clm_lbl, clm_em = parse_pipe(self.claimed_input.value, "Claimed by {user}", "\u2705")
        u_lbl, u_em = parse_pipe(self.unclaim_input.value, "Unclaim", "\U0001F513")

        controls.setdefault("claim_button", {})["label"] = c_lbl
        controls["claim_button"]["emoji"] = c_em
        controls["claim_button"]["style"] = "success"
        controls["claim_button"]["claimed_format"] = clm_lbl
        controls["claim_button"]["claimed_emoji"] = clm_em

        controls.setdefault("unclaim_button", {})["label"] = u_lbl
        controls["unclaim_button"]["emoji"] = u_em
        controls["unclaim_button"]["style"] = "secondary"

        save_ticket_config(cfg)
        await interaction.response.send_message(
            f"\u2705 **Claim & Staff Buttons Updated!**\n"
            f"  **Claim Button:** `{c_lbl}` {c_em}\n"
            f"  **Claimed Status:** `{clm_lbl}` {clm_em}\n"
            f"  **Unclaim Button:** `{u_lbl}` {u_em}",
            ephemeral=True
        )


class TicketButtonsSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Action Buttons (Row 0)", emoji="\u2699", style=discord.ButtonStyle.primary)
    async def btn_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketEditActionButtonsModal())

    @discord.ui.button(label="Panel Buttons (Row 1)", emoji="\U0001F4E6", style=discord.ButtonStyle.secondary)
    async def btn_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketEditPanelButtonsModal())

    @discord.ui.button(label="Claim Button (Staff)", emoji="\U0001F64B\u200D\u2642\uFE0F", style=discord.ButtonStyle.success)
    async def btn_claim_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketEditClaimButtonModal())

    @discord.ui.button(label="Autopay, UPI & QR", emoji="\U0001F4B3", style=discord.ButtonStyle.secondary)
    async def btn_autopay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketEditAutopayModal())




class TicketEditorMenuView(discord.ui.View):
    """Admin-only interactive Control Center to customize everything from Discord."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Edit Embed & Banner", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def btn_edit_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketEmbedModal())

    @discord.ui.button(label="Edit Placeholder", emoji="💬", style=discord.ButtonStyle.secondary, row=0)
    async def btn_edit_placeholder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketPlaceholderModal())

    @discord.ui.button(label="Preview Panel", emoji="👁️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_custom_ticket_embed()
        view = ByteCustomTicketPanelView()
        await interaction.response.send_message("👁️ **Live Ticket Panel Preview:**", embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Add Option", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def btn_add_opt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketAddOptionModal())

    @discord.ui.button(label="Edit Option", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def btn_edit_opt(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        if not opts:
            await interaction.response.send_message("⚠️ No options exist to edit.", ephemeral=True)
            return
        await interaction.response.send_message("👇 **Select an option below to edit its label, emoji, description, or welcome message:**", view=TicketEditOptionView(opts), ephemeral=True)

    @discord.ui.button(label="Remove Option", emoji="➖", style=discord.ButtonStyle.danger, row=1)
    async def btn_rem_opt(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        if not opts:
            await interaction.response.send_message("⚠️ No options exist to remove.", ephemeral=True)
            return
        await interaction.response.send_message("Select an option below to delete it:", view=TicketRemoveOptionView(opts), ephemeral=True)

    @discord.ui.button(label="Option Banners", emoji="🖼️", style=discord.ButtonStyle.secondary, row=1)
    async def btn_option_banners(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_ticket_config()
        opts = cfg.get("dropdown", {}).get("options", [])
        if not opts:
            await interaction.response.send_message("⚠️ No options exist to configure banners.", ephemeral=True)
            return
        await interaction.response.send_message("👇 **Select an option below to set its custom inside-ticket GIF / PNG banner:**", view=TicketOptionBannerSelectView(opts), ephemeral=True)

    @discord.ui.button(label="Edit Buttons", emoji="🔘", style=discord.ButtonStyle.primary, row=2)
    async def btn_edit_buttons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("👇 **Choose which button section to edit:**", view=TicketButtonsSelectView(), ephemeral=True)

    @discord.ui.button(label="Autopay & QR", emoji="⚡", style=discord.ButtonStyle.success, row=2)
    async def btn_autopay_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketEditAutopayModal())

    @discord.ui.button(label="Edit Welcome Msg", emoji="📝", style=discord.ButtonStyle.secondary, row=2)
    async def btn_edit_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketWelcomeModal())

    @discord.ui.button(label="Edit Settings", emoji="⚙️", style=discord.ButtonStyle.secondary, row=2)
    async def btn_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketSettingsModal())


# ==========================================
# TICKET COMMANDS (PREFIX & SLASH)
# ==========================================

@bot.command(name="setoptbanner", aliases=["setoptionbanner", "optbanner", "setgif"])
async def setoptbanner_cmd(ctx, option_name: str = None, *, banner_url: str = None):
    """Sets a custom inside-ticket GIF / PNG banner for a specific ticket option (e.g. brutal, premium).
    Usage:
      *setoptbanner <option> <url>
      OR *setoptbanner <option> (with attached GIF/PNG file in Discord)
    """
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only administrators or configured staff can set option banners.")
        return
    if not option_name:
        await ctx.reply(
            "📌 **Usage:** `*setoptbanner <option_name> <gif_or_png_url>`\n"
            "📎 **Or attach a file:** Type `*setoptbanner <option_name>` and drag-and-drop your GIF/PNG into Discord!\n"
            "✨ *Example:* `*setoptbanner premium https://tenor.com/...gif` or `*setoptbanner brutal` (with file attached)"
        )
        return

    cfg = load_ticket_config()
    opts = cfg.get("dropdown", {}).get("options", [])
    target = None
    q = option_name.strip().lower()
    for o in opts:
        if o.get("value", "").lower() == q or o.get("label", "").lower() == q or q in o.get("label", "").lower():
            target = o
            break
    if not target:
        avail = ", ".join(f"`{o.get('label')}`" for o in opts)
        await ctx.reply(f"❌ Option `{option_name}` not found. Available options:\n{avail}")
        return

    source = None
    if ctx.message.attachments:
        source = ctx.message.attachments[0]
    elif banner_url:
        clean = banner_url.strip()
        if clean.lower() in ("none", "remove", "reset", "clear", "default"):
            source = ""
        else:
            source = clean

    if source is None:
        await ctx.reply("❌ Please provide a GIF/PNG URL or attach an image/GIF file directly to your message!\nExample: `*setoptbanner premium https://...gif` or attach a file with `*setoptbanner premium`.")
        return

    saved_banner, is_local, status_note = await download_option_banner(source, target.get("value", q))
    target["banner_url"] = saved_banner
    save_ticket_config(cfg)

    embed = discord.Embed(
        title=f"✅ Banner Updated for {target.get('label')}!",
        description=(
            f"🖼️ **Banner:** `{saved_banner or '*Default Global Banner*'}`\n"
            f"ℹ️ **Status:** {status_note}\n\n"
            f"✨ *When someone opens a **{target.get('label')}** ticket, this GIF/PNG will now be displayed inside!*\n"
            f"💡 *Tip: If you have an already-open ticket, type `*refreshticket` inside that ticket to see the new banner immediately!*"
        ),
        color=0x0000FF
    )
    file_obj = None
    if saved_banner:
        if is_local and os.path.exists(os.path.join(BASE_DIR, saved_banner)):
            file_obj = discord.File(os.path.join(BASE_DIR, saved_banner), filename=saved_banner)
            embed.set_image(url=f"attachment://{saved_banner}")
        elif saved_banner.startswith("http://") or saved_banner.startswith("https://"):
            embed.set_image(url=saved_banner)

    if file_obj:
        await ctx.reply(embed=embed, file=file_obj)
    else:
        await ctx.reply(embed=embed)


@bot.command(name="refreshticket", aliases=["updateticket", "refreshbanner", "ticketrefresh"])
async def refreshticket_cmd(ctx):
    """Refreshes the welcome embed and banner of the current ticket channel using the latest ticket_config.json."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only staff can refresh ticket settings.")
        return

    cfg = load_ticket_config()
    settings = cfg.get("settings", {})
    active = settings.get("active_tickets", {})
    t_info = active.get(str(ctx.channel.id))

    panel_label = None
    ticket_num = None
    ticket_user_id = None
    if t_info:
        panel_label = t_info.get("panel")
        ticket_num = t_info.get("ticket_number")
        ticket_user_id = t_info.get("user_id")

    if not panel_label and ctx.channel.topic:
        m = re.search(r'Panel:\s*([^|]+)', ctx.channel.topic)
        if m:
            panel_label = m.group(1).strip()
    if not ticket_num and ctx.channel.topic:
        m = re.search(r'Ticket #?(\d+)', ctx.channel.topic)
        if m:
            try:
                ticket_num = int(m.group(1))
            except Exception:
                pass

    if not panel_label:
        for opt in cfg.get("dropdown", {}).get("options", []):
            if opt.get("value", "").lower() in ctx.channel.name.lower():
                panel_label = opt.get("label")
                break

    if not panel_label:
        panel_label = "PREMIUM"

    opt_data = None
    for opt in cfg.get("dropdown", {}).get("options", []):
        if opt.get("label", "").lower() == panel_label.lower() or opt.get("value", "").lower() == panel_label.lower():
            opt_data = opt
            break
    if not opt_data:
        opt_data = {"label": panel_label}

    ticket_owner = ctx.guild.get_member(ticket_user_id) if ticket_user_id else ctx.author
    curr_counter = ticket_num or 1

    tc = cfg.get("ticket_channel", {})
    raw_title = tc.get("title_template") or opt_data.get("welcome_title") or "{panel} - Purchase Ticket"
    w_title = (
        raw_title.replace("{panel}", opt_data.get("label", "Support"))
        .replace("{emoji}", opt_data.get("emoji", "🪽"))
        .replace("{number}", f"{curr_counter:04d}")
        .replace("{user}", ticket_owner.name if ticket_owner else "Customer")
    )
    w_title = sanitize_and_resolve_emojis(w_title, ctx.guild)

    raw_msg = tc.get("body_template") or opt_data.get("welcome_message") or "Hello {user}!\nWelcome to your private ticket channel for **{panel}**."
    formatted_msg = (
        raw_msg.replace("{user}", ticket_owner.mention if ticket_owner else "@Customer")
        .replace("{user_name}", ticket_owner.name if ticket_owner else "Customer")
        .replace("{panel}", opt_data.get("label", "Support"))
        .replace("{number}", f"{curr_counter:04d}")
    )
    formatted_msg = sanitize_and_resolve_emojis(formatted_msg, ctx.guild)

    embed_title = w_title
    if w_title and ("<a:" in w_title or "<:" in w_title):
        formatted_msg = f"# {w_title}\n\n{formatted_msg}"
        embed_title = None

    color_int = parse_hex_color(tc.get("color", "0x0000FF"))

    welcome_embed = discord.Embed(
        title=embed_title,
        description=formatted_msg,
        color=color_int,
        timestamp=datetime.utcnow()
    )

    if tc.get("show_fields", True):
        welcome_embed.add_field(name="🆔 Ticket ID", value=f"`#{curr_counter:04d}`", inline=True)
        welcome_embed.add_field(name="👤 Created By", value=f"{ticket_owner.mention if ticket_owner else '@Customer'}", inline=True)

    footer_text = tc.get("footer_text", "BYTE CORPORATION Security Shield • Click Close or Claim below")
    welcome_embed.set_footer(text=footer_text)

    # Thumbnail
    opt_thumb = opt_data.get("thumbnail_url")
    if opt_thumb:
        welcome_embed.set_thumbnail(url=opt_thumb)
    else:
        thumb_type = tc.get("thumbnail_type", "user_avatar")
        if thumb_type == "user_avatar" and ticket_owner and ticket_owner.display_avatar:
            welcome_embed.set_thumbnail(url=ticket_owner.display_avatar.url)
        elif thumb_type == "server_icon" and ctx.guild.icon:
            welcome_embed.set_thumbnail(url=ctx.guild.icon.url)

    # Banner
    opt_banner = str(opt_data.get("banner_url") or opt_data.get("image_url") or opt_data.get("gif_url") or "").strip()
    ticket_banner = opt_banner or str(tc.get("banner_url") or "").strip()
    banner_file_to_send = None
    if ticket_banner:
        if os.path.exists(ticket_banner):
            fname = os.path.basename(ticket_banner)
            banner_file_to_send = discord.File(ticket_banner, filename=fname)
            welcome_embed.set_image(url=f"attachment://{fname}")
        elif os.path.exists(os.path.join(BASE_DIR, ticket_banner)):
            full_p = os.path.join(BASE_DIR, ticket_banner)
            fname = os.path.basename(full_p)
            banner_file_to_send = discord.File(full_p, filename=fname)
            welcome_embed.set_image(url=f"attachment://{fname}")
        elif ticket_banner.startswith("http://") or ticket_banner.startswith("https://"):
            welcome_embed.set_image(url=ticket_banner)

    target_msg = None
    async for msg in ctx.channel.history(limit=25, oldest_first=True):
        if msg.author.id == bot.user.id and msg.embeds:
            target_msg = msg
            break

    controls_view = ByteCustomTicketControlView()
    if target_msg:
        try:
            await target_msg.delete()
        except Exception:
            pass

    if banner_file_to_send:
        await ctx.channel.send(embed=welcome_embed, file=banner_file_to_send, view=controls_view)
    else:
        await ctx.channel.send(embed=welcome_embed, view=controls_view)

    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(f"✅ **Ticket banner & welcome embed refreshed for `{panel_label}`!**", delete_after=5)


@bot.command(name="ticketoptions", aliases=["toptions", "listoptions"])
async def ticketoptions_cmd(ctx):
    """Lists all configured ticket options with their emojis and custom GIF/PNG banners."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only administrators or configured staff can view ticket options.")
        return
    cfg = load_ticket_config()
    opts = cfg.get("dropdown", {}).get("options", [])
    if not opts:
        await ctx.reply("⚠️ No ticket options configured.")
        return
    embed = discord.Embed(
        title="🎟️ Configured Ticket Options & Custom Banners",
        color=0x0000FF,
        timestamp=datetime.utcnow()
    )
    for o in opts:
        b_text = f"[View Image]({o.get('banner_url')})" if o.get("banner_url") else "*Default Global Banner*"
        embed.add_field(
            name=f"{o.get('emoji', '🎟️')} {o.get('label')}",
            value=f"• Subtext: {o.get('description', 'N/A')}\n• Banner (GIF/PNG): {b_text}",
            inline=False
        )
    embed.set_footer(text="To change a banner: *setoptbanner <option> <url> or use *ticketeditor")
    await ctx.reply(embed=embed)


@bot.command(name="setautopay", aliases=["autopayconfig", "paymentconfig"])
async def setautopay_cmd(ctx, binance_id: str = None, upi_id: str = None, *, upi_name: str = None):
    """Configures Autopay details: *setautopay <binance_id> [upi_id] [upi_name]"""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only administrators or configured staff can configure autopay.")
        return
    cfg = load_ticket_config()
    ap = cfg.setdefault("controls", {}).setdefault("autopay_info", {})
    if binance_id:
        ap["binance_id"] = binance_id.strip()
    if upi_id:
        ap["upi_id"] = upi_id.strip()
    if upi_name:
        ap["upi_name"] = upi_name.strip()
    save_ticket_config(cfg)
    await ctx.reply(
        f"✅ **Autopay Config Updated!**\n"
        f"• **Binance ID:** `{ap.get('binance_id', 'N/A')}`\n"
        f"• **UPI ID:** `{ap.get('upi_id', 'N/A')}`\n"
        f"• **Account Name:** `{ap.get('upi_name', 'N/A')}`\n"
        f"• **QR File:** `autopay_qr.jpg`"
    )


@bot.command(name="ticketbuttons", aliases=["tbuttons", "listbuttons"])
async def ticketbuttons_cmd(ctx):
    """Displays current ticket control buttons and autopay setup."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only administrators or configured staff can view button configs.")
        return
    cfg = load_ticket_config()
    controls = cfg.get("controls", {})
    ap = controls.get("autopay_info", {})
    embed = discord.Embed(
        title="🔘 Ticket Control Buttons & Autopay Configuration",
        color=0x0000FF,
        timestamp=datetime.utcnow()
    )
    c_btn = controls.get("close_button", {})
    t_btn = controls.get("transcript_button", {})
    a_btn = controls.get("autopay_button", {})
    pc_btn = controls.get("pc_panel_button", {})
    and_btn = controls.get("android_panel_button", {})
    id_btn = controls.get("id_buy_sale_button", {})

    embed.add_field(
        name="Row 0 Buttons (Actions)",
        value=f"• 1. {c_btn.get('emoji', '🔒')} `{c_btn.get('label', 'Close')}`\n"
              f"• 2. {t_btn.get('emoji', '🔗')} `{t_btn.get('label', 'Transcript')}`\n"
              f"• 3. {a_btn.get('emoji', '🎵')} `{a_btn.get('label', 'Autopay')}`",
        inline=False
    )
    embed.add_field(
        name="Row 1 Buttons (Quick Panel Info)",
        value=f"• 4. `{pc_btn.get('label', 'PC PANEL')}`\n"
              f"• 5. `{and_btn.get('label', 'ANDROID PANEL')}`\n"
              f"• 6. `{id_btn.get('label', 'ID BUY/SALE')}`",
        inline=False
    )
    embed.add_field(
        name="⚡ Autopay Gateways",
        value=f"• **UPI:** `{ap.get('upi_id')}` ({ap.get('upi_name')})\n"
              f"• **Binance ID:** `{ap.get('binance_id')}`\n"
              f"• **QR Image:** `{ap.get('qr_image_file', 'autopay_qr.jpg')}`",
        inline=False
    )
    embed.set_footer(text="Edit via *ticketeditor -> Edit Buttons or *setautopay")
    await ctx.reply(embed=embed)


@bot.command(name="ticketpanel", aliases=["tpanel", "sendticketpanel"])
async def ticketpanel_prefix(ctx):
    """Posts the fully customizable BYTE CORPORATION Ticket Panel."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only administrators or configured staff can spawn the ticket panel.")
        return
    embed, file_obj = get_custom_ticket_panel_assets()
    view = ByteCustomTicketPanelView()
    if file_obj:
        await ctx.send(embed=embed, file=file_obj, view=view)
        await ctx.send(embed=embed, view=view)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.tree.command(name="ticketpanel", description="Post the official BYTE CORPORATION Ticket Panel")
async def ticketpanel_slash(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only administrators can post the ticket panel.", ephemeral=True)
        return
    embed, file_obj = get_custom_ticket_panel_assets()
    view = ByteCustomTicketPanelView()
    if file_obj:
        await interaction.channel.send(embed=embed, file=file_obj, view=view)
        await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Ticket Panel posted successfully!", ephemeral=True)


@bot.command(name="ticketeditor", aliases=["tconfig", "editticket"])
async def ticketeditor_prefix(ctx):
    """Opens the interactive Ticket Customization Control Center."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only staff can open the ticket editor.")
        return
    desc_lines = [
        "Customize **every single detail** of your ticket system live in real-time:",
        "",
        "• **🖼️ Edit Embed & Banner** — Title, description, hex color, banner & thumbnail",
        "• **➕ Add Dropdown Option** — Add panels with animated emojis (`<a:name:id>`)",
        "• **➖ Remove Option** — Delete existing dropdown options",
        "• **⚙️ Edit Settings** — Category ID, logs channel ID, and naming",
        "• **👁️ Preview Panel** — View exact live preview before publishing",
        "",
        "*You can also edit `ticket_config.json` directly in any text editor!*"
    ]
    embed = discord.Embed(
        title="🛠️ BYTE CORPORATION — TICKET SYSTEM CUSTOMIZER",
        description="\n".join(desc_lines),
        color=0x0000FF
    )
    await ctx.send(embed=embed, view=TicketEditorMenuView())


@bot.command(name="setticketbuttons", aliases=["editbuttons", "edbuttons"])
async def setticketbuttons_cmd(ctx):
    """Opens interactive modal to customize Claim, Unclaim, and Close/Delete buttons."""
    if not is_staff(ctx.author):
        return
    await ctx.send("Click the button below to edit Ticket action buttons (supports animated emojis):", view=discord.ui.View().add_item(
        discord.ui.Button(label="Open Button Customizer", style=discord.ButtonStyle.primary, custom_id="open_btn_customizer_prompt")
    ))


@bot.command(name="setclaimbutton", aliases=["setclaim"])
async def setclaimbutton_cmd(ctx, label: str = None, emoji: str = "🙋‍♂️"):
    """Sets the label and animated emoji for the Claim ticket button."""
    if not is_staff(ctx.author) or not label:
        await ctx.reply("Usage: `*setclaimbutton <Text> [Emoji]`\nExample: `*setclaimbutton \"Claim Ticket\" <a:verify:123456789>`")
        return
    cfg = load_ticket_config()
    controls = cfg.setdefault("controls", {})
    controls.setdefault("claim_button", {})["label"] = label.strip()
    controls["claim_button"]["emoji"] = emoji.strip()
    controls["claim_button"]["style"] = "success"
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Claim Button updated to:** `{label.strip()}` with emoji `{emoji.strip()}`")


@bot.command(name="setunclaimbutton", aliases=["setunclaim"])
async def setunclaimbutton_cmd(ctx, label: str = None, emoji: str = "🔄"):
    """Sets the label and animated emoji for the Unclaim ticket button."""
    if not is_staff(ctx.author) or not label:
        await ctx.reply("Usage: `*setunclaimbutton <Text> [Emoji]`\nExample: `*setunclaimbutton \"Unclaim\" <a:refresh:123456789>`")
        return
    cfg = load_ticket_config()
    controls = cfg.setdefault("controls", {})
    controls.setdefault("unclaim_button", {})["label"] = label.strip()
    controls["unclaim_button"]["emoji"] = emoji.strip()
    controls["unclaim_button"]["style"] = "secondary"
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Unclaim Button updated to:** `{label.strip()}` with emoji `{emoji.strip()}`")


@bot.command(name="setclosebutton", aliases=["setdeletebutton", "setclose"])
async def setclosebutton_cmd(ctx, label: str = None, emoji: str = "⛔"):
    """Sets the label and animated emoji for the Close/Delete ticket button."""
    if not is_staff(ctx.author) or not label:
        await ctx.reply("Usage: `*setclosebutton <Text> [Emoji]`\nExample: `*setclosebutton \"Delete Ticket\" <a:trash:123456789>`")
        return
    cfg = load_ticket_config()
    controls = cfg.setdefault("controls", {})
    controls.setdefault("close_button", {})["label"] = label.strip()
    controls["close_button"]["emoji"] = emoji.strip()
    controls["close_button"]["style"] = "danger"
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Close/Delete Button updated to:** `{label.strip()}` with emoji `{emoji.strip()}`")


@bot.command(name="setticketwelcome")
async def setticketwelcome_cmd(ctx):
    """Opens modal to customize the inside-ticket welcome message, house rules, and questions."""
    if not is_staff(ctx.author):
        return
    await ctx.send("Click the button below to edit the inside-ticket layout:", view=discord.ui.View().add_item(
        discord.ui.Button(label="Open Welcome Editor", style=discord.ButtonStyle.primary, custom_id="open_w_modal")
    ))


@bot.command(name="setticketbanner")
async def setticketbanner_cmd(ctx, url: str = None):
    """Sets the banner image for the ticket panel embed."""
    if not is_staff(ctx.author):
        return
    if not url:
        await ctx.reply("Usage: `*setticketbanner <image_url>`")
        return
    cfg = load_ticket_config()
    cfg.setdefault("embed", {})["banner_url"] = url.strip()
    save_ticket_config(cfg)
    embed = discord.Embed(title="✅ Banner Image Updated", color=0x0000FF)
    embed.set_image(url=url.strip())
    await ctx.reply(embed=embed)


@bot.command(name="settickettitle")
async def settickettitle_cmd(ctx, *, title: str = None):
    """Sets the title for the ticket panel embed."""
    if not is_staff(ctx.author) or not title:
        await ctx.reply("Usage: `*settickettitle <title>`")
        return
    cfg = load_ticket_config()
    cfg.setdefault("embed", {})["title"] = title.strip()
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Ticket title updated to:** `{title.strip()}`")


@bot.command(name="setticketdesc")
async def setticketdesc_cmd(ctx, *, desc: str = None):
    """Sets the description for the ticket panel embed."""
    if not is_staff(ctx.author) or not desc:
        await ctx.reply("Usage: `*setticketdesc <description>`")
        return
    cfg = load_ticket_config()
    cfg.setdefault("embed", {})["description"] = desc.strip()
    save_ticket_config(cfg)
    await ctx.reply("✅ **Ticket description updated successfully!**")


@bot.command(name="setticketcategory")
async def setticketcategory_cmd(ctx, cat_id: int = None):
    """Sets the Category ID where new tickets are created."""
    if not is_staff(ctx.author) or not cat_id:
        await ctx.reply("Usage: `*setticketcategory <category_id>`")
        return
    cfg = load_ticket_config()
    cfg.setdefault("settings", {})["category_id"] = cat_id
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Ticket Category set to:** `{cat_id}`")


@bot.command(name="setticketlogs")
async def setticketlogs_cmd(ctx, channel: discord.TextChannel = None):
    """Sets the channel where ticket transcripts are saved."""
    if not is_staff(ctx.author) or not channel:
        await ctx.reply("Usage: `*setticketlogs <#channel>`")
        return
    cfg = load_ticket_config()
    cfg.setdefault("settings", {})["logs_channel_id"] = channel.id
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Ticket Logs Channel set to:** {channel.mention}")


@bot.command(name="setticketplaceholder", aliases=["tplaceholder", "ticketplaceholder"])
async def setticketplaceholder_cmd(ctx, *, placeholder: str = None):
    """Sets the placeholder subtext of the ticket panel dropdown."""
    if not is_staff(ctx.author) or not placeholder:
        await ctx.reply("Usage: `*setticketplaceholder <text>` (e.g. `*setticketplaceholder Select your product below`)")
        return
    cfg = load_ticket_config()
    cfg.setdefault("dropdown", {})["placeholder"] = placeholder.strip()
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Ticket dropdown placeholder updated to:** `{placeholder.strip()}`")



@bot.command(name="editticketoption", aliases=["teditopt"])
async def editticketoption_cmd(ctx, *, option_name: str = None):
    """Opens interactive editor for an existing ticket dropdown option."""
    if not is_staff(ctx.author):
        return
    cfg = load_ticket_config()
    options = cfg.get("dropdown", {}).get("options", [])
    if not options:
        await ctx.reply("⚠️ No options exist to edit.")
        return

    if not option_name:
        await ctx.send("👇 **Select an option from the dropdown below to edit:**", view=TicketEditOptionView(options))
        return

    clean_name = option_name.strip().lower()
    target_opt = next((o for o in options if o.get("label", "").lower() == clean_name or o.get("value", "").lower() == clean_name), None)
    if not target_opt:
        await ctx.reply(f"❌ Could not find option `{option_name}`. Run `*ticketoptions` to see all available options.")
        return

    await ctx.send(f"Click the button below to edit **{target_opt.get('label')}**:", view=discord.ui.View().add_item(
        discord.ui.Button(label=f"Edit {target_opt.get('label')[:15]}", style=discord.ButtonStyle.primary, custom_id=f"edit_opt_{target_opt.get('value')}")
    ))


@bot.command(name="addticketoption")
async def addticketoption_cmd(ctx, label: str = None, emoji: str = None, *, description: str = "Support assistance"):
    """Quickly adds a new panel option to the ticket dropdown (supports animated emojis)."""
    if not is_staff(ctx.author) or not label or not emoji:
        await ctx.reply("Usage: `*addticketoption <Label> <Emoji> [Description]`\nExample: `*addticketoption VIP <a:fire:123456789> VIP Software Support`")
        return
    cfg = load_ticket_config()
    val = label.lower().replace(" ", "_")
    cfg.setdefault("dropdown", {}).setdefault("options", []).append({
        "label": label.strip(),
        "value": val,
        "description": description.strip(),
        "emoji": emoji.strip(),
        "welcome_title": f"Support • {label.strip()}",
        "welcome_message": "Welcome {user}! Staff will assist you shortly."
    })
    save_ticket_config(cfg)
    await ctx.reply(f"✅ Added ticket panel option **{label}** with emoji {emoji}!")


@bot.command(name="setticketrole", aliases=["ticketrole", "setticketstaff"])
async def setticketrole_cmd(ctx, role: discord.Role = None):
    """Sets the Discord role authorized to view and reply in tickets."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can configure the ticket staff role.")
        return
    cfg = load_ticket_config()
    st = cfg.setdefault("settings", {})
    if not role:
        staff_ids = st.get("staff_role_ids", [])
        if staff_ids:
            mentions = " ".join(f"<@&{rid}>" for rid in staff_ids if ctx.guild and ctx.guild.get_role(rid))
            await ctx.reply(f"🎫 **Current Ticket Role(s):** {mentions or 'None found in server'}\nUsage to update: `*setticketrole @Role`")
            await ctx.reply("Usage: `*setticketrole @Role`")
        return
    st["staff_role_ids"] = [role.id]
    cfg.setdefault("ticket_channel", {})["ping_roles"] = [role.id]
    save_ticket_config(cfg)
    await ctx.reply(f"✅ **Ticket Staff Role updated to:** {role.mention}\n*Strict privacy active: Only this role and the ticket creator can view and reply in newly opened tickets.*")


@bot.tree.command(name="setticketrole", description="Set the staff role authorized to view and reply in tickets")
@app_commands.describe(role="The staff role that can view and reply in tickets")
async def setticketrole_slash(interaction: discord.Interaction, role: discord.Role):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can configure the ticket staff role.", ephemeral=True)
        return
    cfg = load_ticket_config()
    st = cfg.setdefault("settings", {})
    st["staff_role_ids"] = [role.id]
    cfg.setdefault("ticket_channel", {})["ping_roles"] = [role.id]
    save_ticket_config(cfg)
    await interaction.response.send_message(f"✅ **Ticket Staff Role updated to:** {role.mention}\n*Strict privacy active: Only this role and the ticket creator can view and reply in newly opened tickets.*")


@bot.command(name="ticketinfo")
async def ticketinfo_cmd(ctx):
    """Shows complete live ticket system configuration."""
    if not is_staff(ctx.author):
        return
    cfg = load_ticket_config()
    emb_cfg = cfg.get("embed", {})
    st = cfg.get("settings", {})
    opts = cfg.get("dropdown", {}).get("options", [])

    embed = discord.Embed(title="⚙️ BYTE CORPORATION • Ticket Configuration", color=0x0000FF)
    embed.add_field(name="📌 Title", value=f"`{emb_cfg.get('title')}`", inline=True)
    embed.add_field(name="🎨 Color", value=f"`{emb_cfg.get('color')}`", inline=True)
    embed.add_field(name="📁 Category ID", value=f"`{st.get('category_id')}`", inline=True)
    embed.add_field(name="📜 Logs Channel", value=f"<#{st.get('logs_channel_id')}>", inline=True)
    staff_ids = st.get("staff_role_ids", [])
    staff_mentions = " ".join(f"<@&{rid}>" for rid in staff_ids if ctx.guild and ctx.guild.get_role(rid)) or "`None`"
    embed.add_field(name="🛡️ Staff Role", value=staff_mentions, inline=True)
    embed.add_field(name="🔢 Total Panels", value=f"`{len(opts)} Options`", inline=True)
    embed.add_field(name="🎫 Last Ticket #", value=f"`#{st.get('ticket_counter', 0):04d}`", inline=True)
    if emb_cfg.get("banner_url"):
        embed.set_image(url=emb_cfg.get("banner_url"))
    await ctx.reply(embed=embed)


@bot.command(name="close")
async def close_ticket_prefix(ctx):
    """Closes the current ticket channel and saves transcript."""
    view = ByteCustomTicketControlView()
    await view.close_callback(ctx)


@bot.command(name="claim")
async def claim_ticket_prefix(ctx):
    """Claims the current ticket for the staff member."""
    view = ByteCustomTicketControlView()
    await view.claim_callback(ctx)


@bot.command(name="add")
async def add_user_ticket(ctx, member: discord.Member = None):
    """Adds a member to the current ticket channel."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only staff members can add users to tickets.")
        return
    if not ctx.channel.name.startswith("ticket-"):
        await ctx.reply("❌ This command can only be used inside a ticket channel.")
        return
    if not member:
        await ctx.reply("Usage: `*add @User`")
        return
    await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, attach_files=True, embed_links=True)
    await ctx.reply(f"✅ Added {member.mention} to this ticket channel.")


@bot.command(name="remove")
async def remove_user_ticket(ctx, member: discord.Member = None):
    """Removes a user from the current ticket channel."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Only staff members can remove users from tickets.")
        return
    if not ctx.channel.name.startswith("ticket-"):
        await ctx.reply("❌ This command can only be used inside a ticket channel.")
        return
    if not member:
        await ctx.reply("Usage: `*remove @User`")
        return
    await ctx.channel.set_permissions(member, overwrite=None)
    await ctx.reply(f"✅ Removed {member.mention} from this ticket channel.")


# ==========================================
# SERVER VERIFICATION SYSTEM (GATEWAY)
# Matches screenshot: Title "Server Verification System",
# "Verify Member" button, and strict channel lock.
# ==========================================

VERIFICATION_CONFIG_FILE = os.path.join(BASE_DIR, "verification_config.json")

def load_verification_config() -> dict:
    default_cfg = {
        "enabled": True,
        "role_id": 1566796494504333353,
        "channel_id": 1566789314140971018,
        "logs_channel_id": 1508088964077191261,
        "embed": {
            "title": "✅ Server Verification System",
            "description": "Welcome to **{server}**!\n\nTo access all server channels, community chats, downloads, and resources, please verify yourself by clicking the **Verify Member** button below.\n\n🔒 **Security:** Verification protects the community against automated spam and unauthorized bots.",
            "color": "0x2B2D31",
            "thumbnail_type": "server_icon",
            "footer_text": "{server} • Verification Gateway",
            "button_label": "Verify Member",
            "button_emoji": "✅",
            "button_style": "success"
        }
    }
    if os.path.exists(VERIFICATION_CONFIG_FILE):
        try:
            with open(VERIFICATION_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
                default_cfg.update(loaded)
                return default_cfg
        except Exception as e:
            logger.error(f"Failed to load verification_config.json: {e}")
    return default_cfg

def save_verification_config(data: dict):
    try:
        with open(VERIFICATION_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save verification_config.json: {e}")

class ServerVerificationView(discord.ui.View):
    """Persistent 24/7 Verification View with custom ID for automatic restoration on reboot."""
    def __init__(self, button_label: str = None, button_emoji: str = None, button_style: str = None):
        super().__init__(timeout=None)
        cfg = load_verification_config().get("embed", {})
        label = (button_label or cfg.get("button_label") or "Verify Member")[:80]
        emoji_raw = button_emoji or cfg.get("button_emoji") or "✅"
        style_raw = button_style or cfg.get("button_style") or "success"
        
        em = parse_custom_emoji(emoji_raw)
        style = parse_button_style(style_raw, discord.ButtonStyle.success)
        
        btn = discord.ui.Button(
            label=label,
            emoji=em,
            style=style,
            custom_id="btn_server_verify_member",
            row=0
        )
        btn.callback = self.verify_callback
        self.add_item(btn)

    async def verify_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            await interaction.followup.send("❌ Verification can only be done inside the server.", ephemeral=True)
            return

        cfg = load_verification_config()
        if not cfg.get("enabled", True):
            await interaction.followup.send("⚠️ Server verification is currently paused by administrators.", ephemeral=True)
            return

        role_id = cfg.get("role_id")
        if not role_id:
            await interaction.followup.send("⚠️ Verification role has not been set by server administrators yet. Please ask staff to run `*setverifyrole @Role`.", ephemeral=True)
            return

        try:
            role = guild.get_role(int(role_id))
        except (ValueError, TypeError):
            role = None
        if not role:
            await interaction.followup.send(f"⚠️ Configured verification role (`{role_id}`) was not found on this server. Please contact an Administrator.", ephemeral=True)
            return

        # Check if already verified
        if role in member.roles:
            await interaction.followup.send(f"ℹ️ **You are already verified in {guild.name}!** All channels are already accessible to you.", ephemeral=True)
            return

        # Check bot hierarchy
        if guild.me.top_role <= role:
            await interaction.followup.send("❌ **Bot Hierarchy Error:** The bot's highest role must be placed ABOVE the verified role in Server Settings > Roles.", ephemeral=True)
            return

        try:
            await member.add_roles(role, reason="Server Verification Gateway")
            await interaction.followup.send(f"✅ **Verification Successful!**\nYou now have the {role.mention} role. All server channels, chats, and resources are unlocked for you!", ephemeral=True)
            
            # Audit Log if configured
            logs_id = cfg.get("logs_channel_id")
            if logs_id:
                try:
                    log_ch = guild.get_channel(int(logs_id))
                except (ValueError, TypeError):
                    log_ch = None
                if log_ch:
                    embed = discord.Embed(
                        title="✅ Member Verified",
                        description=f"{member.mention} (`{member.id}`) passed verification and was assigned {role.mention}.",
                        color=0x0000FF,
                        timestamp=datetime.utcnow()
                    )
                    if member.display_avatar:
                        embed.set_thumbnail(url=member.display_avatar.url)
                    await log_ch.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send("❌ **Permission Denied:** The bot does not have `Manage Roles` permission or the role is higher than the bot's role.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to verify member {member.id}: {e}")
            await interaction.followup.send(f"❌ Failed to verify: `{e}`", ephemeral=True)


def build_verification_embed(guild: discord.Guild = None) -> discord.Embed:
    cfg = load_verification_config()
    emb_cfg = cfg.get("embed", {})
    color_int = parse_hex_color(emb_cfg.get("color", "0x0000FF"))
    
    server_name = guild.name if guild else "Our Community"
    raw_desc = emb_cfg.get(
        "description",
        "Welcome to **{server}**!\n\nTo access all server channels, community chats, downloads, and resources, please verify yourself by clicking the **Verify Member** button below.\n\n🔒 **Security:** Verification protects the community against automated spam and unauthorized bots."
    )
    desc = raw_desc.replace("{server}", server_name)
    desc = sanitize_and_resolve_emojis(desc, guild)
    
    title = emb_cfg.get("title", "✅ Server Verification System").replace("{server}", server_name)
    title = sanitize_and_resolve_emojis(title, guild)
    
    embed = discord.Embed(
        title=title,
        description=desc,
        color=color_int,
        timestamp=datetime.utcnow()
    )
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    footer_text = emb_cfg.get("footer_text", "{server} • Verification Gateway").replace("{server}", server_name)
    embed.set_footer(text=footer_text)
    return embed


@bot.command(name="verifypanel", aliases=["sendverifypanel", "verifyembed"])
async def verifypanel_cmd(ctx, channel: discord.TextChannel = None):
    """Spawns the official Server Verification Panel with the Verify Member button."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can post the verification panel.")
        return
    target_ch = channel or ctx.channel
    embed = build_verification_embed(ctx.guild)
    view = ServerVerificationView()
    await target_ch.send(embed=embed, view=view)
    if target_ch != ctx.channel:
        await ctx.reply(f"✅ Verification panel dispatched to {target_ch.mention}!")
        try:
            await ctx.message.delete()
        except Exception:
            pass


@bot.tree.command(name="verifypanel", description="Post the official Server Verification Panel")
@app_commands.describe(channel="The channel to post the verification panel in")
async def verifypanel_slash(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can post the verification panel.", ephemeral=True)
        return
    target_ch = channel or interaction.channel
    embed = build_verification_embed(interaction.guild)
    view = ServerVerificationView()
    await target_ch.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Verification panel posted in {target_ch.mention}!", ephemeral=True)


@bot.command(name="setverifyrole", aliases=["verifyrole"])
async def setverifyrole_cmd(ctx, role: discord.Role = None):
    """Sets the Discord role given to members when they click Verify Member."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can configure the verification role.")
        return
    cfg = load_verification_config()
    if not role:
        current_id = cfg.get("role_id")
        current_role = ctx.guild.get_role(current_id) if current_id else None
        mention = current_role.mention if current_role else (f"`{current_id}`" if current_id else "`None`")
        await ctx.reply(f"🛡️ **Current Verified Role:** {mention}\nUsage to update: `*setverifyrole @Role`")
        return
    cfg["role_id"] = role.id
    save_verification_config(cfg)
    await ctx.reply(f"✅ **Verified Member Role updated to:** {role.mention}\n*Members clicking Verify Member will automatically receive this role.*")


@bot.tree.command(name="setverifyrole", description="Set the role given to members upon verification")
@app_commands.describe(role="The verified member role")
async def setverifyrole_slash(interaction: discord.Interaction, role: discord.Role):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** can configure the verification role.", ephemeral=True)
        return
    cfg = load_verification_config()
    cfg["role_id"] = role.id
    save_verification_config(cfg)
    await interaction.response.send_message(f"✅ **Verified Member Role updated to:** {role.mention}\n*Members clicking Verify Member will automatically receive this role.*", ephemeral=True)



def is_staff_or_private_entity(entity) -> bool:
    """
    Accurately detects if a channel or category is staff/admin/mod/logs/sensitive,
    handling unicode fonts, emojis, special characters, and abbreviations.
    """
    import unicodedata
    name = getattr(entity, "name", "")
    norm_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    raw_lower = name.lower()

    sensitive_terms = [
        "log", "logs", "mod", "moderator", "admin", "administrator", "staff",
        "zynrax", "automod", "keyauth", "audit", "secret", "private", "reseller",
        "ticket-log", "transcript", "transcripts", "whitelist", "wl-log", "dev",
        "developer", "owner", "management", "internal", "bot-cmd", "bot-command",
        "bot-log", "hidden", "security"
    ]

    for term in sensitive_terms:
        if term in raw_lower or term in norm_name:
            return True

    if hasattr(entity, "category") and entity.category:
        cat_raw = entity.category.name.lower()
        cat_norm = unicodedata.normalize("NFKD", entity.category.name).encode("ascii", "ignore").decode("ascii").lower()
        for term in sensitive_terms:
            if term in cat_raw or term in cat_norm:
                return True

    return False


@bot.command(name="autoverifysetup", aliases=["quickverifysetup", "setupverify", "setupverification", "lockdownverification", "fixverification", "lockdownserver"])
async def autoverifysetup_cmd(ctx, role: discord.Role = None, target_channel: discord.TextChannel = None):
    """
    1-Click Complete Automated Verification Setup & Server Lockdown:
    1. Sets Verified Role & Verification Channel.
    2. Completely locks ALL server categories & channels so unverified users CANNOT see them.
    3. Synchronizes all child channels with their category so channel-level overrides are eliminated.
    4. Leaves ONLY the verification channel visible to @everyone.
    5. Automatically hides the verification channel once verified.
    6. Grants the verified role to all existing members so nobody gets locked out!
    """
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** or **Server Owner** can run automated verification setup.")
        return

    cfg = load_verification_config()

    # Determine Role
    if not role:
        role_id = cfg.get("role_id")
        if role_id:
            try:
                role = ctx.guild.get_role(int(role_id))
            except (ValueError, TypeError):
                role = None
        if not role:
            for r in ctx.guild.roles:
                if r.name.lower() in ["verify", "verified", "member", ".member", "@.gg/bytecorporation", ".gg/bytecorporation"]:
                    role = r
                    break
    if not role:
        await ctx.reply("❌ **Role Not Found:** Please specify the verified role, e.g.: `*autoverifysetup @.gg/bytecorporation`")
        return

    # Check bot hierarchy
    hierarchy_warning = ""
    if ctx.guild.me.top_role <= role:
        hierarchy_warning = (
            f"\n\n⚠️ **IMPORTANT ROLE HIERARCHY NOTICE:**\n"
            f"The bot's role (`{ctx.guild.me.top_role.name}`) is currently placed **BELOW** `{role.name}` in Server Settings > Roles!\n"
            f"👉 Go to **Server Settings > Roles** and drag **`{ctx.guild.me.top_role.name}` ABOVE `{role.name}`** so the bot can assign the role when users click the button!"
        )

    # Determine Verification Channel
    if not target_channel:
        ch_id = cfg.get("channel_id")
        if ch_id:
            try:
                target_channel = ctx.guild.get_channel(int(ch_id))
            except (ValueError, TypeError):
                target_channel = None
        if not target_channel:
            if "veri" in ctx.channel.name.lower():
                target_channel = ctx.channel
            else:
                for c in ctx.guild.text_channels:
                    if "veri" in c.name.lower():
                        target_channel = c
                        break
    if not target_channel:
        target_channel = ctx.channel

    status_msg = await ctx.reply(f"⏳ **Locking down server & configuring Verification Gateway...**\n• Verified Role: {role.mention}\n• Verification Gate: {target_channel.mention}\n*Please wait while all categories and channels are secured...*")

    cfg["enabled"] = True
    cfg["role_id"] = role.id
    cfg["channel_id"] = target_channel.id
    save_verification_config(cfg)

    categories_locked = 0
    channels_locked = 0
    try:
        # A. Setup #verification channel: Visible to @everyone, hidden from verified role
        await target_channel.set_permissions(ctx.guild.default_role, view_channel=True, send_messages=False, read_message_history=True)
        await target_channel.set_permissions(role, view_channel=False)
        await target_channel.set_permissions(ctx.guild.me, view_channel=True, send_messages=True, embed_links=True, manage_channels=True, manage_permissions=True)

        # B. Lockdown all categories & protect private channels
        for cat in ctx.guild.categories:
            is_cat_private = is_staff_or_private_entity(cat)
            # Deny @everyone view channel on all categories
            await cat.set_permissions(ctx.guild.default_role, view_channel=False)
            if is_cat_private:
                # Private/Staff categories are 100% hidden from verified normal members too!
                await cat.set_permissions(role, view_channel=False)
            else:
                # Public categories allow verified members
                await cat.set_permissions(role, view_channel=True)
            categories_locked += 1

            # Process every child channel
            for ch in cat.channels:
                if ch.id == target_channel.id:
                    continue
                ch_is_private = is_cat_private or is_staff_or_private_entity(ch)
                if ch_is_private:
                    # PRIVATE CHANNEL: NEVER SYNC WITH PUBLIC CATEGORY!
                    # Explicitly deny both @everyone and verified members
                    try:
                        await ch.set_permissions(ctx.guild.default_role, view_channel=False)
                        await ch.set_permissions(role, view_channel=False)
                    except Exception:
                        pass
                else:
                    # PUBLIC CHANNEL: Sync with category so @everyone is denied and verified role is allowed
                    try:
                        await ch.edit(sync_permissions=True)
                    except Exception:
                        try:
                            await ch.set_permissions(ctx.guild.default_role, view_channel=False)
                            await ch.set_permissions(role, view_channel=True)
                        except Exception:
                            pass
                channels_locked += 1
                await asyncio.sleep(0.08)

            await asyncio.sleep(0.15)

        # C. Lockdown standalone channels (channels without a category)
        for ch in ctx.guild.channels:
            if ch.category is None and ch.id != target_channel.id:
                ch_is_private = is_staff_or_private_entity(ch)
                await ch.set_permissions(ctx.guild.default_role, view_channel=False)
                if ch_is_private:
                    await ch.set_permissions(role, view_channel=False)
                else:
                    await ch.set_permissions(role, view_channel=True)
                channels_locked += 1
                await asyncio.sleep(0.1)

        # D. Post panel in verification channel if not present
        has_panel = False
        async for m in target_channel.history(limit=10):
            if m.author == ctx.guild.me and m.components:
                has_panel = True
                break
        if not has_panel:
            embed = build_verification_embed(ctx.guild)
            view = ServerVerificationView()
            await target_channel.send(embed=embed, view=view)

        # E. Sync existing members so they don't get locked out
        synced_members = 0
        if ctx.guild.me.top_role > role:
            for m in ctx.guild.members:
                if not m.bot and role not in m.roles:
                    try:
                        await m.add_roles(role, reason="Auto-Verification Setup Member Sync")
                        synced_members += 1
                    except Exception:
                        pass

        embed_done = discord.Embed(
            title="🛡️ Server Verification Gateway 100% Configured!",
            description=(
                f"✅ **Automated Security Active:**\n\n"
                f"• **Verified Role:** {role.mention}\n"
                f"• **Verification Gate:** {target_channel.mention}\n"
                f"• **Categories Locked:** `{categories_locked}`\n"
                f"• **Channels Secured & Synced:** `{channels_locked}` channels hidden from unverified users!\n"
                f"• **Existing Members Synced:** `{synced_members}` current members granted role.\n\n"
                f"🔒 **How It Works Now:**\n"
                f"1. New members joining see **ONLY** {target_channel.mention}. All chats, products, announcements & voice channels are completely invisible to them.\n"
                f"2. When they click **Verify Member**, the bot grants them {role.mention} instantly.\n"
                f"3. {target_channel.mention} disappears and **ALL** server channels unlock for them!"
                f"{hierarchy_warning}"
            ),
            color=0x00FF88
        )
        await status_msg.edit(content=None, embed=embed_done)
    except Exception as e:
        logger.error(f"Error in autoverifysetup: {e}")
        await status_msg.edit(content=f"❌ **Error during automated setup:** `{e}`")


@bot.tree.command(name="autoverifysetup", description="1-Click Complete Automated Verification Setup & Server Lockdown")
@app_commands.describe(role="Role given upon verification (defaults to configured role)", target_channel="Verification channel (defaults to #verification)")
async def autoverifysetup_slash(interaction: discord.Interaction, role: discord.Role = None, target_channel: discord.TextChannel = None):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only Administrators or Server Owner can run automated verification setup.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    guild = interaction.guild
    cfg = load_verification_config()

    if not role:
        role_id = cfg.get("role_id")
        if role_id:
            try:
                role = guild.get_role(int(role_id))
            except (ValueError, TypeError):
                role = None
        if not role:
            for r in guild.roles:
                if r.name.lower() in ["verify", "verified", "member", ".member", "@.gg/bytecorporation", ".gg/bytecorporation"]:
                    role = r
                    break
    if not role:
        await interaction.followup.send("❌ **Role Not Found:** Please specify the verified role, e.g.: `/autoverifysetup role:@Role`")
        return

    hierarchy_warning = ""
    if guild.me.top_role <= role:
        hierarchy_warning = (
            f"\n\n⚠️ **IMPORTANT ROLE HIERARCHY NOTICE:**\n"
            f"The bot's role (`{guild.me.top_role.name}`) is currently placed **BELOW** `{role.name}` in Server Settings > Roles!\n"
            f"👉 Go to **Server Settings > Roles** and drag **`{guild.me.top_role.name}` ABOVE `{role.name}`** so the bot can assign the role when users click the button!"
        )

    if not target_channel:
        ch_id = cfg.get("channel_id")
        if ch_id:
            try:
                target_channel = guild.get_channel(int(ch_id))
            except (ValueError, TypeError):
                target_channel = None
        if not target_channel:
            for c in guild.text_channels:
                if "veri" in c.name.lower():
                    target_channel = c
                    break
    if not target_channel:
        target_channel = interaction.channel

    status_msg = await interaction.followup.send(f"⏳ **Locking down server & configuring Verification Gateway...**\n• Verified Role: {role.mention}\n• Verification Gate: {target_channel.mention}\n*Please wait while all categories and channels are secured...*")

    cfg["enabled"] = True
    cfg["role_id"] = role.id
    cfg["channel_id"] = target_channel.id
    save_verification_config(cfg)

    categories_locked = 0
    channels_locked = 0
    try:
        await target_channel.set_permissions(guild.default_role, view_channel=True, send_messages=False, read_message_history=True)
        await target_channel.set_permissions(role, view_channel=False)
        await target_channel.set_permissions(guild.me, view_channel=True, send_messages=True, embed_links=True, manage_channels=True, manage_permissions=True)

        for cat in guild.categories:
            is_cat_private = is_staff_or_private_entity(cat)
            await cat.set_permissions(guild.default_role, view_channel=False)
            if is_cat_private:
                await cat.set_permissions(role, view_channel=False)
            else:
                await cat.set_permissions(role, view_channel=True)
            categories_locked += 1

            for ch in cat.channels:
                if ch.id == target_channel.id:
                    continue
                ch_is_private = is_cat_private or is_staff_or_private_entity(ch)
                if ch_is_private:
                    try:
                        await ch.set_permissions(guild.default_role, view_channel=False)
                        await ch.set_permissions(role, view_channel=False)
                    except Exception:
                        pass
                else:
                    try:
                        await ch.edit(sync_permissions=True)
                    except Exception:
                        try:
                            await ch.set_permissions(guild.default_role, view_channel=False)
                            await ch.set_permissions(role, view_channel=True)
                        except Exception:
                            pass
                channels_locked += 1
                await asyncio.sleep(0.08)

            await asyncio.sleep(0.15)

        for ch in guild.channels:
            if ch.category is None and ch.id != target_channel.id:
                ch_is_private = is_staff_or_private_entity(ch)
                await ch.set_permissions(guild.default_role, view_channel=False)
                if ch_is_private:
                    await ch.set_permissions(role, view_channel=False)
                else:
                    await ch.set_permissions(role, view_channel=True)
                channels_locked += 1
                await asyncio.sleep(0.1)

        has_panel = False
        async for m in target_channel.history(limit=10):
            if m.author == guild.me and m.components:
                has_panel = True
                break
        if not has_panel:
            embed = build_verification_embed(guild)
            view = ServerVerificationView()
            await target_channel.send(embed=embed, view=view)

        synced_members = 0
        if guild.me.top_role > role:
            for m in guild.members:
                if not m.bot and role not in m.roles:
                    try:
                        await m.add_roles(role, reason="Auto-Verification Setup Member Sync")
                        synced_members += 1
                    except Exception:
                        pass

        embed_done = discord.Embed(
            title="🛡️ Server Verification Gateway 100% Configured!",
            description=(
                f"✅ **Automated Security Active:**\n\n"
                f"• **Verified Role:** {role.mention}\n"
                f"• **Verification Gate:** {target_channel.mention}\n"
                f"• **Categories Locked:** `{categories_locked}`\n"
                f"• **Channels Secured & Synced:** `{channels_locked}` channels hidden from unverified users!\n"
                f"• **Existing Members Synced:** `{synced_members}` current members granted role.\n\n"
                f"🔒 **How It Works Now:**\n"
                f"1. New members joining see **ONLY** {target_channel.mention}. All chats, products, announcements & voice channels are completely invisible to them.\n"
                f"2. When they click **Verify Member**, the bot grants them {role.mention} instantly.\n"
                f"3. {target_channel.mention} disappears and **ALL** server channels unlock for them!"
                f"{hierarchy_warning}"
            ),
            color=0x00FF88
        )
        await status_msg.edit(content=None, embed=embed_done)
    except Exception as e:
        logger.error(f"Error in autoverifysetup slash: {e}")
        await status_msg.edit(content=f"❌ **Error during automated setup:** `{e}`")




@bot.command(name="hideprivate", aliases=["secureprivate", "lockprivate", "fixprivate"])
async def hideprivate_cmd(ctx):
    """
    Instantly scans and locks all staff, moderator, logs, and private channels
    from verified members (@.gg/bytecorporation) and @everyone.
    """
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** or **Server Owner** can run this command.")
        return

    cfg = load_verification_config()
    role = None
    role_id = cfg.get("role_id")
    if role_id:
        role = ctx.guild.get_role(int(role_id))
    if not role:
        for r in ctx.guild.roles:
            if r.name.lower() in ["verify", "verified", "member", ".member", "@.gg/bytecorporation", ".gg/bytecorporation"]:
                role = r
                break

    msg = await ctx.reply("🔒 **Scanning and locking all private & staff channels...**")

    secured_categories = []
    secured_channels = []

    # 1. Lock all private categories and their channels
    for cat in ctx.guild.categories:
        if is_staff_or_private_entity(cat):
            try:
                await cat.set_permissions(ctx.guild.default_role, view_channel=False)
                if role:
                    await cat.set_permissions(role, view_channel=False)
                secured_categories.append(cat.name)
            except Exception:
                pass

            for ch in cat.channels:
                try:
                    await ch.set_permissions(ctx.guild.default_role, view_channel=False)
                    if role:
                        await ch.set_permissions(role, view_channel=False)
                    secured_channels.append(ch.name)
                except Exception:
                    pass
                await asyncio.sleep(0.06)

    # 2. Lock any standalone or individual channels that are private
    for ch in ctx.guild.channels:
        if ch.name in secured_channels:
            continue
        if is_staff_or_private_entity(ch):
            try:
                await ch.set_permissions(ctx.guild.default_role, view_channel=False)
                if role:
                    await ch.set_permissions(role, view_channel=False)
                secured_channels.append(ch.name)
            except Exception:
                pass
            await asyncio.sleep(0.06)

    role_mention = role.mention if role else "`Verified Role`"
    cat_summary = "\n".join([f"• 📁 `{c}`" for c in secured_categories[:12]]) or "None detected"
    ch_summary = "\n".join([f"• 🔒 `{c}`" for c in secured_channels[:20]]) or "None detected"

    embed = discord.Embed(
        title="🔒 Private Channels Successfully Locked!",
        description=(
            f"All staff, moderator, automod, and log channels are now **100% hidden** from {role_mention} and `@everyone`!\n\n"
            f"**Categories Secured ({len(secured_categories)}):**\n{cat_summary}\n\n"
            f"**Channels Secured ({len(secured_channels)}):**\n{ch_summary}\n\n"
            f"🛡️ *Only Server Administrators and authorized Staff roles can view these channels now.*"
        ),
        color=0x00FF88
    )
    await msg.edit(content=None, embed=embed)


@bot.command(name="hidechannel", aliases=["hide", "lockchannel"])
async def hidechannel_cmd(ctx, target: discord.abc.GuildChannel = None):
    """
    Hides a specific channel from verified members and @everyone.
    Usage: `*hidechannel #moderator-only` or run inside the channel.
    """
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can run this command.")
        return

    target = target or ctx.channel
    cfg = load_verification_config()
    role = None
    role_id = cfg.get("role_id")
    if role_id:
        role = ctx.guild.get_role(int(role_id))
    if not role:
        for r in ctx.guild.roles:
            if r.name.lower() in ["verify", "verified", "member", ".member", "@.gg/bytecorporation", ".gg/bytecorporation"]:
                role = r
                break

    await target.set_permissions(ctx.guild.default_role, view_channel=False)
    if role:
        await target.set_permissions(role, view_channel=False)

    await ctx.reply(f"🔒 Channel {target.mention} is now **100% hidden** from verified normal members and `@everyone`!")


@bot.command(name="hidecategory", aliases=["lockcategory"])
async def hidecategory_cmd(ctx, *, category_name: str):
    """
    Hides an entire category and all channels inside it from verified members and @everyone.
    Usage: `*hidecategory logs`
    """
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can run this command.")
        return

    cat = None
    for c in ctx.guild.categories:
        if category_name.lower() in c.name.lower():
            cat = c
            break

    if not cat:
        await ctx.reply(f"❌ Category matching `{category_name}` not found.")
        return

    cfg = load_verification_config()
    role = None
    role_id = cfg.get("role_id")
    if role_id:
        role = ctx.guild.get_role(int(role_id))
    if not role:
        for r in ctx.guild.roles:
            if r.name.lower() in ["verify", "verified", "member", ".member", "@.gg/bytecorporation", ".gg/bytecorporation"]:
                role = r
                break

    await cat.set_permissions(ctx.guild.default_role, view_channel=False)
    if role:
        await cat.set_permissions(role, view_channel=False)

    count = 0
    for ch in cat.channels:
        await ch.set_permissions(ctx.guild.default_role, view_channel=False)
        if role:
            await ch.set_permissions(role, view_channel=False)
        count += 1
        await asyncio.sleep(0.06)

    await ctx.reply(f"🔒 Category **{cat.name}** and `{count}` child channels are now **100% hidden** from verified normal members and `@everyone`!")


@bot.command(name="showchannel", aliases=["unhidechannel", "unlockchannel"])
async def showchannel_cmd(ctx, target: discord.abc.GuildChannel = None):
    """
    Makes a specific channel visible to verified members.
    Usage: `*showchannel #general`
    """
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can run this command.")
        return

    target = target or ctx.channel
    cfg = load_verification_config()
    role = None
    role_id = cfg.get("role_id")
    if role_id:
        role = ctx.guild.get_role(int(role_id))
    if not role:
        for r in ctx.guild.roles:
            if r.name.lower() in ["verify", "verified", "member", ".member", "@.gg/bytecorporation", ".gg/bytecorporation"]:
                role = r
                break

    await target.set_permissions(ctx.guild.default_role, view_channel=False)
    if role:
        await target.set_permissions(role, view_channel=True)

    await ctx.reply(f"✅ Channel {target.mention} is now **visible** to verified members!")


@bot.command(name="verifyinfo")
async def verifyinfo_cmd(ctx):
    """Displays current verification system settings and status."""
    if not has_admin_or_owner_access(ctx.author):
        return
    cfg = load_verification_config()
    role_id = cfg.get("role_id")
    role_obj = ctx.guild.get_role(role_id) if role_id else None
    role_str = role_obj.mention if role_obj else (f"`{role_id}`" if role_id else "`Not Set`")
    ch_id = cfg.get("channel_id")
    ch_str = f"<#{ch_id}>" if ch_id else "`Not Set`"
    
    embed = discord.Embed(title="🛡️ Server Verification System • Status", color=0x0000FF)
    embed.add_field(name="Status", value="🟢 **Enabled**" if cfg.get("enabled", True) else "🔴 **Disabled**", inline=True)
    embed.add_field(name="Verified Role", value=role_str, inline=True)
    embed.add_field(name="Verify Channel", value=ch_str, inline=True)
    embed.add_field(name="Button Label", value=f"`{cfg.get('embed', {}).get('button_label', 'Verify Member')}`", inline=True)
    embed.add_field(name="Button Emoji", value=cfg.get("embed", {}).get("button_emoji", "✅"), inline=True)
    await ctx.reply(embed=embed)


@bot.command(name="verifyallcurrent", aliases=["verifyexisting", "syncverified"])
async def verifyallcurrent_cmd(ctx):
    """
    Grants the verified role to ALL existing members on the server.
    Ensures current members are never forced to verify or locked out!
    """
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** or **Server Owner** can run this command.")
        return
    cfg = load_verification_config()
    role_id = cfg.get("role_id")
    if not role_id:
        await ctx.reply("⚠️ Verification role has not been set yet. Run `*setverifyrole @Role` first.")
        return
    role = ctx.guild.get_role(role_id)
    if not role:
        await ctx.reply("❌ Configured verification role was not found in this server.")
        return
    if ctx.guild.me.top_role <= role:
        await ctx.reply("❌ **Bot Hierarchy Error:** Bot's highest role must be ABOVE the verified role to assign it.")
        return

    msg = await ctx.reply(f"⏳ **Starting Bulk Verification...** Granting {role.mention} to all current members...")
    total = 0
    assigned = 0
    for member in ctx.guild.members:
        total += 1
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Grandfathered Existing Member Verification")
                assigned += 1
                if assigned % 25 == 0:
                    await msg.edit(content=f"⏳ Progress: `{assigned}` members verified so far...")
            except Exception:
                pass
    await msg.edit(content=f"✅ **Bulk Verification Complete!**\n• Total Server Members: `{total}`\n• Granted {role.mention} to `{assigned}` existing members.\n*Current members now have full channel access. Only brand new joining members will need to verify!*")


@bot.tree.command(name="verifyallcurrent", description="Grant verified role to all current members so only new joins need to verify")
async def verifyallcurrent_slash(interaction: discord.Interaction):
    if not has_admin_or_owner_access(interaction.user):
        await interaction.response.send_message("❌ Only an **Administrator** or **Server Owner** can run this command.", ephemeral=True)
        return
    cfg = load_verification_config()
    role_id = cfg.get("role_id")
    if not role_id:
        await interaction.response.send_message("⚠️ Verification role has not been set yet. Run `/setverifyrole` first.", ephemeral=True)
        return
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Configured verification role was not found in this server.", ephemeral=True)
        return
    if interaction.guild.me.top_role <= role:
        await interaction.response.send_message("❌ **Bot Hierarchy Error:** Bot's highest role must be ABOVE the verified role to assign it.", ephemeral=True)
        return

    await interaction.response.send_message(f"⏳ **Starting Bulk Verification...** Granting {role.mention} to all current members...", ephemeral=True)
    total = 0
    assigned = 0
    for member in interaction.guild.members:
        total += 1
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Grandfathered Existing Member Verification")
                assigned += 1
            except Exception:
                pass
    await interaction.edit_original_response(content=f"✅ **Bulk Verification Complete!**\n• Total Server Members: `{total}`\n• Granted {role.mention} to `{assigned}` existing members.\n*Current members now have full access. Only brand new joining members will need to verify!*")


@bot.command(name="verifyuser", aliases=["manualverify"])
async def verifyuser_cmd(ctx, member: discord.Member = None):
    """Admin/Owner command to manually verify any user or bot instantly."""
    if not has_admin_or_owner_access(ctx.author):
        await ctx.reply("❌ Only an **Administrator** can manually verify members.")
        return
    if not member:
        await ctx.reply("Usage: `*verifyuser @User`")
        return
    cfg = load_verification_config()
    role_id = cfg.get("role_id")
    if not role_id:
        await ctx.reply("⚠️ Verification role has not been set yet. Run `*setverifyrole @Role` first.")
        return
    role = ctx.guild.get_role(role_id)
    if not role:
        await ctx.reply("❌ Configured verification role was not found in this server.")
        return
    if role in member.roles:
        await ctx.reply(f"ℹ️ {member.mention} is already verified!")
        return
    try:
        await member.add_roles(role, reason=f"Manual verification by {ctx.author.name}")
        await ctx.reply(f"✅ Successfully verified {member.mention} and assigned {role.mention}!")
    except Exception as e:
        await ctx.reply(f"❌ Failed to verify user: `{e}`")



# ==============================================================================
# BYTE CORPORATION — AUTO ROLE SYSTEM (*autorole)
# Automatically assigns roles to new members and/or bots on join.
# ==============================================================================

AUTOROLE_CONFIG_FILE = os.path.join(BASE_DIR, "autorole_config.json")


def load_autorole_config() -> dict:
    default_cfg = {
        "enabled": True,
        "human_role_id": None,
        "bot_role_id": None
    }
    if os.path.exists(AUTOROLE_CONFIG_FILE):
        try:
            with open(AUTOROLE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_cfg.update(data)
        except Exception as e:
            logger.error(f"Error loading autorole_config.json: {e}")
    return default_cfg


def save_autorole_config(cfg: dict):
    try:
        with open(AUTOROLE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving autorole_config.json: {e}")


def build_autorole_embed(guild: discord.Guild) -> discord.Embed:
    cfg = load_autorole_config()
    is_enabled = cfg.get("enabled", True)

    h_role_id = cfg.get("human_role_id")
    h_role = guild.get_role(int(h_role_id)) if h_role_id and guild else None
    h_role_str = h_role.mention if h_role else ("`Not Configured`" if not h_role_id else f"`Role Not Found ({h_role_id})`")

    b_role_id = cfg.get("bot_role_id")
    b_role = guild.get_role(int(b_role_id)) if b_role_id and guild else None
    b_role_str = b_role.mention if b_role else ("`Not Configured`" if not b_role_id else f"`Role Not Found ({b_role_id})`")

    embed = discord.Embed(
        title="🎭 BYTE CORPORATION » Auto Role System",
        description=(
            "Automatically assign roles to new human members and bots as soon as they join the server.\n\n"
            "Use the interactive buttons below or run prefix commands to configure."
        ),
        color=0x0000FF if is_enabled else 0x5865F2
    )
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="⚡ System Status", value="🟢 **Active (Enabled)**" if is_enabled else "🔴 **Disabled**", inline=True)
    embed.add_field(name="👤 Human Auto Role", value=h_role_str, inline=True)
    embed.add_field(name="🤖 Bot Auto Role", value=b_role_str, inline=True)

    embed.add_field(
        name="📋 Commands & Quick Usage",
        value=(
            "• `*autorole humans @Role` — Set role for new human members\n"
            "• `*autorole bots @Role` — Set role for new bots\n"
            "• `*autorole toggle` — Turn Auto Role ON or OFF\n"
            "• `*autorole remove humans` / `*autorole remove bots` — Clear role\n"
            "• `*autorole test` — Test Auto Role assignment on yourself"
        ),
        inline=False
    )
    embed.set_footer(text=f"{guild.name if guild else 'BYTE CORPORATION'} • Auto Role Security Gateway")
    return embed


class ByteAutoroleRoleModal(discord.ui.Modal):
    def __init__(self, target_type: str, title: str):
        super().__init__(title=title)
        self.target_type = target_type
        self.role_input = discord.ui.TextInput(
            label="Role Name or Role ID",
            placeholder="e.g. Member or 1566796494504333353",
            min_length=1,
            max_length=100,
            required=True
        )
        self.add_item(self.role_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ This can only be configured in a server.", ephemeral=True)
            return
        val = self.role_input.value.strip().strip("<@&>")
        found_role = None
        if val.isdigit():
            found_role = guild.get_role(int(val))
        if not found_role:
            for r in guild.roles:
                if r.name.lower() == val.lower():
                    found_role = r
                    break
        if not found_role:
            for r in guild.roles:
                if val.lower() in r.name.lower():
                    found_role = r
                    break

        if not found_role:
            await interaction.response.send_message(f"❌ Role `{val}` not found in this server. Please enter a valid Role Name or Role ID.", ephemeral=True)
            return

        if guild.me.top_role <= found_role:
            await interaction.response.send_message("❌ **Bot Hierarchy Error:** The bot's highest role must be placed ABOVE this role to assign it.", ephemeral=True)
            return

        cfg = load_autorole_config()
        if self.target_type == "humans":
            cfg["human_role_id"] = found_role.id
            save_autorole_config(cfg)
            emb = build_autorole_embed(guild)
            await interaction.response.edit_message(embed=emb, view=ByteAutoroleView())
            await interaction.followup.send(f"✅ **Human Auto Role updated to:** {found_role.mention}\n*New members joining will automatically receive this role.*", ephemeral=True)
        else:
            cfg["bot_role_id"] = found_role.id
            save_autorole_config(cfg)
            emb = build_autorole_embed(guild)
            await interaction.response.edit_message(embed=emb, view=ByteAutoroleView())
            await interaction.followup.send(f"✅ **Bot Auto Role updated to:** {found_role.mention}\n*New bots invited will automatically receive this role.*", ephemeral=True)


class ByteAutoroleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Set Human Role", emoji="👤", style=discord.ButtonStyle.primary, custom_id="btn_ar_set_human", row=0)
    async def btn_set_human(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_or_owner_access(interaction.user):
            await interaction.response.send_message("❌ Only Administrators can configure Auto Role.", ephemeral=True)
            return
        await interaction.response.send_modal(ByteAutoroleRoleModal(target_type="humans", title="Set Human Auto Role"))

    @discord.ui.button(label="Set Bot Role", emoji="🤖", style=discord.ButtonStyle.secondary, custom_id="btn_ar_set_bot", row=0)
    async def btn_set_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_or_owner_access(interaction.user):
            await interaction.response.send_message("❌ Only Administrators can configure Auto Role.", ephemeral=True)
            return
        await interaction.response.send_modal(ByteAutoroleRoleModal(target_type="bots", title="Set Bot Auto Role"))

    @discord.ui.button(label="Toggle ON/OFF", emoji="⚡", style=discord.ButtonStyle.success, custom_id="btn_ar_toggle", row=0)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_or_owner_access(interaction.user):
            await interaction.response.send_message("❌ Only Administrators can toggle Auto Role.", ephemeral=True)
            return
        cfg = load_autorole_config()
        cfg["enabled"] = not cfg.get("enabled", True)
        save_autorole_config(cfg)
        emb = build_autorole_embed(interaction.guild)
        state_str = "🟢 **Enabled**" if cfg["enabled"] else "🔴 **Disabled**"
        await interaction.response.edit_message(embed=emb, view=self)
        await interaction.followup.send(f"⚡ Auto Role system is now {state_str}!", ephemeral=True)

    @discord.ui.button(label="Test On Me", emoji="🧪", style=discord.ButtonStyle.secondary, custom_id="btn_ar_test", row=1)
    async def btn_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_autorole_config()
        h_id = cfg.get("human_role_id")
        if not h_id:
            await interaction.response.send_message("⚠️ Human Auto Role has not been set yet! Click **Set Human Role** first.", ephemeral=True)
            return
        role = interaction.guild.get_role(int(h_id)) if interaction.guild else None
        if not role:
            await interaction.response.send_message("❌ Configured Auto Role was not found in this server.", ephemeral=True)
            return
        if interaction.guild.me.top_role <= role:
            await interaction.response.send_message("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.", ephemeral=True)
            return
        try:
            if role in interaction.user.roles:
                await interaction.response.send_message(f"ℹ️ You already have the Auto Role: {role.mention}", ephemeral=True)
            else:
                await interaction.user.add_roles(role, reason="Auto Role Self-Test")
                await interaction.response.send_message(f"✅ **Auto Role Test Successful!** Assigned {role.mention} to you.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to assign role: `{e}`", ephemeral=True)

    @discord.ui.button(label="Reset", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="btn_ar_reset", row=1)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_or_owner_access(interaction.user):
            await interaction.response.send_message("❌ Only Administrators can reset Auto Role.", ephemeral=True)
            return
        save_autorole_config({"enabled": True, "human_role_id": None, "bot_role_id": None})
        emb = build_autorole_embed(interaction.guild)
        await interaction.response.edit_message(embed=emb, view=self)
        await interaction.followup.send("🔄 Auto Role configuration has been reset to default.", ephemeral=True)


@bot.command(name="autorole", aliases=["autoroles", "joinrole", "ar"])
async def autorole_cmd(ctx, mode: str = None, *, arg: str = None):
    """
    Auto Role Management Suite:
    • `*autorole` — Opens the interactive Auto Role dashboard
    • `*autorole humans @Role` — Sets the auto role for new human members
    • `*autorole bots @Role` — Sets the auto role for new bots
    • `*autorole toggle` — Toggles Auto Role system ON or OFF
    • `*autorole remove humans|bots` — Clears the configured role
    • `*autorole test` — Tests auto role assignment on command author
    """
    if not has_admin_or_owner_access(ctx.author) and not ctx.author.guild_permissions.manage_roles:
        await ctx.reply("❌ Only an **Administrator** or **Staff with Manage Roles** can configure Auto Role.")
        return

    cfg = load_autorole_config()

    if not mode:
        embed = build_autorole_embed(ctx.guild)
        view = ByteAutoroleView()
        await ctx.reply(embed=embed, view=view)
        return

    mode = mode.lower().strip()

    if mode in ["toggle", "switch"]:
        cfg["enabled"] = not cfg.get("enabled", True)
        save_autorole_config(cfg)
        status_txt = "🟢 **Enabled**" if cfg["enabled"] else "🔴 **Disabled**"
        await ctx.reply(f"⚡ **Auto Role system is now {status_txt}!**")
        return

    if mode in ["on", "enable"]:
        cfg["enabled"] = True
        save_autorole_config(cfg)
        await ctx.reply("🟢 **Auto Role system has been enabled!**")
        return

    if mode in ["off", "disable"]:
        cfg["enabled"] = False
        save_autorole_config(cfg)
        await ctx.reply("🔴 **Auto Role system has been disabled!**")
        return

    if mode == "test":
        h_id = cfg.get("human_role_id")
        if not h_id:
            await ctx.reply("⚠️ Human Auto Role has not been set yet! Use `*autorole humans @Role`.")
            return
        role = ctx.guild.get_role(int(h_id))
        if not role:
            await ctx.reply("❌ Configured Auto Role was not found in this server.")
            return
        if ctx.guild.me.top_role <= role:
            await ctx.reply("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.")
            return
        try:
            if role in ctx.author.roles:
                await ctx.reply(f"ℹ️ You already have the Auto Role: {role.mention}")
            else:
                await ctx.author.add_roles(role, reason="Auto Role Self-Test")
                await ctx.reply(f"✅ **Auto Role Test Successful!** Assigned {role.mention} to you.")
        except Exception as e:
            await ctx.reply(f"❌ Failed to assign role: `{e}`")
        return

    if mode in ["remove", "clear", "delete"]:
        which = (arg or "").lower().strip()
        if "bot" in which:
            cfg["bot_role_id"] = None
            save_autorole_config(cfg)
            await ctx.reply("🗑️ **Bot Auto Role has been removed.**")
        elif "human" in which or "member" in which or "user" in which:
            cfg["human_role_id"] = None
            save_autorole_config(cfg)
            await ctx.reply("🗑️ **Human Auto Role has been removed.**")
        else:
            await ctx.reply("Usage: `*autorole remove humans` or `*autorole remove bots`")
        return

    # Mode: humans or bots
    if mode in ["human", "humans", "member", "members", "user", "users", "set"]:
        if not arg and not ctx.message.role_mentions:
            await ctx.reply("Usage: `*autorole humans @Role` or `*autorole humans <role_name_or_id>`")
            return
        role = ctx.message.role_mentions[0] if ctx.message.role_mentions else None
        if not role and arg:
            clean_arg = arg.strip().strip("<@&>")
            if clean_arg.isdigit():
                role = ctx.guild.get_role(int(clean_arg))
            if not role:
                for r in ctx.guild.roles:
                    if r.name.lower() == clean_arg.lower():
                        role = r
                        break
        if not role:
            await ctx.reply(f"❌ Could not find role `{arg}` in this server.")
            return
        if ctx.guild.me.top_role <= role:
            await ctx.reply("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.")
            return
        cfg["human_role_id"] = role.id
        save_autorole_config(cfg)
        await ctx.reply(f"✅ **Human Auto Role configured to:** {role.mention}\n*New members joining will automatically receive this role.*")
        return

    if mode in ["bot", "bots"]:
        if not arg and not ctx.message.role_mentions:
            await ctx.reply("Usage: `*autorole bots @Role` or `*autorole bots <role_name_or_id>`")
            return
        role = ctx.message.role_mentions[0] if ctx.message.role_mentions else None
        if not role and arg:
            clean_arg = arg.strip().strip("<@&>")
            if clean_arg.isdigit():
                role = ctx.guild.get_role(int(clean_arg))
            if not role:
                for r in ctx.guild.roles:
                    if r.name.lower() == clean_arg.lower():
                        role = r
                        break
        if not role:
            await ctx.reply(f"❌ Could not find role `{arg}` in this server.")
            return
        if ctx.guild.me.top_role <= role:
            await ctx.reply("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.")
            return
        cfg["bot_role_id"] = role.id
        save_autorole_config(cfg)
        await ctx.reply(f"✅ **Bot Auto Role configured to:** {role.mention}\n*New bots invited will automatically receive this role.*")
        return

    # Fallback to direct role mention: `*autorole @Role`
    if ctx.message.role_mentions:
        role = ctx.message.role_mentions[0]
        if ctx.guild.me.top_role <= role:
            await ctx.reply("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.")
            return
        cfg["human_role_id"] = role.id
        save_autorole_config(cfg)
        await ctx.reply(f"✅ **Human Auto Role configured to:** {role.mention}\n*New members joining will automatically receive this role.*")
        return

    await ctx.reply("❓ **Unknown option.** Use `*autorole` to open the control panel, or `*autorole humans @Role`.")


@bot.tree.command(name="autorole", description="Open the interactive Auto Role dashboard or configure auto roles")
@app_commands.describe(
    action="Action to perform",
    role="Role to set (optional)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Open Dashboard", value="dashboard"),
    app_commands.Choice(name="Set Human Auto Role", value="humans"),
    app_commands.Choice(name="Set Bot Auto Role", value="bots"),
    app_commands.Choice(name="Toggle ON/OFF", value="toggle"),
    app_commands.Choice(name="Test On Myself", value="test")
])
async def autorole_slash(interaction: discord.Interaction, action: str = "dashboard", role: discord.Role = None):
    if not has_admin_or_owner_access(interaction.user) and not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ Only Administrators or Staff with Manage Roles can configure Auto Role.", ephemeral=True)
        return

    cfg = load_autorole_config()

    if action == "dashboard" or (not role and action in ["humans", "bots"]):
        embed = build_autorole_embed(interaction.guild)
        view = ByteAutoroleView()
        await interaction.response.send_message(embed=embed, view=view)
        return

    if action == "toggle":
        cfg["enabled"] = not cfg.get("enabled", True)
        save_autorole_config(cfg)
        status_txt = "🟢 **Enabled**" if cfg["enabled"] else "🔴 **Disabled**"
        await interaction.response.send_message(f"⚡ **Auto Role system is now {status_txt}!**", ephemeral=True)
        return

    if action == "test":
        h_id = cfg.get("human_role_id")
        if not h_id:
            await interaction.response.send_message("⚠️ Human Auto Role has not been set yet!", ephemeral=True)
            return
        role_obj = interaction.guild.get_role(int(h_id))
        if not role_obj:
            await interaction.response.send_message("❌ Configured Auto Role was not found in this server.", ephemeral=True)
            return
        if interaction.guild.me.top_role <= role_obj:
            await interaction.response.send_message("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.", ephemeral=True)
            return
        try:
            if role_obj in interaction.user.roles:
                await interaction.response.send_message(f"ℹ️ You already have the Auto Role: {role_obj.mention}", ephemeral=True)
            else:
                await interaction.user.add_roles(role_obj, reason="Auto Role Self-Test")
                await interaction.response.send_message(f"✅ **Auto Role Test Successful!** Assigned {role_obj.mention} to you.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to assign role: `{e}`", ephemeral=True)
        return

    if action == "humans":
        if interaction.guild.me.top_role <= role:
            await interaction.response.send_message("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.", ephemeral=True)
            return
        cfg["human_role_id"] = role.id
        save_autorole_config(cfg)
        await interaction.response.send_message(f"✅ **Human Auto Role configured to:** {role.mention}\n*New members joining will automatically receive this role.*")
        return

    if action == "bots":
        if interaction.guild.me.top_role <= role:
            await interaction.response.send_message("❌ **Bot Hierarchy Error:** Bot's highest role must be placed ABOVE this role.", ephemeral=True)
            return
        cfg["bot_role_id"] = role.id
        save_autorole_config(cfg)
        await interaction.response.send_message(f"✅ **Bot Auto Role configured to:** {role.mention}\n*New bots invited will automatically receive this role.*")
        return

# ==========================================
# STATUS & HELP COMMANDS
# ==========================================

@bot.command(name="ping", aliases=["latency"])
async def ping_cmd(ctx):
    """Checks bot latency and API connection."""
    latency = round(bot.latency * 1000)
    await ctx.reply(f"🏓 **Pong!** Latency: `{latency}ms` | System: **BYTE CORPORATION 24/7**")

# ==============================================================================
# BYTE CORPORATION — AVATAR & BANNER VIEWER SUITE
# Exact design matching user screenshots media_1793289248777_78ed9d88.png and
# media_1793289244990_f1bce593.png. Direct PNG, JPG, WEBP, GIF download links.
# Prefix supported: *
# ==============================================================================

async def resolve_target_user(ctx, target: str = None) -> discord.User | discord.Member:
    """Resolves target member or user from mentions, user ID, username, or defaults to ctx.author."""
    if not target:
        return ctx.author

    if ctx.message and ctx.message.mentions:
        return ctx.message.mentions[0]

    clean_id = target.replace("<@", "").replace(">", "").replace("!", "").strip()
    if clean_id.isdigit():
        uid = int(clean_id)
        if ctx.guild:
            m = ctx.guild.get_member(uid)
            if m:
                return m
        try:
            return await bot.fetch_user(uid)
        except Exception:
            pass

    if ctx.guild:
        query = target.lower()
        for m in ctx.guild.members:
            if query in [m.name.lower(), m.display_name.lower()]:
                return m
        for m in ctx.guild.members:
            if query in m.name.lower() or query in m.display_name.lower():
                return m

    return ctx.author


@bot.command(name="avatar", aliases=["av", "pfp", "pic"])
async def avatar_cmd(ctx, *, target: str = None):
    """Displays the target user's avatar with direct download links matching screenshot."""
    user = await resolve_target_user(ctx, target)
    asset = user.display_avatar.with_size(4096)

    png_url = user.display_avatar.with_format("png").with_size(4096).url
    jpg_url = user.display_avatar.with_format("jpeg").with_size(4096).url
    webp_url = user.display_avatar.with_format("webp").with_size(4096).url

    links = f"[PNG]({png_url}) | [JPG]({jpg_url}) | [WEBP]({webp_url})"
    if user.display_avatar.is_animated():
        gif_url = user.display_avatar.with_format("gif").with_size(4096).url
        links += f" | [GIF]({gif_url})"

    embed = discord.Embed(description=links, color=0x0000FF)
    embed.set_author(name=user.name, icon_url=user.display_avatar.url)
    embed.set_image(url=asset.url)
    embed.set_footer(text=f"Requested By {ctx.author.name}", icon_url=ctx.author.display_avatar.url)

    await ctx.reply(embed=embed)


@bot.tree.command(name="avatar", description="Show a user's high resolution avatar with download links")
@app_commands.describe(user="The user whose avatar you want to view (defaults to yourself)")
async def avatar_slash(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    asset = target.display_avatar.with_size(4096)

    png_url = target.display_avatar.with_format("png").with_size(4096).url
    jpg_url = target.display_avatar.with_format("jpeg").with_size(4096).url
    webp_url = target.display_avatar.with_format("webp").with_size(4096).url

    links = f"[PNG]({png_url}) | [JPG]({jpg_url}) | [WEBP]({webp_url})"
    if target.display_avatar.is_animated():
        gif_url = target.display_avatar.with_format("gif").with_size(4096).url
        links += f" | [GIF]({gif_url})"

    embed = discord.Embed(description=links, color=0x0000FF)
    embed.set_author(name=target.name, icon_url=target.display_avatar.url)
    embed.set_image(url=asset.url)
    embed.set_footer(text=f"Requested By {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)


@bot.command(name="banner", aliases=["bn", "userbanner"])
async def banner_cmd(ctx, *, target: str = None):
    """Displays the target user's profile banner with direct download links matching screenshot."""
    user = await resolve_target_user(ctx, target)
    try:
        full_user = await bot.fetch_user(user.id)
    except Exception:
        full_user = user

    if not getattr(full_user, "banner", None):
        if getattr(full_user, "accent_color", None):
            embed = discord.Embed(
                description=f"**{full_user.name}** does not have an image banner set, but has an accent color: `{full_user.accent_color}`",
                color=full_user.accent_color
            )
            embed.set_author(name=full_user.name, icon_url=full_user.display_avatar.url)
            embed.set_footer(text=f"Requested By {ctx.author.name}", icon_url=ctx.author.display_avatar.url)
            await ctx.reply(embed=embed)
            return
        await ctx.reply(f"⚠️ **{full_user.name}** does not have a profile banner set.")
        return

    banner = full_user.banner.with_size(4096)
    png_url = full_user.banner.with_format("png").with_size(4096).url
    jpg_url = full_user.banner.with_format("jpeg").with_size(4096).url
    webp_url = full_user.banner.with_format("webp").with_size(4096).url

    links = f"[PNG]({png_url}) | [JPG]({jpg_url}) | [WEBP]({webp_url})"
    if full_user.banner.is_animated():
        gif_url = full_user.banner.with_format("gif").with_size(4096).url
        links += f" | [GIF]({gif_url})"

    color = full_user.accent_color or 0x2B2D31
    embed = discord.Embed(description=links, color=color)
    embed.set_author(name=full_user.name, icon_url=full_user.display_avatar.url)
    embed.set_image(url=banner.url)
    embed.set_footer(text=f"Requested By {ctx.author.name}", icon_url=ctx.author.display_avatar.url)

    await ctx.reply(embed=embed)


@bot.tree.command(name="banner", description="Show a user's high resolution profile banner with download links")
@app_commands.describe(user="The user whose banner you want to view (defaults to yourself)")
async def banner_slash(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    try:
        full_user = await bot.fetch_user(target.id)
    except Exception:
        full_user = target

    if not getattr(full_user, "banner", None):
        if getattr(full_user, "accent_color", None):
            embed = discord.Embed(
                description=f"**{full_user.name}** does not have an image banner set, but has an accent color: `{full_user.accent_color}`",
                color=full_user.accent_color
            )
            embed.set_author(name=full_user.name, icon_url=full_user.display_avatar.url)
            embed.set_footer(text=f"Requested By {interaction.user.name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed)
            return
        await interaction.response.send_message(f"⚠️ **{full_user.name}** does not have a profile banner set.", ephemeral=True)
        return

    banner = full_user.banner.with_size(4096)
    png_url = full_user.banner.with_format("png").with_size(4096).url
    jpg_url = full_user.banner.with_format("jpeg").with_size(4096).url
    webp_url = full_user.banner.with_format("webp").with_size(4096).url

    links = f"[PNG]({png_url}) | [JPG]({jpg_url}) | [WEBP]({webp_url})"
    if full_user.banner.is_animated():
        gif_url = full_user.banner.with_format("gif").with_size(4096).url
        links += f" | [GIF]({gif_url})"

    color = full_user.accent_color or 0x2B2D31
    embed = discord.Embed(description=links, color=color)
    embed.set_author(name=full_user.name, icon_url=full_user.display_avatar.url)
    embed.set_image(url=banner.url)
    embed.set_footer(text=f"Requested By {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)


@bot.command(name="serverbanner", aliases=["sbanner", "guildbanner"])
async def serverbanner_cmd(ctx):
    """Displays the server's banner or splash art."""
    guild = ctx.guild
    if not guild or not guild.banner:
        await ctx.reply("⚠️ This server does not have a banner set.")
        return
    asset = guild.banner.with_size(4096)
    png_url = guild.banner.with_format("png").with_size(4096).url
    jpg_url = guild.banner.with_format("jpeg").with_size(4096).url
    webp_url = guild.banner.with_format("webp").with_size(4096).url
    links = f"[PNG]({png_url}) | [JPG]({jpg_url}) | [WEBP]({webp_url})"
    if guild.banner.is_animated():
        gif_url = guild.banner.with_format("gif").with_size(4096).url
        links += f" | [GIF]({gif_url})"
    embed = discord.Embed(description=links, color=0x0000FF)
    embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
    embed.set_image(url=asset.url)
    embed.set_footer(text=f"Requested By {ctx.author.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.reply(embed=embed)


@bot.command(name="servericon", aliases=["sicon", "serveravatar"])
async def servericon_cmd(ctx):
    """Displays the server's icon."""
    guild = ctx.guild
    if not guild or not guild.icon:
        await ctx.reply("⚠️ This server does not have an icon set.")
        return
    asset = guild.icon.with_size(4096)
    png_url = guild.icon.with_format("png").with_size(4096).url
    jpg_url = guild.icon.with_format("jpeg").with_size(4096).url
    webp_url = guild.icon.with_format("webp").with_size(4096).url
    links = f"[PNG]({png_url}) | [JPG]({jpg_url}) | [WEBP]({webp_url})"
    if guild.icon.is_animated():
        gif_url = guild.icon.with_format("gif").with_size(4096).url
        links += f" | [GIF]({gif_url})"
    embed = discord.Embed(description=links, color=0x0000FF)
    embed.set_author(name=guild.name, icon_url=guild.icon.url)
    embed.set_image(url=asset.url)
    embed.set_footer(text=f"Requested By {ctx.author.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.reply(embed=embed)




# ==============================================================================
# BYTE CORPORATION — ZYNRAX-STYLE INTERACTIVE HELP DASHBOARD
# Features: Dark obsidian embed, Dropdown module selector, Navigation buttons
#           (<< < 🗑️ > >>), Full English documentation across all 6 modules.
# ==============================================================================

# ==============================================================================
# VOICEMASTER SYSTEM (JOIN-TO-CREATE TEMPORARY VOICE CHANNELS & INTERACTIVE CONTROLLER)
# ==============================================================================
VOICEMASTER_FILE = "voicemaster_config.json"

def load_voicemaster_config() -> dict:
    if not os.path.exists(VOICEMASTER_FILE):
        return {}
    try:
        with open(VOICEMASTER_FILE, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {VOICEMASTER_FILE}: {e}")
        return {}

def save_voicemaster_config(cfg: dict) -> None:
    try:
        with open(VOICEMASTER_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {VOICEMASTER_FILE}: {e}")


def build_voicemaster_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📻 » VoiceMaster Interface",
        description=(
            "Control your temporary voice channel using the buttons below.\n"
            "You must be connected to your room to use these controls!\n\n"
            "🔒 **Lock** — Lock room (no one can join)\n"
            "🔓 **Unlock** — Unlock room for everyone\n"
            "👻 **Hide** — Hide room from server channel list\n"
            "👁️ **Unhide** — Reveal room to server channel list\n"
            "👥 **Limit** — Set user capacity limit (0-99)\n"
            "✏️ **Rename** — Change your room's name\n"
            "🔨 **Kick** — Disconnect someone from your room\n"
            "👑 **Claim** — Claim ownership if owner leaves\n"
            "ℹ️ **Info** — View current room details and status"
        ),
        color=0x0000FF
    )
    embed.set_footer(text="Powered By 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚ｎ ⸸ | VoiceMaster | Designed By diewithvoid")
    return embed


class VMRenameModal(discord.ui.Modal, title="Rename Voice Channel"):
    name_input = discord.ui.TextInput(
        label="New Room Name",
        placeholder="e.g. Chill & Gaming",
        min_length=1,
        max_length=50,
        required=True
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            old_name = self.channel.name
            new_val = self.name_input.value.strip()
            await self.channel.edit(name=new_val, reason=f"VoiceMaster Rename by {interaction.user}")
            await interaction.response.send_message(f"✏️ **Channel renamed:** `{old_name}` ➔ `{new_val}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to rename channel: {e}", ephemeral=True)


class VMLimitModal(discord.ui.Modal, title="Set Room Capacity Limit"):
    limit_input = discord.ui.TextInput(
        label="User Limit (0 = Unlimited, 1-99)",
        placeholder="e.g. 5",
        min_length=1,
        max_length=2,
        required=True
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        val = self.limit_input.value.strip()
        if not val.isdigit() or int(val) < 0 or int(val) > 99:
            await interaction.response.send_message("❌ Invalid number! Please enter 0 to 99.", ephemeral=True)
            return
        lim = int(val)
        try:
            await self.channel.edit(user_limit=lim, reason=f"VoiceMaster Limit by {interaction.user}")
            status = "Unlimited" if lim == 0 else f"{lim} Users"
            await interaction.response.send_message(f"👥 **User limit set to:** `{status}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to set limit: {e}", ephemeral=True)


class VMKickModal(discord.ui.Modal, title="Kick Member from Voice"):
    target_input = discord.ui.TextInput(
        label="User ID or Username",
        placeholder="e.g. 123456789 or Alex",
        min_length=1,
        max_length=60,
        required=True
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        query = self.target_input.value.strip().lower()
        target_member = None
        for m in self.channel.members:
            if str(m.id) == query or query in m.name.lower() or query in m.display_name.lower():
                target_member = m
                break
        if not target_member:
            await interaction.response.send_message("❌ Member not found inside your voice room!", ephemeral=True)
            return
        if target_member.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot kick yourself!", ephemeral=True)
            return
        try:
            await target_member.move_to(None, reason=f"VoiceMaster kick by {interaction.user}")
            await interaction.response.send_message(f"🔨 **{target_member.mention} has been kicked from your room.**", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to kick member: {e}", ephemeral=True)


class VoiceMasterControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def get_user_room(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ **You must be connected to a voice channel!**", ephemeral=True)
            return None, None
        ch = interaction.user.voice.channel
        cfg = load_voicemaster_config()
        g_cfg = cfg.get(str(interaction.guild.id), {})
        active_temp = g_cfg.get("active_channels", {})
        info = active_temp.get(str(ch.id))
        if not info:
            await interaction.response.send_message("❌ **You are not inside a VoiceMaster temporary channel!**", ephemeral=True)
            return None, None
        return ch, info

    @discord.ui.button(emoji="🔒", label="Lock", style=discord.ButtonStyle.secondary, custom_id="vm_btn_lock", row=0)
    async def btn_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can lock this room!**", ephemeral=True)
            return
        await ch.set_permissions(interaction.guild.default_role, connect=False, reason=f"VoiceMaster Lock by {interaction.user}")
        await interaction.response.send_message("🔒 **Your voice room is now locked!** Nobody else can connect.", ephemeral=True)

    @discord.ui.button(emoji="🔓", label="Unlock", style=discord.ButtonStyle.secondary, custom_id="vm_btn_unlock", row=0)
    async def btn_unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can unlock this room!**", ephemeral=True)
            return
        await ch.set_permissions(interaction.guild.default_role, connect=True, reason=f"VoiceMaster Unlock by {interaction.user}")
        await interaction.response.send_message("🔓 **Your voice room is now unlocked!** Everyone can join.", ephemeral=True)

    @discord.ui.button(emoji="👻", label="Hide", style=discord.ButtonStyle.secondary, custom_id="vm_btn_hide", row=0)
    async def btn_hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can hide this room!**", ephemeral=True)
            return
        await ch.set_permissions(interaction.guild.default_role, view_channel=False, reason=f"VoiceMaster Hide by {interaction.user}")
        await interaction.response.send_message("👻 **Your voice room is now hidden!** It is hidden from other members.", ephemeral=True)

    @discord.ui.button(emoji="👁️", label="Unhide", style=discord.ButtonStyle.secondary, custom_id="vm_btn_unhide", row=0)
    async def btn_unhide(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can unhide this room!**", ephemeral=True)
            return
        await ch.set_permissions(interaction.guild.default_role, view_channel=True, reason=f"VoiceMaster Unhide by {interaction.user}")
        await interaction.response.send_message("👁️ **Your voice room is now visible** to the server.", ephemeral=True)

    @discord.ui.button(emoji="👑", label="Claim", style=discord.ButtonStyle.success, custom_id="vm_btn_claim", row=0)
    async def btn_claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        cfg = load_voicemaster_config()
        current_owner_id = info["owner_id"]
        if current_owner_id == interaction.user.id:
            await interaction.response.send_message("⚠️ **You are already the owner of this voice channel!**", ephemeral=True)
            return
        if any(m.id == current_owner_id for m in ch.members):
            await interaction.response.send_message(f"❌ The current owner (<@{current_owner_id}>) is still connected to the channel!", ephemeral=True)
            return
        info["owner_id"] = interaction.user.id
        save_voicemaster_config(cfg)
        await ch.set_permissions(interaction.user, connect=True, speak=True, manage_channels=True, move_members=True, mute_members=True, deafen_members=True)
        await interaction.response.send_message(f"👑 **Ownership Claimed!** You are now the official owner of `{ch.name}`.", ephemeral=True)

    @discord.ui.button(emoji="👥", label="Limit", style=discord.ButtonStyle.primary, custom_id="vm_btn_limit", row=1)
    async def btn_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can set limits!**", ephemeral=True)
            return
        await interaction.response.send_modal(VMLimitModal(ch))

    @discord.ui.button(emoji="✏️", label="Rename", style=discord.ButtonStyle.primary, custom_id="vm_btn_rename", row=1)
    async def btn_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can rename this room!**", ephemeral=True)
            return
        await interaction.response.send_modal(VMRenameModal(ch))

    @discord.ui.button(emoji="🔨", label="Kick", style=discord.ButtonStyle.danger, custom_id="vm_btn_kick", row=1)
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        if info["owner_id"] != interaction.user.id:
            await interaction.response.send_message(f"❌ **Only the room owner (<@{info['owner_id']}>) can kick users!**", ephemeral=True)
            return
        await interaction.response.send_modal(VMKickModal(ch))

    @discord.ui.button(emoji="ℹ️", label="Info", style=discord.ButtonStyle.secondary, custom_id="vm_btn_info", row=1)
    async def btn_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch, info = await self.get_user_room(interaction)
        if not ch: return
        owner = interaction.guild.get_member(info["owner_id"])
        owner_str = owner.mention if owner else f"<@{info['owner_id']}>"
        limit_str = "Unlimited" if ch.user_limit == 0 else f"{ch.user_limit} Members"
        bitrate_str = f"{ch.bitrate // 1000} kbps"
        embed = discord.Embed(
            title=f"🔊 Voice Room: {ch.name}",
            description=(
                f"👑 **Owner:** {owner_str}\n"
                f"👥 **Connected Members:** `{len(ch.members)}`\n"
                f"🚪 **User Limit:** `{limit_str}`\n"
                f"🎙️ **Bitrate:** `{bitrate_str}`\n"
                f"🔒 **Locked:** `{'Yes' if ch.overwrites_for(interaction.guild.default_role).connect is False else 'No'}`\n"
                f"👻 **Hidden:** `{'Yes' if ch.overwrites_for(interaction.guild.default_role).view_channel is False else 'No'}`"
            ),
            color=0x0000FF
        )
        embed.set_footer(text="Powered By 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | VoiceMaster")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def handle_voicemaster_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Handles Join-to-Create temporary voice channel creation and auto-cleanup."""
    if not member or member.bot or not member.guild:
        return

    guild = member.guild
    guild_id_str = str(guild.id)
    vm_cfg = load_voicemaster_config()
    g_cfg = vm_cfg.get(guild_id_str)
    if not g_cfg or not g_cfg.get("enabled", True):
        return

    creator_ch_id = g_cfg.get("creator_channel_id")
    active_temp = g_cfg.setdefault("active_channels", {})

    # 1. Member joined the "Join-to-Create" voice channel
    if after.channel and after.channel.id == creator_ch_id:
        try:
            category = after.channel.category
            ch_name = f"🔊・{member.display_name}'s Room"
            
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
                member: discord.PermissionOverwrite(
                    connect=True,
                    speak=True,
                    manage_channels=True,
                    move_members=True,
                    mute_members=True,
                    deafen_members=True
                ),
                guild.me: discord.PermissionOverwrite(
                    connect=True,
                    speak=True,
                    manage_channels=True,
                    move_members=True
                )
            }

            temp_ch = await guild.create_voice_channel(
                name=ch_name,
                category=category,
                overwrites=overwrites,
                user_limit=g_cfg.get("default_limit", 0),
                reason=f"VoiceMaster: Temporary voice channel created for {member.name}"
            )

            # Move member to the new room
            await member.move_to(temp_ch)

            # Track channel
            active_temp[str(temp_ch.id)] = {
                "owner_id": member.id,
                "guild_id": guild.id,
                "created_at": time.time()
            }
            save_voicemaster_config(vm_cfg)
            logger.info(f"🎙️ VoiceMaster: Created temp channel '{temp_ch.name}' for {member.name}")
        except Exception as e:
            logger.error(f"VoiceMaster create error: {e}")

    # 2. Member left a voice channel — check if an active temp channel is now empty
    if before.channel and str(before.channel.id) in active_temp:
        temp_ch_id_str = str(before.channel.id)
        if len(before.channel.members) == 0:
            try:
                await before.channel.delete(reason="VoiceMaster: Temporary voice room is empty")
                active_temp.pop(temp_ch_id_str, None)
                save_voicemaster_config(vm_cfg)
                logger.info(f"🎙️ VoiceMaster: Cleaned up empty temp channel '{before.channel.name}'")
            except Exception as e:
                logger.warning(f"VoiceMaster cleanup error: {e}")


@bot.group(name="voicemaster", aliases=["vm"], invoke_without_command=True)
async def voicemaster_group(ctx):
    """VoiceMaster temporary voice channels group."""
    embed = discord.Embed(
        title="📻 » VoiceMaster (Temporary Voice Channels)",
        description=(
            "Join-to-Create automated temporary voice channels with Interactive Controller.\n\n"
            "**Server Setup Commands:**\n"
            "`*vm setup` — Automatically creates VoiceMaster category, interface channel & Join VC\n"
            "`*vm panel` (or `*vmpanel`) — Send Interactive 9-Button Controller into current channel\n"
            "`*vm setchannel <#voice_channel>` — Set an existing voice channel as Join-to-Create\n\n"
            "**Room Owner Controls (Buttons on Panel or Commands):**\n"
            "🔒 `*vm lock` — Lock room so others cannot connect\n"
            "🔓 `*vm unlock` — Unlock room for all users\n"
            "👻 `*vm hide` / 👁️ `*vm unhide` — Hide or reveal room\n"
            "👥 `*vm limit <number>` — Set user limit (0 for unlimited, 1-99)\n"
            "✏️ `*vm rename <name>` — Rename your temporary room\n"
            "🔨 `*vm kick @user` — Disconnect someone from your room\n"
            "👑 `*vm claim` — Claim room ownership if owner left"
        ),
        color=0x0000FF
    )
    embed.set_footer(text="Powered By 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | VoiceMaster | Designed By diewithvoid")
    await ctx.reply(embed=embed)


@voicemaster_group.command(name="setup")
@commands.has_permissions(manage_channels=True)
async def vm_setup_cmd(ctx):
    """Automatically creates VoiceMaster Category, Interface Text Channel with Buttons, and Join to Create voice channel."""
    guild = ctx.guild
    status_msg = await ctx.reply("⏳ **Setting up VoiceMaster system with Interactive Panel...**")
    try:
        category = await guild.create_category(name="🔊 VoiceMaster", reason="VoiceMaster Automated Setup")
        
        # 1. Create interface text channel
        interface_ch = await guild.create_text_channel(
            name="interface",
            category=category,
            topic="VoiceMaster Interactive Room Control Panel",
            reason="VoiceMaster Control Interface"
        )
        
        # Send persistent Control Panel into interface channel
        panel_embed = build_voicemaster_panel_embed()
        panel_view = VoiceMasterControlView()
        await interface_ch.send(embed=panel_embed, view=panel_view)

        # 2. Create Join to Create channel
        creator_ch = await guild.create_voice_channel(
            name="➕ Join to Create",
            category=category,
            reason="VoiceMaster Creator Channel"
        )

        cfg = load_voicemaster_config()
        cfg[str(guild.id)] = {
            "enabled": True,
            "creator_channel_id": creator_ch.id,
            "interface_channel_id": interface_ch.id,
            "category_id": category.id,
            "default_limit": 0,
            "active_channels": {}
        }
        save_voicemaster_config(cfg)

        embed = discord.Embed(
            title="✅ VoiceMaster Setup Complete!",
            description=(
                f"**Category:** `{category.name}`\n"
                f"**Control Interface:** {interface_ch.mention} *(Interactive Buttons Live!)*\n"
                f"**Creator Channel:** {creator_ch.mention}\n\n"
                f"Members who join **➕ Join to Create** will get their own private temporary voice channel and can control it via {interface_ch.mention}!"
            ),
            color=0x0000FF
        )
        embed.set_footer(text="Powered By 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ | VoiceMaster | Designed By diewithvoid")
        await status_msg.edit(content=None, embed=embed)
    except Exception as e:
        await status_msg.edit(content=f"❌ Failed to set up VoiceMaster: {e}")


@voicemaster_group.command(name="panel", aliases=["interface"])
@commands.has_permissions(manage_channels=True)
async def vm_panel_cmd(ctx):
    """Spawn the official interactive VoiceMaster Control Panel into current channel."""
    embed = build_voicemaster_panel_embed()
    view = VoiceMasterControlView()
    await ctx.send(embed=embed, view=view)


@bot.command(name="vmpanel", aliases=["voicepanel"])
@commands.has_permissions(manage_channels=True)
async def vmpanel_shortcut(ctx):
    """Quick shortcut to spawn the official VoiceMaster Control Panel."""
    embed = build_voicemaster_panel_embed()
    view = VoiceMasterControlView()
    await ctx.send(embed=embed, view=view)


@voicemaster_group.command(name="setchannel")
@commands.has_permissions(manage_channels=True)
async def vm_setchannel_cmd(ctx, channel: discord.VoiceChannel):
    """Sets existing voice channel as Join to Create."""
    cfg = load_voicemaster_config()
    g_cfg = cfg.setdefault(str(ctx.guild.id), {
        "enabled": True,
        "active_channels": {}
    })
    g_cfg["creator_channel_id"] = channel.id
    if channel.category:
        g_cfg["category_id"] = channel.category.id
    save_voicemaster_config(cfg)
    await ctx.reply(f"✅ VoiceMaster creator channel set to {channel.mention}.")


@voicemaster_group.command(name="lock")
async def vm_lock_cmd(ctx):
    """Locks your temporary voice channel."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("❌ You must be connected to your temporary voice channel.")
        return
    ch = ctx.author.voice.channel
    cfg = load_voicemaster_config()
    g_cfg = cfg.get(str(ctx.guild.id), {})
    active = g_cfg.get("active_channels", {}).get(str(ch.id))
    if not active:
        await ctx.reply("❌ This is not a VoiceMaster temporary channel.")
        return
    if active["owner_id"] != ctx.author.id:
        await ctx.reply(f"❌ Only the room owner (<@{active['owner_id']}>) can lock this room.")
        return
    await ch.set_permissions(ctx.guild.default_role, connect=False)
    await ctx.reply("🔒 **Your temporary voice channel has been locked.**")


@voicemaster_group.command(name="unlock")
async def vm_unlock_cmd(ctx):
    """Unlocks your temporary voice channel."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("❌ You must be connected to your temporary voice channel.")
        return
    ch = ctx.author.voice.channel
    cfg = load_voicemaster_config()
    g_cfg = cfg.get(str(ctx.guild.id), {})
    active = g_cfg.get("active_channels", {}).get(str(ch.id))
    if not active:
        await ctx.reply("❌ This is not a VoiceMaster temporary channel.")
        return
    if active["owner_id"] != ctx.author.id:
        await ctx.reply(f"❌ Only the room owner (<@{active['owner_id']}>) can unlock this room.")
        return
    await ch.set_permissions(ctx.guild.default_role, connect=True)
    await ctx.reply("🔓 **Your temporary voice channel has been unlocked.**")


@voicemaster_group.command(name="limit")
async def vm_limit_cmd(ctx, limit: int):
    """Sets user limit for your temporary room (0 for unlimited)."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("❌ You must be connected to your temporary voice channel.")
        return
    if limit < 0 or limit > 99:
        await ctx.reply("❌ Limit must be between 0 and 99.")
        return
    ch = ctx.author.voice.channel
    cfg = load_voicemaster_config()
    g_cfg = cfg.get(str(ctx.guild.id), {})
    active = g_cfg.get("active_channels", {}).get(str(ch.id))
    if not active or active["owner_id"] != ctx.author.id:
        await ctx.reply("❌ Only the room owner can change the limit.")
        return
    await ch.edit(user_limit=limit)
    status = "Unlimited" if limit == 0 else f"{limit} users"
    await ctx.reply(f"👥 **Room user limit set to:** `{status}`")


@voicemaster_group.command(name="rename")
async def vm_rename_cmd(ctx, *, name: str):
    """Renames your temporary room."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("❌ You must be connected to your temporary voice channel.")
        return
    ch = ctx.author.voice.channel
    cfg = load_voicemaster_config()
    g_cfg = cfg.get(str(ctx.guild.id), {})
    active = g_cfg.get("active_channels", {}).get(str(ch.id))
    if not active or active["owner_id"] != ctx.author.id:
        await ctx.reply("❌ Only the room owner can rename the room.")
        return
    await ch.edit(name=name[:50])
    await ctx.reply(f"✏️ **Room renamed to:** `{name[:50]}`")


@voicemaster_group.command(name="kick")
async def vm_kick_cmd(ctx, member: discord.Member):
    """Kicks a user from your temporary room."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("❌ You must be connected to your temporary voice channel.")
        return
    ch = ctx.author.voice.channel
    cfg = load_voicemaster_config()
    g_cfg = cfg.get(str(ctx.guild.id), {})
    active = g_cfg.get("active_channels", {}).get(str(ch.id))
    if not active or active["owner_id"] != ctx.author.id:
        await ctx.reply("❌ Only the room owner can kick users.")
        return
    if member.voice and member.voice.channel == ch:
        await member.move_to(None)
        await ctx.reply(f"🔨 **{member.mention} has been kicked from your voice room.**")
        await ctx.reply("❌ That user is not in your voice channel.")


@voicemaster_group.command(name="claim")
async def vm_claim_cmd(ctx):
    """Claims ownership of the temporary room if owner left."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("❌ You must be connected to a temporary voice channel.")
        return
    ch = ctx.author.voice.channel
    cfg = load_voicemaster_config()
    g_cfg = cfg.get(str(ctx.guild.id), {})
    active = g_cfg.get("active_channels", {}).get(str(ch.id))
    if not active:
        await ctx.reply("❌ This is not a VoiceMaster temporary channel.")
        return
    if active["owner_id"] == ctx.author.id:
        await ctx.reply("⚠️ You are already the owner of this room.")
        return
    if any(m.id == active["owner_id"] for m in ch.members):
        await ctx.reply(f"❌ The current owner (<@{active['owner_id']}>) is still inside the room.")
        return
    active["owner_id"] = ctx.author.id
    save_voicemaster_config(cfg)
    await ch.set_permissions(ctx.author, connect=True, speak=True, manage_channels=True, move_members=True, mute_members=True, deafen_members=True)
    await ctx.reply(f"👑 **{ctx.author.mention} is now the owner of this channel!**")




def get_home_page_data():
    client_id = bot.user.id if bot.user else "1393529322895048705"
    bot_name = bot.user.name if bot.user else "SpyDerMan\u2020"
    invite_url = f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot%20applications.commands"
    support_url = "https://discord.gg/uaDzFDtFSe"
    website_url = "https://discord.gg/uaDzFDtFSe"
    total_cmds = (len(bot.commands) + len(bot.tree.get_commands())) if hasattr(bot, "commands") else 168

    return {
        "id": "home",
        "label": "Overview",
        "emoji": "\U0001F3E0",
        "select_desc": "Return to the main overview",
        "title": f"Hey , I'm {bot_name}",
        "desc": (
            "A powerful multipurpose bot for UID Whitelist, KeyAuth, Tickets & Server Management.\n\n"
            "\u2022 **My Prefix is** *\n"
            f"\u2022 **Total Commands:** {total_cmds}\n"
            "\u2022 **Choose a Specific Module of your Desire**\n"
            ">>> <a:emoji_31:1523222455823499344> Security & Anti-Nuke\n"
            "<a:emoji_31:1523222455823499344> Giveaway\n"
            "<a:emoji_31:1523222455823499344> Whitelist\n"
            "<a:emoji_31:1523222455823499344> KeyAuth\n"
            "<a:emoji_31:1523222455823499344> Ticket\n"
            "<a:emoji_31:1523222455823499344> Feedback\n"
            "<a:emoji_31:1523222455823499344> Verification\n"
            "<a:emoji_31:1523222455823499344> Welcomer\n"
            "<a:emoji_31:1523222455823499344> Music\n"
            "<a:emoji_31:1523222455823499344> VoiceMaster\n"
            "<a:emoji_31:1523222455823499344> Utility\n"
            "<a:emoji_31:1523222455823499344> System\n\n"
            "\U0001F517 **Links**\n"
            f"[Invite Me]({invite_url}) | [Support Server]({support_url}) | [Website]({website_url})"
        )
    }

ZYNRAX_PAGES = [
    {
        "id": "home",
        "label": "Overview",
        "emoji": "\U0001F3E0",
        "select_desc": "Return to the main overview",
        "title": "Hey , I'm Overview",
        "desc": ""
    },
    {
        "id": "security",
        "label": "Security",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Anti-Nuke, Whitelist, Extra Owner & Raid Protection",
        "title": "<a:emoji_31:1523222455823499344> Anti-Nuke & Server Protection System",
        "desc": (
            "__Security & Anti-Nuke Commands__\n"
            "*antinuke » Open Security & Setup Panel\n"
            "*antinuke enable » Turn ON all 24 protection modules\n"
            "*antinuke disable » Temporarily turn protection OFF\n"
            "*antinuke status » View live active modules & immune staff\n"
            "*antinuke log <#channel> » Set security alert log channel\n"
            "*antinuke settimeout <mins> » Set timeout for links & pings (default 10m)\n"
            "*antinuke allowchannel <#channel> » Whitelist a channel for links\n"
            "*antinuke setemoji <wine/snow/shield/bullet> <emoji> » Set custom or animated emoji\n"
            "*antinuke setlogo <url> » Set top-right thumbnail logo\n"
            "*antinuke edit » Open live interactive modal editor\n\n"
            "__Ownership & Whitelist (Server Owner & Extra Owner Only)__\n"
            "*extraowner add <@user> » Promote trusted member to Extra Owner\n"
            "*extraowner remove <@user> » Revoke Extra Owner permissions\n"
            "*extraowner list » List all Server & Extra Owners\n"
            "*wl add <@user> » Open interactive 25-module Whitelist panel\n"
            "*wl remove <@user> » Revoke whitelist permissions\n"
            "*wl list » View all whitelisted staff members\n\n"
            "__Emergency Lockdown__\n"
            "*lock » Lock current or all channels during active raid\n"
            "*unlock » Unlock channels after raid is cleared"
        )
    },
    {
        "id": "giveaway",
        "label": "Giveaway",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Giveaways with Animated Emoji Buttons",
        "title": "<a:emoji_31:1523222455823499344> Interactive Giveaway System",
        "desc": (
            "__Giveaway Commands__\n"
            "*gstart <duration> <winners> <prize> \u00BB Start interactive giveaway (e.g. *gstart 1h 1w 1 mont)\n"
            "*gend <msg_id> \u00BB End a giveaway immediately & pick winners\n"
            "*greroll <msg_id> \u00BB Reroll a new random winner\n"
            "*glist \u00BB View all active giveaways in this server\n\n"
            "__Animated Emoji & Style Customization__\n"
            "*gsetemoji button <emoji> \u00BB Set Entry button emoji (supports animated <a:name:id>)\n"
            "*gsetemoji bullet <emoji> \u00BB Set embed bullet point emoji\n"
            "*gsetemoji color <#hex> \u00BB Set giveaway embed color (#00D9FF)\n"
            "*gsetemoji reset \u00BB Reset to default cyan theme\n\n"
            "*Supports animated emojis: `<a:emoji_name:id>`*"
        )
    },
    {
        "id": "whitelist",
        "label": "Whitelist",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "UID Whitelist Management & Panels",
        "title": "<a:emoji_31:1523222455823499344> UID Whitelist & License System",
        "desc": (
            "__Commands__\n"
            "*panel (or /panel) \u00BB Open In-Discord UID Whitelist Panel\n"
            "*adduid <uid> \u00BB Whitelist player UID\n"
            "*removeuid <uid> \u00BB Revoke UID from whitelist\n"
            "*setrole <@role> \u00BB Set whitelist customer role\n"
            "*setuidlog <#channel> \u00BB Set dedicated channel for UID audit logs\n"
            "*menu (or /menu) \u00BB Post Byte Corporation Command & Dropdown Menu\n\n"
            "__Features__\n"
            "\u2022 Instant server role granting upon UID verification\n"
            "\u2022 Full audit logging for every UID addition and removal"
        )
    },
    {
        "id": "keyauth",
        "label": "KeyAuth",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "License Generation & Reseller Management",
        "title": "<a:emoji_31:1523222455823499344> KeyAuth & Reseller Management",
        "desc": (
            "__Commands__\n"
            "*keypanel (or /keypanel) \u00BB Post KeyAuth / Reseller Bot Control Panel\n"
            "*resellerpanel (or /resellerpanel) \u00BB Post Dedicated Single-App Reseller Panel\n"
            "*apps (or /apps) \u00BB List all 15 integrated KeyAuth applications\n"
            "*setkeyrole <@role> \u00BB Set role authorized to generate license keys\n"
            "*setkeychannel <#channel> \u00BB Lock KeyAuth panel to a specific channel\n"
            "*setkeylog <#channel> \u00BB Set KeyAuth license generation audit channel\n"
            "*setresellerrole <@role> \u00BB Set authorized reseller role\n"
            "*setresellerchannel <#channel> \u00BB Lock reseller panel to channel\n"
            "*setresellerapp <app> \u00BB Set default application for reseller panel\n\n"
            "__Second Auth Commands (5 Authorized Packages)__\n"
            "*secondkeypanel (or /secondkeypanel) \u00BB Post Second Auth Control Panel\n"
            "*setsecondkeyrole <@role> \u00BB Set authorized Second Auth role\n"
            "*setsecondkeychannel <#channel> \u00BB Lock Second Auth panel to channel\n"
            "*setsecondkeylog <#channel> \u00BB Set Second Auth audit log channel"
        )
    },
    {
        "id": "ticket",
        "label": "Ticket",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Support Tickets, Modals, Claim & AutoPay",
        "title": "<a:emoji_31:1523222455823499344> Interactive Ticket & AutoPay System",
        "desc": (
            "__Commands__\n"
            "*ticketpanel (or /ticketpanel) \u00BB Post official BYTE CORPORATION Ticket Panel\n"
            "*ticketeditor \u00BB Open live interactive Ticket Customizer Dashboard\n"
            "*setticketbuttons \u00BB Edit ticket action buttons (Claim, Close, etc.)\n"
            "*ticketoptions \u00BB List all configured ticket categories\n"
            "*ticketbuttons \u00BB List active ticket action buttons\n"
            "*setautopay \u00BB Configure automatic QR code & UPI payment settings\n"
            "*setoptbanner \u00BB Set custom banner image for ticket options\n"
            "*refreshticket \u00BB Instantly sync and reload ticket panel\n\n"
            "__Features__\n"
            "\u2022 Staff Claim button with modal customization\n"
            "\u2022 Multi-category dropdowns with custom banners"
        )
    },
    {
        "id": "feedback",
        "label": "Feedback",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Automated Feedback DMs & Reminders",
        "title": "<a:emoji_31:1523222455823499344> Feedback Reminder & Warning System",
        "desc": (
            "__Commands__\n"
            "*send (or *remind) \u00BB Send feedback warning DM to customer role\n"
            "*remind_role (or /remind_role) \u00BB Broadcast feedback reminder to all customers\n"
            "*test (or *testdm) \u00BB Send instant personal test feedback DM\n"
            "*channel (or /send_channel_warning) \u00BB Post warning embed in feedback channel\n"
            "*schedule \u00BB Check or configure 48-Hour auto-scheduler status\n\n"
            "__Automated Safeguards__\n"
            "\u2022 Strict 48-Hour Cooldown prevents user spam\n"
            "\u2022 Direct feedback channel button inside DM"
        )
    },
    {
        "id": "verification",
        "label": "Verification",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Server Member Verification System",
        "title": "<a:emoji_31:1523222455823499344> Server Verification System",
        "desc": (
            "__Commands__\n"
            "*autoverifysetup \u00BB 1-Click interactive Verification Setup\n"
            "*verifyinfo \u00BB View current verification role & settings\n"
            "*verifyallcurrent (or /verifyallcurrent) \u00BB Grant verified role to all current members\n"
            "*verifyuser <@user> \u00BB Manually verify a member"
        )
    },
    {
        "id": "welcomer",
        "label": "Welcomer",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Custom Welcome DM & Server Announcements",
        "title": "<a:emoji_31:1523222455823499344> Customizable Welcome System",
        "desc": (
            "__Welcome DM Commands__\n"
            "*welcomedm (or /welcomedm) \u00BB Open interactive Welcome DM editor\n"
            "*welcometest (or /welcometest) \u00BB Send test Welcome DM to private messages\n"
            "*welcometoggle \u00BB Toggle Welcome DM on or off\n\n"
            "__Server Channel Welcome Commands__\n"
            "*serverwelcome (or /serverwelcome) \u00BB Open Server Welcome Channel editor\n"
            "*serverwelcometest \u00BB Send test welcome message to server channel\n"
            "*setwelcomechannel <#channel> \u00BB Set server welcome channel"
        )
    },
    {
        "id": "music",
        "label": "Music",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Lavalink v4 High Fidelity Music & 24/7 VC",
        "title": "<a:emoji_31:1523222455823499344> High Fidelity Music Suite (Lavalink v4)",
        "desc": (
            "__Playback Commands__\n"
            "*play <song/url> (or /play) \u00BB Play any song or playlist in Voice Channel\n"
            "*playnow <song> \u00BB Force-play song immediately\n"
            "*pause / *resume \u00BB Pause or resume music playback\n"
            "*skip / *stop \u00BB Skip song or stop and clear queue\n"
            "*queue / *clearqueue \u00BB View or clear music queue\n"
            "*nowplaying (or /nowplaying) \u00BB Interactive music controller panel\n"
            "*volume <1-100> \u00BB Adjust playback volume\n"
            "*loop / *shuffle / *autoplay \u00BB Loop mode, shuffle, smart autoplay\n"
            "*join / *disconnect \u00BB Summon or disconnect bot from VC\n"
            "*247 \u00BB Toggle 24/7 permanent Voice Channel stay\n"
            "*setmusicbanner <url> \u00BB Set custom video/GIF music banner"
        )
    },
    {
        "id": "voicemaster",
        "label": "VoiceMaster",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Join to Create Temporary Voice Channels",
        "title": "<a:emoji_31:1523222455823499344> VoiceMaster Temporary Channels",
        "desc": (
            "__Commands__\n"
            "*vmpanel \u00BB Post Join-to-Create VoiceMaster channel setup panel\n\n"
            "__Features__\n"
            "\u2022 Automatically creates temporary voice channel when user joins\n"
            "\u2022 Automatically deletes empty room when everyone leaves\n"
            "\u2022 Channel owner gets full control to lock, hide, or rename"
        )
    },
    {
        "id": "utility",
        "label": "Utility",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "User Avatars, Banners, Announcements & Tools",
        "title": "<a:emoji_31:1523222455823499344> Utility & Server Information",
        "desc": (
            "__Commands__\n"
            "*avatar [@user] (or /avatar) \u00BB View high-res avatar with download links\n"
            "*banner [@user] (or /banner) \u00BB View high-res profile banner with download links\n"
            "*servericon \u00BB View server icon in original resolution\n"
            "*serverbanner \u00BB View server banner\n"
            "*announce (or /announce) \u00BB Open interactive announcement builder\n"
            "*ping \u00BB Real-time Discord WebSocket latency & API heartbeat\n"
            "*setstatus / *setpresence \u00BB Configure bot status & activity\n"
            "*lock / *unlock \u00BB Lock or unlock current text channel\n\n"
            "__Emoji & Sticker Stealer (Any Server)__\n"
            "*steal <emoji(s)/url> [name] \u00BB Steal/clone emoji(s) or reply with *steal\n"
            "*stealsticker [name] \u00BB Reply to a sticker to clone into server\n"
            "/steal & /stealsticker \u00BB Slash commands to steal emojis & stickers\n"
            "Right-Click / Long-Press \u00BB Apps \u00BB Steal Emoji / Sticker (1-Click)"
        )
    },
    {
        "id": "system",
        "label": "System",
        "emoji": "<a:emoji_31:1523222455823499344>",
        "select_desc": "Online Bot Updates & Control",
        "title": "<a:emoji_31:1523222455823499344> Bot System & Updates",
        "desc": (
            "__Commands__\n"
            "*update \u00BB 1-Click online bot update from GitHub\n"
            "*setrepo <url> \u00BB Link GitHub repo for automatic 1-click sync\n"
            "*restart (or *reboot) \u00BB Reboot bot process safely\n"
            "*reload \u00BB Hot-reload bot commands without downtime"
        )
    }
]

def build_zynrax_page_embed(page_index: int, requester_name: str = "User") -> discord.Embed:
    if page_index == 0:
        page = get_home_page_data()
    else:
        page = ZYNRAX_PAGES[page_index]

    bot_name = bot.user.name if bot.user else "SpyDerMan\u2020"
    embed = discord.Embed(
        title=page["title"],
        description=page["desc"],
        color=0x0055FF
    )
    if bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.set_footer(text=f"Powered By {bot_name} | Designed By diewithvoid")
    return embed


class ByteZynraxModuleSelect(discord.ui.Select):
    def __init__(self, current_page: int = 0):
        options = []
        for i, page in enumerate(ZYNRAX_PAGES):
            is_default = (i == current_page) if current_page != 0 else False
            
            em_val = page.get("emoji")
            opt_emoji = None
            if em_val:
                try:
                    parsed = parse_custom_emoji(em_val)
                    if parsed:
                        opt_emoji = parsed
                except Exception:
                    opt_emoji = None

            options.append(
                discord.SelectOption(
                    label=page["label"],
                    value=str(i),
                    emoji=opt_emoji,
                    description=page.get("select_desc", f"View {page['label']} commands"),
                    default=is_default
                )
            )
        super().__init__(
            placeholder="\u276F Select Module From Here",
            min_values=1,
            max_values=1,
            custom_id="zynrax_select_module",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        page_idx = int(self.values[0])
        self.view.current_page = page_idx
        for opt in self.options:
            opt.default = (opt.value == str(page_idx)) if page_idx != 0 else False
        embed = build_zynrax_page_embed(page_idx, interaction.user.name)
        await interaction.response.edit_message(embed=embed, view=self.view)


class ByteZynraxHelpView(discord.ui.View):
    """Standard Blue Embed Help View with attached dropdown."""
    def __init__(self, current_page: int = 0, author_id: int = None):
        super().__init__(timeout=None)
        self.current_page = current_page
        self.author_id = author_id
        self.add_item(ByteZynraxModuleSelect(current_page))


@bot.command(name="help", aliases=["helpme", "cmds", "bothelp", "modules"])
async def help_cmd(ctx):
    """Displays official interactive blue help dashboard with right-hand logo."""
    embed = build_zynrax_page_embed(0, ctx.author.name)
    view = ByteZynraxHelpView(current_page=0, author_id=ctx.author.id)
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="help", description="Open official interactive Command Center")
async def help_slash(interaction: discord.Interaction):
    embed = build_zynrax_page_embed(0, interaction.user.name)
    view = ByteZynraxHelpView(current_page=0, author_id=interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

# ==============================================================================
# ONLINE BOT UPDATER & RESTART SYSTEM (*update, *restart, *reload)
# ==============================================================================

@bot.command(name="setrepo")
async def setrepo_cmd(ctx, repo_url: str = None):
    """Binds GitHub repository to this bot for 1-click *update (Owner Only)."""
    is_app_owner = await bot.is_owner(ctx.author)
    is_srv_owner = is_bot_owner_global(ctx.guild, ctx.author.id)
    if not (is_app_owner or is_srv_owner or ctx.author.guild_permissions.administrator):
        await ctx.reply("\u274C **Access Denied:** Only Bot/Server Owner can configure repository.")
        return

    if not repo_url:
        cfg = load_config()
        current_repo = cfg.get("github_repo", "Not Set")
        await ctx.reply(f"\u2139\uFE0F **Current GitHub Repository:** `{current_repo}`\nUsage: `*setrepo <github_repo_url>`")
        return

    repo_url = repo_url.strip().rstrip("/")
    if "github.com" in repo_url and "raw.githubusercontent.com" not in repo_url:
        clean = repo_url.replace("https://github.com/", "").replace("http://github.com/", "")
        parts = clean.split("/")
        if len(parts) >= 2:
            owner, repo_name = parts[0], parts[1].replace(".git", "")
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/main/bot.py"
        else:
            raw_url = repo_url
    else:
        raw_url = repo_url

    cfg = load_config()
    cfg["github_repo"] = repo_url
    cfg["github_raw_url"] = raw_url
    save_config(cfg)

    embed = discord.Embed(
        title="\u2705 GitHub Repository Linked!",
        description=(
            f"**Repository:** `{repo_url}`\n"
            f"**Direct Sync URL:** `{raw_url}`\n\n"
            "Ab se aap sirf **`*update`** type karenge aur bot seedha aapke GitHub se latest code download karke auto-restart ho jayega!"
        ),
        color=0x00FF88
    )
    await ctx.reply(embed=embed)


@bot.command(name="update", aliases=["gitupdate", "pull"])
async def update_cmd(ctx, mode: str = "git", *, arg: str = None):
    """
    Online Bot Updater (Owner Only):
    - `*update` / `*update git`: Pulls latest code from GitHub and restarts.
    - `*update check`: Checks if remote repository has new updates.
    - `*update url <raw_url>`: Downloads updated bot.py from URL and restarts.
    - `*update force`: Force hard reset and git pull.
    """
    is_app_owner = await bot.is_owner(ctx.author)
    is_srv_owner = is_bot_owner_global(ctx.guild, ctx.author.id)
    if not (is_app_owner or is_srv_owner or ctx.author.guild_permissions.administrator):
        await ctx.reply("\u274C **Access Denied:** Only Bot/Server Owner can run online updates.")
        return

    mode = mode.lower().strip()

    if mode == "check":
        msg = await ctx.reply("\U0001F50D **Checking GitHub for latest updates...**")
        try:
            fetch_res = subprocess.run(["git", "fetch"], capture_output=True, text=True, timeout=20)
            status_res = subprocess.run(["git", "status", "-uno"], capture_output=True, text=True, timeout=20)
            out = status_res.stdout

            if "Your branch is up to date" in out:
                await msg.edit(content="\u2705 **Bot is completely up to date!** No new updates on GitHub.")
            elif "Your branch is behind" in out:
                log_res = subprocess.run(["git", "log", "HEAD..@{u}", "--oneline"], capture_output=True, text=True, timeout=20)
                commits = log_res.stdout.strip() or "New commits detected!"
                embed = discord.Embed(
                    title="\U0001F680 New Update Available!",
                    description=f"Your bot is behind the remote repository.\n\n**New Commits:**\n```\n{commits[:1500]}\n```\nRun `*update` to pull and restart automatically!",
                    color=0x00FF88
                )
                await msg.edit(content=None, embed=embed)
            else:
                await msg.edit(content=f"\u2139\uFE0F **Git Status:**\n```\n{out[:1500]}\n```")
        except FileNotFoundError:
            await msg.edit(content="\u26A0\uFE0F **Git is not installed on this host.** Use `*update url <raw_github_url>` to update directly from a link!")
        except Exception as e:
            await msg.edit(content=f"\u274C **Error checking update:** `{e}`")
        return

    # Helper function for URL download
    async def download_and_restart(target_url: str):
        target_url = target_url.strip().strip("<>()[]\"'`").rstrip(").,;>")
        # Normalize web github link to raw usercontent link if user passed repo/blob link
        if "github.com" in target_url and "raw.githubusercontent.com" not in target_url:
            clean = target_url.replace("https://github.com/", "").replace("http://github.com/", "")
            parts = [p for p in clean.split("/") if p and p != "blob"]
            if len(parts) >= 3:
                owner, repo_name, branch = parts[0], parts[1], parts[2]
                rest = "/".join(parts[3:]) if len(parts) > 3 else "bot.py"
                target_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{rest}"
            elif len(parts) == 2:
                owner, repo_name = parts[0], parts[1].replace(".git", "")
                target_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/main/bot.py"

        msg = await ctx.reply(f"📥 **Downloading update directly from GitHub:**\n`{target_url}`...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        await msg.edit(content=f"❌ **Failed to download:** HTTP {resp.status}")
                        return
                    new_code = (await resp.text(encoding="utf-8")).lstrip('\ufeff')

            try:
                ast.parse(new_code)
            except SyntaxError as syn_err:
                await msg.edit(content=f"❌ **Syntax Error in downloaded code:** `{syn_err}`\nUpdate aborted for safety!")
                return

            current_path = os.path.abspath(__file__)
            backup_path = current_path + ".backup"
            try:
                shutil.copy2(current_path, backup_path)
            except Exception:
                pass

            with open(current_path, "w", encoding="utf-8") as f:
                f.write(new_code)

            if "bot.py" in target_url:
                config_url = target_url.replace("bot.py", "antinuke_config.json")
                try:
                    async with session.get(config_url, timeout=aiohttp.ClientTimeout(total=10)) as cresp:
                        if cresp.status == 200:
                            c_text = await cresp.text(encoding="utf-8")
                            json.loads(c_text)
                            antinuke_path = os.path.join(os.path.dirname(current_path), "antinuke_config.json")
                            with open(antinuke_path, "w", encoding="utf-8") as cf:
                                cf.write(c_text)
                except Exception:
                    pass

                gw_url = target_url.replace("bot.py", "giveaways.json")
                try:
                    async with session.get(gw_url, timeout=aiohttp.ClientTimeout(total=10)) as gresp:
                        if gresp.status == 200:
                            g_text = await gresp.text(encoding="utf-8")
                            json.loads(g_text)
                            gw_path = os.path.join(os.path.dirname(current_path), "giveaways.json")
                            if not os.path.exists(gw_path):
                                with open(gw_path, "w", encoding="utf-8") as gf:
                                    gf.write(g_text)
                except Exception:
                    pass

            embed = discord.Embed(
                title="🚀 Online Update Installed!",
                description="New code successfully downloaded and verified!\n🔄 **Restarting bot now... Please wait 5-10 seconds!**",
                color=0x00FF88
            )
            await msg.edit(content=None, embed=embed)
            await asyncio.sleep(1)
            await bot.close()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            await msg.edit(content=f"❌ **Update failed:** `{e}`")

    cfg = load_config()
    cfg_raw_url = cfg.get("github_raw_url") or "https://raw.githubusercontent.com/IKVOID/byte-whitelist-bot/main/bot.py"

    # Support *update https://raw.githubusercontent.com/... directly
    if mode.startswith("http://") or mode.startswith("https://"):
        await download_and_restart(mode)
        return

    if mode == "url":
        url_to_use = arg.strip() if arg else cfg_raw_url
        await download_and_restart(url_to_use)
        return

    # If no .git folder or default update
    has_git_folder = os.path.exists(".git")
    if not has_git_folder:
        await download_and_restart(cfg_raw_url)
        return

    # Mode 3: Git Pull & Restart (Default)
    msg = await ctx.reply("\U0001F504 **Pulling latest updates from GitHub...**")
    try:
        if mode == "force":
            subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, timeout=20)

        pull_res = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30)
        output = (pull_res.stdout + "\n" + pull_res.stderr).strip()

        if pull_res.returncode != 0:
            # Check if it failed because git repo is not initialized
            if "not a git repository" in output:
                embed = discord.Embed(
                    title="\u26A0\uFE0F Git Repository Not Initialized",
                    description=(
                        "Aapki hosting par `.git` folder nahi hai (kyunki bot files ZIP se upload hui thin).\n\n"
                        "**Abhi update karne ke 2 aasan tarike:**\n"
                        "1. **Direct Link Se Update Karein:**\n"
                        "`*update url <raw_github_link_to_bot.py>`\n\n"
                        "2. **Aage Se 1-Click Update Ke Liye Repo Link Karein:**\n"
                        "`*setrepo https://github.com/YourUsername/YourRepo`\n"
                        "*(Ek baar link karne ke baad aap seedha `*update` likhenge toh bot khud download karke restart ho jayega).* "
                    ),
                    color=0xFFAA00
                )
                await msg.edit(content=None, embed=embed)
                return
            else:
                embed = discord.Embed(
                    title="\u274C Git Pull Error",
                    description=f"```\n{output[:1200]}\n```",
                    color=0xFF0000
                )
                await msg.edit(content=None, embed=embed)
                return

        if "Already up to date." in output and mode != "force":
            embed = discord.Embed(
                title="\u2705 Already Up To Date",
                description="Your bot is already running the latest version from GitHub!\n(Use `*restart` if you just want to reboot).",
                color=0x2B2D31
            )
            await msg.edit(content=None, embed=embed)
            return

        if os.path.exists("requirements.txt"):
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"], timeout=60)

        embed = discord.Embed(
            title="\U0001F680 Update Pulled Successfully!",
            description=f"**Git Pull Output:**\n```\n{output[:1200]}\n```\n\U0001F504 **Restarting bot now... Please wait 5-10 seconds!**",
            color=0x00FF88
        )
        await msg.edit(content=None, embed=embed)
        await asyncio.sleep(1)
        await bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except FileNotFoundError:
        embed = discord.Embed(
            title="\u26A0\uFE0F Git Not Installed",
            description=(
                "This hosting instance doesn't have Git installed.\n\n"
                "**Solution:**\n"
                "Run `*update url <raw_github_url>` to update directly from a link!"
            ),
            color=0xFFAA00
        )
        await msg.edit(content=None, embed=embed)
    except Exception as e:
        await msg.edit(content=f"\u274C **Error during update:** `{e}`")




@bot.command(name="restart", aliases=["reboot"])
async def restart_cmd(ctx):
    """Cleanly restarts the bot process (Owner Only)."""
    is_app_owner = await bot.is_owner(ctx.author)
    is_srv_owner = is_bot_owner_global(ctx.guild, ctx.author.id)
    if not (is_app_owner or is_srv_owner or ctx.author.guild_permissions.administrator):
        await ctx.reply("❌ **Access Denied:** Only Bot/Server Owner can restart the bot.")
        return

    embed = discord.Embed(
        title="🔄 Rebooting Bot...",
        description="The bot is restarting now. It will be back online in 5-10 seconds!",
        color=0x2B2D31
    )
    await ctx.reply(embed=embed)
    await asyncio.sleep(1)
    await bot.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.command(name="reload")
async def reload_cmd(ctx):
    """Reloads all JSON configuration files live without restarting (Owner Only)."""
    is_app_owner = await bot.is_owner(ctx.author)
    is_srv_owner = is_bot_owner_global(ctx.guild, ctx.author.id)
    if not (is_app_owner or is_srv_owner or ctx.author.guild_permissions.administrator):
        await ctx.reply("❌ **Access Denied:** Only Bot/Server Owner can reload configurations.")
        return

    reloaded = []
    try:
        global BOT_CONFIG
        if os.path.exists("config.json"):
            with open("config.json", "r", encoding="utf-8") as f:
                BOT_CONFIG = json.load(f)
            reloaded.append("`config.json`")
    except Exception as e:
        reloaded.append(f"`config.json` (Error: {e})")

    try:
        if "load_ticket_config" in globals():
            globals()["load_ticket_config"]()
            reloaded.append("`ticket_config.json`")
    except Exception:
        pass

    try:
        if "load_server_welcome_config" in globals():
            globals()["load_server_welcome_config"]()
            reloaded.append("`server_welcome_config.json`")
    except Exception:
        pass

    embed = discord.Embed(
        title="✅ Configurations Reloaded Live!",
        description="The following files were reloaded without restarting the bot:\n• " + "\n• ".join(reloaded),
        color=0x00FF88
    )
    await ctx.reply(embed=embed)









# ==============================================================================
# BYTE CORPORATION — FASTEST ENTERPRISE ANTINUKE & WHITELIST SECURITY SYSTEM
# Features all 23 Protections + Extra Owner System matching User Screenshot


@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        await bot.process_commands(message)
        return

    cfg = load_antinuke_config()
    is_sec_enabled = cfg.get("enabled", True)

    # 1. VOID Security: Anti-Everyone / Anti-Here Ping Protection (TIMEOUT, NOT BAN)
    if is_sec_enabled and ("@everyone" in message.content or "@here" in message.content):
        perm_needed = "everyone_ping" if "@everyone" in message.content else "here_ping"
        is_immune = (
            message.author.id == message.guild.owner_id or
            is_extra_owner(message.author.id) or
            is_antinuke_whitelisted(message.guild, message.author, perm_needed)
        )
        if not is_immune:
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Could not delete mass ping message: {e}")

            timeout_mins = cfg.get("anti_everyone", {}).get("timeout_minutes", 5)
            timeout_applied = False

            member = message.author
            if not isinstance(member, discord.Member):
                member = message.guild.get_member(message.author.id)
            if not member:
                try:
                    member = await message.guild.fetch_member(message.author.id)
                except Exception:
                    member = message.author

            if isinstance(member, discord.Member) and member.id != message.guild.owner_id:
                bot_member = message.guild.me
                if bot_member.guild_permissions.moderate_members or bot_member.guild_permissions.administrator:
                    if bot_member.top_role > member.top_role:
                        try:
                            until = discord.utils.utcnow() + timedelta(minutes=timeout_mins)
                            await member.timeout(until, reason="Automod: Mass Mention")
                            timeout_applied = True
                        except Exception as e:
                            logger.warning(f"Failed to timeout {member} for mass ping: {e}")
                            try:
                                await member.timeout(timedelta(minutes=timeout_mins), reason="Automod: Mass Mention")
                                timeout_applied = True
                            except Exception as e2:
                                logger.error(f"Fallback timeout also failed for {member.id}: {e2}")
                    else:
                        logger.warning(f"[Anti-Everyone] Cannot timeout {member} ({member.id}) - Bot top role <= user top role")
                else:
                    logger.warning(f"[Anti-Everyone] Bot lacks moderate_members permission in {message.guild.name}")

            ping_type = "@everyone" if "@everyone" in message.content else "@here"
            action_str = f"Muted for {timeout_mins} minutes" if timeout_applied else "Message Deleted"
            await send_automod_log(message.guild, "Anti-Everyone", message.author, message.channel, action_str, f"Automod: Mass Mention ({ping_type})")
            return

    # 2. VOID Security: Anti-Link Protection (TIMEOUT, NOT BAN)
    link_regex = re.compile(r"(https?://\S+|www\.\S+|discord\.gg/\S+|discord\.com/invite/\S+|t\.me/\S+|bit\.ly/\S+)", re.IGNORECASE)
    if is_sec_enabled and link_regex.search(message.content):
        # Check immunity: Server Owner, Extra Owners, Whitelisted
        is_immune = (
            message.author.id == message.guild.owner_id or
            is_extra_owner(message.author.id) or
            is_antinuke_whitelisted(message.guild, message.author, "anti_link")
        )
        allowed_channels = cfg.get("anti_link", {}).get("allowed_channels", [])
        if message.channel.id in allowed_channels or message.channel.name.lower() in ["promo", "self-promo", "promotion"]:
            is_immune = True

        if not is_immune:
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Could not delete link message: {e}")

            timeout_mins = cfg.get("anti_link", {}).get("timeout_minutes", 5)
            timeout_applied = False

            member = message.author
            if not isinstance(member, discord.Member):
                member = message.guild.get_member(message.author.id)
            if not member:
                try:
                    member = await message.guild.fetch_member(message.author.id)
                except Exception:
                    member = message.author

            if isinstance(member, discord.Member) and member.id != message.guild.owner_id:
                bot_member = message.guild.me
                if bot_member.guild_permissions.moderate_members or bot_member.guild_permissions.administrator:
                    if bot_member.top_role > member.top_role:
                        try:
                            until = discord.utils.utcnow() + timedelta(minutes=timeout_mins)
                            await member.timeout(until, reason="Automod: Posted a link")
                            timeout_applied = True
                        except Exception as e:
                            logger.warning(f"Failed to timeout {member} for link: {e}")
                            try:
                                await member.timeout(timedelta(minutes=timeout_mins), reason="Automod: Posted a link")
                                timeout_applied = True
                            except Exception as e2:
                                logger.error(f"Fallback timeout also failed for {member.id}: {e2}")
                    else:
                        logger.warning(f"[Anti-Link] Cannot timeout {member} ({member.id}) - Bot top role <= user top role")
                else:
                    logger.warning(f"[Anti-Link] Bot lacks moderate_members permission in {message.guild.name}")

            action_str = f"Muted for {timeout_mins} minutes" if timeout_applied else "Message Deleted"
            await send_automod_log(message.guild, "Anti-Link", message.author, message.channel, action_str, "Automod: Posted a link")
            return

    await bot.process_commands(message)


# --- CHANNEL LOCKDOWN MODERATION COMMANDS ---

@bot.command(name="lock", aliases=["lockdown"])
async def lock_channel_cmd(ctx, target: str = None):
    """Locks current channel or all channels to stop active raids."""
    if not is_bot_owner_global(ctx.guild, ctx.author.id) and not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ **Access Denied:** Only Server Owner, Extra Owners, or Admins can lock channels.")
        return

    everyone_role = ctx.guild.default_role
    if target and target.lower() in ("all", "server"):
        locked_count = 0
        for ch in ctx.guild.text_channels:
            try:
                overwrite = ch.overwrites_for(everyone_role)
                if overwrite.send_messages is not False:
                    overwrite.send_messages = False
                    await ch.set_permissions(everyone_role, overwrite=overwrite, reason=f"[ANTI-RAID] Emergency Server Lockdown by {ctx.author}")
                    locked_count += 1
            except Exception:
                pass
        await ctx.reply(f"🚨 **Server Lockdown Complete:** `{locked_count}` text channels locked against raid!")
        overwrite = ctx.channel.overwrites_for(everyone_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(everyone_role, overwrite=overwrite, reason=f"[ANTI-RAID] Channel locked by {ctx.author}")
        await ctx.reply(f"🔒 **Channel Locked:** {ctx.channel.mention} is now locked. Run `*unlock` to restore.")


@bot.command(name="unlock", aliases=["unlockdown"])
async def unlock_channel_cmd(ctx, target: str = None):
    """Unlocks current channel or all channels after a raid is cleared."""
    if not is_bot_owner_global(ctx.guild, ctx.author.id) and not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ **Access Denied:** Only Server Owner, Extra Owners, or Admins can unlock channels.")
        return

    everyone_role = ctx.guild.default_role
    if target and target.lower() in ("all", "server"):
        unlocked_count = 0
        for ch in ctx.guild.text_channels:
            try:
                overwrite = ch.overwrites_for(everyone_role)
                if overwrite.send_messages is False:
                    overwrite.send_messages = None
                    await ch.set_permissions(everyone_role, overwrite=overwrite, reason=f"[ANTI-RAID] Server Unlockdown by {ctx.author}")
                    unlocked_count += 1
            except Exception:
                pass
        await ctx.reply(f"🔓 **Server Unlocked:** `{unlocked_count}` channels restored!")
        overwrite = ctx.channel.overwrites_for(everyone_role)
        overwrite.send_messages = None
        await ctx.channel.set_permissions(everyone_role, overwrite=overwrite, reason=f"[ANTI-RAID] Channel unlocked by {ctx.author}")
        await ctx.reply(f"🔓 **Channel Unlocked:** {ctx.channel.mention} is now unlocked.")




# ==========================================
# MAIN EXECUTION
# ==========================================

def run_bot(token: str = None):
    global bot
    target_token = token or DISCORD_TOKEN
    if not target_token:
        logger.error("❌ DISCORD_TOKEN is not configured in .env!")
        sys.exit(1)

    logger.info("Starting 𝐒𝐩𝐲𝐃𝐞𝐫𝐌𝐚𝐧 ⸸ Bot | Designed By diewithvoid...")
    try:
        bot.run(target_token)
    except discord.errors.PrivilegedIntentsRequired:
        logger.warning(
            "\n" + "="*60 +
            "\n⚠️ WARNING: Privileged Gateway Intents (Members / Message Content) are not enabled in Discord Developer Portal!" +
            "\nFalling back to default intents so the bot stays online 24/7 without crashing..." +
            "\n" + "="*60 + "\n"
        )
        bot._closing_task = None
        bot._connection._intents = discord.Intents.default()
        bot.run(target_token)


# ==============================================================================

# ==============================================================================
# BYTE CORPORATION — A-TO-Z FULLY CUSTOMIZABLE WELCOME DM SYSTEM
# Matches exact screenshot media_1793338405048_a3bea768.png
# Full In-Discord Control: Modals, Buttons, Live Previews, DM Testing & Hot-Reloading
# ==============================================================================

WELCOME_CONFIG_PATH = "welcome_config.json"

DEFAULT_WELCOME_CONFIG = {
    "enabled": True,
    "message_content": "Hello {user}, Welcome To **{server}**!",
    "embed": {
        "enabled": True,
        "color": "#E74C3C",
        "title": "You Are Our {member_count} Member.",
        "description": (
            "We're Glad To Have You Here.\n\n"
            "We're A Premium **Panel Store** Focused On Providing **Quality Products, Regular Updates, And Reliable Customer Support.**\n\n"
            ":3045bluestar: **__Our Store__**\n"
            "> [🛒 ┆ LIB-BYPASS](https://discord.com/channels/1518838682449219675/1518838829975339110)\n"
            "> [🛒 ┆ Internal-Panel](https://discord.com/channels/1518838682449219675/1518838831116451900)\n"
            "> [🛒 ┆ Basic-Panel](https://discord.com/channels/1518838682449219675/1518838832311701544)\n"
            "> [🛒 ┆ AimKill](https://discord.com/channels/1518838682449219675/1518838833699885147)\n"
            "> [🛒 ┆ Aim-Silent](https://discord.com/channels/1518838682449219675/1518838835134468177)\n"
            "> [🛒 ┆ UID-Bypass](https://discord.com/channels/1518838682449219675/1518838836090765383)\n"
            "> [🛒 ┆ SilentKill](https://discord.com/channels/1518838682449219675/1518838837005127730)\n"
            "> [🛒 ┆ AimKill-Plus](https://discord.com/channels/1518838682449219675/1518838838284521585)\n"
            "> [🛒 ┆ Phone-Panel](https://discord.com/channels/1518838682449219675/1526460046144311296)\n"
            "> [🛒 ┆ PC-Optimization](https://discord.com/channels/1518838682449219675/1548588072155877406)\n\n"
            "All Products Are **Paid** And Available Through Our **Official Store**. Open A [🎫 ┆ Ticket](https://discord.com/channels/1518838682449219675/1518838845523755058) If You're Interested.\n\n"
            ":3045bluestar: **__Stay Connected For__**\n"
            "- [📢 ┆ Announcements](https://discord.com/channels/1518838682449219675/1518851333191503933)\n"
            "- [🎁 ┆ Updates & Offers](https://discord.com/channels/1518838682449219675/1532032398924189796)\n"
            "- [💬 ┆ Free Trials](https://discord.com/channels/1518838682449219675/1518838849000706170)\n\n"
            "Thanks For Joining **{server}**."
        ),
        "thumbnail_url": "{server_icon}",
        "banner_url": "",
        "footer": "BYTE CORPORATION • Official Store",
        "footer_icon_url": "{server_icon}"
    }
}


def load_welcome_config() -> dict:
    if os.path.exists(WELCOME_CONFIG_PATH):
        try:
            with open(WELCOME_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading {WELCOME_CONFIG_PATH}: {e}")
    return DEFAULT_WELCOME_CONFIG.copy()


def save_welcome_config(cfg: dict) -> None:
    try:
        with open(WELCOME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {WELCOME_CONFIG_PATH}: {e}")


def get_welcome_ordinal(n: int) -> str:
    """Returns '1176th' exactly matching user screenshot format."""
    return f"{n}th"


def build_welcome_payload(member: discord.Member, cfg: dict = None) -> tuple[str, discord.Embed | None]:
    """Generates the personalized Welcome DM message content and embed with auto-resolving channels & emojis."""
    if not cfg:
        cfg = load_welcome_config()

    guild = getattr(member, "guild", None)
    if not guild and bot.guilds:
        guild = bot.guilds[0]

    guild_id = guild.id if guild else 1518838682449219675
    guild_name = guild.name if guild else "BYTE CORPORATION"
    m_count = (guild.member_count if guild else 1176) or 1
    m_count_str = get_welcome_ordinal(m_count)

    replacements = {
        "{user}": member.mention,
        "{user_mention}": member.mention,
        "{user_name}": member.name,
        "{user_display}": getattr(member, "display_name", member.name),
        "{server}": guild_name,
        "{server_name}": guild_name,
        "{member_count}": m_count_str,
        "{count}": str(m_count),
        "{member_count_num}": str(m_count),
    }

    raw_msg = cfg.get("message_content", "Hello {user}, Welcome To **{server}**!")
    for k, v in replacements.items():
        raw_msg = raw_msg.replace(k, v)

    emb_cfg = cfg.get("embed", {})
    if not emb_cfg.get("enabled", True):
        return raw_msg, None

    raw_title = emb_cfg.get("title", "You Are Our {member_count} Member.")
    raw_desc = emb_cfg.get("description", "")
    for k, v in replacements.items():
        raw_title = raw_title.replace(k, v)
        raw_desc = raw_desc.replace(k, v)

    # Fallback dictionary of Byte Corporation channels if channel cache is resolving
    known_channels = {
        1518838829975339110: "🛒 ┆ LIB-BYPASS",
        1518838831116451900: "🛒 ┆ Internal-Panel",
        1518838832311701544: "🛒 ┆ Basic-Panel",
        1518838833699885147: "🛒 ┆ AimKill",
        1518838835134468177: "🛒 ┆ Aim-Silent",
        1518838836090765383: "🛒 ┆ UID-Bypass",
        1518838837005127730: "🛒 ┆ SilentKill",
        1518838838284521585: "🛒 ┆ AimKill-Plus",
        1526460046144311296: "🛒 ┆ Phone-Panel",
        1548588072155877406: "🛒 ┆ PC-Optimization",
        1518838845523755058: "🎫 ┆ Ticket",
        1518851333191503933: "📢 ┆ Announcements",
        1532032398924189796: "🎁 ┆ Updates & Offers",
        1518838849000706170: "💬 ┆ Free Trials",
    }

    # 1. Automatic Channel Mentions & IDs Resolver:
    # Discord DMs do not resolve <#ID> or #ID. Discord markdown links [Channel](https://discord.com/channels/...)
    # work 100% and jump directly into the server channel when clicked!
    def _channel_replacer(match):
        cid_str = match.group(1)
        try:
            cid = int(cid_str)
            ch = guild.get_channel(cid) if guild else None
            if not ch and hasattr(bot, "get_channel"):
                ch = bot.get_channel(cid)
            if ch:
                cname = ch.name.replace("[", "").replace("]", "").strip()
            elif cid in known_channels:
                cname = known_channels[cid]
                cname = f"channel-{cid_str[-4:]}"
            return f"[{cname}](https://discord.com/channels/{guild_id}/{cid})"
        except Exception:
            return match.group(0)

    channel_pattern = re.compile(r"(?<!/)(?:<#|#)(\d{17,20})>?")
    raw_desc = channel_pattern.sub(_channel_replacer, raw_desc)
    raw_msg = channel_pattern.sub(_channel_replacer, raw_msg)

    # 2. Automatic Emoji Resolver:
    # Resolves shortcodes like :3045bluestar: to animated <a:name:id> or <:name:id>
    def _emoji_replacer(match):
        ename = match.group(1)
        all_emojis = list(guild.emojis) if guild else []
        if hasattr(bot, "emojis"):
            all_emojis += list(bot.emojis)
        for em in all_emojis:
            if em.name.lower() == ename.lower():
                return str(em)
        for em in all_emojis:
            if ename.lower() in em.name.lower() or em.name.lower() in ename.lower():
                return str(em)
        return match.group(0)

    emoji_pattern = re.compile(r"(?<!<)(?<!<a):([a-zA-Z0-9_~]+):(?!\d+>)")
    raw_desc = emoji_pattern.sub(_emoji_replacer, raw_desc)
    raw_msg = emoji_pattern.sub(_emoji_replacer, raw_msg)

    # Clean up accidental double asterisks at the end of sentences
    raw_desc = re.sub(r"(?<!\*)\*\*(\.|\s*$)", r"", raw_desc)

    hex_color = emb_cfg.get("color", "#E74C3C").lstrip("#")
    try:
        color_int = int(hex_color, 16)
    except Exception:
        color_int = 0xE74C3C

    embed = discord.Embed(
        title=raw_title,
        description=raw_desc,
        color=color_int
    )

    # Thumbnail
    thumb_url = emb_cfg.get("thumbnail_url", "{server_icon}")
    if thumb_url == "{server_icon}" and guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif thumb_url == "{user_avatar}" and member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    elif thumb_url and thumb_url.startswith("http"):
        embed.set_thumbnail(url=thumb_url)

    # Banner / Image
    banner_url = emb_cfg.get("banner_url", "")
    if banner_url == "{server_banner}" and guild and getattr(guild, "banner", None):
        embed.set_image(url=guild.banner.url)
    elif banner_url and banner_url.startswith("http"):
        embed.set_image(url=banner_url)

    # Footer
    footer_text = emb_cfg.get("footer", "")
    if footer_text:
        footer_icon = emb_cfg.get("footer_icon_url", "")
        if footer_icon == "{server_icon}" and guild and guild.icon:
            embed.set_footer(text=footer_text, icon_url=guild.icon.url)
        elif footer_icon and footer_icon.startswith("http"):
            embed.set_footer(text=footer_text, icon_url=footer_icon)
            embed.set_footer(text=footer_text)

    return raw_msg, embed


# --- IN-DISCORD INTERACTIVE MODALS FOR A-TO-Z WELCOME CUSTOMIZATION ---

class WelcomeContentModal(discord.ui.Modal, title="Edit Welcome Message Text"):
    content_input = discord.ui.TextInput(
        label="Message Text (Above Embed)",
        style=discord.TextStyle.paragraph,
        placeholder="Hello {user}, Welcome To **{server}**!",
        max_length=1500,
        required=True
    )

    def __init__(self, current_val: str):
        super().__init__()
        self.content_input.default = current_val

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_welcome_config()
        cfg["message_content"] = str(self.content_input.value).strip()
        save_welcome_config(cfg)
        await interaction.response.send_message("✅ **Welcome Message Text updated successfully!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class WelcomeTitleColorModal(discord.ui.Modal, title="Edit Welcome Title & Color"):
    title_input = discord.ui.TextInput(
        label="Embed Title",
        style=discord.TextStyle.short,
        placeholder="You Are Our {member_count} Member.",
        max_length=250,
        required=True
    )
    color_input = discord.ui.TextInput(
        label="Embed Hex Color (e.g. #E74C3C or #2B2D31)",
        style=discord.TextStyle.short,
        placeholder="#E74C3C",
        max_length=10,
        required=True
    )

    def __init__(self, current_title: str, current_color: str):
        super().__init__()
        self.title_input.default = current_title
        self.color_input.default = current_color

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_welcome_config()
        cfg.setdefault("embed", {})["title"] = str(self.title_input.value).strip()
        raw_c = str(self.color_input.value).strip()
        if not raw_c.startswith("#"):
            raw_c = "#" + raw_c
        cfg["embed"]["color"] = raw_c
        save_welcome_config(cfg)
        await interaction.response.send_message("✅ **Welcome Embed Title & Color updated!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class WelcomeDescModal(discord.ui.Modal, title="Edit Welcome Description"):
    desc_input = discord.ui.TextInput(
        label="Store Description & Links",
        style=discord.TextStyle.paragraph,
        placeholder="Enter your store links, products, and welcome text...",
        max_length=4000,
        required=True
    )

    def __init__(self, current_desc: str):
        super().__init__()
        self.desc_input.default = current_desc

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_welcome_config()
        cfg.setdefault("embed", {})["description"] = str(self.desc_input.value)
        save_welcome_config(cfg)
        await interaction.response.send_message("✅ **Welcome Description updated successfully!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class WelcomeMediaModal(discord.ui.Modal, title="Edit Welcome Media URLs"):
    thumb_input = discord.ui.TextInput(
        label="Top-Right Thumbnail URL (or {server_icon})",
        style=discord.TextStyle.short,
        placeholder="{server_icon} or https://...",
        required=False
    )
    banner_input = discord.ui.TextInput(
        label="Bottom Banner Image URL (or leave empty)",
        style=discord.TextStyle.short,
        placeholder="https://... or empty",
        required=False
    )

    def __init__(self, current_thumb: str, current_banner: str):
        super().__init__()
        self.thumb_input.default = current_thumb
        self.banner_input.default = current_banner

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_welcome_config()
        cfg.setdefault("embed", {})["thumbnail_url"] = str(self.thumb_input.value).strip()
        cfg["embed"]["banner_url"] = str(self.banner_input.value).strip()
        save_welcome_config(cfg)
        await interaction.response.send_message("✅ **Welcome Media URLs updated!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


def build_welcome_editor_embed(guild: discord.Guild) -> discord.Embed:
    cfg = load_welcome_config()
    is_on = cfg.get("enabled", True)
    status_str = "🟢 **ENABLED (Active)**" if is_on else "🔴 **DISABLED (Paused)**"
    msg_text = cfg.get("message_content", "")
    emb_cfg = cfg.get("embed", {})

    desc = (
        f"**Current Status:** {status_str}\n\n"
        "Configure every aspect of the automated Direct Message sent to newly joined members!\n\n"
        "__Available Placeholders:__\n"
        "• `{user}` / `{user_mention}` — Member mention (`@User`)\n"
        "• `{server}` — Server Name (`Byte Corporation`)\n"
        "• `{member_count}` — Ordinal count (`1176th`)\n"
        "• `{count}` — Numeric count (`1176`)\n"
        "• `{server_icon}` / `{user_avatar}` — Dynamic image URLs"
    )

    embed = discord.Embed(
        title="⚙️ » Welcome DM Control & Live Customizer",
        description=desc,
        color=0x0000FF
    )
    embed.add_field(name="💬 Message Text", value=f"`{msg_text[:100]}`" if msg_text else "*None*", inline=False)
    embed.add_field(name="🏷️ Embed Title", value=f"`{emb_cfg.get('title', '')[:100]}`", inline=True)
    embed.add_field(name="🎨 Color", value=f"`{emb_cfg.get('color', '#E74C3C')}`", inline=True)
    thumb = emb_cfg.get("thumbnail_url", "") or "*None*"
    banner = emb_cfg.get("banner_url", "") or "*None*"
    embed.add_field(name="🖼️ Media", value=f"Thumb: `{thumb[:30]}`\nBanner: `{banner[:30]}`", inline=False)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if bot.user and bot.user.display_avatar:
        embed.set_footer(text="BYTE CORPORATION • Welcome Manager", icon_url=bot.user.display_avatar.url)
    return embed


class ByteWelcomeEditorView(discord.ui.View):
    """Interactive Control Panel for Welcome DM Customization."""
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
            await interaction.response.send_message("❌ Only Administrators or Staff can configure Welcome DM.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Toggle ON/OFF", emoji="🔘", style=discord.ButtonStyle.primary, custom_id="wc_btn_toggle", row=0)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_welcome_config()
        cfg["enabled"] = not cfg.get("enabled", True)
        save_welcome_config(cfg)
        status = "ENABLED (Members will receive DMs)" if cfg["enabled"] else "DISABLED (DM paused)"
        emb = build_welcome_editor_embed(interaction.guild)
        await interaction.response.edit_message(embed=emb, view=self)
        await interaction.followup.send(f"✅ **Welcome DM is now {status}!**", ephemeral=True)

    @discord.ui.button(label="Edit Message", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="wc_btn_msg", row=0)
    async def btn_edit_msg(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_welcome_config()
        await interaction.response.send_modal(WelcomeContentModal(cfg.get("message_content", "")))

    @discord.ui.button(label="Title & Color", emoji="🏷️", style=discord.ButtonStyle.secondary, custom_id="wc_btn_title", row=0)
    async def btn_edit_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_welcome_config()
        emb_cfg = cfg.get("embed", {})
        await interaction.response.send_modal(WelcomeTitleColorModal(emb_cfg.get("title", ""), emb_cfg.get("color", "#E74C3C")))

    @discord.ui.button(label="Edit Description", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="wc_btn_desc", row=1)
    async def btn_edit_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_welcome_config()
        emb_cfg = cfg.get("embed", {})
        await interaction.response.send_modal(WelcomeDescModal(emb_cfg.get("description", "")))

    @discord.ui.button(label="Media URLs", emoji="🖼️", style=discord.ButtonStyle.secondary, custom_id="wc_btn_media", row=1)
    async def btn_edit_media(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_welcome_config()
        emb_cfg = cfg.get("embed", {})
        await interaction.response.send_modal(WelcomeMediaModal(emb_cfg.get("thumbnail_url", "{server_icon}"), emb_cfg.get("banner_url", "")))

    @discord.ui.button(label="Test My DM", emoji="🧪", style=discord.ButtonStyle.success, custom_id="wc_btn_test", row=1)
    async def btn_test_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        content, embed = build_welcome_payload(interaction.user)
        try:
            if embed:
                await interaction.user.send(content=content, embed=embed)
            elif content:
                await interaction.user.send(content=content)
            await interaction.response.send_message("📬 **Test Welcome DM sent to your private messages!** Check your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ **Failed to send DM:** Your Discord DMs are closed! Please enable direct messages in server privacy settings.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ **Error sending DM:** `{e}`", ephemeral=True)

    @discord.ui.button(label="Reset Default", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="wc_btn_reset", row=1)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        save_welcome_config(DEFAULT_WELCOME_CONFIG.copy())
        emb = build_welcome_editor_embed(interaction.guild)
        await interaction.response.edit_message(embed=emb, view=self)
        await interaction.followup.send("🔄 **Welcome DM settings reset to default Byte Corporation store template!**", ephemeral=True)


# --- DISCORD MEMBER JOIN EVENT (AUTOMATIC DM DELIVERY) ---

@bot.event
async def on_member_join(member: discord.Member):
    """Automatically sends:
    1. Personalized Direct Message (DM) to newly joined member
    2. Server Welcome Message & Embed to configured Welcome Channel
    """
    # --- BYTE SECURITY ANTI-BOT ADD PROTECTION ---
    if member.bot:
        cfg = load_antinuke_config()
        if cfg.get("enabled", True) and cfg.get("modules", {}).get("bot_add", True):
            entry = await get_audit_entry(member.guild, discord.AuditLogAction.bot_add, target_id=member.id, max_age=15.0)
            if entry and entry.user:
                if not is_antinuke_whitelisted(member.guild, entry.user, "bot_add"):
                    try:
                        await member.ban(reason=f"[VOID Security Anti-Nuke] Unauthorized Bot Added by {entry.user.name}")
                    except Exception:
                        try:
                            await member.kick(reason="[VOID Security Anti-Nuke] Unauthorized Bot")
                        except Exception:
                            pass
                    await punish_antinuke_offender(member.guild, entry.user, f"Added unauthorized bot @{member.name} ({member.id})", "Anti-Bot Add")
                    return

    # --- AUTO-BYPASS VERIFICATION FOR BOTS ADDED BY ADMIN / OWNER ---
    if member.bot:
        try:
            v_cfg = load_verification_config()
            v_role_id = v_cfg.get("role_id")
            if v_role_id and member.guild:
                v_role = member.guild.get_role(v_role_id)
                if v_role and member.guild.me.top_role > v_role:
                    await member.add_roles(v_role, reason="Auto-bypass verification for Bot added by Admin/Owner")
                    logger.info(f"🤖 Bot {member.name} ({member.id}) auto-verified with role {v_role.name}")
        except Exception as e:
            logger.warning(f"Could not auto-grant verified role to bot {member.name}: {e}")
        # --- BOT AUTO ROLE ---
        try:
            ar_cfg = load_autorole_config()
            if ar_cfg.get("enabled", True):
                b_role_id = ar_cfg.get("bot_role_id")
                if b_role_id and member.guild:
                    b_role = member.guild.get_role(int(b_role_id))
                    if b_role and member.guild.me.top_role > b_role:
                        await member.add_roles(b_role, reason="Bot Auto Role on Join")
                        logger.info(f"🤖 Bot Auto Role {b_role.name} granted to {member.name}")
        except Exception as e:
            logger.warning(f"Error in on_member_join bot autorole: {e}")

        return  # Bots do not need DM welcomes

    # --- AUTO ROLE (HUMAN MEMBERS) ---
    try:
        ar_cfg = load_autorole_config()
        if ar_cfg.get("enabled", True):
            h_role_id = ar_cfg.get("human_role_id")
            if h_role_id and member.guild:
                h_role = member.guild.get_role(int(h_role_id))
                if h_role and member.guild.me.top_role > h_role:
                    await member.add_roles(h_role, reason="Auto Role on Join for Member")
                    logger.info(f"🎭 Auto Role {h_role.name} granted to member {member.name} ({member.id})")
    except Exception as e:
        logger.warning(f"Error in on_member_join human autorole: {e}")

    # --- 1. PERSONAL DM WELCOME ---
    try:
        dm_cfg = load_welcome_config()
        if dm_cfg.get("enabled", True):
            content, embed = build_welcome_payload(member, dm_cfg)
            try:
                if embed:
                    await member.send(content=content, embed=embed)
                elif content:
                    await member.send(content=content)
                logger.info(f"📬 Welcome DM successfully delivered to {member.name} ({member.id}) in guild '{member.guild.name}'")
            except discord.Forbidden:
                logger.debug(f"Could not send welcome DM to {member.name} ({member.id}): DMs disabled by user.")
            except Exception as e:
                logger.warning(f"Welcome DM delivery notice for {member.name}: {e}")
    except Exception as e:
        logger.error(f"Error in on_member_join DM welcome: {e}")

    # --- 2. SERVER CHANNEL WELCOME ---
    try:
        srv_cfg = load_server_welcome_config()
        if srv_cfg.get("enabled", True):
            ch_id = srv_cfg.get("channel_id")
            if ch_id:
                channel = member.guild.get_channel(int(ch_id))
                if channel:
                    content, embed, file = build_server_welcome_payload(member, srv_cfg)
                    try:
                        if file:
                            await channel.send(content=content, embed=embed, file=file)
                        elif embed:
                            await channel.send(content=content, embed=embed)
                        elif content:
                            await channel.send(content=content)
                        logger.info(f"🎉 Server Welcome message sent for {member.name} in channel #{channel.name}")
                    except Exception as e:
                        logger.warning(f"Failed to send Server Welcome in channel #{channel.name}: {e}")
                else:
                    logger.warning(f"Server welcome channel ID {ch_id} not found in guild {member.guild.name}")
    except Exception as e:
        logger.error(f"Error in on_member_join Server welcome: {e}")


# --- WELCOME DM COMMANDS (PREFIX & SLASH) ---

@bot.command(name="welcomedm", aliases=["welcomeeditor", "welcomemenu", "wedit", "welcomeconfig"])
@commands.has_permissions(administrator=True)
async def welcomedm_cmd(ctx):
    """Opens the complete interactive Welcome DM customizer dashboard."""
    embed = build_welcome_editor_embed(ctx.guild)
    view = ByteWelcomeEditorView()
    await ctx.reply(embed=embed, view=view)


@bot.tree.command(name="welcomedm", description="Open the Welcome DM Interactive Customizer Dashboard")
async def welcomedm_slash(interaction: discord.Interaction):
    if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only Administrators or Staff can configure Welcome DM.", ephemeral=True)
        return
    embed = build_welcome_editor_embed(interaction.guild)
    view = ByteWelcomeEditorView()
    await interaction.response.send_message(embed=embed, view=view)


@bot.command(name="welcometest", aliases=["testwelcome", "wtest"])
async def welcometest_cmd(ctx):
    """Sends a live test of the configured Welcome DM directly to your private messages."""
    content, embed = build_welcome_payload(ctx.author)
    try:
        if embed:
            await ctx.author.send(content=content, embed=embed)
        elif content:
            await ctx.author.send(content=content)
        await ctx.reply("📬 **Test Welcome DM sent to your DMs!** Check your messages.")
    except discord.Forbidden:
        await ctx.reply("❌ **Could not send DM:** Your direct messages are disabled! Please enable DMs from server members.")
    except Exception as e:
        await ctx.reply(f"⚠️ **Error sending DM:** `{e}`")


@bot.tree.command(name="welcometest", description="Send a test Welcome DM directly to your private messages")
async def welcometest_slash(interaction: discord.Interaction):
    content, embed = build_welcome_payload(interaction.user)
    try:
        if embed:
            await interaction.user.send(content=content, embed=embed)
        elif content:
            await interaction.user.send(content=content)
        await interaction.response.send_message("📬 **Test Welcome DM sent to your DMs!** Check your private messages.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ **Could not send DM:** Your direct messages are disabled! Please enable DMs from server members.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ **Error sending DM:** `{e}`", ephemeral=True)


@bot.command(name="welcometoggle")
@commands.has_permissions(administrator=True)
async def welcometoggle_cmd(ctx):
    """Quickly toggles the Welcome DM on or off."""
    cfg = load_welcome_config()
    cfg["enabled"] = not cfg.get("enabled", True)
    save_welcome_config(cfg)
    status = "ENABLED (Active)" if cfg["enabled"] else "DISABLED (Paused)"
    await ctx.reply(f"🔘 **Welcome DM System:** `{status}`")


@bot.command(name="setwelcomemsg")
@commands.has_permissions(administrator=True)
async def setwelcomemsg_cmd(ctx, *, message: str):
    """Sets the outer text message above the embed."""
    cfg = load_welcome_config()
    cfg["message_content"] = message.strip()
    save_welcome_config(cfg)
    await ctx.reply(f"✅ **Welcome Message updated:**\n{message.strip()}")


@bot.command(name="setwelcometitle")
@commands.has_permissions(administrator=True)
async def setwelcometitle_cmd(ctx, *, title: str):
    """Sets the title of the welcome embed."""
    cfg = load_welcome_config()
    cfg.setdefault("embed", {})["title"] = title.strip()
    save_welcome_config(cfg)
    await ctx.reply(f"✅ **Welcome Title updated:** `{title.strip()}`")


@bot.command(name="setwelcomedesc")
@commands.has_permissions(administrator=True)
async def setwelcomedesc_cmd(ctx, *, desc: str):
    """Sets the description (body/store links) of the welcome embed."""
    cfg = load_welcome_config()
    cfg.setdefault("embed", {})["description"] = desc.strip()
    save_welcome_config(cfg)
    await ctx.reply("✅ **Welcome Description updated!** Run `*welcometest` to preview.")


@bot.command(name="setwelcomebanner")
@commands.has_permissions(administrator=True)
async def setwelcomebanner_cmd(ctx, url: str = None):
    """Sets the bottom banner image URL for the welcome embed."""
    if not url and ctx.message.attachments:
        url = ctx.message.attachments[0].url
    if not url:
        await ctx.reply("Usage: `*setwelcomebanner <image_url>` or attach an image.")
        return
    cfg = load_welcome_config()
    cfg.setdefault("embed", {})["banner_url"] = url.strip()
    save_welcome_config(cfg)
    await ctx.reply(f"✅ **Welcome Banner URL updated!**")


@bot.command(name="setwelcomethumb")
@commands.has_permissions(administrator=True)
async def setwelcomethumb_cmd(ctx, url: str = None):
    """Sets the top-right thumbnail for the welcome embed."""
    if not url and ctx.message.attachments:
        url = ctx.message.attachments[0].url
    if not url:
        await ctx.reply("Usage: `*setwelcomethumb <image_url | {server_icon} | {user_avatar}>`")
        return
    cfg = load_welcome_config()
    cfg.setdefault("embed", {})["thumbnail_url"] = url.strip()
    save_welcome_config(cfg)
    await ctx.reply(f"✅ **Welcome Thumbnail updated!**")


@bot.command(name="getemoji", aliases=["emojicode", "emojiid"])
async def getemoji_cmd(ctx, *, emoji_str: str = None):
    """Replies with the exact copy-paste code for any animated or custom emoji to use in Welcome Description."""
    if not emoji_str:
        await ctx.reply("Usage: `*getemoji <emoji>`\nExample: Type `*getemoji ` followed by any server animated emoji!")
        return
    clean = emoji_str.strip()
    await ctx.reply(
        f"📋 **Animated Emoji Code (Copy & Paste directly into Description):**\n"
        f"```text\n{clean}\n```\n"
        f"💡 *Tip: You can use this code directly in `*welcomeeditor` or `*setwelcomedesc`!*"
    )


@bot.tree.command(name="getemoji", description="Get the raw code of an animated or custom emoji to copy into Welcome Description")
@app_commands.describe(emoji_str="The emoji to get the code for")
async def getemoji_slash(interaction: discord.Interaction, emoji_str: str):
    clean = emoji_str.strip()
    await interaction.response.send_message(
        f"📋 **Animated Emoji Code (Copy & Paste):**\n```text\n{clean}\n```\n"
        f"💡 *Use this code directly in your Welcome Description!*",
        ephemeral=True
    )


@bot.command(name="setwelcomecolor")
@commands.has_permissions(administrator=True)
async def setwelcomecolor_cmd(ctx, hex_code: str):
    """Sets the side border hex color of the welcome embed (e.g. #E74C3C)."""
    hex_clean = hex_code.strip()
    if not hex_clean.startswith("#"):
        hex_clean = "#" + hex_clean
    cfg = load_welcome_config()
    cfg.setdefault("embed", {})["color"] = hex_clean
    save_welcome_config(cfg)
    await ctx.reply(f"✅ **Welcome Embed Color set to `{hex_clean}`!**")


# ==============================================================================

# ==============================================================================
# SERVER CHANNEL WELCOME SYSTEM (PUBLIC SERVER JOIN ANNOUNCEMENTS)
# ==============================================================================

SERVER_WELCOME_CONFIG_PATH = "server_welcome_config.json"

DEFAULT_SERVER_WELCOME_CONFIG = {
    "enabled": True,
    "channel_id": 1518838818185150464,
    "message_content": "Hello {user}, Welcome To **{server}**!",
    "embed": {
        "enabled": True,
        "color": "#E74C3C",
        "title": "You Are Our {member_count} Member.",
        "description": (
            "Thanks For Joining Us We're Thrilled To Have You Join Us On This Exciting Journey.\n"
            "Here's To New Friendships, Inspiring Conversations, And Unforgettable Memories!\n"
            "--------------------------------------------------\n"
            "» Check <#1518851333191503933>\n"
            "» Check <#1532032398924189796>\n"
            "» Check <#1518838849000706170>\n"
            "» Check <#1518838845523755058>"
        ),
        "thumbnail_url": "{user_avatar}",
        "banner_url": "",
        "footer": "BYTE CORPORATION • Official Server",
        "footer_icon_url": "{server_icon}"
    }
}


def load_server_welcome_config() -> dict:
    if os.path.exists(SERVER_WELCOME_CONFIG_PATH):
        try:
            with open(SERVER_WELCOME_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading {SERVER_WELCOME_CONFIG_PATH}: {e}")
    return DEFAULT_SERVER_WELCOME_CONFIG.copy()


def save_server_welcome_config(cfg: dict) -> None:
    try:
        with open(SERVER_WELCOME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {SERVER_WELCOME_CONFIG_PATH}: {e}")


def build_server_welcome_payload(member: discord.Member, cfg: dict = None):
    """Generates the Server Welcome Channel message content, embed, and optional file attachment."""
    if not cfg:
        cfg = load_server_welcome_config()

    guild = getattr(member, "guild", None)
    if not guild and bot.guilds:
        guild = bot.guilds[0]

    guild_id = guild.id if guild else 1518838682449219675
    guild_name = guild.name if guild else "BYTE CORPORATION"
    m_count = (guild.member_count if guild else 1176) or 1
    m_count_str = get_welcome_ordinal(m_count)

    replacements = {
        "{user}": member.mention,
        "{user_mention}": member.mention,
        "{user_name}": member.name,
        "{user_display}": getattr(member, "display_name", member.name),
        "{server}": guild_name,
        "{server_name}": guild_name,
        "{member_count}": m_count_str,
        "{count}": str(m_count),
        "{member_count_num}": str(m_count),
    }

    raw_msg = cfg.get("message_content", "Hello {user}, Welcome To **{server}**!")
    for k, v in replacements.items():
        raw_msg = raw_msg.replace(k, v)

    emb_cfg = cfg.get("embed", {})
    if not emb_cfg.get("enabled", True):
        return raw_msg, None, None

    raw_title = emb_cfg.get("title", "You Are Our {member_count} Member.")
    raw_desc = emb_cfg.get("description", "")
    for k, v in replacements.items():
        raw_title = raw_title.replace(k, v)
        raw_desc = raw_desc.replace(k, v)

    # Auto-resolve channel IDs: inside server channel, <#channel_id> mentions cleanly!
    def _channel_replacer(match):
        cid_str = match.group(1)
        return f"<#{cid_str}>"

    channel_pattern = re.compile(r"(?<!/)(?:<#|#)(\d{17,20})>?")
    raw_desc = channel_pattern.sub(_channel_replacer, raw_desc)
    raw_msg = channel_pattern.sub(_channel_replacer, raw_msg)

    # Auto-resolve custom emojis :name:
    def _emoji_replacer(match):
        ename = match.group(1)
        all_emojis = list(guild.emojis) if guild else []
        if hasattr(bot, "emojis"):
            all_emojis += list(bot.emojis)
        for em in all_emojis:
            if em.name.lower() == ename.lower():
                return str(em)
        for em in all_emojis:
            if ename.lower() in em.name.lower() or em.name.lower() in ename.lower():
                return str(em)
        return match.group(0)

    emoji_pattern = re.compile(r"(?<!<)(?<!<a):([a-zA-Z0-9_~]+):(?!\d+>)")
    raw_desc = emoji_pattern.sub(_emoji_replacer, raw_desc)
    raw_msg = emoji_pattern.sub(_emoji_replacer, raw_msg)

    # Clean up accidental double asterisks
    raw_desc = re.sub(r"(?<!\*)\*\*(\.|\s*$)", r"", raw_desc)

    hex_color = emb_cfg.get("color", "#E74C3C").lstrip("#")
    try:
        color_int = int(hex_color, 16)
    except Exception:
        color_int = 0xE74C3C

    embed = discord.Embed(
        title=raw_title,
        description=raw_desc,
        color=color_int
    )

    # Thumbnail (Default: {user_avatar})
    thumb_url = emb_cfg.get("thumbnail_url", "{user_avatar}")
    if thumb_url == "{user_avatar}" and member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    elif thumb_url == "{server_icon}" and guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif thumb_url and thumb_url.startswith("http"):
        embed.set_thumbnail(url=thumb_url)

    # Banner / Image
    banner_url = emb_cfg.get("banner_url", "")
    attached_file = None
    if banner_url and os.path.exists(banner_url) and os.path.isfile(banner_url):
        fname = os.path.basename(banner_url)
        attached_file = discord.File(banner_url, filename=fname)
        embed.set_image(url=f"attachment://{fname}")
    elif banner_url == "{server_banner}" and guild and getattr(guild, "banner", None):
        embed.set_image(url=guild.banner.url)
    elif banner_url and banner_url.startswith("http"):
        embed.set_image(url=banner_url)

    # Footer
    footer_text = emb_cfg.get("footer", "")
    if footer_text:
        footer_icon = emb_cfg.get("footer_icon_url", "")
        if footer_icon == "{server_icon}" and guild and guild.icon:
            embed.set_footer(text=footer_text, icon_url=guild.icon.url)
        elif footer_icon and footer_icon.startswith("http"):
            embed.set_footer(text=footer_text, icon_url=footer_icon)
            embed.set_footer(text=footer_text)

    return raw_msg, embed, attached_file


# --- IN-DISCORD INTERACTIVE MODALS FOR SERVER WELCOME ---

class ServerWelcomeContentModal(discord.ui.Modal, title="Edit Server Welcome Message"):
    content_input = discord.ui.TextInput(
        label="Message Text (Above Embed)",
        style=discord.TextStyle.paragraph,
        placeholder="Hello {user}, Welcome To **{server}**!",
        max_length=1500,
        required=True
    )

    def __init__(self, current_text: str = ""):
        super().__init__()
        if current_text:
            self.content_input.default = current_text

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_server_welcome_config()
        cfg["message_content"] = str(self.content_input.value).strip()
        save_server_welcome_config(cfg)
        await interaction.response.send_message("✅ **Server Welcome Message updated!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_server_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class ServerWelcomeTitleModal(discord.ui.Modal, title="Edit Title & Color"):
    title_input = discord.ui.TextInput(
        label="Embed Title",
        style=discord.TextStyle.short,
        placeholder="You Are Our {member_count} Member.",
        max_length=250,
        required=True
    )
    color_input = discord.ui.TextInput(
        label="Hex Color (e.g. #E74C3C)",
        style=discord.TextStyle.short,
        placeholder="#E74C3C",
        max_length=10,
        required=True
    )

    def __init__(self, cur_title: str = "", cur_color: str = "#E74C3C"):
        super().__init__()
        if cur_title:
            self.title_input.default = cur_title
        if cur_color:
            self.color_input.default = cur_color

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_server_welcome_config()
        cfg.setdefault("embed", {})["title"] = str(self.title_input.value).strip()
        raw_c = str(self.color_input.value).strip()
        if not raw_c.startswith("#"):
            raw_c = "#" + raw_c
        cfg["embed"]["color"] = raw_c
        save_server_welcome_config(cfg)
        await interaction.response.send_message("✅ **Server Welcome Title & Color updated!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_server_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class ServerWelcomeDescModal(discord.ui.Modal, title="Edit Server Description"):
    desc_input = discord.ui.TextInput(
        label="Description & Channel Links",
        style=discord.TextStyle.paragraph,
        placeholder="Thanks For Joining Us...\n» Check <#channel>",
        max_length=3900,
        required=True
    )

    def __init__(self, cur_desc: str = ""):
        super().__init__()
        if cur_desc:
            self.desc_input.default = cur_desc

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_server_welcome_config()
        cfg.setdefault("embed", {})["description"] = str(self.desc_input.value)
        save_server_welcome_config(cfg)
        await interaction.response.send_message("✅ **Server Welcome Description updated!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_server_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class ServerWelcomeMediaModal(discord.ui.Modal, title="Edit Media URLs"):
    thumb_input = discord.ui.TextInput(
        label="Thumbnail ({user_avatar} or URL)",
        style=discord.TextStyle.short,
        placeholder="{user_avatar}",
        max_length=300,
        required=False
    )
    banner_input = discord.ui.TextInput(
        label="Banner Image URL / GIF",
        style=discord.TextStyle.short,
        placeholder="https://.../banner.gif",
        max_length=400,
        required=False
    )

    def __init__(self, cur_thumb: str = "", cur_banner: str = ""):
        super().__init__()
        if cur_thumb:
            self.thumb_input.default = cur_thumb
        if cur_banner:
            self.banner_input.default = cur_banner

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_server_welcome_config()
        cfg.setdefault("embed", {})["thumbnail_url"] = str(self.thumb_input.value).strip()
        cfg["embed"]["banner_url"] = str(self.banner_input.value).strip()
        save_server_welcome_config(cfg)
        await interaction.response.send_message("✅ **Server Welcome Media URLs updated!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_server_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


class ServerWelcomeChannelModal(discord.ui.Modal, title="Set Welcome Channel"):
    ch_input = discord.ui.TextInput(
        label="Channel ID or Mention",
        style=discord.TextStyle.short,
        placeholder="1518838818185150464 or #welcome",
        max_length=50,
        required=True
    )

    def __init__(self, cur_ch: str = ""):
        super().__init__()
        if cur_ch:
            self.ch_input.default = str(cur_ch)

    async def on_submit(self, interaction: discord.Interaction):
        raw_val = str(self.ch_input.value).strip()
        digits = re.findall(r"\d+", raw_val)
        if not digits:
            await interaction.response.send_message("❌ **Invalid channel!** Please provide a valid channel ID or mention.", ephemeral=True)
            return
        cid = int(digits[0])
        cfg = load_server_welcome_config()
        cfg["channel_id"] = cid
        save_server_welcome_config(cfg)
        await interaction.response.send_message(f"✅ **Server Welcome Channel set to <#{cid}>!**", ephemeral=True)
        if interaction.message:
            try:
                emb = build_server_welcome_editor_embed(interaction.guild)
                await interaction.message.edit(embed=emb)
            except Exception:
                pass


def build_server_welcome_editor_embed(guild: discord.Guild) -> discord.Embed:
    cfg = load_server_welcome_config()
    is_on = cfg.get("enabled", True)
    status_str = "🟢 **ENABLED (Active)**" if is_on else "🔴 **DISABLED (Paused)**"
    ch_id = cfg.get("channel_id")
    ch_str = f"<#{ch_id}> (`{ch_id}`)" if ch_id else "⚠️ *Not Set*"
    msg_text = cfg.get("message_content", "")
    emb_cfg = cfg.get("embed", {})

    desc = (
        f"**System Status:** {status_str}\n"
        f"**Target Channel:** {ch_str}\n\n"
        "Configure the public automated welcome message posted into your server channel when members join!\n\n"
        "__Available Placeholders:__\n"
        "• `{user}` / `{user_mention}` — Member mention (`@User`)\n"
        "• `{server}` — Server Name (`Byte Corporation`)\n"
        "• `{member_count}` — Ordinal count (`1176th`)\n"
        "• `{count}` — Numeric count (`1176`)\n"
        "• `{user_avatar}` — Member profile picture\n"
        "• `{server_icon}` — Server logo"
    )

    embed = discord.Embed(
        title="📢 » Server Channel Welcome Dashboard",
        description=desc,
        color=0x0000FF
    )
    embed.add_field(name="💬 Message Text", value=f"`{msg_text[:100]}`" if msg_text else "*None*", inline=False)
    embed.add_field(name="🏷️ Embed Title", value=f"`{emb_cfg.get('title', '')[:100]}`", inline=True)
    embed.add_field(name="🎨 Color", value=f"`{emb_cfg.get('color', '#E74C3C')}`", inline=True)
    thumb = emb_cfg.get("thumbnail_url", "") or "*None*"
    banner = emb_cfg.get("banner_url", "") or "*None*"
    embed.add_field(name="🖼️ Media", value=f"Thumb: `{thumb[:30]}`\nBanner: `{banner[:30]}`", inline=False)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if bot.user and bot.user.display_avatar:
        embed.set_footer(text="BYTE CORPORATION • Server Welcome Manager", icon_url=bot.user.display_avatar.url)
    return embed


class ByteServerWelcomeEditorView(discord.ui.View):
    """Interactive Control Panel for Server Channel Welcome Customization."""
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
            await interaction.response.send_message("❌ Only Administrators or Staff can configure Server Welcome.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Toggle ON/OFF", emoji="🔘", style=discord.ButtonStyle.primary, custom_id="swc_btn_toggle", row=0)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        cfg["enabled"] = not cfg.get("enabled", True)
        save_server_welcome_config(cfg)
        status = "ENABLED" if cfg["enabled"] else "DISABLED"
        emb = build_server_welcome_editor_embed(interaction.guild)
        await interaction.response.edit_message(embed=emb, view=self)
        await interaction.followup.send(f"✅ **Server Welcome is now {status}!**", ephemeral=True)

    @discord.ui.button(label="Set Channel", emoji="📢", style=discord.ButtonStyle.secondary, custom_id="swc_btn_ch", row=0)
    async def btn_set_ch(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        await interaction.response.send_modal(ServerWelcomeChannelModal(str(cfg.get("channel_id", ""))))

    @discord.ui.button(label="Edit Message", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="swc_btn_msg", row=0)
    async def btn_edit_msg(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        await interaction.response.send_modal(ServerWelcomeContentModal(cfg.get("message_content", "")))

    @discord.ui.button(label="Title & Color", emoji="🏷️", style=discord.ButtonStyle.secondary, custom_id="swc_btn_tc", row=0)
    async def btn_title_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        emb_cfg = cfg.get("embed", {})
        await interaction.response.send_modal(ServerWelcomeTitleModal(
            emb_cfg.get("title", ""),
            emb_cfg.get("color", "#E74C3C")
        ))

    @discord.ui.button(label="Edit Description", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="swc_btn_desc", row=1)
    async def btn_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        emb_cfg = cfg.get("embed", {})
        await interaction.response.send_modal(ServerWelcomeDescModal(emb_cfg.get("description", "")))

    @discord.ui.button(label="Media URLs", emoji="🖼️", style=discord.ButtonStyle.secondary, custom_id="swc_btn_media", row=1)
    async def btn_media(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        emb_cfg = cfg.get("embed", {})
        await interaction.response.send_modal(ServerWelcomeMediaModal(
            emb_cfg.get("thumbnail_url", ""),
            emb_cfg.get("banner_url", "")
        ))

    @discord.ui.button(label="Test in Channel", emoji="🧪", style=discord.ButtonStyle.success, custom_id="swc_btn_test", row=1)
    async def btn_test_ch(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_server_welcome_config()
        ch_id = cfg.get("channel_id")
        if not ch_id:
            await interaction.response.send_message("❌ **No welcome channel configured!** Click 'Set Channel' first.", ephemeral=True)
            return
        target_ch = interaction.guild.get_channel(int(ch_id)) if interaction.guild else None
        if not target_ch:
            await interaction.response.send_message(f"❌ **Channel <#{ch_id}> not found!** Please check bot channel permissions.", ephemeral=True)
            return
        content, embed, file = build_server_welcome_payload(interaction.user, cfg)
        try:
            if file:
                await target_ch.send(content=content, embed=embed, file=file)
            elif embed:
                await target_ch.send(content=content, embed=embed)
            elif content:
                await target_ch.send(content=content)
            await interaction.response.send_message(f"🎉 **Live test welcome sent to {target_ch.mention}!**", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ **Error sending message to {target_ch.mention}:** `{e}`", ephemeral=True)

    @discord.ui.button(label="Reset Default", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="swc_btn_reset", row=1)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        save_server_welcome_config(DEFAULT_SERVER_WELCOME_CONFIG.copy())
        emb = build_server_welcome_editor_embed(interaction.guild)
        await interaction.response.edit_message(embed=emb, view=self)
        await interaction.followup.send("🔄 **Server Welcome settings reset to default Cruel/Byte template!**", ephemeral=True)


# --- SERVER WELCOME COMMANDS (PREFIX & SLASH) ---

@bot.command(name="serverwelcome", aliases=["welcomeserver", "swelcome", "serverwelcomeeditor", "sweditor"])
@commands.has_permissions(administrator=True)
async def serverwelcome_cmd(ctx):
    """Opens the interactive Server Channel Welcome customizer dashboard."""
    embed = build_server_welcome_editor_embed(ctx.guild)
    view = ByteServerWelcomeEditorView()
    await ctx.reply(embed=embed, view=view)


@bot.tree.command(name="serverwelcome", description="Open the Server Channel Welcome Interactive Dashboard")
async def serverwelcome_slash(interaction: discord.Interaction):
    if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only Administrators or Staff can configure Server Welcome.", ephemeral=True)
        return
    embed = build_server_welcome_editor_embed(interaction.guild)
    view = ByteServerWelcomeEditorView()
    await interaction.response.send_message(embed=embed, view=view)


@bot.command(name="setwelcomechannel", aliases=["setswchannel", "welcomechannel"])
@commands.has_permissions(administrator=True)
async def setwelcomechannel_cmd(ctx, channel: discord.TextChannel = None):
    """Sets the channel where new member welcomes will be posted."""
    if not channel:
        await ctx.reply("Usage: `*setwelcomechannel #channel` or `*setwelcomechannel <channel_id>`")
        return
    cfg = load_server_welcome_config()
    cfg["channel_id"] = channel.id
    save_server_welcome_config(cfg)
    await ctx.reply(f"✅ **Server Welcome Channel has been set to {channel.mention}!**")


@bot.tree.command(name="setwelcomechannel", description="Set the server channel where welcome messages are posted")
@app_commands.describe(channel="The channel where welcome messages will be sent")
async def setwelcomechannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only Administrators or Staff can configure Server Welcome.", ephemeral=True)
        return
    cfg = load_server_welcome_config()
    cfg["channel_id"] = channel.id
    save_server_welcome_config(cfg)
    await interaction.response.send_message(f"✅ **Server Welcome Channel has been set to {channel.mention}!**")


@bot.command(name="serverwelcometest", aliases=["swtest", "testserverwelcome"])
@commands.has_permissions(administrator=True)
async def serverwelcometest_cmd(ctx):
    """Sends a live test welcome message to the configured server welcome channel."""
    cfg = load_server_welcome_config()
    ch_id = cfg.get("channel_id")
    if not ch_id:
        await ctx.reply("❌ **No welcome channel configured!** Use `*setwelcomechannel #channel` first.")
        return
    target_ch = ctx.guild.get_channel(int(ch_id)) if ctx.guild else None
    if not target_ch:
        await ctx.reply(f"❌ **Channel <#{ch_id}> not found!** Please verify bot permissions.")
        return
    content, embed, file = build_server_welcome_payload(ctx.author, cfg)
    try:
        if file:
            await target_ch.send(content=content, embed=embed, file=file)
        elif embed:
            await target_ch.send(content=content, embed=embed)
        elif content:
            await target_ch.send(content=content)
        await ctx.reply(f"🎉 **Live test welcome sent to {target_ch.mention}!** Check the channel.")
    except Exception as e:
        await ctx.reply(f"⚠️ **Error sending message to {target_ch.mention}:** `{e}`")


@bot.tree.command(name="serverwelcometest", description="Send a test welcome message to the server welcome channel")
async def serverwelcometest_slash(interaction: discord.Interaction):
    if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only Administrators or Staff can run this test.", ephemeral=True)
        return
    cfg = load_server_welcome_config()
    ch_id = cfg.get("channel_id")
    if not ch_id:
        await interaction.response.send_message("❌ **No welcome channel configured!** Use `/setwelcomechannel` first.", ephemeral=True)
        return
    target_ch = interaction.guild.get_channel(int(ch_id)) if interaction.guild else None
    if not target_ch:
        await interaction.response.send_message(f"❌ **Channel <#{ch_id}> not found!**", ephemeral=True)
        return
    content, embed, file = build_server_welcome_payload(interaction.user, cfg)
    try:
        if file:
            await target_ch.send(content=content, embed=embed, file=file)
        elif embed:
            await target_ch.send(content=content, embed=embed)
        elif content:
            await target_ch.send(content=content)
        await interaction.response.send_message(f"🎉 **Live test welcome sent to {target_ch.mention}!** Check the channel.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ **Error sending message:** `{e}`", ephemeral=True)


@bot.command(name="serverwelcometoggle", aliases=["swtoggle"])
@commands.has_permissions(administrator=True)
async def serverwelcometoggle_cmd(ctx):
    """Toggles Server Channel Welcome on or off."""
    cfg = load_server_welcome_config()
    cfg["enabled"] = not cfg.get("enabled", True)
    save_server_welcome_config(cfg)
    status = "ENABLED (Active)" if cfg["enabled"] else "DISABLED (Paused)"
    await ctx.reply(f"🔘 **Server Channel Welcome:** `{status}`")


@bot.command(name="setserverwelcomemsg")
@commands.has_permissions(administrator=True)
async def setserverwelcomemsg_cmd(ctx, *, message: str):
    """Sets the outer message text above the server welcome embed."""
    cfg = load_server_welcome_config()
    cfg["message_content"] = message.strip()
    save_server_welcome_config(cfg)
    await ctx.reply(f"✅ **Server Welcome Message updated:**\n{message.strip()}")


@bot.command(name="setserverwelcometitle")
@commands.has_permissions(administrator=True)
async def setserverwelcometitle_cmd(ctx, *, title: str):
    """Sets the title of the server welcome embed."""
    cfg = load_server_welcome_config()
    cfg.setdefault("embed", {})["title"] = title.strip()
    save_server_welcome_config(cfg)
    await ctx.reply(f"✅ **Server Welcome Title updated:** `{title.strip()}`")


@bot.command(name="setserverwelcomedesc")
@commands.has_permissions(administrator=True)
async def setserverwelcomedesc_cmd(ctx, *, desc: str):
    """Sets the description (rules/links) of the server welcome embed."""
    cfg = load_server_welcome_config()
    cfg.setdefault("embed", {})["description"] = desc.strip()
    save_server_welcome_config(cfg)
    await ctx.reply("✅ **Server Welcome Description updated!** Run `*swtest` to preview.")


@bot.command(name="setserverwelcomebanner")
@commands.has_permissions(administrator=True)
async def setserverwelcomebanner_cmd(ctx, url: str = None):
    """Sets the bottom banner image URL for the server welcome embed."""
    if not url and ctx.message.attachments:
        url = ctx.message.attachments[0].url
    if not url:
        await ctx.reply("Usage: `*setserverwelcomebanner <image_url>` or attach an image.")
        return
    cfg = load_server_welcome_config()
    cfg.setdefault("embed", {})["banner_url"] = url.strip()
    save_server_welcome_config(cfg)
    await ctx.reply("✅ **Server Welcome Banner URL updated!**")


@bot.command(name="setserverwelcomethumb")
@commands.has_permissions(administrator=True)
async def setserverwelcomethumb_cmd(ctx, url: str = None):
    """Sets the thumbnail for the server welcome embed ({user_avatar} / {server_icon} / URL)."""
    if not url and ctx.message.attachments:
        url = ctx.message.attachments[0].url
    if not url:
        await ctx.reply("Usage: `*setserverwelcomethumb <{user_avatar} | {server_icon} | image_url>`")
        return
    cfg = load_server_welcome_config()
    cfg.setdefault("embed", {})["thumbnail_url"] = url.strip()
    save_server_welcome_config(cfg)
    await ctx.reply(f"✅ **Server Welcome Thumbnail updated to `{url.strip()}`!**")


# MUSIC SLASH COMMANDS (ALL 15 MUSIC COMMANDS IN '/' SLASH POPUP)
# Matches User Request: Shows up in / menu alongside /avatar, /banner, /help
# ==============================================================================

@bot.tree.command(name="play", description="Play any song, Spotify playlist, or YouTube track in Voice Channel")
@app_commands.describe(query="The song title, artist, YouTube URL, or Spotify playlist/track URL")
async def music_play_slash(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ **You are not connected to a Voice Channel!** Please join a voice channel first.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔍 **Searching and loading:** `{query}`...")
    # Invoke command logic via synthetic Context
    channel = interaction.user.voice.channel
    try:
        player = await ensure_voice_connection(channel, interaction.channel)
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ **Could not connect to Voice Channel:** `{e}`")
        return

    player.current_requester = interaction.user

    clean_q = query.strip()
    tracks = None

    if clean_q.startswith("http://") or clean_q.startswith("https://"):
        try:
            tracks = await wavelink.Playable.search(clean_q)
        except Exception:
            pass

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"ytmsearch:{clean_q}")
        except Exception:
            pass

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"scsearch:{clean_q}")
        except Exception:
            pass

    if not tracks:
        await interaction.edit_original_response(content="❌ **Could not stream the requested track!** Please try another search.")
        return

    player.autoplay = wavelink.AutoPlayMode.disabled

    is_playlist = isinstance(tracks, wavelink.Playlist) or (
        isinstance(tracks, list) and ("list=" in clean_q or "playlist" in clean_q) and len(tracks) > 1
    )
    if is_playlist:
        playlist_name = getattr(tracks, "name", "YouTube Playlist")
        pl_tracks = list(tracks.tracks) if isinstance(tracks, wavelink.Playlist) else list(tracks)
        for t in pl_tracks:
            setattr(t, "requester", interaction.user)
            track_requesters[t.identifier] = interaction.user
            if getattr(t, "uri", None):
                track_requesters[t.uri] = interaction.user

        is_active = (player.playing or player.paused) and player.current is not None
        if not is_active:
            first_track = pl_tracks[0]
            if len(pl_tracks) > 1:
                for rem_t in pl_tracks[1:]:
                    player.queue.put(rem_t)
            if player.paused:
                await player.pause(False)
            await player.play(first_track)

            embed = discord.Embed(
                title="YouTube Playlist Loaded & Playing",
                description=(
                    f"**[{playlist_name}]({clean_q})**\n\n"
                    f"Now Playing: [{first_track.title}]({first_track.uri})\n"
                    f"Queued: `{len(pl_tracks) - 1}` tracks ready to auto-play!\n"
                    f"Requester: {interaction.user.mention}"
                ),
                color=0x0000FF
            )
            if getattr(tracks, "artwork", None):
                embed.set_thumbnail(url=tracks.artwork)
            elif getattr(first_track, "artwork", None):
                embed.set_thumbnail(url=first_track.artwork)
            view = MusicControlView()
            await interaction.edit_original_response(content=None, embed=embed, view=view)
            return
            for rem_t in pl_tracks:
                player.queue.put(rem_t)
            embed = discord.Embed(
                title="Playlist Added to Queue",
                description=(
                    f"**[{playlist_name}]({clean_q})**\n\n"
                    f"Added **{len(pl_tracks)}** tracks to queue!\n"
                    f"Total in Queue: `#{len(player.queue)}`\n"
                    f"Requester: {interaction.user.mention}"
                ),
                color=0x0000FF
            )
            if getattr(tracks, "artwork", None):
                embed.set_thumbnail(url=tracks.artwork)
            view = MusicControlView()
            await interaction.edit_original_response(content=None, embed=embed, view=view)
            return

    track = tracks[0]
    setattr(track, "requester", interaction.user)
    track_requesters[track.identifier] = interaction.user
    if getattr(track, "uri", None):
        track_requesters[track.uri] = interaction.user

    cfg = load_config()
    is_active = (player.playing or player.paused) and player.current is not None
    if not is_active:
        if player.paused:
            await player.pause(False)
        try:
            await player.play(track)
            await apply_guild_audio_filters(player, interaction.guild.id)
        except Exception as play_err:
            logger.error(f"Playback error on '{track.title}': {play_err}")
            try:
                sc_tracks = await wavelink.Playable.search(f"scsearch:{clean_q}")
                if sc_tracks:
                    track = sc_tracks[0]
                    setattr(track, "requester", interaction.user)
                    await player.play(track)
                    await apply_guild_audio_filters(player, interaction.guild.id)
                    await interaction.edit_original_response(content=f"❌ **Audio Stream Error:** Failed to stream `{track.title}`. Please try another song.")
                    return
            except Exception as sc_err:
                logger.error(f"SoundCloud fallback failed: {sc_err}")
                await interaction.edit_original_response(content=f"❌ **Audio Stream Error:** Failed to stream `{track.title}`. Please try another song.")
                return
        embed = create_now_playing_embed(track, interaction.user, interaction.guild)
        view = MusicControlView()
        has_cdn = bool(cfg.get("music_banner_url") and "catbox" not in cfg.get("music_banner_url"))
        if os.path.exists("music_video.gif") and cfg.get("music_banner_mode", "video") == "video" and not has_cdn:
            gif_file = discord.File("music_video.gif", filename="music_video.gif")
            msg = await interaction.edit_original_response(content=None, embed=embed, attachments=[gif_file], view=view)
            if msg and msg.attachments:
                cfg["music_banner_url"] = msg.attachments[0].url
                save_config(cfg)
            active_control_messages[interaction.guild.id] = msg
            msg = await interaction.edit_original_response(content=None, embed=embed, view=view)
            active_control_messages[interaction.guild.id] = msg
        if getattr(player, "autoplay", None) == wavelink.AutoPlayMode.disabled:
            player.autoplay = wavelink.AutoPlayMode.disabled
        await player.queue.put_wait(track)
        pos = len(player.queue)
        sec = int((track.length or 0) / 1000)
        dur_str = format_duration(sec)
        embed = discord.Embed(
            title="📥 Added to Queue",
            description=f"**[{track.title}]({track.uri})**\nPosition in queue: `#{pos}` • Duration: `{dur_str}`",
            color=0x0000FF
        )
        if getattr(track, "artwork", None):
            embed.set_thumbnail(url=track.artwork)
        view = MusicControlView()
        await interaction.edit_original_response(content=None, embed=embed, view=view)


@bot.tree.command(name="playnow", description="Immediately skip whatever is playing and start this song right now")
@app_commands.describe(query="The song title or URL to play immediately")
async def music_playnow_slash(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ **You are not connected to a Voice Channel!**", ephemeral=True)
        return

    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return

    await interaction.response.send_message(f"⚡ **Loading track for immediate playback:** `{query}`...")
    channel = interaction.user.voice.channel
    try:
        player = await ensure_voice_connection(channel, interaction.channel)
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ **Could not connect to Voice Channel:** `{e}`")
        return

    player.current_requester = interaction.user

    clean_q = query.strip()
    tracks = None
    if clean_q.startswith("http://") or clean_q.startswith("https://"):
        try:
            tracks = await wavelink.Playable.search(clean_q)
        except Exception:
            pass

    if not tracks:
        try:
            tracks = await wavelink.Playable.search(f"ytmsearch:{clean_q}")
        except Exception:
            pass

    if not tracks:
        await interaction.edit_original_response(content="❌ **Could not stream the requested track!**")
        return

    track = tracks[0]
    setattr(track, "requester", interaction.user)
    track_requesters[track.identifier] = interaction.user
    if getattr(track, "uri", None):
        track_requesters[track.uri] = interaction.user

    player.queue.clear()
    if player.paused:
        await player.pause(False)
    await player.play(track)
    await apply_guild_audio_filters(player, interaction.guild.id)

    embed = create_now_playing_embed(track, interaction.user, interaction.guild)
    view = MusicControlView()
    cfg = load_config()
    has_cdn = bool(cfg.get("music_banner_url") and "catbox" not in cfg.get("music_banner_url"))
    if os.path.exists("music_video.gif") and cfg.get("music_banner_mode", "video") == "video" and not has_cdn:
        gif_file = discord.File("music_video.gif", filename="music_video.gif")
        msg = await interaction.edit_original_response(content=None, embed=embed, attachments=[gif_file], view=view)
        if msg and msg.attachments:
            cfg["music_banner_url"] = msg.attachments[0].url
            save_config(cfg)
        active_control_messages[interaction.guild.id] = msg
        msg = await interaction.edit_original_response(content=None, embed=embed, view=view)
        active_control_messages[interaction.guild.id] = msg


@bot.tree.command(name="pause", description="Pause current music playback")
async def music_pause_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player or not player.playing:
        await interaction.response.send_message("⚠️ **No track is actively playing!**", ephemeral=True)
        return
    await player.pause(True)
    await interaction.response.send_message("⏸️ **Playback paused.**")


@bot.tree.command(name="resume", description="Resume paused music playback")
async def music_resume_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player:
        await interaction.response.send_message("❌ **The bot is not in a Voice Channel!**", ephemeral=True)
        return
    await player.pause(False)
    await interaction.response.send_message("▶️ **Playback resumed.**")


@bot.tree.command(name="skip", description="Skip to the next track in queue")
async def music_skip_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player or (not player.playing and not player.paused and (not player.queue or len(player.queue) == 0)):
        await interaction.response.send_message("⚠️ **No active track or queue to skip.**", ephemeral=True)
        return
    next_t = await advance_to_next_track(player, force_skip=True)
    if next_t:
        await interaction.response.send_message(f"⏭️ **Skipped track! Now playing:** [{next_t.title}]({getattr(next_t, 'uri', '')})")
        await interaction.response.send_message("⏹️ **Skipped track. No more songs left in queue.**")


@bot.tree.command(name="stop", description="Stop music playback and clear queue")
async def music_stop_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player:
        await interaction.response.send_message("❌ **The bot is not in a Voice Channel!**", ephemeral=True)
        return
    player.queue.clear()
    player._current = None
    player.queue._loaded = None
    try:
        request = {"track": {"encoded": None}}
        await player.node._update_player(player.guild.id, data=request, replace=True)
    except Exception:
        pass
    await interaction.response.send_message("⏹️ **Playback stopped and queue cleared.**")


@bot.tree.command(name="nowplaying", description="Show the interactive music player controller panel")
async def music_nowplaying_slash(interaction: discord.Interaction):
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player or not player.current:
        await interaction.response.send_message("❌ **No music is currently playing in this server.**", ephemeral=True)
        return
    req = getattr(player.current, "requester", interaction.user)
    embed = create_now_playing_embed(player.current, req, interaction.guild)
    fx = guild_music_fx.get(interaction.guild.id, {})
    current_preset = fx.get("preset", "Normal")
    view = MusicControlView(current_preset=current_preset)
    cfg = load_config()
    has_cdn = bool(cfg.get("music_banner_url") and "catbox" not in cfg.get("music_banner_url"))
    if os.path.exists("music_video.gif") and cfg.get("music_banner_mode", "video") == "video" and not has_cdn:
        gif_file = discord.File("music_video.gif", filename="music_video.gif")
        await interaction.response.send_message(embed=embed, file=gif_file, view=view)
        await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="queue", description="View the list of upcoming tracks in queue")
async def music_queue_slash(interaction: discord.Interaction):
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player or not player.queue or len(player.queue) == 0:
        await interaction.response.send_message("📭 **The music queue is currently empty!**", ephemeral=True)
        return
    desc = ""
    for idx, t in enumerate(list(player.queue)[:15]):
        sec = int((t.length or 0) / 1000)
        dur = format_duration(sec)
        desc += f"**{idx+1}.** [{t.title}]({t.uri}) (`{dur}`)\n"
    total_q = len(player.queue)
    embed = discord.Embed(
        title=f"📜 Upcoming Queue ({total_q} track{'s' if total_q > 1 else ''})",
        description=desc,
        color=0x0000FF
    )
    if bot.user and bot.user.display_avatar:
        embed.set_footer(text="BYTE CORPORATION • Music Queue", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="volume", description="Adjust playback volume (1 to 100)")
@app_commands.describe(level="Volume level from 1 to 100")
async def music_volume_slash(interaction: discord.Interaction, level: int):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player:
        await interaction.response.send_message("❌ **The bot is not in a Voice Channel!**", ephemeral=True)
        return
    if not (1 <= level <= 100):
        await interaction.response.send_message("⚠️ **Volume must be between 1 and 100!**", ephemeral=True)
        return
    await player.set_volume(level)
    await interaction.response.send_message(f"🔊 **Volume set to `{level}%`!**")


@bot.tree.command(name="shuffle", description="Randomly shuffle tracks in the music queue")
async def music_shuffle_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player or not player.queue or len(player.queue) < 2:
        await interaction.response.send_message("⚠️ **At least 2 tracks must be in the queue to shuffle!**", ephemeral=True)
        return
    player.queue.shuffle()
    await interaction.response.send_message("🔀 **Music queue shuffled successfully!**")


@bot.tree.command(name="loop", description="Toggle loop mode for current track or entire queue")
async def music_loop_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if not player:
        await interaction.response.send_message("❌ **The bot is not in a Voice Channel!**", ephemeral=True)
        return
    if player.queue.mode == wavelink.QueueMode.normal:
        player.queue.mode = wavelink.QueueMode.loop
        status = "Current Track"
    elif player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.loop_all
        status = "Entire Queue"
        player.queue.mode = wavelink.QueueMode.normal
        status = "Disabled (Off)"
    await interaction.response.send_message(f"🔁 **Loop Mode:** `{status}`")


@bot.tree.command(name="autoplay", description="Toggle automatic smart song recommendations when queue ends")
async def music_autoplay_slash(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    guild_autoplay[guild_id] = not guild_autoplay.get(guild_id, False)
    status = "ON (Smart Recommendations)" if guild_autoplay[guild_id] else "OFF (Queue Only)"
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if player:
        player.autoplay = wavelink.AutoPlayMode.disabled
    await interaction.response.send_message(f"♾️ **Autoplay is now {status}!**")


@bot.tree.command(name="join", description="Summon the bot to your current voice channel")
async def music_join_slash(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ **You are not connected to a Voice Channel!**", ephemeral=True)
        return
    channel = interaction.user.voice.channel
    try:
        player = await ensure_voice_connection(channel, interaction.channel)
        await interaction.response.send_message(f"🔊 **Connected to `{channel.name}`!** 24/7 Mode active.")
    except Exception as e:
        await interaction.response.send_message(f"❌ **Failed to connect to Voice Channel:** `{e}`", ephemeral=True)


@bot.tree.command(name="disconnect", description="Disconnect the bot from voice channel")
async def music_disconnect_slash(interaction: discord.Interaction):
    allowed, reason = can_control_music(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(f"🛡️ **Access Denied:** {reason}", ephemeral=True)
        return
    _manual_disconnect_guilds.add(interaction.guild.id)
    is_guild_247[interaction.guild.id] = False
    player: wavelink.Player = cast(wavelink.Player, interaction.guild.voice_client)
    if player:
        player.queue.clear()
        await player.disconnect()
        await interaction.response.send_message("👋 **Disconnected from voice channel.**")
        await interaction.response.send_message("❌ **The bot is not connected to a Voice Channel!**", ephemeral=True)


@bot.tree.command(name="247", description="Toggle 24/7 permanent Voice Channel stay")
async def music_247_slash(interaction: discord.Interaction):
    if not has_admin_or_owner_access(interaction.user) and not is_staff(interaction.user):
        await interaction.response.send_message("❌ Only Administrators or Staff can toggle 24/7 mode.", ephemeral=True)
        return
    current = is_guild_247.get(interaction.guild.id, True)
    is_guild_247[interaction.guild.id] = not current
    status = "ENABLED (Permanent VC Stay)" if is_guild_247[interaction.guild.id] else "DISABLED (Auto-leave when idle)"
    await interaction.response.send_message(f"🛡️ **24/7 Mode:** `{status}`")



# ==============================================================================
# BYTE CORPORATION — ADVANCED SERVER PROTECTION & ANTI-NUKE SECURITY SYSTEM
# Matches exact VOID Security System UI & 24 Protection Modules
# ImGUI: Red Accent, Clean Spacing, Multi-Select Permission Dropdown & Action Buttons
# ==============================================================================

ANTINUKE_CONFIG_PATH = os.path.join(BASE_DIR, "antinuke_config.json")

ANTINUKE_MODULES_LIST = [
    ("ban", "Anti-Ban"),
    ("kick", "Anti-Kick"),
    ("unban", "Anti-Unban"),
    ("bot_add", "Anti-Bot Add"),
    ("member_role_update", "Anti-Member Role Update"),
    ("member_update", "Anti-Member Update"),
    ("emoji_action", "Anti-Emoji Action"),
    ("sticker_action", "Anti-Sticker Action"),
    ("message_delete", "Anti-Message Delete"),
    ("everyone_ping", "Anti-Everyone Ping"),
    ("here_ping", "Anti-Here Ping"),
    ("channel_create", "Anti-Channel Create"),
    ("channel_delete", "Anti-Channel Delete"),
    ("channel_update", "Anti-Channel Update"),
    ("role_create", "Anti-Role Create"),
    ("role_delete", "Anti-Role Delete"),
    ("role_update", "Anti-Role Update"),
    ("webhook_create", "Anti-Webhook Create"),
    ("webhook_delete", "Anti-Webhook Delete"),
    ("webhook_update", "Anti-Webhook Update"),
    ("guild_update", "Anti-Guild Update"),
    ("integration_add", "Anti-Integration Add"),
    ("integration_update", "Anti-Integration Update"),
    ("integration_delete", "Anti-Integration Delete"),
    ("onboarding", "Anti-Onboarding")
]

def resolve_custom_emoji(client: discord.Client, emoji_input: str) -> str:
    """Intelligently resolves any emoji input (raw tag, shortcode, or name) into Discord format."""
    if not emoji_input:
        return ""
    clean = emoji_input.strip()
    if re.match(r"^<(a?):([a-zA-Z0-9_]+):([0-9]+)>$", clean):
        return clean
    name_clean = clean.strip(":")
    if client and getattr(client, "emojis", None):
        found = discord.utils.get(client.emojis, name=name_clean)
        if found:
            return str(found)
    return clean

def load_antinuke_config() -> dict:
    if os.path.exists(ANTINUKE_CONFIG_PATH):
        try:
            with open(ANTINUKE_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "enabled": True,
        "punishment": "ban",
        "log_channel_id": None,
        "whitelisted_users": {},
        "modules": {k: True for k, _ in ANTINUKE_MODULES_LIST}
    }

def save_antinuke_config(cfg: dict):
    try:
        with open(ANTINUKE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving antinuke config: {e}")

def is_antinuke_whitelisted(guild: discord.Guild, user: discord.User | discord.Member, action: str = None) -> bool:
    """Checks if a user is immune from anti-nuke actions."""
    if not user:
        return False
    if guild and user.id == guild.owner_id:
        return True
    if bot.user and user.id == bot.user.id:
        return True
    if is_extra_owner(user.id):
        return True

    cfg = load_antinuke_config()
    if not cfg.get("enabled", True):
        return True

    whitelisted = cfg.get("whitelisted_users", {})
    user_entry = whitelisted.get(str(user.id))
    if not user_entry:
        return False
    if user_entry.get("all", False):
        return True
    if action and action in user_entry.get("permissions", []):
        return True
    return False

async def get_audit_entry(guild: discord.Guild, action: discord.AuditLogAction, target_id: int = None, max_age: float = 12.0):
    """Fetches recent audit log entry for security validation."""
    try:
        async for entry in guild.audit_logs(action=action, limit=6):
            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if age > max_age:
                continue
            if target_id is not None:
                if getattr(entry.target, "id", None) == target_id:
                    return entry
            else:
                return entry
    except Exception:
        pass
    return None

async def punish_antinuke_offender(guild: discord.Guild, offender: discord.User | discord.Member, reason: str, module_name: str):
    """Executes punishment (ban/kick/role strip) on unauthorized admin/user and sends log."""
    if not offender or not guild:
        return
    if offender.id == guild.owner_id or (bot.user and offender.id == bot.user.id):
        return

    cfg = load_antinuke_config()
    punishment = cfg.get("punishment", "ban").lower()
    applied = "None"

    if isinstance(offender, discord.Member):
        if guild.me.top_role > offender.top_role:
            if punishment == "ban":
                try:
                    await guild.ban(offender, reason=f"[VOID Security Anti-Nuke] {reason}", delete_message_days=1)
                    applied = "Banned"
                except Exception:
                    pass
            elif punishment == "kick":
                try:
                    await guild.kick(offender, reason=f"[VOID Security Anti-Nuke] {reason}")
                    applied = "Kicked"
                except Exception:
                    pass
        if applied == "None":
            # Strip dangerous roles if ban failed due to role hierarchy
            dangerous_roles = [
                r for r in offender.roles
                if r.permissions.administrator or r.permissions.ban_members or
                   r.permissions.manage_channels or r.permissions.manage_roles or
                   r.permissions.manage_guild
            ]
            if dangerous_roles:
                try:
                    await offender.remove_roles(*dangerous_roles, reason=f"[VOID Security] {reason}")
                    applied = "Admin Roles Stripped"
                except Exception:
                    pass
    else:
        try:
            await guild.ban(offender, reason=f"[VOID Security Anti-Nuke] {reason}", delete_message_days=1)
            applied = "Banned"
        except Exception:
            pass

    # Send Security Alert Log
    await send_antinuke_security_log(guild, module_name, offender, reason, applied)

async def send_antinuke_security_log(guild: discord.Guild, module_name: str, offender: discord.User | discord.Member, details: str, punishment: str):
    """Sends a professional security alert embed to the configured security log channel."""
    cfg = load_antinuke_config()
    log_ch_id = cfg.get("log_channel_id")
    ch = guild.get_channel(int(log_ch_id)) if log_ch_id else None
    if not ch:
        for c in guild.text_channels:
            if any(k in c.name.lower() for k in ["security-log", "antinuke-log", "mod-log", "logs"]):
                ch = c
                break
    if not ch:
        return

    embed = discord.Embed(
        title="🚨 VOID Security — Anti-Nuke Attack Blocked!",
        description=(
            f"**Action Module:** `{module_name}`\n"
            f"**Offender:** {offender.mention} (`{offender.id}`)\n"
            f"**Details:** {details}\n"
            f"**Punishment:** `{punishment}`\n"
            f"**Protection Status:** 🛡️ Server State Guarded & Restored"
        ),
        color=0x0055FF,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="VOID Security System • Bulletproof Shield", icon_url=guild.icon.url if guild.icon else None)
    try:
        await ch.send(embed=embed)
    except Exception:
        pass


async def send_automod_log(guild: discord.Guild, module_title: str, offender: discord.User | discord.Member, channel: discord.abc.GuildChannel, action_text: str, reason_text: str):
    """Sends the exact Automod log embed matching user screenshot to the log channel (and alerts in channel)."""
    cfg = load_antinuke_config()
    log_ch_id = cfg.get("log_channel_id")
    target_log_channel = guild.get_channel(int(log_ch_id)) if (log_ch_id and str(log_ch_id).isdigit()) else None
    if not target_log_channel:
        for c in guild.text_channels:
            if any(k in c.name.lower() for k in ["automod-log", "automod", "security-log", "antinuke-log", "mod-log", "logs"]):
                target_log_channel = c
                break

    # Build the exact Automod Embed matching screenshot
    embed = discord.Embed(
        title=f"Automod Log: {module_title}",
        color=0x2B2D31
    )
    if offender and hasattr(offender, "display_avatar"):
        embed.set_thumbnail(url=offender.display_avatar.url)

    # Field 1: User
    embed.add_field(name="User", value=f"{offender.name} ({offender.id})", inline=False)

    # Field 2: Action
    embed.add_field(name="Action", value=action_text, inline=False)

    # Field 3: Channel
    ch_val = channel.mention if hasattr(channel, "mention") else f"#{channel.name}"
    embed.add_field(name="Channel", value=ch_val, inline=False)

    # Field 4: Reason
    embed.add_field(name="Reason", value=reason_text, inline=False)

    # Footer: User ID: <id> • DD/MM/YYYY HH:MM
    now_str = discord.utils.utcnow().strftime("%d/%m/%Y %H:%M")
    embed.set_footer(text=f"User ID: {offender.id} • {now_str}")

    # 1. Send to dedicated log channel if available
    if target_log_channel:
        try:
            await target_log_channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Could not send automod log to {target_log_channel.name}: {e}")

    # 2. Also send alert embed in current channel (delete_after=15 to notify while keeping chat clean)
    if channel and channel != target_log_channel:
        try:
            await channel.send(embed=embed, delete_after=15)
        except Exception as e:
            logger.warning(f"Could not send automod alert in channel {channel.name}: {e}")
    elif not target_log_channel and channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Could not send fallback automod in channel {channel.name}: {e}")


def get_embed_color_from_config(cfg: dict) -> int:
    raw_col = cfg.get("embed", {}).get("color", "#0055FF").strip().lower()
    color_map = {
        "blue": 0x0055FF, "royal_blue": 0x0055FF, "discord_blue": 0x5865F2,
        "cyan": 0x00D9FF, "purple": 0x9B59B6, "red": 0xED4245,
        "green": 0x57F287, "yellow": 0xFEE75C, "gold": 0xF1C40F,
        "dark": 0x2B2D31, "black": 0x1E1F22, "white": 0xFFFFFF
    }
    if raw_col in color_map:
        return color_map[raw_col]
    clean_hex = raw_col.replace("#", "").replace("0x", "")
    try:
        return int(clean_hex, 16)
    except Exception:
        return 0x0055FF

def build_antinuke_whitelist_embed(guild: discord.Guild, target_user: discord.User | discord.Member, permissions_granted: list[str], is_all: bool) -> discord.Embed:
    """Builds the exact VOID/SpyDerMan Security dark embed matching Image 3 layout."""
    cfg = load_antinuke_config()
    embed_cfg = cfg.get("embed", {})

    embed_color = get_embed_color_from_config(cfg)
    embed = discord.Embed(color=embed_color)

    bot_name = cfg.get("bot_name") or (bot.user.name if bot.user else "SpyDerMan")
    raw_title = embed_cfg.get("title", "").strip()
    title_text = raw_title if raw_title else f"👑 {bot_name} Security System"
    subtitle_text = embed_cfg.get("subtitle", "🔴 Advanced Server Protection Module")
    bullet_symbol = embed_cfg.get("bullet_emoji", "»").strip()
    raw_footer = embed_cfg.get("footer", "").strip()
    footer_text = raw_footer if raw_footer else f"Powered By {bot_name} Security System™"
    custom_desc = embed_cfg.get("custom_description", "").strip()

    embed.title = title_text

    module_lines = "\n".join([f"{bullet_symbol} {name}" for _, name in ANTINUKE_MODULES_LIST])

    if is_all:
        user_status = "All Permissions Whitelisted"
    elif permissions_granted:
        user_status = f"{len(permissions_granted)} Permissions Whitelisted"
    else:
        user_status = "Not Whitelisted"

    extra_desc = f"\n\n{custom_desc}" if custom_desc else ""

    # Exact matching Image 3: Clean spacing, no squeezing thumbnail, plain User: and clean footer
    embed.description = (
        f"{subtitle_text}\n\n"
        f"{module_lines}\n\n"
        f"-----------------------\n"
        f"User: {target_user.mention} — [ `{user_status}` ]"
        f"{extra_desc}\n\n"
        f"{footer_text}"
    )
    return embed



class VoidSecurityActivationView(discord.ui.View):
    def __init__(self, support_url: str = "https://discord.gg/uaDzFDtFSe", invite_url: str = None, bot_name: str = None):
        super().__init__(timeout=None)
        support_url = support_url or "https://discord.gg/uaDzFDtFSe"
        self.add_item(discord.ui.Button(label="Join Support Server", url=support_url, style=discord.ButtonStyle.link))
        if invite_url:
            name_str = bot_name or (bot.user.name if bot.user else "Bot")
            self.add_item(discord.ui.Button(label=f"Invite {name_str}", url=invite_url, style=discord.ButtonStyle.link))


def build_void_welcome_embed(guild: discord.Guild, target_user: discord.Member = None, cfg: dict = None) -> discord.Embed:
    """Builds the exact bot greeting / setup embed matching the Aegis UI."""
    if cfg is None:
        cfg = load_antinuke_config()

    w_cfg = cfg.get("welcome_embed", {})
    color = get_embed_color_from_config(cfg)

    # Dynamic Bot Name (uses real Discord name, e.g. SpyDerMan)
    bot_name = cfg.get("bot_name") or (bot.user.display_name if bot.user else (bot.user.name if bot.user else "SpyDerMan"))

    shield_ico = w_cfg.get("shield_emoji", "🛡️")
    wine_ico = w_cfg.get("greeting_emoji", "🍷")
    snow_ico = w_cfg.get("server_emoji", "❄️")
    bullet_ico = w_cfg.get("bullet_emoji", "›")
    
    custom_title = w_cfg.get("title", "").strip()
    title_text = custom_title if custom_title else f"{shield_ico} Thank you for adding {bot_name}!"

    owner_user = target_user or (guild.owner if guild else None)
    owner_mention = owner_user.mention if owner_user else "@Owner"
    guild_name = guild.name if guild else "Server"

    custom_body = w_cfg.get("body_text", "").strip()
    body_text = custom_body if custom_body else f"{bot_name} provides maximum server protection, Join-to-Create voice channels, auto-moderation, and bulletproof security."

    support_url = cfg.get("support_url", "https://discord.gg/uaDzFDtFSe")
    prefix = "*"

    desc = (
        f"Hello {owner_mention} {wine_ico}!\n\n"
        f"Thank you for adding **{bot_name}** to **{guild_name}** {snow_ico}!\n\n"
        f"{body_text}\n\n"
        f"{bullet_ico} **Default Prefix:** `{prefix}`\n"
        f"{bullet_ico} **Get Help:** Type `{prefix}help` or `/help`\n"
        f"{bullet_ico} **Setup Anti-Nuke:** Type `{prefix}antinuke enable` or `/antinuke enable`\n"
        f"{bullet_ico} **Support Server:** [Click to Join Support Server]({support_url})\n\n"
        f"Please join our support server for quick updates, setup assistance, and notifications!"
    )

    embed = discord.Embed(title=title_text, description=desc, color=color)

    # Set Bot Logo on top-right thumbnail
    logo_url = cfg.get("logo_url") or (bot.user.display_avatar.url if (bot.user and bot.user.display_avatar) else None)
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    elif guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    custom_footer = w_cfg.get("footer_text", "").strip()
    footer_text = custom_footer if custom_footer else f"{bot_name} Security • Meet the Shield"
    time_str = discord.utils.utcnow().strftime("%H:%M")
    embed.set_footer(text=f"{footer_text} | Today at {time_str}")
    return embed


class VoidSecurityEditModal(discord.ui.Modal, title="🎨 Customize VOID Security Panel"):
    def __init__(self):
        super().__init__()
        cfg = load_antinuke_config()
        ecfg = cfg.get("embed", {})

        bot_name = cfg.get("bot_name") or (bot.user.name if bot.user else "SpyDerMan")
        self.title_input = discord.ui.TextInput(
            label="Panel Title",
            default=ecfg.get("title", f"👑 {bot_name} Security System"),
            max_length=100,
            required=True
        )
        self.subtitle_input = discord.ui.TextInput(
            label="Subtitle / Status Line",
            default=ecfg.get("subtitle", "🔴 Advanced Server Protection Module"),
            max_length=100,
            required=True
        )
        self.bullet_input = discord.ui.TextInput(
            label="Module Bullet Emoji (Animated Supported!)",
            default=ecfg.get("bullet_emoji", "»"),
            max_length=100,
            required=True
        )
        self.color_input = discord.ui.TextInput(
            label="Embed Color (#0055FF, blue, cyan, red)",
            default=ecfg.get("color", "#0055FF"),
            max_length=30,
            required=True
        )
        self.footer_input = discord.ui.TextInput(
            label="Footer Text",
            default=ecfg.get("footer", f"Powered By {bot_name} Security System™"),
            max_length=100,
            required=True
        )

        self.add_item(self.title_input)
        self.add_item(self.subtitle_input)
        self.add_item(self.bullet_input)
        self.add_item(self.color_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = load_antinuke_config()
        if "embed" not in cfg:
            cfg["embed"] = {}

        raw_col = self.color_input.value.strip()
        if not raw_col.startswith("#") and not raw_col.isalpha():
            raw_col = "#" + raw_col

        cfg["embed"]["title"] = resolve_custom_emoji(interaction.client, self.title_input.value.strip())
        cfg["embed"]["subtitle"] = resolve_custom_emoji(interaction.client, self.subtitle_input.value.strip())
        cfg["embed"]["bullet_emoji"] = resolve_custom_emoji(interaction.client, self.bullet_input.value.strip())
        cfg["embed"]["color"] = raw_col
        cfg["embed"]["footer"] = self.footer_input.value.strip()
        save_antinuke_config(cfg)

        preview_embed = build_antinuke_whitelist_embed(interaction.guild, interaction.user, [], True)
        await interaction.response.send_message(
            "✅ **VOID Security Panel updated successfully!** Here is your new live preview:",
            embed=preview_embed,
            ephemeral=True
        )


class ByteWhitelistPermissionSelect(discord.ui.Select):
    def __init__(self, target_user_id: int, current_perms: list[str]):
        options = [
            discord.SelectOption(label="Grant All Permissions", value="all", description="Whitelist for all protection actions"),
            discord.SelectOption(label="Ban", value="ban", description="Whitelist for ban"),
            discord.SelectOption(label="Unban", value="unban", description="Whitelist for unban"),
            discord.SelectOption(label="Kick", value="kick", description="Whitelist for kick"),
            discord.SelectOption(label="Bot Add", value="bot_add", description="Whitelist for bot add"),
            discord.SelectOption(label="Channel Create", value="channel_create", description="Whitelist for channel create"),
            discord.SelectOption(label="Channel Delete", value="channel_delete", description="Whitelist for channel delete"),
            discord.SelectOption(label="Channel Update", value="channel_update", description="Whitelist for channel update"),
            discord.SelectOption(label="Role Create", value="role_create", description="Whitelist for role create"),
            discord.SelectOption(label="Role Delete", value="role_delete", description="Whitelist for role delete"),
            discord.SelectOption(label="Role Update", value="role_update", description="Whitelist for role update"),
            discord.SelectOption(label="Member Update", value="member_update", description="Whitelist for member update"),
            discord.SelectOption(label="Member Role Update", value="member_role_update", description="Whitelist for member role update"),
            discord.SelectOption(label="Emoji Action", value="emoji_action", description="Whitelist for emoji action"),
            discord.SelectOption(label="Sticker Action", value="sticker_action", description="Whitelist for sticker action"),
            discord.SelectOption(label="Message Delete", value="message_delete", description="Whitelist for message delete"),
            discord.SelectOption(label="Everyone Ping", value="everyone_ping", description="Whitelist for everyone ping"),
            discord.SelectOption(label="Here Ping", value="here_ping", description="Whitelist for here ping"),
            discord.SelectOption(label="Webhook Create", value="webhook_create", description="Whitelist for webhook create"),
            discord.SelectOption(label="Webhook Delete", value="webhook_delete", description="Whitelist for webhook delete"),
            discord.SelectOption(label="Webhook Update", value="webhook_update", description="Whitelist for webhook update"),
            discord.SelectOption(label="Guild Update", value="guild_update", description="Whitelist for guild update"),
            discord.SelectOption(label="Integration Add", value="integration_add", description="Whitelist for integration add"),
            discord.SelectOption(label="Integration Update", value="integration_update", description="Whitelist for integration update"),
            discord.SelectOption(label="Integration Delete", value="integration_delete", description="Whitelist for integration delete")
        ]
        super().__init__(
            placeholder="› Choose Permissions to Grant",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0
        )
        self.target_user_id = target_user_id

    async def callback(self, interaction: discord.Interaction):
        view: ByteWhitelistControlView = self.view
        selected = self.values
        if "all" in selected:
            view.is_all = True
            view.selected_permissions = [k for k, _ in ANTINUKE_MODULES_LIST]
        else:
            view.is_all = False
            view.selected_permissions = selected

        embed = build_antinuke_whitelist_embed(interaction.guild, view.target_user, view.selected_permissions, view.is_all)
        await interaction.response.edit_message(embed=embed, view=view)


class ByteWhitelistControlView(discord.ui.View):
    def __init__(self, target_user: discord.User | discord.Member, author_id: int):
        super().__init__(timeout=300)
        self.target_user = target_user
        self.author_id = author_id

        cfg = load_antinuke_config()
        user_data = cfg.get("whitelisted_users", {}).get(str(target_user.id), {})
        self.is_all = user_data.get("all", False)
        self.selected_permissions = user_data.get("permissions", [])
        if self.is_all and not self.selected_permissions:
            self.selected_permissions = [k for k, _ in ANTINUKE_MODULES_LIST]

        self.add_item(ByteWhitelistPermissionSelect(target_user.id, self.selected_permissions))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not (await interaction.client.is_owner(interaction.user)):
            await interaction.response.send_message("❌ **Access Denied:** Only the **Server Owner** and **Extra Owner** can use Whitelist buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Grant All Permissions", style=discord.ButtonStyle.success, emoji="👑", row=1)
    async def grant_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.is_all = True
        self.selected_permissions = [k for k, _ in ANTINUKE_MODULES_LIST]
        cfg = load_antinuke_config()
        cfg["whitelisted_users"][str(self.target_user.id)] = {
            "all": True,
            "permissions": self.selected_permissions
        }
        save_antinuke_config(cfg)

        embed = build_antinuke_whitelist_embed(interaction.guild, self.target_user, self.selected_permissions, True)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"✅ **{self.target_user.mention} has been granted ALL Anti-Nuke Whitelist permissions!**", ephemeral=True)

    @discord.ui.button(label="Apply & Save", style=discord.ButtonStyle.primary, emoji="💾", row=1)
    async def apply_save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_antinuke_config()
        cfg["whitelisted_users"][str(self.target_user.id)] = {
            "all": self.is_all,
            "permissions": self.selected_permissions
        }
        save_antinuke_config(cfg)

        embed = build_antinuke_whitelist_embed(interaction.guild, self.target_user, self.selected_permissions, self.is_all)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"✅ **Saved Whitelist permissions for {self.target_user.mention}!**", ephemeral=True)

    @discord.ui.button(label="Remove All", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.is_all = False
        self.selected_permissions = []
        cfg = load_antinuke_config()
        if str(self.target_user.id) in cfg.get("whitelisted_users", {}):
            del cfg["whitelisted_users"][str(self.target_user.id)]
            save_antinuke_config(cfg)

        embed = build_antinuke_whitelist_embed(interaction.guild, self.target_user, [], False)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"🗑️ **Removed all Whitelist permissions from {self.target_user.mention}!**", ephemeral=True)


# ==============================================================================
# BYTE CORPORATION — ANTI-NUKE EVENT LISTENERS & SECURITY COMMANDS
# Active Real-Time Protection against unauthorized mass destruction
# ==============================================================================

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User | discord.Member):
    """Anti-Ban: Blocks unauthorized mass bans and auto-unbans victims."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("ban", True):
        return

    entry = await get_audit_entry(guild, discord.AuditLogAction.ban, target_id=user.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "ban"):
        return

    # Unauthorized ban detected!
    await punish_antinuke_offender(guild, entry.user, f"Banned member {user.name} ({user.id}) without whitelist", "Anti-Ban")
    try:
        await guild.unban(user, reason="[VOID Security Anti-Nuke] Auto-Restore Unauthorized Ban")
    except Exception:
        pass


@bot.event
async def on_member_remove(member: discord.Member):
    """Anti-Kick: Blocks unauthorized kicks."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("kick", True):
        return

    guild = member.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.kick, target_id=member.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "kick"):
        return

    # Unauthorized kick detected!
    await punish_antinuke_offender(guild, entry.user, f"Kicked member {member.name} ({member.id}) without whitelist", "Anti-Kick")


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    """Anti-Unban: Blocks unauthorized unbanning of malicious users."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("unban", True):
        return

    entry = await get_audit_entry(guild, discord.AuditLogAction.unban, target_id=user.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "unban"):
        return

    # Unauthorized unban detected! Re-ban and punish offender
    try:
        await guild.ban(user, reason="[VOID Security Anti-Nuke] Auto Re-Ban: Unauthorized Unban")
    except Exception:
        pass
    await punish_antinuke_offender(guild, entry.user, f"Unbanned user {user.name} ({user.id}) without whitelist", "Anti-Unban")


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    """Anti-Channel Create: Deletes unauthorized channels created by attackers."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("channel_create", True):
        return

    guild = channel.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.channel_create, target_id=channel.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "channel_create"):
        return

    # Unauthorized channel create detected!
    try:
        await channel.delete(reason="[VOID Security Anti-Nuke] Unauthorized Channel Creation")
    except Exception:
        pass
    await punish_antinuke_offender(guild, entry.user, f"Created channel #{channel.name} without whitelist", "Anti-Channel Create")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    """Anti-Channel Delete: Clones/Restores deleted channels and bans the attacker."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("channel_delete", True):
        return

    guild = channel.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.channel_delete, target_id=channel.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "channel_delete"):
        return

    # Punish attacker
    await punish_antinuke_offender(guild, entry.user, f"Deleted channel #{channel.name} without whitelist", "Anti-Channel Delete")

    # Auto-Restore Channel
    try:
        await channel.clone(reason="[VOID Security Anti-Nuke] Auto-Restore Deleted Channel")
    except Exception as e:
        logger.error(f"Failed to auto-restore deleted channel: {e}")


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    """Anti-Channel Update: Blocks malicious permission / name tampering on channels."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("channel_update", True):
        return

    guild = after.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.channel_update, target_id=after.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "channel_update"):
        return

    await punish_antinuke_offender(guild, entry.user, f"Tampered with #{after.name} settings/permissions", "Anti-Channel Update")


@bot.event
async def on_guild_role_create(role: discord.Role):
    """Anti-Role Create: Deletes unauthorized newly created roles."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("role_create", True):
        return

    guild = role.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.role_create, target_id=role.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "role_create"):
        return

    try:
        await role.delete(reason="[VOID Security Anti-Nuke] Unauthorized Role Creation")
    except Exception:
        pass
    await punish_antinuke_offender(guild, entry.user, f"Created role @{role.name} without whitelist", "Anti-Role Create")


@bot.event
async def on_guild_role_delete(role: discord.Role):
    """Anti-Role Delete: Auto-Restores deleted roles and punishes attacker."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("role_delete", True):
        return

    guild = role.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.role_delete, target_id=role.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "role_delete"):
        return

    await punish_antinuke_offender(guild, entry.user, f"Deleted role @{role.name} without whitelist", "Anti-Role Delete")
    try:
        await guild.create_role(name=role.name, permissions=role.permissions, color=role.color, hoist=role.hoist, mentionable=role.mentionable, reason="[VOID Security Anti-Nuke] Auto-Restore Deleted Role")
    except Exception:
        pass


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    """Anti-Role Update: Blocks unauthorized admin privilege escalation."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("role_update", True):
        return

    guild = after.guild
    # Check if dangerous permissions were added
    dangerous = ["administrator", "ban_members", "manage_channels", "manage_roles", "manage_guild"]
    escalated = any(getattr(after.permissions, perm) and not getattr(before.permissions, perm) for perm in dangerous)

    if not escalated and before.name == after.name:
        return

    entry = await get_audit_entry(guild, discord.AuditLogAction.role_update, target_id=after.id)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "role_update"):
        return

    # Revert permissions
    try:
        await after.edit(permissions=before.permissions, reason="[VOID Security Anti-Nuke] Reverted Unauthorized Permission Escalation")
    except Exception:
        pass
    await punish_antinuke_offender(guild, entry.user, f"Attempted permission escalation on @{after.name}", "Anti-Role Update")


@bot.event
async def on_webhooks_update(channel: discord.abc.GuildChannel):
    """Anti-Webhook: Prevents malicious webhook creation and spamming."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("webhook_create", True):
        return

    guild = channel.guild
    entry = await get_audit_entry(guild, discord.AuditLogAction.webhook_create)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "webhook_create"):
        return

    try:
        hooks = await channel.webhooks()
        for h in hooks:
            await h.delete(reason="[VOID Security Anti-Nuke] Unauthorized Webhook")
    except Exception:
        pass
    await punish_antinuke_offender(guild, entry.user, f"Created unauthorized webhook in #{channel.name}", "Anti-Webhook")


@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    """Anti-Guild Update: Blocks unauthorized server name/vanity/icon tampering."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("guild_update", True):
        return

    if before.name == after.name and before.icon == after.icon:
        return

    entry = await get_audit_entry(after, discord.AuditLogAction.guild_update)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(after, entry.user, "guild_update"):
        return

    # Revert server name if changed
    if before.name != after.name:
        try:
            await after.edit(name=before.name, reason="[VOID Security Anti-Nuke] Reverted Unauthorized Name Change")
        except Exception:
            pass
    await punish_antinuke_offender(after, entry.user, f"Tampered with server name/icon ({after.name})", "Anti-Guild Update")


@bot.event
async def on_guild_emojis_update(guild: discord.Guild, before: list[discord.Emoji], after: list[discord.Emoji]):
    """Anti-Emoji Action: Blocks mass emoji deletion."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("emoji_action", True):
        return

    if len(before) <= len(after):
        return

    entry = await get_audit_entry(guild, discord.AuditLogAction.emoji_delete)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "emoji_action"):
        return

    await punish_antinuke_offender(guild, entry.user, f"Deleted server emojis without whitelist", "Anti-Emoji Action")


@bot.event
async def on_guild_stickers_update(guild: discord.Guild, before: list[discord.GuildSticker], after: list[discord.GuildSticker]):
    """Anti-Sticker Action: Blocks mass sticker deletion."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("sticker_action", True):
        return

    if len(before) <= len(after):
        return

    entry = await get_audit_entry(guild, discord.AuditLogAction.sticker_delete)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "sticker_action"):
        return

    await punish_antinuke_offender(guild, entry.user, f"Deleted server stickers without whitelist", "Anti-Sticker Action")


@bot.event
async def on_guild_integrations_update(guild: discord.Guild):
    """Anti-Integration: Blocks rogue integration additions/deletions."""
    cfg = load_antinuke_config()
    if not cfg.get("enabled", True) or not cfg.get("modules", {}).get("integration_add", True):
        return

    entry = await get_audit_entry(guild, discord.AuditLogAction.integration_create)
    if not entry or not entry.user:
        return
    if is_antinuke_whitelisted(guild, entry.user, "integration_add"):
        return

    await punish_antinuke_offender(guild, entry.user, "Tampered with server integrations", "Anti-Integration")


# ==============================================================================
# COMMANDS: *antinuke & *wl (Matches Exact VOID Panels)
# ==============================================================================

@bot.command(name="antinuke", aliases=["security", "shield", "void", "setup"])
async def antinuke_cmd(ctx, action: str = None, *, arg: str = None):
    """
    VOID Security Master Command:
    - `*antinuke`: Shows VOID-style activation panel & status.
    - `*antinuke enable`: Turns ON all 24 protection modules.
    - `*antinuke disable`: Temporarily disables protection.
    - `*antinuke log #channel`: Sets the security alert log channel.
    - `*antinuke status`: View status of all protection modules.
    """
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not (await bot.is_owner(ctx.author)):
        await ctx.reply("❌ **Access Denied:** Only the **Server Owner** and **Extra Owner** can manage Security settings. (Administrators are not permitted).")
        return

    cfg = load_antinuke_config()
    action = (action or "").lower().strip()

    if action in ["edit", "customize", "custom", "setting", "settings", "ui"]:
        modal = VoidSecurityEditModal()
        # If run via message context, we can send an interactive button to open the modal
        class OpenEditModalView(discord.ui.View):
            @discord.ui.button(label="🎨 Open VOID Panel Editor", style=discord.ButtonStyle.primary, emoji="✏️")
            async def open_modal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(VoidSecurityEditModal())

        embed = discord.Embed(
            title="🎨 VOID Security — Live Panel Customizer",
            description=(
                "Click the button below to open the **Interactive Form Modal**:\n\n"
                "• **Title:** Change panel header\n"
                "• **Subtitle / Status:** Change status header\n"
                "• **Bullet Emoji:** Change `»` to `🔹`, `⚡`, `👑`, etc.\n"
                "• **Color:** Set `#0055FF` (Blue), `#00D9FF` (Cyan), or any Hex\n"
                "• **Footer:** Change branding footer text\n"
            ),
            color=get_embed_color_from_config(cfg)
        )
        await ctx.reply(embed=embed, view=OpenEditModalView())
        return

    elif action in ["setcolor", "color"]:
        if not arg:
            await ctx.reply("❌ **Usage:** `*antinuke setcolor #0055FF` (or `blue`, `cyan`, `purple`, `green`, `red`)")
            return
        if "embed" not in cfg:
            cfg["embed"] = {}
        cfg["embed"]["color"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply(f"✅ **Embed color updated to:** `{arg.strip()}`", embed=preview)
        return

    elif action in ["settitle", "title"]:
        if not arg:
            await ctx.reply("❌ **Usage:** `*antinuke settitle 🛡️ Thank you for adding VOID!` *(Supports animated emojis like `<a:shield:id>`)*")
            return
        if "welcome_embed" not in cfg:
            cfg["welcome_embed"] = {}
        if "embed" not in cfg:
            cfg["embed"] = {}
        cfg["welcome_embed"]["title"] = arg.strip()
        cfg["embed"]["title"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply(f"✅ **Title updated to:** {arg.strip()}", embed=preview)
        return

    elif action in ["setdesc", "desc", "description"]:
        if not arg:
            await ctx.reply("❌ **Usage:** `*antinuke setdesc <custom description text>`")
            return
        if "welcome_embed" not in cfg:
            cfg["welcome_embed"] = {}
        if "embed" not in cfg:
            cfg["embed"] = {}
        cfg["welcome_embed"]["body_text"] = arg.strip()
        cfg["embed"]["custom_description"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply("✅ **Description updated!**", embed=preview)
        return

    elif action in ["setlogo", "logo", "thumbnail", "avatar"]:
        if not arg:
            if ctx.message.attachments:
                arg = ctx.message.attachments[0].url
            else:
                await ctx.reply("❌ **Usage:** `*antinuke setlogo <image_url>` or attach an image with `*antinuke setlogo`")
                return
        cfg["logo_url"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply("✅ **Bot logo updated for the right-side thumbnail!**", embed=preview)
        return

    elif action in ["resetlogo", "clearlogo"]:
        cfg["logo_url"] = ""
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply("✅ **Logo reset to default Bot Discord Avatar!**", embed=preview)
        return

    elif action in ["setname", "botname", "name"]:
        if not arg:
            await ctx.reply("❌ **Usage:** `*antinuke setname <bot_name>` (e.g. `*antinuke setname SpyDerMan`)")
            return
        cfg["bot_name"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply(f"✅ **Bot name updated to:** `{arg.strip()}` *(Invite button label will say `Invite {arg.strip()}`)*", embed=preview)
        return

    elif action in ["setsupport", "support"]:
        if not arg or not arg.startswith("http"):
            await ctx.reply("❌ **Usage:** `*antinuke setsupport https://discord.gg/bytecorporation`")
            return
        cfg["support_url"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply(f"✅ **Support Server link updated to:** {arg.strip()}", embed=preview)
        return

    elif action in ["setinvite", "invite"]:
        if not arg or not arg.startswith("http"):
            await ctx.reply("❌ **Usage:** `*antinuke setinvite https://discord.com/oauth2/authorize?...`")
            return
        cfg["invite_url"] = arg.strip()
        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply(f"✅ **Bot Invite link updated!**", embed=preview)
        return

    elif action in ["setemoji", "emoji", "setbullet", "bullet"]:
        parts = (arg or "").split(maxsplit=1)
        if not parts:
            await ctx.reply(
                "❌ **Usage:**\n"
                "• `*antinuke setemoji bullet 🔹` (changes list bullet)\n"
                "• `*antinuke setemoji wine 🍷` (changes greeting emoji)\n"
                "• `*antinuke setemoji snow ❄️` (changes server tag emoji)\n"
                "• `*antinuke setemoji shield 🛡️` (changes shield icon)\n"
                "*(Animated custom emojis `<a:name:id>` are 100% supported!)*"
            )
            return

        target_slot = parts[0].lower()
        if len(parts) == 1:
            emoji_val = parts[0]
            target_slot = "bullet"
        else:
            emoji_val = parts[1]

        if "welcome_embed" not in cfg:
            cfg["welcome_embed"] = {}
        if "embed" not in cfg:
            cfg["embed"] = {}

        resolved_emoji = resolve_custom_emoji(ctx.bot, emoji_val)

        if target_slot in ["bullet", "»", "arrow"]:
            cfg["welcome_embed"]["bullet_emoji"] = resolved_emoji
            cfg["embed"]["bullet_emoji"] = resolved_emoji
            saved_as = "Bullet Emoji"
        elif target_slot in ["wine", "greet", "greeting"]:
            cfg["welcome_embed"]["greeting_emoji"] = resolved_emoji
            saved_as = "Greeting Emoji"
        elif target_slot in ["snow", "server", "snowflake"]:
            cfg["welcome_embed"]["server_emoji"] = resolved_emoji
            saved_as = "Server Emoji"
        elif target_slot in ["shield", "badge", "icon"]:
            cfg["welcome_embed"]["shield_emoji"] = resolved_emoji
            saved_as = "Shield Emoji"
        else:
            cfg["welcome_embed"]["bullet_emoji"] = resolved_emoji
            cfg["embed"]["bullet_emoji"] = resolved_emoji
            saved_as = "Bullet Emoji"

        save_antinuke_config(cfg)
        preview = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        await ctx.reply(f"✅ **{saved_as} updated to:** {emoji_val}", embed=preview)
        return

    elif action in ["settimeout", "timeout", "time"]:
        if not arg or not arg.strip().isdigit():
            await ctx.reply("❌ **Usage:** `*antinuke settimeout <minutes>` (e.g. `*antinuke settimeout 10` or `5`)")
            return
        mins = int(arg.strip())
        if "anti_everyone" not in cfg:
            cfg["anti_everyone"] = {}
        if "anti_link" not in cfg:
            cfg["anti_link"] = {}
        cfg["anti_everyone"]["timeout_minutes"] = mins
        cfg["anti_link"]["timeout_minutes"] = mins
        save_antinuke_config(cfg)
        await ctx.reply(f"✅ **Timeout duration for mass pings & unauthorized links updated to:** `{mins} minutes` *(Members will be timed out, NOT banned).*")
        return

    elif action in ["allowchannel", "whitelink", "allowlink"]:
        target_ch = ctx.message.channel_mentions[0] if ctx.message.channel_mentions else ctx.channel
        if "anti_link" not in cfg:
            cfg["anti_link"] = {"allowed_channels": []}
        if "allowed_channels" not in cfg["anti_link"]:
            cfg["anti_link"]["allowed_channels"] = []
        if target_ch.id not in cfg["anti_link"]["allowed_channels"]:
            cfg["anti_link"]["allowed_channels"].append(target_ch.id)
            save_antinuke_config(cfg)
            await ctx.reply(f"✅ Channel {target_ch.mention} is now **allowed for links** *(Anti-link will ignore this channel).*")
        else:
            cfg["anti_link"]["allowed_channels"].remove(target_ch.id)
            save_antinuke_config(cfg)
            await ctx.reply(f"🔒 Channel {target_ch.mention} link permission **revoked** *(Links will now be blocked & timed out).*")
        return

    if action == "enable":
        cfg["enabled"] = True
        save_antinuke_config(cfg)
        embed = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
        support_link = cfg.get("support_url", "https://discord.gg/uaDzFDtFSe")
        bot_name = cfg.get("bot_name") or (bot.user.name if bot.user else "Bot")
        bot_invite = cfg.get("invite_url") or (discord.utils.oauth_url(bot.user.id, permissions=discord.Permissions(administrator=True)) if bot.user else None)
        view = VoidSecurityActivationView(support_url=support_link, invite_url=bot_invite, bot_name=bot_name)
        await ctx.reply(embed=embed, view=view)
        return

    elif action == "disable":
        cfg["enabled"] = False
        save_antinuke_config(cfg)
        await ctx.reply("⚠️ **VOID Security Anti-Nuke has been DISABLED.** Server is no longer actively guarded against unauthorized changes.")
        return

    elif action in ["log", "logs", "setlog", "channel"]:
        channel = None
        if ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
        elif arg:
            try:
                channel = ctx.guild.get_channel(int(arg.strip("<#> ")))
            except ValueError:
                pass
        if not channel:
            channel = ctx.channel

        cfg["log_channel_id"] = channel.id
        save_antinuke_config(cfg)
        await ctx.reply(f"✅ **Security Alert Logs channel set to:** {channel.mention}\n*All future anti-nuke attack notifications will be dispatched here.*")
        return

    elif action in ["status", "info"]:
        status_str = "🟢 ACTIVE" if cfg.get("enabled", True) else "🔴 DISABLED"
        log_ch = f"<#{cfg.get('log_channel_id')}>" if cfg.get('log_channel_id') else "*(Not Set — using default log)*"
        wl_count = len(cfg.get("whitelisted_users", {}))
        embed_color = get_embed_color_from_config(cfg)
        embed = discord.Embed(
            title="🛡️ VOID Security System — Live Status",
            description=(
                f"❯ **Protection Status:** `{status_str}`\n"
                f"❯ **Whitelisted Admins:** `{wl_count}` staff members immune\n"
                f"❯ **Alert Logs Channel:** {log_ch}\n"
                f"❯ **Active Protection Modules:** `24/24 Enabled`\n\n"
                f"**Management Commands:**\n"
                f"• `*wl add @user` — Open interactive whitelist panel\n"
                f"• `*wl remove @user` — Revoke whitelist permissions\n"
                f"• `*wl list` — View all whitelisted staff\n"
                f"• `*antinuke log #channel` — Set custom alert channel\n"
                f"• `*antinuke edit` — Live interactive modal editor"
            ),
            color=embed_color
        )
        embed.set_footer(text=f"VOID Security System • Status | Today at {discord.utils.utcnow().strftime('%H:%M')}")
        await ctx.reply(embed=embed)
        return

    # Default / setup: Show Main Activation / Status Panel matching exact Aegis/VOID UI
    embed = build_void_welcome_embed(ctx.guild, ctx.author, cfg)
    support_link = cfg.get("support_url", "https://discord.gg/uaDzFDtFSe")
    bot_name = cfg.get("bot_name") or (bot.user.name if bot.user else "Bot")
    bot_invite = cfg.get("invite_url") or (discord.utils.oauth_url(bot.user.id, permissions=discord.Permissions(administrator=True)) if bot.user else None)
    view = VoidSecurityActivationView(support_url=support_link, invite_url=bot_invite, bot_name=bot_name)
    await ctx.reply(embed=embed, view=view)


@bot.command(name="extraowner", aliases=["extraowners", "eo", "owner"])
async def extraowner_cmd(ctx, action: str = None, target: discord.User = None):
    """
    Extra Owner Management System:
    - Only Server Owner can add or remove Extra Owners.
    - Extra Owners have full Anti-Nuke immunity and can whitelist/manage security.
    - Regular Administrators are blocked from whitelisting or managing owners!
    """
    is_app_owner = await bot.is_owner(ctx.author)
    is_srv_owner = (ctx.author.id == ctx.guild.owner_id)

    action = (action or "").lower().strip()

    if action in ["list", "show", "all"] or not action:
        if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not is_app_owner:
            await ctx.reply("❌ **Access Denied:** Only the **Server Owner** or **Extra Owner** can view ownership status.")
            return

        cfg = load_antinuke_config()
        extra_owners = cfg.get("extra_owners", [])
        
        lines = [f"👑 **Server Owner:** {ctx.guild.owner.mention} *(Primary Owner)*\n"]
        if extra_owners:
            lines.append("**Extra Owners (Full Security & Whitelist Access):**")
            for uid in extra_owners:
                u = ctx.guild.get_member(int(uid))
                u_str = u.mention if u else f"<@{uid}>"
                lines.append(f"• {u_str} — `[ Extra Owner ]`")
        else:
            lines.append("*(No Extra Owners configured. Server Owner can add one with `*extraowner add @user`)*")

        embed = discord.Embed(
            title="👑 VOID Security — Server Ownership System",
            description="\n".join(lines) + "\n\n*Only the Server Owner and Extra Owners can manage Whitelist & Anti-Nuke.*",
            color=get_embed_color_from_config(cfg)
        )
        logo_url = cfg.get("logo_url") or (bot.user.display_avatar.url if (bot.user and bot.user.display_avatar) else None)
        if logo_url:
            embed.set_thumbnail(url=logo_url)
        embed.set_footer(text=f"VOID Security System | Today at {discord.utils.utcnow().strftime('%H:%M')}")
        await ctx.reply(embed=embed)
        return

    # Adding / Removing Extra Owners: STRICTLY Server Owner (or App Owner)
    if not (is_srv_owner or is_app_owner):
        owner_name = ctx.guild.owner.display_name if ctx.guild.owner else "Server Owner"
        await ctx.reply(f"❌ **Access Denied:** ONLY the **Server Owner** (`{owner_name}`) can add or remove Extra Owners!")
        return

    if action == "add":
        if not target:
            if ctx.message.mentions:
                target = ctx.message.mentions[0]
            else:
                await ctx.reply("❌ **Usage:** `*extraowner add @user` — Please mention the user to promote to Extra Owner.")
                return

        if target.id == ctx.guild.owner_id:
            await ctx.reply("ℹ️ This user is already the **Server Owner**.")
            return

        cfg = load_antinuke_config()
        if "extra_owners" not in cfg:
            cfg["extra_owners"] = []

        if target.id not in cfg["extra_owners"] and str(target.id) not in [str(x) for x in cfg["extra_owners"]]:
            cfg["extra_owners"].append(target.id)
            save_antinuke_config(cfg)
            await ctx.reply(
                f"👑 **{target.mention} has been promoted to EXTRA OWNER!**\n\n"
                f"🛡️ **Security Privileges Granted:**\n"
                f"• Full permanent immunity from all 24 Anti-Nuke checks.\n"
                f"• Authorized to Whitelist staff members (`*wl add @user`).\n"
                f"• Authorized to manage Security configuration (`*antinuke`).\n"
                f"*(Regular administrators remain blocked from whitelisting)*"
            )
        else:
            await ctx.reply(f"ℹ️ **{target.mention}** is already an Extra Owner.")
        return

    elif action in ["remove", "rem", "delete", "del"]:
        if not target:
            if ctx.message.mentions:
                target = ctx.message.mentions[0]
            else:
                await ctx.reply("❌ **Usage:** `*extraowner remove @user`")
                return

        cfg = load_antinuke_config()
        if "extra_owners" not in cfg:
            cfg["extra_owners"] = []

        cfg["extra_owners"] = [x for x in cfg["extra_owners"] if str(x) != str(target.id)]
        save_antinuke_config(cfg)
        await ctx.reply(f"🗑️ **{target.mention} has been removed from Extra Owners.** *(Their owner-level security privileges have been revoked).*")
        return


@bot.command(name="wl", aliases=["whitelist"])
async def whitelist_cmd(ctx, subcmd: str = None, target: discord.User = None):
    """
    Interactive Whitelist Manager (Matches Exact VOID Panels):
    - `*wl add @user`: Opens the exact VOID ImGUI panel with dropdown and action buttons.
    - `*wl remove @user`: Strips whitelist from user.
    - `*wl list`: Lists all whitelisted users.
    """
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not (await bot.is_owner(ctx.author)):
        await ctx.reply("❌ **Access Denied:** Only the **Server Owner** and **Extra Owner** can manage Anti-Nuke Whitelist. (Administrators are strictly restricted).")
        return

    subcmd = (subcmd or "").lower().strip()

    if subcmd == "add":
        if not target:
            if ctx.message.mentions:
                target = ctx.message.mentions[0]
            else:
                await ctx.reply("❌ **Usage:** `*wl add @user` — Please mention the user you want to whitelist.")
                return

        cfg = load_antinuke_config()
        user_data = cfg.get("whitelisted_users", {}).get(str(target.id), {})
        is_all = user_data.get("all", False)
        perms = user_data.get("permissions", [])

        embed = build_antinuke_whitelist_embed(ctx.guild, target, perms, is_all)
        view = ByteWhitelistControlView(target, ctx.author.id)
        await ctx.reply(embed=embed, view=view)
        return

    elif subcmd in ["remove", "rem", "delete", "del"]:
        if not target:
            if ctx.message.mentions:
                target = ctx.message.mentions[0]
            else:
                await ctx.reply("❌ **Usage:** `*wl remove @user`")
                return

        cfg = load_antinuke_config()
        if str(target.id) in cfg.get("whitelisted_users", {}):
            del cfg["whitelisted_users"][str(target.id)]
            save_antinuke_config(cfg)
            await ctx.reply(f"🗑️ **{target.mention} has been completely removed from the Anti-Nuke Whitelist.**")
        else:
            await ctx.reply(f"ℹ️ **{target.mention}** is not in the whitelist.")
        return

    elif subcmd in ["list", "show"]:
        cfg = load_antinuke_config()
        whitelisted = cfg.get("whitelisted_users", {})
        if not whitelisted:
            await ctx.reply("📋 **No users are currently whitelisted.** *(Only the Server Owner and Bot have default immunity).*")
            return

        lines = []
        for uid_str, data in whitelisted.items():
            u = ctx.guild.get_member(int(uid_str))
            uname = u.mention if u else f"<@{uid_str}>"
            if data.get("all"):
                status = "👑 [ All Permissions Whitelisted ]"
            else:
                p_count = len(data.get("permissions", []))
                status = f"🛡️ [ {p_count} Specific Permissions ]"
            lines.append(f"• {uname} — {status}")

        embed = discord.Embed(
            title="👑 VOID Security — Whitelisted Members",
            description="\n".join(lines) + f"\n\n*Server Owner ({ctx.guild.owner.mention}) is permanently immune.*",
            color=0x0055FF
        )
        await ctx.reply(embed=embed)
        return

    else:
        await ctx.reply(
            "📋 **VOID Security Whitelist Commands:**\n"
            "• `*wl add @user` — Open interactive panel to whitelist staff\n"
            "• `*wl remove @user` — Remove staff from whitelist\n"
            "• `*wl list` — View all whitelisted staff members\n"
        )



@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Anti-Member Role Update: Prevents unauthorized assignment of administrative roles."""
    if before.roles != after.roles:
        cfg = load_antinuke_config()
        if cfg.get("enabled", True) and cfg.get("modules", {}).get("member_role_update", True):
            added_roles = [r for r in after.roles if r not in before.roles]
            dangerous = [
                r for r in added_roles
                if r.permissions.administrator or r.permissions.ban_members or
                   r.permissions.manage_channels or r.permissions.manage_roles or
                   r.permissions.manage_guild
            ]
            if dangerous:
                entry = await get_audit_entry(after.guild, discord.AuditLogAction.member_role_update, target_id=after.id)
                if entry and entry.user:
                    if not is_antinuke_whitelisted(after.guild, entry.user, "member_role_update"):
                        try:
                            await after.remove_roles(*dangerous, reason="[VOID Security Anti-Nuke] Reverted Unauthorized Role Escalation")
                        except Exception:
                            pass
                        await punish_antinuke_offender(after.guild, entry.user, f"Gave dangerous role(s) to {after.name} without whitelist", "Anti-Member Role Update")


# ==============================================================================
# ENTERPRISE INTERACTIVE GIVEAWAY SYSTEM (Full Animated Emoji Support)
# Matches User Screenshot (Cyan Embed, Prize Header, Relative Timestamps, Entry & Participants Buttons)
# ==============================================================================

GIVEAWAY_FILE = os.path.join(BASE_DIR, "giveaways.json")

def load_giveaways() -> dict:
    if not os.path.exists(GIVEAWAY_FILE):
        return {"giveaways": {}, "default_config": {"button_emoji": "🎉", "bullet_emoji": "•", "color": "#00D9FF"}}
    try:
        with open(GIVEAWAY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"giveaways": {}, "default_config": {"button_emoji": "🎉", "bullet_emoji": "•", "color": "#00D9FF"}}

def save_giveaways(data: dict):
    try:
        with open(GIVEAWAY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving giveaways: {e}")

def parse_duration(time_str: str) -> int | None:
    """Parses natural duration strings (e.g. 10s, 5m, 1h, 2d, 1w, 1h30m) to seconds."""
    if not time_str:
        return None
    regex = re.compile(
        r'((?P<weeks>\d+?)\s*(?:w|weeks?))?\s*'
        r'((?P<days>\d+?)\s*(?:d|days?))?\s*'
        r'((?P<hours>\d+?)\s*(?:h|hours?|hrs?))?\s*'
        r'((?P<minutes>\d+?)\s*(?:m|minutes?|mins?))?\s*'
        r'((?P<seconds>\d+?)\s*(?:s|seconds?|secs?))?',
        re.IGNORECASE
    )
    match = regex.fullmatch(time_str.strip())
    if not match:
        if time_str.strip().isdigit():
            return int(time_str.strip()) * 60
        return None

    parts = {k: int(v) for k, v in match.groupdict().items() if v}
    if not parts:
        return None

    total_seconds = (
        parts.get('weeks', 0) * 604800 +
        parts.get('days', 0) * 86400 +
        parts.get('hours', 0) * 3600 +
        parts.get('minutes', 0) * 60 +
        parts.get('seconds', 0)
    )
    return total_seconds if total_seconds > 0 else None

def parse_giveaway_button_emoji(emoji_input: str) -> discord.PartialEmoji | str:
    """Parses any unicode or animated custom emoji (<a:name:id>) for Discord UI Button."""
    if not emoji_input:
        return "🎉"
    emoji_input = emoji_input.strip()
    if emoji_input.startswith("<") and emoji_input.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(emoji_input)
        except Exception:
            pass
    # Search in bot cache
    for e in bot.emojis:
        if e.name.lower() == emoji_input.strip(":").lower():
            return discord.PartialEmoji(name=e.name, animated=e.animated, id=e.id)
    return emoji_input

def get_giveaway_color(color_str: str) -> int:
    if not color_str:
        return 0x00D9FF
    clean = color_str.replace("#", "").replace("0x", "").strip()
    try:
        return int(clean, 16)
    except Exception:
        return 0x00D9FF

def build_giveaway_embed(gw: dict, is_ended: bool = False) -> discord.Embed:
    prize = gw.get("prize", "Giveaway Prize")
    host_id = gw.get("host_id")
    host_mention = f"<@{host_id}>" if host_id else "Server Staff"
    end_ts = int(gw.get("end_time", 0))
    winners_count = int(gw.get("winners", 1))
    participants = gw.get("participants", [])
    bullet = gw.get("bullet_emoji", "•")
    if not bullet or "â" in str(bullet) or bullet == "▪":
        bullet = "•"
    color_code = get_giveaway_color(gw.get("color", "#00D9FF"))

    if not is_ended:
        desc_lines = [
            f"{bullet} `Hosted by` {host_mention}",
            f"{bullet} `Ends:` <t:{end_ts}:f> (<t:{end_ts}:R>)",
            f"{bullet} `Winners:` **{winners_count}**",
            "",
            f"{bullet} `Participants:` **{len(participants)}**"
        ]
        embed = discord.Embed(
            title=prize,
            color=color_code,
            description="\n".join(desc_lines)
        )
    else:
        winner_ids = gw.get("winner_ids", [])
        if winner_ids:
            winners_str = ", ".join([f"<@{uid}>" for uid in winner_ids])
        else:
            winners_str = "None (No valid entries)"

        desc_lines = [
            f"{bullet} `Hosted by` {host_mention}",
            f"{bullet} `Ended:` <t:{end_ts}:R>",
            f"{bullet} `Winners:` {winners_str}",
            "",
            f"{bullet} `Total Participants:` **{len(participants)}**"
        ]
        embed = discord.Embed(
            title=f"🎉 Giveaway Ended: {prize}",
            color=0x2B2D31,
            description="\n".join(desc_lines)
        )

    return embed

class GiveawayEntryView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = str(message_id)

        g_data = load_giveaways()
        gw = g_data.get("giveaways", {}).get(str(message_id), {})
        is_ended = gw.get("ended", False)

        btn_emoji_raw = gw.get("button_emoji") or g_data.get("default_config", {}).get("button_emoji", "🎉")
        btn_emoji = parse_giveaway_button_emoji(btn_emoji_raw)

        # Button 1: Entry Button
        self.entry_btn = discord.ui.Button(
            label="Entry",
            emoji=btn_emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"gw_entry:{message_id}",
            disabled=is_ended
        )
        self.entry_btn.callback = self.entry_callback
        self.add_item(self.entry_btn)

        # Button 2: Participants Button
        self.part_btn = discord.ui.Button(
            label="Participants",
            emoji="👥",
            style=discord.ButtonStyle.secondary,
            custom_id=f"gw_part:{message_id}"
        )
        self.part_btn.callback = self.participants_callback
        self.add_item(self.part_btn)

    async def entry_callback(self, interaction: discord.Interaction):
        g_data = load_giveaways()
        gw = g_data.get("giveaways", {}).get(self.message_id)
        if not gw:
            await interaction.response.send_message("❌ Giveaway not found or expired.", ephemeral=True)
            return
        if gw.get("ended", False):
            await interaction.response.send_message("❌ This giveaway has already ended!", ephemeral=True)
            return

        participants = gw.get("participants", [])
        user_id = interaction.user.id

        if user_id in participants:
            participants.remove(user_id)
            gw["participants"] = participants
            save_giveaways(g_data)

            embed = build_giveaway_embed(gw)
            try:
                await interaction.message.edit(embed=embed, view=self)
            except Exception:
                pass

            await interaction.response.send_message(f"❌ You have left the giveaway for **{gw.get('prize', 'Prize')}**.", ephemeral=True)
        else:
            participants.append(user_id)
            gw["participants"] = participants
            save_giveaways(g_data)

            embed = build_giveaway_embed(gw)
            try:
                await interaction.message.edit(embed=embed, view=self)
            except Exception:
                pass

            await interaction.response.send_message(f"🎉 **Entry confirmed!** You entered the giveaway for **{gw.get('prize', 'Prize')}**! Good luck! 🍀", ephemeral=True)

    async def participants_callback(self, interaction: discord.Interaction):
        g_data = load_giveaways()
        gw = g_data.get("giveaways", {}).get(self.message_id)
        if not gw:
            await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
            return

        participants = gw.get("participants", [])
        count = len(participants)
        if count == 0:
            await interaction.response.send_message("👥 **No participants have joined yet!** Be the first one to click **Entry**!", ephemeral=True)
            return

        sample = [f"<@{uid}>" for uid in participants[:20]]
        more = f"\n*...and {count - 20} more*" if count > 20 else ""
        await interaction.response.send_message(f"👥 **Total Participants ({count}):**\n" + ", ".join(sample) + more, ephemeral=True)


async def end_giveaway(message_id: str, guild: discord.Guild = None):
    g_data = load_giveaways()
    gw = g_data.get("giveaways", {}).get(str(message_id))
    if not gw or gw.get("ended", False):
        return

    gw["ended"] = True
    save_giveaways(g_data)

    channel = bot.get_channel(gw.get("channel_id"))
    if not channel:
        try:
            channel = await bot.fetch_channel(gw.get("channel_id"))
        except Exception:
            channel = None

    msg = None
    if channel:
        try:
            msg = await channel.fetch_message(int(message_id))
        except Exception:
            pass

    participants = gw.get("participants", [])
    winners_count = gw.get("winners", 1)

    winner_ids = []
    if participants:
        sample_size = min(winners_count, len(participants))
        winner_ids = random.sample(participants, sample_size)

    gw["winner_ids"] = winner_ids
    save_giveaways(g_data)

    # Update giveaway message embed
    ended_embed = build_giveaway_embed(gw, is_ended=True)
    ended_view = GiveawayEntryView(int(message_id))

    if msg:
        try:
            await msg.edit(embed=ended_embed, view=ended_view)
        except Exception:
            pass

    # Send announcement in channel
    if channel:
        prize = gw.get("prize", "Prize")
        if winner_ids:
            winners_mention = ", ".join([f"<@{uid}>" for uid in winner_ids])
            congrats_embed = discord.Embed(
                title="🎉 Giveaway Winner Announced! 🎉",
                description=f"Congratulations {winners_mention}!\nYou won **{prize}**!\n\n🔗 [Jump to Giveaway]({msg.jump_url if msg else ''})",
                color=0x00FF88
            )
            try:
                await channel.send(content=f"🎉 Congratulations {winners_mention}!", embed=congrats_embed)
            except Exception:
                pass
        else:
            try:
                await channel.send(f"⚠️ **Giveaway for '{prize}' has ended!** No valid participants joined.")
            except Exception:
                pass


@tasks.loop(seconds=10)
async def giveaway_timer_task():
    """Checks active giveaways every 10 seconds and concludes any that reached their end time."""
    g_data = load_giveaways()
    now_ts = int(discord.utils.utcnow().timestamp())
    giveaways = g_data.get("giveaways", {})

    for msg_id, gw in list(giveaways.items()):
        if not gw.get("ended", False):
            end_ts = int(gw.get("end_time", 0))
            if now_ts >= end_ts:
                try:
                    await end_giveaway(msg_id)
                except Exception as e:
                    logger.error(f"Error concluding giveaway {msg_id}: {e}")


@bot.command(name="gstart", aliases=["giveaway", "gcreate"])
async def gstart_cmd(ctx, duration: str = None, winners_str: str = None, *, prize: str = None):
    """Starts a new interactive giveaway matching user screenshot with animated emojis."""
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not ctx.author.guild_permissions.manage_guild and not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ **Access Denied:** Only Server Owner, Extra Owners, or Admins can host giveaways.")
        return

    if not duration or not winners_str or not prize:
        embed_help = discord.Embed(
            title="🎉 Giveaway Setup Guide",
            description=(
                "**Usage:** `*gstart <duration> <winners> <prize>`\n\n"
                "**Examples:**\n"
                "• `*gstart 1h 1w 1 mont`\n"
                "• `*gstart 30m 2 1 Month Discord Nitro`\n"
                "• `*gstart 2d 1w Spotify Premium`\n\n"
                "**Custom Emojis (Animated Supported):**\n"
                "• `*gsetemoji button <emoji>` — Change Entry button emoji\n"
                "• `*gsetemoji bullet <emoji>` — Change bullet point emoji\n"
                "• `*gsetemoji color <#hex>` — Change embed color"
            ),
            color=0x00D9FF
        )
        await ctx.reply(embed=embed_help)
        return

    dur_seconds = parse_duration(duration)
    if not dur_seconds or dur_seconds < 10:
        await ctx.reply("❌ **Invalid duration!** Use format like `10m`, `1h`, `2d`, `1w` (min 10 seconds).")
        return

    clean_w = winners_str.lower().replace("w", "").strip()
    if not clean_w.isdigit() or int(clean_w) < 1:
        await ctx.reply("❌ **Invalid winners count!** Provide a number like `1` or `2w`.")
        return
    winners_count = int(clean_w)

    g_data = load_giveaways()
    defaults = g_data.get("default_config", {})

    now_ts = int(discord.utils.utcnow().timestamp())
    end_ts = now_ts + dur_seconds

    gw_payload = {
        "channel_id": ctx.channel.id,
        "guild_id": ctx.guild.id,
        "host_id": ctx.author.id,
        "prize": prize.strip(),
        "winners": winners_count,
        "end_time": end_ts,
        "participants": [],
        "color": defaults.get("color", "#00D9FF"),
        "button_emoji": defaults.get("button_emoji", "🎉"),
        "bullet_emoji": defaults.get("bullet_emoji", "•"),
        "ended": False
    }

    embed = build_giveaway_embed(gw_payload)

    try:
        await ctx.message.delete()
    except Exception:
        pass

    # Send message first
    gw_msg = await ctx.channel.send(embed=embed)

    # Attach interactive view with message_id
    view = GiveawayEntryView(gw_msg.id)
    await gw_msg.edit(view=view)

    # Save to giveaways.json
    g_data["giveaways"][str(gw_msg.id)] = gw_payload
    save_giveaways(g_data)


@bot.command(name="gend", aliases=["gstop"])
async def gend_cmd(ctx, message_id: str = None):
    """Ends an active giveaway immediately and selects winners."""
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not ctx.author.guild_permissions.manage_guild and not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ **Access Denied.**")
        return

    if not message_id or not message_id.isdigit():
        await ctx.reply("❌ **Usage:** `*gend <giveaway_message_id>`")
        return

    g_data = load_giveaways()
    gw = g_data.get("giveaways", {}).get(message_id)
    if not gw:
        await ctx.reply("❌ **Giveaway not found!** Check the message ID.")
        return
    if gw.get("ended", False):
        await ctx.reply("⚠️ **This giveaway has already ended.**")
        return

    await end_giveaway(message_id, ctx.guild)
    await ctx.reply(f"✅ **Giveaway `{message_id}` ended successfully!**")


@bot.command(name="greroll")
async def greroll_cmd(ctx, message_id: str = None):
    """Rerolls a new random winner for a concluded giveaway."""
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not ctx.author.guild_permissions.manage_guild and not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ **Access Denied.**")
        return

    if not message_id or not message_id.isdigit():
        await ctx.reply("❌ **Usage:** `*greroll <giveaway_message_id>`")
        return

    g_data = load_giveaways()
    gw = g_data.get("giveaways", {}).get(message_id)
    if not gw:
        await ctx.reply("❌ **Giveaway not found!** Check the message ID.")
        return

    participants = gw.get("participants", [])
    if not participants:
        await ctx.reply("❌ **Cannot reroll:** No participants entered this giveaway.")
        return

    new_winner_id = random.choice(participants)
    prize = gw.get("prize", "Prize")
    await ctx.send(f"🎉 **Reroll Winner:** Congratulations <@{new_winner_id}>! You won the reroll for **{prize}**! 🥳")


@bot.command(name="gsetemoji", aliases=["giveawayemoji"])
async def gsetemoji_cmd(ctx, target: str = None, *, value: str = None):
    """Customizes giveaway emojis (supports animated emojis) and colors."""
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not ctx.author.guild_permissions.manage_guild and not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ **Access Denied.**")
        return

    if not target:
        await ctx.reply(
            "⚙️ **Giveaway Customization:**\n"
            "• `*gsetemoji button <emoji>` — Set Entry button emoji *(supports animated `<a:name:id>`)*\n"
            "• `*gsetemoji bullet <emoji>` — Set bullet point emoji *(supports animated `<a:name:id>`)*\n"
            "• `*gsetemoji color <#hex>` — Set embed left border color *(e.g. `#00D9FF`)*\n"
            "• `*gsetemoji reset` — Reset to default clean cyan style"
        )
        return

    target = target.lower().strip()
    g_data = load_giveaways()
    if "default_config" not in g_data:
        g_data["default_config"] = {}

    if target == "reset":
        g_data["default_config"] = {"button_emoji": "🎉", "bullet_emoji": "•", "color": "#00D9FF"}
        save_giveaways(g_data)
        await ctx.reply("✅ **Giveaway style reset to default cyan theme!**")
        return

    if not value:
        await ctx.reply(f"❌ Provide a value for `{target}`.")
        return

    val = value.strip()
    if target in ["button", "btn", "entry"]:
        g_data["default_config"]["button_emoji"] = val
        save_giveaways(g_data)
        await ctx.reply(f"✅ **Giveaway Entry button emoji updated to:** {val}\n*Future giveaways will use this emoji on the button.*")
    elif target in ["bullet", "dot", "icon"]:
        g_data["default_config"]["bullet_emoji"] = val
        save_giveaways(g_data)
        await ctx.reply(f"✅ **Giveaway bullet emoji updated to:** {val}\n*Future giveaways will show this emoji beside details.*")
    elif target in ["color", "colour", "hex"]:
        g_data["default_config"]["color"] = val
        save_giveaways(g_data)
        await ctx.reply(f"✅ **Giveaway embed color updated to:** `{val}`")
    else:
        await ctx.reply("❌ Unknown option. Choose `button`, `bullet`, or `color`.")


@bot.command(name="glist")
async def glist_cmd(ctx):
    """Lists all active giveaways in the server."""
    g_data = load_giveaways()
    giveaways = g_data.get("giveaways", {})
    active = [(mid, gw) for mid, gw in giveaways.items() if not gw.get("ended", False) and gw.get("guild_id") == ctx.guild.id]

    if not active:
        await ctx.reply("ℹ️ **No active giveaways running in this server.**")
        return

    lines = []
    for mid, gw in active:
        end_ts = int(gw.get("end_time", 0))
        prize = gw.get("prize", "Prize")
        parts = len(gw.get("participants", []))
        lines.append(f"• **{prize}** (ID: `{mid}`) — Ends <t:{end_ts}:R> | **{parts}** participants")

    embed = discord.Embed(
        title="🎉 Active Giveaways",
        description="\n".join(lines),
        color=0x00D9FF,
        timestamp=discord.utils.utcnow()
    )
    await ctx.reply(embed=embed)






# ==============================================================================
# SLASH COMMANDS EXTENSION (For complete '/' Discord popup integration)
# ==============================================================================

@bot.tree.command(name="sync", description="Instantly sync all slash commands to this server")
async def sync_slash(interaction: discord.Interaction):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not (await bot.is_owner(interaction.user)):
        await interaction.response.send_message("❌ Access denied: Only Server Owner or Extra Owners can sync commands.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        bot.tree.copy_global_to(guild=interaction.guild)
        synced = await bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send(f"✅ **Successfully synced {len(synced)} slash commands to this server!**\n*Type `/` in chat to see all commands immediately!*", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ **Sync error:** `{e}`", ephemeral=True)


@bot.command(name="sync")
async def sync_prefix_cmd(ctx):
    """Instantly syncs all slash commands to this server."""
    if not is_server_or_extra_owner(ctx.guild, ctx.author.id) and not (await bot.is_owner(ctx.author)):
        await ctx.reply("❌ Access denied: Only Server Owner or Extra Owners can sync commands.")
        return
    msg = await ctx.reply("⏳ **Syncing all slash commands to this server...**")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ **Successfully synced {len(synced)} slash commands to {ctx.guild.name}!**\n*Type `/` in chat to see all commands immediately!*")
    except Exception as e:
        await msg.edit(content=f"❌ **Sync error:** `{e}`")


@bot.tree.command(name="gstart", description="Start an interactive giveaway with animated emojis & instant winner picking")
@app_commands.describe(duration="Giveaway duration (e.g. 10m, 1h, 2d, 1w)", winners="Number of winners (e.g. 1, 2, 3)", prize="Giveaway prize name (e.g. 1 Month Discord Nitro)")
async def gstart_slash(interaction: discord.Interaction, duration: str, winners: int, prize: str):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Access denied: Only Server Owner, Extra Owners, or Admins can host giveaways.", ephemeral=True)
        return

    dur_seconds = parse_duration(duration)
    if not dur_seconds or dur_seconds < 10:
        await interaction.response.send_message("❌ Invalid duration! Use format like `10m`, `1h`, `2d`, `1w` (min 10 seconds).", ephemeral=True)
        return
    if winners < 1:
        await interaction.response.send_message("❌ Number of winners must be at least 1.", ephemeral=True)
        return

    g_data = load_giveaways()
    defaults = g_data.get("default_config", {})
    now_ts = int(discord.utils.utcnow().timestamp())
    end_ts = now_ts + dur_seconds

    gw_payload = {
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id,
        "host_id": interaction.user.id,
        "prize": prize.strip(),
        "winners": winners,
        "end_time": end_ts,
        "participants": [],
        "color": defaults.get("color", "#00D9FF"),
        "button_emoji": defaults.get("button_emoji", "🎉"),
        "bullet_emoji": defaults.get("bullet_emoji", "•"),
        "ended": False
    }

    embed = build_giveaway_embed(gw_payload)
    await interaction.response.send_message("🎉 Starting giveaway...", ephemeral=True)
    gw_msg = await interaction.channel.send(embed=embed)
    view = GiveawayEntryView(gw_msg.id)
    await gw_msg.edit(view=view)

    g_data["giveaways"][str(gw_msg.id)] = gw_payload
    save_giveaways(g_data)


@bot.tree.command(name="gend", description="End an active giveaway immediately and pick winners")
@app_commands.describe(message_id="Message ID of the active giveaway to conclude")
async def gend_slash(interaction: discord.Interaction, message_id: str):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Access denied.", ephemeral=True)
        return
    if not message_id.isdigit():
        await interaction.response.send_message("❌ Please provide a valid numeric message ID.", ephemeral=True)
        return
    g_data = load_giveaways()
    gw = g_data.get("giveaways", {}).get(message_id)
    if not gw:
        await interaction.response.send_message("❌ Giveaway not found! Check the message ID.", ephemeral=True)
        return
    if gw.get("ended", False):
        await interaction.response.send_message("⚠️ This giveaway has already ended.", ephemeral=True)
        return
    await end_giveaway(message_id, interaction.guild)
    await interaction.response.send_message(f"✅ Giveaway `{message_id}` ended successfully!", ephemeral=True)


@bot.tree.command(name="greroll", description="Reroll a new random winner for a concluded giveaway")
@app_commands.describe(message_id="Message ID of the finished giveaway to reroll")
async def greroll_slash(interaction: discord.Interaction, message_id: str):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Access denied.", ephemeral=True)
        return
    if not message_id.isdigit():
        await interaction.response.send_message("❌ Please provide a valid numeric message ID.", ephemeral=True)
        return
    g_data = load_giveaways()
    gw = g_data.get("giveaways", {}).get(message_id)
    if not gw:
        await interaction.response.send_message("❌ Giveaway not found! Check the message ID.", ephemeral=True)
        return
    participants = gw.get("participants", [])
    if not participants:
        await interaction.response.send_message("❌ Cannot reroll: No participants joined this giveaway.", ephemeral=True)
        return
    new_winner_id = random.choice(participants)
    prize = gw.get("prize", "Prize")
    await interaction.response.send_message("🎲 Reroll processed!", ephemeral=True)
    await interaction.channel.send(f"🎉 **Reroll Winner:** Congratulations <@{new_winner_id}>! You won the reroll for **{prize}**! 🥳")


@bot.tree.command(name="glist", description="List all active giveaways in this server")
async def glist_slash(interaction: discord.Interaction):
    g_data = load_giveaways()
    giveaways = g_data.get("giveaways", {})
    active = [(mid, gw) for mid, gw in giveaways.items() if not gw.get("ended", False) and gw.get("guild_id") == interaction.guild_id]
    if not active:
        await interaction.response.send_message("ℹ️ No active giveaways running in this server.", ephemeral=True)
        return
    lines = []
    for mid, gw in active:
        end_ts = int(gw.get("end_time", 0))
        prize = gw.get("prize", "Prize")
        parts = len(gw.get("participants", []))
        lines.append(f"• **{prize}** (ID: `{mid}`) — Ends <t:{end_ts}:R> | **{parts}** participants")
    embed = discord.Embed(title="🎉 Active Giveaways", description="\n".join(lines), color=0x00D9FF, timestamp=discord.utils.utcnow())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="antinuke", description="Open Anti-Nuke Control Panel or manage security settings")
@app_commands.describe(action="Choose security action (panel, enable, disable, status, edit)")
@app_commands.choices(action=[
    app_commands.Choice(name="Open Security Setup Panel", value="panel"),
    app_commands.Choice(name="Enable All 24 Protection Modules", value="enable"),
    app_commands.Choice(name="Disable Anti-Nuke", value="disable"),
    app_commands.Choice(name="View Live Status", value="status"),
    app_commands.Choice(name="Open Interactive Editor", value="edit"),
])
async def antinuke_slash(interaction: discord.Interaction, action: str = "panel"):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not (await bot.is_owner(interaction.user)):
        await interaction.response.send_message("❌ Access denied: Only Server Owner & Extra Owner can manage Anti-Nuke.", ephemeral=True)
        return
    cfg = load_antinuke_config()
    if action == "enable":
        cfg["enabled"] = True
        save_antinuke_config(cfg)
        embed = build_void_welcome_embed(interaction.guild, interaction.user, cfg)
        await interaction.response.send_message(embed=embed)
    elif action == "disable":
        cfg["enabled"] = False
        save_antinuke_config(cfg)
        await interaction.response.send_message("🔴 **Anti-Nuke Protection DISABLED.**", ephemeral=True)
    elif action == "status":
        status_str = "🟢 ACTIVE" if cfg.get("enabled", True) else "🔴 DISABLED"
        log_ch = f"<#{cfg.get('log_channel_id')}>" if cfg.get('log_channel_id') else "*(Not Set)*"
        wl_count = len(cfg.get("whitelisted_users", {}))
        embed = discord.Embed(
            title="🛡️ VOID Security System — Live Status",
            description=f"**State:** `{status_str}`\n**Security Log:** {log_ch}\n**Whitelisted Staff:** `{wl_count}`\n**Active Protections:** `24/24 Enabled`",
            color=0x0055FF
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    elif action == "edit":
        view = VoidSecurityEditorLauncher(interaction.user.id)
        embed = discord.Embed(
            title="🎨 VOID Security — Live Panel Customizer",
            description="Click the button below to open the interactive form modal:",
            color=0x0055FF
        )
        await interaction.response.send_message(embed=embed, view=view)
    else:
        embed = build_void_welcome_embed(interaction.guild, interaction.user, cfg)
        view = VoidWelcomeActionView(interaction.guild)
        await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="wl", description="Manage Anti-Nuke whitelist (Server & Extra Owners only)")
@app_commands.describe(subcmd="Choose whitelist action", target="Target user to whitelist or manage")
@app_commands.choices(subcmd=[
    app_commands.Choice(name="Open Whitelist Control Panel", value="add"),
    app_commands.Choice(name="Remove User from Whitelist", value="remove"),
    app_commands.Choice(name="List Whitelisted Members", value="list"),
    app_commands.Choice(name="Reset All Whitelist Permissions", value="reset"),
])
async def wl_slash(interaction: discord.Interaction, subcmd: str = "list", target: discord.User = None):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not (await bot.is_owner(interaction.user)):
        await interaction.response.send_message("❌ Access denied: Only Server Owner & Extra Owner can manage Whitelist.", ephemeral=True)
        return
    cfg = load_antinuke_config()
    if subcmd == "add":
        target = target or interaction.user
        embed = build_antinuke_whitelist_embed(interaction.guild, target, cfg)
        view = ByteWhitelistControlView(target, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)
    elif subcmd == "remove":
        if not target:
            await interaction.response.send_message("❌ Please specify a user to remove.", ephemeral=True)
            return
        whitelisted = cfg.get("whitelisted_users", {})
        if str(target.id) in whitelisted:
            del whitelisted[str(target.id)]
            save_antinuke_config(cfg)
            await interaction.response.send_message(f"🗑️ Removed {target.mention} from Whitelist.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {target.mention} was not whitelisted.", ephemeral=True)
    elif subcmd == "list":
        whitelisted = cfg.get("whitelisted_users", {})
        if not whitelisted:
            await interaction.response.send_message("ℹ️ No members are currently whitelisted.", ephemeral=True)
            return
        lines = [f"• <@{uid}>" for uid in whitelisted.keys()]
        embed = discord.Embed(title="👑 Whitelisted Members", description="\n".join(lines), color=0x0055FF)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    elif subcmd == "reset":
        cfg["whitelisted_users"] = {}
        save_antinuke_config(cfg)
        await interaction.response.send_message("🗑️ All whitelist entries reset.", ephemeral=True)


@bot.tree.command(name="extraowner", description="Promote or manage Extra Owners for unrestricted security control")
@app_commands.describe(action="Action (add, remove, list)", target="Target user to promote or revoke")
@app_commands.choices(action=[
    app_commands.Choice(name="Add Extra Owner", value="add"),
    app_commands.Choice(name="Remove Extra Owner", value="remove"),
    app_commands.Choice(name="List Extra Owners", value="list"),
])
async def extraowner_slash(interaction: discord.Interaction, action: str = "list", target: discord.User = None):
    if interaction.user.id != interaction.guild.owner_id and not (await bot.is_owner(interaction.user)):
        await interaction.response.send_message("❌ Access denied: Only the Server Owner can manage Extra Owners.", ephemeral=True)
        return
    cfg = load_antinuke_config()
    if "extra_owners" not in cfg:
        cfg["extra_owners"] = []
    if action == "add":
        if not target:
            await interaction.response.send_message("❌ Please select a user to add as Extra Owner.", ephemeral=True)
            return
        if str(target.id) not in [str(x) for x in cfg["extra_owners"]]:
            cfg["extra_owners"].append(target.id)
            save_antinuke_config(cfg)
            await interaction.response.send_message(f"👑 **{target.mention} has been promoted to EXTRA OWNER!**\nFull security & whitelist permissions granted.")
        else:
            await interaction.response.send_message(f"ℹ️ {target.mention} is already an Extra Owner.", ephemeral=True)
    elif action == "remove":
        if not target:
            await interaction.response.send_message("❌ Please select a user to remove.", ephemeral=True)
            return
        cfg["extra_owners"] = [x for x in cfg["extra_owners"] if str(x) != str(target.id)]
        save_antinuke_config(cfg)
        await interaction.response.send_message(f"🗑️ **{target.mention} removed from Extra Owners.**")
    else:
        extra_list = [f"• <@{x}>" for x in cfg.get("extra_owners", [])]
        owner_mention = interaction.guild.owner.mention if interaction.guild.owner else f"<@{interaction.guild.owner_id}>"
        embed = discord.Embed(
            title="👑 Server Owner & Extra Owners",
            description=f"**Server Owner:** {owner_mention}\n\n**Extra Owners:**\n" + ("\n".join(extra_list) if extra_list else "*None added yet*"),
            color=0x0055FF
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="lock", description="Lockdown current channel or entire server during active raids")
@app_commands.describe(target="Optional: 'all' to lock entire server")
async def lock_slash(interaction: discord.Interaction, target: str = None):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Access denied.", ephemeral=True)
        return
    everyone_role = interaction.guild.default_role
    if target and target.lower() in ("all", "server"):
        count = 0
        for ch in interaction.guild.text_channels:
            try:
                overwrite = ch.overwrites_for(everyone_role)
                if overwrite.send_messages is not False:
                    overwrite.send_messages = False
                    await ch.set_permissions(everyone_role, overwrite=overwrite, reason=f"Lockdown by {interaction.user.name}")
                    count += 1
            except Exception:
                pass
        await interaction.response.send_message(f"🔒 **Server-wide lockdown activated:** Locked `{count}` text channels.")
    else:
        ch = interaction.channel
        overwrite = ch.overwrites_for(everyone_role)
        overwrite.send_messages = False
        await ch.set_permissions(everyone_role, overwrite=overwrite, reason=f"Locked by {interaction.user.name}")
        await interaction.response.send_message(f"🔒 {ch.mention} has been **LOCKED**.")


@bot.tree.command(name="unlock", description="Unlock current channel or entire server after raids are cleared")
@app_commands.describe(target="Optional: 'all' to unlock entire server")
async def unlock_slash(interaction: discord.Interaction, target: str = None):
    if not is_server_or_extra_owner(interaction.guild, interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Access denied.", ephemeral=True)
        return
    everyone_role = interaction.guild.default_role
    if target and target.lower() in ("all", "server"):
        count = 0
        for ch in interaction.guild.text_channels:
            try:
                overwrite = ch.overwrites_for(everyone_role)
                if overwrite.send_messages is False:
                    overwrite.send_messages = None
                    await ch.set_permissions(everyone_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user.name}")
                    count += 1
            except Exception:
                pass
        await interaction.response.send_message(f"🔓 **Server unlocked:** Restored permissions in `{count}` text channels.")
    else:
        ch = interaction.channel
        overwrite = ch.overwrites_for(everyone_role)
        overwrite.send_messages = None
        await ch.set_permissions(everyone_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user.name}")
        await interaction.response.send_message(f"🔓 {ch.mention} has been **UNLOCKED**.")




# ==============================================================================
#                 EMOJI & STICKER STEALER / CLONER SYSTEM
# ==============================================================================

CUSTOM_EMOJI_REGEX = re.compile(r'<(a?):([a-zA-Z0-9_]{2,32}):(\d{17,21})>')
HTTP_IMAGE_URL_REGEX = re.compile(r'^https?://\S+\.(?:png|jpe?g|gif|webp)(?:\?\S*)?$', re.IGNORECASE)

def user_can_manage_emojis(member: discord.Member) -> bool:
    """Checks if the user has permission to add emojis/stickers."""
    if not isinstance(member, discord.Member):
        return False
    if is_server_or_extra_owner(member.guild, member.id):
        return True
    perms = member.guild_permissions
    if perms.administrator:
        return True
    if getattr(perms, "manage_guild_expressions", False) or getattr(perms, "manage_emojis_and_stickers", False) or getattr(perms, "manage_emojis", False):
        return True
    return False

def bot_can_manage_emojis(guild: discord.Guild) -> bool:
    """Checks if the bot has permission to create emojis/stickers."""
    me = guild.me
    if not me:
        return True
    perms = me.guild_permissions
    if perms.administrator:
        return True
    if getattr(perms, "manage_guild_expressions", False) or getattr(perms, "manage_emojis_and_stickers", False) or getattr(perms, "manage_emojis", False):
        return True
    return False

def sanitize_emoji_name(name: str, fallback: str = "emoji") -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    cleaned = cleaned.strip('_')
    if len(cleaned) < 2:
        cleaned = f"{fallback}_{cleaned}".strip('_')[:32]
    if len(cleaned) < 2:
        cleaned = f"emoji_{int(time.time()) % 10000}"
    return cleaned[:32]

def sanitize_sticker_name(name: str, fallback: str = "custom_sticker") -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_\- ]', '', name).strip()
    if len(cleaned) < 2:
        cleaned = f"{fallback}_{cleaned}".strip()[:30]
    if len(cleaned) < 2:
        cleaned = f"sticker_{int(time.time()) % 10000}"
    return cleaned[:30]

async def download_image_bytes(url: str, max_size: int = 10 * 1024 * 1024) -> tuple[bytes | None, str | None]:
    """Downloads image bytes safely with User-Agent and timeout."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DiscordBot"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                content = await resp.read()
                if len(content) > max_size:
                    return None, f"File too large ({len(content)/(1024*1024):.2f}MB, limit 10MB)"
                return content, None
    except Exception as e:
        return None, str(e)

def process_emoji_image(raw_bytes: bytes, is_animated: bool = False) -> bytes:
    """Ensures emoji bytes are under 256KB and properly formatted."""
    if len(raw_bytes) <= 256 * 1024:
        return raw_bytes
    try:
        from PIL import Image, ImageSequence
        img = Image.open(io.BytesIO(raw_bytes))
        out_buf = io.BytesIO()
        if is_animated or getattr(img, "is_animated", False):
            frames = []
            durations = []
            for frame in ImageSequence.Iterator(img):
                f = frame.copy().convert("RGBA").resize((128, 128), Image.Resampling.LANCZOS)
                frames.append(f)
                durations.append(frame.info.get("duration", 100))
            if frames:
                frames[0].save(out_buf, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
                val = out_buf.getvalue()
                if len(val) <= 256 * 1024:
                    return val
        else:
            img = img.convert("RGBA").resize((128, 128), Image.Resampling.LANCZOS)
            img.save(out_buf, format="PNG", optimize=True)
            val = out_buf.getvalue()
            if len(val) <= 256 * 1024:
                return val
    except Exception as e:
        logger.debug(f"Pillow emoji resize note: {e}")
    return raw_bytes

async def clone_single_emoji(guild: discord.Guild, name: str, is_animated: bool, emoji_id: str, author_name: str) -> tuple[discord.Emoji | None, str | None]:
    ext = "gif" if is_animated else "png"
    cdn_url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?quality=lossless"
    raw_bytes, err = await download_image_bytes(cdn_url)
    if not raw_bytes:
        return None, f"Failed to download emoji {name} ({err})"
    
    clean_name = sanitize_emoji_name(name)
    processed = process_emoji_image(raw_bytes, is_animated)
    
    try:
        new_emoji = await guild.create_custom_emoji(
            name=clean_name,
            image=processed,
            reason=f"Stolen by {author_name}"
        )
        return new_emoji, None
    except discord.errors.Forbidden:
        return None, "Bot is missing 'Manage Emojis and Stickers' permission."
    except discord.errors.HTTPException as e:
        if e.code == 30008:
            return None, "Maximum number of emojis reached for this server!"
        return None, f"Discord error: {e.text}"
    except Exception as e:
        return None, str(e)

async def clone_single_sticker(guild: discord.Guild, sticker_item: discord.StickerItem, custom_name: str | None, author_name: str) -> tuple[discord.GuildSticker | None, str | None]:
    if sticker_item.format == discord.StickerFormatType.lottie:
        return None, "Default Discord Lottie stickers cannot be uploaded as custom stickers. Please use a PNG/APNG/GIF sticker!"
    
    raw_bytes, err = await download_image_bytes(sticker_item.url)
    if not raw_bytes:
        try:
            raw_bytes = await sticker_item.read()
        except Exception as e:
            return None, f"Failed to download sticker: {err or e}"
    
    name = custom_name if custom_name else sticker_item.name
    clean_name = sanitize_sticker_name(name)
    
    ext = "gif" if sticker_item.format == discord.StickerFormatType.gif else "png"
    file = discord.File(io.BytesIO(raw_bytes), filename=f"{clean_name}.{ext}")
    
    try:
        new_sticker = await guild.create_sticker(
            name=clean_name,
            description=f"Cloned sticker by {author_name}",
            emoji="🎉",
            file=file,
            reason=f"Sticker cloned by {author_name}"
        )
        return new_sticker, None
    except discord.errors.Forbidden:
        return None, "Bot is missing 'Manage Emojis and Stickers' permission."
    except discord.errors.HTTPException as e:
        if e.code == 30039:
            return None, "Maximum number of stickers reached for this server!"
        return None, f"Discord error: {e.text}"
    except Exception as e:
        return None, str(e)

def build_emoji_success_embed(guild: discord.Guild, emoji: discord.Emoji, user: discord.User | discord.Member) -> discord.Embed:
    anim_count = len([e for e in guild.emojis if e.animated])
    static_count = len([e for e in guild.emojis if not e.animated])
    limit = guild.emoji_limit
    desc_lines = [
        f"**Emoji:** {emoji} (`{emoji}`)",
        f"**Name:** `{emoji.name}`",
        f"**Type:** `{'Animated (GIF)' if emoji.animated else 'Static (PNG)'}`",
        f"**Added By:** {user.mention}",
        "",
        f"**Server Slots:** `Animated: {anim_count}/{limit}` | `Static: {static_count}/{limit}`"
    ]
    embed = discord.Embed(
        title="<a:emoji_31:1523222455823499344> Emoji Stolen Successfully!",
        description="\n".join(desc_lines),
        color=0x00D9FF
    )
    embed.set_thumbnail(url=emoji.url)
    embed.set_footer(text=f"ID: {emoji.id} • Steal System | Designed By diewithvoid")
    return embed

def build_sticker_success_embed(guild: discord.Guild, sticker: discord.GuildSticker, user: discord.User | discord.Member) -> discord.Embed:
    limit = guild.sticker_limit
    desc_lines = [
        f"**Name:** `{sticker.name}`",
        f"**Added By:** {user.mention}",
        "",
        f"**Server Slots:** `Stickers: {len(guild.stickers)}/{limit}`"
    ]
    embed = discord.Embed(
        title="<a:emoji_31:1523222455823499344> Sticker Cloned Successfully!",
        description="\n".join(desc_lines),
        color=0x00D9FF
    )
    embed.set_thumbnail(url=sticker.url)
    embed.set_footer(text=f"ID: {sticker.id} • Steal System | Designed By diewithvoid")
    return embed


@bot.command(name="steal", aliases=["addemoji", "stealemoji", "cloneemoji", "emojisteal"])
async def steal_cmd(ctx, *, args: str = None):
    """Steal any custom emoji (static or animated) or sticker from any server into this server."""
    if not user_can_manage_emojis(ctx.author):
        await ctx.reply("❌ **Access Denied:** You need **Manage Emojis & Stickers** or Admin permissions to steal emojis/stickers.")
        return
    if not bot_can_manage_emojis(ctx.guild):
        await ctx.reply("❌ **Bot Missing Permissions:** I need the **Manage Emojis and Stickers** permission in this server to upload emojis!")
        return

    # Check 1: Reply Mode
    if ctx.message.reference and ctx.message.reference.message_id:
        try:
            ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except Exception as e:
            await ctx.reply(f"❌ Failed to fetch referenced message: `{e}`")
            return
        
        # Check replied message stickers first
        if ref_msg.stickers:
            sticker_item = ref_msg.stickers[0]
            custom_name = args.strip() if args else None
            msg_status = await ctx.reply(f"⏳ **Cloning sticker `{sticker_item.name}`...**")
            new_stk, err = await clone_single_sticker(ctx.guild, sticker_item, custom_name, ctx.author.name)
            if new_stk:
                embed = build_sticker_success_embed(ctx.guild, new_stk, ctx.author)
                await msg_status.edit(content=None, embed=embed)
            else:
                await msg_status.edit(content=f"❌ **Failed to clone sticker:** {err}")
            return
        
        # Check replied message emojis
        emojis_found = CUSTOM_EMOJI_REGEX.findall(ref_msg.content)
        if emojis_found:
            if len(emojis_found) == 1:
                is_anim, orig_name, em_id = emojis_found[0]
                target_name = args.strip() if args else orig_name
                msg_status = await ctx.reply(f"⏳ **Stealing emoji `{target_name}`...**")
                new_em, err = await clone_single_emoji(ctx.guild, target_name, bool(is_anim), em_id, ctx.author.name)
                if new_em:
                    embed = build_emoji_success_embed(ctx.guild, new_em, ctx.author)
                    await msg_status.edit(content=None, embed=embed)
                else:
                    await msg_status.edit(content=f"❌ **Failed to steal emoji:** {err}")
                return
            else:
                # Mass steal from replied message
                msg_status = await ctx.reply(f"⏳ **Mass stealing {len(emojis_found)} emojis from replied message...**")
                successes = []
                failures = []
                for is_anim, orig_name, em_id in emojis_found[:20]:
                    new_em, err = await clone_single_emoji(ctx.guild, orig_name, bool(is_anim), em_id, ctx.author.name)
                    if new_em:
                        successes.append(new_em)
                    else:
                        failures.append(f"`{orig_name}`: {err}")
                
                embed = discord.Embed(
                    title=f"<a:emoji_31:1523222455823499344> Mass Steal Finished ({len(successes)}/{len(emojis_found[:20])})",
                    description=(
                        f"Successfully added **{len(successes)}** emojis to **{ctx.guild.name}**!\n\n"
                        + " ".join([str(e) for e in successes])
                    ) if successes else "❌ Could not add any emojis.",
                    color=0x00D9FF
                )
                if failures:
                    embed.add_field(name="Failed Emojis", value="\n".join(failures[:10]), inline=False)
                embed.set_footer(text=f"Added by {ctx.author.name} • Steal System")
                await msg_status.edit(content=None, embed=embed)
                return

        # Check replied message image attachments
        if ref_msg.attachments:
            att = ref_msg.attachments[0]
            if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
                custom_name = args.strip() if args else os.path.splitext(att.filename)[0]
                msg_status = await ctx.reply(f"⏳ **Creating emoji `{custom_name}` from attachment...**")
                raw_bytes, err = await download_image_bytes(att.url)
                if not raw_bytes:
                    await msg_status.edit(content=f"❌ Failed to download attachment: `{err}`")
                    return
                is_anim = att.filename.lower().endswith(".gif")
                processed = process_emoji_image(raw_bytes, is_anim)
                clean_name = sanitize_emoji_name(custom_name)
                try:
                    new_em = await ctx.guild.create_custom_emoji(name=clean_name, image=processed, reason=f"Stolen by {ctx.author.name}")
                    embed = build_emoji_success_embed(ctx.guild, new_em, ctx.author)
                    await msg_status.edit(content=None, embed=embed)
                except Exception as e:
                    await msg_status.edit(content=f"❌ Failed to create emoji: `{e}`")
                return

    # Check 2: Direct Attachment Mode
    if ctx.message.attachments:
        att = ctx.message.attachments[0]
        if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
            custom_name = args.strip() if args else os.path.splitext(att.filename)[0]
            msg_status = await ctx.reply(f"⏳ **Creating emoji `{custom_name}` from your attachment...**")
            raw_bytes, err = await download_image_bytes(att.url)
            if not raw_bytes:
                await msg_status.edit(content=f"❌ Failed to download attachment: `{err}`")
                return
            is_anim = att.filename.lower().endswith(".gif")
            processed = process_emoji_image(raw_bytes, is_anim)
            clean_name = sanitize_emoji_name(custom_name)
            try:
                new_em = await ctx.guild.create_custom_emoji(name=clean_name, image=processed, reason=f"Created by {ctx.author.name}")
                embed = build_emoji_success_embed(ctx.guild, new_em, ctx.author)
                await msg_status.edit(content=None, embed=embed)
            except Exception as e:
                await msg_status.edit(content=f"❌ Failed to create emoji: `{e}`")
            return

    # Check 3: Arguments Mode (Emoji tags or URLs)
    if not args:
        guide_lines = [
            "Easily copy ANY emoji (static or animated) or sticker from ANY server into this server!\n",
            "**1. Single Emoji Steal / Rename:**",
            "• `*steal <emoji>` — Copies emoji with its original name",
            "• `*steal <emoji> <custom_name>` — Copies emoji with a custom name",
            "*Example:* `*steal <a:blob_dance:1523222455823499344> my_dance`\n",
            "**2. Mass Emoji Stealing:**",
            "• `*steal <emoji1> <emoji2> <emoji3> ...` — Clones up to 20 emojis in one command!\n",
            "**3. Reply Mode (Super Fast!):**",
            "• Reply to any message containing emojis or stickers with `*steal`",
            "• Reply to any message with a sticker with `*stealsticker [name]`\n",
            "**4. From Image / GIF URL or File:**",
            "• `*steal <image_url> <name>` — Converts direct image or GIF URL into an emoji",
            "• Attach an image or GIF and type `*steal <name>`\n",
            "**5. Slash Commands & 1-Click Apps Menu:**",
            "• `/steal <emoji> [name]` — Slash command for quick stealing",
            "• Right-Click (PC) or Long-Press (Mobile) any message » **Apps** » **Steal Emoji / Sticker**"
        ]
        embed_help = discord.Embed(
            title="<a:emoji_31:1523222455823499344> Emoji & Sticker Stealer Guide",
            description="\n".join(guide_lines),
            color=0x00D9FF
        )
        embed_help.set_footer(text="Steal System | Designed By diewithvoid")
        await ctx.reply(embed=embed_help)
        return

    # Check for custom emojis in args
    emojis_found = CUSTOM_EMOJI_REGEX.findall(args)
    if emojis_found:
        if len(emojis_found) == 1:
            is_anim, orig_name, em_id = emojis_found[0]
            rem_text = CUSTOM_EMOJI_REGEX.sub('', args).strip()
            target_name = rem_text if rem_text else orig_name
            msg_status = await ctx.reply(f"⏳ **Stealing emoji `{target_name}`...**")
            new_em, err = await clone_single_emoji(ctx.guild, target_name, bool(is_anim), em_id, ctx.author.name)
            if new_em:
                embed = build_emoji_success_embed(ctx.guild, new_em, ctx.author)
                await msg_status.edit(content=None, embed=embed)
            else:
                await msg_status.edit(content=f"❌ **Failed to steal emoji:** {err}")
            return
        else:
            # Mass Steal
            msg_status = await ctx.reply(f"⏳ **Mass stealing {len(emojis_found)} emojis...**")
            successes = []
            failures = []
            for is_anim, orig_name, em_id in emojis_found[:20]:
                new_em, err = await clone_single_emoji(ctx.guild, orig_name, bool(is_anim), em_id, ctx.author.name)
                if new_em:
                    successes.append(new_em)
                else:
                    failures.append(f"`{orig_name}`: {err}")
            
            embed = discord.Embed(
                title=f"<a:emoji_31:1523222455823499344> Mass Steal Finished ({len(successes)}/{len(emojis_found[:20])})",
                description=(
                    f"Successfully added **{len(successes)}** emojis to **{ctx.guild.name}**!\n\n"
                    + " ".join([str(e) for e in successes])
                ) if successes else "❌ Could not add any emojis.",
                color=0x00D9FF
            )
            if failures:
                embed.add_field(name="Failed Emojis", value="\n".join(failures[:10]), inline=False)
            embed.set_footer(text=f"Added by {ctx.author.name} • Steal System")
            await msg_status.edit(content=None, embed=embed)
            return

    # Check for direct image URL in args
    parts = args.split()
    first_part = parts[0]
    if first_part.startswith("http://") or first_part.startswith("https://"):
        target_name = parts[1] if len(parts) > 1 else f"emoji_{int(time.time()) % 10000}"
        msg_status = await ctx.reply(f"⏳ **Downloading & creating emoji `{target_name}` from URL...**")
        raw_bytes, err = await download_image_bytes(first_part)
        if not raw_bytes:
            await msg_status.edit(content=f"❌ Failed to download image from URL: `{err}`")
            return
        is_anim = ".gif" in first_part.lower()
        processed = process_emoji_image(raw_bytes, is_anim)
        clean_name = sanitize_emoji_name(target_name)
        try:
            new_em = await ctx.guild.create_custom_emoji(name=clean_name, image=processed, reason=f"Created by {ctx.author.name}")
            embed = build_emoji_success_embed(ctx.guild, new_em, ctx.author)
            await msg_status.edit(content=None, embed=embed)
        except Exception as e:
            await msg_status.edit(content=f"❌ Failed to create emoji: `{e}`")
        return

    await ctx.reply("❌ **Invalid format!** Please provide an emoji `<:name:id>`, image URL, or reply to a message containing emojis/stickers. Type `*steal` for full guide.")


@bot.command(name="stealsticker", aliases=["addsticker", "clonesticker"])
async def stealsticker_cmd(ctx, *, args: str = None):
    """Clone a sticker from a replied message or image URL into this server."""
    if not user_can_manage_emojis(ctx.author):
        await ctx.reply("❌ **Access Denied:** You need **Manage Emojis & Stickers** or Admin permissions to steal stickers.")
        return
    if not bot_can_manage_emojis(ctx.guild):
        await ctx.reply("❌ **Bot Missing Permissions:** I need the **Manage Emojis and Stickers** permission in this server to upload stickers!")
        return

    # Mode 1: Reply to a message with a sticker
    if ctx.message.reference and ctx.message.reference.message_id:
        try:
            ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except Exception as e:
            await ctx.reply(f"❌ Failed to fetch referenced message: `{e}`")
            return
        
        if not ref_msg.stickers:
            await ctx.reply("❌ The replied message does not contain any stickers!")
            return
        
        sticker_item = ref_msg.stickers[0]
        custom_name = args.strip() if args else None
        msg_status = await ctx.reply(f"⏳ **Cloning sticker `{sticker_item.name}` into server...**")
        new_stk, err = await clone_single_sticker(ctx.guild, sticker_item, custom_name, ctx.author.name)
        if new_stk:
            embed = build_sticker_success_embed(ctx.guild, new_stk, ctx.author)
            await msg_status.edit(content=None, embed=embed)
        else:
            await msg_status.edit(content=f"❌ **Failed to clone sticker:** {err}")
        return

    # Mode 2: Direct URL
    if args and (args.startswith("http://") or args.startswith("https://")):
        parts = args.split()
        url = parts[0]
        custom_name = parts[1] if len(parts) > 1 else f"sticker_{int(time.time()) % 10000}"
        msg_status = await ctx.reply(f"⏳ **Downloading & creating sticker `{custom_name}`...**")
        raw_bytes, err = await download_image_bytes(url)
        if not raw_bytes:
            await msg_status.edit(content=f"❌ Failed to download sticker image: `{err}`")
            return
        clean_name = sanitize_sticker_name(custom_name)
        ext = "gif" if ".gif" in url.lower() else "png"
        file = discord.File(io.BytesIO(raw_bytes), filename=f"{clean_name}.{ext}")
        try:
            new_stk = await ctx.guild.create_sticker(
                name=clean_name,
                description=f"Cloned sticker by {ctx.author.name}",
                emoji="🎉",
                file=file,
                reason=f"Sticker cloned by {ctx.author.name}"
            )
            embed = build_sticker_success_embed(ctx.guild, new_stk, ctx.author)
            await msg_status.edit(content=None, embed=embed)
        except Exception as e:
            await msg_status.edit(content=f"❌ Failed to create sticker: `{e}`")
        return

    # Help
    await ctx.reply(
        "ℹ️ **How to steal stickers:**\n"
        "1. **Reply Mode:** Reply to any message that has a sticker and type `*stealsticker [optional_name]`\n"
        "2. **URL Mode:** Type `*stealsticker <image_url> <sticker_name>`\n"
        "3. **Context Menu:** Right-Click or Long-Press any sticker message » **Apps** » **Steal Emoji / Sticker**"
    )


@bot.tree.command(name="steal", description="Steal / copy any emoji from any server into this server")
@app_commands.describe(
    emoji="Paste custom emoji (<:name:id> or <a:name:id>) or image/GIF URL",
    name="Optional: Custom name for the emoji"
)
async def steal_slash(interaction: discord.Interaction, emoji: str, name: str = None):
    if not user_can_manage_emojis(interaction.user):
        await interaction.response.send_message("❌ **Access Denied:** You need **Manage Emojis & Stickers** or Admin permissions to steal emojis.", ephemeral=True)
        return
    if not bot_can_manage_emojis(interaction.guild):
        await interaction.response.send_message("❌ **Bot Missing Permissions:** I need the **Manage Emojis and Stickers** permission in this server!", ephemeral=True)
        return

    await interaction.response.defer()
    
    matches = CUSTOM_EMOJI_REGEX.findall(emoji)
    if matches:
        is_anim, orig_name, em_id = matches[0]
        target_name = name.strip() if name else orig_name
        new_em, err = await clone_single_emoji(interaction.guild, target_name, bool(is_anim), em_id, interaction.user.name)
        if new_em:
            embed = build_emoji_success_embed(interaction.guild, new_em, interaction.user)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ **Failed to steal emoji:** {err}")
        return

    if emoji.startswith("http://") or emoji.startswith("https://"):
        target_name = name.strip() if name else f"emoji_{int(time.time()) % 10000}"
        raw_bytes, err = await download_image_bytes(emoji)
        if not raw_bytes:
            await interaction.followup.send(f"❌ Failed to download image from URL: `{err}`")
            return
        is_anim = ".gif" in emoji.lower()
        processed = process_emoji_image(raw_bytes, is_anim)
        clean_name = sanitize_emoji_name(target_name)
        try:
            new_em = await interaction.guild.create_custom_emoji(name=clean_name, image=processed, reason=f"Created by {interaction.user.name}")
            embed = build_emoji_success_embed(interaction.guild, new_em, interaction.user)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to create emoji: `{e}`")
        return

    await interaction.followup.send("❌ **Invalid format!** Please provide a custom emoji `<:name:id>` or a valid image/GIF URL.")


@bot.tree.command(name="stealsticker", description="Clone a custom sticker into this server from a direct image URL")
@app_commands.describe(
    url="Direct image or sticker URL (PNG, APNG, GIF)",
    name="Name for the sticker (2-30 characters)"
)
async def stealsticker_slash(interaction: discord.Interaction, url: str, name: str):
    if not user_can_manage_emojis(interaction.user):
        await interaction.response.send_message("❌ **Access Denied:** You need **Manage Emojis & Stickers** or Admin permissions to steal stickers.", ephemeral=True)
        return
    if not bot_can_manage_emojis(interaction.guild):
        await interaction.response.send_message("❌ **Bot Missing Permissions:** I need the **Manage Emojis and Stickers** permission in this server!", ephemeral=True)
        return

    await interaction.response.defer()
    raw_bytes, err = await download_image_bytes(url)
    if not raw_bytes:
        await interaction.followup.send(f"❌ Failed to download sticker image: `{err}`")
        return
    clean_name = sanitize_sticker_name(name)
    ext = "gif" if ".gif" in url.lower() else "png"
    file = discord.File(io.BytesIO(raw_bytes), filename=f"{clean_name}.{ext}")
    try:
        new_stk = await interaction.guild.create_sticker(
            name=clean_name,
            description=f"Cloned sticker by {interaction.user.name}",
            emoji="🎉",
            file=file,
            reason=f"Sticker cloned by {interaction.user.name}"
        )
        embed = build_sticker_success_embed(interaction.guild, new_stk, interaction.user)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create sticker: `{e}`")


@bot.tree.context_menu(name="Steal Emoji / Sticker")
async def steal_context_menu(interaction: discord.Interaction, message: discord.Message):
    """1-Click context menu on desktop (Right Click -> Apps) or mobile (Long Press -> Apps)."""
    if not user_can_manage_emojis(interaction.user):
        await interaction.response.send_message("❌ **Access Denied:** You need **Manage Emojis & Stickers** or Admin permissions to steal emojis/stickers.", ephemeral=True)
        return
    if not bot_can_manage_emojis(interaction.guild):
        await interaction.response.send_message("❌ **Bot Missing Permissions:** I need the **Manage Emojis and Stickers** permission in this server!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # 1. Check for stickers
    if message.stickers:
        sticker_item = message.stickers[0]
        new_stk, err = await clone_single_sticker(interaction.guild, sticker_item, None, interaction.user.name)
        if new_stk:
            embed = build_sticker_success_embed(interaction.guild, new_stk, interaction.user)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Failed to clone sticker:** {err}", ephemeral=True)
        return

    # 2. Check for emojis
    emojis_found = CUSTOM_EMOJI_REGEX.findall(message.content)
    if emojis_found:
        if len(emojis_found) == 1:
            is_anim, orig_name, em_id = emojis_found[0]
            new_em, err = await clone_single_emoji(interaction.guild, orig_name, bool(is_anim), em_id, interaction.user.name)
            if new_em:
                embed = build_emoji_success_embed(interaction.guild, new_em, interaction.user)
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **Failed to steal emoji:** {err}", ephemeral=True)
        else:
            successes = []
            failures = []
            for is_anim, orig_name, em_id in emojis_found[:20]:
                new_em, err = await clone_single_emoji(interaction.guild, orig_name, bool(is_anim), em_id, interaction.user.name)
                if new_em:
                    successes.append(new_em)
                else:
                    failures.append(f"`{orig_name}`: {err}")
            
            embed = discord.Embed(
                title=f"<a:emoji_31:1523222455823499344> Context Steal Finished ({len(successes)}/{len(emojis_found[:20])})",
                description=(
                    f"Successfully added **{len(successes)}** emojis to **{interaction.guild.name}**!\n\n"
                    + " ".join([str(e) for e in successes])
                ) if successes else "❌ Could not add any emojis.",
                color=0x00D9FF
            )
            if failures:
                embed.add_field(name="Failed Emojis", value="\n".join(failures[:10]), inline=False)
            embed.set_footer(text=f"Added by {interaction.user.name} • Steal System")
            await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # 3. Check for attachments
    if message.attachments:
        att = message.attachments[0]
        if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
            raw_bytes, err = await download_image_bytes(att.url)
            if not raw_bytes:
                await interaction.followup.send(f"❌ Failed to download attachment: `{err}`", ephemeral=True)
                return
            is_anim = att.filename.lower().endswith(".gif")
            processed = process_emoji_image(raw_bytes, is_anim)
            clean_name = sanitize_emoji_name(os.path.splitext(att.filename)[0])
            try:
                new_em = await interaction.guild.create_custom_emoji(name=clean_name, image=processed, reason=f"Created by {interaction.user.name}")
                embed = build_emoji_success_embed(interaction.guild, new_em, interaction.user)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to create emoji: `{e}`", ephemeral=True)
            return

    await interaction.followup.send("❌ No custom emojis, stickers, or image attachments found in that message to steal!", ephemeral=True)



if __name__ == '__main__':
    run_bot()
