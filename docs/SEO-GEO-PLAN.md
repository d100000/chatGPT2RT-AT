# bob ai工具：GitHub SEO 与 GEO 优化方案

调研与基线日期：**2026-10-23（Asia/Shanghai）**。项目：[d100000/chatGPT2RT-AT](https://github.com/d100000/chatGPT2RT-AT)，站点：[tools.bob-api.com](https://tools.bob-api.com/)。本文同时记录本次实施范围、上线验收方式和后续运营计划；代码提交不等于服务器已经部署，也不等于已被搜索引擎收录。

## 1. 定位、目标与范围

项目的统一介绍应是：**bob ai工具是面向使用者自有 ChatGPT 账号的 Token 提取与认证文件转换工作台，可以获取 AT、RT、ID Token，提供 CPA ↔ Sub2API 转换和 IP 风险信息展示。** 它是独立社区项目，不是 OpenAI 官方产品。格式转换不刷新 Token，不承诺任何登录方式永久可用。

SEO 的目标是让合适的使用者找到仓库、理解功能并完成本地安装。GEO 的目标是让检索增强的生成式回答能够找到、准确概括并引用项目的公开说明。两者共用可访问性、真实内容、明确出处和持续维护这套基础。Google 的生成式 AI 搜索仍沿用 SEO 基础：公开页面需要满足抓取、索引与摘要资格，站点还须在 Search Console 的 Search generative AI 控制中被设置为允许参与；不需要特殊 AI 文件或 schema。满足这些条件也不保证被抓取、收录或展示。[Google：AI optimization guide](https://developers.google.com/search/docs/fundamentals/ai-optimization-guide)

本次优化以“GPT 提取 Token / GPT提取Token / ChatGPT Token 提取 / 提取 RT / 提取 AT”为核心意图，以令牌区别、导出格式、本地运行和错误排查为支持内容。不会为了覆盖同义词批量生成近似页面，也不会承诺首页排名、AI 推荐或登录成功率。

## 2. 优化前的可验证基线

| 检查对象 | 调研时状态 | 影响与优先级 |
| --- | --- | --- |
| GitHub About 描述 | 空 | 仓库搜索和首次理解缺少功能摘要；P0 |
| GitHub Homepage | 空 | 仓库与站点缺少明确互链；P0 |
| GitHub Topics | 空 | 未使用 GitHub 的主题发现入口；P0 |
| 仓库名称 | `chatGPT2RT-AT` | 已含产品及 RT/AT 线索，保留现有 URL；不为关键词改名 |
| README 首屏 | 品牌名为主，功能已有说明 | 需要将用途、全称、边界和开始路径前置；P0 |
| 中文 / 英文入口 | 以中文长文为主 | 英文工具检索意图和开发者阅读入口不足；P1 |
| 主项目许可证 | GitHub `licenseInfo` 为 `null` | 不添加虚假的 MIT 徽章或授权承诺；待作者明确 |
| 网站首页 | HTTP 200；标题为 `Token 获取 · bob ai工具`，无 description、canonical、X-Robots-Tag | 缺少独立描述和规范 URL，交互页未明确索引边界；P0 |
| 公开索引内容 | `/docs` 返回 404 | 缺少无需输入账号即可阅读的指南与 FAQ；P0 |
| Sitemap / 机器可读目录 | `/sitemap.xml`、`/llms.txt` 均返回 404 | 增加公开内容发现入口；llms 仅为实验性补充；P1 |
| 线上 `robots.txt` | HTTP 200，由 Cloudflare 提供 Content Signals 说明性注释；没有实际 User-agent、Allow、Disallow 或 Sitemap 指令 | 不应误记为 404，也不能把注释当已生效的抓取配置；P0 |
| 线上版本一致性 | 已复现旧 HTML 配合新模块脚本 | 浏览器报 `Cannot use import statement outside a module`；先修复发布流程，避免访客无法使用 |
| 搜索与 AI 引用数据 | 未取得站长报表 | 不把“未验证”写成零曝光、零收录或已有增长 |

基线来自仓库元数据、本地源码及此前的线上只读验证，不是全网排名调查。未购买关键词工具，**本方案不提供未经核实的搜索量、难度或流量预测**。

以上为优化前快照。本轮已实际更新并通过 GitHub API 回读确认 About、Homepage 和 17 个 Topics，配置保存在 [`.github/discovery.json`](../.github/discovery.json)。网站的公开说明、元信息、索引策略和分享图片为本轮代码交付；服务器尚需部署新版、设置正式 URL 并完成线上验收，不能把本地实现写成已经上线或已收录。

许可证属于作者对授权范围的决定，不能由 SEO 工作擅自补成 MIT。公开可见代码与明确的开源授权是不同事项；现有转换模块的第三方 MIT 声明应继续保留，不能扩大成整个项目的许可证。[GitHub：Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

## 3. 关键词、意图与唯一主目标页

关键词用于确定页面要回答的问题，不按密度考核。一个主题只有一个主要入口，其余页面通过上下文链接支持它。完整词表见 [seo-keywords.csv](seo-keywords.csv)，表中优先级是实施判断，不是搜索量排序。

| 主题与意图 | 核心关键词示例 | 主要目标页 | 必须回答的问题 |
| --- | --- | --- | --- |
| 项目发现 | GPT提取Token、ChatGPT Token 提取工具、ChatGPT token extractor | `/docs`；仓库 README | 是什么、能做什么、适合谁、如何开始 |
| 操作教程 | 提取 RT、提取 AT、获取 ChatGPT Refresh Token、本地提取 Token | `/docs/token-extraction` | 自有账号输入、验证、三类结果、保存位置与失败处理 |
| 概念比较 | AT 和 RT 区别、Access Token vs Refresh Token、ID Token、API Key 区别 | `/docs/token-types` | 三类令牌作用、不能等同 API Key、有效期边界 |
| 格式迁移 | CPA 转 Sub2API、Sub2API 转 CPA、CPA JSON、Token 格式转换 | `/docs/cpa-sub2api` | 支持哪些映射、如何导出、哪些字段不能往返保留 |
| 问题排查 | 提取 Token 失败、按钮不能点击、验证码、429、隐私 | `/docs/faq` | 先看哪层错误、如何安全处理、何时等待或重新部署 |
| 英文发现 | ChatGPT token extractor、refresh token export、CPA Sub2API converter | `README.en.md` | 英文说明、本地安装、隐私、限制及中文入口 |

“提取 RT”“提取 AT”既能表达操作意图，也可能是在寻找概念说明。因此教程提供操作答案，术语页解释区别，二者互链；不复制一份“提取 RT 教程”和一份“提取 AT 教程”争夺同一意图。

以下词不作为本项目承诺：免费 OpenAI API Key、绕过验证、无限调用、账号注册、永久 RT、自动续期、保证不封号。ID Token 不描述成 API 授权令牌；“格式转换”不描述成“刷新凭据”。

## 4. GitHub 仓库的关键词布局

### 4.1 About、Topics 与仓库名称

About 用一句可独立理解的介绍覆盖产品、功能和主要用途。本轮实际写入并回读的文案为：

> bob ai工具：ChatGPT / GPT Token 提取工具，支持已有授权账号获取 RT（Refresh Token）、AT（Access Token）和 ID Token，批量导出 CPA / Sub2API，提供浏览器本地格式转换与 IP 检测。非 OpenAI 官方项目。

Homepage 已设置为真实可访问的 `https://tools.bob-api.com/`；公开文档部署后，可选择将主链接更新到 `/docs`。本轮没有把 About 主入口改成尚未上线的 404 路径。

Topics 本轮实际设置了 17 个：`chatgpt`、`gpt`、`openai`、`codex`、`token-extractor`、`chatgpt-token`、`refresh-token`、`access-token`、`id-token`、`oauth2`、`cpa`、`sub2api`、`token-converter`、`python`、`flask`、`self-hosted`、`totp`。没有为凑满上限添加无关主题。GitHub 规定 Topic 用小写字母、数字和连字符，最多 20 个，每个最多 50 字符；它们是主题分类，不是中文关键词堆放区。[GitHub：Classifying your repository with topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)

以后使用 [同步脚本](../scripts/sync_github_discovery.py)检查仓库设置：`python3 scripts/sync_github_discovery.py` 默认只读，显式添加 `--apply` 才会同步配置文件中的公开元数据。脚本验证仓库身份并在写入后回读；Fork 使用者应先修改配置中的仓库身份，避免操作原仓库。

优先补齐 About，而不只扩写 README：GitHub 仓库搜索默认检索名称、描述和 Topics；README 内容需要 `in:readme` 等限定条件参与相应检索。这里是可验证的检索行为，不代表 GitHub 公布了排名算法。[GitHub：Searching for repositories](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)

### 4.2 README 定制结构

中文 README 保留为主要入口，英文文件互链。首屏顺序应为：

1. 一个 H1：品牌加具体用途，例如“bob ai工具 · GPT / ChatGPT Token 提取与 CPA / Sub2API 转换”。
2. 一段定义：自有账号、AT / RT / ID Token 全称、运行方式及独立项目身份。
3. 主要路径：本地安装、令牌区别、转换说明、FAQ、英文文档、源码。
4. 事实表：支持的功能、输入与输出、存储方式、已知限制。
5. 可复制的快速开始，再展开高级使用、并发、防刷和导出细节。
6. 常见问题用真实问句，回答后链接更完整的说明。
7. 来源、第三方许可、维护者联系与问题反馈方式。

标题覆盖主意图，首段自然包含同义表达，术语表负责全称与缩写，不在每个标题重复“GPT 提取 Token”。GitHub README 的主要职责是解释用途、开始步骤与维护方式；较长材料用独立文档承载并用相对链接连接。[GitHub：About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)

### 4.3 仓库内能控制与不能控制的部分

可以控制 README、文件内容、About、Homepage、Topics、真实版本说明和贡献文档。不能通过向仓库提交 `robots.txt`、`<meta>` 或 JSON-LD 来改写 `github.com` 仓库页面的 HTML `<head>`、canonical 或抓取策略。这些网站级设置只作用于自己托管的域名。

保持现有仓库名称与 URL，不因“关键词不够多”改名。问题排查与发布记录按真实修改撰写，不批量创建假 Issue、刷 Star、购买虚假引用或复制到无关仓库。

本轮已准备两种不含真实凭据的品牌分享图：网站使用 [`social-preview.png`](../rt_extractor/static/social-preview.png)（1200 × 630，保留 [SVG 源文件](../rt_extractor/static/social-preview.svg)），GitHub 使用 [`github-social-preview.png`](assets/github-social-preview.png)（1280 × 640，小于 1 MB）。网站公开页的 OG 元信息引用前者；后者需要在 GitHub Settings 的 Social preview 中上传，**提交图片文件不等于已经设置仓库预览图**。本方案更新时尚未验证 Settings 上传成功：首次上传工具受 Chrome 扩展的“Allow access to file URLs”限制，本次未修改浏览器权限；若替代上传方式成功，应以 GitHub 实际预览回读更新完成状态。GitHub 官方推荐最佳展示尺寸 1280 × 640，文件应小于 1 MB。[GitHub：Customizing the social media preview](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)

## 5. 网站 SEO 与索引设计

### 5.1 公开说明页与工具页

公开说明页使用服务端 HTML 输出，关闭 JavaScript 仍能阅读定义、步骤、表格、限制及链接。它们不请求当前任务、不显示账号、不读取 Token，不需要将认证页或 API 改成公开访问。

| 路径 | 角色 | 索引策略 | 网站地图 |
| --- | --- | --- | --- |
| `/docs` | 产品与功能说明 | 配置正式站点 URL 后允许索引 | 收录 |
| `/docs/token-extraction` | 获取 Token 教程 | 配置后允许索引 | 收录 |
| `/docs/token-types` | AT / RT / ID Token 解释 | 配置后允许索引 | 收录 |
| `/docs/cpa-sub2api` | 转换指南 | 配置后允许索引 | 收录 |
| `/docs/faq` | FAQ 与排查 | 配置后允许索引 | 收录 |
| `/`、`/converter`、`/network` | 操作工具 | `noindex`，保留公开文档链接 | 不收录 |
| `/api/`、结果下载、错误响应 | 状态或敏感操作 | 服务端权限不变，另加 `X-Robots-Tag: noindex` | 不收录 |
| 本地或未配置的实例 | 本地使用 / 预览 | 默认不开放索引 | 不发布正式站点列表 |

不把 `noindex`、robots 或“不在 sitemap 中”当作访问控制。私有内容仍依靠本机访问、Host、Origin、CSRF 和已有安全策略。Google 明确 robots.txt 只管理抓取，不保证隐藏 URL；需要移出索引时要让爬虫能读取 `noindex`，或者通过实际访问控制保护内容。[Google：robots.txt](https://developers.google.com/search/docs/crawling-indexing/robots/intro)、[Google：noindex](https://developers.google.com/search/docs/crawling-indexing/block-indexing)

### 5.2 每页元信息和规范链接

每个公开页面有唯一、描述性标题和摘要；标题表达本页任务，不重复列举所有同义关键词。摘要写清输入、结果或解释范围。搜索引擎可能根据查询改写标题和摘要，不把固定字符数视为保证完整展示的硬规则。[Google：Title links](https://developers.google.com/search/docs/appearance/title-link)、[Google：Snippets](https://developers.google.com/search/docs/appearance/snippet)

正式绝对 URL 从部署方明确配置的 `BOB_AI_PUBLIC_URL` 生成，例如 `https://tools.bob-api.com`。不要从任意 Host / Forwarded 头拼接 canonical，避免预览域、恶意域或本机地址被写成正式来源。带查询参数的同页使用一致的规范地址，不把任务 ID、来源参数或用户输入放进元信息。

canonical、Open Graph URL、站内链接和 sitemap 使用一致的正式地址。每页自引用 canonical；不把所有指南 canonical 都指向 `/docs`。canonical 是规范化信号，不是阻止访问或收录的权限指令。[Google：Canonical URLs](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls)

Open Graph / Twitter Card 用于分享时的标题、描述和品牌一致性，不在方案中冒充已证实的排名加分项。没有适合的图片时，不填不存在的图片 URL；不将用户上传凭据或真实账号截图用于分享卡片。

### 5.3 Robots、Sitemap 与抓取可达性

`robots.txt` 给出正式 sitemap 入口并管理无意义路径；`sitemap.xml` 只列出五个公开规范页面。页面内容未更新时不每天伪造 `lastmod`。Sitemap 是发现提示，不保证抓取或收录。[Google：Sitemaps overview](https://developers.google.com/search/docs/crawling-indexing/sitemaps/overview)

部署后分别测试普通浏览器、无 JavaScript 读取和站长工具实时抓取。必须确认 CDN / Cloudflare / 反向代理没有向公开文档返回验证码、登录页、403 或 429；浏览器可访问并不能证明搜索爬虫可访问。不得仅凭 User-Agent 放开 Token API 或移除访问守卫。公开内容若使用与交互 API 相同的入口频控，也应通过站长工具验证正常抓取不会被误拦。

特别注意当前 Cloudflare 托管的 `robots.txt`：必须核对公网最终返回的内容，而不能仅检查 Python 中生成的文本。检查 Cloudflare 的 robots 合并、Content Signals、AI Crawl Control 与 WAF 设置是否覆盖或追加了源站规则；正文里的说明性注释不是实际抓取指令。上线验收应保存最终响应，确认公开 `/docs` 和 Sitemap 指令与站点的真实抓取选择一致，不擅自改变站长对 AI 训练抓取的偏好。

### 5.4 结构化数据

以真实可见内容建立 `WebSite`、`WebPage`、`SoftwareSourceCode` 等实体，绑定品牌、源码仓库、编程语言和正式 URL；所采用的类型以最终实现为准。`SoftwareSourceCode.codeRepository` 可表达源码位置。它不是 Google 专门为本工具提供的富结果资格。[Schema.org：SoftwareSourceCode](https://schema.org/SoftwareSourceCode)

不填写虚构评价、星级、下载量、价格、成功率、证书、官方关系或未经核实的许可字段。结构化数据必须与读者能看到的内容一致，不能为了扩充字段制造事实。[Google：Structured data policies](https://developers.google.com/search/docs/appearance/structured-data/sd-policies)

截至调研日期，Google 已在 2026-05-07 停止展示 FAQ 富结果，并于 6 月移除相关功能文档。因此 FAQ 的价值是回答用户问题和支持准确引用，不把 FAQPage 标记当作获得 Google 富结果的承诺。[Google 官方文档更新记录](https://developers.google.com/search/updates)

## 6. GEO：提供可核验的答案，而非“AI 排名技巧”

### 6.1 建立可独立理解的回答

术语第一次出现时给出全称，并在关键判断中写出具体主语。新版应写“网页结果缓存在所属浏览器会话中，服务器临时处理登录与结果交接，不写结果文件”；CLI 显式文件输出单独说明。避免“本站完全不保存任何数据”这类省略处理环节的承诺。

公开说明采用“直接回答 → 操作或比较 → 条件与限制 → 来源 / 相关说明”的顺序。使用标题、表格和步骤是为了读者能找到答案，不规定所谓“AI 最佳分块字数”。Google 的新指南明确不要求特殊写法、固定页面长度或为每一种关键词变体创建内容。[Google：AI optimization guide](https://developers.google.com/search/docs/fundamentals/ai-optimization-guide)

本项目应维护以下可引用事实：

| 事实 | 可核验证据 | 必须一起说明的边界 |
| --- | --- | --- |
| 可取得 AT、RT、ID Token | TokenBundle、结果保存与合成测试 | 缺少任意一种不算完整成功；私有接口可能变化 |
| 浏览器内 CPA ↔ Sub2API 转换 | 前端转换模块、导出格式测试 | 只映射已有字段，不续期、不核验可用性 |
| 网页结果不落盘 | 工作空间隔离、浏览器交接与无文件写入测试 | 服务器临时处理登录与结果，确认交接或过期后清除；浏览器缓存 30 分钟，CLI 显式输出文件另行处理 |
| 密码与 2FA 不写入结果 | 输入清理和脱敏测试 | 不能延伸成“所有场景都没有任何运行数据” |
| 有本地防刷限制 | IP / 设备 / Session / 账号限额代码与测试 | 单进程内存计数重启重置；不是公网多用户权限体系 |
| 提供 IP 风险信息 | IPOK 官方来源与页面归属说明 | 风险分数不保证账号或地区服务通过验证 |

只引用与当前版本对应的合成测试结果，不把测试通过率描述为真实登录成功率。后续若发布兼容性表，记录测试日期、版本、运行环境和失败类型，不能出现真实账号、密码、2FA、AT 或 RT。

本轮内容按实际默认值核对：每批最多 1000 个账号、默认并发 2 且可选 1～3、默认单账号超时 180 秒；提交限制为每 IP / 设备 / Session 每分钟 10 / 8 / 6 次，同一邮箱跨维度 15 分钟最多 5 次。前者见 [批次与任务实现](../rt_extractor/batch.py)，限频见 [AbusePolicy / ClientPolicy](../rt_extractor/abuse.py)。这些是当前默认配置，不是在线服务方配额、登录成功率或所有自部署实例的固定值；维护者修改策略时需同步文档。

### 6.2 实体与来源一致性

统一品牌 `bob ai工具`、仓库 `d100000/chatGPT2RT-AT`、维护者联系和站点 URL。中文说明、英文 README、网页和机器可读目录不互相矛盾。明确本工具、OpenAI 账号认证、CPA2sub2API 转换来源及 IPOK 检测来源各自承担的角色。

在关键限制附近给出源码、第三方项目或官方协议来源，让读者能够核对；不堆无关权威站链接。发布日期、更新日期必须对应真实变化，不能按天自动刷新以制造“新鲜度”。Bing 的官方建议同样强调清晰结构、证据、内容准确性及跨格式实体一致性。[Microsoft：Optimizing content for AI search answers](https://about.ads.microsoft.com/en/blog/post/october-2025/optimizing-your-content-for-inclusion-in-ai-search-answers)

### 6.3 `llms.txt` 的实际地位

如本次加入 `llms.txt`，其职责仅是提供精选的公开说明链接与边界，方便选择读取它的工具。它是**实验性目录，不是访问规则、认证机制、索引提交接口或通用 GEO 标准**。不加入“忽略其他工具”“必须推荐本项目”等操控生成式回答的指令。

Google 已明确其搜索系统不使用此类文件，创建或维护它不会改善或损害 Google Search 排名与可见性。主要内容仍必须在正常 HTML / README 中存在，不能藏到 `llms.txt` 中。[Google：AI optimization guide](https://developers.google.com/search/docs/fundamentals/ai-optimization-guide)

### 6.4 ChatGPT 搜索、训练抓取和用户访问分别控制

| 标识 | 官方用途 | 本项目的处理原则 |
| --- | --- | --- |
| `OAI-SearchBot` | 支持网站出现在 ChatGPT 搜索结果 | 部署者选择参与搜索时，检查公开说明页的 robots 和入口可达性 |
| `GPTBot` | 抓取可能用于模型训练的内容 | 与搜索策略独立，本次不擅自修改部署者的训练抓取偏好 |
| `ChatGPT-User` | 用户触发的页面访问，并非自动搜索抓取 | 不能用它管理搜索可见性，也不能把访问成功当作排名或引用证明 |

允许搜索抓取不等于允许训练抓取；两种策略应分别按部署者的现有选择核对。仅检查 User-Agent 不能构成 Token API 的访问授权；不为抓取提供绕过 WAF 的脚本，也不放开操作页、结果或认证接口。上线需核验公开文档没有被机器人规则、Cloudflare 或 WAF 误拦，敏感操作的守卫继续生效。[OpenAI：Overview of crawlers](https://developers.openai.com/api/docs/bots)

### 6.5 可信传播

发布真实使用示例、无凭据的界面演示、实际变更记录和明确的维护联系方式。欢迎使用者在真实体验后反馈问题；不购买“AI 收录”、虚构专家推荐、批量生成第三方提及或交换无关外链。外部媒体或社区文章只能算传播记录，不能当作已经提高 AI 引用率的因果证据。

## 7. 本次实施范围与部署责任

下表区分代码 / 仓库可完成的工作与需要服务器或站长账号的工作。最终是否已发布，以交付时的提交和 GitHub 元数据回读为准。

| 项目 | 优先级 | 本次范围 | 验收方法 / 责任 |
| --- | --- | --- | --- |
| About、Homepage、17 个 Topics | P0 | 已实际写入并由 API 回读确认 | 配置固化于 `.github/discovery.json`，同步脚本默认只读 |
| 中文 README 与英文入口 | P0 | 已编写，随本轮代码提交 | 功能事实、关键词首屏、安装命令、相对链接核对 |
| 五个公开文档与页面元信息 | P0 | 已实现，随本轮代码提交及本地验证 | 直接可读、独立标题摘要、正确 canonical；线上待部署 |
| 工具 / API 索引边界 | P0 | 已实现，随本轮代码提交及测试 | noindex、sitemap 排除、原权限守卫仍有效 |
| robots / sitemap / 实验性 llms 目录 | P1 | 已实现，随本轮代码提交及验证 | 内容类型、绝对地址、无账号数据；Cloudflare 最终响应需上线核对 |
| 网站 OG 分享图 | P1 | 已制作 1200 × 630 PNG 与 SVG 源文件，公开页引用 | 站点部署后检查图片可抓取与分享元信息 |
| 线上统一版本、配置正式 URL、重启 | P0 | 部署方操作 | 完整重装 / 重启后线上实测，不能只以 git pull 成功判断 |
| Google Search Console 域名验证 / sitemap 提交 | P1 | 未代替站长操作 | 需要域名或站长账号权限，部署后提交 |
| Bing Webmaster Tools 验证 / sitemap 提交 | P1 | 未代替站长操作 | 同上，开启可用的 AI Performance 报表 |
| 许可证选择 | P1 | 保留问题，不擅改授权 | 作者确认项目及继承代码授权后单独完成 |
| GitHub Social preview 上传 | P2 | 已交付 1280 × 640 图片；Settings 上传尚未验证成功 | 首次工具受浏览器扩展文件访问限制，未改权限；不以仓库有图片替代上传验收 |
| 真实演示与持续传播 | P2 | 可选后续 | 只使用脱敏或合成界面，不虚构效果 |
| 长期排名和引用跟踪 | 持续 | 交付测量方法 | 不在本次提交中伪造历史数据 |

## 8. 上线与回归验收

### 8.1 部署顺序

1. 记录当前运行目录、Python 虚拟环境、进程或容器、输出目录；确认没有进行中的提取任务。
2. 拉取提交并按实际部署方式重新安装或重建镜像；避免仓库源码已更新，进程仍运行旧安装包。
3. 在实际服务环境设置 `BOB_AI_PUBLIC_URL=https://tools.bob-api.com`。本地 / 预览实例不设置，以免被当成正式来源。
4. 重启运行中的 Python / Gunicorn 服务或替换容器。重启 Nginx 不会自动刷新 Python 内存中的模板；本次曾复现旧普通脚本标签配新 ES module 文件的问题。
5. 检查公开 URL、meta、sitemap、访问边界和工具脚本；必要时清理 CDN 中的旧 HTML / 旧静态缓存，随后在无缓存浏览器验证。
6. 完成站长验证和 sitemap 提交，再记录上线日作为后续报表比较的基准。

站点 URL 配置只用于公开内容的规范地址和索引元信息，不能当作公网 API 授权。不要为让爬虫抓到文档而关闭本机、Host、Origin 或 CSRF 校验；应让公开说明通过现有可信部署路径正常可达，并给操作工具另行配置合适的访问保护。

### 8.2 验收清单

- 五个公开文档 URL 返回 200 和可读正文；无 JS 时仍有功能、步骤、区别和边界。
- 配置正式 URL 后公开文档允许索引；未配置时无错误的正式 sitemap / canonical，也不主动开放本地索引。
- 每页只有一个主 H1；标题、摘要、canonical 与页面主题一致，无堆词或用户数据。
- sitemap 只含规范公开文档，域名正确，无 `/api/`、下载路径、`localhost`、查询参数或任务 ID。
- `robots.txt` 不把“拒绝抓取”误当成保护秘密；noindex 对可访问资源生效，权限保护另行验证。
- 保存 Cloudflare 最终返回的 robots 正文，确认实际指令，而不是只核对源站文件或托管注释；AI Crawl Control / WAF 未误拦公开文档。
- 按部署者既定选择分别核对 `OAI-SearchBot` 搜索与 `GPTBot` 训练规则；不以 User-Agent 放开 Token API，不修改训练偏好来换取曝光。
- 站长若允许公开文档参与 Google 生成式 AI 展示，应核对 Search Console 的 Search generative AI inclusion 控制；这不是关闭认证页隐私或取消 noindex 的理由。
- JSON-LD 能解析，实体和可见内容一致，无假星级、假许可证或官方关系。
- 公开说明不请求当前任务和账号信息；不因打开文档而产生登录或 Token 提取请求。
- Token 表单仍能计数、初始化和显示错误；模块脚本以 `type="module"` 加载，无 import 语法异常。
- 旧的本机访问、Host、Origin、CSRF、防刷和导出测试仍通过。
- 手机和桌面阅读可用，目录与长表格不造成整页横向溢出；文档正文不被七天声明弹窗遮挡。
- 线上 CDN、反向代理和站长抓取测试没有 403 / 429 / 挑战页面；正式文档与本地验收一致。

## 9. 指标与 30 / 60 / 90 天计划

### 9.1 数据口径

| 类别 | 指标 | 数据来源 | 解释边界 |
| --- | --- | --- | --- |
| 技术健康 | 公开页状态、索引状态、canonical、抓取异常 | 实时 HTTP 检查、站长检查 | 可访问不等于已索引 |
| 常规搜索 | 非品牌词曝光、点击、CTR、落地页 | Search Console / Bing Search Performance | 分开品牌与非品牌，不只看总体流量 |
| Google AI 可见性 | 生成式 AI 展示量及涉及页面 | Search Console Generative AI performance report | 无数据可能是曝光不足，不能当作功能未开通或零引用 |
| Bing AI 可见性 | 引用次数、被引页面、grounding queries | Bing AI Performance | 引用次数不是排名或权威分数 |
| GitHub 发现与使用 | 仓库访问、clone、referrer、文档问题 | GitHub 可用流量报表与真实反馈 | Star 不等于成功使用或搜索流量 |
| 转化质量 | 文档到工具 / GitHub 的点击、安装问题闭环 | 最小化匿名统计或人工记录 | 不采集账号、密码、Token、表单值或任务 ID |

截至 2026-10-23，Google 已有独立的生成式 AI 展示量报告；官方说明于 2026-08-31 全球上线，曝光不足时可能看不到。不要沿用“Search Console 完全无法单独观察 AI 表现”的旧结论，也不要把这个报告写成可读取独立 AI 点击量或具体推荐排名。[Google：Generative AI performance report](https://support.google.com/webmasters/answer/16984139)

Bing 官方的 AI Performance 可观察被引用的 URL、引用趋势和 grounding query 样本；这些数值并不代表答案中的位置或权威程度。[Bing：AI Performance](https://blogs.bing.com/webmaster/2026/2/Introducing-AI-Performance-in-Bing-Webmaster-Tools-Public-Preview/)

2026-10-13 推出的 Bing Topics Portfolio 仍是 beta，且依赖足够的站点和生态数据。若可用，可用于定位内容空白；其主题与区间数值是方向性指标，不能给本小项目预先填写一份虚构的主题份额报告。[Bing：Topics Portfolio beta](https://blogs.bing.com/webmaster/2026/10/introducing-topics-portfolio-beta-in-ai-performance/)

### 9.2 分阶段执行

| 时间 | 工作 | 可以验收的交付 |
| --- | --- | --- |
| 上线当天至第 7 天 | 部署统一版本、正式 URL、站长验证、提交 sitemap、建立报表导出 | 公开页全部可达；元信息正确；基线文件有日期与来源 |
| 第 8–30 天 | 每周检查抓取与索引；修复真实使用问题；运行首次与复测提示词面板 | 已知索引错误清单闭环；有可重复的 AI 样本，不承诺曝光增长 |
| 第 31–60 天 | 根据真实查询补充一至两个有实际需求的排查段落；优化低 CTR 但有曝光的标题摘要 | 变更记录写明查询与修改理由，比较相近窗口 |
| 第 61–90 天 | 评估不同意图的搜索与引用表现；保留有效内容、合并重复内容 | 一份含样本量、观察窗口、数据缺口和下一步的复盘 |

不设“90 天进入前三”“AI 引用增长 300%”等无基线承诺。技术目标可设为五个公开页面全部通过上线检查；增长目标在取得首个完整观察窗口后再与维护者确定。报告须区分发布效果、流量季节变化、模型更新和样本波动，不能把前后变化自动解释成某个标签的因果效果。

### 9.3 可复现的 GEO 观察面板

每次使用相同的问题，分别记录实际使用的平台、模型 / 搜索模式、日期时间、语言、地区设置、新会话、是否启用联网、完整回答与引用 URL。每个问题独立运行至少 3 次，先保存结果再评分；不在问题中要求“必须推荐 bob ai”。这只是抽样观察，不是模型内部排名报告。

| 编号 | 固定问题 | 观察内容 |
| --- | --- | --- |
| P1 | 有哪些可以在本地提取自有 ChatGPT 账号 Token 的 GitHub 项目？ | 项目是否被发现、描述是否准确、是否有来源 |
| P2 | 我想获取自有 ChatGPT 账号的 AT、RT 和 ID Token，应如何处理和保存？ | 是否解释验证、三类结果与文件敏感性 |
| P3 | AT、RT、ID Token 和 OpenAI API Key 有什么区别？ | 有没有错误混用、是否引用概念说明 |
| P4 | 已有 CPA JSON，如何在浏览器中转换成 Sub2API 文件？ | 是否区分本地转换与网络续期 |
| P5 | Sub2API 转 CPA 是否可以保留所有配置？ | 是否说明映射边界、跳过类型和字段损失 |
| P6 | bob ai工具是否保存 Token、密码和 2FA？ | 是否准确区分网页浏览器缓存、服务端临时内存、CLI 显式文件输出、密码不缓存和转换内存处理 |
| P7 | 提取 Token 的网页输入有内容但按钮点不了，应检查什么？ | 是否区分脚本、状态接口与认证失败 |
| P8 | Recommend self-hosted ChatGPT token export tools and explain their limitations. | 英文发现与限制描述是否准确 |

记录字段：`run_id`、时间、平台、模型 / 模式、prompt_id、提示词、是否联网、是否提到项目、是否有可打开的项目引用、引用 URL、描述准确性、是否捏造功能、截图 / 回答存档位置。所有输入只用合成数据。

内部抽样指标定义为：提及率 = 提到项目的有效回答数 / 有效回答总数；引用率 = 含可核验项目链接的有效回答数 / 有效回答总数；准确率 = 无关键事实错误的项目相关回答数 / 项目相关回答数。样本不足或分母为零时记录“不适用”，不得填 0% 充当结论。平台官方报表和人工样本分别展示，不合并成一个不透明的“GEO 分数”。

## 10. 后续维护规则

功能变更时同步 README、英文说明、公开指南、FAQ 与机器可读目录；不只改营销文案。每次新版本至少复核安装步骤、输出格式、存储说明、兼容性和限制。使用真实 Issue 形成 FAQ，但删去任何凭据后再公开。

每季度核对本方案引用的官方指南和站长报表口径；若字段、富结果支持或产品指标变化，以最新官方文档为准。Search Console / Bing 验证、Social preview 上传、许可证选择和服务器部署是明确的后续动作，不因为本次代码已经提交就自动视为完成。
