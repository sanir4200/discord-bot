import os
import io
import datetime
import re
import math
import json
import asyncio
import functools
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps  # pip install pillow
except ImportError:
    Image = None  # senza Pillow /stats usa l'embed

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
# Dal grado più basso al più alto. I nomi si scrivono senza emoji né "|": il bot li trova anche
# se nel server il ruolo si chiama, per esempio, "👑 | owner".
PEX_LADDER = ["utente", "trial", "helper", "helper+", "mod", "mod+", "admin", "Sr. admin"]
# Chi ha uno di questi ruoli non può essere promosso né retrocesso con /pex e /depex
PEX_PROTECTED = ["owner", "co-owner"]
# Chi può usare cosa (il proprietario del server Discord conta sempre come owner):
#  - owner e co-owner: tutti i comandi
#  - Sr. admin: solo /pex, /depex e /stats
#  - tutti gli altri: solo /stats
OWNER_COMMAND_ROLES = ["owner", "co-owner"]  # /setup /set-up-a1 /ticket-log /regolamento /clan member /clan logs
PEX_COMMAND_ROLES = ["owner", "co-owner", "Sr. admin"]  # /pex /depex
OWNER_ONLY_TEXT = "Questo comando può essere usato solo da **owner** e **co-owner**."
PEX_ONLY_TEXT = "Questo comando può essere usato solo da **owner**, **co-owner** e **Sr. admin**."
# Gradi che con /pex e /depex possono assegnare o togliere solo owner e co-owner
PEX_OWNER_ONLY_RANKS = ["Sr. admin"]
PEX_REMOVE_OLD = False  # True = quando sale, perde anche il grado precedente

# Registro dei ticket (/ticket-log)
TICKET_LOG_CHANNEL_NAME = "📋-ticket-log"
TICKET_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket_log.json")

# Messaggio di benvenuto (quando qualcuno entra nel server)
WELCOME_CHANNEL_KEYWORD = "chat-generale"  # canale dove viene scritto il benvenuto
WELCOME_TICKET_KEYWORD = "ticket"          # canale con il pannello ticket (quello dove hai usato /setup)
WELCOME_TITLE = "Nuovo arrivato! 🎉"
WELCOME_FOOTER = "Cavalliers | Ci vediamo in gioco!"

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


