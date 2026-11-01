"""Состав команды данными (ЗАДАЧА-11): файлы crews/<имя>.json.

Формат «luna-crew/1»: состав — просто данные (имя, роль, промпт роли,
модель, режим, worktree, can_spawn) плюс лидер. Лежат в LUNA_HOME/crews/
(общие команды) и в .luna/crews/ проекта (проектная перекрывает домашнюю
при загрузке — как слои настроек и манеры).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.paths import luna_home

FORMAT = "luna-crew/1"


def crew_payload(members: list[dict], leader: str = "") -> dict:
    return {"format": FORMAT, "leader": leader or "", "members": members}


def _dirs(cwd: Path) -> list[Path]:
    """Каталоги поиска: сначала проект, потом дом."""
    return [Path(cwd) / ".luna" / "crews", luna_home() / "crews"]


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("плохой имя команды: " + repr(name))
    return name


def save_crew_file(name: str, data: dict, where: str, cwd: Path) -> Path:
    name = _safe_name(name)
    folder = (Path(cwd) / ".luna" / "crews") if where == "project" else (luna_home() / "crews")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_crew_file(name: str, cwd: Path) -> dict:
    name = _safe_name(name)
    for folder in _dirs(cwd):
        path = folder / (name + ".json")
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("members"), list):
                raise ValueError("битый файл команды: " + str(path))
            return data
    raise ValueError("команда «" + name + "» не найдена ни в проекте, ни в LUNA_HOME/crews")


def list_crew_files(cwd: Path) -> list[str]:
    names: list[str] = []
    for folder in reversed(_dirs(cwd)):
        if folder.exists():
            for path in sorted(folder.glob("*.json")):
                if path.stem not in names:
                    names.append(path.stem)
    return names
