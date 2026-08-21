#!/usr/bin/env python3
"""
Lightweight Basketball Prediction Telegram Bot
Opens odds vs In-Play odds analysis | MONEYLINE + Total O/U
Optimized for 3G / low RAM / low CPU
"""

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import time
import threading
from collections import OrderedDict

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = "8995554775:AAGuxzPuR5kYKaqAt7MFGia5BcvFOk5enW8"
CHAT_ID = 7200809630
BASE_URL = "https://inforadar.live"
SPORT_ID = 18
SCAN_INTERVAL = 60
AUTO_START = True  # fully automatic on launch, no commands needed
NEW_GAME_ALERT = False  # notify when new games enter Q2
ALL_PREDICTIONS = False  # True = send all picks, False = only #1 best
CONFIDENCE_THRESHOLD = 60
MAX_GAMES_IN_MEMORY = 20
REQUEST_TIMEOUT = 8
MAX_WORKERS = 3

# ─── HTTP SESSION (keep-alive, gzip, 3G optimized) ───────────────────────────
_session = requests.Session()
_session.headers.update({"Accept": "application/json", "Accept-Encoding": "gzip"})
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=3, pool_maxsize=6, max_retries=1
))


def api_get(path, params=None):
    try:
        r = _session.get(f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ─── LRU CACHE ────────────────────────────────────────────────────────────────
class LRUCache:
    def __init__(self, maxsize=MAX_GAMES_IN_MEMORY):
        self._cache = OrderedDict()
        self._maxsize = maxsize

    def get(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def clear(self):
        self._cache.clear()


opening_cache = LRUCache()

# ─── BOT STATE ────────────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN)
auto_tracking = False
scan_thread = None
scan_count = 0
pred_count = 0
known_games = set()  # track seen game IDs for new-game alerts
last_predictions = {}  # dedup: key -> last confidence sent


# ─── DATA FETCHING ────────────────────────────────────────────────────────────
def fetch_live_games():
    data = api_get("/api/v1/live_games", {"sport_id": SPORT_ID, "page": 1, "per_page": 1000})
    if not data or not data.get("success"):
        return []
    return data.get("results", [])


def fetch_odds(event_id):
    data = api_get("/api/v1/basketball/game/odds", {
        "event_id": event_id, "odds_market": "1,2,3,4,5,6"
    })
    if not data or not isinstance(data, list):
        return []
    return data


def fetch_game_view(event_id):
    return api_get("/api/v1/basketball/game/view", {"event_id": event_id})


# ─── PARSING ──────────────────────────────────────────────────────────────────
def parse_markets(odds_data):
    markets = {}
    for m in odds_data:
        name = m.get("name", "")
        odds_list = m.get("odds", [])
        if not odds_list:
            continue
        opening_entries = [o for o in odds_list if o.get("game_time") == ""]
        inplay_entries = [o for o in odds_list if o.get("game_time") != ""]
        markets[name] = {
            "rows": m.get("rowsNames", []),
            "full_time": m.get("fullTime", 2400),
            "opening": opening_entries[-1] if opening_entries else None,
            "latest": inplay_entries[0] if inplay_entries else None,
            "inplay_count": len(inplay_entries),
        }
    return markets


def implied_prob(odds_decimal):
    if not odds_decimal or odds_decimal <= 1.01:
        return 1.0 if odds_decimal and odds_decimal <= 1.01 else 0.0
    return 1.0 / odds_decimal


def get_quarter(game):
    try: return int(game.get("time", {}).get("q", 0))
    except (ValueError, TypeError): return 0


def get_elapsed_sec(game):
    t = game.get("time", {})
    try:
        q = int(t.get("q", 0))
        tm = int(t.get("tm", 0))
        ts = int(t.get("ts", 0))
        qt = t.get("qTime", 10)
        elapsed = (q - 1) * qt * 60 + (qt * 60 - tm * 60 - ts)
        return max(0, min(elapsed, 2400))
    except (ValueError, TypeError):
        return 0


def get_scores(game):
    try:
        p = game.get("scores", "0-0").split("-")
        return int(p[0]), int(p[1])
    except (ValueError, IndexError, AttributeError):
        return 0, 0


def fmt_time(game):
    t = game.get("time", {})
    return f"Q{t.get('q','?')} {t.get('tm','0')}:{str(t.get('ts','0')).zfill(2)}"


def fmt_num(v):
    if v is None: return "N/A"
    if isinstance(v, float) and v == int(v): return str(int(v))
    return str(v)


# ─── PREDICTION ENGINE ────────────────────────────────────────────────────────
def predict_moneyline(markets):
    ml = markets.get("1X2")
    if not ml or not ml["opening"] or not ml["latest"]:
        return None
    op, cur = ml["opening"], ml["latest"]
    op_h, op_a = op.get("row1"), op.get("row3")
    cur_h, cur_a = cur.get("row1"), cur.get("row3")
    if not all([op_h, op_a, cur_h, cur_a]):
        return None
    if cur_h < 1.05 or cur_a < 1.05:
        return None

    op_hp, op_ap = implied_prob(op_h), implied_prob(op_a)
    cur_hp, cur_ap = implied_prob(cur_h), implied_prob(cur_a)
    h_shift = cur_hp - op_hp
    a_shift = cur_ap - op_ap

    if abs(h_shift) < 0.03 and abs(a_shift) < 0.03:
        return None

    if h_shift > a_shift:
        pick, shift_pct = "HOME", h_shift * 100
    else:
        pick, shift_pct = "AWAY", a_shift * 100

    raw_conf = min(95, 30 + abs(shift_pct) * 2.5)

    # Cross-validate with Handicap rating
    rating_boost = 0
    hc = markets.get("Handicap")
    if hc and hc["latest"]:
        for r in hc["latest"].get("rating", []):
            if r and isinstance(r, dict):
                rd = r.get("direction", "")
                rv = abs(r.get("rating", 0))
                if (pick == "HOME" and rd == "Home") or (pick == "AWAY" and rd == "Away"):
                    rating_boost = min(10, rv * 2)
                    break

    # Also cross-validate with HT 1X2 direction
    ht_ml = markets.get("HT 1X2")
    if ht_ml and ht_ml["latest"]:
        ht_op, ht_cur = ht_ml["opening"], ht_ml["latest"]
        ht_op_h, ht_op_a = ht_op.get("row1"), ht_op.get("row3")
        ht_cur_h, ht_cur_a = ht_cur.get("row1"), ht_cur.get("row3")
        if all([ht_op_h, ht_op_a, ht_cur_h, ht_cur_a]):
            ht_h_shift = implied_prob(ht_cur_h) - implied_prob(ht_op_h)
            ht_a_shift = implied_prob(ht_cur_a) - implied_prob(ht_op_a)
            ht_pick = "HOME" if ht_h_shift > ht_a_shift else "AWAY"
            if ht_pick == pick:
                rating_boost = min(rating_boost + 5, 12)

    confidence = min(98, raw_conf + rating_boost)
    details = {
        "op_home": op_h, "op_away": op_a,
        "cur_home": cur_h, "cur_away": cur_a,
        "home_shift": round(h_shift * 100, 1),
        "away_shift": round(a_shift * 100, 1),
        "rating_boost": round(rating_boost, 1),
    }
    return (pick, round(confidence, 1), details)


def predict_total(markets, game):
    tot = markets.get("Total")
    if not tot or not tot["opening"] or not tot["latest"]:
        return None
    op, cur = tot["opening"], tot["latest"]
    op_line, cur_line = op.get("row2"), cur.get("row2")
    cur_over, cur_under = cur.get("row1"), cur.get("row3")
    if not all([op_line, cur_line, cur_over, cur_under]):
        return None

    line_move = cur_line - op_line
    hs, as_ = get_scores(game)
    total_score = hs + as_
    elapsed = get_elapsed_sec(game)
    full_time = tot.get("full_time", 2400)

    if elapsed < 300:
        return None

    projected = (total_score / elapsed) * full_time
    pace_gap = projected - cur_line

    over_prob, under_prob = implied_prob(cur_over), implied_prob(cur_under)

    score_sig = 1 if pace_gap > 3 else (-1 if pace_gap < -3 else 0)
    line_sig = -1 if line_move < -2 else (1 if line_move > 2 else 0)
    odds_sig = 1 if over_prob > under_prob else -1
    combined = score_sig * 3 + line_sig * 2 + odds_sig * 1

    if combined == 0:
        return None

    pick = "OVER" if combined > 0 else "UNDER"

    raw_conf = 35
    raw_conf += min(25, abs(pace_gap) * 2)
    raw_conf += min(20, abs(line_move) * 3)
    raw_conf += min(10, abs(combined) * 3)

    rating_boost = 0
    for r in cur.get("rating", []):
        if r and isinstance(r, dict):
            rd = r.get("direction", "")
            if (pick == "OVER" and rd == "Over") or (pick == "UNDER" and rd == "Under"):
                rating_boost = min(10, abs(r.get("rating", 0)) * 3)
                break
            else:
                rating_boost = -5

    # Cross-validate with HT Total direction
    ht_tot = markets.get("HT Total")
    if ht_tot and ht_tot["latest"]:
        ht_op_line = ht_tot["opening"].get("row2") if ht_tot["opening"] else None
        ht_cur_line = ht_tot["latest"].get("row2")
        if ht_op_line and ht_cur_line:
            ht_line_move = ht_cur_line - ht_op_line
            if (pick == "OVER" and ht_line_move > 1) or (pick == "UNDER" and ht_line_move < -1):
                rating_boost = min(rating_boost + 4, 12)

    confidence = max(30, min(95, raw_conf + rating_boost))
    details = {
        "op_line": op_line, "cur_line": cur_line,
        "line_move": round(line_move, 1),
        "current_score": total_score,
        "elapsed_min": round(elapsed / 60, 1),
        "projected_total": round(projected, 1),
        "pace_gap": round(pace_gap, 1),
        "cur_over_odds": cur_over, "cur_under_odds": cur_under,
        "rating_boost": rating_boost,
    }
    return (pick, round(confidence, 1), cur_line, details)


def analyze_game(game, odds_data):
    markets = parse_markets(odds_data)
    preds = []
    if get_quarter(game) < 2:
        return preds

    ml = predict_moneyline(markets)
    if ml:
        pick, conf, details = ml
        if conf >= CONFIDENCE_THRESHOLD:
            preds.append({"type": "MONEYLINE", "pick": pick, "confidence": conf, "details": details})

    tot = predict_total(markets, game)
    if tot:
        pick, conf, line, details = tot
        if conf >= CONFIDENCE_THRESHOLD:
            preds.append({"type": "TOTAL", "pick": pick, "line": line, "confidence": conf, "details": details})

    preds.sort(key=lambda x: x["confidence"], reverse=True)
    return preds, markets


# ─── MESSAGE FORMATTING ───────────────────────────────────────────────────────
def fmt_pred(game, pred, rank=1):
    home = game["home"]["name"]
    away = game["away"]["name"]
    league = game["league"]["name"]
    icon = "\U0001f525" if rank == 1 else "\u26a1"
    lines = [
        f"{icon} #{rank} {pred['type']}",
        f"{home} vs {away}",
        f"{league} | {fmt_time(game)} | {game.get('scores', '0-0')}",
        f"Confidence: {pred['confidence']}%",
        "-" * 30,
    ]
    d = pred["details"]
    if pred["type"] == "MONEYLINE":
        lines.append(f"Pick: <b>{pred['pick']}</b>")
        lines.append(f"Opening: H {d['op_home']} / A {d['op_away']}")
        lines.append(f"Current: H {d['cur_home']} / A {d['cur_away']}")
        hs = f"+{d['home_shift']}%" if d['home_shift'] > 0 else f"{d['home_shift']}%"
        as_ = f"+{d['away_shift']}%" if d['away_shift'] > 0 else f"{d['away_shift']}%"
        lines.append(f"Shift: Home {hs} | Away {as_}")
        rb = round(d['rating_boost'], 1)
        if rb > 0:
            lines.append(f"Rating confirm: +{rb}%")
    else:
        lines.append(f"Pick: <b>{pred['pick']} {pred['line']}</b>")
        lines.append(f"Line: {d['op_line']} -> {d['cur_line']} ({d['line_move']:+.1f})")
        lines.append(f"Score: {d['current_score']}pts in {d['elapsed_min']}min")
        lines.append(f"Pace: {d['projected_total']}pts (gap {d['pace_gap']:+.1f})")
        lines.append(f"Odds: Over {d['cur_over_odds']} / Under {d['cur_under_odds']}")
        rb = round(d['rating_boost'], 1)
        if rb > 0:
            lines.append(f"Rating confirm: +{rb}%")
        elif rb < 0:
            lines.append(f"Rating disagree: {rb}%")
    return "\n".join(lines), game["id"]


def send_pred_with_button(game, pred, rank=1):
    """Send prediction message with clickable 'Match Details' button."""
    text, eid = fmt_pred(game, pred, rank=rank)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"\U0001f4c5 Match Details ({eid})", callback_data=f"detail_{eid}"))
    bot.send_message(CHAT_ID, text, reply_markup=markup, parse_mode="HTML")


