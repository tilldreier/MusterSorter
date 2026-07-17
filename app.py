"""
Review-Oberflaeche fuer Musterfotos vom Strumpfhersteller (Rotex).

Start:  .venv/bin/python app.py
Dann im Browser: http://127.0.0.1:5153

Ablauf:
1. "Neue Mails abrufen" holt neue Mustermails von maskova@rotexponozky.cz
   aus sales@dirtysox.ch und laedt die JPG-Anhaenge herunter.
2. Fuer jedes neue Foto wird automatisch ein Matching-Vorschlag berechnet
   (zuerst exakte Rotex-Nummer, sonst KI-Bildvergleich) - zeigt das Foto
   ueberhaupt keinen Musterstrumpf (z.B. Versand-Screenshot), wird es
   automatisch ignoriert statt der Person vorgelegt.
3. Pro BATCH (= eine Mail, alle Fotos darin gehoeren zur selben Rotex-Nummer/
   Bestellung): per Klick auf eine der Task-Kacheln (mit Referenzbild) den
   richtigen Task auswaehlen (Vorschlag ist vorausgewaehlt) und bestaetigen,
   oder alle Fotos des Batches manuell ignorieren. Bei Bestaetigung werden
   ALLE Fotos des Batches auf einmal verarbeitet: Anhaenge hochladen,
   Rotex-Nummer-Feld setzen, Status auf "Muster Erhalten", Kopien nach
   SharePoint, EINE Kunden-Mail mit allen Fotos versenden.
"""

import hmac
import json
import os
import threading

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, url_for

import clickup_ops
import fetch
import matching
import state
from approval_routes import approval_bp

app = Flask(__name__)
app.register_blueprint(approval_bp)


def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = _load_config()
os.makedirs(CFG["staging_dir"], exist_ok=True)

# Wie beim Freisteller Sorter: fetch.run() (Mail-Abruf) und die anschliessende
# Vorschlags-Berechnung (KI-Bildvergleich, kann ein paar Sekunden dauern)
# duerfen keinen HTTP-Request/Worker blockieren - deshalb im Hintergrund-Thread.
_fetch_lock = threading.Lock()


def _generate_suggestions():
    """Berechnet fuer alle Fotos ohne Vorschlag den Matching-Vorschlag und
    speichert ihn in state.json - laeuft nach fetch.run() im selben
    Hintergrund-Thread."""
    api_key = CFG.get("anthropic_api_key")
    for batch_id, batch in state.get_batches().items():
        if batch.get("status") == "done":
            continue
        for filename, photo in batch["photos"].items():
            if photo["status"] != "pending" or photo.get("suggestion"):
                continue
            try:
                suggestion = matching.suggest_match(photo["path"], batch.get("rotex_nummer"), api_key)
            except Exception as exc:
                print(f"[WARN] Matching fehlgeschlagen fuer {batch_id}/{filename}: {exc}")
                suggestion = None
            if not suggestion:
                continue
            if suggestion.get("is_sock_photo") is False:
                # Kein Muster-Foto (z.B. Versand-Screenshot/Rechnung als
                # Anhang) - automatisch ignorieren statt der Person einen
                # sinnlosen Zuordnungs-Schritt vorzulegen.
                state.resolve_photo(batch_id, filename, "ignored")
                print(f"[INFO] {batch_id}/{filename} automatisch ignoriert "
                      f"(kein Musterfoto erkannt): {suggestion.get('reasoning')}")
                continue
            state.set_photo_suggestion(batch_id, filename, suggestion)


def _run_fetch_in_background():
    if not _fetch_lock.acquire(blocking=False):
        print("[INFO] Fetch laeuft bereits - ueberspringe diesen Trigger.")
        return
    try:
        fetch.run()
        _generate_suggestions()
    except Exception as exc:
        print(f"[FEHLER] Hintergrund-Fetch fehlgeschlagen: {exc}")
    finally:
        _fetch_lock.release()


INDEX_TEMPLATE = """
<!doctype html>
<html><head><title>Muster Sorter</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; }
  .batch { border: 1px solid #ddd; border-radius: 8px; padding: 1em; margin-bottom: 1em; }
  .batch h3 { margin: 0 0 0.3em 0; }
  .meta { color: #666; font-size: 0.9em; }
  a.button, button { display: inline-block; background: #00a0e3; color: white; border: none;
    padding: 0.6em 1.2em; border-radius: 6px; text-decoration: none; cursor: pointer; font-size: 1em; }
</style></head>
<body>
<h1>Muster Sorter</h1>
<form method="post" action="{{ url_for('do_fetch') }}">
  <button type="submit">Neue Mails abrufen</button>
</form>
{% if fetch_message %}<p><strong>{{ fetch_message }}</strong></p>{% endif %}
<h2>Offene Musterfotos</h2>
{% if not open_batches %}<p>Keine offenen Musterfotos - alles einsortiert.</p>{% endif %}
{% for batch_id, b in open_batches %}
  <div class="batch">
    <h3><a href="{{ url_for('batch_detail', batch_id=batch_id) }}">{{ b.subject }}</a></h3>
    <div class="meta">{{ b.received }} - Rotex-Nr.: {{ b.rotex_nummer or "unbekannt" }} -
      {{ pending_counts[batch_id] }} noch offen</div>
  </div>
{% endfor %}
</body></html>
"""

