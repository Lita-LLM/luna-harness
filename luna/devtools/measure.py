"""Замеры по таблице целей (ПЛАН.md, раздел 2): `luna try measure [--json]`.

Честно, на живой машине: настоящая Луна запускается с настоящим терминалом.
(а) старт до первого кадра — N холодных запусков, медиана;
(б) память процесса в простое — /proc (POSIX) или GetProcessMemoryInfo (Windows);
(в) ЦП в простое за 10 секунд — /proc/<pid>/stat или GetProcessTimes.

Транспорт терминала — платформенный (урок РЕВЬЮ-01, п. 3: pty есть не везде):
POSIX — псевдотерминал, момент первого кадра виден по его выводу;
Windows — новая консоль (CREATE_NEW_CONSOLE), а момент первого кадра Луна
сама пишет в файл-метку (переменная LUNA_FIRST_FRAME_FILE, см. ui/screen.py).
Контракт замера от транспорта не зависит. Windows-путь реализован после
ревью вехи 0; в окружении исполнителя живой Windows нет — не проверен.
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

PROGRAM_DIR = Path(__file__).resolve().parents[2]
IS_WINDOWS = sys.platform == "win32"
START_RUNS = 7
IDLE_SECONDS = 10.0
MARKER_ENV = "LUNA_FIRST_FRAME_FILE"
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


def _spawn_luna_console(home: str, marker: Path):
    """Windows: Луна в новой консоли; вернуть (процесс, момент запуска).

    У ребёнка настоящий терминал (своя консоль), размер — какой даст система.
    Первый кадр родитель узнаёт не по выводу, а по файлу-метке: ребёнок
    пишет в него свой time.monotonic() (часы системные, сравнимы между
    процессами) — см. Screen._note_first_frame.
    """
    env = dict(os.environ, LUNA_HOME=home, PYTHONUNBUFFERED="1")
    env[MARKER_ENV] = str(marker)
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "luna"], cwd=PROGRAM_DIR,
                            env=env, creationflags=CREATE_NEW_CONSOLE)
    return proc, t0


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
    """Один запуск Луны под замер: (процесс, ждать_первый_кадр, остановить)."""
    if IS_WINDOWS:
        marker = Path(tempfile.mkdtemp(prefix="luna-measure-mark-")) / "first-frame.txt"
        proc, t0 = _spawn_luna_console(home, marker)
        return (proc,
                lambda: _wait_first_frame_marker(marker, t0, proc),
                lambda: _stop_console(proc))
    proc, master, _ = _spawn_luna(home)
    return (proc,
            lambda: _wait_first_frame(master, proc),
            lambda: _stop(proc, master))


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


def _rss_mb_windows(pid: int) -> float | None:
    """НЕ ПРОВЕРЕНО на живой Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_size_t) for n in
                        ("cb", "PageFaultCount", "PeakWorkingSetSize", "WorkingSetSize",
                         "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                         "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                         "PagefileUsage", "PeakPagefileUsage")]

        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        handle = kernel32.OpenProcess(0x0410, False, pid)
        if not handle:
            return None
        try:
            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
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
    """НЕ ПРОВЕРЕНО на живой Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0400, False, pid)
        if not handle:
            return None
        try:
            ft = [wintypes.FILETIME() for _ in range(4)]
            if kernel32.GetProcessTimes(handle, *ft):
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
        proc, wait_frame, stop = _start_run(home)
        dt = wait_frame()
        stop()
        if dt is not None:
            starts.append(round(dt * 1000))
    if starts:
        res["start_first_frame_ms"] = int(statistics.median(starts))
        res["start_runs_ms"] = starts
    # (б)+(в) простой: один экземпляр, дать заставке закончиться
    proc, wait_frame, stop = _start_run(home)
    try:
        wait_frame()
        time.sleep(2.0)  # заставка ~1,2 с + запас: дальше настоящий простой
        res["idle_rss_mb"] = _rss_mb(proc.pid)
        c0 = _cpu_seconds(proc.pid)
        t0 = time.monotonic()
        time.sleep(IDLE_SECONDS)
        c1 = _cpu_seconds(proc.pid)
        wall = time.monotonic() - t0
        if c0 is not None and c1 is not None:
            res["idle_cpu_pct"] = round(100 * (c1 - c0) / wall, 3)
        res["idle_seconds"] = round(wall, 1)
    finally:
        stop()
    return res


GOALS = [
    ("start_first_frame_ms", "Старт до первого кадра, медиана", "≤ 200 мс", 200, "мс", "le"),
    ("idle_rss_mb", "Память в простое", "≤ 40 МБ", 40, "МБ", "le"),
    ("idle_cpu_pct", "ЦП в простое за 10 с", "≈ 0 %", 0.5, "%", "lt"),
]


def run_measure(as_json: bool = False) -> int:
    res = measure()
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
    print("Цель не достигнута — это честный результат замера, а не повод подкрутить цифры."
          if failed else "Все цели вехи 0 по замерам достигнуты на этой машине.")
    return 0
