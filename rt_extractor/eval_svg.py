"""Extract without rewriting; reject unsafe SVG rather than sanitizing its art."""
from __future__ import annotations

import base64
import hashlib
import re
import struct
from xml.etree import ElementTree as ET
import zlib

import tinycss2

SVG_NS = "http://www.w3.org/2000/svg"
CHECKER_VERSION = "svg-image-v2"
TAGS = set("svg g path rect circle ellipse line polyline polygon text tspan textPath defs title desc metadata linearGradient radialGradient stop clipPath mask pattern marker symbol use filter feGaussianBlur feOffset feBlend feColorMatrix feComponentTransfer feFuncR feFuncG feFuncB feFuncA feMerge feMergeNode feFlood feComposite feMorphology feTurbulence feDisplacementMap feDiffuseLighting feSpecularLighting feDistantLight fePointLight feSpotLight feConvolveMatrix feTile style animate animateTransform animateMotion animateColor set mpath".split())
ANIMATIONS = {"animate", "animateTransform", "animateMotion", "animateColor", "set"}
# SMIL must never mutate href, style, event handlers, namespace/base URLs, or the
# configuration of another animation. Only graphical attributes are permitted.
ANIMATED_ATTRIBUTES = set("d points x y x1 x2 y1 y2 cx cy r rx ry width height dx dy rotate transform opacity fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit color stop-color stop-opacity flood-color flood-opacity lighting-color visibility display font-size font-weight letter-spacing word-spacing text-anchor startOffset pathLength viewBox preserveAspectRatio clip-path clip-rule mask filter gradientTransform gradientUnits offset patternTransform patternUnits patternContentUnits marker-start marker-mid marker-end".split())
CSS_FUNCTIONS = set("rgb rgba hsl hsla hwb lab lch oklab oklch color color-mix light-dark device-cmyk var env calc min max clamp round mod rem abs sign sqrt pow hypot log exp sin cos tan asin acos atan atan2 translate translatex translatey translatez translate3d scale scalex scaley scalez scale3d rotate rotatex rotatey rotatez rotate3d skew skewx skewy matrix matrix3d perspective cubic-bezier steps linear blur brightness contrast drop-shadow grayscale hue-rotate invert opacity saturate sepia linear-gradient radial-gradient conic-gradient repeating-linear-gradient repeating-radial-gradient repeating-conic-gradient path polygon circle ellipse inset rect xywh not is where has nth-child nth-last-child nth-of-type nth-last-of-type lang dir".split())


def local_reference(value):
    return bool(re.fullmatch(r"#[\w:.-]+", value))


