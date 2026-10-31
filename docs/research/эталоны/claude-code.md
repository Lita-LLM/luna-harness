# Claude Code (Anthropic) — исследование эталона для luna-harness (Задача 02)

> Дата среза: **2026-10-31**. Все факты ниже — по состоянию на 2026-10-31.
> Источники — только первоисточники, где это возможно: официальная документация `code.claude.com/docs` (ранее `docs.anthropic.com` / `docs.claude.com`), репозиторий https://github.com/anthropics/claude-code (README, CHANGELOG, issues, releases), npm-пакет `@anthropic-ai/claude-code`.
> Где факт подтверждён только вторичным источником или не подтверждён по первоисточнику — это честно помечено **«не подтверждено»**.
> Локально Claude Code в этой среде **не установлен**, собственных замеров нет (см. раздел 9).
> Актуальная ветка версий на дату среза: **2.1.x**; в публичных записях фигурируют 2.1.314 (пакет FreeBSD-порта, 2026-10-31) и анонс v2.1.316 от 2026-10-31 (по вторичной фиксации release notes на GitHub) — точная «последняя» версия на момент написания по первоисточнику однозначно **не подтверждена**.

---

## 1. Тулы — встроенные инструменты

Полный официальный перечень — страница **Tools reference** (данные на 2026-10-31): https://code.claude.com/docs/en/tools-reference
Имена тулов — точные строки, используемые в permission rules, списках тулов субагентов и matcher'ах хуков. Категории ниже сгруппированы автором отчёта; состав и назначения — дословно по первоисточнику.

### Файлы и редактирование
| Тул | Назначение | Разрешение |
|---|---|---|
| `Read` | Чтение файлов (текст, изображения PNG/JPG, PDF, Jupyter `.ipynb`; вывод с номерами строк) | Не требуется |
| `Write` | Создание / полная перезапись файла | Требуется |
| `Edit` | Точечная замена точной строки в файле (файл должен быть ранее прочитан) | Требуется |
| `NotebookEdit` | Редактирование ячеек Jupyter notebook | Требуется |
| `Glob` | Поиск файлов по glob-паттерну. По докам: **по умолчанию отсутствует на macOS, Linux и WSL** | Не требуется |
| `Grep` | Поиск по содержимому (regex, поверх ripgrep). По умолчанию отсутствует на macOS, Linux и WSL (см. выше) | Не требуется |

### Выполнение
| Тул | Назначение | Разрешение |
|---|---|---|
| `Bash` | Выполнение shell-команд; рабочая директория сохраняется между вызовами, состояние shell — нет; поддержка фоновых задач | Требуется |
| `PowerShell` | Нативное выполнение PowerShell-команд (Windows) | Требуется |
| `Monitor` | Фоновый процесс: каждая строка вывода подаётся Клоду как событие (логи, вотчеры, поллинг); также WebSocket-события | Требуется |

### Веб
| Тул | Назначение | Разрешение |
|---|---|---|
| `WebFetch` | Загрузка URL, конверсия HTML→Markdown, обработка быстрой вторичной моделью, кэш | Требуется |
| `WebSearch` | Веб-поиск | Требуется |

### Агенты, задачи, планирование
| Тул | Назначение | Разрешение |
|---|---|---|
| `Agent` | Спавн субагента с собственным контекстным окном; при включённых Agent Teams вызов с `name` запускает teammate; также fork-режим | Не требуется |
| `Skill` | Выполнение скилла в основном разговоре | Требуется |
| `Workflow` | Динамический воркфлоу: скрипт, оркестрирующий много субагентов в фоне, возвращает один сводный результат | Требуется |
| `EnterPlanMode` / `ExitPlanMode` | Вход в plan mode / предъявление плана на одобрение и выход | Нет / Требуется |
| `EnterWorktree` / `ExitWorktree` | Создание изолированного git worktree и переход в него / выход | Требуется / Нет |
| `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate` | CRUD списка задач сессии (по умолчанию — только на части моделей, на остальных — opt-in) | Не требуется |
| `TaskStop` | Остановка фоновой задачи / субагента / teammate по ID или имени | Не требуется |
| `TaskOutput` | **Удалён в v2.1.277**; раньше — вывод фоновой задачи, теперь вывод читается через `Read` файла вывода | — |
| `TodoWrite` | Чек-лист задач сессии; по умолчанию отключён в пользу Task-тулов, включается обратно `CLAUDE_CODE_ENABLE_TASKS=0` | Не требуется |
| `CronCreate`, `CronDelete`, `CronList` | Сессионные запланированные прогоны (recurring/one-shot), восстанавливаются при `--resume`/`--continue`, если не истекли | Не требуется |
| `ScheduleWakeup` | Перепланирование следующей итерации self-paced `/loop` (от 1 мин до 1 ч) | Не требуется |
| `RemoteTrigger` | Облачные scheduled tasks (бывш. routines) на claude.ai; бэкит `/schedule`. Недоступен на Bedrock/Vertex/Foundry и т.п. | Не требуется |

### Взаимодействие и прочее
| Тул | Назначение | Разрешение |
|---|---|---|
| `AskUserQuestion` | Вопросы пользователю с вариантами выбора (уточнение требований) | Не требуется |
| `SendMessage` | Сообщение другому агенту: teammate, субагенту (resume по ID/имени), другой локальной/облачной сессии Claude Code (кросс-сессионный обмен — с v2.1.224) | Не требуется |
| `ListAgents` | Список агентов, доступных для `SendMessage` (субагенты, teammates, другие сессии); бэкит `/list-agents`, с v2.1.224 | Не требуется |
| `ToolSearch` | Поиск и подгрузка отложенных (deferred) тулов при включённом tool search | Не требуется |
| `WaitForMcpServers` | Ожидание MCP-серверов, ещё подключающихся в фоне (только при отключённом tool search) | Не требуется |
| `ListMcpResourcesTool` / `ReadMcpResourceTool` | Список / чтение ресурсов подключённых MCP-серверов | Не требуется |
| `LSP` | Кодовая разведка через language server: определения, ссылки, типы, диагностики | Не требуется |
| `Artifact` | Публикация HTML/Markdown на claude.ai как интерактивного артефакта (нужны платный план и `/login`) | Требуется |
| `PushNotification` | Десктопное уведомление + push на телефон при подключённом Remote Control | Не требуется |
| `SendUserFile` | Отправка файлов пользователю (render/attach) в Remote Control / cloud-сессиях | Не требуется |
| `SendFeedback` | Черновик фидбэка о Claude Code в очередь на ревью пользователем (с v2.1.238); сам ничего не отправляет | Не требуется |
| `ShareOnboardingGuide` | Загрузка `ONBOARDING.md` и шар-ссылка для команды (`/team-onboarding`, Team/Enterprise) | Требуется |
| `ReportFindings` | Структурированный список code-review находок для рендера в UI | Не требуется |
| `SubagentHandback` | Доставка финального отчёта субагента принимающему разговору (только auto mode, с v2.1.271) | Не требуется |
| `EndConversation` | Завершение сессии (редкие случаи / по прямой просьбе продемонстрировать) | Не требуется |

Дополнительно (Agent SDK, та же таблица тулов): категории — File operations (`Read/Edit/Write`), Search (`Glob/Grep`), Execution (`Bash`), Web (`WebSearch/WebFetch`), Discovery (`ToolSearch`), Orchestration (`Agent`, `Skill`, `AskUserQuestion`, `TaskCreate`, `TaskUpdate`). Только читающие тулы могут выполняться конкурентно; изменяющие состояние (`Edit`, `Write`, `Bash`) — последовательно. Источник: https://code.claude.com/docs/en/agent-sdk/agent-loop (данные на 2026-10-31).

Собственные тулы добавляются только через MCP-серверы; скилл нового тула не создаёт — он работает через существующий тул `Skill`. Источник: https://code.claude.com/docs/en/tools-reference (данные на 2026-10-31).


---

## 2. Режимы и разрешения

### Permission modes
Источник: https://code.claude.com/docs/en/permission-modes (данные на 2026-10-31).

