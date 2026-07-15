"""
Ermittelt einen Matching-Vorschlag (ClickUp-Task) fuer ein neu eingetroffenes
Musterfoto:
  1. Exakter Abgleich der aus dem Betreff erkannten Rotex-Losnummer gegen das
     Custom Field der Kandidaten-Tasks (kostenlos, praezise).
  2. Falls kein Treffer: KI-Bildvergleich (Claude Vision) gegen die
     Design-Sheet-Vorschaubilder der Kandidaten.
Der Vorschlag ist IMMER nur eine Vorauswahl fuer die Review-UI - die
Zuordnung wird erst durch eine manuelle Bestaetigung wirksam.
"""
import base64
import io
import json
import os
import re

import requests
from anthropic import Anthropic
from PIL import Image

import clickup_ops

MODEL = "claude-sonnet-5"


def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_candidates():
    """Alle Tasks im Status "Muster Bestellt" - der Kandidatenkreis fuers
    Matching (nicht alle offenen Tasks)."""
    cfg = _load_config()
    return clickup_ops.get_tasks_by_status(cfg["clickup_status_candidate"])


def match_by_number(rotex_nummer, candidates):
    """Vergleicht die erkannte Rotex-Nummer exakt gegen das Custom Field der
    Kandidaten. Gibt den passenden Task zurueck oder None."""
    if not rotex_nummer:
        return None
    for task in candidates:
        if clickup_ops.get_rotex_nummer(task) == str(rotex_nummer):
            return task
    return None


def _reference_thumbnail_url(task):
    """Findet den Design-Sheet-Vorschaubild-Anhang eines Tasks (bevorzugt die
    kleine PNG-Variante, die design_sheet_batch.py erzeugt)."""
    best = None
    for att in task.get("attachments", []):
        title_lower = att.get("title", "").lower()
        if "designsheet" not in title_lower:
            continue
        if "small" in title_lower:
            return att.get("url")
        best = best or att.get("url")
    return best


MAX_IMAGE_DIMENSION = 1568  # von Anthropic empfohlenes Maximum, spart auch Tokens/Kosten


def _prepare_image_b64(raw_bytes):
    """Verkleinert/komprimiert ein Bild auf JPEG, bevor es an die Anthropic-
    API geschickt wird - Kamerafotos (z.B. von einem iPhone) sind oft
    8-15 MB gross und ueberschreiten Claudes 10-MB-Limit pro Bild, was sonst
    mit einem 400 "image exceeds 10 MB maximum" abbricht."""
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _download_image_b64(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return _prepare_image_b64(resp.content)


def _strip_markdown_fence(text):
    """Claude umschliesst die angeforderte JSON-Antwort manchmal mit
    Markdown-Codebloecken (```json ... ```), obwohl der Prompt reines JSON
    verlangt - ohne dieses Entfernen schlaegt json.loads() fehl und ein
    tatsaechlich gefundener Treffer wird faelschlich als "nicht lesbar"
    verworfen."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


def enrich_with_thumbnails(candidates):
    """Laedt zu jedem Kandidaten-Task das Referenz-Vorschaubild nach (der
    Listen-Endpunkt liefert - wie schon in clickup_watcher.py dokumentiert -
    keine "attachments" mit). Gibt eine Liste von (task, thumbnail_url)
    zurueck, nur fuer Tasks mit vorhandenem Bild. Wird sowohl fuer den
    KI-Bildvergleich als auch fuer die anklickbare Task-Auswahl in der
    Review-UI verwendet."""
    result = []
    for task in candidates:
        task_details = clickup_ops.get_task_details(task["id"])
        ref_url = _reference_thumbnail_url(task_details)
        if ref_url:
            result.append((task, ref_url))
    return result


def match_by_vision(photo_path, candidates, api_key):
    """Fragt Claude Vision, welcher Kandidat am ehesten zum Musterfoto passt,
    UND ob das Foto ueberhaupt einen Musterstrumpf zeigt (manche Anhaenge
    sind z.B. Versand-Screenshots statt Fotos - siehe is_sock_photo).
    Gibt (task_or_None, confidence, reasoning, is_sock_photo) zurueck."""
    candidates_with_refs = enrich_with_thumbnails(candidates)

    if not candidates_with_refs:
        return None, None, "Keine Kandidaten mit Referenzbild vorhanden.", True

    with open(photo_path, "rb") as f:
        photo_b64 = _prepare_image_b64(f.read())

    content = [
        {
            "type": "text",
            "text": (
                "Das erste Bild soll ein Foto eines physischen Musterstrumpfs "
                "sein - manchmal ist es aber stattdessen z.B. ein Screenshot "
                "von Versanddaten, eine Rechnung o.ae. Pruefe zuerst, ob das "
                "erste Bild ueberhaupt einen Strumpf zeigt (is_sock_photo). "
                "Falls ja: Danach folgen durchnummerierte Referenzbilder "
                "(Design-Sheets) bereits bekannter Strumpf-Designs. Finde "
                "heraus, ob eines der Referenzbilder DASSELBE Design zeigt "
                "(Logo, Text, Farben, Muster) wie das Foto - nicht nur eine "
                "aehnliche Grundfarbe. "
                "Antworte NUR mit JSON: "
                '{"is_sock_photo": true|false, "best_match": <Nummer oder null>, '
                '"confidence": "high"|"medium"|"low", "reasoning": "<kurze Begruendung>"}'
            ),
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": photo_b64,
            },
        },
    ]
    for i, (task, ref_url) in enumerate(candidates_with_refs, start=1):
        content.append({"type": "text", "text": f"Referenzbild {i}: {task['name']}"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _download_image_b64(ref_url),
            },
        })

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": content}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    json_text = _strip_markdown_fence(raw_text)

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError:
        return None, None, f"Antwort nicht als JSON lesbar: {raw_text[:200]}", True

    is_sock_photo = result.get("is_sock_photo", True)
    best_match = result.get("best_match")
    confidence = result.get("confidence")
    reasoning = result.get("reasoning", "")
    if not is_sock_photo or not best_match or not (1 <= best_match <= len(candidates_with_refs)):
        return None, confidence, reasoning, is_sock_photo

    matched_task = candidates_with_refs[best_match - 1][0]
    return matched_task, confidence, reasoning, is_sock_photo


def suggest_match(photo_path, rotex_nummer, api_key):
    """Liefert einen Matching-Vorschlag als dict fuer state.set_photo_suggestion(),
    oder None wenn keine Vermutung moeglich ist."""
    candidates = get_candidates()
    if not candidates:
        return None

    number_match = match_by_number(rotex_nummer, candidates)
    if number_match:
        return {
            "task_id": number_match["id"],
            "task_name": number_match["name"],
            "source": "rotex_nummer",
            "confidence": "high",
            "reasoning": f"Rotex-Nummer {rotex_nummer} exakt im Task-Feld gefunden.",
            "is_sock_photo": True,
        }

    vision_match, confidence, reasoning, is_sock_photo = match_by_vision(photo_path, candidates, api_key)
    if vision_match:
        return {
            "task_id": vision_match["id"],
            "task_name": vision_match["name"],
            "source": "vision",
            "confidence": confidence,
            "reasoning": reasoning,
            "is_sock_photo": is_sock_photo,
        }
    return {
        "task_id": None,
        "task_name": None,
        "is_sock_photo": is_sock_photo,
        "source": "vision",
        "confidence": confidence,
        "reasoning": reasoning or "Kein passendes Referenzbild gefunden.",
    }