def fmt_all(results):
    if not results:
        return "No high-confidence predictions found.\nGames may be too early (need Q2+) or odds haven't moved enough."
    lines = [f"\U0001f525 <b>LIVE PREDICTIONS</b> ({len(results)} picks)", ""]
    for i, (g, p) in enumerate(results):
        txt, _ = fmt_pred(g, p, rank=i + 1)
        lines.append(txt)
        lines.append("")
    return "\n".join(lines)


def fmt_detail(game, markets, preds):
    home = game["home"]["name"]
    away = game["away"]["name"]
    lines = [
        f"\U0001f3c0 <b>{home} vs {away}</b>",
        f"{game['league']['name']} | {fmt_time(game)} | Score: {game.get('scores', '0-0')}",
        "",
    ]
    for mname in ["1X2", "Handicap", "Total", "HT 1X2", "HT Handicap", "HT Total"]:
        m = markets.get(mname)
        if not m:
            lines.append(f"<b>{mname}</b>: No data")
            continue
        op, cur, rows = m["opening"], m["latest"], m["rows"]
        if not op or not cur:
            lines.append(f"<b>{mname}</b>: Partial data")
            continue
        lines.append(f"<b>{mname}</b> ({m['inplay_count']} updates)")
        parts = []
        for i, rn in enumerate(rows):
            ov, cv = op.get(f"row{i+1}"), cur.get(f"row{i+1}")
            if ov is not None and cv is not None:
                parts.append(f"{rn}: {fmt_num(ov)}->{fmt_num(cv)}")
        lines.append("  " + " | ".join(parts))
        for r in cur.get("rating", []):
            if r and isinstance(r, dict):
                lines.append(f"  Rating: {r.get('direction', '?')} ({r.get('rating', 0):.2f})")
        lines.append("")
    if preds:
        lines.append("\U0001f4ca <b>PREDICTIONS</b>")
        for p in preds:
            if p["type"] == "MONEYLINE":
                lines.append(f"  {p['pick']} MONEYLINE ({p['confidence']}%)")
            else:
                lines.append(f"  {p['pick']} {p['line']} ({p['confidence']}%)")
    else:
        lines.append("\U0001f4ca No confident prediction (need Q2+)")
    return "\n".join(lines)


