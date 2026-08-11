import os
import uuid
import random
import logging
import sqlite3
from contextlib import closing

from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# الإعدادات
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "roulette.db")
MAX_PLAYERS = 10
JOIN_TRIGGER = "بوك"  # طريقة نصية بديلة (احتياطية) عن زر Join

JOIN_CALLBACK = "roulette_join"
SPIN_CALLBACK = "roulette_spin"

GAME_TITLE = "🎁🎰 روليت هدايا تلغرام"

ROUND_ACTIVE_WARNING = (
    "⚠️ توجد جولة نشطة حالياً في هذا الكروب. "
    "أنهوها أو استخدموا /reset قبل بدء جولة جديدة."
)

SPIN_ONLY_STARTER_WARNING = "🚫 بس الشخص اللي بدأ هذي الجولة يقدر يلف الروليت."

# نص مؤقت يظهر لحظة اختيار خيار الإنلاين، قبل ما يرسل البوت البطاقة التفاعلية الحقيقية.
# رسائل الإنلاين لا تدعم أزرار تفاعلية موثوقة داخل الكروبات، لذلك نستخدمها فقط
# "كمفتاح تشغيل" يكتشفه البوت ليرسل بعدها البطاقة الحقيقية القابلة للتفاعل.
INLINE_PLACEHOLDER_TEXT = "🎁🎰 جاري تجهيز جولة روليت هدايا تلغرام..."


# ---------------------------------------------------------------------------
# طبقة قاعدة البيانات (SQLite)
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                first_name  TEXT,
                join_count  INTEGER DEFAULT 1,
                eliminated  INTEGER DEFAULT 0,
                joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS game_state (
                chat_id          INTEGER PRIMARY KEY,
                status           TEXT DEFAULT 'waiting',
                round_message_id INTEGER,
                starter_user_id  INTEGER
            )
            """
        )
        conn.commit()


def get_game_state(chat_id: int) -> dict:
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT status, round_message_id, starter_user_id FROM game_state WHERE chat_id=?",
            (chat_id,),
        )
        row = c.fetchone()
        if row:
            return {"status": row[0], "message_id": row[1], "starter_user_id": row[2]}
        return {"status": "waiting", "message_id": None, "starter_user_id": None}


def set_game_state(chat_id: int, status: str = None, message_id: int = None, starter_user_id: int = None) -> None:
    current = get_game_state(chat_id)
    new_status = status if status is not None else current["status"]
    new_message_id = message_id if message_id is not None else current["message_id"]
    new_starter_user_id = starter_user_id if starter_user_id is not None else current["starter_user_id"]
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO game_state (chat_id, status, round_message_id, starter_user_id) VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                status=excluded.status,
                round_message_id=excluded.round_message_id,
                starter_user_id=excluded.starter_user_id
            """,
            (chat_id, new_status, new_message_id, new_starter_user_id),
        )
        conn.commit()


def get_status(chat_id: int) -> str:
    return get_game_state(chat_id)["status"]


def set_status(chat_id: int, status: str) -> None:
    set_game_state(chat_id, status=status)


def set_round_message(chat_id: int, message_id: int) -> None:
    set_game_state(chat_id, message_id=message_id)


def set_round_starter(chat_id: int, user_id: int) -> None:
    set_game_state(chat_id, starter_user_id=user_id)


def get_round_starter(chat_id: int):
    return get_game_state(chat_id)["starter_user_id"]


def reset_round_data(chat_id: int) -> None:
    """يمسح لاعبي وحالة هذا الكروب فقط ويبدأ حالة 'انتظار' جديدة نظيفة."""
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM players WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM game_state WHERE chat_id=?", (chat_id,))
        conn.commit()
    set_status(chat_id, "waiting")


def display_name(username: str, first_name: str) -> str:
    """يعيد @username إن وجد، وإلا الاسم الأول، بدون كشف أي آيدي رقمي."""
    if username:
        return f"@{username}"
    return first_name or "لاعب"


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Join | انضمام", callback_data=JOIN_CALLBACK)],
            [InlineKeyboardButton("🎰 Spin | لف الروليت", callback_data=SPIN_CALLBACK)],
        ]
    )


def build_round_message_text(chat_id: int) -> str:
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT username, first_name, eliminated FROM players WHERE chat_id=? ORDER BY joined_at",
            (chat_id,),
        )
        rows = c.fetchall()

    lines = [
        GAME_TITLE,
        "━━━━━━━━━━━━━━━━",
        f"👥 المشاركون: {len(rows)}/{MAX_PLAYERS}",
        "",
    ]

    if rows:
        for i, (username, first_name, eliminated) in enumerate(rows, start=1):
            name = display_name(username, first_name)
            marker = " ❌ مُقصى" if eliminated else " 🟢"
            lines.append(f"{i}. {name}{marker}")
    else:
        lines.append("لا يوجد مشاركون بعد.")
        lines.append("اضغط ✅ Join للمشاركة!")

    return "\n".join(lines)


