# Усилие рассуждений у провайдеров (EFFORT)

Исследование к ЗАДАЧЕ-19, сверено с документациями 2026-11-01. Здесь —
как каждый провайдер принимает «глубину мышления» и как Луна переводит
свою единую шкалу в их форматы (`luna/router/effort.py`).

## Шкала Луны

`none < minimal < low < medium < high < xhigh < max`, плюс `auto` —
«не трогать, провайдер решает сам». Настройки: `model.effort` (общая) и
`model.effort_main|fast|guard|vision` (по ролям, пусто — как общая).
Правило перевода: неподдерживаемое значение не уходит в запрос как
есть — берётся ближайшее допустимое по шкале, расхождение возвращается
нотой в «Обратную сторону» (событие `effort.note`). Ошибки 400 из-за
усилия быть не должно.

## OpenAI

- Chat Completions: поле `reasoning_effort`; Responses API: объект
  `reasoning: {"effort": …}`. Значения: `none, minimal, low, medium,
  high, xhigh, max` — но набор зависит от семейства, лишнее значение
  API отклоняет ошибкой 400 со списком допустимых.
- Старые рассуждающие (o1, o3, o4-mini): только `low, medium, high`.
- GPT-5 (первое поколение): `minimal, low, medium, high`.
- GPT-5.2–5.5: `none, low, medium, high, xhigh` (`xhigh` — с 5.2).
- GPT-5.6 и новее: `none, low, medium, high, xhigh, max`; `minimal`
  из шкалы убрано. Реальное отключение мышления (`none` на уровне API)
  есть только у GPT-5.x начиная с 5.6 и GPT-6; у старших моделей `none`
  просто не задаёт бюджет.
- Нерассуждающие модели (GPT-4.x и т.п.) `reasoning_effort` не принимают.

Источник: Azure Learn, «Reasoning models» (официальная документация
Azure OpenAI, повторяет шкалы OpenAI по семействам), проверено
2026-11-01: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning

## Anthropic

- Современный путь: `thinking: {"type": "adaptive"}` + отдельный
  `output_config: {"effort": "low|medium|high|xhigh|max"}` (умолчание —
  high). Effort — поведенческий сигнал, а не жёсткий потолок токенов;
  влияет на весь вывод. Токены мышления тарифицируются как выходные;
  в usage есть `output_tokens_details.thinking_tokens`.
- Старый путь `thinking: {"type": "enabled", "budget_tokens": N}`:
  новым моделям (Fable 5, Opus 4.8, Opus 4.7, Sonnet 5) отдаёт 400;
  на Opus 4.6 и Sonnet 4.6 — deprecated (adaptive там уже работает);
  единственный текущий носитель — Haiku 4.5, плюс старые Opus/Sonnet.
- Fable 5 / Mythos: мышление не выключается вовсе — `none` у Луны для
  них превращается в adaptive + effort=low с нотой.

Источники: официальные страницы Anthropic (platform.claude.com,
«Effort» и «Adaptive thinking») — содержание сверено 2026-11-01 по
пересказам и выдержкам из этих страниц в поиске; ключевые факты (пара
adaptive + output_config.effort, 400 на budget_tokens у новых моделей,
статус Haiku 4.5) подтверждаются несколькими независимыми источниками:
https://claude3p.com/guides/claude-thinking-modes.html,
https://allthings.how/claude-thinking-modes-explained/

## Gemini (Google)

Два разных механизма по поколениям, вместе не посылаются никогда:

- Gemini 3 и новее: `generationConfig.thinkingConfig.thinkingLevel` —
  `MINIMAL, LOW, MEDIUM, HIGH`. У Flash лестница полная (умолчание
  HIGH), у Pro — только `LOW` и `HIGH`. Полностью мышление не
  выключается (у Flash/Flash-Lite нижняя ступень — MINIMAL; для
  мультимодальных цепочек MINIMAL требует thought signatures, без них
  400).
- Gemini 2.5: `generationConfig.thinkingConfig.thinkingBudget` —
  бюджет в токенах: Flash 0–24 576 (0 — выключено), Pro 128–32 768
  (не выключается), Flash-Lite 512–24 576; −1 — динамический бюджет.
  Бюджет — мягкий ориентир, а не потолок. Уровни thinkingLevel
  поколение 2.5 не понимает.
- Токены мышления тарифицируются как выходные.

Источники: официальные страницы Google, проверено 2026-11-01:
https://ai.google.dev/gemini-api/docs/thinking и
https://cloud.google.com/vertex-ai/generative-ai/docs/thinking

## OpenRouter

- Объект `reasoning`: `{"effort": "low|medium|high|…"}` или
  `{"max_tokens": N}`, выключение — `{"enabled": false}`. Это
  рекомендуемый способ: OpenRouter сам транслирует в формат целевой
  модели. Луна шлёт effort, поджатый к `low..max` (minimal → low).

Источник: документация OpenRouter по reasoning tokens:
https://openrouter.ai/docs/guides/best-practices/reasoning-tokens

## DeepSeek

- API принимает `reasoning_effort` со значениями `low, high, max`;
  отображения шкалы (из официального гайда по thinking mode): minimal
  → low, medium → high, xhigh → high, (ultra) → max. По умолчанию
  мышление включено на high.
- Выключение — отдельным полем `{"thinking": {"type": "disabled"}}`.
- Токены рассуждений сидят внутри completion_tokens.

Источник: официальный гайд DeepSeek по thinking mode
(https://api-docs.deepseek.com/guides/thinking_mode), сверено
2026-10-27/11-01 по статье с прямыми цитатами гайда.

## Локальные и OpenAI-совместимые (Ollama, llama.cpp, Qwen)

- OpenAI-совместимые слои Ollama и llama.cpp понимают `reasoning_effort`
  у думающих моделей (gpt-oss, Qwen3, DeepSeek-R1), но у каждой сборки
  свой набор значений и своя реакция на лишнее поле; Qwen3 штатно
  управляется ещё и `enable_thinking` через chat_template_kwargs —
  нестандартным полем, которого в общем OpenAI-формате нет.
- Поэтому Луна локальным провайдерам усилие шлёт только опознанным
  думающим моделям (по имени: qwen3, gpt-oss, deepseek) и поджатым к
  `low|medium|high`, а остальным не шлёт вовсе, с нотой.

## Как переводит Луна (итог)

| Провайдер | Поле | Поджатие |
|---|---|---|
| OpenAI | `reasoning_effort` | лестница семейства (таблица выше); нерассуждающим — не шлём |
| OpenRouter | `reasoning.effort` / `reasoning.enabled=false` | minimal → low |
| DeepSeek | `reasoning_effort` / `thinking.type=disabled` | к low/high/max |
| Anthropic | adaptive + `output_config.effort`; старым — `budget_tokens` | minimal → low; бюджет: 1024…32000, max_tokens поднимается над бюджетом |
| Gemini 3+ | `thinkingConfig.thinkingLevel` | Pro — только low/high; none → минимум с нотой |
| Gemini 2.5 | `thinkingConfig.thinkingBudget` | числа: 0/128…кап семейства |
| Локальные | `reasoning_effort` только думающим моделям | к low/medium/high |