# ─── SCAN ENGINE ──────────────────────────────────────────────────────────────
def scan_and_predict():
    global scan_count, pred_count
    games = fetch_live_games()
    if not games:
        return None, None, [], games
    scan_count += 1
    eligible = [g for g in games if get_quarter(g) >= 2]
    if not eligible:
        return None, None, [], games

    results = []
    for g in eligible:
        try:
            odds = fetch_odds(g["id"])
            if not odds:
                continue
            preds, markets = analyze_game(g, odds)
            for p in preds:
                results.append((g, p))
                pred_count += 1
        except Exception:
            pass

    results.sort(key=lambda x: x[1]["confidence"], reverse=True)
    if results:
        return results[0][0], results[0][1], results, games
    return None, None, [], games


# ─── AUTO-SCAN BACKGROUND WORKER ─────────────────────────────────────────
def auto_scan_worker():
    """Fully automatic background scanner. Runs forever, pushes predictions."""
    global auto_tracking, known_games
    auto_tracking = True
    cycle = 0

    while auto_tracking:
        cycle += 1
        try:
            bg, bp, all_results, games = scan_and_predict()

            # --- New game alerts: notify when games enter Q2 ---
            if games and NEW_GAME_ALERT:
                current_ids = {g["id"] for g in games if get_quarter(g) >= 2}
                new_ids = current_ids - known_games
                if new_ids:
                    new_games = [g for g in games if g["id"] in new_ids]
                    for ng in new_games[:5]:
                        bot.send_message(CHAT_ID,
                            f"\U0001f3c0 <b>New game entered Q2+</b>\n"
                            f"{ng['home']['name']} vs {ng['away']['name']}\n"
                            f"{ng['league']['name']} | {ng.get('scores', '0-0')}\n"
                            f"\U0001f4c5 ID: <code>{ng['id']}</code>",
                            parse_mode="HTML"
                        )
                known_games = current_ids

            # --- Send predictions ---
            if bg and bp:
                if ALL_PREDICTIONS and all_results:
                    for g, p in all_results:
                        key = f"{g['id']}_{p['type']}"
                        old = last_predictions.get(key, 0)
                        new_conf = p["confidence"]
                        if abs(new_conf - old) >= 5:
                            rank = 1 if (g, p) == (bg, bp) else 2
                            send_pred_with_button(g, p, rank=rank)
                            last_predictions[key] = new_conf
                else:
                    key = f"{bg['id']}_{bp['type']}"
                    old = last_predictions.get(key, 0)
                    new_conf = bp["confidence"]
                    if abs(new_conf - old) >= 5:
                        send_pred_with_button(bg, bp, rank=1)
                        last_predictions[key] = new_conf

            # Periodic status every 10 cycles (10 min)
            if cycle % 10 == 0 and games:
                eligible = len([g for g in games if get_quarter(g) >= 2])
                bot.send_message(CHAT_ID,
                    f"\U0001f4ca Scan #{scan_count} | {len(games)} live | {eligible} analyzing | {pred_count} predictions sent",
                    parse_mode="HTML"
                )

        except Exception as e:
            print(f'Cycle {cycle} error: {e}')

        # Sleep in 1-second increments for instant stop capability
        for _ in range(SCAN_INTERVAL):
            if not auto_tracking:
                break
            time.sleep(1)


