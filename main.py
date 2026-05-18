import asyncio
import json
import logging
import math
import os
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} precisa ser um número inteiro.") from exc


def required_env_int(name: str) -> int:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} não foi definido no ambiente.")
    return env_int(name, 0)


GUILD_ID = required_env_int("DISCORD_GUILD_ID")
SUPPORT_ROLE_ID = required_env_int("DISCORD_SUPPORT_ROLE_ID")
TICKET_RATE_LIMIT_MAX = env_int("TICKET_RATE_LIMIT_MAX", 3)
TICKET_RATE_LIMIT_WINDOW_SECONDS = env_int("TICKET_RATE_LIMIT_WINDOW_SECONDS", 3600)
TICKET_RATE_LIMIT_COOLDOWN_SECONDS = env_int("TICKET_RATE_LIMIT_COOLDOWN_SECONDS", 60)
TOKEN = os.getenv("DISCORD_TOKEN")

CONFIG_PATH = Path("config.json")
TICKETS_PATH = Path("tickets.json")

DEFAULT_CONFIG = {
    "panel_channel_ids": [],
    "panel_title": "Atendimento",
    "panel_description": "Selecione uma opção abaixo para falar com a equipe.",
}

DEFAULT_TICKET_STORE = {
    "last_ticket_id": 0,
    "tickets": {},
    "cooldowns": {},
}

CATEGORY_LABELS = {
    "atendimento": "Atendimento",
    "denuncia": "Denúncia",
}

TICKET_STATUSES = {
    "open": {"emoji": "🔵", "label": "Aberto"},
    "in_progress": {"emoji": "🟢", "label": "Em Atendimento"},
    "completed": {"emoji": "🟠", "label": "Concluído"},
    "closed": {"emoji": "🔴", "label": "Fechado"},
}
ACTIVE_TICKET_STATUSES = {"open", "in_progress", "completed"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ticket-bot")
ticket_store_lock = asyncio.Lock()


# ================= UTIL =================
async def safe_reply(interaction: discord.Interaction, content: str | None = None, *, ephemeral: bool = True, **kwargs):
    if not interaction.response.is_done():
        await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
    else:
        await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def datetime_to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def format_discord_time(value: str | datetime | None, *, style: str = "f") -> str:
    if not value:
        return "desconhecido"

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value

    return discord.utils.format_dt(value, style=style)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        data = deepcopy(default)
        save_json(path, data)
        return data

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"O arquivo {path} está corrompido. Corrija ou renomeie antes de iniciar o bot.") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"O arquivo {path} precisa conter um objeto JSON.")

    return data


