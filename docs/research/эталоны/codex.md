# OpenAI Codex CLI — исследование эталона

> Проект: Лунапарк (luna-harness), задача 02 — эталоны.
> Дата среза: **2026-10-31**. Все факты ниже — на 2026-10-31, если не указано иное.
> Первоисточники: официальная документация `developers.openai.com/codex` (с 2026 г. зеркалируется как `learn.chatgpt.com/docs/...`, Markdown-близнецы с суффиксом `.md` и сводный `codex-manual.md`), репозиторий https://github.com/openai/codex (README, releases/changelog, issues), npm `@openai/codex`.
> Агрегаторы и блоги использовались только как наводка и помечены отдельно; без подтверждения по первоисточнику факт помечен **«не подтверждено»**.
> Ничего не устанавливалось в ходе исследования (кроме ранее замеренной локальной копии, см. §9).

**Контекст версий на дату среза.** Репозиторий: 132 468 звёзд, 1 257 релизов, лицензия Apache-2.0, дефолтная ветка `main` — https://github.com/openai/codex (данные на 2026-10-31). Последний стабильный релиз в changelog, найденный по первоисточнику/зеркалам changelog: **0.166.0 от 2026-10-29** (keyboard-driven transcript selection, `codex queue --resume`, `codex doctor --network-only`, GPT-6.1 Terra в каталогах) — GitHub Releases `rust-v0.166.0`, сводка в https://github.com/openai/codex/releases (данные на 2026-10-31). Предыдущие: 0.165.0 (2026-10-27, встроенный провайдер Google Cloud Vertex AI `model_provider = "gcp"`, memory v2 для свежих профилей), 0.164.0 (2026-10-23). Локально замеренная альфа — 0.164.0-alpha.9 (см. §9).

> **Важная оговорка о дрейфе документации.** Документация Codex в 2025–2026 гг. несколько раз переезжала и переименовывалась: старые URL `developers.openai.com/codex/...` теперь ведут на `learn.chatgpt.com/docs/...`; старые понятия `suggest / auto-edit / full-auto`, `--full-auto`, `approval_policy = "untrusted"`, `[profiles.*]` внутри `config.toml`, `codex mcp-server` — **устарели/удалены** (см. ниже по разделам). При цитировании старых статей легко принести мёртвые факты.

---

## 1. Тулы — встроенные инструменты

Единого публичного «полного списка тулов» одной страницей OpenAI на 2026-10-31 **не публикует**: список собирается из Configuration Reference (`[features]`, `tools.*`), Non-interactive mode (типы item в JSONL), Hooks (tool coverage) и Subagents в сводном мануале. Источник сборки: https://learn.chatgpt.com/docs/codex-manual.md (данные на 2026-10-31).

### 1.1. Ядро

| Тул | Назначение | Источник (на 2026-10-31) |
|---|---|---|
| `shell` / `shell_command` | Выполнение shell-команд в песочнице (argv-форма / строковая форма). Чтение/поиск файлов моделью идёт в основном через него (`cat`, `rg`, `ls`). Флаг `features.shell_tool = true` (stable, по умолчанию вкл.) | Config basics, таблица feature flags: https://learn.chatgpt.com/docs/config-file/config-basic.md |
| `exec_command` + `write_stdin` (unified exec) | Долгоживущие/интерактивные команды: запуск с session id, дописывание stdin, опрос вывода. `features.unified_exec = true` (stable, по умолчанию вкл., «resumable command execution») | Там же; тип item `command_execution` в `codex exec --json`: https://learn.chatgpt.com/docs/non-interactive-mode.md |
| `apply_patch` | Структурированные правки файлов фирменной грамматикой `*** Begin Patch / *** End Patch` (Add / Update / Delete / Move File). Основной путь редактирования, в отличие от heredoc через shell. В hooks матчится также как `Edit` / `Write` | Hooks, tool coverage: https://learn.chatgpt.com/docs/hooks.md (в мануале — раздел Hooks) |
| `update_plan` | Внутренний план/трекер шагов: список шагов со статусами `pending / in_progress / completed`, не более одного `in_progress`. В hooks — «other local function tools», пример именно `update_plan` | Hooks в мануале; JSONL item type `plan` / plan updates в non-interactive mode |
| `web_search` | Поиск в вебе от провайдера. Режимы `web_search = "cached" \| "indexed" \| "live" \| "disabled"` (см. §7). `--search` = live. Отдельная настройка для кастомных провайдеров — `supports_standalone_web_search` | Config basics (Web search mode); CLI page: `codex --search` https://learn.chatgpt.com/docs/codex/cli.md |
| `view_image` | Подгрузка локального файла-картинки в контекст модели (скриншот, диаграмма). Включается `tools.view_image = true` / `features.view_image`. Парный пользовательский путь — `codex --image <file>` и вставка картинки в composer | Config Reference (`tools.view_image` — «Enable the local-image attachment tool `view_image`»); https://learn.chatgpt.com/docs/codex/cli.md |
| `request_user_input` | Запрос ввода/выбора у пользователя посреди хода (в т.ч. в Plan mode задаёт уточняющие вопросы). **Не подтверждено** отдельной страницей доков на 2026-10-31 — встречается в перечне локальных function tools в hooks/исходниках; помечаю как частично подтверждённое | Hooks tool coverage (категория other local function tools), мануал |
| `request_permissions` / request_permissions_tool | Запрос расширения прав (файловая система/сеть) посреди хода; у кастомных агентов отключается `features.request_permissions_tool = false` | Subagents, custom agent file schema: https://learn.chatgpt.com/docs/agent-configuration/subagents.md |
| MCP-тулы | Все тулы подключённых MCP-серверов, имена вида `mcp__<server>__<tool>` в hooks | Hooks; MCP: https://learn.chatgpt.com/docs/extend/mcp.md |
| Apps / connectors тулы | Тулы плагинов/коннекторов (ChatGPT apps). Деструктивные app/MCP-вызовы всегда требуют approval, если тул помечен destructive annotation | Agent approvals & security: https://learn.chatgpt.com/docs/agent-approvals-security.md |
| `image generation`, `computer use`, браузер | **Не ядро CLI-тулов в том же смысле**: это возможности экосистемы ChatGPT/Codex (desktop/web, плагины). Для чистого CLI как встроенные тулы на 2026-10-31 **не подтверждено** | — |

### 1.2. Что важно для Лунапарка

- Нет отдельных тулов `read_file / grep / glob / edit` в Claude-Code-стиле: чтение и поиск — через `shell`, правка — через `apply_patch`. Это сознательная минималистичная поверхность.
- Параллельные вызовы: в 0.163.0 заявлены «parallel read-only tools» (changelog, данные на 2026-10-31, зеркало changelog GitHub Releases). Точный перечень read-only тулов, допущенных к параллели, документацией **не подтверждён**.
- `grep_files`, `read_file`, `list_dir`, встречающиеся в сторонних реимплементациях (например, PyPI `python-codex`), — **не** официальные имена тулов upstream Codex; не путать.

---

## 2. Режимы и разрешения

Источник: https://learn.chatgpt.com/docs/agent-approvals-security.md, https://learn.chatgpt.com/docs/config-file/config-basic.md, https://learn.chatgpt.com/docs/permissions.md (все — данные на 2026-10-31).

### 2.1. Две ортогональные ручки

