"""Изоляция bash ячеек роя (ЗАДАЧА-21, docs/РОЙ.md).

Бэкенды — данными: bubblewrap на Linux, podman/docker контейнером.
Общий контракт один: файловая система только для чтения, папка ячейки
(или её worktree) — на запись, свой /tmp, сеть выключена, пока профиль
не скажет `net: true`. Плюс лимиты процесса: время держит тул bash
своим таймаутом и деревом процессов; память и число процессов — rlimit
у bwrap/без-бэкенда и --memory/--pids-limit у контейнеров.

Windows и macOS: Job Object, restricted token, Windows Sandbox и
sandbox-exec настоящей изоляции файловой системы для произвольной
команды без администратора не дают (Job Object — только лимиты,
Windows Sandbox — отдельная ВМ и фича Pro/Enterprise, sandbox-exec
Apple пометила устаревшим и не границей безопасности). Поэтому здесь
честный ответ «изоляции ФС нет»: bash роя там идёт только с
подтверждением человека, как до этой задачи.

Проверка изоляции — пробным запуском: ячейка обязана суметь записать
в свою папку, не суметь записать наружу и (без net) не достучаться до
слушателя на host-loopback. Только `verified=True` разрешает
Предохранителю не спрашивать про bash.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ORDER = ("bwrap", "podman", "docker")

# Бэкенды данными: имя, платформы, исполняемый файл, подсказка установки.
BACKENDS = (
    {"name": "bwrap", "platforms": ("linux",), "executable": "bwrap",
     "title": "bubblewrap",
     "install": "bubblewrap не найден: поставь пакет bubblewrap "
                "(например: sudo apt install bubblewrap) или podman."},
    {"name": "podman", "platforms": ("linux",), "executable": "podman",
     "title": "podman (контейнер)",
     "install": "podman не найден: поставь podman "
                "(например: sudo apt install podman) и подготовь локальный образ."},
    {"name": "docker", "platforms": ("linux", "darwin"), "executable": "docker",
     "title": "docker (контейнер)",
     "install": "docker не найден или демон не отвечает: поставь docker "
                "и подготовь локальный образ."},
)

_CONTAINER_IMAGE_CACHE: dict = {}
_VERIFY_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


@dataclass
class SandboxStatus:
    """Что выбрано настройкой и что реально есть на этой машине."""

    requested: str = "auto"
    backend: str = "none"          # bwrap | podman | docker | none | unavailable
    available: bool = False
    fs_isolated: bool = False
    verified: bool = False
    executable: str = ""
    image: str = ""
    reason: str = ""
    install_hint: str = ""
    pid_ns: bool = True            # рабочий вариант bwrap по пробе (см. verify)

    def describe(self) -> str:
        if self.backend == "none":
            return "нет (" + (self.reason or "выключена") + ")"
        if self.backend == "unavailable":
            return "недоступна (" + self.reason + ")"
        text = self.backend
        if self.image:
            text += ", образ " + self.image
        if self.verified:
            text += ", проверена пробным запуском"
        elif self.available:
            text += ", есть, но пробный запуск не пройден: " + self.reason
        return text


@dataclass
class SandboxSpec:
    """Готовый рецепт для тула bash одной ячейки."""

    backend: str = "none"
    requested: str = "auto"
    workdir: Path = Path(".")
    net: bool = False
    verified: bool = False
    fs_isolated: bool = False
    executable: str = ""
    image: str = ""
    memory_mb: int = 1024
    max_processes: int = 256
    reason: str = ""
    limits: bool = True            # применять ли rlimit (не контейнеру)
    pid_ns: bool = True            # bwrap: свой pid namespace; во вложенных
    # контейнерах с «запертым» /proc хоста связка unshare-pid + свежий
    # /proc не монтируется — пробный запуск подбирает вариант без него

    @property
    def unavailable(self) -> bool:
        return self.backend == "unavailable"

    def wrap(self, command: str) -> tuple:
        """Команда оболочки → (argv, env для процесса-обёртки).

        Для контейнеров env — окружение клиента (ему нужен HOME для
        конфигов), а в сам контейнер значения попадут только явными
        --env ниже: секретов хозяина там нет.
        """
        cwd = str(self.workdir)
        if self.backend == "bwrap":
            env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   "HOME": cwd, "LANG": "C.UTF-8", "TERM": "dumb"}
            argv = [self.executable,
                    # Явный user namespace обязателен: без него bwrap от
                    # root не может поднять loopback в новой сети
                    # (RTM_NEWADDR) — проверено живым запуском.
                    "--unshare-user",
                    "--die-with-parent"]
            if self.pid_ns:
                argv.append("--unshare-pid")
            argv += ["--unshare-ipc", "--unshare-uts",
                    "--cap-drop", "ALL",
                    "--ro-bind", "/", "/",
                    "--dev", "/dev", "--proc", "/proc",
                    "--tmpfs", "/tmp",
                    *_hide_secrets_args(cwd),
                    "--bind", cwd, cwd,
                    "--chdir", cwd,
                    "--clearenv"]
            for key, value in env.items():
                argv += ["--setenv", key, value]
            if not self.net:
                argv.append("--unshare-net")
            argv += ["/bin/sh", "-c", command]
            return argv, env
        if self.backend in ("podman", "docker"):
            argv = [self.executable, "run", "--rm",
                    "--read-only",
                    "--tmpfs", "/tmp:rw,size=256m",
                    "--mount", "type=bind,src=" + cwd + ",dst=/cell",
                    "--workdir", "/cell",
                    "--memory", str(int(self.memory_mb)) + "m",
                    "--pids-limit", str(int(self.max_processes)),
                    "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges",
                    "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "--env", "HOME=/cell",
                    "--env", "LANG=C.UTF-8"]
            if not self.net:
                argv += ["--network", "none"]
            if os.name == "posix" and hasattr(os, "getuid"):
                argv += ["--user", str(os.getuid()) + ":" + str(os.getgid())]
            argv += [self.image, "/bin/sh", "-c", command]
            return argv, dict(os.environ)
        return [command], None  # вызывающий использует shell=True сам


# «/ только для чтения» всё ещё даёт ячейке ЧИТАТЬ сейф Луны и ключи
# хозяина — а прочитанное уйдёт модели в выводе bash. Прячем их поверх.
_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
                ".config/gh", ".config/gcloud", ".password-store",
                ".claude", ".codex", ".gemini")
_SECRET_FILES = (".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc")


def _hide_secrets_args(cwd: str) -> list:
    home = Path(os.path.expanduser("~"))
    dirs = [home / d for d in _SECRET_DIRS]
    luna_home = os.environ.get("LUNA_HOME")
    lh = Path(luna_home).expanduser() if luna_home else home / ".luna"
    # Целиком и по частям: если папка ячейки внутри LUNA_HOME, целиком
    # её не спрятать — тогда хотя бы сейф, журналы и чаты.
    dirs += [lh, lh / "sessions", lh / "logs", lh / "checkpoints"]
    files = [home / f for f in _SECRET_FILES] + [lh / "secrets.json", lh / "mcp.json"]
    args: list = []
    work = Path(cwd)
    for d in dirs:
        # Папку, внутри которой работает сама ячейка, прятать нельзя.
        if not d.is_dir() or d == work or d in work.parents:
            continue
        args += ["--tmpfs", str(d)]
    for p in files:
        if p.is_file():
            args += ["--ro-bind", "/dev/null", str(p)]
    return args


def _platform_name(platform_name: str | None) -> str:
    return platform_name if platform_name is not None else sys.platform


def _backend_row(name: str) -> dict:
    for row in BACKENDS:
        if row["name"] == name:
            return row
    return {}


def _run_quiet(argv: list, timeout: float = 6.0) -> tuple:
    """(код, текст) короткой служебной команды; любая ошибка — код -1."""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    text = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, text.decode("utf-8", errors="replace").strip()


def _container_image(executable: str, name: str) -> str:
    """Первый локальный образ контейнера; сеть не трогаем принципиально.

    Нет локального образа — бэкенд честно недоступен: тянуть образ из
    реестра ради изоляции ячеек Луна не будет.
    """
    key = (name, executable)
    with _CACHE_LOCK:
        if key in _CONTAINER_IMAGE_CACHE:
            return _CONTAINER_IMAGE_CACHE[key]
    image = ""
    code, _text = _run_quiet([executable, "info", "--format", "{{.ServerVersion}}"
                              if name == "docker" else "json"], timeout=8.0)
    if code == 0:
        code, text = _run_quiet([executable, "images", "--format",
                                 "{{.Repository}}:{{.Tag}}"], timeout=8.0)
        if code == 0:
            for line in text.splitlines():
                line = line.strip()
                if line and "<none>" not in line:
                    image = line
                    break
    with _CACHE_LOCK:
        _CONTAINER_IMAGE_CACHE[key] = image
    return image


def detect_backends(platform_name: str | None = None, environ=None,
                    which=None) -> list:
    """Доступность каждого бэкенда: [{name, available, executable, image, reason}]."""
    plat = _platform_name(platform_name)
    env = os.environ if environ is None else environ
    finder = which or shutil.which
    path = env.get("PATH", "") if isinstance(env, dict) else ""
    out = []
    for row in BACKENDS:
        item = {"name": row["name"], "available": False, "executable": "",
                "image": "", "reason": "", "install": row["install"]}
        if not any(plat.startswith(p) for p in row["platforms"]):
            item["reason"] = "платформа " + plat + ": настоящей изоляции ФС без администратора нет"
            out.append(item)
            continue
        exe = finder(row["executable"], path=path) if path else finder(row["executable"])
        if not exe:
            item["reason"] = row["executable"] + " не найден в PATH"
            out.append(item)
            continue
        item["executable"] = exe
        if row["name"] in ("podman", "docker"):
            image = _container_image(exe, row["name"])
            if not image:
                item["reason"] = "демон не отвечает или нет ни одного локального образа"
                out.append(item)
                continue
            item["image"] = image
        item["available"] = True
        out.append(item)
    return out


def _limits(settings) -> tuple:
    memory = 1024
    procs = 256
    if settings is not None:
        try:
            memory = int(settings.get("starfall.sandbox_memory_mb", memory) or memory)
            procs = int(settings.get("starfall.sandbox_processes", procs) or procs)
        except (TypeError, ValueError):
            pass
    return max(16, memory), max(4, procs)


def sandbox_status(settings=None, *, verify: bool = False,
                   platform_name: str | None = None, environ=None,
                   which=None) -> SandboxStatus:
    """Выбранный бэкенд по настройке starfall.sandbox и его состояние."""
    requested = "auto"
    if settings is not None:
        requested = str(settings.get("starfall.sandbox", "auto") or "auto").strip().lower()
    if requested not in ("auto", "bwrap", "podman", "docker", "none"):
        requested = "auto"
    status = SandboxStatus(requested=requested)
    if requested == "none":
        status.reason = "выключена настройкой starfall.sandbox=none"
        return status
    found = {item["name"]: item for item in
             detect_backends(platform_name, environ, which)}
    names = BACKEND_ORDER if requested == "auto" else (requested,)
    chosen = None
    reasons = []
    for name in names:
        item = found.get(name) or {}
        if item.get("available"):
            chosen = item
            break
        reasons.append(name + ": " + (item.get("reason") or "недоступен"))
    if chosen is None:
        if requested == "auto":
            status.backend = "none"
            status.reason = "ни одного бэкенда: " + "; ".join(reasons)
            hints = [found[n].get("install", "") for n in ("bwrap", "podman")
                     if found.get(n)]
            status.install_hint = " ".join(h for h in hints if h)
        else:
            status.backend = "unavailable"
            status.reason = "; ".join(reasons)
            row = found.get(requested) or {}
            status.install_hint = row.get("install", "")
        return status
    status.backend = chosen["name"]
    status.available = True
    status.fs_isolated = True
    status.executable = chosen["executable"]
    status.image = chosen.get("image", "")
    if verify:
        ok, reason, pid_ns = verify_backend(status)
        status.verified = ok
        status.reason = reason
        status.pid_ns = pid_ns
    return status


def _trial_command(spec: SandboxSpec, outside: Path, port: int) -> str:
    """Команда пробного запуска: внутрь можно, наружу и в сеть (без net) — нет."""
    import shlex

    # touch, а не «: >»: двоеточие — специальный встроенный оператор
    # sh, и ошибка его перенаправления завершает весь shell пробы.
    parts = ["touch inside.txt"]
    parts.append("! touch " + shlex.quote(str(outside)))
    if spec.backend == "bwrap":
        # Слушатель на host-loopback: в своей сетевой namespace ячейка
        # его не увидит; без unshare-net — увидела бы, и проба красная.
        code = ("import socket; "
                "socket.create_connection(('127.0.0.1', " + str(port) + "), 1)")
        parts.append("! " + shlex.quote(sys.executable or "python3") +
                     " -c " + shlex.quote(code))
    return " && ".join(parts)


def verify_backend(status: SandboxStatus) -> tuple:
    """Пробный запуск бэкенда (кэш на процесс). (успех, причина, pid_ns)."""
    key = (status.backend, status.executable, status.image, sys.platform)
    with _CACHE_LOCK:
        if key in _VERIFY_CACHE:
            return _VERIFY_CACHE[key]
    result = _verify_backend_uncached(status)
    with _CACHE_LOCK:
        _VERIFY_CACHE[key] = result
    return result


def _verify_backend_uncached(status: SandboxStatus) -> tuple:
    if not status.available or not status.fs_isolated:
        return False, status.reason or "бэкенд недоступен", True
    listener = None
    try:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
    except OSError:
        port = 0
    try:
        # bwrap: сначала вариант со своим pid namespace; во вложенных
        # контейнерах с запертым /proc хоста свежий /proc к новому pid
        # namespace не монтируется — тогда честный запасной вариант без
        # него (изоляция ФС и сети от этого не страдает).
        variants = (True, False) if status.backend == "bwrap" else (True,)
        reason = ""
        for pid_ns in variants:
            ok, reason = _probe_once(status, port, pid_ns)
            if ok:
                return True, reason, pid_ns
        return False, reason, True
    finally:
        if listener is not None:
            listener.close()


def _probe_once(status: SandboxStatus, port: int, pid_ns: bool) -> tuple:
    with tempfile.TemporaryDirectory(prefix="luna-sandbox-probe-") as tmp:
        root = Path(tmp)
        work = root / "cell"
        work.mkdir()
        # Проба «наружу» целится в КОРЕНЬ ФС, а не в папку рядом с
        # ячейкой: под bwrap /tmp внутри — свой tmpfs, запись туда
        # «успешна» и ничего не доказывает (первый красный CI — он).
        outside = Path("/") / ("luna-probe-" + root.name + ".txt")
        spec = SandboxSpec(backend=status.backend, requested=status.requested,
                           workdir=work, net=False, verified=False,
                           fs_isolated=True, executable=status.executable,
                           image=status.image, pid_ns=pid_ns)
        try:
            argv, env = spec.wrap(_trial_command(spec, outside, port))
            try:
                proc = subprocess.run(argv, cwd=str(work), env=env,
                                      capture_output=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, "пробный запуск не удался: " + str(exc)
            if proc.returncode != 0:
                tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                return False, "пробный запуск не прошёл" + (": " + tail[:200] if tail else "")
            if not (work / "inside.txt").exists():
                return False, "пробный запуск не записал в папку ячейки"
            if outside.exists():
                return False, "пробный запуск записал НАРУЖУ папки ячейки"
            return True, "пробный запуск: запись только в папку ячейки" + \
                (", сети без net нет" if status.backend == "bwrap" else
                 ", сеть закрыта --network none")
        finally:
            try:
                outside.unlink()
            except OSError:
                pass


def isolation_verified(settings=None) -> tuple:
    """(есть ли проверенная изоляция, описание) — вопрос Предохранителя."""
    status = sandbox_status(settings, verify=True)
    if status.verified and status.fs_isolated:
        return True, status.describe()
    return False, status.describe()


def resolve_spec(settings, workdir, *, net: bool = False) -> SandboxSpec:
    """Рецепт изоляции для папки ячейки; недоступный явный бэкенд не
    подменяется молча — spec.unavailable, и bash ячейки честно откажет."""
    memory, procs = _limits(settings)
    status = sandbox_status(settings)
    spec = SandboxSpec(backend=status.backend, requested=status.requested,
                       workdir=Path(workdir), net=bool(net),
                       fs_isolated=status.fs_isolated,
                       executable=status.executable, image=status.image,
                       memory_mb=memory, max_processes=procs,
                       reason=status.reason,
                       limits=status.backend not in ("podman", "docker"))
    if status.backend in BACKEND_ORDER and status.available:
        ok, reason, pid_ns = verify_backend(status)
        spec.verified = ok
        spec.reason = reason
        spec.pid_ns = pid_ns
    return spec
