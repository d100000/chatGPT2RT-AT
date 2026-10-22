# chatGPT2RT-AT

一个面向已有 ChatGPT 账号的 Token 提取工具。输入**邮箱、密码和 Authenticator 2FA 密钥**，完成登录后保存 **AT、RT 和 ID Token**，支持本地网页批量处理、命令行和 Python 调用。

```text
邮箱 + 密码 + 2FA → 登录验证 → 获取 AT / RT / ID Token → 查看、复制或导出
```

## 功能概览

| 功能 | 说明 |
| --- | --- |
| 完整 Token 提取 | 同时保存 Access Token（AT）、Refresh Token（RT）和 ID Token；缺少任意一种时不标记为成功 |
| 批量导入 | 网页支持粘贴账号列表或导入 UTF-8 `.txt` 文件，每批最多 1000 个账号、2 MiB 文本 |
| 并发处理 | 默认同时处理 2 个账号，可选 1～3 个；空出名额后自动处理下一个账号 |
| 验证码交互 | 使用 2FA 密钥生成动态码；遇到额外邮箱验证时，在对应账号下手动输入验证码 |
| 查看与复制 RT | 成功账号旁提供「查看 RT」按钮，可显示完整内容、复制或关闭隐藏 |
| 多种导出格式 | 支持 Sub2API JSON、原始 Token JSON、成功结果 ZIP，以及每行一个 RT 的文本 |
| 进度与取消 | 分别显示账号状态和运行记录；单个账号失败继续处理，取消整批后保留成功结果 |
| 限流等待 | 收到 HTTP 429 后暂停启动后续账号，显示等待时间；已运行账号继续处理 |
| 代理与独立调用 | 支持统一代理；提供单账号 CLI 和异步 Python API，可脱离原管理后台运行 |

## 快速开始

需要 **Python 3.10+**，推荐 Python 3.12。以下命令以 macOS/Linux 为例，在终端中执行：

```bash
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT

python3 -m venv .venv
.venv/bin/python -m pip install '.[web]'
.venv/bin/python -m rt_extractor.web --port 8765 --output-dir output
```

启动后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

Windows 创建虚拟环境时可使用 `python -m venv .venv`，后续将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

### 网页使用流程

1. 粘贴账号列表，或点击「导入 .txt 文件」。
2. 选择「同时处理账号数」；默认 2，选择 1 为逐条处理。
3. 根据需要在「连接设置」中填写统一代理地址，然后开始提取。
4. 查看各账号的处理状态；如需邮箱验证码，在对应邮箱下输入。
5. 账号成功后可立即查看 RT、复制 RT 或下载该账号的原始 JSON。
6. 批次结束后，点击「下载 Sub2API JSON」合并导出成功账号，也可下载 ZIP 或 RT 文本。

## 账号输入格式

每行一个账号，使用四个英文短横线 `----` 分隔：

```text
邮箱----密码----2FA密钥
```

以下为占位示例，使用时需替换为实际账号信息：

```text
user1@example.com----YOUR_PASSWORD----YOUR_BASE32_SECRET
user2@example.com----YOUR_PASSWORD----YOUR_BASE32_SECRET
```

- 网页批量输入的三个字段均不能为空，2FA 支持 Base32 密钥或 `otpauth://totp/` URI。
- `otpauth` URI 只接受 SHA1、6 位动态码、30 秒周期；机器时间应保持准确。
- 支持 UTF-8 文本、文件开头的 BOM，忽略空行。字段中不能包含分隔符 `----`。
- 邮箱和密钥两端的空白会移除，密码中的空格原样保留。
- 开始前会校验全部行。格式错误、无效密钥或重复邮箱会按行号提示，整批不会启动；邮箱重复判断不区分大小写。

## 并发、验证码和限流

| 设置 | 当前行为 |
| --- | --- |
| 同时处理账号数 | 可选 1、2、3，默认 2；同一时刻运行一个批次 |
| 任务补位 | 账号结束后立即从队列领取下一个；单个账号失败不影响其他账号 |
| 人工验证码 | 每个账号有独立表单；等待验证仍占用名额，其他名额继续运行 |
| 单账号超时 | 网页默认 180 秒，包含人工验证码等待；CLI 可通过 `--timeout` 调整 |
| 统一代理 | 适用于本批次全部账号；支持 HTTP、HTTPS、SOCKS5、SOCKS5H，留空直连 |
| 取消整批 | 停止领取新账号、取消运行任务，等待收尾后结束，保留已成功的结果 |

如果全部处理名额都在等待验证码，需要先完成验证才会继续派发。验证码可能过期，应在对应账号下及时提交。

