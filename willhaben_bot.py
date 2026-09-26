#!/usr/bin/env python3
"""
Willhaben -> Telegram Bot
Checks willhaben.at searches and sends new matching ads to Telegram.
Settings live in config.json. No extra packages needed.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")          # only used if CONFIG_JSON secret is missing
DATA_DIR = os.path.join(HERE, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.enc")         # encrypted
MATCHES_FILE = os.path.join(DATA_DIR, "matches.enc")     # encrypted

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
PASSWORD = os.environ.get("BOT_PASSWORD", "").strip()
CONFIG_JSON = os.environ.get("CONFIG_JSON", "").strip()
DEBUG = os.environ.get("DEBUG", "") == "1"   # never enable on a public repo: logs are public

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


OPENSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-a", "-pass", "env:BOT_PASSWORD"]


def load_secret_json(path, default):
    """Read an encrypted JSON file. Returns default if missing."""
    if not os.path.exists(path):
        return default
    r = subprocess.run(OPENSSL + ["-d", "-in", path], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("Could not decrypt data file (wrong BOT_PASSWORD?)")
    return json.loads(r.stdout.decode("utf-8"))


def save_secret_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    r = subprocess.run(OPENSSL + ["-salt", "-out", path], input=raw, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("Encryption failed: " + r.stderr.decode(errors="replace"))


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def telegram(method, params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def find_key(obj, key):
    """Search a nested JSON structure for the first value stored under `key`."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_key(v, key)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------- willhaben
