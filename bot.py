import os
import sys
import json
import socket
import logging
from datetime import datetime
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# --- Single Instance Lock (Cloud & Local Safe) ---
try:
    _instance_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _instance_lock_socket.bind(("127.0.0.1", 5057))
except Exception:
    pass

# Setup logging
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
log_file = os.path.join(BASE_DIR, "bot.log")
log_handlers = [logging.StreamHandler(sys.stdout)] if sys.stdout is not None else []
try:
    log_file = os.path.join(BASE_DIR, "bot.log")
    log_handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("ByteWhitelistBot")

# Load environment variables
env_file = os.path.join(BASE_DIR, ".env")
config_file = os.path.join(BASE_DIR, "config.json")
load_dotenv(dotenv_path=env_file)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
API_BASE_URL = os.getenv("API_BASE_URL", "https://mani272uidbypass.vercel.app/api/v1/uids").rstrip("/")
ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID", "").strip()
LOG_CHANNEL_ID_ENV = os.getenv("LOG_CHANNEL_ID", "").strip()

if not DISCORD_TOKEN or not API_KEY:
    logger.error("DISCORD_TOKEN ya API_KEY .env file me set nahi hai!")
    sys.exit(1)


def load_config() -> dict:
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
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


def get_log_channel_id() -> int | None:
    cfg = load_config()
    cfg_id = cfg.get("log_channel_id")
    if cfg_id:
        try:
            return int(cfg_id)
        except (ValueError, TypeError):
            pass
    if LOG_CHANNEL_ID_ENV and LOG_CHANNEL_ID_ENV.isdigit():
        return int(LOG_CHANNEL_ID_ENV)
    return None


def get_admin_role_id() -> int | None:
    cfg = load_config()
    cfg_role = cfg.get("admin_role_id")
    if cfg_role:
        try:
            return int(cfg_role)
        except (ValueError, TypeError):
            pass
    if ADMIN_ROLE_ID and ADMIN_ROLE_ID.isdigit():
        return int(ADMIN_ROLE_ID)
    return None


def has_permission(user: discord.Member | discord.User) -> tuple[bool, str]:
    """
    Check if member has permission.
    Returns (has_permission, reason_message)
    """
    if not isinstance(user, discord.Member):
        return False, "Commands sirf server channel me use kiye ja sakte hain."

    # Server Administrator always has permission
    if user.guild_permissions.administrator:
        return True, "Administrator"

    role_id = get_admin_role_id()
    if role_id:
        if any(r.id == role_id for r in user.roles):
            return True, f"<@&{role_id}>"
        return False, f"Aapke pass <@&{role_id}> role hona zaroori hai tabhi aap bot use kar sakte hain."

    return False, "Aapke pass Administrator permission nahi hai ya bot ka role set nahi hua (*setrole @Role)."


# --- API Helper Functions ---
async def api_add_uid(uid: str, days: int = 30):
    url = f"{API_BASE_URL}/add"
    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationWhitelistBot/1.0",
    }
    payload = {"uid": str(uid).strip(), "days": int(days)}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=15) as resp:
                data = await resp.json()
                return resp.status, data
    except aiohttp.ClientConnectorError:
        return 500, {"success": False, "error": "API server se connection nahi ho saka."}
    except Exception as e:
        logger.error(f"Error in api_add_uid: {e}")
        return 500, {"success": False, "error": str(e)}


async def api_remove_uid(uid: str):
    url = f"{API_BASE_URL}/remove"
    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "ByteCorporationWhitelistBot/1.0",
    }
    payload = {"uid": str(uid).strip()}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=15) as resp:
                data = await resp.json()
                return resp.status, data
    except aiohttp.ClientConnectorError:
        return 500, {"success": False, "error": "API server se connection nahi ho saka."}
    except Exception as e:
        logger.error(f"Error in api_remove_uid: {e}")
        return 500, {"success": False, "error": str(e)}


async def api_check_uid(uid: str):
    """Check if UID is whitelisted by attempting probe add."""
    _, result = await api_add_uid(uid, days=1)
    if result.get("success"):
        # UID wasn't whitelisted, remove the probe add immediately
        await api_remove_uid(uid)
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


