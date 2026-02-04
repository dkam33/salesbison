import os
import json
import discord
from discord.ext import commands
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ===========================
# TIMEZONE
# ===========================
ET = ZoneInfo("America/New_York")

# ===========================
# ENVIRONMENT VARIABLES
# ===========================
TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

SALES_CHANNEL_ID = int(os.getenv("SALES_CHANNEL_ID", "0"))         # REQUIRED
MANAGERS_CHANNEL_ID = int(os.getenv("MANAGERS_CHANNEL_ID", "0"))   # REQUIRED
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0"))                # Optional: faster command sync during dev
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "0"))   # REQUIRED for /totals

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var.")
if not GOOGLE_SHEET_ID:
    raise RuntimeError("Missing GOOGLE_SHEET_ID env var.")
if not os.getenv("GOOGLE_SERVICE_JSON"):
    raise RuntimeError("Missing GOOGLE_SERVICE_JSON env var.")
if SALES_CHANNEL_ID == 0:
    raise RuntimeError("Missing/invalid SALES_CHANNEL_ID env var.")
if MANAGERS_CHANNEL_ID == 0:
    raise RuntimeError("Missing/invalid MANAGERS_CHANNEL_ID env var.")
if ADMIN_CHANNEL_ID == 0:
    raise RuntimeError("Missing/invalid ADMIN_CHANNEL_ID env var.")


ALLOWED_CHANNEL_IDS = {SALES_CHANNEL_ID, MANAGERS_CHANNEL_ID, ADMIN_CHANNEL_ID}

# ===========================
# GOOGLE SHEETS CLIENT
# ===========================
service_info = json.loads(os.getenv("GOOGLE_SERVICE_JSON"))
credentials = service_account.Credentials.from_service_account_info(
    service_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets_service = build("sheets", "v4", credentials=credentials)
sheet_api = sheets_service.spreadsheets().values()

# ===========================
# SHEET RANGES / SCHEMA
# ===========================
# Sheet1 columns:
#   A Timestamp
#   B RepId
#   C RepName
#   D Manager
#   E Customer
#   F ISP
#   G Plan
SHEET_RANGE = "Sheet1!A:G"

# Roster columns:
#   A RepId
#   B RepName
#   C Manager
#   D Active (TRUE/FALSE)
ROSTER_RANGE = "Roster!A:D"

# ===========================
# HELPERS: CHANNEL GATING
# ===========================
async def require_allowed_channel(interaction: discord.Interaction) -> bool:
    """Allow only #sales or #managers. Return True if ok, else respond and return False."""
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message(
            f"This bot only works in <#{SALES_CHANNEL_ID}> or <#{MANAGERS_CHANNEL_ID}>.",
            ephemeral=True
        )
        return False
    return True
    
async def require_admin_channel(interaction: discord.Interaction) -> bool:
    if interaction.channel_id != ADMIN_CHANNEL_ID:
        await interaction.response.send_message(
            f"Admin-only command. Use it in <#{ADMIN_CHANNEL_ID}>.",
            ephemeral=True
        )
        return False
    return True

async def require_admin_permission(interaction: discord.Interaction) -> bool:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return False
    return True


# ===========================
# GOOGLE SHEETS: APPEND
# ===========================
def append_sale_to_sheet(rep_id: int, rep_name: str, manager: str, customer: str, isp: str, plan: str):
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    row = [[ts, str(rep_id), rep_name, manager, customer, isp, plan]]
    sheet_api.append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=SHEET_RANGE,
        valueInputOption="RAW",
        body={"values": row},
    ).execute()

# ===========================
# TIMESTAMP PARSING
# ===========================
def _parse_et_timestamp(ts_str: str):
    """
    Expects: 'YYYY-MM-DD HH:MM:SS ET'
    Returns aware datetime in ET or None if parsing fails.
    """
    try:
        dt = datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S ET")
        return dt.replace(tzinfo=ET)
    except Exception:
        return None