def parse_ads(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("Willhaben page format not recognised (no __NEXT_DATA__).")
    data = json.loads(m.group(1))
    summaries = find_key(data, "advertSummary")
    if summaries is None:
        return []
    if isinstance(summaries, dict):
        summaries = [summaries]
    ads = []
    for a in summaries:
        attrs = {}
        for at in (a.get("attributes") or {}).get("attribute", []) or []:
            vals = at.get("values") or []
            attrs[str(at.get("name", "")).upper()] = vals[0] if len(vals) == 1 else " ".join(map(str, vals))
        ad_id = str(a.get("id") or attrs.get("ADID") or "")
        if not ad_id:
            continue
        title = attrs.get("HEADING") or a.get("description") or ""
        body = attrs.get("BODY_DYN") or attrs.get("DESCRIPTION") or ""
        price_raw = attrs.get("PRICE")
        try:
            price = float(str(price_raw).replace(",", ".")) if price_raw not in (None, "") else None
        except ValueError:
            price = None
        seo = attrs.get("SEO_URL") or ""
        url = ("https://www.willhaben.at/iad/" + seo.lstrip("/")) if seo else \
              f"https://www.willhaben.at/iad/object?adId={ad_id}"
        img = attrs.get("MMO") or ""
        if img and not img.startswith("http"):
            img = "https://cache.willhaben.at/mmo/" + img.lstrip("/")
        published = attrs.get("PUBLISHED_STRING") or attrs.get("PUBLISHED") or ""
        ads.append({
            "id": ad_id,
            "title": str(title).strip(),
            "body": str(body).strip(),
            "price": price,
            "price_text": str(attrs.get("PRICE_FOR_DISPLAY") or ""),
            "postcode": str(attrs.get("POSTCODE") or ""),
            "location": str(attrs.get("LOCATION") or attrs.get("DISTRICT") or ""),
            "url": url,
            "image": img,
            "published": str(published),
            "all_text": " ".join(str(v) for v in attrs.values()),
        })
    return ads


def is_match(ad, cfg):
    text = f"{ad['title']} {ad['body']}".lower()
    title = ad["title"].lower()
    full = f"{text} {ad['all_text']} {ad['price_text']}".lower()

    if cfg.get("title_must_contain_one_of"):
        if not any(re.search(r"(?<![a-zäöüß])" + re.escape(w.lower()) + r"(?![a-zäöüß])", title)
                   for w in cfg["title_must_contain_one_of"]):
            return False, "title"
    for w in cfg.get("exclude_words", []):
        if w.lower() in text:
            return False, f"excluded:{w}"

    max_price = cfg.get("max_price")
    if max_price is not None:
        free_words = [w.lower() for w in cfg.get("free_words", [])]
        looks_free = any(w in full for w in free_words)
        if ad["price"] is None:
            if not (max_price == 0 and looks_free):
                return False, "no-price"
        elif ad["price"] > max_price and not (max_price == 0 and looks_free and ad["price"] <= 1):
            return False, "price"

    prefixes = cfg.get("postcode_prefixes") or []
    if prefixes:
        if not ad["postcode"] or not any(ad["postcode"].startswith(p) for p in prefixes):
            return False, "area"
    return True, "ok"


# ---------------------------------------------------------------- telegram
def get_chat_id(state):
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    if state.get("chat_id"):
        return state["chat_id"]
    try:  # an old webhook from a previous program would block getUpdates
        telegram("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as e:
        print("deleteWebhook failed:", e)
    res = telegram("getUpdates", {})
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id"):
            state["chat_id"] = str(chat["id"])
            telegram("sendMessage", {
                "chat_id": state["chat_id"],
                "text": "✅ ربات وصل شد! از این به بعد آگهی‌های جدید رو اینجا برات می‌فرستم.",
            })
            return state["chat_id"]
    return None


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_ad(chat_id, ad):
    if ad["price"] in (None, 0) :
        price = "🎁 مجانی / zu verschenken"
    else:
        price = f"💶 {ad['price']:.0f} €"
    place = " ".join(x for x in [ad["postcode"], ad["location"]] if x)
    caption = (f"🆕 <b>{esc(ad['title'])}</b>\n{price}\n📍 {esc(place)}\n"
               f"🕒 {esc(ad['published'])}\n\n<a href=\"{ad['url']}\">آگهی رو باز کن</a>")
    if ad["image"]:
        try:
            telegram("sendPhoto", {"chat_id": chat_id, "photo": ad["image"],
                                   "caption": caption[:1000], "parse_mode": "HTML"})
            return
        except Exception as e:
            print("photo failed, sending text:", e)
    telegram("sendMessage", {"chat_id": chat_id, "text": caption, "parse_mode": "HTML",
                             "disable_web_page_preview": "false"})


# ---------------------------------------------------------------- main
def load_config():
    if CONFIG_JSON:
        return json.loads(CONFIG_JSON)
    return load_json(CONFIG_FILE, {})


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_TOKEN is missing. Add it as a GitHub secret.")
    if not PASSWORD:
        sys.exit("BOT_PASSWORD is missing. Add it as a GitHub secret.")
    cfg = load_config()
    state = load_secret_json(STATE_FILE, {})
    old_state = json.dumps(state, sort_keys=True)
    seen_list = list(state.get("seen", []))
    seen = set(seen_list)
    first_run = not state.get("initialised")

    chat_id = get_chat_id(state)
    if not chat_id:
        print("No chat yet: open your bot in Telegram and press START, then run again.")
        return

    all_ads, errors = {}, []
    for i, url in enumerate(cfg.get("search_urls", []), 1):
        try:
            ads = parse_ads(http_get(url))
            print(f"search {i}: {len(ads)} ads")          # no URLs/keywords: logs are public
            for ad in ads:
                all_ads[ad["id"]] = ad
            time.sleep(2)
        except Exception as e:
            errors.append(f"search {i}: {type(e).__name__}: {e}")
            print("ERROR", errors[-1][:200])

    if errors and not all_ads:
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] in (3, 20):
            telegram("sendMessage", {"chat_id": chat_id,
                     "text": "⚠️ ربات چند بار نتونست ویلهابن رو بخونه. به کلود خبر بده.\n" + errors[0][:300]})
        save_secret_json(STATE_FILE, state)
        return
    state["fail_count"] = 0

    new_matches = []
    for ad in all_ads.values():
        if ad["id"] in seen:
            continue
        seen.add(ad["id"])
        seen_list.append(ad["id"])
        ok, reason = is_match(ad, cfg)
        if DEBUG:
            print(f"  [{reason:>14}] {ad['price']} | {ad['postcode']} | {ad['title'][:60]}")
        if ok:
            new_matches.append(ad)

    to_send = new_matches
    if first_run:
        to_send = new_matches[: cfg.get("first_run_samples", 3)]
        telegram("sendMessage", {"chat_id": chat_id,
                 "text": f"🔎 شروع شد. الان {len(new_matches)} آگهی مناسب پیدا کردم؛ "
                         f"{len(to_send)} تاش رو برای نمونه می‌فرستم. از این به بعد فقط آگهی‌های تازه میاد."})
    for ad in to_send:
        try:
            send_ad(chat_id, ad)
            time.sleep(1)
        except Exception as e:
            print("send failed:", type(e).__name__)

    if new_matches:
        history = load_secret_json(MATCHES_FILE, [])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for ad in new_matches:
            item = {k: ad[k] for k in ("id", "title", "body", "price", "postcode",
                                        "location", "url", "image", "published")}
            item["found"] = now
            history.append(item)
        save_secret_json(MATCHES_FILE, history[-200:])

    state["seen"] = seen_list[-3000:]
    state["initialised"] = True
    if json.dumps(state, sort_keys=True) != old_state:   # only write (and commit) on change
        save_secret_json(STATE_FILE, state)
    print(f"done: {len(all_ads)} ads checked, {len(new_matches)} new, {len(to_send)} sent")


if __name__ == "__main__":
    main()
