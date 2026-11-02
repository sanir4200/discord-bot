import os
import re
import json
import asyncio
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
STAFF_ROLE_NAME = "Staff"
CATEGORY_NAME = "Tickets"
AUTO_ROLE_NAME = "⭐utente"  # ruolo assegnato a chi entra nel server (maiuscole ignorate)

# Canali statistiche (/set-up-a1)
STATS_CATEGORY_NAME = "📊 STATISTICHE"
STATS_UPDATE_MINUTES = 10  # Discord limita i rinomini dei canali: non scendere sotto i 5-10 minuti
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_channels.json")

# Scala dei gradi per /pex, dal più basso al più alto (nomi dei ruoli, maiuscole ignorate).
# "utente" e "bot" restano fuori dalla scala.
PEX_LADDER = ["⭐utente", "Staff", "mod", "mod+"]
PEX_REMOVE_OLD = False  # True = quando sale, perde anche il grado precedente

# Registro dei ticket (/ticket-log)
TICKET_LOG_CHANNEL_NAME = "📋-ticket-log"
TICKET_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket_log.json")

# Regolamento (/regolamento)
RULES_CHANNEL_KEYWORD = "rules"  # il canale è quello che ha questa parola nel nome
RULES_TITLE = "📜 Regolamento Cavalliers"
RULES_BANNER_URL = ""  # facoltativo: link a un'immagine da mostrare in fondo all'embed
RULES = [
    ("Rispetto reciproco", "Comportati con correttezza verso tutti. Non sono ammessi offese, discriminazioni, minacce o atteggiamenti molesti."),
    ("Zero spam", "Non inviare più volte lo stesso messaggio e non abusare di emoji, maiuscole o menzioni."),
    ("Canali corretti", "Usa ogni canale per l'argomento a cui è dedicato."),
    ("Privacy e sicurezza", "Tieni riservate le informazioni personali, tue e altrui, e avvisa lo staff se noti qualcosa di sospetto."),
    ("Fair play", "Cheat, hack e sfruttamento dei bug non sono tollerati, nemmeno sul server di gioco."),
    ("Decisioni dello staff", "Accetta le scelte dello staff. Per qualsiasi dubbio, apri un ticket."),
]

# API CoralMC: /api/v1/stats/bedwars/{username}
# Se conosci l'host esatto dalla documentazione, lascia solo quello in lista.
CORAL_API_HOSTS = ["https://coralmc.it", "https://api.coralmc.it", "https://www.coralmc.it"]

# Clan (/clan member). L'ID è quello della pagina https://coralmc.it/it/clans/33900
CLAN_ID = 33900
CLAN_API_PATH = "/api/v1/clans/"  # confermato: https://coralmc.it/api/v1/clans/33900
CLAN_PAGE = "https://coralmc.it/it/clans/"
CLAN_UPDATE_MINUTES = 10  # ogni quanto il messaggio del clan si aggiorna e controlla chi entra/esce
CLAN_LIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clan_live.json")
CLAN_LOG_CHANNEL_NAME = "📜-logs-del-clan"  # nome del canale creato da /clan logs
CLAN_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clan_log.json")
# Ruoli: 3 Leader e 2 Co-leader (confermati dal sito); 1 e 0 compaiono entrambi come "Membro"
CLAN_ROLE_NAMES = {3: "Leader", 2: "Co-leader", 1: "Membro", 0: "Membro"}
CORAL_API_PATH = "/api/v1/stats/bedwars/"
CORAL_PROFILE = "https://coralmc.it/it/stats/player/"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

# Numero divisione -> nome. 4, 5 e 7 sono confermati dal sito;
# 1-3 sono un'ipotesi e 6 non è noto (viene mostrato come "Divisione 6").
DIVISIONS = {1: "Bronzo", 2: "Argento", 3: "Oro", 4: "Platino", 5: "Diamante", 7: "Campione"}
CLAN_ROLES = {3: "Leader"}

