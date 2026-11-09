import os
import re
import threading
import psycopg2
import jdatetime
from io import BytesIO
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

# ================= تنظیمات و متغیرهای محیطی =================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
DB_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

# ================= وب‌سرور برای زنده ماندن ربات =================
web_app = Flask(__name__)

@web_app.route('/')
def alive():
    return "Bot is Alive and Running!"

def run_web_server():
    web_app.run(host="0.0.0.0", port=PORT)

# ================= توابع پایگاه داده (PostgreSQL) =================
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
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
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (title, amount, shamsi_date) VALUES (%s, %s, %s)",
        (title, amount, shamsi_date)
    )
    conn.commit()
    cur.close()
    conn.close()

def get_transactions_by_date(shamsi_date):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, amount FROM transactions WHERE shamsi_date = %s ORDER BY id ASC", (shamsi_date,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# ================= توابع کمکی و تقویم =================
def get_today_shamsi():
    return jdatetime.date.today().strftime("%Y/%m/%d")

def get_yesterday_shamsi():
    return (jdatetime.date.today() - jdatetime.timedelta(days=1)).strftime("%Y/%m/%d")

def format_price(amount):
    return f"{amount:,} تومان"

def fix_persian_text(text):
    # برای راست‌چین و متصل کردن حروف فارسی در عکس
    reshaped_text = arabic_reshaper.reshape(text)
    return get_display(reshaped_text)

# ================= تولید فاکتور تصویری =================
def generate_invoice_image(date_str, transactions):
    width, height = 800, 200 + (len(transactions) * 60)
    if height < 400: height = 400
    
    image = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    
    try:
        font_title = ImageFont.truetype("Vazirmatn-Regular.ttf", 40)
        font_text = ImageFont.truetype("Vazirmatn-Regular.ttf", 28)
    except:
        font_title = ImageFont.load_default()
        font_text = ImageFont.load_default()

    # هدر
    header_text = fix_persian_text(f"فاکتور هزینه‌ها - تاریخ: {date_str}")
    draw.text((width/2, 40), header_text, fill=(0, 0, 0), font=font_title, anchor="mt")
    draw.line([(50, 100), (750, 100)], fill=(200, 200, 200), width=3)

    # جدول
    y = 130
    total_amount = 0
    for i, (title, amount) in enumerate(transactions):
        row_text = fix_persian_text(f"{i+1}. {title}")
        price_text = fix_persian_text(format_price(amount))
        
        draw.text((700, y), row_text, fill=(50, 50, 50), font=font_text, anchor="rt")
        draw.text((100, y), price_text, fill=(0, 100, 0), font=font_text, anchor="lt")
        
        total_amount += amount
        y += 60

    # فوتر (جمع کل)
    draw.line([(50, y), (750, y)], fill=(200, 200, 200), width=3)
    y += 30
    total_text_label = fix_persian_text("جمع کل:")
    total_text_value = fix_persian_text(format_price(total_amount))
    
    draw.text((700, y), total_text_label, fill=(0, 0, 0), font=font_title, anchor="rt")
    draw.text((100, y), total_text_value, fill=(200, 0, 0), font=font_title, anchor="lt")

    bio = BytesIO()
    image.save(bio, 'JPEG')
    bio.seek(0)
    return bio

# ================= منطق ربات تلگرام =================
TITLE, AMOUNT, DATE = range(3)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    keyboard = [['📝 ثبت هزینه', '🧾 دریافت فاکتورها']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("سلام رئیس! به حسابدار شخصی خودت خوش اومدی.\nچه کاری برات انجام بدم؟", reply_markup=reply_markup)

# --- سیستم ثبت مرحله‌ای ---
async def start_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("لطفاً عنوان خرید یا هزینه را بنویسید (مثلاً: سوپرمارکت):")
    return TITLE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['title'] = update.message.text
    await update.message.reply_text(f"مبلغ برای «{update.message.text}» چقدر شد؟\n(لطفاً فقط عدد به تومان وارد کن)")
    return AMOUNT

async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(',', '')
    if not text.isdigit():
        await update.message.reply_text("لطفاً مبلغ را فقط به صورت عدد وارد کن!")
        return AMOUNT
    
    context.user_data['amount'] = int(text)
    keyboard = [
        [InlineKeyboardButton("امروز", callback_data="date_today"),
         InlineKeyboardButton("دیروز", callback_data="date_yesterday")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("این هزینه کی انجام شد؟", reply_markup=reply_markup)
    return DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "date_today":
        date_str = get_today_shamsi()
    else:
        date_str = get_yesterday_shamsi()
        
    title = context.user_data['title']
    amount = context.user_data['amount']
    
    add_transaction(title, amount, date_str)
    
    msg = f"✅ ثبت شد!\n🛒 خرید: {title}\n💰 مبلغ: {format_price(amount)}\n📅 تاریخ: {date_str}"
    await query.edit_message_text(text=msg)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

# --- سیستم ثبت سریع (Fast Track) ---
async def fast_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    text = update.message.text
    # الگو: عنوان - مبلغ
    match = re.match(r'^(.*?)\s*-\s*([\d,]+)$', text)
    if match:
        title = match.group(1).strip()
        amount = int(match.group(2).replace(',', ''))
        date_str = get_today_shamsi()
        add_transaction(title, amount, date_str)
        await update.message.reply_text(f"⚡️ سریع ثبت شد!\n🛒 {title}\n💰 {format_price(amount)}")

# --- سیستم گزارش‌گیری ---
async def report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    keyboard = [
        [InlineKeyboardButton("فاکتور امروز", callback_data="report_today")],
        [InlineKeyboardButton("فاکتور دیروز", callback_data="report_yesterday")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("فاکتور چه زمانی را می‌خواهی؟", reply_markup=reply_markup)

async def generate_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    date_str = get_today_shamsi() if query.data == "report_today" else get_yesterday_shamsi()
    transactions = get_transactions_by_date(date_str)
    
    if not transactions:
        await query.edit_message_text(f"در تاریخ {date_str} هیچ هزینه‌ای ثبت نشده است.")
        return
        
    await query.edit_message_text(f"در حال تولید فاکتور برای {date_str} ... ⏳")
    
    image_stream = generate_invoice_image(date_str, transactions)
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image_stream)
    await query.delete_message()

# ================= اجرای برنامه =================
def main():
    init_db()
    
    # روشن کردن وب‌سرور در پس‌زمینه
    threading.Thread(target=run_web_server, daemon=True).start()
    
    # راه‌اندازی ربات
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📝 ثبت هزینه$'), start_transaction)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            DATE: [CallbackQueryHandler(get_date, pattern='^date_')]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^🧾 دریافت فاکتورها$'), report_menu))
    application.add_handler(CallbackQueryHandler(generate_report, pattern='^report_'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fast_track))
    
    application.run_polling()

if __name__ == '__main__':
    main()
