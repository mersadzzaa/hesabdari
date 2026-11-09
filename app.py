import os
import re
import html
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
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ================= تنظیمات اولیه =================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
DB_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

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
    """تولید دکمه‌های سال از ۱۴۰۲ تا ۱۴۱۰ در ردیف‌های ۳تایی"""
    years = [1402, 1403, 1404, 1405, 1406, 1407, 1408, 1409, 1410]
    rows = []
    for i in range(0, len(years), 3):
        row = [InlineKeyboardButton(f"سال {y}", callback_data=f"{action_prefix}_y:{y}") for y in years[i:i+3]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)

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

# ================= موتور ساخت فاکتور متنی و اکسل =================
def generate_text_invoice(title_header, items, is_range=False):
    lines = [
        "🧾 <b>صورت‌حساب هزینه‌ها</b>",
        f"📅 <b>بازه:</b> <code>{title_header}</code>",
        "────────────────────"
    ]
    total = 0
    for idx, item in enumerate(items, 1):
        if is_range:
            _, title, amount, d_val = item
            lines.append(f"<b>{idx}. {html.escape(title)}</b>")
            lines.append(f"   ▫️ مبلغ: <code>{amount:,}</code> تومان")
            lines.append(f"   ▫️ تاریخ: <code>{d_val}</code>")
        else:
            _, title, amount = item
            lines.append(f"<b>{idx}. {html.escape(title)}</b>")
            lines.append(f"   ▫️ مبلغ: <code>{amount:,}</code> تومان")
        
        total += amount
        lines.append("")

    lines.append("────────────────────")
    lines.append(f"📊 <b>تعداد اقلام:</b> {len(items)}")
    lines.append(f"💵 <b>جمع کل:</b> <b>{total:,} تومان</b>")
    return "\n".join(lines)

def generate_excel_file(title_header, items, is_range=False):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "صورت‌حساب"
    ws.views.sheetView[0].rightToLeft = True

    ws.append(["ردیف", "شرح کالا و خدمات", "تاریخ", "مبلغ (تومان)"])
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Tahoma", size=11, bold=True, color="FFFFFF")

    for col in range(1, 5):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    total = 0
    for idx, item in enumerate(items, 1):
        if is_range:
            _, title, amount, d_val = item
        else:
            _, title, amount = item
            d_val = title_header

        ws.append([idx, title, d_val, amount])
        total += amount

    ws.append(["", "جمع کل هزینه‌ها", "", total])
    last_row = ws.max_row
    total_font = Font(name="Tahoma", size=11, bold=True, color="B91C1C")
    
    for col in range(1, 5):
        cell = ws.cell(row=last_row, column=col)
        cell.font = total_font
        if col == 4:
            cell.number_format = '#,##0'

    for r in range(2, last_row):
        ws.cell(row=r, column=4).number_format = '#,##0'
        ws.cell(row=r, column=1).alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=3).alignment = Alignment(horizontal="center")

    ws.column_dimensions['A'].width = 8
    ws.column_dimensions['B'].width = 30
    ws.column_dimensions['C'].width = 16
    ws.column_dimensions['D'].width = 22

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

# ================= هندلرهای ربات تلگرام =================
TITLE, AMOUNT = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    menu = [
        ['📝 ثبت هزینه', '🧾 دریافت فاکتورها'],
        ['➕ اضافه کردن به روز خاص', '🗑 مدیریت و حذف هزینه‌ها']
    ]
    markup = ReplyKeyboardMarkup(menu, resize_keyboard=True)
    await update.message.reply_text("سلام رئیس! سیستم با تقویم تا سال ۱۴۱۰ و امکان ثبت در روز خاص آماده است.", reply_markup=markup)

# --- ثبت مرحله‌ای عادی ---
async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['flow'] = None
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

