import hashlib
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel

BASE = os.environ["VIGGO_BASE"].rstrip("/")
USER = os.environ["VIGGO_USER"]
PASS = os.environ["VIGGO_PASS"]
FINGERPRINT = os.environ["VIGGO_FINGERPRINT"]

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
claude = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "1800"))
ROOM_IDS = [r.strip() for r in os.environ.get("ROOM_IDS", "").split(",") if r.strip()]
ROOM_NAMES = dict(p.split(":", 1) for p in os.environ.get("ROOM_NAMES", "").split(",") if ":" in p)
BULLETIN_TYPE_IDS = [b.strip() for b in os.environ.get("BULLETIN_TYPE_IDS", "").split(",") if b.strip()]

STATE_PATH = os.environ.get("STATE_PATH", "/data/seen.json")
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").rstrip("/")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (viggo-watch home notifier)",
}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"messages": [], "bulletins": [], "threads": [], "alerts": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def notify(title, message, click_url=None):
    headers = dict(HEADERS)
    headers["Title"] = title.encode("utf-8")
    headers["Priority"] = "default"
    if click_url:
        headers["Click"] = click_url
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[notify] failed to send push: {e}")


def healthcheck_ping(suffix=""):
    """Dead man's switch: ping an external monitor (e.g. healthchecks.io) so
    it can alert us (by email, independent of ntfy) if we stop checking in
    entirely - covers the case where the whole container/app dies silently,
    which an in-app ntfy alert can never cover."""
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(f"{HEALTHCHECK_URL}{suffix}", timeout=10)
    except requests.RequestException as e:
        print(f"[healthcheck] ping failed: {e}")


def alert_once_per_day(state, key, title, message):
    """Send an ntfy alert for a given problem `key`, but at most once per 24h
    while it stays unresolved, so a persistent problem doesn't spam."""
    alerts = state.setdefault("alerts", {})
    last = alerts.get(key)
    now = datetime.now(timezone.utc)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=23):
                return
        except ValueError:
            pass
    notify(title, message)
    alerts[key] = now.isoformat()
    save_state(state)


def clear_alert(state, key):
    alerts = state.setdefault("alerts", {})
    if alerts.pop(key, None) is not None:
        save_state(state)


def login(session, state):
    session.get(f"{BASE}/Basic/Account/Login?returnUrl=/", headers=HEADERS, timeout=20)
    resp = session.post(
        f"{BASE}/Basic/Account/Login",
        data={
            "UserName": USER,
            "Password": PASS,
            "returnUrl": "/",
            "fingerprint": FINGERPRINT,
        },
        headers=HEADERS,
        timeout=20,
        allow_redirects=True,
    )
    if "TwoFactorLogin" in resp.url:
        alert_once_per_day(
            state,
            "2fa_required",
            "Viggo-watch: login kræver 2FA",
            "Den gemte enheds-fingerprint bliver ikke længere godkendt af Viggo. "
            "Log ind manuelt i en browser fra denne server for at forny den, "
            "ellers stopper notifikationerne.",
        )
        return False
    clear_alert(state, "2fa_required")
    return True


def clean_text(s):
    return re.sub(r"\s+", " ", s or "").strip()


class Classification(BaseModel):
    actionable: bool
    action_summary: str
    deadline: str


def classify(sender, title, full_text):
    """Ask Claude whether this Viggo post contains something the parent needs to
    actually do, buried in the rest of the text (a common complaint with ViGGO
    ugebreve/opslag: action items get lost in long informational text)."""
    if not claude:
        return None
    try:
        response = claude.messages.parse(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=(
                "Du hjælper en forælder med at opdage skjulte opgaver i beskeder fra "
                "skolens kommunikationssystem ViGGO. Beskederne er ofte lange "
                "ugebreve eller opslag, hvor en konkret opgave til forælderen "
                "(fx 'husk kortspil med i uge 41', en frist, noget barnet skal "
                "have med, et tidspunkt man skal møde op) drukner i generel info. "
                "Læs teksten igennem og afgør om der er noget konkret forælderen "
                "skal GØRE (medbringe noget, tilmelde sig, møde op et sted/tidspunkt, "
                "underskrive, svare på noget, osv.) - ren orientering/nyheder tæller "
                "ikke som en handling. Hvis der er en handling, opsummer den kort og "
                "konkret på dansk (én sætning, imperativ form). Hvis der nævnes en "
                "deadline/dato/uge for handlingen, angiv den kort, ellers tom streng."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"Afsender: {sender}\nOverskrift: {title}\n\nTekst:\n{full_text}",
                }
            ],
            output_format=Classification,
        )
        return response.parsed_output
    except Exception as e:
        print(f"[classify] failed: {e}")
        return None


