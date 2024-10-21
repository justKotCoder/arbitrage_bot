import asyncio
import json
import websockets
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
import logging


API_TOKEN = 'Ваш токен'
ADMIN_ID = 'ваш id'

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
MIN_SPREAD = 0.5  # Минимальный спред в процентах
MIN_PROFIT = 1  # Минимальная прибыль в долларах

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ExchangeWebSocket:
    def __init__(self):
        self.prices = {}
        self.connections = {}
        self.tasks = {}

    async def connect_bybit(self):
        uri = "wss://stream.bybit.com/realtime"
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps({
                "op": "subscribe",
                "args": ["trade.BTCUSD"]
            }))
            self.connections['Bybit'] = websocket
            while True:
                response = await websocket.recv()
                data = json.loads(response)
                if 'data' in data and len(data['data']) > 0:
                    self.prices['Bybit'] = float(data['data'][0]['price'])

    async def connect_binance(self):
        uri = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        async with websockets.connect(uri) as websocket:
            self.connections['Binance'] = websocket
            while True:
                response = await websocket.recv()
                data = json.loads(response)
                self.prices['Binance'] = float(data['p'])

    async def connect_okx(self):
        uri = "wss://ws.okx.com:8443/ws/v5/public"
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps({
                "op": "subscribe",
                "args": [{"channel": "tickers", "instId": "BTC-USDT"}]
            }))
            self.connections['OKX'] = websocket
            while True:
                response = await websocket.recv()
                data = json.loads(response)
                if 'data' in data and len(data['data']) > 0:
                    self.prices['OKX'] = float(data['data'][0]['last'])

    async def start(self):
        self.tasks['Bybit'] = asyncio.create_task(self.connect_bybit())
        self.tasks['Binance'] = asyncio.create_task(self.connect_binance())
        self.tasks['OKX'] = asyncio.create_task(self.connect_okx())

    async def stop(self):
        for task in self.tasks.values():
            task.cancel()
        for connection in self.connections.values():
            await connection.close()


exchange_ws = ExchangeWebSocket()

# Глобальные переменные для управления арбитражем
arbitrage_running = False
arbitrage_task = None

# Параметры арбитража (теперь это словарь для удобства обновления)
arbitrage_params = {
    "MIN_SPREAD": 0.5,  # Минимальный спред в процентах
    "MIN_PROFIT": 10,  # Минимальная прибыль в долларах
    "MIN_VOLUME": 0.01,  # Минимальный объем в BTC
    "MAX_VOLUME": 1.0,  # Максимальный объем в BTC
    "MAX_FEE": 0.1,  # Максимальная комиссия в процентах
    "MIN_SPREAD_AGE": 60  # Минимальный возраст спреда в секундах
}

class ConfigStates(StatesGroup):
    waiting_for_value = State()


async def check_arbitrage():
    global arbitrage_running
    while arbitrage_running:
        if len(exchange_ws.prices) < 2:
            await asyncio.sleep(1)
            continue

        exchanges = list(exchange_ws.prices.keys())
        for i in range(len(exchanges)):
            for j in range(i + 1, len(exchanges)):
                exchange1 = exchanges[i]
                exchange2 = exchanges[j]
                price1 = exchange_ws.prices[exchange1]
                price2 = exchange_ws.prices[exchange2]

                spread = abs(price1 - price2) / min(price1, price2) * 100
                profit = abs(price1 - price2)

                if spread >= MIN_SPREAD and profit >= MIN_PROFIT:
                    if price1 < price2:
                        buy_exchange = exchange1
                        sell_exchange = exchange2
                    else:
                        buy_exchange = exchange2
                        sell_exchange = exchange1

                    message = (
                        f"Арбитражная возможность обнаружена!\n"
                        f"Купить на {buy_exchange} по цене {min(price1, price2):.2f}\n"
                        f"Продать на {sell_exchange} по цене {max(price1, price2):.2f}\n"
                        f"Спред: {spread:.2f}%\n"
                        f"Потенциальная прибыль: ${profit:.2f}"
                    )
                    await bot.send_message(ADMIN_ID, message)

        await asyncio.sleep(1)  # Проверка каждую секунду

async def start_arbitrage(message: types.Message):
    global arbitrage_running, arbitrage_task
    if arbitrage_running:
        await message.reply("Арбитраж уже запущен!")
        return

    arbitrage_running = True
    arbitrage_task = asyncio.create_task(check_arbitrage())
    await message.reply("Арбитраж запущен! Вы будете получать уведомления о возможностях.")

async def stop_arbitrage(message: types.Message):
    global arbitrage_running, arbitrage_task
    if not arbitrage_running:
        await message.reply("Арбитраж не запущен!")
        return

    arbitrage_running = False
    if arbitrage_task:
        arbitrage_task.cancel()
        arbitrage_task = None
    await message.reply("Арбитраж остановлен.")

@dp.message_handler(commands=['start', 'help'])
async def send_welcome(message: types.Message):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Получить цены", callback_data='get_prices'))
    keyboard.add(InlineKeyboardButton("Запустить арбитраж", callback_data='start_arbitrage'))
    keyboard.add(InlineKeyboardButton("Остановить арбитраж", callback_data='stop_arbitrage'))
    keyboard.add(InlineKeyboardButton("Настроить параметры", callback_data='configure'))
    await message.reply("Добро пожаловать в арбитражный бот!\nВыберите действие:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data in ['get_prices', 'start_arbitrage', 'stop_arbitrage', 'configure'])
async def process_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    if callback_query.data == 'get_prices':
        await get_prices(callback_query.message)
    elif callback_query.data == 'start_arbitrage':
        await start_arbitrage(callback_query.message)
    elif callback_query.data == 'stop_arbitrage':
        await stop_arbitrage(callback_query.message)
    elif callback_query.data == 'configure':
        await show_config_menu(callback_query.message)

async def get_prices(message: types.Message):
    prices_text = "Текущие цены:\n"
    for exchange, price in exchange_ws.prices.items():
        prices_text += f"{exchange}: {price:.2f}\n"
    await message.reply(prices_text)

async def show_config_menu(message: types.Message):
    keyboard = InlineKeyboardMarkup()
    for param in arbitrage_params:
        keyboard.add(InlineKeyboardButton(f"{param}: {arbitrage_params[param]}", callback_data=f'set_{param}'))
    await message.reply("Выберите параметр для настройки:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('set_'))
async def config_parameter(callback_query: types.CallbackQuery, state: FSMContext):
    param = callback_query.data[4:]  # Убираем 'set_' из начала
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, f"Введите новое значение для {param}:")
    await state.set_state(ConfigStates.waiting_for_value.state)
    await state.update_data(param=param)

@dp.message_handler(state=ConfigStates.waiting_for_value)
async def process_config_value(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    param = user_data['param']
    try:
        value = float(message.text)
        arbitrage_params[param] = value
        await message.reply(f"Параметр {param} успешно обновлен на {value}")
    except ValueError:
        await message.reply("Пожалуйста, введите числовое значение.")
    await state.finish()
    await show_config_menu(message)

async def on_startup(dp):
    await exchange_ws.start()

async def on_shutdown(dp):
    global arbitrage_running, arbitrage_task
    arbitrage_running = False
    if arbitrage_task:
        arbitrage_task.cancel()
    await exchange_ws.stop()


if __name__ == '__main__':
    try:
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
    except Exception as e:
        logger.error(f"Произошла ошибка: {e}", exc_info=True)