"""Память раскладки панелей (ЗАДАЧА-20): LUNA_HOME/layouts.json.

Что хранится: снимок дерева панелей (какие открыты, где стоят, веса,
ширина колонки) и ширина терминала на момент сохранения — чтобы на
узком экране ужать пропорционально, не ломая сохранённое. Две записи:
общая по умолчанию и отдельно на проект (по cwd). Сохраняет экран
только по ручным действиям человека (открыл/закрыл/потянул), не на
каждом кадре; сброс — /panels reset.
"""

from __future__ import annotations

import json
from pathlib import Path


def _path(data_dir) -> Path:
    return Path(data_dir.root) / "layouts.json"


def load_store(data_dir) -> dict:
    try:
        data = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_store(data_dir, store: dict) -> None:
    try:
        path = _path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    except OSError:
        pass  # память раскладки — удобство, а не данные: молча живём без неё


def get_layout(data_dir, cwd: str):
    """Раскладка проекта; нет проектной — общая по умолчанию; нет — None."""
    store = load_store(data_dir)
    projects = store.get("projects") or {}
    entry = projects.get(str(cwd)) or store.get("default")
    return entry if isinstance(entry, dict) else None


def put_layout(data_dir, cwd: str, snapshot: dict, cols: int) -> None:
    """Запомнить раскладку как проектную и как общую по умолчанию."""
    store = load_store(data_dir)
    entry = {"snapshot": snapshot, "cols": int(cols)}
    projects = store.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    projects[str(cwd)] = entry
    store["projects"] = projects
    store["default"] = entry
    save_store(data_dir, store)


def clear_layout(data_dir, cwd: str) -> None:
    """/panels reset: проектная и общая раскладки забыты."""
    store = load_store(data_dir)
    projects = store.get("projects")
    if isinstance(projects, dict):
        projects.pop(str(cwd), None)
    store.pop("default", None)
    save_store(data_dir, store)
