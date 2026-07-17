"""
Verifiziert die signierten "Magic Links", die Trigger.dev (das TypeScript-
Repo "ReplicateCustomerFromClickupToErp") pro Kunden-Freigabe-Mail erzeugt.
Dieses Modul ist reiner VERIFIZIERER - die Tokens werden ausschliesslich auf
der TS-Seite ausgestellt (approval-token.ts dort), damit es nur EIN Schema
gibt, das beide Seiten kennen muessen.

Format (muss mit approval-token.ts exakt uebereinstimmen):
  payloadJson = UTF-8 JSON von { taskId, kind, iat, jti }
  payloadB64  = base64url(payloadJson), ohne Padding
  signature   = hex( HMAC-SHA256(secret, payloadB64-STRING selbst) )
  token       = payloadB64 + "." + signature

Die Signatur wird NIE ueber neu serialisiertes JSON geprueft, sondern immer
ueber den rohen payloadB64-Teilstring, den wir empfangen haben - JSON wird
erst NACH erfolgreicher Pruefung dekodiert, rein um die Felder zu lesen.
Dadurch spielen Unterschiede zwischen JSON.stringify (TS) und json.dumps
(Python) keine Rolle.
"""
import base64
import hashlib
import hmac
import json
import os
import time


class TokenError(Exception):
    """Basisklasse fuer alle Token-Fehler."""


class TokenInvalid(TokenError):
    """Signatur passt nicht oder Format ist kaputt."""


class TokenExpired(TokenError):
    """Signatur ist gueltig, aber der Token ist zu alt."""


class TokenWrongKind(TokenError):
    """Signatur ist gueltig, aber fuer einen anderen Zweck ausgestellt."""


def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _b64url_decode(s):
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def verify(token, expected_kind, secret=None, ttl_days=None):
    """Prueft Signatur, Ablauf und "kind" eines Tokens und gibt das Payload-
    Dict ({taskId, kind, iat, jti}) zurueck. Wirft TokenInvalid/TokenExpired/
    TokenWrongKind bei Problemen - Aufrufer sollen diese gezielt abfangen, um
    unterschiedliche Fehlerseiten zu zeigen."""
    if secret is None or ttl_days is None:
        cfg = _load_config()
        if secret is None:
            secret = cfg["approval_link_secret"]
        if ttl_days is None:
            ttl_days = cfg.get("approval_link_ttl_days", 30)

    if not secret:
        raise TokenInvalid("approval_link_secret ist nicht konfiguriert")

    try:
        payload_b64, signature = token.rsplit(".", 1)
    except ValueError:
        raise TokenInvalid("Token hat nicht das erwartete Format")

    expected_signature = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise TokenInvalid("Signatur stimmt nicht")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError):
        raise TokenInvalid("Payload konnte nicht dekodiert werden")

    if payload.get("kind") != expected_kind:
        raise TokenWrongKind(f"Token ist fuer '{payload.get('kind')}', nicht '{expected_kind}'")

    iat = payload.get("iat")
    if not isinstance(iat, (int, float)):
        raise TokenInvalid("Payload hat kein gueltiges 'iat'")
    if time.time() - iat > ttl_days * 86400:
        raise TokenExpired("Link ist abgelaufen")

    if not payload.get("taskId") or not payload.get("jti"):
        raise TokenInvalid("Payload fehlen Pflichtfelder")

    return payload
