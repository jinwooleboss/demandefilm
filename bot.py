#!/usr/bin/env python3
import os
import re
import asyncio
import sqlite3
import logging
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime

import httpx
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
TMDB_KEY = os.getenv("TMDB_API_KEY", "").strip()
API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION = os.getenv("TELETHON_SESSION", os.path.expanduser("~/demandefilm/session"))
LOCAL_API = os.getenv("LOCAL_BOT_API", "1") == "1"
DB_PATH = os.path.expanduser("~/demandefilm/demandes.db")

def ids_env(name):
    result = []
    for x in os.getenv(name, "").split(","):
        x = x.strip()
        if x:
            try:
                result.append(int(x))
            except ValueError:
                pass
    return result

ADMINS = set(ids_env("ADMIN_IDS"))
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
except ValueError:
    OWNER_ID = 0
ARCHIVES = ids_env("ARCHIVE_CHANNELS")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("gestion_demandes")

def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            title TEXT NOT NULL,
            original_title TEXT,
            media_type TEXT,
            year TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'En attente',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS archive_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            normalized TEXT NOT NULL,
            UNIQUE(channel_id, message_id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS request_users (
            user_id INTEGER NOT NULL,
            request_id INTEGER NOT NULL,
            UNIQUE(user_id, request_id)
        )""")


    with connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(archive_index)")}
        if "is_photo" not in columns:
            con.execute(
                "ALTER TABLE archive_index "
                "ADD COLUMN is_photo INTEGER NOT NULL DEFAULT 0"
            )


def init_admin_db():
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id INTEGER PRIMARY KEY,
                can_stats INTEGER NOT NULL DEFAULT 0,
                can_manage_admins INTEGER NOT NULL DEFAULT 0,
                can_configure INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        migrated = con.execute(
            "SELECT value FROM settings WHERE key='admin_db_migrated'"
        ).fetchone()
        if not migrated:
            for uid in ADMINS:
                if uid > 0 and uid != OWNER_ID:
                    con.execute(
                        "INSERT OR IGNORE INTO bot_admins(user_id,created_at) VALUES(?,?)",
                        (uid, datetime.now().isoformat(timespec="seconds"))
                    )
            con.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('admin_db_migrated','1')"
            )


def is_admin(user_id):
    if not user_id:
        return False
    if OWNER_ID > 0 and int(user_id) == OWNER_ID:
        return True
    with connect() as con:
        return con.execute(
            "SELECT 1 FROM bot_admins WHERE user_id=?", (int(user_id),)
        ).fetchone() is not None


def has_permission(user_id, permission):
    if not user_id:
        return False
    if OWNER_ID > 0 and int(user_id) == OWNER_ID:
        return True
    columns = {
        "stats": "can_stats",
        "manage_admins": "can_manage_admins",
        "configure": "can_configure",
    }
    column = columns.get(permission)
    if not column:
        return False
    with connect() as con:
        row = con.execute(
            f"SELECT {column} FROM bot_admins WHERE user_id=?",
            (int(user_id),)
        ).fetchone()
    return bool(row and row[column])


def admin_ids():
    result = {OWNER_ID} if OWNER_ID > 0 else set()
    with connect() as con:
        result.update(
            row[0] for row in con.execute("SELECT user_id FROM bot_admins")
        )
    return result


def normalize(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\b(1080p|720p|480p|2160p|4k|french|vostfr|vf|vost|multi)\b", " ", s)
    s = re.sub(r"\b(saison|season|episode|ep)\s*\d+\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def add_archive(channel_id, message_id, text, is_photo=False):
    text = (text or "").strip()
    if not text or not is_photo:
        return
    with connect() as con:
        con.execute("""
        INSERT INTO archive_index(
            channel_id, message_id, text, normalized, is_photo
        ) VALUES(?,?,?,?,1)
        ON CONFLICT(channel_id,message_id) DO UPDATE SET
        text=excluded.text,
        normalized=excluded.normalized,
        is_photo=1
        """, (channel_id, message_id, text[:10000], normalize(text)))

def archive_matches(title, limit=5):
    target = normalize(title)
    if not target:
        return []
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM archive_index WHERE is_photo=1 ORDER BY id DESC"
        ).fetchall()

    matches = []
    for row in rows:
        text = row["normalized"]
        # Priorité aux titres présents dans la légende.
        if target in text:
            score = 1.0
        else:
            # Comparaison avec les segments de la légende.
            words = target.split()
            if not words:
                continue
            score = SequenceMatcher(None, target, text[:300]).ratio()
            if len(words) >= 2 and all(w in text for w in words):
                score = max(score, 0.94)
        if score >= 0.86:
            matches.append((score, row))

    matches.sort(key=lambda x: x[0], reverse=True)
    # Une seule copie par publication, au maximum.
    return [r for _, r in matches[:limit]]

async def search_imdb(query):
    url = "https://v2.sg.media-imdb.com/suggestion/x/" + httpx.URL(query).raw_path.decode(errors="ignore") + ".json"
    # Encodage sûr du titre.
    from urllib.parse import quote
    url = "https://v2.sg.media-imdb.com/suggestion/x/" + quote(query, safe="") + ".json"
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

    results = []
    for item in data.get("d", [])[:8]:
        kind = item.get("q", "")
        if kind not in ("feature", "TV series", "TV mini series", "TV movie"):
            continue
        results.append({
            "title": item.get("l", query),
            "original": item.get("l", query),
            "year": str(item.get("y", "")),
            "type": "Série" if "TV" in kind else "Film",
            "source": "IMDb",
            "id": item.get("id", "")
        })
    return results

async def search_tmdb(query):
    if not TMDB_KEY:
        return []
    url = "https://api.themoviedb.org/3/search/multi"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params={
            "api_key": TMDB_KEY,
            "query": query,
            "language": "fr-FR",
            "include_adult": "false"
        })
        r.raise_for_status()
        data = r.json()

    results = []
    for item in data.get("results", []):
        kind = item.get("media_type")
        if kind not in ("movie", "tv"):
            continue
        title = item.get("title") or item.get("name") or query
        original = item.get("original_title") or item.get("original_name") or title
        date = item.get("release_date") or item.get("first_air_date") or ""
        results.append({
            "title": title,
            "original": original,
            "year": date[:4],
            "type": "Série" if kind == "tv" else "Film",
            "source": "TMDb",
            "id": str(item.get("id", "")),
            "overview": item.get("overview", "")
        })
        if len(results) >= 8:
            break
    return results


async def search_allocine(query):
    """Recherche de secours via l'autocomplétion JSON d'AlloCiné."""
    from urllib.parse import quote
    from difflib import SequenceMatcher

    url = "https://www.allocine.fr/_/autocomplete/" + quote(query, safe="")

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

    if data.get("error"):
        return []

    wanted = normalize(query)
    results = []

    for item in data.get("results", []):
        entity_type = str(item.get("entity_type", "")).lower()
        if entity_type not in ("movie", "series", "tvseries", "tv_series"):
            continue

        title = str(item.get("label") or item.get("original_label") or "").strip()
        if not title:
            continue

        original = str(item.get("original_label") or title).strip()
        aliases = item.get("text_search_data") or []
        candidates = [title, original] + [
            str(alias) for alias in aliases if alias
        ]

        # Évite les titres sans rapport avec la recherche.
        similarity = max(
            SequenceMatcher(None, wanted, normalize(candidate)).ratio()
            for candidate in candidates
        )
        if wanted and similarity < 0.45:
            continue

        details = item.get("data") or {}
        year = str(details.get("year") or "")
        is_series = "series" in entity_type

        results.append({
            "title": title,
            "original": original,
            "year": year,
            "type": "Série" if is_series else "Film",
            "source": "AlloCiné",
            "id": str(item.get("entity_id") or details.get("id") or ""),
            "overview": ""
        })

        if len(results) >= 8:
            break

    return results

async def search_titles(query):
    results = []
    errors = []

    # IMDb et TMDb sont recherchés en parallèle.
    responses = await asyncio.gather(
        search_imdb(query),
        search_tmdb(query),
        return_exceptions=True
    )

    imdb_response = responses[0]
    tmdb_response = responses[1]

    if isinstance(imdb_response, Exception):
        log.warning("Erreur IMDb : %s", imdb_response)
        errors.append(imdb_response)
        imdb_results = []
    else:
        imdb_results = imdb_response or []

    if isinstance(tmdb_response, Exception):
        log.warning("Erreur TMDb : %s", tmdb_response)
        errors.append(tmdb_response)
        tmdb_results = []
    else:
        tmdb_results = tmdb_response or []

    results.extend(imdb_results)
    results.extend(tmdb_results)

    # AlloCiné prend le relais uniquement si IMDb ne renvoie rien.
    if not imdb_results:
        try:
            allocine_results = await search_allocine(query)
            if allocine_results:
                log.info(
                    "AlloCiné : %s résultat(s) pour %r",
                    len(allocine_results), query
                )
                results.extend(allocine_results)
            else:
                log.info("Aucun résultat AlloCiné pour %r", query)
        except Exception as exc:
            log.warning("Erreur AlloCiné : %s", exc)
            errors.append(exc)

    unique = []
    seen = set()

    for item in results:
        key = (
            normalize(item.get("title", "")),
            item.get("year", ""),
            item.get("type", "")
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique[:10], bool(errors)

def build_menu(user_id):
    rows = [
        [KeyboardButton("🎬 Nouvelle demande")],
        [KeyboardButton("📋 Mes demandes"), KeyboardButton("ℹ️ Aide")]
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton("📥 Demandes en attente")])
    if has_permission(user_id, "stats"):
        rows.append([KeyboardButton("📊 Statistiques")])
    if has_permission(user_id, "manage_admins"):
        rows.append([KeyboardButton("👥 Administrateurs")])
    if has_permission(user_id, "configure"):
        rows.append([KeyboardButton("⚙️ Configuration")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not user or not chat or not message:
        return

    # Dans les groupes, seul le propriétaire ou un administrateur du bot répond.
    if chat.type in ("group", "supergroup") and not is_admin(user.id):
        return

    # Enregistrement des utilisateurs qui démarrent le bot en privé.
    if chat.type == "private":
        with connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS private_users (
                    user_id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL
                )
            """)
            con.execute(
                "INSERT OR IGNORE INTO private_users(user_id, started_at) VALUES(?, ?)",
                (user.id, datetime.now().isoformat(timespec="seconds"))
            )

    text = (
        "🎬 Bienvenue dans le bot de demandes de films et séries !\\n\\n"
        "Envoie simplement le titre du film ou de la série : "
        "je rechercherai le contenu sur IMDb, puis vérifierai les fiches photo "
        "de nos archives Telegram."
    )
    rows = [
        [InlineKeyboardButton("🎬 Nouvelle demande", callback_data="menu:new")],
        [
            InlineKeyboardButton("📋 Mes demandes", callback_data="menu:requests"),
            InlineKeyboardButton("ℹ️ Aide", callback_data="menu:help"),
        ],
    ]
    if is_admin(user.id):
        rows.append([InlineKeyboardButton("📥 Demandes en attente", callback_data="menu:pending")])
    if has_permission(user.id, "stats"):
        rows.append([InlineKeyboardButton("📊 Statistiques", callback_data="menu:stats")])
    if has_permission(user.id, "manage_admins"):
        rows.append([InlineKeyboardButton("👥 Administrateurs", callback_data="menu:admins")])
    if has_permission(user.id, "configure"):
        rows.append([InlineKeyboardButton("⚙️ Configuration", callback_data="menu:config")])

    await message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(rows)
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


