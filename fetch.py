"""
Holt neue Mustermails von maskova@rotexponozky.cz (bzw. dem in config.json
konfigurierten Absender) aus dem Postfach sales@dirtysox.ch via Microsoft
Graph, laedt die JPG-Anhaenge herunter und legt pro Mail einen Batch in
state.json an.

Aufruf: python3 fetch.py
"""

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

import graph_auth
import state

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}

# Ohne Datumsgrenze liefert Graph (mangels $orderby, das sich mit diesem
# $filter nicht kombinieren laesst) die AELTESTEN Treffer zuerst - bei einem
# seit Jahren aktiven Absender waeren das Mails von 2021. Deshalb nur Mails
# der letzten N Tage abfragen, das reicht fuer den laufenden Betrieb bei
# weitem (die Mailbox wird alle paar Minuten gepollt).
LOOKBACK_DAYS = 45

# Erkennt die Rotex-Losnummer im Betreff, z.B. "DIRTY 793 FAIRTRAIL" oder
# "dirty 795" -> "793"/"795". Nimmt die erste 2-4-stellige Zahl nach "dirty"
# (Gross-/Kleinschreibung egal); ohne dieses Wort im Betreff wird nichts
# erkannt, um Fehltreffer auf beliebige Zahlen zu vermeiden.
_ROTEX_NUMMER_RE = re.compile(r"dirty[\s\-_]*#?(\d{2,4})", re.IGNORECASE)


def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_rotex_nummer(subject):
    match = _ROTEX_NUMMER_RE.search(subject or "")
    return match.group(1) if match else None


def graph_get(token, url, params=None, extra_headers=None):
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_candidate_messages(token, mailbox, sender_filter):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{GRAPH_BASE}/users/{mailbox}/messages"
    params = {
        "$filter": (
            f"from/emailAddress/address eq '{sender_filter}' and hasAttachments eq true"
            f" and receivedDateTime ge {cutoff}"
        ),
        "$top": "50",
        "$select": "id,subject,receivedDateTime,from,hasAttachments",
    }
    data = graph_get(token, url, params=params, extra_headers={"ConsistencyLevel": "eventual"})
    messages = data.get("value", [])
    messages.sort(key=lambda m: m.get("receivedDateTime", ""))
    return messages


def fetch_attachments(token, mailbox, message_id):
    url = f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}/attachments"
    data = graph_get(token, url)
    return data.get("value", [])


def run():
    cfg = _load_config()
    token = graph_auth.get_access_token()

    messages = fetch_candidate_messages(token, cfg["mailbox"], cfg["sender_filter"])
    new_batches = []

    for msg in messages:
        msg_id = msg["id"]
        subject = msg.get("subject", "")
        if state.is_message_processed(msg_id):
            continue

        attachments = fetch_attachments(token, cfg["mailbox"], msg_id)
        image_attachments = [
            a for a in attachments
            if os.path.splitext(a.get("name", ""))[1].lower() in IMAGE_EXTENSIONS
            and a.get("contentBytes")
        ]
        if not image_attachments:
            state.mark_message_processed(msg_id)
            print(f"[INFO] Keine Bildanhaenge in '{subject}' ({msg_id}) - uebersprungen.")
            continue

        received = msg.get("receivedDateTime", "")[:10]
        batch_id = f"{received}_{msg_id[-10:]}"
        batch_dir = os.path.join(cfg["staging_dir"], batch_id)
        os.makedirs(batch_dir, exist_ok=True)

        rotex_nummer = extract_rotex_nummer(subject)
        photos = {}
        for att in image_attachments:
            name = att.get("name")
            dest_path = os.path.join(batch_dir, name)
            with open(dest_path, "wb") as f:
                f.write(base64.b64decode(att["contentBytes"]))
            photos[name] = {"path": dest_path, "status": "pending", "suggestion": None}

        state.add_batch(batch_id, {
            "subject": subject,
            "received": msg.get("receivedDateTime", ""),
            "message_id": msg_id,
            "rotex_nummer": rotex_nummer,
            "status": "open",
            "photos": photos,
        })
        state.mark_message_processed(msg_id)
        new_batches.append(batch_id)
        print(f"[OK] Neuer Batch '{batch_id}' mit {len(photos)} Foto(s) aus '{subject}' "
              f"(Rotex-Nr.: {rotex_nummer or 'unbekannt'}).")

    if not new_batches:
        print("Keine neuen Mustermails gefunden.")
    return new_batches


if __name__ == "__main__":
    run()
