# Token、转换与运行参考

[返回项目 README](../README.md) · [Token 提取指南](TOKEN-GUIDE.md) · [常见问题](FAQ.md)

本页收录 Token 获取、格式转换、IP 检测及 CLI / Python 的详细约定。安装与五个工具入口见 [README](../README.md)；GPT 能力评测和并发分析分别见 [鹈鹕评测说明](PELICAN.md)与 [并发分析说明](benchmark.md)。

[Token 术语](#token-types) · [账号输入](#账号输入格式) · [并发与验证](#并发验证码和限流) · [结果与下载](#查看结果与下载) · [格式转换](#cpa--sub2api-格式转换) · [IP 检测](#ip-纯净度检测) · [CLI / Python](#命令行与-python-接口) · [隐私](#使用声明与隐私) · [运行边界](#数据保存与运行限制)

<a id="token-types"></a>
## RT、AT 和 ID Token 有什么区别？

| 名称 | 完整名称 / 导出字段 | 本项目中的含义 |
| --- | --- | --- |
| RT | Refresh Token / `refresh_token` | 认证服务签发的刷新凭据；本项目提取并导出它，不提供 RT 自动续期功能 |
| AT | Access Token / `access_token` | 用于访问授权资源的访问令牌；导出中的 `expires_at` 描述 AT 到期时间 |
| ID Token | ID Token / `id_token` | 包含登录身份相关 claims 的令牌；页面中的元数据解析不等于验证签名或认证有效性 |
| API Key | API 密钥 | 与上述账号 Token 不同；本项目不会为账号创建 API Key |

术语背景见 [OAuth 2.0 的 AT / RT 定义](https://www.rfc-editor.org/rfc/rfc6749#section-1.4)与 [OpenID Connect 的 ID Token 定义](https://openid.net/specs/openid-connect-core-1_0.html#IDToken)。本项目的支持范围与实际代码为准，平台可能撤销凭据或改变登录要求。

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
| 同时处理账号数 | 每个浏览器会话可选 1、2、3，默认 2；每会话同一时刻运行一个批次；全进程最多 6 个登录、8 个工作空间 |
| 任务补位 | 账号结束后立即从队列领取下一个；单个账号失败不影响其他账号 |
| 人工验证码 | 每个账号有独立表单；等待验证仍占用名额，其他名额继续运行 |
| 跳过邮箱验证 | 默认关闭，运行中可切换；开启后跳过当前和后续需要邮箱验证码的账号，也可单独点击「跳过此账号」；已跳过单独统计，不生成 Token，不影响 Authenticator 验证 |
| 单账号超时 | 网页默认 180 秒，包含人工验证码等待；CLI 可通过 `--timeout` 调整 |
| 统一代理 | 适用于本批次全部账号；支持 HTTP、HTTPS、SOCKS5、SOCKS5H，留空直连 |
| 取消整批 | 停止领取新账号、取消运行任务，等待收尾后结束，保留已成功的结果 |

如果全部处理名额都在等待验证码，可完成验证或跳过等待邮箱验证的账号，让队列继续。关闭「跳过邮箱验证」仅影响后续验证，已跳过的账号不会自动重试，需要在本批次结束后重新导入。验证码可能过期，应在对应账号下及时提交。

收到认证服务或 Sentinel 的 HTTP 429 时，限流账号记为失败，其余队列按照 `Retry-After` 等待后继续。无有效等待时间时默认等待 30 秒，等待范围为 1～900 秒。程序不会因此自动重新提交该账号的密码或验证码，可在批次结束后重新导入失败账号。

取消采用协作方式。同步认证计算或 Node 子进程需要收尾时，页面会暂时显示「正在取消」；Node 单次调用上限为 30 秒。

### 网页跳过接口

以下接口需要所属浏览器的有效签名会话 Cookie 和页面的 `X-RT-CSRF`，只接受 JSON。跳过设置仅用于网页批次，不是 CLI / Python 提取参数。

| 操作 | 请求 | 行为 |
| --- | --- | --- |
| 创建批次时选择 | `POST /api/batches`，加入 `"skip_email_verification": true` 或 `false` | 字段省略时默认关闭；必须为 JSON 布尔值 |
| 修改进行中的批次 | `POST /api/batches/<batch_id>/settings`，正文仅为 `{"skip_email_verification": true}` 或 `false` | 更新当前及后续邮箱验证处理方式；开启后结束当前等待邮箱验证码的账号 |
| 跳过单个等待账号 | `POST /api/jobs/<job_id>/skip`，正文仅为 `{"challenge_id": "当前挑战ID"}` | 只接受当前匹配的邮箱挑战；不接受 Authenticator 挑战 |

设置和单个跳过接口的正文上限均为 1 KiB。无效字段返回 400，已失效挑战或已结束批次返回 409，不属于当前会话的任务返回 404。已跳过状态为 `skipped`，在 `counts.skipped` 单独统计；任务先完成收尾，再释放处理名额。

### 本站请求防护

网页服务同时按 **IP、设备、Session、全局** 检查额度，直接调用 API 的脚本同样受限。“设备”指服务器签名 Cookie 识别的浏览器配置，不是物理硬件。IP 使用实际连接地址；当前仅允许本机访问，因此本机浏览器与脚本共享 IP 额度。

| 操作 | 每 IP | 每设备 | 每 Session | 服务进程总额度 / 额外限制 |
| --- | --- | --- | --- | --- |
| 页面、状态查询及其他动态请求 | 600 次 / 分钟、120 次 / 秒 | 450 次 / 分钟、90 次 / 秒 | 300 次 / 分钟、60 次 / 秒 | 2400 次 / 分钟、240 次 / 秒 |
| 静态资源（含不存在的资源） | 1200 次 / 分钟、240 次 / 秒 | 同 IP | 同 IP | 4800 次 / 分钟、480 次 / 秒；独立于动态请求 |
| 提交单账号或批次 | 10 次 / 分钟 | 8 次 / 分钟 | 6 次 / 分钟 | 两个入口共享；合计 30 次 / 分钟 |
| 账号处理量 | 2000 个 / 小时 | 1500 个 / 小时 | 1000 个 / 小时 | 5000 个 / 小时；批次按账号数量计费 |
| 提交验证码 | 15 次 / 分钟 | 12 次 / 分钟 | 10 次 / 分钟 | 60 次 / 分钟；同一任务最多 5 次 / 分钟 |
| 服务端结果交接 | 30 次 / 分钟 | 24 次 / 分钟 | 20 次 / 分钟 | 合计 120 次 / 分钟，最多同时读取 2 个响应；浏览器已接收结果的下载不调用此接口 |
| 任务控制：取消、跳过、设置、交接确认与清理 | 独立 60 次 / 分钟 | 独立 60 次 / 分钟 | 独立 60 次 / 分钟 | 独立 240 次 / 分钟 |
| Host、Origin 或 CSRF 校验失败 | 10 次 / 5 分钟 | 10 次 / 5 分钟 | 10 次 / 5 分钟 | 超出返回 429，正确的访问校验仍可通过 |
| 签发设备 / Session 标识 | 20 次 / 10 分钟 | — | — | 60 次 / 10 分钟，仅合法页面缺少有效标识时签发 |

同一邮箱另外受跨 IP、设备和 Session 合计 **5 次 / 15 分钟** 的提取限制，邮箱大小写不影响计数。合法标识在换 IP 后保持原额度，开新标签页复用浏览器会话。没有有效 Cookie 的请求仍消耗 IP 匿名限流额度，但任务 API 会拒绝访问，不能以 IP 代替会话授权。清 Cookie、更换 Session 或伪造标识不会重置 IP、账号或全局计数。

多条规则同时适用时，任意一条超限都会返回 HTTP `429`，并通过 `Retry-After` 响应头和 JSON `retry_after` 提供等待秒数。反复请求不会延长原有窗口。页面显示倒计时、保留尚未提交成功的输入，并暂停相关请求；不会自动重新提交密码或验证码。状态查询被限流时，仍可使用独立的任务控制额度取消任务、跳过邮箱验证账号或修改跳过设置。跳过和设置请求与取消共享控制额度，不消耗验证码提交额度。

账号额度在整批成功进入队列时扣除，包含后来失败、未运行或取消的账号；重复取消再提交无法退还额度。任务冲突或启动异常不扣账号额度，批次中任一账号超限时整批不启动。请求正文、字段长度和限流计数器数量也有上限。

这些限制在单个进程内生效，重启后重置，不依赖数据库。它们不替代公网身份验证或入口防护。策略定义、调整示例、请求大小及服务器部署边界见 [安全策略说明](../SECURITY.md)。

## 查看结果与下载

| 操作 | 可用时机 | 内容 |
| --- | --- | --- |
| 查看 RT / 复制 RT | 浏览器接收该账号的成功结果后 | 该账号完整的 Refresh Token，关闭后隐藏并清空显示内容 |
| 原始 JSON | 浏览器接收该账号的成功结果后 | AT、RT、ID Token、有效期和账号元数据 |
| CPA JSON | 浏览器接收该账号的成功结果后 | 可供 CPA 使用的 `codex` 认证文件，包含完整 Token、邮箱及可用的账号 / 套餐 / 时间字段 |
| Sub2API JSON | 浏览器至少接收一个成功账号后，运行中也可下载 | 将成功账号合并为一个 `sub2api-data` 文件 |
| CPA ZIP | 同上 | 每个成功账号一个 CPA JSON，按输入顺序打包，不包含失败账号及原始结果 manifest |
| 原始结果 ZIP | 同上 | 每个成功账号的 JSON，以及包含所有账号行号、邮箱、状态的 `manifest.json` |
| RT 文本 | 同上 | 仅成功账号的 RT，每行一个 |

列表及批量导出始终按输入顺序排列，不受账号完成先后影响。下载仅包含当前浏览器已经接收的成功账号；运行中下载后，后续成功账号不会自动添加到已下载的文件，需要再次下载。失败、已跳过和未完成账号不会混入成功 Token 导出。

网页 Token 获取流程不会创建输出目录、Token 文件、批次状态文件或服务端 ZIP，也不会恢复旧版本留在输出目录中的任务。每个成功结果先交给当前浏览器缓存，缓存成功后再向服务确认清除临时结果；下载由浏览器按需生成。服务端未交接的已结束任务最多保留 10 分钟，活动任务无会话访问 10 分钟后取消并清理。

浏览器结果缓存有效期为 30 分钟；同一会话内可刷新或在新标签页读取，不能把它当作永久备份。页面运行时或下次读取时清除过期缓存；关闭所有页面后浏览器存储不会凭空定时擦除，敏感结果应主动清理。清除站点数据、会话变化或存储策略可能使结果不可恢复，降级为页面内存时刷新会丢失。页面提供清除当前浏览器结果的操作，不影响其他使用者。手动导入转换页的文件仍只在页面内存中处理。

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

<a id="cpa--sub2api-格式转换"></a>
## CPA ↔ Sub2API 格式转换

点击网页顶部「格式转换」，或打开 [http://127.0.0.1:8765/converter](http://127.0.0.1:8765/converter)。选择转换方向后，导入已有 JSON；此流程不需要重新登录账号。

1. 选择 `CPA → Sub2API` 或 `Sub2API → CPA`。
2. 拖入多个 JSON 文件、通过文件选择器选择文件或目录，或粘贴 JSON。粘贴支持单个对象、连续 JSON、JSONL；CPA 方向也可粘贴账号数组。
3. 查看每条结果及失败、跳过原因，按需预览或下载单个结果。无效账号不会阻止其他有效账号转换。
4. CPA 转 Sub2API 可下载合并 JSON，也可导出拆分文件；超过 3 个拆分结果时打成 ZIP。Sub2API 转 CPA 将每个可转换账号拆为一个 CPA 文件，批量下载为 ZIP。

每次最多处理 1000 个文件或账号，单个文件不超过 2 MiB，导入总量不超过 20 MiB。切换方向或清空后，需要重新导入转换。转换结果仅保留在当前页面内存中，刷新或关闭页面后需要重新导入；不会写入浏览器本地存储，也不会上传认证内容到服务端。

### 转换功能与字段映射

本功能移植并适配自 [CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API)，参考版本为 [`42ff785`](https://github.com/gtxx3600/CPA2sub2API/commit/42ff785d5f3527e4050c02a0da51d4f7eb4f1180)。参考项目的核心是纯前端文件解析、认证字段映射与下载打包；其转换逻辑不调用平台认证接口，也不刷新 Token。

| CPA 类型 | Sub2API 平台 | 主要转换内容 |
| --- | --- | --- |
| `codex`（或缺省类型） | `openai` | AT、RT、ID Token、邮箱、账号 / 用户 / 组织标识、套餐与有效期；可从 JWT claims 补充元数据 |
| `claude` | `anthropic` | AT、RT、邮箱、有效期及可选 ID Token |
| `antigravity` | `antigravity` | AT、RT、邮箱、项目 ID、套餐、Token 类型与有效期 |
| `gemini` | `gemini` | 嵌套 `token` 对象中的认证字段、scope、有效期，以及项目 ID、auto / checked 标记 |

转换仅导出已知可映射字段，缺失的可选字段会省略。代理、Sub2API 调度配置和未知扩展字段不会完整往返保留。CPA 转 Sub2API 的账号默认包含 `concurrency: 10`、`priority: 1`；合并文档采用 `sub2api-data` v1 格式。反向转换只处理可映射到 CPA 的 OAuth 账号，其他账号在页面列出跳过原因。JWT claims 仅用于整理元数据，不验证签名或认证状态；转换成功不代表凭据仍然有效。

重复导入的相同 JSON 内容会显示为已跳过，不受文件名、排版或对象键顺序影响。同邮箱更换 Token 后仍可继续导入；清空结果或切换方向会重置去重记录。粘贴内容仅在全部转换成功后清空，出现错误或达到上限时保留输入供修正。

本项目还兼容自身导出的原始 Token JSON，将 `chatgpt_account_id`、`expires_at`、`obtained_at` 分别映射到 CPA 的 `account_id`、`expired`、`last_refresh`。提取页提供的 CPA 下载直接使用已保存结果，并保留实际取得时间和 AT 到期时间。浏览器转换器补充了秒 / 毫秒时间戳处理、UTF-8 JWT 解码及 ZIP 文件名去重与路径清理。

相关前端模块保留原 MIT 许可及来源，见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## IP 纯净度检测

工作台打开时，浏览器直接请求 IPOK 官方自查接口 `https://ipok.io/api/ip`，无需密钥。侧栏展示摘要，`/network` 展示详细归属地、运营商、网络类型、风险信号、逐源证据和查询额度。所有数据注明 [IPOK](https://ipok.io/) 来源；接口约定见 [官方开发文档](https://ipok.io/developers)。

IP 详情页采用与工作台一致的蓝白配色和统一控件，参考 IPOK 截图布局，以风险圆环和资料面板集中展示类型、风险信号、共享人数估计、来源原生标签、归属地、时区、经纬度、组织、ASN、反向解析、逐源标记、评分依据与地区支持快照。共享人数不是实测人数；原生标签只反映来源的国家一致性资料；地区支持快照不是实时解锁测试。缺失字段显示「未知」，没有当前有效分数时圆环不显示数值。

可填写公网 IPv4 / IPv6 查询指定地址，留空检测当前浏览器出口；试填地址按钮仅填写输入框，不自动请求。指定目标不会覆盖侧栏当前出口或自查缓存。支持复制显示的 IP，以及显隐其十进制数值。指定查询可能受到 IPOK 自身的验证和额度限制，页面会显示对应状态并保留来源入口。

- 检测对象是**当前浏览器出口**。Token 获取表单中的统一代理只影响 Python 登录请求，不改变浏览器出口。
- 风险值为 0–100，越低表示已返回的不利证据越弱；不将其反转为“纯净度百分比”。官方不提供当前有效评分时显示「证据不足」，有限证据和商业来源仅有缓存时也会注明。
- 同一标签页跨工具复用 10 分钟内的检测资料，支持手动重新检测。IPOK 自查有每日额度；429 时按照 `Retry-After` 等待，无有效值时等待 60 秒。请求 20 秒超时，失败时清空旧分数并提供重试入口。
- 仅 IP 检测事实及限流冷却时间保存于会话缓存；不会向 IPOK 发送账号、密码、2FA、Token 或本地页面凭据。认证文件转换与 IP 请求相互独立。

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

## 使用声明与隐私

**网页 Token 获取结果缓存在所属浏览器中，服务器不写入 Token、账号历史或结果文件。** 登录仍需由 Python 服务临时处理凭据与结果；浏览器缓存成功后通知服务清除临时结果，未接收结果也会过期。这不等同于“凭据不经过服务器”。推荐在自己控制的设备或可信服务器运行。此处的临时处理规则专指 Token 工具：GPT 能力评测会保存本机 SQLite 历史，并发分析会保存本机 JSON 历史；详细规则见 [鹈鹕评测说明](PELICAN.md#保存与运行边界)和 [并发分析说明](benchmark.md#保存密钥与隔离)。

首次进入 Token 获取、格式转换、IP 检测、GPT 能力评测或并发分析中的任意工具页面时，都会显示同一份全局声明。点击「我已了解，开始使用」、关闭按钮或按 Esc 后，从该次操作起 **7 × 24 小时内不再提示**；到期后再次进入页面时重新显示。刷新、切换工具或打开同站点的新标签页会复用确认记录；在一个标签页确认，也会关闭其他标签页中已打开的声明。

声明包含数据处理方式、源码公开说明、GitHub 源码链接及 QQ 群 **916444032**，支持复制群号。确认记忆和工具数据分别处理：

升级到浏览器缓存版后，旧用户会先看到一次更新后的隐私声明，确认后继续按 7 天周期提示。

| 数据 | 处理与保存方式 |
| --- | --- |
| 手动导入转换页的认证文件 | 仅在当前页面内存中解析和转换，不上传服务器、不写入浏览器存储；下载文件由使用者自行保管 |
| 网页 Token 获取结果 | 使用按签名会话分区的浏览器 `localStorage` 缓存，30 分钟有效，同一浏览器的标签页可共享；服务器只临时使用内存，接收确认后清除，未接收结果到期清除 |
| 输入的密码和 2FA 密钥 | 不写入浏览器持久化存储，也不写入 Token 结果文件 |
| 声明确认记录 | 独立的 `localStorage` 键保存确认时间戳，该记录不包含账号、密码或 Token |
| IP 检测记录 | 在 `sessionStorage` 中缓存检测资料和限流冷却时间，详见 [IP 纯净度检测](#ip-纯净度检测) |
| 会话及防刷标识 | 两枚服务器签名的匿名 Cookie，用于任务归属校验与频率控制；不含账号或 Token，不采集硬件指纹，详见 [安全策略说明](../SECURITY.md) |

浏览器不允许写入 `localStorage` 时，声明确认记录会退回当前标签页的 `sessionStorage`；两者均不可用时仍能关闭弹窗，下次进入可能再次提示。结果缓存通过 Web Locks 协调多标签接收与清除；共享存储或锁不可用时提示降级限制，不让陌生标签抢先接收并清除服务器结果。结果含完整凭据；同一浏览器配置不适合多人共用，使用后请清除结果。CLI 显式保存文件的行为见[数据保存与运行限制](#数据保存与运行限制)。

## 数据保存与运行限制

- **凭据保存**：程序不会把输入的密码、2FA 密钥或代理凭证写入 Token 结果；网页提交成功后清空输入，也不使用浏览器本地存储保存这些内容。自行创建的 `account.json` 本身包含凭据，需自行保管。
- **网页 Token 结果**：仅写入所属浏览器的会话分区缓存，服务器在登录与交接期间临时使用内存，不保存结果文件。普通状态查询不含 Token 正文，读取结果必须校验当前签名会话的归属。
- **CLI 文件**：CLI 的 `--output`、Python 的 `extract_and_save` / `save_tokens` 仍按显式要求保存明文 JSON，使用原子写入及 POSIX `0600` 权限。这与网页无落盘模式分别处理。
- **刷新与重启**：当前标签页可恢复未过期的浏览器缓存；服务重启不能恢复未交接结果或活动登录。旧版输出目录不会再被网页读取，已有历史文件由部署者检查、清理，升级不会自动删除文件。
- **多人隔离**：签名浏览器会话对应独立任务空间，读结果、验证码、取消与清理均检查归属；其他会话访问已知任务 ID 也返回 404。缺失、失效或伪造 Cookie 会被拒绝。同一浏览器配置共享会话，不能代表不同人员。
- **本地运行**：默认仅允许本机访问，不包含公网用户登录或管理员体系。远程部署仍需可信 HTTPS 入口、访问控制与代理配置；不要删除现有访问校验。迁移步骤见[维护说明](MAINTAINERS.md#privacy-migration)。
- **进程模型**：任务状态保存在单个应用进程的内存中，不能直接增加多个应用副本共享当前任务。
- **功能边界**：处理已有账号，不包含自动注册、手机号验证、自动邮箱取码或 RT 自动续期。2FA 不能替代服务端要求的其他验证。「跳过邮箱验证」会结束该账号的提取并标记为已跳过，不会绕过验证获取 Token。
- **兼容性**：登录使用私有接口，可能随服务端更新失效；并发数是本地上限，不代表服务端配额或实际加速倍数。Node.js 仅用于可选的认证 VM 回退，并非基础依赖。

登录协议整理自原项目的 `botcore/protocol_register`，保留 PKCE、HTTP、TOTP 和认证支持代码，独立于原管理后台、数据库、注册、支付及接码模块。客户端开启 TLS 验证，核对 OAuth 回调地址和 state，并在请求前截获回调，不依赖本机端口 1455 正在监听。认证背景可参考 [OpenAI 认证文档](https://learn.chatgpt.com/docs/auth)。