1. **Sandbox mode — что технически можно** (`sandbox_mode`, флаг `-s/--sandbox`):
   - `read-only` — только чтение;
   - `workspace-write` — **дефолт для интерактива**: запись в workspace (cwd + writable roots + `/tmp`/`$TMPDIR`), внутри writable roots каталоги `.git`, `.codex`, `.agents` (и `.aws` как protected metadata) остаются read-only; сеть по умолчанию **выключена**;
   - `danger-full-access` — без песочницы.
   Реализация ОС: macOS — Seatbelt (`sandbox-exec`); Linux/WSL2 — bubblewrap + seccomp (в 0.165.0 документация уточнила именно эту связку; Landlock упоминается как fallback в сторонних сверках — по первоисточнику **частично подтверждено**); Windows — нативный sandbox, MXC (`[features] prefer_mxc = true`, legacy `[windows] sandbox = "elevated" | "unelevated"`).
2. **Approval policy — когда спрашивать** (`approval_policy`, флаг `-a/--ask-for-approval`):
   - `on-request` — дефолт: внутри песочницы работает сам, выход за песочницу/сеть/эскалация — с запросом;
   - `never` — никогда не спрашивает (невозможное в песочнице просто падает);
   - granular-таблица: `approval_policy = { granular = { sandbox_approval = ..., rules = ..., mcp_elicitation = ..., ... } }` — по категориям (детали в Config Reference);
   - ~~`untrusted`~~ — **retired**: документация прямо требует удалить `approval_policy = "untrusted"`, иначе клиент может не стартовать; замена для строгого интерактива — `sandbox_mode = "read-only"` + `approval_policy = "on-request"` + `trust_level = "untrusted"` у проекта (см. ниже).

Старые имена **2025 года** `suggest / auto-edit / full-auto` и флаг `--full-auto` — **удалены**: Non-interactive mode прямо пишет «`--full-auto` is no longer supported», замена — `--sandbox workspace-write` или `--approve-for-me`.

### 2.2. Пресеты и особые флаги

- `/permissions` в сессии — меню пресетов: **Ask for approval** (≈ `-s workspace-write -a on-request`), **Approve for me** (`--approve-for-me`, alias `--not-so-yolo`: запросы уходят автоматическому ревьюеру — модель `codex-auto-review`, песочница при этом остаётся workspace-write, сеть не открывается), **Full Access** (≈ `-s danger-full-access -a never`), **Read Only**. Пресеты по первоисточнику подтверждены страницей approvals; точные подписи меню в TUI сверены по вторичному источнику, проверившему 0.163.0, — помечаю подписи как **частично подтверждённые**.
- `--yolo` = `--dangerously-bypass-approvals-and-sandbox` = `never` + `danger-full-access` одновременно. Нельзя комбинировать с `-a`.
- `codex exec` по умолчанию: sandbox `read-only`, approval фактически `never` (спросить не у кого).

### 2.3. Сеть

- Дефолт: **off** в `workspace-write`. Включение: `[sandbox_workspace_write] network_access = true`.
- Поверх включённой сети — `features.network_proxy` с доменными правилами allow/deny: exact host, `*.example.com` (только поддомены), `**.example.com` (apex+поддомены), глобальный `*` только для allow, `deny` всегда побеждает; `allow_local_binding = false` по умолчанию режет loopback/link-local/private; есть best-effort защита от DNS rebinding. Сам по себе `network_proxy` сеть **не включает**.
- Hosted web search, MCP и apps-тулы командными сетевыми правилами **не** отключаются — это отдельные поверхности.

### 2.4. Permission profiles (новая система поверх sandbox)

- `default_permissions = ":read-only" | ":workspace" | ":danger-full-access" | "<custom>"`; кастомные — `[permissions.<name>]` с `extends`, файловыми правилами read/write/deny (вплоть до glob вида `**/*.env` — по первоисточнику Permissions; пример с glob сверен по вторичке) и `[permissions.<name>.network]` (`enabled`, `domains`).
- Профили в документации Config basics названы поддерживаемыми; в сторонней сверке на октябрь 2026 помечены как **beta**. Статус maturity по первоисточнику на 2026-10-31: **частично подтверждён** (есть страница Permissions и ключи в Config Reference, ярлык beta в самой доке явно не выловлен).
- Если в конфигах присутствует `sandbox_mode`/`--sandbox`, старая система имеет приоритет над `default_permissions` (кроме managed `allowed_permission_profiles`) — по вторичной сверке с первоисточником, **частично подтверждено**.

### 2.5. Профили конфигурации (не путать с permission profiles)

- `--profile <name>` (`-p`): накладывает `~/.codex/<name>.config.toml` поверх `~/.codex/config.toml`. С 0.134.0 `[profiles.<name>]` внутри `config.toml` и селектор `profile = "..."` **не поддерживаются**. Источник: Config Advanced, раздел Profiles (данные на 2026-10-31).
- Приоритет конфига (от высшего): CLI-флаги/`-c` → проектные `.codex/config.toml` (root→cwd, только trusted) → файл профиля `--profile` → `~/.codex/config.toml` → cloud-managed defaults → `/etc/codex/config.toml` → built-in defaults. Плюс `requirements.toml` (managed, admin) как ограничивающая рамка: может запретить `never`/`danger-full-access` и т.д.
- Доверие проекту: `[projects."/path"] trust_level = "trusted" | "untrusted"` в `~/.codex/config.toml`. Untrusted ⇒ проектные `.codex/config.toml`, hooks и rules **не загружаются**.

### 2.6. Rules (execpolicy)

См. §8.4: `prefix_rule(pattern, decision = "allow"|"prompt"|"forbidden")` в `~/.codex/rules/*.rules` (Starlark), проверка — `codex execpolicy check`. Правила управляют именно запусками **вне** песочницы. Статус в доке — experimental.

---

## 3. Субагенты и параллельность

Источники: https://learn.chatgpt.com/docs/agent-configuration/subagents.md, Config basics (feature flags), мануал (данные на 2026-10-31).

### 3.1. Субагенты — есть, включены по умолчанию

