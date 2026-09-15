"""
liquidity_sentry.py - Модуль предварительного расчета пулов ликвидности и триггерных событий (Passive Sentry -> Active Hunt).

Архитектура:
1. Заранее (на старте или при закрытии 1H/15m бара) вычисляет статические уровни:
   - Asian Range High / Low (сформированы к 06:00 UTC)
   - Previous Day High / Low (PDH / PDL, сформированы в 00:00 UTC)
   - Ключевые Swing Highs (BSL) и Swing Lows (SSL) на 1H и 15m/5m
2. Отслеживает текущую цену через тикер (без тяжелой загрузки свечей)
3. При пересечении уровня:
   - Генерирует SweepEvent (мгновенный Telegram-алерт)
   - Переводит инструмент в состояние HUNTING (ARMED) на заданное время (например 45-60 мин)
4. Предоставляет Telegram-интерфейс для команды /levels (визуальная карта с расстояниями в %)
"""

import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import json
import time
import html
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
from smartmoneyconcepts import smc

import config as cfg
from ict_advanced import compute_asian_ranges

DATA_DIR = getattr(cfg, "DATA_DIR", "data")
LIQUIDITY_MAP_FILE = os.path.join(DATA_DIR, "liquidity_map.json")
BOT_STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")


def load_liquidity_map() -> dict:
    """Загружает сохраненную карту ликвидности из JSON."""
    if os.path.exists(LIQUIDITY_MAP_FILE):
        try:
            with open(LIQUIDITY_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Ошибка чтения {LIQUIDITY_MAP_FILE}: {e}")
    return {"updated_at": "", "markets": {}}


def save_liquidity_map(data: dict):
    """Сохраняет карту ликвидности в JSON."""
    try:
        os.makedirs(os.path.dirname(LIQUIDITY_MAP_FILE), exist_ok=True)
        with open(LIQUIDITY_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Ошибка записи {LIQUIDITY_MAP_FILE}: {e}")


def load_bot_state() -> dict:
    """
    Загружает глобальное состояние ботов, активные охоты и историю свипов.
    Автоматически очищает протухшие (expired) охоты.
    """
    now_utc = datetime.now(timezone.utc)
    default_state = {
        "updated_at": "",
        "markets": {
            "crypto": {"symbols": {}},
            "moex": {"symbols": {}},
        },
        "recent_sweeps": [],
    }
    if not os.path.exists(BOT_STATE_FILE):
        return default_state

    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ Ошибка чтения {BOT_STATE_FILE}: {e}")
        return default_state

    if "markets" not in data:
        data["markets"] = {"crypto": {"symbols": {}}, "moex": {"symbols": {}}}
    if "recent_sweeps" not in data:
        data["recent_sweeps"] = []

    # Авто-экспирация неактивных охот
    changed = False
    for m_key in ("crypto", "moex"):
        symbols = data["markets"].get(m_key, {}).get("symbols", {})
        for sym, sinfo in symbols.items():
            if sinfo.get("state") in ("HUNTING", "ARMED"):
                hd = sinfo.get("hunt_data") or {}
                exp_str = hd.get("expires_at")
                if exp_str:
                    try:
                        exp_dt = pd.to_datetime(exp_str)
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.tz_localize("UTC")
                        else:
                            exp_dt = exp_dt.tz_convert("UTC")
                        if now_utc >= exp_dt:
                            sinfo["state"] = "SENTRY"
                            hd["status"] = "EXPIRED"
                            changed = True
                    except Exception:
                        pass

    if changed:
        save_bot_state(data)

    return data


def save_bot_state(data: dict):
    """Сохраняет состояние ботов в data/bot_state.json."""
    try:
        os.makedirs(os.path.dirname(BOT_STATE_FILE), exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Ошибка записи {BOT_STATE_FILE}: {e}")


def set_hunt_state(market: str, symbol: str, hunt_data: dict):
    """
    Фиксирует переход инструмента в режим Охоты за FVG (HUNTING/ARMED) на заданный срок.
    Сохраняет метаданные свипа для рекавери при рестарте и отображения в /status.
    """
    data = load_bot_state()
    now_utc = datetime.now(timezone.utc)
    if market not in data["markets"]:
        data["markets"][market] = {"symbols": {}}

    hunt_min = int(hunt_data.get("hunt_window_min") or getattr(cfg, "HUNT_TIMEOUT_MINUTES", 45))
    armed_at_dt = now_utc
    expires_at_dt = armed_at_dt + timedelta(minutes=hunt_min)

    hunt_entry = {
        "symbol": symbol,
        "market": market,
        "state": "HUNTING",
        "level_name": str(hunt_data.get("level_name") or "Уровень"),
        "level_type": str(hunt_data.get("level_type") or ""),
        "level_price": float(hunt_data.get("level_price") or 0.0),
        "trigger_price": float(hunt_data.get("trigger_price") or 0.0),
        "direction": int(hunt_data.get("direction") or 1),
        "dir_str": str(hunt_data.get("dir_str") or ("LONG" if hunt_data.get("direction") == 1 else "SHORT")),
        "sweep_time": str(hunt_data.get("sweep_time") or now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")),
        "armed_at": armed_at_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "expires_at": expires_at_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "hunt_window_min": hunt_min,
        "stop": float(hunt_data.get("stop") or 0.0),
        "status": "HUNTING",
    }

    data["markets"][market]["symbols"][symbol] = {
        "state": "HUNTING",
        "hunt_data": hunt_entry,
        "updated_at": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    # Добавляем в историю недавних свипов (дедупликация)
    recent = data.get("recent_sweeps", [])
    sw_key = f"{symbol}_{hunt_entry['sweep_time']}"
    filtered = [r for r in recent if f"{r.get('symbol')}_{r.get('sweep_time')}" != sw_key]
    filtered.insert(0, hunt_entry)
    data["recent_sweeps"] = filtered[:15]
    save_bot_state(data)

    # Синхронизация статуса в liquidity_map.json для /levels
    try:
        lmap = load_liquidity_map()
        if market in lmap.get("markets", {}) and symbol in lmap["markets"][market].get("symbols", {}):
            lmap["markets"][market]["symbols"][symbol]["state"] = "HUNTING"
            save_liquidity_map(lmap)
    except Exception:
        pass


def clear_hunt_state(market: str, symbol: str, reason: str = "SENTRY"):
    """
    Сбрасывает инструмент из режима Охоты обратно в Дозор (SENTRY) или Позицию (IN_TRADE).
    Обновляет статус свипа в истории.
    """
    data = load_bot_state()
    now_utc = datetime.now(timezone.utc)
    if market in data.get("markets", {}) and symbol in data["markets"][market].get("symbols", {}):
        sinfo = data["markets"][market]["symbols"][symbol]
        sinfo["state"] = "IN_TRADE" if reason == "ENTERED" else "SENTRY"
        if "hunt_data" in sinfo and sinfo["hunt_data"]:
            sinfo["hunt_data"]["status"] = reason
        sinfo["updated_at"] = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

    for r in data.get("recent_sweeps", []):
        if r.get("symbol") == symbol and r.get("status") == "HUNTING":
            r["status"] = reason
            break

    save_bot_state(data)

    try:
        lmap = load_liquidity_map()
        if market in lmap.get("markets", {}) and symbol in lmap["markets"][market].get("symbols", {}):
            lmap["markets"][market]["symbols"][symbol]["state"] = "IN_TRADE" if reason == "ENTERED" else "SENTRY"
            save_liquidity_map(lmap)
    except Exception:
        pass


def get_active_hunts() -> list[dict]:
    """Возвращает список всех инструментов, находящихся сейчас в режиме Охоты за FVG."""
    data = load_bot_state()
    now_utc = datetime.now(timezone.utc)
    active = []
    for m_key, m_val in data.get("markets", {}).items():
        for sym, sinfo in m_val.get("symbols", {}).items():
            if sinfo.get("state") == "HUNTING" and sinfo.get("hunt_data"):
                hd = dict(sinfo["hunt_data"])
                exp_str = hd.get("expires_at")
                if exp_str:
                    try:
                        exp_dt = pd.to_datetime(exp_str)
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.tz_localize("UTC")
                        else:
                            exp_dt = exp_dt.tz_convert("UTC")
                        rem_sec = (exp_dt - now_utc).total_seconds()
                        if rem_sec > 0:
                            hd["remaining_min"] = max(1, int(rem_sec // 60))
                            hd["market_name"] = "Крипта (Bitget)" if m_key == "crypto" else "Мосбиржа (Т-Банк)"
                            active.append(hd)
                    except Exception:
                        pass
    return active


def get_recent_sweeps(limit: int = 5) -> list[dict]:
    """Возвращает последние зафиксированные свипы ликвидности."""
    data = load_bot_state()
    return data.get("recent_sweeps", [])[:limit]


def extract_symbol_liquidity_levels(
    symbol: str,
    df_1h: pd.DataFrame,
    df_5m: pd.DataFrame,
    current_price: float,
    asian_hours: tuple = (0, 6),
    existing_levels: list = None,
) -> list[dict]:
    """
    Вычисляет статические пулы ликвидности для одного инструмента.
    Возвращает список словарей уровней:
    [
        {
            "id": "...",
            "type": "ASIAN_HIGH" | "ASIAN_LOW" | "PDH" | "PDL" | "1H_BSL" | "1H_SSL" | "5M_BSL" | "5M_SSL",
            "name": "...",
            "price": 139.50,
            "direction": -1 (SHORT) | 1 (LONG),
            "dir_str": "SHORT" | "LONG",
            "distance_pct": 2.5,
            "status": "ACTIVE" | "SWEPT",
            "created_at": "...",
            "swept_at": None,
            "swept_price": None,
        }
    ]
    """
    levels = []
    now_utc = datetime.now(timezone.utc)
    existing_swept_ids = set()
    existing_level_map = {}
    if existing_levels:
        for lvl in existing_levels:
            if lvl.get("status") == "SWEPT":
                existing_swept_ids.add(lvl.get("id"))
            existing_level_map[lvl.get("id")] = lvl

    def _make_lvl(lvl_id, lvl_type, name, price, direction, dir_str, status, dist_pct):
        prev = existing_level_map.get(lvl_id)
        sw_at = prev.get("swept_at") if (prev and prev.get("swept_at")) else (now_utc.strftime("%Y-%m-%d %H:%M:%S UTC") if status == "SWEPT" else None)
        sw_p = prev.get("swept_price") if (prev and prev.get("swept_price")) else (current_price if status == "SWEPT" else None)
        cr_at = prev.get("created_at") if (prev and prev.get("created_at")) else now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        return {
            "id": lvl_id,
            "type": lvl_type,
            "name": name,
            "price": price,
            "direction": direction,
            "dir_str": dir_str,
            "distance_pct": dist_pct,
            "status": status,
            "created_at": cr_at,
            "swept_at": sw_at,
            "swept_price": sw_p,
        }

    # 1. Asian Range High / Low (если есть 5m бары)
    if df_5m is not None and len(df_5m) >= 20:
        ar_dict = compute_asian_ranges(df_5m, asian_hours)
        today_date = now_utc.date()
        today_ar = ar_dict.get(today_date)
        # Если сегодня диапазон еще не готов, смотрим вчерашний
        if not today_ar and (today_date - timedelta(days=1)) in ar_dict:
            today_ar = ar_dict.get(today_date - timedelta(days=1))

        if today_ar:
            ar_h = round(float(today_ar["high"]), 4)
            ar_l = round(float(today_ar["low"]), 4)

            # Asian High (Buy-side liquidity -> sell after sweep)
            lvl_id = f"{symbol}_ASIAN_HIGH_{ar_h}"
            status = "SWEPT" if lvl_id in existing_swept_ids or current_price > ar_h * 1.0005 else "ACTIVE"
            dist_pct = round((ar_h - current_price) / current_price * 100, 2) if current_price > 0 else 0.0
            levels.append(_make_lvl(lvl_id, "ASIAN_HIGH", "Asian Range High (BSL)", ar_h, -1, "SHORT", status, dist_pct))

            # Asian Low (Sell-side liquidity -> buy after sweep)
            lvl_id = f"{symbol}_ASIAN_LOW_{ar_l}"
            status = "SWEPT" if lvl_id in existing_swept_ids or current_price < ar_l * 0.9995 else "ACTIVE"
            dist_pct = round((ar_l - current_price) / current_price * 100, 2) if current_price > 0 else 0.0
            levels.append(_make_lvl(lvl_id, "ASIAN_LOW", "Asian Range Low (SSL)", ar_l, 1, "LONG", status, dist_pct))

    # 2. Previous Day High / Low (PDH / PDL) из 1H баров
    if df_1h is not None and len(df_1h) >= 24:
        yesterday_date = (now_utc - timedelta(days=1)).date()
        df_yesterday = df_1h[df_1h.index.date == yesterday_date]
        if len(df_yesterday) >= 8:
            pdh = round(float(df_yesterday["high"].max()), 4)
            pdl = round(float(df_yesterday["low"].min()), 4)

            # PDH
            lvl_id = f"{symbol}_PDH_{pdh}"
            status = "SWEPT" if lvl_id in existing_swept_ids or current_price > pdh * 1.0005 else "ACTIVE"
            dist_pct = round((pdh - current_price) / current_price * 100, 2) if current_price > 0 else 0.0
            levels.append(_make_lvl(lvl_id, "PDH", "Previous Day High (PDH)", pdh, -1, "SHORT", status, dist_pct))

            # PDL
            lvl_id = f"{symbol}_PDL_{pdl}"
            status = "SWEPT" if lvl_id in existing_swept_ids or current_price < pdl * 0.9995 else "ACTIVE"
            dist_pct = round((pdl - current_price) / current_price * 100, 2) if current_price > 0 else 0.0
            levels.append(_make_lvl(lvl_id, "PDL", "Previous Day Low (PDL)", pdl, 1, "LONG", status, dist_pct))

    # 3. Старшие 1H Swing Highs / Lows (BSL / SSL)
    if df_1h is not None and len(df_1h) >= 15:
        try:
            swings_1h = smc.swing_highs_lows(df_1h, swing_length=3)
            # Неснятые максимумы выше текущей цены
            highs_above = swings_1h[(swings_1h["HighLow"] == 1) & (swings_1h["Level"] > current_price)]
            if not highs_above.empty:
                nearest_h = round(float(highs_above["Level"].iloc[-1]), 4)
                lvl_id = f"{symbol}_1H_BSL_{nearest_h}"
                dist_pct = round((nearest_h - current_price) / current_price * 100, 2)
                levels.append(_make_lvl(lvl_id, "1H_BSL", "1H Swing High (BSL)", nearest_h, -1, "SHORT", "ACTIVE", dist_pct))

            # Неснятые минимумы ниже текущей цены
            lows_below = swings_1h[(swings_1h["HighLow"] == -1) & (swings_1h["Level"] < current_price)]
            if not lows_below.empty:
                nearest_l = round(float(lows_below["Level"].iloc[-1]), 4)
                lvl_id = f"{symbol}_1H_SSL_{nearest_l}"
                dist_pct = round((nearest_l - current_price) / current_price * 100, 2)
                levels.append(_make_lvl(lvl_id, "1H_SSL", "1H Swing Low (SSL)", nearest_l, 1, "LONG", "ACTIVE", dist_pct))
        except Exception as e:
            pass

    # 4. Локальные 5m Swing Highs / Lows (BSL / SSL)
    if df_5m is not None and len(df_5m) >= 25:
        try:
            swings_5m = smc.swing_highs_lows(df_5m, swing_length=cfg.SWING_LENGTH_LTF)
            highs_above_5m = swings_5m[(swings_5m["HighLow"] == 1) & (swings_5m["Level"] > current_price)]
            if not highs_above_5m.empty:
                nearest_h_5m = round(float(highs_above_5m["Level"].iloc[-1]), 4)
                lvl_id = f"{symbol}_5M_BSL_{nearest_h_5m}"
                dist_pct = round((nearest_h_5m - current_price) / current_price * 100, 2)
                levels.append(_make_lvl(lvl_id, "5M_BSL", "5m Swing High (BSL)", nearest_h_5m, -1, "SHORT", "ACTIVE", dist_pct))

            lows_below_5m = swings_5m[(swings_5m["HighLow"] == -1) & (swings_5m["Level"] < current_price)]
            if not lows_below_5m.empty:
                nearest_l_5m = round(float(lows_below_5m["Level"].iloc[-1]), 4)
                lvl_id = f"{symbol}_5M_SSL_{nearest_l_5m}"
                dist_pct = round((nearest_l_5m - current_price) / current_price * 100, 2)
                levels.append(_make_lvl(lvl_id, "5M_SSL", "5m Swing Low (SSL)", nearest_l_5m, 1, "LONG", "ACTIVE", dist_pct))
        except Exception as e:
            pass

    # Устранение дубликатов (если уровни ближе 0.05% друг к другу)
    unique_levels = []
    for lvl in levels:
        duplicate = False
        for u in unique_levels:
            if u["direction"] == lvl["direction"] and abs(u["price"] - lvl["price"]) / max(u["price"], 0.0001) < 0.0005:
                if lvl["type"] not in u["type"]:
                    u["name"] = f"{u['name']} + {lvl['type']}"
                    u["type"] = f"{u['type']}+{lvl['type']}"
                duplicate = True
                break
        if not duplicate:
            unique_levels.append(lvl)

    return unique_levels


def check_price_triggers(
    symbol: str,
    current_price: float,
    levels: list[dict],
) -> list[dict]:
    """
    Проверяет, пробила ли текущая цена какой-либо из активных уровней ликвидности.
    Возвращает список событий свипов (SweepEvent).
    При срабатывании уровень помечается как SWEPT.
    """
    now_utc = datetime.now(timezone.utc)
    sweep_events = []

    for lvl in levels:
        if lvl.get("status") != "ACTIVE":
            continue

        target_price = float(lvl["price"])
        direction = lvl["direction"]

        # High Sweep (BSL): текущая цена поднялась выше уровня
        if direction == -1 and current_price >= target_price:
            lvl["status"] = "SWEPT"
            lvl["swept_at"] = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
            lvl["swept_price"] = current_price
            lvl["penetration_pct"] = round((current_price - target_price) / target_price * 100, 3)

            sweep_events.append({
                "symbol": symbol,
                "level_id": lvl["id"],
                "level_type": lvl["type"],
                "level_name": lvl["name"],
                "level_price": target_price,
                "trigger_price": current_price,
                "direction": -1,
                "dir_str": "SHORT",
                "time": now_utc.strftime("%H:%M:%S UTC"),
                "penetration_pct": lvl["penetration_pct"],
            })

        # Low Sweep (SSL): текущая цена опустилась ниже уровня
        elif direction == 1 and current_price <= target_price:
            lvl["status"] = "SWEPT"
            lvl["swept_at"] = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
            lvl["swept_price"] = current_price
            lvl["penetration_pct"] = round((target_price - current_price) / target_price * 100, 3)

            sweep_events.append({
                "symbol": symbol,
                "level_id": lvl["id"],
                "level_type": lvl["type"],
                "level_name": lvl["name"],
                "level_price": target_price,
                "trigger_price": current_price,
                "direction": 1,
                "dir_str": "LONG",
                "time": now_utc.strftime("%H:%M:%S UTC"),
                "penetration_pct": lvl["penetration_pct"],
            })

    return sweep_events


def format_liquidity_map_telegram(market_data: dict, market_name: str = "crypto") -> str:
    """
    Формирует интерактивную сводную карту ликвидности для Telegram-команды /levels.
    """
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    m_title = "КРИПТОВАЛЮТЫ (BITGET)" if market_name == "crypto" else "МОСБИРЖА (Т-БАНК)"

    lines = [
        f"🗺️ <b>КАРТА ПУЛОВ ЛИКВИДНОСТИ: {m_title}</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"⏱ <b>Время:</b> <code>{now_utc}</code> | <b>Режим:</b> 👁️ <i>Пассивный Дозор</i>\n",
    ]

    symbols_data = market_data.get("symbols", {})
    if not symbols_data:
        lines.append("<i>Карта уровней еще инициализируется... Подождите 5-10 сек.</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    for sym, sdata in symbols_data.items():
        curr_p = sdata.get("current_price", 0.0)
        state = sdata.get("state", "SENTRY")
        levels = sdata.get("levels", [])

        # Индикатор состояния
        if state == "HUNTING" or state == "ARMED":
            state_icon = "🏹 <b>ОХОТА ЗА FVG</b>"
        elif state == "IN_TRADE":
            state_icon = "💼 <b>В ПОЗИЦИИ</b>"
        else:
            state_icon = "👁️ <b>ДОЗОР</b>"

        short_sym = sym.replace(":USDT", "").replace("/USDT", "")
        lines.append(f"🪙 <b>{short_sym}</b> | Цена: <code>{curr_p:,.4f}</code> | {state_icon}")

        active_highs = [lvl for lvl in levels if lvl.get("direction") == -1 and lvl.get("status") == "ACTIVE"]
        active_lows = [lvl for lvl in levels if lvl.get("direction") == 1 and lvl.get("status") == "ACTIVE"]
        swept_recent = [lvl for lvl in levels if lvl.get("status") == "SWEPT"]

        # Выводим ближайшие уровни сверху (BSL)
        active_highs.sort(key=lambda x: x["price"])
        for h in active_highs[:2]:
            dist = round((h["price"] - curr_p) / curr_p * 100, 2) if curr_p > 0 else 0
            lines.append(f"  🔴 <b>BSL:</b> <code>{h['price']:,.4f}</code> (+{dist:.2f}%) — {h['name']}")

        # Выводим ближайшие уровни снизу (SSL)
        active_lows.sort(key=lambda x: x["price"], reverse=True)
        for l in active_lows[:2]:
            dist = round((l["price"] - curr_p) / curr_p * 100, 2) if curr_p > 0 else 0
            lines.append(f"  🟢 <b>SSL:</b> <code>{l['price']:,.4f}</code> ({dist:.2f}%) — {l['name']}")

        # Если недавно снят уровень
        if swept_recent:
            last_sw = swept_recent[-1]
            sw_val = str(last_sw.get("swept_at") or "")
            sw_time = f" ({sw_val.split(' ')[1]})" if " " in sw_val else (f" ({sw_val})" if sw_val else "")
            sw_p = float(last_sw.get("price") or 0.0)
            sw_name = str(last_sw.get("name") or "Уровень")
            lines.append(f"  ⚡ <i>Снят: {sw_name} @ {sw_p:,.4f}{sw_time}</i>")

        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>⚡ При проколе любого уровня бот мгновенно отправит алерт и включит охоту.</i>")
    return "\n".join(lines)


def format_bot_status_telegram() -> str:
    """
    Формирует полный институциональный дашборд состояния торговых ботов ICT Toolkit:
    - Текущие сессии и расписание бирж (Killzones, MOEX)
    - Активные инструменты в режиме Охоты за FVG (HUNTING/ARMED) с таймером обратного отсчета
    - Активные открытые позиции (Bitget / Т-Банк)
    - Инструменты в пассивном Дозоре (SENTRY)
    - Журнал последних свипов ликвидности
    """
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc + timedelta(hours=3)

    # Определение сессии
    hour_utc = now_utc.hour
    if 0 <= hour_utc < 6:
        session_name = "🌏 Asian Session (Азиатская сессия)"
    elif 7 <= hour_utc <= 10:
        session_name = "🇬🇧 London Killzone (Лондонское окно)"
    elif 12 <= hour_utc <= 15:
        session_name = "🇺🇸 New York Killzone (Нью-Йоркское окно)"
    else:
        session_name = "🌐 Вне основных Киллзон (Межсессионный мониторинг)"

    # Статус Мосбиржи
    is_weekend = now_msk.weekday() >= 5
    m_min = now_msk.hour * 60 + now_msk.minute
    moex_open = False
    if not is_weekend:
        if (10 * 60 <= m_min <= 18 * 60 + 40) or (19 * 60 + 5 <= m_min <= 23 * 60 + 50):
            moex_open = True
    moex_status = "🟢 Торги идут (Открыта)" if moex_open else "🔴 Торги закрыты"

    lines = [
        "⚡ <b>СТАТУС И ТЕКУЩИЙ РЕЖИМ БОТОВ (ICT SENTRY & HUNT)</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"⏱ <b>Время MSK:</b> <code>{now_msk.strftime('%Y-%m-%d %H:%M:%S')}</code>",
        f"⏱ <b>Время UTC:</b> <code>{now_utc.strftime('%H:%M:%S')}</code>",
        f"🎯 <b>Сессия:</b> {session_name}",
        f"🏛 <b>Мосбиржа:</b> {moex_status}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Активные охоты
    active_hunts = get_active_hunts()
    if active_hunts:
        lines.append(f"🏹 <b>АКТИВНЫЙ РЕЖИМ ОХОТЫ ЗА FVG ({len(active_hunts)}):</b>")
        for h in active_hunts:
            dir_icon = "🟢" if h.get("direction") == 1 else "🔴"
            rem_m = h.get("remaining_min", 0)
            exp_time = h.get("expires_at", "").split(" ")[1] if " " in h.get("expires_at", "") else h.get("expires_at", "")
            lines.append(
                f"{dir_icon} <b>{h['symbol']}</b> [{h.get('market_name', h.get('market', ''))}] — <b>{h.get('dir_str', 'LONG')}</b>\n"
                f"   ↳ 📍 Снят: <code>{h.get('level_name', 'Уровень')}</code> @ <code>{h.get('level_price', 0):,.4f}</code>\n"
                f"   ↳ ⚡ Прокол: <code>{h.get('trigger_price', 0):,.4f}</code> ({h.get('sweep_time', '')[:19]})\n"
                f"   ↳ ⏳ Окно охоты: <b>осталось {rem_m} мин.</b> (до {exp_time})\n"
                f"   ↳ 🎯 Ожидание: 1m Displacement + FVG ретест перед входом"
            )
        lines.append("")
    else:
        lines.append("🏹 <b>Режим Охоты (Active Hunt):</b>\n<i>В данный момент активных свипов в окне ожидания нет. Все пары в режиме Дозора.</i>\n")

    # Проверка открытых позиций
    def _read_json(fpath, default):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as _f:
                    return json.load(_f)
            except Exception:
                pass
        return default

    bg_trades_file = getattr(cfg, "TRADE_LOG_FILE", os.path.join(DATA_DIR, "live_trade_log.json"))
    tb_pos_file = getattr(cfg, "TBANK_ACTIVE_POSITIONS_FILE", os.path.join(DATA_DIR, "tbank_active_positions.json"))

    bg_trades = _read_json(bg_trades_file, [])
    bg_open = [t for t in bg_trades if isinstance(t, dict) and t.get("status") == "OPEN"]
    tb_pos = _read_json(tb_pos_file, {})
    tot_pos = len(bg_open) + len(tb_pos)

    lines.append(f"💼 <b>Открытые позиции:</b> <code>{tot_pos}</code> (Крипта: {len(bg_open)}/5 | Мосбиржа: {len(tb_pos)}/5)")
    if bg_open:
        for t in bg_open:
            lines.append(f"  • 🪙 <b>{t['symbol']}</b> ({t['direction']}) @ <code>{t['entry_price']:,.4f}</code>")
    if tb_pos and isinstance(tb_pos, dict):
        for tk, p in tb_pos.items():
            lines.append(f"  • 🇷🇺 <b>{tk}</b> ({p.get('dir', 'LONG')}) @ <code>{p.get('entry_price', 0):,.2f} RUB</code>")
    lines.append("")

    # Последние зафиксированные свипы
    recent_sw = get_recent_sweeps(limit=4)
    if recent_sw:
        lines.append("📜 <b>ЖУРНАЛ ПОСЛЕДНИХ СВИПОВ:</b>")
        for s in recent_sw:
            sw_st = s.get("status", "SWEPT")
            st_icon = "🏹" if sw_st == "HUNTING" else ("✅" if sw_st == "ENTERED" else "⚪")
            s_time = s.get("sweep_time", "")
            if " " in s_time:
                s_time = s_time.split(" ")[1][:5]
            lines.append(
                f"  {st_icon} <code>{s.get('symbol')}</code> ({s.get('dir_str', '')}): "
                f"{s.get('level_name', '')} @ <code>{s.get('level_price', 0):,.2f}</code> [{sw_st}] ({s_time})"
            )
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>💡 При проколе любого пула бот автоматически взводится на охоту и присылает алерт.</i>")
    return "\n".join(lines)
