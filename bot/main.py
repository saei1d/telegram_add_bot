# -*- coding: utf-8 -*-
"""
AddBot - Complete single-file Telegram bot (aiogram v2 + sqlite)
Features:
- Works only for a specified GROUP_ID
- When an admin posts in the group, bot posts/edits a "official" message:
  🤖 ربات رسمی گروه
  قیمت هر ادد: 5,000 تومان  •  حداقل برداشت: 50,000 تومان
  [دکمه شیشه‌ای برای شروع کلیک کنید] -> deep link to private chat
- Handles new_chat_members (direct adds) and link-based joins (via start param)
- Stores numeric user IDs, invite relationships, prevents double-pay
- Private chat dashboard with inline keyboard: my income, my refs, my link, request withdraw, verify channels
- Checks membership in REQUIRED_CHANNELS before enabling dashboard
- SQLite used for storage
"""

import logging
import asyncio
import aiomysql 
import uuid
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.types import  InlineKeyboardButton, InlineKeyboardMarkup

from aiogram.client.default import DefaultBotProperties   # اینجا هستش
from typing import Optional
from aiogram import Router, F
from aiogram.filters import Command
from dotenv import load_dotenv
import os
load_dotenv()
# ------------------- CONFIG -------------------

GROUP_ID = -1001234567890   # <-- set your group's numeric ID here (negative for supergroups)
ADMIN_IDS = [123456789, ]   # <-- list of admin numeric Telegram IDs for notifications
REQUIRED_CHANNELS = ["@YourChannel1", ]  # list of channel usernames or numeric IDs that users must join; can be empty

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "saei1d.mysql.pythonanywhere-services.com"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "saei1d"),
    "password": os.getenv("MYSQL_PASSWORD", "1010S1010s"),
    "db": os.getenv("MYSQL_DB", "addbot_db"),
    "autocommit": True
}


PAY_PER_INVITE = 5000       # تومان به ازای هر دعوت
MIN_WITHDRAW = 50000        # حداقل برداشت (تومان)
OFFICIAL_MSG_TEXT = "🤖 ربات رسمی گروه\nقیمت هر ادد: 5,000 تومان  •  حداقل برداشت: 50,000 تومان\n\nبرای شروع روی دکمه زیر کلیک کنید."
# ----------------------------------------------

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=os.getenv("BOT_TOKEN"),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)   # درست برای نسخه 3.7+
)
dp = Dispatcher()

router = Router()

dp.include_router(router)

# ------------------- DATABASE UTIL -------------------
async def init_db():
    try:
        pool = await aiomysql.create_pool(**MYSQL_CONFIG)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # ایجاد جدول users
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        last_name VARCHAR(255),
                        registered_at TEXT,
                        left_at TEXT
                    )
                """)
                
                # ایجاد جدول invites
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS invites (
                        invited_id INT PRIMARY KEY,
                        inviter_id INT,
                        method ENUM('direct', 'link'),
                        created_at TEXT
                    )
                """)
                
                # ایجاد جدول tokens
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS tokens (
                        token VARCHAR(255) PRIMARY KEY,
                        inviter_id INT,
                        created_at TEXT
                    )
                """)
                
                # ایجاد جدول settings
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        `key` VARCHAR(255) PRIMARY KEY,
                        value TEXT
                    )
                """)
                
                # ایجاد جدول withdrawals
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS withdrawals (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT,
                        amount INT,
                        created_at TEXT,
                        status ENUM('pending', 'paid', 'rejected')
                    )
                """)
                
                await conn.commit()
            pool.close()
        await pool.wait_closed()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

async def db_set_setting(key: str, value: str):
    try:
        pool = await aiomysql.create_pool(**MYSQL_CONFIG)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO settings (`key`, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = %s", 
                    (key, value, value)
                )
                await conn.commit()
            pool.close()
        await pool.wait_closed()
    except Exception as e:
        logger.error(f"Error setting setting: {e}")

async def db_get_setting(key: str) -> Optional[str]:
    try:
        pool = await aiomysql.create_pool(**MYSQL_CONFIG)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT value FROM settings WHERE `key` = %s", (key,))
                row = await cur.fetchone()
                return row[0] if row else None
            pool.close()
        await pool.wait_closed()
    except Exception as e:
        logger.error(f"Error getting setting: {e}")
        return None

# ------------------- HELPERS -------------------
def now_iso():
    return datetime.utcnow().isoformat()

async def ensure_user_record(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users WHERE id = ?", (user.id,)) as cur:
            row = await cur.fetchone()
            if not row:
                await db.execute(
                    "INSERT INTO users (id, username, first_name, last_name, registered_at, left_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user.id, user.username, user.first_name, user.last_name, now_iso(), None)
                )
                await db.commit()
            else:
                # update username/name if changed
                await db.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_name = ? WHERE id = ?",
                    (user.username, user.first_name, user.last_name, user.id)
                )
                await db.commit()

async def record_invite(invited_id: int, inviter_id: int, method: str):
    """
    Record an invite only if invited_id not already recorded.
    Returns True if recorded (i.e. counts as a new invite), False otherwise.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT inviter_id FROM invites WHERE invited_id = ?", (invited_id,)) as cur:
            r = await cur.fetchone()
            if r:
                return False
        await db.execute(
            "INSERT INTO invites (invited_id, inviter_id, method, created_at) VALUES (?, ?, ?, ?)",
            (invited_id, inviter_id, method, now_iso())
        )
        await db.commit()
        return True

