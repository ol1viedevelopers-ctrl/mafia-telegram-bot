import html
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import telebot
from dotenv import load_dotenv
from telebot import types

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mafia")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = "mafia_stats.db"
OLD_STATS_FILE = "stats.json"

MIN_PLAYERS = 3
LOBBY_TIMEOUT = 60
NIGHT_TIMEOUT = 90
NIGHT_TIMEOUT_SHORT = 45
VOTE_TIMEOUT = 90

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)


def esc(s):
    return html.escape(str(s or ""), quote=False)


class Phase(str, Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    VOTING = "voting"
    ENDED = "ended"


class Team(str, Enum):
    CITY = "city"
    MAFIA = "mafia"
    NEUTRAL = "neutral"


class Role(str, Enum):
    CITIZEN = "citizen"
    COMMISSAR = "commissar"
    SHERIFF = "sheriff"
    DOCTOR = "doctor"
    BODYGUARD = "bodyguard"
    ALCHEMIST = "alchemist"
    MISTRESS = "mistress"
    WITNESS = "witness"
    LAWYER = "lawyer"
    DON = "don"
    CUTTHROAT = "cutthroat"
    PSYCHO = "psycho"


ROLES = {
    Role.CITIZEN: ("Мирный житель", Team.CITY, "🧔", "Голосуй днём, выживай ночью."),
    Role.COMMISSAR: ("Комиссар", Team.CITY, "🕵️", "Ночью проверяешь или стреляешь (одна пуля за игру)."),
    Role.SHERIFF: ("Шериф", Team.CITY, "🤠", "Две проверки за ночь. Вторую можно пропустить."),
    Role.DOCTOR: ("Доктор", Team.CITY, "🩺", "Лечишь одного. Себя — не две ночи подряд."),
    Role.BODYGUARD: ("Телохранитель", Team.CITY, "🛡️", "Защищаешь цель ценой своей жизни."),
    Role.ALCHEMIST: ("Алхимик", Team.CITY, "🧪", "Два зелья: Спасение и Молчание."),
    Role.MISTRESS: ("Любовница", Team.CITY, "💋", "Блокируешь чужой ночной ход."),
    Role.WITNESS: ("Свидетель", Team.CITY, "👀", "Узнаёшь имя убийцы своей цели."),
    Role.LAWYER: ("Адвокат", Team.CITY, "⚖️", "Один раз отменяешь дневную казнь."),
    Role.DON: ("Дон", Team.MAFIA, "🎩", "Стреляешь каждую ночь. Комиссара ищешь через ход."),
    Role.CUTTHROAT: ("Головорез", Team.MAFIA, "🔫", "Стреляешь вместе с Доном."),
    Role.PSYCHO: ("Псих", Team.NEUTRAL, "🪓", "Побеждаешь один. При казни тянешь обвинителя."),
}

DEATHS = [
    "найден за гаражами без кроссовок.",
    "упакован в ковёр и сброшен в канал.",
    "получил свинцовую пилюлю у шаурмичной.",
    "утоплен в мазуте под эстакадой.",
    "встретил рассвет в багажнике ржавой «девятки».",
    "связан шнурками и усажен на лавку.",
]
LYNCH = [
    "{n} привязан к фонарю и оставлен на суд истории!",
    "{n} торжественно спущен в мусоропровод!",
    "{n} замурован в бетонном гараже!",
    "{n} спущен с обрыва на тележке из супермаркета!",
]

REQUIRED_ROLES = {
    3: [Role.DON, Role.COMMISSAR],
    4: [Role.DON, Role.COMMISSAR],
    5: [Role.DON, Role.COMMISSAR, Role.DOCTOR],
    6: [Role.DON, Role.CUTTHROAT, Role.COMMISSAR, Role.DOCTOR],
    7: [Role.DON, Role.CUTTHROAT, Role.COMMISSAR, Role.DOCTOR],
    8: [Role.DON, Role.CUTTHROAT, Role.COMMISSAR, Role.DOCTOR, Role.BODYGUARD],
    9: [Role.DON, Role.CUTTHROAT, Role.COMMISSAR, Role.DOCTOR, Role.BODYGUARD],
    10: [Role.DON, Role.CUTTHROAT, Role.COMMISSAR, Role.SHERIFF, Role.DOCTOR, Role.BODYGUARD],
    11: [Role.DON, Role.CUTTHROAT, Role.CUTTHROAT, Role.COMMISSAR, Role.SHERIFF, Role.DOCTOR],
    12: [Role.DON, Role.CUTTHROAT, Role.CUTTHROAT, Role.COMMISSAR, Role.SHERIFF, Role.DOCTOR, Role.BODYGUARD],
}

FREE_ROLES = [
    Role.CITIZEN, Role.CITIZEN, Role.CITIZEN, Role.CITIZEN,
    Role.DOCTOR, Role.BODYGUARD, Role.ALCHEMIST, Role.MISTRESS,
    Role.WITNESS, Role.LAWYER, Role.CUTTHROAT, Role.PSYCHO,
]


@dataclass
class Player:
    user_id: int
    chat_id: int
    name: str
    role: Optional[Role] = None
    alive: bool = True
    target: Optional[int] = None
    target_kill: Optional[int] = None
    target_check: Optional[int] = None
    target_check2: Optional[int] = None
    commissar_shot: Optional[int] = None
    alch_potion: Optional[str] = None
    lawyer_target: Optional[int] = None
    protected: bool = False
    silenced: bool = False
    last_healed: Optional[int] = None
    has_save: bool = True
    has_silence: bool = True
    has_shot: bool = False
    has_cancel: bool = True
    don_can_check: bool = True
    said_last_words: bool = False
    extra: dict = field(default_factory=dict)

    def disp(self):
        return f"<b>{esc(self.name)}</b>"

    def plain(self):
        return self.name


@dataclass
class Game:
    chat_id: int
    thread_id: Optional[int] = None
    phase: Phase = Phase.LOBBY
    players: dict = field(default_factory=dict)
    night: int = 0
    day: int = 0
    lobby_msg_id: Optional[int] = None
    lobby_start: float = field(default_factory=time.time)
    lobby_timer: Optional[threading.Timer] = None
    timer: Optional[threading.Timer] = None
    votes: dict = field(default_factory=dict)
    moderator_id: Optional[int] = None
    resolving: bool = False
    mute_enabled: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel_timers(self):
        for t in (self.timer, self.lobby_timer):
            if t:
                try:
                    t.cancel()
                except Exception as e:
                    log.debug(f"timer cancel: {e}")
        self.timer = None
        self.lobby_timer = None

    def alive(self):
        return [p for p in self.players.values() if p.alive]

    def by_team(self, team):
        return [
            p for p in self.players.values()
            if p.alive and p.role and ROLES[p.role][1] == team
        ]

    def get(self, uid):
        return self.players.get(uid)

    def clear_night(self):
        for p in self.players.values():
            p.target = None
            p.target_kill = None
            p.target_check = None
            p.target_check2 = None
            p.commissar_shot = None
            p.alch_potion = None
            p.lawyer_target = None
            p.protected = False
            p.extra.pop("sheriff_used", None)


GAMES = {}
GAMES_LOCK = threading.Lock()
MEMBER_CACHE = {}
MEMBER_CACHE_LOCK = threading.Lock()
SEND_LOCK = threading.Lock()
DB_LOCK = threading.Lock()


def db_connect():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)


