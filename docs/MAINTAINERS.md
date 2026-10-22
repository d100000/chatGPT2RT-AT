# 维护、版本更新与公开文档部署

本文件面向运行 bob ai工具的维护者。**同一部署的 Python 代码、已安装包、HTML 模板和静态资源应使用同一版本。** 源码更新后，需要让实际服务进程重新加载；仓库提交成功不等于线上已部署。

## 更新已有安装

先确认没有仍在执行的登录任务。重启无法续跑活动任务；已经保存的成功结果文件会保留。以下命令在实际部署的项目目录执行：

```bash
git status --short
git fetch origin
git pull --ff-only
.venv/bin/python -m pip install '.[web]'
```

`git status` 有本地修改时应先检查和保留，不要用强制重置覆盖服务器配置。若生产服务使用另一虚拟环境，应使用那个环境的解释器，而不是照抄 `.venv`。可检查当前解释器实际加载的位置：

```bash
.venv/bin/python -c "from pathlib import Path; import rt_extractor; print(Path(rt_extractor.__file__).resolve())"
```

随后按真实运行方式重启：

| 运行方式 | 需要完成的动作 |
| --- | --- |
| systemd / Gunicorn | 重启实际管理 Python / Gunicorn 的服务；服务名由部署者设置 |
| Docker / Compose | 确认代码已进入运行镜像或挂载目录，按现有构建配置重建并替换对应容器 |
| 宝塔进程管理 / Supervisor | 重启对应 Python 应用进程，检查工作目录、解释器与入口是否一致 |
| 手动执行 `python -m rt_extractor.web` | 正常停止原进程，再以原端口、输出目录及环境配置启动 |

只重启 Nginx 不会重启 Python 应用。不要为解决加载错误去删除 Host、Origin、CSRF 或本机访问校验。

<a id="module-loading"></a>
## 更新后输入计数不变或按钮不可用

若输入两行账号后仍显示“已输入 0 条”，连接文案停留在初始状态，先查看浏览器 Console。以下错误说明普通脚本加载到了 ES module 的 `import`：

```text
Cannot use import statement outside a module
```

当前模板应包含以下形式；`workspace.js` 和 `app.js` 均作为模块加载：

```html
<script type="module" src="/static/workspace.js"></script>
<script type="module" src="/static/app.js"></script>
```

如果部署只更新磁盘上的 JavaScript，但运行进程仍缓存旧模板，浏览器可能收到旧 HTML 与新脚本。也可能是反向代理静态目录、安装包路径和服务工作目录分别指向了不同版本。可按以下顺序检查：

1. 查看线上“页面源代码”的两个脚本标签，不只检查本地仓库。
2. 检查 `/static/app.js`、`/static/workspace.js`、`/static/request-limits.mjs` 是否返回 200 和 JavaScript MIME；不要让不存在的脚本回退成 HTML。
3. 更新实际使用的安装包，并重启实际应用进程。当前任务和配额保存在单进程中，不能靠增加独立 worker 或副本解决版本问题。
4. 如存在 CDN 或自定义反向代理缓存，确认没有继续发送旧 HTML；必要时按其配置清理相应缓存。
5. 强制刷新浏览器，确认输入计数会随输入变化，状态接口正常，未开始登录时没有意外提交请求。

服务器配置不同会有不同重启命令，仓库不假定某个 systemd 服务名或 Docker Compose 服务名。仅修改浏览器中的标签可用于诊断，不是持久部署修复。

## 公开说明页与工具页的抓取范围

公开文档用于解释功能、术语、转换和问题排查；认证工具页和任务接口不应成为搜索索引中的结果文件入口。计划与实施说明见 [SEO / GEO 方案](SEO-GEO-PLAN.md)。

| 路径 | 内容目的 |
| --- | --- |
| `/docs` | 项目定义、功能入口与文档导航 |
| `/docs/token-extraction` | GPT / ChatGPT Token 提取步骤和前提 |
| `/docs/token-types` | RT、AT、ID Token 与 API Key 的区别 |
| `/docs/cpa-sub2api` | 认证文件格式转换和支持边界 |
| `/docs/faq` | 可独立理解的常见问题与回答 |
| `/`、`/converter`、`/network` | 实际工具界面，不作为公开文档替代品 |
| `/api/` 与结果响应 | 任务操作和敏感结果，不用于收录 |

索引配置必须以实际部署域名为准，不要从任意客户端的 `Host` 或转发头拼接 canonical。配置、robots 和 sitemap 必须与站点的实际可访问状态一致。新增文档路径只有部署当前版本后才会出现在网站，README 中列出路径并不代表网站已经更新。

### 配置公开文档的索引域名

当前实现使用环境变量 `BOB_AI_PUBLIC_URL` 显式启用公开说明页的索引。正式站点应在**实际运行应用的服务环境**中设置：

