"""Kleine JSON-State-Datei: welche Mails/Musterfotos wurden schon verarbeitet."""

import json
import os
import threading

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
_lock = threading.Lock()


def _load():
    if not os.path.exists(STATE_PATH):
        return {"processed_message_ids": [], "batches": {}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, STATE_PATH)


def is_message_processed(message_id):
    with _lock:
        return message_id in _load()["processed_message_ids"]


def mark_message_processed(message_id):
    with _lock:
        data = _load()
        if message_id not in data["processed_message_ids"]:
            data["processed_message_ids"].append(message_id)
        _save(data)


def add_batch(batch_id, info):
    with _lock:
        data = _load()
        data["batches"][batch_id] = info
        _save(data)


def get_batches():
    with _lock:
        return _load()["batches"]


def get_batch(batch_id):
    with _lock:
        return _load()["batches"].get(batch_id)


def _is_batch_done(batch):
    return all(p["status"] != "pending" for p in batch["photos"].values())


def resolve_photo(batch_id, filename, status, **extra):
    """Setzt den Status eines einzelnen Musterfotos ("assigned" oder
    "ignored") und prueft, ob der Batch damit komplett abgearbeitet ist."""
    with _lock:
        data = _load()
        batch = data["batches"].get(batch_id)
        if not batch:
            return
        photo = batch["photos"].get(filename)
        if not photo:
            return
        photo["status"] = status
        photo.update(extra)

        if _is_batch_done(batch):
            batch["status"] = "done"
        _save(data)


def set_photo_suggestion(batch_id, filename, suggestion):
    """Speichert den automatischen Matching-Vorschlag (Task-ID/Name/Quelle/
    Confidence) fuer ein Foto, damit die Review-UI ihn vorauswaehlen kann."""
    with _lock:
        data = _load()
        batch = data["batches"].get(batch_id)
        if not batch:
            return
        photo = batch["photos"].get(filename)
        if not photo:
            return
        photo["suggestion"] = suggestion
        _save(data)
