#!/usr/bin/env python3
"""
Binance Futures — выставление и снятие отложенных заявок по времени
Тип ордера: TAKE_PROFIT_MARKET (trigger price → market execution)

BUY  trigger: ниже текущей цены на X%  (покупка на откате)
SELL trigger: выше текущей цены на X%  (продажа на росте)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# ── Логирование ───────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    fmt = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s"
    datefmt = "%H:%M:%S"
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler("orders.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)
    return logging.getLogger("binance_trigger")

log = setup_logging()

# ── Конфигурация ──────────────────────────────────────────────────────────────

BASE_URL     = "https://fapi.binance.com"
TESTNET_URL  = "https://testnet.binancefuture.com"
CONFIG_FILE  = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    required = ["api_key", "api_secret", "symbols", "distance_pct",
                "volume_usdt", "place_time_utc", "cancel_time_utc"]
    for key in required:
        if key not in cfg:
            raise ValueError(f"Отсутствует обязательный параметр в config.json: {key}")
    return cfg


# ── Подпись запросов ──────────────────────────────────────────────────────────

def sign_params(params: dict, secret: str) -> dict:
    """Добавляет HMAC-SHA256 подпись к параметрам запроса."""
    query = urllib.parse.urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return {**params, "signature": signature}


# ── Округление по шагам биржи ─────────────────────────────────────────────────

def floor_to_step(value: float, step: float) -> float:
    """Округление вниз по step_size (количество)."""
    if step <= 0:
        return value
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)


def round_to_tick(value: float, tick: float) -> float:
    """Округление по tick_size (цена)."""
    if tick <= 0:
        return value
    precision = max(0, round(-math.log10(tick)))
    return round(round(value / tick) * tick, precision)


def float_precision(step: float) -> int:
    """Количество знаков после запятой для форматирования."""
    if step <= 0:
        return 8
    return max(0, round(-math.log10(step)))


# ── Основной класс ────────────────────────────────────────────────────────────

class BinanceTrigger:

    def __init__(self, cfg: dict):
        self.api_key    = cfg["api_key"]
        self.api_secret = cfg["api_secret"]
        self.symbols    = [s.upper().strip() for s in cfg["symbols"]]
        self.dist_pct   = float(cfg["distance_pct"])
        self.volume_usd = float(cfg["volume_usdt"])
        self.place_time = cfg["place_time_utc"]    # "HH:MM:SS"
        self.cancel_time = cfg["cancel_time_utc"]  # "HH:MM:SS"
        self.mode       = cfg.get("mode", "both")  # buy_only / sell_only / both
        self.testnet    = cfg.get("testnet", False)
        self.base_url   = TESTNET_URL if self.testnet else BASE_URL

        self._time_offset_ms: int = 0
        self._exchange_info: dict = {}   # symbol → {tick, step, min_qty, price_prec, qty_prec}
        self._placed: list = []           # [(symbol, order_id, side)]
        self._session: aiohttp.ClientSession | None = None

    # ── Утилиты времени ───────────────────────────────────────────────────────

    async def sync_time(self):
        """Синхронизация с сервером Binance (усреднение 5 запросов)."""
        offsets = []
        for _ in range(5):
            t0 = time.monotonic()
            async with self._session.get(f"{self.base_url}/fapi/v1/time") as r:
                data = await r.json()
            t1 = time.monotonic()
            rtt_ms = (t1 - t0) * 1000
            server_ms = data["serverTime"]
            local_ms = time.time() * 1000
            offsets.append(server_ms - local_ms + rtt_ms / 2)
        self._time_offset_ms = int(sum(offsets) / len(offsets))
        log.info(f"Синхронизация времени: offset={self._time_offset_ms:+.1f}ms "
                 f"({'testnet' if self.testnet else 'mainnet'})")

    def server_time_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def now_utc(self) -> datetime:
        return datetime.fromtimestamp(self.server_time_ms() / 1000, tz=timezone.utc)

    # ── Exchange info ─────────────────────────────────────────────────────────

    async def load_exchange_info(self):
        """Загрузка tick_size, step_size, precision для всех символов."""
        async with self._session.get(f"{self.base_url}/fapi/v1/exchangeInfo") as r:
            data = await r.json()

        for sym_info in data["symbols"]:
            s = sym_info["symbol"]
            if s not in self.symbols:
                continue
            info = {}
            for f in sym_info["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    info["tick"]       = float(f["tickSize"])
                    info["price_prec"] = float_precision(float(f["tickSize"]))
                elif f["filterType"] == "LOT_SIZE":
                    info["step"]       = float(f["stepSize"])
                    info["min_qty"]    = float(f["minQty"])
                    info["qty_prec"]   = float_precision(float(f["stepSize"]))
            self._exchange_info[s] = info

        missing = [s for s in self.symbols if s not in self._exchange_info]
        if missing:
            log.warning(f"Символы не найдены на бирже: {missing}")
            self.symbols = [s for s in self.symbols if s in self._exchange_info]

        log.info(f"Exchange info загружен: {list(self._exchange_info.keys())}")

    # ── Получение цены ────────────────────────────────────────────────────────

    async def get_mid_price(self, symbol: str) -> tuple[float, float, float]:
        """Возвращает (bid, ask, mid)."""
        async with self._session.get(
            f"{self.base_url}/fapi/v1/ticker/bookTicker",
            params={"symbol": symbol}
        ) as r:
            d = await r.json()
        bid = float(d["bidPrice"])
        ask = float(d["askPrice"])
        return bid, ask, (bid + ask) / 2

    # ── Выставление одной заявки ──────────────────────────────────────────────

    async def place_order(self, symbol: str, side: str):
        """Выставить TAKE_PROFIT_MARKET заявку для символа и стороны."""
        try:
            info = self._exchange_info[symbol]
            bid, ask, mid = await self.get_mid_price(symbol)

            dist = self.dist_pct / 100
            if side == "BUY":
                stop_price = round_to_tick(mid * (1 - dist), info["tick"])
            else:
                stop_price = round_to_tick(mid * (1 + dist), info["tick"])

            quantity = floor_to_step(self.volume_usd / mid, info["step"])

            if quantity < info.get("min_qty", 0):
                log.error(f"[{symbol}] {side}: qty={quantity} < min_qty={info['min_qty']}, пропуск")
                return

            price_fmt = f"{stop_price:.{info['price_prec']}f}"
            qty_fmt   = f"{quantity:.{info['qty_prec']}f}"

            params = sign_params({
                "symbol":      symbol,
                "side":        side,
                "type":        "TAKE_PROFIT_MARKET",
                "stopPrice":   price_fmt,
                "quantity":    qty_fmt,
                "workingType": "LAST_PRICE",
                "timestamp":   self.server_time_ms(),
                "recvWindow":  5000,
            }, self.api_secret)

            async with self._session.post(
                f"{self.base_url}/fapi/v1/order",
                params=params,
                headers={"X-MBX-APIKEY": self.api_key}
            ) as r:
                resp = await r.json()

            if "orderId" in resp:
                self._placed.append((symbol, resp["orderId"], side))
                log.info(
                    f"✅ ВЫСТАВЛЕНА  {symbol:12s} {side:4s} | "
                    f"trigger={price_fmt} | qty={qty_fmt} | "
                    f"id={resp['orderId']} | {self.now_utc().strftime('%H:%M:%S.%f')[:12]} UTC"
                )
            else:
                log.error(f"❌ ОШИБКА выставления [{symbol}] {side}: {resp}")

        except Exception as e:
            log.error(f"❌ ИСКЛЮЧЕНИЕ [{symbol}] {side}: {e}")

    # ── Снятие одной заявки ───────────────────────────────────────────────────

    async def cancel_order(self, symbol: str, order_id: int, side: str, retry: bool = False):
        """Снять заявку по order_id. При ошибке — одна повторная попытка."""
        try:
            params = sign_params({
                "symbol":     symbol,
                "orderId":    order_id,
                "timestamp":  self.server_time_ms(),
                "recvWindow": 5000,
            }, self.api_secret)

            async with self._session.delete(
                f"{self.base_url}/fapi/v1/order",
                params=params,
                headers={"X-MBX-APIKEY": self.api_key}
            ) as r:
                resp = await r.json()

            status = resp.get("status", "")
            if status in ("CANCELED", "EXPIRED") or "orderId" in resp:
                tag = "🔁 ПОВТОР" if retry else "✅ СНЯТА"
                log.info(f"{tag}     {symbol:12s} {side:4s} | id={order_id}")
            else:
                log.error(f"❌ ОШИБКА снятия [{symbol}] {side} id={order_id}: {resp}")
                if not retry:
                    await asyncio.sleep(0.3)
                    await self.cancel_order(symbol, order_id, side, retry=True)

        except Exception as e:
            log.error(f"❌ ИСКЛЮЧЕНИЕ при снятии [{symbol}] {side} id={order_id}: {e}")

    # ── Точное ожидание до времени ────────────────────────────────────────────

    async def wait_until(self, time_str: str):
        """Ждём до HH:MM:SS UTC. Спин-лок на последние 50мс."""
        now = self.now_utc()
        target = now.replace(
            hour=int(time_str[0:2]),
            minute=int(time_str[3:5]),
            second=int(time_str[6:8]),
            microsecond=0
        )
        if target <= now:
            log.warning(f"Время {time_str} уже прошло ({now.strftime('%H:%M:%S.%f')[:12]} UTC)")
            return

        wait_sec = (target - now).total_seconds()
        log.info(f"Ожидание {time_str} UTC (через {wait_sec:.3f}s)")

        # Грубое ожидание
        if wait_sec > 1.0:
            await asyncio.sleep(wait_sec - 0.5)

        # Точный спин-лок
        target_ts = target.timestamp() - (self._time_offset_ms / 1000)
        while True:
            remaining = target_ts - time.time()
            if remaining <= 0.001:
                break
            await asyncio.sleep(max(0.001, min(remaining * 0.3, 0.05)))

    # ── Главный сценарий ──────────────────────────────────────────────────────

    async def run(self):
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        timeout   = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            self._session = session

            log.info("=" * 65)
            log.info(f"  Монеты    : {', '.join(self.symbols)}")
            log.info(f"  Расстояние: {self.dist_pct}%   Объём: {self.volume_usd} USDT")
            log.info(f"  Режим     : {self.mode}")
            log.info(f"  Выставить : {self.place_time} UTC")
            log.info(f"  Снять     : {self.cancel_time} UTC")
            log.info(f"  Сеть      : {'TESTNET ⚠' if self.testnet else 'MAINNET'}")
            log.info("=" * 65)

            # Подготовка
            await self.sync_time()
            await self.load_exchange_info()

            if not self.symbols:
                log.error("Нет доступных символов. Выход.")
                return

            # ── Фаза 1: выставление ───────────────────────────────────────
            await self.wait_until(self.place_time)
            log.info(f"▶ ВЫСТАВЛЯЕМ | {self.now_utc().strftime('%H:%M:%S.%f')[:12]} UTC")

            tasks = []
            for symbol in self.symbols:
                if self.mode in ("buy_only", "both"):
                    tasks.append(self.place_order(symbol, "BUY"))
                if self.mode in ("sell_only", "both"):
                    tasks.append(self.place_order(symbol, "SELL"))

            await asyncio.gather(*tasks)
            log.info(f"Выставлено заявок: {len(self._placed)}")

            if not self._placed:
                log.error("Ни одна заявка не выставлена. Выход.")
                return

            # ── Фаза 2: снятие ────────────────────────────────────────────
            await self.wait_until(self.cancel_time)
            log.info(f"■ СНИМАЕМ    | {self.now_utc().strftime('%H:%M:%S.%f')[:12]} UTC")

            await asyncio.gather(*[
                self.cancel_order(sym, oid, side)
                for sym, oid, side in self._placed
            ])

            log.info("✔ Готово. Все заявки обработаны.")


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        cfg = load_config()
        asyncio.run(BinanceTrigger(cfg).run())
    except KeyboardInterrupt:
        log.info("Прерывание пользователем.")
    except Exception as e:
        log.critical(f"Критическая ошибка: {e}", exc_info=True)
