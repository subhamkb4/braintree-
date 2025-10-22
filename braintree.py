import logging
import sqlite3
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import braintree
import asyncio
from threading import Thread
import time

# === CONFIGURATION ===
BOT_TOKEN = "8490537906:AAF2VFbK6lNaABdfTYLyAWyvrvmSlx17Ohg"
ADMIN_ID = 7896890222

# Braintree Configuration (YOU NEED TO ADD YOUR OWN KEYS)
BRAINTREE_MERCHANT_ID = "xxrvx2hm6tjzxpzs"
BRAINTREE_PUBLIC_KEY = "c6vthxf9kdpznzsq"
BRAINTREE_PRIVATE_KEY = "85e51581a66d3e74a8de5bfed0e62939"

# Configure braintree gateway
gateway = braintree.BraintreeGateway(
    braintree.Configuration(
        environment=braintree.Environment.Sandbox,  # Change to Production for live
        merchant_id=BRAINTREE_MERCHANT_ID,
        public_key=BRAINTREE_PUBLIC_KEY,
        private_key=BRAINTREE_PRIVATE_KEY
    )
)

# === DATABASE SETUP ===
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS authorized_users
                 (user_id INTEGER PRIMARY KEY, is_premium INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS check_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  card_number TEXT,
                  result TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# === ADMIN & PREMIUM SYSTEM ===
def is_authorized(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT * FROM authorized_users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def add_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO authorized_users (user_id, is_premium) VALUES (?, 1)", (user_id,))
        conn.commit()
    except:
        pass
    conn.close()

def log_check(user_id, card_number, result):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT INTO check_logs (user_id, card_number, result) VALUES (?, ?, ?)",
              (user_id, card_number, result))
    conn.commit()
    conn.close()

# === CC CHECKING FUNCTION ===
def check_cc_with_braintree(card_number, expiration_date, cvv):
    try:
        # Create payment method nonce
        result = gateway.payment_method.create({
            "credit_card": {
                "number": card_number,
                "expiration_date": expiration_date,
                "cvv": cvv
            }
        })

        if result.is_success:
            # Try a $1 authorization
            transaction_result = gateway.transaction.sale({
                "amount": "1.00",
                "payment_method_nonce": result.payment_method.nonce,
                "options": {
                    "submit_for_settlement": False  # Just authorization, no settlement
                }
            })
            
            if transaction_result.is_success:
                return "✅ **APPROVED** - Transaction Authorized"
            else:
                return "❌ **DECLINED** - Authorization Failed"
        else:
            return "❌ **DECLINED** - Invalid Card Details"
            
    except Exception as e:
        return f"⚠️ **ERROR** - Gateway Error: {str(e)}"

# === TELEGRAM BOT HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(
            "🚫 **Access Denied**\n\n"
            "You are not authorized to use this bot.\n"
            "Contact admin for access."
        )
        return
    
    keyboard = [['/b3', '/help']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🔒 **Premium CC Checker**\n\n"
        "Welcome to the professional card checking system.\n\n"
        "Available Commands:\n"
        "• /b3 - Check Credit Card\n"
        "• /help - Show help\n\n"
        "**Status: ✅ AUTHORIZED**",
        reply_markup=reply_markup
    )

async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("🚫 **Admin Only**")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /add <user_id>")
        return
    
    try:
        new_user_id = int(context.args[0])
        add_user(new_user_id)
        await update.message.reply_text(f"✅ User {new_user_id} added successfully!")
    except:
        await update.message.reply_text("❌ Invalid user ID")

async def b3_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("🚫 **Unauthorized**")
        return

    await update.message.reply_text(
        "💳 **Braintree CC Checker**\n\n"
        "Please send card details in format:\n"
        "`CardNumber|MMYY|CVV`\n\n"
        "Example:\n"
        "`4111111111111111|0125|123`"
    )
    return 'AWAITING_CC'

async def handle_cc_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return ConversationHandler.END

    text = update.message.text
    parts = text.split('|')
    
    if len(parts) != 3:
        await update.message.reply_text("❌ Invalid format. Use: CardNumber|MMYY|CVV")
        return 'AWAITING_CC'

    card_number, exp_date, cvv = parts[0].strip(), parts[1].strip(), parts[2].strip()

    # Show checking message
    progress_msg = await update.message.reply_text(
        "🔍 **Checking Card...**\n"
        "⏳ Please wait while we process your request..."
    )

    # Perform the check
    result = await asyncio.get_event_loop().run_in_executor(
        None, check_cc_with_braintree, card_number, exp_date, cvv
    )

    # Log the check
    log_check(user_id, card_number[:6] + "XXXXXX", result)

    # Send result
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=progress_msg.message_id,
        text=f"💳 **Card Check Result**\n\n"
             f"**Card:** `{card_number[:6]}XXXXXX{card_number[-4:]}`\n"
             f"**Expiry:** `{exp_date}`\n"
             f"**CVV:** `{cvv}`\n\n"
             f"**Result:** {result}\n\n"
             f"**Gateway:** Braintree\n"
             f"**Status:** Real-time Check"
    )
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Help Guide**\n\n"
        "**Available Commands:**\n"
        "• /start - Start bot\n"
        "• /b3 - Check credit card\n"
        "• /help - This message\n\n"
        "**Admin Commands:**\n"
        "• /add <user_id> - Add premium user\n\n"
        "**Format:**\n"
        "CardNumber|MMYY|CVV\n"
        "Example: 4111111111111111|0125|123"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

def main():
    # Initialize database
    init_db()
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add admin user by default
    add_user(ADMIN_ID)

    # Conversation handler for CC checking
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('b3', b3_check)],
        states={
            'AWAITING_CC': [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cc_details)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_user_cmd))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)

    # Start bot
    application.run_polling()
    print("🤖 Bot is running...")

if __name__ == '__main__':
    main()