收到认证服务或 Sentinel 的 HTTP 429 时，限流账号记为失败，其余队列按照 `Retry-After` 等待后继续。无有效等待时间时默认等待 30 秒，等待范围为 1～900 秒。程序不会因此自动重新提交该账号的密码或验证码，可在批次结束后重新导入失败账号。

取消采用协作方式。同步认证计算或 Node 子进程需要收尾时，页面会暂时显示「正在取消」；Node 单次调用上限为 30 秒。

## 查看结果与下载

| 操作 | 可用时机 | 内容 |
| --- | --- | --- |
| 查看 RT / 复制 RT | 单个账号成功后 | 该账号完整的 Refresh Token，关闭后隐藏并清空显示内容 |
| 原始 JSON | 单个账号成功后 | AT、RT、ID Token、有效期和账号元数据 |
| Sub2API JSON | 批次完成或取消后，且至少有一个成功账号 | 将成功账号合并为一个 `sub2api-data` 文件 |
| 原始结果 ZIP | 同上 | 每个成功账号的 JSON，以及包含所有账号行号、邮箱、状态的 `manifest.json` |
| RT 文本 | 同上 | 仅成功账号的 RT，每行一个 |

列表及批量导出始终按输入顺序排列，不受账号完成先后影响。失败和未完成账号不会混入成功 Token 导出。

使用快速开始中的启动命令时，每个成功账号的原始 JSON 会自动保存到：

```text
output/
└── batch-<批次ID>/
    ├── tokens-<任务ID>.json
    └── batch-state.json
```

`batch-state.json` 在批次结束后保存状态，不包含 Token 正文。输出目录可通过 `--output-dir` 修改；不指定时默认为 `rt-output/`。

<details>
<summary>原始 Token JSON 示例</summary>

```json
{
  "email": "you@example.com",
  "access_token": "<AT>",
  "refresh_token": "<RT>",
  "id_token": "<ID Token>",
  "expires_in": 3600,
  "obtained_at": "2026-10-22T00:00:00+00:00",
  "expires_at": "2026-10-22T01:00:00+00:00",
  "chatgpt_account_id": "...",
  "chatgpt_user_id": "...",
  "plan_type": "...",
  "sub": "...",
  "token_type": "Bearer"
}
```

`expires_at` 表示 AT 的到期时间，不代表 RT 的寿命。账号标识等元数据来自 Token claims，仅作为信息展示，不用于身份验证。

</details>

<details>
<summary>Sub2API 批量 JSON 示例</summary>

```json
{
  "type": "sub2api-data",
  "version": 1,
  "exported_at": "2026-10-22T00:00:00.000Z",
  "proxies": [],
  "accounts": [
    {
      "name": "oauth---you@example.com",
      "platform": "openai",
      "type": "oauth",
      "credentials": {
        "access_token": "<AT>",
        "chatgpt_account_id": "<账号 ID>",
        "email": "you@example.com",
        "id_token": "<ID Token>",
        "refresh_token": "<RT>"
      },
      "extra": {
        "account_id": "<账号 ID>",
        "chatgpt_account_id": "<账号 ID>",
        "chatgpt_user_id": "<用户 ID>",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "email": "you@example.com",
        "openai_long_context_billing_enabled": false,
        "openai_oauth_responses_websockets_v2_enabled": false,
        "openai_oauth_responses_websockets_v2_mode": "off",
        "privacy_mode": "training_set_failed"
      },
      "concurrency": 10,
      "priority": 1,
      "rate_multiplier": 1,
      "auto_pause_on_expired": true
    }
  ]
}
```

`accounts` 只包含成功账号；`exported_at` 为生成文件时的 UTC 时间，`proxies` 固定为空数组，不导出登录代理及其凭据。

导出字段中的 `concurrency: 10` 是 Sub2API 账号配置，与网页选择的同时处理账号数无关。`privacy_mode` 等字段沿用导出格式的配置值，不表示本工具修改或核验了账号的服务端隐私设置。

</details>

## 命令行与 Python 接口

CLI 和公开 Python API 用于**单个账号**；批量导入及并发控制通过网页使用。仅需 CLI/API 时，安装命令可使用 `.venv/bin/python -m pip install .`，无需 Flask。

### 交互式命令行

```bash
.venv/bin/python -m rt_extractor --email you@example.com --output tokens.json
```

程序隐藏输入密码和 2FA 密钥。密钥留空时，可在需要验证时输入 Authenticator 当前的 6 位动态码。已有动态码可能在登录过程中到期，优先使用密钥或按提示输入新码。

