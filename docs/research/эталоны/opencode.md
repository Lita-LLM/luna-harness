# Эталон: OpenCode (opencode.ai, SST → Anomaly)

Исследование для проекта luna-harness (задача 02, «эталоны»). Дата исследования: **2026-10-31**. Источники — только первоисточники: официальная документация opencode.ai/docs, официальный репозиторий и релизы на GitHub, официальные issues. Вторичные источники помечены отдельно и не используются как единственное основание для фактов. Что не удалось подтвердить по первоисточнику — помечено «не подтверждено».

## Важная рамка: две параллельные линии версий

На 2026-10-31 у OpenCode **две живые линии**, и почти все источники в сети их смешивают:

- **OpenCode 1 (V1)** — npm-пакет `opencode-ai`, релизы оформляются как GitHub Releases. Актуальная ветка на момент исследования — **1.19.x**: v1.19.0 (2026-10-11), v1.19.1 (2026-10-15), v1.19.2, **v1.19.3 (2026-10-16)** — именно она установлена у исполнителя локально, v1.19.4 (2026-10-22). Документация V1: https://opencode.ai/docs/ . По комментарию официального бота проекта в issue #55737, разработка переехала на V2, а V1 получает только исправления критических багов: https://github.com/anomalyco/opencode/issues/55737
- **OpenCode 2 (V2)** — npm-пакет `@opencode/cli`, первый релиз 2.0.0 опубликован 2026-09-11 (по данным npm, цитируется вторичными источниками; GitHub Releases для 2.x не оформляются, есть только теги), формальный анонс — 2026-10-12. Документация V2: https://opencode.ai/v2/docs/ , changelog: https://opencode.ai/changelog . Текущий README репозитория уже ведёт на установку V2 (`npm i -g @opencode/cli@latest`): https://github.com/anomalyco/opencode

**Основной предмет этого обзора — V1 (1.19.3)**, как установленная у исполнителя версия; отличия V2 отмечены отдельно там, где они существенны.

Официальный репозиторий: **github.com/anomalyco/opencode** (перенесён из `sst/opencode`; старый адрес редиректит). На 2026-10-31: лицензия MIT, ~223,5 тыс. звёзд, ветка по умолчанию `dev`, монорепозиторий на TypeScript/Bun (`bun.lock`, `packages/`, `tsconfig.json`): https://github.com/anomalyco/opencode . Не путать с архивным Go-проектом `opencode-ai/opencode` — это другой, более ранний проект (предшественник Crush), к нынешнему OpenCode отношения не имеет.

Архитектура (V1 и V2): клиент-сервер. TUI — лишь один из клиентов локального HTTP-сервера; тот же сервер обслуживают десктопное приложение, веб-интерфейс и IDE-плагины: https://opencode.ai/docs/server/

---

## 1. Тулы (встроенные инструменты)

Источник: https://opencode.ai/docs/tools/ (V1, данные на 2026-10-31). Все тулы включены по умолчанию и управляются системой разрешений (раздел 2). Набор расширяется своими тулами и MCP-серверами (раздел 4).

| Тул | Назначение |
|---|---|
| `bash` | Выполнение shell-команд в терминале пользователя |
| `edit` | Точная замена строки в файле (exact string replacement) |
| `write` | Создание/перезапись файлов целиком; управляется разрешением `edit` |
| `read` | Чтение файлов, в т.ч. диапазонами строк |
| `grep` | Поиск по регулярным выражениям; под капотом ripgrep, уважает `.gitignore`; файл `.ignore` может разрешать игнорируемые пути |
| `glob` | Поиск файлов по glob-паттернам (например `**/*.js`), сортировка по времени изменения |
| `lsp` | Семантические операции LSP: goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol, goToImplementation, prepareCallHierarchy, incomingCalls, outgoingCalls. В V1 — экспериментальный, включается `OPENCODE_EXPERIMENTAL_LSP_TOOL` |
| `apply_patch` | Применение патчей (в документации раздел называется «patch»); маркеры `*** Add File:` / `*** Update File:` / `*** Move to:` / `*** Delete File:` |
| `task` | Запуск субагентов (см. раздел 3); в документации описан на страницах permissions/agents, отдельного заголовка на странице Tools нет |
| `skill` | Загрузка скилла по имени (SKILL.md, см. раздел 4) |
| `todowrite` | Ведение списка задач (todo) внутри сессии |
| `webfetch` | Загрузка веб-страницы и извлечение её содержимого |
| `websearch` | Веб-поиск; работает через Exa или Parallel, API-ключ не нужен (подключается к хостящемуся MCP-бэкенду OpenCode) |
| `question` | Вопрос пользователю с вариантами ответа и возможностью своего ответа |

Расхождение в источниках: в таблицах разрешений на странице агентов фигурирует также тул `list` (листинг файлов), но на странице Tools отдельного заголовка `list` нет — по состоянию на 2026-10-31 не подтверждено, является ли `list` отдельным встроенным тулом в 1.19.3 или частью `read`.

Свои тулы: TypeScript/JavaScript-файлы в `.opencode/tools/` (проект) или `~/.config/opencode/tools/` (глобально); имя файла = имя тула; несколько экспортов из одного файла дают имена `<файл>_<экспорт>`; свой тул с именем встроенного **переопределяет** встроенный; определение может вызывать скрипты на любом языке. Контекст тула содержит `directory` и `worktree`: https://opencode.ai/docs/custom-tools/

---

## 2. Режимы и разрешения

### Разрешения (permissions)

Источник: https://opencode.ai/docs/permissions/ (V1, данные на 2026-10-31).

- С версии **v1.1.1** старый булев конфиг `tools` объявлен устаревшим и слит в `permission` (старый формат всё ещё поддерживается).
- Действия: **`allow`** (разрешить), **`ask`** (спросить), **`deny`** (запретить).
- Формы записи: глобальное `"*"` с переопределениями по тулам; объект с паттернами для конкретного тула. При совпадении нескольких правил побеждает **последнее совпавшее**. Вайлдкарды: `*` (ноль и более символов), `?` (один символ); в паттернах путей раскрываются `~` и `$HOME`.
- Ключи разрешений: `read`, `edit` (покрывает edit/write/patch), `glob`, `grep`, `bash` (матчится по разобранной команде), `task` (по типу субагента), `todowrite`, `skill` (по имени скилла), `lsp` (без гранулярности), `question`, `webfetch` (в V1 — только одно действие, без паттернов URL), `websearch`, `external_directory` (тул трогает путь вне рабочей директории), `doom_loop` (см. ниже).
- Дефолты: большинство — `allow`; `doom_loop` и `external_directory` — `ask`. Для `read` документация приводит дефолтный блок, где `*.env` и `*.env.*` — `ask`, а `*.env.example` — `allow` (часть вторичных обзоров пишет про `deny` для .env — расхождение с текущей страницей документации, фиксируем как есть).
- Флаг **`--auto`** (в т.ч. `opencode run --auto`) автоматически одобряет все `ask`, кроме явно запрещённых `deny`; в TUI переключается из палитры команд, в промпте виден приглушённый индикатор `auto`.
- Ответы на запрос разрешения: `once` (один раз), `always` (до конца сессии; тул предлагает паттерн, например `git status*`), `reject`.
- Разрешения агента (раздел 3) накладываются поверх глобальных, приоритет — у агента.

### Агенты build / plan

Источник: https://opencode.ai/docs/agents/ (V1). Встроенных первичных агента два, переключение — клавишей **Tab**:

