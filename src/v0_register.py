from __future__ import annotations

import random
import re
import time
from typing import Any, Callable

from rich.console import Console

from .stealth import (
    human_click_auto,
    human_mouse_wander,
    human_sleep,
    human_type,
    paste_email,
    paste_otp,
    page_submit_strategies,
    pre_submit_warmup,
)
from .utils import sleep

console = Console()


def random_delay_ms(lo: int = 70, hi: int = 160) -> int:
    return random.randint(lo, hi)


class V0Registrar:
    """v0 / Vercel 邮箱验证码注册 + API Key 创建。"""

    def __init__(self, page, cfg: dict[str, Any]):
        self.page = page
        self.cfg = cfg
        self.v0 = cfg.get("v0") or {}

    def register_with_email(
        self,
        email: str,
        wait_code_fn: Callable[[], str],
    ) -> dict[str, Any]:
        home = self.v0.get("home_url") or "https://v0.app/"
        signup = self.v0.get("signup_url") or "https://v0.app/api/auth/login?next=%2F&action=signup"

        console.print(f"[cyan]打开 v0 注册: {signup}[/cyan]")
        self.page.goto(signup, wait_until="domcontentloaded")
        sleep(2)

        # 可能已经在 vercel signup
        self._ensure_signup_page()
        # Kasada: human warmup + wait real token material (never forge x-kpsdk)
        pre_submit_warmup(self.page, self.cfg)

        # 半自动：只填邮箱，Continue 必须真人手点（验证是否自动化通道被杀）
        if bool(self.v0.get("semi_auto_continue", False)):
            self._fill_email_wait_manual_continue(email)
        else:
            self._fill_email_and_continue(email)

        # 验证码页
        self._wait_code_page(int(self.v0.get("code_page_timeout", 45)))
        code = wait_code_fn()
        self._fill_verification_code(code)

        # 等待跳回 v0 / 登录完成
        self._wait_logged_in(home, int(self.v0.get("login_timeout", 90)))
        console.print(f"[bold green]登录/注册成功[/bold green] 当前: {self.page.url}")
        # 登录成功后不要停在 projects；密钥创建由 main 调用 create_api_key 强制跳转
        return {"email": email, "logged_in": True, "url": self.page.url}

    def _ensure_signup_page(self) -> None:
        # 若落在登录页，切到 Sign up
        try:
            if "login" in self.page.url and "signup" not in self.page.url:
                for name in ("Sign Up", "Sign up", "注册", "Create account"):
                    loc = self.page.get_by_role("link", name=re.compile(name, re.I))
                    if loc.count() and loc.first.is_visible(timeout=800):
                        loc.first.click()
                        sleep(1.5)
                        break
        except Exception:
            pass

    def _fill_email_wait_manual_continue(self, email: str) -> None:
        """半自动：脚本只填邮箱，Continue 由用户手点。

        用于对照实验：若手点能过、脚本点不能过 → 确认是自动化输入/点击通道被 Kasada 杀。
        """
        timeout = max(30.0, float(self.v0.get("manual_continue_timeout", 180)))
        console.print(
            "[bold yellow]===== 半自动模式 =====[/bold yellow]\n"
            "[yellow]1. 脚本已/将填写邮箱\n"
            "2. 请你在弹出的 Edge 无痕窗口里，用鼠标【亲手点一次】Continue with Email\n"
            "3. 不要让脚本代点；若页面清空，可再手点一次\n"
            f"4. 最长等待 {int(timeout)} 秒[/yellow]"
        )

        email_input = self._wait_ready_email_input(min(20.0, timeout))
        if email_input is None:
            if self._is_code_page():
                console.print("[green]已在验证码页[/green]")
                return
            raise RuntimeError(f"半自动：找不到邮箱输入框，当前: {self.page.url}")

        try:
            current = email_input.input_value(timeout=500) or ""
        except Exception:
            current = ""
        if current != email:
            # 半自动仍用逐字输入；若你希望完全手动输入邮箱，可设 semi_auto_fill_email=false
            if bool(self.v0.get("semi_auto_fill_email", True)):
                paste_email(email_input, self.page, email)
            else:
                console.print(
                    f"[bold yellow]请手动在输入框填写邮箱: {email}[/bold yellow]"
                )
        else:
            console.print(f"[green]邮箱已就绪: {email}[/green]")

        console.print(
            f"[bold cyan]请现在手点 Continue with Email（等待最多 {int(timeout)}s）...[/bold cyan]"
        )
        # 把窗口拉到前台（尽力）
        try:
            self.page.bring_to_front()
        except Exception:
            pass

        post_events: list[dict[str, Any]] = []

        def on_response(resp) -> None:
            try:
                url = resp.url or ""
                if resp.request.method != "POST":
                    return
                if "signup" not in url.lower() and "registration" not in url.lower():
                    return
                body = ""
                try:
                    body = (resp.text() or "")[:800]
                except Exception:
                    body = ""
                post_events.append(
                    {"status": resp.status, "url": url[:200], "body": body, "ts": time.time()}
                )
                low = body.lower()
                if "unknown_error" in low or "different signup method" in low:
                    console.print(
                        f"[red]检测到 signup 硬风控响应: {(body or '')[:200]}[/red]"
                    )
            except Exception:
                pass

        try:
            self.page.on("response", on_response)
        except Exception:
            pass

        deadline = time.time() + timeout
        last_hint = 0.0
        while time.time() < deadline:
            if self._is_code_page():
                console.print("[bold green]半自动成功：已进入验证码页（手点 Continue 有效）[/bold green]")
                return
            if self._is_hard_signup_block():
                raise RuntimeError(
                    "半自动：手点后仍出现硬风控。"
                    "若纯手动开 Edge 无痕能过、半自动不能过，"
                    "说明仅「被脚本打开/填邮箱」也会被记分。"
                    f" 当前: {self.page.url}"
                )
            for ev in post_events:
                body = (ev.get("body") or "").lower()
                if "unknown_error" in body or "different signup method" in body:
                    raise RuntimeError(
                        "半自动：signup POST 返回硬风控 unknown_error。"
                        f" body={(ev.get('body') or '')[:220]}"
                    )
            now = time.time()
            if now - last_hint > 15:
                left = int(deadline - now)
                console.print(
                    f"[cyan]仍在等待你手点 Continue... 剩余约 {left}s | {self.page.url[:80]}[/cyan]"
                )
                last_hint = now
            sleep(0.5)

        if self._is_code_page():
            console.print("[bold green]半自动成功：已进入验证码页[/bold green]")
            return
        raise TimeoutError(
            f"半自动：{int(timeout)} 秒内未进入验证码页。"
            "请确认是否已手点 Continue，或页面是否被硬风控。"
            f" 当前: {self.page.url}"
        )

    def _fill_email_and_continue(self, email: str) -> None:
        """提交邮箱：贴近手动——最多真实点击 1~2 次，禁止连点。

        状态机：
        1. 填邮箱 → 停顿 → 点 Continue（第 1 次）
        2. 监听 signup POST + 页面变化
           - 验证码页 → 成功返回
           - 硬风控 unknown_error → 立即停手
           - 空白/新表单重渲染 → 重填邮箱，换坐标再点第 2 次
        3. 第 2 次仍失败 → 抛错（不再第 3~10 次狂点）
        """
        console.print(f"[cyan]填写邮箱: {email}[/cyan]")
        attempts = max(1, min(3, int(self.v0.get("continue_max_attempts", 2))))
        interval = max(1.0, float(self.v0.get("continue_interval", 8)))
        transition_timeout = max(interval, float(self.v0.get("continue_wait_timeout", 18)))
        use_offset = bool(self.v0.get("continue_second_click_offset", True))
        post_events: list[dict[str, Any]] = []

        def on_response(resp) -> None:
            try:
                url = resp.url or ""
                if resp.request.method != "POST":
                    return
                if "signup" not in url.lower() and "registration" not in url.lower():
                    return
                body = ""
                try:
                    body = (resp.text() or "")[:800]
                except Exception:
                    body = ""
                post_events.append(
                    {
                        "status": resp.status,
                        "url": url[:200],
                        "body": body,
                        "ts": time.time(),
                    }
                )
            except Exception:
                pass

        try:
            self.page.on("response", on_response)
        except Exception:
            pass

        for attempt in range(1, attempts + 1):
            post_before = len(post_events)
            if self._is_hard_signup_block():
                raise RuntimeError(
                    "注册页硬风控（different signup method / unknown_error），"
                    f"停止继续点击。当前地址: {self.page.url}"
                )
            self._raise_for_auth_error()
            if self._is_code_page():
                console.print("[green]已进入验证码页面[/green]")
                return

            email_input = self._wait_ready_email_input(transition_timeout)
            if email_input is None:
                if self._is_code_page():
                    console.print("[green]已进入验证码页面[/green]")
                    return
                if self._is_hard_signup_block():
                    raise RuntimeError(
                        "注册页硬风控，邮箱输入框不可用，停止重试。"
                        f"当前地址: {self.page.url}"
                    )
                if attempt < attempts:
                    console.print("[yellow]邮箱输入框暂不可用，等待页面重渲染...[/yellow]")
                    sleep(interval)
                    continue
                raise RuntimeError(
                    f"注册页没有找到可用的邮箱输入框，当前地址: {self.page.url}"
                )

            try:
                current_value = email_input.input_value(timeout=500) or ""
            except Exception:
                current_value = ""
            try:
                if current_value != email:
                    input_mode = str(
                        (self.cfg.get("anti_bot") or {}).get("email_input_mode")
                        or self.v0.get("email_input_mode")
                        or "paste"
                    ).lower()
                    if input_mode == "type":
                        human_type(email_input, email)
                    elif input_mode == "fill":
                        email_input.click(timeout=3000)
                        email_input.fill(email)
                    else:
                        # 默认 paste：对齐你手动「复制粘贴邮箱」
                        paste_email(email_input, self.page, email)
                else:
                    try:
                        email_input.click(timeout=2000)
                    except Exception:
                        pass
            except Exception:
                if self._is_code_page():
                    return
                if attempt < attempts:
                    console.print("[yellow]邮箱输入框状态切换中，等待后重试...[/yellow]")
                    sleep(1.5)
                    continue
                raise

            # 填完后固定停留，贴近手动“看一眼再点”
            dwell = float(self.v0.get("pre_continue_dwell_s", 2.2))
            human_sleep(max(0.8, dwell - 0.4), dwell + 1.0)
            if (self.cfg.get("anti_bot") or {}).get("enabled", True):
                human_mouse_wander(self.page, moves=3 if attempt == 1 else 2)

            clicked = self._click_continue_button(
                attempt=attempt,
                use_offset=(use_offset and attempt >= 2),
            )
            if not clicked:
                try:
                    self.page.keyboard.press("Enter")
                    console.print(
                        f"[green]按 Enter 提交（第 {attempt}/{attempts} 次）[/green]"
                    )
                except Exception:
                    pass

            # 点击后只等待，禁止在 timeout 内再次点击
            outcome = self._wait_after_continue(
                timeout=transition_timeout,
                post_events=post_events,
                post_before=post_before,
            )
            if outcome == "code":
                console.print("[green]已进入验证码页面[/green]")
                return
            if outcome == "hard_block":
                raise RuntimeError(
                    "Continue 后收到硬风控（unknown_error / different signup method），"
                    "已停止继续点击（避免连点加重封禁）。"
                    f" 当前地址: {self.page.url}"
                )
            if outcome == "blank_rerender":
                console.print(
                    "[yellow]页面回到空白/新表单（非硬拒绝），"
                    f"将重新填写并换位置再点（{attempt + 1}/{attempts}）...[/yellow]"
                )
                sleep(interval)
                continue
            # still_same_form
            if attempt < attempts:
                console.print(
                    "[yellow]仍停留在邮箱页且无硬风控文案，"
                    f"按空白重渲染策略再试一次（{attempt + 1}/{attempts}）...[/yellow]"
                )
                sleep(interval)
                continue

        if self._is_hard_signup_block():
            raise RuntimeError(
                "连续提交后仍被硬风控拦截，请换全新窗口或对照手动 Edge 无痕。"
                f" 当前地址: {self.page.url}"
            )
        raise TimeoutError(
            f"已按策略最多点击 {attempts} 次 Continue，仍未进入验证码页；"
            f"当前地址: {self.page.url}"
        )

    def _wait_ready_email_input(self, timeout: float):
        deadline = time.time() + max(2.0, timeout)
        while time.time() < deadline:
            if self._is_code_page() or self._is_hard_signup_block():
                return None
            email_input = self._find_email_input()
            if email_input is not None:
                return email_input
            sleep(0.4)
        return self._find_email_input()

    def _click_continue_button(self, attempt: int, use_offset: bool) -> bool:
        """提交 Continue：默认页面内策略，不拖系统鼠标。

        策略轮换（开源批量注册常见组合）：
        第1次 click(position) → 第2次 Enter → 再试 form.requestSubmit
        """
        names = (
            "Continue with Email",
            "Continue",
            "继续",
            "Sign Up",
            "Sign up",
            "Submit",
            "发送验证码",
        )
        anti = self.cfg.get("anti_bot") or {}
        # 默认关闭 os_click，避免拖动你的真实鼠标
        prefer_os = bool(anti.get("os_click", False))
        strategies = list(anti.get("submit_strategies") or ["click", "enter", "form"])
        # 按 attempt 轮换策略
        strategy = strategies[(attempt - 1) % len(strategies)]

        for name in names:
            try:
                btn = self.page.get_by_role(
                    "button", name=re.compile(rf"^{re.escape(name)}$", re.I)
                )
                count = btn.count()
                if not count:
                    continue
                target = None
                for i in range(min(count, 4)):
                    cand = btn.nth(i)
                    try:
                        if cand.is_visible(timeout=600):
                            target = cand
                            if use_offset and i > 0:
                                break
                            if not use_offset:
                                break
                    except Exception:
                        continue
                if target is None:
                    continue
                offset_mode = "edge" if use_offset else "center"
                if prefer_os:
                    human_click_auto(
                        target,
                        self.page,
                        offset_mode=offset_mode,
                        prefer_os=True,
                        submit_strategy=strategy,
                    )
                else:
                    page_submit_strategies(
                        target,
                        self.page,
                        strategy=strategy,
                        offset_mode=offset_mode,
                    )
                console.print(
                    f"[green]提交: {name}（第 {attempt} 次, 策略={strategy}"
                    f"{'，换坐标' if use_offset else ''}）[/green]"
                )
                return True
            except Exception:
                continue
        return False

    def _wait_after_continue(
        self,
        timeout: float,
        post_events: list[dict[str, Any]],
        post_before: int,
    ) -> str:
        """返回: code | hard_block | blank_rerender | still_same_form"""
        deadline = time.time() + max(3.0, timeout)
        saw_disabled = False
        saw_empty = False
        while time.time() < deadline:
            # 优先看网络硬拒绝，避免页面文案还没刷出来又去点
            for ev in post_events[post_before:]:
                body = (ev.get("body") or "").lower()
                if "unknown_error" in body or "different signup method" in body:
                    console.print(
                        f"[red]signup POST 硬风控: status={ev.get('status')} "
                        f"body={(ev.get('body') or '')[:180]}[/red]"
                    )
                    return "hard_block"
            if self._is_hard_signup_block():
                return "hard_block"
            if self._is_code_page():
                return "code"
            try:
                email_input = self._find_email_input()
                if email_input is None:
                    saw_empty = True
                else:
                    try:
                        if not email_input.is_enabled(timeout=300):
                            saw_disabled = True
                        val = email_input.input_value(timeout=300) or ""
                        if saw_disabled and val == "":
                            saw_empty = True
                        if saw_disabled and val == "" and email_input.is_enabled(timeout=300):
                            # 曾禁用后清空并重新可编辑 → 空白重渲染
                            return "blank_rerender"
                    except Exception:
                        pass
            except Exception:
                pass
            if self._is_signup_retry_page() and not self._is_hard_signup_block():
                # 软提示且表单可能清空
                if self._find_email_input() is not None:
                    return "blank_rerender"
            sleep(0.45)

        if self._is_hard_signup_block():
            return "hard_block"
        if self._is_code_page():
            return "code"
        # 表单被清空视为软重渲染，允许第二次换位点
        try:
            email_input = self._find_email_input()
            if email_input is not None:
                val = email_input.input_value(timeout=400) or ""
                if val == "" or saw_empty:
                    return "blank_rerender"
        except Exception:
            pass
        return "still_same_form"

    def _is_hard_signup_block(self) -> bool:
        """硬风控：服务端明确拒绝，不应再连点 Continue。"""
        try:
            body = re.sub(r"\s+", " ", self.page.inner_text("body") or "").strip().lower()
        except Exception:
            return False
        hard_markers = (
            "please try again later or use a different signup method",
            "please try again later or use a different sign up method",
            "try a different sign up method",
            "try a different signup method",
            "unknown_error",
        )
        # 仅 “try again” 太宽，不作为硬停；必须带不同注册方式或 later
        if any(m in body for m in hard_markers):
            return True
        if "please try again later" in body and "sign up" in body:
            return True
        return False

    def _find_email_input(self):

        for sel in (
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="email" i]',
            'input[id*="email" i]',
        ):
            try:
                loc = self.page.locator(sel).first
                if (
                    loc.count()
                    and loc.is_visible(timeout=800)
                    and loc.is_enabled(timeout=800)
                ):
                    return loc
            except Exception:
                continue
        try:
            loc = self.page.get_by_role("textbox").first
            if loc.count() and loc.is_visible(timeout=800):
                return loc
        except Exception:
            pass
        return None

    def _is_code_page(self) -> bool:
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        if self._contains_phone_verification(body):
            return False
        try:
            if self.page.locator('input[autocomplete="one-time-code"]').count():
                return True
            if self.page.locator('input[name="digits"]:visible').count():
                return True
            if self.page.locator('input[maxlength="1"]:visible').count() >= 4:
                return True
        except Exception:
            pass
        if any(
            marker in body
            for marker in (
                "verification code",
                "enter verification code",
                "enter the code",
                "验证码",
                "check your email",
                "we sent",
                "one-time",
                "otp",
            )
        ):
            return True
        return False

    def _wait_code_page(self, timeout: int = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_for_auth_error()
            if self._is_code_page():
                console.print("[green]已进入验证码页面[/green]")
                return
            sleep(1)
        raise TimeoutError(
            f"提交邮箱后 {timeout} 秒内没有进入验证码页面，当前地址: {self.page.url}"
        )

    def _fill_verification_code(self, code: str) -> None:
        """填写验证码：对齐手动「复制粘贴 → 停顿等待 → 再点 Continue」。

        关键因：原先对多格 OTP 用 fill() 瞬填，或 keyboard.type(delay=50) 极快连敲，
        Kasada/Vercel 行为分会判机器人 → 假「密码/验证码错误」或升级手机号验证。
        """
        console.print(f"[cyan]填写验证码: {code}[/cyan]")
        code = re.sub(r"\D", "", code)
        if not code:
            raise ValueError("验证码为空")
        expected_length = int((self.cfg.get("mail") or {}).get("code_length", 6))
        if len(code) != expected_length:
            raise ValueError(f"验证码长度应为 {expected_length} 位，实际为 {len(code)} 位")

        anti = self.cfg.get("anti_bot") or {}
        code_mode = str(
            anti.get("code_input_mode")
            or self.v0.get("code_input_mode")
            or "paste"
        ).lower()
        post_dwell = float(
            self.v0.get("post_code_dwell_s")
            or anti.get("post_code_dwell_s")
            or 2.6
        )

        # 粘贴前轻微闲逛，模拟从邮箱切回后的鼠标活动
        if anti.get("enabled", True):
            human_mouse_wander(self.page, moves=2)
            human_sleep(0.4, 0.9)

        filled = False

        # —— 多格 OTP：优先在首格粘贴整串（站点通常会自动分发到各格）——
        boxes = self.page.locator('input[maxlength="1"]:visible')
        try:
            n = boxes.count()
        except Exception:
            n = 0
        if n >= 4:
            if n < len(code):
                raise RuntimeError(f"验证码输入框只有 {n} 格，但验证码有 {len(code)} 位")
            first = boxes.nth(0)
            if code_mode == "type":
                first.click(timeout=4000)
                human_sleep(0.2, 0.45)
                for i, ch in enumerate(code[:n]):
                    boxes.nth(i).type(ch, delay=random_delay_ms())
                    human_sleep(0.05, 0.14)
                console.print("[cyan]验证码输入: 多格慢速逐字[/cyan]")
            else:
                # paste / 默认：整串贴到首格
                paste_otp(first, self.page, code)
                # 若粘贴未自动分发，补填剩余格（慢速，禁止 fill 瞬填）
                try:
                    got = ""
                    for i in range(min(n, len(code))):
                        try:
                            got += boxes.nth(i).input_value(timeout=300) or ""
                        except Exception:
                            break
                    if len(re.sub(r"\D", "", got)) < len(code):
                        console.print("[yellow]粘贴未完整分发，慢速补填各格...[/yellow]")
                        for i, ch in enumerate(code[:n]):
                            try:
                                cur = boxes.nth(i).input_value(timeout=300) or ""
                            except Exception:
                                cur = ""
                            if cur == ch:
                                continue
                            boxes.nth(i).click(timeout=2000)
                            human_sleep(0.05, 0.12)
                            try:
                                boxes.nth(i).press("Control+A")
                                boxes.nth(i).press("Backspace")
                            except Exception:
                                pass
                            boxes.nth(i).type(ch, delay=random_delay_ms())
                            human_sleep(0.06, 0.15)
                except Exception:
                    pass
            filled = True

        # —— 单框 OTP ——
        if not filled:
            for sel in (
                'input[autocomplete="one-time-code"]',
                'input[name="digits"]',
                'input[name*="code" i]',
                'input[placeholder*="code" i]',
                'input[inputmode="numeric"]',
                'input[type="tel"]',
                'input[type="text"]',
            ):
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() and loc.is_visible(timeout=800):
                        if code_mode == "type":
                            human_type(loc, code, min_delay=70, max_delay=170)
                        elif code_mode == "fill":
                            loc.click(timeout=3000)
                            loc.fill(code)
                        else:
                            paste_otp(loc, self.page, code)
                        filled = True
                        break
                except Exception:
                    continue

        if not filled:
            # 兜底：焦点在页面上时慢速键盘输入
            console.print("[yellow]未定位到验证码框，使用键盘慢速输入[/yellow]")
            self.page.keyboard.type(code, delay=random_delay_ms(90, 180))
            filled = True

        # 粘贴后停顿：对齐你手动「粘贴后等一会儿再通过」
        console.print(f"[cyan]验证码已输入，停顿约 {post_dwell:.1f}s（模拟手动等待）...[/cyan]")
        human_sleep(max(1.0, post_dwell - 0.5), post_dwell + 1.2)
        if anti.get("enabled", True):
            human_mouse_wander(self.page, moves=2)

        # 若站点粘贴后自动校验/跳转，则不必再点
        if self._left_code_page():
            console.print("[green]验证码提交后页面已自动跳转[/green]")
            self._raise_for_auth_error()
            return

        self._click_verify_submit()
        # 提交后多等一会再判错，避免文案未刷出就误判
        human_sleep(1.2, 2.2)
        self._raise_for_auth_error()
        if self._is_code_page() and self._has_code_error():
            raise RuntimeError(
                "验证码提交后仍显示错误（可能是行为风控假拒绝）。"
                f" 当前: {self.page.url}"
            )

    def _left_code_page(self) -> bool:
        """已离开验证码页（登录中/首页/手机号等）。"""
        if self._is_code_page():
            return False
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        if self._contains_phone_verification(body):
            return True
        url = (self.page.url or "").lower()
        if "v0.app" in url and "login" not in url and "signup" not in url:
            return True
        if any(k in body for k in ("new chat", "api keys", "settings", "continue to", "authorize")):
            return True
        return not self._is_code_page()

    def _has_code_error(self) -> bool:
        try:
            body = re.sub(r"\s+", " ", self.page.inner_text("body") or "").strip().lower()
        except Exception:
            return False
        markers = (
            "invalid verification code",
            "incorrect verification code",
            "incorrect code",
            "invalid code",
            "wrong code",
            "verification code has expired",
            "code is incorrect",
            "code is invalid",
            "password is incorrect",
            "incorrect password",
            "wrong password",
            "验证码无效",
            "验证码错误",
            "验证码已过期",
            "密码错误",
            "密码不正确",
        )
        return any(m in body for m in markers)

    def _raise_for_auth_error(self) -> None:
        try:
            body = re.sub(r"\s+", " ", self.page.inner_text("body") or "").strip()
        except Exception:
            return
        low = body.lower()
        error_markers = (
            "invalid verification code",
            "incorrect verification code",
            "incorrect code",
            "invalid code",
            "wrong code",
            "code is incorrect",
            "code is invalid",
            "verification code has expired",
            "password is incorrect",
            "incorrect password",
            "wrong password",
            "too many attempts",
            "email is not allowed",
            "email address is not valid",
            "验证码无效",
            "验证码错误",
            "验证码已过期",
            "密码错误",
            "密码不正确",
            "尝试次数过多",
        )
        for marker in error_markers:
            if marker in low:
                raise RuntimeError(f"注册页面要求更换全新 v0 窗口: {body[:300]}")
        if self._contains_phone_verification(low):
            self._abort_phone_verification()
            raise RuntimeError("注册流程要求额外完成手机号验证，已终止本次任务")

    def _is_signup_retry_page(self) -> bool:
        """识别 V0 提交后短暂出现的“重新尝试/更换方式”页面。

        该页面有时会把邮箱表单清空后重新渲染。它属于 Continue 提交状态未完成，
        由外层循环重新填写并点击；真正的手机号和验证码错误仍由
        ``_raise_for_auth_error`` 立即终止。
        """
        try:
            body = re.sub(r"\s+", " ", self.page.inner_text("body") or "").strip().lower()
        except Exception:
            return False
        return any(
            marker in body
            for marker in (
                "please try again or try a different sign up method",
                "try a different sign up method",
                "complete the account recovery form",
                "if you continue to have issues",
            )
        )

    @staticmethod
    def _contains_phone_verification(text: str) -> bool:
        phone_markers = (
            "phone number",
            "mobile number",
            "telephone number",
            "verify your phone",
            "phone verification",
            "手机号",
            "手机号码",
            "手机验证",
        )
        verify_markers = ("verify", "verification", "confirm", "验证", "确认", "number")
        return any(marker in text for marker in phone_markers) and any(
            marker in text for marker in verify_markers
        )

    def _abort_phone_verification(self) -> None:
        """遇到手机号验证时关闭当前提示，阻止流程继续创建账号。"""
        console.print("[red]检测到手机号验证，自动终止本次注册流程[/red]")
        try:
            self._click_if_visible(
                [
                    ("button", r"^(?:Cancel|Close|Back|返回|取消|关闭)$"),
                    ("link", r"^(?:Cancel|Close|Back|返回|取消|关闭)$"),
                ]
            )
        except Exception:
            pass

    def _click_verify_submit(self) -> None:
        """验证码提交：优先真人式 click(position)，避免 btn.click 机器感。"""
        anti = self.cfg.get("anti_bot") or {}
        for name in (
            "Continue",
            "Verify",
            "Confirm",
            "Submit",
            "验证",
            "继续",
            "确认",
            "Log in",
            "Sign in",
        ):
            try:
                btn = self.page.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() and btn.first.is_visible(timeout=600):
                    target = btn.first
                    try:
                        page_submit_strategies(
                            target,
                            self.page,
                            strategy="click",
                            offset_mode="center",
                        )
                    except Exception:
                        target.click(timeout=2000, delay=random.randint(50, 130))
                    console.print(f"[green]提交验证码按钮: {name}[/green]")
                    sleep(2)
                    return
            except Exception:
                continue
        try:
            self.page.keyboard.press("Enter")
            console.print("[green]验证码提交: Enter[/green]")
        except Exception:
            pass
        sleep(2)

    def _wait_logged_in(self, home: str, timeout: int = 90) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_for_auth_error()
            if self._is_logged_in():
                console.print(f"[green]已确认登录: {self.page.url}[/green]")
                self._dismiss_post_login_overlays(rounds=2)
                return
            # 登录后 v0 可能欢迎 / 服务条款 / 授权遮罩，需要先点掉
            if self._dismiss_post_login_overlays(rounds=2):
                continue
            sleep(1.5)
        # 超时前：若 URL 已是 projects/chat 仍判定成功，避免误杀
        if self._is_logged_in():
            console.print(f"[green]超时前二次确认已登录: {self.page.url}[/green]")
            self._dismiss_post_login_overlays(rounds=3)
            return
        # 最后回首页校验 Cookie
        try:
            self.page.goto(home, wait_until="domcontentloaded")
            sleep(2)
            self._dismiss_post_login_overlays(rounds=2)
        except Exception:
            pass
        if self._is_logged_in():
            console.print(f"[green]回首页后确认已登录: {self.page.url}[/green]")
            return
        raise TimeoutError(
            f"验证码已提交后，{timeout} 秒内未确认登录成功。当前地址: {self.page.url}"
        )

    def _is_logged_in(self) -> bool:
        url = (self.page.url or "").lower()
        # 仅识别 v0 主站；登录/注册页直接否
        if "v0.app" not in url and "v0.dev" not in url:
            return False
        if any(
            part in url
            for part in ("/login", "/signup", "/api/auth/login", "action=signup")
        ):
            return False

        # 验证码通过后常直接落到 *-projects / chat / settings，URL 即可判定已登录
        logged_in_url_markers = (
            "-projects",
            "/projects",
            "/chat",
            "/settings",
            "/account",
            "/dashboard",
            "/team",
        )
        if any(marker in url for marker in logged_in_url_markers):
            return True

        # 例如 https://v0.app/<workspace-slug>
        path = url.split("?", 1)[0].rstrip("/")
        home_urls = {
            "https://v0.app",
            "https://v0.dev",
            "http://v0.app",
            "http://v0.dev",
        }
        if path not in home_urls and path.count("/") >= 3 and "auth" not in path:
            return True

        try:
            body = re.sub(r"\s+", " ", self.page.inner_text("body") or "").lower()
        except Exception:
            return False
        logged_out_markers = (
            "sign up for v0",
            "continue with email",
            "log in sign up",
            "sign in sign up",
            "create your account",
        )
        if any(marker in body for marker in logged_out_markers):
            return False
        return any(
            marker in body
            for marker in (
                "new chat",
                "api keys",
                "settings",
                "upgrade",
                "recent chats",
                "projects",
                "my projects",
                "create project",
                "new project",
                "community",
                "deployments",
            )
        )

    def _dismiss_post_login_overlays(self, rounds: int = 3) -> bool:
        """点掉登录成功后 v0 弹出的欢迎 / 服务条款 / 引导弹层。

        用户实测：验证码通过后 v0 会先弹一个官方引导页，必须点一下才能进入
        控制台并创建 API Key。这里用宽松文案循环点击，并刻意避开
        登出 / 删除 / 取消 等危险按钮，避免误伤登录态。返回是否至少点过一个按钮。
        """
        safe_patterns = (
            r"^Accept and continue$",
            r"^Accept$",
            r"^I agree$",
            r"^Agree$",
            r"^Continue to v0$",
            r"^Continue to dashboard$",
            r"^Continue$",
            r"^Get started$",
            r"^Let's go$",
            r"^Got it$",
            r"^Skip for now$",
            r"^Skip$",
            r"^Maybe later$",
            r"^Not now$",
            r"^Next$",
            r"^Done$",
            r"^OK$",
            r"^Okay$",
            r"^Authorize$",
            r"^Allow$",
            r"^接受并继续$",
            r"^接受$",
            r"^同意$",
            r"^继续$",
            r"^开始$",
            r"^跳过$",
            r"^完成$",
            r"^好的$",
            r"^知道了$",
            r"^允许$",
        )
        danger = re.compile(
            r"log ?out|sign ?out|delete|remove|cancel|discard|退出|删除|取消",
            re.I,
        )
        any_clicked = False
        for _ in range(max(1, rounds)):
            clicked = False
            for pat in safe_patterns:
                try:
                    btn = self.page.get_by_role("button", name=re.compile(pat, re.I))
                    if not btn.count():
                        continue
                    first = btn.first
                    if not first.is_visible(timeout=400):
                        continue
                    label = (first.inner_text(timeout=300) or "").strip()
                    if danger.search(label):
                        continue
                    first.click(timeout=2000)
                    console.print(f"[green]关闭登录后引导弹层: {label or pat}[/green]")
                    sleep(1.2)
                    clicked = True
                    any_clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                break
        return any_clicked

    def create_api_key(self, name: str | None = None) -> str:
        """创建 API Key：登录后强制进 keys 页 → 创建 → 读取 v1:team_*:vcp_* → Done。"""
        # 登录后可能停在 projects/欢迎页，先关遮罩再强制跳密钥地址
        self._dismiss_post_login_overlays(rounds=3)
        prefix = self.v0.get("api_key_name_prefix") or "auto"
        name = name or f"{prefix}-{int(time.time())}"

        candidates = list(self.v0.get("keys_url_candidates") or [])
        candidates.extend(
            [
                "https://v0.app/chat/settings/keys",
                "https://v0.dev/chat/settings/keys",
                "https://v0.app/settings/keys",
                "https://v0.dev/settings/keys",
                "https://v0.app/chat/settings",
                "https://v0.app/settings",
            ]
        )
        # 当前在 projects 页时，直接 goto 更稳，不依赖侧栏
        console.print(
            f"[cyan]登录后准备打开密钥页，当前: {self.page.url}[/cyan]"
        )

        def _keys_page_ready() -> bool:
            url = (self.page.url or "").lower()
            if any(x in url for x in ("/settings/keys", "/chat/settings/keys", "settings/keys")):
                return True
            try:
                body = (self.page.inner_text("body") or "").lower()
            except Exception:
                body = ""
            markers = (
                "api key",
                "api keys",
                "create key",
                "create api",
                "generate",
                "save your key",
                "secret key",
                "new api key",
                "密钥",
            )
            return any(k in body for k in markers)

        opened = False
        last_err = None
        for url in dict.fromkeys(candidates):
            try:
                console.print(f"[cyan]尝试打开 API Keys: {url}[/cyan]")
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                sleep(2)
                self._dismiss_post_login_overlays(rounds=2)
                self._accept_terms_if_present()
                sleep(0.8)
                # 被重定向回登录则继续试下一个
                cur = (self.page.url or "").lower()
                if any(x in cur for x in ("/login", "/signup", "action=signup", "/api/auth/login")):
                    console.print(f"[yellow]密钥地址被重定向到登录: {self.page.url}[/yellow]")
                    continue
                if _keys_page_ready() or self._is_logged_in():
                    # 已登录即可继续；keys 文案有时延迟渲染
                    if _keys_page_ready():
                        opened = True
                        console.print(f"[green]已进入密钥相关页: {self.page.url}[/green]")
                        break
                    # 在 settings 根页再点 API Keys
                    self._click_if_visible(
                        [
                            ("link", r"API.?Keys?|密钥"),
                            ("button", r"API.?Keys?|密钥"),
                            ("text", r"API.?Keys?|密钥"),
                        ]
                    )
                    sleep(1.2)
                    if _keys_page_ready():
                        opened = True
                        console.print(f"[green]已从设置进入密钥页: {self.page.url}[/green]")
                        break
            except Exception as exc:
                last_err = exc
                console.print(f"[yellow]打开密钥页失败 {url}: {exc}[/yellow]")
                continue

        if not opened:
            console.print("[yellow]直接 URL 未稳定进入密钥页，改走 UI 菜单[/yellow]")
            self._open_settings_via_ui()
            self._accept_terms_if_present()
            self._click_if_visible(
                [
                    ("link", r"API.?Keys?|密钥"),
                    ("button", r"API.?Keys?|密钥"),
                    ("text", r"API.?Keys?|密钥"),
                ]
            )
            sleep(1.5)
            opened = _keys_page_ready() or self._is_logged_in()

        body = ""
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            body = ""

        # 若弹窗里已经露出 key，直接取
        existing = self._extract_api_key_from_page()
        if existing:
            console.print(
                f"[bold green]API Key 已在页面出现: {existing[:12]}...{existing[-6:]}[/bold green]"
            )
            self._click_done_after_key()
            return existing

        if not self._is_logged_in() and not _keys_page_ready():
            raise RuntimeError(
                f"未进入已登录的 API Keys 页面，当前地址: {self.page.url}"
                + (f"；上次错误: {last_err}" if last_err else "")
            )

        # 创建 / 生成按钮
        created = False
        for name_pat in (
            r"Create(\s+API)?\s*Key",
            r"New(\s+API)?\s*Key",
            r"Generate(\s+API)?\s*Key",
            r"^Generate$",
            r"^Create$",
            r"创建",
            r"新建",
            r"生成",
        ):
            try:
                btn = self.page.get_by_role("button", name=re.compile(name_pat, re.I))
                if btn.count() and btn.first.is_visible(timeout=1000):
                    btn.first.click()
                    created = True
                    console.print(f"[green]已点击创建 API Key 按钮: {name_pat}[/green]")
                    sleep(1.2)
                    break
            except Exception:
                continue
        if not created:
            try:
                self.page.get_by_text(
                    re.compile(r"Create.*Key|Generate.*Key|创建.*密钥|生成", re.I)
                ).first.click(timeout=2000)
                created = True
                sleep(1)
            except Exception:
                pass
        if not created:
            api_key = self._extract_api_key_from_page()
            if api_key:
                self._click_done_after_key()
                console.print(
                    f"[bold green]API Key 创建成功: {api_key[:12]}...{api_key[-6:]}[/bold green]"
                )
                return api_key
            hint = re.sub(r"\s+", " ", self.page.inner_text("body") or "")[:300]
            raise RuntimeError(
                f"API Keys 页面没有找到创建密钥按钮，当前: {self.page.url}；页面摘要: {hint}"
            )

        # 填写名称（可选）
        named = False
        for sel in (
            '[role="dialog"] input[name*="name" i]',
            '[role="dialog"] input[placeholder*="name" i]',
            '[role="dialog"] input[type="text"]',
            'input[name*="name" i]',
            'input[placeholder*="name" i]',
            'input[placeholder*="名称" i]',
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=800):
                    try:
                        val = loc.input_value(timeout=300) or ""
                    except Exception:
                        val = ""
                    if self._looks_like_api_key(val):
                        continue
                    loc.fill(name)
                    named = True
                    console.print(f"[green]API Key 命名: {name}[/green]")
                    break
            except Exception:
                continue
        if not named:
            console.print("[yellow]创建对话框没有名称输入框，将使用站点默认名称[/yellow]")

        # 确认创建
        confirmed = False
        for name_pat in (
            r"^Create$",
            r"^Create(?: API)?\s*Key$",
            r"^Generate$",
            r"^Generate(?: API)?\s*Key$",
            r"^Confirm$",
            r"^创建$",
            r"^确认$",
            r"^生成$",
            r"^Save$",
            r"^保存$",
            r"^Continue$",
            r"^Accept$",
        ):
            try:
                btn = self.page.get_by_role("button", name=re.compile(name_pat, re.I))
                if btn.count() and btn.first.is_visible(timeout=800):
                    btn.first.click()
                    sleep(1.5)
                    confirmed = True
                    console.print(f"[green]确认创建密钥: {name_pat}[/green]")
                    break
            except Exception:
                continue
        if not confirmed:
            console.print("[yellow]未找到二次确认按钮，直接尝试读取密钥[/yellow]")

        api_key = None
        for round_i in range(8):
            api_key = self._extract_api_key_from_page()
            if api_key:
                break
            self._click_if_visible(
                [
                    ("button", r"^(?:Copy|复制|Copy key|Copy API Key)$"),
                    ("button", r"Copy|复制"),
                ]
            )
            sleep(1.0 + round_i * 0.25)
        if not api_key:
            raise RuntimeError(
                f"未能从页面读取 API Key，当前: {self.page.url}；请手动复制或检查选择器"
            )
        console.print(
            f"[bold green]API Key 创建成功: {api_key[:12]}...{api_key[-6:]}[/bold green]"
        )
        self._click_done_after_key()
        return api_key


    def _accept_terms_if_present(self) -> bool:
        """Accept 服务条款 / 继续引导。"""
        clicked = False
        for name_pat in (
            r"^(?:Accept|I agree|Agree|Continue|Accept and continue|接受|同意|继续)$",
            r"Accept|I agree|Agree|接受并继续|同意并继续",
        ):
            try:
                btn = self.page.get_by_role("button", name=re.compile(name_pat, re.I))
                if btn.count() and btn.first.is_visible(timeout=600):
                    btn.first.click(timeout=2000)
                    console.print(f"[green]已点击服务条款/引导: {name_pat}[/green]")
                    sleep(1.5)
                    clicked = True
                    break
            except Exception:
                continue
        return clicked

    def _click_done_after_key(self) -> None:
        """密钥展示后点 Done / Close。弹窗按钮可能延迟渲染，重试几轮。"""
        for _ in range(3):
            for name_pat in (
                r"^(?:Done|Close|OK|完成|关闭|好的)$",
                r"Done|完成",
            ):
                try:
                    btn = self.page.get_by_role("button", name=re.compile(name_pat, re.I))
                    if btn.count() and btn.first.is_visible(timeout=600):
                        btn.first.click(timeout=2000)
                        console.print("[green]已点击 Done 关闭密钥弹窗[/green]")
                        sleep(0.8)
                        return
                except Exception:
                    continue
            sleep(0.6)

    @staticmethod
    def _normalize_api_key_candidate(text: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        # 去掉粘连的说明文案
        t = re.sub(r"(?i)\s*(save your key|please save your secret key).*$", "", t).strip()
        t = re.sub(r"(?i)(Save|Please|Keep|Copy|Done)$", "", t).strip()
        m = re.search(r"(v1:team_[A-Za-z0-9]{10,80}:vcp_[A-Za-z0-9]{24,80})", t)
        if m:
            return m.group(1)
        m = re.search(r"(vcp_[A-Za-z0-9]{24,80})", t)
        if m:
            return m.group(1)
        return t

    @classmethod
    def _looks_like_api_key(cls, text: str) -> bool:
        t = cls._normalize_api_key_candidate(text)
        if not t or len(t) < 24:
            return False
        if re.fullmatch(
            r"v1:team_[A-Za-z0-9]{10,80}:vcp_[A-Za-z0-9]{24,80}",
            t,
        ):
            return True
        if re.fullmatch(r"vcp_[A-Za-z0-9]{24,80}", t):
            return True
        if re.fullmatch(r"v[0-9]_[A-Za-z0-9]{20,}", t):
            return True
        if re.fullmatch(r"sk-[A-Za-z0-9_\-]{20,}", t):
            return True
        return False

    def logout(self) -> bool:
        """退出 v0/Vercel 登录态；不清理浏览器缓存，避免影响其他浏览器数据。"""
        try:
            clicked = self._click_if_visible(
                [
                    ("button", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                    ("link", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                    ("text", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                ]
            )
            if not clicked:
                clicked_menu = self._click_if_visible(
                    [
                        ("button", r"account|profile|user|avatar|账户|账号|个人中心"),
                        ("text", r"account|profile|user|avatar|账户|账号|个人中心"),
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
                clicked = self._click_if_visible(
                    [
                        ("button", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                        ("link", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                        ("text", r"^(?:Log out|Logout|Sign out|退出登录|退出)$"),
                    ]
                )
            if clicked:
                sleep(1)
                console.print("[green]v0 已退出登录[/green]")
            else:
                console.print("[yellow]当前页面没有可见的 v0 退出按钮，将由浏览器会话关闭结束本次任务[/yellow]")
            return clicked
        except Exception as exc:
            console.print(f"[yellow]v0 退出登录失败: {exc}[/yellow]")
            return False

    def _open_settings_via_ui(self) -> None:
        console.print("[cyan]通过 UI 进入设置页...[/cyan]")
        self.page.goto(self.v0.get("home_url") or "https://v0.app/", wait_until="domcontentloaded")
        sleep(2)
        # 头像/菜单
        for sel in (
            'button[aria-label*="menu" i]',
            'button[aria-label*="account" i]',
            'button[aria-label*="user" i]',
            'img[alt*="avatar" i]',
            '[data-testid*="user" i]',
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=800):
                    loc.click()
                    sleep(1)
                    break
            except Exception:
                continue
        self._click_if_visible(
            [
                ("link", r"Settings|设置"),
                ("button", r"Settings|设置"),
                ("text", r"Settings|设置"),
            ]
        )
        sleep(1)
        self._click_if_visible(
            [
                ("link", r"API.?Keys?|密钥"),
                ("button", r"API.?Keys?|密钥"),
                ("text", r"API.?Keys?|密钥"),
            ]
        )
        sleep(1)

    def _click_if_visible(self, specs: list[tuple[str, str]]) -> bool:
        for kind, pat in specs:
            try:
                if kind == "link":
                    loc = self.page.get_by_role("link", name=re.compile(pat, re.I))
                elif kind == "button":
                    loc = self.page.get_by_role("button", name=re.compile(pat, re.I))
                else:
                    loc = self.page.get_by_text(re.compile(pat, re.I))
                if loc.count() and loc.first.is_visible(timeout=800):
                    loc.first.click()
                    return True
            except Exception:
                continue
        return False

    def _extract_api_key_from_page(self) -> str | None:
        """提取 API Key。当前 v0 展示形态: v1:team_xxx:vcp_yyy"""
        # 优先级：完整 v1:team_*:vcp_* → 单独 vcp_ → 旧前缀
        # 注意：页面常把 key 与 "Save your key" 粘在一起，不能用 \b 截断字母，
        # 用长度上限 + 否定前瞻，避免吃掉后面的 Save。
        prefixed_patterns = [
            r"\bv1:team_[A-Za-z0-9]{10,80}:vcp_[A-Za-z0-9]{24,80}(?![A-Za-z0-9])",
            r"\bvcp_[A-Za-z0-9]{24,80}(?![A-Za-z0-9])",
            r"\bv[0-9]_[A-Za-z0-9]{20,}\b",
            r"\bsk-[A-Za-z0-9_\-]{20,}\b",
            r"\bv0_[A-Za-z0-9]{20,}\b",
            r"\bv0-[A-Za-z0-9_\-]{20,}\b",
        ]
        broad_texts: list[str] = []
        try:
            broad_texts.append(self.page.inner_text("body") or "")
        except Exception:
            pass
        try:
            # 有时密钥在 value / data 属性里
            attr_blob = self.page.evaluate(
                """() => {
                  const parts = [];
                  document.querySelectorAll('input, textarea, code, pre, [data-key], [class*="key"], [class*="token"]').forEach(el => {
                    if (el.value) parts.push(el.value);
                    if (el.textContent) parts.push(el.textContent);
                    for (const a of el.attributes || []) parts.push(a.value || '');
                  });
                  return parts.join('\\n');
                }"""
            )
            if attr_blob:
                broad_texts.append(str(attr_blob))
        except Exception:
            pass

        scoped_values: list[str] = []
        for sel in (
            '[role="dialog"] code',
            '[role="dialog"] pre',
            '[role="dialog"] input',
            '[role="dialog"] textarea',
            '[role="dialog"] *',
            'code',
            'pre',
            '[data-testid*="key" i]',
            '[class*="api-key" i]',
            '[class*="token" i]',
            '[class*="secret" i]',
            'input[readonly]',
            'input[type="text"]',
        ):
            try:
                locs = self.page.locator(sel)
                n = min(locs.count(), 30)
                for i in range(n):
                    try:
                        t = locs.nth(i).input_value(timeout=200)
                    except Exception:
                        try:
                            t = locs.nth(i).inner_text(timeout=200)
                        except Exception:
                            t = ""
                    if t:
                        scoped_values.append(t.strip())
            except Exception:
                continue

        blob = "\n".join(broad_texts + scoped_values)
        # 页面常把 key 与 "Save your key" 粘在同一段文本
        blob = re.sub(
            r"(?i)(save your key|please save your secret key|keep it secure)[^\n]*",
            " ",
            blob,
        )
        blob = re.sub(r"(?i)(v1:team_[A-Za-z0-9]+:vcp_[A-Za-z0-9]+?)(Save|Please|Keep|Copy|Done)\b", r"\1 ", blob)
        blob = re.sub(r"(?i)(vcp_[A-Za-z0-9]+?)(Save|Please|Keep|Copy|Done)\b", r"\1 ", blob)

        for pat in prefixed_patterns:
            for m in re.finditer(pat, blob):
                val = self._normalize_api_key_candidate(m.group(0))
                if not val:
                    continue
                if val.lower() in ("content-type", "authorization"):
                    continue
                if self._looks_like_api_key(val) or len(val) >= 24:
                    return val

        for value in scoped_values:
            candidate = self._normalize_api_key_candidate(value)
            if not candidate:
                continue
            m = re.search(
                r"(v1:team_[A-Za-z0-9]{10,80}:vcp_[A-Za-z0-9]{24,80})",
                candidate,
            )
            if m:
                return m.group(1)
            if self._looks_like_api_key(candidate):
                return candidate
            if re.fullmatch(r"[A-Za-z0-9_.:\-]{32,220}", candidate) and (
                "vcp_" in candidate or "team_" in candidate or candidate.startswith("v1:")
            ):
                return candidate

        # 点 Copy / Show 后再读
        for name in (r"Copy", r"复制", r"Show", r"Reveal", r"显示"):
            try:
                btn = self.page.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() and btn.first.is_visible(timeout=400):
                    btn.first.click()
                    sleep(0.6)
            except Exception:
                pass
        try:
            blob2 = self.page.inner_text("body") or ""
            for pat in prefixed_patterns:
                m = re.search(pat, blob2)
                if m and len(m.group(0)) >= 24:
                    return m.group(0)
        except Exception:
            pass
        return None