async def inviter_stats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM invites WHERE inviter_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
            count = r[0] if r else 0
        earnings = count * PAY_PER_INVITE
        return {"count": count, "earnings": earnings}

async def create_token_for_user(inviter_id: int):
    token = uuid.uuid4().hex[:10]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO tokens (token, inviter_id, created_at) VALUES (?, ?, ?)",
                         (token, inviter_id, now_iso()))
        await db.commit()
    return token

async def get_inviter_by_token(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT inviter_id FROM tokens WHERE token = ?", (token,)) as cur:
            r = await cur.fetchone()
            return r[0] if r else None

async def user_is_member_of_required_channels(user_id: int):
    # if no required channels -> OK
    if not REQUIRED_CHANNELS:
        return True, []
    not_member = []
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ("left", "kicked"):
                not_member.append(ch)
        except Exception as e:
            # if bot not in channel or channel invalid, we treat as not verified (but we do not block - return False)
            logger.warning(f"Error checking membership for {user_id} in {ch}: {e}")
            not_member.append(ch)
    return len(not_member) == 0, not_member

async def get_official_message_info():
    val = await db_get_setting("official_msg")
    if not val:
        return None
    # stored as "chat_id:message_id"
    try:
        chat_id, msg_id = val.split(":")
        return {"chat_id": int(chat_id), "message_id": int(msg_id)}
    except Exception:
        return None

async def set_official_message_info(chat_id: int, message_id: int):
    await db_set_setting("official_msg", f"{chat_id}:{message_id}")

# ------------------- UI BUILDERS -------------------
def group_start_button():
    # deep link to private chat
    # We will use "start=welcome" param for convenience (no token)
    url = f"https://t.me/{(bot.username or 'Bot')}?start=welcome"
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("دکمه شیشه‌ای برای شروع کلیک کنید", url=url))
    return kb

def private_main_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📈 گزارش درآمد", callback_data="my_income"),
        InlineKeyboardButton("🧾 زیرمجموعه‌ها", callback_data="my_refs")
    )
    kb.add(
        InlineKeyboardButton("🔗 لینک دعوت من", callback_data="my_link"),
        InlineKeyboardButton("✅ تایید کانال‌ها", callback_data="check_channels")
    )
    kb.add(
        InlineKeyboardButton("💸 درخواست برداشت", callback_data="withdraw"),
        InlineKeyboardButton("🆘 پشتیبانی", callback_data="support")
    )
    return kb

# ------------------- HANDLERS -------------------

