"""Sentinel VM runner — Python + Node.js 双引擎。

Executes turnstile, collector, and snapshot VMs with browser-like mocks
to generate sentinel tokens for OpenAI registration.

优先使用 pure-Python VM；当 Python VM 无法产出 turnstile token 时，
自动降级到 Node.js sentinel_vm.js（与 wuzu-image 对齐）。
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import random
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel version + SDK URL (must stay in sync with sentinel.py)
SENTINEL_VERSION = "20260219f9f6"
SENTINEL_CHALLENGE_SDK_URL = (
    f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
)

# ─── atob / btoa ──────────────────────────────────────────────────
def _atob(s: str) -> str:
    return base64.b64decode(s).decode("latin-1")


def _btoa(s: str) -> str:
    return base64.b64encode(s.encode("latin-1")).decode("ascii")


# ─── Browser Mocks ────────────────────────────────────────────────
_SENTINEL = object()


class DeepProxy:
    """Mimics JS Proxy — returns None for unknown attributes, supports item access."""

    def __init__(self, data: dict | None = None):
        object.__setattr__(self, "_data", dict(data or {}))

    def __getattr__(self, name: str) -> Any:
        d = object.__getattribute__(self, "_data")
        return d.get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_data")[name] = value

    def __getitem__(self, key: Any) -> Any:
        d = object.__getattribute__(self, "_data")
        return d.get(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        object.__getattribute__(self, "_data")[key] = value

    def __contains__(self, item: Any) -> bool:
        return item in object.__getattribute__(self, "_data")

    def __str__(self) -> str:
        return "[object Object]"

    def __repr__(self) -> str:
        return "[object Object]"

    def __int__(self) -> int:
        return 0

    def __float__(self) -> float:
        return 0.0

    def __bool__(self) -> bool:
        return True

    def __len__(self) -> int:
        d = object.__getattribute__(self, "_data")
        return d.get("length", 0)

    def __iter__(self):
        d = object.__getattribute__(self, "_data")
        length = d.get("length", 0)
        if isinstance(length, int) and length > 0:
            for i in range(length):
                yield d.get(i)

    def _get(self, key: Any, default: Any = None) -> Any:
        d = object.__getattribute__(self, "_data")
        return d.get(key, default)

    def _set(self, key: Any, value: Any) -> None:
        object.__getattribute__(self, "_data")[key] = value

    def _keys(self) -> list:
        return list(object.__getattribute__(self, "_data").keys())


def _dp(data: dict | None = None) -> DeepProxy:
    return DeepProxy(data)


# ─── Build mock browser environment ──────────────────────────────
def _build_mocks(device_id: str = "", user_agent: str = "") -> dict[str, Any]:
    perf_origin = time.time() * 1000 - random.random() * 100000
    browser_user_agent = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    app_version = browser_user_agent.removeprefix("Mozilla/")

    mock_performance = _dp({
        "now": lambda: time.time() * 1000 - perf_origin,
        "timeOrigin": perf_origin,
        "memory": _dp({"jsHeapSizeLimit": 4294705152}),
        "mark": lambda *a: None,
        "measure": lambda *a: None,
        "getEntriesByType": lambda *a: [],
        "getEntriesByName": lambda *a: [],
    })
    mock_screen = _dp({
        "width": 1920, "height": 1080, "availWidth": 1920, "availHeight": 1040,
        "colorDepth": 24, "pixelDepth": 24,
    })
    mock_navigator = _dp({
        "userAgent": browser_user_agent,
        "language": "en-US",
        "languages": ["en-US", "en"],
        "hardwareConcurrency": 8,
        "vendorSub": "",
        "vendor": "Google Inc.",
        "appVersion": app_version,
        "appName": "Netscape",
        "platform": "Win32",
        "product": "Gecko",
        "productSub": "20030107",
        "cookieEnabled": True,
        "onLine": True,
        "maxTouchPoints": 0,
        "doNotTrack": None,
        "plugins": _dp({
            "length": 5,
            0: _dp({"name": "PDF Viewer"}), 1: _dp({"name": "Chrome PDF Viewer"}),
            2: _dp({"name": "Chromium PDF Viewer"}), 3: _dp({"name": "Microsoft Edge PDF Viewer"}),
            4: _dp({"name": "WebKit built-in PDF"}),
        }),
        "mimeTypes": _dp({"length": 2, 0: _dp({"type": "application/pdf"}), 1: _dp({"type": "text/pdf"})}),
        "webdriver": False,
        "connection": _dp({"effectiveType": "4g", "rtt": 50, "downlink": 10, "saveData": False}),
        "getGamepads": lambda: [],
        "javaEnabled": lambda: False,
        "sendBeacon": lambda *a: True,
        "clipboard": _dp(), "credentials": _dp(), "locks": _dp(),
        "mediaSession": _dp(), "serviceWorker": _dp(), "storage": _dp(),
        "usb": _dp(),
        "userActivation": _dp({"hasBeenActive": True, "isActive": False}),
    })
    mock_location = _dp({
        "href": "https://auth.openai.com/about-you",
        "search": "", "origin": "https://auth.openai.com", "protocol": "https:",
        "host": "auth.openai.com", "hostname": "auth.openai.com", "port": "",
        "pathname": "/about-you", "hash": "",
    })

    scripts = [
        _dp({"src": SENTINEL_CHALLENGE_SDK_URL}),
        _dp({"src": "https://auth-cdn.oaistatic.com/assets/manifest-ef4d6d9d.js"}),
    ]
    mock_document = _dp({
        "scripts": scripts,
        "cookie": f"oai-did={device_id}" if device_id else "",
        "documentElement": _dp({"getAttribute": lambda n: None, "style": _dp(), "classList": _dp({"add": lambda *a: None, "remove": lambda *a: None, "contains": lambda *a: False})}),
        "head": _dp(), "body": _dp(),
        "createElement": lambda tag: _dp({"tagName": tag, "style": {}, "src": "", "appendChild": lambda *a: None}),
        "createTextNode": lambda text: _dp({"textContent": text}),
        "getElementById": lambda *a: None,
        "querySelector": lambda *a: None,
        "querySelectorAll": lambda *a: [],
        "getElementsByTagName": lambda tag: scripts if tag == "script" else [],
        "title": "", "readyState": "complete", "visibilityState": "visible",
        "hidden": False, "hasFocus": lambda: True,
        "referrer": "https://auth.openai.com/about-you",
        "URL": "https://auth.openai.com/about-you",
        "domain": "auth.openai.com", "characterSet": "UTF-8", "contentType": "text/html",
        "currentScript": _dp({"src": SENTINEL_CHALLENGE_SDK_URL}),
    })

    window = {
        "performance": mock_performance, "screen": mock_screen,
        "navigator": mock_navigator, "location": mock_location,
        "document": mock_document, "Math": math, "Date": _JSDate,
        "atob": _atob, "btoa": _btoa,
        "parseInt": _js_parseInt, "parseFloat": _js_parseFloat,
        "isNaN": _js_isNaN, "isFinite": _js_isFinite,
        "encodeURIComponent": _js_encodeURIComponent,
        "decodeURIComponent": _js_decodeURIComponent,
        "encodeURI": _js_encodeURIComponent,
        "decodeURI": _js_decodeURIComponent,
        "escape": _js_encodeURIComponent,
        "unescape": _js_decodeURIComponent,
        "JSON": json, "Object": _JSObject, "Array": _JSArray,
        "Number": _JSNumber, "String": str, "Boolean": bool,
        "RegExp": _JSRegExp, "Error": _JSError, "TypeError": _JSError,
        "RangeError": _JSError, "SyntaxError": _JSError, "ReferenceError": _JSError,
        "Promise": _JSPromise, "Map": dict, "Set": set,
        "Function": _JSFunction,
        "Proxy": _JSProxyClass, "Symbol": _JSSymbol, "Reflect": _JSReflect,
        "console": _dp({"log": lambda *a: None, "warn": lambda *a: None, "error": lambda *a: None}),
        "undefined": None, "NaN": float("nan"), "Infinity": float("inf"),
        "cache": {}, "caches": _dp(),
        "indexedDB": _dp(),
        "localStorage": _dp({"getItem": lambda k: None, "setItem": lambda k, v: None, "removeItem": lambda k: None}),
        "sessionStorage": _dp({"getItem": lambda k: None, "setItem": lambda k, v: None, "removeItem": lambda k: None}),
        "history": _dp({"length": 1, "pushState": lambda *a: None, "replaceState": lambda *a: None}),
        "innerWidth": 1920, "innerHeight": 937, "outerWidth": 1920, "outerHeight": 1040,
        "devicePixelRatio": 1, "scrollX": 0, "scrollY": 0,
        "pageXOffset": 0, "pageYOffset": 0, "screenX": 0, "screenY": 0,
        "screenLeft": 0, "screenTop": 0,
        "visualViewport": _dp({"width": 1920, "height": 937, "offsetLeft": 0, "offsetTop": 0, "scale": 1}),
        "getComputedStyle": lambda *a: _dp({"getPropertyValue": lambda *a: ""}),
        "matchMedia": lambda *a: _dp({"matches": False, "media": "", "addEventListener": lambda *a: None, "removeEventListener": lambda *a: None}),
        "open": lambda *a: None, "close": lambda *a: None, "focus": lambda *a: None,
        "blur": lambda *a: None, "alert": lambda *a: None, "confirm": lambda *a: True,
        "prompt": lambda *a: "", "print": lambda *a: None,
        "addEventListener": lambda *a: None, "removeEventListener": lambda *a: None,
        "dispatchEvent": lambda *a: True, "postMessage": lambda *a: None,
        "requestAnimationFrame": lambda cb: None, "cancelAnimationFrame": lambda *a: None,
        "requestIdleCallback": lambda cb, *a: None,
        "setTimeout": lambda *a: None, "clearTimeout": lambda *a: None,
        "setInterval": lambda *a: None, "clearInterval": lambda *a: None,
        "getSelection": lambda: _dp({"toString": lambda: "", "rangeCount": 0}),
        "length": 0,
        "fetch": lambda *a, **kw: _dp({"ok": True, "status": 200}),
        "XMLHttpRequest": _dp(),
    }
    window["top"] = window
    window["self"] = window
    window["window"] = window
    window["globalThis"] = window
    window["parent"] = window
    window["frames"] = window
    return window


# ─── JS built-in stubs ───────────────────────────────────────────
class _JSDate:
    @staticmethod
    def now() -> float:
        return time.time() * 1000

    def __init__(self, *args: Any):
        self._ts = time.time() * 1000

    def getTime(self) -> float:
        return self._ts

    def toISOString(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self._ts / 1000))


def _js_parseInt(s: Any, radix: int = 10) -> int | float:
    try:
        return int(str(s).strip(), radix)
    except (ValueError, TypeError):
        return float("nan")


def _js_parseFloat(s: Any) -> float:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return float("nan")


def _js_isNaN(v: Any) -> bool:
    try:
        return math.isnan(float(v))
    except (ValueError, TypeError):
        return True


def _js_isFinite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (ValueError, TypeError):
        return False


def _js_encodeURIComponent(s: Any) -> str:
    from urllib.parse import quote
    return quote(str(s), safe="")


def _js_decodeURIComponent(s: Any) -> str:
    from urllib.parse import unquote
    return unquote(str(s))


class _JSObject:
    @staticmethod
    def keys(obj: Any) -> list:
        if isinstance(obj, dict):
            return list(obj.keys())
        if isinstance(obj, DeepProxy):
            return obj._keys()
        return []

    @staticmethod
    def values(obj: Any) -> list:
        if isinstance(obj, dict):
            return list(obj.values())
        return []

    @staticmethod
    def entries(obj: Any) -> list:
        if isinstance(obj, dict):
            return list(obj.items())
        return []

    @staticmethod
    def assign(target: Any, *sources: Any) -> Any:
        for src in sources:
            if isinstance(src, dict):
                if isinstance(target, dict):
                    target.update(src)
        return target

    @staticmethod
    def create(proto: Any) -> dict:
        return {}

    @staticmethod
    def defineProperty(obj: Any, prop: str, desc: Any) -> Any:
        return obj

    @staticmethod
    def getPrototypeOf(obj: Any) -> Any:
        return None

    @staticmethod
    def getOwnPropertyDescriptor(obj: Any, prop: str) -> Any:
        return None

    @staticmethod
    def freeze(obj: Any) -> Any:
        return obj


class _JSArray:
    @staticmethod
    def isArray(obj: Any) -> bool:
        return isinstance(obj, list)

    @staticmethod
    def from_(obj: Any) -> list:
        try:
            return list(obj)
        except TypeError:
            return []

    # Make Array.from work when accessed as window.Array.from
    # JS uses Array.from, Python attribute access
    def __init__(self):
        pass

    @staticmethod
    def of(*args: Any) -> list:
        return list(args)

# Allow Array.from to work via _prop_get dispatch
# (JS Array.from is handled in _prop_get for list/Array access)


class _JSNumber:
    @staticmethod
    def isInteger(v: Any) -> bool:
        return isinstance(v, int) or (isinstance(v, float) and v.is_integer())

    @staticmethod
    def isFinite(v: Any) -> bool:
        return _js_isFinite(v)

    @staticmethod
    def isNaN(v: Any) -> bool:
        return _js_isNaN(v)

    def __init__(self, v: Any = 0):
        try:
            self._v = float(v)
        except (ValueError, TypeError):
            self._v = float("nan")


class _JSRegExp:
    def __init__(self, pattern: str, flags: str = ""):
        self._pattern = pattern
        self._flags = 0
        if "i" in flags:
            self._flags |= 2  # re.IGNORECASE
        self._re = None

    def test(self, s: str) -> bool:
        import re
        return bool(re.search(self._pattern, str(s)))


class _JSError(Exception):
    pass


class _JSPromise:
    @staticmethod
    def resolve(v: Any = None) -> Any:
        return v

    @staticmethod
    def reject(v: Any = None) -> Any:
        raise _JSError(str(v))


class _JSFunction:
    pass


class _JSProxyClass:
    pass


class _JSSymbol:
    toPrimitive = "_Symbol_toPrimitive"
    iterator = "_Symbol_iterator"
    toStringTag = "_Symbol_toStringTag"


class _JSReflect:
    @staticmethod
    def get(target: Any, prop: Any) -> Any:
        return _prop_get(target, prop)

    @staticmethod
    def set(target: Any, prop: Any, value: Any) -> bool:
        _prop_set(target, prop, value)
        return True

    @staticmethod
    def has(target: Any, prop: Any) -> bool:
        return _prop_has(target, prop)

    @staticmethod
    def ownKeys(target: Any) -> list:
        if isinstance(target, dict):
            return list(target.keys())
        if isinstance(target, DeepProxy):
            return target._keys()
        return []


# ─── Property access helpers ─────────────────────────────────────
def _prop_get(obj: Any, prop: Any) -> Any:
    if obj is None:
        return None
    if isinstance(prop, str):
        # dict
        if isinstance(obj, dict):
            if prop in obj:
                return obj[prop]
            # Array-like methods on lists stored in dicts
            return None
        # list
        if isinstance(obj, list):
            if prop == "length":
                return len(obj)
            if prop == "push":
                return obj.append
            if prop == "pop":
                return obj.pop
            if prop == "shift":
                return lambda: obj.pop(0) if obj else None
            if prop == "indexOf":
                return lambda v: obj.index(v) if v in obj else -1
            if prop == "splice":
                return lambda i, c=1: [obj.pop(i) for _ in range(min(c, len(obj) - i))] if i < len(obj) else []
            if prop == "map":
                return lambda fn: [fn(v) for v in obj]
            if prop == "filter":
                return lambda fn: [v for v in obj if fn(v)]
            if prop == "join":
                return lambda sep=",": sep.join(str(v) for v in obj)
            if prop == "slice":
                return lambda *a: obj[slice(*a)]
            if prop == "concat":
                return lambda *a: obj + list(a[0]) if a and isinstance(a[0], list) else obj + list(a)
            if prop == "forEach":
                return lambda fn: [fn(v, i) for i, v in enumerate(obj)]
            if prop == "some":
                return lambda fn: any(fn(v) for v in obj)
            if prop == "every":
                return lambda fn: all(fn(v) for v in obj)
            if prop == "find":
                return lambda fn: next((v for v in obj if fn(v)), None)
            if prop == "includes":
                return lambda v: v in obj
            if prop == "sort":
                return lambda key=None: obj.sort(key=key) or obj
            if prop == "reverse":
                return lambda: obj.reverse() or obj
            try:
                idx = int(prop)
                return obj[idx] if 0 <= idx < len(obj) else None
            except (ValueError, TypeError):
                pass
            return None
        # str
        if isinstance(obj, str):
            if prop == "length":
                return len(obj)
            if prop == "match":
                import re as _re
                return lambda pattern: _re.findall(str(pattern), obj) if isinstance(pattern, str) else None
            if prop == "replace":
                return lambda old, new: obj.replace(str(old), str(new), 1)
            if prop == "split":
                return lambda sep=None: obj.split(sep)
            if prop == "trim":
                return lambda: obj.strip()
            if prop == "indexOf":
                return lambda s: obj.find(str(s))
            if prop == "substring":
                return lambda *a: obj[a[0]:a[1]] if len(a) > 1 else obj[a[0]:]
            if prop == "charAt":
                return lambda i: obj[i] if 0 <= i < len(obj) else ""
            if prop == "charCodeAt":
                return lambda i: ord(obj[i]) if 0 <= i < len(obj) else float("nan")
            if prop == "toLowerCase":
                return lambda: obj.lower()
            if prop == "toUpperCase":
                return lambda: obj.upper()
            if prop == "startsWith":
                return lambda s: obj.startswith(str(s))
            if prop == "endsWith":
                return lambda s: obj.endswith(str(s))
            if prop == "includes":
                return lambda s: str(s) in obj
            if prop == "slice":
                return lambda *a: obj[a[0]:a[1]] if len(a) > 1 else obj[a[0]:]
            if prop == "padStart":
                return lambda n, c=" ": obj.rjust(int(n), str(c))
            if prop == "repeat":
                return lambda n: obj * int(n)
            try:
                idx = int(prop)
                return obj[idx] if 0 <= idx < len(obj) else None
            except (ValueError, TypeError):
                pass
            return None
        # DeepProxy
        if isinstance(obj, DeepProxy):
            return obj._get(prop)
        # generic object attribute
        return getattr(obj, prop, None)
    # numeric index
    if isinstance(prop, (int, float)):
        idx = int(prop)
        if isinstance(obj, (list, str)):
            return obj[idx] if 0 <= idx < len(obj) else None
        if isinstance(obj, dict):
            return obj.get(idx, obj.get(str(idx)))
        if isinstance(obj, DeepProxy):
            return obj._get(idx)
    return None


def _prop_set(obj: Any, prop: Any, value: Any) -> None:
    if isinstance(obj, dict):
        obj[prop] = value
    elif isinstance(obj, DeepProxy):
        obj._set(prop, value)
    elif isinstance(obj, list) and isinstance(prop, (int, float)):
        idx = int(prop)
        while len(obj) <= idx:
            obj.append(None)
        obj[idx] = value
    elif obj is not None:
        try:
            setattr(obj, str(prop), value)
        except (AttributeError, TypeError):
            pass


def _prop_has(obj: Any, prop: Any) -> bool:
    if isinstance(obj, dict):
        return prop in obj
    if isinstance(obj, DeepProxy):
        return prop in obj
    if isinstance(obj, list):
        return isinstance(prop, (int, float)) and 0 <= int(prop) < len(obj)
    return hasattr(obj, str(prop)) if obj is not None else False


# ─── XOR decrypt ──────────────────────────────────────────────────
def xor_decrypt(ciphertext: str, key: str) -> str:
    if not key:
        return ciphertext
    result = []
    for i, ch in enumerate(ciphertext):
        result.append(chr(ord(ch) ^ ord(key[i % len(key)])))
    return "".join(result)


# ─── VM Implementation ───────────────────────────────────────────
class SentinelVM:
    """Register-based bytecode VM matching the Sentinel SDK protocol."""

    def __init__(self, window: dict[str, Any]):
        self.st: dict[Any, Any] = {}
        self.ct = 0
        self.window = window
        self._done = False
        self._result: str | None = None

    def _init_opcodes(self, key: str) -> None:
        self.st.clear()
        self.ct = 0

        vm = self

        # Opcode 0: recursive VM call
        def op0(t: Any) -> Any:
            return vm._run_bytecode(t)
        self.st[0] = op0

        # Opcode 1: XOR
        def op1(n: Any, e: Any) -> None:
            self.st[n] = xor_decrypt(str(self.st.get(n, "")), str(self.st.get(e, "")))
        self.st[1] = op1

        # Opcode 2: set value
        self.st[2] = lambda n, e: self.st.__setitem__(n, e)

        # Opcode 5: append
        def op5(n: Any, e: Any) -> None:
            o = self.st.get(n)
            v = self.st.get(e)
            if isinstance(o, list):
                o.append(v)
            else:
                self.st[n] = str(o if o is not None else "") + str(v if v is not None else "")
        self.st[5] = op5

        # Opcode 6: index access
        def op6(n: Any, e: Any, r: Any) -> None:
            obj = self.st.get(e)
            prop = self.st.get(r)
            self.st[n] = _prop_get(obj, prop) if obj is not None else None
        self.st[6] = op6

        # Opcode 7: function call (no result capture)
        def op7(n: Any, *e: Any) -> Any:
            fn = self.st.get(n)
            args = [self.st.get(k) for k in e]
            if callable(fn):
                return fn(*args)
        self.st[7] = op7

        # Opcode 8: copy
        self.st[8] = lambda n, e: self.st.__setitem__(n, self.st.get(e))

        # Opcode 9: instruction queue
        self.st[9] = []

        # Opcode 10: window
        self.st[10] = self.window

        # Opcode 11: script src matcher
        def op11(n: Any, e: Any) -> None:
            import re
            pattern = str(self.st.get(e, ""))
            scripts = self.window.get("document")
            script_list = scripts.scripts if isinstance(scripts, DeepProxy) else _prop_get(scripts, "scripts") or []
            if isinstance(script_list, DeepProxy):
                script_list = list(script_list)
            result = None
            for s in (script_list or []):
                src = _prop_get(s, "src") or ""
                if src:
                    m = re.search(pattern, str(src))
                    if m:
                        result = m.group(0)
                        break
            self.st[n] = result
        self.st[11] = op11

        # Opcode 12: self reference
        self.st[12] = lambda n: self.st.__setitem__(n, self.st)

        # Opcode 13: try-call with error capture
        def op13(n: Any, e: Any, *r: Any) -> None:
            try:
                fn = self.st.get(e)
                if callable(fn):
                    fn(*r)
            except Exception as t:
                self.st[n] = str(t)
        self.st[13] = op13

        # Opcode 14: JSON.parse
        def op14(n: Any, e: Any) -> None:
            self.st[n] = json.loads(str(self.st.get(e, "{}")))
        self.st[14] = op14

        # Opcode 15: JSON.stringify
        def op15(n: Any, e: Any) -> None:
            v = self.st.get(e)
            self.st[n] = json.dumps(v, separators=(",", ":"), default=str)
        self.st[15] = op15

        # Opcode 16: encryption key
        self.st[16] = key

        # Opcode 17: try-call with result + error capture
        def op17(n: Any, e: Any, *r: Any) -> None:
            try:
                fn = self.st.get(e)
                args = [self.st.get(k) for k in r]
                if callable(fn):
                    result = fn(*args)
                    self.st[n] = result
                else:
                    self.st[n] = None
            except Exception as t:
                self.st[n] = str(t)
        self.st[17] = op17

        # Opcode 18: atob
        def op18(n: Any) -> None:
            try:
                self.st[n] = _atob(str(self.st.get(n, "")))
            except Exception:
                self.st[n] = ""
        self.st[18] = op18

        # Opcode 19: btoa
        def op19(n: Any) -> None:
            try:
                self.st[n] = _btoa(str(self.st.get(n, "")))
            except Exception:
                self.st[n] = ""
        self.st[19] = op19

        # Opcode 20: if-equal call
        def op20(n: Any, e: Any, r: Any, *o: Any) -> Any:
            if self.st.get(n) == self.st.get(e):
                fn = self.st.get(r)
                if callable(fn):
                    return fn(*o)
            return None
        self.st[20] = op20

        # Opcode 21: if-diff call
        def op21(n: Any, e: Any, r: Any, o: Any, *i: Any) -> Any:
            try:
                a = float(self.st.get(n) or 0)
                b = float(self.st.get(e) or 0)
                threshold = float(self.st.get(r) or 0)
                if abs(a - b) > threshold:
                    fn = self.st.get(o)
                    if callable(fn):
                        return fn(*i)
            except (ValueError, TypeError):
                pass
            return None
        self.st[21] = op21

        # Opcode 22: sub-execute
        def op22(n: Any, e: Any) -> None:
            saved = list(self.st.get(9, []))
            self.st[9] = list(e) if isinstance(e, list) else []
            try:
                self._execute_instructions()
            except Exception as t:
                self.st[n] = str(t)
            finally:
                self.st[9] = saved
        self.st[22] = op22

        # Opcode 23: if-defined call
        def op23(n: Any, e: Any, *r: Any) -> Any:
            if self.st.get(n) is not None:
                fn = self.st.get(e)
                if callable(fn):
                    return fn(*r)
            return None
        self.st[23] = op23

        # Opcode 24: bind and call (tolerant)
        def op24(n: Any, e: Any, r: Any) -> None:
            obj = self.st.get(e)
            prop = self.st.get(r)
            method = _prop_get(obj, prop) if obj is not None else None
            if callable(method):
                self.st[n] = method
            else:
                self.st[n] = lambda *a: None
        self.st[24] = op24

        # Opcodes 25, 26, 28: noop
        self.st[25] = lambda *a: None
        self.st[26] = lambda *a: None
        self.st[28] = lambda *a: None

        # Opcode 27: remove
        def op27(n: Any, e: Any) -> None:
            o = self.st.get(n)
            v = self.st.get(e)
            if isinstance(o, list):
                try:
                    o.remove(v)
                except ValueError:
                    pass
            else:
                try:
                    self.st[n] = float(o or 0) - float(v or 0)
                except (ValueError, TypeError):
                    self.st[n] = 0
        self.st[27] = op27

        # Opcode 29: less-than
        def op29(n: Any, e: Any, r: Any) -> None:
            a = self.st.get(e)
            b = self.st.get(r)
            try:
                self.st[n] = float(a) < float(b)
            except (ValueError, TypeError):
                self.st[n] = str(a or "") < str(b or "")
        self.st[29] = op29

        # Opcode 30: function define
        def op30(t: Any, n: Any, e: Any, r: Any = None) -> None:
            is_arr = isinstance(r, list)
            params = e if is_arr else []
            body = (r if is_arr else e) or []

            def defined_fn(*args: Any) -> Any:
                if self._done:
                    return None
                saved = list(self.st.get(9, []))
                if is_arr and isinstance(params, list):
                    for i, p in enumerate(params):
                        self.st[p] = args[i] if i < len(args) else None
                self.st[9] = list(body)
                try:
                    self._execute_instructions()
                    return self.st.get(n)
                except Exception as ex:
                    return str(ex)
                finally:
                    self.st[9] = saved
            self.st[t] = defined_fn
        self.st[30] = op30

        # Opcode 33: multiply
        def op33(n: Any, e: Any, r: Any) -> None:
            try:
                self.st[n] = float(self.st.get(e) or 0) * float(self.st.get(r) or 0)
            except (ValueError, TypeError):
                self.st[n] = 0
        self.st[33] = op33

        # Opcode 34: await/resolve (synchronous in Python)
        def op34(n: Any, e: Any) -> None:
            self.st[n] = self.st.get(e)
        self.st[34] = op34

        # Opcode 35: divide
        def op35(n: Any, e: Any, r: Any) -> None:
            try:
                a = float(self.st.get(e) or 0)
                b = float(self.st.get(r) or 0)
                self.st[n] = 0 if b == 0 else a / b
            except (ValueError, TypeError):
                self.st[n] = 0
        self.st[35] = op35

    def _execute_instructions(self) -> None:
        queue = self.st.get(9, [])
        iterations = 0
        max_iterations = 500000
        while queue and iterations < max_iterations:
            if self._done:
                break
            instr = queue.pop(0)
            if not isinstance(instr, list) or not instr:
                continue
            opcode_key = instr[0]
            args = instr[1:]
            fn = self.st.get(opcode_key)
            if callable(fn):
                try:
                    fn(*args)
                except Exception:
                    pass
            self.ct += 1
            iterations += 1

    def _run_bytecode(self, bytecode_b64: Any, key: str | None = None) -> str | None:
        if key is not None:
            self._init_opcodes(key)

        self._done = False
        self._result = None

        # Set resolve/reject handlers
        def resolve_handler(t: Any) -> None:
            if not self._done:
                self._done = True
                self._result = _btoa(str(t))
        self.st[3] = resolve_handler

        def reject_handler(t: Any) -> None:
            if not self._done:
                self._done = True
                self._result = _btoa(str(t))
        self.st[4] = reject_handler

        try:
            decoded = _atob(str(bytecode_b64))
            decrypted = xor_decrypt(decoded, str(self.st.get(16, "")))
            instructions = json.loads(decrypted)
            self.st[9] = instructions if isinstance(instructions, list) else []
            self._execute_instructions()
        except Exception as t:
            if not self._done:
                self._done = True
                self._result = _btoa(f"{self.ct}: {t}")

        return self._result

    def run(self, bytecode_b64: str, key: str) -> str | None:
        return self._run_bytecode(bytecode_b64, key)

    def run_continue(self, bytecode_b64: str) -> str | None:
        """Run another bytecode on the same VM (reuses state, e.g. for snapshot after collector)."""
        self._done = False
        self._result = None

        def resolve_handler(t: Any) -> None:
            if not self._done:
                self._done = True
                self._result = _btoa(str(t))
        self.st[3] = resolve_handler

        def reject_handler(t: Any) -> None:
            if not self._done:
                self._done = True
                self._result = _btoa(str(t))
        self.st[4] = reject_handler

        # Re-install opcode 30 with done check
        def op30(t: Any, n: Any, e: Any, r: Any = None) -> None:
            is_arr = isinstance(r, list)
            params = e if is_arr else []
            body = (r if is_arr else e) or []

            def defined_fn(*args: Any) -> Any:
                if self._done:
                    return None
                saved = list(self.st.get(9, []))
                if is_arr and isinstance(params, list):
                    for i, p in enumerate(params):
                        self.st[p] = args[i] if i < len(args) else None
                self.st[9] = list(body)
                try:
                    self._execute_instructions()
                    return self.st.get(n)
                except Exception as ex:
                    return str(ex)
                finally:
                    self.st[9] = saved
            self.st[t] = defined_fn
        self.st[30] = op30

        try:
            decoded = _atob(str(bytecode_b64))
            decrypted = xor_decrypt(decoded, str(self.st.get(16, "")))
            instructions = json.loads(decrypted)
            self.st[9] = instructions if isinstance(instructions, list) else []
            self._execute_instructions()
        except Exception as t:
            if not self._done:
                self._done = True
                self._result = _btoa(f"{self.ct}: {t}")

        return self._result


class TurnstileVM:
    """Separate VM for turnstile with 500ms equivalent timeout (max 100k iterations)."""

    def __init__(self, window: dict[str, Any]):
        self.st: dict[Any, Any] = {}
        self.ct = 0
        self.window = window
        self._done = False
        self._result: str | None = None

    def _execute_instructions(self) -> None:
        queue = self.st.get(9, [])
        iterations = 0
        max_iterations = 200000  # ~500ms worth of work
        while queue and iterations < max_iterations:
            if self._done:
                break
            instr = queue.pop(0)
            if not isinstance(instr, list) or not instr:
                continue
            opcode_key = instr[0]
            args = instr[1:]
            fn = self.st.get(opcode_key)
            if callable(fn):
                try:
                    fn(*args)
                except Exception:
                    pass
            self.ct += 1
            iterations += 1

    def run(self, bytecode_b64: str, key: str) -> str | None:
        self.st.clear()
        self.ct = 0
        self._done = False
        self._result = None
        vm = self

        # Opcode 0: recursive VM call with sub-timeout
        def op0(t: Any) -> Any:
            saved_done = vm._done
            vm._done = False
            old_resolve = vm.st.get(3)
            old_reject = vm.st.get(4)

            def sub_resolve(v: Any) -> None:
                if not vm._done:
                    vm._done = True
                    vm._result = _btoa(str(v))
            def sub_reject(v: Any) -> None:
                if not vm._done:
                    vm._done = True

            vm.st[3] = sub_resolve
            vm.st[4] = sub_reject

            try:
                decoded = _atob(str(t))
                decrypted = xor_decrypt(decoded, str(vm.st.get(16, "")))
                instructions = json.loads(decrypted)
                vm.st[9] = instructions if isinstance(instructions, list) else []
                vm._execute_instructions()
            except Exception as e:
                if not vm._done:
                    vm._result = _btoa(f"{vm.ct}: {e}")
            finally:
                vm.st[3] = old_resolve
                vm.st[4] = old_reject

            return vm._result
        self.st[0] = op0

        self.st[1] = lambda n, e: self.st.__setitem__(n, xor_decrypt(str(self.st.get(n, "")), str(self.st.get(e, ""))))
        self.st[2] = lambda n, e: self.st.__setitem__(n, e)

        def op5(n: Any, e: Any) -> None:
            o = self.st.get(n)
            v = self.st.get(e)
            if isinstance(o, list):
                o.append(v)
            else:
                self.st[n] = str(o if o is not None else "") + str(v if v is not None else "")
        self.st[5] = op5

        self.st[6] = lambda n, e, r: self.st.__setitem__(n, _prop_get(self.st.get(e), self.st.get(r)) if self.st.get(e) is not None else None)
        self.st[7] = lambda n, *e: self.st.get(n)(*[self.st.get(k) for k in e]) if callable(self.st.get(n)) else None
        self.st[8] = lambda n, e: self.st.__setitem__(n, self.st.get(e))
        self.st[9] = []
        self.st[10] = self.window
        def op11(n: Any, e: Any) -> None:
            import re
            pattern = str(self.st.get(e, ""))
            doc = self.window.get("document")
            script_list = doc.scripts if isinstance(doc, DeepProxy) else _prop_get(doc, "scripts") or []
            result = None
            for s in (list(script_list) if isinstance(script_list, (list, DeepProxy)) else []):
                src = _prop_get(s, "src") or ""
                if src:
                    m = re.search(pattern, str(src))
                    if m:
                        result = m.group(0)
                        break
            self.st[n] = result
        self.st[11] = op11
        self.st[12] = lambda n: self.st.__setitem__(n, self.st)
        def op13(n: Any, e: Any, *r: Any) -> None:
            try:
                fn = self.st.get(e)
                if callable(fn):
                    fn(*r)
            except Exception as t:
                self.st[n] = str(t)
        self.st[13] = op13
        self.st[14] = lambda n, e: self.st.__setitem__(n, json.loads(str(self.st.get(e, "{}"))))
        self.st[15] = lambda n, e: self.st.__setitem__(n, json.dumps(self.st.get(e), separators=(",", ":"), default=str))
        self.st[16] = key
        def op17(n: Any, e: Any, *r: Any) -> None:
            try:
                fn = self.st.get(e)
                args = [self.st.get(k) for k in r]
                self.st[n] = fn(*args) if callable(fn) else None
            except Exception as t:
                self.st[n] = str(t)
        self.st[17] = op17
        self.st[18] = lambda n: self.st.__setitem__(n, _atob(str(self.st.get(n, ""))))
        self.st[19] = lambda n: self.st.__setitem__(n, _btoa(str(self.st.get(n, ""))))
        self.st[20] = lambda n, e, r, *o: (self.st.get(r)(*o) if callable(self.st.get(r)) else None) if self.st.get(n) == self.st.get(e) else None
        def op21(n: Any, e: Any, r: Any, o: Any, *i: Any) -> Any:
            try:
                if abs(float(self.st.get(n) or 0) - float(self.st.get(e) or 0)) > float(self.st.get(r) or 0):
                    fn = self.st.get(o)
                    return fn(*i) if callable(fn) else None
            except (ValueError, TypeError):
                pass
            return None
        self.st[21] = op21
        def op22(n: Any, e: Any) -> None:
            saved = list(self.st.get(9, []))
            self.st[9] = list(e) if isinstance(e, list) else []
            try:
                self._execute_instructions()
            except Exception as t:
                self.st[n] = str(t)
            finally:
                self.st[9] = saved
        self.st[22] = op22
        self.st[23] = lambda n, e, *r: (self.st.get(e)(*r) if callable(self.st.get(e)) else None) if self.st.get(n) is not None else None
        def op24(n: Any, e: Any, r: Any) -> None:
            obj = self.st.get(e)
            prop = self.st.get(r)
            m = _prop_get(obj, prop) if obj is not None else None
            self.st[n] = m if callable(m) else lambda *a: None
        self.st[24] = op24
        self.st[25] = lambda *a: None
        self.st[26] = lambda *a: None
        self.st[28] = lambda *a: None
        def op27(n: Any, e: Any) -> None:
            o = self.st.get(n)
            v = self.st.get(e)
            if isinstance(o, list):
                try:
                    o.remove(v)
                except ValueError:
                    pass
            else:
                try:
                    self.st[n] = float(o or 0) - float(v or 0)
                except (ValueError, TypeError):
                    self.st[n] = 0
        self.st[27] = op27
        def op29(n: Any, e: Any, r: Any) -> None:
            a = self.st.get(e)
            b = self.st.get(r)
            try:
                self.st[n] = float(a) < float(b)
            except (ValueError, TypeError):
                self.st[n] = str(a or "") < str(b or "")
        self.st[29] = op29
        def op30(t: Any, n: Any, e: Any, r: Any = None) -> None:
            is_arr = isinstance(r, list)
            params = e if is_arr else []
            body = (r if is_arr else e) or []
            def defined_fn(*args: Any) -> Any:
                if vm._done:
                    return None
                saved = list(vm.st.get(9, []))
                if is_arr and isinstance(params, list):
                    for i, p in enumerate(params):
                        vm.st[p] = args[i] if i < len(args) else None
                vm.st[9] = list(body)
                try:
                    vm._execute_instructions()
                    return vm.st.get(n)
                except Exception as ex:
                    return str(ex)
                finally:
                    vm.st[9] = saved
            vm.st[t] = defined_fn
        self.st[30] = op30
        self.st[33] = lambda n, e, r: self.st.__setitem__(n, float(self.st.get(e) or 0) * float(self.st.get(r) or 0))
        self.st[34] = lambda n, e: self.st.__setitem__(n, self.st.get(e))
        def op35(n: Any, e: Any, r: Any) -> None:
            try:
                a = float(self.st.get(e) or 0)
                b = float(self.st.get(r) or 0)
                self.st[n] = 0 if b == 0 else a / b
            except (ValueError, TypeError):
                self.st[n] = 0
        self.st[35] = op35

        # Resolve/reject
        def resolve(v: Any) -> None:
            if not self._done:
                self._done = True
                self._result = _btoa(str(v))
        def reject(v: Any) -> None:
            if not self._done:
                self._done = True
        self.st[3] = resolve
        self.st[4] = reject

        try:
            decoded = _atob(bytecode_b64)
            decrypted = xor_decrypt(decoded, str(self.st[16]))
            instructions = json.loads(decrypted)
            self.st[9] = instructions if isinstance(instructions, list) else []
            self._execute_instructions()
        except Exception:
            if not self._done:
                self._result = None

        return self._result


# ─── Node.js fallback ─────────────────────────────────────────────

_SENTINEL_VM_JS = str(Path(__file__).with_name("sentinel_vm.js"))
_NODE_BIN: str | None = None


def _find_node_bin() -> str:
    """查找 node 可执行文件（PATH / Volta / nvm / 宝塔）。"""
    global _NODE_BIN
    if _NODE_BIN is not None:
        return _NODE_BIN

    import shutil
    node = shutil.which("node")
    if node:
        _NODE_BIN = node
        return _NODE_BIN

    # 常见安装路径
    candidates = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files\Volta\node.exe",
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]
    for c in candidates:
        if os.path.isfile(c):
            _NODE_BIN = c
            return _NODE_BIN

    _NODE_BIN = ""
    return _NODE_BIN


def _run_node_vm(
    req_token: str,
    device_id: str,
    flow: str,
    sentinel_data: dict,
    user_agent: str,
) -> dict[str, Any]:
    """通过 Node.js 运行 sentinel_vm.js（wuzu-image 同款）。"""
    if not os.path.isfile(_SENTINEL_VM_JS):
        logger.warning("[sentinel_vm] Node.js VM 文件不存在: %s", _SENTINEL_VM_JS)
        return {"turnstile_result": None, "so_token": None}

    node_bin = _find_node_bin()
    if not node_bin:
        logger.warning("[sentinel_vm] 系统未安装 Node.js")
        return {"turnstile_result": None, "so_token": None}

    vm_input = json.dumps({
        "req_token": req_token,
        "device_id": device_id,
        "flow": flow,
        "sentinel_data": sentinel_data,
        "user_agent": user_agent,
        "sdk_url": SENTINEL_CHALLENGE_SDK_URL,
    })

    try:
        proc = subprocess.run(
            [node_bin, _SENTINEL_VM_JS],
            input=vm_input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            return json.loads(proc.stdout)
        logger.warning(
            "[sentinel_vm] Node.js VM failed: exit=%d",
            proc.returncode,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[sentinel_vm] Node.js VM 超时 (30s)")
    except json.JSONDecodeError as e:
        logger.warning("[sentinel_vm] Node.js VM returned invalid JSON")
    except Exception as e:
        logger.warning("[sentinel_vm] Node.js VM failed")

    return {"turnstile_result": None, "so_token": None}


# ─── Public API ───────────────────────────────────────────────────
def run_sentinel_vm(
    req_token: str,
    device_id: str,
    flow: str,
    sentinel_data: dict,
    user_agent: str = "",
) -> dict:
    """Run sentinel VMs and return turnstile_result + so_token.

    优先级：Python VM → 简化 turnstile solver → Node.js VM。
    """
    window = _build_mocks(device_id, user_agent)
    result: dict[str, Any] = {"turnstile_result": None, "so_token": None}

    try:
        # 1. Run turnstile VM (Python)
        turnstile_data = sentinel_data.get("turnstile") or {}
        if turnstile_data.get("dx"):
            try:
                tvm = TurnstileVM(window)
                result["turnstile_result"] = tvm.run(turnstile_data["dx"], req_token)
            except Exception:
                result["turnstile_result"] = None

            # Fallback 1: 简化版 turnstile 解算器
            if not result["turnstile_result"]:
                try:
                    from .turnstile_solver import solve_turnstile_token
                    result["turnstile_result"] = solve_turnstile_token(
                        turnstile_data["dx"], req_token
                    )
                except Exception:
                    pass

        # 2. Run collector + snapshot for SO token (Python)
        so_data = sentinel_data.get("so") or {}
        if so_data.get("required") and so_data.get("collector_dx") and so_data.get("snapshot_dx"):
            try:
                vm = SentinelVM(window)
                vm.run(so_data["collector_dx"], req_token)
                snapshot_result = vm.run_continue(so_data["snapshot_dx"])
                if snapshot_result:
                    so_payload = {
                        "so": snapshot_result,
                        "c": sentinel_data.get("token", ""),
                        "id": device_id,
                        "flow": flow,
                    }
                    result["so_token"] = json.dumps(so_payload, separators=(",", ":"))
            except Exception:
                pass
    except Exception:
        pass

    # 3. Fallback 2: Node.js VM（当 Python 不够用时）
    t_need = bool((sentinel_data.get("turnstile") or {}).get("required"))
    s_need = bool((sentinel_data.get("so") or {}).get("required"))
    t_ok = bool(result.get("turnstile_result"))
    s_ok = bool(result.get("so_token"))

    if (t_need and not t_ok) or (s_need and not s_ok):
        logger.info(
            "[sentinel_vm] Python VM 不完整 (turnstile=%s, so=%s)，降级 Node.js",
            "OK" if t_ok else "EMPTY", "OK" if s_ok else "EMPTY",
        )
        node_result = _run_node_vm(req_token, device_id, flow, sentinel_data, user_agent)
        # 补齐缺失的字段
        if not t_ok and node_result.get("turnstile_result"):
            result["turnstile_result"] = node_result["turnstile_result"]
        if not s_ok and node_result.get("so_token"):
            result["so_token"] = node_result["so_token"]

    return result
