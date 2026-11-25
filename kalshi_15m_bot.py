"""
Kalshi 15-minute crypto bot.

Rule: in the last WINDOW_SECONDS of a 15-minute crypto market, buy the side
whose ask is between MIN_PRICE and MAX_PRICE (default $0.98 - $0.999).

Sizing: every trade is capped at MAX_TRADE_DOLLARS ($1,500), a FIXED amount.
It never grows with your balance, so profits are never reinvested. Realized
profit from each settled trade is added to a "banked" tally (state file + log)
that the bot never spends. Total money at risk in unsettled trades is also
capped at MAX_OPEN_EXPOSURE.

Defaults: DEMO environment, DRY_RUN on. In dry-run the bot simulates fills at
the ask and tracks simulated settlement P&L, so you can measure results first.
Setup:  pip install requests cryptography
Env:    KALSHI_KEY_ID=<api key id>   KALSHI_KEY_PATH=/path/to/private.pem
"""
import base64
import datetime as dt
import json
import logging
import math
import os
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------------------------------------------------------------- config ---
USE_DEMO = False          # False = real money
DRY_RUN = true
# True = simulate fills, place nothing

SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]  # edit as needed

MIN_PRICE = 0.98         # ask band, in dollars
MAX_PRICE = 0.999
WINDOW_SECONDS = 120     # only trade in the last 2 minutes
MIN_SECONDS_LEFT = 4     # don't fire so late the market closes mid-order

MAX_TRADE_DOLLARS = 25.00    # hard cap per trade, never scaled up
MAX_OPEN_EXPOSURE = 1500.00    # cap on total unsettled cost across ALL markets.
                               # 1500 = one trade at a time. Raise it if you want
                               # several markets at once (each still <= 1500).
FEE_BUFFER = 0.0               # no fee reserve: contracts are sized to the full $1,500
                               # of contract cost; fees are charged on top
MAX_DAILY_LOSS = 1500.00       # halt for the day once realized losses hit this

FAST_POLL = 1.5          # seconds between checks inside the window
STATE_FILE = "kalshi_15m_state.json"

HOSTS = {True: "https://demo-api.kalshi.co", False: "https://api.elections.kalshi.com"}
API_PREFIX = "/trade-api/v2"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler("kalshi_15m_bot.log")])
log = logging.getLogger("bot")


# ------------------------------------------------------------------ API ----
class Kalshi:
    def __init__(self, key_id, key_path, demo=True):
        self.key_id, self.base = key_id, HOSTS[demo]
        with open(key_path, "rb") as f:
            self.key = serialization.load_pem_private_key(f.read(), password=None)
        self.s = requests.Session()

    def _headers(self, method, path):
        ts = str(int(time.time() * 1000))
        sig = self.key.sign(
            (ts + method + path).encode(),          # path only, no query string
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "Content-Type": "application/json"}

    def request(self, method, endpoint, params=None, body=None):
        path = API_PREFIX + endpoint
        for attempt in range(4):
            r = self.s.request(method, self.base + path, params=params, json=body,
                               headers=self._headers(method, path), timeout=10)
            if r.status_code == 429:
                time.sleep(0.5 * 2 ** attempt)
                continue
            if not r.ok:
                log.error("HTTP %s on %s: %s", r.status_code, endpoint, r.text[:300])
                r.raise_for_status()
            return r.json()
        raise RuntimeError("rate limited repeatedly")

    def balance(self):
        b = self.request("GET", "/portfolio/balance")
        if "balance_dollars" in b:
            return float(b["balance_dollars"])
        return b["balance"] / 100.0                  # legacy: cents

    def open_markets(self, series):
        return self.request("GET", "/markets",
                            params={"series_ticker": series, "status": "open",
                                    "limit": 20}).get("markets", [])

    def market(self, ticker):
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def buy(self, ticker, side, count, price_str):
        body = {"ticker": ticker, "action": "buy", "side": side, "type": "limit",
                "count_fp": f"{count:.2f}",
                f"{side}_price_dollars": price_str,
                # take whatever is available at <= our price, cancel the rest
                # (a full $1,500 may be more than the book offers)
                "time_in_force": "immediate_or_cancel",
                "client_order_id": str(uuid.uuid4())}
        return self.request("POST", "/portfolio/orders", body=body)


# --------------------------------------------------------------- helpers ---
def dollars(m, field):
    v = m.get(f"{field}_dollars")
    if v not in (None, ""):
        return float(v)
    c = m.get(field)
    return c / 100.0 if c else None


def seconds_left(m):
    t = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
    return (t - dt.datetime.now(dt.timezone.utc)).total_seconds()