BATCH_TEMPLATE = """
<!doctype html>
<html><head><title>Batch {{ batch_id }}</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; }
  .gallery { display: flex; gap: 1em; margin-bottom: 1em; flex-wrap: wrap; }
  .gallery img { max-width: 220px; max-height: 220px; object-fit: contain; border: 1px solid #ddd;
    border-radius: 6px; }
  .suggestion { background: #eaf7ff; border-radius: 6px; padding: 0.6em 1em; margin-bottom: 1em; }
  .suggestion.none { background: #fff3e0; }
  .task-grid { display: flex; flex-wrap: wrap; gap: 0.8em; margin: 1em 0; }
  .task-card { border: 3px solid #ddd; border-radius: 8px; padding: 0.5em; width: 140px;
    text-align: center; cursor: pointer; }
  .task-card img { width: 100%; height: 100px; object-fit: contain; }
  .task-card input { display: none; }
  .task-card:has(input:checked) { border-color: #00a0e3; background: #eaf7ff; }
  .task-card .name { font-size: 0.85em; margin-top: 0.3em; }
  .actions button { margin-right: 0.6em; padding: 0.6em 1.2em; border: none; border-radius: 6px;
    background: #00a0e3; color: white; cursor: pointer; font-size: 1em; }
  .actions button.ignore { background: #999; }
</style>
</head>
<body>
<p><a href="{{ url_for('index') }}">&larr; zurueck</a></p>
<h1>{{ batch.subject }}</h1>
<p class="meta">{{ batch.received }} - Rotex-Nr.: {{ batch.rotex_nummer or "unbekannt" }} -
  {{ pending_photos|length }} Foto(s), gehoeren alle zur selben Bestellung</p>

<div class="gallery">
  {% for filename, photo in pending_photos %}
    <img src="{{ url_for('photo_image', batch_id=batch_id, filename=filename) }}">
  {% endfor %}
</div>

{% if suggestion and suggestion.task_id %}
  <div class="suggestion">
    <strong>Vorschlag:</strong> {{ suggestion.task_name }}
    ({{ suggestion.source }}, Confidence: {{ suggestion.confidence }})<br>
    <small>{{ suggestion.reasoning }}</small>
  </div>
{% elif suggestion %}
  <div class="suggestion none">
    <strong>Kein automatischer Treffer.</strong>
    <small>{{ suggestion.reasoning }}</small>
  </div>
{% endif %}

<form method="post" action="{{ url_for('assign_batch_photos', batch_id=batch_id) }}">
  <div class="task-grid">
    {% for t in candidate_tasks %}
      <label class="task-card">
        <input type="radio" name="task_id" value="{{ t.id }}"
               {% if suggestion and suggestion.task_id == t.id %}checked{% endif %}>
        <img src="{{ t.thumb }}">
        <div class="name">{{ t.name }}</div>
      </label>
    {% endfor %}
  </div>
  <div class="actions">
    <button type="submit">Alle Fotos bestaetigen &amp; einsortieren</button>
    <button type="submit" formaction="{{ url_for('ignore_batch_photos', batch_id=batch_id) }}"
            formnovalidate class="ignore">Alle Fotos ignorieren</button>
  </div>
</form>
</body></html>
"""


def _open_batches():
    batches = state.get_batches()
    return sorted(
        ((bid, b) for bid, b in batches.items() if b.get("status") != "done"),
        key=lambda kv: kv[1].get("received", ""),
        reverse=True,
    )


def _pending_photos(batch):
    return [(name, p) for name, p in batch["photos"].items() if p["status"] == "pending"]


@app.route("/")
def index():
    open_batches = _open_batches()
    pending_counts = {bid: len(_pending_photos(b)) for bid, b in open_batches}
    return render_template_string(
        INDEX_TEMPLATE,
        open_batches=open_batches,
        pending_counts=pending_counts,
        fetch_message=request.args.get("msg"),
    )


@app.route("/fetch", methods=["POST"])
def do_fetch():
    if _fetch_lock.locked():
        msg = "Es laeuft bereits ein Abruf im Hintergrund - bitte kurz warten."
    else:
        threading.Thread(target=_run_fetch_in_background, daemon=True).start()
        msg = "Abruf gestartet - neue Musterfotos erscheinen hier, sobald sie fertig sind."
    return redirect(url_for("index", msg=msg))


@app.route("/internal/fetch", methods=["POST"])
def internal_fetch():
    expected_secret = CFG.get("internal_fetch_secret") or ""
    provided_secret = request.headers.get("X-Internal-Secret", "")
    if not expected_secret or not hmac.compare_digest(provided_secret, expected_secret):
        abort(401)

    if _fetch_lock.locked():
        return jsonify({"ok": True, "status": "already_running"})

    threading.Thread(target=_run_fetch_in_background, daemon=True).start()
    return jsonify({"ok": True, "status": "started"})