# ===========================
# READ SALES ROWS
# ===========================
def fetch_sales_rows():
    """Returns all rows excluding header (if present)."""
    resp = sheet_api.get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=SHEET_RANGE
    ).execute()

    values = resp.get("values", [])
    if not values:
        return []

    # Safe header detection
    first_row = values[0] if values else []
    first = [str(c).strip().lower() for c in first_row] if first_row else []
    header_like = False
    if first:
        header_like = ("timestamp" in first[0]) or ("rep" in "".join(first))
    if header_like:
        values = values[1:]

    return values

# ===========================
# COUNTS
# ===========================
def compute_counts(rows, *, mode: str, key: str = "rep"):
    """
    mode in {"daily","monthly","ytd","all"}
    key in {"rep","manager"} determines grouping.

    Columns (Sheet1):
      A Timestamp
      B RepId
      C RepName
      D Manager
      E Customer
      F ISP
      G Plan
    """
    now = datetime.now(ET)
    counts = {}

    for r in rows:
        if len(r) < 4:
            continue

        ts = _parse_et_timestamp(str(r[0]))
        rep_id = str(r[1]).strip()
        rep_name = str(r[2]).strip()
        manager = str(r[3]).strip()

        if not ts:
            continue

        include = False
        if mode == "all":
            include = True
        elif mode == "daily":
            include = (ts.date() == now.date())
        elif mode == "monthly":
            include = (ts.year == now.year and ts.month == now.month)
        elif mode == "ytd":
            include = (ts.year == now.year)

        if not include:
            continue

        if key == "manager":
            k = manager or "Unassigned"
        else:
            # Use stable RepId for grouping
            k = rep_id if rep_id else rep_name

        counts[k] = counts.get(k, 0) + 1

    return counts

def get_rep_counts(rep_id: int):
    """Returns {"daily": n, "monthly": n, "ytd": n} computed from the sheet."""
    rows = fetch_sales_rows()
    rep_key = str(rep_id)
    daily = compute_counts(rows, mode="daily", key="rep").get(rep_key, 0)
    monthly = compute_counts(rows, mode="monthly", key="rep").get(rep_key, 0)
    ytd = compute_counts(rows, mode="ytd", key="rep").get(rep_key, 0)
    return {"daily": daily, "monthly": monthly, "ytd": ytd}

def get_total_counts():
    rows = fetch_sales_rows()
    daily = sum(compute_counts(rows, mode="daily", key="rep").values())
    monthly = sum(compute_counts(rows, mode="monthly", key="rep").values())
    ytd = sum(compute_counts(rows, mode="ytd", key="rep").values())
    all_time = sum(compute_counts(rows, mode="all", key="rep").values())
    return {"daily": daily, "monthly": monthly, "ytd": ytd, "all": all_time}


# ===========================
# ROSTER LOOKUP (RepId -> Manager / RepName)
# ===========================
_ROSTER_CACHE = {"ts": 0, "map": {}}
_ROSTER_TTL_SECONDS = 120  # refresh every 2 minutes

def _now_unix():
    return int(datetime.now(timezone.utc).timestamp())

def fetch_roster_rows():
    resp = sheet_api.get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=ROSTER_RANGE
    ).execute()
    return resp.get("values", [])

def build_roster_map(values):
    """
    Returns dict:
      rep_id(int) -> {"rep_name": str, "manager": str, "active": bool}
    """
    if not values:
        return {}

    # Safe header detection
    first_row = values[0] if values else []
    first = [str(c).strip().lower() for c in first_row] if first_row else []
    if first and ("repid" in first[0] or "manager" in "".join(first)):
        values = values[1:]

    out = {}
    for row in values:
        if len(row) < 3:
            continue

        rep_id_str = str(row[0]).strip()
        rep_name = str(row[1]).strip() if len(row) >= 2 else ""
        manager = str(row[2]).strip() if len(row) >= 3 else ""
        active_raw = str(row[3]).strip().lower() if len(row) >= 4 else "true"

        try:
            rep_id = int(rep_id_str)
        except Exception:
            continue

        active = active_raw not in ("false", "0", "no", "n")
        if rep_id and manager:
            out[rep_id] = {"rep_name": rep_name, "manager": manager, "active": active}

    return out

