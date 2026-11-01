"""ЗАДАЧА-11: панель «Экипаж» и строка бюджета в «Луне».

События — словари контракта docs/СОБЫТИЯ.md; терминала и сети нет.
Главное: расход ребёнка не двигает контекст/стоимость главного агента,
а CrewData честно собирает состав, состояния, письма и кошелёк.
"""

from __future__ import annotations

import unittest

from luna.ui.panels import PanelData, create_panel, find_panel, panel_data
from luna.ui.panels.builtin import MoonPanel
from luna.ui.panels.crewdata import CrewData

SPAWN = {"type": "agent.spawn", "agent_id": "a1", "parent_id": "main",
         "kind": "satellite", "name": "Кодер1", "role": "пишет код",
         "model": "fake:echo", "leader": False}


class CrewDataTest(unittest.TestCase):
    def test_roster_states_and_letters(self):
        data = CrewData()
        data.observe(SPAWN)
        data.observe({"type": "agent.state", "agent_id": "a1", "state": "думает"})
        data.observe({"type": "usage", "agent_id": "a1", "input": 1200,
                      "output": 300, "cost": 0.0015})
        data.observe({"type": "agent.message", "agent_id": "a1",
                      "from": "Тимлид", "to": "Кодер1", "text": "Сделай фичу X"})
        data.observe({"type": "budget.update", "spent_usd": 0.0015,
                      "limit_usd": 5.0, "rate_per_min_usd": 0.09,
                      "exhausted": False})
        text = "\n".join(data.lines(60))
        self.assertIn("Кодер1", text)
        self.assertIn("думает", text)
        self.assertIn("1500 ток", text)
        self.assertIn("Сделай фичу X", text)
        self.assertIn("бюджет детей", data.budget_line())
        data.observe({"type": "agent.done", "agent_id": "a1",
                      "summary": "готово", "tokens": 1500, "cost": 0.0015})
        self.assertEqual(data.agents["a1"]["state"], "сдал")

    def test_main_session_start_puts_crew_to_sleep(self):
        data = CrewData()
        data.observe(SPAWN)
        data.observe({"type": "agent.state", "agent_id": "a1", "state": "думает"})
        data.observe({"type": "session.start"})
        self.assertEqual(data.agents["a1"]["state"], "спит")
        # session.start самого ребёнка команду не усыпляет.
        data.observe({"type": "agent.state", "agent_id": "a1", "state": "думает"})
        data.observe({"type": "session.start", "agent_id": "a1"})
        self.assertEqual(data.agents["a1"]["state"], "думает")


class PanelRoutingTest(unittest.TestCase):
    def test_child_usage_does_not_touch_main_stats(self):
        data = PanelData()
        data.observe({"type": "usage", "agent_id": "a1", "model": "fake:echo",
                      "input": 1200, "output": 300, "cost": 0.5,
                      "context_pct": 42})
        self.assertEqual(data.context_pct, 0)
        self.assertEqual(data.cost, 0.0)
        self.assertEqual(data.tokens_in, 0)
        self.assertEqual(data.crew.agents["a1"]["tokens"], 1500)

    def test_panel_registered_and_moon_budget_line(self):
        found = find_panel("экипаж")
        self.assertIsNotNone(found)
        panel = create_panel("crew")
        self.assertEqual(panel.title, "Экипаж")

        class App:
            pass

        app = App()
        data = panel_data(app)
        data.observe(SPAWN)
        data.observe({"type": "budget.update", "spent_usd": 1.25,
                      "limit_usd": 5.0, "rate_per_min_usd": 0.5,
                      "exhausted": False})
        crew_lines = "\n".join(panel.lines(app, 60))
        self.assertIn("Кодер1", crew_lines)
        moon_lines = "\n".join(MoonPanel().lines(app, 40))
        self.assertIn("бюджет детей: $1.2500 из $5.00", moon_lines)


if __name__ == "__main__":
    unittest.main()
