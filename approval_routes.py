"""
Kunden-Freigabeseiten, die aus den von Trigger.dev verschickten Freigabe-
Mails verlinkt werden (siehe approval_tokens.py fuer das Token-Format).
Dieses Blueprint ist bewusst der EINZIGE oeffentlich (ohne Cloudflare Access)
erreichbare Teil dieser App - die Signatur + Einmal-Verwendung des Tokens ist
die alleinige Authentifizierung, es gibt keine Session/kein Login.

Zwei Flows:
  - /approve/design/<token>   - Entwurf(e) freigeben oder mit Begruendung ablehnen
  - /approve/sample/<token>   - Muster bestaetigen (inkl. Mengen) oder ablehnen
"""
import json
import os
import re

import requests
from flask import Blueprint, render_template_string, request

import clickup_ops
import state
from approval_tokens import TokenError, TokenExpired, TokenInvalid, TokenWrongKind, verify

approval_bp = Blueprint("approval", __name__)

# Muss mit DESIGN_SHEET_JPG_PATTERN in design-sheet.ts (Repo A) uebereinstimmen.
DESIGN_SHEET_PATTERN = re.compile(r"designsheet.*\.jpe?g$", re.IGNORECASE)
APPROVED_PREFIX = "APPROVED_"

IMAGE_MIMETYPE_PREFIX = "image/"

# Fuer die Muster-Bestaetigungsseite: es gibt keine Namenskonvention, die Musterfotos POSITIV
# erkennt, aber Designsheets/Templates sind bereits zuverlaessig erkennbar - die auszuschliessen
# (statt Musterfotos positiv zu matchen) verhindert, dass hochaufgeloeste Design-Dateien mit
# angezeigt werden. Bewusst breiter als DESIGN_SHEET_PATTERN (das nur die eine kanonische .jpg
# fuer die woechentliche Mail matcht) - muss auch die .png-Varianten (Vollaufloesung und "_small")
# erfassen, die neben ihr auf dem Task liegen. Muss mit DESIGN_ARTIFACT_PATTERN in
# sample-approval-trigger.ts (Repo A) uebereinstimmen.
DESIGN_ARTIFACT_PATTERN = re.compile(r"designsheet|template", re.IGNORECASE)


def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = _load_config()

RESULT_TEMPLATE = """
<!doctype html>
<html><head><title>DirtySox</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 4em auto; padding: 0 1em;
    text-align: center; }
  h1 { color: {{ '#c0392b' if is_error else '#00a0e3' }}; }
</style></head>
<body>
<h1>{{ title }}</h1>
<p>{{ message }}</p>
</body></html>
"""


def _render_result(title, message, is_error=False):
    return render_template_string(RESULT_TEMPLATE, title=title, message=message, is_error=is_error)


def _verify_or_error(token, kind):
    """Prueft Token + Einmal-Verwendung. Gibt (payload, None) bei Erfolg oder
    (None, error_response) zurueck, wenn die Aufruferroute sofort eine
    Fehlerseite zeigen soll."""
    try:
        payload = verify(token, kind, CFG.get("approval_link_secret"), CFG.get("approval_link_ttl_days", 30))
    except TokenExpired:
        return None, _render_result("Link abgelaufen", "Dieser Link ist nicht mehr gültig. Bitte wende dich an DirtySox.", is_error=True)
    except TokenWrongKind:
        return None, _render_result("Ungültiger Link", "Dieser Link gehört zu einem anderen Vorgang.", is_error=True)
    except TokenInvalid:
        return None, _render_result("Ungültiger Link", "Dieser Link ist nicht gültig.", is_error=True)
    except TokenError:
        return None, _render_result("Ungültiger Link", "Dieser Link konnte nicht verarbeitet werden.", is_error=True)

    if state.is_token_consumed(payload["jti"]):
        return None, _render_result("Bereits bearbeitet", "Zu diesem Link wurde bereits eine Entscheidung gespeichert.")

    return payload, None