- Feature flag `features.multi_agent = true` (stable, default on). В Config Reference: «Enable multi-agent collaboration tools (`spawn_agent`, `send_input`, `resume_agent`, `wait_agent`, and `close_agent`) (stable; on by default)».
- **Тулы оркестрации:** `spawn_agent`, `send_input`, `resume_agent`, `wait_agent`, `close_agent`. В hooks `spawn_agent` также матчится как `Agent`.
- Триггер: только по прямой просьбе пользователя («spawn two agents», «delegate in parallel») или по инструкции из `AGENTS.md`/скилла; проактивной делегации в CLI на обычном уровне reasoning нет (проактивность — фишка уровня Ultra в ChatGPT Work, не CLI-дефолт).
- Встроенные агенты: `default` (general-purpose fallback), `worker` (implementation/fixes), `explorer` (read-heavy exploration).
- Кастомные агенты: по одному TOML-файлу в `~/.codex/agents/*.toml` (personal) или `.codex/agents/*.toml` (project). Обязательные поля: `name`, `description`, `developer_instructions`; опционально `model`, `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, отключение фич (`features.shell_tool` и др.) и отдельных скиллов (`skills.config`). Наследуют от родителя permissions, provider и MCP; переопределить их из файла агента **нельзя**. Имя кастомного агента, совпавшее со встроенным, побеждает встроенный.
- Глобальные настройки `[agents]` в `config.toml`: `agents.enabled` (default true), `agents.max_concurrent_threads_per_session` (legacy alias `agents.max_threads`), `agents.default_subagent_model`, `agents.default_subagent_reasoning_effort`, `agents.interrupt_message` (default true).
- Управление в TUI: `/subagents` (в ряде вторичных источников — `/agent(s)`; по первоисточнику Subagents подтверждена именно `/subagents` для CLI; `/agents` как вид-обзор фигурирует в DevDay-материалах — статус **частично подтверждён**). Approval-запросы из неактивных тредов всплывают оверлеем с меткой треда, клавиша `o` открывает тред. Субагенты наследуют sandbox родителя; в non-interactive потоке действие, требующее свежего approval, падает и ошибка возвращается родителю.
- Цена: каждый субагент жжёт свои токены; дока прямо предупреждает о большем расходе и советует параллелить read-heavy работу, осторожно — write-heavy (конфликты правок).

### 3.2. Фоновые задачи, worktree, очередь

- **Фоновые терминалы внутри сессии:** unified exec (сессии команд), в TUI — `/ps` (список фоновых терминалов) и `/stop` (остановить) — по сводкам слэш-команд, сверенным с докой; статус **частично подтверждён** (официальная страница developer-commands в ходе исследования не открылась из-за лимита browser-сервиса, см. «слабые места источников»).
- **Worktree:** `codex --worktree` / `/worktree` — сессия в managed Git worktree; с 0.162.0 — managed worktree tools (create/list из trusted проектов). По первоисточнику уровня changelog — **частично подтверждено** (changelog 0.162.0 в зеркалах; в вычитанных страницах доков отдельной страницы worktree не нашлось).
- **Очередь сообщений:** `codex queue`, в 0.166.0 добавлен `codex queue --resume` — возобновить выгруженный тред после постановки сообщения в очередь (GitHub Release rust-v0.166.0, данные на 2026-10-31).
- **Фоновый демон app-server:** с 0.157.0 интерактивный запуск поднимает managed-daemon `codex app-server --managed-daemon`, переживающий закрытие терминала, с копией пакета в `~/.codex/packages/app-server-daemon/releases/` (~374 МБ в 0.157.0 по issue #48195). Это **подтверждено GitHub issue #48195** (https://github.com/openai/codex/issues/48195, открыто 2026-09-25) и вызвало требование сделать `daemon_auto_start` opt-in. Для Лунапарка — анти-паттерн-пример: «лёгкий CLI» обзавёлся постоянным самообновляющимся демоном.

### 3.3. Codex Cloud и `codex exec` как параллельность «вширь»

- `codex cloud`: просмотр/сабмит задач из терминала; `codex cloud exec --env <ENV_ID> [--branch ...] [--attempts N]`, `codex cloud list`, `codex cloud diff`, применение результата локально — `codex cloud apply` / `codex apply <TASK_ID>`. Источник: CLI page https://learn.chatgpt.com/docs/codex/cli.md («Move work to Codex cloud») и страница Cloud; точный синтаксис `cloud exec/apply/attempts` сверен по вторичным источникам, проверившим `--help` 0.157.1 — **частично подтверждён**.
- Облачная задача: изолированный контейнер OpenAI, клон репозитория с remote, двухфазная модель в legacy-облаке (setup с сетью → agent phase offline по умолчанию; секреты setup удаляются перед агентной фазой).
- Скриптовый fan-out: N × `codex exec` в N worktree — поддерживаемый паттерн, но оркестрация — на пользователе; встроенного «роя с барьером слияния» нет, барьер — это `wait_agent` в рамках одной сессии субагентов.

---

## 4. Расширения

### 4.1. Хуки — есть, богатые

Источник: https://learn.chatgpt.com/docs/hooks.md (в мануале — раздел Hooks), данные на 2026-10-31.

**События (12+):**

| Когда | События |
|---|---|
| Внутри хода | `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `SubagentStop`, `Stop` |
| Прерывание хода | `Interrupt` (не для субагентов) |
| Старт сессии/субагента | `SessionStart`, `SubagentStart` |
| Конец главного треда | `SessionEnd` (не для субагентов) |

- Хендлеры: `type = "command"` (shell-команда, JSON на stdin/stdout) и `type = "mcp_tool"` (вызов тула уже подключённого MCP-сервера); `prompt`/`agent` хендлеры парсятся, но **пропускаются**.
- Матчеры: по имени тула. `PreToolUse`/`PostToolUse` покрывают Bash, `apply_patch` (матчеры `apply_patch|Edit|Write`), MCP-тулы (`mcp__server__tool`) и «other local function tools» (`update_plan`; `spawn_agent` = `Agent`). `PreToolUse` может заблокировать вызов или переписать вход (`updatedInput`); для Bash/`apply_patch` переписанный вход обязан содержать строковый `command`.
- Где лежат: `hooks.json` или inline `[hooks]`/`[[hooks.<Event>]]` в `config.toml` рядом с активными слоями конфига: `~/.codex/hooks.json`, `~/.codex/config.toml`, `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`; плюс хуки из плагинов. Все слои **мерджатся**, высший не затирает низшие. В одном слое оба представления — merge + warning.
- Trust: non-managed хуки требуют ревью и доверия по хэшу определения (`/hooks` в TUI); изменился хук — снова ревью, до этого пропускается. Managed (system/MDM/`requirements.toml`) — доверены политикой, не отключаются из UI. Для автоматизации — `--dangerously-bypass-hook-trust`.
- Таймауты: по умолчанию 600 с; `SessionEnd`/`Interrupt` — 1 с (макс. 3 с). Поддерживается `async = true` (фон), `statusMessage`, `additionalContextLimit` (сверх лимита вывод хука пишется на диск, модели уходит превью). Несколько совпавших command-хуков одного события запускаются **конкурентно**.
- Оговорка enterprise: при cloud-оркестрации (Work Cloud) command/local/plugin хуки не поддерживаются, только admin MCP hooks на координаторе.

### 4.2. Скиллы — есть, стандарт Agent Skills

Источник: https://learn.chatgpt.com/docs/build-skills.md, данные на 2026-10-31.

- Формат: директория с `SKILL.md` (YAML frontmatter, обязательны `name` и `description`) + опциональные скрипты/референсы/ассеты; опциональный `agents/openai.yaml` (UI-метаданные, `policy.allow_implicit_invocation`, зависимости-тулы). Базируется на open agent skills standard (agentskills.io).
- Progressive disclosure: в начальный список попадают только имя/описание/путь; бюджет списка — ≤2% контекста модели или 8 000 символов при неизвестном окне, настраивается `skills.max_context_tokens` (до 10 000 токенов); полный `SKILL.md` читается только при выборе скилла.
- Активация: явно — `/skills` или `$<skill>` в CLI (`$skill-creator`, `$skill-installer` — встроенные системные); неявно — по совпадению задачи с `description`.
- **Пути загрузки (важно, здесь часто путают):**

  | Scope | Путь дословно |
  |---|---|
  | REPO | `$CWD/.agents/skills`, далее вверх до `$REPO_ROOT/.agents/skills` (каждый каталог по пути) |
  | USER | `$HOME/.agents/skills` |
  | ADMIN | `/etc/codex/skills` |
  | SYSTEM | bundled with Codex |

  То есть актуальный пользовательский путь — **`~/.agents/skills`**, а не `~/.codex/skills`. Путь `~/.codex/skills/...` встречается в примерах app-server JSON и в старых материалах (например, `"path": "/Users/me/.codex/skills/skill-creator/SKILL.md"`) — на 2026-10-31 это **устаревший/примерный** путь; первоисточник Build skills даёт таблицу выше. Совпадение имён скиллов не мерджится — оба видны в селекторе; симлинки на папки скиллов поддерживаются.
- Отключение без удаления: `[[skills.config]] path = ".../SKILL.md", enabled = false` в `~/.codex/config.toml` + рестарт.
- Дистрибуция: плагины (см. 4.4); кураторские скиллы — репозиторий https://github.com/openai/skills.

