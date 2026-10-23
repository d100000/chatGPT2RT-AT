# bob ai tools: ChatGPT Token Extraction for RT, AT and ID Token

[简体中文](README.md) · [English](README.en.md) · [Project website](https://tools.bob-api.com/) · [Source repository](https://github.com/d100000/chatGPT2RT-AT)

**bob ai tools signs in to an existing, authorized ChatGPT account and exports its Refresh Token (RT), Access Token (AT) and ID Token.** It provides a local web workbench, a single-account CLI and a Python API, plus CPA ↔ Sub2API conversion and browser IP diagnostics.

This is an independent, unofficial project with no affiliation with or endorsement from OpenAI. Account tokens are not OpenAI API keys. The project does not create API keys, register accounts, bypass verification or automatically renew refresh tokens. Login compatibility depends on the upstream authentication service.

## Choose a workflow

| Goal | Workflow | Output |
| --- | --- | --- |
| Extract ChatGPT refresh and access tokens | Sign in with an account you own or are authorized to use; complete required verification | Original JSON with AT, RT and ID Token; CPA JSON; Sub2API JSON |
| Convert existing authentication files | Import JSON in the browser converter | Merged Sub2API JSON, individual CPA files or ZIP |
| Inspect the browser's network exit | Request IPOK diagnostics from the browser | IP location, available risk score and source evidence |

Conversion maps existing fields. It neither refreshes credentials nor proves that a token is still valid. Browser IP diagnostics describe the browser's exit, which may differ from the proxy used by the Python login process.

## Quick start

Requirements: **Python 3.10+**; Python 3.12 is recommended. Run the following commands on macOS or Linux:

```bash
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT
python3 -m venv .venv
.venv/bin/python -m pip install '.[web]'
.venv/bin/python -m rt_extractor.web --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The three workbench routes are `/` for token extraction, `/converter` for file conversion and `/network` for IP diagnostics. The server binds to loopback and checks Host, Origin and session-bound CSRF. Signed browser sessions own separate workspaces; this is not a public user login or administrator system.

On Windows, create the environment with `python -m venv .venv` and replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

## Extract RT and AT through the web interface

1. Paste one account per line or import a UTF-8 `.txt` file.
2. Use four hyphens between the email, password and Authenticator 2FA secret. Replace all placeholders below.
3. Choose concurrency: **2 by default, from 1 to 3**. A batch accepts at most **1,000 accounts and 2 MiB of input text**.
4. Start extraction and complete any additional email verification requested by the service. Waiting for a verification code occupies a worker slot.
5. On success, view or copy RT, download an account's original JSON or CPA file, or download batch CPA ZIP / Sub2API JSON.

```text
user@example.com----YOUR_PASSWORD----YOUR_BASE32_SECRET
```

The web form requires a Base32 2FA secret or supported `otpauth://totp/` URI. It accepts SHA1, six digits and a 30-second period. All three tokens must be present before extraction is marked successful. Failure or cancellation does not discard already successful outputs.

## Single-account CLI and Python API

The CLI hides password and 2FA input. An empty secret allows interactive entry of the current Authenticator code when required:

```bash
.venv/bin/python -m rt_extractor --email you@example.com --output tokens.json
```

For unattended use, create a private local `account.json`, then run:

```bash
.venv/bin/python -m rt_extractor --input account.json --output tokens.json --non-interactive
```

The input fields are `email`, `password`, optional `totp_secret` and optional `proxy`. Environment variables `RT_EMAIL`, `RT_PASSWORD`, `RT_TOTP_SECRET`, `RT_TOTP_CODE` and `RT_PROXY` are also supported. When `--input` is supplied, account credentials are not mixed with environment variables. Do not commit the input file. Existing output files are not overwritten unless `--overwrite` is explicitly passed.

```python
import asyncio
import os
from rt_extractor import extract_and_save

async def main():
    await extract_and_save(
        os.environ["RT_EMAIL"],
        os.environ["RT_PASSWORD"],
        "tokens.json",
        totp_secret=os.environ["RT_TOTP_SECRET"],
        timeout=180,
    )

asyncio.run(main())
```

`extract_tokens(...)` returns a `TokenBundle` without writing files; `extract_and_save(...)` saves the result; `save_tokens(...)` saves an existing result. The CLI and public Python API handle one account per call. See the [full API and CLI documentation](README.md#命令行与-python-接口) for callbacks and failure types.

## Data handling and limits

| Data or operation | Actual behavior |
| --- | --- |
| Imported conversion JSON | Parsed in page memory; authentication contents are not uploaded or written to browser storage |
| Web token extraction | Credentials and results are processed temporarily in server memory. Results are handed to the owning browser; no token, account-history or result files are written by the web service |
| Passwords and 2FA secrets | Not stored in browser persistence or included in result files; a user-created input file still contains its own secrets |
| Browser result cache | Owner-scoped `localStorage`, valid for 30 minutes and shared by tabs in the same browser session. Web Locks coordinate receipt and clearing; unsupported/blocked storage has explicit fallback limits. Downloads are generated in the browser |
| Server handoff | Successful browser receipt is acknowledged and clears the temporary result; uncollected terminal tasks expire after 10 minutes. Idle active workspaces are cancelled after 10 minutes |
| CLI result files | Explicit `--output` / Python save calls still write plaintext JSON atomically with POSIX `0600` permissions |
| Workspace isolation | Every task operation checks the signed browser session. Another session receives 404 even with a known task ID; missing or forged session cookies are rejected |
| Conversion capacity | Up to 1,000 files/accounts, 2 MiB per document and 20 MiB total |
| Abuse prevention | IP, anonymous browser device, session, account and global quotas; HTTP 429 includes a retry delay |
| Device identification | Signed anonymous cookies, not hardware fingerprints; quota state and signing keys reset when the process restarts |
| Deployment | One process holds task state. Multiple independent workers do not share tasks or quotas |

An AT expiration timestamp does not state the lifetime of RT. Tokens can be revoked by the issuing service, and a converted file is not a successful authentication test. See [security controls](SECURITY.md), the [token guide](docs/TOKEN-GUIDE.md), [FAQ](docs/FAQ.md) and [deployment/update notes](docs/MAINTAINERS.md).

The browser cache contains full credentials, is readable by scripts on the same origin, and is not a permanent backup. Expired entries are removed while the page runs or on its next read; closing every tab does not schedule physical deletion from browser storage. Download needed results promptly and clear them after use. People sharing one browser profile also share its session identity; use separate profiles. Restarting the service cannot restore active logins or uncollected results. Legacy output files are no longer loaded by the web service and are not automatically deleted during an upgrade.

## Updating an existing installation

Update the code and installed package used by the service, then restart that actual Python/Gunicorn process or rebuild and restart its container. Reload the browser afterward. Pulling code without reloading the running app can leave cached HTML templates paired with new module scripts and cause `Cannot use import statement outside a module`. Restarting Nginx alone does not reload Python templates.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
```

Tests use synthetic accounts and mocked network responses, not real account logins. Node.js is needed for frontend tests and an optional authentication VM fallback, not for browser file conversion.

## Source, attribution and license status

The source is publicly available. **A project-wide license has not yet been declared**; public visibility alone does not grant unrestricted reuse or redistribution rights. Do not assume that the entire project is MIT licensed.

The converter is adapted from [CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API) at commit `42ff785`. Its MIT notice is preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Login protocol provenance and implementation boundaries are documented in the [Chinese README](README.md#数据保存与运行限制).

The project also supplies public documentation routes at `/docs`, `/docs/token-extraction`, `/docs/token-types`, `/docs/cpa-sub2api` and `/docs/faq`. Availability depends on deployment of the current version; this README is not an assertion that the live site has been updated. The [SEO / GEO plan](docs/SEO-GEO-PLAN.md) records discoverability work and follow-up measurement.

## Help and contact

- [Report a reproducible issue](https://github.com/d100000/chatGPT2RT-AT/issues) with the version and redacted errors; never include passwords, 2FA secrets or tokens.
- Author: [bbo_dong@qq.com](mailto:bbo_dong@qq.com)
- QQ discussion group: **916444032**