def _batch_suggestion(batch):
    """Alle Fotos eines Batches gehoeren zur selben Bestellung/demselben Task
    (gleiche Rotex-Nummer) - deshalb genuegt EIN repraesentativer Vorschlag
    fuer den ganzen Batch statt einem pro Foto."""
    for _, photo in _pending_photos(batch):
        if photo.get("suggestion"):
            return photo["suggestion"]
    return None


@app.route("/batch/<batch_id>")
def batch_detail(batch_id):
    batch = state.get_batch(batch_id)
    if not batch:
        return "Batch nicht gefunden", 404
    candidates_with_thumbs = matching.enrich_with_thumbnails(matching.get_candidates())
    candidate_tasks = [
        {"id": task["id"], "name": task["name"], "thumb": thumb_url}
        for task, thumb_url in candidates_with_thumbs
    ]
    return render_template_string(
        BATCH_TEMPLATE,
        batch_id=batch_id,
        batch=batch,
        pending_photos=_pending_photos(batch),
        suggestion=_batch_suggestion(batch),
        candidate_tasks=candidate_tasks,
    )


@app.route("/photo/<batch_id>/<filename>")
def photo_image(batch_id, filename):
    batch = state.get_batch(batch_id)
    if not batch:
        return "not found", 404
    photo = batch["photos"].get(filename)
    if not photo:
        return "not found", 404
    return send_file(photo["path"])


@app.route("/batch/<batch_id>/assign", methods=["POST"])
def assign_batch_photos(batch_id):
    """Ordnet ALLE noch offenen Fotos des Batches auf einmal demselben Task
    zu - ein Batch (eine Mail) enthaelt immer nur Fotos derselben Rotex-
    Nummer/Bestellung, es gibt also nie unterschiedliche Ziel-Tasks
    innerhalb eines Batches. Das vermeidet auch das Problem, dass der Task
    nach der ersten Zuordnung aus der Kandidatenliste verschwindet (weil
    sich sein ClickUp-Status aendert) - alle Fotos werden VOR der
    Statusaenderung verarbeitet."""
    batch = state.get_batch(batch_id)
    if not batch:
        return "Batch nicht gefunden", 404

    task_id = request.form.get("task_id", "").strip()
    if not task_id:
        return redirect(url_for("batch_detail", batch_id=batch_id))

    pending = _pending_photos(batch)
    if not pending:
        return redirect(url_for("batch_detail", batch_id=batch_id))

    task = clickup_ops.get_task_details(task_id)

    photo_paths = [photo["path"] for _, photo in pending]
    for path in photo_paths:
        clickup_ops.upload_attachment(task_id, path)

    rotex_nummer = batch.get("rotex_nummer")
    if rotex_nummer and not clickup_ops.get_rotex_nummer(task):
        clickup_ops.set_custom_field(task_id, clickup_ops.FIELD_ID_ROTEX_NUMMER, rotex_nummer)

    clickup_ops.set_task_status(task_id, CFG["clickup_status_done"])

    sharepoint_folder, sharepoint_error = clickup_ops.resolve_sharepoint_folder(task)
    if sharepoint_folder:
        for path in photo_paths:
            try:
                clickup_ops._copy_to_onedrive(path, sharepoint_folder)
            except Exception as exc:
                print(f"[WARN] SharePoint-Kopie fehlgeschlagen fuer {batch_id}/{path}: {exc}")
    else:
        print(f"[WARN] Nicht nach SharePoint kopiert - {sharepoint_error}")

    # Die Kunden-Benachrichtigung wird NICHT mehr hier verschickt - sobald der
    # Status oben auf CFG["clickup_status_done"] wechselt, feuert eine
    # ClickUp-Automation einen Webhook, den Trigger.dev's
    # "sample-approval-trigger"-Task empfaengt und der die Mail (inkl.
    # Freigabe-/Mengen-Bestaetigungslink) verschickt - siehe approval_routes.py.

    for filename, _ in pending:
        state.resolve_photo(batch_id, filename, "assigned",
                             assigned_task_id=task_id, assigned_task_name=task["name"])
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.route("/batch/<batch_id>/ignore", methods=["POST"])
def ignore_batch_photos(batch_id):
    batch = state.get_batch(batch_id)
    if not batch:
        return "Batch nicht gefunden", 404
    for filename, _ in _pending_photos(batch):
        state.resolve_photo(batch_id, filename, "ignored")
    return redirect(url_for("batch_detail", batch_id=batch_id))


if __name__ == "__main__":
    # WICHTIG: bewusst Flasks eigener Server, NICHT gunicorn (siehe
    # freisteller/app.py fuer die ausfuehrliche Begruendung - gunicorns
    # fork() verursachte OneDrive-Deadlocks).
    app.run(host="127.0.0.1", port=5153, debug=False)