- **build** — агент по умолчанию, все тулы включены, полный доступ.
- **plan** — ограниченный: запрещает правки/записи/патчи файлов, кроме Markdown-файлов планов в `.opencode/plans/` и `plans/` в директории данных OpenCode (отказ без запроса, а не `ask`); разрешён тул `question`; bash при этом **не** ограничивается самим режимом и следует обычному конфигу разрешений (в README репозитория сформулировано мягче: «asks permission before running bash commands»). При `OPENCODE_EXPERIMENTAL_PLAN_MODE` планы для git-проектов сохраняются в `.opencode/plans/`.

### Песочница

**Встроенной песочницы нет.** Официальная позиция проекта (SECURITY.md в репозитории, цитируется дословно несколькими источниками): «OpenCode does **not** sandbox the agent. The permission system exists as a UX feature… it is not designed to provide security isolation. If you need true isolation, run OpenCode inside a Docker container or VM»: https://github.com/anomalyco/opencode (файл SECURITY.md в корне репозитория). Разрешения — это UX-контроль и осведомлённость пользователя, а не изоляция; серверный режим без `OPENCODE_SERVER_PASSWORD` работает без аутентификации (с предупреждением).

### Защита от зацикливания (doom_loop)

Есть: разрешение `doom_loop` по умолчанию `ask` — срабатывает, когда один и тот же вызов тула повторяется **3 раза с одинаковым входом**: https://opencode.ai/docs/permissions/

---

## 3. Субагенты и параллельность

Источник: https://opencode.ai/docs/agents/ (V1, данные на 2026-10-31).

### Типы агентов

- **Primary agents** — основные, пользователь работает с ними напрямую; переключение циклически клавишей Tab (keybind `switch_agent`).
- **Subagents** — вызываются первичными агентами автоматически (по полю `description`) либо вручную через **@-упоминание** в сообщении, например `@general help me search for this function`. Каждый субагент работает в дочерней сессии; навигация по дочерним сессиям: `session_child_first` (Leader+Down), `session_child_cycle` (Right), `session_child_cycle_reverse` (Left), `session_parent` (Up).

### Встроенные агенты

Первичные: **build** и **plan** (раздел 2).

Субагенты:

- **general** — универсальный: исследования и многошаговые задачи, полный доступ к тулам, кроме `todowrite`; по документации «может выполнять несколько единиц работы параллельно».
- **explore** — быстрый, только чтение: поиск и изучение кода, файлы не изменяет.

Третий субагент **scout** упоминается в материалах по V2 и во вторичных источниках, но на странице агентов V1 не назван — для 1.19.3 его наличие **не подтверждено**.

Системные скрытые агенты (mode `primary`, скрыты, не выбираются пользователем): **compaction** (сжатие контекста), **title** (генерация заголовков сессий), **summary** (суммаризация).

Примечание о качестве источника: страница агентов V1 внутренне противоречива — в одном месте говорит о «двух встроенных субагентах», в другом о «трёх»; поимённо при этом названы только general и explore плюс скрытые системные.

### Свои агенты

- JSON: ключ `agent` в `opencode.json` (в V2 ключ переименован во множественное `agents`).
- Markdown: глобально `~/.config/opencode/agents/`, в проекте `.opencode/agents/`; имя файла = имя агента. (В задании и в части старых зеркал документации встречается единственное число `.opencode/agent/`; актуальная страница документации V1 на 2026-10-31 использует множественное `agents/`.)
- Frontmatter/поля: `description` (обязательно в JSON-конфиге), `mode` (`primary` | `subagent` | `all`, по умолчанию `all`), `model` (формат `provider/model-id`), `temperature` (0–1), `top_p`, `prompt` (в т.ч. `{file:./prompts/...}`), `permission`, `steps` (максимум агентных итераций), `disable`, `hidden` (субагент скрыт из @-автодополнения, но вызывается через Task), `color` (hex или цвет темы). Неизвестные опции пробрасываются провайдеру (например `reasoningEffort: "high"`).
- `permission.task` с glob-паттернами определяет, каких субагентов агент вправе вызывать; `deny` убирает субагента из описания тула Task.
- CLI: `opencode agent create` (интерактивно; с флагами `--path/--description/--mode/--permissions/--model` — неинтерактивно), `opencode agent list`.
- Конфиг: `default_agent` (должен быть первичным; fallback — `build`), **`subagent_depth`** — глубина вложенности субагентов: по умолчанию **1** (субагенты не могут запускать субагентов), 2 — один дополнительный уровень, 0 — субагенты запрещены: https://opencode.ai/docs/config/

### Параллельность и фон

- Параллельность: первичный агент может запускать несколько вызовов Task; general описан как выполняющий единицы работы параллельно.
- Фоновые субагенты в V1 — только экспериментально, за переменной окружения `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS`: https://opencode.ai/docs/cli/ . Полноценные фоновые субагенты и фоновые shell-команды — заявленная фича V2 (анонс 2026-10-12, вторичные источники; по первоисточнику V2 детально не проверялось).
- Worktree: в V1 встроенного управления git worktree (создание/переключение из UI) не найдено — **не подтверждено**; контекст тулов знает `worktree` (корень git worktree), сессии могут жить в разных worktree. В V2 переключение worktree одной клавишей заявлено во вторичных источниках (v2.2.5, 2026-10-30) — по первоисточнику не подтверждено.

---

## 4. Расширения

### Плагины

Источник: https://opencode.ai/docs/plugins/ (V1, данные на 2026-10-31).

