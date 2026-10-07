import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import main as app

session = __import__("requests").Session()
state = app.load_state()
ok = app.login(session, state)
print("login ok:", ok)
if ok:
    msgs = app.fetch_messages(session)
    print(f"\n--- messages: {len(msgs)} ---")
    for m in msgs[:3]:
        print(m["key"], "|", app.base_title(m))

    for t in app.BULLETIN_TYPE_IDS:
        b = app.fetch_bulletins(session, t)
        print(f"\n--- bulletins[{t}]: {len(b)} ---")
        for x in b[:3]:
            print(x["key"], "|", app.base_title(x))

    for r in app.ROOM_IDS:
        th = app.fetch_threads(session, r)
        print(f"\n--- threads[room {r}]: {len(th)} ---")
        for x in th[:3]:
            print(x["key"], "|", app.base_title(x))

    if app.claude and msgs:
        print("\n--- classify test on first message ---")
        full = app.fetch_message_full_text(session, msgs[0]["msg_id"])
        print("full_text length:", len(full))
        result = app.classify(msgs[0]["sender"], msgs[0]["subject"], full)
        print(result)
    elif not app.claude:
        print("\n(ANTHROPIC_API_KEY not set, skipping classify test)")
