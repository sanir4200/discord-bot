import os
import re
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
STAFF_ROLE_NAME = "Staff"
CATEGORY_NAME = "Tickets"

# API CoralMC: /api/v1/stats/bedwars/{username}
# Se conosci l'host esatto dalla documentazione, lascia solo quello in lista.
CORAL_API_HOSTS = ["https://api.coralmc.it", "https://www.coralmc.it"]
CORAL_API_PATH = "/api/v1/stats/bedwars/"
CORAL_PROFILE = "https://coralmc.it/it/stats/player/"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

# Numero divisione -> nome. 4, 5 e 7 sono confermati dal sito;
# 1-3 sono un'ipotesi e 6 non è noto (viene mostrato come "Divisione 6").
DIVISIONS = {1: "Bronzo", 2: "Argento", 3: "Oro", 4: "Platino", 5: "Diamante", 7: "Campione"}
CLAN_ROLES = {3: "Leader"}

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


# Tipi di ticket. "ticket:open" resta l'ID di Assistenza, così i pannelli
# già pubblicati continuano a funzionare.
TICKET_TYPES = {
    "assistenza": {
        "label": "Assistenza",
        "emoji": "🛠️",
        "style": discord.ButtonStyle.secondary,
        "custom_id": "ticket:open",
        "prefix": "assistenza",
        "welcome": "Descrivi il tuo problema, lo staff ti risponderà a breve.",
    },
    "clan": {
        "label": "Provino Clan",
        "emoji": "🛡️",
        "style": discord.ButtonStyle.secondary,
        "custom_id": "ticket:clan",
        "prefix": "provino-clan",
        "welcome": "Scrivi qui la tua richiesta per il provino clan, lo staff ti risponderà a breve.",
    },
    "staff": {
        "label": "Provino Staff",
        "emoji": "⚜️",
        "style": discord.ButtonStyle.secondary,
        "custom_id": "ticket:staff",
        "prefix": "provino-staff",
        "welcome": "Presentati e scrivi perché vuoi entrare nello staff, ti risponderemo a breve.",
    },
}


async def create_ticket(interaction: discord.Interaction, kind: str):
    cfg = TICKET_TYPES[kind]
    guild = interaction.guild
    user = interaction.user

    # Un solo ticket aperto per tipo e per utente
    existing = discord.utils.find(
        lambda ch: (ch.topic or "").startswith(f"[{kind}]") and (ch.topic or "").endswith(f"({user.id})"),
        guild.text_channels,
    )
    if existing:
        await interaction.response.send_message(
            f"Hai già un ticket di tipo {cfg['label']} aperto: {existing.mention}", ephemeral=True)
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
        name=f"{cfg['prefix']}-{user.name}",
        category=category,
        overwrites=overwrites,
        topic=f"[{kind}] Ticket di {user} ({user.id})",
    )

    embed = discord.Embed(
        title=f"{cfg['emoji']} {cfg['label']}",
        description=f"Ciao {user.mention}! {cfg['welcome']}",
        color=discord.Color.green(),
    )
    await channel.send(
        content=staff_role.mention if staff_role else None,
        embed=embed,
        view=CloseView(),
    )
    await interaction.response.send_message(
        f"Ticket creato: {channel.mention}", ephemeral=True)


def make_ticket_callback(kind: str):
    async def callback(interaction: discord.Interaction):
        await create_ticket(interaction, kind)
    return callback


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for kind, cfg in TICKET_TYPES.items():
            btn = discord.ui.Button(label=cfg["label"],
                                    emoji=cfg["emoji"],
                                    style=cfg["style"],
                                    custom_id=cfg["custom_id"])
            btn.callback = make_ticket_callback(kind)
            self.add_item(btn)


# ---------------------------------------------------------------- /stats ---

