# chatGPT2RT-AT

输入已有账号的邮箱、密码和 Authenticator 2FA，完成 PKCE OAuth 登录，保存服务端返回的 **AT (`access_token`)、RT (`refresh_token`) 和 ID Token (`id_token`)**。

将三类 Token 一起保存为本地 JSON，提供批量网页、命令行和 Python 接口。项目不依赖原管理后台、数据库、注册、支付或接码平台；命令行和 Python 接口不需要 Flask，网页使用独立的本地服务。

## 安装和运行

要求 Python 3.10+，推荐 Python 3.12。以下命令在 macOS/Linux 终端中执行，`python3` 需对应符合要求的 Python 版本。

克隆仓库并进入项目目录：

```bash
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT
```

### 本地网页

安装网页依赖后启动（只监听本机）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[web]'
.venv/bin/python -m rt_extractor.web --port 8765 --output-dir output
```

打开 `http://127.0.0.1:8765`，批量粘贴账号，或导入 UTF-8 `.txt` 文件。每行一条，格式为：

```text
账号邮箱----账号密码----2FA密钥
```

分隔符为四个英文短横线。字段本身不能包含此分隔符；密码中的空格会原样保留，邮箱和密钥两端的空白会移除。支持 Base32 密钥及符合下文约束的 `otpauth://totp/` URI。

每批最多 1000 条，忽略空行，支持文件开头的 UTF-8 BOM。开始前校验所有行；格式错误、无效密钥或重复邮箱（不区分大小写）会按原始行号提示，整批不启动，不会默默跳过账号。超过上限会明确拒绝。

网页默认同时处理 **2 个账号**，可在「同时处理账号数」选择 **1、2 或 3**；选择 1 为逐条处理。后台使用固定数量的工作线程，按输入顺序从队列领取账号，完成一个后补充下一个，单个账号失败不影响其他账号。同一时刻运行一个批次，使用统一的可选代理。页面中的账号列表及导出结果始终按输入顺序排列，完成先后不会改变账号与 Token 的对应关系。

「正在处理的账号」分别显示各账号进度、运行记录和验证码输入框；同时出现多个验证请求时，请在对应邮箱下提交验证码。等待人工验证码的账号继续占用处理名额，其他名额正常运行；全部名额都等待验证时，需要先输入验证码。每个账号的默认 180 秒总超时包含人工验证码等待。取消整批后会停止领取新账号并取消所有运行任务，页面显示「正在取消」，等待任务收尾后结束批次并保留已有成功结果。同步认证计算或 Node 子进程收尾可能使取消稍晚完成。

认证服务或 Sentinel 返回 HTTP 429 时，该账号记为失败，其余队列按照 `Retry-After` 暂停启动新账号，已运行的账号继续处理。未提供有效等待时间时默认等待 30 秒，等待范围为 1～900 秒，页面显示倒计时；取消整批可立即中断队列等待。限流不会自动重复提交该账号的密码或验证码，可在批次结束后自行重试失败账号。

本次并发数是本地调度上限，不代表已验证的服务端并发配额，也未承诺真实账号的加速倍数。

成功后每个账号的完整 AT、RT、ID Token 自动保存到 `output/batch-<批次ID>/`（或你指定的输出目录）。批次结束后点击「下载 Sub2API JSON」，将成功账号合并为一个与提供样例结构一致的 `sub2api-data` 文件。也可下载原始结果 ZIP（含每个成功账号的原始 JSON 和记录行号、邮箱、状态的 `manifest.json`），或每行一个 RT 的文本；单行仍可下载原始 JSON。取消批次后，已成功的结果仍可下载。

每个成功账号旁提供「查看 RT」按钮，点击后可查看完整 RT、复制或关闭隐藏。RT 按需读取，关闭窗口或开始新批次会清空显示内容，不写入浏览器本地存储。

任务查询只返回状态，不返回 Token 正文；只有主动查看或下载时才返回凭据。密码和 2FA 密钥不会写入磁盘或浏览器本地存储；提交成功后清空输入区。刷新页面可恢复当前批次状态；重启服务可恢复已保存的已结束批次，正在执行的登录不会自动续跑。最近三个批次提供下载接口，更早的结果仍在输出目录。

### 命令行

仅使用命令行时，在仓库根目录安装基础依赖即可：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/rt-extractor --email you@example.com --output tokens.json
```

程序隐藏输入密码与 2FA 密钥。2FA 支持：

- Base32 密钥，如认证器绑定时保存的密钥；在本地按当前时间生成动态码。
- `otpauth://totp/...` URI；只接受 SHA1、6 位、30 秒配置。
- 密钥留空，在服务端请求 2FA 时输入认证器当前的 6 位动态码。

机器时间需准确。已有 6 位动态码可能在网络登录过程中到期，优先使用密钥或在提示出现时输入新码。

输出文件默认不覆盖。确认需要替换时加 `--overwrite`。使用代理可传 `--proxy http://127.0.0.1:7890`，也支持 `socks5://`。`--timeout 180` 设置登录流程的超时秒数。