- Плагины — модули **JavaScript/TypeScript**. Загрузка: локальные директории `.opencode/plugins/` (проект) и `~/.config/opencode/plugins/` (глобально) — в актуальной документации множественное число `plugins/`; npm-пакеты из ключа `plugin: [...]` в конфиге (автоустановка через Bun при старте, кэш в `~/.cache/opencode/node_modules/`). Порядок загрузки: глобальный конфиг → проектный конфиг → глобальная директория плагинов → проектная директория.
- CLI: `opencode plugin <module>` (алиас `opencode plug`), флаги `--global`, `--force`.
- Сигнатура: `export const MyPlugin: Plugin = async ({ project, client, $, directory, worktree }) => ({ ...hooks })`; типы из `@opencode-ai/plugin`; `$` — shell API Bun; плагин может добавлять свои тулы через хелпер `tool()` (схема на Zod); логирование — `client.app.log()`.
- Ключевые хуки: `"tool.execute.before"` / `"tool.execute.after"` (можно мутировать аргументы; брошенное исключение в before блокирует вызов), `"shell.env"`, `"experimental.session.compacting"` (можно добавить `output.context` или заменить `output.prompt`).
- **Полный список событий** с официальной страницы плагинов V1: `command.executed`; `file.edited`; `file.watcher.updated`; `installation.updated`; `lsp.client.diagnostics`; `lsp.updated`; `message.part.removed`; `message.part.updated`; `message.removed`; `message.updated`; `permission.asked`; `permission.replied`; `server.connected`; `session.created`; `session.compacted`; `session.deleted`; `session.diff`; `session.error`; `session.idle`; `session.status`; `session.updated`; `todo.updated`; `shell.env`; `tool.execute.after`; `tool.execute.before`; `tui.prompt.append`; `tui.command.execute`; `tui.toast.show`.
- **V2: API плагинов переписано, плагины V1 в V2 не работают** (официальная страница миграции: https://opencode.ai/v2/docs/build/plugins/migrate-v1/); в V2 ключ конфига — `plugins`, плагин оформляется как `Plugin.define({ id, setup(ctx) })` с default-экспортом.

### Свои команды (custom commands)

Источник: https://opencode.ai/docs/commands/ (V1).

- Markdown-файлы: глобально `~/.config/opencode/commands/`, в проекте `.opencode/commands/` (актуальная документация — множественное число `commands/`; в задании и старых зеркалах встречается `.opencode/command/`). Имя файла = имя команды. Frontmatter: `description`, `agent`, `model`, `subtask`; тело файла — шаблон промпта. Альтернатива — JSON-ключ `command` в конфиге (обязательное поле `template`).
- Подстановки: `$ARGUMENTS`, `$1`, `$2`, `$3`…; вставка вывода shell: `` !`команда` `` (выполняется в корне проекта); `@файл` — вставка содержимого файла в промпт.
- `subtask: true` форсит вызов через субагента; если агент команды — субагент, вызов идёт субагентом по умолчанию. Своя команда с именем встроенной переопределяет встроенную.

### Скиллы (SKILL.md)

Источник: https://opencode.ai/docs/skills/ (V1). **Поддержка SKILL.md в V1 есть** — это нативная фича, а не плагин.

- Скиллы загружаются по требованию через встроенный тул `skill`: агент видит блок `<available_skills>` (имя+описание) в описании тула и вызывает `skill({ name: "..." })`.
- Пути поиска: проект `.opencode/skills/<имя>/SKILL.md`; глобально `~/.config/opencode/skills/<имя>/SKILL.md`; **Claude-совместимые**: проект `.claude/skills/<имя>/SKILL.md`, глобально `~/.claude/skills/<имя>/SKILL.md`; **agent-совместимые**: проект `.agents/skills/<имя>/SKILL.md`, глобально `~/.agents/skills/<имя>/SKILL.md`. Проектные пути ищутся вверх до корня git worktree.
- Frontmatter: `name` (обязательно; 1–64 символа, `^[a-z0-9]+(-[a-z0-9]+)*$`, должно совпадать с именем директории), `description` (обязательно, 1–1024 символа), опционально `license`, `compatibility`, `metadata` (строковая карта; V1 не использует metadata для слэш-команд и автовыбора). Файл обязан называться именно `SKILL.md` (верхний регистр); имена уникальны между локациями.
- Контроль доступа: `permission.skill` по паттернам (`allow` — загрузка сразу, `deny` — скилл скрыт и отклоняется, `ask` — запрос); переопределения per-agent; легаси `tools: { skill: false }` отключает тул (блок `<available_skills>` не показывается).

### MCP

Источник: https://opencode.ai/docs/mcp-servers/ (V1).

- Конфиг: ключ `mcp` в `opencode.json`, каждый сервер — прямой ребёнок ключа (в V2 иначе: `mcp.servers.<имя>`, `disabled` вместо `enabled`).
- Локальные серверы: `"type": "local"`, `command` (массив, обязательно), `cwd`, `environment`, `enabled`, `timeout` (по умолчанию 30000 мс).
- Удалённые: `"type": "remote"`, `url` (обязательно), `enabled`, `headers`, `oauth`, `timeout` (30000 мс).
- **OAuth**: автоматический для удалённых серверов — при 401 выполняется Dynamic Client Registration (RFC 7591), если сервер её поддерживает. Команды: `opencode mcp auth <имя>`, `mcp auth list`, `mcp logout <имя>`, `mcp debug <имя>`; токены хранятся в `~/.local/share/opencode/mcp-auth.json`. `oauth: false` отключает авто-OAuth (для серверов с API-ключами); объект `oauth` поддерживает `clientId`/`clientSecret`/`scope` (в т.ч. `{env:...}`).
- CLI: `opencode mcp add` (интерактивно), `mcp list`/`ls`. Организационные дефолты возможны через удалённый конфиг `.well-known/opencode` (серверы могут быть выключены по умолчанию и включаться локально opt-in).
- Документация предупреждает: MCP-серверы съедают контекст; например, GitHub MCP может превысить лимит контекста. Тулами MCP можно управлять глобально и per-agent глобами легаси-ключа `tools` (например `"my-mcp*": false`).

### LSP

Источник: https://opencode.ai/docs/lsp/ (V1).

- **В V1 LSP по умолчанию выключен**: отсутствие ключа `lsp` = выключено; `lsp: true` включает все встроенные серверы; по-серверная настройка — объект с `disabled`, `command`, `extensions`, `env`, `initialization`. Часть серверов автоустанавливается; `OPENCODE_DISABLE_LSP_DOWNLOAD` запрещает загрузки. Тул `lsp` (раздел 1) — экспериментальный (`OPENCODE_EXPERIMENTAL_LSP_TOOL`).
- Сама документация предупреждает: LSP может рассинхронизироваться, потребляет заметную память и замедляет работу; часто лучше гонять lint/typecheck CLI-командами через AGENTS.md/скиллы.
- **Встроенные серверы (34, по таблице документации)**: astro, bash (bash-language-server), clangd, csharp, clojure-lsp, dart, deno, elixir-ls, eslint, fsharp, gleam, gopls, haskell-language-server, jdtls, julials, kotlin-ls, lua-ls, nixd, ocaml-lsp, oxlint, php (intelephense), prisma, pyright, razor, ruby-lsp (rubocop), rust (rust-analyzer), sourcekit-lsp, svelte, terraform, tinymist, typescript, vue, yaml-ls (Red Hat yaml-language-server), zls.
- **V2: конфиг LSP принимается, но сами language servers не запускаются** (по вторичным источникам, согласованно между собой; детальной проверки по первоисточнику V2 не проводилось).

### Темы

Источник: https://opencode.ai/docs/themes/ (V1).

- Тема по умолчанию — `opencode`; выбор через `/themes` или ключ `theme` в `tui.json`; нужен truecolor (`COLORTERM=truecolor` или 24bit), иначе приближение 256 цветами.
- Встроенные (по документации, список неполный — «and more»): system (адаптируется к фону терминала, ANSI 0–15), tokyonight, everforest, ayu, catppuccin, catppuccin-macchiato, gruvbox, kanagawa, nord, matrix, one-dark.
- Свои темы — JSON по схеме `https://opencode.ai/theme.json`: палитра `defs` + карта `theme`; значения — hex, ANSI 0–255, ссылки на цвета, варианты `{dark, light}`, `"none"` (дефолт терминала/прозрачность). Порядок загрузки: встроенные (зашиты в бинарь) → `~/.config/opencode/themes/*.json` (или `$XDG_CONFIG_HOME/opencode/themes/*.json`) → `<корень проекта>/.opencode/themes/*.json` → `./.opencode/themes/*.json`; поздние переопределяют одноимённые.

### Headless: run / serve / attach

Источник: https://opencode.ai/docs/cli/ и https://opencode.ai/docs/server/ (V1).

- `opencode run [message..]` — неинтерактивный запуск: флаги `--command`, `--continue/-c`, `--session/-s`, `--fork`, `--model/-m`, `--agent`, `--file/-f`, `--format` (default|json), `--title`, `--attach`, `--variant` (reasoning effort), `--thinking`, `--auto`. Через `--attach http://localhost:4096` можно подключиться к уже работающему `serve`, чтобы избежать холодного старта MCP.
- `opencode serve` — headless HTTP-сервер. По умолчанию порт `4096`, hostname `127.0.0.1`; флаги `--port`, `--hostname`, `--mdns`, `--mdns-domain` (по умолчанию `opencode.local`), `--cors` (можно несколько раз). `OPENCODE_SERVER_PASSWORD` включает HTTP Basic Auth (пользователь по умолчанию `opencode`, переопределяется `OPENCODE_SERVER_USERNAME`); действует и для `serve`, и для `web`. При запуске обычного TUI сервер стартует вместе с ним (TUI — клиент сервера; порт/hostname случайные, если не заданы флагами `--hostname`/`--port`).
- Сервер публикует **OpenAPI 3.1** спецификацию по адресу `http://<hostname>:<port>/doc` (например `http://localhost:4096/doc`); из неё генерируется SDK. Эндпоинт `/tui` позволяет управлять TUI через сервер (так работают IDE-плагины).
- Основные группы API сервера: `/global/health`, `/global/event` (SSE), `/project`, `/project/current`, `/path`, `/vcs`, `/config`, `/config/providers`, `/provider` (+ OAuth authorize/callback), `/session` (CRUD, `/children`, `/todo`, `/init`, `/fork`, `/abort`, `/diff`, `/summarize`, `/revert`, `/unrevert`, `/permissions/:id`), `/session/:id/message`, `/prompt_async`, `/command`, `/shell`, `/command`, `/find`, `/find/file`, `/find/symbol`, `/file`, `/file/content`, `/file/status`, `/lsp`, `/formatter`, `/mcp` (статус + динамическое добавление), `/agent`, `/log`, `/tui/*`, `/auth/:id`, `/event` (SSE; первое событие — `server.connected`), `/doc`, `/experimental/tool*`.
- `opencode attach [url]` — подключить TUI к уже запущенному бэкенду (`serve`/`web`): флаги `--dir`, `--continue/-c`, `--session/-s`, `--fork`, `--password/-p`, `--username/-u`.
- `opencode web` — headless-сервер + веб-интерфейс одной командой.

### SDK

Источник: https://opencode.ai/docs/sdk/ (V1).

- **`@opencode-ai/sdk`** — типобезопасный JS/TS клиент для сервера (установка через npm). `createOpencode()` поднимает сервер+клиент (по умолчанию hostname 127.0.0.1, порт 4096, timeout 5000 мс); `createOpencodeClient({ baseUrl })` — клиент к существующему серверу. Типы (`Session`, `Message`, `Part` и др.) генерируются из OpenAPI-спецификации сервера.
- Поверхность API: global.health (только v2-клиент), app.log/agents, project.list, path.get, config.get/providers, session.* (list/get/children/create/delete/update/init/abort/share/unshare/summarize/messages/message/prompt/command/shell/revert/unrevert/permissions), find.text/files/symbols, file.read/status, tui.* (appendPrompt, openHelp/Sessions/Themes/Models, submitPrompt, clearPrompt, executeCommand, showToast), auth.set, event.subscribe (SSE-поток событий).
- Структурированный JSON-вывод — через клиент `@opencode-ai/sdk/v2` (`format: { type: "json_schema", schema, retryCount }`, по умолчанию retryCount 2; результат — тул StructuredOutput).
- Важно: на странице SDK прямо сказано, что `session.share` **больше не работает** — «Sharing has been discontinued; this endpoint returns an error»; `session.unshare` лишь очищает сохранённую ссылку (см. раздел 5).

### ACP (Agent Client Protocol)

Источник: https://opencode.ai/docs/acp/ (V1).

- Команда **`opencode acp`** запускает OpenCode как ACP-совместимый подпроцесс: общение с редактором по JSON-RPC через stdio (CLI-документация описывает транспорт как nd-JSON; флаги `--cwd/--port/--hostname/--mdns/--cors`).
- Поддерживаемые редакторы из документации: Zed (установка из Zed ACP Registry командой `zed: acp registry` или ручная запись в `~/.config/zed/settings.json`: `"agent_servers": { "OpenCode": { "type": "custom", "command": "opencode", "args": ["acp"] } }`), JetBrains IDE (запись в `acp.json` с абсолютным путём к бинарю), Avante.nvim, CodeCompanion.nvim.
- Через ACP доступны все фичи: встроенные тулы, свои тулы и слэш-команды, MCP-серверы из конфига, правила из `AGENTS.md`, форматтеры/линтеры, агенты и система разрешений.

### GitHub-интеграция

Источник: https://opencode.ai/docs/github/ (V1). Актуальна.

- Упоминание **`/opencode`** или **`/oc`** в комментарии к issue или PR запускает OpenCode внутри GitHub Actions runner: триаж issue, исправления с созданием ветки и PR, ревью с контекстом файла/строк/диффа.
- Установка: `opencode github install` (ведёт через установку GitHub App, создание workflow и секреты) либо вручную: приложение **github.com/apps/opencode-agent** + workflow `.github/workflows/opencode.yml` с шагом `uses: anomalyco/opencode/github@latest`.
- Входные параметры action: `model` (обязателен, формат `provider/model`), `agent` (первичный; fallback — `default_agent` или `build`), `prompt`, `mentions` (по умолчанию `/opencode,/oc`, без учёта регистра), `variant` (reasoning effort: `high`, `max`, `minimal`…), `oidc_base_url` (по умолчанию `https://api.opencode.ai`), `use_github_token` (по умолчанию false: вместо OIDC-обмена на токен GitHub App используется переданный `GITHUB_TOKEN`).
- События-триггеры: `issue_comment`, `pull_request_review_comment`, `issues` (нужен `prompt`), `pull_request` (без `prompt` по умолчанию делает ревью PR), `schedule` (нужен `prompt`), `workflow_dispatch` (нужен `prompt`).
- CLI: `opencode github install|run` (`--event`, `--token`); также `opencode pr <номер>` — fetch+checkout ветки PR и запуск OpenCode.

---

## 5. Контекст и сессии

### Автосжатие (compaction)

Источник: https://opencode.ai/docs/config/ (V1, данные на 2026-10-31).

- Конфиг `compaction`: `auto` — автосжатие при приближении к лимиту контекста, по умолчанию **true**; `prune` — удаление старых выводов тулов, по умолчанию **false**; `reserved` — резервный буфер токенов, который оставляют свободным.
- Точных числовых порогов (в процентах или токенах) документация V1 не публикует — **не подтверждено**. Для сравнения: в V2 форма иная (`keep.tokens` по умолчанию 15000, `buffer` — «15%» или число токенов; https://opencode.ai/v2/docs/compaction) — значения V2 нельзя переносить на V1.
- Сжатие выполняет скрытый системный агент **compaction** (раздел 3); какая именно модель при этом используется (основная или `small_model`) — по документации **не подтверждено**. Плагины могут вмешиваться через хук `experimental.session.compacting` (добавить контекст или полностью заменить промпт сжатия). Отключение автосжатия: `OPENCODE_DISABLE_AUTOCOMPACT`; отключение prune — `OPENCODE_DISABLE_PRUNE`.
- Вручную: команда `/compact` (алиас `/summarize`, ctrl+x c) в TUI; через API — `POST /session/:id/summarize` с явными `providerID`/`modelID`.
- Критика из вторичных источников (доклад автора фреймворка Pi, 2026): прунинг OpenCode описывается как «обрезает все выводы тулов, кроме последних ~40 000 токенов, на каждом ходе», что ломает prompt caching у провайдеров. Цифра 40k по первоисточнику **не подтверждена**; сам факт прунинга как механизма — подтверждён конфигом выше.

### /undo и /redo

Источник: https://opencode.ai/docs/tui/ и https://opencode.ai/docs/config/ (V1).

- `/undo` (ctrl+x u) удаляет последнее сообщение пользователя, все последующие ответы **и все изменения файлов**; `/redo` (ctrl+x r) восстанавливает (только после `/undo`). Через API этому соответствуют `/session/:id/revert` и `/unrevert`.
- Механизм: по странице TUI, для файловых изменений «внутри используется Git» — **проект должен быть git-репозиторием**. По странице конфига, за это отвечает система **snapshot**: включён по умолчанию внутренний git-репозиторий, который трекает изменения файлов; при отключении `snapshot` откат через UI недоступен. Предупреждение документации: на больших репозиториях и с submodule снапшоты могут медленно индексироваться и занимать много диска.
- Глубина undo (один шаг или многоуровневый стек): вторичные источники говорят о многоуровневости, страница документации глубину явно не оговаривает — строго по первоисточнику **не подтверждено**.

### Сессии: продолжить, форк, экспорт

Источник: https://opencode.ai/docs/cli/ , https://opencode.ai/docs/tui/ , https://opencode.ai/docs/troubleshooting/ (V1).

- Продолжить: запуск с `--continue/-c` или `--session/-s <id>` (и у TUI, и у `opencode run`); в TUI — `/sessions` (алиасы `/resume`, `/continue`; ctrl+x l). `opencode session list|delete` (`-n`, `--format` table|json).
- Форк: флаг `--fork` при продолжении (TUI и `run`), в API — `POST /session/:id/fork` с необязательным `messageID` (форк от конкретного сообщения).
- Хранение: с версии 1.2.0 сессии лежат в единой SQLite-базе **`opencode.db`** в директории данных `~/.local/share/opencode/` (там же `auth.json` и `log/` — 10 последних файлов логов); путь к базе переопределяется `OPENCODE_DB`.
- Экспорт/импорт: **`opencode export [sessionID]`** — JSON (флаг `--sanitize` редактирует чувствительные данные); **`opencode import <файл>`** — импорт из JSON-файла. В TUI `/export` (ctrl+x x) сохраняет переписку в Markdown и открывает в `$EDITOR` — это другой, «человеческий» экспорт.

### Share (публичные ссылки) — ОТМЕНЕНО в V1

Критичное изменение, которое не отражено в большинстве обзоров:

- Исторически: конфиг `share` (`manual` по умолчанию / `auto` / `disabled`), команда `/share` создавала публичную ссылку вида `https://opncd.ai/s/{id}`, история сессии синхронизировалась на серверы OpenCode и была доступна любому со ссылкой; `/unshare` отзывал доступ (по довоенной документации и её зеркалам).
- **С версии v1.19.1 (2026-10-15) шаринг прекращён**: единственный пункт релиза в секции Core/Bugfixes — «Session sharing has been discontinued. Existing share links and session-sharing commands are no longer available, and importing sessions from share URLs is unsupported»: https://github.com/anomalyco/opencode/releases/tag/v1.19.1
- Следствия для установленной 1.19.3: команд `/share` и `/unshare` в актуальном списке команд TUI нет; SDK-метод `session.share` возвращает ошибку. Замена — `opencode export`/`import` JSON-файлами. В V2 шаринг также удалён (v2.0.30, по вторичным источникам).

### Память между сессиями

Встроенной кросс-сессионной памяти (автоматических заметок о пользователе/проекте между сессиями) в V1 **нет** — не найдено ни в документации, ни в конфиге (**не подтверждено** наличие в каком-либо виде). Единственный «долгоживущий» слой — файлы правил `AGENTS.md` и `instructions` (раздел 8), которые пользователь/агент поддерживают вручную (`/init` генерирует AGENTS.md). Существуют сторонние плагины памяти (например, supermemory) — это не ядро.

---

## 6. Интерфейс (TUI)

### На чём написана

Источник: репозиторий https://github.com/anomalyco/opencode (состав и структура на 2026-10-31), документация.

- Актуальная линия (V1 1.19.x и V2): **TypeScript на рантайме Bun**, монорепозиторий (`packages/`, `bun.lock`, `turbo.json`). TUI построена на **SolidJS + OpenTUI** (`@opentui/solid`, `@opentui/core`; OpenTUI — движок с нативным ядром на Zig и TS-биндингами, проект той же компании Anomaly). Прямым следствием является дифф-рендеринг по клеткам терминала вместо полной перерисовки.
- **Go/Bubble Tea к текущему OpenCode отношения не имеет**: на Go была написана ранняя генерация проекта 2025 года; отдельный Go-проект `opencode-ai/opencode` архивирован в сентябре 2025 (по вторичным источникам) и является предшественником Crush, а не этого продукта.
- Помимо TUI, тот же сервер обслуживают: десктопное приложение (бета), веб-интерфейс (`opencode web`), IDE-интеграции и ACP-редакторы (раздел 4).

### Поведение экрана, мышь, картинки

Источник: https://opencode.ai/docs/tui/ и https://opencode.ai/docs/config/ (V1).

- Лидер-клавиша по умолчанию — `ctrl+x` (`leader_timeout` по умолчанию 2000 мс); палитра команд — `ctrl+p`; настройки вида (например, видимость юзернейма) сохраняются между перезапусками.
- Мышь: поддержка включена по умолчанию (ключ `mouse` в `tui.json`); отключение возвращает терминалу нативное выделение/скролл; также `OPENCODE_DISABLE_MOUSE`.
- Полноэкранный/alternate-screen режим: документация TUI явно не описывает — **не подтверждено** по первоисточнику (для V2 в issue #56412 интерфейс прямо назван «full-screen TUI»: https://github.com/anomalyco/opencode/issues/56412).
- Картинки: конфиг `attachment.image` — авто-ресайз вложений (по умолчанию максимум 2000×2000, `max_base64_bytes` по умолчанию 5242880); перетаскивание/вставка картинок из буфера упоминается во вторичных источниках, по странице TUI на 2026-10-31 прямо **не подтверждено**. Видит ли модель картинки — определяется возможностями модели по метаданным Models.dev (раздел 7).
- Ввод: `@` — нечёткий поиск файлов, содержимое автоматически добавляется в контекст (в автодополнении также настроенные references); сообщение, начинающееся с `!команда`, выполняет shell-команду и добавляет вывод как результат тула; `/editor` (ctrl+x e) — редактирование промпта в `$EDITOR`; `/thinking` — показ/скрытие блоков рассуждений; `diff_style`: `auto`/`stacked`; курсор: стиль block (по умолчанию)/underline/line/default, мигание по умолчанию включено; скролл: `scroll_speed` по умолчанию 3, есть `scroll_acceleration`.
- Уведомления: ключ `attention` в tui.json — десктопные уведомления и звуки, **по умолчанию выключены**; события: question, permission, error, done, subagent_done; звуковой пак `opencode.default`, громкость по умолчанию 0.4.

### Слэш-команды TUI (V1)

Источник: https://opencode.ai/docs/tui/ . `/connect`, `/compact` (алиас `/summarize`), `/details`, `/editor`, `/exit` (алиасы `/quit`, `/q`), `/export`, `/help`, `/init`, `/models`, `/new` (алиас `/clear`), `/redo`, `/sessions` (алиасы `/resume`, `/continue`), `/themes`, `/thinking`, `/undo`, `/update`. Команд `/share` и `/unshare` в актуальном списке нет (раздел 5).

### Статусная строка и спиннер

- Документированного описания состава статусной строки в V1 **не найдено** (не подтверждено). Известно из конфига/интерфейса: отображаются текущий агент и модель, в режиме `--auto` — приглушённый индикатор `auto` в промпте.
- Спиннер ожидания существует; косвенное подтверждение из первоисточника issues: issue **#22017** — «~30% CPU usage while waiting for model response — possibly spinner-driven render loop at 60 FPS»: https://github.com/anomalyco/opencode/issues/22017

### Проблемы рендера и производительности TUI (из issues)

- **#22017** — ~30% CPU в простое ожидания ответа модели, предположительно из-за рендер-цикла спиннера на 60 FPS.
- **#52921** (V2, 2.0.22) — при многих загруженных директориях проектов компакшн оставляет обновления TUI «на секунды позади», разговоры выглядят замороженными: события шины доставляются всем подписчикам с последующей фильтрацией (159 180 визитов подписчиков в регрессионном тесте против 780 после фикса): https://github.com/anomalyco/opencode/issues/52921
- **#56412** (V2, 2.0.32) — клики по вкладкам сессий в полноэкранном TUI перестают переключать сессии до перезапуска.
- **#49777** (V2, 2.0.8) — краш TUI при рендере диалога ответа `/btw` («PluginProvider is missing»), исправлен в 2.0.9: https://github.com/anomalyco/opencode/issues/49777
- Жалобы на «мерцание» (flicker) именно OpenCode в первоисточниках найти не удалось — наоборот, вторичные сравнения хвалят OpenCode за отсутствие мерцания на фоне конкурентов; фиксируем как **не подтверждено** для OpenCode и не переносим чужие проблемы.

---

## 7. Модели и провайдеры

Источник: https://opencode.ai/docs/models/ и https://opencode.ai/docs/providers/ (V1, данные на 2026-10-31).

### Провайдеры и Models.dev

- OpenCode использует **Vercel AI SDK + каталог Models.dev** и поддерживает **75+ провайдеров LLM**, включая локальные модели. Формат ID везде — `provider_id/model_id`.
- Приоритет выбора модели при старте: флаг `--model` → конфиг `model` → последняя использованная модель → первая по внутреннему приоритету.
- Ключи подключаются через `/connect` и хранятся в `~/.local/share/opencode/auth.json` (CLI: `opencode auth login|list|logout`; также подхватываются ключи из env и проектного `.env`). На провайдера можно задать `options.baseURL`, а также `blacklist`/`whitelist` моделей; конфиг: `disabled_providers`/`enabled_providers` (при попадании в оба списка побеждает disabled).
- Собственные сервисы Anomaly (опционально): **OpenCode Zen** — курируемый набор протестированных моделей, отдельный провайдер; **OpenCode Go** — недорогая подписка на открытые модели.
- Подписки: официально поддерживаются ChatGPT Plus, GitHub Copilot, GitLab Duo. **Claude Pro/Max в сторонних тулах запрещён самой Anthropic**; плагины, делавшие это, удалены из комплекта с версии 1.3.0 (по документации провайдеров).

### Свои OpenAI-совместимые и локальные модели

В конфиге, ключ `provider.<свой-id>`:

- `"npm": "@ai-sdk/openai-compatible"` — для эндпоинтов `/v1/chat/completions`; для `/v1/responses` — `@ai-sdk/openai`;
- `name`, `options.baseURL`, опционально `options.apiKey` (в т.ч. `{env:...}`), `options.headers`;
- карта `models` с `name` и `limit { context, output }` (для стандартных провайдеров лимиты берутся из models.dev, для своих — задаются вручную).

Тем же паттерном в документации описаны локальные серверы: Atomic Chat (`http://127.0.0.1:1337/v1`), llama.cpp, LM Studio, Ollama.

### Роли моделей

- **`small_model`** — отдельная модель для лёгких задач, в документации назван пример — генерация заголовков сессий; если не задана, сначала пробуется более дешёвая модель провайдера, при её отсутствии — основная модель: https://opencode.ai/docs/config/
- Своя модель может быть задана per-agent полем `model` у агента (раздел 3) — так субагентам дают дешёвые/быстрые модели.
- Скрытые системные агенты title/summary/compaction существуют (раздел 3), но какая модель закреплена за каждым — по первоисточнику **не подтверждено**.

### Варианты (variants) и reasoning effort

- Варианты задаются объектом `variants` у модели: `provider.<id>.models.<model>.variants`. Встроенные дефолты: Anthropic — `high` (по умолчанию) и `max` (budgetTokens на thinking); OpenAI — `none/minimal/low/medium/high/xhigh` (reasoningEffort); Google — `low/high`.
- Глобальные опции модели: например, для OpenAI — `reasoningEffort`, `textVerbosity`, `reasoningSummary`, `include`; для Anthropic — `thinking: { type: enabled, budgetTokens }`.
- Переключение в рантайме — keybind `variant_cycle` (по вторичным источникам — Ctrl+T; в первоисточнике сочетание не сверено) и флаг `opencode run --variant`.
- Именно в **1.19.3** (2026-10-16) вышло изменение: «Configure model reasoning options to generate reasoning variants, replacing catalog variants while preserving your configured variant overrides»: https://github.com/anomalyco/opencode/releases/tag/v1.19.3

### Зрение

Поддержка изображений моделью берётся из метаданных Models.dev для конкретной модели; вложения нормализуются конфигом `attachment.image` (раздел 6). Отдельного «своего» vision-контура у OpenCode нет.

---

## 8. Совместимость: форматы и пути

Источник: https://opencode.ai/docs/config/ , https://opencode.ai/docs/rules/ и страницы разделов 3–4 (V1, данные на 2026-10-31). Пути приведены дословно.

### Конфиг

- Форматы: **JSON и JSONC**. Схема: `"$schema": "https://opencode.ai/config.json"`; схема TUI — `https://opencode.ai/tui.json`; схема тем — `https://opencode.ai/theme.json`.
- Конфиги **сливаются** (merge): поздние переопределяют только конфликтующие ключи. Прецедентность (от низкой к высокой): 1) удалённый конфиг (`.well-known/opencode`) → 2) глобальный `~/.config/opencode/opencode.json` → 3) кастомный (`OPENCODE_CONFIG`) → 4) проектный `opencode.json` (ищется от текущей директории вверх до ближайшей git-директории) → 5) директории `.opencode` → 6) инлайн (`OPENCODE_CONFIG_CONTENT`) → 7) managed-конфиги (Linux `/etc/opencode/`, macOS `/Library/Application Support/opencode/`, Windows `%ProgramData%\opencode`) → 8) macOS managed preferences (MDM, `ai.opencode.managed`, высший приоритет, пользователем не переопределяется).
- `OPENCODE_CONFIG_DIR` — дополнительная директория конфига: ищутся агенты/команды/режимы/плагины как в `.opencode`, загружается после глобальной и `.opencode` и может их переопределять.
- TUI-конфиг — отдельный: `tui.json`/`tui.jsonc` (глобально `~/.config/opencode/tui.json`, в проекте `tui.json` рядом с проектным конфигом). Легаси-ключи `theme`/`keybinds`/`tui` в `opencode.json` объявлены устаревшими и автоматически мигрируются.
- Подстановка переменных в конфиге: `{env:VAR}` (пустая строка, если не задана) и `{file:path}` (относительно директории конфига, абсолютный путь или `~`).
- Прочие важные ключи V1: `model`, `small_model`, `provider` (опции `timeout`, `headerTimeout` по умолчанию 300000, `chunkTimeout` по умолчанию 300000, `setCacheKey`), `agent`, `default_agent`, `subagent_depth`, `command`, **`snapshot`** (внутренний git для отката, по умолчанию включён), `autoupdate` (автоустановка обновлений отключена; `true`/`"notify"` — только уведомление; установка — `/update` или `opencode upgrade`), `formatter`, `lsp`, `permission`, `compaction` (`auto`/`prune`/`reserved`), `watcher.ignore`, `mcp`, `plugin`, `instructions`, `disabled_providers`/`enabled_providers`, `experimental` (примечание: `experimental.policies` требует V2 и в V1 не применяется), `server` (`port`/`hostname`/`mdns`/`mdnsDomain`/`cors`), `shell`, `attachment.image` (`auto_resize`, максимум 2000×2000, `max_base64_bytes` 5242880).