输出文件默认不覆盖；可使用 `--overwrite` 明确覆盖。其他参数示例：

```bash
.venv/bin/python -m rt_extractor --email you@example.com --output tokens.json --proxy http://127.0.0.1:7890 --timeout 180
```

### 无交互调用

自行创建本地 `account.json`，以下内容均为占位值；不需要代理时删除 `proxy` 字段：

```json
{
  "email": "you@example.com",
  "password": "YOUR_ACCOUNT_PASSWORD",
  "totp_secret": "YOUR_BASE32_SECRET",
  "proxy": "http://127.0.0.1:7890"
}
```

```bash
.venv/bin/python -m rt_extractor --input account.json --output tokens.json --non-interactive
```

也可使用环境变量 `RT_EMAIL`、`RT_PASSWORD`、`RT_TOTP_SECRET`、`RT_TOTP_CODE`、`RT_PROXY`；`totp_secret` 与 `totp_code` 二选一。指定 `--input` 时，不会混用环境变量中的账号凭证。密码和 2FA 不提供命令行参数，避免出现在进程参数列表中。

如需额外邮箱验证码，交互模式会提示输入，无交互模式返回额外验证错误。

### Python 调用

```python
import asyncio
import os
from rt_extractor import extract_and_save

async def main():
    result = await extract_and_save(
        os.environ["RT_EMAIL"],
        os.environ["RT_PASSWORD"],
        "tokens.json",
        totp_secret=os.environ["RT_TOTP_SECRET"],
        timeout=180,
    )
    # result.access_token / result.refresh_token / result.id_token
    print("已保存完整 Token")

asyncio.run(main())
```

- `extract_tokens(...)`：异步登录并返回 `TokenBundle`，不写文件。
- `extract_and_save(...)`：异步登录并保存三类 Token。
- `save_tokens(result, path)`：单独保存已有结果。

支持异步 `totp_fetcher() -> str`、`otp_fetcher(email) -> str` 回调，以及同步 `on_step(stage, message)` 脱敏进度回调。调用方已有事件循环时直接 `await`。

输入错误为 `ValueError`；网络失败、超时或结果不完整为 `ExtractionError`；协议拒绝为 `rt_extractor._protocol.login.ProtocolError`。额外验证为其子类 `AdditionalVerificationRequired`，可通过 `.kind` 区分。

## 数据保存与运行限制

- **凭据保存**：程序不会把输入的密码、2FA 密钥或代理凭证写入 Token 结果；网页提交成功后清空输入，也不使用浏览器本地存储保存这些内容。自行创建的 `account.json` 本身包含凭据，需自行保管。
- **Token 文件**：保存为明文 JSON，以原子方式写入，POSIX 权限为 `0600`。查看 RT 时按需读取，普通状态查询不返回 Token 正文。
- **刷新与重启**：刷新页面可恢复当前批次；服务重启可恢复最近三个已保存的已结束批次，无法续跑正在执行的登录。已写出的 Token 文件仍保留在输出目录。
- **本地运行**：当前仅允许本机访问，不包含公网管理员登录。远程开放访问前，需要增加 HTTPS、身份验证和私有持久化存储。
- **进程模型**：任务状态保存在单个应用进程的内存中，不能直接增加多个应用副本共享当前任务。
- **功能边界**：处理已有账号，不包含自动注册、手机号验证、自动邮箱取码或 RT 自动续期。2FA 不能替代服务端要求的其他验证。
- **兼容性**：登录使用私有接口，可能随服务端更新失效；并发数是本地上限，不代表服务端配额或实际加速倍数。Node.js 仅用于可选的认证 VM 回退，并非基础依赖。

登录协议整理自原项目的 `botcore/protocol_register`，保留 PKCE、HTTP、TOTP 和认证支持代码，独立于原管理后台、数据库、注册、支付及接码模块。客户端开启 TLS 验证，核对 OAuth 回调地址和 state，并在请求前截获回调，不依赖本机端口 1455 正在监听。认证背景可参考 [OpenAI 认证文档](https://learn.chatgpt.com/docs/auth)。

## 测试与项目结构

安装网页依赖后，在仓库根目录执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用合成账号和模拟网络，覆盖登录协议、保存、网页交互接口、并发调度、验证码、取消、限流和导出，不会登录真实账号。

```text
chatGPT2RT-AT/
├── README.md
├── pyproject.toml
├── requirements.txt
├── rt_extractor/          # 独立模块、网页和登录协议
└── tests/                 # 合成账号和模拟网络测试
```
