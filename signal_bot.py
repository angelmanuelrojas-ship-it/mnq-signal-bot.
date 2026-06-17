#!/usr/bin/env python3
"""
MNQ Signal Bot — Detecta señales de entrada (RSI bajo + cerca de banda inferior +
debajo de EMA) sobre NQ=F (proxy de MNQ) y avisa por Telegram. Hace seguimiento
de la operación abierta hasta que toque Take-Profit, Stop-Loss, o se agote el
tiempo máximo, y avisa también el cierre.

Diseñado para correr cada 15 minutos vía GitHub Actions. El estado de la
operación abierta se persiste en state.json dentro del propio repo (se hace
commit automático al final de cada corrida si el estado cambió).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIGURACIÓN DE LA ESTRATEGIA (validada en 2 años: PF 2.00, drawdown $2524)
# ----------------------------------------------------------------------------
RSI_PERIOD = 13
BOLLINGER_PERIOD = 20
BOLLINGER_STDDEV = 2
EMA_PERIOD = 20

RSI_MAX_ENTRY = 20          # RSI debe ser <= a esto para entrar
BAND_DIST_MAX_PCT = 40      # % máx. distancia a banda inferior (0=banda, 100=banda superior)
EMA_POSITION_MAX_PCT = 0    # posición vs EMA debe ser <= a esto (precio bajo la EMA)

TAKE_PROFIT_PCT = 2.0       # %
STOP_LOSS_PCT = 0.7         # %
MAX_HOLD_CANDLES = 8        # velas de 1h

# Símbolo a descargar (proxy de MNQ). Mismo que se usó en el backtester.
SYMBOL = "NQ=F"

# MNQ contract specs
MNQ_MULTIPLIER = 2.0        # $ por punto de índice
TICK_SIZE = 0.25            # puntos
TICK_VALUE = 0.50           # $ por contrato

STATE_FILE = Path(__file__).parent / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ----------------------------------------------------------------------------
# DESCARGA DE DATOS (mismo patrón de fallback en cascada que las herramientas HTML)
# ----------------------------------------------------------------------------
def fetch_candles(symbol=SYMBOL, range_="10d", interval="1h"):
    """
    Descarga velas recientes desde Yahoo Finance. Usamos un rango corto (10
    días) porque el bot solo necesita las últimas ~200 velas para calcular
    los indicadores con suficiente warm-up, no todo el historial.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_, "interval": interval, "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MNQSignalBot/1.0)"}

    last_error = None

    # Intento directo primero
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        return _parse_yahoo_json(data)
    except Exception as e:
        last_error = e

    # Fallback vía proxies CORS públicos (igual que en las herramientas HTML)
    proxies = [
        "https://corsproxy.io/?url=",
        "https://api.allorigins.win/raw?url=",
        "https://thingproxy.freeboard.io/fetch/",
        "https://api.codetabs.com/v1/proxy?quest=",
    ]
    full_url = f"{url}?range={range_}&interval={interval}&includePrePost=false"
    for proxy in proxies:
        try:
            r = requests.get(proxy + full_url, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            return _parse_yahoo_json(data)
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"No se pudo descargar datos de {symbol}: {last_error}")


def _parse_yahoo_json(data):
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes = quote["close"]
    highs = quote["high"]
    lows = quote["low"]
    opens = quote["open"]

    candles = []
    for i in range(len(timestamps)):
        if closes[i] is None:
            continue
        candles.append({
            "ts": timestamps[i],
            "datetime": datetime.fromtimestamp(timestamps[i], tz=timezone.utc),
            "open": opens[i],
            "high": highs[i],
            "low": lows[i],
            "close": closes[i],
        })
    return candles


