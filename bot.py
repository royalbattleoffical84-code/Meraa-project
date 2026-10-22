"""Payment Bot  -  Telegram bot + FamPay UPI auto-payment (fampay_gateway.py).

Render Start Command:   python bot.py

Render > Environment me ye variables set karo:
    BOT_TOKEN           BotFather se mila token            (zaroori)
    GMAIL_ADDRESS       jis Gmail pe FamPay ki mail aati hai  (zaroori)
    GMAIL_APP_PASSWORD  Gmail App Password (16 letters)      (zaroori)
    FAMPAY_UPI_ID       aapki FamPay UPI ID                  (zaroori)
    ADMIN_ID            aapki Telegram numeric ID            (admin commands ke liye)
    STORE_NAME          QR pe dikhne wala naam               (optional)
    SUPPORT_USERNAME    jaise @mysupport                     (optional)
    DB_PATH             database file ka path                (optional, default bot.db)
"""
import html
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot import types

import fampay_gateway as fg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = _int_env("ADMIN_ID")
DB_PATH = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"
STORE_NAME = os.getenv("STORE_NAME", "MY STORE").strip()
SUPPORT = os.getenv("SUPPORT_USERNAME", "").strip()
MIN_AMOUNT = _int_env("MIN_AMOUNT", 10)
MAX_AMOUNT = _int_env("MAX_AMOUNT", 50000)
PRESETS = (10, 50, 100, 200, 500, 1000)
IST = timezone(timedelta(hours=5, minutes=30))
esc = fg.esc

