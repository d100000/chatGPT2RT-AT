# 网页安全策略

本工具当前使用单进程任务管理，网页仅接受本机访问。防刷在服务端执行，关闭浏览器 JavaScript、修改按钮状态或直接用脚本请求都不能绕过服务端计数。

## 多人任务隔离与结果交接

网页按服务器验证过的 `bob_ai_session` 建立独立任务空间。任务状态、结果交接、验证码、取消、清理以及已退役的下载路径均先检查归属；其他会话访问已知任务 ID 与访问不存在的 ID 一样返回 404。没有有效签名会话的任务 API 返回 403，IP 地址只用于限流，不能充当任务所有者。CSRF 值绑定当前会话，不能跨会话复用。

网页不创建输出目录，不写入 Token、账号历史、批次状态或 ZIP 文件，也不读取旧版输出文件。服务器仅在登录与交接期间临时使用内存：浏览器缓存成功后确认接收并清除服务器临时结果，未确认的已结束任务固定 10 分钟过期，轮询不会延长该期限；活动任务闲置 10 分钟后取消清理。不同会话可同时运行任务，同时受全局资源和防刷限制。

结果缓存在按签名会话分区的浏览器 `localStorage` 中，有效期 30 分钟，下载在浏览器生成。同一浏览器标签页共享结果，Web Locks 协调接收、缓存、ACK 与清理，避免一个标签先 ACK 后另一个标签拿不到结果。共享能力不可用时明确提示降级范围，不让没有本页任务的标签抢先清除服务器结果。清除结果会同时清理当前会话的服务端临时任务，其他会话不受影响。

浏览器过期记录在页面运行或下次读取时清除，关闭所有标签不会触发浏览器存储的定时物理擦除。浏览器存储并未加密，页面脚本可读取；应保持 CSP、依赖和同源脚本安全，使用后主动清理。浏览器会话不是人员账号，同一浏览器配置中的多人不能视为隔离用户。

