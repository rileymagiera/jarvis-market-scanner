import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

STATE = Path("alert_state.json")
BASE_URL = os.getenv("JARVIS_SCANNER_URL", "https://jarvis-market-scanner.onrender.com").rstrip("/")
SENDER = os.environ["JARVIS_GMAIL_USER"]
PASSWORD = os.environ["JARVIS_GMAIL_APP_PASSWORD"].replace(" ", "")
RECIPIENT = os.environ["JARVIS_ALERT_TO"]


def email(subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SENDER, RECIPIENT, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(SENDER, PASSWORD)
        smtp.send_message(msg)


def main():
    if "--test" in sys.argv:
        email("Jarvis email test", "Jarvis successfully sent a test email. Email delivery is connected. This test does not confirm market scanning. Jarvis is recommendation-only and never places trades.")
        print("Test email sent.")
        return

    req = Request(BASE_URL + "/scan?asset=all", headers={"User-Agent": "JarvisAlertWorkflow/1.0"})
    with urlopen(req, timeout=240) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload.get("candidates"), list):
        raise RuntimeError("Scanner response did not contain candidates.")

    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    now = datetime.now(timezone.utc)
    selected, updates = [], {}
    for item in payload["candidates"]:
        score = int(item.get("score") or 0)
        volume = float(item.get("relative_volume") or 0)
        breakout = bool(item.get("breakout_20d") or item.get("breakout_24h"))
        if score < 60 or not (volume >= 1.5 or breakout):
            continue
        key = f'{item.get("asset_type", "asset")}:{item.get("symbol", "unknown")}'
        previous = state.get(key, {})
        previous_score = int(previous.get("score") or 0)
        last = previous.get("alerted_at")
        expired = True
        if last:
            try:
                expired = now - datetime.fromisoformat(last) >= timedelta(hours=24)
            except ValueError:
                pass
        if not previous or expired or (score >= 75 and previous_score < 75) or score >= previous_score + 12:
            selected.append(item)
            updates[key] = {"score": score, "alerted_at": now.isoformat()}

    if not selected:
        # Send one heartbeat per UTC day when there are no new qualifying setups.
        daily_key = now.date().isoformat()
        if state.get("daily_status_date") == daily_key:
            print("No new qualifying setups; daily status email already sent today.")
            return
        generated = str(payload.get("timestamp", now.isoformat()))
        candidates = payload.get("candidates_found", len(payload["candidates"]))
        errors = payload.get("errors") or []
        body = "\n".join([
            "JARVIS DAILY MARKET STATUS",
            "Generated: " + generated,
            "",
            "Jarvis completed its scheduled stock and crypto scan.",
            "No new setups met the current alert criteria today.",
            f"Candidates returned: {candidates}",
            f"Scanner errors reported: {len(errors)}",
            "",
            "This is a monitoring update, not a buy or sell instruction.",
            "Jarvis is recommendation-only and never places trades.",
        ])
        email("Jarvis daily status: no new setups", body)
        state["daily_status_date"] = daily_key
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
        print("Daily no-new-setups status email sent.")
        return

    lines = [
        "JARVIS MARKET SCANNER — NEW SETUPS",
        "Generated: " + str(payload.get("timestamp", now.isoformat())),
        "",
        "Research watchlist only. Not personalized financial advice. No trades are placed.",
        "Verify current price, liquidity, news and risk before making any decision.",
        "",
    ]
    for item in sorted(selected, key=lambda x: int(x.get("score") or 0), reverse=True):
        lines += [
            f'{item.get("asset_type", "asset").upper()} {item.get("symbol", "")} | Score {item.get("score", "n/a")}/100 | {item.get("signal", "WATCH")}',
            f'Price: {item.get("price", "n/a")} | Daily/24h change: {item.get("daily_change_pct", "n/a")}% | Relative volume: {item.get("relative_volume", "n/a")}x',
            f'Breakout: {item.get("breakout_20d", item.get("breakout_24h", False))} | Source: {item.get("data_source", "n/a")}',
        ]
        levels = item.get("levels") or {}
        if levels:
            lines.append(f'Watch range: {levels.get("watch_entry_low", "n/a")}–{levels.get("watch_entry_high", "n/a")} | Illustrative invalidation: {levels.get("invalidation_level", "n/a")}')
        for news in (item.get("news") or [])[:2]:
            if news.get("title"):
                lines.append("Headline: " + news["title"] + ((" — " + news["url"]) if news.get("url") else ""))
        lines.append("")
    lines += [f'Scan candidates: {payload.get("candidates_found", "n/a")}', f'Scan errors: {len(payload.get("errors") or [])}']
    email(f"Jarvis: {len(selected)} new market setup(s)", "\n".join(lines))
    state.update(updates)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Sent {len(selected)} alert(s).")


if __name__ == "__main__":
    main()
