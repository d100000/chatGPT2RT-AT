"""Atomic, private local token files; never persist the password or TOTP seed."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import TokenBundle


def check_output(output: str | Path, *, overwrite: bool = False) -> Path:
    path = Path(output).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("输出路径不能是符号链接")
    if path.exists():
        if not path.is_file():
            raise ValueError("输出路径必须是文件")
        if not overwrite:
            raise FileExistsError("输出文件已存在；如需替换，请显式指定 overwrite / --overwrite")
    if not path.parent.is_dir():
        raise ValueError("输出目录不存在，请先创建目录")
    return path


def save_tokens(tokens: TokenBundle, output: str | Path, *, overwrite: bool = False) -> Path:
    """Save all tokens as UTF-8 JSON, atomically, with POSIX mode 0600.

    A same-directory temporary file is fsynced before publishing. Without
    overwrite, a hard link publishes the file without a race overwriting an
    existing result. With overwrite, os.replace replaces the directory entry.
    """
    path = check_output(output, overwrite=overwrite)
    payload = tokens.to_dict()
    for key in ("access_token", "refresh_token", "id_token"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError("Token 不完整，拒绝保存")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".rt-extractor-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