intents = discord.Intents.default()
intents.members = True  # serve per on_member_join (da abilitare anche nel Developer Portal)
intents.presences = True  # serve per contare chi è online (anche questo va abilitato nel Developer Portal)
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
        await log_ticket_closed(interaction)
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
    "cw": {
        "label": "CW",
        "emoji": "⚔️",
        "style": discord.ButtonStyle.secondary,
        "custom_id": "ticket:cw",
        "prefix": "cw",
        "welcome": "Scrivi qui la tua richiesta per la CW, lo staff ti risponderà a breve.",
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
    await log_ticket_opened(guild, user, cfg, channel)
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


# ------------------------------------------------------ /set-up-a1 (stats) ---

def load_stats_config() -> dict:
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_stats_config(cfg: dict) -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def stats_names(guild: discord.Guild) -> dict:
    """Nomi dei canali. Membri e Online escludono i bot."""
    humans = [m for m in guild.members if not m.bot]
    online = [m for m in humans if m.status != discord.Status.offline]
    bots = [m for m in guild.members if m.bot]
    return {
        "members": f"👥 Membri: {len(humans)}",
        "online": f"🟢 Online: {len(online)}",
        "bots": f"🤖 Bot: {len(bots)}",
    }


async def update_stats(guild: discord.Guild) -> bool:
    """Aggiorna i nomi dei canali statistiche (solo se sono cambiati)."""
    entry = load_stats_config().get(str(guild.id))
    if not entry:
        return False
    names = stats_names(guild)
    for key, name in names.items():
        channel = guild.get_channel(entry["channels"].get(key, 0))
        if channel is None:
            print(f"[stats] Canale '{key}' non trovato in {guild.name}: rilancia /set-up-a1")
            return False
        if channel.name != name:
            try:
                await channel.edit(name=name, reason="Aggiornamento statistiche")
            except discord.HTTPException as exc:
                print(f"[stats] Impossibile rinominare {channel.name}: {exc}")
                return False
    return True


@tasks.loop(minutes=STATS_UPDATE_MINUTES)
async def refresh_stats():
    for guild in bot.guilds:
        await update_stats(guild)


@bot.tree.command(name="set-up-a1",
                  description="Crea i canali con le statistiche del server (membri, online, bot)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup_stats(interaction: discord.Interaction):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)

    cfg = load_stats_config()
    old = cfg.get(str(guild.id)) or {}
    old_channels = [guild.get_channel(cid) for cid in (old.get("channels") or {}).values()]

    # Già configurato e completo: basta aggiornare i numeri
    if old and len(old_channels) == 3 and all(old_channels):
        await update_stats(guild)
        await interaction.followup.send("Le statistiche sono già configurate: le ho aggiornate ✅",
                                        ephemeral=True)
        return

    try:
        for ch in old_channels:  # elimina eventuali resti di una configurazione incompleta
            if ch:
                await ch.delete(reason="Ricreazione statistiche")

        # Bloccati: tutti vedono i canali ma nessuno può entrare (lucchetto)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True,
                                                  manage_channels=True),
        }
        category = guild.get_channel(old.get("category", 0))
        if category is None:
            category = await guild.create_category(STATS_CATEGORY_NAME, overwrites=overwrites)
            await category.edit(position=0)  # in cima alla lista dei canali

        names = stats_names(guild)
        created = {}
        for key in ("members", "online", "bots"):
            ch = await guild.create_voice_channel(
                names[key], category=category, overwrites=overwrites,
                reason="Canali statistiche")
            created[key] = ch.id
    except discord.Forbidden:
        await interaction.followup.send(
            "Non ho i permessi: serve **Gestisci canali** per il bot.", ephemeral=True)
        return

    cfg[str(guild.id)] = {"category": category.id, "channels": created}
    save_stats_config(cfg)
    await interaction.followup.send(
        f"Statistiche create ✅ Si aggiornano ogni {STATS_UPDATE_MINUTES} minuti.", ephemeral=True)