```text
BOB_AI_PUBLIC_URL=https://tools.bob-api.com
```

默认值为空，页面保持 `noindex`；设置有效域名后，只有上表的五个 `/docs` 页面启用索引、canonical 和 sitemap 条目。三个工具页及敏感接口继续禁止索引。这个变量只控制搜索和分享元信息，**不是公网访问开关**，不会放开本机、Host、Origin、CSRF 或限流校验。

按实际运行方式保存配置并重启应用：

- **systemd**：在对应服务的 `[Service]` 中使用 `Environment="BOB_AI_PUBLIC_URL=https://tools.bob-api.com"`，或在服务已经引用的 `EnvironmentFile` 中写入上面的赋值行。修改服务单元或 override 后重新加载 systemd 配置，再重启该实际服务。只在交互式终端执行 `export` 后重启 systemd，通常不会让服务继承该终端变量。
- **Docker / Compose**：将变量写入对应容器的环境配置；Compose 可在实际服务的 `environment` 下加入 `BOB_AI_PUBLIC_URL: https://tools.bob-api.com`。然后按现有构建和部署配置更新、重建或重新创建对应容器，确认新容器确实收到变量。该仓库不假定某个 Compose 服务名。
- **宝塔 / Supervisor 等进程管理器**：在该应用的环境变量配置中设置，再重启对应进程，不要只修改某个临时终端会话。
- **手动运行**：可在本次启动命令前设置变量，例如 `BOB_AI_PUBLIC_URL=https://tools.bob-api.com .venv/bin/python -m rt_extractor.web --port 8765 --output-dir output`；后续每次启动也需保留相同配置。

应用重新加载后，应检查线上 `/docs` 的 canonical 和 robots、`/sitemap.xml` 的五个条目，以及 `/robots.txt` 中的站点地图地址。域名配置不能证明这些地址已能从公网抓取；仍需结合实际入口、HTTPS 和访问策略检查。

公开文档不应包含真实账号、登录状态、CSRF 值、检测到的访问者 IP、下载路径或 Token 示例正文。公开 FAQ 的可见内容与结构化数据应相同；不能在 JSON-LD 里补上页面不存在的评价、排名、定价或功能承诺。

## 维护 GitHub 描述、Topics 与分享图

仓库的发现元数据保存在 [`.github/discovery.json`](../.github/discovery.json)，包括 About 描述、Homepage 和 Topics。维护者可使用已安装并登录的 GitHub CLI，通过脚本核对配置与线上值：

```bash
.venv/bin/python scripts/sync_github_discovery.py
```

该命令默认只读，报告是否存在差异。明确要把配置应用到 GitHub 时才使用：

```bash
.venv/bin/python scripts/sync_github_discovery.py --apply
```

`--apply` 会修改公开的 About、Homepage 和 Topics，运行前应审查配置；fork 维护者应先把配置中的仓库身份改成自己的仓库。Homepage 暂使用已存在的站点根地址 `https://tools.bob-api.com/`，避免在 `/docs` 尚未上线时把仓库入口改成不存在的页面；确认公开文档上线后可按实际入口再调整。

定制分享图位于 [`docs/assets/github-social-preview.png`](assets/github-social-preview.png)，尺寸为 **1280 × 640**。可在 GitHub 仓库 **Settings → General → Social preview** 中手动上传；文件进入仓库不会自动替换 GitHub 的 Social preview 设置。修改分享图后，应重新检查社交预览和图片上的项目定义，避免保留失效功能或不实承诺。

网页 PNG / SVG 静态资源已纳入 Python 包的资源声明。发布安装包时仍应检查构建产物确实包含对应资源，避免出现源码目录可访问、安装包部署却返回 404 的差异。

## 发布前验证

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
git diff --check
```

测试使用合成账号和模拟网络，不能把它们描述为真实平台登录成功率测试。人工检查时使用占位或合成数据；截图、Issue 与构建日志不得包含真实凭据。

发布后应分别确认：

- 服务健康、脚本 MIME 与模块标签正确，三个工具页面没有 JavaScript 初始化错误。
- 公开文档返回正常 HTML，canonical、站点地图和抓取配置指向实际公开域名。
- 敏感接口及结果响应保持禁止索引，访问控制、CSRF 和限流仍生效。
- Google Search Console / Bing Webmaster Tools 的产权验证、站点地图提交和后续抓取数据由有权限的站长完成；提交不保证收录、排名或 AI 引用。

主项目许可证仍需作者明确选择；不要在未完成授权确认前添加整仓 MIT 徽章或许可证字段。第三方通知必须随相应模块保留。

[项目 README](../README.md) · [常见问题](FAQ.md) · [安全策略](../SECURITY.md) · [完整 SEO / GEO 方案](SEO-GEO-PLAN.md)