async def dispatch_log(guild: discord.Guild, embed: discord.Embed):
    """Send audit log to the designated logging channel."""
    channel_id = get_log_channel_id()
    if not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            channel = None

    if channel and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send log to channel {channel_id}: {e}")


def create_panel_embed(guild: discord.Guild = None, bot_instance: commands.Bot = None) -> discord.Embed:
    """Creates the clean, sleek dark panel inside a box with server logo."""
    embed = discord.Embed(
        title="🛡️ BYTE CORPORATION — UID WL SYSTEM",
        description=(
            ">>> **Version:** `1.0.0` · ✔ **Active**\n\n"
            "• **Whitelist UID** — new registration (UID / days / note)\n"
            "• **Extend UID** — extend / renew active UID validity\n"
            "• **Check UID** — verify if player UID is whitelisted\n"
            "• **Delete Record** — terminate / delete UID from whitelist\n"
            "• **System Status** — check bot latency & API connection\n\n"
            "BYTE CORPORATION Security Shield"
        ),
        color=0x2B2D31,
    )
    # Server logo thumbnail
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif bot_instance and bot_instance.user and bot_instance.user.display_avatar:
        embed.set_thumbnail(url=bot_instance.user.display_avatar.url)

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
                "❌ **Invalid Days:** Kripya days me valid positive number daalein (e.g. 1, 7, 30).",
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
                description=f"Player UID `{raw_uid}` ko **{days} din** ke liye whitelist kar diya gaya hai.",
                color=0x00FF88,
                timestamp=datetime.utcnow(),
            )
            reply_embed.add_field(name="🆔 UID", value=f"`{raw_uid}`", inline=True)
            reply_embed.add_field(name="⏳ Duration", value=f"`{days} Days`", inline=True)
            reply_embed.add_field(name="📅 Expiry", value=expiry_str, inline=False)
            await interaction.followup.send(embed=reply_embed, ephemeral=True)

            # Audit Log in separate channel
            log_embed = discord.Embed(
                title="📋 BYTE CORPORATION — UID WHITELIST LOG",
                color=0x00FF88,
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
                await dispatch_log(interaction.guild, log_embed)
        else:
            err = result.get("error") or "Unknown error"
            error_embed = discord.Embed(
                title="❌ Whitelist Failed",
                description=f"**Error:** {err}\n**Target UID:** `{raw_uid}`",
                color=0xFF3366,
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
            await interaction.response.send_message(
                "❌ **Invalid Days:** Kripya valid number daalein.",
                ephemeral=True,
            )
            return

        days = int(raw_days)
        await interaction.response.defer(ephemeral=True)

        # Renew by re-adding with new duration
        await api_remove_uid(raw_uid)
        _, result = await api_add_uid(raw_uid, days=days)

        if result.get("success"):
            data = result.get("data", {})
            expiry_str = format_expiry(data.get("expiresAt", ""))

            reply_embed = discord.Embed(
                title="⏳ UID Extended Successfully",
                description=f"UID `{raw_uid}` ki validity **{days} din** ke liye renew kar di gayi hai.",
                color=0x00FF88,
                timestamp=datetime.utcnow(),
            )
            reply_embed.add_field(name="🆔 UID", value=f"`{raw_uid}`", inline=True)
            reply_embed.add_field(name="⏳ Validity", value=f"`{days} Days`", inline=True)
            reply_embed.add_field(name="📅 New Expiry", value=expiry_str, inline=False)
            await interaction.followup.send(embed=reply_embed, ephemeral=True)

            # Audit Log
            log_embed = discord.Embed(
                title="⏳ BYTE CORPORATION — UID EXTENSION LOG",
                color=0x00D26A,
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
                await dispatch_log(interaction.guild, log_embed)
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
                description=f"UID `{raw_uid}` hamare system me **Active & Whitelisted** hai.",
                color=0x00FF88,
            )
        else:
            embed = discord.Embed(
                title="❌ UID Status: Not Whitelisted",
                description=f"UID `{raw_uid}` whitelist database me **Nahi mila**.",
                color=0xFF3366,
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
                description=f"UID `{raw_uid}` ko whitelist se permanently delete kar diya gaya hai.",
                color=0xFFAA00,
                timestamp=datetime.utcnow(),
            )
            await interaction.followup.send(embed=reply_embed, ephemeral=True)

            # Audit Log in separate channel
            log_embed = discord.Embed(
                title="🗑️ BYTE CORPORATION — UID REMOVAL LOG",
                color=0xFF3366,
                timestamp=datetime.utcnow(),
            )
            log_embed.add_field(name="🎯 Removed UID", value=f"```\n{raw_uid}\n```", inline=False)
            log_embed.add_field(name="👤 Deleted By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
            log_embed.add_field(name="📝 Reason", value=f"`{reason}`", inline=True)
            if "gtcStatus" in result:
                log_embed.add_field(name="ℹ️ System Note", value=f"`{result['gtcStatus']}`", inline=False)
            log_embed.set_footer(text=f"Admin: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

            if interaction.guild:
                await dispatch_log(interaction.guild, log_embed)
        else:
            err = result.get("error") or result.get("message") or "UID remove nahi ho saka."
            error_embed = discord.Embed(
                title="❌ Removal Failed",
                description=f"**Error:** {err}\n**Target UID:** `{raw_uid}`",
                color=0xFF3366,
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


# --- Persistent Interactive Panel View ---
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

    # --- Row 0 ---
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

    # --- Row 1 ---
    @discord.ui.button(
        label="Bot Status",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="byte_btn_wl_status",
        row=1,
    )
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        latency = round(interaction.client.latency * 1000)
        log_id = get_log_channel_id()
        role_id = get_admin_role_id()
        log_chan_text = f"<#{log_id}>" if log_id else "`Not Set (*setlogchannel)`"
        role_text = f"<@&{role_id}>" if role_id else "`Not Set (*setrole)`"

        embed = discord.Embed(
            title="🛡️ BYTE CORPORATION SYSTEM STATUS",
            color=0x2B2D31,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="⚡ Bot Latency", value=f"`{latency}ms`", inline=True)
        embed.add_field(name="🟢 API Cluster", value="`ONLINE (v1.0.0)`", inline=True)
        embed.add_field(name="👥 Authorized Role", value=role_text, inline=True)
        embed.add_field(name="📋 Audit Log Channel", value=log_chan_text, inline=False)
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
        log_id = get_log_channel_id()
        if log_id:
            msg = f"✅ Current Audit Log Channel: <#{log_id}>\nBadalne ke liye: `*setlogchannel #channel`"
        else:
            msg = "⚠️ Abhi koi Log Channel set nahi hai.\nSet karne ke liye: `*setlogchannel #channel` run karein."
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
                "### ⚡ Prefix: `*`\n"
                "• `*panel` — Spawn the Control Panel\n"
                "• `*setrole @Role` — Set authorized role required to use bot\n"
                "• `*setlogchannel #channel` — Set dedicated Audit Log channel\n"
                "• `*adduid <UID> [1/7/30]` — Quick command to whitelist\n"
                "• `*removeuid <UID>` — Quick command to remove UID\n\n"
                "### 🖱️ Button Features:\n"
                "• **Whitelist UID** — 1, 7, ya 30 din ke liye UID whitelist karein\n"
                "• **Extend UID** — UID ki validity badhayein\n"
                "• **Check UID** — Check karein UID whitelisted hai ya nahi\n"
                "• **Delete Record** — Whitelist se UID delete karein"
            ),
            color=0x2B2D31,
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


# --- Bot Setup ---
class WhitelistBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="*",
            intents=intents,
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="BYTE CORPORATION | *panel",
            ),
        )

    async def setup_hook(self):
        # Register persistent view so buttons work across restarts
        self.add_view(BytePanelWhitelistView())
        logger.info("Persistent BytePanelWhitelistView registered.")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash Commands synced: {len(synced)} commands.")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="BYTE CORPORATION | *panel",
            ),
        )


