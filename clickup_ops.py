"""
ClickUp-Hilfsfunktionen fuer den Muster Sorter - wiederverwendet dieselben,
bereits bewaehrten Muster wie clickup-photoshop/clickup_watcher.py (gleiche
Codebasis-Familie, gleicher Workspace).
"""
import io
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

import requests

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_CFG = _load_config()
CLICKUP_API_TOKEN = _CFG["clickup_api_token"]
LIST_ID = _CFG["clickup_list_id"]
FIELD_ID_ROTEX_NUMMER = _CFG["clickup_field_rotex_nummer"]
FIELD_ID_SHAREPOINT = _CFG["clickup_field_sharepoint"]
FIELD_ID_EMAIL = _CFG["clickup_field_email"]
SHAREPOINT_LOCAL_BASE = _CFG["sharepoint_base"]

# Optional (per-Groesse) Mengenfelder fuer die Muster-Freigabe - .get() statt
# direkter Indizierung, damit ein config.json ohne diese Schluessel (z.B. vor
# dem ersten Deploy dieser Funktion) nicht den kompletten App-Import zum
# Absturz bringt.
FIELD_ID_MENGE_TOTAL = _CFG.get("clickup_field_menge_total") or None
FIELD_ID_MENGE_XS = _CFG.get("clickup_field_menge_xs") or None
FIELD_ID_MENGE_S = _CFG.get("clickup_field_menge_s") or None
FIELD_ID_MENGE_M = _CFG.get("clickup_field_menge_m") or None
FIELD_ID_MENGE_L = _CFG.get("clickup_field_menge_l") or None

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"
HEADERS = {
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type": "application/json",
}


_RETRY_DELAYS = [1, 2, 4, 8]


def _with_retry(func):
    """Wiederholt func() bei subprocess.CalledProcessError mit steigender
    Wartezeit - beobachtet, dass /bin/cp gegen einen frisch angelegten/
    gerade erst synchronisierten OneDrive-Ordner vereinzelt beim ERSTEN
    Versuch mit exit code 1 scheitert (z.B. weil OneDrive den Ordner/die
    Metadaten in dem Moment noch aktualisiert), ein Wiederholungsversuch
    kurz danach aber zuverlaessig klappt (siehe freisteller/fs_ops.py fuer
    das gleiche, dort bereits geloeste Muster)."""
    last_exc = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return func()
        except subprocess.CalledProcessError as exc:
            last_exc = exc
    raise last_exc


def _copy_to_onedrive(src, dest_dir):
    """Kopiert src nach dest_dir per frisch gespawntem /bin/cp statt Pythons
    shutil.copy2() - Pythons eigene os/shutil-Syscalls scheitern reproduzierbar
    mit "OSError: [Errno 11] Resource deadlock avoided" gegen OneDrive-Pfade,
    wenn sie innerhalb eines langlaufenden Prozesses aufgerufen werden (siehe
    freisteller-/clickup-photoshop-Projekt fuer die ausfuehrliche Herleitung)."""
    dest_path = os.path.join(dest_dir, os.path.basename(src))
    _with_retry(lambda: subprocess.run(["/bin/cp", src, dest_path], check=True, capture_output=True))


def get_tasks_by_status(status_name):
    """Liefert alle Tasks der Liste mit dem angegebenen Status (z.B.
    "Muster Bestellt") - Kandidatenkreis fuer das Matching."""
    url = f"{CLICKUP_API_BASE}/list/{LIST_ID}/task"
    params = {"archived": "false", "include_closed": "true"}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    tasks = resp.json().get("tasks", [])
    return [t for t in tasks if t.get("status", {}).get("status", "").lower() == status_name.lower()]


def get_task_details(task_id):
    """Holt den vollstaendigen Task inkl. "attachments" ueber den Einzel-Task-
    Endpunkt (der Listen-Endpunkt liefert Attachments nicht mit)."""
    url = f"{CLICKUP_API_BASE}/task/{task_id}"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def _read_field_value(task, field_id):
    for field in task.get("custom_fields", []):
        if field.get("id") == field_id:
            value = field.get("value")
            if value:
                return str(value).strip()
    return None


def get_rotex_nummer(task):
    return _read_field_value(task, FIELD_ID_ROTEX_NUMMER) if FIELD_ID_ROTEX_NUMMER else None


def get_email(task):
    return _read_field_value(task, FIELD_ID_EMAIL) if FIELD_ID_EMAIL else None


def get_menge_total(task):
    return _read_field_value(task, FIELD_ID_MENGE_TOTAL) if FIELD_ID_MENGE_TOTAL else None


def get_menge_xs(task):
    return _read_field_value(task, FIELD_ID_MENGE_XS) if FIELD_ID_MENGE_XS else None


def get_menge_s(task):
    return _read_field_value(task, FIELD_ID_MENGE_S) if FIELD_ID_MENGE_S else None