def init_db():
    with DB_LOCK:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    games INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()
    migrate_old_stats()


def migrate_old_stats():
    if not os.path.exists(OLD_STATS_FILE):
        return
    try:
        import json
        with open(OLD_STATS_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        log.warning(f"migrate: не удалось прочитать stats.json: {e}")
        return

    with DB_LOCK:
        with db_connect() as conn:
            for key, data in old.items():
                try:
                    uid = int(key)
                except ValueError:
                    continue
                name = data.get("name", f"Игрок_{uid}")
                games = int(data.get("games", 0))
                wins = int(data.get("wins", 0))
                conn.execute("""
                    INSERT INTO players (user_id, name, games, wins)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        name = excluded.name,
                        games = MAX(players.games, excluded.games),
                        wins = MAX(players.wins, excluded.wins)
                """, (uid, name, games, wins))
            conn.commit()

    try:
        os.rename(OLD_STATS_FILE, OLD_STATS_FILE + ".migrated")
    except OSError:
        pass
    log.info("stats.json перенесён в SQLite")


def add_win(uid, name, won):
    try:
        with DB_LOCK:
            with db_connect() as conn:
                conn.execute("""
                    INSERT INTO players (user_id, name, games, wins)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        name = excluded.name,
                        games = players.games + 1,
                        wins = players.wins + excluded.wins
                """, (uid, name, 1 if won else 0))
                conn.commit()
    except sqlite3.Error as e:
        log.warning(f"add_win: {e}")


def top_players(limit=10):
    try:
        with DB_LOCK:
            with db_connect() as conn:
                cur = conn.execute(
                    "SELECT name, wins, games FROM players "
                    "ORDER BY wins DESC, games DESC LIMIT ?",
                    (limit,),
                )
                return cur.fetchall()
    except sqlite3.Error as e:
        log.warning(f"top_players: {e}")
        return []


def balance(n):
    required = REQUIRED_ROLES.get(n)
    if not required:
        required = [Role.DON, Role.COMMISSAR]
        mafia_extra = max(1, n // 3 - 1)
        required += [Role.CUTTHROAT] * mafia_extra
        required += [Role.DOCTOR, Role.BODYGUARD, Role.SHERIFF]

    if len(required) > n:
        required = required[:n]

    need = n - len(required)
    free = list(FREE_ROLES)
    random.shuffle(free)

    picked = []
    for r in free:
        if r not in required:
            picked.append(r)
        if len(picked) >= need:
            break

    while len(picked) < need:
        picked.append(Role.CITIZEN)

    result = list(required) + picked[:need]
    random.shuffle(result)
    return result


def send(chat_id, text, thread_id=None, kb=None):
    kw = {"chat_id": chat_id, "text": text}
    if kb is not None:
        kw["reply_markup"] = kb
    if thread_id is not None:
        kw["message_thread_id"] = thread_id
    with SEND_LOCK:
        try:
            return bot.send_message(**kw)
        except Exception as e:
            log.warning(f"send {chat_id}: {e}")
            return None


def send_game(g, text, kb=None):
    return send(g.chat_id, text, thread_id=g.thread_id, kb=kb)


def edit(call, text, kb=None):
    kw = {
        "chat_id": call.from_user.id,
        "message_id": call.message.message_id,
        "parse_mode": "HTML",
    }
    if kb is not None:
        kw["reply_markup"] = kb
    with SEND_LOCK:
        try:
            bot.edit_message_text(text, **kw)
        except Exception as e:
            log.debug(f"edit: {e}")


def is_chat_admin(chat_id, user_id):
    now = time.time()
    with MEMBER_CACHE_LOCK:
        cached = MEMBER_CACHE.get((chat_id, user_id))
        if cached and now - cached[1] < 60:
            return cached[0]
    try:
        m = bot.get_chat_member(chat_id, user_id)
        status = m.status in ("administrator", "creator")
    except Exception as e:
        log.debug(f"get_chat_member: {e}")
        status = False
    with MEMBER_CACHE_LOCK:
        MEMBER_CACHE[(chat_id, user_id)] = (status, now)
    return status


def bot_can_delete(chat_id):
    try:
        me = bot.get_me()
        m = bot.get_chat_member(chat_id, me.id)
        if m.status == "creator":
            return True
        return bool(getattr(m, "can_delete_messages", False))
    except Exception as e:
        log.debug(f"bot_can_delete: {e}")
        return False


def kb_reg():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🎮 Присоединиться", callback_data="r"))
    m.add(types.InlineKeyboardButton("🚀 Начать игру", callback_data="s"))
    return m


def kb_targets(alive, action, param="", exclude=None, add_skip=True):
    m = types.InlineKeyboardMarkup()
    for p in alive:
        if exclude and p.user_id == exclude:
            continue
        cb = f"n:{action}:{p.user_id}"
        if param:
            cb += f":{param}"
        m.add(types.InlineKeyboardButton(f"🎯 {p.plain()}", callback_data=cb))
    if add_skip:
        m.add(types.InlineKeyboardButton("❌ Пропустить", callback_data=f"n:{action}:0"))
    return m


def kb_alch():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("💚 Спасение", callback_data="a:save"))
    m.add(types.InlineKeyboardButton("🤫 Молчание", callback_data="a:silence"))
    m.add(types.InlineKeyboardButton("❌ Ничего", callback_data="n:al:0"))
    return m


def kb_vote(alive):
    m = types.InlineKeyboardMarkup()
    for p in alive:
        m.add(types.InlineKeyboardButton(f"⚖️ {p.plain()}", callback_data=f"v:{p.user_id}"))
    return m


def lobby_text(g):
    n = len(g.players)
    left = max(0, int(LOBBY_TIMEOUT - (time.time() - g.lobby_start)))
    roster = "\n".join(f"{i+1}. {p.disp()}" for i, p in enumerate(g.players.values()))
    if not roster:
        roster = "<i>Пока пусто</i>"
    if n >= MIN_PLAYERS:
        status = f"✅ Готово к старту! ({n})"
    else:
        status = f"⚠️ Нужно ещё {MIN_PLAYERS - n}"
    return (
        f"🎲 <b>НАБОР В ИГРУ</b>\n\n"
        f"👥 Игроков: <b>{n}</b>\n{status}\n\n"
        f"📋 Участники:\n{roster}\n\n"
        f"⏳ До автостарта: <b>{left} сек</b>\n"
        "<i>За 1 минуту наберётся 3+ — старт автоматически.</i>"
    )


def lobby_expire(chat_id):
    try:
        _lobby_expire_impl(chat_id)
    except Exception as e:
        log.exception(f"lobby_expire {chat_id}: {e}")


def _lobby_expire_impl(chat_id):
    with GAMES_LOCK:
        g = GAMES.get(chat_id)
    if not g or g.phase != Phase.LOBBY:
        return
    with g.lock:
        n = len(g.players)
        g.lobby_timer = None
    if n >= MIN_PLAYERS:
        start_game(chat_id, auto=True)
    else:
        send_game(g, f"⏳ Время набора истекло. Игроков {n} из {MIN_PLAYERS}. Игра отменена.", kb=kb_reg())
        with GAMES_LOCK:
            GAMES.pop(chat_id, None)


def register(chat_id, user, message=None, call_id=None, thread_id=None):
    if not user or user.id == 1087968824:
        if call_id:
            bot.answer_callback_query(call_id, "Отключите анонимность!", show_alert=True)
        return
    if message and message.chat.type not in ("group", "supergroup"):
        if call_id:
            bot.answer_callback_query(call_id, "Игра только в группе!", show_alert=True)
        return

    with GAMES_LOCK:
        g = GAMES.get(chat_id)
        if not g:
            tid = thread_id or (message.message_thread_id if message else None)
            g = Game(chat_id=chat_id, thread_id=tid, lobby_start=time.time())
            g.mute_enabled = bot_can_delete(chat_id)
            GAMES[chat_id] = g
            if not g.mute_enabled:
                send(chat_id, "⚠️ Бот не админ в этом чате. Мут работать не будет — выдайте права на удаление сообщений.")

    with g.lock:
        if g.phase != Phase.LOBBY:
            if call_id:
                bot.answer_callback_query(call_id, "Игра идёт!", show_alert=True)
            return
        if user.id in g.players:
            if call_id:
                bot.answer_callback_query(call_id, "Ты уже в игре.")
            return
        name = user.full_name or user.username or f"Игрок_{user.id}"
        g.players[user.id] = Player(user_id=user.id, chat_id=chat_id, name=name)
        if g.moderator_id is None:
            g.moderator_id = user.id
        n = len(g.players)
        if not g.lobby_timer:
            g.lobby_start = time.time()
            g.lobby_timer = threading.Timer(LOBBY_TIMEOUT, lobby_expire, args=[chat_id])
            g.lobby_timer.daemon = True
            g.lobby_timer.start()

    if call_id:
        bot.answer_callback_query(call_id, f"✅ Ты в игре! ({n})")

    txt = lobby_text(g)
    kb = kb_reg()
    edited = False
    if g.lobby_msg_id:
        with SEND_LOCK:
            try:
                bot.edit_message_text(txt, chat_id=chat_id, message_id=g.lobby_msg_id, reply_markup=kb, parse_mode="HTML")
                edited = True
            except Exception as e:
                log.debug(f"lobby edit: {e}")
    if not edited:
        sent = send_game(g, txt, kb=kb)
        if sent:
            g.lobby_msg_id = sent.message_id


def start_game(chat_id, message=None, call_id=None, auto=False):
    with GAMES_LOCK:
        g = GAMES.get(chat_id)
    if not g or g.phase != Phase.LOBBY:
        if call_id:
            bot.answer_callback_query(call_id, "Игра не найдена.", show_alert=True)
        return

    with g.lock:
        n = len(g.players)
        if n < MIN_PLAYERS:
            if call_id:
                bot.answer_callback_query(call_id, f"Нужно {MIN_PLAYERS}, есть {n}.", show_alert=True)
            return
        g.cancel_timers()
        roles = balance(n)
        ids = list(g.players.keys())
        random.shuffle(ids)
        for uid, role in zip(ids, roles):
            p = g.players[uid]
            p.role = role
            p.alive = True
            p.silenced = False
            p.has_shot = False
            p.has_save = True
            p.has_silence = True
            p.has_cancel = True
            p.said_last_words = False
            p.don_can_check = True
            p.last_healed = None
            p.commissar_shot = None
            p.extra.clear()
        g.night = 1
        g.phase = Phase.NIGHT
        g.resolving = False

    if call_id:
        bot.answer_callback_query(call_id, "Раздача ролей...")

    header = "⏳ <b>1 МИНУТА ИСТЕКЛА! ВСЕ В СБОРЕ!</b>\n" if auto else "🚀 <b>ИГРА НАЧИНАЕТСЯ!</b>\n"
    roster = "\n".join(f"{i+1}. {p.disp()}" for i, p in enumerate(g.players.values()))
    send_game(
        g,
        f"{header}\n🏙️ <b>ГОРОД ЗАСЫПАЕТ</b>\n\n👥 Участники ({n}):\n{roster}\n\n"
        "📩 Роли в ЛС. Открой бота!\n\n🌙 <b>Ночь #{g.night}</b>",
    )
    distribute_roles_async(g)
    start_night(g)


def distribute_roles_async(g):
    threading.Thread(target=distribute_roles, args=[g], daemon=True).start()


def distribute_roles(g):
    for p in g.players.values():
        if not p.role:
            continue
        name, team, emoji, desc = ROLES[p.role]
        if team == Team.CITY:
            tname = "Город"
        elif team == Team.MAFIA:
            tname = "Мафия"
        else:
            tname = "Одиночка"
        try:
            with SEND_LOCK:
                bot.send_message(
                    p.user_id,
                    f"🩸 <b>ТВОЯ РОЛЬ</b>\n\n{emoji} <b>{name}</b>\nКоманда: <b>{tname}</b>\n\n{desc}\n\n"
                    "<i>Если умрёшь — сможешь отправить 1 послание командой /say текст.</i>",
                )
        except Exception as e:
            log.warning(f"role send {p.user_id}: {e}")
            send_game(g, f"⚠️ {p.disp()}, открой ЛС с ботом, иначе не получишь роль!")


def start_night(g):
    with g.lock:
        g.phase = Phase.NIGHT
        g.clear_night()
        g.cancel_timers()
        g.resolving = False
        alive = g.alive()
        timeout = NIGHT_TIMEOUT_SHORT if len(alive) <= 4 else NIGHT_TIMEOUT

    mafia = [p for p in alive if p.role in (Role.DON, Role.CUTTHROAT)]
    mafia_names = ", ".join(m.plain() for m in mafia)

    for p in alive:
        if not p.role:
            continue
        try:
            if p.role in (Role.DON, Role.CUTTHROAT):
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. МАФИЯ</b>\nКлан: <b>{esc(mafia_names)}</b>\n\n"
                    "Кого убираем? Общаться — <code>/m текст</code>.",
                    kb=kb_targets(alive, "k"),
                )
                if p.role == Role.DON and p.don_can_check:
                    send(
                        p.user_id,
                        "🎩 <b>ПРОВЕРКА ДОНА:</b> Кто комиссар?",
                        kb=kb_targets(alive, "dc", exclude=p.user_id),
                    )
            elif p.role == Role.COMMISSAR:
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. КОМИССАР</b>\nПроверка или выстрел?",
                    kb=build_commissar_kb(alive, p),
                )
            elif p.role == Role.SHERIFF:
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. ШЕРИФ</b>\nПроверка 1 из 2:",
                    kb=kb_targets(alive, "sh", exclude=p.user_id),
                )
            elif p.role == Role.DOCTOR:
                exclude = p.user_id if p.last_healed == p.user_id else None
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. ДОКТОР</b>\nКого лечим?",
                    kb=kb_targets(alive, "d", exclude=exclude),
                )
            elif p.role == Role.BODYGUARD:
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. ТЕЛОХРАНИТЕЛЬ</b>\nКого прикрыть?",
                    kb=kb_targets(alive, "g", exclude=p.user_id),
                )
            elif p.role == Role.ALCHEMIST:
                if p.has_save or p.has_silence:
                    st = f"💚 {'✅' if p.has_save else '❌'} | 🤫 {'✅' if p.has_silence else '❌'}"
                    send(p.user_id, f"🌙 <b>НОЧЬ #{g.night}. АЛХИМИК</b>\n{st}", kb=kb_alch())
                else:
                    send(p.user_id, "🧪 Зелья кончились.")
            elif p.role == Role.MISTRESS:
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. ЛЮБОВНИЦА</b>",
                    kb=kb_targets(alive, "mi", exclude=p.user_id),
                )
            elif p.role == Role.WITNESS:
                send(
                    p.user_id,
                    f"🌙 <b>НОЧЬ #{g.night}. СВИДЕТЕЛЬ</b>",
                    kb=kb_targets(alive, "w", exclude=p.user_id),
                )
            elif p.role == Role.LAWYER:
                if p.has_cancel:
                    send(
                        p.user_id,
                        f"🌙 <b>НОЧЬ #{g.night}. АДВОКАТ</b>\nКого защитить от казни завтра?",
                        kb=kb_targets(alive, "l", exclude=p.user_id),
                    )
                else:
                    send(p.user_id, "⚖️ Право отмены использовано.")
        except Exception as e:
            log.error(f"night {p.user_id}: {e}")

    g.timer = threading.Timer(timeout, night_timeout, args=[g.chat_id])
    g.timer.daemon = True
    g.timer.start()