### 4.3. Слэш-команды

Источник: страница Developer commands (https://learn.chatgpt.com/docs/developer-commands?surface=cli) — в ходе исследования открыть не удалось (429 browser-сервиса); состав ниже собран из страниц CLI/Config/Subagents/Memories/Hooks/Custom prompts первоисточника и помечен там, где сверка только вторичная.

**Подтверждены по первоисточнику (страницы выше):** `/init` (каркас `AGENTS.md`), `/status` (конфиг сессии и токены), `/permissions`, `/model`, `/plan` (Plan mode), `/review`, `/diff`, `/subagents`, `/skills`, `/hooks`, `/mcp`, `/mcp login <name>` (добавлена в 0.161.0 по changelog), `/memories`, `/theme`, `/prompts:<name>` (кастомные промпты), `/compact`, `/resume`, `/fork`, `/new`, `/clear`, `/copy`, `/goal`, `/keymap`, `/ps`, `/stop`, `/pwd`, `/exit`, `/side` (эфемерный сайд-тред), `/agent` (переключение тредов агентов).

**По вторичным сводкам, сверившим 0.163.0/0.166.0 (частично подтверждено):** всего 50+ (в одной сводке — 57) пунктов меню, набор зависит от плана/платформы; новые в октябре 2026: `/rename`, `/delete` (удаляет сессию и потомков), `/recap` (саммари без компакта), `/export` (транскрипт в Markdown), `/presets` (10 переиспользуемых слотов из 0.164.0), `/statusline`, `/title`, `/pets`/`/pet`, `/tui`, `/raw`, `/setup-default-sandbox` (только Windows), `/daybreak` (только eligible аккаунты). Команду, набранную во время хода, можно поставить в очередь клавишей Tab.

**Свои команды:** два механизма —
1. **Custom prompts (deprecated):** `~/.codex/prompts/*.md` → `/prompts:<имя_файла>` (см. §8.5). Официально deprecated в пользу скиллов.
2. **Скиллы как команды:** `$<skill>` — рекомендуемый путь. Произвольных «своих слэш-команд с кодом» вне этих двух механизмов нет.

### 4.4. Плагины

- Плагин = пакет: манифест (`plugin.json` / `.codex-plugin/`), скиллы, MCP-конфиги/серверы, lifecycle hooks, presentation assets. Источник: Build skills / Build plugins (developers.openai.com/plugins), данные на 2026-10-31.
- Установка в CLI: `codex plugin marketplace add <owner/repo | url | local path> [--ref ...] [--sparse ...]`, затем `codex plugin add <plugin>@<marketplace>`, список/удаление — `codex plugin list/remove`, апгрейд каталога — `codex plugin marketplace upgrade`; в TUI — `/plugins`. Команды сверены по гайдам, проверявшим 0.159.2, и по докам плагинов ChatGPT; для CLI как первоисточник — **частично подтверждено** (отдельная CLI-страница плагинов в вычитанных доках не открывалась).
- В 0.146.0–0.147.0 (июль 2026) по changelog: Agent Plugins publishing/marketplaces, Portable Agent Plugins. Плагины работают в CLI, desktop app и Chat/Work; universal plugin directory общий с ChatGPT.

### 4.5. MCP

Источник: https://learn.chatgpt.com/docs/extend/mcp.md, данные на 2026-10-31.

- **Клиент:** транспорты **STDIO** (локальный процесс: `command`, `args`, `env`, `cwd`) и **Streamable HTTP** (`url`, `bearer_token_env_var`, заголовки). Авторизация HTTP: Bearer token, **OAuth** (включая CIMD и Dynamic Client Registration), ChatGPT session auth для first-party серверов; логин из TUI — `/mcp login <name>` (0.161.0). Читается `instructions` сервера (первые 512 символов — самые важные). Конфиг — `[mcp_servers.<name>]` в `config.toml` (общий для CLI/IDE/desktop одного хоста), управление — `codex mcp add/list/get/remove/login/logout`; `codex mcp add openaiDeveloperDocs --url https://developers.openai.com/mcp` — канонический пример из доки. Поля сервера: `enabled`, `required` (если true и сервер не поднялся — `codex exec` падает), `startup_timeout_sec`, `tool_timeout_sec`, фильтры тулов (allow/deny — в Config Reference).
- **Сервер:** `codex mcp-server` и отдельный бинарь `codex-mcp-server` **удалены**. Официальная замена — **Codex App Server** (JSON-RPC протокол встраивания: auth, история, approvals, стриминг событий). Прямая цитата SDK-страницы: «The `codex mcp-server` command and standalone `codex-mcp-server` binary have been removed. Use the Codex app server…» — https://learn.chatgpt.com/docs/codex-sdk.md (данные на 2026-10-31). В старых статьях 2025 года `codex mcp-server` ещё «есть» — это мёртвый факт.

### 4.6. Headless, SDK, ACP, App Server

- **Headless:** `codex exec` (alias `codex e`): прогресс в stderr, финальное сообщение в stdout; `--json` → JSONL-события (`thread.started`, `turn.started/completed/failed`, `item.*` — agent_message, reasoning, command_execution, file_change, mcp_tool_call, web_search, plan update — и `error`, с usage в `turn.completed`); `--output-schema <file>` — финальный ответ по JSON Schema; `-o/--output-last-message <file>`; `--ephemeral` — не писать rollout; stdin-пайп = дополнительный контекст; `codex exec resume [--last | <SESSION_ID>]`; требует git-репозиторий (`--skip-git-repo-check` — обход). Источник: https://learn.chatgpt.com/docs/non-interactive-mode.md.
- **Codex SDK:** TypeScript — `npm install @openai/codex-sdk` (Node ≥18, `new Codex()`, `startThread()`, `thread.run()`, `resumeThread(id)`); Python — `pip install openai-codex` (Python ≥3.10, управляет локальным app-server по JSON-RPC, `Sandbox.read_only / workspace_write / full_access`, есть `AsyncCodex`; stable, для пререлизов `--pre`). Источник: https://learn.chatgpt.com/docs/codex-sdk.md. Отдельно — Codex Security SDK/CLI (`npx @openai/codex-security`) — смежный продукт, не ядро CLI.
- **App Server:** `codex app-server` — JSON-RPC поверхность для своих клиентов (IDE и т.д.) + managed-daemon режим (см. 3.2). Протокол описан в https://learn.chatgpt.com/docs/app-server.md.
- **ACP (Agent Client Protocol): нативно — нет.** Codex сам ACP не говорит; официальный путь — внешний адаптер `@agentclientprotocol/codex-acp` (проект Agent Client Protocol / исторически Zed, `codex-acp`), одобренный в openai/codex#2785 (по вторичным источникам со ссылкой на issue; сам issue в ходе исследования не открывался — **частично подтверждено**). Внутри репозитория есть `codex-rs/acp` следы в сторонних индексах кода — статус нативной ACP-поддержки на 2026-10-31: **не подтверждено**, считать «нет, только адаптер».
- **GitHub Action:** `openai/codex-action@v1` — официальный способ гонять `codex exec` в CI без засветки ключа (Responses API proxy). Источник: Non-interactive mode + репозиторий openai/codex-action.

---

## 5. Контекст и сессии

### 5.1. Сжатие (compact)

- **Автоматически:** при приближении к окну модели; порог — `model_auto_compact_token_limit` (по умолчанию — модельный), область подсчёта — `model_auto_compact_token_limit_scope = "total" | "body_after_prefix"` (Config Reference / sample config, данные на 2026-10-31). Срабатывают хуки `PreCompact`/`PostCompact`. В rollout пишется запись типа `compacted` с саммари и `replacement_history`; при resume воспроизводится только последняя компакция + всё после неё (формат rollout — см. 5.3; детали формата по первоисточнику-доке **не описаны**, выведены из исходников/сторонних разборов — **частично подтверждено**).
- **Вручную:** `/compact` — «summarize earlier context». Есть также `/recap` — саммари **без** компакта (октябрь 2026, частично подтверждено).
- Жалобы на компакт — см. §10 (issue #19585: долгий/нестабильный компакт жжёт недельную квоту Pro и может сделать сессию непригодной).

### 5.2. Resume, fork, ветвления

- `codex resume` — пикер последних сессий (по текущему репозиторию; поиск по всем — `--all` по вторичным сводкам), `codex resume --last`, `codex resume <SESSION_ID>`.
- `codex fork [--last | <SESSION_ID>]` и `/fork` внутри сессии — новый тред из транскрипта, оригинал сохраняется. Esc Esc на пустом вводе — редактирование прошлого промпта с форком в этой точке (prompt edit из DevDay-обновления; частично подтверждено). В 0.165.0 форки «attached threads» оставались experimental.
- В 0.165.0/0.166.0 пофиксили сохранение approval/permission настроек и учёта reasoning-токенов при resume/fork (иначе — преждевременный компакт).
- `codex archive / unarchive / delete` сессий — с линейки 0.136+ по стороннему разбору файлов; `/delete` внутри TUI — октябрь 2026. Статус: **частично подтверждено**.
- Редактирование промпта задним числом в 0.159+ откатывает тред к этой точке.

### 5.3. Хранение истории

- `CODEX_HOME` по умолчанию `~/.codex`. Транскрипты (rollout): `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl` — **путь не задокументирован** в официальной доке на 2026-10-31 (подтверждён наблюдением на диске и исходниками `codex-rs/core/src/rollout/recorder.rs` через вторичные разборы; помечаю как подтверждённый фактически, но не как контрактный API).
- `~/.codex/history.jsonl` — только тексты вводов (при включённой `[history] persistence`; `max_bytes` ограничивает именно этот файл, **не** rollout'ы). `history.persistence = "none"` транскрипты **не** останавливает — известная ловушка.
- `~/.codex/auth.json`, `~/.codex/log/` (или `log_dir`), `~/.codex/logs_2.sqlite` (диагностический SQLite-лог, герой §10), `~/.codex/memories/`, `~/.codex/packages/app-server-daemon/`, `~/.codex/pets/`, sqlite-домашний каталог `sqlite_home`.
- Автоудаления rollout по возрасту задокументировано нет — лежат, пока не удалишь руками/`codex delete`.

### 5.4. Откат изменений

- **Встроенного undo/rewind файлов в CLI нет.** Дока предписывает Git-чекпоинты до/после задачи («Create Git checkpoints before and after a task…» — CLI page / best practices). Откат контекста частично решается fork/edit промпта, откат файлов — только `git restore` снаружи.
- Issue #6449 «Why doesn't the terminal version support code and context rollback?» — давний запрос фичи (https://github.com/openai/codex/issues/6449). Сообщество делает внешние `codex-rewind`-патчи — это **не** upstream.

### 5.5. Память (memories)

Источник: https://learn.chatgpt.com/docs/customization/memories.md, данные на 2026-10-31.

- Локальные memories: `features.memories = false` по умолчанию, maturity — **Experimental**. В 0.165.0 changelog заявляет «memory v2 enabled by default for fresh profiles» — противоречие с докой разрешается так: для свежих профилей v2 может включаться, глобальный документированный дефолт — off; помечаю дефолт v2 как **частично подтверждённый**.
- Файлы: `~/.codex/memories/` (summaries, durable entries, recent inputs, evidence). Генерация — в фоне, после простоя чата, с редактой секретов; пропускается при низком остатке квоты (`memories.min_rate_limit_remaining_percent`). Настройки: `memories.generate_memories`, `memories.use_memories`, `memories.disable_on_external_context` (MCP/web в чате ⇒ чат не идёт в память), `memories.extract_model`, `memories.consolidation_model`. Управление по чату — `/memories`. Ручное редактирование файлов памяти дока **не** рекомендует как основной интерфейс.

---

## 6. Интерфейс (TUI)

- **Стек:** Rust, крейт `codex-rs/tui`, рендер на **ratatui** (по исходникам репозитория; официальная дока ratatui не называет — факт из исходного кода, **подтверждён по репозиторию** через индексы кода). Полноэкранный alternate screen; есть inline-режим `--no-alt-screen` и `/tui` переключение fullscreen/scrollback, `/raw` — сырой scrollback для копирования (частично подтверждено).
- **Состав экрана:** транскрипт (Markdown-рендер, подсветка fenced-кода и диффов), статус-индикатор над composer'ом (спиннер-«shimmer», текст статуса, elapsed, подсказка прерывания), нижняя статусная строка/футер: настраивается `/statusline` (модель, effort, контекст, git, квоты — точный набор items по доке **не подтверждён**, есть ключ `tui.*` в Config Reference), composer с очередью сообщений, оверлеи approvals от субагентов.
- **Темы:** `/theme` — пикер, выбор сохраняется в `tui.theme` в `$CODEX_HOME/config.toml`; свои темы — `.tmTheme` файлы в `$CODEX_HOME/themes`. Источник: https://learn.chatgpt.com/docs/cli-customization.md (данные на 2026-10-31).
- **Клавиши:** `Ctrl+G` — внешний редактор (`VISUAL`/`EDITOR`); `Ctrl+C`/`Ctrl+D` на пустом вводе — выход; `Ctrl+U` — очистить ввод; Esc Esc — edit+fork; Tab — очередь сообщения/команды во время хода; `Alt/,` и `Alt/.` (Option на macOS) — шаг reasoning effort; leader keys настраиваются (0.163.0), вся карта — `[tui.keymap.*]` в конфиге и `/keymap`. Есть Vim mode (по вторичной сводке, частично подтверждено).
- **Картинки:** `codex --image <file,...>` при старте, вставка из буфера в composer (в 0.165.0 на Windows починили Ctrl+V/Shift+Insert для текста и картинок; image previews в терминале — 0.164.0); `view_image` — обратный ход (модель смотрит локальный файл).
- **Компаньоны/пасхалки:** **Pets** — терминальный питомец `/pets` (`/pets <name>`, `/pets off`): анимированный спрайт статуса (running / waiting / ready / blocked), нужен терминал с Kitty graphics/Sixel или iTerm2 ≥3.6, в tmux/Zellij не показывается (по вторичному разбору). Свои питомцы: `pet.json` + `spritesheet.webp` в `~/.codex/pets/<name>/`, генерация — скилл `hatch-pet`. В desktop app питомец — плавающий оверлей (`/pet`, Settings → Appearance → Pets, 8 встроенных). Официальная страница Pets существует в доке (learn.chatgpt.com/docs/pets в индексе llms.txt), в ходе исследования не открывалась — **частично подтверждено**. Фразы спиннера: в исходниках статус-индикатор описан как «whimsical» spinner; **конкретный список фраз по первоисточнику на 2026-10-31 не подтверждён** (не нашёл публикуемого перечня; в транскрипте статус обычно — глагол вида Working + таймер).
- **Голос:** с 0.156.0 voice conversations в CLI (F8 toggle, `/voice`), отдельный пакет `openai-codex-voice` в дистрибутивах. Для CLI-доки — **частично подтверждено** (DevDay/changelog).
- **Проблемы рендера из issues:** пачка жалоб относится к **desktop app** (Chromium), не к TUI: #52631 — V8 heap OOM краши macOS app; #52786/#51870 — лаг UI. По самому TUI: в 0.162.1 чинили краш TUI на многострочных async questions (#51866); в community-тредах — «TUI fails during account/read workspace requirements bootstrap (-32603)». Богатой публичной статистики именно ratatui-глитчей (flicker/развал при resize) по первоисточнику на дату среза **не подтверждено** — в доке таких разборов нет, а поиск по issues упирался в desktop-шум.

---

## 7. Модели

Источники: https://learn.chatgpt.com/docs/models.md, Config Advanced (Custom model providers, OSS, Azure, Bedrock), Config Reference (данные на 2026-10-31).

### 7.1. Каталог и дефолты (ChatGPT-auth)

- Дефолт с 0.159.1: **GPT-6.1 Sol** (`gpt-6.1-sol`) — near-Astra по качеству, дешевле Astra; дефолтное окно в Codex — **500 000 токенов** (поднято 2026-10-11 с 272k, конфигурируется до 1M).
- Семейство: **GPT-6 Astra** (`gpt-6-astra`, топ; есть Astra Ultrafast — Pro $500/Enterprise), **GPT-6.1 Sol**, **GPT-6.1 Terra** (`gpt-6.1-terra`, с 0.159.0+; быстрая, зрение + computer use), **GPT-6 Luna** (`gpt-6-luna`, самая дешёвая/быстрая для узких задач, в т.ч. субагентов), **GPT-6 Sol** (предыдущий слой каталога).
- Ретайры: GPT-5.5 убран из ChatGPT-auth Codex 2026-10-14 12:00 PDT (API-key сессии не затронуты); GPT-5.3-Codex-Spark deprecated 2026-09-14.
- `codex-auto-review` — служебная модель авто-ревью approvals (не выбирается пользователем как рабочая).
- Переключение: `/model` в сессии, `-m/--model` при запуске и в `codex exec`; смена effort на лету — поддерживается (0.165.0 — включено по умолчанию для поддерживаемых моделей).

### 7.2. Reasoning effort

- Шкала зависит от модели: `low / medium / high / xhigh / max / ultra` (у части моделей нет `none` или нижних ступеней). Ключи: `model_reasoning_effort`, отдельный `plan_mode_reasoning_effort` для Plan mode, `model_reasoning_summary`, `model_verbosity`. Ultra в Work — ещё и проактивная делегация субагентам.
- План/доступность моделей завязаны на тариф ChatGPT (Plus/Pro/Business/Edu/Enterprise) или на API key (тогда биллинг API; облачные фичи типа GitHub review и Slack с API key недоступны).

### 7.3. Провайдеры

- Встроенные: `openai` (базу можно перекинуть `openai_base_url` — прокси/роутер/data-residency, не создавая провайдера), `amazon-bedrock` (встроенный, только profile/region override), `gcp` (Google Cloud Vertex AI, с 0.165.0), зарезервированные id `ollama`, `lmstudio` (нельзя переопределять кастомно).
- **OSS локально:** `codex --oss [--local-provider ollama|lmstudio]`, дефолт — `oss_provider = "ollama" | "lmstudio"` в конфиге; интерактив спросит, если не задан, `codex exec --oss` без провайдера — ошибка. Классический кейс — gpt-oss-20b/120b через Ollama/LM Studio.
- **Кастомные:** `[model_providers.<id>]` — `name`, `base_url`, `env_key`, `wire_api = "responses"`, `http_headers`/`env_http_headers`, `query_params`, command-auth (`[model_providers.<id>.auth]` — внешняя команда за токеном, refresh_interval), native gateway OAuth (`gateway_oauth`), `requires_openai_auth`. **Azure** — шаблоном: `[model_providers.azure] base_url = "https://<PROJECT>.openai.azure.com/openai"`, `env_key = "AZURE_OPENAI_API_KEY"`, `query_params = { api-version = ... }`.
- Выбор: `model_provider = "<id>"` + `model = "..."`. Проектный `.codex/config.toml` провайдеров переопределять **не может** (ключи игнорируются с warning).
- Каталог моделей можно подменять: `model_catalog_json` (свой JSON каталога, в т.ч. в профиле).
- Зрение: модели Sol/Terra/Luna в каталоге — с image understanding (Terra прямо позиционируется на visual); вход — `--image`/`view_image`. Для OSS/кастомных провайдеров зрение зависит от модели/эндпоинта и докой не гарантируется — **не подтверждено** в общем виде.
- Service tier / Fast mode: `service_tier = "fast"`, `features.fast_mode`.

---

## 8. Совместимость — форматы и пути (дословно)

### 8.1. `AGENTS.md`

Источник: https://learn.chatgpt.com/docs/agent-configuration/agents-md.md (данные на 2026-10-31).

- Глобально: `$CODEX_HOME/AGENTS.override.md` если есть и непуст, иначе `$CODEX_HOME/AGENTS.md` (по умолчанию `~/.codex/AGENTS.md` / `~/.codex/AGENTS.override.md`); берётся **только первый непустой** файл уровня.
- Проект: от корня проекта (обычно git root; маркеры — `project_root_markers`, дефолт `.git`) вниз до cwd; в **каждом** каталоге проверяется по порядку: `AGENTS.override.md` → `AGENTS.md` → имена из `project_doc_fallback_filenames`; не более одного файла на каталог.
- Слияние: конкатенация root→cwd через пустые строки; ближние к cwd стоят позже и фактически переопределяют. Пустые файлы пропускаются; общий лимит — `project_doc_max_bytes` = **32 KiB** по умолчанию.
- Цепочка строится один раз при старте (в TUI — на сессию), кэша для ручной чистки нет.
- Секция `## Code Review Rules` в ближайшем `AGENTS.md` используется GitHub-ревьюером Codex.
- `/init` генерирует каркас `AGENTS.md`.

### 8.2. Конфиги

- Пользовательский: `~/.codex/config.toml` (точнее `$CODEX_HOME/config.toml`).
- Проектные: `.codex/config.toml` в каталогах от корня до cwd (только trusted проекты; относительные пути внутри — от папки `.codex/`).
- Профили: `~/.codex/<name>.config.toml` + `--profile <name>`.
- Системный: `/etc/codex/config.toml`; managed: `requirements.toml` (путь зависит от канала доставки политики; в репозитории организации/системы).
- MCP внутри того же `config.toml`: `[mcp_servers.<name>]` (см. 4.5).
- Авторизация: `~/.codex/auth.json` (file-based storage; содержит access tokens — «treat like a password») или OS keychain/keyring; env для CI — `CODEX_API_KEY` (и `OPENAI_API_KEY` в ряде путей).
- Прочее в `$CODEX_HOME`: `history.jsonl`, `log/` (`log_dir`), `sessions/`, `memories/`, `skills`-эпоха сменилась на `~/.agents/skills` (см. 4.2), `themes/`, `pets/`, `agents/` (кастомные агенты), `rules/`, `prompts/`, `hooks.json`, `model-catalogs/` (пример пути каталога из доки профиля).

### 8.3. Кастомные агенты

`~/.codex/agents/*.toml` и `.codex/agents/*.toml` — по одному агенту на файл (`name`, `description`, `developer_instructions` обязательны). См. §3.1.

### 8.4. Rules

- Файлы: `rules/*.rules` рядом с активными слоями конфига: `~/.codex/rules/default.rules` (сюда TUI пишет «allow» из approval-диалога), `<repo>/.codex/rules/*.rules` (только trusted), team config locations.
- Язык: Starlark; единственная документированная конструкция — `prefix_rule(pattern = [...], decision = "allow"|"prompt"|"forbidden", justification = "...", match = [...], not_match = [...])`; при нескольких совпадениях побеждает самое строгое (`forbidden` > `prompt` > `allow`); `bash -lc`-цепочки из простых команд разбираются tree-sitter'ом и проверяются по частям, сложные (редиректы, подстановки, переменные) — как единое целое.
- Проверка: `codex execpolicy check --pretty --rules ~/.codex/rules/default.rules -- <cmd...>`.

### 8.5. Промпты (deprecated)

Источник: https://learn.chatgpt.com/docs/custom-prompts.md (данные на 2026-10-31).

- Путь дословно: `~/.codex/prompts/*.md`, **только верхний уровень** папки, не-Markdown игнорируется.
- Frontmatter: `description:`, `argument-hint: [KEY=<...>]`; плейсхолдеры `$1`–`$9`, `$ARGUMENTS`, именованные `$FILE` (значения `KEY=value`), `$$` — литеральный доллар.
- Вызов: `/prompts:<имя>`; изменения подхватываются после рестарта сессии. Статус — **deprecated**, замена — скиллы.

### 8.6. Хуки и скиллы — пути

- Хуки: `~/.codex/hooks.json`, `~/.codex/config.toml` (`[hooks]`), `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`; скрипты принято класть в `.codex/hooks/*.py` (примеры доки используют `$(git rev-parse --show-toplevel)/.codex/hooks/...`).
- Скиллы: `.agents/skills` (repo, по цепочке вверх), `$HOME/.agents/skills` (user), `/etc/codex/skills` (admin) — см. таблицу в 4.2.

---

## 9. Вес и старт

- **Локальный замер исполнителя, 2026-10-31** (облачная ВМ, Linux x86_64): установлена `codex-cli 0.164.0-alpha.9`; бинарь `/opt/hatch-image/bin/codex` — **306 508 968 байт (~306 МБ)**, статический ELF; `codex --version` — **10–130 мс** по 5 запускам (это только печать версии, **не** старт TUI и не готовность к работе).
- npm `@openai/codex` — тонкая обёртка, тянет платформенный пакет с тем же Rust-бинарём (плюс в поставке 0.157+: `codex-code-mode-host`, bundled `bwrap`/`rg` и др. — состав по issue #48195). Точный unpacked size npm-пакета по первоисточнику (npmjs) в ходе исследования **не подтверждён**; вторичная оценка «~50 MB npm» из гайдов явно занижена относительно реального бинаря и не согласуется с замером — не использовать.
- Дистрибутивная сверка (не первоисточник, но реестр пакетов): Arch `openai-codex 0.166.0-1` — package 92.3 MB (сжатый), **installed 355.3 MB** — порядок совпадает с локальным замером бинаря + спутники.
- Демон app-server с 0.157.0 **копирует ещё ~374–431 МБ** в `~/.codex/packages/app-server-daemon/releases/<version>/` (issue #48195: ~374 MB в 0.157.0; вторичный аудит: 431.5 MB к 0.162.1) и самообновляется по таймеру — фактический дисковой след «лёгкого» CLI на машине легко превышает 700 МБ.
- Время старта TUI и потребление RAM: официальных цифр OpenAI **не публикует** (не подтверждено). По issues есть только патологические кейсы desktop app (не CLI): MainThread 10–12 GiB RSS и OOM на Ubuntu (#50567, относится к desktop/CUA Node, не к Rust TUI — не экстраполировать на CLI). Для CLI честный публичный замер на дату среза — только локальный выше (`--version`), TUI-start никто из первоисточников не мерял.
- Частота релизов: 1 257 релизов в репозитории, в октябре 2026 стабильные выходили каждые 2–4 дня (0.162.1 → 0.166.0 за 20 дней) — «эталон» движется быстрее, чем успевает документация.

---

## 10. Что бесит — жалобы из GitHub issues

Все ссылки — https://github.com/openai/codex/issues/<N>, данные на 2026-10-31.

1. **SSD-логгер (главный скандал 2026 года).** #28224 «Codex SQLite feedback logs can write ~640 TB/year and rapidly consume SSD endurance»: TRACE-логгер в `~/.codex/logs_2.sqlite` игнорировал `RUST_LOG`, писал ~37 TB за 21 день у автора; связанные #17320 (WAL writes 5–16 MB/s при стриминге), #29876. Фиксы шли раундами 0.142.0–0.145.0, но претензии к отсутствию ротации/капа/выключателя у лог-БД сохранялись и в октябрьских аудитах.
2. **Самообновляющийся демон.** #48195: с 0.157.0 интерактивный запуск ставит persistent daemon, копирует ~374 MB в `~/.codex/packages/`, обновляется сам (curl install.sh в sh), переживает терминал; требование — opt-in. Рядом #48050: 0.157.0 перестал стартовать из Arch-пакета без `--no-daemon`.
3. **Лимиты и учёт квот.** #19585: недельная квота Pro ($200) сгорала за ~2 дня на 5.5, усугублено долгим/падающим компактом. #23994: баннер «usage limit reached» при 83%/97% остатка квот. #48650: после исчерпания нативной квоты desktop блокирует отправку даже в сторонние модели с собственной квотой (связанные #48369, #47344 — серая кнопка Send при 0% usage). В community — массовые треды «usage draining really fast after reset».
4. **Компакт.** #19585 (см. выше): авто-компакт долгий, нестабилен, при падении сессия мертва, а попытки жгут квоту; в community — «Premature auto-compaction».
5. **Нет отката.** #6449: нет ни code rollback, ни context rollback в терминальной версии; «без этого писать код бесполезно» — давний открытый запрос.
6. **Регрессии и краши desktop/TUI-стека.** #52631 (V8 OOM, macOS app 26.1007), #50567 (10–12 GiB OOM, Linux desktop), #52786/#51870 (лаг UI), TUI crash на async questions (починен 0.162.1, #51866). Объём трекера: в community жалуются на ~19k открытых issues и 20–30 новых в час — триаж не успевает (цифра — из community-треда, **частично подтверждено**).
7. **Тормоза.** Community-треды октября 2026: «extremely slow responses and code generation», лаги ввода 5–6 с в объединённом ChatGPT/Codex app; часть лечится `--disable-gpu` (признак Chromium-композитора, desktop, не CLI).
8. **Настройка и сюрпризы обновлений.** Смена дефолтной модели молча при апдейте (0.159.1 — GPT-6.1 Sol стал дефолтом «одной строкой changelog»); ретайр моделей внутри подписки (GPT-5.5 — 2026-10-14); ломающие переезды конфига за год: `[profiles.*]` → отдельные файлы (0.134.0), `untrusted` → retired (может не дать клиенту стартовать), `--full-auto` удалён, `codex mcp-server` удалён, пути скиллов `.codex/skills` → `.agents/skills`.
9. **Хуки на Windows.** Community: v0.163.0 на Windows — повторные hook timeouts и долгое «Running hooks».
10. **Сеть/песочница на Linux.** `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` — повторяющийся класс жалоб на bubblewrap в ограниченных окружениях (community + issues; конкретный номер в ходе исследования не зафиксирован — **частично подтверждено**).

---

## Итоговая таблица возможностей

| Возможность | Статус | Примечание |
|---|---|---|
| Shell-тул (sandboxed) | есть | `shell/shell_command` + unified exec (`exec_command`/`write_stdin`) |
| `apply_patch` | есть | фирменная Begin/End Patch грамматика |
| Отдельные read/edit/grep/glob тулы | нет | чтение/поиск — через shell, правка — через apply_patch |
| `update_plan` | есть | 1 шаг in_progress максимум |
| `web_search` | есть | cached (дефолт) / indexed / live (`--search`) / disabled |
| `view_image` + `--image` | есть | `tools.view_image`, вставка в composer |
| Approval policies | есть | `on-request` (дефолт) / `never` / granular; `untrusted` retired |
| Sandbox modes | есть | `read-only` / `workspace-write` (дефолт) / `danger-full-access`; Seatbelt / bwrap+seccomp / Windows MXC |
| Permission profiles | есть (частично: beta по вторичке) | `:read-only` / `:workspace` / `:danger-full-access` + `[permissions.*]` |
| Сетевые правила | есть | `network_access` + `features.network_proxy` (allow/deny домены) |
| Конфиг-профили `--profile` | есть | `~/.codex/<name>.config.toml`; старый `[profiles.*]` удалён |
| Execpolicy rules | есть (experimental) | `~/.codex/rules/*.rules`, Starlark `prefix_rule` |
| Субагенты | есть | `spawn_agent/send_input/resume_agent/wait_agent/close_agent`, stable, default on |
| Кастомные агенты | есть | `~/.codex/agents/*.toml`, `.codex/agents/*.toml`; встроенные default/worker/explorer |
| Фоновые задачи | частично | unified exec сессии, `/ps`, `/stop`, `codex queue`; полноценного daemon-менеджера задач нет |
| Worktree | есть (частично подтверждено) | `--worktree`/`/worktree`, managed worktree tools с 0.162.0 |
| Codex Cloud из CLI | есть | `codex cloud [exec/list/diff/apply]`, attempts/best-of-N |
| Headless `codex exec` | есть | JSONL `--json`, `--output-schema`, `-o`, `--ephemeral`, resume |
| Хуки | есть | 12 событий: PreToolUse/PostToolUse/PermissionRequest/PreCompact/PostCompact/SessionStart/SessionEnd/SubagentStart/SubagentStop/UserPromptSubmit/Stop/Interrupt; command + mcp_tool |
| Скиллы SKILL.md | есть | стандарт Agent Skills; пути `.agents/skills`, `~/.agents/skills`, `/etc/codex/skills` |
| Слэш-команды встроенные | есть | 50+ пунктов; свои — только prompts/skills |
| Свои слэш-команды | частично | `~/.codex/prompts/*.md` → `/prompts:*` (**deprecated**); рекомендуемо — скиллы `$name` |
| MCP-клиент | есть | STDIO + Streamable HTTP, Bearer + OAuth (CIMD/DCR), `/mcp login` |
| MCP-сервер (`codex mcp-server`) | нет | **удалён**; замена — Codex App Server (JSON-RPC) |
| Плагины/marketplace | есть | `codex plugin marketplace add`, `codex plugin add`, `/plugins` |
| SDK | есть | TypeScript `@openai/codex-sdk`, Python `openai-codex` |
| ACP | нет (нативно) | только внешний адаптер `@agentclientprotocol/codex-acp` |
| GitHub Action | есть | `openai/codex-action@v1` |
| Авто-компакт + `/compact` | есть | `model_auto_compact_token_limit[_scope]`; `Pre/PostCompact` хуки |
| Resume / fork сессий | есть | `codex resume [--last]`, `codex fork`, `/fork`, Esc Esc edit |
| Откат файлов (undo/rewind) | нет | только Git-чекпоинты снаружи; issue #6449 |
| История сессий на диске | есть | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (недокументировано) |
| Memories | частично | experimental, по умолчанию off; `~/.codex/memories/`, `/memories` |
| AGENTS.md иерархия | есть | global `~/.codex/AGENTS[.override].md` + root→cwd, лимит 32 KiB |
| OSS-модели (Ollama/LM Studio) | есть | `codex --oss`, `oss_provider` |
| Кастомные провайдеры / Azure / шлюзы | есть | `[model_providers.*]`, Azure-шаблон, Bedrock и Vertex AI встроены |
| Reasoning effort | есть | low→ultra, отдельный effort для Plan mode и субагентов |
| TUI: темы/статус-строка/кеймапы | есть | `/theme` (`.tmTheme` в `$CODEX_HOME/themes`), `/statusline`, `[tui.keymap]` |
| Питомцы-компаньоны | есть (частично подтверждено) | `/pets` в CLI (Kitty/Sixel/iTerm2), `~/.codex/pets/` |
| Голос в CLI | есть (частично подтверждено) | с 0.156.0, F8, `/voice` |

---

## Источники (первоисточники — приоритет)

- Документация (зеркало developers.openai.com/codex на 2026-10-31): https://learn.chatgpt.com/docs/codex/cli.md, https://learn.chatgpt.com/docs/codex-manual.md, https://learn.chatgpt.com/docs/agent-approvals-security.md, https://learn.chatgpt.com/docs/config-file/config-basic.md, https://learn.chatgpt.com/docs/config-file/config-advanced.md, https://learn.chatgpt.com/docs/config-file/config-reference.md, https://learn.chatgpt.com/docs/agent-configuration/agents-md.md, https://learn.chatgpt.com/docs/agent-configuration/subagents.md, https://learn.chatgpt.com/docs/agent-configuration/rules.md, https://learn.chatgpt.com/docs/hooks.md, https://learn.chatgpt.com/docs/build-skills.md, https://learn.chatgpt.com/docs/extend/mcp.md, https://learn.chatgpt.com/docs/non-interactive-mode.md, https://learn.chatgpt.com/docs/codex-sdk.md, https://learn.chatgpt.com/docs/custom-prompts.md, https://learn.chatgpt.com/docs/customization/memories.md, https://learn.chatgpt.com/docs/cli-customization.md, https://learn.chatgpt.com/docs/models.md — все данные на 2026-10-31.
- Репозиторий и релизы: https://github.com/openai/codex, https://github.com/openai/codex/releases (rust-v0.164.0…rust-v0.166.0) — данные на 2026-10-31.
- Issues: #6449, #17320, #19585, #23994, #28224, #29876, #48050 (упомянут в #48195), #48195, #48650, #50567, #51866 (фикс в changelog 0.162.1), #52631, #52786 — https://github.com/openai/codex/issues — данные на 2026-10-31.
- npm: `@openai/codex`, `@openai/codex-sdk`; PyPI: `openai-codex` (как дистрибутив SDK, описан в официальной SDK-доке).

### Слабые места источников

1. `browser.open` для `learn.chatgpt.com/docs/developer-commands` и GitHub Releases в ходе исследования падал с 429 на стороне browser-сервиса: полный официальный список слэш-команд и построчные release notes 0.164–0.166 сверены по мануалу и зеркалам changelog, а не по самой странице — соответствующие пункты помечены «частично подтверждено».
2. Официальная дока **не публикует**: единый список встроенных тулов, формат и путь rollout-файлов, время старта TUI, расход RAM CLI, список фраз спиннера, unpacked size npm-пакета. Эти места закрыты исходниками/наблюдением и честно помечены.
3. Документация переехала (developers.openai.com → learn.chatgpt.com) и местами сама себе противоречит (memories default, пути скиллов в примерах app-server vs Build skills). При конфликте в отчёте приоритет отдан специализированной странице (Build skills, Memories).
4. Модели/даты после сентября 2026 (GPT-6.x линейка) подтверждаются только актуальной докой OpenAI и changelog; независимых внешних подтверждений у линейки по определению мало — это срез первоисточника, а не консенсус.
