<div align="center">

# bob ai工具

**ChatGPT Token 提取 · CPA / Sub2API 转换 · 模型能力评测与并发分析**

把账号认证、文件转换和接口测试放在同一个本地工作台。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Flask](https://img.shields.io/badge/Flask-3.1%2B-000000?logo=flask&logoColor=white)](pyproject.toml)
[![JavaScript](https://img.shields.io/badge/JavaScript-ES_Modules-F7DF1E?logo=javascript&logoColor=black)](rt_extractor/static/)

简体中文 · [English](README.en.md)

[项目网站](https://tools.bob-api.com/) · [快速开始](#快速开始) · [功能概览](#功能概览) · [文档导航](#文档导航) · [问题反馈](https://github.com/d100000/chatGPT2RT-AT/issues)

</div>

## 项目介绍

**bob ai工具**用于登录已有且获授权的 ChatGPT 账号，取得 Refresh Token（RT）、Access Token（AT）和 ID Token，并导出原始 JSON、CPA 与 Sub2API 认证文件。已有文件可直接在浏览器中转换，无需重新登录。

工作台还提供 IP 检测、鹈鹕 SVG 能力评测和多模型并发分析，便于在同一界面查看网络证据、比较生成结果和测量接口性能。后端使用 Python / Flask，前端使用原生 JavaScript，无需前端构建；Token 提取另提供单账号 CLI 与异步 Python API。

这是独立开发的非官方项目，与 OpenAI 无隶属或背书关系。账号 Token 与 API Key 不同；登录兼容性取决于上游认证服务。项目不提供自动注册、绕过认证或 RT 自动续期功能。

## 功能概览

| 工具 | 可以做什么 | 入口 / 文档 |
| --- | --- | --- |
| **Token 获取** | 1～10 并发、超量自动分组、验证码交互、跳过邮箱验证账号、查看与复制 RT；导出原始 JSON、CPA、Sub2API、ZIP 和 RT 文本 | `/` · [提取指南](docs/TOKEN-GUIDE.md) |
| **格式转换** | CPA ↔ Sub2API 双向转换；支持多文件、目录和粘贴 JSON，在浏览器内解析、预览和打包 | `/converter` · [字段映射](docs/REFERENCE.md#转换功能与字段映射) |
| **IP 检测** | 查询浏览器出口或指定公网 IP，查看 IPOK 返回的归属地、风险信号与来源证据 | `/network` · [检测说明](docs/REFERENCE.md#ip-纯净度检测) |
| **GPT 能力评测** | 鹈鹕骑自行车 SVG 单次、批量、定时与持续测试；人工评分、可选 AI 看图评分、历史对比与报告导出 | `/pelican` · [评测指南](docs/PELICAN.md) |
| **并发分析** | 比较多个模型与并发档位的首字延迟、吞吐、成功率和缓存用量；支持后台任务与 HTML / CSV / JSON 报告 | `/benchmark` · [指标与操作](docs/benchmark.md) |

Token 提取只在三类 Token 全部取得后标记成功；单个账号失败不会中断整批。切换工具页面不会停止已在服务端运行的任务。

## 快速开始

### 环境要求

- **Python 3.10+**，推荐 Python 3.12。
- 浏览器需支持 JavaScript ES modules。
- Node.js 用于前端测试及可选的认证 VM 回退；日常文件转换无需安装 Node.js。

### macOS / Linux

```bash
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT

python3 -m venv .venv
.venv/bin/python -m pip install '.[web]'
.venv/bin/python -m rt_extractor.web --port 8765
```

打开 **[http://127.0.0.1:8765](http://127.0.0.1:8765)**，从导航中选择工具。端口已被占用时，可将 `--port 8765` 改为 `--port 8766`。

<details>
<summary>Windows PowerShell</summary>

```powershell
git clone https://github.com/d100000/chatGPT2RT-AT.git
cd chatGPT2RT-AT

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install '.[web]'
.\.venv\Scripts\python.exe -m rt_extractor.web --port 8765
```

无需激活虚拟环境，直接使用其中的解释器即可。

</details>

服务绑定并仅接受本机访问，采用单进程、多线程运行。远程自用可使用 [SSH 隧道](SECURITY.md#服务器使用边界)；部署与更新说明见 [维护文档](docs/MAINTAINERS.md)。

## 使用指南

### 批量获取 Token

<a id="账号输入格式"></a>

1. 粘贴账号列表，或导入 UTF-8 `.txt` 文件，每行一个账号。
2. 选择同时处理的账号数：默认 **2**，可选 **1～10**。网页总输入不超过 **2 MiB**；超过 **1,000 条**自动分组，每组最多 1,000 条。
3. 按需填写统一代理，点击开始，并在确认窗口核对连接方式。
4. 查看各账号状态；遇到邮箱验证码时，输入验证码或跳过该账号。
5. 成功结果接收到浏览器后，即可查看 RT 或下载文件，无需等待整批结束。

输入格式使用四个英文短横线 `----` 分隔，下面均为占位值：

```text
user1@example.com----YOUR_PASSWORD----YOUR_BASE32_SECRET
user2@example.com----YOUR_PASSWORD----YOUR_BASE32_SECRET
```

三个字段均不能为空。2FA 支持 Base32 密钥或 `otpauth://totp/` URI，使用 SHA1、6 位动态码和 30 秒周期。分组前检查基础格式和跨组重复邮箱；服务端逐组完整校验，当前组有无效密钥等错误时不会启动该组。更多规则见 [账号输入格式](docs/REFERENCE.md#账号输入格式)。

自动分组会等待当前组结束、结果完整接收并确认后，再提交下一组。已完成组提供独立下载，不会被下一组覆盖。网页仍受每会话每小时 1,000 个账号等额度约束，提交被限流时显示倒计时并等待；停止计划会取消当前组并清除尚未提交的账号。后续账号和分组归档只保留在当前页面内存，刷新或关闭页面不能续跑；长时间运行可使用下方的本地分组 CLI。

<a id="并发验证码和限流"></a>
### 等待邮箱验证码时跳过账号

| 操作 | 行为 |
| --- | --- |
| **跳过邮箱验证**开关 | 默认关闭；开启后，当前等待及后续需要邮箱验证码的账号都会结束提取，释放名额继续队列；运行中可切换 |
| **跳过此账号**按钮 | 仅跳过对应的邮箱验证码请求，其他账号继续处理 |
| 关闭开关 | 后续邮箱验证恢复等待输入；已跳过的账号不会自动重试，可在批次结束后重新导入 |

跳过的账号单独计入 **“已跳过”**，不会生成 Token。该开关不影响 Authenticator 动态码验证。默认每个账号总超时为 180 秒，包含人工验证码等待；取消整批会保留已成功结果。

<a id="查看结果与下载"></a>
### 查看结果与下载

单账号可查看或复制 RT，下载原始 JSON、CPA JSON 或 Sub2API JSON。批量导出支持合并 Sub2API JSON、CPA ZIP、原始结果 ZIP 和每行一个 RT 的文本，按输入顺序整理已接收到浏览器的成功结果。原始 ZIP 的 `manifest.json` 另记录账号状态，包含失败、取消和跳过项。

字段定义及完整样例见 [导出格式参考](docs/REFERENCE.md#查看结果与下载)。

### CPA ↔ Sub2API 格式转换

选择转换方向，导入 JSON 文件、目录或粘贴内容，即可预览、拆分、合并与下载。支持 Codex/OpenAI、Claude、Antigravity、Gemini 的已知认证字段；每次最多 1,000 个文件或账号，单文件 2 MiB，总量 20 MiB。

转换在浏览器内完成，不上传手动导入的认证内容。它只映射已有字段，不刷新 Token，也不验证当前认证状态；未映射字段不能保证往返保留。[支持类型与映射规则 →](docs/REFERENCE.md#转换功能与字段映射)

### IP 纯净度检测

查看当前浏览器出口，或输入公网 IPv4 / IPv6 查询来源证据。数据来自 [IPOK](https://ipok.io/)，缺失信息显示为未知；风险分越低代表已返回的不利证据越弱，不表示登录成功率。

Token 页的统一代理只影响 Python 登录请求，**浏览器检测出口可能与登录出口不同**。[数据来源、缓存与额度 →](docs/REFERENCE.md#ip-纯净度检测)

### 模型能力评测与并发分析

- **能力评测**：围绕“鹈鹕骑自行车”SVG 题目保留原始输出、渲染证据、人工或 AI 评分与历史报告，用于观察指定视觉任务的表现。它不能代表总体智力或证明上游模型身份。[完整说明 →](docs/PELICAN.md)
- **并发分析**：通过 Responses / Chat Completions 实际请求，比较模型、输入模式和并发档位。首字、吞吐和缓存统计保留各自口径，报告文字由统计结果生成，不额外调用模型。[指标说明 →](docs/benchmark.md)

两类测试都需要可用的模型接口；启动前可查看请求计划，持续评测需手动停止。历史数据与密钥的处理方式见下表。

<a id="数据保存与运行限制"></a>
## 使用声明与隐私

各工具的数据保存方式不同。**网页 Token 结果不写入服务器文件；能力评测与并发分析会保存本机历史。**

| 数据 | 保存与清理方式 |
| --- | --- |
| 网页登录凭据 | 由服务端在内存中临时处理；密码和 2FA 密钥不写入浏览器持久化存储或 Token 结果文件 |
| 网页 Token 结果 | 按签名会话分区缓存到浏览器 `localStorage`，有效期 30 分钟，同会话标签页可共享；下载由浏览器生成 |
| 网页已完成分组归档 | 当前页面内存中保留，最多 20 MiB；计划结束或停止后 30 分钟清除，刷新或关闭页面即丢失，需按组下载 |
| 服务端 Token 临时结果 | 浏览器确认接收后清除；未交接的已结束结果最多保留 10 分钟，活动工作空间闲置 10 分钟后取消清理 |
| 手动导入的转换文件 | 只在当前页面内存中处理，不上传、不写入浏览器存储；刷新后需重新导入 |
| IP 检测与声明记忆 | IP 资料和冷却时间使用会话缓存；公告确认时间独立保存，7 天内免提示 |
| 鹈鹕能力评测 | 原始输出、评分与图片保存在本机 SQLite；目录可通过 `GPT_EVAL_DATA_DIR` 配置 |
| 并发分析 | 摘要与请求记录保存在本机 JSON 历史目录；目录可通过 `GPT_BENCHMARK_DATA_DIR` 配置 |
| 模型接口 API key | 仅在浏览器输入框和服务端内存中处理，不写入历史、浏览器持久化存储或导出文件 |
| CLI / Python 显式保存 | 按指定路径写入明文 JSON，采用原子写入及 POSIX `0600` 权限 |

浏览器缓存包含完整凭据，过期清理发生在页面运行或下次读取时；关闭所有标签页不会立即擦除存储。请下载需要的结果并主动清理，避免多人共用同一浏览器配置。存储不可用时页面会说明降级限制；服务重启不能恢复活动登录或尚未交接的 Token 结果。

<a id="本站请求防护"></a>

任务操作均校验签名浏览器会话归属，并检查 Host、Origin、CSRF 和请求频率。HTTP 429 按 `Retry-After` 等待；自动分组只重试被限流拒绝、尚未启动的分组，账号认证失败或提交状态不确定时不自动重试，也不自动重提人工验证码。服务不包含公网用户登录体系，也不能通过增加独立进程共享当前任务。详见 [安全策略](SECURITY.md)、[数据与运行边界](docs/REFERENCE.md#数据保存与运行限制)及 [迁移说明](docs/MAINTAINERS.md#privacy-migration)。

<a id="token-types"></a>
## Token 术语

| 名称 | 本项目中的含义 |
| --- | --- |
| **RT** · Refresh Token | 认证服务签发的刷新凭据；本项目提取并导出，不负责自动续期 |
| **AT** · Access Token | 访问令牌；导出中的 `expires_at` 描述 AT 到期时间，不代表 RT 寿命 |
| **ID Token** | 包含身份 claims 的令牌；读取元数据不等于验证签名或当前有效性 |
| **API Key** | 与账号 Token 不同；本项目不会为 ChatGPT 账号创建 API Key |

术语定义及来源见 [技术参考](docs/REFERENCE.md#token-types)。

## 命令行与 Python 接口

默认 CLI 和公开 Python API 每次处理单个账号；本地分组 CLI 支持一次读取账号文本并顺序执行多个分组。无需网页时，安装可使用 `.venv/bin/python -m pip install .`。

```bash
.venv/bin/python -m rt_extractor --email you@example.com --output tokens.json
```

命令行会隐藏密码与 2FA 输入；输出默认不覆盖已有文件。Python 提供 `extract_tokens(...)` 返回内存结果，或 `extract_and_save(...)` 显式保存。无交互 JSON 输入、环境变量、异步回调与错误类型见 [CLI / Python 完整说明](docs/REFERENCE.md#命令行与-python-接口)。

长时间批量提取可使用本地分组命令。`accounts.txt` 与网页使用相同的三字段格式，输出目录必须尚不存在：

```bash
.venv/bin/python -m rt_extractor.batch_cli --input accounts.txt --output-dir ./private-results/run-001 --concurrency 10 --skip-email-verification
```

全部输入先校验，每 1,000 条分一组，上一组完成后继续下一组。每条成功结果立即以私有文件权限保存，另生成每组下载文件和不含凭据的 `progress.json` / `report.json`；这些本地文件不受浏览器 30 分钟缓存期限影响。失败账号不会自动重试。详见 [本地分组提取](docs/REFERENCE.md#本地分组提取)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [Token 提取指南](docs/TOKEN-GUIDE.md) | 从安装、导入与验证到下载结果 |
| [技术参考](docs/REFERENCE.md) | 输入规范、并发与限流、转换字段、JSON 示例、CLI / Python API |
| [GPT 能力评测](docs/PELICAN.md) | 题目、评分规则、历史保存与报告 |
| [并发分析](docs/benchmark.md) | 测试配置、指标口径、接口与导出 |
| [常见问题](docs/FAQ.md) | 认证、Token、转换、代理与故障排查 |
| [批次进度停住排查](docs/BATCH-TROUBLESHOOTING.md) | 旧缓存假卡住、任务过期、结果保留与恢复步骤 |
| [安全策略](SECURITY.md) | 会话隔离、请求额度、资源边界与远程自用 |
| [维护与更新](docs/MAINTAINERS.md) | 安装升级、进程重启、旧版迁移与公开文档配置 |
| [产品说明](PRODUCT.md) / [界面规范](DESIGN.md) | 产品范围与设计约定 |
| [SEO / GEO 方案](docs/SEO-GEO-PLAN.md) | 网站与仓库的发现信息及公开文档规划 |

应用也提供 `/docs`、`/docs/token-extraction`、`/docs/token-types`、`/docs/cpa-sub2api`、`/docs/faq` 公开说明页，具体可用内容取决于所部署版本。

## 开发与测试

后端使用 Python / Flask / httpx，前端为 Jinja2 模板与原生 JavaScript / CSS。开发时可用 `.venv/bin/python -m pip install -e '.[web]'` 安装，静态资源无需打包。

```text
chatGPT2RT-AT/
├── rt_extractor/          # 登录协议、任务调度、评测与并发分析
│   ├── templates/        # Jinja2 页面
│   ├── static/           # JavaScript / CSS 与资源
│   └── _protocol/        # HTTP、PKCE、TOTP 与认证协议
├── tests/                # 单元、集成与浏览器测试
├── docs/                 # 使用指南、技术参考与维护说明
├── pyproject.toml        # Python 依赖与入口
└── THIRD_PARTY_NOTICES.md # 第三方来源与许可
```

安装网页依赖后，在仓库根目录运行；前端测试推荐 Node.js 22+：

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
git diff --check
```

测试使用合成账号和模拟网络，不登录真实账号。评测与并发模块的可选浏览器验收命令见各自文档。欢迎提交复现步骤、文档修正和有针对性的测试；Issue 中请注明版本与脱敏错误，不附真实账号凭据。

## 更新已有安装

先结束当前任务，下载需要的 Token 结果并保留评测历史，再在实际部署目录执行：

```bash
git status --short
git pull --ff-only
.venv/bin/python -m pip install '.[web]'
```

有本地修改时先检查和保留。更新后重启实际运行的 Python / Gunicorn 服务或重建并重启对应容器，再刷新页面；只拉取代码或重启 Nginx 不会让 Python 重新加载模板。详细步骤见 [维护文档](docs/MAINTAINERS.md)。

## 常见问题

<details>
<summary>批次还有几百个账号，为什么进度不再变化？</summary>

若页面仍显示“提取中”却没有运行账号详情，可能在显示旧缓存。旧版遇到任务接口 404 后会停止刷新但保留运行状态；本次已修复，任务不存在时明确结束旧进度，保留已接收结果。页面在后台且会话闲置超过 10 分钟、服务重启或多进程部署都可能让任务无法查询，具体需结合请求状态码判断。见 [排查记录与恢复步骤](docs/BATCH-TROUBLESHOOTING.md)。

</details>

<details>
<summary>等待邮箱验证码会卡住整批吗？</summary>

等待验证码会占用处理名额。可输入验证码、点击「跳过此账号」，或开启「跳过邮箱验证」让当前和后续邮箱验证账号自动结束提取。跳过账号没有 Token，其他名额和队列继续处理。

</details>

<details>
<summary>已有 Token 可以直接转换吗？</summary>

可以。在格式转换页导入已有 JSON，无需重新登录。转换不会刷新 Token，也不保证凭据仍有效。

</details>

<details>
<summary>更新后输入仍显示 0 条，按钮不能点击？</summary>

检查浏览器 Console 是否出现 `Cannot use import statement outside a module`。通常需要确认运行进程、已安装包、HTML 模板与静态文件属于同一版本，重启实际应用进程后再刷新。见 [排查步骤](docs/MAINTAINERS.md#module-loading)。

</details>

更多问题见 [FAQ](docs/FAQ.md)。

## 来源与许可

主项目目前**尚未声明统一许可证**。源码公开不等于已授予任意复制、修改或再分发权限，使用或分发前应确认授权。

CPA 转换模块适配自 [CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API)，原 MIT 许可及修改说明保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，该局部许可不代表整个项目的许可。登录协议整理自原项目 `botcore/protocol_register`，来源和实现边界见 [技术参考](docs/REFERENCE.md#数据保存与运行限制)。

## 联系与交流

- 问题与建议：[GitHub Issues](https://github.com/d100000/chatGPT2RT-AT/issues)
- 作者邮箱：[bbo_dong@qq.com](mailto:bbo_dong@qq.com)
- QQ 交流群：**916444032**

欢迎分享使用经验、提交问题复现和参与文档改进。
