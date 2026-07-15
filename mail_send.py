"""
Versendet die Kunden-Benachrichtigung fuer ein zugeordnetes Musterfoto via
Microsoft Graph "sendMail" - Absender ist eine eigene Mailbox
(config["sender_from_address"], z.B. custom@dirtysox.ch), NICHT die
sales@dirtysox.ch-Mailbox, die fuer den Mail-Abruf verwendet wird. Braucht
zusaetzlich zu Mail.ReadWrite die Graph-Anwendungsberechtigung "Mail.Send"
mit Admin-Zustimmung (gleiche Azure-App wie graph_auth.py).
"""
import base64
import json
import os

import requests

import graph_auth

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def send_sample_notification(to_address, subject, body_text, attachment_path):
    """Sendet eine E-Mail mit dem Musterfoto als Anhang. Wirft eine Exception
    bei einem Fehler - der Aufrufer (Review-UI) faengt das ab und zeigt eine
    Warnung, statt den ganzen Zuordnungs-Schritt scheitern zu lassen."""
    cfg = _load_config()
    token = graph_auth.get_access_token()
    sender = cfg["sender_from_address"]

    with open(attachment_path, "rb") as f:
        attachment_b64 = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": os.path.basename(attachment_path),
                    "contentBytes": attachment_b64,
                }
            ],
        },
        "saveToSentItems": "true",
    }

    url = f"{GRAPH_BASE}/users/{sender}/sendMail"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()


def default_subject(task_name):
    return f"Neues Musterfoto - {task_name}"


def default_body(task_name):
    return (
        f"Guten Tag\n\n"
        f"Anbei ein neues Musterfoto zu Ihrer Bestellung \"{task_name}\".\n\n"
        f"Freundliche Grüsse\nDirtySox GmbH"
    )
