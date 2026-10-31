# Установка Лунапарка в Windows. Запускается через install.bat из папки программы.
# Удаление команды (данные не трогаются): install.bat --uninstall
# Зависимостей нет — нужен только Python 3.10+. Повторный запуск безопасен (идемпотентен).

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir = Join-Path $env:LOCALAPPDATA "Luna\bin"
$Launcher = Join-Path $BinDir "luna.cmd"

if ($args -contains "--uninstall") { $Uninstall = $true }

function Fail($what, $hint) {
    Write-Host ""
    Write-Host "  ✗ $what" -ForegroundColor Red
    Write-Host "    $hint"
    exit 1
}

function Remove-UserPathEntry($dir) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { return }
    $parts = @($userPath -split ";" | Where-Object { $_ -and ($_.TrimEnd("\") -ine $dir.TrimEnd("\")) })
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

if ($Uninstall) {
    if (Test-Path $Launcher) {
        Remove-Item $Launcher -Force
        Write-Host "  ✓ Команда luna удалена: $Launcher"
    } else {
        Write-Host "  Команда luna не была установлена ($Launcher нет) — удалять нечего."
    }
    Remove-UserPathEntry $BinDir
    if ((Test-Path $BinDir) -and -not (Get-ChildItem $BinDir -Force)) {
        Remove-Item $BinDir -Force
    }
    Write-Host "  Данные Лунапарка (LUNA_HOME, по умолчанию папка .luna в профиле) не тронуты."
    exit 0
}

Write-Host "== Лунапарк: установка =="
Write-Host "  Папка: $Here" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $Here "luna\__main__.py"))) {
    Fail "Установщик запущен не из папки Лунапарка" "Распакуй архив целиком (или склонируй репозиторий) и запусти install.bat из получившейся папки."
}

function Find-Python {
    $candidates = @(
        @("py", "-3.14"), @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("py", "-3"),
        @("python"), @("python3")
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # «python» из Microsoft Store — заглушка, которая открывает магазин; её отличаем по пути.
        if ($cmd.Source -like "*WindowsApps*") { continue }
        try {
            $a = @()
            if ($c.Length -gt 1) { $a += $c[1..($c.Length - 1)] }
            $a += @("-c", "import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 10) else 1)")
            $exe = & $c[0] $a 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch {}
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Fail "Не найден Python 3.10 или новее" "Установи Python с python.org (при установке отметь «Add python.exe to PATH») или выполни в терминале: winget install Python.Python.3.13 — и запусти install.bat снова."
}
Write-Host "  Python: $Python" -ForegroundColor DarkGray

# Лаунчер: найденный Python; папка именно этой копии репозитория — первой в sys.path,
# поэтому неважно, из какого каталога запускают luna.
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
$code = "import sys; sys.path.insert(0, sys.argv.pop(1)); from luna.__main__ import main; sys.exit(main())"
$launcherText = "@echo off`r`n`"$Python`" -c `"$code`" `"$Here`" %*`r`n"
[System.IO.File]::WriteAllText($Launcher, $launcherText, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "  ✓ Команда: $Launcher"

# Папка лаунчера — в PATH пользователя (без прав администратора), если её там ещё нет.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$parts = @()
if ($userPath) { $parts = @($userPath -split ";" | Where-Object { $_ }) }
$already = $false
foreach ($p in $parts) { if ($p.TrimEnd("\") -ieq $BinDir.TrimEnd("\")) { $already = $true } }
if (-not $already) {
    [Environment]::SetEnvironmentVariable("Path", (($parts + $BinDir) -join ";"), "User")
    Write-Host "  ✓ Папка добавлена в PATH пользователя: $BinDir"
    Write-Host "    Уже открытые терминалы её не увидят — открой новый."
} else {
    Write-Host "  Папка уже есть в PATH пользователя."
}
if (($env:Path -split ";") -notcontains $BinDir) { $env:Path = "$env:Path;$BinDir" }

# Самопроверка.
$out = & $Launcher --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "Луна не запускается через лаунчер" "Вывод: $out"
}
Write-Host "  ✓ Проверка: $out"
Write-Host ""
Write-Host "  ✓ Готово! Запуск: luna · Обновление git-копии: luna update · Удаление команды: install.bat --uninstall"
Write-Host "  Данные лежат в папке .luna профиля (или LUNA_HOME) и при удалении команды не трогаются."
exit 0
