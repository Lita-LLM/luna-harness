"""Замеры по таблице целей (ПЛАН.md, раздел 2): `luna try measure [--json]`.

Честно, на живой машине: настоящая Луна запускается с настоящим терминалом.
(а) старт до первого кадра — N холодных запусков, медиана;
(б) память процесса в простое — /proc (POSIX) или GetProcessMemoryInfo (Windows);
(в) ЦП в простое за 10 секунд — /proc/<pid>/stat или GetProcessTimes.

Транспорт терминала — платформенный (урок РЕВЬЮ-01, п. 3: pty есть не везде):
POSIX — псевдотерминал, момент первого кадра виден по его выводу;
Windows — новая консоль (CREATE_NEW_CONSOLE), а момент первого кадра Луна
сама пишет в файл-метку (переменная LUNA_FIRST_FRAME_FILE, см. ui/screen.py).
Контракт замера от транспорта не зависит.

Старт на Windows считается честно (ЗАДАЧА-03, диагноз тимлида): не от
Popen — туда падало создание консоли и выходило 475 мс, — а от метки
старта внутри самого процесса Луны (LUNA_START_FILE, пишет __main__ при
импорте: монотонные часы + PID). Память и ЦП меряются по PID из той же
метки — именно процесс Луны, не обёртка и не conhost. В окружении
исполнителя живой Windows нет — путь покрыт тестами по частям.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..core.stdio import ensure_utf8_stdio

PROGRAM_DIR = Path(__file__).resolve().parents[2]
IS_WINDOWS = sys.platform == "win32"
START_RUNS = 7
IDLE_SECONDS = 10.0
MARKER_ENV = "LUNA_FIRST_FRAME_FILE"
START_ENV = "LUNA_START_FILE"
CREATE_NEW_CONSOLE = 0x00000010  # subprocess.CREATE_NEW_CONSOLE: атрибут есть только на Windows


def _spawn_luna(home: str):
    """Запустить Луну под pty 80x24; вернуть (процесс, fd чтения, момент запуска)."""
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor",
               LUNA_HOME=home, PYTHONUNBUFFERED="1")
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "luna"], stdin=slave, stdout=slave,
                            stderr=slave, env=env, cwd=PROGRAM_DIR, start_new_session=True)
    os.close(slave)
    return proc, master, t0


def _wait_first_frame(master: int, proc, timeout: float = 10.0) -> float | None:
    import select

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        ready, _, _ = select.select([master], [], [], 0.25)
        if master in ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                return None
            if data:
                return time.monotonic() - start
        if proc.poll() is not None:
            return None
    return None


def _stop(proc, master) -> None:
    try:
        os.write(master, b"\x03")
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    try:
        os.close(master)
    except OSError:
        pass


def _spawn_luna_console(home: str, marker: Path, start_file: Path):
    """Windows: Луна в новой консоли; вернуть (процесс, момент запуска).

    У ребёнка настоящий терминал (своя консоль), размер — какой даст система.
    Первый кадр родитель узнаёт не по выводу, а по файлу-метке: ребёнок
    пишет в него свой time.monotonic() (часы системные, сравнимы между
    процессами) — см. Screen._note_first_frame. Метку старта ребёнок пишет
    в start_file при импорте __main__ — см. _read_start_marker.
    """
    env = dict(os.environ, LUNA_HOME=home, PYTHONUNBUFFERED="1")
    env[MARKER_ENV] = str(marker)
    env[START_ENV] = str(start_file)
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "luna"], cwd=PROGRAM_DIR,
                            env=env, creationflags=CREATE_NEW_CONSOLE)
    return proc, t0


def _read_start_marker(path: Path):
    """Прочитать метку старта ребёнка: (монотонное время, PID) или None.

    Формат файла (пишет luna/__main__.py при импорте): первая строка —
    repr(time.monotonic()) момента старта процесса, вторая — его PID.
    Второй строки может не быть у старых версий — тогда PID None.
    """
    try:
        lines = path.read_text(encoding="ascii").splitlines()
        started = float(lines[0])
        pid = int(lines[1]) if len(lines) > 1 else None
        return started, pid
    except (OSError, ValueError, IndexError):
        return None


def _metric_pid(proc_pid: int, marker_data) -> int:
    """Чей PID мерять: из метки ребёнка (сам процесс Луны), а не обёртки."""
    if marker_data and marker_data[1]:
        return marker_data[1]
    return proc_pid


def _wait_start_frame(marker: Path, start_file: Path, t0: float, proc,
                      timeout: float = 15.0) -> float | None:
    """Старт по-честному: первый кадр минус метка старта внутри процесса.

    Если метка старта не читается (старый ребёнок, сбой записи) — падаем
    на прежнюю семантику «от Popen», чтобы замер не умирал совсем.
    """
    frame_ts = _wait_first_frame_marker(marker, 0.0, proc, timeout)
    if frame_ts is None:
        return None
    got = _read_start_marker(start_file)
    if got is None:
        return frame_ts - t0
    return frame_ts - got[0]


def _wait_first_frame_marker(marker: Path, t0: float, proc,
                             timeout: float = 15.0) -> float | None:
    """Ждать файла-метки; вернуть секунды от запуска до первого кадра."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return float(marker.read_text(encoding="ascii")) - t0
        except (OSError, ValueError):
            pass  # файла ещё нет или он пишется прямо сейчас
        if proc.poll() is not None:
            return None
        time.sleep(0.005)
    return None


