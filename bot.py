import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
STAFF_ROLE_NAME = "Staff"
CATEGORY_NAME = "Tickets"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label="🔒 Chiudi ticket",
                                style=discord.ButtonStyle.danger,
                                custom_id="ticket:close")
        btn.callback = self.close
        self.add_item(btn)

    async def close(self, interaction: discord.Interaction):
        await interaction.response.send_message("Chiusura del ticket tra 5 secondi...")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket chiuso da {interaction.user}")


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label="🎫 Apri ticket",
                                style=discord.ButtonStyle.primary,
                                custom_id="ticket:open")
        btn.callback = self.open
        self.add_item(btn)

    async def open(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        existing = discord.utils.get(guild.text_channels, name=f"ticket-{user.name.lower()}")
        if existing:
            await interaction.response.send_message(
                f"Hai già un ticket aperto: {existing.mention}", ephemeral=True)
            return

        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(CATEGORY_NAME)

        staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  manage_channels=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket di {user} ({user.id})",
        )

        embed = discord.Embed(
            title="Ticket aperto",
            description=f"Ciao {user.mention}! Descrivi il tuo problema, lo staff ti risponderà a breve.",
            color=discord.Color.green(),
        )
        await channel.send(
            content=staff_role.mention if staff_role else None,
            embed=embed,
            view=CloseView(),
        )
        await interaction.response.send_message(
            f"Ticket creato: {channel.mention}", ephemeral=True)


@bot.tree.command(name="setup", description="Pubblica il pannello per aprire i ticket")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Supporto",
        description="Clicca il pulsante qui sotto per aprire un ticket.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=TicketView())
    await interaction.response.send_message("Pannello pubblicato ✅", ephemeral=True)


@setup.error
async def setup_error(interaction: discord.Interaction, error):
    await interaction.response.send_message("Serve il permesso Amministratore.", ephemeral=True)


@bot.event
async def on_ready():
    bot.add_view(TicketView())
    bot.add_view(CloseView())
    await bot.tree.sync()
    print(f"Online come {bot.user}")


bot.run(TOKEN)