class NotAllowedError(app_commands.CheckFailure):
    """L'utente non ha uno dei ruoli richiesti per il comando."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def require_roles(role_names: list, text: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None:
            raise NotAllowedError(text)
        if interaction.user.id == guild.owner_id:  # il proprietario del server non resta mai escluso
            return True
        allowed = [find_role(guild, name) for name in role_names]
        user_roles = getattr(interaction.user, "roles", [])
        if any(role is not None and role in user_roles for role in allowed):
            return True
        raise NotAllowedError(text)
    return app_commands.check(predicate)


def owner_only():
    return require_roles(OWNER_COMMAND_ROLES, OWNER_ONLY_TEXT)


def pex_allowed():
    return require_roles(PEX_COMMAND_ROLES, PEX_ONLY_TEXT)


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

    staff_role = find_role(guild, STAFF_ROLE_NAME)

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


DIVISION_EMOJI = {1: "🥉", 2: "🥈", 3: "🥇", 4: "🔷", 5: "💎", 7: "👑"}
DIVISION_COLOR = {4: 0x4FC3F7, 5: 0x5DE2E7, 7: 0xE74C3C}  # colore dell'embed in base alla divisione


def division_label(n) -> str:
    return f"{DIVISION_EMOJI.get(n, '🏅')} {division_name(n)}"


def win_bar(percent: float, size: int = 10) -> str:
    filled = max(0, min(size, round(percent / 100 * size)))
    return "🟩" * filled + "⬜" * (size - filled)


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

    # Riga di riepilogo: divisione e clan
    top = []
    if d.get("current_division") is not None:
        top.append(f"**{division_label(d['current_division'])}**")
    if d.get("clan_name"):
        role = CLAN_ROLES.get(d.get("clan_role"))
        top.append(f"🛡️ **{d['clan_name']}**" + (f" ({role})" if role else ""))

    # Riga di riepilogo: livello, classifica, monete
    info = [f"🏆 Livello **{fmt(d.get('level'))}★**"]
    rank, total = d.get("level_rank"), d.get("total_players")
    if rank and total:
        pct = max(rank / total * 100, 0.1)
        info.append(f"🌍 Classifica **#{fmt(rank)}** *(Top {pct:.1f}%)*".replace(".", ","))
    elif rank:
        info.append(f"🌍 Classifica **#{fmt(rank)}**")
    info.append(f"🪙 **{fmt(d.get('coins'))}**")

    description = "  ·  ".join(top) + "\n" if top else ""
    description += "  ·  ".join(info)

    embed = discord.Embed(
        title=f"⚔️ Statistiche Bedwars · {username}",
        url=f"{CORAL_PROFILE}{username}",
        description=description,
        color=discord.Color(DIVISION_COLOR.get(d.get("current_division"), 0x3498DB)),
    )
    embed.set_thumbnail(url=f"https://minotar.net/helm/{username}/100.png")

    # Combattimento
    embed.add_field(name="⚔️ Uccisioni", value=fmt(kills), inline=True)
    embed.add_field(name="💀 Morti", value=fmt(deaths), inline=True)
    embed.add_field(name="📊 K/D", value=ratio(kills, deaths), inline=True)
    # Finali
    embed.add_field(name="🎯 Uccisioni finali", value=fmt(final_kills), inline=True)
    embed.add_field(name="☠️ Morti finali", value=fmt(final_deaths), inline=True)
    embed.add_field(name="📈 FKDR", value=ratio(final_kills, final_deaths), inline=True)
    # Letti e serie
    embed.add_field(name="🛏️ Letti rotti", value=fmt(d.get("beds_broken")), inline=True)
    embed.add_field(name="🔥 Winstreak", value=fmt(d.get("winstreak")), inline=True)
    embed.add_field(name="🏅 Top winstreak", value=fmt(d.get("h_winstreak")), inline=True)

    # Vittorie con barra
    results = f"✅ **{fmt(wins)}** vittorie  ·  ❌ **{fmt(losses)}** sconfitte"
    if played:
        pct_wins = wins / played * 100
        results += f"\n{win_bar(pct_wins)}  **{pct_wins:.1f}%**".replace(".", ",")
    embed.add_field(name="🎮 Partite", value=results, inline=False)

    # Stagioni precedenti, raggruppate per divisione
    past = sorted(d.get("past_divisions") or [], key=lambda x: x.get("season_id", 0), reverse=True)
    if past:
        groups = {}
        for season in past:
            groups.setdefault(season.get("division"), []).append(f"S{season['season_id']}")
        order = sorted(groups, key=lambda x: (x is None, -(x or 0)))
        lines = [f"{division_label(div)}: {', '.join(groups[div])}" for div in order]
        embed.add_field(name="📅 Stagioni precedenti", value="\n".join(lines)[:1024], inline=False)

    embed.set_footer(text="📡 Dati da CoralMC")
    return embed


# ------------------------------------------------- scheda immagine di /stats ---
# Disegna la scheda con Pillow (pip install pillow). Se Pillow manca, /stats usa l'embed.

CARD_W = 900
CARD_FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_font.ttf")
CARD_FONT_CANDIDATES = [
    CARD_FONT_FILE,  # metti qui un tuo .ttf (grassetto) per cambiare carattere
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
]

C_TEXT = (240, 243, 250)
C_MUTED = (165, 177, 200)
C_GOLD = (236, 200, 140)
C_CYAN = (93, 226, 231)
C_GREEN = (84, 214, 122)
C_RED = (240, 92, 92)
C_PANEL = (27, 34, 53)
C_EDGE = (56, 68, 96)
DIV_COLORS = {7: C_GOLD, 5: C_CYAN, 4: (140, 200, 255), 3: (240, 200, 80),
              2: (205, 210, 220), 1: (205, 140, 92)}


@functools.lru_cache(maxsize=None)
def card_font(size: int):
    for path in CARD_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow molto vecchio
        return ImageFont.load_default()


def fit_font(text: str, max_w: float, size: int, min_size: int = 12):
    while size > min_size:
        font = card_font(size)
        if font.getlength(text) <= max_w:
            return font
        size -= 1
    return card_font(min_size)


def star_points(cx, cy, r_out, r_in, n=5):
    pts = []
    for i in range(n * 2):
        r = r_out if i % 2 == 0 else r_in
        a = -math.pi / 2 + i * math.pi / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def draw_icon(d, kind: str, cx: float, cy: float, s: float = 30) -> None:
    """Piccole icone disegnate a mano (nessun file esterno)."""
    u = s / 2
    w = max(3, int(s // 9))
    if kind == "swords":
        d.line((cx - u, cy + u, cx + u, cy - u), fill=(205, 212, 226), width=w)
        d.line((cx - u, cy - u, cx + u, cy + u), fill=(205, 212, 226), width=w)
        g = u * 0.3
        hx, hy = cx - u * 0.5, cy + u * 0.5
        d.line((hx - g, hy - g, hx + g, hy + g), fill=(214, 168, 80), width=w)
        hx, hy = cx + u * 0.5, cy + u * 0.5
        d.line((hx - g, hy + g, hx + g, hy - g), fill=(214, 168, 80), width=w)
    elif kind == "skull":
        d.ellipse((cx - u * .8, cy - u, cx + u * .8, cy + u * .45), fill=(235, 238, 245))
        d.rounded_rectangle((cx - u * .45, cy + u * .1, cx + u * .45, cy + u * .95), 4, fill=(235, 238, 245))
        d.ellipse((cx - u * .55, cy - u * .35, cx - u * .12, cy + u * .12), fill=(30, 34, 48))
        d.ellipse((cx + u * .12, cy - u * .35, cx + u * .55, cy + u * .12), fill=(30, 34, 48))
    elif kind in ("bars", "bars_down"):
        heights = [.7, 1.2, 1.7] if kind == "bars" else [1.7, 1.2, .7]
        color = (70, 200, 110) if kind == "bars" else (170, 90, 200)
        for i, h in enumerate(heights):
            x0 = cx - u * .85 + i * u * .6
            d.rectangle((x0, cy + u * .8 - h * u, x0 + u * .45, cy + u * .8), fill=color)
    elif kind == "target":
        d.ellipse((cx - u, cy - u, cx + u, cy + u), fill=(225, 70, 70))
        d.ellipse((cx - u * .65, cy - u * .65, cx + u * .65, cy + u * .65), fill=(240, 240, 245))
        d.ellipse((cx - u * .3, cy - u * .3, cx + u * .3, cy + u * .3), fill=(225, 70, 70))
    elif kind == "explosion":
        d.polygon(star_points(cx, cy, u * 1.05, u * .5, 8), fill=(255, 140, 50))
        d.polygon(star_points(cx, cy, u * .6, u * .3, 8), fill=(255, 214, 90))
    elif kind == "bed":
        d.rectangle((cx - u, cy + u * .1, cx + u, cy + u * .6), fill=(150, 100, 70))
        d.rectangle((cx - u, cy + u * .6, cx - u * .8, cy + u * .95), fill=(110, 74, 50))
        d.rectangle((cx + u * .8, cy + u * .6, cx + u, cy + u * .95), fill=(110, 74, 50))
        d.rounded_rectangle((cx - u * .9, cy - u * .35, cx - u * .35, cy + u * .1), 3, fill=(240, 240, 245))
        d.rectangle((cx - u * .25, cy - u * .2, cx + u, cy + u * .1), fill=(220, 70, 80))
    elif kind == "trophy":
        gold = (245, 190, 60)
        d.ellipse((cx - u * 1.0, cy - u * .7, cx - u * .45, cy - u * .05), outline=gold, width=w)
        d.ellipse((cx + u * .45, cy - u * .7, cx + u * 1.0, cy - u * .05), outline=gold, width=w)
        d.polygon([(cx - u * .7, cy - u * .85), (cx + u * .7, cy - u * .85),
                   (cx + u * .45, cy + u * .15), (cx - u * .45, cy + u * .15)], fill=gold)
        d.rectangle((cx - u * .12, cy + u * .15, cx + u * .12, cy + u * .55), fill=gold)
        d.rectangle((cx - u * .5, cy + u * .55, cx + u * .5, cy + u * .85), fill=(214, 160, 40))
    elif kind == "star":
        d.polygon(star_points(cx, cy + u * .05, u * 1.05, u * .45), fill=(255, 205, 70), outline=(214, 150, 30))
    elif kind == "globe":
        d.ellipse((cx - u, cy - u, cx + u, cy + u), fill=(60, 130, 220))
        d.ellipse((cx - u * .6, cy - u * .5, cx - u * .05, cy + u * .1), fill=(80, 190, 110))
        d.ellipse((cx + u * .1, cy, cx + u * .65, cy + u * .6), fill=(80, 190, 110))
        d.ellipse((cx - u, cy - u, cx + u, cy + u), outline=(190, 220, 255), width=2)
    elif kind == "chest":
        d.rounded_rectangle((cx - u, cy - u * .35, cx + u, cy + u * .85), 3, fill=(190, 130, 50))
        d.rounded_rectangle((cx - u, cy - u * .85, cx + u, cy - u * .05), 5, fill=(220, 160, 62))
        d.rectangle((cx - u * .16, cy - u * .25, cx + u * .16, cy + u * .25), fill=(252, 224, 104))
    elif kind in ("check", "cross"):
        color = (60, 190, 100) if kind == "check" else (220, 60, 66)
        d.rounded_rectangle((cx - u * .95, cy - u * .95, cx + u * .95, cy + u * .95), 6, fill=color)
        if kind == "check":
            d.line([(cx - u * .5, cy), (cx - u * .1, cy + u * .45), (cx + u * .55, cy - u * .4)],
                   fill=(255, 255, 255), width=w + 1, joint="curve")
        else:
            d.line((cx - u * .45, cy - u * .45, cx + u * .45, cy + u * .45), fill=(255, 255, 255), width=w + 1)
            d.line((cx - u * .45, cy + u * .45, cx + u * .45, cy - u * .45), fill=(255, 255, 255), width=w + 1)
    elif kind == "trend":
        d.rounded_rectangle((cx - u, cy - u, cx + u, cy + u), 6, fill=(38, 46, 68), outline=C_EDGE)
        d.line([(cx - u * .65, cy + u * .5), (cx - u * .15, cy - u * .05), (cx + u * .2, cy + u * .25),
                (cx + u * .65, cy - u * .5)], fill=(110, 220, 140), width=w, joint="curve")
    elif kind == "scroll":
        d.rounded_rectangle((cx - u * .7, cy - u, cx + u * .7, cy + u), 4, fill=(232, 214, 170))
        d.rectangle((cx - u * .7, cy - u, cx + u * .7, cy - u * .65), fill=(196, 164, 110))
        d.rectangle((cx - u * .7, cy + u * .65, cx + u * .7, cy + u), fill=(196, 164, 110))
        for k in (-.15, .15, .45):
            d.line((cx - u * .4, cy + u * k, cx + u * .4, cy + u * k), fill=(150, 120, 80), width=2)
    elif kind == "diamond":
        d.polygon([(cx - u * .55, cy - u * .55), (cx + u * .55, cy - u * .55), (cx + u, cy - u * .1),
                   (cx, cy + u), (cx - u, cy - u * .1)], fill=(80, 222, 236), outline=(200, 250, 255))
        d.line((cx - u, cy - u * .1, cx + u, cy - u * .1), fill=(200, 250, 255), width=2)
        d.line((cx - u * .3, cy - u * .55, cx - u * .6, cy - u * .1, cx, cy + u), fill=(160, 240, 250), width=2)
        d.line((cx + u * .3, cy - u * .55, cx + u * .6, cy - u * .1, cx, cy + u), fill=(160, 240, 250), width=2)
    elif kind == "shield":
        d.polygon([(cx - u * .8, cy - u * .9), (cx + u * .8, cy - u * .9), (cx + u * .8, cy + u * .1),
                   (cx, cy + u), (cx - u * .8, cy + u * .1)], fill=(222, 227, 237), outline=(150, 160, 180))
        d.rectangle((cx - u * .13, cy - u * .65, cx + u * .13, cy + u * .55), fill=(200, 50, 56))
        d.rectangle((cx - u * .5, cy - u * .22, cx + u * .5, cy + u * .04), fill=(200, 50, 56))
    elif kind == "crown":
        d.polygon([(cx - u, cy + u * .6), (cx - u, cy - u * .6), (cx - u * .45, cy), (cx, cy - u * .9),
                   (cx + u * .45, cy), (cx + u, cy - u * .6), (cx + u, cy + u * .6)], fill=(245, 190, 60),
                  outline=(214, 150, 30))
    elif kind == "gem":
        d.regular_polygon((cx, cy, u), 6, fill=(130, 200, 255), outline=(235, 248, 255))
    elif kind.startswith("medal"):
        color = {"medal_gold": (240, 200, 80), "medal_silver": (205, 210, 220),
                 "medal_bronze": (205, 140, 92)}.get(kind, (150, 155, 170))
        d.ellipse((cx - u * .9, cy - u * .9, cx + u * .9, cy + u * .9), fill=color, outline=(255, 255, 255))
        d.ellipse((cx - u * .45, cy - u * .45, cx + u * .45, cy + u * .45), outline=(255, 255, 255), width=2)
    else:
        d.ellipse((cx - u * .8, cy - u * .8, cx + u * .8, cy + u * .8), fill=(150, 155, 170))


def division_icon(n) -> str:
    return {7: "crown", 5: "diamond", 4: "gem", 3: "medal_gold", 2: "medal_silver",
            1: "medal_bronze"}.get(n, "medal")


def render_stats_card(username: str, data: dict, head_bytes) -> io.BytesIO:
    W = CARD_W
    kills, deaths = data.get("kills") or 0, data.get("deaths") or 0
    f_kills, f_deaths = data.get("final_kills") or 0, data.get("final_deaths") or 0
    wins, played = data.get("wins") or 0, data.get("played") or 0
    losses = data.get("losses")
    if losses is None:
        losses = max(played - wins, 0)
    winrate = (wins / played * 100) if played else None
    past = sorted(data.get("past_divisions") or [], key=lambda x: x.get("season_id", 0), reverse=True)

    content = Image.new("RGBA", (W, 1700), (0, 0, 0, 0))
    d = ImageDraw.Draw(content)

    def panel(rect, fill=C_PANEL, outline=C_EDGE, r=16, width=2):
        d.rounded_rectangle(rect, r, fill=fill, outline=outline, width=width)

    def stat_box(x, y, w, h, icon, label, value, value_color=C_TEXT, star=False):
        panel((x, y, x + w, y + h))
        draw_icon(d, icon, x + 32, y + 34, 28)
        d.text((x + 58, y + 38), label.upper() + ":", font=fit_font(label.upper() + ":", w - 70, 20),
               fill=C_TEXT, anchor="ls")
        vfont = fit_font(value, w - (74 if star else 40), 42)
        d.text((x + 22, y + h - 16), value, font=vfont, fill=value_color, anchor="ls")
        if star:
            sx = x + 22 + vfont.getlength(value) + 24
            d.polygon(star_points(sx, y + h - 32, 16, 7), fill=(255, 205, 70), outline=(214, 150, 30))

    # --- titolo
    title_a, title_b = "STATISTICHE BEDWARS - ", username
    tfont = fit_font(title_a + title_b, 590, 40)
    tx, ty = 78, 92
    d.text((tx, ty), title_a, font=tfont, fill=C_GOLD, anchor="ls")
    d.text((tx + tfont.getlength(title_a), ty), title_b, font=tfont, fill=C_CYAN, anchor="ls")
    end_x = tx + tfont.getlength(title_a + title_b) + 24
    for sx, sy in ((50, ty - 16), (end_x, ty - 16)):
        d.polygon(star_points(sx, sy, 13, 4, 4), fill=(250, 214, 120))

    # --- riquadro divisione e clan
    hx, hy, hw, hh = 40, 122, 620, 110
    panel((hx, hy, hx + hw, hy + hh), fill=(22, 30, 46), outline=(70, 190, 210), r=22, width=3)
    div = data.get("current_division")
    div_color = DIV_COLORS.get(div, C_TEXT)
    draw_icon(d, "swords", hx + 40, hy + 36, 30)
    d.text((hx + 66, hy + 42), "DIVISIONE:", font=card_font(24), fill=C_TEXT, anchor="ls")
    d.text((hx + 30, hy + 88), division_name(div) if div is not None else "N/D",
           font=fit_font(division_name(div) if div is not None else "N/D", 200, 36), fill=div_color, anchor="ls")
    draw_icon(d, division_icon(div), hx + 300, hy + 56, 64)
    clan = data.get("clan_name")
    draw_icon(d, "shield", hx + 372, hy + 56, 58)
    d.text((hx + 410, hy + 42), "CLAN:", font=card_font(24), fill=C_TEXT, anchor="ls")
    clan_w = hw - 410 - 18
    if clan:
        d.text((hx + 410, hy + 76), clan, font=fit_font(clan, clan_w, 34), fill=C_GOLD, anchor="ls")
        role = CLAN_ROLES.get(data.get("clan_role"))
        if role:
            d.text((hx + 410, hy + 100), role, font=card_font(22), fill=C_MUTED, anchor="ls")
    else:
        d.text((hx + 410, hy + 80), "Nessuno", font=card_font(30), fill=C_MUTED, anchor="ls")

    # --- testa del giocatore con alone
    head_x, head_y, head_s = 705, 44, 160
    glow = Image.new("RGBA", (W, 400), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((head_x - 4, head_y - 4, head_x + head_s + 4, head_y + head_s + 4),
                                           18, fill=(150, 110, 255, 150))
    content.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)), (0, 0))
    placed = False
    if head_bytes:
        try:
            head = Image.open(io.BytesIO(head_bytes)).convert("RGBA").resize((head_s, head_s), Image.NEAREST)
            content.alpha_composite(head, (head_x, head_y))
            placed = True
        except Exception:
            placed = False
    if not placed:
        d.rounded_rectangle((head_x, head_y, head_x + head_s, head_y + head_s), 14, fill=C_PANEL, outline=C_EDGE, width=3)
        d.text((head_x + head_s / 2, head_y + head_s / 2), "?", font=card_font(70), fill=C_MUTED, anchor="mm")

    # --- griglia statistiche (3 colonne)
    gx, gw, gap, bh, rgap = 40, 260, 20, 100, 18
    cols = [gx, gx + gw + gap, gx + 2 * (gw + gap)]
    y = 256
    rows = [
        [("swords", "Uccisioni", fmt(kills)), ("skull", "Morti", fmt(deaths)), ("bars", "K/D", ratio(kills, deaths))],
        [("skull", "Uccisioni finali", fmt(f_kills)), ("explosion", "Morti finali", fmt(f_deaths)),
         ("bars_down", "FKDR", ratio(f_kills, f_deaths))],
        [("bed", "Letti rotti", fmt(data.get("beds_broken"))), ("trophy", "Winstreak", fmt(data.get("winstreak"))),
         ("trophy", "Top winstreak", fmt(data.get("h_winstreak")))],
    ]
    for row in rows:
        for cx, (icon, label, value) in zip(cols, row):
            stat_box(cx, y, gw, bh, icon, label, value)
        y += bh + rgap

    # --- livello, classifica, monete
    stat_box(cols[0], y, gw, bh, "star", "Livello", fmt(data.get("level")), star=True)
    rank, total = data.get("level_rank"), data.get("total_players")
    stat_box(cols[1], y, gw, bh, "globe", "Classifica", f"#{fmt(rank)}" if rank else "N/D")
    stat_box(cols[2], y, gw, bh, "chest", "Monete", fmt(data.get("coins")))
    if rank and total:
        pct = f"Top {max(rank / total * 100, 0.1):.1f}%".replace(".", ",")
        pf = card_font(20)
        pw = pf.getlength(pct) + 36
        px = cols[1] + gw / 2 - pw / 2
        d.rounded_rectangle((px, y + bh - 14, px + pw, y + bh + 16), 15, fill=(24, 52, 44), outline=C_GREEN, width=2)
        d.text((cols[1] + gw / 2, y + bh + 2), pct, font=pf, fill=(150, 240, 180), anchor="mm")
    y += bh + rgap + 20

    # --- record partite
    py, ph = y, 134
    panel((gx, py, W - gx, py + ph), r=18)
    d.rounded_rectangle((gx + 18, py - 15, gx + 18 + 150, py + 15), 14, fill=(20, 40, 56), outline=(70, 190, 210), width=2)
    d.text((gx + 93, py), "PARTITE", font=card_font(20), fill=(150, 220, 240), anchor="mm")
    colx = [gx + 28, gx + 28 + 290, gx + 28 + 580]
    for cxx, icon, label, value, color in (
            (colx[0], "check", "Vittorie", fmt(wins), C_TEXT),
            (colx[1], "cross", "Sconfitte", fmt(losses), C_TEXT),
            (colx[2], "trend", "% vittorie", f"{winrate:.1f}%".replace(".", ",") if winrate is not None else "N/D", C_TEXT)):
        draw_icon(d, icon, cxx + 14, py + 46, 26)
        d.text((cxx + 38, py + 53), label.upper() + ":", font=card_font(22), fill=C_TEXT, anchor="ls")
        d.text((cxx, py + 94), value, font=card_font(40), fill=color, anchor="ls")
    if winrate is not None:
        arrow_x = colx[2] + card_font(40).getlength(f"{winrate:.1f}%") + 22
        if winrate >= 50:
            d.polygon([(arrow_x, py + 62), (arrow_x + 18, py + 62), (arrow_x + 9, py + 46)], fill=C_GREEN)
        else:
            d.polygon([(arrow_x, py + 70), (arrow_x + 18, py + 70), (arrow_x + 9, py + 86)], fill=C_RED)
        bx0, bx1, by = gx + 28, W - gx - 28, py + ph - 20
        d.rounded_rectangle((bx0, by, bx1, by + 9), 4, fill=(96, 48, 58))
        if winrate > 0:
            d.rounded_rectangle((bx0, by, bx0 + (bx1 - bx0) * winrate / 100, by + 9), 4, fill=C_GREEN)
    y = py + ph + 36

    # --- stagioni precedenti
    if past:
        draw_icon(d, "scroll", gx + 14, y + 4, 28)
        d.text((gx + 40, y + 12), "STAGIONI PRECEDENTI", font=card_font(24), fill=C_TEXT, anchor="ls")
        y += 34
        colw = (W - 2 * gx - 20) / 2
        per_col = math.ceil(len(past) / 2)
        for i, season in enumerate(past):
            col, row = divmod(i, per_col)
            sx = gx + col * (colw + 20)
            sy = y + row * 48
            d.rounded_rectangle((sx, sy, sx + colw, sy + 40), 12, fill=(30, 38, 58), outline=C_EDGE, width=2)
            sdiv = season.get("division")
            draw_icon(d, division_icon(sdiv), sx + 28, sy + 20, 26)
            label = f"STAGIONE {season.get('season_id')}: "
            sf = card_font(22)
            d.text((sx + 54, sy + 28), label, font=sf, fill=C_TEXT, anchor="ls")
            d.text((sx + 54 + sf.getlength(label), sy + 28), division_name(sdiv).upper(), font=sf,
                   fill=DIV_COLORS.get(sdiv, C_TEXT), anchor="ls")
        y += per_col * 48
    else:
        y += 4

    # --- piè di pagina
    foot = "Dati da CoralMC"
    ff = card_font(20)
    fw = ff.getlength(foot) + 70
    fy = y + 10
    d.rounded_rectangle((W / 2 - fw / 2, fy, W / 2 + fw / 2, fy + 38), 19, fill=(22, 30, 46), outline=C_EDGE, width=2)
    d.text((W / 2, fy + 19), foot, font=ff, fill=C_MUTED, anchor="mm")
    H = int(fy + 38 + 34)

    # --- sfondo, cornice e angoli arrotondati
    content = content.crop((0, 0, W, H))
    gradient = Image.linear_gradient("L").resize((W, H))
    card = ImageOps.colorize(gradient, (13, 19, 33), (23, 31, 50)).convert("RGBA")
    card.alpha_composite(content)
    fd = ImageDraw.Draw(card)
    fd.rounded_rectangle((1, 1, W - 2, H - 2), 26, outline=(125, 141, 170), width=4)
    fd.rounded_rectangle((13, 13, W - 14, H - 14), 20, outline=(58, 72, 104), width=2)
    for x in range(70, W - 60, 20):  # tacche "runiche" tra le due cornici
        fd.line((x, 7, x + 7, 7), fill=(92, 112, 152), width=2)
        fd.line((x, H - 8, x + 7, H - 8), fill=(92, 112, 152), width=2)
    for yy in range(70, H - 60, 20):
        fd.line((7, yy, 7, yy + 7), fill=(92, 112, 152), width=2)
        fd.line((W - 8, yy, W - 8, yy + 7), fill=(92, 112, 152), width=2)
    for gxp, gyp in ((13, 13), (W - 14, 13), (13, H - 14), (W - 14, H - 14), (W / 2, 7), (W / 2, H - 8)):
        fd.polygon([(gxp, gyp - 11), (gxp + 9, gyp), (gxp, gyp + 11), (gxp - 9, gyp)],
                   fill=(70, 210, 230), outline=(210, 250, 255))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), 28, fill=255)
    card.putalpha(mask)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    return buf


def finalize_card(content, W: int, H: int) -> io.BytesIO:
    """Ritaglia, aggiunge sfondo sfumato, cornice e angoli arrotondati; restituisce il PNG."""
    content = content.crop((0, 0, W, H))
    gradient = Image.linear_gradient("L").resize((W, H))
    card = ImageOps.colorize(gradient, (13, 19, 33), (23, 31, 50)).convert("RGBA")
    card.alpha_composite(content)
    fd = ImageDraw.Draw(card)
    fd.rounded_rectangle((1, 1, W - 2, H - 2), 26, outline=(125, 141, 170), width=4)
    fd.rounded_rectangle((13, 13, W - 14, H - 14), 20, outline=(58, 72, 104), width=2)
    for x in range(70, W - 60, 20):
        fd.line((x, 7, x + 7, 7), fill=(92, 112, 152), width=2)
        fd.line((x, H - 8, x + 7, H - 8), fill=(92, 112, 152), width=2)
    for yy in range(70, H - 60, 20):
        fd.line((7, yy, 7, yy + 7), fill=(92, 112, 152), width=2)
        fd.line((W - 8, yy, W - 8, yy + 7), fill=(92, 112, 152), width=2)
    for gxp, gyp in ((13, 13), (W - 14, 13), (13, H - 14), (W - 14, H - 14), (W / 2, 7), (W / 2, H - 8)):
        fd.polygon([(gxp, gyp - 11), (gxp + 9, gyp), (gxp, gyp + 11), (gxp - 9, gyp)],
                   fill=(70, 210, 230), outline=(210, 250, 255))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), 28, fill=255)
    card.putalpha(mask)
    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    return buf


def now_label() -> str:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:  # fuso non disponibile (es. Windows senza tzdata): ora del computer
        now = datetime.datetime.now()
    return now.strftime("%H:%M")


CLAN_CARD_MAX_ROWS = 20  # oltre questo numero scrive "e altri N membri"


def render_clan_card(clan: dict, exps: list, heads: list) -> io.BytesIO:
    W = CARD_W
    members = clan["members"]
    shown = min(len(members), CLAN_CARD_MAX_ROWS)
    content = Image.new("RGBA", (W, 520 + shown * 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(content)

    # --- titolo centrato con scintille
    title_a, title_b = "CLAN ", clan["name"]
    tfont = fit_font(title_a + title_b, 700, 44)
    total_w = tfont.getlength(title_a + title_b)
    tx, ty = W / 2 - total_w / 2, 92
    d.text((tx, ty), title_a, font=tfont, fill=C_GOLD, anchor="ls")
    d.text((tx + tfont.getlength(title_a), ty), title_b, font=tfont, fill=C_CYAN, anchor="ls")
    for sx in (tx - 34, tx + total_w + 34):
        d.polygon(star_points(sx, ty - 16, 13, 4, 4), fill=(250, 214, 120))

    # --- riepilogo (3 riquadri)
    def box(x, y, w, h, icon, label, value, color=C_TEXT):
        d.rounded_rectangle((x, y, x + w, y + h), 16, fill=C_PANEL, outline=C_EDGE, width=2)
        draw_icon(d, icon, x + 32, y + 34, 28)
        d.text((x + 58, y + 38), label.upper() + ":", font=fit_font(label.upper() + ":", w - 70, 20),
               fill=C_TEXT, anchor="ls")
        d.text((x + 22, y + h - 16), value, font=fit_font(value, w - 40, 40), fill=color, anchor="ls")

    gx, gw, gap, bh = 40, 260, 20, 100
    leader = next((n for n, r in members if role_rank(r) == 3), "N/D")
    xp = clan.get("xp")
    y = 130
    box(gx, y, gw, bh, "shield", "Membri", str(len(members)))
    box(gx + gw + gap, y, gw, bh, "star", "XP del clan", fmt(xp) if xp is not None else "N/D", C_GOLD)
    box(gx + 2 * (gw + gap), y, gw, bh, "crown", "Leader", leader, C_CYAN)
    y += bh + 34

    # --- elenco membri
    draw_icon(d, "scroll", gx + 14, y + 4, 28)
    d.text((gx + 40, y + 12), "ELENCO MEMBRI", font=card_font(24), fill=C_TEXT, anchor="ls")
    y += 38

    known = [e for e in exps if isinstance(e, int)]
    max_exp = max(known) if known else 0
    rw, rh = W - 2 * gx, 70
    accent = {3: C_GOLD, 2: C_CYAN}
    for i, (name, role) in enumerate(members[:shown]):
        rank = role_rank(role)
        color = accent.get(rank, (120, 132, 160))
        d.rounded_rectangle((gx, y, gx + rw, y + rh), 14, fill=(30, 38, 58), outline=C_EDGE, width=2)
        d.rounded_rectangle((gx + 8, y + 12, gx + 14, y + rh - 12), 3, fill=color)

        # testa del giocatore
        hx, hy = gx + 28, y + 13
        placed = False
        if heads[i]:
            try:
                head = Image.open(io.BytesIO(heads[i])).convert("RGBA").resize((44, 44), Image.NEAREST)
                content.alpha_composite(head, (hx, hy))
                placed = True
            except Exception:
                placed = False
        if not placed:
            d.rounded_rectangle((hx, hy, hx + 44, hy + 44), 8, fill=C_PANEL, outline=C_EDGE, width=2)
            d.text((hx + 22, hy + 22), "?", font=card_font(24), fill=C_MUTED, anchor="mm")

        # nome, ruolo, icona del grado
        nfont = fit_font(name, 330, 28)
        d.text((gx + 88, y + 36), name, font=nfont, fill=C_TEXT, anchor="ls")
        if rank >= 2:
            draw_icon(d, "crown" if rank == 3 else "star", gx + 88 + nfont.getlength(name) + 20, y + 26, 22)
        d.text((gx + 88, y + 59), role_label(role).upper(), font=card_font(17), fill=color, anchor="ls")

        # EXP con barra proporzionale al migliore
        exp = exps[i]
        right = gx + rw - 24
        if isinstance(exp, int):
            d.text((right, y + 36), f"{fmt(exp)} EXP", font=card_font(28), fill=C_CYAN, anchor="rs")
            bx0 = right - 220
            d.rounded_rectangle((bx0, y + 48, right, y + 56), 4, fill=(38, 48, 72))
            if max_exp > 0 and exp > 0:
                d.rounded_rectangle((bx0, y + 48, bx0 + 220 * exp / max_exp, y + 56), 4, fill=(70, 200, 220))
        else:
            d.text((right, y + 36), "EXP N/D", font=card_font(24), fill=C_MUTED, anchor="rs")
        y += rh + 10

    if len(members) > shown:
        d.text((W / 2, y + 12), f"… e altri {len(members) - shown} membri", font=card_font(22),
               fill=C_MUTED, anchor="mm")
        y += 36

    # --- piè di pagina
    foot = f"Aggiornato alle {now_label()} · ogni {CLAN_UPDATE_MINUTES} min · Dati da CoralMC"
    ff = card_font(20)
    fw = ff.getlength(foot) + 70
    fy = y + 14
    d.rounded_rectangle((W / 2 - fw / 2, fy, W / 2 + fw / 2, fy + 38), 19, fill=(22, 30, 46), outline=C_EDGE, width=2)
    d.text((W / 2, fy + 19), foot, font=ff, fill=C_MUTED, anchor="mm")
    return finalize_card(content, W, int(fy + 38 + 34))


async def make_clan_card(clan: dict) -> io.BytesIO:
    """EXP e teste dei membri in parallelo, poi disegna la scheda in un thread."""
    names = [name for name, _ in clan["members"][:CLAN_CARD_MAX_ROWS]]

    async def head(session, name):
        try:
            async with session.get(f"https://minotar.net/helm/{name}/64.png") as resp:
                return await resp.read() if resp.status == 200 else None
        except Exception:
            return None

    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        heads = await asyncio.gather(*(head(session, n) for n in names))
    players = await asyncio.gather(*(fetch_player(n) for n in names), return_exceptions=True)
    exps = []
    for result in players:
        data = result[0] if isinstance(result, tuple) else None
        exps.append(data.get("current_division_exp") if data else None)
    return await bot.loop.run_in_executor(None, render_clan_card, clan, exps, list(heads))


async def make_stats_card(username: str, data: dict) -> io.BytesIO:
    """Scarica la testa della skin e disegna la scheda (in un thread, per non bloccare il bot)."""
    head = None
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://minotar.net/helm/{username}/200.png") as resp:
                if resp.status == 200:
                    head = await resp.read()
    except Exception as exc:
        print(f"[stats] Skin non scaricata: {exc!r}")
    return await bot.loop.run_in_executor(None, render_stats_card, username, data, head)


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
    if Image is not None:
        try:
            card = await make_stats_card(username, payload)
            await interaction.followup.send(file=discord.File(card, filename=f"stats_{username}.png"))
            return
        except Exception as exc:  # se il disegno fallisce, risponde comunque
            print(f"[stats] Scheda non generata, uso l'embed: {exc!r}")
    await interaction.followup.send(embed=build_stats_embed(username, payload))


# ---------------------------------------------------------------- /setup ---

@bot.tree.command(name="setup", description="Pubblica il pannello per aprire i ticket")
@owner_only()
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
    if isinstance(error, NotAllowedError):
        text = error.text
    else:
        print(f"[setup] Errore: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


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
        "members": f"👥 𝙢𝙚𝙢𝙗𝙧𝙞: {len(humans)}",
        "online": f"🟢 𝙤𝙣𝙡𝙞𝙣𝙚: {len(online)}",
        "bots": f"🤖 𝙗𝙤𝙩: {len(bots)}",
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
@owner_only()
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
    if isinstance(error, NotAllowedError):
        text = error.text
    else:
        print(f"[stats] Errore in /set-up-a1: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ------------------------------------------------------------------ /pex ---

def role_key(name: str) -> str:
    """'👑 | owner' -> 'owner', '⭐utente' -> 'utente', 'Sr. admin' -> 'sr. admin'."""
    name = name.split("|")[-1].strip().lower()
    return re.sub(r"^[^\w]+", "", name)  # toglie emoji e simboli iniziali


def find_role(guild: discord.Guild, name: str):
    key = role_key(name)
    return discord.utils.find(lambda r: role_key(r.name) == key, guild.roles)


def is_owner_tier(guild: discord.Guild, user) -> bool:
    """Proprietario del server, owner o co-owner."""
    if user.id == guild.owner_id:
        return True
    allowed = [find_role(guild, name) for name in OWNER_COMMAND_ROLES]
    return any(role is not None and role in getattr(user, "roles", []) for role in allowed)


def is_restricted_rank(guild: discord.Guild, role: discord.Role) -> bool:
    return any(find_role(guild, name) == role for name in PEX_OWNER_ONLY_RANKS)


def has_protected_role(guild: discord.Guild, member: discord.Member) -> bool:
    protected = [find_role(guild, n) for n in PEX_PROTECTED]
    return any(role is not None and role in member.roles for role in protected)


@bot.tree.command(name="pex", description="Promuove un utente al grado successivo")
@app_commands.describe(utente="L'utente da promuovere")
@pex_allowed()
@app_commands.guild_only()
async def pex(interaction: discord.Interaction, utente: discord.Member):
    guild = interaction.guild

    if utente.bot:
        await interaction.response.send_message("Non posso promuovere un bot.", ephemeral=True)
        return
    if has_protected_role(guild, utente):
        await interaction.response.send_message(
            f"{utente.mention} ha un grado protetto ({', '.join(PEX_PROTECTED)}): non lo modifico.",
            ephemeral=True)
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
    if is_restricted_rank(guild, new_role) and not is_owner_tier(guild, interaction.user):
        await interaction.response.send_message(
            f"Solo **owner** e **co-owner** possono assegnare il grado **{new_role.name}**.",
            ephemeral=True)
        return
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
    if isinstance(error, NotAllowedError):
        text = error.text
    else:
        print(f"[pex] Errore: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="depex", description="Abbassa un utente al grado precedente")
@app_commands.describe(utente="L'utente da retrocedere")
@pex_allowed()
@app_commands.guild_only()
async def depex(interaction: discord.Interaction, utente: discord.Member):
    guild = interaction.guild

    if utente.bot:
        await interaction.response.send_message("Non posso retrocedere un bot.", ephemeral=True)
        return
    if has_protected_role(guild, utente):
        await interaction.response.send_message(
            f"{utente.mention} ha un grado protetto ({', '.join(PEX_PROTECTED)}): non lo modifico.",
            ephemeral=True)
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
    if is_restricted_rank(guild, old_role) and not is_owner_tier(guild, interaction.user):
        await interaction.response.send_message(
            f"Solo **owner** e **co-owner** possono togliere il grado **{old_role.name}**.",
            ephemeral=True)
        return
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
    if isinstance(error, NotAllowedError):
        text = error.text
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
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        # Se il registro è stato creato a mano (o il file di configurazione manca),
        # usa un canale con "ticket-log" nel nome
        channel = discord.utils.find(
            lambda c: "ticket-log" in c.name.lower(), guild.text_channels)
    return channel


async def send_ticket_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Scrive nel registro; se non è configurato o fallisce, il ticket funziona lo stesso."""
    channel = get_log_channel(guild)
    if channel is None:
        print("[ticket-log] Nessun registro trovato: lancia /ticket-log per crearlo.")
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        print(f"[ticket-log] Impossibile scrivere in #{channel.name}: {exc} "
              "(controlla che il bot possa vedere il canale e scrivere/incorporare link)")


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
@owner_only()
@app_commands.guild_only()
async def ticket_log(interaction: discord.Interaction):
    guild = interaction.guild
    existing = get_log_channel(guild)
    if existing:
        await interaction.response.send_message(
            f"Il registro esiste già: {existing.mention}", ephemeral=True)
        return

    staff_role = find_role(guild, STAFF_ROLE_NAME)
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
    if isinstance(error, NotAllowedError):
        text = error.text
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
@owner_only()
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
    if isinstance(error, NotAllowedError):
        text = error.text
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


