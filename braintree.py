import logging
import sqlite3
import asyncio
import time
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import braintree

# === CONFIGURATION ===
BOT_TOKEN = "8490537906:AAF2VFbK6lNaABdfTYLyAWyvrvmSlx17Ohg"
ADMIN_ID = 7896890222

# Braintree Configuration - REPLACE WITH YOUR KEYS
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
    conn = sqlite3.connect('braintree_bot.db')
    c = conn.cursor()
    
    # Authorized users table
    c.execute('''CREATE TABLE IF NOT EXISTS authorized_users
                 (user_id INTEGER PRIMARY KEY, 
                  username TEXT,
                  is_premium INTEGER DEFAULT 1,
                  join_date DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    
    # Check logs table
    c.execute('''CREATE TABLE IF NOT EXISTS check_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  card_bin TEXT,
                  card_last4 TEXT,
                  card_type TEXT,
                  amount REAL DEFAULT 1.00,
                  result TEXT,
                  gateway TEXT DEFAULT 'braintree',
                  response_time REAL,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    
    # System stats table
    c.execute('''CREATE TABLE IF NOT EXISTS system_stats
                 (date DATE PRIMARY KEY,
                  total_checks INTEGER DEFAULT 0,
                  approved_checks INTEGER DEFAULT 0,
                  declined_checks INTEGER DEFAULT 0)''')
    
    conn.commit()
    conn.close()

# === USER MANAGEMENT ===
def is_authorized(user_id):
    conn = sqlite3.connect('braintree_bot.db')
    c = conn.cursor()
    c.execute("SELECT * FROM authorized_users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def add_user(user_id, username=None):
    conn = sqlite3.connect('braintree_bot.db')
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO authorized_users (user_id, username) VALUES (?, ?)", 
                 (user_id, username))
        conn.commit()
    except Exception as e:
        print(f"Error adding user: {e}")
    finally:
        conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect('braintree_bot.db')
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM check_logs WHERE user_id=?", (user_id,))
    total_checks = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM check_logs WHERE user_id=? AND result LIKE '%APPROVED%'", (user_id,))
    approved_checks = c.fetchone()[0]
    
    conn.close()
    
    return total_checks, approved_checks

# === ADVANCED BRAINTREE CHECKING ===
def advanced_braintree_check(card_number, expiration_date, cvv, amount=1.00):
    """
    Advanced Braintree CC check with comprehensive error handling and analytics
    """
    start_time = time.time()
    
    try:
        # Basic card validation
        if not all([card_number, expiration_date, cvv]):
            return "❌ **INVALID** - Missing card details"
        
        if len(card_number) < 13 or len(card_number) > 19:
            return "❌ **INVALID** - Card number length invalid"
        
        # Create payment method
        result = gateway.payment_method.create({
            "credit_card": {
                "number": card_number.strip(),
                "expiration_date": expiration_date.strip(),
                "cvv": cvv.strip()
            }
        })

        if result.is_success:
            # Get card type from Braintree response
            card_type = result.payment_method.card_type.lower() if result.payment_method.card_type else "unknown"
            
            # Attempt authorization with specified amount
            transaction_result = gateway.transaction.sale({
                "amount": str(amount),
                "payment_method_nonce": result.payment_method.nonce,
                "options": {
                    "submit_for_settlement": False,  # Auth only, no settlement
                    "verify_card": True
                }
            })
            
            response_time = time.time() - start_time
            
            if transaction_result.is_success:
                return {
                    "status": "APPROVED",
                    "message": f"✅ **APPROVED** - {card_type.upper()} Card",
                    "card_type": card_type,
                    "response_time": response_time,
                    "transaction_id": transaction_result.transaction.id
                }
            else:
                error_msg = transaction_result.message if transaction_result.message else "Authorization Failed"
                return {
                    "status": "DECLINED", 
                    "message": f"❌ **DECLINED** - {error_msg}",
                    "card_type": card_type,
                    "response_time": response_time,
                    "error_code": getattr(transaction_result.transaction, 'processor_response_code', 'N/A')
                }
        else:
            error_msg = result.message if result.message else "Invalid Card Details"
            return {
                "status": "DECLINED",
                "message": f"❌ **DECLINED** - {error_msg}",
                "card_type": "unknown",
                "response_time": time.time() - start_time,
                "error_code": "N/A"
            }
            
    except Exception as e:
        return {
            "status": "ERROR",
            "message": f"⚠️ **GATEWAY ERROR** - {str(e)}",
            "card_type": "unknown", 
            "response_time": time.time() - start_time,
            "error_code": "EXCEPTION"
        }

# === LOGGING AND ANALYTICS ===
def log_check_result(user_id, card_number, result_data):
    conn = sqlite3.connect('braintree_bot.db')
    c = conn.cursor()
    
    card_bin = card_number[:6]
    card_last4 = card_number[-4:]
    
    c.execute('''INSERT INTO check_logs 
                 (user_id, card_bin, card_last4, card_type, result, response_time) 
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id, card_bin, card_last4, result_data.get('card_type', 'unknown'), 
               result_data['message'], result_data.get('response_time', 0)))
    
    # Update daily stats
    today = datetime.now().date()
    c.execute('''INSERT OR IGNORE INTO system_stats (date) VALUES (?)''', (today,))
    
    c.execute('''UPDATE system_stats SET total_checks = total_checks + 1 WHERE date = ?''', (today,))
    
    if result_data['status'] == 'APPROVED':
        c.execute('''UPDATE system_stats SET approved_checks = approved_checks + 1 WHERE date = ?''', (today,))
    elif result_data['status'] == 'DECLINED':
        c.execute('''UPDATE system_stats SET declined_checks = declined_checks + 1 WHERE date = ?''', (today,))
    
    conn.commit()
    conn.close()

# === TELEGRAM BOT HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    if not is_authorized(user_id):
        add_user(user_id, username)
    
    total_checks, approved_checks = get_user_stats(user_id)
    success_rate = (approved_checks / total_checks * 100) if total_checks > 0 else 0
    
    # Create professional keyboard
    keyboard = [
        ['🔍 Check CC', '📊 My Stats'],
        ['🛠️ Check Mode', 'ℹ️ Help']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_text = f"""
🏦 **BRAINTREE CC CHECKER BOT** 🚀

🤖 **Welcome to Professional CC Validation**
🔒 **Enterprise Grade Braintree Integration**

📈 **Your Statistics:**
• Total Checks: `{total_checks}`
• Approved: `{approved_checks}`
• Success Rate: `{success_rate:.1f}%`

💳 **Available Commands:**
• `🔍 Check CC` - Validate credit card
• `📊 My Stats` - Your check statistics  
• `🛠️ Check Mode` - Change check settings
• `ℹ️ Help` - Usage instructions

**Status:** ✅ **AUTHORIZED**
**Gateway:** 🌐 **Braintree**
    """
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def handle_check_cc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("🚫 **Access Denied**")
        return
    
    check_mode = context.user_data.get('check_mode', 'standard')
    amount = context.user_data.get('check_amount', 1.00)
    
    mode_text = "Standard Auth" if check_mode == 'standard' else "Zero Amount"
    amount_text = f"${amount:.2f}" if check_mode == 'standard' else "$0.00"
    
    instructions = f"""
💳 **CREDIT CARD CHECK** 🔍

**Mode:** {mode_text}
**Amount:** {amount_text}

📝 **Please send card details in format:**
`CardNumber|MMYY|CVV`

**Example:**
`4111111111111111|0125|123`

⚡ **Features:**
• Real-time Braintree validation
• Card type detection  
• Instant approval/decline
• Professional analytics
    """
    
    await update.message.reply_text(instructions)
    return 'AWAITING_CC_DETAILS'

async def handle_cc_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return ConversationHandler.END

    text = update.message.text
    parts = text.split('|')
    
    if len(parts) != 3:
        await update.message.reply_text(
            "❌ **Invalid Format**\n\n"
            "Please use: `CardNumber|MMYY|CVV`\n"
            "Example: `4111111111111111|0125|123`"
        )
        return 'AWAITING_CC_DETAILS'

    card_number, exp_date, cvv = parts[0].strip(), parts[1].strip(), parts[2].strip()
    
    # Validate basic card format
    if not card_number.isdigit() or not exp_date.isdigit() or not cvv.isdigit():
        await update.message.reply_text("❌ **Invalid characters** - Use only digits")
        return 'AWAITING_CC_DETAILS'
    
    # Get check mode and amount
    check_mode = context.user_data.get('check_mode', 'standard')
    amount = 0.00 if check_mode == 'zero' else context.user_data.get('check_amount', 1.00)
    
    # Send processing message
    progress_msg = await update.message.reply_text(
        f"🔍 **Processing CC Check**\n\n"
        f"**Card:** `{card_number[:6]}XXXXXX{card_number[-4:]}`\n"
        f"**Mode:** {'Zero Auth' if check_mode == 'zero' else 'Standard Auth'}\n"
        f"**Amount:** ${amount:.2f}\n\n"
        f"⏳ Contacting Braintree Gateway..."
    )
    
    try:
        # Perform the check
        result = await asyncio.get_event_loop().run_in_executor(
            None, advanced_braintree_check, card_number, exp_date, cvv, amount
        )
        
        # Log the result
        log_check_result(user_id, card_number, result)
        
        # Build result message
        result_text = f"""
💳 **CC CHECK RESULT** ✅

**Card:** `{card_number[:6]}XXXXXX{card_number[-4:]}`
**Expiry:** `{exp_date}`
**CVV:** `{cvv}`
**Type:** `{result.get('card_type', 'Unknown').upper()}`
**Response Time:** `{result.get('response_time', 0):.2f}s`

**Result:** {result['message']}

**Gateway:** 🌐 Braintree
**Mode:** {'Zero Amount' if check_mode == 'zero' else 'Standard Auth'}
        """
        
        if result.get('transaction_id'):
            result_text += f"\n**Transaction ID:** `{result['transaction_id']}`"
        
        if result.get('error_code') and result['error_code'] != 'N/A':
            result_text += f"\n**Error Code:** `{result['error_code']}`"
        
    except Exception as e:
        result_text = f"⚠️ **SYSTEM ERROR**\n\nError: {str(e)}"
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=progress_msg.message_id,
        text=result_text
    )
    
    return ConversationHandler.END

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    
    total_checks, approved_checks = get_user_stats(user_id)
    success_rate = (approved_checks / total_checks * 100) if total_checks > 0 else 0
    
    # Get today's stats
    conn = sqlite3.connect('braintree_bot.db')
    c = conn.cursor()
    today = datetime.now().date()
    
    c.execute('''SELECT total_checks, approved_checks FROM system_stats WHERE date = ?''', (today,))
    today_stats = c.fetchone()
    
    today_total = today_stats[0] if today_stats else 0
    today_approved = today_stats[1] if today_stats else 0
    
    conn.close()
    
    stats_text = f"""
📊 **YEAR STATISTICS** 📈

**Personal Stats:**
• Total Checks: `{total_checks}`
• Approved: `{approved_checks}`
• Declined: `{total_checks - approved_checks}`
• Success Rate: `{success_rate:.1f}%`

**Today's Activity:**
• Checks Today: `{today_total}`
• Approved Today: `{today_approved}`

**System Status:**
• Gateway: ✅ **Braintree**
• Database: ✅ **Connected**
• Bot: ✅ **Operational**
    """
    
    await update.message.reply_text(stats_text)

async def check_mode_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    
    current_mode = context.user_data.get('check_mode', 'standard')
    current_amount = context.user_data.get('check_amount', 1.00)
    
    mode_text = {
        'standard': 'Standard Authorization ($1.00)',
        'zero': 'Zero Amount Authorization ($0.00)',
        'custom': f'Custom Amount (${current_amount:.2f})'
    }
    
    keyboard = [
        [InlineKeyboardButton("🟢 Standard Auth - $1.00", callback_data="mode_standard")],
        [InlineKeyboardButton("🔵 Zero Auth - $0.00", callback_data="mode_zero")],
        [InlineKeyboardButton("🟣 Custom Amount", callback_data="mode_custom")],
        [InlineKeyboardButton("❌ Close", callback_data="mode_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    mode_text = f"""
🛠️ **CHECK MODE SETTINGS**

**Current Mode:** {mode_text[current_mode]}

**Available Modes:**
• 🟢 **Standard Auth** - $1.00 authorization (recommended)
• 🔵 **Zero Auth** - $0.00 authorization (stealth)
• 🟣 **Custom Amount** - Set custom auth amount

💡 **Tip:** Zero amount may not work with all banks but is less detectable.
    """
    
    await update.message.reply_text(mode_text, reply_markup=reply_markup)

async def handle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if not is_authorized(user_id):
        await query.edit_message_text("🚫 **Access Denied**")
        return
    
    if data == "mode_standard":
        context.user_data['check_mode'] = 'standard'
        context.user_data['check_amount'] = 1.00
        await query.edit_message_text("✅ **Mode Set:** Standard Authorization ($1.00)")
    
    elif data == "mode_zero":
        context.user_data['check_mode'] = 'zero' 
        context.user_data['check_amount'] = 0.00
        await query.edit_message_text("✅ **Mode Set:** Zero Amount Authorization ($0.00)")
    
    elif data == "mode_custom":
        await query.edit_message_text("💵 **Enter custom amount** (e.g., 0.50 for $0.50):")
        return 'AWAITING_CUSTOM_AMOUNT'
    
    elif data == "mode_close":
        await query.edit_message_text("⚙️ **Settings Closed**")
    
    return ConversationHandler.END

async def handle_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return ConversationHandler.END
    
    try:
        amount = float(update.message.text)
        if amount < 0 or amount > 100:
            await update.message.reply_text("❌ **Invalid amount** - Use between $0.00 and $100.00")
            return 'AWAITING_CUSTOM_AMOUNT'
        
        context.user_data['check_mode'] = 'custom'
        context.user_data['check_amount'] = amount
        
        await update.message.reply_text(f"✅ **Custom amount set:** ${amount:.2f}")
        
    except ValueError:
        await update.message.reply_text("❌ **Invalid format** - Use numbers only (e.g., 0.50)")
        return 'AWAITING_CUSTOM_AMOUNT'
    
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
ℹ️ **BRAINTREE CC CHECKER - HELP** 🆘

**📋 BASIC USAGE:**
1. Press `🔍 Check CC` or send /check
2. Send card details: `CardNumber|MMYY|CVV`
3. Get instant results from Braintree

**🎛️ CHECK MODES:**
• **Standard:** $1.00 authorization (most reliable)
• **Zero:** $0.00 authorization (stealth)  
• **Custom:** Set your own amount

**💳 CARD FORMAT:**