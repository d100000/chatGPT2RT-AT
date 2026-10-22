# bob ai工具：ChatGPT / GPT Token 提取（RT、AT、ID Token）

**Bob AI，只做最靠谱的 AI。**

[简体中文](README.md) · [English](README.en.md) · [项目网站](https://tools.bob-api.com/) · [GitHub 源码](https://github.com/d100000/chatGPT2RT-AT)

**bob ai工具用于登录已有且获授权的 ChatGPT 账号，提取 Refresh Token（RT）、Access Token（AT）和 ID Token，并导出原始 JSON、CPA 与 Sub2API 文件。** 项目同时提供浏览器本地格式转换和 IP 纯净度检测，支持网页、单账号命令行及 Python API。

这是独立开发的非官方工具，与 OpenAI 无隶属或背书关系。这里的“GPT 提取 Token”“提取 RT / AT”指账号登录后的认证结果，**不是创建 OpenAI API Key**；格式转换不会刷新 Token，也不保证凭据仍有效。提取结果会保存到运行工具的设备，服务器部署时该设备就是服务器。源码公开，主项目许可证状态见[来源与许可](#来源与许可)。

| 你要完成的事 | 对应功能 | 说明文档 |
| --- | --- | --- |
| 获取 ChatGPT RT、AT 和 ID Token | Token 获取：网页批量、CLI / Python 单账号 | [Token 提取指南](docs/TOKEN-GUIDE.md) |
| 转换已有 CPA / Sub2API 认证 JSON | CPA ↔ Sub2API 浏览器本地转换 | [转换与字段映射](#cpa--sub2api-格式转换) |
| 了解当前浏览器出口的风险证据 | IPOK IP 纯净度检测 | [IP 检测说明](#ip-纯净度检测) |

```text
邮箱 + 密码 + 2FA → 登录验证 → 获取 AT / RT / ID Token → 原始 JSON / Sub2API / CPA
已有认证 JSON → 浏览器本地转换 → CPA 单文件 / CPA ZIP / Sub2API JSON
```

## 文档导航

[快速开始](#快速开始) · [RT / AT 术语](#token-types) · [输入格式](#账号输入格式) · [结果与下载](#查看结果与下载) · [CLI / Python](#命令行与-python-接口) · [隐私与保存](#使用声明与隐私) · [防刷策略](SECURITY.md) · [常见问题](docs/FAQ.md) · [维护与更新](docs/MAINTAINERS.md) · [SEO / GEO 方案](docs/SEO-GEO-PLAN.md)

<a id="token-types"></a>
## RT、AT 和 ID Token 有什么区别？

| 名称 | 完整名称 / 导出字段 | 本项目中的含义 |
| --- | --- | --- |
| RT | Refresh Token / `refresh_token` | 认证服务签发的刷新凭据；本项目提取并导出它，不提供 RT 自动续期功能 |
| AT | Access Token / `access_token` | 用于访问授权资源的访问令牌；导出中的 `expires_at` 描述 AT 到期时间 |
| ID Token | ID Token / `id_token` | 包含登录身份相关 claims 的令牌；页面中的元数据解析不等于验证签名或认证有效性 |
| API Key | API 密钥 | 与上述账号 Token 不同；本项目不会为账号创建 API Key |

术语背景见 [OAuth 2.0 的 AT / RT 定义](https://www.rfc-editor.org/rfc/rfc6749#section-1.4)与 [OpenID Connect 的 ID Token 定义](https://openid.net/specs/openid-connect-core-1_0.html#IDToken)。本项目的支持范围与实际代码为准，平台可能撤销凭据或改变登录要求。

## 功能概览

| 功能 | 说明 |
| --- | --- |
| 完整 Token 提取 | 同时保存 Access Token（AT）、Refresh Token（RT）和 ID Token；缺少任意一种时不标记为成功 |
| 批量导入 | 网页支持粘贴账号列表或导入 UTF-8 `.txt` 文件，每批最多 1000 个账号、2 MiB 文本 |
| 并发处理 | 默认同时处理 2 个账号，可选 1～3 个；空出名额后自动处理下一个账号 |
| 验证码交互 | 使用 2FA 密钥生成动态码；遇到额外邮箱验证时，在对应账号下手动输入验证码 |
| 查看与复制 RT | 成功账号旁提供「查看 RT」按钮，可显示完整内容、复制或关闭隐藏 |
| 多种导出格式 | 支持 Sub2API JSON、CPA JSON / ZIP、原始 Token JSON、成功结果 ZIP，以及每行一个 RT 的文本 |
| 双向格式转换 | 独立页面支持 CPA 与 Sub2API 互转，兼容 Codex/OpenAI、Claude、Antigravity、Gemini；认证内容在浏览器内处理 |
| 自动生成文件 | 每个账号成功后自动保存原始、CPA、Sub2API 三份 JSON；批次结束后自动打包成功账号的 CPA ZIP 和合并 Sub2API JSON |
| IP 纯净度检测 | 浏览器自动调用 IPOK 官方接口，显示当前出口、官方风险值、纯净度标签和证据状态；共享侧栏摘要与独立详情页 |
| 全局使用声明 | 三个页面共用声明与 7 天确认记忆，包含隐私说明、开源地址和 QQ 群号复制；支持键盘关闭与手机滚动查看 |
| 进度与取消 | 分别显示账号状态和运行记录；单个账号失败继续处理，取消整批后保留成功结果 |
| 限流等待 | 收到 HTTP 429 后暂停启动后续账号，显示等待时间；已运行账号继续处理 |
| 防刷与防爆破 | 网页服务按来源、账号和操作限流，限制验证码尝试、批量账号处理量和导出并发；前端遵循冷却时间 |
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

桌面左侧、手机顶部提供三个功能入口：**Token 获取**（`/`）、**格式转换**（`/converter`）、**IP 检测**（`/network`）。切换工具不停止正在执行的提取任务；回到 Token 页可继续跟踪进度。

Windows 创建虚拟环境时可使用 `python -m venv .venv`，后续将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

### 网页使用流程

1. 粘贴账号列表，或点击「导入 .txt 文件」。
2. 选择「同时处理账号数」；默认 2，选择 1 为逐条处理。
3. 根据需要在「连接设置」中填写统一代理地址，然后开始提取。
4. 查看各账号的处理状态；如需邮箱验证码，在对应邮箱下输入。
5. 账号成功后可立即查看 RT、复制 RT，或下载该账号的原始 JSON、CPA JSON。
6. 批次结束后，CPA ZIP 与合并 Sub2API JSON 已自动保存，可直接下载，也可下载原始结果 ZIP 或 RT 文本。
7. 点击「在转换工具中查看」，无需手动下载、重新上传；也可在转换页选择方向后点击「载入最近提取结果」。载入会替换当前转换结果，并保留原提取结果的套餐、刷新时间和有效期。

## 使用声明与隐私

**转换文件只在浏览器内存中处理；Token 获取结果会写入运行设备的输出目录。** 在第三方服务器上使用提取功能意味着凭据经该服务器处理，不能等同于“数据只在你的浏览器中”。推荐在自己控制的设备或服务器运行。

首次进入 Token 获取、格式转换或 IP 检测中的任意页面时，都会显示同一份全局声明。点击「我已了解，开始使用」、关闭按钮或按 Esc 后，从该次操作起 **7 × 24 小时内不再提示**；到期后再次进入页面时重新显示。刷新、切换工具或打开同站点的新标签页会复用确认记录；在一个标签页确认，也会关闭其他标签页中已打开的声明。

声明包含数据处理方式、**代码完全开源**说明、GitHub 源码链接及 QQ 群 **916444032**，支持复制群号。确认记忆和工具数据分别处理：

| 数据 | 处理与保存方式 |
| --- | --- |
| 导入的转换文件与 Token | 仅在当前页面内存中解析和转换，不上传服务器、不写入浏览器存储；下载文件由使用者自行保管 |
| Token 获取结果 | 原始 Token、CPA、Sub2API 文件保存在运行本工具的设备的输出目录中，不会上传给作者；7 天提醒周期不会删除这些文件 |
| 输入的密码和 2FA 密钥 | 不写入浏览器持久化存储，也不写入 Token 结果文件 |
| 声明确认记录 | `localStorage` 仅保存确认时间戳，不包含账号、密码、Token 或用户标识 |
| IP 检测记录 | 在 `sessionStorage` 中缓存检测资料和限流冷却时间，详见 [IP 纯净度检测](#ip-纯净度检测) |
| 防刷标识 | 两枚服务器签名的匿名 Cookie，分别识别浏览器和短会话；不含账号或 Token，不采集硬件指纹，详见 [安全策略说明](SECURITY.md) |

浏览器不允许写入 `localStorage` 时，声明确认记录会退回当前标签页的 `sessionStorage`；两者均不可用时仍能关闭弹窗、正常使用工具，下次进入可能再次提示。清除站点存储或更换浏览器后也会重新提示。Token 文件按[数据保存与运行限制](#数据保存与运行限制)中的方式保留。

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

相关前端模块保留原 MIT 许可及来源，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## IP 纯净度检测

工作台打开时，浏览器直接请求 IPOK 官方自查接口 `https://ipok.io/api/ip`，无需密钥。侧栏展示摘要，`/network` 展示详细归属地、运营商、网络类型、风险信号、逐源证据和查询额度。所有数据注明 [IPOK](https://ipok.io/) 来源；接口约定见 [官方开发文档](https://ipok.io/developers)。

IP 详情页采用与工作台一致的蓝白配色和统一控件，参考 IPOK 截图布局，以风险圆环和资料面板集中展示类型、风险信号、共享人数估计、来源原生标签、归属地、时区、经纬度、组织、ASN、反向解析、逐源标记、评分依据与地区支持快照。共享人数不是实测人数；原生标签只反映来源的国家一致性资料；地区支持快照不是实时解锁测试。缺失字段显示「未知」，没有当前有效分数时圆环不显示数值。

可填写公网 IPv4 / IPv6 查询指定地址，留空检测当前浏览器出口；试填地址按钮仅填写输入框，不自动请求。指定目标不会覆盖侧栏当前出口或自查缓存。支持复制显示的 IP，以及显隐其十进制数值。指定查询可能受到 IPOK 自身的验证和额度限制，页面会显示对应状态并保留来源入口。

- 检测对象是**当前浏览器出口**。Token 获取表单中的统一代理只影响 Python 登录请求，不改变浏览器出口。
- 风险值为 0–100，越低表示已返回的不利证据越弱；不将其反转为“纯净度百分比”。官方不提供当前有效评分时显示「证据不足」，有限证据和商业来源仅有缓存时也会注明。
- 同一标签页跨工具复用 10 分钟内的检测资料，支持手动重新检测。IPOK 自查有每日额度；429 时按照 `Retry-After` 等待，无有效值时等待 60 秒。请求 20 秒超时，失败时清空旧分数并提供重试入口。
- 仅 IP 检测事实及限流冷却时间保存于会话缓存；不会向 IPOK 发送账号、密码、2FA、Token 或本地页面凭据。认证文件转换与 IP 请求相互独立。

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

### 本站请求防护

网页服务同时按 **IP、设备、Session、全局** 检查额度，直接调用 API 的脚本同样受限。“设备”指服务器签名 Cookie 识别的浏览器配置，不是物理硬件。IP 使用实际连接地址；当前仅允许本机访问，因此本机浏览器与脚本共享 IP 额度。

| 操作 | 每 IP | 每设备 | 每 Session | 服务进程总额度 / 额外限制 |
| --- | --- | --- | --- | --- |
| 页面、状态查询及其他动态请求 | 600 次 / 分钟、120 次 / 秒 | 450 次 / 分钟、90 次 / 秒 | 300 次 / 分钟、60 次 / 秒 | 2400 次 / 分钟、240 次 / 秒 |
| 静态资源（含不存在的资源） | 1200 次 / 分钟、240 次 / 秒 | 同 IP | 同 IP | 4800 次 / 分钟、480 次 / 秒；独立于动态请求 |
| 提交单账号或批次 | 10 次 / 分钟 | 8 次 / 分钟 | 6 次 / 分钟 | 两个入口共享；合计 30 次 / 分钟 |
| 账号处理量 | 2000 个 / 小时 | 1500 个 / 小时 | 1000 个 / 小时 | 5000 个 / 小时；批次按账号数量计费 |
| 提交验证码 | 15 次 / 分钟 | 12 次 / 分钟 | 10 次 / 分钟 | 60 次 / 分钟；同一任务最多 5 次 / 分钟 |
| 查看 RT、读取转换源及所有结果下载 | 30 次 / 分钟 | 24 次 / 分钟 | 20 次 / 分钟 | 合计 120 次 / 分钟，最多同时读取或导出 2 个响应 |
| 取消任务 / 批次 | 独立 60 次 / 分钟 | 独立 60 次 / 分钟 | 独立 60 次 / 分钟 | 独立 240 次 / 分钟 |
| Host、Origin 或 CSRF 校验失败 | 10 次 / 5 分钟 | 10 次 / 5 分钟 | 10 次 / 5 分钟 | 超出返回 429，正确的访问校验仍可通过 |
| 签发设备 / Session 标识 | 20 次 / 10 分钟 | — | — | 60 次 / 10 分钟，仅合法页面缺少有效标识时签发 |

同一邮箱另外受跨 IP、设备和 Session 合计 **5 次 / 15 分钟** 的提取限制，邮箱大小写不影响计数。合法标识在换 IP 后保持原额度，开新标签页复用浏览器会话；没有 Cookie 或 Cookie 无效的 API 请求使用该 IP 的固定匿名额度。清 Cookie、更换 Session 或伪造标识不会重置 IP、账号或全局计数。

多条规则同时适用时，任意一条超限都会返回 HTTP `429`，并通过 `Retry-After` 响应头和 JSON `retry_after` 提供等待秒数。反复请求不会延长原有窗口。页面显示倒计时、保留尚未提交成功的输入，并暂停相关请求；不会自动重新提交密码或验证码。状态查询被限流时，仍可使用独立额度取消任务。

账号额度在整批成功进入队列时扣除，包含后来失败、未运行或取消的账号；重复取消再提交无法退还额度。任务冲突或启动异常不扣账号额度，批次中任一账号超限时整批不启动。请求正文、字段长度和限流计数器数量也有上限。

这些限制在单个进程内生效，重启后重置，不依赖数据库。它们不替代公网身份验证或入口防护。策略定义、调整示例、请求大小及服务器部署边界见 [安全策略说明](SECURITY.md)。

## 查看结果与下载

| 操作 | 可用时机 | 内容 |
| --- | --- | --- |
| 查看 RT / 复制 RT | 单个账号成功后 | 该账号完整的 Refresh Token，关闭后隐藏并清空显示内容 |
| 原始 JSON | 单个账号成功后 | AT、RT、ID Token、有效期和账号元数据 |
| CPA JSON | 单个账号成功后 | 可供 CPA 使用的 `codex` 认证文件，包含完整 Token、邮箱及可用的账号 / 套餐 / 时间字段 |
| Sub2API JSON | 批次完成或取消后，且至少有一个成功账号 | 将成功账号合并为一个 `sub2api-data` 文件 |
| CPA ZIP | 同上 | 每个成功账号一个 CPA JSON，按输入顺序打包，不包含失败账号及原始结果 manifest |
| 原始结果 ZIP | 同上 | 每个成功账号的 JSON，以及包含所有账号行号、邮箱、状态的 `manifest.json` |
| RT 文本 | 同上 | 仅成功账号的 RT，每行一个 |

列表及批量导出始终按输入顺序排列，不受账号完成先后影响。失败和未完成账号不会混入成功 Token 导出。

使用快速开始中的启动命令时，每个成功账号的原始 JSON 会自动保存到：

```text
output/
└── batch-<批次ID>/
    ├── tokens-<任务ID>.json
    ├── codex-<任务ID>.json
    ├── sub2api-<任务ID>.json
    ├── cpa-<批次ID>.zip
    ├── sub2api-<批次ID>.json
    └── batch-state.json
```

前三类文件在每个账号成功时自动生成；两类批量文件在批次结束或取消后，仅对成功账号自动生成。保存使用原子写入及 POSIX `0600` 权限。自动转换失败会明确显示，原始结果保留。`batch-state.json` 在批次结束后保存状态，不包含 Token 正文。输出目录可通过 `--output-dir` 修改；不指定时默认为 `rt-output/`。

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

前端模块测试使用 Node.js（推荐 22+）：

```bash
node --test tests/*.mjs
```

测试使用合成账号和模拟网络，覆盖登录协议、保存、网页交互接口、并发调度、验证码、取消、限流、CPA / Sub2API 导出，以及前端双向字段映射、粘贴解析、时间戳与 ZIP 打包。声明弹窗测试覆盖 7 天到期边界、跨页记忆、跨标签确认、损坏或未来时间戳、存储异常回退、关闭与焦点恢复及 QQ 群号复制。测试不会登录真实账号。Node.js 仅为运行前端模块测试所需，使用浏览器转换页面不需要安装 Node.js。

```text
chatGPT2RT-AT/
├── README.md
├── README.en.md
├── docs/                 # 提取指南、FAQ、维护说明与 SEO / GEO 方案
├── pyproject.toml
├── requirements.txt
├── rt_extractor/          # 独立模块、网页和登录协议
└── tests/                 # 合成账号和模拟网络测试
```

## 常见问题

### 如何提取 ChatGPT RT 和 AT？

在自己控制的设备上按[快速开始](#快速开始)启动工具，通过网页导入已有授权账号并完成要求的验证。只有同时获得 AT、RT 和 ID Token 才会标记成功；随后可查看 RT 或下载认证 JSON。单账号也可使用 CLI / Python。分步说明见 [Token 提取指南](docs/TOKEN-GUIDE.md)。

### 已有 Token 可以直接转换 CPA 或 Sub2API 吗？

可以。在格式转换页导入已有认证 JSON，不需要重新登录。转换只整理已有字段，不会兑换、续期或检查 Token 是否仍有效，详见[转换说明](#cpa--sub2api-格式转换)。

### 为什么输入账号后仍显示 0 条，开始按钮不可用？

先检查浏览器 Console。若看到 `Cannot use import statement outside a module`，可能是旧 HTML 模板仍在加载含 `import` 的新 JavaScript。更新后需让实际运行的 Python / Gunicorn 服务或容器加载同一版本，再刷新页面；只执行 `git pull` 或只重启 Nginx 不一定生效。详见[维护与更新](docs/MAINTAINERS.md#module-loading)。

### 会自动注册、跳过 2FA 或一直刷新 RT 吗？

不会。工具处理已有账号，2FA 密钥不能替代认证服务要求的额外邮箱或其他验证；项目不包含自动注册、手机号验证、自动邮箱取码或 RT 自动续期。

更多问题包括“Token 保存在哪”“AT 过期是否代表 RT 失效”“IP 检测是否等于登录代理”，见 [FAQ](docs/FAQ.md)。

## 来源与许可

主项目目前没有声明完整的项目许可证，源码公开不等于已授予任意复制、修改或再分发权限；使用或分发前应确认作者授权。不要将局部第三方 MIT 许可视为整个项目的许可。

CPA 转换模块适配自 [CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API)，原始 MIT 许可和修改说明保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。登录协议整理来源见[数据保存与运行限制](#数据保存与运行限制)。问题报告请附版本、脱敏错误和复现步骤，不要提交账号、密码、2FA 密钥或 Token。

公开说明页由本项目提供：`/docs`、`/docs/token-extraction`、`/docs/token-types`、`/docs/cpa-sub2api`、`/docs/faq`。它们需要部署当前版本后才可访问；本 README 不表示已核验线上部署状态。网站与仓库的关键词、内容及抓取配置方案见 [SEO / GEO 优化方案](docs/SEO-GEO-PLAN.md)。

## 联系与交流

- 作者邮箱：[bbo_dong@qq.com](mailto:bbo_dong@qq.com)
- QQ 交流群：**916444032**
- 开源仓库：[d100000/chatGPT2RT-AT](https://github.com/d100000/chatGPT2RT-AT)

欢迎交流使用经验、反馈问题和参与改进。相关第三方模块的来源与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
