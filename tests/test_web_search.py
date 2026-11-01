"""web_search: разбор выдачи, SearXNG первым, честная ошибка при пустой выдаче."""
import json
import unittest
from pathlib import Path

from luna.tools.registry import ToolContext
from luna.tools.web import web_search_tool

DDG = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2F&amp;rut=x">'
       'Python <b>3.13</b></a><a class="result__snippet" href="#">Вышел релиз</a>')


class Settings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class WebSearchTest(unittest.TestCase):
    def run_search(self, fetcher, settings=None):
        ctx = ToolContext(cwd=Path("."), settings=settings, web_fetcher=fetcher)
        return web_search_tool().run(ctx, {"query": "python"})

    def test_ddg_redirect_unwrapped(self):
        res = self.run_search(lambda url: DDG)
        self.assertTrue(res.ok)
        self.assertIn("https://python.org/", res.text)
        self.assertIn("Python 3.13", res.text)
        self.assertIn("Вышел релиз", res.text)

    def test_searxng_first(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return json.dumps({"results": [{"title": "Свой", "url": "https://a.example", "content": "x"}]})

        res = self.run_search(fetch, Settings(**{"web.searxng_url": "http://searx.local"}))
        self.assertTrue(res.ok)
        self.assertTrue(seen[0].startswith("http://searx.local/search?format=json"))
        self.assertEqual(len(seen), 1)

    def test_empty_is_honest_error(self):
        res = self.run_search(lambda url: "<html>заглушка</html>")
        self.assertFalse(res.ok)
        self.assertIn("SearXNG", res.text)


if __name__ == "__main__":
    unittest.main()
