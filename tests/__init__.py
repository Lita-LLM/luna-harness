"""Тесты живут в своём каталоге данных, а не в ~/.luna человека.

Иначе код под тестом читает настоящие настройки и сейф: чат в тестах звал
настоящую модель человека (Ниблер через OmniRoute автора) на фальшивых событиях.
Тест, которому нужен свой LUNA_HOME, по-прежнему ставит его сам.
"""

import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="luna-tests-home-")
os.environ["LUNA_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, True)
