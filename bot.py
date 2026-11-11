# --- START OF FILE bot.py ---

import os
import hmac
import hashlib
import json
import logging
import random
import asyncio
from urllib.parse import unquote
import asyncpg # ❗️ НОВАЯ БИБЛИОТЕКА
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import MenuButtonWebApp, WebAppInfo

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN", "9015659427:AAEHv6oruPJswEq_q4_F0999F_ixYtIz538")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_ID", "8340826085"))
GAME_URL = "https://ka3a3-sketch.github.io/arcade-hub/"

# ❗️ ВСТАВЬ СЮДА СВОЮ ССЫЛКУ ОТ NEON.TECH
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_YbPVS6EjgN7o@ep-gentle-mud-zafdlrmz-pooler.c-2.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8081))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None
active_mines_games = {}

# --- БАЗА ДАННЫХ (PostgreSQL) ---
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance DOUBLE PRECISION DEFAULT 0.0,
                demo_balance DOUBLE PRECISION DEFAULT 1000.0,
                referrer_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount DOUBLE PRECISION,
                wallet TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

async def get_user(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT balance, demo_balance FROM users WHERE user_id = $1", user_id)
        return {"balance": row['balance'], "demo_balance": row['demo_balance']}

async def update_balance(user_id, amount, is_demo=False):
    column = "demo_balance" if is_demo else "balance"
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE users SET {column} = {column} + $1 WHERE user_id = $2", amount, user_id)
        row = await conn.fetchrow(f"SELECT {column} FROM users WHERE user_id = $1", user_id)
        return row[0]

# --- ВАЛИДАЦИЯ ---
def validate_telegram_data(init_data: str) -> dict:
    if not init_data: return None
    try:
        parsed_data = {}
        for chunk in init_data.split('&'):
            if '=' in chunk:
                k, v = chunk.split('=', 1)
                parsed_data[k] = unquote(v)
        if "hash" not in parsed_data: return None
        hash_received = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        hash_calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hash_calculated == hash_received:
            return json.loads(parsed_data.get("user", "{}"))
        return None
    except Exception as e:
        return None

def calculate_mines_multiplier(total_tiles, mines, revealed):
    if revealed == 0: return 0.80
    probability = 1.0
    for i in range(revealed): probability *= (total_tiles - mines - i) / (total_tiles - i)
    return max(0.80, round((1.0 / probability) * 0.90, 2))

# --- API ЭНДПОИНТЫ ---
async def api_deposit(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    amount = float(data.get("amount", 0))
    if amount > 0:
        new_bal = await update_balance(user_data["id"], amount, is_demo=False)
        return web.json_response({"status": "success", "new_balance": new_bal})
    return web.json_response({"error": "Invalid amount"}, status=400)

async def api_get_balance(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response(await get_user(user_data.get("id")))

# ВЫВОД СРЕДСТВ
async def api_withdraw(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    
    user_id = user_data.get("id")
    data = await request.json()
    amount = float(data.get("amount", 0))
    wallet = data.get("wallet", "").strip()
    
    if amount < 100: return web.json_response({"error": "Минималка 100 GRAM"}, status=400)
    if not wallet: return web.json_response({"error": "Некорректный кошелек"}, status=400)

    async with db_pool.acquire() as conn:
        result = await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1", amount, user_id)
        if result == "UPDATE 0":
            return web.json_response({"error": "Недостаточно средств"}, status=400)
            
        row = await conn.fetchrow("INSERT INTO withdrawals (user_id, amount, wallet) VALUES ($1, $2, $3) RETURNING id", user_id, amount, wallet)
        withdraw_id = row['id']
        row_bal = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        new_bal = row_bal['balance']

    await bot.send_message(user_id, f"⏳ **Заявка на вывод принята**\nСумма: {amount} GRAM\nКошелек: `{wallet}`", parse_mode="Markdown")

    username = user_data.get("username", "Unknown")
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"wd_approve_{withdraw_id}")
    builder.button(text="❌ Отклонить", callback_data=f"wd_reject_{withdraw_id}")
    
    try:
        await bot.send_message(ADMIN_TELEGRAM_ID, f"🚨 **Новая заявка #{withdraw_id}**\nИгрок: @{username} (`{user_id}`)\nСумма: {amount} GRAM\nКошелек: `{wallet}`", reply_markup=builder.as_markup(), parse_mode="Markdown")
    except: pass

    return web.json_response({"status": "success", "new_balance": new_bal})

# (Остальные игровые роуты остаются без изменений, так как они используют функцию update_balance)
async def api_mines_start(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    user_id = user_data.get("id")
    data = await request.json()
    bet, mines_count, is_demo = float(data.get("bet", 0.25)), int(data.get("mines", 3)), bool(data.get("is_demo", False))
    db_user = await get_user(user_id)
    if (db_user["demo_balance"] if is_demo else db_user["balance"]) < bet: return web.json_response({"error": "Insufficient funds"}, status=400)
    new_bal = await update_balance(user_id, -bet, is_demo)
    board = ['safe'] * 25
    planted = 0
    while planted < mines_count:
        idx = random.randint(0, 24)
        if board[idx] == 'safe': board[idx] = 'mine'; planted += 1
    active_mines_games[user_id] = {"board": board, "bet": bet, "mines_count": mines_count, "revealed": 0, "is_demo": is_demo}
    return web.json_response({"status": "started", "new_balance": new_bal})

async def api_mines_click(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    user_id = user_data.get("id")
    if user_id not in active_mines_games: return web.json_response({"error": "No active game"}, status=400)
    game = active_mines_games[user_id]
    cell_index = int((await request.json()).get("index", -1))
    if game["board"][cell_index] == 'mine':
        board_copy = game["board"]; del active_mines_games[user_id]
        return web.json_response({"status": "loss", "board": board_copy})
    else:
        game["revealed"] += 1
        mult = calculate_mines_multiplier(25, game["mines_count"], game["revealed"])
        if game["revealed"] == (25 - game["mines_count"]):
            winnings = round(game["bet"] * mult, 2)
            new_bal = await update_balance(user_id, winnings, game["is_demo"])
            board_copy = game["board"]; del active_mines_games[user_id]
            return web.json_response({"status": "win", "winnings": winnings, "board": board_copy, "new_balance": new_bal})
        return web.json_response({"status": "continue", "multiplier": mult})

async def api_mines_cashout(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    user_id = user_data.get("id")
    if user_id not in active_mines_games: return web.json_response({"error": "No active game"}, status=400)
    game = active_mines_games[user_id]
    winnings = round(game["bet"] * calculate_mines_multiplier(25, game["mines_count"], game["revealed"]), 2)
    new_bal = await update_balance(user_id, winnings, game["is_demo"])
    board_copy = game["board"]; del active_mines_games[user_id]
    return web.json_response({"status": "cashed_out", "winnings": winnings, "board": board_copy, "new_balance": new_bal})

async def api_upgrade_play(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    user_id = user_data.get("id")
    data = await request.json()
    bet, chance, is_demo = float(data.get("bet", 0.25)), int(data.get("chance", 50)), bool(data.get("is_demo", False))
    db_user = await get_user(user_id)
    if (db_user["demo_balance"] if is_demo else db_user["balance"]) < bet: return web.json_response({"error": "Insufficient funds"}, status=400)
    await update_balance(user_id, -bet, is_demo)
    is_win = random.randint(1, 100) <= chance
    win_amount = round(bet * round(98.0 / chance, 2), 2) if is_win else 0.0
    new_bal = await update_balance(user_id, win_amount, is_demo)
    return web.json_response({"status": "success", "is_win": is_win, "win_amount": win_amount, "new_balance": new_bal})

async def api_escape_play(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    user_id = user_data.get("id")
    data = await request.json()
    bet, is_demo = float(data.get("bet", 0.25)), bool(data.get("is_demo", False))
    db_user = await get_user(user_id)
    if (db_user["demo_balance"] if is_demo else db_user["balance"]) < bet: return web.json_response({"error": "Insufficient funds"}, status=400)
    await update_balance(user_id, -bet, is_demo)
    is_win = random.random() < 0.40
    multiplier = round(random.uniform(1.20, 3.50), 2) if is_win else round(random.uniform(1.01, 1.49), 2)
    win_amount = round(bet * multiplier, 2) if is_win else 0.0
    new_bal = await update_balance(user_id, win_amount, is_demo)
    return web.json_response({"status": "success", "is_win": is_win, "multiplier": multiplier, "win_amount": win_amount, "new_balance": new_bal})

# --- БОТ ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await get_user(user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="Играть в Arcade Hub", web_app=WebAppInfo(url=GAME_URL))
    builder.button(text="Пополнить баланс", callback_data="deposit_ton")
    builder.adjust(1)
    await message.answer("Привет! Добро пожаловать в Arcade Hub.\nИгры на баланс GRAM.", reply_markup=builder.as_markup())

@dp.message(Command("addbalance"))
async def cmd_addbalance(message: types.Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID: return
    args = message.text.split()
    if len(args) < 2: return await message.answer("🛠 Использование: `/addbalance 500`", parse_mode="Markdown")
    try:
        amount = float(args[1])
        await get_user(message.from_user.id)
        new_bal = await update_balance(message.from_user.id, amount, is_demo=False)
        await message.answer(f"✅ Зачислено: `{amount}`\nНовый баланс: `{new_bal}`", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(lambda c: c.data == "deposit_ton")
async def process_payments(callback: types.CallbackQuery):
    await callback.message.answer("Пополнение доступно прямо внутри Web App приложения. Откройте игру и перейдите в Профиль -> Пополнить.")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("wd_"))
async def process_withdrawal_admin(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_TELEGRAM_ID: return await callback.answer("Нет прав", show_alert=True)
    action, wd_id = callback.data.split("_")[1], int(callback.data.split("_")[2])
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, amount, wallet, status FROM withdrawals WHERE id = $1", wd_id)
        if not row or row['status'] != 'pending': return await callback.answer("Заявка уже обработана!", show_alert=True)
            
        user_id, amount, wallet = row['user_id'], row['amount'], row['wallet']
        
        if action == "approve":
            await conn.execute("UPDATE withdrawals SET status = 'completed' WHERE id = $1", wd_id)
            try: await bot.send_message(user_id, f"✅ Вывод **{amount} GRAM** выполнен!\nКошелек: `{wallet}`", parse_mode="Markdown")
            except: pass
            await callback.message.edit_text(f"{callback.message.text}\n\n**СТАТУС:** ✅ ВЫПОЛНЕНА")
            
        elif action == "reject":
            await conn.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = $1", wd_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
            try: await bot.send_message(user_id, f"❌ Вывод **{amount} GRAM** отклонен. Средства возвращены.", parse_mode="Markdown")
            except: pass
            await callback.message.edit_text(f"{callback.message.text}\n\n**СТАТУС:** ❌ ОТКЛОНЕНА")
    await callback.answer()

async def main():
    await init_db()
    app = web.Application()
    app.router.add_get('/api/balance', api_get_balance)
    app.router.add_post('/api/deposit', api_deposit)
    app.router.add_post('/api/withdraw', api_withdraw)
    app.router.add_post('/api/mines/start', api_mines_start)
    app.router.add_post('/api/mines/click', api_mines_click)
    app.router.add_post('/api/mines/cashout', api_mines_cashout)
    app.router.add_post('/api/upgrade/play', api_upgrade_play)
    app.router.add_post('/api/escape/play', api_escape_play)
    
    import aiohttp_cors
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")})
    for route in list(app.router.routes()): cors.add(route)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    logging.info(f"API Server started at http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Меню", web_app=WebAppInfo(url=GAME_URL)))
    logging.info("Telegram Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())