DESIGN_APPROVAL_TEMPLATE = """
<!doctype html>
<html><head><title>Entwurf freigeben</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 700px; margin: 2em auto; padding: 0 1em; }
  .gallery { display: flex; gap: 1em; margin-bottom: 1em; flex-wrap: wrap; }
  .design-card { border: 3px solid #ddd; border-radius: 8px; padding: 0.5em; width: 200px;
    text-align: center; }
  .design-thumb-wrap { position: relative; cursor: pointer; }
  .design-card img { width: 100%; max-height: 220px; object-fit: contain; }
  .design-card:has(input:checked) { border-color: #00a0e3; background: #eaf7ff; }
  .view-full { position: absolute; bottom: 6px; right: 6px; background: rgba(0,0,0,0.6); color: #fff;
    padding: 0.25em 0.6em; border-radius: 4px; font-size: 0.75em; text-decoration: none; }
  .select-label { display: block; margin-top: 0.4em; font-size: 0.9em; cursor: pointer; }
  textarea { width: 100%; box-sizing: border-box; padding: 0.6em; margin-top: 0.5em; }
  .actions { margin-top: 1.2em; }
  .actions button { margin-right: 0.6em; padding: 0.7em 1.4em; border: none; border-radius: 6px;
    color: white; cursor: pointer; font-size: 1em; }
  .actions button.approve { background: #00a0e3; }
  .actions button.reject { background: #c0392b; }
  .error { color: #c0392b; margin: 0.5em 0; }
  .lightbox { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.88); align-items: center; justify-content: center; z-index: 1000; }
  .lightbox:target { display: flex; }
  .lightbox img { max-width: 92%; max-height: 92%; object-fit: contain; }
  .lightbox-close { position: absolute; top: 16px; right: 24px; color: #fff; font-size: 2em;
    text-decoration: none; line-height: 1; }
</style></head>
<body>
<h1>Entwurf-Freigabe</h1>
<p>{{ task_name }}</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post">
  {% if candidates|length > 1 %}
    <p>Bitte wähle die Entwürfe aus, die du freigeben möchtest:</p>
  {% else %}
    <p>Bitte wähle den Entwurf aus, den du freigeben möchtest:</p>
  {% endif %}
  <div class="gallery">
    {% for a in candidates %}
      <div class="design-card">
        <div class="design-thumb-wrap">
          <img src="{{ a.url }}">
          <a href="#lightbox-{{ loop.index }}" class="view-full">Vollbild</a>
        </div>
        <label class="select-label">
          <input type="checkbox" name="approve_attachment_id" value="{{ a.id }}"> auswählen
        </label>
      </div>
    {% endfor %}
  </div>
  <p>Falls du ablehnst, teile uns bitte kurz den Grund mit:</p>
  <textarea name="reject_reason" rows="3" placeholder="Grund für die Ablehnung (nur bei Ablehnung erforderlich)"></textarea>
  <div class="actions">
    <button type="submit" name="action" value="approve" class="approve">Freigeben</button>
    <button type="submit" name="action" value="reject" class="reject" formnovalidate>Ablehnen</button>
  </div>
</form>
{% for a in candidates %}
  <div id="lightbox-{{ loop.index }}" class="lightbox">
    <a href="#" class="lightbox-close">&times;</a>
    <img src="{{ a.url }}">
  </div>
{% endfor %}
</body></html>
"""

