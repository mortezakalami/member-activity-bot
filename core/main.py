import os
import json
import html
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import telebot
from flask import Flask, request, abort
from telebot.types import ChatMemberUpdated, Update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
log = logging.getLogger("channel-guard-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATA_FILE = os.environ.get("DATA_FILE", "channel_members.json")
TEHRAN = ZoneInfo("Asia/Tehran")

# Render خودش RENDER_EXTERNAL_URL را می‌گذارد
WEBHOOK_HOST = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN تنظیم نشده.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_FULL_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

app = Flask(__name__)
_data_lock = threading.Lock()


def load_data():
    raw = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Data file is corrupted, starting fresh: %s", e)
            raw = {}
    raw.setdefault("titles", {})
    raw.setdefault("channels", {})
    raw.setdefault("receivers", {})
    return raw


def save_data(data):
    folder = os.path.dirname(os.path.abspath(DATA_FILE))
    if folder:
        os.makedirs(folder, exist_ok=True)
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.error("Failed to save data: %s", e)


def known_channel_keys(data):
    return set(data["titles"]) | set(data["channels"]) | set(data["receivers"])


def channel_title(data, chat_key):
    return data["titles"].get(chat_key) or f"کانال {chat_key}"


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانیه"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} دقیقه"

    hours = minutes // 60
    leftover_min = minutes % 60
    if hours < 24:
        if leftover_min:
            return f"{hours} ساعت و {leftover_min} دقیقه"
        return f"{hours} ساعت"

    days = hours // 24
    leftover_hour = hours % 24
    if leftover_hour:
        return f"{days} روز و {leftover_hour} ساعت"
    return f"{days} روز"


def get_user_photo(user_id: int):
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.total_count > 0:
            return photos.photos[0][-1].file_id
    except Exception as e:
        log.warning("Could not fetch profile photo: %s", e)
    return None


def user_mention(user) -> str:
    name = (user.first_name or "").strip() or "کاربر ناشناس"
    if user.last_name:
        name = f"{name} {user.last_name}"
    name = esc(name)
    if user.username:
        return f'<a href="https://t.me/{user.username}">{name}</a>'
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def build_user_card(user) -> str:
    username = f"@{esc(user.username)}" if user.username else "نداره"
    return (
        f"👤 <b>اسم:</b> {user_mention(user)}\n"
        f"🆔 <b>آیدی:</b> <code>{user.id}</code>\n"
        f"🔗 <b>یوزرنیم:</b> {username}"
    )


def is_member(status: str) -> bool:
    return status in ("member", "administrator", "creator", "restricted")


def is_admin_in_channel(chat_id: int, user_id: int) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def send_private(user_id: int, text: str, photo_file_id=None) -> bool:
    try:
        if photo_file_id:
            bot.send_photo(user_id, photo_file_id, caption=text)
        else:
            bot.send_message(user_id, text)
        return True
    except Exception as e:
        log.warning("Private message failed (%s): %s", user_id, e)
        return False


def notify_all_receivers(chat_id: int, text: str, photo_file_id=None):
    with _data_lock:
        data = load_data()
        receivers = list(data["receivers"].get(str(chat_id), []))

    if not receivers:
        log.info("No receivers for this channel")
        return

    ok = sum(send_private(uid, text, photo_file_id) for uid in receivers)
    log.info("Report delivered to %s of %s receivers", ok, len(receivers))


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if message.chat.type != "private":
        return

    user_id = message.from_user.id

    with _data_lock:
        data = load_data()
        chat_keys = list(known_channel_keys(data))

    newly_registered = []
    already_registered = []
    accepted = []

    for chat_key in chat_keys:
        try:
            chat_id = int(chat_key)
        except ValueError:
            continue

        with _data_lock:
            data = load_data()
            title = channel_title(data, chat_key)
            already = user_id in data["receivers"].get(chat_key, [])

        if already:
            already_registered.append(title)
            continue

        if is_admin_in_channel(chat_id, user_id):
            accepted.append((chat_key, title))
            newly_registered.append(title)

    if accepted:
        with _data_lock:
            data = load_data()
            for chat_key, _title in accepted:
                receivers = data["receivers"].setdefault(chat_key, [])
                if user_id not in receivers:
                    receivers.append(user_id)
            save_data(data)

    registered = newly_registered + already_registered

    if registered:
        lines = "\n".join(f"• {esc(title)}" for title in registered)
        bot.reply_to(
            message,
            "سلام، خوش اومدی 👋\n\n"
            "از این به بعد ورود و خروج اعضای این کانال‌ها رو همین‌جا می‌فرستم:\n\n"
            f"{lines}\n\n"
            "لیست کانال‌ها: /status",
        )
        return

    bot.reply_to(
        message,
        "سلام 👋\n\n"
        "ورود و خروج اعضای کانالت رو بهت خبر می‌دم؛ "
        "با اسم، آیدی، یوزرنیم، عکس و مدت عضویت.\n\n"
        "<b>فعال‌سازی:</b>\n"
        "۱. منو بذار تو کانال\n"
        "۲. ادمینم کن (دیدن اعضا کافی‌ه)\n"
        "۳. دوباره /start بزن\n\n"
        "وقتی ادمین بودنت معلوم بشه، گزارش‌ها میان.",
    )


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if message.chat.type != "private":
        return

    user_id = message.from_user.id
    with _data_lock:
        data = load_data()
        mine = [
            channel_title(data, chat_key)
            for chat_key, receivers in data["receivers"].items()
            if user_id in receivers
        ]

    if not mine:
        bot.reply_to(
            message,
            "هنوز کانالی برات ثبت نشده.\n"
            "ادمین کانال باش، ربات هم توش باشه، بعد /start بزن.",
        )
        return

    lines = "\n".join(f"• {esc(title)}" for title in mine)
    bot.reply_to(message, f"گزارش این کانال‌ها روشنه:\n\n{lines}")


@bot.my_chat_member_handler()
def on_bot_status_change(update: ChatMemberUpdated):
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    chat = update.chat
    chat_key = str(chat.id)
    from_user = update.from_user

    log.info("Bot status in %s: %s -> %s", chat_key, old_status, new_status)

    if new_status == "administrator" and old_status != "administrator":
        with _data_lock:
            data = load_data()
            data["titles"][chat_key] = chat.title or chat_key
            data["channels"].setdefault(chat_key, {})
            receivers = data["receivers"].setdefault(chat_key, [])
            if from_user and not from_user.is_bot and from_user.id not in receivers:
                receivers.append(from_user.id)
            save_data(data)

        if from_user:
            send_private(
                from_user.id,
                f"ربات تو کانال <b>{esc(chat.title)}</b> فعاله.\n\n"
                "ورود و خروج اعضا همین‌جا میاد.\n"
                "ادمین دیگه هم اگر می‌خواد ببینه، یه بار /start بزنه.",
            )
        return

    if new_status in ("left", "kicked"):
        with _data_lock:
            data = load_data()
            data["titles"].pop(chat_key, None)
            data["channels"].pop(chat_key, None)
            data["receivers"].pop(chat_key, None)
            save_data(data)
        log.info("Removed channel data for %s", chat_key)


@bot.chat_member_handler()
def on_member_change(update: ChatMemberUpdated):
    chat = update.chat
    old_member = update.old_chat_member
    new_member = update.new_chat_member
    user = new_member.user

    if user.is_bot:
        return

    was_in = is_member(old_member.status)
    is_in = is_member(new_member.status)
    if was_in == is_in:
        return

    chat_key = str(chat.id)
    user_key = str(user.id)
    title = esc(chat.title)
    photo = get_user_photo(user.id)
    photo_note = "" if photo else "\n<i>عکس پروفایل عمومی نداره</i>"

    if not was_in and is_in:
        now = int(time.time())
        with _data_lock:
            data = load_data()
            data["titles"][chat_key] = chat.title or data["titles"].get(
                chat_key, chat_key
            )
            data["channels"].setdefault(chat_key, {})[user_key] = {
                "joined_at": now,
                "name": user.first_name or "",
            }
            save_data(data)

        time_str = datetime.now(TEHRAN).strftime("%Y/%m/%d - %H:%M")
        text = (
            f"🟢 <b>عضو جدید تو {title}</b>\n\n"
            + build_user_card(user)
            + photo_note
            + f"\n🕐 <b>ورود:</b> <code>{time_str}</code>"
        )
        notify_all_receivers(chat.id, text, photo)
        return

    with _data_lock:
        data = load_data()
        data["titles"][chat_key] = chat.title or data["titles"].get(chat_key, chat_key)
        joined_at = data["channels"].get(chat_key, {}).pop(user_key, {}).get("joined_at")
        save_data(data)

    duration = fmt_duration(int(time.time()) - joined_at) if joined_at else "مشخص نیست"
    text = (
        f"🔴 <b>یه نفر از {title} رفت</b>\n\n"
        + build_user_card(user)
        + photo_note
        + f"\n⏱ <b>مدت عضویت:</b> {duration}"
    )
    notify_all_receivers(chat.id, text, photo)


@app.route("/", methods=["GET"])
def health_check():
    return "ok", 200


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)

    update = Update.de_json(request.get_data().decode("utf-8"))
    if update:
        bot.process_new_updates([update])
    return "", 200


def setup_webhook():
    if not WEBHOOK_HOST:
        log.warning("WEBHOOK_URL and RENDER_EXTERNAL_URL are empty")
        return
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=WEBHOOK_FULL_URL,
            allowed_updates=["message", "chat_member", "my_chat_member"],
        )
        log.info("Webhook set")
    except Exception as e:
        log.error("Webhook setup failed: %s", e)


setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
