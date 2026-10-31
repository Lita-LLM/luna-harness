"""Страховка Python 3.10: пакет обязан грузиться на минимальном интерпретаторе.

Урок РЕВЬЮ-01 (раунд 2): проверка «грамматикой 3.10» через ast.parse —
неполная страховка: лексические ограничения f-строк (до 3.12 в выражениях
f-строк запрещены обратные слэши и кавычки разделителя, а проект держит
правило строже — никаких строковых литералов внутри выражений f-строк)
ast не проверяет вообще. Поэтому исходники пакета прочёсываются tokenize:
литерал или слэш внутри поля f-строки — регрессия ловится на любом
интерпретаторе, даже без 3.10 под рукой.
"""

from __future__ import annotations

import ast
import io
import token
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "luna"

_FSTRING_START = getattr(token, "FSTRING_START", None)
_FSTRING_END = getattr(token, "FSTRING_END", None)


def _split_literal(literal: str):
    """(префикс, разделитель, тело) строкового литерала одним токеном."""
    idx = min(i for i, ch in enumerate(literal) if ch in "'\"")
    prefix, quote = literal[:idx], literal[idx]
    delim = quote * 3 if literal[idx:idx + 3] == quote * 3 else quote
    body = literal[idx + len(delim):]
    if body.endswith(delim):
        body = body[:-len(delim)]
    return prefix, delim, body


def _replacement_fields(body: str):
    """Тексты полей {...} тела f-строки (двойные скобки — литералы)."""
    i, n = 0, len(body)
    while i < n:
        if body[i] == "{":
            if i + 1 < n and body[i + 1] == "{":
                i += 2
                continue
            depth, j = 1, i + 1
            while j < n and depth:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if not depth:
                        break
                j += 1
            yield body[i + 1:j]
            i = j + 1
        elif body[i] == "}" and i + 1 < n and body[i + 1] == "}":
            i += 2
        else:
            i += 1


def fstring_violations(path: Path) -> list:
    """Поля f-строк файла, где внутри выражения есть литерал или слэш."""
    src = path.read_text(encoding="utf-8")
    bad = []
    if _FSTRING_START is not None:
        # Python 3.12+: f-строка разрезана на токены; строковый литерал
        # внутри поля приходит отдельным токеном STRING при глубине > 0.
        stack: list[int] = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == _FSTRING_START:
                stack.append(0)
            elif tok.type == _FSTRING_END:
                if stack:
                    stack.pop()
            elif not stack:
                continue
            elif tok.type == token.OP and tok.string == "{":
                stack[-1] += 1
            elif tok.type == token.OP and tok.string == "}":
                stack[-1] -= 1
            elif stack[-1] > 0 and tok.type == token.STRING:
                bad.append((tok.start[0], tok.string))
        return bad
    # Python < 3.12: f-строка — один токен STRING; поля разбираем сами.
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != token.STRING:
            continue
        prefix, _delim, body = _split_literal(tok.string)
        if "f" not in prefix.lower():
            continue
        for field in _replacement_fields(body):
            if any(ch in field for ch in ("'", '"', "\\")):
                bad.append((tok.start[0], field))
    return bad


class Python310Test(unittest.TestCase):
    def test_sources_parse_with_310_grammar(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            with self.subTest(path=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                          feature_version=(3, 10))

    def test_no_literals_inside_fstring_expressions(self):
        bad = []
        for path in sorted(PACKAGE.rglob("*.py")):
            for line, frag in fstring_violations(path):
                bad.append(f"{path.relative_to(ROOT)}:{line}: {frag}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