# ----------------------------------------------------------------------------
# INDICADORES
# ----------------------------------------------------------------------------
def compute_rsi(closes, period):
    """RSI estilo Wilder (suavizado), misma fórmula estándar usada en la herramienta HTML."""
    if len(closes) < period + 1:
        return [None] * len(closes)

    rsis = [None] * len(closes)
    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_from_avgs(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    rsis[period] = rsi_from_avgs(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsis[i] = rsi_from_avgs(avg_gain, avg_loss)

    return rsis


def compute_sma(values, period):
    sma = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        sma[i] = sum(window) / period
    return sma


def compute_ema(values, period):
    ema = [None] * len(values)
    multiplier = 2 / (period + 1)
    seed_window = [v for v in values[:period] if v is not None]
    if len(seed_window) < period:
        return ema
    ema[period - 1] = sum(seed_window) / period
    for i in range(period, len(values)):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def compute_bollinger(closes, period, stddev_mult):
    sma = compute_sma(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean = sma[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        upper[i] = mean + stddev_mult * std
        lower[i] = mean - stddev_mult * std
    return upper, lower


# ----------------------------------------------------------------------------
# DETECCIÓN DE SEÑAL
# ----------------------------------------------------------------------------
def detect_signal(candles):
    """
    Devuelve un dict con la info de la última vela cerrada si cumple la señal
    de entrada, o None si no hay señal. Usa la ÚLTIMA VELA CERRADA (no la vela
    en curso, que todavía puede cambiar), igual que haría un humano mirando
    el cierre de la hora.
    """
    closes = [c["close"] for c in candles]

    rsis = compute_rsi(closes, RSI_PERIOD)
    ema = compute_ema(closes, EMA_PERIOD)
    upper_band, lower_band = compute_bollinger(closes, BOLLINGER_PERIOD, BOLLINGER_STDDEV)

    # Última vela con todos los indicadores disponibles
    i = len(candles) - 1
    while i >= 0 and (rsis[i] is None or ema[i] is None or lower_band[i] is None):
        i -= 1
    if i < 0:
        return None

    close = closes[i]
    rsi = rsis[i]
    ema_val = ema[i]
    band_width = upper_band[i] - lower_band[i]
    if band_width <= 0:
        return None

    band_dist_pct = (close - lower_band[i]) / band_width * 100
    ema_position_pct = (close - ema_val) / ema_val * 100

    meets_rsi = rsi <= RSI_MAX_ENTRY
    meets_band = band_dist_pct <= BAND_DIST_MAX_PCT
    meets_ema = ema_position_pct <= EMA_POSITION_MAX_PCT

    candle = candles[i]

    if meets_rsi and meets_band and meets_ema:
        return {
            "candle_index": i,
            "candle_ts": candle["ts"],
            "datetime": candle["datetime"].isoformat(),
            "close": close,
            "rsi": round(rsi, 2),
            "band_dist_pct": round(band_dist_pct, 1),
            "ema_position_pct": round(ema_position_pct, 3),
        }
    return None


# ----------------------------------------------------------------------------
# SEGUIMIENTO DE OPERACIÓN ABIERTA
# ----------------------------------------------------------------------------
def check_open_trade(trade, candles):
    """
    Revisa las velas posteriores a la entrada para ver si ya tocó TP, SL, o
    se acabó el tiempo. Aplica la misma regla conservadora del backtester:
    si TP y SL caen en la misma vela, se asume que el SL se dispara primero.
    Devuelve (resultado_dict, sigue_abierta_bool).
    """
    entry_price = trade["entry_price"]
    tp_price = trade["tp_price"]
    sl_price = trade["sl_price"]
    entry_ts = trade["entry_candle_ts"]
    max_hold = trade["max_hold_candles"]

    # Buscar velas posteriores a la entrada
    candles_after = [c for c in candles if c["ts"] > entry_ts]

    for idx, c in enumerate(candles_after):
        velas_transcurridas = idx + 1
        hit_sl = c["low"] <= sl_price
        hit_tp = c["high"] >= tp_price

        if hit_sl:
            return ({
                "exit_reason": "Stop-Loss",
                "exit_price": sl_price,
                "candles_held": velas_transcurridas,
                "exit_datetime": c["datetime"].isoformat(),
            }, False)
        if hit_tp:
            return ({
                "exit_reason": "Target",
                "exit_price": tp_price,
                "candles_held": velas_transcurridas,
                "exit_datetime": c["datetime"].isoformat(),
            }, False)
        if velas_transcurridas >= max_hold:
            return ({
                "exit_reason": "Tiempo",
                "exit_price": c["close"],
                "candles_held": velas_transcurridas,
                "exit_datetime": c["datetime"].isoformat(),
            }, False)

    return (None, True)  # sigue abierta, todavía no hay suficientes velas nuevas


def compute_pnl(entry_price, exit_price, contracts=1):
    points = exit_price - entry_price
    gross_pnl = points * MNQ_MULTIPLIER * contracts
    return round(gross_pnl, 2)


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID; no se envía mensaje.")
        print("Mensaje que se hubiera enviado:\n", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=20)
    if not r.ok:
        print(f"[ERROR] Telegram respondió {r.status_code}: {r.text}")
    r.raise_for_status()


def format_entry_message(signal):
    tp_price = round(signal["close"] * (1 + TAKE_PROFIT_PCT / 100), 2)
    sl_price = round(signal["close"] * (1 - STOP_LOSS_PCT / 100), 2)
    dt_str = signal["datetime"].replace("T", " ").split("+")[0]
    return (
        "🟢 <b>SEÑAL DE ENTRADA — MNQ</b>\n\n"
        f"📅 Vela: {dt_str} UTC\n"
        f"💰 Precio entrada: <b>{signal['close']:.2f}</b>\n"
        f"📊 RSI({RSI_PERIOD}): {signal['rsi']}\n"
        f"📉 Dist. banda inferior: {signal['band_dist_pct']}%\n"
        f"📈 Posición vs EMA: {signal['ema_position_pct']}%\n\n"
        f"🎯 Take-Profit ({TAKE_PROFIT_PCT}%): <b>{tp_price}</b>\n"
        f"🛑 Stop-Loss ({STOP_LOSS_PCT}%): <b>{sl_price}</b>\n"
        f"⏱ Tiempo máximo: {MAX_HOLD_CANDLES} velas (1h c/u)\n\n"
        "<i>Estrategia validada en 2 años: PF 2.00, drawdown máx. histórico $2524 (1 contrato).</i>"
    )


def format_exit_message(trade, exit_info):
    pnl = compute_pnl(trade["entry_price"], exit_info["exit_price"])
    emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
    reason_emoji = {"Target": "🎯", "Stop-Loss": "🛑", "Tiempo": "⏱"}.get(exit_info["exit_reason"], "")
    dt_str = exit_info["exit_datetime"].replace("T", " ").split("+")[0]
    return (
        f"{emoji} <b>CIERRE DE OPERACIÓN — MNQ</b>\n\n"
        f"{reason_emoji} Salida por: <b>{exit_info['exit_reason']}</b>\n"
        f"📅 Vela de cierre: {dt_str} UTC\n"
        f"💰 Entrada: {trade['entry_price']:.2f} → Salida: {exit_info['exit_price']:.2f}\n"
        f"🕯 Velas en posición: {exit_info['candles_held']}\n\n"
        f"💵 <b>P&L bruto (1 contrato): {'+' if pnl >= 0 else ''}{pnl:.2f}</b>\n"
        "<i>No incluye comisión ni slippage. Esto es información generada automáticamente, "
        "no es una recomendación de inversión.</i>"
    )


# ----------------------------------------------------------------------------
# ESTADO PERSISTENTE
# ----------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"open_trade": None, "last_checked_ts": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Iniciando revisión de señal MNQ...")

    try:
        candles = fetch_candles()
    except Exception as e:
        print(f"[ERROR] No se pudieron descargar velas: {e}")
        sys.exit(1)

    if len(candles) < BOLLINGER_PERIOD + 5:
        print("[ERROR] No hay suficientes velas para calcular indicadores.")
        sys.exit(1)

    state = load_state()
    state_changed = False

    # 1. ¿Hay una operación abierta? Revisar si ya cerró.
    if state.get("open_trade"):
        trade = state["open_trade"]
        exit_info, still_open = check_open_trade(trade, candles)
        if not still_open:
            msg = format_exit_message(trade, exit_info)
            send_telegram_message(msg)
            print("Operación cerrada:", exit_info)
            state["open_trade"] = None
            state_changed = True
        else:
            print("Operación sigue abierta, sin cambios.")

    # 2. Si no hay operación abierta, buscar nueva señal.
    if not state.get("open_trade"):
        signal = detect_signal(candles)
        if signal:
            last_seen_ts = state.get("last_signal_ts")
            if last_seen_ts == signal["candle_ts"]:
                print("Señal ya notificada anteriormente para esta vela, se ignora.")
            else:
                tp_price = round(signal["close"] * (1 + TAKE_PROFIT_PCT / 100), 2)
                sl_price = round(signal["close"] * (1 - STOP_LOSS_PCT / 100), 2)

                msg = format_entry_message(signal)
                send_telegram_message(msg)
                print("Nueva señal enviada:", signal)

                state["open_trade"] = {
                    "entry_price": signal["close"],
                    "entry_candle_ts": signal["candle_ts"],
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "max_hold_candles": MAX_HOLD_CANDLES,
                }
                state["last_signal_ts"] = signal["candle_ts"]
                state_changed = True
        else:
            print("Sin señal en esta revisión.")

    state["last_checked_ts"] = int(time.time())
    if state_changed or True:  # siempre guardamos last_checked_ts
        save_state(state)

    print("Revisión completada.")


if __name__ == "__main__":
    main()