async def render_clan(clan: dict):
    """Ritorna (file, embed): la scheda immagine se possibile, altrimenti l'embed di ripiego."""
    if Image is not None:
        try:
            return discord.File(await make_clan_card(clan), filename="clan.png"), None
        except Exception as exc:
            print(f"[clan] Scheda non generata, uso l'embed: {exc!r}")
    return None, await build_clan_embed(clan)


@clan_group.command(name="member", description="Mostra i membri del clan (si aggiorna da solo ogni 10 minuti)")
@owner_only()
async def clan_member(interaction: discord.Interaction):
    await interaction.response.defer()
    clan, error = await fetch_clan(CLAN_ID)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    file, embed = await render_clan(clan)
    if file:
        message = await interaction.followup.send(file=file, wait=True)
    else:
        message = await interaction.followup.send(embed=embed, wait=True)

    # Da ora questo è il messaggio che si aggiorna da solo (uno per server)
    cfg = load_clan_live()
    cfg[str(interaction.guild_id)] = {
        "channel": interaction.channel_id,
        "message": message.id,
        "members": [name for name, _ in clan["members"]],
    }
    save_clan_live(cfg)


@clan_member.error
async def clan_member_error(interaction: discord.Interaction, error):
    if isinstance(error, NotAllowedError):
        text = error.text
    else:
        print(f"[clan] Errore in /clan member: {error!r}")
        text = "Si è verificato un errore, controlla il terminale del bot."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def load_clan_log() -> dict:
    try:
        with open(CLAN_LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@clan_group.command(name="logs",
                    description="Imposta il canale dove avvisare chi entra o esce dal clan")
@app_commands.describe(canale="Canale esistente da usare (se lo lasci vuoto ne creo uno nuovo)")
@owner_only()
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
    if isinstance(error, NotAllowedError):
        text = error.text
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
            file, embed = await render_clan(clan)
            if file:
                await message.edit(embed=None, attachments=[file])
            else:
                await message.edit(embed=embed, attachments=[])
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
        when = discord.utils.format_dt(discord.utils.utcnow(), "F")  # data e ora, nel fuso di chi legge
        try:
            for name in joined:
                await log_channel.send(
                    f"👋 **{name}** è entrato nel clan **{clan['name']}**!\n🕒 {when}")
            for name in left:
                await log_channel.send(
                    f"🚪 **{name}** ha lasciato il clan **{clan['name']}**.\n🕒 {when}")
        except discord.HTTPException as exc:
            print(f"[clan] Impossibile scrivere nel canale dei log: {exc}")

        entry["members"] = [name for name, _ in clan["members"]]
        changed = True
    if changed:
        save_clan_live(cfg)


bot.tree.add_command(clan_group)


async def assign_auto_role(member: discord.Member) -> None:
    role = find_role(member.guild, AUTO_ROLE_NAME)
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


def find_ticket_panel_channel(guild: discord.Guild):
    """Canale del pannello ticket: ha 'ticket' nel nome, ma non è il log né un ticket aperto."""
    prefixes = tuple(f"{cfg['prefix']}-" for cfg in TICKET_TYPES.values())
    for ch in guild.text_channels:
        name = ch.name.lower()
        if WELCOME_TICKET_KEYWORD not in name or "log" in name or name.startswith(prefixes):
            continue
        if ch.category and ch.category.name == CATEGORY_NAME:
            continue
        return ch
    return None


async def send_welcome(member: discord.Member) -> None:
    guild = member.guild
    channel = discord.utils.find(
        lambda c: WELCOME_CHANNEL_KEYWORD in c.name.lower(), guild.text_channels)
    if channel is None:
        print(f"[benvenuto] Nessun canale con '{WELCOME_CHANNEL_KEYWORD}' nel nome in {guild.name}")
        return

    ticket = find_ticket_panel_channel(guild)
    rules = discord.utils.find(
        lambda c: RULES_CHANNEL_KEYWORD in c.name.lower(), guild.text_channels)
    ticket_text = f"in {ticket.mention}" if ticket else "dal pannello del supporto"
    rules_text = f"in {rules.mention}" if rules else f"nel canale «{RULES_CHANNEL_KEYWORD}»"

    embed = discord.Embed(
        title=WELCOME_TITLE,
        description=(
            f"{member.mention}, benvenuto/a su **{guild.name}**! ⚔️\n\n"
            f"➜ Leggi il regolamento {rules_text}!\n"
            f"➜ Hai domande, vuoi fare un provino o ti serve aiuto? "
            f"Apri un ticket {ticket_text}!"
        ),
        color=discord.Color.gold(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=WELCOME_FOOTER)
    try:
        await channel.send(content=member.mention, embed=embed)
    except discord.HTTPException as exc:
        print(f"[benvenuto] Impossibile scrivere in #{channel.name}: {exc}")


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    await assign_auto_role(member)
    await send_welcome(member)


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