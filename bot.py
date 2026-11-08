import os
import hmac
import hashlib
import json
import logging
import random
import asyncio
from urllib.parse import parse_qsl
import aiosqlite
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import MenuButtonWebApp, WebAppInfo

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN", "9015659427:AAEHv6oruPJswEq_q4_F0999F_ixYtIz538")
GAME_URL = "https://ka3a3-sketch.github.io/arcade-hub/"

# ВАЖНО: Настройки для работы на Render и других облачных сервисах
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8081))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

active_mines_games = {}

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect("database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                demo_balance REAL DEFAULT 1000.0,
                referrer_id INTEGER
            )
        """)
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect("database.db") as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.commit()
        async with db.execute("SELECT balance, demo_balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return {"balance": row[0], "demo_balance": row[1]}

async def update_balance(user_id, amount, is_demo=False):
    column = "demo_balance" if is_demo else "balance"
    async with aiosqlite.connect("database.db") as db:
        await db.execute(f"UPDATE users SET {column} = {column} + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        async with db.execute(f"SELECT {column} FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0]

# --- ВАЛИДАЦИЯ ---
def validate_telegram_data(init_data: str) -> dict:
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data: return None
        hash_received = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        hash_calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hash_calculated == hash_received:
            return json.loads(parsed_data.get("user", "{}"))
        return None
    except Exception:
        return None

# --- МАТЕМАТИКА МИН ---
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

async def api_mines_start(request):
    user_data = validate_telegram_data(request.headers.get("Authorization", ""))
    if not user_data: return web.json_response({"error": "Unauthorized"}, status=401)
    user_id = user_data.get("id")
    data = await request.json()
    bet, mines_count, is_demo = float(data.get("bet", 0.25)), int(data.get("mines", 3)), bool(data.get("is_demo", False))
    if bet < 0.25 or mines_count < 1 or mines_count > 24: return web.json_response({"error": "Invalid params"}, status=400)
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
    if cell_index < 0 or cell_index > 24: return web.json_response({"error": "Invalid cell"}, status=400)
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
    if game["revealed"] == 0: return web.json_response({"error": "Cannot cashout 0"}, status=400)
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
    if bet < 0.25 or chance < 30 or chance > 75: return web.json_response({"error": "Invalid params"}, status=400)
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
    if bet < 0.25: return web.json_response({"error": "Invalid params"}, status=400)
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
    args = message.text.split()
    await get_user(user_id)
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = args[1].replace("ref_", "")
        if str(referrer_id) != str(user_id):
            async with aiosqlite.connect("database.db") as db:
                await db.execute("UPDATE users SET referrer_id = ? WHERE user_id = ? AND referrer_id IS NULL", (referrer_id, user_id))
                await db.commit()

    builder = InlineKeyboardBuilder()
    builder.button(text="Играть в Arcade Hub", web_app=WebAppInfo(url=GAME_URL))
    builder.button(text="Пополнить баланс", callback_data="deposit_ton")
    builder.button(text="Вывести средства", callback_data="withdraw_ton")
    builder.adjust(1)
    await message.answer("Привет! Добро пожаловать в Arcade Hub.\nИгры на баланс GRAM.", reply_markup=builder.as_markup())

@dp.callback_query(lambda c: c.data in ["deposit_ton", "withdraw_ton"])
async def process_payments(callback: types.CallbackQuery):
    if callback.data == "deposit_ton":
        text = "Переведите TON/GRAM на системный кошелек:\n`UQC...`\nПосле перевода баланс обновится автоматически."
    else:
        text = "Вывод средств\nМинимум для вывода: 100 GRAM.\nОтправьте адрес кошелька:"
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

async def main():
    await init_db()
    app = web.Application()
    app.router.add_get('/api/balance', api_get_balance)
    app.router.add_post('/api/deposit', api_deposit)
    app.router.add_post('/api/mines/start', api_mines_start)
    app.router.add_post('/api/mines/click', api_mines_click)
    app.router.add_post('/api/mines/cashout', api_mines_cashout)
    app.router.add_post('/api/upgrade/play', api_upgrade_play)
    app.router.add_post('/api/escape/play', api_escape_play)
    
    import aiohttp_cors
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })
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
    import asyncio
    try:
        import aiohttp_cors
    except ImportError:
        import sys; print("Установите aiohttp_cors"); sys.exit(1)
    asyncio.run(main())