超时采用协作取消：验证码输入可取消并恢复终端回显；已有认证 VM 计算或其 Node 子进程收尾可能让命令稍晚退出，Node 单次调用上限为 30 秒。

安装后也可以使用 `.venv/bin/python -m rt_extractor`。后文的 `rt-extractor` 命令需在已激活虚拟环境的终端中执行，或替换为 `.venv/bin/rt-extractor`。

## 无交互调用

创建本地 `account.json`（以下为占位示例）：

```json
{
  "email": "you@example.com",
  "password": "YOUR_ACCOUNT_PASSWORD",
  "totp_secret": "YOUR_BASE32_SECRET",
  "proxy": "http://127.0.0.1:7890"
}
```

不需要代理时删除 `proxy` 字段。执行：

```bash
rt-extractor --input account.json --output tokens.json --non-interactive
```

也支持环境变量 `RT_EMAIL`、`RT_PASSWORD`、`RT_TOTP_SECRET`、`RT_TOTP_CODE`、`RT_PROXY`。使用 `--input` 时不会混用环境变量中的其他账号凭证。`totp_secret` 与 `totp_code` 二选一。密码和 2FA 不提供命令行参数，避免出现在进程参数列表中。

如果服务端另外要求邮箱验证码，交互模式会提示输入；无交互模式返回额外验证错误。手机号验证、绑手机或不支持的验证方式会停止并明确报错。2FA 不能替代服务端要求的其他验证。

## Python 接口

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
        # proxy="http://127.0.0.1:7890",
        timeout=180,
    )
    # result.access_token / result.refresh_token / result.id_token
    print("已保存完整 Token")

asyncio.run(main())
```

`extract_tokens(...)` 只返回 `TokenBundle`；`save_tokens(result, path)` 单独保存结果。可传异步 `totp_fetcher() -> str` 在挑战时取当前动态码、`otp_fetcher(email) -> str` 获取额外邮箱验证码，以及同步 `on_step(stage, message)` 接收脱敏进度。

模块为异步接口，调用方已有事件循环时直接 `await`。输入错误为 `ValueError`，网络失败/超时/结果不完整为 `ExtractionError`，协议拒绝为 `rt_extractor._protocol.login.ProtocolError`。额外验证为其子类 `AdditionalVerificationRequired`，可通过 `.kind` 区分。

## 保存格式

### 原始 Token JSON

每个账号的磁盘文件、单行下载及 ZIP 内的账号文件使用以下格式：

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

三类 Token 缺少任何一个都会失败，不把部分结果标为成功。到期时间指 AT 的有效期，不代表 RT 的寿命。账号标识等元数据取自 Token claims，仅作为信息展示，不用于身份验证。文件不保存密码、2FA 密钥、代理凭证。文件以原子方式写入，POSIX 权限为 `0600`，内容为明文凭证；输出不显示在普通日志中。

### Sub2API 批量 JSON

网页的「下载 Sub2API JSON」使用样例的字段层级和默认配置。`accounts` 按输入顺序收集本批次成功的账号，失败和取消的账号不写入；`exported_at` 是下载生成时的 UTC 时间，保留毫秒并以 `Z` 结尾。`proxies` 固定为空数组，登录使用的代理及其凭据不会导出。

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

邮箱、三类 Token、账号 ID 和用户 ID 均取自本次成功提取的账号，不复用样例中的账号数据。导出账号中的 `concurrency: 10` 是样例的 Sub2API 配置字段，与网页选择的本批次同时处理账号数无关。`privacy_mode` 等配置值沿用样例以兼容其格式；本工具没有修改账号训练或隐私设置，这些字段不代表服务端设置的检查结果。

## 实现与验证范围

登录协议代码整理自原项目的 `botcore/protocol_register`，保留登录所需的 PKCE、HTTP、TOTP 和认证支持代码；本仓库不依赖原项目的数据库、注册入口或自动接码模块。新客户端开启 TLS 验证，OAuth 回调核对完整地址及 state，并在请求前截获回调，不依赖本机端口 1455 正在监听。

这是对现有项目协议的整理，涉及私有登录接口，可能随服务端更新而失效。官方文档描述的是浏览器或设备码登录，没有为本项目的密码/TOTP HTTP 协议提供稳定性承诺：[OpenAI 认证文档](https://learn.chatgpt.com/docs/auth)。本次仅以模拟网络和合成凭证验证代码；没有执行真实账号登录，因此不承诺当前账号能成功获取 RT。

离线测试，在仓库根目录执行（先安装网页依赖）：

```bash
.venv/bin/python -m unittest discover -s tests -v
```


## 目录结构

```text
chatGPT2RT-AT/
├── README.md
├── pyproject.toml
├── requirements.txt
├── rt_extractor/          # 独立模块、网页和登录协议
└── tests/                 # 模拟网络和合成账号测试
```

当前网页仅监听本机，不包含公网管理员登录。将其部署到远程服务器并开放浏览器访问前，需要配置 HTTPS、身份验证和私有持久化存储；当前内存任务调度按单应用进程运行，服务重启不会续跑进行中的登录。