def build_commissar_kb(alive, comm):
    m = types.InlineKeyboardMarkup()
    for p in alive:
        if p.user_id == comm.user_id:
            continue
        m.add(types.InlineKeyboardButton(f"🔎 {p.plain()}", callback_data=f"n:c:{p.user_id}"))
    if not comm.has_shot:
        for p in alive:
            if p.user_id == comm.user_id:
                continue
            m.add(types.InlineKeyboardButton(f"🔫 Убить {p.plain()}", callback_data=f"n:cs:{p.user_id}"))
    m.add(types.InlineKeyboardButton("❌ Пропустить", callback_data="n:c:0"))
    return m


def build_sheriff_kb2(alive, exclude):
    m = types.InlineKeyboardMarkup()
    for p in alive:
        if p.user_id == exclude:
            continue
        m.add(types.InlineKeyboardButton(f"🎯 {p.plain()}", callback_data=f"n:sh2:{p.user_id}"))
    m.add(types.InlineKeyboardButton("❌ Пропустить вторую проверку", callback_data="n:sh2:0"))
    return m


def night_timeout(chat_id):
    try:
        _night_timeout_impl(chat_id)
    except Exception as e:
        log.exception(f"night_timeout {chat_id}: {e}")


def _night_timeout_impl(chat_id):
    with GAMES_LOCK:
        g = GAMES.get(chat_id)
    if not g or g.phase != Phase.NIGHT:
        return
    send_game(g, "⏰ Ночь истекла.")
    resolve_night(g)