SAMPLE_APPROVAL_TEMPLATE = """
<!doctype html>
<html><head><title>Muster bestätigen</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 700px; margin: 2em auto; padding: 0 1em; }
  .gallery { display: flex; gap: 1em; margin-bottom: 1em; flex-wrap: wrap; }
  .gallery img { max-width: 220px; max-height: 220px; object-fit: contain; border: 1px solid #ddd;
    border-radius: 6px; }
  .sizes { display: flex; gap: 1em; margin: 1em 0; }
  .sizes label { display: flex; flex-direction: column; font-size: 0.9em; }
  .sizes input { width: 70px; padding: 0.4em; margin-top: 0.3em; }
  textarea { width: 100%; box-sizing: border-box; padding: 0.6em; margin-top: 0.5em; }
  .actions { margin-top: 1.2em; }
  .actions button { margin-right: 0.6em; padding: 0.7em 1.4em; border: none; border-radius: 6px;
    color: white; cursor: pointer; font-size: 1em; }
  .actions button.approve { background: #00a0e3; }
  .actions button.reject { background: #c0392b; }
  .error { color: #c0392b; margin: 0.5em 0; }
</style></head>
<body>
<h1>Muster-Bestätigung</h1>
<p>{{ task_name }}</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<div class="gallery">
  {% for photo in sample_photos %}<img src="{{ photo.url }}">{% endfor %}
</div>
<form method="post">
  <p>Bitte prüfe/korrigiere die Bestellmenge pro Größe:</p>
  <div class="sizes">
    <label>XS<input type="number" min="0" name="menge_xs" value="{{ menge_xs }}"></label>
    <label>S<input type="number" min="0" name="menge_s" value="{{ menge_s }}"></label>
    <label>M<input type="number" min="0" name="menge_m" value="{{ menge_m }}"></label>
    <label>L<input type="number" min="0" name="menge_l" value="{{ menge_l }}"></label>
  </div>
  <p>Falls das Muster nicht deinen Erwartungen entspricht, teile uns bitte kurz den Grund mit:</p>
  <textarea name="reject_reason" rows="3" placeholder="Grund für die Ablehnung (nur bei Ablehnung erforderlich)"></textarea>
  <div class="actions">
    <button type="submit" name="action" value="confirm" class="approve">Bestätigen</button>
    <button type="submit" name="action" value="reject" class="reject" formnovalidate>Ablehnen</button>
  </div>
</form>
</body></html>
"""


def _design_candidates(task):
    return [a for a in task.get("attachments", []) if DESIGN_SHEET_PATTERN.search(a.get("title", ""))]


@approval_bp.route("/approve/design/<token>", methods=["GET", "POST"])
def approve_design(token):
    payload, error_response = _verify_or_error(token, "design_approval")
    if error_response:
        return error_response

    task_id = payload["taskId"]
    task = clickup_ops.get_task_details(task_id)
    candidates = _design_candidates(task)

    if request.method == "GET":
        return render_template_string(DESIGN_APPROVAL_TEMPLATE, task_name=task["name"], candidates=candidates, error=None)

    action = request.form.get("action")

    if action == "reject":
        reason = request.form.get("reject_reason", "").strip()
        if not reason:
            return render_template_string(
                DESIGN_APPROVAL_TEMPLATE, task_name=task["name"], candidates=candidates,
                error="Bitte gib einen Grund für die Ablehnung an.",
            )
        clickup_ops.set_task_status(task_id, CFG["clickup_status_draft_rejected"])
        clickup_ops.post_comment(task_id, f"Entwurf vom Kunden abgelehnt: {reason}")
        state.mark_token_consumed(payload["jti"])
        return _render_result("Danke für dein Feedback", "Wir haben deine Ablehnung samt Begründung erhalten und überarbeiten den Entwurf.")

    if action == "approve":
        selected_ids = request.form.getlist("approve_attachment_id")
        if not selected_ids:
            return render_template_string(
                DESIGN_APPROVAL_TEMPLATE, task_name=task["name"], candidates=candidates,
                error="Bitte wähle mindestens einen Entwurf aus.",
            )
        by_id = {a["id"]: a for a in candidates}
        for attachment_id in selected_ids:
            attachment = by_id.get(attachment_id)
            if not attachment:
                continue
            resp = requests.get(attachment["url"], timeout=30)
            resp.raise_for_status()
            try:
                clickup_ops.delete_attachment(attachment_id)
            except Exception as exc:
                print(f"[WARN] Konnte alten Anhang {attachment_id} nicht löschen: {exc}")
            clickup_ops.upload_attachment_bytes(
                task_id, resp.content, f"{APPROVED_PREFIX}{attachment['title']}",
                content_type=attachment.get("mimetype"),
            )
        clickup_ops.set_task_status(task_id, CFG["clickup_status_draft_approved"])
        state.mark_token_consumed(payload["jti"])
        return _render_result("Vielen Dank!", "Deine Freigabe wurde gespeichert.")

    return render_template_string(DESIGN_APPROVAL_TEMPLATE, task_name=task["name"], candidates=candidates, error="Bitte wähle 'Freigeben' oder 'Ablehnen'.")


