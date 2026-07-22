from __future__ import annotations

import html
import re
import time
from typing import Any, Callable

from playwright.sync_api import Page
from rich.console import Console

from .github_login import GitHubAutoLogin
from .utils import extract_verification_code, normalize_email, sleep

console = Console()


class UnsnowMail:
    """
    mail.unsnow.org 临时邮箱自动化。
    该站有 Cloudflare，优先走页面操作；同时监听 XHR 以便解析邮件。
    """

    def __init__(self, page: Page, cfg: dict[str, Any]):
        self.page = page
        self.cfg = cfg
        self.base = cfg["mail"]["base_url"].rstrip("/")
        self.inbox_url = cfg["mail"].get("inbox_url") or f"{self.base}/app#inbox"
        self.emails: list[dict[str, Any]] = []
        self.current_address: str | None = None
        self._network_listener_added = False
        self._baseline_signatures: set[str] = set()
        self._verification_started_at: float | None = None
        self.github_login = GitHubAutoLogin(cfg)

    def _collect_network(self) -> None:
        if self._network_listener_added:
            return

        def on_response(resp):
            try:
                url = resp.url
                if resp.status != 200:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct and "text" not in ct:
                    return
                if any(k in url for k in ("mail", "message", "inbox", "api", "box", "letter")):
                    try:
                        data = resp.json()
                    except Exception:
                        return
                    self._ingest_payload(data)
            # 页面关闭/上下文销毁时 Playwright 可能抛出 BaseException 子类
            #（例如 CancelledError、TargetClosedError），此时直接忽略回调，
            # 避免任务已经结束后又打印一串误导性的“未处理异常”。
            except BaseException:
                return

        self.page.on("response", on_response)
        self._network_listener_added = True

    def _ingest_payload(self, data: Any) -> None:
        items: list[dict[str, Any]] = []

        def collect(value: Any, depth: int = 0) -> None:
            if depth > 4:
                return
            if isinstance(value, list):
                for child in value:
                    collect(child, depth + 1)
                return
            if not isinstance(value, dict):
                return
            if any(k in value for k in ("subject", "from", "sender", "body", "text", "content", "html")):
                items.append(value)
            for key in ("data", "mails", "messages", "items", "list", "result", "records"):
                if key in value:
                    collect(value[key], depth + 1)

        collect(data)
        for it in items:
            body = (
                it.get("body")
                or it.get("text")
                or it.get("content")
                or it.get("html")
                or it.get("preview")
                or ""
            )
            subject = it.get("subject") or it.get("title") or ""
            sender = it.get("from") or it.get("sender") or it.get("fromAddress") or ""
            rec = {
                "subject": str(subject),
                "from": str(sender),
                "body": html.unescape(re.sub(r"<[^>]+>", " ", str(body))),
                "observed_at": time.time(),
                "raw": it,
            }
            # 去重
            sig = self._mail_signature(rec)
            if not any(self._mail_signature(x) == sig for x in self.emails):
                self.emails.append(rec)

    @staticmethod
    def _mail_signature(mail: dict[str, Any]) -> str:
        raw = mail.get("raw") or {}
        message_id = ""
        if isinstance(raw, dict):
            message_id = str(raw.get("id") or raw.get("messageId") or raw.get("uuid") or "")
        if message_id:
            return f"id:{message_id}"
        return f"{mail.get('subject', '')}|{mail.get('from', '')}|{str(mail.get('body', ''))[:160]}"

    def _mail_ui_ready(self) -> bool:
        """收件箱 UI 已可用（已过 CF / 已登录），避免误判。"""
        try:
            url = (self.page.url or "").lower()
            if not url.startswith(self.base.lower()):
                return False
            title = (self.page.title() or "").lower()
            body = ""
            try:
                body = (self.page.inner_text("body") or "")[:2500].lower()
            except Exception:
                body = ""
            # 明确仍在 CF 挑战页
            hard_cf = (
                "just a moment",
                "checking your browser",
                "performing security verification",
                "security service to protect",
                "attention required",
                "cf-browser-verification",
            )
            if any(m in title or m in body for m in hard_cf):
                return False
            if "ray id" in body and "cloudflare" in body and "inbox" not in body:
                return False
            ready_markers = (
                "sign out",
                "logout",
                "inbox",
                "mailboxes",
                "replace mailbox",
                "recall mailbox",
                "current address",
                "listening",
                "收件箱",
                "退出",
            )
            if any(m in body for m in ready_markers):
                return True
            if "unsnow" in title and "inbox" in title:
                return True
            return False
        except Exception:
            return False

    def _try_pass_cf_challenge(self) -> bool:
        """尝试点击 Cloudflare / Turnstile 人机验证（勾选框或 Verify 按钮）。

        无法保证 100% 自动过（部分挑战需真实人机），但常见 checkbox 可点。
        返回是否执行了点击动作。
        """
        clicked = False
        # 1) 页面上的 Verify / 确认 按钮
        for pat in (
            r"^Verify you are human$",
            r"^Verify$",
            r"^I am human$",
            r"^Continue$",
            r"^确认$",
            r"^验证$",
            r"Verify you are human",
            r"确认您是真人",
            r"人机验证",
        ):
            try:
                btn = self.page.get_by_role("button", name=re.compile(pat, re.I))
                if btn.count() and btn.first.is_visible(timeout=400):
                    btn.first.click(timeout=2000)
                    console.print(f"[green]已点击人机验证按钮: {pat}[/green]")
                    sleep(1.2)
                    clicked = True
                    break
            except Exception:
                continue
            try:
                loc = self.page.get_by_text(re.compile(pat, re.I))
                if loc.count() and loc.first.is_visible(timeout=400):
                    loc.first.click(timeout=2000)
                    console.print(f"[green]已点击人机验证文本: {pat}[/green]")
                    sleep(1.2)
                    clicked = True
                    break
            except Exception:
                continue

        # 2) 主页面 checkbox
        if not clicked:
            for sel in (
                'input[type="checkbox"]',
                'label:has(input[type="checkbox"])',
                ".cf-turnstile",
                "#challenge-stage input",
                '[id*="cf-" i] input[type="checkbox"]',
            ):
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() and loc.is_visible(timeout=400):
                        loc.click(timeout=2000)
                        console.print(f"[green]已点击人机验证勾选: {sel}[/green]")
                        sleep(1.2)
                        clicked = True
                        break
                except Exception:
                    continue

        # 3) Cloudflare / Turnstile iframe 内勾选
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = []
        for frame in frames:
            try:
                furl = (frame.url or "").lower()
            except Exception:
                furl = ""
            if not any(
                k in furl
                for k in (
                    "challenges.cloudflare.com",
                    "turnstile",
                    "cf-chl",
                    "cloudflare",
                )
            ) and furl not in ("", "about:blank"):
                # 仍尝试 frame 内 checkbox（有些无完整 url）
                pass
            try:
                # checkbox
                cb = frame.locator('input[type="checkbox"], label, body')
                # 优先 checkbox
                box = frame.locator('input[type="checkbox"]').first
                if box.count() and box.is_visible(timeout=300):
                    box.click(timeout=2000)
                    console.print("[green]已点击 CF iframe 内 checkbox[/green]")
                    sleep(1.5)
                    clicked = True
                    break
                # 点 body 中心（部分 turnstile 可点区域）
                body = frame.locator("body")
                if body.count():
                    box2 = body.bounding_box()
                    if box2 and box2.get("width", 0) > 10:
                        # 左侧勾选区
                        frame.click(
                            "body",
                            position={"x": min(30, box2["width"] * 0.15), "y": box2["height"] * 0.5},
                            timeout=2000,
                        )
                        console.print("[green]已点击 CF iframe 勾选区域[/green]")
                        sleep(1.5)
                        clicked = True
                        break
            except Exception:
                continue

        # 4) 直接找 turnstile iframe 元素点一下
        if not clicked:
            for sel in (
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="Widget" i]',
                'iframe[title*="cloudflare" i]',
            ):
                try:
                    iframe = self.page.locator(sel).first
                    if iframe.count() and iframe.is_visible(timeout=400):
                        box = iframe.bounding_box()
                        if box:
                            # 点 iframe 左侧（勾选框常见位置）
                            self.page.mouse.click(
                                box["x"] + min(28, box["width"] * 0.12),
                                box["y"] + box["height"] * 0.5,
                            )
                            console.print(f"[green]已鼠标点击 CF iframe 区域: {sel}[/green]")
                            sleep(1.5)
                            clicked = True
                            break
                except Exception:
                    continue
        return clicked

    def open_and_pass_cf(self, max_wait: int = 180) -> None:
        """打开/附着邮箱页。已就绪则绝不重复导航/重载；CF 只等待不轮询刷新。"""
        console.print(f"[cyan]打开邮箱: {self.inbox_url}[/cyan]")
        self._collect_network()
        # 优先附着同 context 已打开的邮箱标签
        try:
            ctx = self.page.context
            for p in ctx.pages:
                try:
                    u = (p.url or "").lower()
                    if "mail.unsnow" in u or u.startswith(self.base.lower()):
                        self.page = p
                        break
                except Exception:
                    continue
        except Exception:
            pass

        already = False
        try:
            cur = (self.page.url or "").lower()
            body_preview = ""
            try:
                body_preview = (self.page.inner_text("body") or "")[:2500]
            except Exception:
                body_preview = ""
            # 已登录收件箱：直接返回，禁止 reload / goto
            if cur.startswith(self.base.lower()) and (
                self._mail_ui_ready()
                or self._looks_authenticated(body_preview)
                or self._is_inbox_url(body_preview)
            ):
                already = True
                console.print(
                    "[green]邮箱页已经就绪（已登录），不重复导航/全自动硬刷 CF[/green]"
                )
            elif cur.startswith(self.base.lower()) and any(
                k in (self.page.title() or "").lower() for k in ("inbox", "unsnow")
            ):
                already = True
                console.print("[green]邮箱标签标题 Inbox，直接复用[/green]")
        except Exception:
            already = False

        if already:
            sleep(0.3)
            return

        # 仅当完全不在邮箱站时才 goto 一次；禁止对已在邮箱站的页面 reload（CF 会更卡）
        try:
            cur = (self.page.url or "").lower()
        except Exception:
            cur = ""
        if not cur.startswith(self.base.lower()) and "mail.unsnow" not in cur:
            try:
                self.page.goto(self.inbox_url, wait_until="domcontentloaded")
            except Exception:
                pass
        else:
            console.print(
                "[yellow]已在邮箱站但 UI 未就绪，仅等待（不 reload，避免触发/加重 CF）[/yellow]"
            )

        deadline = time.time() + max_wait
        ready = False
        challenge_notified = False
        soft_ok_since = None
        last_progress = time.time()
        while time.time() < deadline:
            if self._mail_ui_ready():
                ready = True
                break
            title = ""
            content = ""
            try:
                title = (self.page.title() or "").lower()
            except Exception:
                pass
            try:
                content = (self.page.inner_text("body") or "")[:1200].lower()
            except Exception:
                pass
            # 已登录信号出现 → 立刻当就绪
            if self._looks_authenticated(content):
                ready = True
                console.print("[green]等待中检测到已登录信号，停止 CF 等待[/green]")
                break

            hard_cf = (
                "just a moment",
                "checking your browser",
                "performing security verification",
                "security service to protect",
                "attention required",
                "cf-browser-verification",
                "verify you are human",
                "needs to review the security",
            )
            on_hard_cf = any(m in title or m in content for m in hard_cf) or (
                "cloudflare" in title and "inbox" not in title and "unsnow" not in title
            )
            if on_hard_cf:
                if not challenge_notified:
                    console.print(
                        "[yellow]Cloudflare 人机验证中：尝试自动点击验证按钮/勾选框（不刷新）...[/yellow]"
                    )
                    challenge_notified = True
                # 尝试点击人机验证，禁止 reload
                try:
                    self._try_pass_cf_challenge()
                except Exception:
                    pass
                sleep(2)
                continue

            if (self.page.url or "").lower().startswith(self.base.lower()) or "mail.unsnow" in (
                self.page.url or ""
            ).lower():
                if soft_ok_since is None:
                    soft_ok_since = time.time()
                if "inbox" in title or "unsnow" in title:
                    ready = True
                    break
                # 已在邮箱域 12s 无硬 CF → 放行，避免死等
                if time.time() - soft_ok_since >= 12:
                    console.print(
                        "[yellow]邮箱域无硬 CF 已等待足够，放行进入登录态检查[/yellow]"
                    )
                    ready = True
                    break
            sleep(1)

        if not ready:
            try:
                if (self.page.url or "").lower().startswith(self.base.lower()) or "mail.unsnow" in (
                    self.page.url or ""
                ).lower():
                    console.print(
                        "[yellow]CF 等待超时，但已在邮箱站，继续尝试登录态[/yellow]"
                    )
                    ready = True
            except Exception:
                pass
        if not ready:
            raise TimeoutError(
                "邮箱页 Cloudflare 验证在等待时间内未通过（未强制刷新以避免卡死）"
            )
        sleep(0.5)


    def wait_until_authenticated(self, timeout: int | None = None) -> None:
        """等待邮箱站完成 GitHub 登录。已登录则直接返回，绝不重复点登录。"""
        timeout = timeout or int(self.cfg["mail"].get("auth_timeout", 300))
        deadline = time.time() + timeout
        prompted = False
        clicked_github = False

        self._select_mail_page_if_available()
        try:
            body0 = self._read_mail_text()
        except Exception:
            body0 = ""
        if self._looks_authenticated(body0) or self._is_inbox_url(body0):
            console.print("[green]邮箱站已确认登录，跳过 GitHub 登录[/green]")
            return
        try:
            cur = (self.page.url or "").lower()
            if "mail.unsnow" in cur and self._looks_authenticated(body0):
                console.print("[green]收件箱已打开且已登录，跳过 GitHub 登录[/green]")
                return
        except Exception:
            pass

        while time.time() < deadline:
            self._select_mail_page_if_available()
            try:
                body = self._read_mail_text()
            except Exception:
                body = ""
            low = body.lower()
            if self._looks_authenticated(low) or self._is_inbox_url(low):
                console.print("[green]邮箱站已确认登录，跳过 GitHub 登录[/green]")
                return

            still_need_login = any(
                m in low
                for m in (
                    "sign in with github",
                    "continue with github",
                    "使用 github 登录",
                    "通过 github 登录",
                    "login with github",
                )
            )
            github_page = self._find_github_page()
            if github_page and still_need_login:
                self.github_login.login_if_needed(github_page)

            if still_need_login and not clicked_github:
                clicked_github = self._click_visible(
                    [
                        (
                            "button",
                            r"(?:Continue|Sign in|Log in).*GitHub|GitHub.*(?:登录|登入|Sign in|Log in)",
                        ),
                        (
                            "link",
                            r"(?:Continue|Sign in|Log in).*GitHub|GitHub.*(?:登录|登入|Sign in|Log in)",
                        ),
                        ("text", r"使用 GitHub 登录|通过 GitHub 登录|Sign in with GitHub"),
                    ]
                )
            if not prompted and still_need_login:
                console.print(
                    "[yellow]请在邮箱窗口完成 GitHub 登录；登录完成后脚本自动继续...[/yellow]"
                )
                prompted = True
            sleep(2)
        raise TimeoutError(f"邮箱站 GitHub 登录等待超时（{timeout}s）")


    def _select_mail_page_if_available(self) -> None:
        try:
            for page in self.page.context.pages:
                if self.base.lower() in (page.url or "").lower():
                    self.page = page
                    self._collect_network()
                    return
        except Exception:
            return

    def _find_github_page(self) -> Page | None:
        try:
            for page in self.page.context.pages:
                if "github.com" in (page.url or "").lower():
                    return page
        except Exception:
            return None
        return None

    @staticmethod
    def _looks_authenticated(text: str) -> bool:
        """已登录收件箱判定：有退出/Replace/当前地址即可，勿再点 GitHub 登录。"""
        low = (text or "").lower()
        login_markers = (
            "sign in with github",
            "continue with github",
            "使用 github 登录",
            "通过 github 登录",
            "login with github",
        )
        strong = (
            "sign out",
            "log out",
            "logout",
            "退出登录",
            "replace mailbox",
            "替换邮箱",
            "current address",
            "recall mailbox",
            "available mailboxes",
        )
        if any(m in low for m in strong):
            return True
        weak = ("inbox", "收件箱", "mailbox", "listening", "temporary inbox")
        if any(m in low for m in weak) and not any(m in low for m in login_markers):
            return True
        return False


    def _is_inbox_url(self, text: str = "") -> bool:
        url = (self.page.url or "").lower()
        text = text.lower()
        login_markers = (
            "sign in with github",
            "continue with github",
            "使用 github 登录",
            "github 登录",
            "skip to content",
        )
        return (
            self.base.lower() in url
            and "#inbox" in url
            and bool(text.strip())
            and not any(marker in text for marker in login_markers)
        )

    def create_or_set_address(self, requested_email: str | None = None) -> str:
        """选择临时域名并读取站点生成的真实邮箱地址。"""
        if requested_email:
            raise ValueError(
                "当前邮箱站点的地址由登录后的邮箱页面生成，不支持通过 --email/--local-part 自定义"
            )
        # BitBrowser profile 里可能同时存在邮箱页和控制台页；认证流程结束后
        # 再次明确选中邮箱页，避免后续按钮查找落到控制台页面。
        self._select_mail_page_if_available()
        domain = str(self.cfg["mail"]["domain"]).strip().lower()
        mail_cfg = self.cfg["mail"]
        attempts = max(1, int(mail_cfg.get("address_create_attempts", 5)))
        verify_timeout = max(5, int(mail_cfg.get("address_verify_timeout", 45)))
        retry_interval = max(0.5, float(mail_cfg.get("address_retry_interval", 2)))
        last_hint = ""

        for attempt in range(1, attempts + 1):
            # 已有活动邮箱时，页面按钮会变成 Replace mailbox。每一轮都重新读取旧地址，
            # 防止上一轮替换失败后把旧地址误当成新地址。
            previous_addresses = set(self._extract_generated_addresses(domain))
            has_active = self._has_active_mailbox()
            console.print(f"[cyan]准备创建新的临时邮箱（第 {attempt}/{attempts} 轮）[/cyan]")

            open_specs = (
                [
                    ("button", r"^Replace mailbox$|^替换邮箱$"),
                    ("link", r"^Replace mailbox$|^替换邮箱$"),
                    ("text", r"^Replace mailbox$|^替换邮箱$"),
                ]
                if has_active
                else []
            )
            open_specs.extend(
                [
                    ("button", r"^New mailbox$|^新建邮箱$"),
                    ("link", r"^New mailbox$|^新建邮箱$"),
                    ("text", r"^New mailbox$|^新建邮箱$"),
                ]
            )
            if not has_active:
                # 页面文本读取失败时仍给 Replace mailbox 一次兜底机会，
                # 防止活动邮箱存在但检测阶段遗漏。
                open_specs.extend(
                    [
                        ("button", r"^Replace mailbox$|^替换邮箱$"),
                        ("link", r"^Replace mailbox$|^替换邮箱$"),
                        ("text", r"^Replace mailbox$|^替换邮箱$"),
                    ]
                )
            opened = self._click_visible(open_specs)
            if not opened:
                console.print("[yellow]本轮未找到 New/Replace mailbox 按钮[/yellow]")
                self._close_mailbox_dialog()
                if attempt < attempts:
                    sleep(retry_interval)
                    continue
                break

            console.print(
                "[green]已打开 Replace mailbox 对话框[/green]"
                if has_active
                else "[green]已打开邮箱创建/替换对话框[/green]"
            )
            sleep(0.8)
            console.print(f"[cyan]选择临时邮箱域名: {domain}[/cyan]")
            if not self._select_domain(domain):
                console.print("[yellow]本轮域名选择失败，准备重新打开邮箱替换流程[/yellow]")
                self._close_mailbox_dialog()
                if attempt < attempts:
                    sleep(retry_interval)
                    continue
                break

            # 某些版本在选择域名后就已经生成地址，先检查一次，避免再次点击页面顶部
            # 的 Replace mailbox 入口，把刚刚成功的结果重新打开成弹窗。
            actual_email = self._wait_for_new_address(
                domain, previous_addresses, min(1.5, verify_timeout)
            )
            created = False
            if not actual_email:
                created = self._click_mailbox_confirm()
            if not actual_email and not created:
                console.print("[yellow]未找到标准确认按钮，继续检查是否已直接生成新邮箱[/yellow]")
                actual_email = self._wait_for_new_address(
                    domain, previous_addresses, min(5, verify_timeout)
                )
            elif created:
                console.print("[green]已确认创建/替换邮箱，等待新地址返回[/green]")
                actual_email = self._wait_for_new_address(domain, previous_addresses, verify_timeout)

            if actual_email:
                self.current_address = actual_email
                self.emails.clear()
                self._baseline_signatures.clear()
                console.print(f"[green]邮箱站点已生成: {actual_email}[/green]")
                return actual_email

            last_hint = re.sub(r"\s+", " ", self.dump_page_hint())[:400]
            console.print("[yellow]本轮未检测到新邮箱地址，准备重新选择邮箱[/yellow]")
            self._close_mailbox_dialog()
            if attempt < attempts:
                sleep(retry_interval)

        raise RuntimeError(
            f"邮箱站点连续 {attempts} 轮未生成新的真实地址（域名 {domain}）。"
            f"请确认 GitHub 登录、域名选择和 Replace mailbox 操作。页面摘要: {last_hint or '（空）'}"
        )

    def _click_mailbox_confirm(self) -> bool:
        """点击邮箱创建弹窗的确认按钮，兼容按钮、链接、submit 和 role=button。"""
        action_pattern = re.compile(
            r"^(?:Create mailbox|Create|Generate mailbox|Generate|Replace mailbox|Replace|"
            r"Confirm|Continue|Submit|Save|创建邮箱|创建|生成邮箱|生成|替换邮箱|替换|"
            r"确认|继续|提交|保存)$",
            re.I,
        )
        modal_hint = False
        for selector in (
            '[role="dialog"]:visible',
            '[aria-modal="true"]:visible',
            '[class*="modal" i]:visible',
            '[class*="dialog" i]:visible',
            'select[name*="domain" i]:visible',
            'input[type="submit"]:visible',
        ):
            try:
                if self.page.locator(selector).count():
                    modal_hint = True
                    break
            except Exception:
                continue

        # 先查弹窗内部，避免把页面底层的 Replace mailbox 按钮再次点击。
        for selector in (
            '[role="dialog"] button',
            '[role="dialog"] a',
            '[role="dialog"] [role="button"]',
            '[role="dialog"] input[type="submit"]',
        ):
            try:
                locs = self.page.locator(selector)
                for index in range(min(locs.count(), 80) - 1, -1, -1):
                    loc = locs.nth(index)
                    if not loc.is_visible(timeout=300):
                        continue
                    labels: list[str] = []
                    for attr in ("aria-label", "title", "value", "data-testid"):
                        value = loc.get_attribute(attr)
                        if value:
                            labels.append(value)
                    try:
                        labels.append(loc.inner_text(timeout=200))
                    except Exception:
                        pass
                    label = re.sub(r"\s+", " ", " ".join(labels)).strip()
                    if not action_pattern.match(label):
                        continue
                    if not modal_hint and re.match(r"^(?:Replace mailbox|Replace|替换邮箱|替换)$", label, re.I):
                        # 页面顶部的 Replace mailbox 是打开弹窗的入口，不是确认按钮。
                        continue
                    if re.search(r"^(?:Close|Cancel|关闭|取消)$", label, re.I):
                        continue
                    loc.click(timeout=2500)
                    return True
            except Exception:
                continue

        # 页面没有 role=dialog 时，按 DOM 后部优先查找弹窗新增的按钮。
        for selector in (
            'button[type="submit"]',
            'input[type="submit"]',
            'button',
            '[role="button"]',
            'a',
        ):
            try:
                locs = self.page.locator(selector)
                for index in range(min(locs.count(), 80) - 1, -1, -1):
                    loc = locs.nth(index)
                    if not loc.is_visible(timeout=300):
                        continue
                    labels: list[str] = []
                    for attr in ("aria-label", "title", "value", "data-testid"):
                        value = loc.get_attribute(attr)
                        if value:
                            labels.append(value)
                    try:
                        labels.append(loc.inner_text(timeout=200))
                    except Exception:
                        pass
                    label = re.sub(r"\s+", " ", " ".join(labels)).strip()
                    if not action_pattern.match(label):
                        continue
                    if re.search(r"^(?:Close|Cancel|关闭|取消)$", label, re.I):
                        continue
                    loc.click(timeout=2500)
                    return True
            except Exception:
                continue
        return False

    def _wait_for_new_address(
        self, domain: str, previous_addresses: set[str], timeout: float
    ) -> str | None:
        deadline = time.time() + max(0.5, timeout)
        synced = False
        while time.time() < deadline:
            candidates = self._extract_generated_addresses(domain)
            new_candidates = [
                c for c in candidates if c not in previous_addresses
            ]
            if new_candidates:
                return new_candidates[-1]
            # 无旧地址时，直接采用页面第一个
            if not previous_addresses and candidates:
                return candidates[-1]
            # 中途点一次 Sync，帮助地址刷新（仅一次）
            if not synced and time.time() + 2 < deadline:
                try:
                    # 换号弹窗也可能弹出 CF
                    self._try_pass_cf_challenge()
                except Exception:
                    pass
                try:
                    self._click_visible(
                        [
                            ("button", r"^Sync$|^刷新$|^同步$"),
                            ("text", r"^Sync$"),
                        ]
                    )
                    synced = True
                except Exception:
                    pass
            sleep(0.5)
        # 兜底：若页面仍显示 CURRENT ADDRESS 且在目标域，返回当前地址
        # （Replace 有时 UI 未变 local，但地址可用；调用方会据此继续）
        try:
            body = self._read_mail_text()
        except Exception:
            body = ""
        m = re.search(
            rf"current\s+address\s*([a-z0-9][a-z0-9._-]{{2,63}}@{re.escape(domain)})",
            body,
            re.I,
        )
        if m:
            email = m.group(1).lower()
            if email not in previous_addresses or not previous_addresses:
                console.print(
                    f"[yellow]未检测到相对旧地址的变化，回退使用 CURRENT ADDRESS: {email}[/yellow]"
                )
                return email
        return None


    def _close_mailbox_dialog(self) -> None:
        """失败重试前关闭可能残留的邮箱弹窗，避免下一轮点击被遮挡。"""
        try:
            clicked = self._click_visible(
                [
                    ("button", r"^Close$|^Cancel$|^关闭$|^取消$|^×$"),
                    ("link", r"^Close$|^Cancel$|^关闭$|^取消$|^×$"),
                ]
            )
            if clicked:
                sleep(0.3)
                return
        except Exception:
            pass
        try:
            self.page.keyboard.press("Escape")
        except Exception:
            pass

    def _has_active_mailbox(self) -> bool:
        """判断页面是否已经存在活动邮箱，兼容中英文界面和按钮文本。"""
        try:
            for kind, pattern in (
                ("button", r"^Replace mailbox$|^替换邮箱$"),
                ("link", r"^Replace mailbox$|^替换邮箱$"),
                ("text", r"^Replace mailbox$|^替换邮箱$"),
            ):
                if kind == "button":
                    loc = self.page.get_by_role("button", name=re.compile(pattern, re.I))
                elif kind == "link":
                    loc = self.page.get_by_role("link", name=re.compile(pattern, re.I))
                else:
                    loc = self.page.get_by_text(re.compile(pattern, re.I))
                if loc.count() and loc.first.is_visible(timeout=400):
                    return True
        except Exception:
            pass
        try:
            body = self._read_mail_text().lower()
        except Exception:
            body = ""
        if "replace mailbox" in body or "替换邮箱" in body:
            return True
        return bool(
            re.search(r"current\s+address", body)
            and re.search(r"\b[a-z0-9][a-z0-9._-]{2,63}@[^\s<]+", body, re.I)
            and any(marker in body for marker in ("listening", "expires", "status"))
        )

    def _select_domain(self, domain: str) -> bool:
        deadline = time.time() + 10
        while time.time() < deadline:
            for sel in ("select[name*='domain' i]", "select[id*='domain' i]", "select"):
                try:
                    dropdowns = self.page.locator(sel)
                    for i in range(min(dropdowns.count(), 10)):
                        dropdown = dropdowns.nth(i)
                        if not dropdown.is_visible(timeout=500):
                            continue
                        try:
                            dropdown.select_option(label=domain)
                        except Exception:
                            try:
                                dropdown.select_option(value=domain)
                            except Exception:
                                continue
                        console.print(f"[green]已选择域名: {domain}[/green]")
                        return True
                except Exception:
                    continue

            # 非原生 select 可能需要先点击下拉框，等待选项渲染。
            if self._click_visible(
                [
                    ("button", r"domain|域名|邮箱后缀"),
                    ("combobox", rf"{re.escape(domain)}"),
                ]
            ):
                sleep(0.3)

        # 非原生 select 的下拉框：先点域名选择器，再点精确域名文本。
        self._click_visible(
            [
                ("button", r"domain|域名|邮箱后缀"),
                ("text", r"domain|域名|邮箱后缀"),
            ]
        )
        if self._click_visible(
            [
                ("option", rf"^{re.escape(domain)}$"),
                ("text", rf"^{re.escape(domain)}$"),
            ]
        ):
            console.print(f"[green]已选择域名: {domain}[/green]")
            return True
        else:
            console.print(f"[yellow]未找到域名下拉控件，请在邮箱页面手动选择 {domain}[/yellow]")
        return False

    def _extract_generated_address(self, domain: str) -> str | None:
        candidates = self._extract_generated_addresses(domain)
        return candidates[-1] if candidates else None

    def _extract_generated_addresses(self, domain: str) -> list[str]:
        structured_values: list[str] = []
        for sel in (
            "input[readonly]",
            "input[disabled]",
            "[data-email]",
            "[data-address]",
            "[class*='address' i]",
            "[class*='email' i]",
        ):
            try:
                locs = self.page.locator(sel)
                for i in range(min(locs.count(), 30)):
                    loc = locs.nth(i)
                    for attr in ("value", "data-email", "data-address"):
                        value = loc.get_attribute(attr)
                        if value:
                            structured_values.append(value)
                    try:
                        structured_values.append(loc.inner_text(timeout=200))
                    except Exception:
                        pass
            except Exception:
                continue

        pattern = re.compile(
            rf"\b([a-z0-9][a-z0-9._-]{{2,63}})@{re.escape(domain)}\b", re.I
        )
        def find_candidates(values: list[str]) -> list[str]:
            found: list[str] = []
            for value in values:
                for match in pattern.finditer(str(value)):
                    try:
                        candidate = normalize_email(
                            f"{match.group(1)}@{domain}", expected_domain=domain
                        )
                    except ValueError:
                        continue
                    if candidate not in found:
                        found.append(candidate)
            return found

        candidates = find_candidates(structured_values)
        try:
            body = self._read_mail_text()
        except Exception:
            body = ""
        for candidate in find_candidates([body]):
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def logout(self) -> bool:
        """点击邮箱站点的退出登录，不清理浏览器缓存。"""
        try:
            clicked = self._click_visible(
                [
                    ("button", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                    ("link", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                    ("text", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                ]
            )
            if not clicked:
                clicked_menu = self._click_visible(
                    [
                        ("button", r"account|profile|user|账户|账号|个人中心"),
                        ("text", r"account|profile|user|账户|账号|个人中心"),
                    ]
                )
                if not clicked_menu:
                    for sel in (
                        "button[aria-label*='account' i]",
                        "button[aria-label*='profile' i]",
                        "button[aria-label*='user' i]",
                        "[data-testid*='account' i]",
                        "[data-testid*='user' i]",
                    ):
                        try:
                            loc = self.page.locator(sel).first
                            if loc.count() and loc.is_visible(timeout=400):
                                loc.click(timeout=1500)
                                break
                        except Exception:
                            continue
                clicked = self._click_visible(
                    [
                        ("button", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                        ("link", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                        ("text", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                    ]
                )
            if clicked:
                sleep(1)
                console.print("[green]邮箱站点已退出登录[/green]")
            else:
                console.print("[yellow]当前页面没有可见的邮箱退出按钮，将由浏览器会话关闭结束本次任务[/yellow]")
            return clicked
        except Exception as exc:
            console.print(f"[yellow]邮箱站点退出登录失败: {exc}[/yellow]")
            return False

    def _click_visible(self, specs: list[tuple[str, str]]) -> bool:
        for kind, pattern in specs:
            try:
                if kind == "button":
                    loc = self.page.get_by_role("button", name=re.compile(pattern, re.I))
                elif kind == "link":
                    loc = self.page.get_by_role("link", name=re.compile(pattern, re.I))
                elif kind == "option":
                    loc = self.page.get_by_role("option", name=re.compile(pattern, re.I))
                elif kind == "combobox":
                    loc = self.page.get_by_role("combobox", name=re.compile(pattern, re.I))
                else:
                    loc = self.page.get_by_text(re.compile(pattern, re.I))
                if loc.count() and loc.first.is_visible(timeout=600):
                    loc.first.click(timeout=2000)
                    return True
            except Exception:
                continue

        # 某些前端按钮的可访问树会短暂失效，但原生 DOM 已经可见；按文本/属性
        # 做一次兜底点击，兼容 Replace mailbox 弹窗的过渡状态。
        try:
            matcher = re.compile(pattern, re.I)
        except Exception:
            matcher = None
        if matcher:
            for selector in ("button:visible", "[role='button']:visible", "a:visible"):
                try:
                    locs = self.page.locator(selector)
                    for index in range(min(locs.count(), 80)):
                        loc = locs.nth(index)
                        labels = [
                            loc.get_attribute("aria-label") or "",
                            loc.get_attribute("title") or "",
                            loc.get_attribute("value") or "",
                            loc.inner_text(timeout=200) or "",
                        ]
                        label = re.sub(r"\s+", " ", " ".join(labels)).strip()
                        if matcher.search(label):
                            loc.click(timeout=2000)
                            return True
                except Exception:
                    continue
        return False

    def _address_is_active(self, email: str) -> bool:
        local, _, domain = email.lower().partition("@")
        body_text = ""
        try:
            body_text = self.page.inner_text("body")
        except Exception:
            pass
        strong_texts: list[str] = [body_text]
        for sel in (
            "[data-email]",
            "[data-address]",
            "[class*='address' i]",
            "[class*='email' i]",
            "input[readonly]",
            "input[disabled]",
        ):
            try:
                locs = self.page.locator(sel)
                for i in range(min(locs.count(), 30)):
                    loc = locs.nth(i)
                    value = loc.get_attribute("value") or loc.get_attribute("data-email") or loc.get_attribute("data-address")
                    if value:
                        strong_texts.append(value)
                    try:
                        strong_texts.append(loc.inner_text(timeout=150))
                    except Exception:
                        pass
            except Exception:
                continue
        blob = " ".join(strong_texts).lower()
        if email.lower() in blob:
            return True
        # 一些 UI 将前缀、@ 和域名拆成三个节点；必须同时出现收件箱语义，
        # 不能只凭输入框里仍留着 local 就判定创建成功。
        return (
            local in blob
            and domain in blob
            and any(word in blob for word in ("inbox", "收件箱", "mailbox", "邮件"))
        )

    def prepare_for_verification(self) -> None:
        """记录当前收件箱基线，后续只从新邮件中提取本次 OTP。"""
        try:
            self._refresh_inbox(open_latest=False)
        except Exception:
            pass
        self._baseline_signatures = {self._mail_signature(mail) for mail in self.emails}
        self._verification_started_at = time.time()
        console.print(f"[dim]验证码邮件基线: {len(self._baseline_signatures)} 封[/dim]")

    def wait_code(
        self,
        timeout: int | None = None,
        keywords: list[str] | None = None,
        status_check: Callable[[], None] | None = None,
    ) -> str:
        timeout = timeout or int(self.cfg["mail"].get("code_timeout", 180))
        interval = max(1.0, float(self.cfg["mail"].get("code_poll_interval", 5)))
        keywords = keywords or ["vercel", "v0", "verification", "verify", "验证码", "code"]
        code_length = int(self.cfg["mail"].get("code_length", 6))
        if self._verification_started_at is None:
            self.prepare_for_verification()
        # 明确回到邮箱收件箱；v0 页面只负责等待输入，验证码始终从邮箱页读取。
        self._select_mail_page_if_available()
        try:
            self.page.bring_to_front()
            if "#inbox" not in (self.page.url or "").lower():
                self.page.goto(self.inbox_url, wait_until="domcontentloaded")
                sleep(1)
        except Exception:
            pass
        console.print(f"[cyan]已切回邮箱收件箱，每 {int(interval)} 秒刷新一次，最长等待 {timeout}s ...[/cyan]")
        deadline = time.time() + timeout

        while time.time() < deadline:
            # v0 可能在等待邮件期间跳转到手机号验证页；每次邮箱刷新前
            # 先检查一次 v0 状态，让上层及时销毁当前 v0 窗口。
            if status_check:
                status_check()
            # 刷新收件箱
            try:
                self._refresh_inbox()
            except Exception as e:
                console.print(f"[yellow]刷新收件箱异常: {e}[/yellow]")

            # 从网络缓存找
            code = self._scan_emails_for_code(keywords, code_length)
            if code:
                console.print(f"[bold green]拿到验证码: {code}[/bold green]")
                return code

            # 从页面 DOM 找
            try:
                body = self._read_mail_text()
                code = extract_verification_code(body, code_length, require_context=True)
                if code and any(k.lower() in body.lower() for k in keywords + ["vercel", "v0"]):
                    console.print(f"[bold green]页面提取验证码: {code}[/bold green]")
                    return code
            except Exception:
                pass

            remain = int(deadline - time.time())
            console.print(f"[dim]本轮未收到验证码，{int(interval)} 秒后再次刷新，剩余 {remain}s ...[/dim]")
            sleep(interval)

        raise TimeoutError(f"等待 {self.current_address or '目标邮箱'} 的验证码超时")

    def _refresh_inbox(self, open_latest: bool = True) -> None:
        # 点刷新按钮
        refreshed = False
        for name in ("Sync", "Synchronize", "Refresh", "刷新", "Reload", "Check", "更新", "同步"):
            try:
                btn = self.page.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() and btn.first.is_visible(timeout=300):
                    btn.first.click(timeout=1000)
                    sleep(1)
                    refreshed = True
                    break
            except Exception:
                continue
        # 重新打开 inbox
        if not refreshed:
            try:
                if "#inbox" not in (self.page.url or ""):
                    self.page.goto(self.inbox_url, wait_until="domcontentloaded")
                else:
                    self.page.reload(wait_until="domcontentloaded")
                sleep(1)
            except Exception:
                pass

        if not open_latest:
            return

        # 尝试点击最新的 v0/Vercel 邮件，确保正文（包括 iframe）进入 DOM。
        try:
            rows = self.page.locator(
                "tr, [class*='mail'], [class*='message'], [class*='inbox'] li, article, .email-item"
            )
            n = min(rows.count(), 8)
            for i in range(n):
                row = rows.nth(i)
                txt = (row.inner_text(timeout=500) or "").lower()
                if any(k in txt for k in ("vercel", "v0", "verify", "code", "验证")):
                    row.click(timeout=1500)
                    sleep(1)
                    break
        except Exception:
            pass

    def _scan_emails_for_code(self, keywords: list[str], code_length: int) -> str | None:
        for mail in reversed(self.emails):
            if self._mail_signature(mail) in self._baseline_signatures:
                continue
            blob = f"{mail.get('subject','')} {mail.get('from','')} {mail.get('body','')}"
            low = blob.lower()
            if keywords and not any(k.lower() in low for k in keywords):
                continue
            code = extract_verification_code(blob, code_length, require_context=True)
            if code:
                return code
        return None

    def _read_mail_text(self) -> str:
        parts: list[str] = []
        for frame in self.page.frames:
            try:
                parts.append(frame.inner_text("body", timeout=1000))
            except Exception:
                continue
        return "\n".join(part for part in parts if part)

    def dump_page_hint(self) -> str:
        try:
            return self.page.inner_text("body")[:800]
        except Exception:
            return ""
