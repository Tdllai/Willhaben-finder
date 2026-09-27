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


def has_word(text, w):
    w = w.lower()
    if len(w) <= 3:   # short words (tv, lg, ps5...) must stand alone
        return re.search(r"(?<![a-zäöüß0-9])" + re.escape(w) + r"(?![a-zäöüß0-9])", text) is not None
    return w in text  # longer words may sit inside compounds


def is_match(ad, cfg):
    text = f"{ad['title']} {ad['body']}".lower()
    title = ad["title"].lower()
    full = f"{text} {ad['all_text']} {ad['price_text']}".lower()

    if cfg.get("title_must_contain_one_of"):
        if not any(has_word(title, w) for w in cfg["title_must_contain_one_of"]):
            return False, "title"
    for w in cfg.get("exclude_title_words", []):      # furniture/accessories: title only
        if w.lower() in title:
            return False, f"title-excluded:{w}"
    for w in cfg.get("exclude_words", []):            # defects: title + description
        if w.lower() in text:
            return False, f"excluded:{w}"

    if cfg.get("value_words") or cfg.get("new_words"):
        valuable = any(has_word(title, w) for w in cfg.get("value_words", []))
        brand_new = any(has_word(text, w) for w in cfg.get("new_words", []))
        if not (valuable or brand_new):
            return False, "no-value-sign"
    min_price = cfg.get("min_price")
    if min_price and ad["price"] not in (None, 0) and ad["price"] < min_price:
        return False, "too-cheap"

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


def local_time(s):
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("Europe/Vienna")).strftime("%d.%m. %H:%M")
    except Exception:
        return str(s)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_ad(chat_id, ad):
    if ad["price"] in (None, 0) :
        price = "🎁 مجانی / zu verschenken"
    else:
        price = f"💶 {ad['price']:.0f} €"
    place = " ".join(x for x in [ad["postcode"], ad["location"]] if x)
    label = ad.get("label", "🆕")
    caption = (f"{label} <b>{esc(ad['title'])}</b>\n{price}\n📍 {esc(place)}\n"
               f"🕒 {esc(local_time(ad['published']))}\n\n<a href=\"{ad['url']}\">آگهی رو باز کن</a>")
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


def categories(cfg):
    """Old single-item configs become one category; new configs have a 'categories' list.
    Settings outside 'categories' are shared defaults for every category."""
    if "categories" not in cfg:
        return [dict(cfg, name=cfg.get("name", "item"), label=cfg.get("label", "🆕"))]
    shared = {k: v for k, v in cfg.items() if k != "categories"}
    out = []
    for c in cfg["categories"]:
        merged = dict(shared)
        for k, v in c.items():
            if k in ("exclude_words",) and k in shared:   # shared defect words + extra ones
                merged[k] = list(shared[k]) + list(v)
            else:
                merged[k] = v
        out.append(merged)
    return out


