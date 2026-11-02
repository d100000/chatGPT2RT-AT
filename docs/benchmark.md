# 并发分析

本模块新增公共导航「并发分析」和 `/benchmark` 页面，沿用 Flask、httpx、Jinja2 与原生 JavaScript/CSS。所有性能指标来自服务端实际 HTTP 请求；页面与 HTML 报告中的文字分析由统计结果确定性生成，不调用额外模型。

## 启动与使用

在此发布仓库安装已有网页依赖并启动：

```bash
python -m pip install -e '.[web]'
python -m rt_extractor.web --port 8765
```

父目录的 `start-rt-extractor.command` 同样会启动此仓库。已有服务需重启以载入新路由。打开 `http://127.0.0.1:8765/benchmark`。

1. 在「连接设置」中保存共享连接，或直接在测试配置中填写 URL、协议和 API key。支持 Responses / Chat Completions，允许无鉴权接口。
2. 读取模型，搜索并勾选 1–10 个模型。连接修改后会清除旧列表，700ms 后自动重新读取；读取列表不会开始压测。
3. 设置并发档位、每档请求数、近似输入范围和输入模式。预览会显示测量、预热和总调用数。每阶段测量数为 `max(请求数, 并发数)`，总调用上限 10,000。
4. 开始测量。任务按模型、输入模式、升序并发档位依次执行；每阶段有界工作池，完成一个补发一个。共享前缀阶段先执行一次独立预热。
5. 刷新、切换 Tab 或关闭页面不会停止任务。回到页面即可恢复当前运行配置与进度，但不恢复密钥原文。停止按钮始终停止当前后台批次，即使正在查看历史。
6. 查看最近 50 批历史，导出 HTML、CSV 或 JSON。运行中、停止后或错误后的部分结果也可导出。页面只展示最近 50 条请求；CSV/JSON 包含全部已派发请求。

例如 2 模型 × 2 模式 × 并发 1/5/10 × 每档 20 次：12 阶段、240 次测量、6 次预热，总计 246 次调用。

## 保存、密钥与隔离

历史默认位于 `~/.local/share/bob-ai-tools/benchmark/`，可用 `GPT_BENCHMARK_DATA_DIR` 指定独立目录。每批一个目录，包含 `summary.json` 和 `records/*.json`。开始状态和完成状态通过临时文件及原子重命名保存；摘要写入在同一进程锁中串行执行。启动前无法保存历史时不会发往上游；运行期间保存失败会停止派发、取消本地连接，并保留内存结果供导出。

共享密钥仅在服务进程内存保存；手动密钥优先，留空仅复用相同协议、域名和端口的共享密钥。共享密钥不会发送回浏览器。保存共享连接时空密钥表示移除现有密钥；清除按钮会删除整个共享连接。普通测试配置可写入 localStorage，密钥不进入 localStorage、sessionStorage、历史或导出。每批结束释放服务端运行密钥引用。

历史由独立签名 HttpOnly Cookie 隔离。`.identity.key` 是用于验证本地历史访问身份的随机签名材料，不是上游 API key；保留该文件及浏览器 Cookie 才能在服务重启后访问原历史。重启后的 running/stopping 批次改为 interrupted，未确认完成请求改为 cancelled，不自动重发。不要同时用多个服务进程共享同一历史目录；模块按项目原有的单进程、多线程服务方式运行。

公共工作空间接口提供共享连接状态、当前并发任务及后台任务数。所有接口沿用现有本机地址、来源和 `X-RT-CSRF` 校验；写接口要求 JSON，配置请求体最多 128 KiB。上游禁止自动重定向，模型发现总超时 30 秒，单个响应体上限 8 MiB。

## 指标口径