@setup_stats.error
async def setup_stats_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "Serve il permesso Amministratore."
    else:
        print(f"[stats] Errore in /set-up-a1: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ------------------------------------------------------------------ /pex ---

def find_role(guild: discord.Guild, name: str):
    return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)


@bot.tree.command(name="pex", description="Promuove un utente al grado successivo")
@app_commands.describe(utente="L'utente da promuovere")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def pex(interaction: discord.Interaction, utente: discord.Member):
    guild = interaction.guild

    if utente.bot:
        await interaction.response.send_message("Non posso promuovere un bot.", ephemeral=True)
        return

    ladder = [find_role(guild, name) for name in PEX_LADDER]
    missing = [name for name, role in zip(PEX_LADDER, ladder) if role is None]
    if missing:
        await interaction.response.send_message(
            "Non trovo questi ruoli nel server: " + ", ".join(f"**{m}**" for m in missing),
            ephemeral=True)
        return

    # Grado attuale = il più alto della scala tra i ruoli dell'utente (-1 = nessuno)
    current = -1
    for i, role in enumerate(ladder):
        if role in utente.roles:
            current = i

    if current == len(ladder) - 1:
        await interaction.response.send_message(
            f"{utente.mention} è già al grado massimo (**{ladder[current].name}**).",
            ephemeral=True)
        return

    new_role = ladder[current + 1]
    if new_role >= guild.me.top_role:
        await interaction.response.send_message(
            f"Non posso assegnare **{new_role.name}**: sposta il ruolo del bot sopra di esso.",
            ephemeral=True)
        return

    try:
        await utente.add_roles(new_role, reason=f"/pex da {interaction.user}")
        if PEX_REMOVE_OLD and current >= 0:
            await utente.remove_roles(ladder[current], reason=f"/pex da {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Non ho i permessi per modificare i ruoli di questo utente "
            "(il suo ruolo più alto è sopra a quello del bot?).", ephemeral=True)
        return

    old_name = ladder[current].name if current >= 0 else "nessun grado"
    await interaction.response.send_message(
        f"⬆️ {utente.mention} promosso: **{old_name}** → **{new_role.name}**")


