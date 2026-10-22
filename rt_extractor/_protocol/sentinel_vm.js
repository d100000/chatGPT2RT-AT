/**
 * Sentinel VM Runner - executes turnstile, collector, and snapshot VMs
 * with browser-like mocks. Called from Python to generate sentinel tokens.
 *
 * Usage: echo '<json>' | node sentinel_vm.js
 * Input JSON: { req_token, device_id, flow, user_agent, sdk_url, sentinel_data: { token, turnstile, so, proofofwork } }
 * Output JSON: { turnstile_result, so_token }
 */
'use strict';

// ─── Browser Mocks (Proxy-based, handles arbitrary property access) ──
const _atob = (s) => Buffer.from(s, 'base64').toString('binary');
const _btoa = (s) => Buffer.from(s, 'binary').toString('base64');

function createDeepProxy(base = {}, name = 'proxy') {
  const handler = {
    get(target, prop) {
      if (prop === Symbol.toPrimitive) return (hint) => hint === 'number' ? 0 : '';
      if (prop === Symbol.iterator) return undefined;
      if (prop === Symbol.toStringTag) return 'Object';
      if (prop === 'then') return undefined; // prevent Promise detection
      if (prop === 'toString') return () => '[object Object]';
      if (prop === 'valueOf') return () => 0;
      if (prop === 'toJSON') return () => ({});
      if (prop === 'length') return target.length !== undefined ? target.length : 0;
      if (prop === 'constructor') return Object;
      if (prop in target) return target[prop];
      return undefined;
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
    has(target, prop) {
      return prop in target;
    },
    getPrototypeOf() {
      return Object.prototype;
    },
    ownKeys(target) {
      return Reflect.ownKeys(target);
    },
    getOwnPropertyDescriptor(target, prop) {
      if (prop in target) return { configurable: true, enumerable: true, writable: true, value: target[prop] };
      return undefined;
    },
  };
  return new Proxy(Object.assign(Object.create(null), base), handler);
}

const perf_origin = Date.now() - Math.random() * 100000;
const mockPerformance = createDeepProxy({
  now: () => Date.now() - perf_origin,
  timeOrigin: perf_origin,
  memory: createDeepProxy({ jsHeapSizeLimit: 4294705152 }),
  mark: () => {},
  measure: () => {},
  getEntriesByType: () => [],
  getEntriesByName: () => [],
});
const mockScreen = createDeepProxy({ width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24 });
const mockNavigator = createDeepProxy({
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  language: 'en-US',
  languages: ['en-US', 'en'],
  hardwareConcurrency: 8,
  vendorSub: '',
  vendor: 'Google Inc.',
  appVersion: '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  appName: 'Netscape',
  platform: 'Win32',
  product: 'Gecko',
  productSub: '20030107',
  cookieEnabled: true,
  onLine: true,
  maxTouchPoints: 0,
  doNotTrack: null,
  plugins: { length: 5, 0: { name: 'PDF Viewer' }, 1: { name: 'Chrome PDF Viewer' }, 2: { name: 'Chromium PDF Viewer' }, 3: { name: 'Microsoft Edge PDF Viewer' }, 4: { name: 'WebKit built-in PDF' } },
  mimeTypes: { length: 2, 0: { type: 'application/pdf' }, 1: { type: 'text/pdf' } },
  webdriver: false,
  mediaDevices: createDeepProxy({ enumerateDevices: async () => [] }),
  connection: createDeepProxy({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false }),
  getBattery: async () => createDeepProxy({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 }),
  getGamepads: () => [],
  javaEnabled: () => false,
  sendBeacon: () => true,
  clipboard: createDeepProxy({}),
  credentials: createDeepProxy({}),
  locks: createDeepProxy({}),
  mediaSession: createDeepProxy({}),
  permissions: createDeepProxy({ query: async () => createDeepProxy({ state: 'prompt' }) }),
  serviceWorker: createDeepProxy({}),
  storage: createDeepProxy({}),
  usb: createDeepProxy({}),
  userActivation: createDeepProxy({ hasBeenActive: true, isActive: false }),
});
const mockLocation = createDeepProxy({
  href: 'https://auth.openai.com/about-you',
  search: '',
  origin: 'https://auth.openai.com',
  protocol: 'https:',
  host: 'auth.openai.com',
  hostname: 'auth.openai.com',
  port: '',
  pathname: '/about-you',
  hash: '',
});
const mockDocumentElement = createDeepProxy({
  getAttribute: (name) => null,
  style: createDeepProxy({}),
  classList: createDeepProxy({ add: () => {}, remove: () => {}, contains: () => false }),
});
const mockDocument = createDeepProxy({
  scripts: [
    { src: 'https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js' },
    { src: 'https://auth-cdn.oaistatic.com/assets/manifest-ef4d6d9d.js' },
  ],
  cookie: '',
  documentElement: mockDocumentElement,
  head: createDeepProxy({}),
  body: createDeepProxy({}),
  createElement: (tag) => createDeepProxy({ tagName: tag, style: {}, src: '', appendChild: () => {} }),
  createTextNode: (text) => createDeepProxy({ textContent: text }),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementsByTagName: (tag) => tag === 'script' ? mockDocument.scripts : [],
  title: '',
  readyState: 'complete',
  visibilityState: 'visible',
  hidden: false,
  hasFocus: () => true,
  referrer: 'https://auth.openai.com/about-you',
  URL: 'https://auth.openai.com/about-you',
  domain: 'auth.openai.com',
  characterSet: 'UTF-8',
  contentType: 'text/html',
  currentScript: { src: 'https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js' },
});

// Build window mock with all standard globals
const mockWindow = {
  performance: mockPerformance,
  screen: mockScreen,
  navigator: mockNavigator,
  location: mockLocation,
  document: mockDocument,
  Math,
  Date,
  crypto: globalThis.crypto,
  TextEncoder: globalThis.TextEncoder,
  TextDecoder: globalThis.TextDecoder,
  URL: globalThis.URL,
  URLSearchParams: globalThis.URLSearchParams,
  Uint8Array: globalThis.Uint8Array,
  ArrayBuffer: globalThis.ArrayBuffer,
  DataView: globalThis.DataView,
  Int8Array: globalThis.Int8Array,
  Int16Array: globalThis.Int16Array,
  Int32Array: globalThis.Int32Array,
  Float32Array: globalThis.Float32Array,
  Float64Array: globalThis.Float64Array,
  requestIdleCallback: (cb, opts) => setTimeout(() => cb({ timeRemaining: () => 1, didTimeout: false }), 0),
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  atob: _atob,
  btoa: _btoa,
  fetch: async () => createDeepProxy({ ok: true, status: 200, json: async () => ({}), text: async () => '', headers: createDeepProxy({}) }),
  XMLHttpRequest: createDeepProxy({}),
  Reflect,
  Object,
  Array,
  JSON,
  Number,
  String,
  Boolean,
  RegExp,
  Error,
  TypeError,
  RangeError,
  SyntaxError,
  ReferenceError,
  Promise,
  Map,
  Set,
  WeakMap,
  WeakSet,
  Proxy,
  Symbol,
  Function,
  parseInt,
  parseFloat,
  isNaN,
  isFinite,
  encodeURIComponent,
  decodeURIComponent,
  encodeURI,
  decodeURI,
  escape: (s) => encodeURIComponent(s),
  unescape: (s) => decodeURIComponent(s),
  undefined: undefined,
  NaN: NaN,
  Infinity: Infinity,
  // Feature detection properties (matching _getConfig)
  cache: {},      // CacheStorage - "cache" in window = true
  caches: createDeepProxy({}),
  indexedDB: createDeepProxy({}),
  localStorage: createDeepProxy({ getItem: () => null, setItem: () => {}, removeItem: () => {} }),
  sessionStorage: createDeepProxy({ getItem: () => null, setItem: () => {}, removeItem: () => {} }),
  history: createDeepProxy({ length: 1, pushState: () => {}, replaceState: () => {} }),
  innerWidth: 1920,
  innerHeight: 937,
  outerWidth: 1920,
  outerHeight: 1040,
  devicePixelRatio: 1,
  scrollX: 0,
  scrollY: 0,
  pageXOffset: 0,
  pageYOffset: 0,
  screenX: 0,
  screenY: 0,
  screenLeft: 0,
  screenTop: 0,
  visualViewport: createDeepProxy({ width: 1920, height: 937, offsetLeft: 0, offsetTop: 0, scale: 1 }),
  getComputedStyle: () => createDeepProxy({ getPropertyValue: () => '' }),
  matchMedia: () => createDeepProxy({ matches: false, media: '', addEventListener: () => {}, removeEventListener: () => {} }),
  open: () => null,
  close: () => {},
  focus: () => {},
  blur: () => {},
  alert: () => {},
  confirm: () => true,
  prompt: () => '',
  print: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => true,
  postMessage: () => {},
  requestAnimationFrame: (cb) => setTimeout(cb, 16),
  cancelAnimationFrame: (id) => clearTimeout(id),
  getSelection: () => createDeepProxy({ toString: () => '', rangeCount: 0 }),
  structuredClone: globalThis.structuredClone,
  console,
  // Explicit false for detection features
  // These should NOT be in window
  // ai: undefined, (not set = not "in window")
  // solana: undefined,
  // dump: undefined,
  // startEnforcement: undefined,
  // InstallTrigger: undefined,
  // createPRNG: undefined,
  // data: undefined,
};
mockWindow.top = mockWindow;
mockWindow.self = mockWindow;
mockWindow.window = mockWindow;
mockWindow.globalThis = mockWindow;
mockWindow.parent = mockWindow;
mockWindow.frames = mockWindow;
mockWindow.length = 0;

// Wrap mockWindow in a Proxy for fallback property access
const _globalWindow = new Proxy(mockWindow, {
  get(target, prop) {
    if (prop === Symbol.toPrimitive) return () => '[object Window]';
    if (prop === Symbol.iterator) return undefined;
    if (prop in target) return target[prop];
    // Return undefined for unknown properties (important for "x in window" checks)
    return undefined;
  },
  set(target, prop, value) {
    target[prop] = value;
    return true;
  },
  has(target, prop) {
    return prop in target;
  },
});

// ─── XOR decrypt ─────────────────────────────────────────────────
function xorDecrypt(ciphertext, key) {
  let result = '';
  for (let i = 0; i < ciphertext.length; i++) {
    result += String.fromCharCode(ciphertext.charCodeAt(i) ^ key.charCodeAt(i % key.length));
  }
  return result;
}

// ─── VM Implementation ──────────────────────────────────────────
function createVM() {
  const St = new Map();
  let Ct = 0;
  let At = Promise.resolve();

  function Ot(t) {
    const n = At.then(t, t);
    At = n.then(() => {}, () => {});
    return n;
  }

  async function _t() {
    while (St.get(9 /* Y */).length > 0) {
      const [n, ...e] = St.get(9).shift();
      const r = St.get(n)(...e);
      if (r && typeof r.then === 'function') await r;
      Ct++;
    }
  }

  function initVM(key) {
    St.clear();
    Ct = 0;

    // Opcode 0: recursive VM call
    St.set(0, (t) => Ot(() => runVM(t)));
    // Opcode 1: XOR
    St.set(1, (n, e) =>
      St.set(
        n,
        xorDecrypt('' + St.get(n), '' + St.get(e))
      )
    );
    // Opcode 2: set value
    St.set(2, (n, e) => St.set(n, e));
    // Opcode 5: append
    St.set(5, (n, e) => {
      const o = St.get(n);
      if (Array.isArray(o)) o.push(St.get(e));
      else St.set(n, o + St.get(e));
    });
    // Opcode 6: index access
    St.set(6, (n, e, r) => { const obj = St.get(e); const prop = St.get(r); St.set(n, obj != null ? obj[prop] : undefined); });
    // Opcode 7: function call (no result capture)
    St.set(7, (n, ...e) => St.get(n)(...e.map((k) => St.get(k))));
    // Opcode 8: copy
    St.set(8, (n, e) => St.set(n, St.get(e)));
    // Opcode 9: instruction queue (set during run)
    St.set(9, []);
    // Opcode 10: window
    St.set(10, _globalWindow);
    // Opcode 11: script src matcher
    St.set(11, (n, e) =>
      St.set(
        n,
        (
          Array.from(_globalWindow.document.scripts || [])
            .map((s) => s?.src?.match(St.get(e)))
            .filter((m) => m?.length)[0] ?? []
        )[0] ?? null
      )
    );
    // Opcode 12: self reference (return the map)
    St.set(12, (n) => St.set(n, St));
    // Opcode 13: try-call with error capture
    St.set(13, (n, e, ...r) => {
      try {
        St.get(e)(...r);
      } catch (t) {
        St.set(n, '' + t);
      }
    });
    // Opcode 14: JSON.parse
    St.set(14, (n, e) => St.set(n, JSON.parse('' + St.get(e))));
    // Opcode 15: JSON.stringify
    St.set(15, (n, e) => St.set(n, JSON.stringify(St.get(e))));
    // Opcode 16: encryption key
    St.set(16, key);
    // Opcode 17: try-call with result + error capture
    St.set(17, (n, e, ...r) => {
      try {
        const t = St.get(e)(...r.map((k) => St.get(k)));
        if (t && typeof t.then === 'function') {
          return t
            .then((v) => St.set(n, v))
            .catch((err) => St.set(n, '' + err));
        }
        St.set(n, t);
      } catch (t) {
        St.set(n, '' + t);
      }
    });
    // Opcode 18: atob
    St.set(18, (n) => St.set(n, _globalWindow.atob('' + St.get(n))));
    // Opcode 19: btoa
    St.set(19, (n) => St.set(n, _globalWindow.btoa('' + St.get(n))));
    // Opcode 20: if-equal call
    St.set(20, (n, e, r, ...o) =>
      St.get(n) === St.get(e) ? St.get(r)(...o) : null
    );
    // Opcode 21: if-diff call (absolute difference > threshold)
    St.set(21, (n, e, r, o, ...i) =>
      Math.abs(St.get(n) - St.get(e)) > St.get(r) ? St.get(o)(...i) : null
    );
    // Opcode 22: sub-execute (run instructions in sub-context)
    St.set(22, (n, e) => {
      const o = [...St.get(9)];
      return St.set(9, [...e]),
        _t()
          .catch((t) => St.set(n, '' + t))
          .finally(() => St.set(9, o));
    });
    // Opcode 23: if-defined call
    St.set(23, (n, e, ...r) =>
      void 0 !== St.get(n) ? St.get(e)(...r) : null
    );
    // Opcode 24: bind and call
    // Opcode 24: bind and call (tolerant - returns noop if method missing)
    St.set(24, (n, e, r) => {
      const obj = St.get(e);
      const prop = St.get(r);
      const method = obj?.[prop];
      if (typeof method === 'function') {
        St.set(n, method.bind(obj));
      } else {
        St.set(n, () => undefined);
      }
    });
    // Opcodes 25-26, 28: noop
    St.set(25, () => {});
    St.set(26, () => {});
    St.set(28, () => {});
    // Opcode 27: remove
    St.set(27, (n, e) => {
      const o = St.get(n);
      if (Array.isArray(o)) o.splice(o.indexOf(St.get(e)), 1);
      else St.set(n, o - St.get(e));
    });
    // Opcode 29: less-than compare
    St.set(29, (n, e, r) => St.set(n, St.get(e) < St.get(r)));
    // Opcode 30: function define
    St.set(30, (t, n, e, r) => {
      const isArr = Array.isArray(r);
      const params = isArr ? e : [];
      const body = (isArr ? r : e) || [];
      St.set(t, (...args) => {
        const saved = [...St.get(9)];
        if (isArr) {
          for (let i = 0; i < params.length; i++) {
            St.set(params[i], args[i]);
          }
        }
        St.set(9, [...body]);
        return _t()
          .then(() => St.get(n))
          .catch((t) => '' + t)
          .finally(() => St.set(9, saved));
      });
    });
    // Opcode 33: multiply
    St.set(33, (n, e, r) => {
      St.set(n, Number(St.get(e)) * Number(St.get(r)));
    });
    // Opcode 34: await/resolve
    St.set(34, (n, e) => {
      try {
        const v = St.get(e);
        return Promise.resolve(v).then((r) => St.set(n, r));
      } catch {
        return;
      }
    });
    // Opcode 35: divide
    St.set(35, (n, e, r) => {
      const a = Number(St.get(e));
      const b = Number(St.get(r));
      St.set(n, b === 0 ? 0 : a / b);
    });
  }

  function runVM(bytecodeB64, key) {
    return new Promise((resolve, reject) => {
      if (key !== undefined) {
        initVM(key);
      }

      let done = false;
      const timer = setTimeout(() => {
        if (!done) {
          done = true;
          reject(new Error('session_observer_vm_timeout'));
        }
      }, 60000);

      const finish = (v) => {
        if (!done) {
          done = true;
          clearTimeout(timer);
          resolve(v);
        }
      };

      // Set resolve/reject handlers
      St.set(3 /* z */, (t) => finish(_globalWindow.btoa('' + t)));
      St.set(4 /* H */, (t) => {
        if (!done) {
          done = true;
          clearTimeout(timer);
          reject(_globalWindow.btoa('' + t));
        }
      });
      // Set function define handler (opcode 30)
      St.set(30, (t, n, e, r) => {
        const isArr = Array.isArray(r);
        const params = isArr ? e : [];
        const body = (isArr ? r : e) || [];
        St.set(t, (...args) => {
          if (done) return;
          const saved = [...St.get(9)];
          if (isArr) {
            for (let i = 0; i < params.length; i++) {
              St.set(params[i], args[i]);
            }
          }
          St.set(9, [...body]);
          return _t()
            .then(() => St.get(n))
            .catch((t) => '' + t)
            .finally(() => St.set(9, saved));
        });
      });

      try {
        const decoded = _globalWindow.atob(bytecodeB64);
        const decrypted = xorDecrypt(decoded, '' + St.get(16));
        const instructions = JSON.parse(decrypted);
        St.set(9, instructions);
        _t().catch((t) => {
          finish(_globalWindow.btoa(Ct + ': ' + t));
        });
      } catch (t) {
        finish(_globalWindow.btoa(Ct + ': ' + t));
      }
    });
  }

  return { runVM, St, getCt: () => Ct };
}

// ─── Turnstile VM (separate instance, 500ms timeout) ────────────
function createTurnstileVM() {
  const bn = new Map();
  let kn = 0;
  let Sn = Promise.resolve();

  function Cn(t) {
    const n = Sn.then(t, t);
    Sn = n.then(() => {}, () => {});
    return n;
  }

  async function An() {
    while (bn.get(9).length > 0) {
      const [n, ...e] = bn.get(9).shift();
      const r = bn.get(n)(...e);
      if (r && typeof r.then === 'function') await r;
      kn++;
    }
  }

  function runTurnstile(bytecodeB64, key) {
    return Cn(() =>
      new Promise((resolve, reject) => {
        // Init VM
        bn.clear();
        kn = 0;

        bn.set(0, (t) => Cn(() => new Promise((res) => {
          let o2 = false;
          setTimeout(() => { o2 = true; res('' + kn); }, 500);
          bn.set(3, (v) => { if (!o2) { o2 = true; res(_globalWindow.btoa('' + v)); } });
          bn.set(4, (v) => { if (!o2) { o2 = true; reject(_globalWindow.btoa('' + v)); } });
          try {
            bn.set(9, JSON.parse(xorDecrypt(_globalWindow.atob(t), '' + bn.get(16))));
            An().catch((e) => { res(_globalWindow.btoa(kn + ': ' + e)); });
          } catch (e) {
            res(_globalWindow.btoa(kn + ': ' + e));
          }
        })));
        bn.set(1, (n, e) => bn.set(n, xorDecrypt('' + bn.get(n), '' + bn.get(e))));
        bn.set(2, (n, e) => bn.set(n, e));
        bn.set(5, (n, e) => {
          const o = bn.get(n);
          if (Array.isArray(o)) o.push(bn.get(e));
          else bn.set(n, o + bn.get(e));
        });
        bn.set(6, (n, e, r) => { const obj = bn.get(e); const prop = bn.get(r); bn.set(n, obj != null ? obj[prop] : undefined); });
        bn.set(7, (n, ...e) => bn.get(n)(...e.map((k) => bn.get(k))));
        bn.set(8, (n, e) => bn.set(n, bn.get(e)));
        bn.set(9, []);
        bn.set(10, _globalWindow);
        bn.set(11, (n, e) =>
          bn.set(n, (Array.from(_globalWindow.document.scripts || []).map((s) => s?.src?.match(bn.get(e))).filter((m) => m?.length)[0] ?? [])[0] ?? null)
        );
        bn.set(12, (n) => bn.set(n, bn));
        bn.set(13, (n, e, ...r) => { try { bn.get(e)(...r); } catch (t) { bn.set(n, '' + t); } });
        bn.set(14, (n, e) => bn.set(n, JSON.parse('' + bn.get(e))));
        bn.set(15, (n, e) => bn.set(n, JSON.stringify(bn.get(e))));
        bn.set(16, key);
        bn.set(17, (n, e, ...r) => {
          try {
            const t = bn.get(e)(...r.map((k) => bn.get(k)));
            if (t && typeof t.then === 'function') return t.then((v) => bn.set(n, v)).catch((e2) => bn.set(n, '' + e2));
            bn.set(n, t);
          } catch (t) { bn.set(n, '' + t); }
        });
        bn.set(18, (n) => bn.set(n, _globalWindow.atob('' + bn.get(n))));
        bn.set(19, (n) => bn.set(n, _globalWindow.btoa('' + bn.get(n))));
        bn.set(20, (n, e, r, ...o) => bn.get(n) === bn.get(e) ? bn.get(r)(...o) : null);
        bn.set(21, (n, e, r, o, ...i) => Math.abs(bn.get(n) - bn.get(e)) > bn.get(r) ? bn.get(o)(...i) : null);
        bn.set(22, (n, e) => {
          const o = [...bn.get(9)];
          return bn.set(9, [...e]), An().catch((t) => bn.set(n, '' + t)).finally(() => bn.set(9, o));
        });
        bn.set(23, (n, e, ...r) => void 0 !== bn.get(n) ? bn.get(e)(...r) : null);
        bn.set(24, (n, e, r) => { const obj = bn.get(e); const prop = bn.get(r); const m = obj?.[prop]; bn.set(n, typeof m === 'function' ? m.bind(obj) : () => undefined); });
        bn.set(25, () => {});
        bn.set(26, () => {});
        bn.set(27, (n, e) => {
          const o = bn.get(n);
          if (Array.isArray(o)) o.splice(o.indexOf(bn.get(e)), 1);
          else bn.set(n, o - bn.get(e));
        });
        bn.set(28, () => {});
        bn.set(29, (n, e, r) => bn.set(n, bn.get(e) < bn.get(r)));
        bn.set(30, (t, n, e, r) => {
          const isArr = Array.isArray(r);
          const params = isArr ? e : [];
          const body = (isArr ? r : e) || [];
          bn.set(t, (...args) => {
            const saved = [...bn.get(9)];
            if (isArr) for (let i = 0; i < params.length; i++) bn.set(params[i], args[i]);
            bn.set(9, [...body]);
            return An().then(() => bn.get(n)).catch((t2) => '' + t2).finally(() => bn.set(9, saved));
          });
        });
        bn.set(33, (n, e, r) => bn.set(n, Number(bn.get(e)) * Number(bn.get(r))));
        bn.set(34, (n, e) => { try { return Promise.resolve(bn.get(e)).then((v) => bn.set(n, v)); } catch { return; } });
        bn.set(35, (n, e, r) => { const a = Number(bn.get(e)); const b = Number(bn.get(r)); bn.set(n, b === 0 ? 0 : a / b); });

        let done = false;
        setTimeout(() => {
          if (!done) { done = true; resolve(null); }
        }, 500);

        bn.set(3, (v) => { if (!done) { done = true; resolve(_globalWindow.btoa('' + v)); } });
        bn.set(4, (v) => { if (!done) { done = true; reject(_globalWindow.btoa('' + v)); } });

        bn.set(30, (t, n, e, r) => {
          const isArr = Array.isArray(r);
          const params = isArr ? e : [];
          const body = (isArr ? r : e) || [];
          bn.set(t, (...args) => {
            if (done) return;
            const saved = [...bn.get(9)];
            if (isArr) for (let i = 0; i < params.length; i++) bn.set(params[i], args[i]);
            bn.set(9, [...body]);
            return An().then(() => bn.get(n)).catch((t2) => '' + t2).finally(() => bn.set(9, saved));
          });
        });

        try {
          bn.set(9, JSON.parse(xorDecrypt(_globalWindow.atob(bytecodeB64), '' + bn.get(16))));
          An().catch(() => { if (!done) { done = true; resolve(null); } });
        } catch {
          if (!done) { done = true; resolve(null); }
        }
      })
    );
  }

  return { runTurnstile };
}

// ─── Main ────────────────────────────────────────────────────────
async function main() {
  // Read input from stdin
  let inputData = '';
  for await (const chunk of process.stdin) {
    inputData += chunk;
  }

  const input = JSON.parse(inputData);
  const { req_token, device_id, flow, sentinel_data, user_agent, sdk_url } = input;

  // Set oai-did cookie on document
  mockDocument.cookie = `oai-did=${device_id}`;
  if (user_agent) {
    mockNavigator.userAgent = user_agent;
    mockNavigator.appVersion = user_agent.replace(/^Mozilla\//, '');
  }
  if (sdk_url) {
    mockDocument.scripts[0].src = sdk_url;
    mockDocument.currentScript.src = sdk_url;
  }

  const result = {
    turnstile_result: null,
    so_token: null,
    error: null,
  };

  try {
    // 1. Run turnstile VM
    if (sentinel_data.turnstile?.dx) {
      try {
        const tvm = createTurnstileVM();
        result.turnstile_result = await tvm.runTurnstile(
          sentinel_data.turnstile.dx,
          req_token
        );
      } catch (e) {
        result.turnstile_result = null;
      }
    }

    // 2. Run collector + snapshot for SO token
    if (sentinel_data.so?.required && sentinel_data.so?.collector_dx && sentinel_data.so?.snapshot_dx) {
      try {
        const vm = createVM();
        // Run collector (with key initialization)
        await vm.runVM(sentinel_data.so.collector_dx, req_token);
        // Run snapshot (reuses state from collector)
        const snapshotResult = await vm.runVM(sentinel_data.so.snapshot_dx);

        if (snapshotResult) {
          // Build SO token: {so: snapshot, c: cached_token, id: device_id, flow: flow}
          const soPayload = {
            so: snapshotResult,
            c: sentinel_data.token,
            id: device_id,
            flow: flow,
          };
          result.so_token = JSON.stringify(soPayload);
        }
      } catch (e) {
        // SO token generation failed - this is OK, browser also handles this gracefully
        result.so_error = '' + e;
      }
    }
  } catch (e) {
    result.error = '' + e;
  }

  process.stdout.write(JSON.stringify(result));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ error: '' + e, turnstile_result: null, so_token: null }));
  process.exit(0);
});