def start_auto_scan():
    """Start the auto-scan background thread (called once at boot)."""
    global scan_thread, auto_tracking
    if auto_tracking:
        return
    auto_tracking = True
    scan_thread = threading.Thread(target=auto_scan_worker, daemon=True)
    scan_thread.start()


# ─── INLINE BUTTON HANDLER ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: call.data.startswith("detail_"))
def on_detail_button(call):
    """Handle 'Match Details' button click - show full odds breakdown."""
    eid = call.data.replace("detail_", "")
    bot.answer_callback_query(call.id, f"Fetching {eid}...")
    odds = fetch_odds(eid)
    if not odds:
        bot.send_message(call.message.chat.id, f"No odds data for <code>{eid}</code>", parse_mode="HTML")
        return
    markets = parse_markets(odds)
    # Build minimal game dict from odds data
    sample = None
    for m in odds:
        inplay = [o for o in m.get("odds", []) if o.get("game_time") != ""]
        if inplay:
            s = inplay[0].get("ss", [0, 0])
            gt = inplay[0].get("game_time", "")
            sample = {
                "home": {"name": "Home"}, "away": {"name": "Away"},
                "league": {"name": "N/A"},
                "scores": f"{s[0] if s[0] else '?'}-{s[1] if s[1] else '?'}",
                "time": {"q": gt.split(" - ")[0].replace("Q","").strip() if " - " in gt else "?",
                         "tm": "0", "ts": "0"},
            }
            break
    if not sample:
        sample = {"home": {"name": "Home"}, "away": {"name": "Away"},
                 "league": {"name": "N/A"}, "scores": "?-?",
                 "time": {"q": "0", "tm": "0", "ts": "0"}}
    preds, _ = analyze_game(sample, odds)
    text = fmt_detail(sample, markets, preds)
    bot.send_message(call.message.chat.id, text, parse_mode="HTML")


# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    bot.reply_to(msg,
        "\U0001f3c0 <b>Basketball Prediction Bot</b>\n"
        "\u26a1 <b>Fully automatic</b> - predictions auto-sent!\n\n"
        "Optional commands:\n"
        "/scan all - All predictions now\n"
        "/stop - Pause\n"
        "/track - Resume\n"
        "/status - Stats\n"
        "/live - Live games list",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["help"])
def cmd_help(msg):
    bot.reply_to(msg,
        "<b>Commands:</b>\n"
        "/scan - Find best prediction\n"
        "/scan all - All predictions\n"
        "/game &lt;event_id&gt; - Detailed odds\n"
        "/track - Auto-scan every 60s\n"
        "/stop - Stop auto-scan\n"
        "/status - Bot status & stats\n\n"
        "<b>Prediction types:</b>\n"
        "- MONEYLINE (Home/Away)\n"
        "- TOTAL Over/Under\n\n"
        "<b>How it works:</b>\n"
        "Compares opening odds vs live in-play odds.\n"
        "Finds the game with the biggest odds movement\n"
        "and highest confidence signal.\n\n"
        "<b>Confidence factors:</b>\n"
        "- Odds drift magnitude (40%)\n"
        "- Scoring pace vs total line (25%)\n"
        "- API rating agreement (20%)\n"
        "- Cross-market confirmation (15%)",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["scan"])
