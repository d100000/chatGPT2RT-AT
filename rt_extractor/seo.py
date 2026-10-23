"""Public documentation facts and explicitly configured search metadata.

Nothing in this module derives a public URL from a request, credentials, or jobs.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree


REPOSITORY_URL = "https://github.com/d100000/chatGPT2RT-AT"
SITE_NAME = "bob ai工具"
PROJECT_DESCRIPTION = (
    "源码公开的 ChatGPT 已有授权账号工具集：提取 RT、AT 和 ID Token，"
    "转换 CPA 与 Sub2API 认证文件，并查看 IP 风险资料。"
)


def validate_public_url(value: str | None) -> str:
    """Accept only an explicit HTTPS origin, never a request-derived base URL."""
    if value is None or value == "":
        return ""
    message = "BOB_AI_PUBLIC_URL 必须为不含账号、路径、查询参数或片段的 HTTPS 站点地址"
    if (not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value)
            or "\\" in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or parsed.netloc.endswith(":")
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError(message)
        try:
            address = ipaddress.ip_address(host)
            normalized_host = f"[{address}]" if address.version == 6 else str(address)
        except ValueError:
            normalized_host = host.encode("idna").decode("ascii").lower()
            if (len(normalized_host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in normalized_host.split(".")
            )):
                raise ValueError(message)
        return "https://" + normalized_host + (f":{port}" if port not in {None, 443} else "")
    except (ValueError, UnicodeError):
        raise ValueError(message) from None


@dataclass(frozen=True)
class DocSection:
    anchor: str
    heading: str
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class DocPage:
    slug: str
    title: str
    label: str
    description: str
    heading: str
    answer: str
    sections: tuple[DocSection, ...]

    @property
    def path(self) -> str:
        return "/docs" + ("/" + self.slug if self.slug else "")


DOC_PAGES = (
    DocPage(
        "", "GPT Token 提取与 CPA / Sub2API 转换指南", "文档首页",
        "了解 bob ai工具的 GPT Token 提取、RT / AT / ID Token、CPA 与 Sub2API 转换、数据保存方式及使用边界。",
        "GPT Token 提取与格式转换指南",
        "bob ai工具面向你已获授权的现有 ChatGPT 账号，提供 RT、AT 和 ID Token 提取、CPA ↔ Sub2API 文件转换及 IP 风险资料查看。它是独立项目，不是 OpenAI 官方产品。",
        (
            DocSection("tools", "三个工具分别做什么？", bullets=(
                "Token 获取：登录已有授权账号，成功时将 RT、AT 和 ID Token 交给当前浏览器缓存，文件在浏览器生成。",
                "格式转换：在浏览器内转换已有认证 JSON，不需要重新登录，不上传导入的 Token。",
                "IP 检测：浏览器向 IPOK 查询出口及风险资料，结果仅供判断网络环境参考。",
            )),
            DocSection("choose", "从哪一篇开始？", paragraphs=(
                "首次使用可先阅读 Token 提取流程；需要理解凭据的区别时，查看 RT、AT、ID Token 与 API Key 说明；已有 JSON 时，直接查看 CPA 与 Sub2API 转换指南。",
            )),
            DocSection("privacy", "数据实际保存在哪里？", paragraphs=(
                "网页提取结果仅缓存在所属浏览器的会话存储中，有效期 30 分钟。服务器临时处理登录和结果交接，收到浏览器确认即清除临时结果，不写入 Token、账号历史或结果文件。密码和 2FA 密钥不写入浏览器缓存。",
                "手动导入的格式转换文件仅在当前页面内存中处理。使用声明确认时间另行保存；匿名 Cookie 用于任务归属和防刷，不包含账号或 Token，不采集硬件指纹。CLI 显式文件输出仍由使用者自行保管。",
            )),
            DocSection("scope", "使用边界", paragraphs=(
                "只处理你有权使用的已有账号，不提供自动注册、自动取邮箱验证码、生成 API Key 或自动续期 RT。来源接口、账号状态和额外验证会影响提取结果，格式转换成功也不代表凭据仍有效。",
            )),
        ),
    ),
    DocPage(
        "token-extraction", "如何提取 GPT Token：RT、AT 与 ID Token", "Token 提取",
        "了解已有授权 ChatGPT 账号提取 RT、AT 和 ID Token 的输入格式、批量并发、验证码、结果导出和保存位置。",
        "如何提取 GPT Token：RT、AT 与 ID Token",
        "在 Token 获取页输入你已获授权的账号邮箱、密码和 Authenticator 2FA 密钥，完成服务方要求的验证后，工具提取 RT、AT 与 ID Token，并自动生成 CPA 和 Sub2API 认证文件。",
        (
            DocSection("steps", "提取流程", bullets=(
                "每行一个账号，使用 邮箱----密码----2FA密钥 格式，或导入 UTF-8 .txt 文件。不要把凭据贴到反馈、搜索页面或公开截图中。",
                "每批最多 1000 个账号、2 MiB 账号文本；默认同时处理 2 个账号，可选择 1～3 个。",
                "按需配置运行服务设备使用的连接代理，然后开始提取。等待邮箱验证时，在对应账号下提交验证码。",
                "成功账号可查看 RT，下载原始 JSON、CPA JSON；批次结束后可导出 CPA ZIP、Sub2API JSON 或逐行 RT 文本。",
            )),
            DocSection("verification", "验证码与失败处理", paragraphs=(
                "2FA 密钥用于生成动态验证码，不能替代服务方另外要求的邮箱或其他验证。等待验证仍占一个并发名额，其他名额可以继续运行。",
                "默认单账号超时为 180 秒。单个账号失败不阻止其他账号继续；取消批次会停止领取新账号，并保留已成功结果。服务端或本站返回 429 时应遵循等待提示，不要连续重试。",
            )),
            DocSection("results", "怎样判断提取成功？", paragraphs=(
                "结果需要同时包含有效形式的 Access Token、Refresh Token 和 ID Token，工具才标记为成功。这个状态表示此次提取完整，不保证账号后续一直可用，也不代表凭据拥有任何额外权限。",
                "网页结果接收到当前浏览器的会话缓存，下载由浏览器生成，服务器不写结果文件。浏览器缓存有效期 30 分钟，未交接的已结束结果在服务器内存中最多保留 10 分钟；请及时下载并清理。服务重启不会恢复未交接结果或活动登录。",
            )),
            DocSection("limits", "这项功能不做什么？", paragraphs=(
                "它不会从 Session Token、API Key 或仅有 AT 的文件凭空生成缺失的 RT，不会自动注册账号，也不提供 RT 自动刷新。使用私有认证接口的兼容性可能变化。",
            )),
        ),
    ),
    DocPage(
        "token-types", "RT、AT、ID Token 和 API Key 有什么区别？", "Token 类型",
        "区分 Refresh Token、Access Token、ID Token 与 API Key，了解 bob ai工具会提取、转换哪些凭据，以及有效期和 claims 的含义。",
        "RT、AT、ID Token 和 API Key 的区别",
        "AT 用于访问被授权的资源，RT 用于向签发方换取新的访问凭据，ID Token 描述身份认证信息；API Key 是另一类调用凭据。它们用途不同，不能通过改字段名相互替代。",
        (
            DocSection("comparison", "四种凭据对照", columns=("名称", "用途", "本项目中的处理"), rows=(
                ("AT / Access Token", "访问授权范围内的资源", "提取并写入认证 JSON；有效期通常对应 AT"),
                ("RT / Refresh Token", "向签发方换取新的访问凭据", "提取、查看、导出；不提供自动续期功能"),
                ("ID Token", "传达身份认证信息和 claims", "提取并整理可映射的账号资料"),
                ("API Key", "服务方定义的另一种接口调用凭据", "不生成 API Key，不把 OAuth Token 转为 API Key"),
            )),
            DocSection("expiry", "expires_at 是 RT 的到期时间吗？", paragraphs=(
                "不是。提取结果中的 expires_at 表示 AT 的到期时间，不能据此推断 RT 的寿命。撤销、账号状态和服务方策略都可能影响凭据是否还能使用，最终由签发方判断。",
            )),
            DocSection("claims", "读取 JWT claims 等于验证 Token 吗？", paragraphs=(
                "不等于。转换器读取 JWT claims 只是为了补充邮箱、账号、套餐等可映射资料，不验证签名、认证状态或权限。解析成功、转换成功与服务方接受该凭据是不同的结果。",
            )),
            DocSection("handling", "如何保管这些内容？", paragraphs=(
                "Token、密码和 2FA 密钥都应按敏感凭据保管。不要提交到代码仓库、公开截图、搜索参数或讨论群；分享问题时只保留脱敏错误信息。",
            )),
        ),
    ),
    DocPage(
        "cpa-sub2api", "CPA ↔ Sub2API 格式转换说明", "CPA ↔ Sub2API",
        "在浏览器内将 CPA 与 Sub2API 认证 JSON 双向转换，了解支持的平台、字段映射、批量导出及转换不刷新 Token 的边界。",
        "CPA ↔ Sub2API 认证文件转换",
        "格式转换将已有认证 JSON 整理成另一种文件结构，在浏览器内处理，不上传导入的 Token，也不会登录账号、刷新 Token、延长有效期或创造缺失凭据。",
        (
            DocSection("workflow", "转换步骤", bullets=(
                "选择 CPA → Sub2API 或 Sub2API → CPA。",
                "粘贴 JSON，或拖入 JSON 文件，也可导入文件目录；支持 CPA 账号数组、连续 JSON 和 JSONL。",
                "查看逐条转换结果及跳过原因，再下载单账号 JSON、拆分 ZIP 或合并 Sub2API 文件。",
                "若刚完成提取，可选择“载入最近提取结果”；这一步只读取当前浏览器已接收的会话缓存，并替换当前转换列表。",
            )),
            DocSection("platforms", "支持哪些映射？", columns=("CPA 类型", "Sub2API 平台", "说明"), rows=(
                ("codex / 缺省", "openai", "OAuth Token 与可映射的账号资料"),
                ("claude", "anthropic", "可映射的 OAuth 认证字段"),
                ("antigravity", "antigravity", "可映射的 OAuth 与项目字段"),
                ("gemini", "gemini", "可映射的 OAuth 与项目字段"),
            )),
            DocSection("boundaries", "转换会完整保留所有设置吗？", paragraphs=(
                "不会。转换只导出已知映射字段，缺失的可选字段会省略。代理、调度配置和未知扩展字段不保证往返保留；不支持的账号类型会显示跳过原因。请先核对结果，再导入目标服务。",
                "Sub2API 合并文件使用 sub2api-data v1 结构。账号配置中的 concurrency 与本工具提取时选择的并发数是不同的设置，不表示本工具提高了账号服务配额。",
            )),
            DocSection("privacy", "导入的 Token 会被保存吗？", paragraphs=(
                "手动导入内容仅在当前页面内存中转换，不写入浏览器存储。网页提取结果另缓存在当前浏览器的会话存储中，30 分钟有效；服务器只临时处理登录与交接，不保存结果文件。下载文件由你自行保管。",
            )),
        ),
    ),
    DocPage(
        "faq", "GPT Token 提取与格式转换常见问题", "常见问题",
        "解答 GPT Token 提取失败、验证码、RT 有效期、CPA / Sub2API 转换、IP 风险评分、数据保存及部署限制等常见问题。",
        "GPT Token 与格式转换常见问题",
        "先区分问题发生在哪一步：按钮未启用通常是页面脚本或连接初始化问题；任务已启动后失败要查看脱敏错误；文件能转换并不代表其中的 Token 仍有效。",
        (
            DocSection("official", "bob ai工具是 OpenAI 官方产品吗？", paragraphs=(
                "不是。这是源码公开的独立工具，面向已有授权账号和认证文件，不代表 OpenAI 或其他平台。代码仓库提供实现与使用说明，服务方的验证和访问政策仍然适用。",
            )),
            DocSection("button", "输入了账号，为什么条数仍为 0、按钮不能点？", paragraphs=(
                "先检查浏览器控制台和网络请求。若输入监听没有运行，可能是脚本加载失败或 HTML 与静态文件版本不一致；只更新代码但未重启服务，可能仍在使用旧模板。完成安装与更新后应重启实际运行的应用进程，再刷新页面。不要通过反复提交账号来排查脚本问题。",
            )),
            DocSection("failed", "任务已开始，为什么获取失败？", paragraphs=(
                "账号信息、机器时间、代理连接、邮箱验证、服务方限制或认证接口变化都可能影响结果。查看当前账号的脱敏状态及错误，根据提示处理；HTTP 429 应等待，不自动重新提交密码或验证码。",
            )),
            DocSection("refresh", "能把 AT 转成 RT，或者让 RT 永久有效吗？", paragraphs=(
                "不能。格式转换只是字段映射，不会创造缺失的认证凭据、刷新 Token 或延长有效期。本项目也不提供 RT 自动续期，是否可用由签发方判断。",
            )),
            DocSection("stored", "线上提取结果会保存到我的浏览器吗？", paragraphs=(
                "会。新版网页结果仅缓存在所属浏览器的会话存储中，30 分钟有效。登录凭据仍需由服务器临时处理，浏览器确认接收后清除临时结果，服务器不写结果文件。手动导入的转换内容只在页面内存中处理，CLI 显式输出文件则另行保管。",
            )),
            DocSection("isolation", "为什么旧版会显示别人的任务？", paragraphs=(
                "旧版使用单人设计的全局任务和服务器输出目录恢复。新版改为签名浏览器会话的独立任务空间，所有读取和操作均校验归属；跨会话访问已知任务 ID 也返回 404。部署者必须更新并重启实际服务、清理代理缓存和旧文件；可能泄露的 Token 还需撤销或更换。",
            )),
            DocSection("ip", "IP 风险评分低，就一定能使用账号吗？", paragraphs=(
                "不能据此保证。IP 检测资料由 IPOK 提供，可能有缓存、额度或证据不足。账号、设备、行为及服务方策略都可能另外影响访问；风险评分不是认证结果或可用性承诺。",
            )),
            DocSection("deploy", "设置公开文档地址就能对公网开放工具吗？", paragraphs=(
                "不能。BOB_AI_PUBLIC_URL 只控制公开文档的 canonical、索引和分享元信息，不改变本机访问、Host、Origin、CSRF 或限流规则。当前已有匿名浏览器会话任务隔离，但没有公网用户登录或管理员体系；公开入口仍需配置可信 HTTPS、访问控制与代理限制。",
            )),
        ),
    ),
)
DOCS_BY_SLUG = {page.slug: page for page in DOC_PAGES}

TOOL_META = {
    "/": ("GPT Token 提取：RT / AT / ID Token", "登录已有授权 ChatGPT 账号，批量提取 RT、AT 和 ID Token，并在浏览器生成 CPA 与 Sub2API 认证文件。网页结果仅缓存在所属浏览器会话中。"),
    "/converter": ("CPA ↔ Sub2API 格式转换", "在浏览器内双向转换 CPA 与 Sub2API 认证 JSON，支持 Codex/OpenAI、Claude、Antigravity 和 Gemini；导入的 Token 不上传服务器。"),
    "/network": ("IP 纯净度检测", "通过 IPOK 查看浏览器出口 IP、地区、运营商、风险值及来源证据；IP 风险资料仅供参考，不保证账号可用性。"),
}


def metadata(public_url: str, path: str, *, page: DocPage | None = None) -> dict:
    title, description = (page.title, page.description) if page else TOOL_META[path]
    canonical = public_url + path if public_url else ""
    return {
        "title": f"{title} · {SITE_NAME}", "description": description,
        "robots": "index,follow" if page and public_url else "noindex,follow",
        "canonical": canonical, "site_name": SITE_NAME, "locale": "zh_CN",
        "image": public_url + "/static/social-preview.png" if public_url and page else "",
        "image_alt": "bob ai工具：RT、AT、ID Token 提取与 CPA / Sub2API 格式转换",
    }


def structured_data(public_url: str, page: DocPage) -> dict:
    website = public_url + "/docs" if public_url else None
    canonical = public_url + page.path if public_url else None
    source_id = REPOSITORY_URL + "#source"
    source = {
        "@type": "SoftwareSourceCode", "@id": source_id,
        "name": SITE_NAME, "description": PROJECT_DESCRIPTION,
        "codeRepository": REPOSITORY_URL, "programmingLanguage": ["Python", "JavaScript"],
        "runtimePlatform": "Python 3.10+", "inLanguage": "zh-CN",
    }
    site = {"@type": "WebSite", "name": SITE_NAME, "description": PROJECT_DESCRIPTION,
            "inLanguage": "zh-CN", "about": {"@id": source_id}}
    document = {"@type": "WebPage", "name": page.title, "description": page.description,
                "inLanguage": "zh-CN", "about": {"@id": source_id}}
    if website:
        site.update({"@id": website + "#website", "url": website})
        document.update({"@id": canonical + "#webpage", "url": canonical,
                         "isPartOf": {"@id": website + "#website"}})
    return {"@context": "https://schema.org", "@graph": [source, site, document]}


def robots_text(public_url: str) -> str:
    lines = ["User-agent: *", "Allow: /static/", "Disallow: /api/", "Disallow: /health"]
    if public_url:
        lines.extend(["Allow: /docs", "", "Sitemap: " + public_url + "/sitemap.xml"])
    else:
        lines.extend(["", "# Indexing is disabled until BOB_AI_PUBLIC_URL is configured."])
    return "\n".join(lines) + "\n"


def sitemap_xml(public_url: str) -> bytes:
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ElementTree.register_namespace("", namespace)
    root = ElementTree.Element(f"{{{namespace}}}urlset")
    if public_url:
        for page in DOC_PAGES:
            node = ElementTree.SubElement(root, f"{{{namespace}}}url")
            ElementTree.SubElement(node, f"{{{namespace}}}loc").text = public_url + page.path
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def llms_text(public_url: str) -> str:
    lines = [f"# {SITE_NAME}", "", "> " + PROJECT_DESCRIPTION, "",
             "这是实验性公开文档索引，不保证任何搜索或 AI 系统收录、引用或排名。", ""]
    if public_url:
        lines.append("## 使用文档")
        lines.extend(f"- [{page.title}]({public_url}{page.path}): {page.description}" for page in DOC_PAGES)
    else:
        lines.append("公开文档索引尚未启用。维护者需先配置 BOB_AI_PUBLIC_URL 并重启服务。")
    return "\n".join(lines) + "\n"
