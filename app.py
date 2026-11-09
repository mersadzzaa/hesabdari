import os
import re
import threading
import psycopg2
import jdatetime
from io import BytesIO
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

# ================= تنظیمات اولیه =================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
DB_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

# نام دقیق فایل فونت آپلود شده در گیت‌هاب
FONT_FILE = "font.ttf"

def fix_text(text):
    """پردازش تمیز متن‌های فارسی"""
    if not text:
        return ""
    s = str(text).strip().replace('\u200c', ' ')
    if re.search(r'[\u0600-\u06FF]', s):
        reshaped = arabic_reshaper.reshape(s)
        return get_display(reshaped)
    return s

# ================= سرور زنده نگه‌دارنده =================
web_app = Flask(__name__)

@web_app.route('/')
def alive():
    return "Bot is Alive and Running!"

def run_web_server():
    web_app.run(host="0.0.0.0", port=PORT)

# ================= توابع پایگاه داده (PostgreSQL) =================
def get_db():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            amount BIGINT NOT NULL,
            shamsi_date TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def add_transaction(title, amount, shamsi_date):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (title, amount, shamsi_date) VALUES (%s, %s, %s)",
        (title, amount, shamsi_date)
    )
    conn.commit()
    cur.close()
    conn.close()

def get_tx_by_date(date_str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, title, amount FROM transactions WHERE shamsi_date = %s ORDER BY id ASC", (date_str,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_tx_by_month(year, month):
    conn = get_db()
    cur = conn.cursor()
    pattern = f"{year}/{month}/%"
    cur.execute("SELECT id, title, amount, shamsi_date FROM transactions WHERE shamsi_date LIKE %s ORDER BY shamsi_date ASC, id ASC", (pattern,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_tx_by_range(start_date, end_date):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, amount, shamsi_date FROM transactions WHERE shamsi_date >= %s AND shamsi_date <= %s ORDER BY shamsi_date ASC, id ASC",
        (start_date, end_date)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def delete_single_tx(tx_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
    conn.commit()
    cur.close()
    conn.close()

def delete_day_txs(date_str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE shamsi_date = %s", (date_str,))
    conn.commit()
    cur.close()
    conn.close()

# ================= توابع تاریخ و تقویم شمسی =================
def get_today():
    return jdatetime.date.today().strftime("%Y/%m/%d")

def get_yesterday():
    return (jdatetime.date.today() - jdatetime.timedelta(days=1)).strftime("%Y/%m/%d")

def format_price(amount):
    return f"{amount:,} تومان"

def make_years_keyboard(action_prefix):
    current_y = jdatetime.date.today().year
    years = [current_y - 1, current_y, current_y + 1]
    buttons = [[InlineKeyboardButton(f"سال {y}", callback_data=f"{action_prefix}_y:{y}")] for y in years]
    return InlineKeyboardMarkup(buttons)

def make_months_keyboard(action_prefix, year):
    months = [
        ("فروردین", "01"), ("اردیبهشت", "02"), ("خرداد", "03"),
        ("تیر", "04"), ("مرداد", "05"), ("شهریور", "06"),
        ("مهر", "07"), ("آبان", "08"), ("آذر", "09"),
        ("دی", "10"), ("بهمن", "11"), ("اسفند", "12")
    ]
    rows = []
    for i in range(0, 12, 3):
        row = [InlineKeyboardButton(m[0], callback_data=f"{action_prefix}_m:{year}:{m[1]}") for m in months[i:i+3]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def make_days_keyboard(action_prefix, year, month):
    m_int = int(month)
    if m_int <= 6:
        max_days = 31
    elif m_int <= 11:
        max_days = 30
    else:
        max_days = 30 if jdatetime.date(int(year), 1, 1).isleap() else 29
        
    rows = []
    row = []
    for d in range(1, max_days + 1):
        d_str = f"{d:02d}"
        row.append(InlineKeyboardButton(str(d), callback_data=f"{action_prefix}_d:{year}:{month}:{d_str}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ================= موتور رسم فاکتور PDF =================
def create_pdf_invoice(title_header, items, is_range=False):
    if not os.path.exists(FONT_FILE):
        return None  # هشدار به نبود فایل فونت

    page_width, page_height = 1240, 1754
    image = Image.new('RGB', (page_width, page_height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    
    font_large = ImageFont.truetype(FONT_FILE, 40)
    font_bold = ImageFont.truetype(FONT_FILE, 26)
    font_regular = ImageFont.truetype(FONT_FILE, 22)

    # سربرگ سرمه‌ای
    draw.rectangle([(60, 50), (1180, 160)], fill=(30, 41, 59))
    draw.text((page_width / 2, 90), fix_text("صورت حساب هزینه ها"), fill=(255, 255, 255), font=font_large, anchor="mm")
    
    # کادر تاریخ بازه گزارش
    draw.text((1140, 200), fix_text("بازه فاکتور:"), fill=(70, 70, 70), font=font_bold, anchor="rt")
    draw.text((950, 200), str(title_header), fill=(30, 41, 59), font=font_bold, anchor="lt")
    
    # سربرگ جدول
    draw.rectangle([(60, 260), (1180, 320)], fill=(226, 232, 240))
    draw.text((1140, 278), fix_text("ردیف"), fill=(15, 23, 42), font=font_bold, anchor="rt")
    draw.text((980, 278), fix_text("شرح کالا و خدمات"), fill=(15, 23, 42), font=font_bold, anchor="rt")
    if is_range:
        draw.text((550, 278), fix_text("تاریخ"), fill=(15, 23, 42), font=font_bold, anchor="rt")
    draw.text((100, 278), fix_text("مبلغ (تومان)"), fill=(15, 23, 42), font=font_bold, anchor="lt")

    # سطرهای جدول
    y = 350
    total_amount = 0
    for idx, item in enumerate(items):
        if is_range:
            _, title, amount, date_val = item
        else:
            _, title, amount = item
            date_val = ""

        # رنگ پس‌زمینه یک‌درمیان
        if idx % 2 == 1:
            draw.rectangle([(60, y - 10), (1180, y + 45)], fill=(248, 250, 252))

        # ردیف
        draw.text((1130, y), str(idx + 1), fill=(70, 70, 70), font=font_regular, anchor="rt")
        
        # شرح کالا
        draw.text((980, y), fix_text(title), fill=(30, 30, 30), font=font_regular, anchor="rt")
        
        # تاریخ (فقط برای فاکتورهای بازه زمانی)
        if is_range:
            draw.text((550, y), str(date_val), fill=(70, 70, 70), font=font_regular, anchor="rt")
            
        # مبلغ
        draw.text((100, y), f"{amount:,}", fill=(16, 120, 50), font=font_bold, anchor="lt")

        total_amount += amount
        y += 58
        if y > 1550:
            break

    # کادر جمع کل
    draw.line([(60, y + 20), (1180, y + 20)], fill=(148, 163, 184), width=2)
    y += 50
    draw.rectangle([(60, y), (1180, y + 80)], fill=(241, 245, 249))
    
    draw.text((1140, y + 25), fix_text("جمع کل هزینه ها:"), fill=(15, 23, 42), font=font_bold, anchor="rt")
    
    total_str = f"{total_amount:,} "
    draw.text((100, y + 25), total_str, fill=(185, 28, 28), font=font_large, anchor="lt")
    draw.text((100 + draw.textlength(total_str, font=font_large) + 10, y + 32), fix_text("تومان"), fill=(185, 28, 28), font=font_bold, anchor="lt")

    pdf_buffer = BytesIO()
    image.save(pdf_buffer, format="PDF", resolution=150.0)
    pdf_buffer.seek(0)
    return pdf_buffer

# ================= هندلرهای ربات تلگرام =================
TITLE, AMOUNT = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    menu = [
        ['📝 ثبت هزینه', '🧾 دریافت فاکتورها (PDF)'],
        ['🗑 مدیریت و حذف هزینه‌ها']
    ]
    markup = ReplyKeyboardMarkup(menu, resize_keyboard=True)
    await update.message.reply_text("سلام رئیس! سیستم با استفاده از فایل مستقیم فونت آماده به کار است.", reply_markup=markup)

# --- ثبت مرحله‌ای ---
async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("عنوان خرید یا هزینه را بنویسید (مثلاً: بنزین):")
    return TITLE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['temp_title'] = update.message.text
    await update.message.reply_text(f"مبلغ برای «{update.message.text}» به تومان (فقط عدد):")
    return AMOUNT

async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.replace(',', '').strip()
    if not val.isdigit():
        await update.message.reply_text("لطفاً فقط عدد بنویسید:")
        return AMOUNT

    context.user_data['temp_amount'] = int(val)
    buttons = [
        [InlineKeyboardButton("امروز", callback_data="add_date_today"),
         InlineKeyboardButton("دیروز", callback_data="add_date_yesterday")],
        [InlineKeyboardButton("📅 تاریخ دیگر (انتخاب تقویم)", callback_data="add_date_custom")]
    ]
    await update.message.reply_text("تاریخ انجام این هزینه را تعیین کنید:", reply_markup=InlineKeyboardMarkup(buttons))
    return ConversationHandler.END

async def handle_add_date_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_date_today":
        date_str = get_today()
        finalize_add(update, context, date_str)
        await query.edit_message_text(f"✅ هزینه برای «امروز» ({date_str}) ثبت شد.")
    elif data == "add_date_yesterday":
        date_str = get_yesterday()
        finalize_add(update, context, date_str)
        await query.edit_message_text(f"✅ هزینه برای «دیروز» ({date_str}) ثبت شد.")
    elif data == "add_date_custom":
        await query.edit_message_text("سال مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("add_cal"))

async def handle_add_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("add_cal_y:"):
        year = data.split(":")[1]
        await query.edit_message_text(f"سال {year} انتخاب شد. ماه را انتخاب کنید:", reply_markup=make_months_keyboard("add_cal", year))
    elif data.startswith("add_cal_m:"):
        _, year, month = data.split(":")
        await query.edit_message_text(f"ماه انتخاب شد. روز را مشخص کنید:", reply_markup=make_days_keyboard("add_cal", year, month))
    elif data.startswith("add_cal_d:"):
        _, year, month, day = data.split(":")
        selected_date = f"{year}/{month}/{day}"
        finalize_add(update, context, selected_date)
        await query.edit_message_text(f"✅ هزینه در تاریخ انتخابی ({selected_date}) با موفقیت ثبت شد.")

def finalize_add(update, context, date_str):
    title = context.user_data.get('temp_title', 'بدون عنوان')
    amount = context.user_data.get('temp_amount', 0)
    add_transaction(title, amount, date_str)

# --- منوی فاکتورها (PDF) ---
async def report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    buttons = [
        [InlineKeyboardButton("📄 فاکتور امروز", callback_data="rep_today"),
         InlineKeyboardButton("📄 فاکتور دیروز", callback_data="rep_yesterday")],
        [InlineKeyboardButton("🗓 فاکتور یک ماه کامل", callback_data="rep_month_start")],
        [InlineKeyboardButton("📅 فاکتور یک روز مشخص", callback_data="rep_day_start")],
        [InlineKeyboardButton("⏳ بازه زمانی دلخواه (از تا)", callback_data="rep_range_start")]
    ]
    await update.message.reply_text("نوع فاکتور مورد نظرتان را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "rep_today":
        await send_date_pdf(query, context, get_today())
    elif data == "rep_yesterday":
        await send_date_pdf(query, context, get_yesterday())
    elif data == "rep_day_start":
        await query.edit_message_text("سال روز مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("rep_day"))
    elif data == "rep_month_start":
        await query.edit_message_text("سال ماه مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("rep_mon"))
    elif data == "rep_range_start":
        context.user_data['range_mode'] = 'start'
        await query.edit_message_text("انتخاب «تاریخ شروع» بازه:\nسال را مشخص کنید:", reply_markup=make_years_keyboard("rep_rng"))

async def send_date_pdf(query, context, date_str):
    rows = get_tx_by_date(date_str)
    if not rows:
        await query.edit_message_text(f"هیچ هزینه‌ای در تاریخ {date_str} یافت نشد.")
        return
    await query.edit_message_text("در حال آماده‌سازی فایل PDF ... ⏳")
    pdf_file = create_pdf_invoice(f"{date_str}", rows, is_range=False)
    
    if pdf_file is None:
        await query.edit_message_text("❌ خطا: فایل font.ttf در گیت‌هاب پیدا نشد! لطفاً فایل فونت را آپلود کنید.")
        return

    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=pdf_file,
        filename=f"Factor_{date_str.replace('/', '-')}.pdf",
        caption=f"🧾 فاکتور هزینه‌های تاریخ {date_str}"
    )
    await query.delete_message()

async def handle_report_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("rep_day_y:"):
        y = data.split(":")[1]
        await query.edit_message_text("ماه را انتخاب کنید:", reply_markup=make_months_keyboard("rep_day", y))
    elif data.startswith("rep_day_m:"):
        _, y, m = data.split(":")
        await query.edit_message_text("روز را انتخاب کنید:", reply_markup=make_days_keyboard("rep_day", y, m))
    elif data.startswith("rep_day_d:"):
        _, y, m, d = data.split(":")
        await send_date_pdf(query, context, f"{y}/{m}/{d}")

    elif data.startswith("rep_mon_y:"):
        y = data.split(":")[1]
        await query.edit_message_text("ماه را انتخاب کنید:", reply_markup=make_months_keyboard("rep_mon", y))
    elif data.startswith("rep_mon_m:"):
        _, y, m = data.split(":")
        rows = get_tx_by_month(y, m)
        if not rows:
            await query.edit_message_text(f"در ماه {y}/{m} هیچ خریدی ثبت نشده است.")
            return
        await query.edit_message_text("در حال رسم و ساخت فاکتور ماهانه PDF ... ⏳")
        pdf_file = create_pdf_invoice(f"{y}/{m}", rows, is_range=True)
        
        if pdf_file is None:
            await query.edit_message_text("❌ خطا: فایل font.ttf در گیت‌هاب پیدا نشد! لطفاً فایل فونت را آپلود کنید.")
            return

        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=pdf_file,
            filename=f"Monthly_Factor_{y}_{m}.pdf",
            caption=f"🧾 فاکتور جامع ماهانه {y}/{m}"
        )
        await query.delete_message()

    elif data.startswith("rep_rng_y:"):
        y = data.split(":")[1]
        await query.edit_message_text("ماه را انتخاب کنید:", reply_markup=make_months_keyboard("rep_rng", y))
    elif data.startswith("rep_rng_m:"):
        _, y, m = data.split(":")
        await query.edit_message_text("روز را مشخص کنید:", reply_markup=make_days_keyboard("rep_rng", y, m))
    elif data.startswith("rep_rng_d:"):
        _, y, m, d = data.split(":")
        date_val = f"{y}/{m}/{d}"
        if context.user_data.get('range_mode') == 'start':
            context.user_data['start_date'] = date_val
            context.user_data['range_mode'] = 'end'
            await query.edit_message_text(f"تاریخ شروع: {date_val}\nحالا «تاریخ پایان» را انتخاب کنید:\nسال را بزنید:", reply_markup=make_years_keyboard("rep_rng"))
        else:
            start_d = context.user_data.get('start_date')
            end_d = date_val
            rows = get_tx_by_range(start_d, end_d)
            if not rows:
                await query.edit_message_text(f"هیچ هزینه‌ای بین {start_d} تا {end_d} ثبت نشده است.")
                return
            await query.edit_message_text("در حال آماده‌سازی فاکتور بازه زمانی دلخواه ... ⏳")
            pdf_file = create_pdf_invoice(f"{start_d} تا {end_d}", rows, is_range=True)
            
            if pdf_file is None:
                await query.edit_message_text("❌ خطا: فایل font.ttf در گیت‌هاب پیدا نشد! لطفاً فایل فونت را آپلود کنید.")
                return

            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=pdf_file,
                filename=f"Range_Factor_{start_d.replace('/', '-')}.pdf",
                caption=f"🧾 فاکتور بازه {start_d} تا {end_d}"
            )
            await query.delete_message()

# --- سیستم مدیریت و حذف هزینه‌ها ---
async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    buttons = [
        [InlineKeyboardButton("امروز", callback_data="del_check_today"),
         InlineKeyboardButton("دیروز", callback_data="del_check_yesterday")],
        [InlineKeyboardButton("📅 انتخاب روز دیگر", callback_data="del_check_custom")]
    ]
    await update.message.reply_text("برای مدیریت و حذف، کدام روز را می‌خواهید بررسی کنید؟", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_delete_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "del_check_today":
        await show_day_items_for_deletion(query, get_today())
    elif data == "del_check_yesterday":
        await show_day_items_for_deletion(query, get_yesterday())
    elif data == "del_check_custom":
        await query.edit_message_text("سال روز مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("del_cal"))

async def handle_delete_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("del_cal_y:"):
        y = data.split(":")[1]
        await query.edit_message_text("ماه را انتخاب کنید:", reply_markup=make_months_keyboard("del_cal", y))
    elif data.startswith("del_cal_m:"):
        _, y, m = data.split(":")
        await query.edit_message_text("روز را انتخاب کنید:", reply_markup=make_days_keyboard("del_cal", y, m))
    elif data.startswith("del_cal_d:"):
        _, y, m, d = data.split(":")
        await show_day_items_for_deletion(query, f"{y}/{m}/{d}")

async def show_day_items_for_deletion(query, date_str):
    rows = get_tx_by_date(date_str)
    if not rows:
        await query.edit_message_text(f"در تاریخ {date_str} هیچ تراکنشی برای حذف وجود ندارد.")
        return

    buttons = []
    for tx_id, title, amount in rows:
        btn_text = f"❌ حذف: {title} ({amount:,})"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"del_item:{tx_id}:{date_str}")])

    buttons.append([InlineKeyboardButton("🔥 حذف کامل تمام هزینه‌های این روز", callback_data=f"del_all:{date_str}")])
    
    await query.edit_message_text(
        f"لیست هزینه‌های تاریخ {date_str}:\nبرای حذف هر قلم، روی آن کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_deletion_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("del_item:"):
        _, tx_id, date_str = data.split(":")
        delete_single_tx(int(tx_id))
        await show_day_items_for_deletion(query, date_str)
    elif data.startswith("del_all:"):
        date_str = data.split(":")[1]
        delete_day_txs(date_str)
        await query.edit_message_text(f"🗑 تمام هزینه‌های تاریخ {date_str} به طور کامل پاک شدند.")

# --- ثبت سریع ---
async def fast_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    text = update.message.text
    match = re.match(r'^(.*?)\s*-\s*([\d,]+)$', text)
    if match:
        title = match.group(1).strip()
        amount = int(match.group(2).replace(',', ''))
        date_str = get_today()
        add_transaction(title, amount, date_str)
        await update.message.reply_text(f"⚡️ سریع ثبت شد!\n🛒 {title}\n💰 {format_price(amount)}\n📅 امروز")

# ================= اجرای برنامه =================
def main():
    init_db()
    
    threading.Thread(target=run_web_server, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📝 ثبت هزینه$'), start_add)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)]
        },
        fallbacks=[]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    
    application.add_handler(CallbackQueryHandler(handle_add_date_buttons, pattern='^add_date_'))
    application.add_handler(CallbackQueryHandler(handle_add_calendar, pattern='^add_cal_'))
    
    application.add_handler(MessageHandler(filters.Regex('^🧾 دریافت فاکتورها \\(PDF\\)$'), report_menu))
    application.add_handler(CallbackQueryHandler(handle_reports, pattern='^rep_'))
    application.add_handler(CallbackQueryHandler(handle_report_calendar, pattern='^rep_(day|mon|rng)_'))
    
    application.add_handler(MessageHandler(filters.Regex('^🗑 مدیریت و حذف هزینه‌ها$'), delete_menu))
    application.add_handler(CallbackQueryHandler(handle_delete_selection, pattern='^del_check_'))
    application.add_handler(CallbackQueryHandler(handle_delete_calendar, pattern='^del_cal_'))
    application.add_handler(CallbackQueryHandler(handle_deletion_action, pattern='^del_(item|all):'))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fast_track))

    application.run_polling()

if __name__ == '__main__':
    main()