bot = WhitelistBot()


# --- Slash Commands ---
@bot.tree.command(name="panel", description="Send the BYTE CORPORATION UID Whitelist Control Panel")
async def send_panel(interaction: discord.Interaction):
    has_perm, reason = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return

    embed = create_panel_embed(guild=interaction.guild, bot_instance=interaction.client)
    view = BytePanelWhitelistView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Control Panel successfully send kar diya gaya!", ephemeral=True)


@bot.tree.command(name="setrole", description="Set the role required to use the bot and whitelist UID")
@app_commands.describe(role="The Discord role that will have permission to use the bot")
async def set_role_slash(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Sirf Server Administrator hi permission role set kar sakte hain.", ephemeral=True)
        return

    cfg = load_config()
    cfg["admin_role_id"] = role.id
    save_config(cfg)

    embed = discord.Embed(
        title="✅ Whitelist Role Configured",
        description=f"Ab se sirf {role.mention} role wale members hi bot ke buttons aur commands use kar sakte hain.",
        color=0x00FF88,
    )
    embed.set_footer(text="BYTE CORPORATION Security Shield")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setlogchannel", description="Set the channel where whitelist audit logs will be posted")
@app_commands.describe(channel="Text channel for logs")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    has_perm, reason = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return

    cfg = load_config()
    cfg["log_channel_id"] = channel.id
    save_config(cfg)

    embed = discord.Embed(
        title="✅ Log Channel Configured",
        description=f"Whitelist logs ab se {channel.mention} channel me aayenge.",
        color=0x00FF88,
    )
    embed.set_footer(text="BYTE CORPORATION Security Shield")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="whitelist", description="Whitelist a UID with duration")