def get_roster_map_cached():
    now = _now_unix()
    if _ROSTER_CACHE["map"] and (now - _ROSTER_CACHE["ts"] < _ROSTER_TTL_SECONDS):
        return _ROSTER_CACHE["map"]

    values = fetch_roster_rows()
    m = build_roster_map(values)

    _ROSTER_CACHE["ts"] = now
    _ROSTER_CACHE["map"] = m
    return m

def lookup_manager_for_rep(rep_id: int):
    info = get_roster_map_cached().get(rep_id)
    if not info:
        return None
    if not info.get("active", True):
        return None
    return info.get("manager")

def get_rep_name_map():
    """RepId -> RepName map from Roster only (fast + stable)."""
    rep_map = {}
    roster = get_roster_map_cached()
    for rep_id, info in roster.items():
        name = (info.get("rep_name") or "").strip()
        if name:
            rep_map[str(rep_id)] = name
    return rep_map

# ===========================
# DISCORD UI: SALE FLOW
# ===========================
class CustomerModal(discord.ui.Modal, title="Enter Customer Name"):
    customer_name = discord.ui.TextInput(
        label="Customer Name",
        placeholder="John Doe",
        required=True,
        max_length=80
    )

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_allowed_channel(interaction):
            return

        embed = discord.Embed(
            title="Customer received",
            description=f"**{self.customer_name.value}**\n\nSelect ISP:",
            color=discord.Color.blurple()
        )

        await interaction.response.send_message(
            embed=embed,
            view=ISPButtons(self.customer_name.value, self.user_id),
            ephemeral=True
        )