async def refresh_round_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, keep_keyboard: bool = True):
    """يحدّث بطاقة الجولة (النص وقائمة اللاعبين) إن كانت محفوظة، بدون رفع خطأ للمستخدم إذا فشل."""
    state = get_game_state(chat_id)
    message_id = state["message_id"]
    if not message_id:
        return
    text = build_round_message_text(chat_id)
    markup = build_keyboard() if keep_keyboard else None
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup
        )
    except Exception:
        # رسالة "لم يتغير شيء" أو رسالة قديمة محذوفة — نتجاهلها بأمان
        pass


# ---------------------------------------------------------------------------
# منطق الانضمام (مشترك بين /join، كلمة "بوك"، وزر Join)
# ---------------------------------------------------------------------------
def join_player(chat_id: int, user) -> dict:
    """ينفذ منطق الانضمام. يرجع dict فيه kind (joined/repeat_warn/repeat_spam/loser/locked/finished/full) ونص."""
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT join_count, eliminated FROM players WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        )
        row = c.fetchone()

        if row is not None and row[1] == 1:
            return {"kind": "loser", "text": "انت خسرت يا فاشل"}

        status = get_status(chat_id)

        if status == "finished":
            return {
                "kind": "finished",
                "text": "الجولة السابقة انتهت. استخدم /reset لبدء جولة جديدة قبل الانضمام.",
            }

        if status == "active" and row is None:
            return {
                "kind": "locked",
                "text": "الجولة بدأت بالفعل ولا يمكن الانضمام الآن. انتظروا الجولة القادمة أو استخدموا /reset.",
            }

        if row is None:
            c.execute("SELECT COUNT(*) FROM players WHERE chat_id=?", (chat_id,))
            count = c.fetchone()[0]
            if count >= MAX_PLAYERS:
                return {"kind": "full", "text": "اكتمل عدد اللاعبين!"}

            c.execute(
                """
                INSERT INTO players (chat_id, user_id, username, first_name, join_count)
                VALUES (?, ?, ?, ?, 1)
                """,
                (chat_id, user.id, user.username, user.first_name),
            )
            conn.commit()

            c.execute("SELECT COUNT(*) FROM players WHERE chat_id=?", (chat_id,))
            new_count = c.fetchone()[0]

            if new_count >= MAX_PLAYERS:
                return {"kind": "joined_full", "text": "✅ تم انضمامك للروليت\n🎉 اكتمل عدد اللاعبين!"}
            return {"kind": "joined", "text": "✅ تم انضمامك للروليت"}
        else:
            new_join_count = row[0] + 1
            c.execute(
                "UPDATE players SET join_count=? WHERE chat_id=? AND user_id=?",
                (new_join_count, chat_id, user.id),
            )
            conn.commit()

            if new_join_count == 2:
                return {"kind": "repeat_warn", "text": "كافي مو لحيت"}
            return {"kind": "repeat_spam", "text": "دطير أنت مشترك أصلاً"}


# الأنواع التي يجب إعلامها فعلاً برسالة نصية عند الانضمام عبر الكتابة (بوك / /join).
# النجاح العادي (joined / joined_full) لا يُعلن كرسالة لأن قائمة اللاعبين بالبطاقة الثابتة تعكسه مباشرة.
TEXT_JOIN_REPLY_KINDS = {"loser", "finished", "locked", "full", "repeat_warn", "repeat_spam"}


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    result = join_player(chat_id, update.effective_user)
    if result["kind"] in TEXT_JOIN_REPLY_KINDS:
        await update.message.reply_text(result["text"])
    await refresh_round_message(context, chat_id)


# ---------------------------------------------------------------------------
# منطق السبين (مشترك بين /spin وزر Spin)
# ---------------------------------------------------------------------------
def perform_spin(chat_id: int) -> dict:
    """ينفذ خطوة سبين واحدة. يرجع dict فيه kind ونصوص الإقصاء/الفائز عند الحاجة."""
    status = get_status(chat_id)
    if status == "finished":
        return {"kind": "already_finished"}

    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT user_id, username, first_name, eliminated FROM players WHERE chat_id=?",
            (chat_id,),
        )
        rows = c.fetchall()
        active_players = [r for r in rows if r[3] == 0]

        if len(rows) < 2:
            return {"kind": "not_enough"}

        if len(active_players) == 1:
            _, username, first_name, _ = active_players[0]
            set_status(chat_id, "finished")
            return {"kind": "winner", "winner": display_name(username, first_name)}

        set_status(chat_id, "active")

        victim = random.choice(active_players)
        v_user_id, v_username, v_first_name, _ = victim
        c.execute(
            "UPDATE players SET eliminated=1 WHERE chat_id=? AND user_id=?",
            (chat_id, v_user_id),
        )
        conn.commit()

        eliminated_name = display_name(v_username, v_first_name)

        c.execute(
            "SELECT username, first_name FROM players WHERE chat_id=? AND eliminated=0",
            (chat_id,),
        )
        remaining = c.fetchall()

        if len(remaining) == 1:
            winner_username, winner_first_name = remaining[0]
            set_status(chat_id, "finished")
            return {
                "kind": "eliminated_and_winner",
                "eliminated": eliminated_name,
                "winner": display_name(winner_username, winner_first_name),
            }

        return {"kind": "eliminated", "eliminated": eliminated_name}