def all_moves_done(g):
    for p in g.alive():
        r = p.role
        if r in (Role.DON, Role.CUTTHROAT) and p.target_kill is None:
            return False
        if r == Role.COMMISSAR and p.target is None and p.target_check is None and p.commissar_shot is None:
            return False
        if r == Role.SHERIFF and p.extra.get("sheriff_used", 0) == 0:
            return False
        if r == Role.DOCTOR and p.target is None:
            return False
        if r == Role.BODYGUARD and p.target is None:
            return False
        if r == Role.MISTRESS and p.target is None:
            return False
        if r == Role.WITNESS and p.target is None:
            return False
    return True


def resolve_target_id(raw):
    if raw == "0":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def night_action(call):
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) < 3:
        bot.answer_callback_query(call.id)
        return
    action = parts[1]
    target_id = resolve_target_id(parts[2])
    param = parts[3] if len(parts) > 3 else ""

    with GAMES_LOCK:
        g = next((x for x in GAMES.values() if uid in x.players), None)
    if not g or g.phase != Phase.NIGHT:
        bot.answer_callback_query(call.id, "Ночь кончилась.", show_alert=True)
        return

    with g.lock:
        p = g.get(uid)
        if not p or not p.alive:
            bot.answer_callback_query(call.id, "Мёртвые не ходят.", show_alert=True)
            return

        allowed = {
            "k": (Role.DON, Role.CUTTHROAT),
            "dc": (Role.DON,),
            "c": (Role.COMMISSAR,),
            "cs": (Role.COMMISSAR,),
            "sh": (Role.SHERIFF,),
            "sh2": (Role.SHERIFF,),
            "d": (Role.DOCTOR,),
            "g": (Role.BODYGUARD,),
            "mi": (Role.MISTRESS,),
            "w": (Role.WITNESS,),
            "al": (Role.ALCHEMIST,),
            "l": (Role.LAWYER,),
        }
        if action not in allowed or p.role not in allowed[action]:
            bot.answer_callback_query(call.id, "Не твоя кнопка.", show_alert=True)
            return

        tp = g.get(target_id) if target_id else None
        alive = g.alive()

        if action == "k":
            p.target_kill = target_id
            edit(call, f"🔫 Цель: {tp.disp() if tp else 'Пропуск'}")
            for m in alive:
                if m.role in (Role.DON, Role.CUTTHROAT) and m.user_id != uid:
                    send(m.user_id, f"👥 {p.disp()} выбрал: {tp.disp() if tp else '—'}")

        elif action == "dc":
            p.target_check = target_id
            p.don_can_check = False
            if tp:
                is_comm = tp.role == Role.COMMISSAR
                edit(call, f"🎩 {tp.disp()}: {'🚨 КОМИССАР!' if is_comm else '❌ Не комиссар.'}")
            else:
                edit(call, "Пропущено.")

        elif action == "c":
            p.target = target_id
            p.target_check = target_id
            if tp:
                is_mafia = tp.role and ROLES[tp.role][1] == Team.MAFIA
                edit(call, f"🕵️ {tp.disp()}: {'🩸 МАФИЯ!' if is_mafia else '🕊️ Мирный.'}")
            else:
                edit(call, "Пропущено.")

        elif action == "sh":
            used = p.extra.get("sheriff_used", 0)
            if used == 0:
                p.target_check = target_id
                p.extra["sheriff_used"] = 1
                if tp:
                    is_mafia = tp.role and ROLES[tp.role][1] == Team.MAFIA
                    edit(call, f"🤠 {tp.disp()}: {'🩸 МАФИЯ!' if is_mafia else '🕊️ Мирный.'}")
                    kb = build_sheriff_kb2(alive, p.user_id)
                    try:
                        with SEND_LOCK:
                            bot.send_message(uid, "🤠 <b>Вторая проверка</b> (можно пропустить):", reply_markup=kb)
                    except Exception as e:
                        log.debug(f"sheriff 2nd: {e}")
                else:
                    p.extra["sheriff_used"] = 2
                    edit(call, "Пропущено.")
            else:
                p.extra["sheriff_used"] = 2
                edit(call, "Ты уже сделал ход.")

        elif action == "sh2":
            if p.extra.get("sheriff_used", 0) >= 2:
                bot.answer_callback_query(call.id, "Вторая проверка уже закрыта.")
                return
            p.target_check2 = target_id
            p.extra["sheriff_used"] = 2
            if tp:
                is_mafia = tp.role and ROLES[tp.role][1] == Team.MAFIA
                edit(call, f"🤠 {tp.disp()}: {'🩸 МАФИЯ!' if is_mafia else '🕊️ Мирный.'}")
            else:
                edit(call, "Вторая проверка пропущена.")

        elif action == "cs":
            if p.has_shot:
                bot.answer_callback_query(call.id, "Пуля потрачена.", show_alert=True)
                return
            p.has_shot = True
            p.commissar_shot = target_id
            p.target = target_id
            if tp:
                edit(call, f"🔫 Ты целишься в {tp.disp()}. Результат узнаешь утром.")
            else:
                edit(call, "Пропущено.")

        elif action == "d":
            p.target = target_id
            p.last_healed = target_id
            edit(call, f"🩺 Лечишь: {tp.disp() if tp else '—'}")

        elif action == "g":
            p.target = target_id
            edit(call, f"🛡️ Защищаешь: {tp.disp() if tp else '—'}")

        elif action == "mi":
            p.target = target_id
            edit(call, f"💋 Идёшь к: {tp.disp() if tp else '—'}")

        elif action == "w":
            p.target = target_id
            edit(call, f"👀 Следишь: {tp.disp() if tp else '—'}")

        elif action == "l":
            p.lawyer_target = target_id
            edit(call, f"⚖️ Защищаешь: {tp.disp() if tp else '—'}")

        elif action == "al":
            p.target = target_id
            p.alch_potion = param
            if param == "save":
                p.has_save = False
                edit(call, f"💚 Спасение → {tp.disp() if tp else '—'}")
            elif param == "silence":
                p.has_silence = False
                edit(call, f"🤫 Молчание → {tp.disp() if tp else '—'}")

        done = all_moves_done(g)

    bot.answer_callback_query(call.id)

    if done and action != "sh":
        if g.phase == Phase.NIGHT:
            g.cancel_timers()
            resolve_night(g)


