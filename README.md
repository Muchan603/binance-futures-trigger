# Binance Futures — Trigger Order Script

Скрипт для размещения и отмены стоп-ордеров на **Binance USDT-M Futures** в точный момент времени по UTC.

**Тип ордеров:** `TAKE_PROFIT_MARKET` — при достижении стоп-цены исполняется по рынку.

---

## Логика работы

```
В place_time UTC:
  для каждого символа параллельно:
    - получить текущую цену (bid/ask)
    - BUY  stop = цена × (1 - distance%)   ← ниже рынка
    - SELL stop = цена × (1 + distance%)   ← выше рынка
    - разместить стоп-ордер

В cancel_time UTC:
  отменить все размещённые ордера параллельно
```

---

## Быстрый старт

**1. Установить зависимости**
```bash
pip install -r requirements.txt
```

**2. Настроить config.json**

| Параметр | Описание | Пример |
|---|---|---|
| `api_key` | API ключ Binance Futures | `"abc123..."` |
| `api_secret` | API секрет | `"xyz789..."` |
| `symbols` | Список монет | `["BTCUSDT", "ETHUSDT"]` |
| `distance_pct` | Расстояние от цены, % | `1.0` |
| `volume_usdt` | Объём одного ордера, USDT | `10000` |
| `place_time_utc` | Время выставления (UTC) | `"09:05:02"` |
| `cancel_time_utc` | Время снятия (UTC) | `"09:05:10"` |
| `mode` | Режим: `both` / `buy_only` / `sell_only` | `"both"` |
| `testnet` | Тестовая сеть | `true` / `false` |

**3. Запустить**
```bash
python binance_trigger.py
```

---

## Пример вывода

```
09:05:01.998 | INFO     | Ожидание 09:05:02 UTC (через 0.002s)
09:05:02.001 | INFO     | ▶ ВЫСТАВЛЯЕМ | 09:05:02.001 UTC
09:05:02.087 | INFO     | ✅ ВЫСТАВЛЕНА  BTCUSDT      BUY  | trigger=64350.00 | qty=0.155 | id=123456
09:05:02.091 | INFO     | ✅ ВЫСТАВЛЕНА  ETHUSDT      BUY  | trigger=3148.20 | qty=3.170 | id=123457
09:05:02.093 | INFO     | ✅ ВЫСТАВЛЕНА  BTCUSDT      SELL | trigger=65650.00 | qty=0.155 | id=123458
09:05:10.001 | INFO     | ■ СНИМАЕМ    | 09:05:10.001 UTC
09:05:10.120 | INFO     | ✅ СНЯТА      BTCUSDT      BUY  | id=123456
09:05:10.122 | INFO     | ✅ СНЯТА      ETHUSDT      BUY  | id=123457
```

---

## Требования к API ключу

На Binance Futures создать ключ с разрешением **Futures Trading**.
IP whitelist — опционально.

Testnet ключи: [testnet.binancefuture.com](https://testnet.binancefuture.com)

---

## Технические детали

- **Асинхронность:** `asyncio` + `aiohttp` — все ордера выставляются и снимаются параллельно
- **Тайминг:** синхронизация с сервером Binance (offset), спин-лок на последние 50мс
- **Точность цены:** автоматический учёт `tick_size` и `step_size` для каждого символа
- **Надёжность:** ошибка на одной монете не останавливает остальные; повторная попытка снятия при неудаче
- **Логи:** консоль + файл `orders.log`

---

## Зависимости

```
aiohttp >= 3.9.0
```
Стандартная библиотека Python 3.11+: `asyncio`, `hashlib`, `hmac`, `json`, `logging`
