from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import httpx
from rich.console import Console

from .stealth import (
    apply_stealth,
    bitbrowser_open_args,
    context_options_from_config,
    get_playwright_api,
    merge_launch_args,
)

console = Console()


def build_bitbrowser_fingerprint(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """为新建 BitBrowser 窗口提供一致指纹；空 {} 会让客户端随机且可能不一致。

    字段按比特本地 API 常见命名；客户端不认识的字段会被忽略，不影响启动。
    """
    anti = (cfg or {}).get("anti_bot") or {}
    browser_cfg = (cfg or {}).get("browser") or {}
    viewport = anti.get("viewport") or browser_cfg.get("viewport") or {
        "width": 1366,
        "height": 900,
    }
    width = int(viewport.get("width") or 1366)
    height = int(viewport.get("height") or 900)
    # 常见 Win 桌面分辨率外壳，避免 1366x900 单独过怪
    return {
        "coreProduct": "chrome",
        "coreVersion": "",  # 交给比特匹配本地内核
        "ostype": "PC",
        "os": "Win32",
        "osVersion": "10,1,0",
        "version": "",
        "userAgent": "",  # 空=由比特按内核生成，避免 UA/TLS 不一致
        "isIpCreateTimeZone": True,
        "timeZone": str(anti.get("timezone_id") or "Asia/Shanghai"),
        "isIpCreateLanguage": False,
        "languages": str(anti.get("locale") or "zh-CN"),
        "isIpCreateDisplayLanguage": False,
        "displayLanguages": str(anti.get("locale") or "zh-CN"),
        "isIpCreatePosition": True,
        "position": "",
        "isIpCreateLanguageOpen": False,
        "openWidth": width,
        "openHeight": height,
        "resolutionType": "1",
        "resolution": f"{width} x {height}",
        "devicePixelRatio": float(anti.get("device_scale_factor") or 1),
        "fontType": "2",
        "canvas": "0",  # 0 噪声 / 跟比特默认策略
        "webGL": "0",
        "webGLMeta": "0",
        "audioContext": "0",
        "mediaDevice": "0",
        "clientRectNoiseEnabled": True,
        "speechVoices": "0",
        "hardwareConcurrency": "8",
        "deviceMemory": "8",
        "doNotTrack": "0",
        "launchArgs": "",
    }


class BitBrowserAPI:
    """比特浏览器本地 HTTP API。默认端口 54345。"""

    def __init__(self, api_url: str = "http://127.0.0.1:54345"):
        self.api_url = api_url.rstrip("/")
        # 本地 API 必须绕过 HTTP_PROXY/HTTPS_PROXY。否则 localhost 请求可能被
        # 系统代理转发，表现为“客户端已启动，但脚本始终连不上 54345”。
        self.client = httpx.Client(timeout=60.0, trust_env=False)

    def ping(self) -> bool:
        # 官方本地 API 均为 POST。只把 2xx 且业务 success 非 false 视为成功，
        # 避免 404/405 被旧逻辑误判为服务健康。
        for path, body in (
            ("/health", {}),
            ("/browser/list", {"page": 0, "pageSize": 1}),
            ("/browser/pids/all", {}),
        ):
            try:
                r = self.client.post(f"{self.api_url}{path}", json=body)
                if not r.is_success:
                    continue
                data = r.json()
                if not isinstance(data, dict) or data.get("success") is not False:
                    return True
            except Exception:
                continue
        return False

    def list_browsers(self, page: int = 0, page_size: int = 100) -> list[dict[str, Any]]:
        """读取本地 BitBrowser 窗口列表。

        BitBrowser 的列表响应通常是 ``data.list``，这里统一转换成列表，
        这样启动流程就不需要用户手工填写窗口 ID。
        """
        data = self._post(
            "/browser/list",
            {"page": max(0, int(page)), "pageSize": max(1, int(page_size))},
        )
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        raw_list = payload.get("list") if isinstance(payload, dict) else []
        if not isinstance(raw_list, list):
            return []
        return [item for item in raw_list if isinstance(item, dict) and item.get("id")]

    def running_browser_ids(self) -> set[str]:
        """返回当前已经占用的 BitBrowser 窗口 ID。"""
        for path in ("/browser/pids/alive", "/browser/pids/all"):
            try:
                data = self._post(path, {}).get("data")
                if isinstance(data, dict):
                    return {str(browser_id) for browser_id, pid in data.items() if pid}
                if isinstance(data, list):
                    return {str(item) for item in data}
            except Exception:
                continue
        return set()

    def _post(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.api_url}{path}"
        r = self.client.post(url, json=payload or {})
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError as exc:
            raise RuntimeError(f"BitBrowser API 返回的不是 JSON: {path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"BitBrowser API 返回格式异常: {path} -> {type(data).__name__}")
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"BitBrowser API error: {data}")
        return data

    def close(self) -> None:
        self.client.close()

    def update_partial(self, browser_id: str, **fields: Any) -> None:
        """只更新指定字段，避免用 /browser/update 意外覆盖已有窗口配置。"""
        payload: dict[str, Any] = {
            "ids": [browser_id],
            "browserFingerPrint": {},
            **fields,
        }
        self._post("/browser/update/partial", payload)

    def is_browser_open(self, browser_id: str) -> bool:
        """通过官方 alive 接口判断窗口进程是否仍然存活。"""
        for path in ("/browser/pids/alive", "/browser/pids"):
            try:
                response = self._post(path, {"ids": [browser_id]})
                data = response.get("data")
                if isinstance(data, dict):
                    return browser_id in data and bool(data[browser_id])
                if isinstance(data, list):
                    return browser_id in {str(item) for item in data}
            except Exception:
                continue
        return False

    def wait_until_closed(self, browser_id: str, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_browser_open(browser_id):
                return True
            time.sleep(0.5)
        return not self.is_browser_open(browser_id)

    def create_browser(
        self,
        name: str = "v0-auto",
        proxy: dict | None = None,
        clean_before_launch: bool = False,
        fingerprint: dict[str, Any] | None = None,
    ) -> str:
        """
        创建浏览器窗口。
        proxy 示例:
          {enabled: true, type: http, host: 127.0.0.1, port: 7890, username: '', password: ''}
        """
        payload: dict[str, Any] = {
            "name": name,
            "remark": "v0 auto register single key",
            "platform": "https://v0.app",
            "platformIcon": "v0.app",
            "url": "",
            # 注册任务不应继承上一次登录态。以下字段是比特浏览器官方支持的
            # “启动前清理”开关，同时关闭 Cookie/本地存储等资料同步。
            "clearCacheFilesBeforeLaunch": clean_before_launch,
            "clearCookiesBeforeLaunch": clean_before_launch,
            "clearHistoriesBeforeLaunch": clean_before_launch,
            "syncTabs": False,
            "syncCookies": False,
            "syncIndexedDb": False,
            "syncLocalStorage": False,
            "syncAuthorization": False,
            "syncHistory": False,
            "workbench": "disable",
            # 不传空指纹；交给比特生成一致环境
            "browserFingerPrint": fingerprint if fingerprint is not None else {},
        }

        proxy = proxy or {}
        if proxy.get("enabled", False):
            ptype = str(proxy.get("type") or "http").lower()
            host = str(proxy.get("host") or "127.0.0.1")
            port = int(proxy.get("port") or 7890)
            payload.update(
                {
                    "proxyMethod": 2,  # 自定义代理
                    "proxyType": ptype,  # http / socks5 / noproxy
                    "host": host,
                    "port": port,
                    "proxyUserName": str(proxy.get("username") or ""),
                    "proxyPassword": str(proxy.get("password") or ""),
                }
            )
            console.print(f"[green]比特窗口代理: {ptype}://{host}:{port}[/green]")
        else:
            payload.update(
                {
                    "proxyMethod": 2,
                    "proxyType": "noproxy",
                    "host": "",
                    "port": "",
                }
            )

        data = self._post("/browser/update", payload)
        browser_id = None
        if isinstance(data.get("data"), dict):
            browser_id = data["data"].get("id")
        if not browser_id:
            browser_id = data.get("data")
        if not browser_id:
            raise RuntimeError(f"创建比特浏览器窗口失败: {data}")
        return str(browser_id)

    def open_browser(
        self,
        browser_id: str,
        args: list[str] | None = None,
    ) -> dict:
        """打开 BitBrowser 窗口，并可传递 Chromium 启动参数。

        BitBrowser 的 ``/browser/open`` 接口支持 ``args`` 数组。这里仅在
        调用方明确提供参数时写入请求，保证旧版本客户端仍能按原请求格式工作。
        """
        payload: dict[str, Any] = {
            "id": browser_id,
            "loadExtensions": False,
        }
        normalized_args = [str(value) for value in (args or []) if str(value).strip()]
        if normalized_args:
            payload["args"] = normalized_args
        data = self._post(
            "/browser/open",
            payload,
        )
        info = data.get("data") or {}
        endpoint = info.get("ws") or info.get("wsEndpoint") or data.get("ws")
        if not endpoint:
            # Playwright 的 connect_over_cdp 同时支持 http:// 和 ws://。
            endpoint = info.get("http") or data.get("http")
            if endpoint and not str(endpoint).startswith(("http://", "https://")):
                endpoint = f"http://{endpoint}"
        if not endpoint:
            raise RuntimeError(f"打开比特浏览器失败，无 CDP 地址: {data}")
        info["endpoint"] = str(endpoint)
        return info

    def close_browser(self, browser_id: str) -> None:
        try:
            self._post("/browser/close", {"id": browser_id})
        except Exception as e:
            console.print(f"[yellow]关闭比特浏览器窗口失败: {e}[/yellow]")

    def delete_browser(self, browser_id: str) -> None:
        try:
            self._post("/browser/delete", {"id": browser_id})
        except Exception as e:
            console.print(f"[yellow]删除比特浏览器窗口失败: {e}[/yellow]")


def _find_edge_executable() -> str:
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge Beta\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge Beta\Application\msedge.exe",
    ):
        if os.path.exists(path):
            return path
    return "msedge"


def _pick_free_debug_port(preferred: int = 9333) -> int:
    import socket

    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


class BrowserSession:
    def __init__(self, cfg: dict[str, Any], playwright: Any | None = None):
        self.cfg = cfg
        self.mode = (cfg.get("browser") or {}).get("mode", "bitbrowser")
        self.headless = bool((cfg.get("browser") or {}).get("headless", False))
        self.slow_mo = int((cfg.get("browser") or {}).get("slow_mo", 50))
        self.timeout_ms = int((cfg.get("browser") or {}).get("timeout_ms", 60000))
        self._pw: Any | None = playwright
        self._owns_pw = playwright is None
        self.browser: Any | None = None
        self.context: Any | None = None
        self.page: Any | None = None
        self.bit_api: BitBrowserAPI | None = None
        self.bit_id: str | None = None
        self._created_bit = False
        # 只在手机号验证这类需要丢弃登录态的场景置为 True；普通失败仍保留窗口。
        self._delete_bit_on_stop = False
        # CDP 原生 Edge 进程与临时资料目录
        self._edge_proc: subprocess.Popen | None = None
        self._edge_profile: str | None = None
        self._playwright_backend = "unknown"

    def _bit_cleanup_fields(self) -> dict[str, Any]:
        return {
            "clearCacheFilesBeforeLaunch": True,
            "clearCookiesBeforeLaunch": True,
            "clearHistoriesBeforeLaunch": True,
            "syncTabs": False,
            "syncCookies": False,
            "syncIndexedDb": False,
            "syncLocalStorage": False,
            "syncAuthorization": False,
            "syncHistory": False,
            "workbench": "disable",
        }

    def _auto_select_bitbrowser_id(self, bit_cfg: dict[str, Any]) -> str | None:
        """自动选择窗口：优先复用已创建的 v0 窗口，正在使用的窗口交给新窗口逻辑。"""
        if bit_cfg.get("force_new_once", False):
            console.print("[yellow]上一轮触发手机号验证，本轮强制新建 BitBrowser 窗口[/yellow]")
            return None
        if not bit_cfg.get("auto_select_existing", True) or self.bit_api is None:
            return None

        # 固定邮箱窗口通常由用户先手动打开。若调用方明确允许，且当前只有一个
        # 正在运行的窗口，优先把它当作固定窗口，不要求用户先抄写 ID。
        if bit_cfg.get("select_running_as_fixed", False):
            try:
                running = self.bit_api.running_browser_ids()
                if len(running) == 1:
                    browser_id = next(iter(running))
                    console.print(f"[cyan]自动识别正在运行的固定 BitBrowser 窗口: {browser_id}[/cyan]")
                    return browser_id
            except Exception:
                pass

        try:
            profiles = self.bit_api.list_browsers()
        except Exception as exc:
            console.print(f"[yellow]读取 BitBrowser 窗口列表失败，将创建新窗口: {exc}[/yellow]")
            return None

        hint = str(bit_cfg.get("profile_name_hint") or "v0").lower()
        candidates = [
            profile
            for profile in profiles
            if not int(profile.get("isDelete") or 0)
            and (
                hint in str(profile.get("name") or "").lower()
                or hint in str(profile.get("platform") or "").lower()
            )
        ]
        candidates.sort(
            key=lambda profile: (
                str(profile.get("createdTime") or ""),
                int(profile.get("seq") or 0),
            ),
            reverse=True,
        )
        if not candidates:
            return None

        running = self.bit_api.running_browser_ids()
        for profile in candidates:
            browser_id = str(profile["id"])
            if browser_id not in running:
                console.print(f"[cyan]自动找到可复用的 BitBrowser 窗口: {browser_id}[/cyan]")
                return browser_id

        # 所有匹配窗口都在使用，返回空值让调用方创建全新的窗口。
        console.print("[yellow]匹配到的 BitBrowser 窗口均已在使用，将创建新窗口[/yellow]")
        return None

    def clear_browser_state(self, stage: str = "运行前") -> None:
        """清理登录态、缓存和站点存储；CDP 清理是比特启动清理的二次保险。"""
        if self.context is None:
            return

        errors: list[str] = []
        try:
            self.context.clear_cookies()
        except Exception as exc:
            errors.append(f"Cookie: {exc}")
        try:
            self.context.clear_permissions()
        except Exception as exc:
            errors.append(f"权限: {exc}")

        pages = list(self.context.pages)
        cdp_page = pages[0] if pages else self.context.new_page()
        try:
            cdp = self.context.new_cdp_session(cdp_page)
            cdp.send("Network.enable")
            cdp.send("Network.clearBrowserCookies")
            cdp.send("Network.clearBrowserCache")
            origins = (
                (self.cfg.get("browser") or {})
                .get("bitbrowser", {})
                .get("clear_origins")
                or [
                    "https://v0.app",
                    "https://v0.dev",
                    "https://vercel.com",
                    "https://mail.unsnow.org",
                ]
            )
            for origin in origins:
                try:
                    cdp.send(
                        "Storage.clearDataForOrigin",
                        {"origin": str(origin).rstrip("/"), "storageTypes": "all"},
                    )
                except Exception as exc:
                    errors.append(f"{origin}: {exc}")
            cdp.detach()
        except Exception as exc:
            errors.append(f"CDP: {exc}")

        # 旧标签页可能保留敏感页面或触发后台请求，只保留一个干净页。
        keep = cdp_page
        for old_page in list(self.context.pages):
            if old_page is keep:
                continue
            try:
                old_page.close()
            except Exception:
                pass
        try:
            keep.goto("about:blank", wait_until="commit", timeout=5000)
        except Exception:
            pass
        self.page = keep

        if errors:
            console.print(f"[yellow]{stage}浏览器清理完成，但有部分警告: {'; '.join(errors)}[/yellow]")
        else:
            console.print(f"[green]{stage}已清理 Cookie、缓存、权限及站点存储[/green]")

    def _ensure_playwright(self) -> Any:
        if self._pw is not None:
            return self._pw
        sync_playwright, backend = get_playwright_api()
        self._playwright_backend = backend
        self._pw = sync_playwright().start()
        self._owns_pw = True
        console.print(f"[cyan]Playwright 后端: {backend}[/cyan]")
        return self._pw

    def _anti_bot_enabled(self) -> bool:
        return bool((self.cfg.get("anti_bot") or {}).get("enabled", True))

    def _stealth_mode(self) -> str:
        """stealth 策略：

        - edge_private：默认 off（伪造 Canvas/插件会被 Kasada 当 bot）
        - bitbrowser：默认 light（只藏 webdriver）
        - 其他：full
        """
        anti = self.cfg.get("anti_bot") or {}
        explicit = str(anti.get("stealth_mode") or "").strip().lower()
        if explicit in ("full", "light", "off"):
            return explicit
        mode = str(self.mode or "").lower()
        if mode in ("edge_private", "edge_inprivate", "private_edge"):
            return "off"
        if mode == "bitbrowser":
            return "light"
        if mode in ("edge_cdp", "cdp_edge", "native_edge", "edge", "msedge"):
            return "light"
        return "full"

    def _apply_session_stealth(self, target: Any) -> None:
        if not self._anti_bot_enabled():
            return
        mode = self._stealth_mode()
        if mode == "off":
            return
        apply_stealth(target, enabled=True, mode=mode)

    def _proxy_server_dict(self) -> dict[str, str] | None:
        proxy_cfg = (self.cfg.get("browser") or {}).get("bitbrowser", {}).get("proxy") or {}
        if not proxy_cfg.get("enabled"):
            return None
        ptype = str(proxy_cfg.get("type") or "http")
        host = proxy_cfg.get("host") or "127.0.0.1"
        port = proxy_cfg.get("port") or 7890
        return {"server": f"{ptype}://{host}:{port}"}

    def _start_cdp_edge(self) -> Any:
        """启动真实 Edge 进程 + remote-debugging，再 connect_over_cdp。

        比 Playwright launch(channel=msedge) 更接近手动浏览器：
        navigator.webdriver 通常天然为 false，且无 AutomationControlled 注入痕迹。
        """
        pw = self._ensure_playwright()
        bcfg = self.cfg.get("browser") or {}
        anti = self.cfg.get("anti_bot") or {}
        proxy_cfg = (bcfg.get("bitbrowser") or {}).get("proxy") or {}

        edge = str(bcfg.get("edge_path") or "").strip() or _find_edge_executable()
        profile = tempfile.mkdtemp(prefix="v0_edge_cdp_")
        self._edge_profile = profile
        port = int(bcfg.get("cdp_port") or 0) or _pick_free_debug_port(
            int(anti.get("cdp_port_base") or 9333)
        )

        # Force InPrivate: match manual Edge private mode.
        force_inprivate = bool(bcfg.get("edge_inprivate", True))
        args = [
            edge,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--lang=zh-CN",
            "--disable-blink-features=AutomationControlled",
        ]
        if force_inprivate:
            args.append("--inprivate")
        if proxy_cfg.get("enabled"):
            ptype = str(proxy_cfg.get("type") or "http").lower()
            host = str(proxy_cfg.get("host") or "127.0.0.1")
            port_proxy = int(proxy_cfg.get("port") or 7890)
            scheme = "socks5" if ptype == "socks5" else "http"
            args.append(f"--proxy-server={scheme}://{host}:{port_proxy}")
            console.print(f"[cyan]CDP Edge proxy: {scheme}://{host}:{port_proxy}[/cyan]")
        args.append("about:blank")

        console.print(
            f"[cyan]Start Edge CDP: port={port} inprivate={force_inprivate} profile={profile}[/cyan]"
        )
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._edge_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        endpoint = f"http://127.0.0.1:{port}"
        last_err: Exception | None = None
        for attempt in range(1, 16):
            try:
                with httpx.Client(timeout=2.0, trust_env=False) as client:
                    ver = client.get(f"{endpoint}/json/version").json()
                ws = ver.get("webSocketDebuggerUrl")
                if not ws:
                    raise RuntimeError("Edge CDP 未返回 webSocketDebuggerUrl")
                self.browser = pw.chromium.connect_over_cdp(ws)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                time.sleep(0.6 + attempt * 0.1)
        if self.browser is None:
            raise RuntimeError(f"连接原生 Edge CDP 失败: {last_err}")

        if self.browser.contexts:
            # InPrivate may create extra contexts; last is usually private session
            self.context = self.browser.contexts[-1]
        else:
            self.context = self.browser.new_context(**context_options_from_config(self.cfg))
        self._apply_session_stealth(self.context)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        console.print(
            f"[green]Connected Edge CDP (inprivate={force_inprivate}, webdriver usually false)[/green]"
        )
        return self.page

    def _start_edge_private(self) -> Any:
        """本机 Edge 无痕：patchright/playwright 直接 launch，不走 CDP attach。

        手动「Edge 无痕能过」对应的是无 remote-debugging 附加路径。
        edge_cdp 仍保留作对照，但默认推荐 edge_private / edge。
        """
        pw = self._ensure_playwright()
        bcfg = self.cfg.get("browser") or {}
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
        ]
        if bool(bcfg.get("edge_inprivate", True)):
            launch_args.append("--inprivate")
        proxy_server = self._proxy_server_dict()
        ctx_opts = context_options_from_config(self.cfg)
        console.print(
            f"[cyan]启动 Edge 无痕 launch（backend={self._playwright_backend}, "
            f"inprivate={bool(bcfg.get('edge_inprivate', True))}, no CDP attach）[/cyan]"
        )
        user_data = (bcfg.get("user_data_dir") or "").strip()
        if not user_data:
            user_data = tempfile.mkdtemp(prefix="v0_edge_private_")
            self._edge_profile = user_data
        # persistent context + 临时目录 ≈ 干净无痕资料
        self.context = pw.chromium.launch_persistent_context(
            user_data_dir=user_data,
            channel="msedge",
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=launch_args,
            proxy=proxy_server,
            **ctx_opts,
        )
        # 无痕 Edge 默认不注入伪造指纹脚本
        if self._stealth_mode() != "off":
            self._apply_session_stealth(self.context)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        console.print(
            f"[green]Edge 无痕会话已就绪（非 CDP 附加, stealth={self._stealth_mode()}）[/green]"
        )
        return self.page

    def start(self) -> Any:
        self._ensure_playwright()
        bcfg = self.cfg.get("browser") or {}
        mode = str(self.mode or "bitbrowser").lower()

        if mode == "bitbrowser":
            bit_cfg = bcfg.get("bitbrowser") or {}
            api_url = bit_cfg.get("api_url") or "http://127.0.0.1:54345"
            self.bit_api = BitBrowserAPI(api_url)

            if not self.bit_api.ping():
                raise RuntimeError(
                    "连不上比特浏览器本地 API。\n"
                    f"  地址: {api_url}\n"
                    "  请先打开【比特浏览器】客户端，并确认：\n"
                    "  设置 → 本地 API → 已开启（默认 54345）"
                )

            browser_id = (bit_cfg.get("browser_id") or "").strip()
            proxy = bit_cfg.get("proxy") or {}
            if proxy.get("enabled"):
                console.print(
                    f"[cyan]BitBrowser 窗口代理: {str(proxy.get('type') or 'http').lower()}://"
                    f"{proxy.get('host') or '127.0.0.1'}:{int(proxy.get('port') or 7890)}[/cyan]"
                )
            # 手机号验证后的下一轮必须绕过显式 ID 和自动复用逻辑。
            force_new_once = bool(bit_cfg.get("force_new_once", False))
            if force_new_once:
                console.print("[yellow]已标记本轮新建窗口，跳过旧 BitBrowser 窗口[/yellow]")
                browser_id = ""
            elif not browser_id:
                browser_id = self._auto_select_bitbrowser_id(bit_cfg) or ""

            if not browser_id and bit_cfg.get("require_existing", False):
                raise RuntimeError(
                    "没有找到固定邮箱 BitBrowser 窗口，请配置 browser.bitbrowser.mail_browser_id"
                )

            # 明确指定的窗口正在使用时，不抢占当前浏览器；创建独立窗口继续任务。
            if browser_id and bit_cfg.get("new_if_busy", True) and self.bit_api.is_browser_open(browser_id):
                console.print(f"[yellow]BitBrowser 窗口 {browser_id} 已在使用，将创建新窗口[/yellow]")
                browser_id = ""

            if not browser_id:
                fp = bit_cfg.get("fingerprint")
                if not isinstance(fp, dict) or not fp:
                    fp = build_bitbrowser_fingerprint(self.cfg)
                browser_id = self.bit_api.create_browser(
                    name=f"v0-key-{int(time.time())}",
                    proxy=proxy,
                    clean_before_launch=bool(bit_cfg.get("clean_before_start", False)),
                    fingerprint=fp,
                )
                self._created_bit = True
                # 只消费一次“强制新建”标记；如果创建失败，标记保留给下一轮重试。
                bit_cfg["force_new_once"] = False
                console.print(f"[green]已创建比特浏览器窗口: {browser_id}[/green]")
            else:
                console.print(f"[cyan]使用已有比特窗口: {browser_id}[/cyan]")
                if bit_cfg.get("close_existing_before_start", True) and self.bit_api.is_browser_open(browser_id):
                    console.print("[cyan]已有窗口正在运行，先关闭以应用清理设置...[/cyan]")
                    self.bit_api.close_browser(browser_id)
                    if not self.bit_api.wait_until_closed(browser_id):
                        raise RuntimeError("已有比特窗口在 15 秒内未完全退出，请手动关闭后重试")

                update_fields = self._bit_cleanup_fields() if bit_cfg.get("clean_before_start", True) else {}
                if proxy.get("enabled"):
                    update_fields.update(
                        {
                            "proxyMethod": 2,
                            "proxyType": str(proxy.get("type") or "http").lower(),
                            "host": str(proxy.get("host") or "127.0.0.1"),
                            "port": int(proxy.get("port") or 7890),
                            "proxyUserName": str(proxy.get("username") or ""),
                            "proxyPassword": str(proxy.get("password") or ""),
                        }
                    )
                if update_fields:
                    self.bit_api.update_partial(browser_id, **update_fields)
                    console.print("[green]已更新已有窗口的代理与启动前清理设置[/green]")

            self.bit_id = browser_id
            # BitBrowser 自带指纹：只允许白名单启动参数，禁止 merge 全量 Chromium 参数
            open_args = bitbrowser_open_args(
                extra=list(bit_cfg.get("open_args") or []),
                incognito=bool(bit_cfg.get("incognito", False)),
            )
            if open_args:
                console.print(
                    f"[cyan]BitBrowser 启动参数(白名单): {' '.join(open_args)}[/cyan]"
                )
            info = self.bit_api.open_browser(browser_id, args=open_args)
            endpoint = info["endpoint"]
            connect_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    self.browser = self._pw.chromium.connect_over_cdp(endpoint)
                    connect_error = None
                    break
                except Exception as exc:
                    connect_error = exc
                    if attempt < 3:
                        time.sleep(attempt)
            if self.browser is None:
                raise RuntimeError(f"连接比特浏览器 CDP 失败: {connect_error}")
            if self.browser.contexts:
                self.context = self.browser.contexts[0]
            else:
                self.context = self.browser.new_context(**context_options_from_config(self.cfg))
            self._apply_session_stealth(self.context)
            # 固定邮箱窗口：优先复用已打开的 mail 标签，禁止 about:blank 冲掉登录页
            preserve = bool(bit_cfg.get("preserve_window", False))
            self.page = None
            if self.context.pages:
                preferred = None
                for p in self.context.pages:
                    try:
                        u = (p.url or "").lower()
                        if "mail.unsnow" in u or "unsnow" in u:
                            preferred = p
                            break
                    except Exception:
                        continue
                self.page = preferred or self.context.pages[0]
            if self.page is None:
                self.page = self.context.new_page()
            console.print("[green]已连接比特浏览器 CDP[/green]")
            if bit_cfg.get("clean_before_start", False) and not preserve:
                self.clear_browser_state("运行前")
            # 仅一次性 v0 窗口做 blank 停顿；固定邮箱窗绝对不能 blank（会触发 CF 重验）
            blank_dwell = float((self.cfg.get("anti_bot") or {}).get("blank_dwell_s", 1.2))
            if (
                blank_dwell > 0
                and self.page is not None
                and not preserve
                and not bit_cfg.get("require_existing", False)
            ):
                try:
                    self.page.goto("about:blank", wait_until="commit", timeout=5000)
                    time.sleep(blank_dwell)
                except Exception:
                    pass
        elif mode in ("edge_private", "edge_inprivate", "private_edge"):
            # 推荐：直接 launch Edge 无痕，避免 CDP attach 被 Kasada 识别
            self._start_edge_private()
        elif mode in ("edge_cdp", "cdp_edge", "native_edge"):
            # 对照：真实 Edge + remote-debugging 附加（更容易被检）
            self._start_cdp_edge()
        else:
            # mode=edge/msedge 时用本机 Edge（Playwright 每次启动都是全新临时
            # 用户目录，等效无痕）；其余情况用 Playwright 自带 Chromium。
            channel = (bcfg.get("channel") or "").strip() or None
            if mode in ("edge", "msedge") and not channel:
                channel = "msedge"
            launch_args = merge_launch_args(
                list(bcfg.get("launch_args") or []) if self._anti_bot_enabled() else [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ]
            )
            if mode in ("edge", "msedge") and bool(bcfg.get("edge_inprivate", True)):
                if "--inprivate" not in launch_args and "-inprivate" not in launch_args:
                    launch_args.append("--inprivate")
                    console.print("[cyan]Edge launch with --inprivate[/cyan]")
            proxy_server = self._proxy_server_dict()

            user_data = (bcfg.get("user_data_dir") or "").strip()
            ctx_opts = context_options_from_config(self.cfg)
            if user_data:
                self.context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=user_data,
                    channel=channel,
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    args=launch_args,
                    proxy=proxy_server,
                    **ctx_opts,
                )
                self._apply_session_stealth(self.context)
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            else:
                self.browser = self._pw.chromium.launch(
                    channel=channel,
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    args=launch_args,
                    proxy=proxy_server,
                )
                # 不再伪造 UA：真实浏览器的 UA 与 TLS 指纹一致才不易触发风控。
                self.context = self.browser.new_context(**ctx_opts)
                self._apply_session_stealth(self.context)
                self.page = self.context.new_page()

        assert self.page is not None
        self.page.set_default_timeout(self.timeout_ms)
        # 双保险：仅 light/full 再注入；off 时完全不碰页面原型
        if self._anti_bot_enabled() and self._stealth_mode() != "off":
            self._apply_session_stealth(self.page)
        return self.page

    def new_page(self) -> Any:
        assert self.context is not None
        p = self.context.new_page()
        p.set_default_timeout(self.timeout_ms)
        if self._anti_bot_enabled():
            self._apply_session_stealth(p)
        return p

    def discard(self, reason: str = "") -> None:
        """标记当前 BitBrowser 窗口为废弃，并让下一轮强制创建新窗口。"""
        if self.mode != "bitbrowser" or not self.bit_id:
            return
        self._delete_bit_on_stop = True
        bit_cfg = (self.cfg.get("browser") or {}).get("bitbrowser") or {}
        if bit_cfg.get("recreate_on_phone_verification", True):
            bit_cfg["force_new_once"] = True
        suffix = f"：{reason}" if reason else ""
        console.print(f"[yellow]当前 BitBrowser 窗口将关闭并删除{suffix}；下一轮新建窗口[/yellow]")

    def stop(self) -> None:
        bcfg = self.cfg.get("browser") or {}
        bit_cfg = bcfg.get("bitbrowser") or {}
        mode = str(self.mode or "").lower()
        if mode == "bitbrowser" and bit_cfg.get("clean_after_finish", False):
            try:
                self.clear_browser_state("运行后")
            except Exception as exc:
                console.print(f"[yellow]运行后清理失败: {exc}[/yellow]")

        try:
            if self.context and mode not in ("bitbrowser", "edge_cdp", "cdp_edge", "native_edge"):
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser and mode not in ("bitbrowser", "edge_cdp", "cdp_edge", "native_edge"):
                self.browser.close()
        except Exception:
            pass
        # edge_private uses temp profile dir
        if mode in ("edge_private", "edge_inprivate", "private_edge", "edge", "msedge") and self._edge_profile:
            try:
                shutil.rmtree(self._edge_profile, ignore_errors=True)
            except Exception:
                pass
            self._edge_profile = None
        # 先关闭 CDP 标签页，再停止 Playwright 连接，减少待处理网络响应在
        # 目标关闭后抛出 TargetClosedError 的噪音。
        if (
            mode == "bitbrowser"
            and self.context
            and not bit_cfg.get("preserve_window", False)
        ):
            for page in list(self.context.pages):
                try:
                    page.close(run_before_unload=False)
                except Exception:
                    pass
        # CDP Edge：先断开再杀进程
        if mode in ("edge_cdp", "cdp_edge", "native_edge"):
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
            if self._edge_proc is not None:
                try:
                    self._edge_proc.terminate()
                    try:
                        self._edge_proc.wait(timeout=5)
                    except Exception:
                        self._edge_proc.kill()
                except Exception:
                    pass
                self._edge_proc = None
            if self._edge_profile:
                shutil.rmtree(self._edge_profile, ignore_errors=True)
                self._edge_profile = None

        # bitbrowser 用 CDP 连接，不要 close browser 进程本身，交给 API 关窗口
        try:
            if self._pw and self._owns_pw:
                self._pw.stop()
        except Exception:
            pass

        if mode == "bitbrowser" and self.bit_api and self.bit_id:
            # 废弃窗口即使 close_after=false 也必须先关再删，避免窗口仍运行时删除失败。
            should_close = bool(bit_cfg.get("close_after", True)) or self._delete_bit_on_stop
            if should_close:
                self.bit_api.close_browser(self.bit_id)
                self.bit_api.wait_until_closed(self.bit_id)
            delete_requested = self._delete_bit_on_stop or (
                self._created_bit and bit_cfg.get("delete_after", False)
            )
            if delete_requested:
                self.bit_api.delete_browser(self.bit_id)
                console.print(f"[yellow]已删除 BitBrowser 窗口: {self.bit_id}[/yellow]")
        if self.bit_api:
            self.bit_api.close()


@contextmanager
def browser_session(
    cfg: dict[str, Any], playwright: Any | None = None
) -> Generator[BrowserSession, None, None]:
    session = BrowserSession(cfg, playwright=playwright)
    try:
        session.start()
        yield session
    finally:
        session.stop()