bot = telebot.TeleBot(BOT_TOKEN or "0:missing", threaded=True, num_threads=8)


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0, joined_at INTEGER);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
                type TEXT, details TEXT, reference_id TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now')));
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id);
        """)
        conn.commit()
    finally:
        conn.close()


def upsert_user(u) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (id, username, first_name, balance, joined_at) VALUES (?,?,?,0,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (u.id, u.username or "", u.first_name or "", int(time.time())))
        conn.commit()
    finally:
        conn.close()


def get_balance(uid: int) -> int:
    conn = get_db()
    try:
        row = conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
        return int(row["balance"]) if row else 0
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Telegram helpers
# ----------------------------------------------------------------------------
def send(chat_id, text, markup=None):
    try:
        return bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        log.warning("send failed chat=%s: %s", chat_id, e)
        return None


def screen(call, text, markup=None):
    """Button dabane par wahi message edit karo; edit na ho paye (photo etc.) to naya bhejo."""
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        send(call.message.chat.id, text, markup)


def main_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton("💰 Add Money", callback_data="add_money"),
          types.InlineKeyboardButton("👛 Balance", callback_data="balance"))
    m.add(types.InlineKeyboardButton("📜 History", callback_data="history"),
          types.InlineKeyboardButton("❓ Help", callback_data="help"))
    return m


def back_menu():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("◀️ Menu", callback_data="home"))
    return m


def welcome_text(user) -> str:
    return (f"👋 Namaste <b>{esc(user.first_name)}</b>!\n"
            f"<b>{esc(STORE_NAME)}</b> me aapka swagat hai.\n\n"
            f"💰 Balance: <b>₹{get_balance(user.id)}</b>\n\n"
            f"Niche se option chuno 👇")


def fmt_ist(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=IST).strftime("%d %b, %I:%M %p")
    except Exception:
        return "-"


# ----------------------------------------------------------------------------
# Gateway wiring
# ----------------------------------------------------------------------------
cfg = fg.Config(
    gmail_address=os.getenv("GMAIL_ADDRESS", "").strip(),
    gmail_app_password=os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip(),
    upi_id=os.getenv("FAMPAY_UPI_ID", "").strip(),
    store_name=STORE_NAME,
    min_amount=MIN_AMOUNT,
    max_amount=MAX_AMOUNT,
)

_QUIET_EVENTS = {"INSTANT_PAYMENT_STARTED", "INSTANT_PAYMENT_EXPIRED", "UTR_SUBMITTED"}


def hook_notify_user(uid, text):
    bot.send_message(uid, text, parse_mode="HTML")


def hook_notify_admin(event, **kw):
    if not ADMIN_ID or event in _QUIET_EVENTS:
        return
    lines = [f"🔔 <b>{esc(event)}</b>"]
    for k in ("status", "user_id", "amount", "utr", "ref", "method", "details"):
        if kw.get(k) not in (None, ""):
            lines.append(f"{k}: <code>{esc(kw[k])}</code>")
    send(ADMIN_ID, "\n".join(lines))


def hook_on_credited(uid, amount, utr, order_id):
    if ADMIN_ID:
        send(ADMIN_ID, f"✅ <b>PAYMENT CREDITED</b>\nuser: <code>{uid}</code>\n₹{amount}\n"
                       f"utr: <code>{esc(utr)}</code>\norder: <code>{esc(order_id)}</code>")


gw = fg.FamPayGateway(cfg, fg.Hooks(
    get_db=get_db,
    notify_user=hook_notify_user,
    notify_admin=hook_notify_admin,
    on_credited=hook_on_credited,
    log_activity=lambda uid, action, detail: log.info("activity user=%s %s %s", uid, action, detail),
    send_default_success=True,
))


# ----------------------------------------------------------------------------
# Per-user state (amount / UTR ka intezaar)
# ----------------------------------------------------------------------------
STATE: dict = {}
STATE_TTL = 600


def set_state(uid, **kw):
    kw["ts"] = time.time()
    STATE[uid] = kw


def get_state(uid):
    s = STATE.get(uid)
    if s and time.time() - s["ts"] > STATE_TTL:
        STATE.pop(uid, None)
        return None
    return s


def clear_state(uid):
    STATE.pop(uid, None)


# ----------------------------------------------------------------------------
# Add money flow
# ----------------------------------------------------------------------------
def amount_menu():
    m = types.InlineKeyboardMarkup(row_width=3)
    m.add(*[types.InlineKeyboardButton(f"₹{a}", callback_data=f"amt_{a}") for a in PRESETS if MIN_AMOUNT <= a <= MAX_AMOUNT])
    m.add(types.InlineKeyboardButton("✏️ Custom Amount", callback_data="custom_amt"))
    m.add(types.InlineKeyboardButton("◀️ Menu", callback_data="home"))
    return m


def start_order(chat_id, user, amount: int):
    """QR order banata hai. Error ho to error text return karta hai, sab theek to None."""
    if not cfg.configured:
        log.error("Gateway not configured: GMAIL_ADDRESS / GMAIL_APP_PASSWORD / FAMPAY_UPI_ID missing")
        return "❌ Auto payment abhi available nahi hai. Support se contact karo."
    upsert_user(user)
    oid, err, uri = gw.create_order(user.id, amount)
    if not oid:
        return {"too_many_open": "⛔ Pehle purane pending orders complete ya cancel karo.",
                "bad_amount": f"❌ Amount ₹{MIN_AMOUNT} – ₹{MAX_AMOUNT} ke beech hona chahiye.",
                "not_configured": "❌ Auto payment abhi available nahi hai."}.get(err, "❌ Error aaya. Thodi der baad try karo.")

    caption = (
        f"💳 <b>ADD MONEY</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"💰 Amount: <b>₹{amount}</b>\n"
        f"🆔 Order: <code>{oid}</code>\n"
        f"📲 UPI ID: <code>{esc(cfg.upi_id)}</code>\n"
        f"⏱ Valid: {cfg.order_window // 60} minute\n━━━━━━━━━━━━━━━━━━\n"
        f"1️⃣ QR scan karke <b>exactly ₹{amount}</b> pay karo\n"
        f"2️⃣ Payment ke baad <b>✅ Payment Done</b> dabao\n"
        f"3️⃣ Apna <b>UTR / Transaction ID</b> bhejo\n\n"
        f"⚠️ Kam ya zyada amount bhejoge to balance add nahi hoga."
    )
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(types.InlineKeyboardButton("✅ Payment Done — UTR bhejo", callback_data=f"utr_{oid}"),
          types.InlineKeyboardButton("🔄 Order Status", callback_data=f"status_{oid}"),
          types.InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_{oid}"))
    png = fg.make_qr_png(uri)
    try:
        if png is None:
            raise RuntimeError("qr_unavailable")
        bot.send_photo(chat_id, png, caption=caption, parse_mode="HTML", reply_markup=m)
    except Exception as e:
        log.warning("QR photo send failed (%s) - text bhej raha hu", e)
        send(chat_id, caption + "\n\n⚠️ QR nahi ban paya. Upar di UPI ID pe manually pay karo.", m)
    return None


def latest_open_order(uid: int):
    row = gw._one("SELECT order_id FROM pending_payments WHERE user_id=? AND pay_method='upi_gmail' "
                  "AND status IN ('pending','expired') AND (utr IS NULL OR utr='') AND created_at>? "
                  "ORDER BY created_at DESC LIMIT 1", (uid, int(time.time()) - cfg.late_utr_window))
    return row["order_id"] if row else None


UTR_ERR = {
    "invalid_utr": "❌ UTR galat lag raha hai. 12 digit UTR number (ya FMPIB… wala ID) dobara bhejo.",
    "order_inactive": "❌ Ye order ab active nahi hai. Naya QR banao.",
    "utr_window_closed": "⏰ Is order ka UTR bhejne ka time (24 ghante) khatam ho gaya.",
    "too_many_tries": "⛔ UTR ke bahut zyada attempts ho gaye. Support se contact karo.",
    "duplicate_utr": "⛔ Ye UTR pehle hi use ho chuka hai.",
}

VERIFY_MSG = {
    "credited": "✅ <b>Payment verify ho gayi!</b>",
    "not_found": ("⏳ <b>Payment email abhi nahi mili</b>\n\n"
                  "Mail aane me kabhi kabhi 1-2 minute lagte hain. Bot khud baar baar check karta rahega. "
                  "Verify hote hi balance add ho jayega aur message aa jayega (max 6 ghante)."),
    "rejected": "⚠️ <b>Verification fail hua.</b> Detail message alag se bheja gaya hai.",
    "duplicate_utr": "⛔ Ye UTR pehle hi use ho chuka hai.",
    "busy": "⏳ Verification chal raha hai, result yahin aa jayega.",
    "skip": "⏳ Verification chal raha hai, result yahin aa jayega.",
    "error": "⚠️ Abhi verify nahi ho paya (temporary error). Bot khud dobara try karega.",
}


def _verify_and_report(chat_id, mid, oid):
    try:
        res = gw.verify_order(oid, force=True)
    except Exception as e:
        log.exception("verify thread failed: %s", e)
        res = "error"
    text = VERIFY_MSG.get(res, VERIFY_MSG["error"])
    try:
        if mid:
            bot.edit_message_text(text, chat_id, mid, parse_mode="HTML", reply_markup=back_menu())
        else:
            send(chat_id, text, back_menu())
    except Exception:
        send(chat_id, text, back_menu())


def submit_utr_flow(chat_id, user, oid, raw_text):
    uid = user.id
    ok, code = gw.submit_utr(uid, oid, raw_text, auto_verify=False)
    if not ok:
        if code != "invalid_utr":
            clear_state(uid)
        send(chat_id, UTR_ERR.get(code, "❌ Error aaya. Dobara try karo."))
        return
    clear_state(uid)
    msg = send(chat_id, f"🔍 <b>Payment verify ho raha hai...</b>\n📝 UTR: <code>{esc(code)}</code>\n\nThoda wait karo ⏳")
    threading.Thread(target=_verify_and_report, args=(chat_id, msg.message_id if msg else None, oid),
                     daemon=True).start()


# ----------------------------------------------------------------------------
# User commands
# ----------------------------------------------------------------------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    upsert_user(m.from_user)
    clear_state(m.from_user.id)
    send(m.chat.id, welcome_text(m.from_user), main_menu())


@bot.message_handler(commands=["help"])
def cmd_help(m):
    send(m.chat.id, help_text(), back_menu())


def help_text() -> str:
    t = ("❓ <b>HELP</b>\n━━━━━━━━━━━━━━━━━━\n"
         "💰 <b>Add Money</b> dabao, amount chuno, QR scan karke pay karo.\n"
         "✅ Payment ke baad <b>UTR / Transaction ID</b> bhejo.\n"
         "⚡ Bot Gmail se payment check karke balance khud add kar deta hai.\n\n"
         "ℹ️ Exactly wahi amount bhejo jo order me hai.\n"
         "ℹ️ Ek UTR sirf ek baar use ho sakta hai.")
    if SUPPORT:
        t += f"\n\n🆘 Support: {esc(SUPPORT)}"
    return t


def history_text(uid: int) -> str:
    conn = get_db()
    try:
        rows = conn.execute("SELECT amount, type, details, reference_id, created_at FROM transactions "
                            "WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return "📜 <b>HISTORY</b>\n━━━━━━━━━━━━━━━━━━\nAbhi koi transaction nahi hai."
    names = {"add_money": "Add Money", "admin_adjust": "Admin Adjust"}
    lines = ["📜 <b>LAST 10 TRANSACTIONS</b>\n━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        a = int(r["amount"])
        lines.append(f"{'➕' if a >= 0 else '➖'} <b>₹{abs(a)}</b> · {names.get(r['type'], esc(r['type']))}\n"
                     f"     🕒 {fmt_ist(r['created_at'])}" + (f"\n     🔖 <code>{esc(r['reference_id'])}</code>" if r["reference_id"] else ""))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Buttons
# ----------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid = call.from_user.id
    data = call.data or ""
    alert = None
    try:
        upsert_user(call.from_user)
        if data == "home":
            clear_state(uid)
            screen(call, welcome_text(call.from_user), main_menu())
        elif data == "add_money":
            clear_state(uid)
            screen(call, f"💰 <b>ADD MONEY</b>\n━━━━━━━━━━━━━━━━━━\nAmount chuno (₹{MIN_AMOUNT} – ₹{MAX_AMOUNT}):", amount_menu())
        elif data == "custom_amt":
            set_state(uid, kind="amount")
            screen(call, f"✏️ Apna amount number me bhejo (₹{MIN_AMOUNT} – ₹{MAX_AMOUNT}).\nJaise: <code>250</code>", back_menu())
        elif data.startswith("amt_"):
            amount = int(data[4:])
            err = start_order(call.message.chat.id, call.from_user, amount)
            if err:
                alert = err[:190]
        elif data == "balance":
            screen(call, f"👛 <b>BALANCE</b>\n━━━━━━━━━━━━━━━━━━\n💰 <b>₹{get_balance(uid)}</b>", back_menu())
        elif data == "history":
            screen(call, history_text(uid), back_menu())
        elif data == "help":
            screen(call, help_text(), back_menu())
        elif data.startswith("utr_"):
            oid = data[4:]
            row = gw.get_order(oid)
            if not row or row["user_id"] != uid or row["status"] not in fg.OPEN_STATUSES + ("review",):
                alert = "❌ Ye order ab active nahi hai."
            else:
                set_state(uid, kind="utr", oid=oid)
                send(call.message.chat.id,
                     "📝 Ab apna <b>UTR / Transaction ID</b> bhejo.\n"
                     "(12 digit number ya FMPIB… wala ID, payment app me 'Transaction details' me milta hai)")
        elif data.startswith("status_"):
            row = gw.get_order(data[7:])
            if not row or row["user_id"] != uid:
                alert = "❌ Order nahi mila."
            else:
                label = {"pending": "⏳ Pending", "expired": "⌛ Expired (UTR bhej sakte ho)", "success": "✅ Success",
                         "review": "⚠️ Review me", "cancelled": "❌ Cancelled"}.get(row["status"], row["status"])
                alert = f"{label}\n₹{row['amount']}"
                if row["utr"]:
                    alert += f"\nUTR: {row['utr']}"
                if row["status"] == "review" and row["reject_reason"]:
                    alert += "\n" + fg.REVIEW_MSG.get(row["reject_reason"], "")
                alert = alert[:190]
        elif data.startswith("cancel_"):
            if gw.cancel_order(uid, data[7:]):
                try:
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except Exception:
                    pass
                send(call.message.chat.id, "❌ Order cancel ho gaya.", main_menu())
            else:
                alert = "Ye order cancel nahi ho sakta (UTR submit ho chuka ya order band hai)."
    except Exception:
        log.exception("callback error data=%s", data)
        alert = "❌ Error aaya. Dobara try karo."
    try:
        bot.answer_callback_query(call.id, text=alert, show_alert=bool(alert))
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
def is_admin(uid) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID


@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if not is_admin(m.from_user.id):
        return
    send(m.chat.id, "🛠 <b>ADMIN COMMANDS</b>\n\n"
                    "/stats - users, balance, orders\n"
                    "/health - Gmail + QR check\n"
                    "/review - jo orders review me hain\n"
                    "/addbal &lt;user_id&gt; &lt;amount&gt; - balance add (minus se kam)\n"
                    "/broadcast &lt;message&gt; - sab users ko message")


@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_admin(m.from_user.id):
        return
    conn = get_db()
    try:
        u = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(balance),0) AS b FROM users").fetchone()
        st = conn.execute("SELECT status, COUNT(*) AS c FROM pending_payments GROUP BY status").fetchall()
        ist_now = int(time.time()) + 19800
        day_start = ist_now - ist_now % 86400 - 19800
        t = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM payment_utrs WHERE credited_at>=?",
                         (day_start,)).fetchone()
    finally:
        conn.close()
    orders = "\n".join(f"  {esc(r['status'])}: {r['c']}" for r in st) or "  -"
    send(m.chat.id, f"📊 <b>STATS</b>\n👥 Users: <b>{u['c']}</b>\n💰 Total user balance: <b>₹{u['b']}</b>\n"
                    f"📅 Aaj credited: <b>₹{t['s']}</b> ({t['c']} payments)\n\n🧾 Orders:\n{orders}")


@bot.message_handler(commands=["health"])
def cmd_health(m):
    if not is_admin(m.from_user.id):
        return
    h = gw.health()
    send(m.chat.id, f"🩺 <b>HEALTH</b>\nConfigured: {'✅' if h['configured'] else '❌'}\n"
                    f"Gmail: {'✅' if h['gmail_ok'] else '❌'} {esc(h['message'])}\n"
                    f"QR: {'✅' if h['qr_ok'] else '❌ (pip install segno)'}\n"
                    f"IMAP paused: {'⚠️ haan' if h['breaker_paused'] else 'nahi'}")


@bot.message_handler(commands=["review"])
def cmd_review(m):
    if not is_admin(m.from_user.id):
        return
    conn = get_db()
    try:
        rows = conn.execute("SELECT order_id, user_id, amount, utr, reject_reason FROM pending_payments "
                            "WHERE status='review' ORDER BY id DESC LIMIT 10").fetchall()
    finally:
        conn.close()
    if not rows:
        send(m.chat.id, "✅ Koi order review me nahi hai.")
        return
    lines = ["⚠️ <b>REVIEW ORDERS</b>"]
    for r in rows:
        lines.append(f"\n<code>{esc(r['order_id'])}</code>\nuser: <code>{r['user_id']}</code> · ₹{r['amount']}\n"
                     f"utr: <code>{esc(r['utr'])}</code>\nreason: {esc(r['reject_reason'])}")
    lines.append("\nPayment sahi hai to /addbal se manually balance add karo.")
    send(m.chat.id, "\n".join(lines))


@bot.message_handler(commands=["addbal"])
def cmd_addbal(m):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    try:
        target, amt = int(parts[1]), int(parts[2])
        if len(parts) != 3 or amt == 0:
            raise ValueError
    except (IndexError, ValueError):
        send(m.chat.id, "Use: <code>/addbal 123456789 100</code>  (minus likho to balance kam hoga)")
        return
    conn = get_db()
    try:
        cur = conn.execute("UPDATE users SET balance = balance + ? WHERE id=? AND balance + ? >= 0", (amt, target, amt))
        if cur.rowcount != 1:
            conn.rollback()
            send(m.chat.id, "❌ User nahi mila (usne /start nahi kiya) ya balance minus me chala jayega.")
            return
        conn.execute("INSERT INTO transactions (user_id, amount, type, details, reference_id) VALUES (?,?,?,?,?)",
                     (target, amt, "admin_adjust", "by_admin", f"ADM{m.from_user.id}"))
        conn.commit()
    finally:
        conn.close()
    send(m.chat.id, f"✅ Done. User <code>{target}</code> ka balance ₹{amt:+d} se update hua. Naya balance: ₹{get_balance(target)}")
    send(target, f"💰 Aapke balance me <b>₹{amt:+d}</b> update hua.\nNaya balance: <b>₹{get_balance(target)}</b>")


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not is_admin(m.from_user.id):
        return
    text = (m.text or "").partition(" ")[2].strip()
    if not text:
        send(m.chat.id, "Use: <code>/broadcast aapka message</code>")
        return

    def run():
        conn = get_db()
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM users").fetchall()]
        finally:
            conn.close()
        ok = bad = 0
        for i in ids:
            try:
                bot.send_message(i, text)
                ok += 1
            except Exception:
                bad += 1
            time.sleep(0.06)
        send(m.chat.id, f"📣 Broadcast complete: {ok} sent, {bad} failed.")

    threading.Thread(target=run, daemon=True).start()
    send(m.chat.id, "📣 Broadcast shuru ho gaya...")


# ----------------------------------------------------------------------------
# Plain text (custom amount / UTR)
# ----------------------------------------------------------------------------
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    text = (m.text or "").strip()
    upsert_user(m.from_user)
    st = get_state(uid)

    if st and st["kind"] == "amount":
        raw = text.replace(",", "").replace("₹", "").strip()
        if not raw.isdigit():
            send(m.chat.id, "❌ Sirf number bhejo, jaise <code>250</code>")
            return
        amt = int(raw)
        if not (MIN_AMOUNT <= amt <= MAX_AMOUNT):
            send(m.chat.id, f"❌ Amount ₹{MIN_AMOUNT} – ₹{MAX_AMOUNT} ke beech hona chahiye.")
            return
        clear_state(uid)
        err = start_order(m.chat.id, m.from_user, amt)
        if err:
            send(m.chat.id, err)
        return

    if st and st["kind"] == "utr":
        submit_utr_flow(m.chat.id, m.from_user, st["oid"], text)
        return

    if fg.looks_like_utr(text):
        oid = latest_open_order(uid)
        if oid:
            submit_utr_flow(m.chat.id, m.from_user, oid, text)
        else:
            send(m.chat.id, "Koi open order nahi mila. Pehle <b>💰 Add Money</b> se QR banao.", main_menu())
        return

    send(m.chat.id, "Menu se option chuno 👇", main_menu())


# ----------------------------------------------------------------------------
# Render ke liye chhota web server (Web Service ko ek port chahiye)
# ----------------------------------------------------------------------------
class _Health(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(b"OK - bot is running")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(body=False)

    def log_message(self, *args):
        pass


def start_health_server():
    port = _int_env("PORT", 10000)
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, name="health-http", daemon=True).start()
    log.info("health server port %s pe chal raha hai", port)


# ----------------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN set nahi hai. Render > Environment me BOT_TOKEN daalo (BotFather se milta hai).")
        sys.exit(1)

    start_health_server()          # pehle port kholo, taaki Render deploy ko 'live' maane
    init_db()
    gw.init()

    ok, msg = gw.mail.selftest()
    log.info("Gmail check: %s | %s", ok, msg)
    if ADMIN_ID:
        send(ADMIN_ID, f"🟢 <b>Bot start ho gaya</b>\nGmail: {'✅' if ok else '❌'} {esc(msg)}")

    gw.start_worker()
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log.info("Bot polling shuru")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
