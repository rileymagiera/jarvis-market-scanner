
import os
from datetime import datetime, timezone

import yfinance as yf
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Jarvis Market Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_SYMBOLS = [
    "PLTR", "RKLB", "ASTS", "SOUN", "BBAI",
    "IONQ", "RGTI", "QBTS", "HIMS", "ONDS",
    "LUNR", "RDW", "ACHR", "JOBY", "SMCI",
    "NVDA", "AMD", "TSLA", "MARA", "RIOT",
    "CLSK", "IREN", "HOOD", "COIN", "SOFI",
    "OPEN", "SERV", "TEM", "OKLO", "SMR"
]

def get_symbols():
    configured = os.getenv("SCAN_SYMBOLS", "")
    if configured.strip():
        return list(dict.fromkeys(
            s.strip().upper()
            for s in configured.split(",") if s.strip()
        ))
    return DEFAULT_SYMBOLS


@app.get("/")
def home():
    return {
        "name": "Jarvis Market Scanner",
        "status": "online",
        "version": "1.0"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/scan")
def scan():
    results = []
    errors = []

    for symbol in get_symbols():
        try:
            data = yf.Ticker(symbol).history(
                period="3mo",
                interval="1d"
            )

            if data.empty or len(data) < 20:
                continue

            close = data["Close"]
            volume = data["Volume"]

            price = float(close.iloc[-1])
            previous = float(close.iloc[-2])
            avg_volume = float(volume.iloc[-21:-1].mean())
            current_volume = float(volume.iloc[-1])

            if price <= 0 or avg_volume <= 0:
                continue

            daily_change = ((price / previous) - 1) * 100
            volume_ratio = current_volume / avg_volume
            high_20 = float(close.iloc[-21:-1].max())
            breakout = price > high_20

            score = 0

            if daily_change >= 3:
                score += 20
            if daily_change >= 7:
                score += 15
            if volume_ratio >= 1.5:
                score += 20
            if volume_ratio >= 2:
                score += 15
            if breakout:
                score += 20
            if price > float(close.iloc[-5]):
                score += 10

            if score >= 30:
                results.append({
                    "symbol": symbol,
                    "price": round(price, 4),
                    "daily_change_pct": round(daily_change, 2),
                    "relative_volume": round(volume_ratio, 2),
                    "breakout": breakout,
                    "score": score,
                    "signal": (
                        "HIGH INTEREST" if score >= 70
                        else "WATCH" if score >= 45
                        else "EARLY WATCH"
                    )
                })

        except Exception as error:
            errors.append({
                "symbol": symbol,
                "error": str(error)
            })

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "scanner": "Jarvis",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols_scanned": len(get_symbols()),
        "candidates_found": len(results),
        "candidates": results,
        "errors": errors
    }