# --- سیستم جدید: اضافه کردن هزینه به روز خاص (مشابه پنل حذف) ---
async def add_to_specific_date_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['flow'] = None
    buttons = [
        [InlineKeyboardButton("امروز", callback_data="add_tgt_today"),
         InlineKeyboardButton("دیروز", callback_data="add_tgt_yesterday")],
        [InlineKeyboardButton("📅 انتخاب روز دیگر از تقویم", callback_data="add_tgt_custom")]
    ]
    await update.message.reply_text("برای اضافه کردن هزینه، کدام روز را مد نظر دارید؟", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_add_target_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_tgt_today":
        await prompt_for_custom_expense(query, context, get_today())
    elif data == "add_tgt_yesterday":
        await prompt_for_custom_expense(query, context, get_yesterday())
    elif data == "add_tgt_custom":
        await query.edit_message_text("سال روز مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("add_tgt_cal"))

async def handle_add_target_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("add_tgt_cal_y:"):
        y = data.split(":")[1]
        await query.edit_message_text("ماه را انتخاب کنید:", reply_markup=make_months_keyboard("add_tgt_cal", y))
    elif data.startswith("add_tgt_cal_m:"):
        _, y, m = data.split(":")
        await query.edit_message_text("روز را انتخاب کنید:", reply_markup=make_days_keyboard("add_tgt_cal", y, m))
    elif data.startswith("add_tgt_cal_d:"):
        _, y, m, d = data.split(":")
        selected_date = f"{y}/{m}/{d}"
        await prompt_for_custom_expense(query, context, selected_date)

async def prompt_for_custom_expense(query, context, date_str):
    context.user_data['target_date'] = date_str
    context.user_data['flow'] = 'AWAIT_SPECIFIC_TITLE'
    await query.edit_message_text(
        f"🗓 <b>تاریخ انتخابی:</b> <code>{date_str}</code>\n\n"
        f"لطفاً <b>عنوان خرید یا هزینه</b> را بفرستید:",
        parse_mode="HTML"
    )

# --- منوی فاکتورها (حل کامل باگ دکمه‌های سال) ---
async def report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['flow'] = None
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
        await show_date_report(query, get_today())
    elif data == "rep_yesterday":
        await show_date_report(query, get_yesterday())
    elif data == "rep_day_start":
        await query.edit_message_text("سال روز مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("rep_day"))
    elif data == "rep_month_start":
        await query.edit_message_text("سال ماه مورد نظر را انتخاب کنید:", reply_markup=make_years_keyboard("rep_mon"))
    elif data == "rep_range_start":
        context.user_data['range_mode'] = 'start'
        await query.edit_message_text("انتخاب «تاریخ شروع» بازه:\nسال را مشخص کنید:", reply_markup=make_years_keyboard("rep_rng"))

async def show_date_report(query, date_str):
    rows = get_tx_by_date(date_str)
    if not rows:
        await query.edit_message_text(f"در تاریخ {date_str} هیچ هزینه‌ای ثبت نشده است.")
        return
    text = generate_text_invoice(date_str, rows, is_range=False)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 دانلود فایل اکسل (Excel)", callback_data=f"xl_day:{date_str}")
    ]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

async def handle_report_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("rep_day_y:"):
        y = data.split(":")[1]
        await query.edit_message_text(f"سال {y} انتخاب شد. ماه را انتخاب کنید:", reply_markup=make_months_keyboard("rep_day", y))
    elif data.startswith("rep_day_m:"):
        _, y, m = data.split(":")
        await query.edit_message_text(f"ماه انتخاب شد. روز را انتخاب کنید:", reply_markup=make_days_keyboard("rep_day", y, m))
    elif data.startswith("rep_day_d:"):
        _, y, m, d = data.split(":")
        await show_date_report(query, f"{y}/{m}/{d}")

    elif data.startswith("rep_mon_y:"):
        y = data.split(":")[1]
        await query.edit_message_text(f"سال {y} انتخاب شد. ماه را انتخاب کنید:", reply_markup=make_months_keyboard("rep_mon", y))
    elif data.startswith("rep_mon_m:"):
        _, y, m = data.split(":")
        rows = get_tx_by_month(y, m)
        if not rows:
            await query.edit_message_text(f"در ماه {y}/{m} هیچ خریدی ثبت نشده است.")
            return
        text = generate_text_invoice(f"ماه {y}/{m}", rows, is_range=True)
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 دانلود فایل اکسل (Excel)", callback_data=f"xl_mon:{y}:{m}")
        ]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

    elif data.startswith("rep_rng_y:"):
        y = data.split(":")[1]
        await query.edit_message_text(f"سال {y} انتخاب شد. ماه را انتخاب کنید:", reply_markup=make_months_keyboard("rep_rng", y))
    elif data.startswith("rep_rng_m:"):
        _, y, m = data.split(":")
        await query.edit_message_text("روز را مشخص کنید:", reply_markup=make_days_keyboard("rep_rng", y, m))
    elif data.startswith("rep_rng_d:"):
        _, y, m, d = data.split(":")
        date_val = f"{y}/{m}/{d}"
        if context.user_data.get('range_mode') == 'start':
            context.user_data['start_date'] = date_val
            context.user_data['range_mode'] = 'end'
            await query.edit_message_text(f"تاریخ شروع: <code>{date_val}</code>\nحالا «تاریخ پایان» را انتخاب کنید:\nسال را بزنید:", parse_mode="HTML", reply_markup=make_years_keyboard("rep_rng"))
        else:
            start_d = context.user_data.get('start_date')
            end_d = date_val
            rows = get_tx_by_range(start_d, end_d)
            if not rows:
                await query.edit_message_text(f"هیچ هزینه‌ای بین {start_d} تا {end_d} ثبت نشده است.")
                return
            text = generate_text_invoice(f"از {start_d} تا {end_d}", rows, is_range=True)
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 دانلود فایل اکسل (Excel)", callback_data=f"xl_rng:{start_d}:{end_d}")
            ]])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