def pick_side(m):
    """Return (side, ask_price_str) if either side's ask is in the band."""
    for side in ("yes", "no"):
        ask = dollars(m, f"{side}_ask")
        if ask is not None and MIN_PRICE <= ask <= MAX_PRICE:
            raw = m.get(f"{side}_ask_dollars")
            return side, (raw if raw else f"{ask:.4f}")
    return None


def filled_count(res):
    o = res.get("order", res)
    for k in ("fill_count_fp", "fill_count"):
        if o.get(k) not in (None, ""):
            return float(o[k])
    return 0.0


def load_state():
    today = str(dt.date.today())
    s = {"day": today, "day_pnl": 0.0, "tickers": [], "halted": False,
         "open": [], "banked": 0.0, "wins": 0, "losses": 0}
    try:
        with open(STATE_FILE) as f:
            old = json.load(f)
        s.update(old)
        if old.get("day") != today:                   # new day: reset daily fields
            s.update({"day": today, "day_pnl": 0.0, "tickers": [], "halted": False})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def settle_open_trades(client, state):
    """Check unsettled trades; bank profit / record loss when a result is in."""
    still_open = []
    for t in state["open"]:
        try:
            result = client.market(t["ticker"]).get("result")
        except Exception:
            still_open.append(t)
            continue
        if result not in ("yes", "no"):
            still_open.append(t)
            continue
        won = result == t["side"]
        pnl = (t["count"] - t["cost"]) if won else -t["cost"]   # before fees
        state["day_pnl"] += pnl
        if won:
            state["banked"] += pnl          # profit is banked, never redeployed
            state["wins"] += 1
        else:
            state["losses"] += 1
        log.info("SETTLED%s %s %s | %s | pnl %+.2f | banked profit $%.2f | W/L %d/%d",
                 " [SIM]" if t.get("sim") else "", t["ticker"], t["side"].upper(),
                 "WIN" if won else "LOSS", pnl, state["banked"],
                 state["wins"], state["losses"])
    state["open"] = still_open


# ------------------------------------------------------------------ loop ---
def main():
    client = Kalshi(os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_KEY_PATH"],
                    demo=USE_DEMO)
    state = load_state()
    log.info("Start | demo=%s dry_run=%s balance=$%.2f cap/trade=$%.0f series=%s",
             USE_DEMO, DRY_RUN, client.balance(), MAX_TRADE_DOLLARS, SERIES)

    while True:
        try:
            if state["day"] != str(dt.date.today()):
                state = load_state()

            settle_open_trades(client, state)
            if state["day_pnl"] <= -MAX_DAILY_LOSS and not state["halted"]:
                state["halted"] = True
                log.warning("Daily loss limit hit (%.2f), halting until tomorrow",
                            state["day_pnl"])
            save_state(state)
            if state["halted"]:
                time.sleep(60)
                continue

            markets = []
            for s in SERIES:
                markets += client.open_markets(s)
            lefts = [(seconds_left(m), m) for m in markets]
            lefts = [(t, m) for t, m in lefts if t > 0]
            in_window = [(t, m) for t, m in lefts
                         if MIN_SECONDS_LEFT <= t <= WINDOW_SECONDS]

            for t, m in in_window:
                tk = m["ticker"]
                if tk in state["tickers"]:
                    continue
                hit = pick_side(m)
                if not hit:
                    continue
                side, price_str = hit
                price = float(price_str)

                # budget: never above the per-trade cap, the open-exposure cap,
                # or the cash actually in the account. Fixed, not balance-scaled.
                exposure = sum(x["cost"] for x in state["open"])
                budget = min(MAX_TRADE_DOLLARS, MAX_OPEN_EXPOSURE - exposure)
                if not DRY_RUN:
                    budget = min(budget, client.balance())
                count = math.floor(budget / (price + FEE_BUFFER))
                if count < 1:
                    continue

                log.info("MATCH %s | %ds left | %s x%d @ %s (budget $%.2f)",
                         tk, int(t), side.upper(), count, price_str, budget)
                if DRY_RUN:
                    filled = float(count)                    # assume full fill
                else:
                    filled = filled_count(client.buy(tk, side, count, price_str))
                    log.info("FILLED %.2f of %d", filled, count)

                state["tickers"].append(tk)                   # one shot per market
                if filled > 0:
                    state["open"].append({"ticker": tk, "side": side,
                                          "count": filled, "cost": filled * price,
                                          "sim": DRY_RUN})
                save_state(state)

            if in_window:
                time.sleep(FAST_POLL)
            elif lefts:
                nxt = min(t for t, _ in lefts)
                time.sleep(max(FAST_POLL, min(nxt - WINDOW_SECONDS - 2, 30)))
            else:
                time.sleep(10)

        except Exception:
            log.exception("Loop error (continuing)")
            time.sleep(3)


if __name__ == "__main__":
    main()