def get_menge_m(task):
    return _read_field_value(task, FIELD_ID_MENGE_M) if FIELD_ID_MENGE_M else None


def get_menge_l(task):
    return _read_field_value(task, FIELD_ID_MENGE_L) if FIELD_ID_MENGE_L else None


def resolve_sharepoint_folder(task):
    """Leitet aus dem Custom Field "SharePoint Projektordner" den lokalen Pfad
    im gemounteten OneDrive-Sync ab. Gibt den Pfad nur zurueck, wenn er lokal
    TATSAECHLICH existiert - kein Fuzzy-Match/Raten (siehe clickup_watcher.py
    fuer die ausfuehrliche Begruendung)."""
    url = _read_field_value(task, FIELD_ID_SHAREPOINT)
    if not url:
        return None, "Feld 'SharePoint Projektordner' ist leer"

    decoded = unquote(url).replace("&#x3a;", ":")
    marker = "Freigegebene Dokumente/"
    idx = decoded.find(marker)
    if idx == -1:
        return None, f"Marker '{marker}' nicht in URL gefunden: {decoded}"

    relative_path = decoded[idx + len(marker):]
    candidate = str(Path(SHAREPOINT_LOCAL_BASE) / relative_path)
    if os.path.isdir(candidate):
        return candidate, None
    return None, f"Ordner existiert lokal nicht (OneDrive-Sync?): {candidate}"


def upload_attachment_bytes(task_id, file_bytes, filename, content_type=None):
    """Wie upload_attachment(), nimmt aber Bytes statt eines lokalen Pfads -
    fuer Dateien, die wir bereits im Speicher haben (z.B. von einer ClickUp-
    Attachment-URL heruntergeladen), ohne Umweg ueber eine Temp-Datei."""
    url = f"{CLICKUP_API_BASE}/task/{task_id}/attachment"
    file_obj = io.BytesIO(file_bytes)
    files = {"attachment": (filename, file_obj, content_type) if content_type else (filename, file_obj)}
    headers = {"Authorization": CLICKUP_API_TOKEN}
    resp = requests.post(url, headers=headers, files=files)
    resp.raise_for_status()
    return resp.json()


def upload_attachment(task_id, file_path):
    with open(file_path, "rb") as f:
        return upload_attachment_bytes(task_id, f.read(), Path(file_path).name)


def delete_attachment(attachment_id):
    """Loescht einen bestehenden Anhang - genutzt, um einen Entwurf beim
    Freigeben unter neuem (mit APPROVED_ markiertem) Dateinamen neu
    hochzuladen. Nicht Teil von ClickUps offiziell dokumentierter API-
    Referenz - vor produktivem Einsatz gegen einen echten Test-Anhang
    verifizieren."""
    url = f"{CLICKUP_API_BASE}/attachment/{attachment_id}"
    resp = requests.delete(url, headers=HEADERS)
    if resp.status_code not in (200, 204):
        print(f"  WARNUNG: Anhang {attachment_id} konnte nicht geloescht werden "
              f"({resp.status_code}): {resp.text}")


def post_comment(task_id, comment_text):
    url = f"{CLICKUP_API_BASE}/task/{task_id}/comment"
    payload = {"comment_text": comment_text, "notify_all": False}
    resp = requests.post(url, headers=HEADERS, json=payload)
    if resp.status_code not in (200, 201):
        print(f"  WARNUNG: Kommentar konnte nicht gepostet werden "
              f"({resp.status_code}): {resp.text}")


def set_task_name(task_id, name):
    url = f"{CLICKUP_API_BASE}/task/{task_id}"
    payload = {"name": name}
    resp = requests.put(url, headers=HEADERS, json=payload)
    if resp.status_code not in (200, 201):
        print(f"  WARNUNG: Taskname konnte nicht auf '{name}' gesetzt werden "
              f"({resp.status_code}): {resp.text}")


def set_task_status(task_id, status_name):
    url = f"{CLICKUP_API_BASE}/task/{task_id}"
    payload = {"status": status_name}
    resp = requests.put(url, headers=HEADERS, json=payload)
    if resp.status_code not in (200, 201):
        print(f"  WARNUNG: Status konnte nicht auf '{status_name}' gesetzt werden "
              f"({resp.status_code}): {resp.text}")


def set_custom_field(task_id, field_id, value):
    """Setzt ein Custom Field (z.B. die Rotex-Nummer) - nur wenn eine Feld-ID
    konfiguriert ist. Ueberschreibt keinen bereits vorhandenen Wert (siehe
    Aufrufer: nur setzen, wenn get_rotex_nummer() zuvor None ergab)."""
    if not field_id:
        return
    url = f"{CLICKUP_API_BASE}/task/{task_id}/field/{field_id}"
    resp = requests.post(url, headers=HEADERS, json={"value": value})
    if resp.status_code not in (200, 201):
        print(f"  WARNUNG: Custom Field {field_id} konnte nicht gesetzt werden "
              f"({resp.status_code}): {resp.text}")