@pex.error
async def pex_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "Serve il permesso Amministratore."
    else:
        print(f"[pex] Errore: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="depex", description="Abbassa un utente al grado precedente")
@app_commands.describe(utente="L'utente da retrocedere")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def depex(interaction: discord.Interaction, utente: discord.Member):
    guild = interaction.guild

    if utente.bot:
        await interaction.response.send_message("Non posso retrocedere un bot.", ephemeral=True)
        return

    ladder = [find_role(guild, name) for name in PEX_LADDER]
    missing = [name for name, role in zip(PEX_LADDER, ladder) if role is None]
    if missing:
        await interaction.response.send_message(
            "Non trovo questi ruoli nel server: " + ", ".join(f"**{m}**" for m in missing),
            ephemeral=True)
        return

    # Grado attuale = il più alto della scala tra i ruoli dell'utente (-1 = nessuno)
    current = -1
    for i, role in enumerate(ladder):
        if role in utente.roles:
            current = i

    if current == -1:
        await interaction.response.send_message(
            f"{utente.mention} non ha nessun grado della scala.", ephemeral=True)
        return
    if current == 0:
        await interaction.response.send_message(
            f"{utente.mention} è già al grado minimo (**{ladder[0].name}**).", ephemeral=True)
        return

    old_role, new_role = ladder[current], ladder[current - 1]
    if old_role >= guild.me.top_role:
        await interaction.response.send_message(
            f"Non posso togliere **{old_role.name}**: sposta il ruolo del bot sopra di esso.",
            ephemeral=True)
        return

    try:
        await utente.remove_roles(old_role, reason=f"/depex da {interaction.user}")
        # Assicura che abbia il grado inferiore (se ce l'ha già non cambia nulla)
        await utente.add_roles(new_role, reason=f"/depex da {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Non ho i permessi per modificare i ruoli di questo utente "
            "(il suo ruolo più alto è sopra a quello del bot?).", ephemeral=True)
        return

    await interaction.response.send_message(
        f"⬇️ {utente.mention} retrocesso: **{old_role.name}** → **{new_role.name}**")


@depex.error
async def depex_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "Serve il permesso Amministratore."
    else:
        print(f"[depex] Errore: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ------------------------------------------------------------ /ticket-log ---

def load_ticket_log_config() -> dict:
    try:
        with open(TICKET_LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_log_channel(guild: discord.Guild):
    channel_id = load_ticket_log_config().get(str(guild.id))
    return guild.get_channel(channel_id) if channel_id else None


async def send_ticket_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Scrive nel registro; se non è configurato o fallisce, il ticket funziona lo stesso."""
    channel = get_log_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        print(f"[ticket-log] Impossibile scrivere nel registro: {exc}")


async def log_ticket_opened(guild, user, cfg, channel) -> None:
    now = discord.utils.utcnow()
    embed = discord.Embed(title="🎫 Ticket aperto", color=discord.Color.green(), timestamp=now)
    embed.add_field(name="Aperto da", value=f"{user.mention}\n`{user}` · ID {user.id}", inline=False)
    embed.add_field(name="Tipo", value=f"{cfg['emoji']} {cfg['label']}", inline=True)
    embed.add_field(name="Canale", value=channel.mention, inline=True)
    embed.add_field(name="Data e ora", value=discord.utils.format_dt(now, "F"), inline=False)
    await send_ticket_log(guild, embed)


async def log_ticket_closed(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    closer = interaction.user
    now = discord.utils.utcnow()

    # Il topic del ticket è "[tipo] Ticket di nome (id)"
    topic = getattr(channel, "topic", None) or ""
    kind_match = re.match(r"^\[(\w+)\]", topic)
    id_match = re.search(r"\((\d+)\)$", topic)
    cfg = TICKET_TYPES.get(kind_match.group(1)) if kind_match else None
    type_text = f"{cfg['emoji']} {cfg['label']}" if cfg else "N/D"
    opener = f"<@{id_match.group(1)}>" if id_match else "N/D"

    embed = discord.Embed(title="🔒 Ticket chiuso", color=discord.Color.red(), timestamp=now)
    embed.add_field(name="Chiuso da", value=f"{closer.mention}\n`{closer}` · ID {closer.id}", inline=False)
    embed.add_field(name="Aperto da", value=opener, inline=True)
    embed.add_field(name="Tipo", value=type_text, inline=True)
    embed.add_field(name="Canale", value=f"#{channel.name}", inline=True)
    embed.add_field(name="Aperto il", value=discord.utils.format_dt(channel.created_at, "F"), inline=False)
    embed.add_field(name="Chiuso il", value=discord.utils.format_dt(now, "F"), inline=False)
    await send_ticket_log(interaction.guild, embed)


@bot.tree.command(name="ticket-log",
                  description="Crea il canale dove vengono registrati apertura e chiusura dei ticket")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def ticket_log(interaction: discord.Interaction):
    guild = interaction.guild
    existing = get_log_channel(guild)
    if existing:
        await interaction.response.send_message(
            f"Il registro esiste già: {existing.mention}", ephemeral=True)
        return

    staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              embed_links=True),
    }
    if staff_role:  # lo staff può solo leggere
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False)

    try:
        channel = await guild.create_text_channel(
            TICKET_LOG_CHANNEL_NAME,
            overwrites=overwrites,
            topic="Registro di apertura e chiusura dei ticket",
            reason="Registro ticket",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Non ho i permessi: serve **Gestisci canali** per il bot.", ephemeral=True)
        return

    cfg = load_ticket_log_config()
    cfg[str(guild.id)] = channel.id
    with open(TICKET_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    await interaction.response.send_message(
        f"Registro creato ✅ {channel.mention}\nVisibile agli amministratori e al ruolo "
        f"**{STAFF_ROLE_NAME}** (solo lettura).", ephemeral=True)


@ticket_log.error
async def ticket_log_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "Serve il permesso Amministratore."
    else:
        print(f"[ticket-log] Errore: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ----------------------------------------------------------- /regolamento ---

def build_rules_embed(guild: discord.Guild) -> discord.Embed:
    lines = ["Prenditi un momento per leggere le regole: aiutano a mantenere un clima "
             "tranquillo per tutti ed evitano sanzioni a sorpresa.", ""]
    for i, (title, text) in enumerate(RULES, 1):
        lines.append(f"**{i}. {title}**\n{text}\n")
    lines.append("» Lo **staff** può applicare **sanzioni** in base alla gravità dell'infrazione.")
    lines.append("» Per qualsiasi segnalazione, **apri un ticket**.")

    embed = discord.Embed(title=RULES_TITLE, description="\n".join(lines),
                          color=discord.Color.gold())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if RULES_BANNER_URL:
        embed.set_image(url=RULES_BANNER_URL)
    embed.set_footer(text="Cavalliers | Regolamento")
    return embed


@bot.tree.command(name="regolamento",
                  description="Pubblica (o aggiorna) il regolamento nel canale rules")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def regolamento(interaction: discord.Interaction):
    guild = interaction.guild
    channel = discord.utils.find(
        lambda c: RULES_CHANNEL_KEYWORD in c.name.lower(), guild.text_channels)
    if channel is None:
        await interaction.response.send_message(
            f"Non trovo nessun canale testuale con «{RULES_CHANNEL_KEYWORD}» nel nome.",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    embed = build_rules_embed(guild)
    try:
        # Se il regolamento c'è già lo aggiorna, altrimenti lo pubblica
        existing = None
        async for msg in channel.history(limit=50):
            if msg.author == guild.me and msg.embeds and msg.embeds[0].title == RULES_TITLE:
                existing = msg
                break
        if existing:
            await existing.edit(embed=embed)
            verb = "aggiornato"
        else:
            await channel.send(embed=embed)
            verb = "pubblicato"
    except discord.Forbidden:
        await interaction.followup.send(
            f"Non ho i permessi in {channel.mention}: servono Vedi canale, Invia messaggi, "
            "Incorpora link e Leggi cronologia messaggi.", ephemeral=True)
        return
    await interaction.followup.send(f"Regolamento {verb} in {channel.mention} ✅", ephemeral=True)


@regolamento.error
async def regolamento_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "Serve il permesso Amministratore."
    else:
        print(f"[regolamento] Errore: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ------------------------------------------------------------ /clan member ---

def role_rank(role) -> int:
    if isinstance(role, int):
        return role
    if isinstance(role, str):
        key = role.lower().replace(" ", "").replace("_", "").replace("-", "")
        return {"leader": 3, "coleader": 2}.get(key, 1)
    return 0


def role_label(role) -> str:
    if isinstance(role, int):
        return CLAN_ROLE_NAMES.get(role, f"Ruolo {role}")
    if isinstance(role, str) and role:
        return role.replace("_", "-").capitalize()
    return "Membro"


def parse_clan(data):
    """Estrae nome del clan e membri da JSON con nomi di campo non noti con certezza."""
    if not isinstance(data, dict):
        return None
    clan = data["clan"] if isinstance(data.get("clan"), dict) else data
    raw = clan.get("members") or clan.get("players") or data.get("members") or []
    members = []
    for m in raw:
        if isinstance(m, str):
            members.append((m, None))
        elif isinstance(m, dict):
            name = m.get("username") or m.get("name") or m.get("player_name") or m.get("nickname")
            if name:
                members.append((name, m.get("role", m.get("clan_role"))))
    members.sort(key=lambda x: (-role_rank(x[1]), x[0].lower()))
    return {
        "name": clan.get("name") or clan.get("clan_name") or "Cavalliers",
        "members": members,
        "xp": clan.get("currentExperience"),  # XP del clan nella stagione corrente
    }


async def fetch_clan(clan_id: int):
    """Ritorna (clan | None, messaggio_errore | None)."""
    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": "CoralStatsDiscordBot/1.0", "Accept": "application/json"}
    last_error = "Impossibile contattare l'API di CoralMC."

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for host in CORAL_API_HOSTS:
            url = f"{host}{CLAN_API_PATH}{clan_id}"
            try:
                async with session.get(url) as resp:
                    is_json = "json" in (resp.content_type or "")
                    if resp.status != 200 or not is_json:
                        print(f"[clan] {url} -> {resp.status} {resp.content_type}")
                        last_error = f"L'API di CoralMC ha risposto in modo inatteso ({resp.status})."
                        continue
                    data = await resp.json()
            except asyncio.TimeoutError:
                print(f"[clan] {url} -> timeout")
                last_error = "L'API di CoralMC non risponde, riprova tra poco."
                continue
            except aiohttp.ClientError as exc:
                print(f"[clan] {url} -> {exc!r}")
                continue

            clan = parse_clan(data)
            if clan and clan["members"]:
                return clan, None
            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            print(f"[clan] {url} -> JSON senza membri riconoscibili. Campi: {keys}")
            last_error = "Ho letto la risposta ma non trovo l'elenco dei membri."
    return None, last_error


clan_group = app_commands.Group(name="clan", description="Informazioni sul clan", guild_only=True)


async def build_clan_embed(clan: dict) -> discord.Embed:
    # EXP di ogni membro dalle statistiche Bedwars (richieste in parallelo)
    results = await asyncio.gather(
        *(fetch_player(name) for name, _ in clan["members"]), return_exceptions=True)

    icons = {3: "👑", 2: "⭐"}
    lines = []
    for (name, role), result in zip(clan["members"], results):
        exp = None
        if isinstance(result, tuple) and result[0]:
            exp = result[0].get("current_division_exp")
        exp_text = f"{fmt(exp)} EXP" if exp is not None else "EXP N/D"
        lines.append(f"{icons.get(role_rank(role), '•')} **{name}** · {role_label(role)} · {exp_text}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n…"

    embed = discord.Embed(
        title=f"Membri del clan {clan['name']}",
        url=f"{CLAN_PAGE}{CLAN_ID}",
        description=text,
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    footer = f"{len(clan['members'])} membri"
    if clan.get("xp") is not None:
        footer += f" · XP del clan: {fmt(clan['xp'])}"
    embed.set_footer(text=footer + f" · Aggiornato ogni {CLAN_UPDATE_MINUTES} min")
    return embed


def load_clan_live() -> dict:
    try:
        with open(CLAN_LIVE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_clan_live(cfg: dict) -> None:
    with open(CLAN_LIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


@clan_group.command(name="member", description="Mostra i membri del clan (si aggiorna da solo ogni 10 minuti)")
async def clan_member(interaction: discord.Interaction):
    await interaction.response.defer()
    clan, error = await fetch_clan(CLAN_ID)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    embed = await build_clan_embed(clan)
    message = await interaction.followup.send(embed=embed, wait=True)

    # Da ora questo è il messaggio che si aggiorna da solo (uno per server)
    cfg = load_clan_live()
    cfg[str(interaction.guild_id)] = {
        "channel": interaction.channel_id,
        "message": message.id,
        "members": [name for name, _ in clan["members"]],
    }
    save_clan_live(cfg)


def load_clan_log() -> dict:
    try:
        with open(CLAN_LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@clan_group.command(name="logs",
                    description="Imposta il canale dove avvisare chi entra o esce dal clan")
@app_commands.describe(canale="Canale esistente da usare (se lo lasci vuoto ne creo uno nuovo)")
@app_commands.checks.has_permissions(administrator=True)
async def clan_logs(interaction: discord.Interaction, canale: Optional[discord.TextChannel] = None):
    guild = interaction.guild
    created = False
    if canale is None:
        existing = discord.utils.get(guild.text_channels, name=CLAN_LOG_CHANNEL_NAME)
        if existing:
            canale = existing
        else:
            # Tutti possono leggerlo, solo il bot può scriverci
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True),
            }
            try:
                canale = await guild.create_text_channel(
                    CLAN_LOG_CHANNEL_NAME, overwrites=overwrites,
                    topic="Chi entra e chi esce dal clan", reason="Log del clan")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Non ho i permessi: serve **Gestisci canali** per il bot.", ephemeral=True)
                return
            created = True

    perms = canale.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages):
        await interaction.response.send_message(
            f"Non posso scrivere in {canale.mention}: dai al bot i permessi Vedi canale e Invia messaggi.",
            ephemeral=True)
        return

    cfg = load_clan_log()
    cfg[str(guild.id)] = canale.id
    with open(CLAN_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    verb = "creato e impostato" if created else "impostato"
    await interaction.response.send_message(
        f"Canale dei log del clan {verb}: {canale.mention} ✅\n"
        "Qui scriverò chi entra e chi esce dal clan.", ephemeral=True)


@clan_logs.error
async def clan_logs_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "Serve il permesso Amministratore."
    else:
        print(f"[clan] Errore in /clan logs: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


@tasks.loop(minutes=CLAN_UPDATE_MINUTES)
async def refresh_clan():
    cfg = load_clan_live()
    changed = False
    for guild_id, entry in list(cfg.items()):
        channel = bot.get_channel(entry["channel"])
        if channel is None:
            continue

        clan, error = await fetch_clan(CLAN_ID)
        if error:
            print(f"[clan] Aggiornamento saltato: {error}")
            continue

        # Chi è entrato / uscito rispetto all'ultimo controllo
        before = {n.lower(): n for n in entry.get("members", [])}
        now = {n.lower(): n for n, _ in clan["members"]}
        joined = [now[k] for k in now if k not in before]
        left = [before[k] for k in before if k not in now]

        try:
            message = await channel.fetch_message(entry["message"])
            await message.edit(embed=await build_clan_embed(clan))
        except discord.NotFound:
            print("[clan] Messaggio eliminato: smetto di aggiornarlo. Rilancia /clan member.")
            del cfg[guild_id]
            changed = True
            continue
        except discord.HTTPException as exc:
            print(f"[clan] Impossibile aggiornare il messaggio: {exc}")
            continue

        # Avvisi: nel canale dei log del clan, se impostato; altrimenti nel canale del messaggio
        log_channel = bot.get_channel(load_clan_log().get(guild_id) or 0) or channel
        try:
            for name in joined:
                await log_channel.send(f"👋 **{name}** è entrato nel clan **{clan['name']}**!")
            for name in left:
                await log_channel.send(f"🚪 **{name}** ha lasciato il clan **{clan['name']}**.")
        except discord.HTTPException as exc:
            print(f"[clan] Impossibile scrivere nel canale dei log: {exc}")

        entry["members"] = [name for name, _ in clan["members"]]
        changed = True
    if changed:
        save_clan_live(cfg)


bot.tree.add_command(clan_group)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    role = discord.utils.find(
        lambda r: r.name.lower() == AUTO_ROLE_NAME.lower(), member.guild.roles)
    if role is None:
        print(f"[autorole] Ruolo '{AUTO_ROLE_NAME}' non trovato in {member.guild.name}")
        return
    try:
        await member.add_roles(role, reason="Ruolo automatico all'ingresso")
    except discord.Forbidden:
        print("[autorole] Permessi insufficienti: dai al bot 'Gestisci ruoli' e "
              f"metti il suo ruolo sopra '{role.name}'.")
    except discord.HTTPException as exc:
        print(f"[autorole] Errore nell'assegnare il ruolo a {member}: {exc}")


@bot.event
async def on_ready():
    bot.add_view(TicketView())
    bot.add_view(CloseView())
    synced = await bot.tree.sync()
    print("Comandi sincronizzati:", [c.name for c in synced])
    if not refresh_stats.is_running():
        refresh_stats.start()
    if not refresh_clan.is_running():
        refresh_clan.start()
    print(f"Online come {bot.user}")


bot.run(TOKEN)