"""Почта агентов (ЗАДАЧА-11, docs/РОЙ.md §3): общая для детей и будущего роя.

Письмо — событие agent.message (from/to/text) плюс доставка во входящие
получателя; письмо будит спящего спутника. Кто кому может писать — правило
у родителя (РОЙ.md §3):

- хозяин (главный агент) пишет кому угодно, ему пишут все;
- ассистент пишет только своему родителю и отвечает только ему;
- спутник пишет всем по именам: хозяину, другим спутникам и своим детям;
- чужим детям (ассистентам другого хозяина) писать нельзя.

Рой (ЗАДАЧА-15) позже будет слать письма по адресам через эту же почту:
адреса вида «поток/каста/звено» лягут в те же поля from/to.
"""

from __future__ import annotations

MAIN_ID = "main"
MAIN_NAME = "Луна"


class MailError(Exception):
    """Письмо нельзя доставить: нет адресата или правило запрещает."""


def endpoint(agent_id: str, kind: str, parent_id: str, name: str) -> dict:
    return {"id": agent_id, "kind": kind, "parent_id": parent_id, "name": name}


def main_endpoint() -> dict:
    return endpoint(MAIN_ID, "main", "", MAIN_NAME)


def check_mail(sender: dict, recipient: dict) -> str | None:
    """None — можно; строка — почему нельзя (правило родителя, РОЙ.md §3)."""
    if sender["id"] == recipient["id"]:
        return "нельзя писать самому себе"
    if sender["kind"] == "main" or recipient["kind"] == "main":
        return None  # хозяин пишет всем, хозяину пишут все
    if sender["kind"] == "assistant":
        if recipient["id"] == sender["parent_id"]:
            return None  # ассистент отвечает только своему родителю
        return "ассистент пишет только своему хозяину"
    if sender["kind"] == "satellite":
        if recipient["kind"] == "satellite":
            return None  # спутники пишут друг другу по именам
        if recipient["parent_id"] == sender["id"]:
            return None  # спутник пишет своим детям
        return "нельзя писать чужим детям"
    # ячейки роя и прочие будущие виды: только родителю (дальше — ЗАДАЧА-15)
    if recipient["id"] == sender["parent_id"]:
        return None
    return "этому виду агентов можно писать только родителю"