def render_spin_result(result: dict) -> str:
    kind = result["kind"]
    if kind == "already_finished":
        return "الجولة انتهت بالفعل. استخدم /reset لبدء جولة جديدة."
    if kind == "not_enough":
        return "يجب أن ينضم لاعبان على الأقل قبل بدء الروليت."
    if kind == "winner":
        return f"🏆 مبروك! الفائز بروليت هدايا تلغرام هو {result['winner']} 🎉"
    if kind == "eliminated_and_winner":
        return (
            f"❌ تم إقصاء {result['eliminated']} من الروليت\n"
            f"🏆 مبروك! الفائز بروليت هدايا تلغرام هو {result['winner']} 🎉"
        )
    if kind == "eliminated":
        return f"❌ تم إقصاء {result['eliminated']} من الروليت"
    return ""


async def spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    starter_id = get_round_starter(chat_id)
    if starter_id is not None and update.effective_user.id != starter_id:
        await update.message.reply_text(SPIN_ONLY_STARTER_WARNING)
        return
    result = perform_spin(chat_id)
    await update.message.reply_text(render_spin_result(result))
    keep_keyboard = result["kind"] not in ("winner", "eliminated_and_winner", "already_finished")
    await refresh_round_message(context, chat_id, keep_keyboard=keep_keyboard)


# ---------------------------------------------------------------------------
# بدء جولة جديدة (إنلاين أو /roulette) + الأزرار
# ---------------------------------------------------------------------------
async def handle_round_started(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يُستدعى عند ظهور رسالة "التجهيز" المرسلة عبر اختيار خيار الإنلاين داخل الكروب.
    رسائل الإنلاين لا يمكن أن تحمل أزراراً تفاعلية موثوقة (لأن تيليجرام لا يكشف
    chat_id الحقيقي لأزرار مرسلة عبر الإنلاين)، لذلك بمجرد وصولها كرسالة عادية
    (نراها دائماً بغض النظر عن Privacy Mode) نستخرج منها chat_id الحقيقي
    ونرسل بدلها البطاقة التفاعلية الحقيقية عبر رسالة بوت عادية.
    """
    chat_id = update.effective_chat.id
    status = get_status(chat_id)
    if status == "active":
        await update.message.reply_text(ROUND_ACTIVE_WARNING)
        return

    # كل جولة مرتبطة حصراً بالكروب الذي بدأت فيه (chat_id) — لا تأثير على كروبات أخرى
    reset_round_data(chat_id)

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=build_round_message_text(chat_id),
        reply_markup=build_keyboard(),
    )
    set_round_message(chat_id, sent.message_id)
    set_round_starter(chat_id, update.message.from_user.id)


async def text_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()

    # هذه الرسالة وصلت عبر اختيار خيار الإنلاين "ابدأ روليت" من داخل هذا الكروب.
    # رسائل الإنلاين يراها البوت دائماً بغض النظر عن Privacy Mode.
    if msg.via_bot and context.bot.id == msg.via_bot.id and text == INLINE_PLACEHOLDER_TEXT:
        await handle_round_started(update, context)
        return

    if text == JOIN_TRIGGER:
        chat_id = update.effective_chat.id
        result = join_player(chat_id, update.effective_user)
        if result["kind"] in TEXT_JOIN_REPLY_KINDS:
            await update.message.reply_text(result["text"])
        await refresh_round_message(context, chat_id)


async def roulette_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر احتياطي يقوم بنفس عمل خيار الإنلاين، لاستخدامه إن لم يعمل الإنلاين."""
    chat_id = update.effective_chat.id
    status = get_status(chat_id)
    if status == "active":
        await update.message.reply_text(ROUND_ACTIVE_WARNING)
        return
    reset_round_data(chat_id)
    sent = await update.message.reply_text(build_round_message_text(chat_id), reply_markup=build_keyboard())
    set_round_message(chat_id, sent.message_id)
    set_round_starter(chat_id, update.effective_user.id)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ملاحظة مهمة: لا نُرفق أزراراً بهذه النتيجة، لأن تيليجرام لا يكشف الـ chat_id
    # الحقيقي لأزرار مرسلة عبر الإنلاين (فقط inline_message_id مبهم)، فتظل الأزرار
    # عالقة بدون استجابة. لذلك هذه النتيجة تُرسل فقط كـ"إشارة بدء" نصية بسيطة،
    # والبوت يستبدلها فوراً برسالة عادية فيها البطاقة التفاعلية الحقيقية.
    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="ابدأ روليت هدايا تلغرام",
            description="يبدأ جولة جديدة داخل هذا الكروب فوراً",
            input_message_content=InputTextMessageContent(INLINE_PLACEHOLDER_TEXT),
        )
    ]
    await update.inline_query.answer(results, cache_time=0, is_personal=False)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.message is None:
        # زر قديم/عالق لا يحمل بيانات كروب حقيقية — نرد بأدب بدل ما يعلق بدون رد.
        await query.answer("انتهت صلاحية هذا الزر، ابدأوا جولة جديدة.", show_alert=True)
        return

    chat_id = query.message.chat.id

    if query.data == JOIN_CALLBACK:
        # الانضمام بالزر: رد سريع فوق المحادثة (Popup) فقط — بدون إرسال أي رسالة جديدة بالكروب.
        result = join_player(chat_id, query.from_user)
        await query.answer(text=result["text"], show_alert=False)
        await refresh_round_message(context, chat_id)

    elif query.data == SPIN_CALLBACK:
        starter_id = get_round_starter(chat_id)
        if starter_id is not None and query.from_user.id != starter_id:
            await query.answer(text=SPIN_ONLY_STARTER_WARNING, show_alert=True)
            return

        # الإقصاء: يُرسل كرسالة فعلية بالكروب فيها يوزر الشخص المقصى (وليس Popup فقط).
        result = perform_spin(chat_id)
        await query.answer()  # مجرد تأكيد استلام الضغطة (يلغي علامة التحميل بالزر)
        text = render_spin_result(result)
        if text:
            await context.bot.send_message(chat_id=chat_id, text=text)
        keep_keyboard = result["kind"] not in ("winner", "eliminated_and_winner", "already_finished")
        await refresh_round_message(context, chat_id, keep_keyboard=keep_keyboard)