@router.message(F.chat.id == GROUP_ID)
async def on_group_message(message: types.Message):
    """
    Catch messages in the target group.
    If the sender is an admin (and not the bot), update/post official message.
    """
    try:
        # ignore bot messages
        if message.from_user and message.from_user.is_bot:
            return

        # check if sender is admin in the group
        try:
            member = await bot.get_chat_member(chat_id=GROUP_ID, user_id=message.from_user.id)
            if member.status not in ("creator", "administrator"):
                return  # only react when an admin posts
        except Exception as e:
            logger.exception("Cannot get chat member info: %s", e)
            return

        # we have an admin message -> we should ensure official message exists (post or edit)
        info = await get_official_message_info()
        if info:
            # try to edit existing message
            try:
                await bot.edit_message_text(
                    chat_id=info["chat_id"],
                    message_id=info["message_id"],
                    text=OFFICIAL_MSG_TEXT,
                    parse_mode=ParseMode.HTML,
                    reply_markup=group_start_button()
                )
                logger.info("Edited official message in group.")
            except Exception as e:
                logger.warning("Failed to edit official message, will try send new: %s", e)
                # try to send-and-pin
                try:
                    sent = await bot.send_message(GROUP_ID, OFFICIAL_MSG_TEXT, reply_markup=group_start_button())
                    # pin the message
                    await bot.pin_chat_message(chat_id=GROUP_ID, message_id=sent.message_id, disable_notification=True)
                    await set_official_message_info(GROUP_ID, sent.message_id)
                    logger.info("Sent & pinned new official message.")
                except Exception as e2:
                    logger.exception("Failed to send official message: %s", e2)
        else:
            # send & pin new
            try:
                sent = await bot.send_message(GROUP_ID, OFFICIAL_MSG_TEXT, reply_markup=group_start_button())
                await bot.pin_chat_message(chat_id=GROUP_ID, message_id=sent.message_id, disable_notification=True)
                await set_official_message_info(GROUP_ID, sent.message_id)
                logger.info("Sent & pinned official message initially.")
            except Exception as e:
                logger.exception("Failed to create official message: %s", e)
    except Exception as e:
        logger.exception("Error in on_group_message: %s", e)