def cmd_scan(msg):
    send_all = "all" in msg.text.lower()
    bot.reply_to(msg, "\u23f3 Scanning live games...", parse_mode="HTML")
    bg, bp, all_r, _ = scan_and_predict()
    if send_all:
        text = fmt_all(all_r)
        bot.send_message(CHAT_ID, text, parse_mode="HTML")
    elif bg and bp:
        send_pred_with_button(bg, bp, rank=1)
    else:
        bot.send_message(CHAT_ID, "No high-confidence predictions found.\nTry /scan all or wait for games to reach Q2+.", parse_mode="HTML")


@bot.message_handler(commands=["game"])
def cmd_game(msg):
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /game &lt;event_id&gt;\nExample: /game 12466117", parse_mode="HTML")
        return
    eid = parts[1]
    bot.reply_to(msg, f"\u23f3 Fetching odds for game {eid}...", parse_mode="HTML")
    odds = fetch_odds(eid)
    if not odds:
        bot.reply_to(msg, f"No odds data for game {eid}. Check the ID and try again.", parse_mode="HTML")
        return
    markets = parse_markets(odds)
    # Build a minimal game dict from first market's latest entry
    sample = None
    for m in odds:
        if m.get("odds"):
            inplay = [o for o in m["odds"] if o.get("game_time") != ""]
            if inplay:
                s = inplay[0].get("ss", [0, 0])
                gt = inplay[0].get("game_time", "")
                sample = {
                    "home": {"name": "Home"}, "away": {"name": "Away"},
                    "league": {"name": "N/A"},
                    "scores": f"{s[0] if s[0] else '?'}-{s[1] if s[1] else '?'}",
                    "time": {"q": gt.split(" - ")[0].replace("Q","").strip() if " - " in gt else "?",
                             "tm": "0", "ts": "0"},
                }
                break
    if not sample:
        sample = {"home": {"name": "Home"}, "away": {"name": "Away"},
                 "league": {"name": "N/A"}, "scores": "?-?",
                 "time": {"q": "0", "tm": "0", "ts": "0"}}
    preds, _ = analyze_game(sample, odds)
    text = fmt_detail(sample, markets, preds)
    bot.send_message(CHAT_ID, text, parse_mode="HTML")