工具 HTML 和 API 响应（含错误）设置 `Cache-Control: private, no-store, max-age=0` 及 `Vary: Cookie`。反向代理与 CDN 必须对工具页和 `/api/` 禁用共享缓存，不得覆盖这些响应头。旧 RT / 文件下载接口不再读取磁盘，所属会话访问返回 410，其他会话返回 404。依据参见 [OWASP IDOR 防护](https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html)与[会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。

从旧版升级需要更新并重启实际应用、清理入口缓存和检查遗留输出文件，见[隐私修复迁移步骤](docs/MAINTAINERS.md#privacy-migration)。旧文件不会自动删除；曾暴露的 Token 需要撤销或更换。CLI 显式输出文件不在网页无落盘模式的范围内。

## 默认限制与计费

完整默认额度见 [README 的本站请求防护](README.md#本站请求防护)，定义位于 `rt_extractor/abuse.py` 的 `AbusePolicy`、`ClientPolicy`。每个请求同时检查 IP、设备、Session 和全局额度，三层客户端额度在一次加锁操作中原子检查，任意维度超限不会部分扣除其他维度的本次配额。规则按固定窗口计数，窗口从第一次请求开始；拒绝请求不刷新窗口。多条规则同时超限时，返回其中最长的等待时间。

- 页面、状态查询、未知路径共享动态请求额度。静态资源使用单独额度，不存在的静态文件同样计数；随机更换路径无法获得新的额度。
- 单账号和批量提交共享额度。同一邮箱还受跨来源的 5 次 / 15 分钟限制，批量任务按账号数扣除小时额度，不能通过一次请求塞入大量账号来绕过。
- 账号额度在任务成功进入队列时一次性扣除，包含后续失败、取消和尚未运行的账号；取消不退额度。已有活动任务造成的冲突、任务启动异常不扣账号额度。批次账号配额原子检查，任一账号超限时整批不启动、不部分扣费。
- 验证码按来源、全局和已知任务共同计数，无论验证码是否正确。随机任务 ID 仍消耗统一的验证码额度，不为不存在的任务创建新计数器。
- 服务端结果交接共享读取 / 导出额度。全进程最多同时处理 2 个结果响应，结束、异常或客户端关闭响应时释放名额。浏览器已缓存结果的 RT 查看、文件下载和转换无需再次读取服务器。
- 取消有独立额度，普通状态轮询、导出或提交超限不会消耗取消额度。Host、Origin、CSRF 错误单独记录；错误请求过多时返回 429，正确的访问校验不会因此被封禁。

限流返回示例：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 42
Content-Type: application/json

{"error":"任务提交过于频繁，请稍后重试。","retry_after":42}
```

前端按服务器给出的时间暂停相关操作，显示倒计时并保留输入。它不会自动重新提交密码或验证码；轮询 429 也不会显示为“服务离线”。刷新页面或重写客户端只会清除前端提示，无法清除服务端额度。

## IP、设备和 Session

IP 取自服务实际连接地址，不接受客户端声明的转发头。设备和 Session 使用服务器签名的随机匿名标识，不读取硬件信息、Canvas、字体或 User-Agent 来拼接指纹。

| Cookie | 用途 | 有效期 |
| --- | --- | --- |
| `bob_ai_device` | 在同一浏览器配置中聚合不同 Session 的频率 | 最长 30 天 |
| `bob_ai_session` | 识别任务所有者并聚合当前会话请求，多个标签页共享 | 浏览器会话 Cookie，服务器最长接受 12 小时 |

两枚 Cookie 只含随机标识及签名时间，会话标识另绑定对应设备；不含邮箱、密码、2FA 或 Token。标识由 `secrets` 生成，使用独立签名用途和 SHA-256，通过 ItsDangerous 验证签名与有效期。客户端自行指定的 ID、修改内容、过期或设备绑定不匹配的值都不生效；单枚输入长度限制为 256 个字符。

仅通过本机、Host、Origin 校验的三个工具页面的成功 GET 响应会签发缺失标识；API、静态资源、健康检查、HEAD 和错误响应不会签发。Cookie 设为 `HttpOnly`、`SameSite=Lax`、`Path=/`，不设置 `Domain`；HTTPS 请求另加 `Secure`。有效标识不滚动续期。属性选择参考 [Flask Cookie 安全说明](https://flask.palletsprojects.com/en/stable/web-security/)和 [OWASP 会话管理建议](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。

首次页面请求仍使用 IP 的固定匿名额度计数，通过检查后才签发新标识。没有 Cookie、禁用 Cookie 或提供无效 Cookie 的请求使用按 IP 派生的固定匿名额度计数，但任务 API 会拒绝访问，不为匿名来源创建任务空间。随机伪造 Cookie 不会创建新计数键。新建身份受每 IP 20 次 / 10 分钟、全局 60 次 / 10 分钟限制，反复清空后访问页面不能无限签发身份。

有效设备和会话标识在 IP 改变后仍共用原额度；换 Session 仍受设备额度限制，换浏览器、隐私窗口或清除所有 Cookie 仍受 IP、账号及全局总额度限制。“设备”是浏览器配置的近似识别，不代表不可伪造的物理设备身份。会话 Cookie 是访问所属临时任务的凭证，泄露或复制它会破坏这层隔离，必须通过 HTTPS 与安全浏览器环境保护；它不授予管理员权限，也不能阻止分布式攻击。

签名密钥与计数器只保存在当前进程内，服务重启会提前使旧 Cookie 失效并重置限流计数；用户重新打开页面后取得新标识。30 天、12 小时均为进程持续运行时的最长有效期。浏览器可能通过会话恢复保留会话 Cookie，因此服务器仍独立校验 12 小时有效期。

## 输入与资源边界

| 项目 | 上限 |
| --- | --- |
| HTTP 请求正文（包括批量 JSON） | 3 MiB |
| 单账号提交正文 | 16 KiB |
| 验证码正文 | 4 KiB |
| 取消请求正文 | 1 KiB |
| 批量账号文本 | 2 MiB，最多 1000 个账号 |
| 邮箱 / 密码 | 320 / 4096 个字符 |
| 2FA 密钥 / 代理地址 | 4096 / 2048 个字符 |
| CSRF 头 / 验证挑战 ID | 128 / 128 个字符 |
| 表单非文件字段 / multipart 部分数 | 16 KiB / 10 个 |
| 活跃限流计数器 | 10000 个 |

正文过大返回 413，无效输入返回 400。限流状态有容量上限，过期后回收；容量满时拒绝新增计数键，不淘汰仍有效的限制，避免随机更换账号挤掉原有处罚。客户端地址、浏览器标识和规范化邮箱以每次启动生成的随机密钥计算 HMAC，限流内存只保存指纹、计数及过期时间，不保存输入密码、2FA 或 Token 正文。

网页模式只暂存内存并交接到浏览器，不写结果文件。CLI `--output`、Python `extract_and_save` / `save_tokens` 仍按调用方显式要求保存文件，详见 [数据保存与运行限制](README.md#数据保存与运行限制)。

## 调整策略

可在调用应用工厂时传入明确的策略覆盖项，其余默认限制仍然生效。例如收紧每来源的提交次数及同一账号尝试次数：

```python
from dataclasses import replace
from rt_extractor.abuse import AbusePolicy, Quota
from rt_extractor.web import create_app

defaults = AbusePolicy()
app = create_app(
    abuse_policy=AbusePolicy(
        submissions=Quota(6, 60),
        account_attempts=Quota(3, 15 * 60),
        device=replace(defaults.device, submissions=Quota(5, 60)),
        session=replace(defaults.session, submissions=Quota(4, 60)),
    ),
)
```

`Quota(次数, 秒数)` 必须为正值。CLI 启动使用默认策略。不要通过关闭来源校验、取消 CSRF 或信任任意 `X-Forwarded-For` 来调整配额。

## 服务器使用边界

当前服务继续绑定 `127.0.0.1`，并验证实际连接地址、Host、Origin 和 CSRF；已有匿名浏览器会话的任务隔离，但没有公网用户登录或管理员权限体系。在服务器上供自己使用时，可保留这一模式，通过 SSH 隧道访问：

```bash
ssh -N -L 8765:127.0.0.1:8765 your-user@your-server
```

服务器上将网页服务运行在 8765 端口，本机浏览器打开 `http://127.0.0.1:8765`。当前限制使用直接连接地址，忽略请求自带的 `X-Forwarded-For`、`Forwarded` 等头；隧道或同机代理后的访问会共享来源额度。

应用层限流只能在请求到达应用后拒绝工作，不能挡住连接耗尽、慢速发送或带宽攻击。公开部署前还需配置访问控制及可信入口的 HTTPS、请求速率与并发限制、正文大小和读写超时；不能只去掉本机校验就对公网开放。入口须透传安全 Cookie 与会话 CSRF，并禁止缓存私有工具页和接口。Nginx 的速率与连接限制应显式设置拒绝状态为 429，并给出可用的重试提示。参考 [Nginx 请求限流](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)、[Nginx 连接限制](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html)和 [Flask 资源限制](https://flask.palletsprojects.com/en/stable/web-security/)。

当前计数保存在单个进程的内存里，重启后清空。多进程或多副本会各自计数，而且任务状态也不共享；不能通过直接增加 worker 获得统一防护。需要扩展部署时，应同时迁移任务状态及限流状态到可信共享服务。

IP 检测由浏览器直接调用 IPOK，遵循 IPOK 的配额和页面已有的冷却逻辑；本站服务端限制不改变第三方配额。已有文件的格式转换和载入本浏览器最近结果均在浏览器内执行。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
```

防护测试使用假时钟、合成账号与本地模拟，不发起真实登录。覆盖原子计费、窗口恢复、随机键和伪造转发头、单 / 批次共享限制、验证码、导出流关闭及异常释放、独立取消、正文与字符边界，以及前端 429 等待解析和转换输入保留。