@router.message(F.chat.id == GROUP_ID, F.content_type == "new_chat_members")
async def on_new_members(message: types.Message):

    """
    Handle new_chat_members - direct add via Add Members.
    message.from_user is the adder (if added manually)
    message.new_chat_members contains list of users added
    """
    try:
        adder = message.from_user  # the account who performed the add (might be the user if they joined via link)
        for new_user in message.new_chat_members:
            if new_user.is_bot:
                continue

            # ensure user record
            await ensure_user_record(new_user)

            # determine inviter:
            inviter_id = None
            method = "direct"
            # If adder exists and is not the new_user himself, treat as direct invite
            if adder and adder.id != new_user.id:
                inviter_id = adder.id
            else:
                # adder is same as new_user -> probably joined via link; attempts to find token won't work here
                inviter_id = None
                method = "link"

            # record invite only if not already recorded
            if inviter_id:
                recorded = await record_invite(new_user.id, inviter_id, method)
                if recorded:
                    # optionally notify inviter or log
                    try:
                        await bot.send_message(
                            chat_id=GROUP_ID,
                            text=f"✅ کاربر <a href='tg://user?id={inviter_id}'>دعوت‌کننده</a> یک نفر را دعوت کرد و {PAY_PER_INVITE:,} تومان منظور شد.",
                            parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        pass

            # welcome message in group (brief)
            try:
                await bot.send_message(
                    chat_id=GROUP_ID,
                    text=f"به گروه خوش آمدی {new_user.full_name} 👋\nبرای دیدن حساب و کسب درآمد روی دکمه زیر کلیک کن.",
                    reply_markup=group_start_button()
                )
            except Exception as e:
                logger.warning("Failed to send welcome message: %s", e)
    except Exception as e:
        logger.exception("Error in on_new_members: %s", e)


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    """
    /start handler: may receive params like start=welcome or start=invite_<token>
    """
    arg = None
    if message.get_args():
        arg = message.get_args().strip()

    user = message.from_user
    await ensure_user_record(user)

    # If start param starts with invite_ it means someone shared an invite link
    if arg and arg.startswith("invite_"):
        token = arg.split("invite_", 1)[1]
        inviter_id = await get_inviter_by_token(token)
        if inviter_id and inviter_id != user.id:
            recorded = await record_invite(user.id, inviter_id, "link")
            if recorded:
                # Notify group or inviter
                logger.info(f"User {user.id} joined via token of {inviter_id}")
                try:
                    # optional: notify inviter privately
                    await bot.send_message(inviter_id, f"✅ شخصی با لینک دعوت شما وارد گروه شد: <a href='tg://user?id={user.id}'>{user.full_name}</a>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    # Show initial private dashboard, but first ensure membership in required channels
    ok, missing = await user_is_member_of_required_channels(user.id)
    if not ok:
        # show button to check channels and show which missing
        txt = "برای استفاده از ربات لازم است عضو کانال(های) زیر شوید:\n"
        for ch in missing:
            txt += f"- {ch}\n"
        txt += "\nپس از عضویت روی دکمه «من عضو شدم» بزنید تا وضعیت شما بررسی شود."
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("من عضو شدم ✅", callback_data="i_joined_channels"))
        await message.answer(txt, reply_markup=kb)
        return

    # show dashboard
    stats = await inviter_stats(user.id)
    txt = f"سلام {user.full_name} 👋\n\n🔢 دعوت‌های موفق: {stats['count']}\n💰 درآمد کل: {stats['earnings']:,} تومان\n💸 حداقل برداشت: {MIN_WITHDRAW:,} تومان"
    await message.answer(txt, reply_markup=private_main_kb())


@router.callback_query(F.data == "i_joined_channels")
async def callback_i_joined_channels(cb: types.CallbackQuery):
    user = cb.from_user
    ok, missing = await user_is_member_of_required_channels(user.id)
    if ok:
        stats = await inviter_stats(user.id)
        txt = f"تشکر! وضعیت عضویت شما تایید شد.\n\n🔢 دعوت‌های موفق: {stats['count']}\n💰 درآمد کل: {stats['earnings']:,} تومان"
        await cb.message.edit_text(txt, reply_markup=private_main_kb())
    else:
        txt = "هنوز عضویت شما در کانال(های) زیر تایید نشده:\n"
        for ch in missing:
            txt += f"- {ch}\n"
        txt += "\nلطفاً مطمئن شوید که عضو شده‌اید و دوباره تلاش کنید."
        await cb.answer(txt, show_alert=True)


@router.callback_query(F.data == "my_income")
async def callback_my_income(cb: types.CallbackQuery):
    user = cb.from_user
    stats = await inviter_stats(user.id)
    # compute withdrawables: for this simple model all earnings are withdrawable but minimum applies when requesting
    txt = f"📈 گزارش درآمد\n\n🔢 دعوت‌های موفق: {stats['count']}\n💰 درآمد کل: {stats['earnings']:,} تومان\n\nبرای درخواست برداشت روی «درخواست برداشت» بزنید."
    await cb.message.edit_text(txt, reply_markup=private_main_kb())


@router.callback_query(F.data == "my_refs")
async def callback_my_refs(cb: types.CallbackQuery):
    user = cb.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT invited_id, method, created_at FROM invites WHERE inviter_id = ? ORDER BY created_at DESC LIMIT 50", (user.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await cb.answer("شما هنوز زیرمجموعه‌ای ندارید.", show_alert=True)
        return
    txt = "🧾 زیرمجموعه‌های شما (آخرین ۵۰):\n\n"
    for r in rows:
        invited_id, method, created_at = r
        txt += f"- <a href='tg://user?id={invited_id}'>{invited_id}</a>  ({method})  - {created_at.split('T')[0]}\n"
    txt += "\n(نمایش به صورت آیدی عددی برای شفافیت)"
    await cb.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=private_main_kb())


@router.callback_query(F.data == "my_link")
async def callback_my_link(cb: types.CallbackQuery):
    user = cb.from_user
    token = await create_token_for_user(user.id)
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=invite_{token}"
    txt = f"🔗 لینک دعوت اختصاصی شما:\n\n{link}\n\nبا اشتراک‌گذاری این لینک، وقتی کسی با آن وارد شود شما به‌عنوان دعوت‌کننده ثبت می‌شوید."
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("کپی لینک (در موبایل long-press کنید)", url=link),
        InlineKeyboardButton("بازگشت", callback_data="back_main")
    )
    await cb.message.edit_text(txt, reply_markup=kb)


@router.callback_query(F.data == "check_channels")
async def callback_check_channels(cb: types.CallbackQuery):
    user = cb.from_user
    ok, missing = await user_is_member_of_required_channels(user.id)
    if ok:
        await cb.answer("عضویت در کانال‌ها تایید شد.", show_alert=True)
    else:
        txt = "شما هنوز عضو کانال(های) زیر نیستید:\n" + "\n".join(missing)
        await cb.answer(txt, show_alert=True)