- TTFT：实际发出请求至第一个非空白可见正文流式 delta。推理首段和响应头延迟单独记录。成功 JSON 回退没有 TTFT，完成事件中的整段正文也不作为首字事件。
- 成功要求合法正文和结束状态。Chat 要求 stop/length 与 `[DONE]`；Responses 要求 completed。工具调用、非法 JSON、异常结束和提前断流均失败，保留已收到的最多 2048 字符输出预览。全程无自动重试。
- 成功率分母为已结束测量请求的成功 + 失败 + 取消，在途和未派发不计入。所有性能统计排除预热。任务 completed 只表示计划执行结束，不表示请求全部成功。
- 时间分布使用有效样本，P50/P95/P99 为 R-7 插值。任务和模型的测量时长是阶段测量窗口之和；成功 RPS 和输出 Token/s 使用此墙钟时间。输出 Token 可能包含推理，不是正文生成速度。任务总耗时另外包含预热与阶段间隔。
- 缓存率只对 input/cached 都为安全非负整数、且 cached≤input 的成功测量样本按总量计算。明确 0 是有效未命中；缺失为未知，负数、小数、数字字符串、超安全整数与 cached>input 为异常。记录有效/缺失/异常数、请求覆盖、输入量覆盖及两种命中比例。
- 输入由随机 seed 和模式、档位索引、样本索引、预热标志确定性生成，不包含模型 ID。同位置不同模型使用相同输入；记录目标长度、字符数、SHA-256、nonce 和 180 字符摘要。英文词数只是近似 Token 目标，实际 usage 不按文字估算。
- 随机输入不保证冷缓存，共享前缀不保证缓存命中。结果受网络、负载、限流、分词、输出长度、推理量及模型顺序执行时间差影响；最佳档位不直接等于稳定容量。取消本地请求无法确认上游计算停止，不评估费用、身份、智力或答案质量。

HTML 为无脚本、无外部资源的可打印离线报告，动态值全部转义，错误明细限前 100 条，时间标注 Asia/Shanghai。CSV 带 UTF-8 BOM，正确引用引号和换行，防止公式注入。JSON 保留 null、真实 0、seed、全部阶段、全部请求和重新计算的报告。

## 接口

所有 API 需要先访问工具页面获取浏览器会话，并携带该页面 `meta[name="rt-csrf"]` 的 `X-RT-CSRF`。

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| POST | `/api/benchmark/models` | 当前连接的有效、去重、排序模型 ID |
| POST | `/api/benchmark/start` | 201：创建后台批次并立即返回 runId 与初始状态 |
| POST | `/api/benchmark/stop` | 进入 stopping；可传 runId 防止停止已变更的批次 |
| GET | `/api/benchmark/state` | 当前状态、脱敏配置、进度、统计、报告、最近 50 条记录 |
| GET | `/api/benchmark/runs` | 最近 50 批历史，时间倒序 |
| GET | `/api/benchmark/runs/:id` | 完整批次与全部记录 |
| GET | `/api/benchmark/runs/:id/export?format=html\|csv\|json` | 下载报告 |
| GET | `/api/workspace` | 共享连接（仅 hasKey）、当前后台任务及数量 |
| POST / DELETE | `/api/workspace/connection` | 保存 / 清除共享连接 |

start 配置字段：`url`、`apiKey`、`protocol`、`models`、`concurrencyLevels`、`requestsPerStage`、`inputMinTokens`、`inputMaxTokens`、`modes`、`maxOutputTokens`、`timeoutSeconds`、`reasoningEffort`、`outputTokenField`。参数错误 400，冲突 409，不存在或不属于当前浏览器的批次 404，错误方法 405，过大请求 413，存储不可用 507。开始前必须从相同连接成功读取模型目录。

## 验证

测试只访问本地 mock，不使用真实账户或付费模型：

```bash
python -m unittest tests.test_benchmark -v
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_*.mjs
```

可选真实浏览器验收（需要本机 Playwright 和 Chrome）：

```bash
BENCHMARK_PYTHON=/absolute/path/to/python \
PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs \
BENCHMARK_CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
node tests/browser/benchmark.mjs
```

该脚本自行启动临时工具服务和真实 loopback HTTP mock，覆盖共享连接、计划计算、两种协议、刷新恢复、跨 Tab 后台执行、历史切换、部分停止、三种导出与离线 HTML、桌面/手机无溢出，并输出截图目录。也可以运行 `python -m tests.benchmark_browser_server` 手动验收；启动时打印页面和 mock 上游的临时地址，关闭后删除测试历史。

实现分别位于 `benchmark_config.py`（参数、URL、输入）、`benchmark_protocol.py`（模型发现、HTTP/SSE、usage）、`benchmark.py`（任务与历史）、`benchmark_stats.py`（统计、报告、导出）、`benchmark_routes.py`（接口）及 `templates/benchmark.html`、`static/benchmark*.mjs/css`（前端）。
