#!/usr/bin/env bash
# Установка Лунапарка: Linux, macOS, Termux. Запускается из папки программы:  bash install.sh
# Удаление команды (данные LUNA_HOME остаются):  bash install.sh --uninstall
# Зависимостей нет — нужен только Python 3.10+. Повторный запуск безопасен (идемпотентен).

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- платформа и папка лаунчера ---------------------------------------------------------

# В Termux команда живёт в $PREFIX/bin (он уже в PATH), в остальных — ~/.local/bin.
if [ -n "${TERMUX_VERSION:-}" ] || { [ -d /data/data/com.termux/files/usr ] && command -v pkg >/dev/null 2>&1; }; then
  PLATFORM="termux"
  BIN_DIR="${PREFIX:-/data/data/com.termux/files/usr}/bin"
elif [ "$(uname -s)" = "Darwin" ]; then
  PLATFORM="macos"
  BIN_DIR="$HOME/.local/bin"
else
  PLATFORM="linux"
  BIN_DIR="$HOME/.local/bin"
fi
LAUNCHER="$BIN_DIR/luna"

fail() {
  echo ""
  echo "  ✗ $1"
  echo "    $2"
  exit 1
}

uninstall() {
  if [ -f "$LAUNCHER" ]; then
    rm -f "$LAUNCHER" && echo "  ✓ Команда luna удалена: $LAUNCHER"
  else
    echo "  Команда luna не была установлена ($LAUNCHER нет) — удалять нечего."
  fi
  echo "  Данные ($HOME/.luna или LUNA_HOME) не тронуты."
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --uninstall) uninstall ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) fail "Неизвестный аргумент: $arg" "Есть только --uninstall и --help." ;;
  esac
done

echo "== Лунапарк: установка =="
echo "  Платформа: $PLATFORM · папка: $HERE"

if [ ! -f "$HERE/luna/__main__.py" ]; then
  fail "Установщик запущен не из папки Лунапарка" "Распакуй архив целиком (или склонируй репозиторий) и запусти install.sh из получившейся папки."
fi

# --- Python -----------------------------------------------------------------------------

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
    PY="$(command -v "$cand")"; break
  fi
done

if [ -z "$PY" ]; then
  case "$PLATFORM" in
    macos) HINT="Поставь Python с python.org или через Homebrew: brew install python — и запусти установку снова." ;;
    termux) HINT="Выполни pkg install python и запусти установку снова." ;;
    *) HINT="Debian/Ubuntu — sudo apt install python3; Fedora — sudo dnf install python3; Arch — sudo pacman -S python. Потом запусти установку снова." ;;
  esac
  fail "Не найден Python 3.10 или новее" "$HINT"
fi
echo "  Python: $("$PY" --version 2>&1) ($PY)"

# --- лаунчер ----------------------------------------------------------------------------
# Лаунчер вызывает найденный Python; папка именно этой копии репозитория —
# первой в sys.path, поэтому неважно, из какого каталога запускают luna.

BASH_PATH="$(command -v bash)"
mkdir -p "$BIN_DIR" || fail "Не удалось создать папку $BIN_DIR" "Проверь права на домашний каталог."
cat > "$LAUNCHER" <<EOF
#!$BASH_PATH
exec "$PY" -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from luna.__main__ import main; sys.exit(main())' "$HERE" "\$@"
EOF
chmod +x "$LAUNCHER" || fail "Не удалось сделать лаунчер исполняемым: $LAUNCHER" "Проверь права на папку $BIN_DIR."
echo "  ✓ Команда: $LAUNCHER"

# --- самопроверка -----------------------------------------------------------------------

if ! OUT="$("$LAUNCHER" --version 2>&1)"; then
  fail "Луна не запускается через лаунчер" "Вывод: $OUT"
fi
echo "  ✓ Проверка: $OUT"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "  Папка $BIN_DIR пока не в PATH — запускай полным путём: $LAUNCHER"
    echo "  Чтобы писать просто «luna», добавь в профиль оболочки (~/.bashrc, ~/.zshrc):"
    echo "    export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo ""
echo "  ✓ Готово! Запуск: luna · Обновление git-копии: luna update · Удаление команды: bash install.sh --uninstall"
echo "  Данные лежат в ${LUNA_HOME:-$HOME/.luna} и при удалении команды не трогаются."
