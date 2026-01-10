import os
import json
import discord
from discord.ext import commands
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ===========================
# ENVIRONMENT VARIABLES
# ===========================

TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

SALES_CHANNEL_ID = int(os.getenv("SALES_CHANNEL_ID", "0"))         # REQUIRED
MANAGERS_CHANNEL_ID = int(os.getenv("MANAGERS_CHANNEL_ID", "0"))   # REQUIRED

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

ALLOWED_CHANNEL_IDS = {SALES_CHANNEL_ID, MANAGERS_CHANNEL_ID}

service_info = json.loads(os.getenv("GOOGLE_SERVICE_JSON"))
credentials = service_account.Credentials.from_service_account_info(
    service_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets_service = build("sheets", "v4", credentials=credentials)
sheet_api = sheets_service.spreadsheets().values()

SHEET_RANGE = "Sheet1!A:E"  # Timestamp | RepName | Customer | ISP | Plan

# ===========================
# HELPERS: CHANNEL GATING
# ===========================
async def require_allowed_channel(interaction: discord.Interaction) -> bool:
    """Allow only #sales or #managers. Return True if ok, else respond and return False."""
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message(
            "This bot only works in **#sales** or **#managers**.",
            ephemeral=True
        )
        return False
    return True

# ===========================
# GOOGLE SHEETS: APPEND + READ COUNTS
# ===========================
def append_sale_to_sheet(rep_name: str, customer: str, isp: str, plan: str):
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    row = [[ts, rep_name, customer, isp, plan]]
    sheet_api.append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=SHEET_RANGE,
        valueInputOption="RAW",
        body={"values": row},
    ).execute()

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

def fetch_sales_rows():
    """
    Returns all rows excluding header (if present).
    """
    resp = sheet_api.get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=SHEET_RANGE
    ).execute()

    values = resp.get("values", [])
    if not values:
        return []

    # If first row looks like a header, drop it
    first = [c.strip().lower() for c in values[0]]
    header_like = ("timestamp" in first[0]) or ("rep" in "".join(first))
    if header_like:
        values = values[1:]

    return values

def compute_counts(rows, *, mode: str):
    """
    mode in {"daily","monthly","ytd","all"}
    Uses RepName (col B) and Timestamp (col A).
    """
    now = datetime.now(ET)
    counts = {}

    for r in rows:
        if len(r) < 2:
            continue

        ts = _parse_et_timestamp(r[0]) if len(r) >= 1 else None
        rep = r[1].strip() if len(r) >= 2 else ""
        if not rep or not ts:
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

        if include:
            counts[rep] = counts.get(rep, 0) + 1

    return counts

def get_rep_counts(rep_name: str):
    """
    Returns {"daily": n, "monthly": n, "ytd": n}
    computed from the sheet so manual deletions are reflected.
    """
    rows = fetch_sales_rows()
    daily = compute_counts(rows, mode="daily").get(rep_name, 0)
    monthly = compute_counts(rows, mode="monthly").get(rep_name, 0)
    ytd = compute_counts(rows, mode="ytd").get(rep_name, 0)
    return {"daily": daily, "monthly": monthly, "ytd": ytd}

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
        # Enforce allowed channels even after modal submit
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
    async def Omni(self, i: discord.Interaction, b: discord.ui.Button):
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
        rep_name = interaction.user.display_name

        # 1) append to sheet
        append_sale_to_sheet(rep_name, self.customer, self.isp, plan)

        # 2) fetch today's sales FROM SHEET (so deletions/cancels update counts)
        counts = get_rep_counts(rep_name)

        # confirmation embed
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
        await interaction.response.defer()  # avoid "interaction failed" while we fetch sheet

        rows = fetch_sales_rows()
        counts = compute_counts(rows, mode=mode)

        if not counts:
            await interaction.followup.send("No sales found for that timeframe.", ephemeral=True)
            return

        sorted_reps = sorted(counts.items(), key=lambda x: x[1], reverse=True)

        title_map = {
            "daily": "🏆 Daily Leaderboard",
            "monthly": "🏆 Monthly Leaderboard",
            "ytd": "🏆 YTD Leaderboard",
        }
        embed = discord.Embed(title=title_map.get(mode, "🏆 Leaderboard"), color=discord.Color.gold())

        medals = ["🥇", "🥈", "🥉"]
        for idx, (rep, total) in enumerate(sorted_reps[:25], start=1):
            rank_icon = medals[idx - 1] if idx <= 3 else f"#{idx}"
            embed.add_field(name=f"{rank_icon} {rep}", value=f"**{total}** sales", inline=False)

        embed.set_footer(text="Counts pulled from Google Sheets")
        await interaction.followup.send(embed=embed)

class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(LeaderboardModeSelect())

# ===========================
# BOT SETUP
# ===========================
intents = discord.Intents.default()
intents.members = True  # for general usefulness; counts use sheet rep names anyway

bot = commands.Bot(command_prefix="!", intents=intents)

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0"))

@bot.event
async def on_ready():
    if DEV_GUILD_ID:
        guild = discord.Object(id=DEV_GUILD_ID)
        await bot.tree.sync(guild=guild)
        print(f"Bot is live as {bot.user} (guild sync)")
    else:
        await bot.tree.sync()
        print(f"Bot is live as {bot.user} (global sync)")


# ===========================
# SLASH COMMANDS
# ===========================
@bot.tree.command(name="sale", description="Log a new sale (#sales or #managers)")
async def sale(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return
    await interaction.response.send_modal(CustomerModal(interaction.user.id))

@bot.tree.command(name="leaderboard", description="Show leaderboard: Daily, Monthly, or YTD (#sales or #managers)")
async def leaderboard(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return

    embed = discord.Embed(
        title="Leaderboard",
        description="Pick a timeframe:",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=LeaderboardView(), ephemeral=True)

@bot.tree.command(name="mysales", description="View your sales: Daily, Monthly, YTD (#sales or #managers)")
async def mysales(interaction: discord.Interaction):
    if not await require_allowed_channel(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    rep_name = interaction.user.display_name
    counts = get_rep_counts(rep_name)

    embed = discord.Embed(title="📊 Your Sales", color=discord.Color.blue())
    embed.add_field(name="Daily", value=str(counts["daily"]), inline=True)
    embed.add_field(name="Monthly", value=str(counts["monthly"]), inline=True)
    embed.add_field(name="YTD", value=str(counts["ytd"]), inline=True)
    embed.set_footer(text="Counts pulled from Google Sheets")

    await interaction.followup.send(embed=embed, ephemeral=True)

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
