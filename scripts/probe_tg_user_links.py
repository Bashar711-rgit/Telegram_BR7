#!/usr/bin/env python3
"""Probe tg://user?id= behavior in REAL Telegram Bot API (entities vs buttons).
Deletes each probe message after measuring. No secrets printed."""
import asyncio, json, os, sys, urllib.request, urllib.parse

TOKEN = open('/home/z/.secrets/bot_token').read().strip()
CHAT = -1002707889921
BOT_ID = 7886937106

def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

async def main():
    probes = []

    # P1: entity text_link with REAL known id (the bot itself)
    s, b = api("sendMessage", chat_id=CHAT,
               text="PROBE1",
               parse_mode="HTML",
               entities=json.dumps([{"type": "text_link", "offset": 0, "length": 6,
                                     "url": f"tg://user?id={BOT_ID}"}]))
    print(f"P1 entity tg://user?id={BOT_ID} (real id)   -> HTTP {s} ok={b.get('ok')} {('desc='+str(b.get('description'))) if not b.get('ok') else ''}")
    if b.get("ok"):
        r = b["result"]; probes.append(r["message_id"])
        kept = [e for e in (r.get("entities") or []) if e.get("type") == "text_link"]
        print(f"   entities surviving: {len(kept)} text_link kept={bool(kept)}")

    # P2: URL BUTTON with real id
    s, b = api("sendMessage", chat_id=CHAT, text="PROBE2",
               reply_markup=json.dumps({"inline_keyboard": [[
                   {"text": "مراسلة", "url": f"tg://user?id={BOT_ID}"}]]}))
    print(f"P2 button tg://user?id={BOT_ID} (real id)   -> HTTP {s} ok={b.get('ok')} {('desc='+str(b.get('description'))) if not b.get('ok') else ''}")
    if b.get("ok"): probes.append(b["result"]["message_id"])

    # P3: URL BUTTON with https t.me (control)
    s, b = api("sendMessage", chat_id=CHAT, text="PROBE3",
               reply_markup=json.dumps({"inline_keyboard": [[
                   {"text": "مراسلة", "url": "https://t.me/BZ711BR_bot"}]]}))
    print(f"P3 button https://t.me/BZ711BR_bot (control) -> HTTP {s} ok={b.get('ok')} {('desc='+str(b.get('description'))) if not b.get('ok') else ''}")
    if b.get("ok"): probes.append(b["result"]["message_id"])

    # P4: entity text_link with FAKE id=1 (server-drop check)
    s, b = api("sendMessage", chat_id=CHAT,
               text="PROBE4",
               parse_mode="HTML",
               entities=json.dumps([{"type": "text_link", "offset": 0, "length": 6,
                                     "url": "tg://user?id=1"}]))
    print(f"P4 entity tg://user?id=1 (fake id)          -> HTTP {s} ok={b.get('ok')} {('desc='+str(b.get('description'))) if not b.get('ok') else ''}")
    if b.get("ok"):
        r = b["result"]; probes.append(r["message_id"])
        kept = [e for e in (r.get("entities") or []) if e.get("type") == "text_link"]
        print(f"   entities surviving: {len(kept)} text_link kept={bool(kept)}")

    # cleanup
    for mid in probes:
        s, b = api("deleteMessage", chat_id=CHAT, message_id=mid)
        print(f"cleanup delete {mid} -> ok={b.get('ok')}")

asyncio.run(main())
