import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from rt_extractor import ExtractionError, TokenBundle, extract_and_save, extract_tokens, save_tokens
from rt_extractor.__main__ import _code_prompt, main


def response(**changes):
    fields = dict(
        access_token="test-access-token", refresh_token="test-refresh-token",
        id_token="test-id-token", expires_in=3600,
        chatgpt_account_id="account-id", chatgpt_user_id="user-id", plan_type="free", sub="subject",
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


def bundle():
    return TokenBundle(
        email="test@example.com", access_token="test-access-token",
        refresh_token="test-refresh-token", id_token="test-id-token", expires_in=3600,
        obtained_at="2026-10-22T00:00:00+00:00", expires_at="2026-10-22T01:00:00+00:00",
    )


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_result_preserves_tokens_and_password_spaces(self):
        with patch("rt_extractor._protocol.login.login_get_tokens", new=AsyncMock(return_value=response())) as login:
            result = await extract_tokens(" test@example.com ", " password ", totp_secret="JBSWY3DPEHPK3PXP")
        self.assertEqual(login.await_args.kwargs["password"], " password ")
        self.assertEqual(login.await_args.kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(result.email, "test@example.com")
        self.assertEqual(result.refresh_token, "test-refresh-token")
        self.assertEqual(result.id_token, "test-id-token")
        self.assertEqual(result.chatgpt_account_id, "account-id")
        for secret in (result.access_token, result.refresh_token, result.id_token):
            self.assertNotIn(secret, repr(result))
        self.assertNotIn("password", result.to_dict())
        self.assertNotIn("totp_secret", result.to_dict())

    async def test_each_missing_token_refuses_to_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tokens.json"
            for token in ("access_token", "refresh_token", "id_token"):
                for invalid in ("", None, 123):
                    with self.subTest(token=token, invalid=invalid):
                        with patch("rt_extractor._protocol.login.login_get_tokens", new=AsyncMock(return_value=response(**{token: invalid}))):
                            with self.assertRaises(ExtractionError):
                                await extract_and_save("test@example.com", "password", output)
                        self.assertFalse(output.exists())

    async def test_static_code_is_supplied_only_at_mfa_request(self):
        async def login(**kwargs):
            self.assertIsNone(kwargs["totp_secret"])
            self.assertEqual(await kwargs["totp_fetcher"](), "012345")
            return response()
        with patch("rt_extractor._protocol.login.login_get_tokens", new=login):
            await extract_tokens("test@example.com", "password", totp_code="012345")

    async def test_invalid_inputs_rejected_before_login(self):
        cases = [
            {"email": "not-an-email"}, {"password": ""}, {"timeout": 0},
            {"timeout": float("nan")}, {"timeout": float("inf")},
            {"totp_secret": "invalid***"}, {"totp_code": "12345"},
            {"totp_secret": "JBSWY3DPEHPK3PXP", "totp_code": "123456"},
            {"proxy": "ftp://localhost:8080"}, {"proxy": "http://localhost:bad"},
        ]
        with patch("rt_extractor._protocol.login.login_get_tokens", new=AsyncMock()) as login:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    await extract_tokens(**({"email": "test@example.com", "password": "pw"} | changes))
        login.assert_not_awaited()

    async def test_network_errors_never_echo_credentials(self):
        with patch("rt_extractor._protocol.login.login_get_tokens", new=AsyncMock(side_effect=RuntimeError("password=private TOTP=secret token=private"))):
            with self.assertRaises(ExtractionError) as raised:
                await extract_tokens("test@example.com", "password")
        self.assertNotIn("private", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    async def test_timeout_cancels_login(self):
        cancelled = asyncio.Event()
        async def login(**_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch("rt_extractor._protocol.login.login_get_tokens", new=login):
            with self.assertRaises(ExtractionError):
                await extract_tokens("test@example.com", "password", timeout=0.01)
        self.assertTrue(cancelled.is_set())

    async def test_existing_output_stops_before_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tokens.json"
            output.write_text("original", encoding="utf-8")
            with patch("rt_extractor.api.extract_tokens", new=AsyncMock()) as login:
                with self.assertRaises(FileExistsError):
                    await extract_and_save("test@example.com", "pw", output)
            login.assert_not_awaited()
            self.assertEqual(output.read_text(), "original")


class StorageTests(unittest.TestCase):
    def test_private_complete_json_and_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tokens.json"
            save_tokens(bundle(), output)
            self.assertEqual(json.loads(output.read_text()), bundle().to_dict())
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                save_tokens(bundle(), output)
            save_tokens(bundle(), output, overwrite=True)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_write_failure_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tokens.json"
            output.write_text("original", encoding="utf-8")
            with patch("rt_extractor.storage.os.replace", side_effect=OSError("simulated disk error")):
                with self.assertRaises(OSError):
                    save_tokens(bundle(), output, overwrite=True)
            self.assertEqual(output.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.json"
            original.write_text("original", encoding="utf-8")
            output = Path(directory) / "link.json"
            output.symlink_to(original)
            with self.assertRaises(ValueError):
                save_tokens(bundle(), output, overwrite=True)
            self.assertEqual(original.read_text(), "original")

    def test_no_overwrite_race_leaves_other_writers_file_intact(self):
        real_link = os.link
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tokens.json"
            def race(source, destination):
                output.write_text("other writer", encoding="utf-8")
                return real_link(source, destination)
            with patch("rt_extractor.storage.os.link", side_effect=race):
                with self.assertRaises(FileExistsError):
                    save_tokens(bundle(), output)
            self.assertEqual(output.read_text(), "other writer")


class CliTests(unittest.TestCase):
    def test_empty_totp_setting_allows_interactive_code_callback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RT_EMAIL": "test@example.com", "RT_PASSWORD": "password", "RT_TOTP_SECRET": "",
        }, clear=True):
            with patch("rt_extractor.__main__._secret", return_value=""), patch(
                "rt_extractor.__main__.extract_and_save", new=AsyncMock(return_value=bundle())
            ) as extract, contextlib.redirect_stdout(io.StringIO()):
                code = main(["--output", str(Path(directory) / "tokens.json")])
            self.assertEqual(code, 0)
            self.assertNotIn("totp_secret", extract.await_args.kwargs)
            self.assertTrue(callable(extract.await_args.kwargs["totp_fetcher"]))

    def test_noninteractive_missing_credentials_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with patch("rt_extractor.__main__._secret") as secret, contextlib.redirect_stderr(io.StringIO()):
                code = main(["--non-interactive", "--output", str(Path(directory) / "tokens.json")])
            self.assertEqual(code, 1)
            secret.assert_not_called()

    def test_json_input_does_not_inherit_another_accounts_environment_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "account.json"
            input_path.write_text(json.dumps({"email": "test@example.com", "password": "new-account-password"}))
            with patch.dict(os.environ, {"RT_TOTP_SECRET": "other-account-secret"}), patch(
                "rt_extractor.__main__.extract_and_save", new=AsyncMock(return_value=bundle())
            ) as extract, contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(["--input", str(input_path), "--output", str(Path(directory) / "tokens.json"), "--non-interactive"])
            self.assertEqual(code, 0)
            self.assertNotIn("totp_secret", extract.await_args.kwargs)
            self.assertNotIn("test-refresh-token", stdout.getvalue())
            self.assertNotIn("new-account-password", stdout.getvalue())


@unittest.skipUnless(os.name == "posix", "POSIX terminal restoration")
class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_cancellation_restores_terminal_without_waiting_for_input(self):
        import pty
        import termios

        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        try:
            with patch("rt_extractor.__main__.os.open", side_effect=lambda *_: os.dup(slave)), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(_code_prompt("code: "), timeout=0.02)
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(master)
            os.close(slave)

    async def test_prompt_reads_current_code_and_restores_terminal(self):
        import pty
        import termios

        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        try:
            with patch("rt_extractor.__main__.os.open", side_effect=lambda *_: os.dup(slave)), contextlib.redirect_stderr(io.StringIO()):
                task = asyncio.create_task(_code_prompt("code: "))
                await asyncio.sleep(0)
                self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO)
                os.write(master, b"123456\n")
                self.assertEqual(await asyncio.wait_for(task, timeout=1), "123456")
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
