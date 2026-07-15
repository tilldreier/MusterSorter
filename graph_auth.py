"""
Microsoft Graph Auth.

Bevorzugt: App-only via Client-Credentials-Flow (client_secret in
config.json gesetzt) - liest per Application-Permission direkt jedes
Postfach im Tenant, unabhaengig von Exchange-Delegationen/Full-Access.
Voraussetzung: Azure App-Registrierung mit
- Client-Secret (Zertifikate & Geheimnisse)
- Anwendungsberechtigung (nicht delegiert!) "Mail.ReadWrite"
- Administratorzustimmung erteilt

Fallback (falls kein client_secret gesetzt ist): delegierter
Device-Code-Login (MSAL, kein Secret noetig) - erfordert dann aber
Full-Access-Delegation auf das Zielpostfach in Exchange.
"""

import json
import os

import msal

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
TOKEN_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".token_cache.json")

DELEGATED_SCOPES = ["Mail.ReadWrite"]
APP_ONLY_SCOPES = ["https://graph.microsoft.com/.default"]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("client_id") or not cfg.get("tenant_id"):
        raise RuntimeError(
            f"Bitte client_id und tenant_id in {CONFIG_PATH} eintragen "
            "(siehe Azure App-Registrierung)."
        )
    return cfg


def _authority(cfg):
    return f"https://login.microsoftonline.com/{cfg['tenant_id']}"


def _get_app_only_token(cfg):
    app = msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        client_credential=cfg["client_secret"],
        authority=_authority(cfg),
    )
    result = app.acquire_token_for_client(scopes=APP_ONLY_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(
            f"App-only Login fehlgeschlagen: {result.get('error_description', result)}"
        )
    return result["access_token"]


def _get_delegated_token(cfg):
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        client_id=cfg["client_id"],
        authority=_authority(cfg),
        token_cache=cache,
    )

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=DELEGATED_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device-Flow konnte nicht gestartet werden: {flow}")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)

    if cache.has_state_changed:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())

    if "access_token" not in result:
        raise RuntimeError(f"Login fehlgeschlagen: {result.get('error_description', result)}")

    return result["access_token"]


def get_access_token():
    cfg = load_config()
    if cfg.get("client_secret"):
        return _get_app_only_token(cfg)
    return _get_delegated_token(cfg)


if __name__ == "__main__":
    token = get_access_token()
    print("Login erfolgreich, Token erhalten (gekuerzt):", token[:20], "...")