def fetch_messages(session):
    r = session.get(f"{BASE}/Basic/Message/Folder/10?ajax=1", headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select('li[id^="tree-item-message-id-"]'):
        msg_id = li["id"].replace("tree-item-message-id-", "")
        a = li.find("a")
        if not a:
            continue
        divs = a.find_all("div", recursive=False)
        sender = clean_text(divs[1].find("div").get_text()) if len(divs) > 1 else ""
        subject = clean_text(divs[2].get_text()) if len(divs) > 2 else ""
        preview = clean_text(divs[3].get_text()) if len(divs) > 3 else ""
        items.append(
            {
                "key": f"msg-{msg_id}",
                "kind": "message",
                "sender": sender,
                "subject": subject,
                "preview": preview,
                "msg_id": msg_id,
                "url": f"{BASE}/Basic/Message/Details/10/{msg_id}",
            }
        )
    return items


def fetch_message_full_text(session, msg_id):
    r = session.get(f"{BASE}/Basic/Message/Details/10/{msg_id}/?ajax=1", headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    parts = []
    for li in soup.select('li[id^="mid_"]'):
        p = li.select_one("div.p")
        if p:
            parts.append(clean_text(p.get_text()))
    return "\n\n".join(parts)


def fetch_bulletins(session, type_id):
    r = session.get(
        f"{BASE}/Basic/BulletinBoard/LoadBulletins/?skip=0&useTypsId={type_id}",
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select("ul.list-board > li"):
        a = li.find("a", class_="hinted")
        sender = clean_text(a.contents[0]) if a and a.contents else ""
        title_el = li.select_one("p.h")
        title = clean_text(title_el.get_text()) if title_el else ""
        body_el = li.select_one("div.content")
        body = clean_text(body_el.get_text()) if body_el else ""
        if not title and not body:
            continue
        dedup_key = hashlib.sha1(f"{type_id}|{sender}|{title}".encode("utf-8")).hexdigest()
        items.append(
            {
                "key": f"bulletin-{dedup_key}",
                "kind": "bulletin",
                "sender": sender,
                "subject": title,
                "preview": body,
                "full_text": body,
                "url": f"{BASE}/Basic/Home",
            }
        )
    return items


def fetch_threads(session, room_id):
    r = session.get(f"{BASE}/Basic/Rooms/Index/{room_id}?tab=Forum", headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    room_name = ROOM_NAMES.get(str(room_id), f"rum {room_id}")
    items = []
    thread_list = soup.select_one("ul#forum-thread-list")
    if not thread_list:
        return items
    for li in thread_list.select("li"):
        a = li.find("a")
        if not a or not a.get("href"):
            continue
        m = re.search(r"rowid=(\d+)", a["href"])
        if not m:
            continue
        thread_id = m.group(1)
        ps = a.find_all("p")
        sender_date = clean_text(ps[0].get_text()) if len(ps) > 0 else ""
        title = clean_text(ps[1].get_text()) if len(ps) > 1 else ""
        preview_el = a.find("small")
        preview = clean_text(preview_el.get_text()) if preview_el else ""
        items.append(
            {
                "key": f"thread-{room_id}-{thread_id}",
                "kind": "thread",
                "sender": sender_date,
                "subject": title,
                "preview": preview,
                "room_id": room_id,
                "thread_id": thread_id,
                "room_name": room_name,
                "url": f"{BASE}/Basic/Rooms/Index/{room_id}?tab=Forum&rowid={thread_id}",
            }
        )
    return items


def fetch_thread_full_text(session, room_id, thread_id):
    r = session.get(
        f"{BASE}/Basic/Rooms/Index/{room_id}?tab=Forum&ajax=1&rowid={thread_id}",
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    parts = [clean_text(el.get_text()) for el in soup.select("ol.list-thread div.content")]
    return "\n\n".join(parts)


def base_title(it):
    if it["kind"] == "message":
        return f"Besked fra {it['sender']}"
    if it["kind"] == "bulletin":
        return f"Opslag/ugebrev fra {it['sender']}" if it["sender"] else "Nyt opslag på opslagstavlen"
    return f"Ny tråd i {it['room_name']}: {it['subject']}" if it["subject"] else f"Ny tråd i {it['room_name']}"


def notify_item(session, it):
    title = base_title(it)
    fallback_body = f"{it['subject']}\n{it['preview']}"[:400]

    full_text = it.get("full_text")
    if full_text is None:
        try:
            if it["kind"] == "message":
                full_text = fetch_message_full_text(session, it["msg_id"])
            elif it["kind"] == "thread":
                full_text = fetch_thread_full_text(session, it["room_id"], it["thread_id"])
            else:
                full_text = it["preview"]
        except Exception as e:
            print(f"[notify_item] failed to fetch full text: {e}")
            full_text = it["preview"]

    result = classify(it["sender"], it["subject"], full_text)
    if result is None:
        notify(title, fallback_body, it["url"])
        return

    if result.actionable:
        headline = f"🔴 HUSK: {result.action_summary}"
        if result.deadline:
            headline += f" ({result.deadline})"
        body = f"{headline}\n\n— {title} —\n{clean_text(full_text)[:500]}"
        notify(f"Handling påkrævet: {title}", body, it["url"])
    else:
        body = f"Info, ingen handling krævet.\n\n{clean_text(full_text)[:400]}"
        notify(title, body, it["url"])


def poll_once(session, state, first_run):
    all_items = []
    all_items += fetch_messages(session)
    for type_id in BULLETIN_TYPE_IDS:
        all_items += fetch_bulletins(session, type_id)
    for room_id in ROOM_IDS:
        all_items += fetch_threads(session, room_id)

    seen = set()
    for source in ("messages", "bulletins", "threads"):
        seen |= set(state.get(source, []))

    new_items = [it for it in all_items if it["key"] not in seen]

    # Viggo almost never has literally nothing in all three sources at once -
    # if it does, that's more likely our HTML scraping broke (selectors no
    # longer match a changed page) than a genuinely empty account.
    if not first_run and len(all_items) == 0:
        alert_once_per_day(
            state,
            "empty_poll",
            "Viggo-watch: intet fundet - mistænkeligt",
            "Scraperen fandt 0 beskeder/opslag/tråde i alt. Det sker normalt "
            "aldrig - Viggo har nok ændret deres side, så vores parsing er "
            "knækket. Tjek 'docker compose logs' på serveren.",
        )
    else:
        clear_alert(state, "empty_poll")

    if first_run:
        # Don't spam on first ever run: just record the current baseline.
        print(f"[poll] first run, baselining {len(all_items)} existing items without notifying")
    else:
        for it in new_items:
            print(f"[poll] NEW: {base_title(it)}")
            notify_item(session, it)

    all_keys = [it["key"] for it in all_items]
    state["messages"] = [k for k in all_keys if k.startswith("msg-")]
    state["bulletins"] = [k for k in all_keys if k.startswith("bulletin-")]
    state["threads"] = [k for k in all_keys if k.startswith("thread-")]
    save_state(state)


def main():
    state = load_state()
    first_run = not os.path.exists(STATE_PATH)
    session = requests.Session()

    while True:
        try:
            if login(session, state):
                poll_once(session, state, first_run)
                first_run = False
            else:
                print("[main] login failed (2FA required), will retry next cycle")
            clear_alert(state, "poll_error")
            healthcheck_ping()
        except Exception as e:
            print("[main] error during poll cycle:")
            traceback.print_exc()
            alert_once_per_day(
                state,
                "poll_error",
                "Viggo-watch: fejl under tjek",
                f"Noget gik galt under login/scraping: {e}\n\n"
                "Tjek 'docker compose logs' på serveren. Denne besked gentages "
                "højst én gang i døgnet så længe problemet varer ved.",
            )
            healthcheck_ping("/fail")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
