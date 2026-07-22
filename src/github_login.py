from __future__ import annotations

"""GitHub 登录辅助。

凭证只从本地两行文本读取，不写入日志。GitHub 的二次验证、验证码和设备确认
仍然保留浏览器内的人工处理入口；OAuth 授权页的常见确认按钮会自动点击。
"""

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page
from rich.console import Console

from .utils import ROOT, sleep

console = Console()


class GitHubAutoLogin:
    def __init__(self, cfg: dict[str, Any]):
        github_cfg = cfg.get("github") or {}
        self.enabled = bool(github_cfg.get("enabled", False))
        configured = str(github_cfg.get("credentials_file") or "github_credentials.txt")
        path = Path(configured)
        self.credentials_path = path if path.is_absolute() else ROOT / path
        self.timeout = int(github_cfg.get("login_timeout", 90))
        self._attempted = False

    def login_if_needed(self, page: Page) -> bool:
        """自动填充账号密码，并持续处理 GitHub 登录后的授权确认页。"""
        if not self.enabled or "github.com" not in (page.url or "").lower():
            return False

        try:
            if not self._attempted:
                credentials = self._load_credentials()
                if not credentials:
                    return False
                login = self._first_visible(page, ("input[name='login']", "#login_field"))
                password = self._first_visible(page, ("input[name='password']", "#password"))
                if login and password:
                    login.fill(credentials["username"])
                    password.fill(credentials["password"])
                    submit = self._first_visible(
                        page,
                        (
                            "input[type='submit'][value*='Sign in' i]",
                            "button[type='submit']",
                            "input[type='submit']",
                        ),
                    )
                    if not submit:
                        console.print("[yellow]未找到 GitHub 登录提交按钮，请在浏览器中点击[/yellow]")
                        return False
                    submit.click(timeout=3000)
                    self._attempted = True
                    console.print("[green]已自动填充 GitHub 登录信息并提交[/green]")

            deadline = time.time() + self.timeout
            while time.time() < deadline:
                url = (page.url or "").lower()
                auth_paths = ("/login", "/sessions", "/oauth/authorize")
                if "github.com" not in url or not any(path in url for path in auth_paths):
                    return True

                # OAuth 授权页依旧位于 github.com，必须点击确认才能回到邮箱站点。
                if self._click_confirmation(page):
                    sleep(2)
                    continue

                try:
                    body = (page.inner_text("body") or "").lower()
                    if any(
                        marker in body
                        for marker in (
                            "incorrect username or password",
                            "incorrect email or password",
                            "authentication failed",
                        )
                    ):
                        console.print("[red]GitHub 账号或密码校验失败，请检查本地凭证文件[/red]")
                        return False
                    if any(
                        marker in body
                        for marker in (
                            "two-factor",
                            "two factor",
                            "authentication code",
                            "verify your identity",
                            "device verification",
                            "二次验证",
                            "设备确认",
                        )
                    ):
                        console.print("[yellow]GitHub 需要二次验证或设备确认，请在浏览器中完成[/yellow]")
                        return False
                except Exception:
                    pass
                sleep(1)

            console.print("[yellow]GitHub 登录等待超时，请在浏览器中完成确认后继续[/yellow]")
            return False
        except Exception as exc:
            console.print(f"[yellow]GitHub 自动登录异常，请在浏览器中继续: {exc}[/yellow]")
            return False

    @classmethod
    def _click_confirmation(cls, page: Page) -> bool:
        """点击 GitHub OAuth 授权页常见的确认按钮。"""
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        if not any(marker in body for marker in ("authorize", "authorization", "allow access", "授权", "允许")):
            return False
        patterns = (
            r"^Authorize(?: .*)?$",
            r"^Allow(?: .*)?$",
            r"^Approve$",
            r"^Grant access$",
            r"^Confirm$",
            r"^Continue(?: .*)?$",
            r"^授权$",
            r"^允许$",
            r"^确认$",
            r"^继续$",
        )
        for pattern in patterns:
            for kind in ("button", "link"):
                try:
                    loc = page.get_by_role(kind, name=re.compile(pattern, re.I))
                    if loc.count() and loc.first.is_visible(timeout=500):
                        loc.first.click(timeout=2500)
                        console.print("[green]已点击 GitHub 授权确认按钮[/green]")
                        return True
                except Exception:
                    continue
        for selector in (
            "input[type='submit'][value*='Authorize' i]",
            "input[type='submit'][value*='Allow' i]",
            "input[type='submit'][value*='Confirm' i]",
            "input[type='submit'][value*='授权' i]",
            "input[type='submit'][value*='确认' i]",
        ):
            try:
                submit = page.locator(selector).first
                if submit.count() and submit.is_visible(timeout=500):
                    submit.click(timeout=2500)
                    console.print("[green]已点击 GitHub 授权确认按钮[/green]")
                    return True
            except Exception:
                continue
        return False

    def _load_credentials(self) -> dict[str, str] | None:
        if not self.credentials_path.is_file():
            console.print(
                f"[yellow]未找到 GitHub 凭证文件: {self.credentials_path}，请在浏览器中手动登录[/yellow]"
            )
            return None
        try:
            lines = self.credentials_path.read_text(encoding="utf-8").splitlines()
            if len(lines) < 2:
                raise ValueError("第一行填写账号或邮箱，第二行填写密码")
            username = lines[0].strip()
            password = lines[1].strip()
            if not username or not password:
                raise ValueError("账号和密码都必须填写")
            return {"username": username, "password": password}
        except Exception as exc:
            console.print(f"[red]GitHub 凭证文件读取失败: {exc}[/red]")
            return None

    @staticmethod
    def _first_visible(page: Page, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() and loc.is_visible(timeout=500):
                    return loc
            except Exception:
                continue
        return None