def save_json(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(path)


def load_config() -> dict:
    config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    changed = False

    for key, value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = deepcopy(value)
            changed = True

    if changed:
        save_json(CONFIG_PATH, config)

    return config


def load_ticket_store() -> dict:
    store = load_json(TICKETS_PATH, DEFAULT_TICKET_STORE)
    changed = False

    for key, value in DEFAULT_TICKET_STORE.items():
        if key not in store:
            store[key] = deepcopy(value)
            changed = True

    if not isinstance(store.get("tickets"), dict):
        store["tickets"] = {}
        changed = True

    if changed:
        save_json(TICKETS_PATH, store)

    return store


def parse_channel_ids(raw_value: str) -> list[int]:
    ids = []
    for match in re.finditer(r"\d{17,20}", raw_value):
        channel_id = int(match.group(0))
        if channel_id not in ids:
            ids.append(channel_id)
    return ids


def sanitize_thread_piece(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.replace("@", "").replace("#", "").replace("`", "").replace("|", "")
    return value[:32] or "usuario"


def normalize_ticket_status(status: str | None) -> str:
    return status if status in TICKET_STATUSES else "open"


def ticket_status_text(status: str | None) -> str:
    status_data = TICKET_STATUSES[normalize_ticket_status(status)]
    return f"{status_data['emoji']} {status_data['label']}"


def ticket_status_label(status: str | None) -> str:
    return TICKET_STATUSES[normalize_ticket_status(status)]["label"]


def build_ticket_name(ticket_id: str, user: discord.abc.User, *, status: str = "open") -> str:
    username = sanitize_thread_piece(getattr(user, "display_name", user.name))
    status_data = TICKET_STATUSES[normalize_ticket_status(status)]
    return f"{status_data['emoji']} ticket-{ticket_id} | {username} | {status_data['label']}"[:100]


def build_ticket_name_from_record(record: dict, *, status: str | None = None) -> str:
    raw_ticket_id = record.get("ticket_id") or "?????"
    ticket_id = str(raw_ticket_id).zfill(5) if str(raw_ticket_id).isdigit() else str(raw_ticket_id)
    status = normalize_ticket_status(status or record.get("status"))
    status_data = TICKET_STATUSES[status]

    user_snapshot = record.get("user_last_snapshot") or record.get("user_open_snapshot") or {}
    if not isinstance(user_snapshot, dict):
        user_snapshot = {}

    username = (
        user_snapshot.get("display_name")
        or user_snapshot.get("username")
        or f"usuario-{safe_int(record.get('user_id'))}"
    )
    username = sanitize_thread_piece(username)

    return f"{status_data['emoji']} ticket-{ticket_id} | {username} | {status_data['label']}"[:100]


def limited_text(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def clean_subject(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return limited_text(value, 90)


def subject_from_report(report: str) -> str:
    for line in (report or "").splitlines():
        line = clean_subject(line)
        if line:
            return line
    return ""


def ticket_subject(record: dict) -> str:
    subject = clean_subject(str(record.get("subject") or ""))
    if subject:
        return subject

    report_subject = subject_from_report(str(record.get("report") or ""))
    return report_subject or "Sem assunto"


def build_user_snapshot(user: discord.abc.User) -> dict:
    snapshot = {
        "id": user.id,
        "mention": user.mention,
        "username": user.name,
        "display_name": getattr(user, "display_name", user.name),
        "global_name": getattr(user, "global_name", None),
        "discriminator": getattr(user, "discriminator", None),
        "bot": user.bot,
        "created_at": datetime_to_iso(user.created_at),
        "avatar_url": str(user.avatar.url) if user.avatar else None,
        "display_avatar_url": str(user.display_avatar.url),
    }

    if isinstance(user, discord.Member):
        roles = [role for role in user.roles if role.name != "@everyone"]
        snapshot.update(
            {
                "nick": user.nick,
                "joined_at": datetime_to_iso(user.joined_at),
                "top_role_id": user.top_role.id if user.top_role else None,
                "top_role_name": user.top_role.name if user.top_role else None,
                "role_ids": [role.id for role in roles],
                "role_names": [role.name for role in roles],
            }
        )

    return snapshot


def build_panel_embed(config: dict) -> discord.Embed:
    embed = discord.Embed(
        title=config.get("panel_title") or DEFAULT_CONFIG["panel_title"],
        description=config.get("panel_description") or DEFAULT_CONFIG["panel_description"],
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Sistema de tickets")
    return embed


def configured_panel_channel_ids() -> set[int]:
    return {int(channel_id) for channel_id in load_config().get("panel_channel_ids", [])}


def is_configured_panel_channel(channel_id: int) -> bool:
    channel_ids = configured_panel_channel_ids()
    return not channel_ids or channel_id in channel_ids


def get_ticket_record(thread_id: int, store: dict | None = None) -> dict | None:
    store = store or load_ticket_store()
    record = store.get("tickets", {}).get(str(thread_id))
    return record if isinstance(record, dict) else None


def get_user_ticket_records(user_id: int, store: dict | None = None, *, exclude_thread_id: int | None = None) -> list[dict]:
    store = store or load_ticket_store()
    records = []

    for record in store.get("tickets", {}).values():
        if not isinstance(record, dict):
            continue

        try:
            record_user_id = int(record.get("user_id", 0))
            record_thread_id = int(record.get("thread_id", 0))
        except (TypeError, ValueError):
            continue

        if record_user_id != user_id:
            continue

        if exclude_thread_id is not None and record_thread_id == exclude_thread_id:
            continue

        records.append(record)

    return sorted(records, key=lambda record: record.get("opened_at") or "", reverse=True)


def normalize_ticket_id_input(value: str | None) -> str:
    value = re.sub(r"\D+", "", value or "")
    return value.zfill(5) if value else ""


def ticket_id_from_record(record: dict) -> str:
    value = str(record.get("ticket_id") or "")
    return value.zfill(5) if value.isdigit() else value


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def humanize_seconds(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds} segundo(s)"

    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} minuto(s)" + (f" e {remainder} segundo(s)" if remainder else "")

    hours, minutes = divmod(minutes, 60)
    return f"{hours} hora(s)" + (f" e {minutes} minuto(s)" if minutes else "")


def recent_ticket_records_for_user(store: dict, user_id: int, *, now: datetime) -> list[dict]:
    window_start = now - timedelta(seconds=TICKET_RATE_LIMIT_WINDOW_SECONDS)
    records = []

    for record in store.get("tickets", {}).values():
        if not isinstance(record, dict) or safe_int(record.get("user_id")) != user_id:
            continue

        opened_at = parse_datetime(record.get("opened_at"))
        if opened_at and opened_at >= window_start:
            records.append(record)

    return sorted(records, key=lambda record: record.get("opened_at") or "", reverse=True)


async def ticket_rate_limit_remaining(user_id: int) -> int:
    if TICKET_RATE_LIMIT_MAX <= 0 or TICKET_RATE_LIMIT_WINDOW_SECONDS <= 0 or TICKET_RATE_LIMIT_COOLDOWN_SECONDS <= 0:
        return 0

    async with ticket_store_lock:
        store = load_ticket_store()
        cooldowns = store.setdefault("cooldowns", {})
        user_key = str(user_id)
        now = utc_now()

        cooldown = cooldowns.get(user_key)
        if isinstance(cooldown, dict):
            cooldown_until = parse_datetime(cooldown.get("until"))
            if cooldown_until and cooldown_until > now:
                return math.ceil((cooldown_until - now).total_seconds())

        recent_records = recent_ticket_records_for_user(store, user_id, now=now)
        if len(recent_records) < TICKET_RATE_LIMIT_MAX:
            cooldowns.pop(user_key, None)
            save_json(TICKETS_PATH, store)
            return 0

        latest_opened_at = recent_records[0].get("opened_at")
        if isinstance(cooldown, dict) and cooldown.get("latest_opened_at") == latest_opened_at:
            return 0

        cooldown_until = now + timedelta(seconds=TICKET_RATE_LIMIT_COOLDOWN_SECONDS)
        cooldowns[user_key] = {
            "until": cooldown_until.isoformat(),
            "latest_opened_at": latest_opened_at,
            "triggered_at": now.isoformat(),
            "recent_ticket_count": len(recent_records),
        }
        save_json(TICKETS_PATH, store)
        return TICKET_RATE_LIMIT_COOLDOWN_SECONDS


def build_ticket_info_embed(record: dict, store: dict | None = None) -> discord.Embed:
    store = store or load_ticket_store()
    ticket_id = record.get("ticket_id", "?????")
    user_id = safe_int(record.get("user_id"))
    category = CATEGORY_LABELS.get(record.get("category"), str(record.get("category") or "desconhecida").title())
    status = normalize_ticket_status(record.get("status"))
    user_snapshot = record.get("user_last_snapshot") or record.get("user_open_snapshot") or {}
    if not isinstance(user_snapshot, dict):
        user_snapshot = {}
    thread_id = safe_int(record.get("thread_id"))
    source_channel_id = safe_int(record.get("source_channel_id"))
    subject = ticket_subject(record)

    embed = discord.Embed(
        title=f"Informações internas do ticket #{ticket_id}",
        color=discord.Color.blurple(),
    )

    user_lines = [
        f"Menção: <@{user_id}>",
        f"ID: `{user_id}`",
        f"Username: `{user_snapshot.get('username', 'desconhecido')}`",
        f"Nome exibido: `{user_snapshot.get('display_name', 'desconhecido')}`",
        f"Global name: `{user_snapshot.get('global_name') or 'nenhum'}`",
        f"Conta criada: {format_discord_time(user_snapshot.get('created_at'), style='F')}",
    ]

    server_lines = [
        f"Entrou no servidor: {format_discord_time(user_snapshot.get('joined_at'), style='F')}",
        f"Apelido no servidor: `{user_snapshot.get('nick') or 'nenhum'}`",
        f"Cargo mais alto: `{user_snapshot.get('top_role_name') or 'nenhum'}`",
        f"Qtd. cargos: `{len(user_snapshot.get('role_ids', []))}`",
    ]

    ticket_lines = [
        f"Assunto: **{subject}**",
        f"Categoria: **{category}**",
        f"Status: **{ticket_status_text(status)}**",
        f"Aberto em: {format_discord_time(record.get('opened_at'), style='F')}",
        f"Fechado em: {format_discord_time(record.get('closed_at'), style='F')}",
        f"Canal de origem: <#{source_channel_id}>",
        f"Thread: <#{thread_id}>",
    ]
    report = str(record.get("report") or "").strip()

    previous_records = get_user_ticket_records(user_id, store, exclude_thread_id=thread_id)
    previous_open = sum(1 for previous in previous_records if normalize_ticket_status(previous.get("status")) in ACTIVE_TICKET_STATUSES)
    previous_closed = sum(1 for previous in previous_records if normalize_ticket_status(previous.get("status")) == "closed")
    history_lines = [
        f"Tickets anteriores: `{len(previous_records)}`",
        f"Ativos: `{previous_open}` | Fechados: `{previous_closed}`",
    ]

    for previous in previous_records[:5]:
        previous_category = CATEGORY_LABELS.get(
            previous.get("category"),
            str(previous.get("category") or "desconhecida").title(),
        )
        previous_status = ticket_status_label(previous.get("status")).lower()
        history_lines.append(
            f"`#{previous.get('ticket_id', '?????')}` {previous_status} | {previous_category} | "
            f"{format_discord_time(previous.get('opened_at'), style='d')} | <#{previous.get('thread_id')}>"
        )

    if len(previous_records) > 5:
        history_lines.append(f"...e mais `{len(previous_records) - 5}` ticket(s).")

    embed.add_field(name="Usuário", value=limited_text("\n".join(user_lines)), inline=False)
    embed.add_field(name="Servidor", value=limited_text("\n".join(server_lines)), inline=False)
    embed.add_field(name="Ticket", value=limited_text("\n".join(ticket_lines)), inline=False)
    if report:
        embed.add_field(name="Relato", value=limited_text(report), inline=False)
    embed.add_field(name="Histórico", value=limited_text("\n".join(history_lines)), inline=False)

    avatar_url = user_snapshot.get("display_avatar_url")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    return embed


def build_user_ticket_history_embed(user: discord.abc.User, records: list[dict]) -> discord.Embed:
    active_count = sum(1 for record in records if normalize_ticket_status(record.get("status")) in ACTIVE_TICKET_STATUSES)
    closed_count = sum(1 for record in records if normalize_ticket_status(record.get("status")) == "closed")

    embed = discord.Embed(
        title="Meus tickets",
        description=f"Total: `{len(records)}` | Ativos: `{active_count}` | Fechados: `{closed_count}`",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Use /meustickets ticket_id:00001 para ver um ticket específico.")

    if not records:
        embed.description = "Você ainda não possui tickets registrados."
        return embed

    for record in records[:10]:
        ticket_id = ticket_id_from_record(record)
        category = CATEGORY_LABELS.get(record.get("category"), str(record.get("category") or "desconhecida").title())
        status = ticket_status_text(record.get("status"))
        thread_id = safe_int(record.get("thread_id"))
        report = str(record.get("report") or "").strip()
        subject = ticket_subject(record)
        field_title = f"#{ticket_id} | {limited_text(subject, 80)}"

        lines = [
            f"Status: **{status}**",
            f"Assunto: **{subject}**",
            f"Categoria: **{category}**",
            f"Aberto em: {format_discord_time(record.get('opened_at'), style='f')}",
            f"Fechado em: {format_discord_time(record.get('closed_at'), style='f')}",
        ]
        if report:
            lines.append(f"Relato: {limited_text(report, 220)}")
        if thread_id:
            lines.append(f"Thread: <#{thread_id}>")

        embed.add_field(
            name=field_title,
            value=limited_text("\n".join(lines), 1024),
            inline=False,
        )

    if len(records) > 10:
        embed.add_field(
            name="Mais tickets",
            value=f"Existem mais `{len(records) - 10}` ticket(s). Consulte pelo ID com `/meustickets ticket_id:00001`.",
            inline=False,
        )

    avatar_url = getattr(user, "display_avatar", None)
    if avatar_url:
        embed.set_thumbnail(url=str(user.display_avatar.url))

    return embed


def build_user_ticket_detail_embed(record: dict) -> discord.Embed:
    ticket_id = ticket_id_from_record(record)
    category = CATEGORY_LABELS.get(record.get("category"), str(record.get("category") or "desconhecida").title())
    status = ticket_status_text(record.get("status"))
    thread_id = safe_int(record.get("thread_id"))
    source_channel_id = safe_int(record.get("source_channel_id"))
    report = str(record.get("report") or "").strip()
    subject = ticket_subject(record)

    embed = discord.Embed(
        title=f"Ticket #{ticket_id} | {subject}",
        color=discord.Color.blurple(),
    )

    lines = [
        f"Status: **{status}**",
        f"Assunto: **{subject}**",
        f"Categoria: **{category}**",
        f"Aberto em: {format_discord_time(record.get('opened_at'), style='F')}",
        f"Em atendimento em: {format_discord_time(record.get('assigned_at'), style='F')}",
        f"Concluído em: {format_discord_time(record.get('completed_at'), style='F')}",
        f"Fechado em: {format_discord_time(record.get('closed_at'), style='F')}",
    ]

    if source_channel_id:
        lines.append(f"Canal de origem: <#{source_channel_id}>")
    if thread_id:
        lines.append(f"Thread: <#{thread_id}>")

    embed.description = limited_text("\n".join(lines), 4096)
    if report:
        embed.add_field(name="Relato", value=limited_text(report), inline=False)
    embed.set_footer(text="Threads fechadas ou arquivadas podem depender das permissões do Discord para abrir.")
    return embed


async def fetch_ticket_user(guild: discord.Guild, client: discord.Client, user_id: int) -> discord.abc.User | None:
    member = guild.get_member(user_id)
    if member:
        return member

    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.warning("Sem permissão para buscar membro %s no servidor %s.", user_id, guild.id)
    except discord.HTTPException as exc:
        logger.warning("Falha ao buscar membro %s no servidor %s: %s", user_id, guild.id, exc)

    try:
        return await client.fetch_user(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException as exc:
        logger.warning("Falha ao buscar usuário %s: %s", user_id, exc)
        return None


async def maybe_rename_legacy_ticket_thread(
    thread: discord.Thread,
    record: dict,
    user: discord.abc.User | None = None,
) -> None:
    if user is not None:
        new_name = build_ticket_name(str(record.get("ticket_id") or "?????").zfill(5), user, status=record.get("status"))
    else:
        new_name = build_ticket_name_from_record(record)

    if new_name == thread.name:
        return

    try:
        await thread.edit(name=new_name)
    except discord.Forbidden:
        logger.warning("Sem permissão para renomear ticket antigo %s.", thread.id)
    except discord.HTTPException as exc:
        logger.warning("Falha ao renomear ticket antigo %s: %s", thread.id, exc)


async def apply_ticket_thread_name(thread: discord.Thread, record: dict) -> None:
    await maybe_rename_legacy_ticket_thread(thread, record)


def legacy_thread_matches_user(thread: discord.Thread, user: discord.abc.User) -> bool:
    return thread.name.endswith(f"({user.id})") or thread.name.endswith(f"-{user.id}")


def thread_matches_open_ticket(thread: discord.Thread, user: discord.abc.User, store: dict) -> bool:
    record = get_ticket_record(thread.id, store)
    if record:
        status = normalize_ticket_status(record.get("status"))
        return status in ACTIVE_TICKET_STATUSES and safe_int(record.get("user_id")) == user.id

    return legacy_thread_matches_user(thread, user) and not thread.locked


async def find_open_ticket(channel: discord.TextChannel, user: discord.abc.User) -> discord.Thread | None:
    store = load_ticket_store()

    for thread in channel.threads:
        if thread_matches_open_ticket(thread, user, store):
            return thread

    async for thread in channel.archived_threads(private=True, limit=None):
        if thread_matches_open_ticket(thread, user, store):
            return thread

    return None


async def reserve_next_ticket_id() -> str:
    async with ticket_store_lock:
        store = load_ticket_store()
        next_id = int(store.get("last_ticket_id", 0)) + 1
        store["last_ticket_id"] = next_id
        save_json(TICKETS_PATH, store)

    return str(next_id).zfill(5)


async def upsert_ticket_record(
    thread: discord.Thread,
    user: discord.abc.User,
    *,
    ticket_id: str,
    category: str,
    source_channel_id: int,
    subject: str,
    report: str,
) -> None:
    async with ticket_store_lock:
        store = load_ticket_store()
        tickets = store.setdefault("tickets", {})
        existing = tickets.get(str(thread.id), {})
        user_snapshot = build_user_snapshot(user)
        now = utc_now_iso()
        existing.update(
            {
                "ticket_id": ticket_id,
                "thread_id": thread.id,
                "user_id": user.id,
                "source_channel_id": source_channel_id,
                "category": category,
                "subject": clean_subject(subject),
                "report": report.strip(),
                "status": "open",
                "opened_at": existing.get("opened_at") or now,
                "status_changed_at": now,
                "status_changed_by": user.id,
                "closed_at": None,
                "closed_by": None,
                "user_open_snapshot": existing.get("user_open_snapshot") or user_snapshot,
                "user_last_snapshot": user_snapshot,
                "user_last_seen_at": now,
            }
        )
        tickets[str(thread.id)] = existing
        save_json(TICKETS_PATH, store)


async def update_ticket_user_snapshot(thread: discord.Thread, user: discord.abc.User) -> None:
    async with ticket_store_lock:
        store = load_ticket_store()
        record = store.setdefault("tickets", {}).get(str(thread.id))

        if record:
            user_snapshot = build_user_snapshot(user)
            if not record.get("user_open_snapshot"):
                record["user_open_snapshot"] = user_snapshot
            record["user_last_snapshot"] = user_snapshot
            record["user_last_seen_at"] = utc_now_iso()
            save_json(TICKETS_PATH, store)


async def set_ticket_status_record(thread: discord.Thread, status: str, changed_by: discord.abc.User) -> dict | None:
    status = normalize_ticket_status(status)

    async with ticket_store_lock:
        store = load_ticket_store()
        record = store.setdefault("tickets", {}).get(str(thread.id))

        if record:
            now = utc_now_iso()
            changed_by_snapshot = build_user_snapshot(changed_by)

            record["status"] = status
            record["status_changed_at"] = now
            record["status_changed_by"] = changed_by.id
            record["status_changed_by_snapshot"] = changed_by_snapshot

            if status == "in_progress":
                record["assigned_at"] = record.get("assigned_at") or now
                record["assigned_to"] = record.get("assigned_to") or changed_by.id
                record["assigned_to_snapshot"] = record.get("assigned_to_snapshot") or changed_by_snapshot
            elif status == "completed":
                record["completed_at"] = now
                record["completed_by"] = changed_by.id
                record["completed_by_snapshot"] = changed_by_snapshot
            elif status == "closed":
                record["closed_at"] = now
                record["closed_by"] = changed_by.id
                record["closed_by_snapshot"] = changed_by_snapshot

            save_json(TICKETS_PATH, store)
            return deepcopy(record)

    return None


async def set_ticket_status(thread: discord.Thread, status: str, changed_by: discord.abc.User) -> dict | None:
    record = await set_ticket_status_record(thread, status, changed_by)
    if record:
        await apply_ticket_thread_name(thread, record)
    return record


async def close_ticket_record(thread: discord.Thread, closed_by: discord.abc.User) -> dict | None:
    return await set_ticket_status(thread, "closed", closed_by)


def validate_bot_permissions(channel: discord.TextChannel, member: discord.Member) -> list[str]:
    permissions = channel.permissions_for(member)
    missing = []

    required_permissions = {
        "view_channel": "Ver canal",
        "send_messages": "Enviar mensagens",
        "read_message_history": "Ler histórico de mensagens",
        "create_private_threads": "Criar threads privadas",
        "send_messages_in_threads": "Enviar mensagens em threads",
        "manage_threads": "Gerenciar threads",
    }

    for permission, label in required_permissions.items():
        if not getattr(permissions, permission):
            missing.append(label)

    return missing


def is_support_member(member: discord.Member, guild: discord.Guild) -> bool:
    support_role = guild.get_role(SUPPORT_ROLE_ID)
    has_support_role = support_role in member.roles if support_role else False
    return has_support_role or member.guild_permissions.manage_guild or member.guild_permissions.manage_threads


async def respond_with_user_tickets(interaction: discord.Interaction, ticket_id: str | None = None) -> None:
    if interaction.guild is None:
        await safe_reply(interaction, "Este recurso só pode ser usado dentro de um servidor.")
        return

    store = load_ticket_store()
    records = get_user_ticket_records(interaction.user.id, store)

    normalized_ticket_id = normalize_ticket_id_input(ticket_id)
    if normalized_ticket_id:
        record = next(
            (candidate for candidate in records if ticket_id_from_record(candidate) == normalized_ticket_id),
            None,
        )

        if not record:
            await safe_reply(interaction, f"Não encontrei um ticket `#{normalized_ticket_id}` no seu histórico.")
            return

        await safe_reply(interaction, embed=build_user_ticket_detail_embed(record))
        return

    await safe_reply(interaction, embed=build_user_ticket_history_embed(interaction.user, records))


# ================= DROPDOWN =================
class Dropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(value="atendimento", label="Atendimento", emoji="📨"),
            discord.SelectOption(value="denuncia", label="Denúncia", emoji="🚨"),
            discord.SelectOption(value="meus_tickets", label="Meus tickets", emoji="📁"),
        ]
        super().__init__(
            placeholder="Selecione uma opção...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="persistent_view:dropdown_help",
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "meus_tickets":
            await respond_with_user_tickets(interaction)
            return

        if self.values[0] not in CATEGORY_LABELS:
            await safe_reply(interaction, "Este painel está desatualizado. Peça para a equipe reenviar o painel com `/setup`.")
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await safe_reply(interaction, "Este painel precisa ser usado em um canal de texto configurado.")
            return

        if not is_configured_panel_channel(interaction.channel.id):
            await safe_reply(interaction, "Este canal não está configurado como painel de tickets.")
            return

        await interaction.response.send_modal(TicketReportModal(category=self.values[0]))


class DropdownView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Dropdown())


# ================= SETUP =================
class SetupModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Configurar tickets")
        config = load_config()
        configured_channels = ", ".join(str(channel_id) for channel_id in config.get("panel_channel_ids", []))

        self.channels_input = discord.ui.TextInput(
            label="Canal ou canais do painel",
            placeholder="#tickets ou IDs separados por vírgula",
            default=configured_channels,
            max_length=400,
            required=True,
        )
        self.title_input = discord.ui.TextInput(
            label="Título do painel",
            placeholder="Atendimento",
            default=config.get("panel_title", DEFAULT_CONFIG["panel_title"]),
            max_length=80,
            required=True,
        )
        self.description_input = discord.ui.TextInput(
            label="Descrição do painel",
            placeholder="Selecione uma opção abaixo para falar com a equipe.",
            default=config.get("panel_description", DEFAULT_CONFIG["panel_description"]),
            style=discord.TextStyle.paragraph,
            max_length=800,
            required=True,
        )

        self.add_item(self.channels_input)
        self.add_item(self.title_input)
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.guild.me is None:
            await safe_reply(interaction, "Não consegui identificar o servidor.")
            return

        channel_ids = parse_channel_ids(str(self.channels_input.value))
        if not channel_ids:
            await safe_reply(interaction, "Informe pelo menos um canal válido, como #tickets ou o ID do canal.")
            return

        valid_channels = []
        invalid_lines = []

        for channel_id in channel_ids:
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await interaction.guild.fetch_channel(channel_id)
                except discord.DiscordException:
                    channel = None

            if not isinstance(channel, discord.TextChannel):
                invalid_lines.append(f"`{channel_id}`: canal não encontrado ou não é um canal de texto.")
                continue

            missing = validate_bot_permissions(channel, interaction.guild.me)
            if missing:
                invalid_lines.append(f"{channel.mention}: faltam permissões: {', '.join(missing)}.")
                continue

            valid_channels.append(channel)

        if not valid_channels:
            message = "Nenhum canal foi configurado.\n" + "\n".join(invalid_lines[:8])
            await safe_reply(interaction, message[:1900])
            return

        config = load_config()
        config["panel_channel_ids"] = [channel.id for channel in valid_channels]
        config["panel_title"] = str(self.title_input.value).strip() or DEFAULT_CONFIG["panel_title"]
        config["panel_description"] = str(self.description_input.value).strip() or DEFAULT_CONFIG["panel_description"]
        save_json(CONFIG_PATH, config)

        sent_channels = []
        failed_lines = []
        embed = build_panel_embed(config)

        for channel in valid_channels:
            try:
                await channel.send(embed=embed, view=DropdownView())
                sent_channels.append(channel.mention)
            except discord.DiscordException as exc:
                logger.warning("Falha ao enviar painel em %s: %s", channel.id, exc)
                failed_lines.append(f"{channel.mention}: não consegui enviar o painel.")

        response_lines = [
            f"Configuração salva. Painel enviado em: {', '.join(sent_channels) if sent_channels else 'nenhum canal'}.",
        ]
        response_lines.extend(invalid_lines[:6])
        response_lines.extend(failed_lines[:6])

        await safe_reply(interaction, "\n".join(response_lines)[:1900])


# ================= TICKET =================
async def create_ticket_from_report(interaction: discord.Interaction, *, category: str, subject: str, report: str) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await safe_reply(interaction, "Este botão precisa ser usado em um canal de texto configurado.")
        return

    if not is_configured_panel_channel(interaction.channel.id):
        await safe_reply(interaction, "Este canal não está configurado como painel de tickets.")
        return

    subject = clean_subject(subject)
    report = report.strip()
    if not subject:
        await safe_reply(interaction, "O assunto não pode ficar vazio.")
        return

    if not report:
        await safe_reply(interaction, "O relato não pode ficar vazio.")
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    existing_ticket = await find_open_ticket(interaction.channel, interaction.user)
    if existing_ticket:
        try:
            if existing_ticket.archived:
                await existing_ticket.edit(archived=False, locked=False, auto_archive_duration=10080, invitable=False)

            await existing_ticket.add_user(interaction.user)
            await update_ticket_user_snapshot(existing_ticket, interaction.user)
        except discord.Forbidden:
            await safe_reply(interaction, "Erro: não tenho permissão para reabrir seu ticket.")
            return
        except discord.HTTPException as exc:
            logger.warning("Falha ao reabrir ticket %s: %s", existing_ticket.id, exc)
            await safe_reply(interaction, "Não consegui reabrir seu ticket agora.")
            return

        await safe_reply(interaction, f"Você já tem um atendimento em andamento: {existing_ticket.mention}")
        return

    remaining_seconds = await ticket_rate_limit_remaining(interaction.user.id)
    if remaining_seconds:
        await safe_reply(
            interaction,
            "Você abriu muitos tickets recentemente. "
            f"Aguarde {humanize_seconds(remaining_seconds)} para abrir outro.",
        )
        return

    ticket = None
    try:
        ticket_id = await reserve_next_ticket_id()
        ticket = await interaction.channel.create_thread(
            name=build_ticket_name(ticket_id, interaction.user, status="open"),
            auto_archive_duration=10080,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        await ticket.add_user(interaction.user)
        await upsert_ticket_record(
            ticket,
            interaction.user,
            ticket_id=ticket_id,
            category=category,
            source_channel_id=interaction.channel.id,
            subject=subject,
            report=report,
        )
    except discord.Forbidden:
        if ticket is not None:
            try:
                await ticket.edit(archived=True, locked=True)
            except discord.DiscordException:
                logger.warning("Não consegui arquivar ticket órfão %s.", ticket.id)
        await safe_reply(interaction, "Erro: não tenho permissão para criar ou gerenciar o ticket.")
        return
    except discord.HTTPException as exc:
        logger.warning("Falha ao criar ticket: %s", exc)
        if ticket is not None:
            try:
                await ticket.edit(archived=True, locked=True)
            except discord.DiscordException:
                logger.warning("Não consegui arquivar ticket órfão %s.", ticket.id)
        await safe_reply(interaction, "Não consegui criar o ticket agora. Tente novamente em alguns instantes.")
        return

    category_label = CATEGORY_LABELS.get(category, category.title())
    quoted_report = "\n".join(f"> {line}" if line else ">" for line in limited_text(report, 1200).splitlines())
    await safe_reply(interaction, f"Criei um ticket para você: {ticket.mention}")
    await ticket.send(
        f"📩 **|** {interaction.user.mention} ticket `#{ticket_id}` criado.\n\n"
        f"Assunto: **{subject}**\n"
        f"Categoria: **{category_label}**\n"
        f"Relato:\n{quoted_report}\n\n"
        "Aguarde um atendente.\n"
        "Use `/fecharticket` para encerrar."
    )


class TicketReportModal(discord.ui.Modal):
    def __init__(self, *, category: str):
        super().__init__(title="Abrir ticket")
        self.category = category
        category_label = CATEGORY_LABELS.get(category, category.title())

        self.subject_input = discord.ui.TextInput(
            label=f"Assunto - {category_label}",
            placeholder="Resumo curto do problema",
            min_length=3,
            max_length=90,
            required=True,
        )
        self.report_input = discord.ui.TextInput(
            label=f"Relato - {category_label}",
            placeholder="Descreva o que aconteceu e inclua detalhes importantes.",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=1800,
            required=True,
        )
        self.add_item(self.subject_input)
        self.add_item(self.report_input)

    async def on_submit(self, interaction: discord.Interaction):
        await create_ticket_from_report(
            interaction,
            category=self.category,
            subject=str(self.subject_input.value),
            report=str(self.report_input.value),
        )


# ================= CLIENT =================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True


class TicketClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.synced = False

    async def setup_hook(self):
        self.add_view(DropdownView())

    async def on_ready(self):
        if not self.synced:
            synced_commands = await tree.sync(guild=discord.Object(id=GUILD_ID))
            self.synced = True
            logger.info("Sincronizados %s comandos no servidor %s.", len(synced_commands), GUILD_ID)

        logger.info("Entramos como %s.", self.user)


ticket = TicketClient()
tree = app_commands.CommandTree(ticket)


# ================= COMANDOS =================
@tree.command(guild=discord.Object(id=GUILD_ID), name="setup", description="Configurar e enviar o painel de tickets")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    await interaction.response.send_modal(SetupModal())


async def set_ticket_status_from_command(interaction: discord.Interaction, status: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await safe_reply(interaction, "Este comando só pode ser usado dentro de um servidor.")
        return

    if not isinstance(interaction.channel, discord.Thread):
        await safe_reply(interaction, "Este comando só pode ser usado dentro de um ticket.")
        return

    if not is_support_member(interaction.user, interaction.guild):
        await safe_reply(interaction, "Apenas a equipe de atendimento pode alterar o status do ticket.")
        return

    record = get_ticket_record(interaction.channel.id)
    if not record:
        await safe_reply(interaction, "Não encontrei registro interno para este ticket.")
        return

    if normalize_ticket_status(record.get("status")) == "closed":
        await safe_reply(interaction, "Este ticket já está fechado.")
        return

    updated_record = await set_ticket_status(interaction.channel, status, interaction.user)
    if not updated_record:
        await safe_reply(interaction, "Não consegui atualizar o status deste ticket.")
        return

    await safe_reply(
        interaction,
        f"Status atualizado para **{ticket_status_text(status)}** por {interaction.user.mention}.",
        ephemeral=False,
    )


@tree.command(guild=discord.Object(id=GUILD_ID), name="atenderticket", description="Marcar ticket como em atendimento")
async def atenderticket(interaction: discord.Interaction):
    await set_ticket_status_from_command(interaction, "in_progress")


@tree.command(guild=discord.Object(id=GUILD_ID), name="concluirticket", description="Marcar ticket como concluído")
async def concluirticket(interaction: discord.Interaction):
    await set_ticket_status_from_command(interaction, "completed")


@tree.command(guild=discord.Object(id=GUILD_ID), name="fecharticket", description="Fechar atendimento")
async def fecharticket(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await safe_reply(interaction, "Este comando só pode ser usado dentro de um servidor.")
        return

    if not isinstance(interaction.channel, discord.Thread):
        await safe_reply(interaction, "Este comando só pode ser usado dentro de um ticket.")
        return

    record = get_ticket_record(interaction.channel.id)
    is_ticket_owner = False

    if record:
        is_ticket_owner = safe_int(record.get("user_id")) == interaction.user.id
    else:
        is_ticket_owner = legacy_thread_matches_user(interaction.channel, interaction.user)

    is_support = is_support_member(interaction.user, interaction.guild)

    if not is_ticket_owner and not is_support:
        await safe_reply(interaction, "Você não pode fechar este ticket.")
        return

    if record:
        await close_ticket_record(interaction.channel, interaction.user)

    await safe_reply(interaction, f"Fechando ticket por {interaction.user.mention}...", ephemeral=False)

    try:
        await interaction.channel.edit(archived=True, locked=True)
    except discord.Forbidden:
        await safe_reply(interaction, "Não tenho permissão para arquivar ou bloquear este ticket.")
        return
    except discord.HTTPException as exc:
        logger.warning("Falha ao fechar ticket %s: %s", interaction.channel.id, exc)
        await safe_reply(interaction, "Não consegui arquivar o ticket agora.")
        return


@tree.command(guild=discord.Object(id=GUILD_ID), name="meustickets", description="Consultar seu histórico de tickets")
@app_commands.describe(ticket_id="Opcional: ID do ticket, exemplo: 00001")
async def meustickets(interaction: discord.Interaction, ticket_id: str | None = None):
    await respond_with_user_tickets(interaction, ticket_id)


@tree.command(guild=discord.Object(id=GUILD_ID), name="ticketinfo", description="Ver informações internas do ticket")
async def ticketinfo(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await safe_reply(interaction, "Este comando só pode ser usado dentro de um servidor.")
        return

    if not isinstance(interaction.channel, discord.Thread):
        await safe_reply(interaction, "Este comando só pode ser usado dentro de um ticket.")
        return

    if not is_support_member(interaction.user, interaction.guild):
        await safe_reply(interaction, "Apenas a equipe de atendimento pode ver as informações internas do ticket.")
        return

    record = get_ticket_record(interaction.channel.id)
    if not record:
        await safe_reply(
            interaction,
            "Não encontrei registro interno para este ticket. Isso pode acontecer com tickets criados antes desta versão.",
        )
        return

    record_user_id = safe_int(record.get("user_id"))
    if record_user_id:
        ticket_user = await fetch_ticket_user(interaction.guild, interaction.client, record_user_id)
        if ticket_user:
            await update_ticket_user_snapshot(interaction.channel, ticket_user)
            record = get_ticket_record(interaction.channel.id) or record
            await maybe_rename_legacy_ticket_thread(interaction.channel, record, ticket_user)
        else:
            await maybe_rename_legacy_ticket_thread(interaction.channel, record)

    await safe_reply(interaction, embed=build_ticket_info_embed(record))


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await safe_reply(interaction, "Você não tem permissão para usar este comando.")
        return

    logger.exception("Erro em comando slash.", exc_info=error)
    await safe_reply(interaction, "Ocorreu um erro inesperado ao executar este comando.")


# ================= RUN =================
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN não foi definido no ambiente.")

try:
    ticket.run(TOKEN, reconnect=False, log_handler=None)
except aiohttp.ClientConnectorDNSError as exc:
    logger.error(
        "Não consegui resolver discord.com no DNS. Verifique internet, DNS, proxy/VPN ou firewall. Erro original: %s",
        exc,
    )
except aiohttp.ClientConnectorError as exc:
    logger.error(
        "Não consegui conectar ao Discord. Verifique internet, proxy/VPN ou firewall. Erro original: %s",
        exc,
    )
except (discord.GatewayNotFound, discord.ConnectionClosed, asyncio.TimeoutError, OSError) as exc:
    logger.error(
        "Não consegui conectar ao Gateway do Discord. Verifique internet, DNS, proxy/VPN, firewall ou instabilidade do Discord. Erro original: %s",
        exc,
    )
except AttributeError as exc:
    if "'NoneType' object has no attribute 'sequence'" not in str(exc):
        raise

    logger.error(
        "A conexão com o Gateway falhou antes do websocket ficar pronto. Isso acionou um erro interno do discord.py. "
        "Verifique internet, DNS, proxy/VPN ou firewall e tente iniciar o bot novamente."
    )