class ISPButtons(discord.ui.View):
    def __init__(self, customer_name: str, user_id: int):
        super().__init__(timeout=120)
        self.customer_name = customer_name
        self.user_id = user_id

    async def pick(self, interaction: discord.Interaction, isp: str):
        if not await require_allowed_channel(interaction):
            return

        embed = discord.Embed(
            title="ISP selected",
            description=f"**{isp}**\n\nChoose plan:",
            color=discord.Color.green()
        )
        await interaction.response.send_message(
            embed=embed,
            view=PlanDropdown(self.customer_name, isp, self.user_id),
            ephemeral=True
        )

    @discord.ui.button(label="Wire3", style=discord.ButtonStyle.primary)
    async def wire3(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Wire3")

    @discord.ui.button(label="Omni", style=discord.ButtonStyle.primary)
    async def omni(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Omni")

    @discord.ui.button(label="Brightspeed", style=discord.ButtonStyle.primary)
    async def brightspeed(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Brightspeed")

    @discord.ui.button(label="Kinetic", style=discord.ButtonStyle.primary)
    async def kinetic(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Kinetic")

    @discord.ui.button(label="Astound", style=discord.ButtonStyle.primary)
    async def astound(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Astound")

    @discord.ui.button(label="Quantum", style=discord.ButtonStyle.primary)
    async def quantum(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Quantum")

    @discord.ui.button(label="Bluepeak", style=discord.ButtonStyle.primary)
    async def bluepeak(self, i: discord.Interaction, b: discord.ui.Button):
        await self.pick(i, "Bluepeak")

class PlanDropdown(discord.ui.View):
    def __init__(self, customer: str, isp: str, user_id: int):
        super().__init__(timeout=120)
        self.add_item(PlanSelect(customer, isp, user_id))

class PlanSelect(discord.ui.Select):
    def __init__(self, customer: str, isp: str, user_id: int):
        self.customer = customer
        self.isp = isp
        self.user_id = user_id

        options = [
            discord.SelectOption(label="500mbps"),
            discord.SelectOption(label="1G"),
            discord.SelectOption(label="1G+"),
        ]
        super().__init__(
            placeholder="Choose a plan…",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_allowed_channel(interaction):
            return

        plan = self.values[0]
        rep_id = interaction.user.id
        rep_name = interaction.user.display_name

        manager = lookup_manager_for_rep(rep_id)
        if not manager:
            await interaction.response.send_message(
                "⚠️ You’re not assigned to a manager yet (or you’re inactive). "
                "An admin needs to add you to the **Roster** sheet.",
                ephemeral=True
            )
            return

        try:
            append_sale_to_sheet(rep_id, rep_name, manager, self.customer, self.isp, plan)
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ Could not log to Google Sheets. Try again.\n`{type(e).__name__}: {e}`",
                ephemeral=True
            )
            return

        counts = get_rep_counts(rep_id)

        embed = discord.Embed(title="✅ Sale Logged!", color=discord.Color.gold())
        embed.add_field(name="Rep", value=rep_name, inline=False)
        embed.add_field(name="Customer", value=self.customer, inline=False)
        embed.add_field(name="ISP", value=self.isp, inline=True)
        embed.add_field(name="Plan", value=plan, inline=True)
        embed.add_field(name="Today's Sales", value=str(counts["daily"]), inline=False)
        embed.set_footer(text="Logged to Google Sheets")

        await interaction.response.send_message(embed=embed, ephemeral=False)

# ===========================
# DISCORD UI: LEADERBOARD MODE SELECT
# ===========================
class LeaderboardModeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Daily", value="daily", description="Today only"),
            discord.SelectOption(label="Monthly", value="monthly", description="This month"),
            discord.SelectOption(label="YTD", value="ytd", description="Year-to-date"),
        ]
        super().__init__(
            placeholder="Choose leaderboard timeframe…",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_allowed_channel(interaction):
            return

        mode = self.values[0]
        await interaction.response.defer()  # not ephemeral so it posts normally

        rows = fetch_sales_rows()
        counts = compute_counts(rows, mode=mode, key="rep")

        if not counts:
            await interaction.followup.send("No sales found for that timeframe.", ephemeral=True)
            return

        rep_name_map = get_rep_name_map()
        sorted_reps = sorted(counts.items(), key=lambda x: x[1], reverse=True)

        title_map = {
            "daily": "🏆 Daily Leaderboard",
            "monthly": "🏆 Monthly Leaderboard",
            "ytd": "🏆 YTD Leaderboard",
        }
        embed = discord.Embed(title=title_map.get(mode, "🏆 Leaderboard"), color=discord.Color.gold())

        medals = ["🥇", "🥈", "🥉"]
        for idx, (rep_id_str, total) in enumerate(sorted_reps[:25], start=1):
            rank_icon = medals[idx - 1] if idx <= 3 else f"#{idx}"
            display_name = rep_name_map.get(str(rep_id_str), f"Unknown ({rep_id_str})")
            embed.add_field(name=f"{rank_icon} {display_name}", value=f"**{total}** sales", inline=False)

        embed.set_footer(text="Counts pulled from Google Sheets")
        await interaction.followup.send(embed=embed)

class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(LeaderboardModeSelect())

# ===========================
# DISCORD UI: MANAGER LEADERBOARD
# ===========================
class ManagerboardModeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Daily", value="daily", description="Today only"),
            discord.SelectOption(label="Monthly", value="monthly", description="This month"),
            discord.SelectOption(label="YTD", value="ytd", description="Year-to-date"),
        ]
        super().__init__(
            placeholder="Choose manager leaderboard timeframe…",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_allowed_channel(interaction):
            return

        mode = self.values[0]
        await interaction.response.defer()

        rows = fetch_sales_rows()
        counts = compute_counts(rows, mode=mode, key="manager")

        if not counts:
            await interaction.followup.send("No sales found for that timeframe.")
            return

        sorted_mgrs = sorted(counts.items(), key=lambda x: x[1], reverse=True)

        title_map = {
            "daily": "🏆 Manager Leaderboard (Daily)",
            "monthly": "🏆 Manager Leaderboard (Monthly)",
            "ytd": "🏆 Manager Leaderboard (YTD)",
        }
        embed = discord.Embed(title=title_map.get(mode, "🏆 Manager Leaderboard"), color=discord.Color.gold())

        medals = ["🥇", "🥈", "🥉"]
        for idx, (mgr, total) in enumerate(sorted_mgrs[:25], start=1):
            rank_icon = medals[idx - 1] if idx <= 3 else f"#{idx}"
            embed.add_field(name=f"{rank_icon} {mgr}", value=f"**{total}** sales", inline=False)

        embed.set_footer(text="Counts pulled from Google Sheets")
        await interaction.followup.send(embed=embed)

class ManagerboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(ManagerboardModeSelect())

# ===========================
# BOT SETUP
# ===========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    if DEV_GUILD_ID:
        guild = discord.Object(id=DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Bot is live as {bot.user} (guild sync)")
        print("Synced commands:", [c.name for c in synced])
    else:
        synced = await bot.tree.sync()
        print(f"Bot is live as {bot.user} (global sync)")
        print("Synced commands:", [c.name for c in synced])

# ===========================
# SLASH COMMANDS
# ===========================
@bot.tree.command(name="totals", description="Admin totals: Daily, Monthly, YTD, All-time")
async def totals(interaction: discord.Interaction):
    # allow bot to run in your allowed channels generally
    if not await require_allowed_channel(interaction):
        return
    # hard gate: only in admin channel
    if not await require_admin_channel(interaction):
        return
    # optional: only admins can run it
    if not await require_admin_permission(interaction):
        return

    await interaction.response.defer()  # public message in admin channel

    totals = get_total_counts()
    now = datetime.now(ET)

    embed = discord.Embed(title="📈 Total Sales", color=discord.Color.green())
    embed.add_field(name="Daily", value=str(totals["daily"]), inline=True)
    embed.add_field(name="Monthly", value=str(totals["monthly"]), inline=True)
    embed.add_field(name="YTD", value=str(totals["ytd"]), inline=True)
    embed.add_field(name="All-time", value=str(totals["all"]), inline=True)
    embed.set_footer(text=f"As of {now.strftime('%Y-%m-%d %H:%M:%S ET')}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="sale", description="Log a new sale (#sales or #managers)")
async def sale(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    await interaction.response.send_modal(CustomerModal(interaction.user.id))

@bot.tree.command(name="leaderboard", description="Show rep leaderboard: Daily, Monthly, or YTD (#sales or #managers)")
async def leaderboard(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return

    embed = discord.Embed(
        title="Leaderboard",
        description="Pick a timeframe:",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=LeaderboardView(), ephemeral=True)

@bot.tree.command(name="managerboard", description="Show manager leaderboard: Daily, Monthly, or YTD (#sales or #managers)")
async def managerboard(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return

    embed = discord.Embed(
        title="Manager Leaderboard",
        description="Pick a timeframe:",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=ManagerboardView(), ephemeral=True)

@bot.tree.command(name="mysales", description="View your sales: Daily, Monthly, YTD (#sales or #managers)")
async def mysales(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    rep_id = interaction.user.id
    counts = get_rep_counts(rep_id)

    embed = discord.Embed(title="📊 Your Sales", color=discord.Color.blue())
    embed.add_field(name="Daily", value=str(counts["daily"]), inline=True)
    embed.add_field(name="Monthly", value=str(counts["monthly"]), inline=True)
    embed.add_field(name="YTD", value=str(counts["ytd"]), inline=True)
    embed.set_footer(text="Counts pulled from Google Sheets")

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="myid", description="Show your Discord User ID (useful for the Roster sheet)")
async def myid(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    await interaction.response.send_message(f"Your Discord User ID is: `{interaction.user.id}`", ephemeral=True)

@bot.tree.command(name="reset", description="Reset bot (admin only) — does NOT delete Google Sheet rows")
async def reset(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🧹 Reset complete",
        description="Bot state reset. Google Sheet data was NOT changed.",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ===========================
# RUN BOT
# ===========================
bot.run(TOKEN)

