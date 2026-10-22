"""Interactive CLI: python -m rt_extractor."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import math
import os
import sys
import warnings
from pathlib import Path

from .api import ExtractionError, extract_and_save
from .storage import check_output


def _secret(prompt: str) -> str:
    # Refuse getpass's echoing fallback when /dev/tty is unavailable.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("无法隐藏输入，请在交互终端运行或使用环境变量 / --input") from None


async def _code_prompt(prompt: str) -> str:
    """Read hidden challenge input without a thread that survives cancellation."""
    if os.name == "nt":
        import msvcrt

        print(prompt, end="", file=sys.stderr, flush=True)
        characters = []
        try:
            while True:
                if not msvcrt.kbhit():
                    await asyncio.sleep(0.05)
                    continue
                char = msvcrt.getwch()
                if char in ("\r", "\n"):
                    return "".join(characters)
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\b":
                    if characters:
                        characters.pop()
                elif char in ("\x00", "\xe0"):
                    msvcrt.getwch()
                else:
                    characters.append(char)
        finally:
            print(file=sys.stderr)

    import termios

    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        raise ValueError("无法打开交互终端；请通过回调或输入参数提供验证码") from None
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    original = None
    reader_added = False

    def read_ready() -> None:
        if result.done():
            return
        try:
            value = os.read(descriptor, 4096)
            if not value:
                result.set_exception(EOFError())
            else:
                result.set_result(value.decode("utf-8").rstrip("\r\n"))
        except BlockingIOError:
            pass
        except Exception as exc:
            result.set_exception(exc)

    try:
        original = termios.tcgetattr(descriptor)
        hidden = original[:]
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(descriptor, termios.TCSANOW, hidden)
        print(prompt, end="", file=sys.stderr, flush=True)
        loop.add_reader(descriptor, read_ready)
        reader_added = True
        return await result
    finally:
        if reader_added:
            loop.remove_reader(descriptor)
        try:
            if original is not None:
                termios.tcsetattr(descriptor, termios.TCSANOW, original)
        finally:
            os.close(descriptor)
            print(file=sys.stderr)


def _load_input(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("无法读取输入 JSON 文件") from None
    if not isinstance(data, dict):
        raise ValueError("输入 JSON 必须是单个账号对象")
    allowed = {"email", "password", "totp_secret", "totp_code", "proxy"}
    if set(data) - allowed:
        raise ValueError("输入 JSON 包含不支持的字段；允许 email/password/totp_secret/totp_code/proxy")
    if any(not isinstance(value, str) for value in data.values()):
        raise ValueError("输入 JSON 的所有字段必须为字符串")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账号 + 密码 + 2FA 登录，保存 AT、RT、ID Token")
    parser.add_argument("--email", help="账号邮箱；也可使用 RT_EMAIL")
    parser.add_argument("--input", help="单个账号的 JSON 文件")
    parser.add_argument("--output", default="tokens.json", help="Token JSON 输出文件（默认 tokens.json）")
    parser.add_argument("--proxy", help="代理 URL；也可使用 RT_PROXY")
    parser.add_argument("--timeout", type=float, default=180, help="整个登录流程超时秒数（默认 180）")
    parser.add_argument("--overwrite", action="store_true", help="允许原子替换已有输出文件")
    parser.add_argument("--non-interactive", action="store_true", help="缺参数或额外验证码时直接失败")
    args = parser.parse_args(argv)

    try:
        check_output(args.output, overwrite=args.overwrite)
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("timeout 必须是大于零的有限秒数")
        # An input file is self-contained; never mix it with another account's env secrets.
        if args.input:
            data = _load_input(args.input)
        else:
            data = {key: os.environ[env] for key, env in (
                ("email", "RT_EMAIL"), ("password", "RT_PASSWORD"),
                ("totp_secret", "RT_TOTP_SECRET"), ("totp_code", "RT_TOTP_CODE"),
                ("proxy", "RT_PROXY"),
            ) if env in os.environ}
        if args.email:
            data["email"] = args.email
        if args.proxy:
            data["proxy"] = args.proxy
        for key in ("totp_secret", "totp_code", "proxy"):
            if key in data and not data[key].strip():
                del data[key]
        if not data.get("email") and not args.non_interactive:
            data["email"] = input("账号邮箱: ").strip()
        if not data.get("password") and not args.non_interactive:
            data["password"] = _secret("账号密码（隐藏输入）: ")
        if not data.get("email") or not data.get("password"):
            raise ValueError("缺少账号或密码，请使用环境变量或 --input 提供")
        if not args.non_interactive and not (data.get("totp_secret") or data.get("totp_code")):
            seed = _secret("2FA Base32 密钥 / otpauth URI（留空则在验证时输入动态码）: ").strip()
            if seed:
                data["totp_secret"] = seed

        async def ask_totp() -> str:
            return await _code_prompt("Authenticator 当前 6 位动态码: ")

        async def ask_email_otp(_email: str) -> str:
            return await _code_prompt("登录还要求邮箱验证码，请输入: ")

        def progress(_stage: str, message: str) -> None:
            print(message, file=sys.stderr)

        if not args.non_interactive:
            data["otp_fetcher"] = ask_email_otp
            if not (data.get("totp_secret") or data.get("totp_code")):
                data["totp_fetcher"] = ask_totp
        asyncio.run(extract_and_save(
            **data, output=args.output, overwrite=args.overwrite,
            timeout=args.timeout, on_step=progress,
        ))
        print(f"已保存 AT、RT、ID Token：{Path(args.output).expanduser().absolute()}")
        return 0
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except (ValueError, FileExistsError, ExtractionError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    except EOFError:
        print("失败：输入已结束", file=sys.stderr)
        return 1
    except Exception as exc:
        try:
            from ._protocol.login import ProtocolError
            safe = isinstance(exc, ProtocolError)
        except ImportError:
            safe = False
        if safe:
            print(f"失败：{exc}", file=sys.stderr)
        else:
            # Do not print remote exceptions or traceback locals containing credentials.
            print(f"失败：{type(exc).__name__}，请检查依赖、网络或输出目录权限", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