def alch_choose(call):
    uid = call.from_user.id
    with GAMES_LOCK:
        g = next((x for x in GAMES.values() if uid in x.players), None)
    if not g or g.phase != Phase.NIGHT:
        bot.answer_callback_query(call.id)
        return
    with g.lock:
        p = g.get(uid)
        if not p or not p.alive or p.role != Role.ALCHEMIST:
            bot.answer_callback_query(call.id)
            return
        alive = g.alive()

    parts = call.data.split(":")
    if len(parts) < 2:
        bot.answer_callback_query(call.id)
        return
    potion = parts[1]
    if potion == "save":
        if not p.has_save:
            bot.answer_callback_query(call.id, "Израсходовано!", show_alert=True)
            return
        kb = kb_targets(alive, "al", param="save")
        text = "💚 Кого лечим?"
    elif potion == "silence":
        if not p.has_silence:
            bot.answer_callback_query(call.id, "Израсходовано!", show_alert=True)
            return
        kb = kb_targets(alive, "al", param="silence", exclude=p.user_id)
        text = "🤫 Кого лишаем голоса?"
    else:
        bot.answer_callback_query(call.id)
        return

    with SEND_LOCK:
        try:
            bot.edit_message_text(text, chat_id=uid, message_id=call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            log.debug(f"alch edit: {e}")
    bot.answer_callback_query(call.id)


def resolve_night(g):
    with g.lock:
        if g.phase != Phase.NIGHT or g.resolving:
            return
        g.resolving = True
        g.cancel_timers()

    try:
        _resolve_night_impl(g)
    finally:
        with g.lock:
            g.resolving = False


def _resolve_night_impl(g):
    alive = g.alive()
    deaths = []
    witness_reports = []

    mistresses = [p for p in alive if p.role == Role.MISTRESS and p.target]
    blocked = set()
    for m in mistresses:
        blocked.add(m.target)
        tp = g.get(m.target)
        if tp:
            tp.target = None
            tp.target_kill = None
            tp.target_check = None
            tp.target_check2 = None
            tp.commissar_shot = None
            tp.alch_potion = None
            tp.lawyer_target = None
            send(tp.user_id, "💋 Любовница была у тебя. Ход пропущен.")

    doctor = next((p for p in alive if p.role == Role.DOCTOR), None)
    if doctor and doctor.user_id not in blocked and doctor.target:
        tp = g.get(doctor.target)
        if tp:
            tp.protected = True

    for alch in [p for p in alive if p.role == Role.ALCHEMIST]:
        if alch.user_id not in blocked and alch.alch_potion == "save" and alch.target:
            tp = g.get(alch.target)
            if tp:
                tp.protected = True

    bodyguard = next((p for p in alive if p.role == Role.BODYGUARD), None)
    bg_target = None
    if bodyguard and bodyguard.user_id not in blocked:
        bg_target = bodyguard.target

    for p in alive:
        if p.role != Role.COMMISSAR or not p.commissar_shot:
            continue
        if p.user_id in blocked:
            continue
        shot_target = g.get(p.commissar_shot)
        if not shot_target or not shot_target.alive:
            send(p.user_id, "🔫 Твоя цель к утру уже была мертва. Выстрел ушёл в пустоту.")
            continue
        if shot_target.protected:
            send(p.user_id, f"🔫 Ты выстрелил в {shot_target.disp()}, но его спасли этой ночью.")
            continue
        if shot_target.role and ROLES[shot_target.role][1] == Team.MAFIA:
            shot_target.alive = False
            if shot_target not in deaths:
                deaths.append(shot_target)
            send(p.user_id, f"🔫 Точное попадание! {shot_target.disp()} — мафия.")
            send_game(g, f"🔫 <b>НОЧНОЙ ВЫСТРЕЛ!</b> {shot_target.disp()} найден мёртвым.")
        else:
            send(p.user_id, f"🔫 Ты выстрелил в {shot_target.disp()}, но он оказался мирным.")

    mafia = [p for p in alive if p.role in (Role.DON, Role.CUTTHROAT)]
    don = next((p for p in mafia if p.role == Role.DON), None)
    kill_target = None
    killer_name = "Мафия"

    if don and don.user_id not in blocked and don.target_kill:
        kill_target = don.target_kill
        killer_name = f"Дон {don.plain()}"
    else:
        votes = [m.target_kill for m in mafia if m.user_id not in blocked and m.target_kill]
        if votes:
            kill_target = max(set(votes), key=votes.count)
            for m in mafia:
                if m.user_id not in blocked and m.target_kill == kill_target:
                    killer_name = m.plain()
                    break

    if kill_target:
        victim = g.get(kill_target)
        if victim and victim.alive:
            if bodyguard and bg_target == victim.user_id:
                bodyguard.alive = False
                deaths.append(bodyguard)
                for w in [p for p in alive if p.role == Role.WITNESS and p.user_id not in blocked]:
                    if w.target == victim.user_id:
                        witness_reports.append((w.user_id, killer_name))
            elif not victim.protected:
                victim.alive = False
                deaths.append(victim)
                for w in [p for p in alive if p.role == Role.WITNESS and p.user_id not in blocked]:
                    if w.target == victim.user_id:
                        witness_reports.append((w.user_id, killer_name))

    for alch in [p for p in alive if p.role == Role.ALCHEMIST]:
        if alch.user_id not in blocked and alch.alch_potion == "silence" and alch.target:
            tp = g.get(alch.target)
            if tp and tp.alive:
                tp.silenced = True

    for uid, kname in witness_reports:
        send(uid, f"👀 <b>ТЫ ВИДЕЛ УБИЙЦУ!</b>\nУбил: <b>{esc(kname)}</b>!")

    with g.lock:
        g.phase = Phase.VOTING
        g.day += 1

    header = f"☀️ <b>УТРО. ДЕНЬ #{g.day}</b>\n\n"
    parts = []
    if deaths:
        for d in deaths:
            name, team, emoji, _ = ROLES[d.role]
            parts.append(f"☠️ {d.disp()} {random.choice(DEATHS)}\nРоль: <b>{emoji} {name}</b> ({team.value})")
    else:
        parts.append("🕊️ Ночь прошла спокойно.")

    silenced = [p.disp() for p in g.alive() if p.silenced]
    if silenced:
        parts.append(f"\n🤫 Замолкнуты: {', '.join(silenced)}")

    send_game(g, header + "\n\n".join(parts))

    for d in deaths:
        if not d.said_last_words:
            try:
                with SEND_LOCK:
                    bot.send_message(d.user_id, "💀 Ты погиб. Напиши <code>/say текст</code>, чтобы отправить послание в чат. Одно за игру.")
            except Exception as e:
                log.debug(f"death notify: {e}")

    if g.moderator_id and g.moderator_id in g.players:
        mod = g.players[g.moderator_id]
        if mod.role:
            log_text = "\n".join(
                f"• {x.disp()} — {ROLES[x.role][2]} {ROLES[x.role][0]}"
                for x in g.players.values() if x.role
            )
            try:
                with SEND_LOCK:
                    bot.send_message(mod.user_id, f" 📋 <b>Лог ролей:</b>\n{log_text}")
            except Exception as e:
                log.debug(f"mod log: {e}")

    if check_win(g):
        return
    start_voting(g)


def start_voting(g):
    with g.lock:
        g.phase = Phase.VOTING
        g.votes.clear()
        g.cancel_timers()
        alive = g.alive()

    send_game(
        g,
        "⚖️ <b>СУД ЛИНЧА!</b>\nГолосуйте кнопками.\n"
        "<i>Мёртвые и замолкнутые не голосуют.</i>\n"
        f"⏳ {VOTE_TIMEOUT} сек.",
        kb=kb_vote(alive),
    )
    g.timer = threading.Timer(VOTE_TIMEOUT, vote_timeout, args=[g.chat_id])
    g.timer.daemon = True
    g.timer.start()


def vote_timeout(chat_id):
    try:
        _vote_timeout_impl(chat_id)
    except Exception as e:
        log.exception(f"vote_timeout {chat_id}: {e}")


def _vote_timeout_impl(chat_id):
    with GAMES_LOCK:
        g = GAMES.get(chat_id)
    if not g or g.phase != Phase.VOTING:
        return
    send_game(g, "⏰ Голосование истекло.")
    finalize_voting(g)


def vote(call):
    uid = call.from_user.id
    g = GAMES.get(call.message.chat.id)
    if not g or g.phase != Phase.VOTING:
        bot.answer_callback_query(call.id, "Не сейчас.", show_alert=True)
        return

    parts = call.data.split(":")
    if len(parts) < 2:
        bot.answer_callback_query(call.id)
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        bot.answer_callback_query(call.id)
        return

    with g.lock:
        voter = g.get(uid)
        if not voter or not voter.alive:
            bot.answer_callback_query(call.id, "Мертвецы не голосуют.", show_alert=True)
            return
        if voter.silenced:
            bot.answer_callback_query(call.id, "🤫 Ты под Молчанием!", show_alert=True)
            return
        tp = g.get(target_id)
        if not tp or not tp.alive:
            bot.answer_callback_query(call.id, "Его уже нет.", show_alert=True)
            return
        if g.votes.get(uid) == target_id:
            bot.answer_callback_query(call.id, "Уже так голосовал.")
            return
        g.votes[uid] = target_id
        eligible_count = sum(1 for p in g.alive() if not p.silenced)
        votes_count = len(g.votes)

    bot.answer_callback_query(call.id, f"Голос за {tp.plain()}")
    send_game(g, f"🗳️ {voter.disp()} → {tp.disp()}")

    if votes_count >= eligible_count:
        g.cancel_timers()
        finalize_voting(g)


def finalize_voting(g):
    with g.lock:
        if g.phase != Phase.VOTING:
            return
        g.cancel_timers()

    if not g.votes:
        send_game(g, "🤷 Никто не голосовал. Казни нет.")
        prepare_next_night(g)
        return

    counts = {}
    for tid in g.votes.values():
        counts[tid] = counts.get(tid, 0) + 1

    lines = [f"• {g.get(t).disp()}: <b>{c}</b>" for t, c in sorted(counts.items(), key=lambda x: -x[1])]
    send_game(g, "📊 <b>Итоги:</b>\n" + "\n".join(lines))

    sorted_v = sorted(counts.items(), key=lambda x: -x[1])
    top = sorted_v[0][1]
    leaders = [t for t, c in sorted_v if c == top]

    if len(leaders) > 1:
        send_game(g, "⚖️ Ничья. Казни нет.")
        prepare_next_night(g)
        return

    lynched_id = leaders[0]
    lynched = g.get(lynched_id)

    lawyer = next((p for p in g.alive() if p.role == Role.LAWYER and p.has_cancel), None)
    if lawyer and lawyer.lawyer_target == lynched_id:
        lawyer.has_cancel = False
        send_game(g, f"⚖️ <b>АДВОКАТ ВМЕШАЛСЯ!</b> Казнь {lynched.disp()} отменена.")
        prepare_next_night(g)
        return

    if lynched and lynched.alive:
        lynched.alive = False
        role_info = ROLES[lynched.role]
        send_game(
            g,
            "⚡ <b>КАЗНЬ:</b> " + random.choice(LYNCH).format(n=esc(lynched.plain())) +
            f"\nРоль: <b>{role_info[2]} {role_info[0]}</b>",
        )
        if lynched.role == Role.PSYCHO:
            accusers = [
                g.get(v) for v, t in g.votes.items()
                if t == lynched_id and g.get(v) and g.get(v).alive
            ]
            if accusers:
                victim = random.choice(accusers)
                victim.alive = False
                send_game(g, f"🪓 <b>ПСИХ СОШЁЛ С УМА!</b> Утащил {victim.disp()} с собой!")

    if check_win(g):
        return
    prepare_next_night(g)


def prepare_next_night(g):
    with g.lock:
        for p in g.players.values():
            p.silenced = False
        g.night += 1
        g.phase = Phase.NIGHT
    send_game(g, f"🌙 <b>НОЧЬ #{g.night}...</b>")
    start_night(g)


def check_win(g):
    mafia = g.by_team(Team.MAFIA)
    city = g.by_team(Team.CITY)
    psycho = g.by_team(Team.NEUTRAL)
    alive_total = len(g.alive())

    winner = None
    if alive_total == 1 and psycho:
        winner = Team.NEUTRAL
    elif not mafia and not psycho:
        winner = Team.CITY
    elif not mafia and psycho and not city:
        winner = Team.NEUTRAL
    elif mafia and len(mafia) >= len(city) + len(psycho):
        winner = Team.MAFIA

    if not winner:
        return False

    with g.lock:
        g.phase = Phase.ENDED
        g.cancel_timers()

    reward = 150
    for p in g.players.values():
        is_winner = p.role and ROLES[p.role][1] == winner
        add_win(p.user_id, p.name, bool(is_winner))

    if winner == Team.CITY:
        banner = "🏆 <b>ГОРОД ПОБЕДИЛ!</b> 🕊️"
    elif winner == Team.MAFIA:
        banner = "🩸 <b>МАФИЯ ПОБЕДИЛА!</b> 🎩🔫"
    else:
        banner = "🪓 <b>ПСИХ ПОБЕДИЛ!</b> Город опустел..."

    roster = []
    for p in g.players.values():
        if p.role:
            n, t, e, _ = ROLES[p.role]
            state = "❤️" if p.alive else "💀"
            bonus = f"+{reward}🪙" if t == winner else ""
            roster.append(f"• {p.disp()} — {e} {n} [{state}] {bonus}")
    txt = (
        f"{banner}\n\n📋 <b>Итоги партии:</b>\n" + "\n".join(roster) +
        "\n\n🔁 <code>/reg</code> — новая партия."
    )
    send_game(g, txt, kb=kb_reg())

    with GAMES_LOCK:
        GAMES.pop(g.chat_id, None)
    return True


def is_restricted(message):
    if message.chat.type not in ("group", "supergroup"):
        return False
    if not message.from_user:
        return False
    if message.text and message.text.startswith(("/", "!")):
        return False
    with GAMES_LOCK:
        g = GAMES.get(message.chat.id)
    if not g or g.phase == Phase.ENDED:
        return False
    if not g.mute_enabled:
        return False

    if is_chat_admin(message.chat.id, message.from_user.id):
        return False

    p = g.get(message.from_user.id)
    if not p:
        return True
    if not p.alive:
        return True
    if g.phase == Phase.LOBBY:
        return False
    if g.phase == Phase.NIGHT:
        return True
    if p.silenced and g.phase == Phase.VOTING:
        return True
    return False


@bot.message_handler(
    func=is_restricted,
    content_types=["text", "audio", "document", "photo", "sticker", "video", "voice"],
)
def chat_guard(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        log.debug(f"delete: {e}")


@bot.message_handler(commands=["say"])
def cmd_say(message):
    if message.chat.type != "private":
        send(message.chat.id, "Команда только в ЛС боту.")
        return
    uid = message.from_user.id
    g = next((x for x in GAMES.values() if uid in x.players), None)
    if not g:
        send(uid, "Ты не в активной игре.")
        return
    p = g.get(uid)
    if not p or p.alive:
        send(uid, "Только мёртвые могут отправлять послания.")
        return
    if p.said_last_words:
        send(uid, "Ты уже отправлял послание.")
        return
    text = message.text[len("/say"):].strip()
    if not text:
        send(uid, "Использование: /say текст")
        return
    if len(text) > 300:
        text = text[:300] + "..."
    p.said_last_words = True
    send_game(g, f"💀 <b>ПРЕДСМЕРТНОЕ ПОСЛАНИЕ</b> от {p.disp()}:\n<i>{esc(text)}</i>")
    send(uid, "Послание отправлено.")


@bot.message_handler(commands=["m"])
def cmd_mafia_chat(message):
    if message.chat.type != "private":
        send(message.chat.id, "Только в ЛС боту.")
        return
    uid = message.from_user.id
    g = next((x for x in GAMES.values() if uid in x.players), None)
    if not g or g.phase != Phase.NIGHT:
        send(uid, "Сейчас нельзя.")
        return
    p = g.get(uid)
    if not p or not p.alive or p.role not in (Role.DON, Role.CUTTHROAT):
        send(uid, "Только мафия и только ночью.")
        return
    text = message.text[len("/m"):].strip()
    if not text:
        send(uid, "Использование: /m текст")
        return
    if len(text) > 300:
        text = text[:300] + "..."
    for m in g.alive():
        if m.role in (Role.DON, Role.CUTTHROAT) and m.user_id != uid:
            send(m.user_id, f"🎩 <b>{esc(p.plain())}:</b> {esc(text)}")
    send(uid, "Отправлено союзникам.")


@bot.message_handler(commands=["top"])
def cmd_top(message):
    rows = top_players(10)
    if not rows:
        send(message.chat.id, "Статистики пока нет.")
        return
    lines = []
    for i, (name, wins, games) in enumerate(rows):
        lines.append(f"{i+1}. <b>{esc(name)}</b> — {wins}/{games}")
    send(message.chat.id, "🏆 <b>ТОП-10 ИГРОКОВ</b>\n\n" + "\n".join(lines))


@bot.message_handler(commands=["reg"])
def cmd_reg(message):
    register(message.chat.id, message.from_user, message=message)


@bot.message_handler(commands=["start_game"])
def cmd_start_game(message):
    start_game(message.chat.id, message=message)


@bot.message_handler(commands=["stop_game"])
def cmd_stop(message):
    with GAMES_LOCK:
        g = GAMES.get(message.chat.id)
    if not g:
        send(message.chat.id, "Нет активной игры.")
        return
    g.cancel_timers()
    with GAMES_LOCK:
        GAMES.pop(message.chat.id, None)
    send(message.chat.id, "🛑 Игра остановлена. /reg — новая.")


@bot.message_handler(commands=["status"])
def cmd_status(message):
    with GAMES_LOCK:
        g = GAMES.get(message.chat.id)
    if not g:
        send(message.chat.id, "Игры нет. /reg")
        return
    if g.phase == Phase.LOBBY:
        send(message.chat.id, f"Лобби: {len(g.players)} чел. /reg")
        return
    alive = [f"• {p.disp()}{' 🤫' if p.silenced else ''}" for p in g.alive()]
    dead = [f"• 💀 {p.disp()}" for p in g.players.values() if not p.alive]
    send(
        message.chat.id,
        f"Фаза: <b>{g.phase.value}</b> | Ночь {g.night} | День {g.day}\n\n"
        f"❤️ Живых ({len(alive)}):\n" + ("\n".join(alive) or "—") +
        f"\n\n💀 Мёртвых ({len(dead)}):\n" + ("\n".join(dead) or "—"),
    )


@bot.message_handler(commands=["help", "start"])
def cmd_help(message):
    send(
        message.chat.id,
        "🏙️ <b>МАФИЯ: КРОВАВЫЙ ГОРОД</b>\n\n"
        "<code>/reg</code> — присоединиться\n"
        "<code>/start_game</code> — начать\n"
        "<code>/status</code> — состояние\n"
        "<code>/stop_game</code> — стоп\n"
        "<code>/top</code> — таблица лидеров\n"
        "<code>/say текст</code> (ЛС, мёртвым) — послание в чат\n"
        "<code>/m текст</code> (ЛС, мафии ночью) — чат мафии\n\n"
        "<i>Мут: с момента старта лобби пишут только игроки. Ночью — никто, кроме мафии в ЛС.</i>",
    )


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    d = call.data or ""
    try:
        if d == "r":
            register(call.message.chat.id, call.from_user, message=call.message, call_id=call.id)
        elif d == "s":
            start_game(call.message.chat.id, message=call.message, call_id=call.id)
        elif d.startswith("v:"):
            vote(call)
        elif d.startswith("a:"):
            alch_choose(call)
        elif d.startswith("n:"):
            night_action(call)
        else:
            bot.answer_callback_query(call.id)
    except (IndexError, ValueError) as e:
        log.warning(f"callback parse: {e} | data={d!r}")
        try:
            bot.answer_callback_query(call.id, "Кнопка устарела.", show_alert=False)
        except Exception:
            pass
    except Exception as e:
        log.exception(f"callback: {e} | data={d!r}")
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass


def cleanup_sessions():
    while True:
        try:
            time.sleep(300)
            now = time.time()
            with GAMES_LOCK:
                for cid, g in list(GAMES.items()):
                    if g.phase == Phase.ENDED or now - g.lobby_start > 3600:
                        g.cancel_timers()
                        GAMES.pop(cid, None)
            with MEMBER_CACHE_LOCK:
                for k, (_, t) in list(MEMBER_CACHE.items()):
                    if now - t > 120:
                        MEMBER_CACHE.pop(k, None)
        except Exception as e:
            log.exception(f"cleanup: {e}")


def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        log.error("BOT_TOKEN не задан или неверный. Проверь .env")
        return
    init_db()
    threading.Thread(target=cleanup_sessions, daemon=True).start()
    try:
        me = bot.get_me()
        log.info(f"Bot @{me.username}")
        bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.error(f"startup: {e}")
        return
    bot.infinity_polling(skip_pending=True, timeout=30)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped")