def fmt(n) -> str:
    """19972 -> '19.972' (stile italiano)."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "N/D"


def ratio(a, b) -> str:
    value = a / b if b else float(a)
    return f"{value:.2f}".replace(".", ",")


def division_name(n) -> str:
    return DIVISIONS.get(n, f"Divisione {n}")


def build_stats_embed(username: str, d: dict) -> discord.Embed:
    kills = d.get("kills") or 0
    deaths = d.get("deaths") or 0
    final_kills = d.get("final_kills") or 0
    final_deaths = d.get("final_deaths") or 0
    wins = d.get("wins") or 0
    played = d.get("played") or 0
    losses = d.get("losses")
    if losses is None:
        losses = max(played - wins, 0)
    winrate = f"{(wins / played * 100):.1f}%".replace(".", ",") if played else "N/D"

    # Intestazione: divisione attuale e clan
    header = []
    if d.get("current_division") is not None:
        header.append(f"**Divisione:** {division_name(d['current_division'])}")
    if d.get("clan_name"):
        role = CLAN_ROLES.get(d.get("clan_role"))
        header.append(f"**Clan:** {d['clan_name']}" + (f" · {role}" if role else ""))

    embed = discord.Embed(
        title=f"Statistiche Bedwars · {username}",
        url=f"{CORAL_PROFILE}{username}",
        description="\n".join(header) or None,
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=f"https://minotar.net/helm/{username}/100.png")

    embed.add_field(name="Uccisioni", value=fmt(kills), inline=True)
    embed.add_field(name="Morti", value=fmt(deaths), inline=True)
    embed.add_field(name="K/D", value=ratio(kills, deaths), inline=True)

    embed.add_field(name="Uccisioni finali", value=fmt(final_kills), inline=True)
    embed.add_field(name="Morti finali", value=fmt(final_deaths), inline=True)
    embed.add_field(name="FKDR", value=ratio(final_kills, final_deaths), inline=True)

    embed.add_field(name="Letti rotti", value=fmt(d.get("beds_broken")), inline=True)
    embed.add_field(name="Winstreak", value=fmt(d.get("winstreak")), inline=True)
    embed.add_field(name="Top winstreak", value=fmt(d.get("h_winstreak")), inline=True)

    embed.add_field(name="Livello", value=f"{fmt(d.get('level'))}★", inline=True)
    rank, total = d.get("level_rank"), d.get("total_players")
    if rank and total:
        top = max(rank / total * 100, 0.1)
        rank_text = f"#{fmt(rank)}\nTop {top:.1f}%".replace(".", ",")
    elif rank:
        rank_text = f"#{fmt(rank)}"
    else:
        rank_text = "N/D"
    embed.add_field(name="Classifica globale", value=rank_text, inline=True)
    embed.add_field(name="Monete", value=fmt(d.get("coins")), inline=True)

    embed.add_field(name="Vittorie", value=fmt(wins), inline=True)
    embed.add_field(name="Sconfitte", value=fmt(losses), inline=True)
    embed.add_field(name="% vittorie", value=winrate, inline=True)

    past = sorted(d.get("past_divisions") or [], key=lambda s: s.get("season_id", 0), reverse=True)
    if past:
        lines = [f"Stagione {s['season_id']}: {division_name(s.get('division'))}" for s in past]
        embed.add_field(name="Stagioni precedenti", value="\n".join(lines)[:1024], inline=False)

    embed.set_footer(text="Dati da CoralMC")
    return embed


async def fetch_player(username: str):
    """Ritorna (payload | None, messaggio_errore | None)."""
    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": "CoralStatsDiscordBot/1.0", "Accept": "application/json"}
    last_error = "Impossibile contattare l'API di CoralMC."

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for host in CORAL_API_HOSTS:
            url = f"{host}{CORAL_API_PATH}{username}"
            try:
                async with session.get(url) as resp:
                    is_json = "json" in (resp.content_type or "")
                    if resp.status == 404 and is_json:
                        return None, "Giocatore non trovato."
                    if resp.status in (401, 403):
                        last_error = ("L'API di CoralMC ha rifiutato la richiesta "
                                      "(potrebbe richiedere un token o bloccare i bot).")
                        print(f"[stats] {url} -> {resp.status}")
                        continue
                    if resp.status != 200 or not is_json:
                        last_error = f"L'API di CoralMC ha risposto in modo inatteso ({resp.status})."
                        print(f"[stats] {url} -> {resp.status} {resp.content_type}")
                        continue
                    data = await resp.json()
            except asyncio.TimeoutError:
                last_error = "L'API di CoralMC non risponde, riprova tra poco."
                print(f"[stats] {url} -> timeout")
                continue
            except aiohttp.ClientError as exc:
                print(f"[stats] {url} -> {exc!r}")
                continue

            if isinstance(data, dict) and "kills" in data:
                return data, None
            last_error = "Giocatore non trovato o senza statistiche Bedwars."
    return None, last_error


@bot.tree.command(name="stats", description="Mostra le statistiche Bedwars di un giocatore CoralMC")
@app_commands.describe(username="Nome del giocatore su CoralMC")
async def stats(interaction: discord.Interaction, username: str):
    username = username.strip()
    if not USERNAME_RE.match(username):
        await interaction.response.send_message(
            "Username non valido (3-16 caratteri: lettere, numeri o _).", ephemeral=True)
        return

    await interaction.response.defer()  # la richiesta HTTP può superare i 3 secondi
    payload, error = await fetch_player(username)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return
    await interaction.followup.send(embed=build_stats_embed(username, payload))


# ---------------------------------------------------------------- /setup ---

@bot.tree.command(name="setup", description="Pubblica il pannello per aprire i ticket")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Supporto",
        description="Scegli qui sotto il tipo di ticket che vuoi aprire.",
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
    synced = await bot.tree.sync()
    print("Comandi sincronizzati:", [c.name for c in synced])
    print(f"Online come {bot.user}")


bot.run(TOKEN)