@app_commands.describe(
    uid="Game User ID to whitelist",
    days="Select validity: 1, 7, or 30 days",
    note="Customer details / Order ID (Optional)",
)
@app_commands.choices(
    days=[
        app_commands.Choice(name="1 Day", value=1),
        app_commands.Choice(name="7 Days", value=7),
        app_commands.Choice(name="30 Days", value=30),
    ]
)
async def whitelist_slash(interaction: discord.Interaction, uid: str, days: app_commands.Choice[int] = None, note: str = "None"):
    has_perm, reason = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason}", ephemeral=True)
        return

    chosen_days = days.value if days else 30
    await interaction.response.defer()
    _, result = await api_add_uid(uid, days=chosen_days)

    if result.get("success"):
        data = result.get("data", {})
        expiry_str = format_expiry(data.get("expiresAt", ""))

        embed = discord.Embed(
            title="✅ UID Whitelisted Successfully",
            color=0x00FF88,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="🆔 UID", value=f"`{uid}`", inline=True)
        embed.add_field(name="⏳ Duration", value=f"`{chosen_days} Days`", inline=True)
        embed.add_field(name="⚡ Status", value=f"`{data.get('status', 'active').upper()}`", inline=True)
        embed.add_field(name="📅 Expiry", value=expiry_str, inline=False)
        embed.set_footer(text="BYTE CORPORATION Security Shield", icon_url=bot.user.display_avatar.url)
        await interaction.followup.send(embed=embed)

        # Dispatch log to separate channel
        log_embed = discord.Embed(
            title="📋 BYTE CORPORATION — UID WHITELIST LOG",
            color=0x00FF88,
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="🎯 Player UID", value=f"```\n{uid}\n```", inline=False)
        log_embed.add_field(name="👤 Whitelisted By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
        log_embed.add_field(name="⏳ Duration", value=f"`{chosen_days} Days`", inline=True)
        log_embed.add_field(name="📅 Expiry Date", value=expiry_str, inline=False)
        if note != "None":
            log_embed.add_field(name="📝 Note", value=f"`{note}`", inline=False)
        log_embed.set_footer(text=f"Admin: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

        if interaction.guild:
            await dispatch_log(interaction.guild, log_embed)
    else:
        err = result.get("error") or "Unknown error"
        embed = discord.Embed(
            title="❌ Whitelist Failed",
            description=f"**Error:** {err}\n**Target UID:** `{uid}`",
            color=0xFF3366,
        )
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="removeuid", description="Remove a UID from the whitelist")
@app_commands.describe(uid="Game User ID to remove", reason="Reason for removal (Optional)")
async def remove_slash(interaction: discord.Interaction, uid: str, reason: str = "None"):
    has_perm, reason_msg = has_permission(interaction.user)
    if not has_perm:
        await interaction.response.send_message(f"❌ **Permission Denied:** {reason_msg}", ephemeral=True)
        return

    await interaction.response.defer()
    _, result = await api_remove_uid(uid)

    if result.get("success"):
        embed = discord.Embed(
            title="🗑️ UID Removed Successfully",
            description=f"UID `{uid}` whitelist se delete ho gaya.",
            color=0xFFAA00,
        )
        await interaction.followup.send(embed=embed)

        # Send removal log
        log_embed = discord.Embed(
            title="🗑️ BYTE CORPORATION — UID REMOVAL LOG",
            color=0xFF3366,
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="🎯 Removed UID", value=f"```\n{uid}\n```", inline=False)
        log_embed.add_field(name="👤 Removed By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
        if reason != "None":
            log_embed.add_field(name="📝 Reason", value=f"`{reason}`", inline=False)
        log_embed.set_footer(text=f"Admin: {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

        if interaction.guild:
            await dispatch_log(interaction.guild, log_embed)
    else:
        err = result.get("error") or "Failed to remove UID"
        await interaction.followup.send(f"❌ **Failed:** {err}")


@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 **Pong:** `{latency}ms`", ephemeral=True)


# --- Prefix Commands (Prefix: *) ---
@bot.command(name="panel")
async def panel_prefix(ctx):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ Permission denied! {reason}")
        return
    embed = create_panel_embed(guild=ctx.guild, bot_instance=ctx.bot)
    view = BytePanelWhitelistView()
    await ctx.send(embed=embed, view=view)


@bot.command(name="setrole")
async def setrole_prefix(ctx, role: discord.Role = None):
    if not ctx.author.guild_permissions.administrator:
        await ctx.reply("❌ Sirf Server Administrator hi permission role set kar sakte hain.")
        return
    if not role:
        await ctx.reply("Usage: `*setrole @RoleName`")
        return

    cfg = load_config()
    cfg["admin_role_id"] = role.id
    save_config(cfg)
    await ctx.reply(f"✅ Ab se sirf {role.mention} role wale members hi bot ke buttons aur whitelist commands use kar sakte hain!")


@bot.command(name="setlogchannel")
async def setlogchannel_prefix(ctx, channel: discord.TextChannel = None):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ Permission denied! {reason}")
        return
    target = channel or ctx.channel
    cfg = load_config()
    cfg["log_channel_id"] = target.id
    save_config(cfg)
    await ctx.reply(f"✅ Log channel set to {target.mention}")


@bot.command(name="adduid")
async def adduid_prefix(ctx, uid: str = None, days: int = 30):
    has_perm, reason = has_permission(ctx.author)
    if not has_perm:
        await ctx.reply(f"❌ Permission denied! {reason}")
        return
    if not uid:
        await ctx.reply("Usage: `*adduid <UID> [1/7/30 days]` (Default: 30)")
        return

    async with ctx.typing():
        _, result = await api_add_uid(uid, days=days)

    if result.get("success"):
        data = result.get("data", {})
        expiry_str = format_expiry(data.get("expiresAt", ""))
        await ctx.reply(f"✅ UID `{uid}` whitelisted for **{days} days**! Expiry: {expiry_str}")

        # Dispatch log
        log_embed = discord.Embed(
            title="📋 BYTE CORPORATION — UID WHITELIST LOG",
            color=0x00FF88,
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="🎯 Player UID", value=f"`{uid}`", inline=True)
        log_embed.add_field(name="👤 Whitelisted By", value=f"{ctx.author.mention}", inline=True)
        log_embed.add_field(name="⏳ Duration", value=f"`{days} Days`", inline=True)
        log_embed.add_field(name="📅 Expiry Date", value=expiry_str, inline=False)
        log_embed.set_footer(text=f"Admin: {ctx.author.name}")

        if ctx.guild:
            await dispatch_log(ctx.guild, log_embed)
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
                color=0xFF3366,
                timestamp=datetime.utcnow(),
            )
            log_embed.add_field(name="🎯 Removed UID", value=f"`{uid}`", inline=True)
            log_embed.add_field(name="👤 Removed By", value=f"{ctx.author.mention}", inline=True)
            await dispatch_log(ctx.guild, log_embed)
    else:
        err = result.get("error", "Error removing UID")
        await ctx.reply(f"❌ **Failed:** {err}")


if __name__ == "__main__":
    logger.info("Starting BYTE CORPORATION UID Whitelist Bot (Prefix: *)...")
    bot.run(DISCORD_TOKEN)
