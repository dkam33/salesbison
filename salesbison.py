import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from datetime import date, datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ----------------------------
# Load environment variables
# ----------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

service_info = json.loads(os.getenv("GOOGLE_SERVICE_JSON"))
credentials = service_account.Credentials.from_service_account_info(
    service_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

sheets_service = build("sheets", "v4", credentials=credentials)
sheet_api = sheets_service.spreadsheets().values()

# ----------------------------
# Local daily sales storage
# ----------------------------
sales_data = {}
current_day = date.today()

def reset_if_new_day():
    global current_day, sales_data
    if date.today() != current_day:
        current_day = date.today()
        sales_data = {}

# ----------------------------
# Discord Dropdowns
# ----------------------------
class ISPDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Wire3"),
            discord.SelectOption(label="Brightspeed"),
            discord.SelectOption(label="Kinetic"),
            discord.SelectOption(label="Astound"),
            discord.SelectOption(label="Quantum"),
            discord.SelectOption(label="Bluepeak")
        ]
        super().__init__(placeholder="Select ISP", min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        self.view.isp = self.values[0]
        await interaction.response.send_message(f"**ISP Selected:** {self.values[0]}", ephemeral=True)


class PlanDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="500mbps"),
            discord.SelectOption(label="1G"),
            discord.SelectOption(label="1G+")
        ]
        super().__init__(placeholder="Select Plan", min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        self.view.plan = self.values[0]
        await interaction.response.send_message(f"**Plan Selected:** {self.values[0]}", ephemeral=True)


class SaleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.isp = None
        self.plan = None
        self.add_item(ISPDropdown())
        self.add_item(PlanDropdown())

# ----------------------------
# Google Sheets Helper
# ----------------------------
def append_sale_to_sheet(rep, customer, isp, plan):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    row = [[timestamp, rep, customer, isp, plan]]

    sheet_api.append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:E",
        valueInputOption="RAW",
        body={"values": row}
    ).execute()

# ----------------------------
# Discord Bot Setup
# ----------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot is live as {bot.user}")

# ----------------------------
# /sale Command
# ----------------------------
@bot.tree.command(name="sale", description="Log a new sale")
@app_commands.describe(customer_name="Customer's name")
async def sale(interaction: discord.Interaction, customer_name: str):
    reset_if_new_day()

    view = SaleView()
    await interaction.response.send_message(
        f"Logging sale for **{customer_name}**.\nSelect ISP and Plan below:",
        view=view
    )

    await view.wait()

    if not view.isp or not view.plan:
        await interaction.followup.send("Sale cancelled. Missing selection.", ephemeral=True)
        return

    rep_id = interaction.user.id
    rep_name = interaction.user.display_name

    # track in memory
    if rep_id not in sales_data:
        sales_data[rep_id] = {"count": 0, "details": []}

    sales_data[rep_id]["count"] += 1
    sales_data[rep_id]["details"].append({
        "customer": customer_name,
        "isp": view.isp,
        "plan": view.plan
    })

    # send to Google Sheets
    append_sale_to_sheet(rep_name, customer_name, view.isp, view.plan)

    await interaction.followup.send(
        f"✅ **Sale Logged**\n"
        f"Rep: {rep_name}\n"
        f"Customer: **{customer_name}**\n"
        f"ISP: **{view.isp}**\n"
        f"Plan: **{view.plan}**\n\n"
        f"You now have **{sales_data[rep_id]['count']}** sales today."
    )

# ----------------------------
# /mysales
# ----------------------------
@bot.tree.command(name="mysales", description="View your sales today")
async def mysales(interaction: discord.Interaction):
    reset_if_new_day()

    rep_id = interaction.user.id
    count = sales_data.get(rep_id, {}).get("count", 0)

    await interaction.response.send_message(
        f"You have **{count}** sales today.",
        ephemeral=True
    )

# ----------------------------
# /leaderboard
# ----------------------------
@bot.tree.command(name="leaderboard", description="Today's leaderboard")
async def leaderboard(interaction: discord.Interaction):
    reset_if_new_day()

    if not sales_data:
        await interaction.response.send_message("No sales logged today yet.")
        return

    sorted_reps = sorted(sales_data.items(), key=lambda x: x[1]["count"], reverse=True)

    text = "🏆 **Today's Leaderboard**\n\n"
    for i, (uid, info) in enumerate(sorted_reps, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else "Unknown"
        text += f"**{i}. {name}** — {info['count']} sales\n"

    await interaction.response.send_message(text)

# ----------------------------
# /reset (Admin Only)
# ----------------------------
@bot.tree.command(name="reset", description="Reset today's sales")
async def reset(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return

    global sales_data, current_day
    sales_data = {}
    current_day = date.today()

    await interaction.response.send_message("✅ Sales reset for today.")

# ----------------------------
# Run Bot
# ----------------------------
bot.run(TOKEN)