# ---------------------------------------------------------------------------
# أوامر مساعدة
# ---------------------------------------------------------------------------
async def players_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = build_round_message_text(chat_id)
    await update.message.reply_text(text)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_round_data(chat_id)
    await update.message.reply_text("تم تصفير جولة هذا الكروب. يمكن الانضمام من جديد ✅")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"{GAME_TITLE}\n"
        "(ترفيهي بدون فلوس أو مراهنات)\n\n"
        "لبدء جولة جديدة في الكروب:\n"
        "  • اكتب @اسم_البوت في مربع الكتابة واختر \"ابدأ روليت هدايا تلغرام\" (الطريقة الأساسية)\n"
        "  • أو استخدم /roulette كخيار احتياطي إذا لم يعمل الإنلاين\n\n"
        "تظهر بطاقة ثابتة فيها قائمة المشاركين، وتحتها زرّان:\n"
        "  ✅ Join | انضمام — ينضم مباشرة، ويرد لك تأكيد سريع فوق المحادثة بدون رسالة جديدة\n"
        "  🎰 Spin | لف الروليت — بس اللي بدأ الجولة يقدر يضغطه، ويقصي مشاركاً عشوائياً ويرسل رسالة بالكروب باسمه، حتى يبقى فائز واحد\n\n"
        "بديل نصي (اختياري): /join أو كلمة \"بوك\" للانضمام، و/spin للإقصاء.\n"
        "/players — عرض عدد المشاركين وأسمائهم\n"
        "/reset — تصفير جولة هذا الكروب فقط\n"
    )
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# نقطة التشغيل
# ---------------------------------------------------------------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("يرجى ضبط متغير البيئة BOT_TOKEN قبل التشغيل.")

    init_db()

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", help_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("join", join_command))
    app.add_handler(CommandHandler("players", players_cmd))
    app.add_handler(CommandHandler("spin", spin))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("roulette", roulette_command))  # خيار احتياطي عن الإنلاين

    # وضع الإنلاين: كتابة @اسم_البوت داخل الكروب يظهر خيار "ابدأ روليت هدايا تلغرام"
    app.add_handler(InlineQueryHandler(inline_query))

    # أزرار Join وSpin أسفل البطاقة التفاعلية
    app.add_handler(CallbackQueryHandler(button_handler))

    # يلتقط كلمة "بوك" (بديل نصي)، وكذلك رسالة "التجهيز" المرسلة عبر الإنلاين
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_router))

    logger.info("Bot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