def extract_svgs(text):
    # Ignore tag-like text in comments/CDATA. Only top-level complete SVGs count.
    tokens = re.finditer(r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|</?svg\b(?:[^\"'<>]|\"[^\"]*\"|'[^']*')*>", text)
    result, depth, start = [], 0, None
    for match in tokens:
        token = match.group()
        if token.startswith("<!"):
            continue
        if token.startswith("</"):
            if depth:
                depth -= 1
                if depth == 0:
                    result.append(text[start:match.end()])
        elif token.rstrip().endswith("/>"):
            if depth == 0:
                result.append(token)
        else:
            if depth == 0:
                start = match.start()
            depth += 1
    return result


def local_name(name):
    return name.rsplit("}", 1)[-1]


def safe_css_tokens(tokens, depth=0):
    """Inspect decoded CSS tokens, including escaped names and nested URLs."""
    if depth > 32:
        return False
    for token in tokens:
        if token.type in {"error", "at-keyword"}:
            return False
        if token.type == "url" and not local_reference(token.value):
            return False
        if token.type == "function":
            if token.lower_name == "url":
                args = [t for t in token.arguments if t.type not in {"whitespace", "comment"}]
                if len(args) != 1 or args[0].type != "string" or not local_reference(args[0].value):
                    return False
            elif token.lower_name not in CSS_FUNCTIONS or not safe_css_tokens(token.arguments, depth + 1):
                # image-set/image/src can fetch bare strings; expression/attr
                # and unknown future functions are deliberately not allowed.
                return False
        if hasattr(token, "content") and not safe_css_tokens(token.content, depth + 1):
            return False
    return True


def safe_declarations(tokens):
    for declaration in tinycss2.parse_blocks_contents(tokens, skip_comments=True, skip_whitespace=True):
        if (declaration.type != "declaration" or declaration.lower_name in {"behavior", "-moz-binding"}
                or not safe_css_tokens(declaration.value)):
            return False
    return True


def safe_rules(rules, depth=0, *, keyframes=False):
    if depth > 32:
        return False
    for rule in rules:
        if rule.type not in {"qualified-rule", "at-rule"} or not safe_css_tokens(rule.prelude):
            return False
        if rule.type == "qualified-rule":
            if not safe_declarations(rule.content):
                return False
        elif keyframes or rule.content is None or rule.lower_at_keyword not in {"media", "keyframes", "-webkit-keyframes"}:
            return False
        elif not safe_rules(tinycss2.parse_blocks_contents(rule.content, skip_comments=True, skip_whitespace=True),
                            depth + 1, keyframes=rule.lower_at_keyword != "media"):
            return False
    return True


def safe_css(value, *, context="value"):
    try:
        if context == "stylesheet":
            return safe_rules(tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True))
        if context == "declarations":
            return safe_declarations(value)
        return safe_css_tokens(tinycss2.parse_component_value_list(value))
    except (RecursionError, ValueError):
        return False


def inspect_svg(raw):
    values = extract_svgs(raw)
    svg = values[0] if values else None
    check = {"complete_svg": bool(values), "svg_count": len(values), "structure_valid": False,
             "safe": False, "browser_rendered": None, "issues": [],
             "extraction_rule": "first-complete-top-level-svg-v1", "checker_version": CHECKER_VERSION}
    if not svg:
        check["issues"].append("未找到完整 SVG")
        return None, check
    if re.search(r"<!DOCTYPE|<!ENTITY|<\?", raw, re.I):
        # XML declarations surrounding an SVG are harmless; other processing
        # instructions and DTDs are not. Do not parse entities at all.
        rest = re.sub(r"<\?xml\s[^?]*\?>", "", raw, flags=re.I)
        if re.search(r"<!DOCTYPE|<!ENTITY|<\?", rest, re.I):
            check["issues"].append("禁止 DTD、实体或处理指令")
            return svg, check
    try:
        root = ET.fromstring(svg)
        if root.tag not in {"svg", f"{{{SVG_NS}}}svg"}:
            raise ValueError("根元素不是 SVG")
        check["structure_valid"] = True
    except (ET.ParseError, ValueError) as error:
        check["issues"].append("SVG XML 无效：" + str(error))
        return svg, check
    for node in root.iter():
        name = local_name(node.tag)
        if name not in TAGS or (node.tag.startswith("{") and not node.tag.startswith("{" + SVG_NS + "}")):
            check["issues"].append("禁止元素：" + name)
        if name == "style" and not safe_css(node.text or "", context="stylesheet"):
            check["issues"].append("样式含外部资源、不支持的规则或无效 CSS")
        if name in ANIMATIONS:
            target = node.get("attributeName", "")
            if name == "animateMotion":
                valid_target = not target
            elif name == "animateTransform":
                valid_target = target == "transform"
            else:
                valid_target = target in ANIMATED_ATTRIBUTES
            if not valid_target:
                check["issues"].append("禁止动画修改属性：" + (target or "未指定"))
        for attr, value in node.attrib.items():
            attr_name = local_name(attr).lower()
            if attr_name.startswith("on") or attr_name in {"src", "base"}:
                check["issues"].append("禁止属性：" + attr_name)
            if attr.startswith("{") and not attr.startswith(("{http://www.w3.org/1999/xlink}", "{http://www.w3.org/XML/1998/namespace}")):
                check["issues"].append("禁止属性命名空间")
            if attr_name == "href" and not local_reference(value):
                check["issues"].append("禁止外部资源引用")
            if not safe_css(value, context="declarations" if attr_name == "style" else "value"):
                check["issues"].append("属性含外部资源、不支持的表达式或无效 CSS")
    check["issues"] = list(dict.fromkeys(check["issues"]))
    check["safe"] = not check["issues"]
    return svg, check


def svg_hash(svg):
    return hashlib.sha256(svg.encode()).hexdigest()


def validate_png(value):
    """Validate a bounded, browser-produced PNG before retaining review evidence."""
    if not isinstance(value, str) or not value.startswith("data:image/png;base64,") or len(value) > 2_700_000:
        raise ValueError("需要不超过 2 MiB 的浏览器渲染 PNG")
    try:
        data = base64.b64decode(value.split(",", 1)[1], validate=True)
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError()
        offset, width, height, ended, compressed = 8, 0, 0, False, bytearray()
        while offset < len(data):
            size = struct.unpack(">I", data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            content = data[offset + 8:offset + 8 + size]
            checksum = struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
            if zlib.crc32(kind + content) & 0xffffffff != checksum:
                raise ValueError()
            if kind == b"IHDR":
                width, height = struct.unpack(">II", content[:8])
                if not 1 <= width <= 1600 or not 1 <= height <= 1600:
                    raise ValueError()
            if kind == b"IDAT":
                compressed.extend(content)
            offset += size + 12
            if kind == b"IEND":
                ended = True
                break
        if not ended or offset != len(data) or not width or not compressed:
            raise ValueError()
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, 24 * 1024 * 1024)
        if not decoder.eof or len(decoded) > 24 * 1024 * 1024:
            raise ValueError()
    except (ValueError, struct.error, zlib.error):
        raise ValueError("PNG 渲染证据无效") from None
    return {"data_url": value, "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height}
