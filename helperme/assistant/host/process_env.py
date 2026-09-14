"""Give Host and Worker the current user's session environment.

Channel is transport. A GUI client may start HelperMe with a leftover PATH.
The process that runs Sessions must see the user session environment, with
Channel-declared variables kept as overlays.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def host_process_environment(
    current: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if current is None else current
    result = {name: value for name, value in source.items()}
    session = user_session_environment()
    if session is None:
        return result
    for name, value in session.items():
        if not result.get(name):
            result[name] = value
    result["PATH"] = _merge_path(_path_of(result), _path_of(session))
    return result


def install_host_environment() -> None:
    os.environ.update(host_process_environment())


def user_session_environment() -> dict[str, str] | None:
    if os.name != "nt":
        return None
    return _windows_user_environment()


def _path_of(env: Mapping[str, str]) -> str:
    for name, value in env.items():
        if name.upper() == "PATH":
            return value
    return ""


def _merge_path(current: str, session: str) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for raw in (*current.split(os.pathsep), *session.split(os.pathsep)):
        item = raw.strip()
        if not item:
            continue
        key = os.path.normcase(os.path.normpath(item))
        if key in seen:
            continue
        seen.add(key)
        parts.append(item)
    return os.pathsep.join(parts)


def _windows_user_environment() -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    create = userenv.CreateEnvironmentBlock
    create.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.BOOL,
    ]
    create.restype = wintypes.BOOL
    destroy = userenv.DestroyEnvironmentBlock
    destroy.argtypes = [ctypes.c_void_p]
    destroy.restype = wintypes.BOOL

    block = ctypes.c_void_p()
    if not create(ctypes.byref(block), None, False):
        error = ctypes.get_last_error()
        raise OSError(error, "CreateEnvironmentBlock failed")
    try:
        return _parse_environment_block(block.value)
    finally:
        destroy(block)


def _parse_environment_block(address: int | None) -> dict[str, str]:
    if not address:
        raise OSError("CreateEnvironmentBlock returned an empty block")
    import ctypes

    result: dict[str, str] = {}
    offset = 0
    wchar = ctypes.sizeof(ctypes.c_wchar)
    while True:
        entry = ctypes.wstring_at(address + offset)
        if not entry:
            break
        name, separator, value = entry.partition("=")
        if separator and name:
            result[name] = value
        offset += (len(entry) + 1) * wchar
    if not result:
        raise OSError("CreateEnvironmentBlock returned an empty block")
    return result