@approval_bp.route("/approve/sample/<token>", methods=["GET", "POST"])
def approve_sample(token):
    payload, error_response = _verify_or_error(token, "sample_confirm")
    if error_response:
        return error_response

    task_id = payload["taskId"]
    task = clickup_ops.get_task_details(task_id)
    sample_photos = [
        a for a in task.get("attachments", [])
        if (a.get("mimetype") or "").startswith(IMAGE_MIMETYPE_PREFIX) and not DESIGN_ARTIFACT_PATTERN.search(a.get("title", ""))
    ]

    def _render_form(error=None, menge_xs=None, menge_s=None, menge_m=None, menge_l=None):
        return render_template_string(
            SAMPLE_APPROVAL_TEMPLATE,
            task_name=task["name"],
            sample_photos=sample_photos,
            error=error,
            menge_xs=menge_xs if menge_xs is not None else (clickup_ops.get_menge_xs(task) or 0),
            menge_s=menge_s if menge_s is not None else (clickup_ops.get_menge_s(task) or 0),
            menge_m=menge_m if menge_m is not None else (clickup_ops.get_menge_m(task) or 0),
            menge_l=menge_l if menge_l is not None else (clickup_ops.get_menge_l(task) or 0),
        )

    if request.method == "GET":
        return _render_form()

    action = request.form.get("action")

    if action == "reject":
        reason = request.form.get("reject_reason", "").strip()
        if not reason:
            return _render_form(error="Bitte gib einen Grund für die Ablehnung an.")
        # Mengen werden bei Ablehnung bewusst NICHT gespeichert.
        clickup_ops.set_task_status(task_id, CFG["clickup_status_sample_rejected"])
        clickup_ops.post_comment(task_id, f"Muster vom Kunden abgelehnt: {reason}")
        state.mark_token_consumed(payload["jti"])
        return _render_result("Danke für dein Feedback", "Wir haben deine Ablehnung samt Begründung erhalten.")

    if action == "confirm":
        raw = {
            "xs": request.form.get("menge_xs", ""),
            "s": request.form.get("menge_s", ""),
            "m": request.form.get("menge_m", ""),
            "l": request.form.get("menge_l", ""),
        }
        try:
            parsed = {key: int(value) for key, value in raw.items()}
            if any(value < 0 for value in parsed.values()):
                raise ValueError("negative Menge")
        except ValueError:
            return _render_form(error="Bitte gib für jede Größe eine ganze Zahl >= 0 an.",
                                 menge_xs=raw["xs"], menge_s=raw["s"], menge_m=raw["m"], menge_l=raw["l"])

        total = sum(parsed.values())
        clickup_ops.set_custom_field(task_id, clickup_ops.FIELD_ID_MENGE_XS, parsed["xs"])
        clickup_ops.set_custom_field(task_id, clickup_ops.FIELD_ID_MENGE_S, parsed["s"])
        clickup_ops.set_custom_field(task_id, clickup_ops.FIELD_ID_MENGE_M, parsed["m"])
        clickup_ops.set_custom_field(task_id, clickup_ops.FIELD_ID_MENGE_L, parsed["l"])
        clickup_ops.set_custom_field(task_id, clickup_ops.FIELD_ID_MENGE_TOTAL, total)
        clickup_ops.post_comment(
            task_id,
            f"Menge vom Kunden bestätigt: XS={parsed['xs']}, S={parsed['s']}, M={parsed['m']}, L={parsed['l']}, Total={total}",
        )
        state.mark_token_consumed(payload["jti"])
        return _render_result("Vielen Dank!", "Deine Bestätigung wurde gespeichert.")

    return _render_form(error="Bitte wähle 'Bestätigen' oder 'Ablehnen'.")