@router.callback_query(F.data == "withdraw")
async def callback_withdraw(cb: types.CallbackQuery):
    user = cb.from_user
    stats = await inviter_stats(user.id)
    earnings = stats["earnings"]
    if earnings < MIN_WITHDRAW:
        await cb.answer(f"موجودی شما {earnings:,} تومان است. حداقل برداشت {MIN_WITHDRAW:,} تومان می‌باشد.", show_alert=True)
        return
    # show quick options
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(f"درخواست {MIN_WITHDRAW:,} تومان", callback_data=f"withdraw_do:{MIN_WITHDRAW}"))
    kb.add(InlineKeyboardButton("درخواست کل موجودی", callback_data=f"withdraw_do:{earnings}"))
    kb.add(InlineKeyboardButton("بازگشت", callback_data="back_main"))
    await cb.message.edit_text(f"💸 شما می‌توانید درخواست برداشت ثبت کنید.\nموجودی شما: {earnings:,} تومان", reply_markup=kb)


@router.callback_query(F.data.startswith("withdraw_do:"))
async def callback_withdraw_do(cb: types.CallbackQuery):
    user = cb.from_user
    _, amount_str = cb.data.split(":", 1)
    try:
        amount = int(amount_str)
    except Exception:
        await cb.answer("مقدار نامعتبر", show_alert=True)
        return
    stats = await inviter_stats(user.id)
    if stats["earnings"] < amount:
        await cb.answer("موجودی کافی نیست.", show_alert=True)
        return
    # create withdrawal record
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO withdrawals (user_id, amount, created_at, status) VALUES (?, ?, ?, ?)",
                         (user.id, amount, now_iso(), "pending"))
        await db.commit()
    # notify admins
    text_admin = f"📢 درخواست برداشت جدید\nکاربر: <a href='tg://user?id={user.id}'>{user.full_name}</a>\nمقدار: {amount:,} تومان\nزمان: {now_iso()}"
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text_admin, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    await cb.answer("درخواست برداشت شما ثبت شد. پشتیبانی برای پرداخت با شما تماس خواهد گرفت.", show_alert=True)
    await cb.message.edit_text("✅ درخواست برداشت ثبت شد. پس از تایید تیم پشتیبانی پرداخت انجام می‌شود.", reply_markup=private_main_kb())


@router.callback_query(F.data == "support")
async def callback_support(cb: types.CallbackQuery):
    user = cb.from_user
    txt = "برای تماس با پشتیبانی پیام خود را تایپ کنید. (این پیام به ادمین‌ها ارسال خواهد شد)"
    await cb.message.edit_text(txt)
    # set a simple state by storing a setting with key f"support_from_{user.id}" - we'll capture next message
    await db_set_setting(f"support_from_{user.id}", "awaiting")


@router.message(F.chat.type == "private")
async def private_text_handler(message: types.Message):
    # handle support reply when waiting
    key = f"support_from_{message.from_user.id}"
    val = await db_get_setting(key)
    if val == "awaiting":
        # send to admins
        txt = f"📨 پیام از کاربر <a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a> برای پشتیبانی:\n\n{message.text}"
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, txt, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        await message.reply("پیام شما برای پشتیبانی ارسال شد. به زودی پاسخ داده خواهد شد.")
        # clear the state
        await db_set_setting(key, "")
        return

    # otherwise ignore or show help
    await message.reply("برای مشاهده داشبورد از /start استفاده کنید.")

@router.callback_query(F.data == "back_main")
async def callback_back_main(cb: types.CallbackQuery):
    stats = await inviter_stats(cb.from_user.id)
    txt = f"سلام {cb.from_user.full_name} 👋\n\n🔢 دعوت‌های موفق: {stats['count']}\n💰 درآمد کل: {stats['earnings']:,} تومان\n💸 حداقل برداشت: {MIN_WITHDRAW:,} تومان"
    await cb.message.edit_text(txt, reply_markup=private_main_kb())




# ------------------- STARTUP -------------------
async def on_startup():
    # initialize DB
    await init_db()
    # set bot.username available
    me = await bot.get_me()
    logger.info(f"Bot @{me.username} started")
    # ensure official message exists? we'll let first admin message create it

async def main():
    await on_startup()  # جایگزین on_startup=...
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