| Режим | Что выполняется без запроса | Для чего |
|---|---|---|
| `default` (в UI — Manual) | Только чтение | Чувствительная работа, ревью каждого действия |
| `acceptEdits` | Чтение, правки файлов, обычные файловые команды (`mkdir`, `touch`, `mv`, `cp` и т.п.) | Итерация по коду под присмотром |
| `plan` | Только чтение; при доступном auto — ещё команды, одобренные классификатором | Исследование кодовой базы до изменений |
| `auto` | Всё, с фоновыми проверками безопасности (отдельная модель-классификатор ревьюит действия) | Длинные задачи, снижение «усталости от промптов» |
| `dontAsk` | Только заранее одобренные тулы; остальное, что потребовало бы промпта, — отклоняется | Залоченный CI и скрипты |
| `bypassPermissions` | Всё | Только изолированные контейнеры и VM |

Ключевые факты (тот же источник, данные на 2026-10-31):
- С **v2.1.283** `auto` — встроенный стартовый режим для интерактивных терминальных и VS Code сессий (ранее — только на Pro/Max/Ultra/Team).
- Переключение в сессии — `Shift+Tab`, цикл `default → acceptEdits → plan` (+ включённые опциональные режимы); `dontAsk` в цикл не входит никогда, ставится только `--permission-mode dontAsk`.
- При запуске: `claude --permission-mode plan`; дефолт — `permissions.defaultMode` в settings. Флаг `--dangerously-skip-permissions` = `bypassPermissions`; запрещён под root/sudo вне распознанной песочницы.
- Во всех режимах, кроме `bypassPermissions`, записи в защищённые пути (`.git`, `.claude` и др.) никогда не авто-одобряются. Но **deny-правила и явные ask-правила действуют во всех режимах, включая `bypassPermissions`**; allow-правила в нём бессмысленны.
- Plan mode — не песочница: в интерактивных сессиях, где доступен bypass, блокировки plan mode могут не форситься (фикс/нота в релизе 2.1.299 — по вторичной фиксации changelog, помечено как частично подтверждённое).

### Правила allow / deny / ask
Источник: https://code.claude.com/docs/en/permissions и страница Configure permissions https://code.claude.com/docs/en/agent-sdk/permissions (данные на 2026-10-31).
- Правила живут в `settings.json`: `"permissions": { "allow": [...], "ask": [...], "deny": [...] }`.
- Формат: `Tool` или `Tool(specifier)`, напр. `Bash(npm run test *)`, `Read(./.env)`, `Edit(/src/**)`, `WebFetch(domain:example.com)`, `Agent(Explore)`, `Skill(deploy *)`.
- Порядок оценки: **deny → ask → allow** (по SDK-докам: сначала deny из настроек/`disallowedTools`, затем проверка режима; хук-`allow` не перебивает совпадающий deny). Первое совпадение решает.
- Управление в сессии — `/permissions`; из CLI — `--allowedTools` / `--disallowedTools`; в SDK — `allowedTools`/`disallowedTools` и колбэк `canUseTool`.

### Песочница Bash (sandbox)
Источник: https://code.claude.com/docs/en/sandboxing и https://code.claude.com/docs/en/sandbox-environments (данные на 2026-10-31).
- Отдельный от разрешений слой: границу применяет **ОС** на время выполнения команды. По умолчанию **выключена**; включение — `/sandbox` в сессии или `sandbox.enabled: true` в settings.
- Примитивы: macOS — **Seatbelt**, Linux и WSL2 — **bubblewrap** (+ `socat`); реализация — открытый пакет `@anthropic-ai/sandbox-runtime`. На нативном Windows песочницы нет (команды идут без неё; рекомендация — WSL2).
- Ограничивает файловую систему (чтение/запись по спискам, напр. `sandbox.filesystem.allowWrite`, `denyRead`) и сеть (`sandbox.network.allowedDomains`, `allowLocalBinding`); `sandbox.autoAllowBashIfSandboxed` — авто-одобрение команд внутри песочницы; `allowUnsandboxedCommands: false` убирает фолбэк «повторить вне песочницы».
- Охват — **только shell-команды** (Bash, PowerShell, Monitor и их дочерние процессы). Файловые тулы, MCP-серверы и хуки выполняются **вне** песочницы. Чтобы закрыть всё одним периметром — весь процесс Claude Code запускают внутри sandbox-runtime (`~/.srt-settings.json`), dev container или VM (та же документация).
- С v2.1.216 есть `sandbox.filesystem.disabled` — отключить файловую изоляцию, оставив контроль сетевого egress (по фиксации изменений доков; первоисточник — страница sandboxing, данные на 2026-10-31).