def menu_keyboard(user_id):
    rows = [
        [InlineKeyboardButton("🎬 Nouvelle demande", callback_data="menu:new")],
        [
            InlineKeyboardButton("📋 Mes demandes", callback_data="menu:requests"),
            InlineKeyboardButton("ℹ️ Aide", callback_data="menu:help"),
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("📥 Demandes en attente", callback_data="menu:pending")])
    if has_permission(user_id, "stats"):
        rows.append([InlineKeyboardButton("📊 Statistiques", callback_data="menu:stats")])
    if has_permission(user_id, "manage_admins"):
        rows.append([InlineKeyboardButton("👥 Administrateurs", callback_data="menu:admins")])
    if has_permission(user_id, "configure"):
        rows.append([InlineKeyboardButton("⚙️ Configuration", callback_data="menu:config")])
    return InlineKeyboardMarkup(rows)


def back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Retour au menu", callback_data="menu:home")
    ]])


async def menu_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    action = q.data

    if action == "menu:home":
        text = (
            "🎬 Bienvenue dans le bot de demandes de films et séries !\n\n"
            "Envoie simplement le titre du film ou de la série : "
            "je rechercherai le contenu sur IMDb, puis vérifierai les fiches photo "
            "de nos archives Telegram."
        )
        await q.edit_message_text(text, reply_markup=menu_keyboard(uid))

    elif action == "menu:new":
        context.user_data.pop("search_results", None)
        context.user_data.pop("search_query", None)
        await q.edit_message_text(
            "🎬 **Nouvelle demande**\n\nEnvoie le titre du film ou de la série.",
            reply_markup=back_keyboard(),
            parse_mode="Markdown"
        )

    elif action == "menu:requests":
        with connect() as con:
            rows = con.execute(
                "SELECT id,title,status,created_at FROM requests "
                "WHERE user_id=? ORDER BY id DESC LIMIT 15",
                (uid,)
            ).fetchall()
        if rows:
            lines = ["📋 Tes demandes :"]
            lines.extend(f'#{r["id"]} — {r["title"]} — {r["status"]}' for r in rows)
            text = "\n".join(lines)
        else:
            text = "📋 Tu n'as pas encore de demande."
        await q.edit_message_text(text, reply_markup=back_keyboard())

    elif action == "menu:help":
        await q.edit_message_text(
            "ℹ️ Aide\n\n"
            "1. Appuie sur 🎬 Nouvelle demande.\n"
            "2. Envoie le titre du film ou de la série.\n"
            "3. Choisis le bon résultat IMDb.\n"
            "4. Le bot vérifie les fiches photo des archives.\n\n"
            "/annuler : annuler une recherche.",
            reply_markup=back_keyboard()
        )

    elif action == "menu:pending":
        if not is_admin(uid):
            await q.answer("Accès réservé aux administrateurs.", show_alert=True)
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 Films", callback_data="menu:pending:film"),
                InlineKeyboardButton("📺 Séries", callback_data="menu:pending:series"),
            ],
            [InlineKeyboardButton("⬅️ Retour au menu", callback_data="menu:home")],
        ])
        await q.edit_message_text(
            "📥 Demandes en attente\n\nChoisis une catégorie :",
            reply_markup=keyboard,
        )

    elif action in ("menu:pending:film", "menu:pending:series"):
        if not is_admin(uid):
            await q.answer("Accès réservé aux administrateurs.", show_alert=True)
            return

        media_type = "Film" if action.endswith(":film") else "Série"
        category = "film" if media_type == "Film" else "series"

        with connect() as con:
            rows = con.execute(
                "SELECT id,title,year,media_type,user_id,status FROM requests "
                "WHERE media_type=? AND status IN ('En attente','En cours') "
                "ORDER BY id DESC LIMIT 30",
                (media_type,),
            ).fetchall()

        buttons = []
        for r in rows:
            title = str(r["title"])
            if len(title) > 38:
                title = title[:35] + "..."
            buttons.append([
                InlineKeyboardButton(
                    f'#{r["id"]} — {title} [{r["status"]}]',
                    callback_data=f'menu:req:{r["id"]}:{category}',
                )
            ])

        buttons.append([
            InlineKeyboardButton("⬅️ Catégories", callback_data="menu:pending"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu:home"),
        ])

        label = "Films" if media_type == "Film" else "Séries"
        if rows:
            text = f"📥 Demandes en attente — {label}\n\nSélectionne une demande."
        else:
            text = f"📥 Aucune demande en attente dans la catégorie {label}."

        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif action.startswith("menu:req:"):
        if not is_admin(uid):
            await q.answer("Accès réservé aux administrateurs.", show_alert=True)
            return

        parts = action.split(":")
        if len(parts) != 4 or not parts[2].isdigit():
            await q.answer("Demande invalide.", show_alert=True)
            return

        request_id = int(parts[2])
        category = parts[3]
        if category not in ("film", "series"):
            await q.answer("Catégorie invalide.", show_alert=True)
            return

        with connect() as con:
            r = con.execute(
                "SELECT id,title,year,media_type,user_id,status "
                "FROM requests WHERE id=?",
                (request_id,),
            ).fetchone()

        if not r:
            await q.edit_message_text(
                "Demande introuvable.",
                reply_markup=back_keyboard(),
            )
            return

        expected_type = "Film" if category == "film" else "Série"
        if r["media_type"] != expected_type:
            await q.answer(
                "Cette demande ne correspond pas à cette catégorie.",
                show_alert=True,
            )
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⏳ En cours",
                    callback_data=f'status:{request_id}:En cours',
                ),
                InlineKeyboardButton(
                    "✅ Publié",
                    callback_data=f'status:{request_id}:Publié',
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Refuser",
                    callback_data=f'status:{request_id}:Refusée',
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Retour aux demandes",
                    callback_data=f"menu:pending:{category}",
                ),
                InlineKeyboardButton("🏠 Menu", callback_data="menu:home"),
            ],
        ])

        await q.edit_message_text(
            f"📄 Demande #{r['id']}\n"
            f"🎬 Titre : {r['title']} ({r['year'] or '?'})\n"
            f"Type : {r['media_type']}\n"
            f"Utilisateur : {r['user_id']}\n"
            f"Statut actuel : {r['status']}\n\n"
            "Choisis une action :",
            reply_markup=keyboard,
        )

    elif action == "menu:stats":
        if not has_permission(uid, "stats"):
            await q.answer("Permission refusée.", show_alert=True)
            return
        with connect() as con:
            reqs = con.execute(
                "SELECT status,COUNT(*) n FROM requests GROUP BY status"
            ).fetchall()
            archive_count = con.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        lines = ["📊 Statistiques"]
        lines.extend(f'{r["status"]} : {r["n"]}' for r in reqs)
        lines.append(f"Publications indexées : {archive_count}")
        await q.edit_message_text("\n".join(lines), reply_markup=back_keyboard())

    elif action == "menu:admins":
        if not has_permission(uid, "manage_admins"):
            await q.answer("Permission refusée.", show_alert=True)
            return
        with connect() as con:
            rows = con.execute(
                "SELECT user_id,can_stats,can_manage_admins,can_configure "
                "FROM bot_admins ORDER BY user_id"
            ).fetchall()
        lines = ["👥 Administrateurs"]
        if OWNER_ID > 0:
            lines.append(f"👑 Créateur : {OWNER_ID} (tous les droits)")
        for r in rows:
            rights = []
            if r["can_stats"]:
                rights.append("statistiques")
            if r["can_manage_admins"]:
                rights.append("gestion admins")
            if r["can_configure"]:
                rights.append("configuration")
            lines.append(
                f'{r["user_id"]} — {", ".join(rights) if rights else "aucun droit supplémentaire"}'
            )
        lines.extend(["", "Ajouter : /admin_ajouter ID", "Supprimer : /admin_supprimer ID"])
        if uid == OWNER_ID:
            lines.append("Modifier les droits : /droits ID")
        await q.edit_message_text("\n".join(lines), reply_markup=back_keyboard())

    elif action == "menu:config":
        if not has_permission(uid, "configure"):
            await q.answer("Permission refusée.", show_alert=True)
            return
        text = (
            "⚙️ Configuration\n\n"
            f"Archives actuelles : {', '.join(map(str, ARCHIVES)) or 'aucune'}\n\n"
            "Pour modifier les archives, utilise :\n"
            "/config_archives -1001234567890 -1009876543210"
        )
        await q.edit_message_text(text, reply_markup=back_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "ℹ️ Aide\n\n"
        "1. Appuie sur 🎬 Nouvelle demande.\n"
        "2. Envoie le titre du film ou de la série.\n"
        "3. Choisis le bon résultat IMDb.\n"
        "4. Le bot vérifie les fiches photo des archives.\n\n"
        "📋 Mes demandes : consulter tes demandes.\n"
        "/annuler : annuler une recherche.\n"
        "/menu : afficher le menu."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("search_results", None)
    context.user_data.pop("search_query", None)
    await update.message.reply_text("Recherche annulée.")


async def menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    choice = update.message.text.strip()
    uid = update.effective_user.id

    if choice == "🎬 Nouvelle demande":
        await update.message.reply_text("🎬 Envoie maintenant le titre du film ou de la série.")
    elif choice == "📋 Mes demandes":
        await my_requests(update, context)
    elif choice == "ℹ️ Aide":
        await help_command(update, context)
    elif choice == "📥 Demandes en attente":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Accès refusé.")
        else:
            await admin_requests(update, context)
    elif choice == "📊 Statistiques":
        await stats(update, context)
    elif choice == "👥 Administrateurs":
        await admin_management(update, context)
    elif choice == "⚙️ Configuration":
        await configuration(update, context)


async def handle_group_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Empêche l'enregistrement direct des titres saisis dans les groupes."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not message.text or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        return

    # Ignorer les messages envoyés au nom d'un canal et les transferts.
    if getattr(message, "sender_chat", None) is not None:
        return
    if getattr(message, "is_automatic_forward", False):
        return
    forward_origin = getattr(message, "forward_origin", None)
    if getattr(forward_origin, "type", None) == "channel":
        return

    query = message.text.strip()
    if not query or query.startswith("/"):
        return
    if len(query) < 2 or len(query) > 150:
        return

    # L'utilisateur doit d'abord démarrer le bot en privé.
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS private_users (
                user_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL
            )
        """)
        started = con.execute(
            "SELECT 1 FROM private_users WHERE user_id=?",
            (user.id,)
        ).fetchone()

    if not started:
        await message.reply_text(
            "Démarre d'abord le bot en privé avec /start."
        )
        return

    # Une seule demande active par utilisateur.
    existing = find_open_request(user.id)
    if existing:
        await message.reply_text(
            f"Tu as déjà une demande active : "
            f"#{existing['id']} — {existing['title']}."
        )
        return

    # Ne pas enregistrer un texte brut depuis le groupe.
    await message.reply_text(
        "Pour faire une demande, écris-moi en privé : "
        "ouvre le bot, appuie sur 🎬 Nouvelle demande, "
        "puis envoie le titre et sélectionne le bon résultat."
    )


async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Les demandes de films/séries sont acceptées uniquement en privé.
    # Ignore les messages des canaux et des groupes.
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    query = update.message.text.strip()
    if query.startswith("/"):
        return
    if len(query) < 2 or len(query) > 150:
        await update.message.reply_text("Envoie un titre entre 2 et 150 caractères.")
        return

    await update.message.reply_text("🔎 Recherche sur IMDb et TMDb…")
    results, had_error = await search_titles(query)

    if not results:
        msg = "Je n'ai trouvé aucun résultat exploitable. Vérifie l'orthographe et réessaie."
        if had_error:
            msg += "\nUne source de recherche est peut-être temporairement indisponible."
        await update.message.reply_text(msg)
        return

    context.user_data["search_results"] = results
    context.user_data["search_query"] = query

    buttons = []
    for i, item in enumerate(results):
        label = f'{item["title"]} ({item["year"] or "année inconnue"}) · {item["source"]}'
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"pick:{i}")])
    buttons.append([InlineKeyboardButton("❌ Annuler", callback_data="cancel_search")])

    await update.message.reply_text(
        "Choisis le titre correspondant à ta demande :",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def create_request(user, item, status="En attente"):
    """Crée une demande seulement si l'utilisateur n'en a aucune d'active."""
    now = datetime.now().isoformat(timespec="seconds")

    with connect() as con:
        # Verrouille les écritures concurrentes avant de vérifier.
        con.execute("BEGIN IMMEDIATE")

        existing = con.execute(
            """SELECT id, title FROM requests
               WHERE user_id=?
                 AND status IN ('En attente', 'En cours')
               ORDER BY id DESC LIMIT 1""",
            (int(user.id),),
        ).fetchone()

        if existing:
            return None

        cur = con.execute("""
            INSERT INTO requests(
                user_id,username,title,original_title,media_type,year,source,status,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            user.id,
            user.username or "",
            item["title"],
            item["original"],
            item["type"],
            item["year"],
            item["source"],
            status,
            now,
            now,
        ))

        request_id = cur.lastrowid
        con.execute(
            "INSERT OR IGNORE INTO request_users(user_id,request_id) VALUES(?,?)",
            (user.id, request_id),
        )

    return request_id


async def pick_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "cancel_search":
        context.user_data.pop("search_results", None)
        context.user_data.pop("search_query", None)
        await q.edit_message_text("Recherche annulée.")
        return

    try:
        index = int(data.split(":")[1])
        item = context.user_data["search_results"][index]
    except (ValueError, IndexError, KeyError):
        await q.edit_message_text(
            "Cette recherche a expiré. Envoie de nouveau le titre."
        )
        return

    title = item["title"]
    await q.edit_message_text(
        f"⏳ Vérification des deux archives Telegram : {title}…"
    )

    matches = archive_matches(title)

    # Déjà publié : ne PAS créer de demande.
    # Envoyer les liens des publications trouvées.
    if matches:
        links = []
        entities = context.application.bot_data.get("archive_entities", [])

        for row in matches:
            channel_id = int(row["channel_id"])
            message_id = int(row["message_id"])
            entity = next(
                (e for cid, e in entities if int(cid) == channel_id),
                None
            )

            username = getattr(entity, "username", None) if entity else None

            if username:
                url = f"https://t.me/{username}/{message_id}"
            else:
                internal_id = str(channel_id)
                if internal_id.startswith("-100"):
                    internal_id = internal_id[4:]
                else:
                    internal_id = internal_id.lstrip("-")
                url = f"https://t.me/c/{internal_id}/{message_id}"

            if url not in links:
                links.append(url)

        text = (
            f"✅ « {title} » a déjà été publié dans les archives !\n\n"
            "Voici le lien de la publication :\n"
            + "\n".join(f"🔗 {url}" for url in links)
        )

        await context.bot.send_message(
            chat_id=q.from_user.id,
            text=text,
            disable_web_page_preview=True
        )
        return

    # Absent des archives : enregistrer la demande.
    existing = find_open_request(q.from_user.id)
    if existing:
        await q.answer(
            f"Tu as déjà une demande active : #{existing['id']} — "
            f"{existing['title']}.",
            show_alert=True,
        )
        return
    request_id = await create_request(q.from_user, item)

    # Une autre demande a pu être créée entre la vérification et le clic.
    if request_id is None:
        existing = find_open_request(q.from_user.id)
        if existing:
            await context.bot.send_message(
                chat_id=q.from_user.id,
                text=(
                    f"Tu as déjà une demande active : "
                    f"#{existing['id']} — {existing['title']}."
                ),
            )
        else:
            await context.bot.send_message(
                chat_id=q.from_user.id,
                text="Impossible d'enregistrer la demande. Réessaie."
            )
        return

    await context.bot.send_message(
        chat_id=q.from_user.id,
        text=(
            f"📥 Demande enregistrée !\n\n"
            f"🎬 {title} ({item.get('year') or 'année inconnue'})\n"
            f"Type : {item.get('type', 'Inconnu')}\n"
            f"Numéro : #{request_id}\n"
            f"Statut : En attente"
        )
    )

    admin_text = (
        f"📥 Nouvelle demande #{request_id}\n\n"
        f"Titre : {title}\n"
        f"Titre original : {item.get('original', title)}\n"
        f"Type : {item.get('type', 'Inconnu')}\n"
        f"Année : {item.get('year') or 'inconnue'}\n"
        f"Source : {item.get('source', 'IMDb')}\n"
        f"Utilisateur : {q.from_user.full_name}\n"
        f"ID : {q.from_user.id}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⏳ En cours",
            callback_data=f"status:{request_id}:En cours"
        ),
        InlineKeyboardButton(
            "✅ Publié",
            callback_data=f"status:{request_id}:Publié"
        ),
        InlineKeyboardButton(
            "❌ Refuser",
            callback_data=f"status:{request_id}:Refusée"
        )
    ]])

    for admin_id in admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=keyboard
            )
        except Exception:
            log.exception(
                "Notification impossible pour l'administrateur %s",
                admin_id
            )

    if not admin_ids():
        log.warning("ADMIN_IDS n'est pas configuré.")

async def my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with connect() as con:
        rows = con.execute("""
        SELECT id,title,status,created_at FROM requests
        WHERE user_id=? ORDER BY id DESC LIMIT 15
        """, (update.effective_user.id,)).fetchall()
    if not rows:
        await (update.effective_message or update.callback_query.message).reply_text("Tu n'as pas encore de demande.")
        return
    lines = ["📋 Tes demandes :"]
    for r in rows:
        lines.append(f'#{r["id"]} — {r["title"]} — {r["status"]}')
    await (update.effective_message or update.callback_query.message).reply_text("\n".join(lines))

async def admin_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await (update.effective_message or update.callback_query.message).reply_text("⛔ Accès réservé aux administrateurs.")
        return
    with connect() as con:
        rows = con.execute("""
        SELECT * FROM requests
        WHERE status IN ('En attente','En cours')
        ORDER BY id DESC LIMIT 20
        """).fetchall()
    if not rows:
        await (update.effective_message or update.callback_query.message).reply_text("Aucune demande en attente.")
        return
    for r in rows:
        text = (
            f'#{r["id"]} — {r["title"]} ({r["year"] or "?"})\n'
            f'Type : {r["media_type"]}\n'
            f'Utilisateur : {r["user_id"]}\n'
            f'Statut : {r["status"]}'
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ En cours", callback_data=f'status:{r["id"]}:En cours'),
            InlineKeyboardButton("✅ Publié", callback_data=f'status:{r["id"]}:Publié'),
            InlineKeyboardButton("❌ Refuser", callback_data=f'status:{r["id"]}:Refusée')
        ]])
        await (update.effective_message or update.callback_query.message).reply_text(text, reply_markup=kb)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "stats"):
        await update.effective_message.reply_text("⛔ Tu n'as pas le droit de consulter les statistiques.")
        return
    with connect() as con:
        reqs = con.execute("SELECT status,COUNT(*) n FROM requests GROUP BY status").fetchall()
        archive_count = con.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
    lines = ["📊 Statistiques"]
    lines += [f'{r["status"]} : {r["n"]}' for r in reqs]
    lines.append(f"Publications indexées : {archive_count}")
    await update.effective_message.reply_text("\n".join(lines))

def find_open_request(user_id):
    """Retourne la demande active la plus récente de cet utilisateur."""
    with connect() as con:
        return con.execute(
            """SELECT id, title, status FROM requests
               WHERE user_id=? AND status IN ('En attente', 'En cours')
               ORDER BY id DESC LIMIT 1""",
            (int(user_id),),
        ).fetchone()


def cleanup_duplicate_requests():
    """Au démarrage, conserve la demande active la plus récente par utilisateur."""
    removed = 0
    with connect() as con:
        users = con.execute(
            """SELECT user_id FROM requests
               WHERE status IN ('En attente', 'En cours')
               GROUP BY user_id HAVING COUNT(*) > 1"""
        ).fetchall()

        for item in users:
            rows = con.execute(
                """SELECT id FROM requests
                   WHERE user_id=? AND status IN ('En attente', 'En cours')
                   ORDER BY id DESC""",
                (item["user_id"],),
            ).fetchall()

            # La première ligne est la plus récente : on conserve celle-ci.
            old_ids = [int(row["id"]) for row in rows[1:]]
            for old_id in old_ids:
                con.execute(
                    "DELETE FROM request_users WHERE request_id=?",
                    (old_id,),
                )
                con.execute("DELETE FROM requests WHERE id=?", (old_id,))
                removed += 1

    log.info(
        "Vérification des demandes terminée : %s doublon(s) supprimé(s).",
        removed,
    )


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer("Accès refusé.", show_alert=True)
        return

    try:
        _, raw_id, status = q.data.split(":", 2)
        request_id = int(raw_id)
    except (ValueError, AttributeError):
        await q.answer("Action invalide.", show_alert=True)
        return

    allowed = {"En cours", "Publié", "Refusée"}
    if status not in allowed:
        await q.answer("Statut invalide.", show_alert=True)
        return

    with connect() as con:
        row = con.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()

    if not row:
        await q.answer("Demande introuvable.", show_alert=True)
        try:
            await q.edit_message_text("Cette demande n'existe plus.")
        except Exception:
            pass
        return

    # PUBLIÉ : retrouver la fiche photo et envoyer son lien avant suppression.
    if status == "Publié":
        matches = archive_matches(row["title"], limit=1)

        if not matches:
            await q.answer(
                f"Aucune photo trouvée pour « {row['title']} ». "
                "La demande est conservée.",
                show_alert=True,
            )
            return

        photo = matches[0]
        channel_id = int(photo["channel_id"])
        message_id = int(photo["message_id"])

        entities = context.application.bot_data.get("archive_entities", [])
        entity = next(
            (
                e for cid, e in entities
                if int(cid) == channel_id
            ),
            None,
        )
        username = getattr(entity, "username", None) if entity else None

        if username:
            photo_url = f"https://t.me/{username}/{message_id}"
        else:
            internal_id = str(channel_id)
            if internal_id.startswith("-100"):
                internal_id = internal_id[4:]
            else:
                internal_id = internal_id.lstrip("-")
            photo_url = f"https://t.me/c/{internal_id}/{message_id}"

        try:
            await context.bot.send_message(
                chat_id=int(row["user_id"]),
                text=(
                    f"✅ La fiche photo de « {row['title']} » a été retrouvée !\n\n"
                    f"🖼️ Lien de la publication :\n{photo_url}"
                ),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.warning(
                "Lien photo non envoyé au demandeur %s pour la demande #%s : %s",
                row["user_id"], request_id, exc,
            )
            await q.answer(
                "Impossible d'envoyer le lien au demandeur. "
                "La demande est conservée.",
                show_alert=True,
            )
            return

        # Supprimer uniquement après la réussite de l'envoi privé.
        try:
            with connect() as con:
                con.execute(
                    "DELETE FROM requests WHERE id=?", (request_id,)
                )
        except Exception:
            log.exception(
                "Lien envoyé, mais suppression impossible pour la demande #%s",
                request_id,
            )
            await q.answer(
                "Lien envoyé, mais la suppression a échoué. "
                "Vérifie la demande avant de recommencer.",
                show_alert=True,
            )
            return

        await q.answer("Lien photo envoyé au demandeur.")
        try:
            await q.edit_message_text(
                f"✅ Demande #{request_id} — {row['title']}\n"
                "Photo trouvée, lien envoyé en privé et demande supprimée."
            )
        except Exception:
            log.exception("Impossible de mettre à jour le message admin.")
        return

    # Les autres statuts conservent le comportement habituel.
    with connect() as con:
        con.execute(
            "UPDATE requests SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(timespec="seconds"), request_id),
        )

    await q.answer()
    try:
        await q.edit_message_text(
            f"Demande #{request_id} — {row['title']}\nStatut : {status}"
        )
    except Exception:
        log.exception("Impossible de mettre à jour le message admin.")

    try:
        messages = {
            "En cours": (
                f"⏳ Ta demande #{request_id} — {row['title']} "
                "est en cours de traitement."
            ),
            "Refusée": (
                f"❌ Ta demande #{request_id} — {row['title']} "
                "a été refusée."
            ),
        }
        await context.bot.send_message(
            chat_id=int(row["user_id"]),
            text=messages[status],
        )
    except Exception as exc:
        log.warning("Notification utilisateur impossible : %s", exc)


async def resolve_entity(client, channel_id):
    async for dialog in client.iter_dialogs():
        if dialog.id == channel_id:
            return dialog.entity
    return await client.get_entity(channel_id)

async def index_channel(client, channel_id, entity):
    log.info("Indexation initiale des fiches photo du canal %s", channel_id)
    scanned = 0
    count = 0

    async for msg in client.iter_messages(entity, limit=None):
        scanned += 1
        text = msg.message or ""
        if msg.photo and text.strip():
            add_archive(channel_id, msg.id, text, is_photo=True)
            count += 1

        if scanned % 500 == 0:
            log.info(
                "Canal %s : %s messages examinés, %s fiches photo",
                channel_id, scanned, count
            )

    with connect() as con:
        con.execute("""
            INSERT INTO settings(key, value)
            VALUES(?, '1')
            ON CONFLICT(key) DO UPDATE SET value='1'
        """, (f"archive_indexed_{channel_id}",))

    log.info(
        "Indexation terminée pour le canal %s : %s messages examinés, "
        "%s fiches photo indexées",
        channel_id, scanned, count
    )

async def actualiser_archives(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or not has_permission(user.id, "configure"):
        await message.reply_text("⛔ Réservé au créateur ou aux administrateurs ayant le droit Configuration.")
        return

    client = context.application.bot_data.get("telethon_client")
    entities = context.application.bot_data.get("archive_entities", [])

    if not client or not entities:
        await message.reply_text("⚠️ Telethon n'est pas connecté ou aucun canal d'archive n'est accessible.")
        return

    status = await message.reply_text("🔄 Actualisation des fiches photo en cours…")
    total_scanned = 0
    total_updated = 0
    errors = []

    for channel_id, entity in entities:
        scanned = 0
        updated = 0
        try:
            async for msg in client.iter_messages(entity, limit=None):
                scanned += 1
                caption = (msg.message or "").strip()

                if msg.photo and caption:
                    add_archive(channel_id, msg.id, caption, is_photo=True)
                    updated += 1

                if scanned % 1000 == 0:
                    log.info(
                        "Actualisation canal %s : %s messages examinés",
                        channel_id, scanned
                    )

            with connect() as con:
                con.execute(
                    "INSERT INTO settings(key,value) VALUES(?, '1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'",
                    (f"archive_indexed_{channel_id}",),
                )

            total_scanned += scanned
            total_updated += updated
            log.info(
                "Actualisation terminée : canal=%s, examinés=%s, fiches=%s",
                channel_id, scanned, updated
            )

        except Exception as exc:
            log.exception("Échec de l'actualisation du canal %s", channel_id)
            errors.append(f"{channel_id}: {exc}")

    result = (
        f"✅ Actualisation terminée.\n"
        f"Messages examinés : {total_scanned}\n"
        f"Fiches photo relues : {total_updated}"
    )

    if errors:
        result += "\n\n⚠️ Erreurs :\n" + "\n".join(errors[:5])

    await status.edit_text(result[:4000])


async def admin_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_admins"):
        await update.effective_message.reply_text("⛔ Accès refusé.")
        return
    with connect() as con:
        rows = con.execute(
            "SELECT user_id,can_stats,can_manage_admins,can_configure FROM bot_admins ORDER BY user_id"
        ).fetchall()
    lines = ["👥 Administrateurs"]
    if OWNER_ID > 0:
        lines.append(f"👑 Créateur : {OWNER_ID} (tous les droits)")
    for r in rows:
        rights = []
        if r["can_stats"]:
            rights.append("statistiques")
        if r["can_manage_admins"]:
            rights.append("gestion admins")
        if r["can_configure"]:
            rights.append("configuration")
        lines.append(f'{r["user_id"]} — droits supplémentaires : {", ".join(rights) if rights else "aucun"}')
    lines.extend(["", "Ajouter : /admin_ajouter ID", "Supprimer : /admin_supprimer ID"])
    if update.effective_user.id == OWNER_ID:
        lines.append("Modifier les droits : /droits ID")
    await update.effective_message.reply_text("\n".join(lines))

async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "manage_admins"):
        await update.effective_message.reply_text("⛔ Accès refusé.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage : /admin_ajouter ID")
        return
    target = int(context.args[0])
    if target <= 0 or target == OWNER_ID:
        await update.effective_message.reply_text("⛔ Identifiant invalide ou réservé au créateur.")
        return
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO bot_admins(user_id,created_at) VALUES(?,?)",
            (target, datetime.now().isoformat(timespec="seconds"))
        )
    await update.effective_message.reply_text(
        f"✅ Administrateur {target} ajouté. Aucun droit supplémentaire accordé."
    )

async def admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_permission(uid, "manage_admins"):
        await update.effective_message.reply_text("⛔ Accès refusé.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage : /admin_supprimer ID")
        return
    target = int(context.args[0])
    if target == OWNER_ID or target == uid:
        await update.effective_message.reply_text("⛔ Suppression interdite pour cet identifiant.")
        return
    with connect() as con:
        cur = con.execute("DELETE FROM bot_admins WHERE user_id=?", (target,))
    await update.effective_message.reply_text(
        "✅ Administrateur supprimé." if cur.rowcount else "Administrateur introuvable."
    )

async def admin_rights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID <= 0 or update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("⛔ Seul le créateur peut modifier les permissions.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage : /droits ID")
        return
    target = int(context.args[0])
    if target == OWNER_ID:
        await update.effective_message.reply_text("Le créateur possède déjà tous les droits.")
        return
    with connect() as con:
        row = con.execute("SELECT user_id FROM bot_admins WHERE user_id=?", (target,)).fetchone()
    if not row:
        await update.effective_message.reply_text("Cet utilisateur n'est pas administrateur. Ajoute-le d'abord.")
        return
    await send_rights_keyboard(update.effective_message.reply_text, target)

async def send_rights_keyboard(reply_method, target):
    with connect() as con:
        row = con.execute(
            "SELECT can_stats,can_manage_admins,can_configure FROM bot_admins WHERE user_id=?",
            (target,)
        ).fetchone()
    if not row:
        await reply_method("Administrateur introuvable.")
        return
    options = [
        ("stats", "Consulter les statistiques", row["can_stats"]),
        ("manage_admins", "Gérer les administrateurs", row["can_manage_admins"]),
        ("configure", "Modifier la configuration", row["can_configure"]),
    ]
    keyboard = [[InlineKeyboardButton(
        ("✅ " if enabled else "❌ ") + label,
        callback_data=f"perm:{target}:{key}"
    )] for key, label, enabled in options]
    await reply_method(
        f"🔐 Permissions de l'administrateur {target}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if OWNER_ID <= 0 or q.from_user.id != OWNER_ID:
        await q.answer("Seul le créateur peut modifier les permissions.", show_alert=True)
        return
    try:
        _, raw_target, permission = q.data.split(":", 2)
        target = int(raw_target)
    except (ValueError, AttributeError):
        await q.answer("Action invalide.", show_alert=True)
        return
    columns = {
        "stats": "can_stats",
        "manage_admins": "can_manage_admins",
        "configure": "can_configure",
    }
    column = columns.get(permission)
    if not column or target == OWNER_ID:
        await q.answer("Action interdite.", show_alert=True)
        return
    with connect() as con:
        row = con.execute(
            f"SELECT {column} FROM bot_admins WHERE user_id=?", (target,)
        ).fetchone()
        if not row:
            await q.answer("Administrateur introuvable.", show_alert=True)
            return
        con.execute(
            f"UPDATE bot_admins SET {column}=? WHERE user_id=?",
            (0 if row[column] else 1, target)
        )
    await q.answer("Permission mise à jour.")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_rights_keyboard(q.message.reply_text, target)

async def configuration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_permission(update.effective_user.id, "configure"):
        await update.effective_message.reply_text("⛔ Accès refusé.")
        return
    await update.effective_message.reply_text(
        "⚙️ Configuration\n\n"
        f"Archives actuelles : {', '.join(map(str, ARCHIVES)) or 'aucune'}\n\n"
        "Pour modifier : /config_archives -1001234567890 -1009876543210\n"
        "Redémarrage nécessaire après modification."
    )

async def config_archives(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ARCHIVES
    if not has_permission(update.effective_user.id, "configure"):
        await update.effective_message.reply_text("⛔ Accès refusé.")
        return
    values = []
    for arg in context.args:
        try:
            value = int(arg)
        except ValueError:
            await update.effective_message.reply_text("Identifiant invalide.")
            return
        if value not in values:
            values.append(value)
    if not values:
        await update.effective_message.reply_text("Usage : /config_archives -1001234567890 -1009876543210")
        return
    env_path = os.path.expanduser("~/demandefilm/.env")
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new_line = "ARCHIVE_CHANNELS=" + ",".join(map(str, values)) + "\n"
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("ARCHIVE_CHANNELS="):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    ARCHIVES = values
    await update.effective_message.reply_text(
        "✅ Canaux enregistrés dans .env. Redémarre le bot pour appliquer la configuration."
    )


async def start_telethon(app):
    if not (API_ID and API_HASH and ARCHIVES):
        log.warning("Telethon non configuré : renseigne API_ID, API_HASH et ARCHIVE_CHANNELS.")
        return
    try:
        from telethon import TelegramClient, events
        client = TelegramClient(SESSION, int(API_ID), API_HASH)
        await client.start()
        entities = []
        for cid in ARCHIVES:
            try:
                entity = await resolve_entity(client, cid)
                entities.append((cid, entity))
            except Exception as exc:
                log.error("Impossible d'accéder au canal %s : %s", cid, exc)

        async def new_message(event):
            msg = event.message
            text = msg.message or ""
            if not msg.photo or not text.strip():
                return

            for cid, entity in entities:
                if event.chat_id == int(cid) or event.chat_id == getattr(entity, "id", None):
                    add_archive(cid, msg.id, text, is_photo=True)
                    log.info(
                        "Fiche photo ajoutée : canal=%s message=%s",
                        cid, msg.id
                    )

                    if int(cid) == -1001217043641:
                        try:
                            await client.pin_message(
                                entity, msg.id, notify=False
                            )
                            log.info("Photo épinglée dans le canal des séries : %s", msg.id)
                        except Exception as exc:
                            log.warning(
                                "Épinglage impossible pour le message %s : %s",
                                msg.id, exc
                            )
                    break

        async def edited_message(event):
            msg = event.message
            chat_id = event.chat_id

            for cid, entity in entities:
                if chat_id == int(cid) or chat_id == getattr(entity, "id", None):
                    caption = (msg.message or "").strip()

                    if msg.photo and caption:
                        add_archive(cid, msg.id, caption, is_photo=True)
                        log.info(
                            "Légende modifiée et index actualisée : canal=%s message=%s",
                            cid, msg.id
                        )
                    else:
                        with connect() as con:
                            con.execute(
                                "DELETE FROM archive_index "
                                "WHERE channel_id=? AND message_id=?",
                                (cid, msg.id),
                            )
                    break

        if entities:
            client.add_event_handler(
                new_message,
                events.NewMessage(chats=[entity for _, entity in entities])
            )
            client.add_event_handler(
                edited_message,
                events.MessageEdited(chats=[entity for _, entity in entities])
            )
        app.bot_data["telethon_client"] = client
        app.bot_data["archive_entities"] = entities

        for cid, entity in entities:
            marker = f"archive_indexed_{cid}"
            with connect() as con:
                done = con.execute(
                    "SELECT value FROM settings WHERE key=?", (marker,)
                ).fetchone()
                existing = con.execute(
                    "SELECT COUNT(*) FROM archive_index WHERE channel_id=?",
                    (cid,)
                ).fetchone()[0]

                if done and done["value"] == "1":
                    log.info("Index historique déjà terminé pour %s : scan ignoré", cid)
                    continue

                # Préserver l'index existant et éviter un nouveau scan complet.
                if existing > 0:
                    con.execute("""
                        INSERT INTO settings(key, value) VALUES(?, '1')
                        ON CONFLICT(key) DO UPDATE SET value='1'
                    """, (marker,))
                    log.info(
                        "Index existant conservé pour %s (%s fiches) : scan ignoré",
                        cid, existing
                    )
                    continue

            asyncio.create_task(index_channel(client, cid, entity))
        log.info("Telethon connecté : %s canal/canaux accessibles", len(entities))
    except Exception:
        log.exception("Échec du démarrage de Telethon. Le bot reste disponible.")

async def post_init(app):
    await start_telethon(app)

async def post_shutdown(app):
    client = app.bot_data.get("telethon_client")
    if client:
        await client.disconnect()

def main():
    if not TOKEN:
        raise SystemExit(
            "BOT_TOKEN absent. Configure-le dans ~/demandefilm/.env"
        )

    init_db()
    cleanup_duplicate_requests()
    if OWNER_ID <= 0:
        raise SystemExit("OWNER_ID absent ou invalide dans ~/demandefilm/.env")
    init_admin_db()
    builder = Application.builder().token(TOKEN)
    if LOCAL_API:
        builder = builder.base_url("http://127.0.0.1:8081/bot")
        builder = builder.base_file_url("http://127.0.0.1:8081/file/bot")
    builder = builder.post_init(post_init).post_shutdown(post_shutdown)
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("annuler", cancel))
    app.add_handler(CommandHandler("mesdemandes", my_requests))
    app.add_handler(CommandHandler("demandes", admin_requests))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("admins", admin_management))
    app.add_handler(CommandHandler("admin_ajouter", admin_add))
    app.add_handler(CommandHandler("admin_supprimer", admin_remove))
    app.add_handler(CommandHandler("droits", admin_rights))
    app.add_handler(CommandHandler("configuration", configuration))
    app.add_handler(CommandHandler("config_archives", config_archives))
    app.add_handler(CommandHandler("actualiser", actualiser_archives))
    app.add_handler(CallbackQueryHandler(menu_button_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(permission_callback, pattern=r"^perm:"))
    app.add_handler(CallbackQueryHandler(pick_title, pattern=r"^(pick:\d+|cancel_search)$"))
    app.add_handler(CallbackQueryHandler(status_callback, pattern=r"^status:"))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        handle_group_title
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        handle_title
    ))

    log.info("Démarrage du bot de demandes.")
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()