@bot.message_handler(commands=["track"])
def cmd_track(msg):
    global auto_tracking
    if auto_tracking:
        bot.reply_to(msg, "\u26a1 Auto-scan is already running!", parse_mode="HTML")
        return
    start_auto_scan()
    bot.reply_to(msg, "\u2705 Auto-scan RESUMED.", parse_mode="HTML")


@bot.message_handler(commands=["stop"])
def cmd_stop(msg):
    global auto_tracking
    if not auto_tracking:
        bot.reply_to(msg, "\u23f9 Already paused.", parse_mode="HTML")
        return
    auto_tracking = False
    bot.reply_to(msg, "\u23f9 Auto-scan PAUSED.\nUse /track to resume.", parse_mode="HTML")


@bot.message_handler(commands=["status"])
def cmd_status(msg):
    games = fetch_live_games()
    eligible = len([g for g in games if get_quarter(g) >= 2]) if games else 0
    text = (
        f"\U0001f4ca <b>Bot Status</b>\n"
        f"Live games: {len(games) if games else 0}\n"
        f"Eligible (Q2+): {eligible}\n"
        f"Total scans: {scan_count}\n"
        f"Total predictions: {pred_count}\n"
        f"Auto-track: {'ON' if auto_tracking else 'OFF'}\n"
        f"Cache size: {len(opening_cache._cache)}\n"
        f"Interval: {SCAN_INTERVAL}s\n"
        f"Confidence threshold: {CONFIDENCE_THRESHOLD}%"
    )
    bot.send_message(CHAT_ID, text, parse_mode="HTML")


# ─── INLINE GAME LIST (quick access) ─────────────────────────────────────────
@bot.message_handler(commands=["live"])
def cmd_live(msg):
    games = fetch_live_games()
    if not games:
        bot.reply_to(msg, "No live basketball games right now.", parse_mode="HTML")
        return
    lines = [f"\U0001f3c0 <b>Live Games ({len(games)})</b>", ""]
    for g in games[:15]:  # cap at 15 for 3G
        q = get_quarter(g)
        q_badge = f"Q{q}" if q >= 1 else ""
        lines.append(f"\u2022 <code>{g['id']}</code> {g['home']['name']} vs {g['away']['name']}  {g.get('scores', '')} {q_badge}")
    if len(games) > 15:
        lines.append(f"\n... and {len(games) - 15} more")
    lines.append("\nUse /game &lt;id&gt; for details")
    bot.send_message(CHAT_ID, "\n".join(lines), parse_mode="HTML")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\U0001f3c0 Basketball Prediction Bot starting...")
    print(f"   Chat ID: {CHAT_ID}")
    print(f"   Scan interval: {SCAN_INTERVAL}s")
    print(f"   Confidence threshold: {CONFIDENCE_THRESHOLD}%")
    print(f"   Auto-start: {AUTO_START}")
    print(f"   3G optimized: timeout={REQUEST_TIMEOUT}s, workers={MAX_WORKERS}")

    _VER = "v3.0"

    # Launch message
    mode = "\U0001f525 FULLY AUTOMATIC" if AUTO_START else "manual"
    bot.send_message(CHAT_ID,
        f"\U0001f3c0 <b>Bot Online! {_VER} {mode}</b>\n"
        f"Scanning every {SCAN_INTERVAL}s. Predictions auto-sent.\n"
        f"Confidence threshold: {CONFIDENCE_THRESHOLD}%",
        parse_mode="HTML"
    )

    # Auto-start background scanner
    if AUTO_START:
        start_auto_scan()
        print("   Auto-scan started (background)")

    bot.polling(timeout=30, long_polling_timeout=25)
