<h1 align="center">bob ai tools</h1>

<p align="center">Token extraction · Authentication file conversion · Model evaluation</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Flask-3.1%2B-000000?logo=flask&logoColor=white" alt="Flask 3.1+">
  <img src="https://img.shields.io/badge/JavaScript-ES%20modules-F7DF1E?logo=javascript&logoColor=black" alt="JavaScript ES modules">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README.en.md">English</a> ·
  <a href="https://tools.bob-api.com/">Project website</a> ·
  <a href="https://github.com/d100000/chatGPT2RT-AT/issues">Report an issue</a>
</p>

## Overview

**A local web workbench for ChatGPT account tokens, authentication files and model testing.** Sign in to an account you own or are authorized to use, receive RT / AT / ID Token, and export CPA or Sub2API files. The same app includes browser IP diagnostics, repeated SVG evaluations and concurrency benchmarks.

The project is independent and unofficial, with no affiliation with or endorsement from OpenAI. Account tokens are not OpenAI API keys. Login compatibility depends on the upstream authentication service; the tool does not register accounts or automatically renew refresh tokens.

[Quick start](#quick-start) · [Token workflow](#extract-rt-and-at-through-the-web-interface) · [Data handling](#data-handling-and-limits) · [Documentation](#documentation) · [Development](#development-and-tests)

## Choose a workflow

| Tool | Route | What it does |
| --- | --- | --- |
| Token extraction | `/` | Batch login, email-code handling, complete RT / AT / ID Token results and browser-generated exports |
| Authentication converter | `/converter` | Convert existing CPA ↔ Sub2API JSON in the browser, including recently received extraction results |
| IP diagnostics | `/network` | Inspect the browser's exit IP, location, available IPOK risk score and supporting evidence |
| Pelican evaluation | `/pelican` | Repeat an SVG generation task, compare samples, collect human or optional AI reviews and export reports |
| Concurrency benchmark | `/benchmark` | Compare models across concurrency levels and input modes using latency, throughput, success and cache metrics |

Token extraction also has a single-account CLI and Python API. Model evaluation and benchmarking use an API connection you configure separately.

## Quick start

Requires **Python 3.10+**. Python 3.12 is recommended. On macOS or Linux:

```bash
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT
python3 -m venv .venv
.venv/bin/python -m pip install '.[web]'
.venv/bin/python -m rt_extractor.web --port 8765
```

Open **[http://127.0.0.1:8765](http://127.0.0.1:8765)**. All five tools are available from the navigation.

<details>
<summary>Windows / PowerShell</summary>

```powershell
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT
python -m venv .venv
.venv\Scripts\python.exe -m pip install ".[web]"
.venv\Scripts\python.exe -m rt_extractor.web --port 8765
```

For the remaining commands, replace `.venv/bin/python` with `.venv\Scripts\python.exe`. Calling the environment's interpreter directly does not require activating it.

</details>

The server binds to loopback and checks Host, Origin and session-bound CSRF. Signed browser sessions own separate workspaces; the app does not provide public user accounts or an administrator login. Deployment and update details are in [MAINTAINERS.md](docs/MAINTAINERS.md).

## Extract RT and AT through the web interface

1. Paste an account list or import a UTF-8 `.txt` file. Use four hyphens between each field:

   ```text
   user@example.com----YOUR_PASSWORD----YOUR_BASE32_SECRET
   ```

2. Choose **1–3 concurrent accounts**, with **2** selected by default. A batch accepts up to **1,000 accounts and 2 MiB of input text**.
3. If needed, enter a shared proxy under connection settings, then start the batch.
4. Follow each account's progress. Enter a requested email code, skip that account, or enable automatic email-verification skipping.
5. Once a successful result reaches this browser, view or copy RT and download the account's original JSON or CPA JSON.
6. Export received successes as merged Sub2API JSON, CPA ZIP, original-result ZIP or one RT per line. You can also open them directly in the converter.

The web form requires a Base32 2FA secret or supported `otpauth://totp/` URI. Supported settings are SHA1, six digits and a 30-second period. An account is successful only when **all three tokens** are present. One failure does not stop the remaining queue.

### Skip accounts that require email verification

The **Skip email verification** switch is **off by default**. Turn it on before starting or while a batch is running:

- Currently waiting email challenges and future email challenges end that account's extraction as **Skipped**.
- The worker completes cleanup, releases its slot and continues with the next queued account.
- **Skip this account** performs the same action for one waiting email challenge while leaving the batch setting unchanged.
- Turning the switch off restores normal email-code prompts for future challenges; already skipped accounts are not resumed.

**Skipped accounts produce no Token result.** This skips the account, not the authentication service's requirement. Authenticator / TOTP handling is unchanged. Skipped, failed and cancelled accounts have separate states; completed progress includes skipped accounts.

### Results and exports

Downloads use successful results **already received by the current browser**; they do not require the entire batch to finish. Failed, skipped and unfinished accounts do not contribute Token files. The original-result ZIP also includes a manifest of account statuses.

Results and exports retain input order even when accounts finish in a different order. Cancelling a batch preserves received successes. Use **End task and clear** to stop the current session's extraction and clear its browser results.

`expires_at` describes AT expiry, not RT lifetime. Token claims are displayed as metadata; parsing them does not verify their signature or prove that authentication remains valid.

## Converter, diagnostics and evaluation

### CPA ↔ Sub2API conversion

Import or paste authentication JSON at `/converter`, or load recent extraction results. The converter supports Codex/OpenAI, Claude, Antigravity and Gemini formats, with merged JSON, individual CPA files and ZIP downloads.

Manually imported content is processed in page memory. Conversion maps existing fields: it does not log in, refresh credentials or check whether a Token is valid. See the [technical reference](docs/REFERENCE.md) for supported fields, limits and examples.

### Browser IP diagnostics

`/network` requests IPOK diagnostics directly from the browser. It displays the available location, official risk score and evidence status. The browser's network exit can differ from the proxy used by the Python login process; an unavailable score is not a clean-IP result.

### Pelican SVG evaluation

`/pelican` runs the “pelican riding a bicycle” SVG task through a configured Responses or Chat Completions endpoint. It supports single runs, batches, scheduled repetition, continuous loops, sample comparisons, human scoring, blind reviews and optional AI image reviews.

History includes original outputs, technical checks and review evidence, with JSON / CSV / HTML / SVG exports. This is a specific visual-task evaluation, not an IQ test, general intelligence score or proof of upstream model identity. See [the Pelican guide](docs/PELICAN.md) for rules and storage details.

### Concurrency benchmarks

`/benchmark` compares selected models, concurrency levels and input modes. Reports cover time to first visible output, latency distributions, throughput, success rate and provider-reported cache usage. Responses and Chat Completions are supported, with HTML / CSV / JSON exports.

Benchmark and Pelican jobs continue on the server when you switch tools, refresh or close the page. Stop them explicitly when finished. Cancelling a local request cannot guarantee that the upstream service stops computation or billing. Metric definitions, history behavior and mock tests are in [the benchmark guide](docs/benchmark.md).

## Data handling and limits

**Token extraction uses temporary server memory and a browser result cache. Evaluation tools have separate persistent history.** Their storage rules are different:

| Data | Processing and retention |
| --- | --- |
| Token extraction credentials | Temporarily processed by the Python server; passwords and 2FA secrets are not written to browser persistence or Token result files |
| Token results in the browser | Session-owner-scoped `localStorage`, valid for **30 minutes**; shared across tabs with Web Locks coordinating receipt and clearing. Blocked or unsupported storage falls back to page memory |
| Token results on the server | Memory only; acknowledged results are cleared. Uncollected results and terminal tasks expire after **10 minutes**; active workspaces idle for 10 minutes are cancelled and cleared |
| Manually imported converter files | Parsed and converted in page memory; authentication contents are not uploaded or written to browser storage |
| IP diagnostics | Browser `sessionStorage` caches diagnostics and cooldown information |
| Pelican history | Persistent SQLite at `~/.local/share/bob-ai-tools/pelican/pelican.sqlite3`; configurable with `GPT_EVAL_DATA_DIR` |
| Benchmark history | Persistent JSON under `~/.local/share/bob-ai-tools/benchmark/`, including batch summaries and request records; configurable with `GPT_BENCHMARK_DATA_DIR` |
| Evaluation API keys | Used in input fields and server memory; not saved in browser storage, evaluation history or exports. A benchmark shared connection may retain its key in process memory until cleared or restarted |
| CLI / Python output | Explicit save calls write plaintext JSON atomically, with POSIX `0600` permissions |
| Session identity | Signed anonymous cookies enforce ownership and quotas; no hardware fingerprinting. People sharing a browser profile share its session identity |

The Token browser cache contains full credentials and is readable by scripts on the same origin. It is not a permanent backup. Expired entries are removed while the page runs or on its next read; closing all tabs does not schedule physical deletion from browser storage. Download needed results and clear them after use.

Restarting the service cannot restore active Token logins or uncollected Token results. Evaluation history survives separately; access depends on retaining the corresponding history identity and browser cookies. Upgrades do not automatically delete legacy Token files or evaluation history.

Task state and quotas use a **single application process**. Multiple independent workers do not share scheduling or quotas. Limits apply by IP, anonymous device, session, account and operation; HTTP 429 responses provide a retry delay. The [technical reference](docs/REFERENCE.md) and [security policy](SECURITY.md) describe capacities and controls.

## Single-account CLI and Python API

For one authorized account, run:

```bash
.venv/bin/python -m rt_extractor --email you@example.com --output tokens.json
```

Password and 2FA prompts are hidden. Leaving the secret empty allows interactive entry of an Authenticator code when requested. Existing output files require an explicit `--overwrite` to replace them.

For unattended use, create a private local input file and run:

```bash
.venv/bin/python -m rt_extractor --input account.json --output tokens.json --non-interactive
```

The Python API exposes async `extract_tokens(...)` and `extract_and_save(...)`, plus `save_tokens(...)`. CLI and public API calls process one account at a time; the email-skip switch belongs to the web batch workflow. Full input examples, environment variables, callbacks and error types are in [CLI and Python reference](docs/REFERENCE.md#命令行与-python-接口) (Chinese).

## Documentation

| Document | Contents |
| --- | --- |
| [Token guide](docs/TOKEN-GUIDE.md) | Account input, extraction and export workflow |
| [Technical reference](docs/REFERENCE.md) | Token definitions, file formats, fields, CLI / API examples and detailed limits |
| [Pelican evaluation](docs/PELICAN.md) | Task rules, scoring, history and SVG validation |
| [Concurrency benchmarks](docs/benchmark.md) | Configuration, metric definitions, storage and exports |
| [FAQ](docs/FAQ.md) | Common setup, authentication and result questions |
| [Stalled batch troubleshooting](docs/BATCH-TROUBLESHOOTING.md) | Stale progress after task expiry, preserved results and recovery steps |
| [Maintenance](docs/MAINTAINERS.md) | Deployment, upgrades and troubleshooting |
| [Security](SECURITY.md) | Session isolation, request limits and reporting guidance |

Detailed guides are currently in Chinese. The app also serves public documentation at `/docs`, `/docs/token-extraction`, `/docs/token-types`, `/docs/cpa-sub2api` and `/docs/faq`. These routes depend on the deployed version; this README does not verify the live website's deployment state.

<a id="tests"></a>
## Development and tests

Install the web dependencies, then run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
```

Tests use synthetic accounts and mocked responses or local mock servers, without real account logins or paid model calls. Node.js is required for frontend tests and an optional authentication VM fallback, not for browser conversion. Optional browser checks are documented in the individual evaluation guides.

```text
rt_extractor/    Python services, login protocol, templates and browser modules
tests/          Python, JavaScript and optional browser tests
docs/           User guides, technical reference and maintenance notes
```

## Updating an existing installation

Update the checkout and reinstall the package in the environment used by the running service:

```bash
git pull --ff-only
.venv/bin/python -m pip install '.[web]'
```

Then restart the actual Python / Gunicorn service and reload the browser. Pulling code without restarting can pair cached HTML templates with new module scripts and cause `Cannot use import statement outside a module`. Restarting Nginx alone does not reload Python templates. See [maintenance notes](docs/MAINTAINERS.md) for your deployment's update path.

## Source, attribution and license status

Source is publicly available, but **a project-wide license has not yet been declared**. Public visibility alone does not grant unrestricted reuse or redistribution rights. The MIT notice for a bundled third-party component does not license the entire project.

The converter is adapted from [CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API), commit `42ff785`. Its license and modification notes are preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Login protocol code was separated from the original project's `botcore/protocol_register`; implementation boundaries are described in the [technical reference](docs/REFERENCE.md).

## Help and contact

- [Report a reproducible issue](https://github.com/d100000/chatGPT2RT-AT/issues) with the version and redacted errors; omit account credentials, API keys and Tokens.
- Author: [bbo_dong@qq.com](mailto:bbo_dong@qq.com)
- QQ discussion group: **916444032**
- Repository: [d100000/chatGPT2RT-AT](https://github.com/d100000/chatGPT2RT-AT)