### Файлы настроек (иерархия)
Источники: https://code.claude.com/docs/en/claude-directory , https://code.claude.com/docs/en/memory (данные на 2026-10-31).
- Managed policy: `/etc/claude-code/managed-settings.json` (Linux/WSL), `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), аналог на Windows — не переопределяется пользовательскими настройками.
- User: `~/.claude/settings.json`
- Project: `.claude/settings.json` (коммитится, командный)
- Local: `.claude/settings.local.json` (только проект, личные оверрайды, gitignored при записи самим Claude Code)

---

## 3. Субагенты, команды, параллельность

### Субагенты (Task tool → Agent tool)
Источники: https://code.claude.com/docs/en/tools-reference , https://code.claude.com/docs/en/agents , страница `.claude` directory (данные на 2026-10-31).
- Вызов — тул **`Agent`** (историческое имя механизма — Task tool; в актуальных доках инструмент называется `Agent`). Субагент работает в **отдельном контекстном окне**, родитель видит только финальный результат, не промежуточные вызовы.
- Определение — Markdown с YAML-frontmatter: `.claude/agents/*.md` (проект) и `~/.claude/agents/*.md` (пользователь); сканирование рекурсивное. Обязательные поля — только `name` и `description`; именно по `description` главный агент решает делегировать.
- Поля frontmatter субагента (по странице `.claude` directory, данные на 2026-10-31): `name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `autoCompactWindow`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `effort`, `isolation`, `color`, `initialPrompt`, `omitClaudeMd`, `experimental`.
- Встроенные типы (по докам/вторичной сверке с доками): `Explore` (быстрый read-only поиск по кодовой базе), `Plan` (исследование в plan mode), `general-purpose`, `claude`, `statusline-setup`, `claude-code-guide`. Explore и Plan не грузят CLAUDE.md и git-статус снапшот. Полный состав встроенных по первоисточнику в одном списке **не подтверждён** — в доках явно названы Explore и Plan.
- Параллельность: субагенты из главного разговора по умолчанию стартуют в фоне; дефолтный кап конкурентных субагентов — 20, настраивается `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` (по вторичному источнику, сверенному с доками; по первоисточнику в рамках этого исследования **не подтверждено**). Вложенность субагентов — до 3 уровней, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` (аналогично — **не подтверждено** по первоисточнику).
- Видимость друг друга: субагенты **не видят друг друга** напрямую — каждый отчитывается только вызвавшему. Исключения: `SendMessage` может возобновить субагента по ID/имени и слать сообщения между сессиями; в Agent Teams тиммейты пишут друг другу напрямую.
- Своя память субагентов: `agent-memory/<name>/` и поле `memory` — персистентная память конкретного субагента (страница `.claude` directory).
- Запуск всей сессии как агента: `claude --agent <name>`; дефолт проекта — ключ `agent` в `.claude/settings.json`. Команда `/agents` в актуальных версиях лишь печатает напоминание, где лежат файлы (интерактивный визард был до v2.1.197) — по вторичной сверке с доками.

### Пять способов параллельной работы
Источник: https://code.claude.com/docs/en/agents (данные на 2026-10-31).
1. **Subagents** — делегаты внутри одной сессии, возвращают сводку.
2. **Agent view** — экран диспетчеризации и мониторинга фоновых сессий, `claude agents`. Research preview.
3. **Agent Teams** — несколько скоординированных сессий: общий список задач, меж-агентные сообщения, лид распределяет работу. **Экспериментально, по умолчанию выключено** (включается `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). Тиммейты **не** изолируются worktree'ями автоматически — работу надо делить по наборам файлов.
4. **Dynamic workflows** — скрипт (`workflows/*.js`), запускающий много субагентов с перекрёстной проверкой результатов; управление — `/workflows`.
5. **Projects** — облачные параллельные треды на claude.ai/code и в desktop app (public beta, Pro/Max/Ultra).

### Git worktree
Источник: https://code.claude.com/docs/en/worktrees (данные на 2026-10-31).
- `claude --worktree <name>` (коротко `-w`): создаёт `.claude/worktrees/<name>/` в корне репозитория на новой ветке `worktree-<name>`; без имени имя генерируется. База — по умолчанию default-ветка; `worktree.baseRef: "head"` — от текущего HEAD.
- `.worktreeinclude` (синтаксис .gitignore) — какие gitignored файлы копировать в новый worktree (напр. `.env`).
- Тулы `EnterWorktree`/`ExitWorktree` делают то же изнутри сессии; у субагента изоляция задаётся полем `isolation: worktree`.
- При выходе: чистый безымянный worktree удаляется автоматически; именной/грязный — запрос «оставить/удалить», для возврата печатается `claude --worktree <name> --resume`. Периодическая уборка (sweep) удаляет worktree субагентов и фоновых сессий старше `cleanupPeriodDays`.

### Фоновые задачи
Источники: https://code.claude.com/docs/en/agents , https://code.claude.com/docs/en/headless (данные на 2026-10-31).
- Фоновые bash-команды: команда не блокирует разговор; это **не** агент. Управление всем фоновым в текущей сессии — `/tasks` (алиас `/bashes`): список, просмотр, attach, остановка; туда же попадают завершённые субагенты.
- Фоновые сессии: `claude --bg "prompt"` / `/background` (алиас `/bg`) — сессия отсоединяется и работает без терминала; мониторинг — `claude agents`.
- Форк сессии: `/fork` — копия всего разговора в новую фоновую сессию; `/subtask` — forked-субагент, наследующий полный контекст родителя (при выключенном agent view форк-субагент вызывается через `/fork`).
- В `claude -p`: после конца хода процесс может ждать фоновую работу (команды главной сессии, субагенты, воркфлоу, Monitor-вотчи, `/loop`-wakeups); дефолтный потолок ожидания простоя — 10 минут (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`).


---

## 4. Расширения

### 4.1 Хуки
Источники: https://code.claude.com/docs/en/agent-sdk/hooks (полный список событий SDK — открыт полностью, данные на 2026-10-31); CLI-хуки конфигурируются в `settings.json` в ключе `hooks` (тип `command` — shell-команда, получает JSON на stdin; exit 0 — ок/JSON-решение на stdout, exit 2 — блокирующая ошибка, stderr уходит модели).
Полный список событий (33):

`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `UserPromptSubmit`, `UserPromptExpansion`, `MessageDisplay`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`, `PreModelSwitch`, `PostModelSwitch`, `PermissionRequest`, `PermissionDenied`, `SessionStart`, `SessionEnd`, `Notification`, `Setup`, `TeammateIdle`, `TaskCreated`, `TaskCompleted`, `Elicitation`, `ElicitationResult`, `ConfigChange`, `InstructionsLoaded`, `WorktreeCreate`, `WorktreeRemove`, `CwdChanged`, `FileChanged`, `DirectoryAdded`.

Ключевые свойства (тот же источник, данные на 2026-10-31):
- `PreToolUse` — единственный массовый «гейт»: может заблокировать/изменить вызов; решения `permissionDecision: allow|deny|ask|defer` (приоритет при конфликтах: deny > defer > ask > allow), `updatedInput` правит аргументы; `defer` работает только в `-p`.
- Matcher фильтрует по `tool_name` для туловых событий (`Bash`, `Edit`, `Write`, `Read`, `Glob`, `Grep`, `Agent`, `WebFetch`, `WebSearch`, MCP — как `mcp__<server>__<tool>`), по `source` для `SessionStart` и т.д.; поле `if` — сужение синтаксисом permission rules (только туловые события).
- Все хуки события запускаются параллельно; самый строгий результат побеждает; дефолтный таймаут — 600 с (**по вторичной сверке с hooks reference; в открытой SDK-странице таймаут по умолчанию назван унаследованным от command-хуков**).
- В Python SDK доступна только часть событий; полный набор — в TypeScript SDK / CLI (см. таблицу на странице источника).
- Хуки также можно объявлять в frontmatter скилла/субагента (поле `hooks`) и внутри плагинов (`hooks/hooks.json`).

### 4.2 Скиллы (Agent Skills)
Источник: https://code.claude.com/docs/en/skills (данные на 2026-10-31).
- Скилл — папка с обязательным `SKILL.md` (YAML-frontmatter + инструкции) и опциональными файлами (`scripts/`, `references/`, `assets/` и др.). Соответствует открытому стандарту **Agent Skills**; Claude Code расширяет его контролем вызова, выполнением в субагенте и динамической вставкой контекста.
- Пути: личные — `~/.claude/skills/<name>/SKILL.md`; проектные — `.claude/skills/<name>/SKILL.md`; также из `--add-dir` каталогов и из плагинов (неймспейс `plugin-name:skill-name`).
- Прогрессивное раскрытие: в контекст всегда грузится только метадата (name+description, ~100 токенов); тело — при срабатывании; ресурсы — по необходимости. Рекомендация доков — тело < 500 строк.
- Вызов: моделью автоматически по `description` или пользователем как `/<name>`. Поля frontmatter (по странице `.claude` directory): `name`, `description`, `when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`, `model`, `effort`, `context`, `agent`, `background`, `hooks`, `paths`, `shell`, `metadata`, `license`, `compatibility`. `context: fork` + `agent` — выполнение в субагенте (Explore/Plan/general-purpose).
- **Кастомные команды слиты со скиллами**: `.claude/commands/deploy.md` и `.claude/skills/deploy/SKILL.md` одинаково создают `/deploy`; старые файлы команд продолжают работать.
- Встроенные (bundled) скиллы: `/code-review`, `/batch`, `/debug`, `/loop`, `/claude-api`, `/doctor`, `/verify`, `/simplify`, `/workflow-authoring` (часть — только при включённых фичах); отключаются `disableBundledSkills`.
- После компакции вызванные скиллы переподключаются усечённо: первые 5 000 токенов каждого, суммарный бюджет 25 000 (по вторичной сверке с доками; в первоисточнике в рамках исследования страница целиком не открывалась — пометка: частично подтверждено).

### 4.3 Слэш-команды
Источник: https://code.claude.com/docs/en/commands (данные на 2026-10-31). Встроенные команды исполняют фиксированную логику CLI; bundled-скиллы помечены в справочнике как Skill. Доступность части команд зависит от плана, платформы и провайдера; суммарно справочник насчитывает 100+ записей с алиасами.
Основные: `/help`, `/clear` (алиасы `/reset`, `/new`), `/compact [инструкции]`, `/context`, `/resume` (алиас `/continue`), `/rewind` (алиасы `/checkpoint`, `/undo`), `/branch`, `/fork`, `/subtask`, `/model`, `/effort`, `/fast`, `/plan`, `/config`, `/permissions`, `/sandbox`, `/mcp`, `/plugin`, `/agents`, `/list-agents`, `/tasks`, `/background` (`/bg`), `/workflows`, `/memory`, `/init`, `/doctor`, `/feedback`, `/usage` (алиас `/cost`; `/stats` открывает её вкладки), `/status`, `/statusline`, `/theme`, `/color`, `/tui [default|fullscreen]`, `/focus`, `/scroll-speed`, `/terminal-setup`, `/vim`, `/voice`, `/review`, `/ultrareview`, `/goal`, `/loop`, `/batch`, `/schedule`, `/cd`, `/add-dir`, `/export`, `/copy`, `/diff`, `/recap`, `/insights`, `/team-onboarding`, `/login`, `/logout`, `/exit`.
Свои команды: `.claude/commands/*.md` (проект) и `~/.claude/commands/*.md` (пользователь) — однофайловые промпты, тот же механизм, что у скиллов (frontmatter — поля скилла, кроме `name` и `paths`).

### 4.4 Плагины и маркетплейсы
Источники: https://code.claude.com/docs/en/plugins , https://code.claude.com/docs/en/plugins/create , https://code.claude.com/docs/en/plugins/manifest-reference , https://code.claude.com/docs/en/plugins/publish (данные на 2026-10-31).
- Плагин — каталог компонентов, устанавливаемый и загружаемый как единица: `skills/`, `agents/`, `commands/`, `hooks/hooks.json`, `.mcp.json`, LSP-конфиги (`.lsp.json`), мониторы и др. Манифест (опционален) — `.claude-plugin/plugin.json` (имя, версия, описание, `dependencies`, `userConfig`, `defaultEnabled` и др.); без манифеста компоненты ищутся в стандартной раскладке, имя — из записи маркетплейса или каталога.
- Маркетплейс — каталог (JSON): репозиторий/папка с `.claude-plugin/marketplace.json`. Официальный `claude-plugins-official` добавляется сам при первом интерактивном запуске; комьюнити-маркетплейс сам не добавляется. Установка: `/plugin` (UI с вкладками Discover/Marketplaces/Errors) или `claude plugin marketplace add <source>` + `claude plugin install <name>@<marketplace>`; источники — GitHub `owner/repo`, git URL, локальный путь, URL hosted `marketplace.json`.
- Скоупы установки: user / project (через коммитимый `.claude/settings.json`, `enabledPlugins`) / local. Установленное лежит в `~/.claude/plugins` (клоны маркетплейсов, версии, `installed_plugins.json`); синк с claude.ai — `~/.claude/plugins/synced/`.
- Разработка без маркетплейса: `--plugin-dir <path>` (папка плагинов — с v2.1.265), `--plugin-url`.
- Известная дыра: `extraKnownMarketplaces` + `enabledPlugins` из проектных настроек **не** срабатывают в headless/CI, т.к. автоустановка привязана к интерактивному trust-диалогу (GitHub gist-расследование, вторичный источник; по первоисточнику **не подтверждено**).

### 4.5 MCP
Источник: https://code.claude.com/docs/en/mcp и https://code.claude.com/docs/en/mcp-quickstart (данные на 2026-10-31).
- Транспорты: **stdio** (локальный подпроцесс), **HTTP** (Streamable HTTP, рекомендован для удалённых), **SSE** (легаси, помечен deprecated в пользу HTTP); в конфигах также встречается `ws`. Добавление: `claude mcp add [--transport http|sse|stdio] [--scope ...] <name> <url|-- command...>`, заголовки `--header`, env `--env`; управление и OAuth — `/mcp` внутри сессии.
- Аутентификация: OAuth 2.0 для удалённых серверов (флоу через `/mcp`), кастомные заголовки, в `.mcp.json` — блок `oauth` (`clientId`, `callbackPort`, `authServerMetadataUrl`, `scopes`). Имена тулов: `mcp__<server>__<tool>`. Поддерживаются MCP resources (`ListMcpResourcesTool`/`ReadMcpResourceTool`), prompts, elicitation (хуки `Elicitation`/`ElicitationResult`), tasks; `alwaysLoad` — сервер, чьи тулы не уходят в deferred tool search.
- Скоупы (приоритет при коллизии имён: local > project > user > плагины > claude.ai connectors; managed — выше всех; записи не мерджатся, побеждает целиком):
  - `local` (дефолт): только вы, только проект — `~/.claude.json` под ключом проекта;
  - `project`: команда, через git — `.mcp.json` в корне проекта (требует одобрения при первом использовании в интерактиве; в `-p`/SDK грузится без промпта);
  - `user`: все проекты — `~/.claude.json`, верхний `mcpServers`.
- Переменные окружения в конфигах — `${VAR}` и `${VAR:-default}`.

### 4.6 LSP
Источники: https://code.claude.com/docs/en/tools-reference (тул `LSP`), официальные плагины маркетплейса на claude.com (данные на 2026-10-31).
- Встроенный тул `LSP`: go-to-definition, find references, hover/типы, document/workspace symbols, call hierarchy, диагностики. Диагностики после каждой правки/записи файла пушатся в контекст автоматически, без вызова тула.
- Включение: установить бинарь language server + code intelligence плагин языка из официального маркетплейса (`typescript-lsp@claude-plugins-official`, `pyright-lsp`, `gopls-lsp`, `rust-analyzer-lsp`, `clangd-lsp`, `csharp-lsp` и др.; конфиг плагина — `.lsp.json`, есть `initializationOptions`). Ранее требовался недокументированный `ENABLE_LSP_TOOL` — более не нужен (по вторичной сверке с доками, v2.1.296).

### 4.7 Headless и SDK
Источник: https://code.claude.com/docs/en/headless (данные на 2026-10-31).
- Non-interactive (бывш. headless): `claude -p "<prompt>"` (`--print`). Форматы вывода: `--output-format text|json|stream-json`, `--json-schema` для структурированного вывода, `--include-partial-messages`, `--verbose`. Exit code 0/не-0; SIGTERM → 143.
- `--bare`: быстрый старт без автообнаружения хуков, скиллов, команд, субагентов, плагинов, MCP, auto memory и CLAUDE.md; доступны Bash/Read/Edit; контекст подаётся флагами (`--append-system-prompt[-file]`, `--settings`, `--mcp-config`, `--agents`, `--plugin-dir/--plugin-url`); для Anthropic API нужен `ANTHROPIC_API_KEY` (OAuth не читается).
- **Claude Agent SDK** (бывш. Claude Code SDK): пакеты TypeScript (`@anthropic-ai/claude-agent-sdk`) и Python; тот же агентный цикл, тулы и управление контекстом; SDK бандлит нативный бинарь Claude Code. Колбэки одобрения тулов (`canUseTool`), хуки, structured outputs, стриминг, `rewind_files()` и т.п.
- Прочее: `claude -p` отклоняет `--bg`; сессии `-p` сохраняются и резимятся, если не дан `--no-session-persistence`.

### 4.8 ACP (Agent Client Protocol)
По первоисточнику (документация и репозиторий Anthropic) нативной поддержки ACP в самом `claude` CLI **не подтверждено**. Фактически ACP для Claude Code живёт **внешним адаптером**: `@agentclientprotocol/claude-agent-acp` (бывш. `@zed-industries/claude-code-acp`) — отдельный npm-пакет организации Agent Client Protocol / команды Zed, оборачивающий Claude Agent SDK; именно его Zed ставит как «Claude Code» external agent. То есть: ACP — **частично/через адаптер, не встроено**. Источники: PyPI/репозитории адаптеров и доки интеграций (вторичные относительно Anthropic; утверждение «в CLI не встроено» — по отсутствию ACP в официальной документации Claude Code на 2026-10-31).


---

## 5. Контекст и сессии

Источники: https://code.claude.com/docs/en/agent-sdk/agent-loop , https://code.claude.com/docs/en/checkpointing , https://code.claude.com/docs/en/commands , https://code.claude.com/docs/en/memory (данные на 2026-10-31).

### Автосжатие / compact
- При приближении к лимиту контекста Claude Code **автоматически компактит**: старые сообщения заменяются модельной сводкой, свежие обмены и ключевые решения сохраняются; сессия не умирает от переполнения. В SDK это событие `compact_boundary`.
- Вручную: `/compact [фокус-инструкции]`. Порог настраивается: `/autocompact` показывает окно и его источник; `--autocompact 500k`; настройка `autoCompactWindow` (по моделям — `modelSettings.<model>.autoCompactWindow`); env `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (с v2.1.304 корректно понимает `500k`/`1M`); `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` может только снизить порог. Отключение: `autoCompactEnabled: false` или `DISABLE_AUTO_COMPACT=1`; `DISABLE_COMPACT=1` отключает и ручной `/compact`.
- Ориентиры порога: модели с нативным 1M компактятся около ~967K; конфигурации на 200K (напр. Sonnet 4.6 / Opus 4.6 без extended context) — у границы 200K. Есть idle-компакция после ~54 минут тишины (по вторичным фиксациям доков — частично подтверждено).
- Что переживает компакцию: системный промпт; проектный CLAUDE.md, unscoped rules и auto memory **перечитываются с диска**; до 5 недавно трогавшихся файлов перечитываются; вызванные скиллы переподключаются усечённо (см. 4.2). Ранние устные инструкции могут теряться — устойчивые правила док-но велят держать в CLAUDE.md.
- Хуки: `PreCompact` (поле `trigger: manual|auto`; exit 2 блокирует компакцию) и `PostCompact`.

### Сессии: resume / continue / fork
- Транскрипты: `~/.claude/projects/<project>/<session-id>.jsonl` (сообщения, вызовы тулов, системный промпт и параметры запросов — plain text на диске).
- `claude --continue` / `-c` — самая свежая сессия текущего каталога; `claude --resume [id|имя]` / `-r` — пикер или конкретная сессия; внутри сессии — `/resume`. Имя сессии при старте: `-n <name>`.
- Форк: `claude --continue --fork-session` — копия разговора с новым session ID; внутри сессии — `/branch [name]` (копия разговора, оригинал не тронут) и `/fork` (копия в фоновую сессию).
- `/clear` — новый пустой контекст в том же процессе; прежний разговор остаётся доступен через `/resume` и через спец-запись в `/rewind`.

### Чекпоинты и /rewind
Источник: https://code.claude.com/docs/en/checkpointing (данные на 2026-10-31).
- Чекпоинт создаётся автоматически **перед каждым промптом, начинающим ход**. Хранятся снапшоты файлов для **100 последних чекпоинтов** сессии; снапшоты живут с разговором (rewind работает после resume); уборка — retention sweep, по умолчанию ~30 дней (`cleanupPeriodDays`).
- Открыть: `/rewind` или двойной `Esc` при пустом вводе. Действия по выбранной точке: **Restore code and conversation**, **Restore conversation**, **Restore code**, **Summarize from here**, **Summarize up to here**.
- Ограничения (прямо из доков): трекаются только правки файловыми тулами (`Write`, `Edit`, `NotebookEdit`) главной сессии. **Не** откатываются: изменения через Bash (`rm`, `mv`, `cp`, `sed -i`, редиректы), правки субагентов, ручные/внешние правки, создание/перемещение/удаление директорий; restore пропускает symlink/hardlink пути (с v2.1.216 — с отчётом о числе пропущенных). «Чекпоинты — не замена VCS» (формулировка самих доков).
- В SDK чекпоинтинг файлов включается опцией (`enableFileCheckpointing`), точки — UUID пользовательских сообщений, откат — `rewindFiles()`/`rewind_files()` (https://code.claude.com/docs/en/agent-sdk/file-checkpointing).

### Память
Источник: https://code.claude.com/docs/en/memory (данные на 2026-10-31).
- **CLAUDE.md** — инструкции пользователя/команды, грузятся в начале каждой сессии целиком (рекомендация — < 200 строк на файл). Иерархия и порядок загрузки (от широкого к конкретному): managed policy (`/etc/claude-code/CLAUDE.md` на Linux) → user (`~/.claude/CLAUDE.md`) → project (`./CLAUDE.md` или `./.claude/CLAUDE.md`) → local (`./CLAUDE.local.md`). Файлы вверх по дереву грузятся на старте; из подкаталогов — лениво, при первом обращении к файлам в них. Импорты — синтаксис `@path`, рекурсивно до 4 уровней. HTML-комментарии вырезаются из контекста. Также читается `AGENTS.md` вместо/вместе с CLAUDE.md; `/init` генерирует стартовый CLAUDE.md.
- **`.claude/rules/*.md`** — модульные правила; с frontmatter `paths` грузятся только при работе с подходящими файлами.
- **Auto memory** — заметки, которые Клод пишет сам (команды сборки, инсайты отладки, стиль, привычки). Требуется v2.1.59+; по умолчанию включена. Хранилище: `~/.claude/projects/<project>/memory/` — `MEMORY.md` (индекс) + тематические файлы; в сессию грузится первые **200 строк или 25 KB** индекса; общий каталог на репозиторий, шарится между worktree. Управление: `/memory`, `autoMemoryEnabled`, `autoMemoryDirectory` (не принимается из проектных настроек), env `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Субагенты могут иметь собственную auto memory.
- CLAUDE.md и auto memory — контекст, а не принуждение: жёсткие запреты велят делать хуком `PreToolUse` или deny-правилом.

---

## 6. Интерфейс

Источники: https://code.claude.com/docs/en/fullscreen , https://code.claude.com/docs/en/terminal-config , https://code.claude.com/docs/en/commands (данные на 2026-10-31).

### Рендереры: classic vs fullscreen
- **Classic renderer**: вывод идёт в нативный scrollback терминала; известен хроническим мерцанием (см. раздел 10).
- **Fullscreen rendering** (research preview, с v2.1.89): альтернативный путь отрисовки — **alternate screen buffer** (как vim/htop), рендерятся только видимые сообщения; устраняет мерцание, держит память плоской на длинных разговорах, добавляет **поддержку мыши** (клики, выделение, скролл колесом, `/scroll-speed`). Включение: `/tui fullscreen` (сохраняет настройку `tui` и перезапускает сессию без потери контекста), `/tui default` — обратно, `/tui` — показать активный; env `CLAUDE_CODE_NO_FLICKER=1` (с v2.1.110 — через `/tui`; переменная старше). Мышь можно отключить `CLAUDE_CODE_DISABLE_MOUSE=1`.
- Для впервые пришедших с 2026-05-06 fullscreen — дефолт; ранние пользователи остаются на classic, пока не включат (по фиксации изменений доков от 2026-08-03 — частично подтверждено по первоисточнику).
- Плата за alt-screen: не работает нативный Cmd+F/выделение терминала — поиск и копирование делаются средствами Claude Code (Ctrl+O + `/` и т.п.). В screen reader режиме всегда classic (кроме attached background sessions). Дополнительно: `CLAUDE_CODE_FORCE_SYNC_OUTPUT=1` — synchronized output (DEC 2026) без смены рендерера для терминалов, которые его умеют, но не детектятся.
- **Проблемы рендера из первоисточников**: команда Anthropic публично писала о переписывании рендера с уменьшением мерцания «примерно на 85%» (дек. 2025, откат, повторный роллаут 21.01.2026; пайплайн — cell diff, бюджет кадра ~16 мс). В CHANGELOG/release notes регулярно встречаются фиксы: моргание пустым экраном на простое, утечка mouse-tracking escape-последовательностей в шелл после выхода, дублирование scrollback по Ctrl+L вне fullscreen (tmux/GNOME Terminal/Windows Terminal/Konsole), квадратичное замедление на длинных сессиях (фикс в 2.1.216). Полный список жалоб — раздел 10.

### Картинки, темы, прочее
- Картинки: вставка из буфера/прикрепление файлов; `Read` показывает изображения модели; в fullscreen изображения рендерятся в терминале на поддерживаемых терминалах (**детали протокола рендера картинок по первоисточнику не подтверждены**). Прикреплённые изображения кэшируются (исторически `~/.claude/image-cache/`, с поздних версий — temp-каталог сессии).
- Темы: `/theme`, кастомные темы — JSON в `~/.claude/themes/*.json`; `/color` — цвет prompt-бара сессии. Отдельные цвета fullscreen/classic задаются в terminal-config (ключи вида `userMessageBackground`, `selectionBg`, `composerSidebarBackground` и т.п.).
- Режим фокуса: `/focus` (только fullscreen) — последний промпт, однострочная сводка тулов и финальный ответ. Vim-режим ввода — `/vim` / настройка в `/config`. Голосовой ввод — `/voice`.
- **Фразы спиннера**: пока модель работает, спиннер показывает случайный глагол из встроенного списка (по вторичным данным — 204 глагола: «Clauding», «Discombobulating», «Flibbertigibbeting» и пр.; точное число по первоисточнику **не подтверждено**). Кастомизация — настройка `spinnerVerbs` в settings (`mode: replace|append|...` и список `verbs`) — документирована в settings reference (в рамках исследования подтверждена многочисленными вторичными источниками, ссылающимися на доки).
- **Статусная строка (statusLine API)**: настройка `"statusLine": {"type": "command", "command": "..."}` в settings.json. Claude Code на каждый рендер/обновление запускает команду и подаёт ей JSON на stdin; stdout команды становится строкой статуса. Поля JSON (по докам statusline и примерам): `session_id`, `cwd`/`workspace.current_dir`, `model.id`, `model.display_name`, `context_window.used_percentage`, `context_window.total_input_tokens`/`total_output_tokens`/`context_window_size`, `cost.total_cost_usd`, `cost.total_duration_ms`, `rate_limits.five_hour.used_percentage`/`resets_at`, `rate_limits.seven_day.*` (только Pro/Max после первого API-ответа), `effort.level`, `output_style`. Настройка из описания словами: `/statusline покажи модель и процент контекста`. Есть также `subagentStatusLine` с per-task данными (включая `effort`, с v2.1.214).

### Компаньон /buddy
По первоисточнику (официальные доки/CHANGELOG Anthropic) `/buddy` в рамках исследования **не подтверждён** в актуальной документации; сведения — из вторичных источников и анализа утёкшего исходника, помечаю как **не подтверждено по первоисточнику**:
- ASCII-питомец-компаньон рядом с полем ввода: 18 видов (duck, goose, blob, cat, dragon, octopus, owl, penguin, turtle, snail, ghost, axolotl, capybara, cactus, robot, rabbit, mushroom, chonk), редкости, шляпы и глаза, реплики в speech bubble, статы; вид детерминированно хэшируется из ID аккаунта.
- Запущен как первоапрельская фича в **v2.1.89 (2026-04-01)**, обнаружен днём раньше в случайно опубликованном source map npm-пакета 2.1.88 (2026-03-31).
- По вторичным данным, **удалён в v2.1.97 (2026-04-09)**; в актуальных версиях команды нет. На дату среза считать: **был, сейчас отсутствует (не подтверждено по первоисточнику)**.
- Не путать с отдельным открытым проектом Anthropic `claude-desktop-buddy` (BLE-фирмварь для физического деск-пета к Claude Desktop).

---

## 7. Модели

Источники: страница Model configuration (origin: https://code.claude.com/docs/en/model-config ; читалась через зеркало доков и вторичные сверки), https://code.claude.com/docs/en/amazon-bedrock (данные на 2026-10-31).
**Осторожно:** линейка моделей в 2026 году менялась несколько раз; ниже — срез, согласованный несколькими источниками на конец октября 2026; точную привязку алиасов по первоисточнику в открытой странице проверить не удалось (страница model-config не открылась из-за лимита fetch) — помечаю ключевые привязки как частично подтверждённые.

### Алиасы и дефолты
| Алиас | Смысл |
|---|---|
| `default` | Сброс к дефолту аккаунта/организации |
| `best` | Самая способная доступная модель (Fable, где доступен, иначе Opus) |
| `fable` | Последняя Fable (на срезе: Claude Fable 5.1, `claude-fable-5-1`, контекст 1M) |
| `opus` | Последний Opus (на срезе: Claude Opus 5.5, `claude-opus-5-5`, 1M) |
| `sonnet` | Последний Sonnet (на срезе: Claude Sonnet 5.5, `claude-sonnet-5-5`, 1M) |
| `haiku` | Последний Haiku (Claude Haiku 4.5, `claude-haiku-4-5`, 200K) |
| `opusplan` | Opus в plan mode → Sonnet на исполнении (спецрежим, не модель) |
| `opus[1m]`, `sonnet[1m]` | Та же модель с контекстом 1M токенов |

- Дефолт: **Opus 5.5** на Pro/Max/Team/Enterprise, Anthropic API, Bedrock, Google Cloud Agent Platform и Claude Platform on AWS; исключение — **Microsoft Foundry: Sonnet 4.5**. Fable дефолтом не бывает нигде, выбирается явно и может биллиться в usage credits.
- Переключение: `/model` (пикер; Enter — сохранить дефолтом, `s` — только на сессию), `/model <alias|полное имя>`, `claude --model <...>`, настройка `model` в settings, env `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_MODEL` (дефолт новых сессий). Приоритет: сессионный выбор > флаг запуска > env > settings.
- Пиннинг алиасов: `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL` (на Bedrock — inference profile ARN, на Vertex — version name, на Foundry — deployment name).

### Роли моделей
- **Основная** — выбранная модель сессии.
- **Субагенты**: поле `model` в определении; глобальный оверрайд — `CLAUDE_CODE_SUBAGENT_MODEL`; усилие субагентов — `CLAUDE_CODE_SUBAGENT_EFFORT` (добавлено в 2.1.316 по release notes, вторично).
- **Фоновые/служебные задачи** (тайтлы сессий, сводки и т.п.): модель алиаса `haiku` (`ANTHROPIC_DEFAULT_HAIKU_MODEL`); на Bedrock для фона используется дефолтный Sonnet (Haiku может быть не включён в аккаунте) — прямо из страницы Bedrock (данные на 2026-10-31).
- **Классификатор auto mode** — отдельная модель, ревьюящая действия в фоне (permission-modes).
- Усилие (effort): `/effort` — `low|medium|high|xhigh|max` (+ служебные значения); у Opus 5.5 дефолт `medium`, у прочих в основном `high`. `/fast` — ускоренный режим Opus за отдельную плату (детали тарифов — вне первоисточников этого исследования, **не подтверждено**).

### Зрение и провайдеры
- Зрение: модели принимают изображения (Read картинок, вставки, скриншоты); все актуальные модели линейки мультимодальны — детали по первоисточнику в рамках исследования **не подтверждены** постранично.
- Сторонние провайдеры (все — те же модели Claude, не чужие модели): **Amazon Bedrock** (`CLAUDE_CODE_USE_BEDROCK=1`, есть мастер `/setup-bedrock`), **Google Vertex AI / Google Cloud Agent Platform** (`CLAUDE_CODE_USE_VERTEX=1`), **Microsoft Foundry** (`CLAUDE_CODE_USE_FOUNDRY=1`), **Claude Platform on AWS**, а также **LLM-шлюзы** через `ANTHROPIC_BASE_URL` + ключ (любой Anthropic-совместимый endpoint; `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` наполняет `/model` из `/v1/models` шлюза). Не-Anthropic модели официально не поддерживаются; локальные модели гоняют только через шлюз-транслятор на свой риск.


---

## 8. Совместимость — точные форматы и пути

Источник: https://code.claude.com/docs/en/claude-directory (открыта полностью; данные на 2026-10-31). На Windows `~/.claude` = `%USERPROFILE%\.claude`, `~/.claude.json` = `%USERPROFILE%\.claude.json`. При заданном `CLAUDE_CONFIG_DIR` все `~/.claude`-пути и `.claude.json` живут под ним.

### Инструкции и память
| Путь | Скоуп | Коммитить | Назначение |
|---|---|---|---|
| `/etc/claude-code/CLAUDE.md` (Linux), `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS), `C:\Program Files\ClaudeCode\CLAUDE.md` (Windows) | Managed policy | — | Корпоративные инструкции |
| `~/.claude/CLAUDE.md` | User | Нет | Личные инструкции на все проекты |
| `./CLAUDE.md` или `./.claude/CLAUDE.md` | Project | Да | Проектные инструкции |
| `./CLAUDE.local.md` | Local | Нет (.gitignore) | Личные проектные предпочтения |
| `AGENTS.md` (корень, `.claude/`, любой каталог) | Project | Да | Читается вместо/вместе с CLAUDE.md |
| `.claude/rules/*.md`, `~/.claude/rules/*.md` | Project/User | Да | Тематические правила, frontmatter только `paths` |

### Настройки
| Путь | Скоуп | Назначение |
|---|---|---|
| `/etc/claude-code/managed-settings.json` (Linux), `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS) | Managed | Принудительные настройки организации |
| `~/.claude/settings.json` | User | Разрешения, хуки, env, дефолты модели |
| `.claude/settings.json` | Project | То же, командные (коммитится) |
| `.claude/settings.local.json` | Project-local | Личные оверрайды, не коммитится |
| `~/.claude.json` | Global | Состояние приложения: аккаунт, UI-тоглы, личные MCP-серверы (user- и local-скоупы), история. Бэкапы — `~/.claude/backups/` (5 новейших) |
| `~/.claude/keybindings.json` | Global | Кастомные сочетания клавиш |
| `~/.claude/themes/*.json` | Global | Кастомные цветовые темы |

Содержимое `settings.json` (ключи, встречающиеся в доках): `permissions` (`allow`/`ask`/`deny`/`defaultMode`), `hooks`, `env`, `model`, `effortLevel`, `sandbox`, `statusLine`, `subagentStatusLine`, `spinnerVerbs`, `enabledPlugins`, `extraKnownMarketplaces`, `autoMemoryEnabled`, `autoMemoryDirectory`, `autoCompactWindow` / `modelSettings`, `cleanupPeriodDays`, `disableBundledSkills`, `skillOverrides`, `claudeMdExcludes`, `tui`, `viewMode`, `agent` и др. Схема для редакторов: `https://json.schemastore.org/claude-code-settings.json` (по вторичному источнику).

### Расширения (проект `.claude/` и пользователь `~/.claude/`, если не указано иное)
| Путь | Назначение | Frontmatter |
|---|---|---|
| `.claude/skills/<name>/SKILL.md` (+ файлы рядом) | Скиллы | `name`, `description`, `when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`, `model`, `effort`, `context`, `agent`, `background`, `hooks`, `paths`, `shell`, `metadata`, `license`, `compatibility` |
| `.claude/commands/*.md` | Свои слэш-команды (механизм скиллов) | Поля скилла, кроме `name` и `paths` |
| `.claude/agents/*.md` | Субагенты | `name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `autoCompactWindow`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `effort`, `isolation`, `color`, `initialPrompt`, `omitClaudeMd`, `experimental` |
| `.claude/workflows/*.js` | Динамические воркфлоу; каждый файл — команда `/<name>` | — |
| `.claude/output-styles/*.md` | Стили вывода | `name`, `description`, `keep-coding-instructions`, `force-for-plugin` |
| `.claude/agent-memory/<name>/` | Персистентная память субагентов | — |
| Хуки | Конфиг — ключ `hooks` в `settings.json` (не отдельный файл); у плагинов — `hooks/hooks.json` | — |

### MCP, worktree, плагины
| Путь | Назначение |
|---|---|
| `.mcp.json` (корень проекта) | Project-scope MCP-серверы, ключ `mcpServers` (stdio: `command`/`args`/`env`; удалённые: `type: http|sse|ws`, `url`, `headers`, опц. `oauth`, `timeout`, `alwaysLoad`) |
| `~/.claude.json` → `mcpServers` / `projects.<path>.mcpServers` | User-scope / local-scope MCP-серверы |
| `.worktreeinclude` (корень проекта) | Gitignored файлы, копируемые в новые worktree |
| `.claude/worktrees/<name>/` | Сами worktree-сессии (ветка `worktree-<name>`) |
| Плагин: `.claude-plugin/plugin.json`, компоненты `skills/`, `agents/`, `commands/`, `hooks/hooks.json`, `.mcp.json`, `.lsp.json` | Раскладка плагина |
| Маркетплейс: `.claude-plugin/marketplace.json` | Каталог плагинов |
| `~/.claude/plugins/` | Установленные плагины, `installed_plugins.json`, клоны маркетплейсов, `known_marketplaces.json` |

### Данные, которые пишет сам Claude Code (под `~/.claude/`)
`projects/<project>/<session>.jsonl` — транскрипты; `projects/<project>/<session>/subagents/` — транскрипты субагентов; `projects/<project>/memory/` — auto memory; `file-history/<session>/` — снапшоты чекпоинтов (100 последних); `plans/` — файлы планов plan mode; `tasks/` — списки задач; `shell-snapshots/` — снапшоты шелла для Bash-тула; `debug/`, `paste-cache/`, `session-env/`, `usage-data/` и др. Ретеншн по умолчанию — 30 дней (`cleanupPeriodDays`).

---

## 9. Вес и старт

**Честно: локально Claude Code не установлен, своих замеров (размер, время старта, RSS) в этом исследовании нет.** Ниже — только документированное и опубликованное.

- Системные требования (доки setup, по вторичным фиксациям официальной страницы): macOS 13+, Windows 10 1809+/Server 2019+, Ubuntu 20.04+, Debian 10+, Alpine 3.19+; **4 GB RAM минимум**; Node.js 22+ требуется только npm-пути (с v2.1.198; на старее npm лишь предупреждает).
- Установка сейчас: **нативный инсталлер — рекомендуемый** (`curl -fsSL https://claude.ai/install.sh | bash`; Windows — `irm https://claude.ai/install.ps1 | iex`), также Homebrew cask, WinGet, apt/dnf/apk. **npm-метод официально deprecated** в пользу нативного; npm-пакет `@anthropic-ai/claude-code` остаётся каналом распространения/наследием. Источники: README https://github.com/anthropics/claude-code и доки setup (данные на 2026-10-31).
- Размер бинаря: официальный источник размер не публикует (**не подтверждено** по первоисточнику). Вне первоисточника: distinfo FreeBSD-порта для `claude-code-linux-x64-2.1.314.tgz` — **137 217 467 байт (~137 MB)** (FreshPorts, запись от 2026-10-31) — это нативный бинарь со встроенным рантаймом. Исторические npm-тарболы (вторичные анализы): ~18.7 MB упаковано / ~60.5 MB распаковано для обычной сборки и аномальные 59.8 MB у 2.1.88 из-за случайно включённого source map; оценки install size ~47 MB. Все цифры размера — **не из Anthropic**.
- Время запуска: официальной цифры нет (**не подтверждено**). Косвенно из release notes: чинились регрессии старта ~3 с (ожидание фетча MCP-конфига claude.ai) и зависание UI на 1–8 с при включённом голосовом вводе (v2.1.83 notes) — то есть старт в плохих случаях измерялся секундами; типовой старт по первоисточнику не нормирован.
- Память: официальной нормы нет. Из документации по траблшутингу (пересказ вторичным источником официальной performance-страницы): при heap **> 2.5 GB** показывается critical memory usage warning, лечение — рестарт с `claude --continue`; fullscreen-рендерер заявлен как держащий память плоской на длинных сессиях. В changelog задокументированы фиксы утечек: неограничный рост RSS (multi-GB) на многих картинках, `/usage` утекал до ~2 GB на больших историях транскриптов, утечка tool use ID в remote-сессиях. Итог: аппетит к памяти — документированная боль (см. раздел 10), точных официальных цифр нет.

---

## 10. Что бесит — жалобы из GitHub issues

Репозиторий: https://github.com/anthropics/claude-code/issues (данные на 2026-10-31). Номера и заголовки ниже сверены по поиску; реакции/статусы, отмеченные как вторичные, по первоисточнику постранично не открывались (лимит fetch) — помечено.

### Рендер и производительность
- **#769** «In-progress Call causes Screen Flickering» (открыт 2025-04-12) — фирменное мерцание при стриминге тулов; на дату среза, по вторичным данным, всё ещё открыт, ~168–321 реакция в разных подсчётах.
- **#1913** «Terminal Flickering» (с июня 2025) — дубликат-кластер той же проблемы, сотни реакций (вторично).
- **#3648** — неконтролируемая прокрутка терминала, ~282 реакции (вторично).
- **#9935** — инструментальный замер: 4 000–6 700 scroll-событий в секунду из-за полного перерендера на каждый чанк (вторично).
- **#37283** (2026-03-22) — мерцание в tmux 3.4 split panes под WSL2/Windows Terminal на 2.1.81: перерисовки без synchronized output (DEC 2026), курсор прыгает на каждый апдейт.
- Квадратичное замедление длинных сессий — исправлено в 2.1.216 (фиксация changelog/доков); зависания UI на старте и при больших диффах — серия фиксов в release notes (напр. v2.1.83).

### Лимиты и учёт использования
- **#97074** (открыт 2026-09-25, открыт на дату среза) — headless `claude -p` / Agent SDK списывают 5-часовое окно примерно в **1.8× быстрее** интерактивного режима при тех же токенах; 120+ реакций, 60+ комментариев с замерами от 1.3× до 3×; ответа Anthropic в треде не зафиксировано (по вторичному разбору треда).
- **#102618** «[BUG] Usage draining» (2026-10-16, открыт) — 5-часовой лимит дважды заполнялся до 100% **без использования** аккаунта, недельный счётчик рос параллельно (Pro, Windows, 2.1.299); есть подтверждающие комментарии с тем же симптомом.
- **#101937** (2026-10-14) — недельная квота на Opus 5.5 выгорает за 1–2 дня при неизменном стиле работы (2.1.299, macOS).
- Волна однотипных отчётов середины октября 2026 о «сгорании» квот (включая #102667 о возможной ошибке учёта) — без публичного объяснения Anthropic на дату среза (вторично).

### Память и стабильность
- Утечки памяти как класс: multi-GB RSS на сессиях с картинками, ~2 GB на `/usage` при больших историях, утечки в remote-сессиях — по changelog это повторяющиеся фиксы, а не разовый инцидент.
- **#42817** — авто-компакт срабатывает даже при выключенной настройке (длинный тред; воспроизводимость по первоисточнику **не подтверждена**).
- Краши/зависания на resume больших сессий и битых строках транскрипта — фиксы в changelog (skip corrupted line, улучшение памяти `--resume`).

### Настройка и поведение
- Тихие отказы конфигурации: опечатка в permission rule не диагностируется — правило просто не применяется (вторично, частая жалоба).
- `extraKnownMarketplaces` не работает в headless/CI (см. 4.4) — команды не могут раскатить плагины в пайплайнах штатным путём.
- LSP: первый ответ language server в каждой сессии бывает неверным — известный баг, для TypeScript есть воркэраунд конфигом (вторичная сверка с доками).
- Source map инцидент: npm-релиз **2.1.88 (2026-03-31)** случайно включал полный исходник (~512–513 тыс. строк TypeScript) — Anthropic подтвердила «ошибку упаковки, не брешь»; жалоба сообщества — отсутствие size-гейтов при публикации (факт инцидента подтверждён заявлением Anthropic во вторичных источниках; по первоисточнику в рамках исследования **не подтверждён**).

### Слабые места источников этого раздела
Issues постранично открывались выборочно (#102618, #101937 — по GitHub напрямую); часть номеров (#769, #1913, #3648, #9935, #37283, #97074) сверена по нескольким вторичным разборам, цитирующим треды, а не по полному чтению самих тредов. Счётчики реакций быстро устаревают.

---

## Итоговая таблица возможностей

| Возможность | Статус | Примечание |
|---|---|---|
| Файловые тулы Read/Write/Edit/NotebookEdit | есть | Edit требует предварительного чтения файла |
| Поиск Glob/Grep | частично | На macOS/Linux/WSL по умолчанию отсутствуют как отдельные тулы |
| Bash / PowerShell | есть | Bash — с фоновыми задачами; PowerShell — Windows |
| Веб WebFetch/WebSearch | есть | WebFetch обрабатывается вторичной быстрой моделью |
| Permission modes (6) | есть | default, acceptEdits, plan, auto, dontAsk, bypassPermissions; auto — дефолт с 2.1.283 |
| Правила allow/ask/deny | есть | `permissions` в settings.json; оценка deny → ask → allow |
| Песочница shell-команд | есть | Seatbelt (macOS) / bubblewrap (Linux, WSL2); нет на нативном Windows; только shell, не файловые тулы/MCP/хуки |
| Субагенты (Agent tool) | есть | `.claude/agents/*.md`, свой контекст, только финальный отчёт родителю |
| Субагенты видят друг друга | частично | Напрямую нет; SendMessage — resume/межсессионно; в Teams — да |
| Фоновые субагенты и bash-задачи | есть | `/tasks`, фоновые сессии `claude --bg`, agent view |
| Agent Teams | частично | Экспериментально, выключено по умолчанию, без авто-изоляции worktree |
| Git worktree из коробки | есть | `--worktree`/`-w`, `.claude/worktrees/`, `.worktreeinclude`, `isolation: worktree` |
| Dynamic workflows | есть | `.claude/workflows/*.js`, оркестрация многих субагентов скриптом |
| Хуки | есть | 33 события, command-хуки в settings.json, PreToolUse может блокировать/менять вызов |
| Скиллы (Agent Skills) | есть | `SKILL.md`, progressive disclosure, открытый стандарт + расширения CC |
| Свои слэш-команды | есть | `.claude/commands/*.md`, механизм слит со скиллами |
| Плагины и маркетплейсы | есть | `.claude-plugin/plugin.json`, `marketplace.json`; в headless автоустановка из проекта не работает |
| MCP | есть | stdio/HTTP/SSE, OAuth, скоупы local/project/user, `.mcp.json` |
| LSP | есть | Через code intelligence плагины + бинарь сервера; диагностики пушатся сами |
| Headless / print mode | есть | `claude -p`, text/json/stream-json, `--json-schema`, `--bare` |
| Agent SDK | есть | TypeScript + Python, тот же цикл и тулы |
| ACP | частично | Не встроено; внешний адаптер `@agentclientprotocol/claude-agent-acp` поверх SDK |
| Автокомпакт контекста | есть | Настраиваемый порог, PreCompact/PostCompact хуки |
| Resume / continue / fork сессии | есть | `--resume`, `--continue`, `--fork-session`, `/branch`, транскрипты JSONL |
| Чекпоинты и /rewind | есть | 100 последних, только правки файловых тулов главной сессии; Bash/субагенты не откатываются |
| CLAUDE.md иерархия | есть | Managed → user → project → local; плюс AGENTS.md и `.claude/rules/` |
| Auto memory | есть | С v2.1.59; `~/.claude/projects/<project>/memory/`, лимит загрузки 200 строк/25 KB |
| Fullscreen (alt-screen) рендер | есть | Research preview, с v2.1.89; `/tui fullscreen`, мышь; дефолт для новых с 2026-05-06 |
| Классический рендер без мерцания | нет | Хроническая жалоба #769/#1913; лечится fullscreen/sync output |
| Картинки (зрение, вставка) | есть | Read изображений, прикрепления; детали терминального рендера картинок не подтверждены |
| Мышь в терминале | частично | Только fullscreen renderer |
| Темы | есть | `/theme`, свои JSON в `~/.claude/themes/` |
| Компаньон /buddy | нет (был) | Первоапрельская фича 2.1.89; по вторичным данным удалён в 2.1.97; по первоисточнику не подтверждён |
| Фразы спиннера кастомные | есть | Настройка `spinnerVerbs` |
| Статусная строка (statusLine API) | есть | Команда + JSON на stdin; поля контекста, стоимости, rate_limits |
| Модели Fable/Opus/Sonnet/Haiku + opusplan | есть | Алиасы пиннятся env; дефолт — Opus 5.5 (Foundry — Sonnet 4.5) |
| Своя модель для субагентов/фона | есть | `CLAUDE_CODE_SUBAGENT_MODEL`; фон — модель haiku-алиаса |
| Сторонние провайдеры | частично | Bedrock / Vertex / Foundry / шлюзы `ANTHROPIC_BASE_URL` — только модели Claude; чужие модели не поддерживаются |
| Локальная установка и self-host CLI | есть | Нативный инсталлер; npm deprecated; размер бинаря официально не публикуется (~137 MB по данным порта) |

---

## Источники (первоисточники — главное)

- Документация Claude Code: https://code.claude.com/docs/en/tools-reference , https://code.claude.com/docs/en/permission-modes , https://code.claude.com/docs/en/permissions , https://code.claude.com/docs/en/sandboxing , https://code.claude.com/docs/en/sandbox-environments , https://code.claude.com/docs/en/agents , https://code.claude.com/docs/en/worktrees , https://code.claude.com/docs/en/skills , https://code.claude.com/docs/en/commands , https://code.claude.com/docs/en/plugins , https://code.claude.com/docs/en/mcp , https://code.claude.com/docs/en/mcp-quickstart , https://code.claude.com/docs/en/headless , https://code.claude.com/docs/en/agent-sdk/hooks , https://code.claude.com/docs/en/agent-sdk/agent-loop , https://code.claude.com/docs/en/agent-sdk/file-checkpointing , https://code.claude.com/docs/en/checkpointing , https://code.claude.com/docs/en/memory , https://code.claude.com/docs/en/claude-directory , https://code.claude.com/docs/en/fullscreen , https://code.claude.com/docs/en/terminal-config , https://code.claude.com/docs/en/glossary , https://code.claude.com/docs/en/amazon-bedrock , https://code.claude.com/docs/en/model-config — все данные на 2026-10-31.
- Репозиторий: https://github.com/anthropics/claude-code (README, CHANGELOG.md, releases, issues #769, #1913, #3648, #9935, #37283, #42817, #97074, #101937, #102618) — данные на 2026-10-31.
- Пакет: npm `@anthropic-ai/claude-code` — данные на 2026-10-31.