# --- دانلود فایل اکسل ---
async def handle_excel_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("xl_day:"):
        date_str = data.split(":")[1]
        rows = get_tx_by_date(date_str)
        buf = generate_excel_file(date_str, rows, is_range=False)
        filename = f"Factor_{date_str.replace('/', '-')}.xlsx"
    elif data.startswith("xl_mon:"):
        _, y, m = data.split(":")
        rows = get_tx_by_month(y, m)
        buf = generate_excel_file(f"{y}/{m}", rows, is_range=True)
        filename = f"Factor_Month_{y}_{m}.xlsx"
    elif data.startswith("xl_rng:"):
        _, s_d, e_d = data.split(":")
        rows = get_tx_by_range(s_d, e_d)
        buf = generate_excel_file(f"{s_d} تا {e_d}", rows, is_range=True)
        filename = f"Factor_{s_d.replace('/', '-')}_to_{e_d.replace('/', '-')}.xlsx"
    else:
        return

    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=buf,
        filename=filename,
        caption="📊 فایل اکسل صورت‌حساب"
    )

# --- سیستم مدیریت و حذف هزینه‌ها ---
async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['flow'] = None
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

# --- مدیریت پیام‌های متنی (ثبت سریع یا دریافت اطلاعات ثبت روز خاص) ---
async def general_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    flow = context.user_data.get('flow')
    text = update.message.text.strip()

    # ۱. مرحله دریافت عنوان برای روز خاص
    if flow == 'AWAIT_SPECIFIC_TITLE':
        context.user_data['specific_title'] = text
        context.user_data['flow'] = 'AWAIT_SPECIFIC_AMOUNT'
        await update.message.reply_text(
            f"عنوان «{text}» دریافت شد.\nحالا لطفاً <b>مبلغ را به تومان</b> بنویسید (فقط عدد):",
            parse_mode="HTML"
        )
        return

    # ۲. مرحله دریافت مبلغ برای روز خاص
    elif flow == 'AWAIT_SPECIFIC_AMOUNT':
        val = text.replace(',', '')
        if not val.isdigit():
            await update.message.reply_text("لطفاً مبلغ را فقط به صورت عدد بنویسید:")
            return

        amount = int(val)
        title = context.user_data.get('specific_title', 'بدون عنوان')
        target_date = context.user_data.get('target_date', get_today())

        add_transaction(title, amount, target_date)
        context.user_data['flow'] = None

        await update.message.reply_text(
            f"✅ هزینه با موفقیت ثبت شد!\n"
            f"🛒 شرح: <b>{html.escape(title)}</b>\n"
            f"💰 مبلغ: <code>{amount:,}</code> تومان\n"
            f"📅 تاریخ: <code>{target_date}</code>",
            parse_mode="HTML"
        )
        return

    # ۳. ثبت سریع (Fast Track با الگو: عنوان - مبلغ)
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
    
    # ثبت عادی تقویم
    application.add_handler(CallbackQueryHandler(handle_add_date_buttons, pattern='^add_date_'))
    application.add_handler(CallbackQueryHandler(handle_add_calendar, pattern='^add_cal_'))
    
    # بخش جدید: اضافه کردن به روز خاص
    application.add_handler(MessageHandler(filters.Regex('^➕ اضافه کردن به روز خاص$'), add_to_specific_date_menu))
    application.add_handler(CallbackQueryHandler(handle_add_target_selection, pattern='^add_tgt_(today|yesterday|custom)$'))
    application.add_handler(CallbackQueryHandler(handle_add_target_calendar, pattern='^add_tgt_cal_'))
    
    # فاکتورها (الگوی دقیق برای جلوگیری از تداخل کلیدهای سال)
    application.add_handler(MessageHandler(filters.Regex('^🧾 دریافت فاکتورها$'), report_menu))
    application.add_handler(CallbackQueryHandler(handle_reports, pattern='^rep_(today|yesterday|month_start|day_start|range_start)$'))
    application.add_handler(CallbackQueryHandler(handle_report_calendar, pattern='^rep_(day|mon|rng)_'))
    application.add_handler(CallbackQueryHandler(handle_excel_download, pattern='^xl_'))
    
    # مدیریت و حذف
    application.add_handler(MessageHandler(filters.Regex('^🗑 مدیریت و حذف هزینه‌ها$'), delete_menu))
    application.add_handler(CallbackQueryHandler(handle_delete_selection, pattern='^del_check_'))
    application.add_handler(CallbackQueryHandler(handle_delete_calendar, pattern='^del_cal_'))
    application.add_handler(CallbackQueryHandler(handle_deletion_action, pattern='^del_(item|all):'))

    # پیام‌های متنی
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, general_text_router))

    application.run_polling()

if __name__ == '__main__':
    main()