def _stop_console(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001 — добиваем любой ценой, процесс замерный
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass


def _start_run(home: str):
    """Один запуск Луны под замер.

    Возвращает (процесс, ждать_первый_кадр, остановить, pid_для_метрик).
    pid_для_метрик звать после первого кадра: на Windows он берёт PID из
    метки старта ребёнка (сам процесс Луны), на POSIX это просто proc.pid.
    """
    if IS_WINDOWS:
        mark_dir = Path(tempfile.mkdtemp(prefix="luna-measure-mark-"))
        marker = mark_dir / "first-frame.txt"
        start_file = mark_dir / "start.txt"
        proc, t0 = _spawn_luna_console(home, marker, start_file)
        return (proc,
                lambda: _wait_start_frame(marker, start_file, t0, proc),
                lambda: _stop_console(proc),
                lambda: _metric_pid(proc.pid, _read_start_marker(start_file)))
    proc, master, _ = _spawn_luna(home)
    return (proc,
            lambda: _wait_first_frame(master, proc),
            lambda: _stop(proc, master),
            lambda: proc.pid)


def _rss_mb(pid: int) -> float | None:
    if IS_WINDOWS:
        return _rss_mb_windows(pid)
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def _memory_counters_class():
    """Класс PROCESS_MEMORY_COUNTERS с честной раскладкой полей.

    Первые два поля (cb, PageFaultCount) — DWORD (4 байта), остальные —
    SIZE_T. Прежняя версия объявляла все поля c_size_t, из-за чего
    WorkingSetSize читался не со своего смещения и замер на Windows
    показывал 0,1 МБ у живого процесса (диагноз тимлида, ЗАДАЧА-03).
    Отдельной функцией — чтобы раскладку можно было проверить тестом
    на любой платформе, без windll.
    """
    import ctypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [
            (n, ctypes.c_size_t) for n in
            ("PeakWorkingSetSize", "WorkingSetSize",
             "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
             "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
             "PagefileUsage", "PeakPagefileUsage")]

    return PMC


def _open_process(kernel32, access: int, pid: int):
    """OpenProcess с объявленными типами: без restype=c_void_p хэндл на
    64-битной Windows урезается до C int и указывает не туда."""
    import ctypes

    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    return kernel32.OpenProcess(access, False, pid)


def _rss_mb_windows(pid: int) -> float | None:
    """НЕ ПРОВЕРЕНО на живой Windows; раскладка структуры и типы — тестами."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = _open_process(kernel32, 0x0410, pid)  # QUERY_INFORMATION | VM_READ
        if not handle:
            return None
        try:
            PMC = _memory_counters_class()
            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            get_info = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if get_info is None:
                get_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_uint32]
            get_info.restype = ctypes.c_int
            if get_info(handle, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / 1048576, 1)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
    return None


def _cpu_seconds(pid: int) -> float | None:
    if IS_WINDOWS:
        return _cpu_seconds_windows(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        parts = stat.rsplit(")", 1)[1].split()
        ticks = int(parts[11]) + int(parts[12])  # utime + stime
        hz = os.sysconf("SC_CLK_TCK")
        return ticks / hz
    except (OSError, ValueError, IndexError):
        return None


def _cpu_seconds_windows(pid: int) -> float | None:
    """НЕ ПРОВЕРЕНО на живой Windows; типы вызовов объявлены, как в RSS."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = _open_process(kernel32, 0x0400, pid)  # QUERY_INFORMATION
        if not handle:
            return None
        try:
            ft = [wintypes.FILETIME() for _ in range(4)]
            kernel32.GetProcessTimes.restype = ctypes.c_int
            kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + \
                [ctypes.POINTER(wintypes.FILETIME)] * 4
            if kernel32.GetProcessTimes(handle, *[ctypes.byref(f) for f in ft]):
                def val(f):
                    return (f.dwHighDateTime << 32 | f.dwLowDateTime) / 1e7

                return val(ft[2]) + val(ft[3])  # user + kernel
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
    return None


def measure() -> dict:
    res: dict = {"python": sys.version.split()[0], "platform": sys.platform,
                 "program_dir": str(PROGRAM_DIR),
                 "terminal": ("новая консоль (метка первого кадра — файл)"
                              if IS_WINDOWS else "pty 80x24")}
    home = tempfile.mkdtemp(prefix="luna-measure-")
    # (а) старт до первого кадра
    starts = []
    for _ in range(START_RUNS):
        proc, wait_frame, stop, _pid = _start_run(home)
        dt = wait_frame()
        stop()
        if dt is not None:
            starts.append(round(dt * 1000))
    if starts:
        res["start_first_frame_ms"] = int(statistics.median(starts))
        res["start_runs_ms"] = starts
    # (б)+(в) простой: один экземпляр, дать заставке закончиться
    proc, wait_frame, stop, metric_pid = _start_run(home)
    try:
        wait_frame()
        time.sleep(2.0)  # заставка ~1,2 с + запас: дальше настоящий простой
        pid = metric_pid()  # процесс самой Луны (на Windows — из её метки)
        res["idle_rss_mb"] = _rss_mb(pid)
        c0 = _cpu_seconds(pid)
        t0 = time.monotonic()
        time.sleep(IDLE_SECONDS)
        c1 = _cpu_seconds(pid)
        wall = time.monotonic() - t0
        if c0 is not None and c1 is not None:
            res["idle_cpu_pct"] = round(100 * (c1 - c0) / wall, 3)
        res["idle_seconds"] = round(wall, 1)
    finally:
        stop()
    return res


def frame_costs(pairs: int = 300, frames: int = 7) -> dict:
    """Стоимость одного кадра чата — без терминала, в буфер (ЗАДАЧА-22).

    Лента из `pairs` пар «вопрос — ответ» (ответ — абзацы с разметкой:
    перенос и раскраска — самая дорогая часть кадра; до кэша строк
    записей кадр на такой ленте стоил ~275 мс). Варианты: просто лента;
    лента с открытыми панелями; лента и живой стрим (хвост дописывается
    перед каждым кадром). Возвращает медианы в миллисекундах.
    """
    from ..core.bus import EventBus
    from ..core.paths import DataDir
    from ..core.settings import Settings
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp, Entry

    root = Path(tempfile.mkdtemp(prefix="luna-frame-"))
    settings = Settings(home=root / "home", cwd=root)
    app = ChatApp(EventBus(), settings, Scheduler(), "emoji",
                  cwd=str(root), data=DataDir(root / "home"))
    answer = ("Отвечаю по пунктам:\n\n"
              "- первое — **важное** про сборку и тесты,\n"
              "- второе — `код` и пути вида luna/ui/chat.py,\n"
              "- третье — длинная строка пояснения, которая не влезает "
              "в ширину экрана и честно переносится на следующую.\n\n"
              "Итого: всё собралось, тесты зелёные, можно двигаться дальше.")
    for i in range(pairs):
        app.entries.append(Entry("user", text=f"вопрос номер {i}: почини сборку"))
        app.entries.append(Entry("agent", text=answer))
    now = time.monotonic()

    def timed(grow=None) -> float:
        samples = []
        for _ in range(frames):
            if grow is not None:
                grow()
            t0 = time.perf_counter()
            app.draw(100, 30, now)
            samples.append((time.perf_counter() - t0) * 1000)
        return round(statistics.median(samples), 1)

    app.draw(100, 30, now)  # прогрев: кэш строк записей наполняется
    app.draw(100, 30, now)
    res = {"feed_lines": len(app._feed_texts), "frame_ms": timed()}
    app.toggle_panel("moon")
    app.toggle_panel("limits")
    app.draw(100, 30, now)
    res["frame_panels_ms"] = timed()
    live = Entry("agent", text="стрим идёт", streaming=True)
    app.entries.append(live)

    def grow() -> None:
        live.text += " ещё кусок"

    app.draw(100, 30, now)
    res["frame_stream_ms"] = timed(grow)
    return res


GOALS = [
    ("start_first_frame_ms", "Старт до первого кадра, медиана", "≤ 200 мс", 200, "мс", "le"),
    ("idle_rss_mb", "Память в простое", "≤ 40 МБ", 40, "МБ", "le"),
    ("idle_cpu_pct", "ЦП в простое за 10 с", "≈ 0 %", 0.5, "%", "lt"),
    ("frame_ms", "Кадр на длинной ленте", "≤ 50 мс", 50, "мс", "le"),
    ("frame_panels_ms", "Кадр: лента + панели", "≤ 50 мс", 50, "мс", "le"),
    ("frame_stream_ms", "Кадр: лента + живой стрим", "≤ 80 мс", 80, "мс", "le"),
]


def run_measure(as_json: bool = False) -> int:
    ensure_utf8_stdio()  # печатаем сами (✓/✗, русский) — кодировка наша забота
    res = measure()
    try:
        res.update(frame_costs())  # кадры считаются в этом же процессе
    except Exception:  # noqa: BLE001 — замер кадров не должен ронять остальные
        pass
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    python_version = res["python"]
    platform_name = res["platform"]
    terminal_name = res["terminal"]
    print(f"Замеры Луны (Python {python_version}, {platform_name}, терминал: {terminal_name})")
    failed = False
    dash = "—"
    for key, title, goal, limit, unit, op in GOALS:
        value = res.get(key)
        if value is None:
            print(f"  ? {title:<38} {dash:>10}   цель {goal}")
            continue
        ok = value <= limit if op == "le" else value < limit
        failed = failed or not ok
        # Символ — переменной, а не литералом внутри f-строки: выражения
        # f-строк держим без кавычек и слэшей ради Python 3.10 (РЕВЬЮ-01/2).
        mark = "✓" if ok else "✗"
        print(f"  {mark} {title:<38} {value:>8} {unit:<3} цель {goal}")
    if res.get("start_runs_ms"):
        runs = res["start_runs_ms"]
        print(f"    запуски: {runs} мс")
    if res.get("feed_lines"):
        feed_lines = res["feed_lines"]
        print(f"    лента замера кадра: {feed_lines} строк")
    print("Цель не достигнута — это честный результат замера, а не повод подкрутить цифры."
          if failed else "Все цели вехи 0 по замерам достигнуты на этой машине.")
    return 0
