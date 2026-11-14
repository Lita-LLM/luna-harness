"""Сейф: файл под паролем — для сохранения сессии Новолуния (/keep).

Только стандартная библиотека, поэтому не AES, а известная связка из
примитивов hashlib/hmac:
  * ключ из пароля — scrypt (соль 16 байт; нет scrypt в сборке — PBKDF2);
  * шифр — поток HMAC-SHA256(ключ, nonce ‖ счётчик), XOR с текстом;
  * целостность — HMAC-SHA256 по всему файлу (encrypt-then-MAC): чужой
    или битый файл и неверный пароль отличить нельзя, и это правильно.
Пароль нигде не хранится. Текст перед шифрованием сжат zlib.

Формат: MAGIC(8) ‖ kdf(1) ‖ соль(16) ‖ nonce(16) ‖ шифротекст ‖ тег(32).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import zlib
from pathlib import Path

MAGIC = b"LUNAKP01"
SUFFIX = ".lunakeep"
KDF_SCRYPT = 1
KDF_PBKDF2 = 2
_HEAD = len(MAGIC) + 1 + 16 + 16
_TAG = 32


class VaultError(Exception):
    """Не открылось: неверный пароль, чужой или повреждённый файл."""


def _derive(password: str, salt: bytes, kdf: int) -> tuple:
    pw = password.encode("utf-8")
    if kdf == KDF_SCRYPT:
        key = hashlib.scrypt(pw, salt=salt, n=2 ** 15, r=8, p=1,
                             maxmem=64 * 1024 * 1024, dklen=64)
    elif kdf == KDF_PBKDF2:
        key = hashlib.pbkdf2_hmac("sha256", pw, salt, 600_000, dklen=64)
    else:
        raise VaultError("неизвестный формат ключа")
    return key[:32], key[32:]


def _stream_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    block = 32
    for i in range(0, len(data), block):
        pad = hmac.new(key, nonce + (i // block).to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[i:i + block]
        out[i:i + len(chunk)] = (int.from_bytes(chunk, "big")
                                 ^ int.from_bytes(pad[:len(chunk)], "big")
                                 ).to_bytes(len(chunk), "big")
    return bytes(out)


def seal(payload: dict, password: str) -> bytes:
    if not password:
        raise VaultError("пустой пароль")
    kdf = KDF_SCRYPT if hasattr(hashlib, "scrypt") else KDF_PBKDF2
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    enc_key, mac_key = _derive(password, salt, kdf)
    plain = zlib.compress(json.dumps(payload, ensure_ascii=False,
                                     default=str).encode("utf-8"), 6)
    head = MAGIC + bytes([kdf]) + salt + nonce
    body = head + _stream_xor(enc_key, nonce, plain)
    return body + hmac.new(mac_key, body, hashlib.sha256).digest()


def unseal(blob: bytes, password: str) -> dict:
    if len(blob) < _HEAD + _TAG or not blob.startswith(MAGIC):
        raise VaultError("это не файл сейфа Луны")
    kdf = blob[len(MAGIC)]
    salt = blob[len(MAGIC) + 1:len(MAGIC) + 17]
    nonce = blob[len(MAGIC) + 17:_HEAD]
    body, tag = blob[:-_TAG], blob[-_TAG:]
    enc_key, mac_key = _derive(password, salt, kdf)
    if not hmac.compare_digest(tag, hmac.new(mac_key, body, hashlib.sha256).digest()):
        raise VaultError("неверный пароль или файл повреждён")
    try:
        data = json.loads(zlib.decompress(_stream_xor(enc_key, nonce, body[_HEAD:])))
    except (ValueError, zlib.error) as exc:
        raise VaultError("файл повреждён") from exc
    if not isinstance(data, dict):
        raise VaultError("файл повреждён")
    return data


def write(path, payload: dict, password: str) -> Path:
    """Атомарно: сначала соседний временный файл, потом замена."""
    path = Path(path).expanduser()
    if path.suffix != SUFFIX:
        path = path.with_name(path.name + SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = seal(payload, password)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(blob)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def read(path, password: str) -> dict:
    try:
        blob = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise VaultError("не прочитать файл: " + str(exc)) from exc
    return unseal(blob, password)