def main():
    if not PASSWORD:
        sys.exit("BOT_PASSWORD is missing. Add it as a GitHub secret.")
    cfg = load_config()
    if cfg.get("telegram", True) and not TOKEN:
        sys.exit("TELEGRAM_TOKEN is missing. Add it as a GitHub secret.")
    cats = categories(cfg)
    state = load_secret_json(STATE_FILE, {})
    old_state = json.dumps(state, sort_keys=True)
    seen_list = list(state.get("seen", []))
    seen = set(seen_list)
    started = set(state.get("started", []))
    if state.get("initialised") and not started and "categories" not in cfg:
        started = {cats[0]["name"]}
    if state.get("initialised") and not state.get("started"):
        # upgrading from the single-item bot: its category was already running
        started |= {c["name"] for c in cats if c["name"] in ("تلویزیون", "tv", "item")}

    global telegram
    if cfg.get("telegram", True):
        chat_id = get_chat_id(state)
        if not chat_id:
            print("No chat yet: open your bot in Telegram and press START, then run again.")
            return
    else:                       # Telegram switched off: Claude does all the messaging
        chat_id = None
        telegram = lambda method, params: {}

    total_checked, total_new, total_sent, any_ok, errors = 0, [], 0, False, []
    stats = {}
    for ci, cat in enumerate(cats, 1):
        ads_by_id = {}
        urls = list(cat.get("search_urls", []))
        paged = cat.get("paged_url")            # newest-first search, walked page by page
        max_pages = cat.get("max_pages", 1)
        if paged:
            urls = [paged + (f"&page={p}" if p > 1 else "") for p in range(1, max_pages + 1)]
        for i, url in enumerate(urls, 1):
            try:
                ads = parse_ads(http_get(url))
                known = sum(1 for a in ads if f"{cat['name']}:{a['id']}" in seen)
                print(f"cat {ci} page {i}: {len(ads)} ads, {known} already known")   # no URLs: logs are public
                for ad in ads:
                    ads_by_id[ad["id"]] = ad
                any_ok = True
                time.sleep(2)
                if paged and (not ads or known >= len(ads) * 0.5):
                    break                      # reached listings we saw last time
            except Exception as e:
                errors.append(f"cat {ci} page {i}: {type(e).__name__}: {e}")
                print("ERROR", errors[-1][:200])
                break
        total_checked += len(ads_by_id)

        new_matches = []
        for ad in ads_by_id.values():
            key = f"{cat['name']}:{ad['id']}"
            if key in seen or (ad["id"] in seen and cat["name"] in started):
                continue      # plain ids = seen by the older single-item bot
            seen.add(key)
            seen_list.append(key)
            ok, reason = is_match(ad, cat)
            if DEBUG:
                print(f"  [{reason:>14}] {ad['price']} | {ad['postcode']} | {ad['title'][:60]}")
            if ok:
                ad["label"] = cat.get("label", "🆕")
                ad["category"] = cat["name"]
                new_matches.append(ad)

        collect_all = cat.get("collect_all", False)   # deals mode: store for Claude, no per-ad message
        to_send = [] if collect_all else new_matches
        if cat["name"] not in started and ads_by_id:
            if collect_all:
                telegram("sendMessage", {"chat_id": chat_id,
                         "text": f"🔎 جمع‌کردن آگهی‌های «{cat['name']}» شروع شد. کلود هر دو ساعت "
                                 f"بهترین معامله‌ها رو بررسی می‌کنه و خبرت می‌کنه."})
            else:
                to_send = new_matches[: cat.get("first_run_samples", 3)]
                telegram("sendMessage", {"chat_id": chat_id,
                         "text": f"🔎 جست‌وجوی «{cat['name']}» شروع شد. الان {len(new_matches)} آگهی مناسب "
                                 f"پیدا کردم؛ {len(to_send)} تاش رو برای نمونه می‌فرستم. از این به بعد فقط آگهی‌های تازه میاد."})
            started.add(cat["name"])
        # overflow = almost everything on the pages was new -> we may be missing ads between runs
        stats[cat["name"]] = {"checked": len(ads_by_id), "new": len(new_matches),
                              "overflow": bool(ads_by_id) and len(new_matches) >= 0.9 * len(ads_by_id)}
        for ad in to_send:
            try:
                send_ad(chat_id, ad)
                time.sleep(1)
            except Exception as e:
                print("send failed:", type(e).__name__)
        total_new += new_matches
        total_sent += len(to_send)

    if errors and not any_ok:
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] in (3, 20):
            telegram("sendMessage", {"chat_id": chat_id,
                     "text": "⚠️ ربات چند بار نتونست ویلهابن رو بخونه. به کلود خبر بده.\n" + errors[0][:300]})
        save_secret_json(STATE_FILE, state)
        return
    state["fail_count"] = 0

    if total_new:
        history = load_secret_json(MATCHES_FILE, [])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for ad in total_new:
            item = {k: ad.get(k) for k in ("id", "category", "title", "price", "postcode",
                                            "location", "url", "image", "published")}
            item["body"] = (ad.get("body") or "")[:400]
            item["found"] = now
            history.append(item)
        keep_h = cfg.get("keep_hours", 18)
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - keep_h * 3600))
        history = [h for h in history if h.get("found", "") >= cutoff]
        save_secret_json(MATCHES_FILE, history[-cfg.get("keep_max", 4000):])

    state["seen"] = seen_list[-cfg.get("seen_max", 5000):]
    state["last_stats"] = stats
    state["started"] = sorted(started)
    state["initialised"] = True
    if json.dumps(state, sort_keys=True) != old_state:   # only write (and commit) on change
        save_secret_json(STATE_FILE, state)
    print(f"done: {total_checked} ads checked, {len(total_new)} new, {total_sent} sent")


if __name__ == "__main__":
    main()