### Правила и инструкции

Источник: https://opencode.ai/docs/rules/

- **`AGENTS.md`**: в корне проекта + глобальный `~/.config/opencode/AGENTS.md`. Команда `/init` создаёт/обновляет проектный файл (сканирует репозиторий; фокус — build/lint/test-команды, архитектура, конвенции).
- **`CLAUDE.md` в V1 читается — как fallback**: проектный `CLAUDE.md` — только если нет `AGENTS.md`; глобальный `~/.claude/CLAUDE.md` — только если нет `~/.config/opencode/AGENTS.md`; также fallback на `~/.claude/skills/`. Порядок поиска: локальные файлы вверх по дереву (AGENTS.md, CLAUDE.md) → глобальный файл → файл Claude; в каждой категории побеждает первое совпадение. Отключение: `OPENCODE_DISABLE_CLAUDE_CODE=1`, `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1`, `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`.
- Ключ `instructions` в конфиге — массив путей/глобов/URL (удалённые файлы качаются с таймаутом 5 секунд), объединяется с AGENTS.md. Ссылки на файлы внутри AGENTS.md автоматически не разбираются.
- **V2: распознаётся только AGENTS.md, fallback на CLAUDE.md нет** (https://opencode.ai/v2/docs/instructions/); там же описано объединение нескольких вложенных AGENTS.md — это отличается от правила V1 «первое совпадение в категории».

### Пути расширений (V1, дословно)

| Что | Проект | Глобально |
|---|---|---|
| Агенты | `.opencode/agents/*.md` | `~/.config/opencode/agents/*.md` |
| Команды | `.opencode/commands/*.md` | `~/.config/opencode/commands/*.md` |
| Скиллы | `.opencode/skills/<имя>/SKILL.md` (+ `.claude/skills/`, `.agents/skills/`) | `~/.config/opencode/skills/<имя>/SKILL.md` (+ `~/.claude/skills/`, `~/.agents/skills/`) |
| Плагины | `.opencode/plugins/` | `~/.config/opencode/plugins/` |
| Свои тулы | `.opencode/tools/` | `~/.config/opencode/tools/` |
| Темы | `.opencode/themes/*.json` | `~/.config/opencode/themes/*.json` |
| Конфиг | `opencode.json` / `opencode.jsonc` в проекте, `tui.json` рядом | `~/.config/opencode/opencode.json`, `~/.config/opencode/tui.json` |
| Правила | `AGENTS.md` (fallback `CLAUDE.md`) | `~/.config/opencode/AGENTS.md` (fallback `~/.claude/CLAUDE.md`) |
| MCP | ключ `mcp` в `opencode.json` | тот же механизм конфигов |
| Keybinds | ключ `keybinds` в `tui.json` (сливается с дефолтами) | тот же механизм |

### Данные и секреты на диске

- Директория данных: `~/.local/share/opencode/` — `opencode.db` (SQLite: проекты, сессии, сообщения), `auth.json` (креды провайдеров), `mcp-auth.json` (токены MCP OAuth), `log/`.
- Кэш npm-плагинов: `~/.cache/opencode/node_modules/`.
- Бинарь при установке скриптом: `~/.opencode/bin/opencode` (README репозитория: «The install script installs OpenCode to `$HOME/.opencode/bin`»).

---

## 9. Вес и старт

### Локальный замер (облачная ВМ исполнителя, Linux x86_64)

Источник: **замер исполнителя, 2026-10-31**.

- Установлен OpenCode **1.19.3**; бинарь `~/.opencode/bin/opencode` — **186 013 824 байта (~186 МБ)** (скомпилированный Bun-бинарь со всеми зависимостями внутри).
- `opencode --version`: **862–1449 мс** по 5 запускам. Это только печать версии, **не** старт TUI; время холодного старта TUI до интерактивности отдельно не замерялось.
- Для контекста: luna-harness целится в <1 МБ и старт ~200 мс чистой Python-stdlib программой — разрыв с эталоном по весу более чем в 180 раз, по старту — в 4–7 раз даже на тривиальной операции печати версии.

### Данные из источников

- По вторичному источнику (обзор с замерами дистрибутивов, октябрь 2026): скачиваемый Linux-дистрибутив v1.19.4 — 79 667 571 байт (это размер пакета/архива, не установленного бинаря), пакет V2 2.1.4 с npm — 113 868 298 байт. Первоисточником (страницей релиза с размерами asset'ов) не сверено — использовать с осторожностью.
- Память: в документации CLI зафиксирована переменная `OPENCODE_AUTO_HEAP_SNAPSHOT` — при превышении heap **2 GiB** автоматически снимается heap snapshot; сам факт существования такого порога говорит о порядке ожидаемого потребления памяти: https://opencode.ai/docs/cli/
- В трекере есть отдельный **«Memory Megathread»** (открыт 2026-04-02) для жалоб на память — номер issue по имеющимся выдержкам не установлен. Вторичные обзоры приводят жалобы на утечки в одной сессии «до 23 ГБ» и 200–300% CPU при рендере текста — эти цифры первоисточником **не подтверждены**, приведены как иллюстрация тона жалоб.
- Из первоисточника issues: issue о LSP-диагностиках, сохраняемых инлайн в событиях, — раздувание сессий на гигабайты и сильный лаг GUI (64–392 МБ на событие), июль 2026; номер по выдержкам не установлен.
- Официальных опубликованных бенчмарков времени старта/потребления памяти у проекта нет — **не подтверждено**.

---

## 10. Что бесит пользователей (GitHub issues)

Источник: трекер https://github.com/anomalyco/opencode/issues (выборка на 2026-10-31). Номера и ссылки — на конкретные issue; где issue относится к V2, это помечено.

1. **«Каждое обновление что-то ломает»** — #57783 «Another bug after update» (2026-10-21): после очередного обновления агент «зависает в thinking навсегда»; автор прямо пишет, что будет удивлён обновлению без нового бага: https://github.com/anomalyco/opencode/issues/57783 . В том же треде официальный бот сводит похожие жалобы: зависание агента на ~10 минут после прерывания ответа (#56941), краш сессии «Failed to drain Session» (#57442), обрыв стрима OpenAI без finish_reason (#56858).
2. **Бесшумные бесконечные ретраи стрима** — #49030: при сбое стрима провайдера CLI повторяет попытки молча и бесконечно — снаружи выглядит как зависание без какой-либо обратной связи (номер приведён в сводке бота в #57783).
3. **Деградация после нескольких компакшнов** — #50103: «Performance degradation and long "Thinking" timeouts after multiple context compactions» (номер приведён в сводке бота в #57783).
4. **CPU в простое** — #22017: ~30% CPU, пока TUI просто ждёт ответ модели (предположительно рендер-цикл спиннера на 60 FPS): https://github.com/anomalyco/opencode/issues/22017
5. **Память** — отдельный «Memory Megathread» (с 2026-04-02) как постоянный тред жалоб на потребление и утечки; плюс раздувание сессий LSP-диагностиками (64–392 МБ на событие) с гигабайтами на диске и лагом (июль 2026, номер не установлен).
6. **Отставание TUI при нагрузке** — #52921 (V2): при многих загруженных проектах обновления интерфейса отстают на секунды, «разговоры выглядят замороженными» во время стриминга/компакшна: https://github.com/anomalyco/opencode/issues/52921
7. **Поломки бесплатного тира / путаница с провайдерами** — кластер дублей вокруг ошибки «OpenCode's free tier can only be used from within OpenCode» (#55737, #55443, #55616, #55428, #50366, #55435, октябрь 2026): бесплатные модели из каталога внезапно отказывают и в TUI, и в CLI, при этом `opencode models` их по-прежнему перечисляет, а `auth list` показывает 0 кредов: https://github.com/anomalyco/opencode/issues/55737 . Официальный ответ бота в треде — разработка идёт на V2, V1 чинят только критичное, — сам по себе стал источником раздражения у пользователей V1.
8. **Краши TUI на ровном месте** — #49777 (V2): `/btw` роняет весь TUI при рендере ответа на всех платформах (исправлено в 2.0.9): https://github.com/anomalyco/opencode/issues/49777 ; #56412 (V2): вкладки сессий перестают отвечать на клики до перезапуска: https://github.com/anomalyco/opencode/issues/56412
9. **V1 заброшена де-факто** — следствие п. 7: пользователи ветки 1.x получают ответ «переходите на V2», хотя V2 ломает совместимость плагинов и конфигов (раздел 4); одновременное существование двух линий и двух документаций — устойчивый источник путаницы (пример — расхождения singular/plural в путях и переименованные ключи конфига).

Справедливости ради: тот же трекер отмечают как сильную сторону проекта — официальный GitHub App проекта сам читает issue, воспроизводит баг (для TUI-багов записывает видео) и комментирует результат за минуты (оценка из дискуссии анонса V2 на Hacker News, вторичный источник).

---

## Итоговая таблица возможностей (V1, 1.19.3, на 2026-10-31)

| Возможность | Статус | Примечание |
|---|---|---|
| Тул bash | есть | shell-команды; разрешение `bash` по паттернам команд |
| Тул edit / write | есть | точная замена строки / перезапись; оба под разрешением `edit` |
| Тул read | есть | диапазоны строк; `*.env` по умолчанию `ask` |
| Тул grep / glob | есть | ripgrep под капотом / паттерны, сортировка по mtime |
| Тул list | частично | есть в таблицах разрешений, нет отдельного заголовка в Tools — статус в 1.19.3 неясен |
| Тул apply_patch (patch) | есть | маркеры `*** Add/Update/Move/Delete File:` |
| Тул task (субагенты) | есть | запуск субагентов, глубина — `subagent_depth` (дефолт 1) |
| Тул todowrite | есть | todo-списки сессии |
| Тул webfetch / websearch | есть | websearch — через Exa/Parallel без API-ключа |
| Тул question | есть | вопрос пользователю с вариантами |
| Тул lsp | частично | экспериментальный (`OPENCODE_EXPERIMENTAL_LSP_TOOL`), LSP по умолчанию выключен |
| Тул skill | есть | нативная загрузка SKILL.md по требованию |
| Permissions allow/ask/deny + паттерны | есть | последнее совпавшее правило побеждает; `--auto` для автоодобрения |
| Агент build / plan | есть | plan запрещает правки, кроме файлов планов; bash — по обычным разрешениям |
| Песочница | нет | официально: permissions — UX, не изоляция; изоляция — Docker/VM |
| Защита doom_loop | есть | `ask` при 3 одинаковых вызовах подряд |
| Субагенты general / explore | есть | general — полный доступ и параллельная работа; explore — read-only |
| Субагент scout | не подтверждено | есть в материалах V2; на странице агентов V1 не назван |
| @-упоминания агентов | есть | `@general …`; дочерние сессии, навигация клавишами |
| Свои агенты (.opencode/agents/*.md) | есть | frontmatter: mode/model/permission/steps/hidden и др. |
| Параллельные субагенты | есть | несколько Task-вызовов; general описан как параллельный |
| Фоновые субагенты | частично | V1 — только `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS`; полноценно — V2 |
| Worktree | нет (V1) | встроенного управления нет; V2 — заявлено во вторичных источниках |
| Плагины JS/TS + события | есть | 27 событий + хуки tool.execute.before/after; в V2 API переписано, плагины V1 не работают |
| Свои команды (.opencode/commands/*.md) | есть | `$ARGUMENTS`, `` !`shell` ``, `@файл`, `subtask` |
| Свои тулы (.opencode/tools/) | есть | TS/JS-обёртки, могут переопределять встроенные |
| Скиллы SKILL.md | есть | пути `.opencode/skills`, `~/.config/opencode/skills` + Claude/agent-совместимые |
| MCP локальные и удалённые | есть | ключ `mcp`; OAuth с авто-регистрацией (RFC 7591), `mcp auth` |
| LSP встроенные серверы | частично | 34 сервера, но по умолчанию выключено; в V2 серверы не запускаются |
| Темы (свои JSON) | есть | схема theme.json; встроенных 11+ |
| Headless: run / serve / attach | есть | serve — OpenAPI 3.1 на /doc, порт 4096; attach к живому бэкенду |
| SDK @opencode-ai/sdk | есть | типы из OpenAPI; есть v2-клиент со structured output |
| ACP (opencode acp) | есть | JSON-RPC/stdio; Zed, JetBrains, Avante, CodeCompanion |
| GitHub-интеграция | есть | `/opencode` и `/oc` в комментариях; Actions `anomalyco/opencode/github` |
| Автокомпакшн контекста | есть | `compaction.auto` дефолт true; числовые пороги V1 не опубликованы |
| Prune выводов тулов | есть | `compaction.prune`, по умолчанию false |
| /undo / /redo | есть | снапшоты на внутреннем git (`snapshot` дефолт on); нужен git-репозиторий |
| Продолжение/форк сессии | есть | `--continue`, `--session`, `--fork`; хранение — SQLite `opencode.db` |
| Экспорт/импорт сессии | есть | `opencode export/import` JSON; `/export` в TUI — Markdown |
| Share (публичные ссылки) | нет | прекращено в v1.19.1 (2026-10-15); было до этого — `opncd.ai/s/{id}` |
| Память между сессиями | нет | только ручные AGENTS.md/instructions; встроенной памяти нет |
| Провайдеры 75+ (Models.dev) | есть | Vercel AI SDK + models.dev; формат `provider/model` |
| Свои OpenAI-совместимые провайдеры | есть | `@ai-sdk/openai-compatible`, baseURL, свои лимиты моделей |
| Локальные модели | есть | Ollama, LM Studio, llama.cpp, Atomic Chat — тем же паттерном |
| small_model (роль малой модели) | есть | заголовки и лёгкие задачи; fallback — дешёвая модель провайдера/основная |
| Варианты моделей (reasoning effort) | есть | `variants` у модели; в 1.19.3 генерируются из reasoning-опций |
| Зрение (картинки моделям) | есть | по метаданным models.dev; `attachment.image` — ресайз до 2000×2000 |
| AGENTS.md | есть | проектный + `~/.config/opencode/AGENTS.md`; `/init` генерирует |
| CLAUDE.md | частично | V1 — только fallback при отсутствии AGENTS.md; V2 — не читается |
| Конфиг opencode.json/jsonc + схема | есть | merge по 8 уровням прецедентности; TUI — отдельный tui.json |
| Keybinds | есть | в `tui.json`, лидер `ctrl+x`, сливаются с дефолтами |
| Мышь в TUI | есть | по умолчанию on; отключается (`OPENCODE_DISABLE_MOUSE`) |

---

## Примечание о слабых местах источников

1. **Две линии версий смешаны везде**: сайт по умолчанию показывает V2, документация V1 обновляется на месте и уже содержит вкрапления про V2 (например, на странице конфига описан V2-only `experimental.policies`, на страницах моделей/SDK — v2-клиенты). Каждый факт приходилось атрибутировать к линии вручную.
2. **Внутренние противоречия официальной документации V1**: страница агентов в разных местах говорит о двух и о трёх встроенных субагентах; формы путей в единственном/множественном числе (`agent/` vs `agents/`, `command/` vs `commands/`, `plugin/` vs `plugins/`) различаются между версиями страниц и зеркалами — в отчёте зафиксированы формы актуальных страниц на 2026-10-31 (множественное число).
3. **V2 почти не имеет первоисточников в удобной форме**: GitHub Releases для 2.x нет (только теги), даты и состав релизов приходится брать с opencode.ai/changelog и npm; значительная часть деталей V2 в этом отчёте опирается на согласованные вторичные источники и помечена соответственно.
4. **Числовые пороги компакшна V1, модель для сжатия, наличие scout в 1.19.3 и статус тула `list`** по первоисточникам не устанавливаются — в отчёте честно помечены «не подтверждено», а не додуманы.
5. **Замеры веса/памяти**: официальные бенчмарки отсутствуют; единственный твёрдый замер — локальный (бинарь и `--version`). Цифры из обзоров (размер дистрибутива, «23 ГБ утечек») приведены как вторичные и не сверены с первоисточником.
