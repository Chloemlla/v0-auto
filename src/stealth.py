# -*- coding: utf-8 -*-
"""浏览器反自动化与人机行为辅助。

针对 v0/Vercel 使用的 Kasada（ips.js / x-kpsdk-* / KP_UIDz）做工程向加固：
1. rebrowser 补丁内核（减少 CDP Runtime 泄漏）
2. 启动参数与 init 脚本（隐藏 webdriver / Playwright / CDP 痕迹）
3. Canvas / WebGL / 插件等指纹噪声
4. 贝塞尔鼠标轨迹、真人打字
5. 提交前等待页面侧 Kasada 脚本真正签发的 cookie/header 材料

重要：x-kpsdk-* 令牌由 Kasada 前端脚本动态计算并随请求发出。
禁止伪造随机 token（服务端会立刻判定失败）。本模块只做：
- 等 ips.js 加载与挑战完成
- 读取页面已有 KP_*/kpsdk cookie 状态
- 降低“一眼自动化”指纹，让脚本有机会正常签发
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# 启动参数（Chromium / Edge / BitBrowser open_args）
# ---------------------------------------------------------------------------

BITBROWSER_SAFE_ARGS: list[str] = [
    # 比特自带指纹，禁止塞大量 Chromium 参数以免打架
    "--disable-blink-features=AutomationControlled",
]


DEFAULT_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=IsolateOrigins,site-per-process,TranslateUI,AutomationControlled",
    "--disable-ipc-flooding-protection",
    "--password-store=basic",
    "--use-mock-keychain",
    "--lang=zh-CN",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-service-autorun",
    "--export-tagged-pdf",
    "--force-color-profile=srgb",
]


# ---------------------------------------------------------------------------
# Init script：隐藏自动化 + 轻度指纹噪声（不伪造 UA/TLS）
# ---------------------------------------------------------------------------

STEALTH_INIT_SCRIPT = r"""
(() => {
  const define = (obj, prop, getter) => {
    try {
      Object.defineProperty(obj, prop, {
        get: getter,
        configurable: true,
        enumerable: true,
      });
    } catch (e) {}
  };

  // 1) webdriver
  try {
    define(Navigator.prototype, 'webdriver', () => undefined);
    define(navigator, 'webdriver', () => undefined);
  } catch (e) {}

  // 2) chrome runtime 外壳
  try {
    if (!window.chrome) {
      window.chrome = {
        runtime: {
          OnInstalledReason: { CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update', INSTALL: 'install', UPDATE: 'update' },
          OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
          PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
          PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
          PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
          RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
          connect: function () {},
          sendMessage: function () {},
          id: undefined,
        },
        loadTimes: function () { return {}; },
        csi: function () { return {}; },
        app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
      };
    } else if (!window.chrome.runtime) {
      window.chrome.runtime = { connect: function () {}, sendMessage: function () {} };
    }
  } catch (e) {}

  // 3) languages / platform / hardware
  try { define(Navigator.prototype, 'languages', () => Object.freeze(['zh-CN', 'zh', 'en-US', 'en'])); } catch (e) {}
  try { define(Navigator.prototype, 'language', () => 'zh-CN'); } catch (e) {}
  try { define(Navigator.prototype, 'maxTouchPoints', () => 0); } catch (e) {}
  try {
    if (!navigator.hardwareConcurrency || navigator.hardwareConcurrency < 2) {
      define(Navigator.prototype, 'hardwareConcurrency', () => 8);
    }
  } catch (e) {}
  try {
    if (!navigator.deviceMemory) {
      define(Navigator.prototype, 'deviceMemory', () => 8);
    }
  } catch (e) {}

  // 4) plugins / mimeTypes（看起来像真实 Chromium PDF 插件）
  try {
    const makeMime = (type, suffixes, description, plugin) => {
      const mime = { type, suffixes, description, enabledPlugin: plugin };
      return mime;
    };
    const makePlugin = (name, filename, description) => {
      const plugin = {
        name,
        filename,
        description,
        length: 1,
        0: null,
        item: function (i) { return this[i] || null; },
        namedItem: function (n) { return n === this[0]?.type ? this[0] : null; },
      };
      plugin[0] = makeMime('application/pdf', 'pdf', 'Portable Document Format', plugin);
      plugin.length = 1;
      return plugin;
    };
    const pluginData = [
      makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      makePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      makePlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
    ];
    const plugins = {
      length: pluginData.length,
      item: (i) => pluginData[i] || null,
      namedItem: (n) => pluginData.find((p) => p.name === n) || null,
      refresh: () => {},
    };
    pluginData.forEach((p, i) => { plugins[i] = p; });
    define(Navigator.prototype, 'plugins', () => plugins);
    const mimeTypes = {
      length: pluginData.length,
      item: (i) => pluginData[i] && pluginData[i][0],
      namedItem: (n) => pluginData.map((p) => p[0]).find((m) => m && m.type === n) || null,
    };
    pluginData.forEach((p, i) => { mimeTypes[i] = p[0]; });
    define(Navigator.prototype, 'mimeTypes', () => mimeTypes);
  } catch (e) {}

  // 5) permissions
  try {
    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (originalQuery) {
      window.navigator.permissions.query = (parameters) =>
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : originalQuery.call(window.navigator.permissions, parameters);
    }
  } catch (e) {}

  // 6) 清理 Playwright / CDP / selenium 痕迹
  try {
    const badKey = /^(cdc_|__playwright|__pw_|__webdriver|__driver|__selenium|__fxdriver|domAutomation|domAutomationController|_Selenium|callPhantom|__nightmare)/i;
    for (const key of Object.getOwnPropertyNames(window)) {
      if (badKey.test(key)) {
        try { delete window[key]; } catch (e) {
          try { define(window, key, () => undefined); } catch (e2) {}
        }
      }
    }
    for (const key of ['webdriver', 'domAutomation', 'domAutomationController', '__webdriver_evaluate', '__selenium_evaluate', '__webdriver_script_function', '__webdriver_script_func', '__webdriver_script_fn', '__fxdriver_evaluate', '__driver_unwrapped', '__webdriver_unwrapped', '__driver_evaluate', '__selenium_unwrapped', '__fxdriver_unwrapped']) {
      try { if (key in document) define(document, key, () => undefined); } catch (e) {}
      try { if (key in window) define(window, key, () => undefined); } catch (e) {}
    }
    try {
      if (window.navigator.webdriver) define(Navigator.prototype, 'webdriver', () => undefined);
    } catch (e) {}
  } catch (e) {}

  // 7) Canvas 噪声（轻微扰动，避免破坏业务渲染）
  try {
    const toDataURL = HTMLCanvasElement.prototype.toDataURL;
    const toBlob = HTMLCanvasElement.prototype.toBlob;
    const getImageData = CanvasRenderingContext2D.prototype.getImageData;
    const noise = (canvas) => {
      try {
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const { width, height } = canvas;
        if (!width || !height || width > 3000 || height > 3000) return;
        const img = ctx.getImageData(0, 0, Math.min(width, 16), Math.min(height, 16));
        for (let i = 0; i < img.data.length; i += 4) {
          img.data[i] = img.data[i] ^ (Math.random() < 0.02 ? 1 : 0);
        }
        ctx.putImageData(img, 0, 0);
      } catch (e) {}
    };
    HTMLCanvasElement.prototype.toDataURL = function (...args) {
      noise(this);
      return toDataURL.apply(this, args);
    };
    HTMLCanvasElement.prototype.toBlob = function (...args) {
      noise(this);
      return toBlob.apply(this, args);
    };
    CanvasRenderingContext2D.prototype.getImageData = function (...args) {
      const data = getImageData.apply(this, args);
      try {
        for (let i = 0; i < Math.min(16, data.data.length); i += 4) {
          data.data[i] = data.data[i] ^ (Math.random() < 0.01 ? 1 : 0);
        }
      } catch (e) {}
      return data;
    };
  } catch (e) {}

  // 8) WebGL vendor/renderer 与参数噪声
  try {
    const patchGetParameter = (proto) => {
      const original = proto.getParameter;
      proto.getParameter = function (parameter) {
        // UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL
        if (parameter === 37445) return 'Google Inc. (Intel)';
        if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return original.call(this, parameter);
      };
    };
    if (window.WebGLRenderingContext) patchGetParameter(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchGetParameter(WebGL2RenderingContext.prototype);
  } catch (e) {}

  // 9) AudioContext 轻微噪声
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
      const originalGetChannelData = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function (...args) {
        const results = originalGetChannelData.apply(this, args);
        try {
          if (results && results.length > 0 && Math.random() < 0.1) {
            const idx = Math.floor(Math.random() * Math.min(results.length, 32));
            results[idx] = results[idx] + 1e-7;
          }
        } catch (e) {}
        return results;
      };
    }
  } catch (e) {}

  // 10) iframe contentWindow chrome 一致性
  try {
    const originalContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (originalContentWindow && originalContentWindow.get) {
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function () {
          const win = originalContentWindow.get.call(this);
          try {
            if (win && !win.chrome) win.chrome = window.chrome;
          } catch (e) {}
          return win;
        },
      });
    }
  } catch (e) {}

  // 11) Notification.permission
  try {
    if (window.Notification) {
      define(Notification, 'permission', () => 'default');
    }
  } catch (e) {}

  // 12) 暴露只读探测（调试用，不污染全局命名冲突）
  try {
    window.__v0_stealth = { ok: true, ts: Date.now() };
  } catch (e) {}
})();
"""

STEALTH_LIGHT_INIT_SCRIPT = r"""
(() => {
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}
  try {
    const bad = /^(cdc_|__playwright|__pw_|__webdriver|__driver|__selenium)/i;
    for (const key of Object.getOwnPropertyNames(window)) {
      if (bad.test(key)) {
        try { delete window[key]; } catch (e) {}
      }
    }
  } catch (e) {}
  try { window.__v0_stealth = { ok: true, mode: 'light', ts: Date.now() }; } catch (e) {}
})();
"""



def get_playwright_api():
    """优先 patchright（更强 CDP/Runtime 补丁）→ rebrowser → 官方 playwright。

    公开结论：Kasada 常查 CDP 自动化痕迹；手动能过、CDP 附加不过，
    关键在控制通道而不只是 webdriver 字段。
    """
    try:
        from patchright.sync_api import sync_playwright  # type: ignore

        return sync_playwright, "patchright"
    except Exception:
        pass
    try:
        from rebrowser_playwright.sync_api import sync_playwright  # type: ignore

        return sync_playwright, "rebrowser-playwright"
    except Exception:
        from playwright.sync_api import sync_playwright

        return sync_playwright, "playwright"


def bitbrowser_open_args(
    extra: list[str] | None = None,
    incognito: bool = False,
) -> list[str]:
    """BitBrowser 启动参数白名单：只保留安全项。"""
    allowed_prefixes = (
        "--disable-blink-features=",
        "--lang=",
        "--incognito",
    )
    seen: set[str] = set()
    out: list[str] = []
    for item in list(BITBROWSER_SAFE_ARGS) + list(extra or []):
        value = str(item).strip()
        if not value or value in seen:
            continue
        if value == "--incognito" or value.startswith(allowed_prefixes):
            seen.add(value)
            out.append(value)
    if incognito and "--incognito" not in seen:
        out.append("--incognito")
    return out


def merge_launch_args(extra: list[str] | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in list(DEFAULT_LAUNCH_ARGS) + list(extra or []):
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def apply_stealth(
    context_or_page: Any,
    enabled: bool = True,
    mode: str = "full",
) -> None:
    """mode=full 全量指纹噪声；mode=light 只藏 webdriver（比特指纹场景推荐）。"""
    if not enabled or context_or_page is None:
        return
    script = STEALTH_LIGHT_INIT_SCRIPT if mode == "light" else STEALTH_INIT_SCRIPT
    try:
        context_or_page.add_init_script(script)
    except Exception as exc:
        console.print(f"[yellow]注入 stealth 脚本失败: {exc}[/yellow]")


def human_sleep(a: float = 0.35, b: float = 1.1) -> None:
    time.sleep(random.uniform(a, b))


def _bezier_points(
    x0: float, y0: float, x1: float, y1: float, steps: int
) -> list[tuple[float, float]]:
    """三次贝塞尔曲线路径，模拟真人鼠标。"""
    cx1 = x0 + (x1 - x0) * random.uniform(0.15, 0.45) + random.uniform(-40, 40)
    cy1 = y0 + (y1 - y0) * random.uniform(0.05, 0.35) + random.uniform(-50, 50)
    cx2 = x0 + (x1 - x0) * random.uniform(0.55, 0.85) + random.uniform(-40, 40)
    cy2 = y0 + (y1 - y0) * random.uniform(0.65, 0.95) + random.uniform(-50, 50)
    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        # ease-in-out
        te = t * t * (3 - 2 * t)
        u = 1 - te
        x = (
            (u ** 3) * x0
            + 3 * (u ** 2) * te * cx1
            + 3 * u * (te ** 2) * cx2
            + (te ** 3) * x1
        )
        y = (
            (u ** 3) * y0
            + 3 * (u ** 2) * te * cy1
            + 3 * u * (te ** 2) * cy2
            + (te ** 3) * y1
        )
        points.append((x, y))
    return points


def human_mouse_move_to(page: Any, x: float, y: float, steps: int | None = None) -> None:
    steps = steps or random.randint(18, 36)
    try:
        # 从当前位置近似：用上次坐标或视口随机起点
        start_x = getattr(page, "_v0_mouse_x", None)
        start_y = getattr(page, "_v0_mouse_y", None)
        if start_x is None or start_y is None:
            viewport = page.viewport_size or {"width": 1366, "height": 900}
            start_x = random.randint(40, int(viewport.get("width") or 1366) - 40)
            start_y = random.randint(40, int(viewport.get("height") or 900) - 40)
        for px, py in _bezier_points(float(start_x), float(start_y), x, y, steps):
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.004, 0.018))
        page._v0_mouse_x = x  # type: ignore[attr-defined]
        page._v0_mouse_y = y  # type: ignore[attr-defined]
    except Exception:
        try:
            page.mouse.move(x, y, steps=steps)
        except Exception:
            pass


def human_mouse_wander(page: Any, moves: int = 10) -> None:
    """随机曲线式移动鼠标，给行为评分积累信号。"""
    try:
        viewport = page.viewport_size or {"width": 1366, "height": 900}
        width = int(viewport.get("width") or 1366)
        height = int(viewport.get("height") or 900)
        x = float(getattr(page, "_v0_mouse_x", random.randint(80, max(120, width - 80))))
        y = float(getattr(page, "_v0_mouse_y", random.randint(80, max(120, height - 80))))
        for _ in range(max(1, moves)):
            nx = min(width - 20, max(20, x + random.randint(-220, 220)))
            ny = min(height - 20, max(20, y + random.randint(-160, 160)))
            human_mouse_move_to(page, nx, ny, steps=random.randint(14, 30))
            time.sleep(random.uniform(0.06, 0.28))
            if random.random() < 0.22:
                page.mouse.wheel(0, random.randint(-220, 260))
                time.sleep(random.uniform(0.08, 0.28))
            x, y = nx, ny
    except Exception:
        pass


def human_scroll(page: Any) -> None:
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(80, 320))
            time.sleep(random.uniform(0.15, 0.45))
        if random.random() < 0.5:
            page.mouse.wheel(0, -random.randint(40, 160))
            time.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass


def human_type(locator: Any, text: str, min_delay: int = 55, max_delay: int = 160) -> None:
    """逐字输入（可选）。真人常用复制粘贴时请用 paste_email。"""
    locator.click(timeout=5000)
    human_sleep(0.15, 0.4)
    try:
        locator.fill("")
    except Exception:
        try:
            locator.press("Control+A")
            locator.press("Backspace")
        except Exception:
            pass
    for ch in text:
        if random.random() < 0.03 and ch.isalnum():
            wrong = random.choice("abcdefghijklmnopqrstuvwxyz0123456789")
            locator.type(wrong, delay=random.randint(min_delay, max_delay))
            time.sleep(random.uniform(0.05, 0.15))
            locator.press("Backspace")
            time.sleep(random.uniform(0.04, 0.12))
        locator.type(ch, delay=random.randint(min_delay, max_delay))
        if random.random() < 0.08:
            time.sleep(random.uniform(0.12, 0.35))
    human_sleep(0.25, 0.7)


def _write_clipboard(page: Any, text: str) -> bool:
    """尽量把文本写入页面剪贴板，供 Ctrl+V 使用。"""
    try:
        ok = page.evaluate(
            """async (value) => {
              try {
                await navigator.clipboard.writeText(value);
                return true;
              } catch (e) {
                return false;
              }
            }""",
            text,
        )
        return bool(ok)
    except Exception:
        return False


def _clear_input(locator: Any) -> None:
    try:
        locator.press("Control+A")
        human_sleep(0.04, 0.1)
        locator.press("Backspace")
    except Exception:
        try:
            locator.fill("")
        except Exception:
            pass


def paste_text(
    locator: Any,
    page: Any,
    text: str,
    *,
    label: str = "文本",
    verify: bool = True,
) -> str:
    """模拟真人粘贴：聚焦 → 清空 → Ctrl+V / insert_text（避免 fill 瞬填）。

    返回实际采用的方式：ctrl_v | insert_text | type | fill
    """
    locator.click(timeout=5000)
    human_sleep(0.15, 0.4)
    _clear_input(locator)
    human_sleep(0.12, 0.28)

    # 1) 剪贴板 + Ctrl+V（对齐手动「复制后粘贴」）
    if _write_clipboard(page, text):
        try:
            locator.press("Control+V")
            human_sleep(0.2, 0.45)
            if not verify:
                console.print(f"[cyan]{label}输入: Ctrl+V 粘贴[/cyan]")
                return "ctrl_v"
            try:
                val = locator.input_value(timeout=800) or ""
            except Exception:
                val = ""
            # 完整相等，或 OTP 多格时只验证首格是否有内容
            if val == text or (text and val and (val in text or text.startswith(val))):
                console.print(f"[cyan]{label}输入: Ctrl+V 粘贴[/cyan]")
                return "ctrl_v"
        except Exception:
            pass

    # 2) insert_text（整段键盘插入，非 fill）
    try:
        locator.focus(timeout=2000)
        page.keyboard.insert_text(text)
        human_sleep(0.15, 0.35)
        if not verify:
            console.print(f"[cyan]{label}输入: keyboard.insert_text[/cyan]")
            return "insert_text"
        try:
            val = locator.input_value(timeout=800) or ""
        except Exception:
            val = ""
        if val == text or (text and val and (val in text or text.startswith(val))):
            console.print(f"[cyan]{label}输入: keyboard.insert_text[/cyan]")
            return "insert_text"
    except Exception:
        pass

    # 3) 慢速逐字（比 fill 更像人，OTP 短码可接受）
    if len(text) <= 12:
        try:
            _clear_input(locator)
            for ch in text:
                locator.type(ch, delay=random.randint(70, 160))
                if random.random() < 0.12:
                    time.sleep(random.uniform(0.08, 0.22))
            human_sleep(0.2, 0.45)
            console.print(f"[cyan]{label}输入: 慢速逐字[/cyan]")
            return "type"
        except Exception:
            pass

    # 4) 最后回退 fill
    locator.fill(text)
    console.print(f"[cyan]{label}输入: fill 回退[/cyan]")
    human_sleep(0.15, 0.35)
    return "fill"


def paste_email(locator: Any, page: Any, text: str) -> None:
    """模拟真人：聚焦 → 全选清空 → 粘贴邮箱（不拖系统鼠标）。"""
    paste_text(locator, page, text, label="邮箱", verify=True)


def paste_otp(locator: Any, page: Any, code: str) -> str:
    """验证码粘贴：对齐手动「复制验证码 → 粘贴 → 稍等」。

    Vercel/v0 多格 OTP 常支持在首格粘贴整串并自动分发。
    """
    return paste_text(locator, page, code, label="验证码", verify=True)


def page_submit_strategies(
    locator: Any,
    page: Any,
    strategy: str = "auto",
    offset_mode: str = "center",
) -> str:
    """页面内提交（绝不拖动系统鼠标）。返回实际使用的策略名。

    开源批量注册常见做法（rebrowser/patchright 生态）：
    1. locator.click(position=...)  — 元素内偏移点，仍是浏览器事件
    2. focus + keyboard Enter      — 表单默认提交
    3. form.requestSubmit()        — 原生表单提交
    4. dispatchEvent click           — 最后手段（isTrusted=false，Kasada 可能不认）

    strategy: auto|click|enter|form|js_click
    """
    order: list[str]
    if strategy == "auto":
        order = ["click", "enter", "form"]
    else:
        order = [strategy]

    for name in order:
        try:
            if name == "click":
                try:
                    locator.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                human_sleep(0.2, 0.5)
                box = None
                try:
                    box = locator.bounding_box()
                except Exception:
                    box = None
                if box and box.get("width") and box.get("height"):
                    if offset_mode == "edge":
                        px = box["width"] * random.choice([0.2, 0.8])
                        py = box["height"] * random.choice([0.3, 0.7])
                    else:
                        px = box["width"] * random.uniform(0.3, 0.7)
                        py = box["height"] * random.uniform(0.35, 0.7)
                    locator.hover(timeout=3000)
                    human_sleep(0.15, 0.4)
                    locator.click(
                        timeout=5000,
                        delay=random.randint(50, 140),
                        position={"x": px, "y": py},
                    )
                else:
                    locator.hover(timeout=3000)
                    human_sleep(0.15, 0.4)
                    locator.click(timeout=5000, delay=random.randint(50, 140))
                console.print(f"[cyan]提交策略: Playwright click(position)[/cyan]")
                return "click"

            if name == "enter":
                try:
                    locator.focus(timeout=3000)
                except Exception:
                    locator.click(timeout=3000)
                human_sleep(0.2, 0.5)
                page.keyboard.press("Enter")
                console.print("[cyan]提交策略: focus + Enter[/cyan]")
                return "enter"

            if name == "form":
                ok = locator.evaluate(
                    """(el) => {
                      const form = el.closest('form');
                      if (form && typeof form.requestSubmit === 'function') {
                        try { form.requestSubmit(el.tagName === 'BUTTON' ? el : undefined); return 'requestSubmit'; }
                        catch (e) {}
                        try { form.requestSubmit(); return 'requestSubmit2'; } catch (e2) {}
                      }
                      if (form) { form.submit(); return 'submit'; }
                      el.click();
                      return 'el.click';
                    }"""
                )
                console.print(f"[cyan]提交策略: form/{ok}[/cyan]")
                return "form"

            if name == "js_click":
                locator.evaluate(
                    """(el) => {
                      el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                      el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                      el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
                    }"""
                )
                console.print("[cyan]提交策略: JS MouseEvent（isTrusted=false）[/cyan]")
                return "js_click"
        except Exception as exc:
            console.print(f"[yellow]策略 {name} 失败: {exc}[/yellow]")
            continue
    # last resort
    locator.click(timeout=5000)
    return "click_fallback"



def human_click(
    locator: Any,
    page: Any | None = None,
    offset_mode: str = "center",
) -> None:
    """offset_mode: center=常规随机点; edge=二次点击偏左/右上角，换落点。"""
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    human_sleep(0.2, 0.55)
    try:
        box = locator.bounding_box()
        if box and page is not None:
            if offset_mode == "edge":
                # 二次点击故意换相对位置，避免永远点中心
                rx = random.choice(
                    [random.uniform(0.12, 0.28), random.uniform(0.72, 0.88)]
                )
                ry = random.choice(
                    [random.uniform(0.18, 0.35), random.uniform(0.65, 0.85)]
                )
            else:
                rx = random.uniform(0.28, 0.72)
                ry = random.uniform(0.32, 0.72)
            x = box["x"] + box["width"] * rx
            y = box["y"] + box["height"] * ry
            human_mouse_move_to(page, x, y, steps=random.randint(16, 28))
            human_sleep(0.08, 0.22)
            page.mouse.down()
            time.sleep(random.uniform(0.04, 0.12))
            page.mouse.up()
            return
    except Exception:
        pass
    try:
        locator.hover(timeout=3000)
    except Exception:
        pass
    human_sleep(0.15, 0.4)
    locator.click(timeout=5000, delay=random.randint(40, 120))




def _bring_browser_to_front(page: Any) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        import pygetwindow as gw  # type: ignore

        title = ""
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        candidates = []
        for w in gw.getAllWindows():
            name = (w.title or "").lower()
            if not name or w.width < 200:
                continue
            if any(k in name for k in ("edge", "vercel", "v0", "sign up", "chrome")):
                candidates.append(w)
        if title:
            for w in candidates:
                if title[:20].lower() in (w.title or "").lower():
                    try:
                        if w.isMinimized:
                            w.restore()
                        w.activate()
                        return
                    except Exception:
                        pass
        if candidates:
            w = candidates[0]
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
            except Exception:
                pass
    except Exception:
        pass


def _element_screen_xy(locator: Any, page: Any, offset_mode: str = "center") -> tuple[float, float] | None:
    """计算元素中心（或偏移点）的屏幕坐标。"""
    try:
        if offset_mode == "edge":
            rx = random.choice([0.2, 0.8])
            ry = random.choice([0.3, 0.7])
        else:
            rx = random.uniform(0.35, 0.65)
            ry = random.uniform(0.4, 0.65)
        point = locator.evaluate(
            """(el, arg) => {
              const r = el.getBoundingClientRect();
              const px = r.left + r.width * arg.rx;
              const py = r.top + r.height * arg.ry;
              // Chromium: outer-inner 差近似顶栏；左右边框均分
              const borderX = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
              const topChrome = Math.max(0, window.outerHeight - window.innerHeight - borderX);
              const sx = (window.screenX || window.screenLeft || 0) + borderX + px;
              const sy = (window.screenY || window.screenTop || 0) + topChrome + py;
              return {
                sx, sy,
                vw: window.innerWidth,
                vh: window.innerHeight,
                visible: r.width > 0 && r.height > 0 &&
                  r.bottom > 0 && r.right > 0 &&
                  r.top < window.innerHeight && r.left < window.innerWidth
              };
            }""",
            {"rx": rx, "ry": ry},
        )
        if not point or not point.get("visible"):
            return None
        return float(point["sx"]), float(point["sy"])
    except Exception:
        return None


def os_level_click(
    locator: Any,
    page: Any,
    offset_mode: str = "center",
) -> bool:
    """操作系统真实鼠标点击（绕过 Playwright CDP Input）。"""
    try:
        import pyautogui
    except Exception as exc:
        console.print(f"[yellow]pyautogui 不可用: {exc}[/yellow]")
        return False

    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    human_sleep(0.12, 0.35)
    _bring_browser_to_front(page)
    human_sleep(0.15, 0.35)

    xy = _element_screen_xy(locator, page, offset_mode=offset_mode)
    if not xy:
        console.print("[yellow]无法计算按钮屏幕坐标[/yellow]")
        return False
    sx, sy = xy
    try:
        pyautogui.FAILSAFE = False
        # 从附近滑入，更像人手
        cur = pyautogui.position()
        mid_x = (cur.x + sx) / 2 + random.uniform(-30, 30)
        mid_y = (cur.y + sy) / 2 + random.uniform(-20, 20)
        pyautogui.moveTo(mid_x, mid_y, duration=random.uniform(0.12, 0.28))
        pyautogui.moveTo(sx, sy, duration=random.uniform(0.18, 0.4))
        human_sleep(0.06, 0.16)
        pyautogui.mouseDown()
        time.sleep(random.uniform(0.05, 0.12))
        pyautogui.mouseUp()
        console.print(f"[cyan]OS 真实鼠标点击 screen=({sx:.0f},{sy:.0f})[/cyan]")
        return True
    except Exception as exc:
        console.print(f"[yellow]OS 鼠标点击失败: {exc}[/yellow]")
        return False


def os_level_submit_continue(locator: Any, page: Any) -> bool:
    """Continue 专用：先 focus，再用系统级 Enter（不依赖坐标，半自动等价操作）。

    半自动成功路径 = 人手点按钮。坐标不准时 Enter 更稳。
    """
    try:
        import pyautogui
    except Exception:
        return False
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    _bring_browser_to_front(page)
    human_sleep(0.2, 0.45)
    # focus 用 Playwright（半自动时用户也是先看到已填邮箱的表单）
    focused = False
    try:
        locator.focus(timeout=3000)
        focused = True
    except Exception:
        try:
            locator.click(timeout=2000, force=True)
            focused = True
        except Exception:
            pass
    if not focused:
        return False
    human_sleep(0.25, 0.6)
    try:
        pyautogui.FAILSAFE = False
        pyautogui.press("enter")
        console.print("[cyan]OS 级按键: Enter 提交 Continue[/cyan]")
        return True
    except Exception as exc:
        console.print(f"[yellow]OS Enter 失败: {exc}[/yellow]")
        return False


def human_click_auto(
    locator: Any,
    page: Any | None = None,
    offset_mode: str = "center",
    prefer_os: bool = False,
    prefer_os_enter: bool = False,
    submit_strategy: str = "auto",
) -> None:
    """默认不拖系统鼠标。用页面内 click/Enter/form（开源常见做法）。

    prefer_os 仅在用户显式开启 anti_bot.os_click 时使用。
    """
    if page is not None and not prefer_os:
        page_submit_strategies(
            locator, page, strategy=submit_strategy, offset_mode=offset_mode
        )
        return
    if prefer_os and page is not None:
        # 仅显式开启时：不推荐，会拖动真实鼠标
        if prefer_os_enter and os_level_submit_continue(locator, page):
            return
        if os_level_click(locator, page, offset_mode=offset_mode):
            return
    human_click(locator, page, offset_mode=offset_mode)


def inspect_kasada_state(page: Any) -> dict[str, Any]:
    """读取页面侧 Kasada 相关信号（只读，不伪造 token）。"""
    state: dict[str, Any] = {
        "ips_script": False,
        "kpsdk_cookie_names": [],
        "document_cookie_has_kp": False,
        "local_storage_keys": [],
        "header_probe": {},
    }
    try:
        if page.locator('script[src*="ips.js"], script[src*="kpsdk"], script[src*="kasada"]').count():
            state["ips_script"] = True
    except Exception:
        pass
    try:
        cookies = page.context.cookies()
        names = []
        for c in cookies:
            n = str(c.get("name") or "")
            if (
                n.startswith("KP_")
                or n.lower().startswith("x-kpsdk")
                or "kpsdk" in n.lower()
                or n.startswith("ak_bmsc")
                or n.startswith("_abck")
            ):
                names.append(n)
        state["kpsdk_cookie_names"] = names
    except Exception:
        pass
    try:
        doc = page.evaluate(
            """() => {
              const out = {
                cookieHasKp: /(?:^|;\\s*)(KP_|x-kpsdk|kpsdk)/i.test(document.cookie || ''),
                ls: [],
                hasKpsdkGlobal: false,
              };
              try {
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i) || '';
                  if (/kp|kpsdk|kasada/i.test(k)) out.ls.push(k);
                }
              } catch (e) {}
              try {
                out.hasKpsdkGlobal = !!(window.KPSDK || window._kpsdk || window.kasada);
              } catch (e) {}
              return out;
            }"""
        )
        if isinstance(doc, dict):
            state["document_cookie_has_kp"] = bool(doc.get("cookieHasKp"))
            state["local_storage_keys"] = list(doc.get("ls") or [])
            state["has_kpsdk_global"] = bool(doc.get("hasKpsdkGlobal"))
    except Exception:
        pass
    return state


def wait_for_kasada(page: Any, timeout: float = 25.0) -> dict[str, Any]:
    """等待 Kasada 脚本/cookie 就绪。只等待真签发，绝不写入假 token。"""
    deadline = time.time() + max(3.0, timeout)
    start = time.time()
    last: dict[str, Any] = {}
    ready_hits = 0
    while time.time() < deadline:
        last = inspect_kasada_state(page)
        has_cookie = bool(last.get("kpsdk_cookie_names")) or bool(last.get("document_cookie_has_kp"))
        has_script = bool(last.get("ips_script")) or bool(last.get("has_kpsdk_global"))
        if has_script or has_cookie:
            ready_hits += 1
            # 就绪即继续：检测到脚本/cookie 后极短确认即可
            if ready_hits >= 1:
                human_sleep(0.15, 0.35)
                break
        else:
            ready_hits = 0
        # 等待期间不再做额外鼠标乱晃，加快就绪检测
        human_sleep(0.2, 0.4)
    last["waited_s"] = round(time.time() - start, 2)
    last["ready"] = bool(last.get("kpsdk_cookie_names") or last.get("ips_script") or last.get("document_cookie_has_kp"))
    if last["ready"]:
        console.print(
            f"[cyan]Kasada 就绪: script={last.get('ips_script')} "
            f"cookies={last.get('kpsdk_cookie_names') or '-'} "
            f"wait={last['waited_s']}s[/cyan]"
        )
    else:
        console.print(
            f"[yellow]未明确检测到 Kasada cookie/脚本（已等待 {last['waited_s']}s），继续流程[/yellow]"
        )
    return last


def attach_kasada_header_observer(page: Any) -> list[dict[str, Any]]:
    """监听出站请求中的 x-kpsdk-* 头（只读，用于确认脚本是否签发）。"""
    seen: list[dict[str, Any]] = []

    def on_request(req: Any) -> None:
        try:
            headers = req.headers or {}
            kpsdk = {k: v for k, v in headers.items() if "kpsdk" in k.lower() or k.lower().startswith("x-kp")}
            if kpsdk:
                seen.append(
                    {
                        "url": req.url[:180],
                        "method": req.method,
                        "headers": {k: (v[:24] + "...") if len(v) > 28 else v for k, v in kpsdk.items()},
                    }
                )
                if len(seen) <= 3:
                    console.print(f"[cyan]捕获到真 x-kpsdk 头: {list(kpsdk.keys())}[/cyan]")
        except Exception:
            pass

    try:
        page.on("request", on_request)
    except Exception:
        pass
    return seen


def pre_submit_warmup(page: Any, cfg: dict[str, Any] | None = None) -> None:
    """打开注册页后、填邮箱前的轻量预热。

    优化点：
    - 默认不做长时间鼠标乱晃 / 滚动（可配置开启）
    - Kasada 就绪即继续，不空等满超时
    """
    anti = (cfg or {}).get("anti_bot") or {}
    if not anti.get("enabled", True):
        return
    moves = int(anti.get("warmup_mouse_moves", 0))
    do_scroll = bool(anti.get("warmup_scroll", False))
    dwell = float(anti.get("warmup_dwell_s", 0.4))
    kasada_timeout = float(anti.get("kasada_wait_timeout", 12))
    console.print("[cyan]人机预热：等待 Kasada 就绪（就绪即继续）...[/cyan]")
    if anti.get("observe_kpsdk_headers", True):
        attach_kasada_header_observer(page)
    if moves > 0:
        human_mouse_wander(page, moves=moves)
    if do_scroll:
        human_scroll(page)
    if anti.get("wait_kasada", True):
        wait_for_kasada(page, timeout=kasada_timeout)
    if dwell > 0:
        time.sleep(max(0.05, dwell + random.uniform(-0.1, 0.15)))



def context_options_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """统一 new_context / persistent context 的指纹相关参数。"""
    browser_cfg = cfg.get("browser") or {}
    anti = cfg.get("anti_bot") or {}
    viewport = anti.get("viewport") or browser_cfg.get("viewport") or {
        "width": 1366,
        "height": 900,
    }
    opts: dict[str, Any] = {
        "viewport": {
            "width": int(viewport.get("width") or 1366),
            "height": int(viewport.get("height") or 900),
        },
        "locale": str(anti.get("locale") or browser_cfg.get("locale") or "zh-CN"),
        "timezone_id": str(
            anti.get("timezone_id") or browser_cfg.get("timezone_id") or "Asia/Shanghai"
        ),
        "color_scheme": str(anti.get("color_scheme") or "light"),
        "has_touch": False,
        "is_mobile": False,
        "java_script_enabled": True,
        "ignore_https_errors": False,
        "device_scale_factor": float(anti.get("device_scale_factor") or 1),
    }
    # 不伪造 UA：与真实 Edge/Chromium TLS 指纹保持一致
    return opts
