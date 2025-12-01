import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, date

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ===========================
# ENVIRONMENT VARIABLES
# ===========================
TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

service_info = json.loads(os.getenv("GOOGLE_SERVICE_JSON"))
credentials = service_account.Credentials.from_service_account_info(
    service_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

sheets_service = build("sheets", "v4", credentials=credentials)
sheet_api = sheets_service.spreadsheets().values()


# ===========================
# LOCAL IN-MEMORY STORAGE
# ===========================
# Tracks today's sales
daily_sales = {}

# Tracks ALL-TIME sales
all_time_sales = {}   # rep_id -> int


def add_all_time_sale(rep_id):
    if rep_id not in all_time_sales:
        all_time_sales[rep_id] = 0
    all_time_sales[rep_id] += 1


# ===========================
# GOOGLE SHEETS APPEND
# ===========================
def append_sale_to_sheet(rep_name, customer, isp, plan):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    row = [[timestamp, rep_name, customer, isp, plan]]

    sheet_api.append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:E",
        valueInputOption="RAW",
        body={"values": row}
    ).execute()


# ===========================
#   DISCORD ELEMENTS
# ===========================

# -------- Customer Modal --------
class CustomerModal(discord.ui.Modal, title="Enter Customer Name"):
    customer_name = discord.ui.TextInput(
        label="Customer Name",
        placeholder="John Doe",
        required=True
    )

    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction):
        await interaction.response.send_message(
            f"Customer: **{self.customer_name.value}**\nSelect ISP:",
            view=ISPButtons(self.customer_name.value, self.user_id),
            ephemeral=True
        )


# -------- ISP Buttons --------
class ISPButtons(discord.ui.View):
    def __init__(self, customer_name, user_id):
        super().__init__(timeout=120)
        self.customer_name = customer_name
        self.user_id = user_id

    async def pick(self, interaction, isp):
        await interaction.response.send_message(
            f"ISP **{isp}** selected.\nChoose plan:",
            view=PlanDropdown(self.customer_name, isp, self.user_id),
            ephemeral=True
        )

    @discord.ui.button(label="Wire3", style=discord.ButtonStyle.primary)
    async def wire3(self, i, b): await self.pick(i, "Wire3")

    @discord.ui.button(label="Brightspeed", style=discord.ButtonStyle.primary)
    async def brightspeed(self, i, b): await self.pick(i, "Brightspeed")

    @discord.ui.button(label="Kinetic", style=discord.ButtonStyle.primary)
    async def kinetic(self, i, b): await self.pick(i, "Kinetic")

    @discord.ui.button(label="Astound", style=discord.ButtonStyle.primary)
    async def astound(self, i, b): await self.pick(i, "Astound")

    @discord.ui.button(label="Quantum", style=discord.ButtonStyle.primary)
    async def quantum(self, i, b): await self.pick(i, "Quantum")

    @discord.ui.button(label="Bluepeak", style=discord.ButtonStyle.primary)
    async def bluepeak(self, i, b): await self.pick(i, "Bluepeak")


# -------- Plan Dropdown --------
class PlanDropdown(discord.ui.View):
    def __init__(self, customer, isp, user_id):
        super().__init__(timeout=120)
        self.add_item(PlanSelect(customer, isp, user_id))


class PlanSelect(discord.ui.Select):
    def __init__(self, customer, isp, user_id):
        self.customer = customer
        self.isp = isp
        self.user_id = user_id

        options = [
            discord.SelectOption(label="500mbps"),
            discord.SelectOption(label="1G"),
            discord.SelectOption(label="1G+"),
        ]

        super().__init__(placeholder="Choose a plan…", options=options)

    async def callback(self, interaction):

        plan = self.values[0]
        rep_id = self.user_id
        rep_name = interaction.user.display_name

        # Track today's sales
        daily_sales.setdefault(rep_id, 0)
        daily_sales[rep_id] += 1

        # Track all-time
        add_all_time_sale(rep_id)

        # Log to Google Sheets
        append_sale_to_sheet(rep_name, self.customer, self.isp, plan)

        await interaction.response.send_message(
            f"✅ **Sale Logged!**\n"
            f"Rep: **{rep_name}**\n"
            f"Customer: **{self.customer}**\n"
            f"ISP: **{self.isp}**\n"
            f"Plan: **{plan}**\n"
            f"Total all-time sales: **{all_time_sales[rep_id]}**",
            ephemeral=False
        )


# ===========================
#   BOT SETUP
# ===========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot is live as {bot.user}")


# ===========================
#   SLASH COMMANDS
# ===========================

# ----- SALE -----
@bot.tree.command(name="sale", description="Log a new sale")
async def sale(interaction):
    await interaction.response.send_modal(CustomerModal(interaction.user.id))


# ----- MY SALES (TODAY) -----
@bot.tree.command(name="mysales", description="Your sales today")
async def mysales(interaction):
    rep_id = interaction.user.id
    count = daily_sales.get(rep_id, 0)
    await interaction.response.send_message(
        f"You have **{count}** sales today.", ephemeral=True
    )


# ----- LEADERBOARD (ALL-TIME) -----
@bot.tree.command(name="leaderboard", description="View all-time sales leaderboard")
async def leaderboard(interaction):

    if not all_time_sales:
        return await interaction.response.send_message("No all-time sales recorded yet.")

    sorted_reps = sorted(all_time_sales.items(), key=lambda x: x[1], reverse=True)

    text = "🏆 **All-Time Leaderboard**\n\n"
    for i, (uid, total) in enumerate(sorted_reps, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else "Unknown"
        text += f"**{i}. {name}** — {total} sales\n"

    await interaction.response.send_message(text)


# ----- RESET -----
@bot.tree.command(name="reset", description="Reset all sales (Admin Only)")
async def reset(interaction):

    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    global daily_sales, all_time_sales
    daily_sales = {}
    all_time_sales = {}

    await interaction.response.send_message("🧹 All sales reset successfully.")


# ===========================
# RUN BOT
# ===========================
bot.run(TOKEN)
