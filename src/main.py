from __future__ import annotations

import argparse
import copy
import os
import sys
import traceback
from pathlib import Path

import httpx

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from .browser import BrowserSession, browser_session
from .stealth import get_playwright_api
from .mail_client import UnsnowMail
from .utils import (
    ROOT,
    ensure_dirs,
    load_config,
    record_email_event,
    save_account,
    sleep,
    ts_name,
)
from .v0_register import V0Registrar

console = Console()


def _is_phone_verification_error(exc: BaseException) -> bool:
    """识别手机号验证异常，供窗口生命周期清理使用。"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "手机号验证",
            "手机号码验证",
            "phone verification",
            "verify your phone",
            "phone number",
            "mobile number",
        )
    )


def _requires_fresh_v0_window(exc: BaseException) -> bool:
    """识别手机号和 v0 注册风控页，触发本轮 v0 窗口销毁。"""
    text = str(exc).lower()
    return _is_phone_verification_error(exc) or any(
        marker in text
        for marker in (
            "更换全新 v0 窗口",
            "different sign up method",
            "account recovery form",
            "if you continue to have issues",
            "v0 出口检查失败",
        )
    )


def _verify_v0_egress(session: BrowserSession, cfg: dict) -> None:
    """在提交邮箱前确认 V0 窗口确实走了本地 7890 代理。

    不再固定节点 IP 前缀：先用 httpx 直接通过 7890 查一次出口 IP，
    再和浏览器里看到的出口 IP 对比。节点随便切换都能通过，
    只有浏览器没走 7890（IP 不一致）时才报错。
    若配置了 expected_egress_prefix，则在此基础上额外校验前缀。
    """
    proxy_cfg = ((cfg.get("browser") or {}).get("bitbrowser") or {}).get("proxy") or {}
    if not proxy_cfg.get("enabled") or session.context is None:
        return

    ptype = str(proxy_cfg.get("type") or "http").lower()
    host = str(proxy_cfg.get("host") or "127.0.0.1")
    port = int(proxy_cfg.get("port") or 7890)
    proxy_url = f"{'socks5' if ptype == 'socks5' else 'http'}://{host}:{port}"

    proxy_ip = ""
    try:
        with httpx.Client(proxy=proxy_url, timeout=15.0, trust_env=False) as client:
            proxy_ip = client.get("https://api.ipify.org").text.strip()
    except Exception as exc:
        console.print(f"[yellow]无法通过 {proxy_url} 查询代理出口 IP: {exc}[/yellow]")

    probe = session.context.new_page()
    try:
        probe.goto("https://api.ipify.org", wait_until="domcontentloaded", timeout=30000)
        actual_ip = (probe.locator("body").inner_text(timeout=10000) or "").strip()
    finally:
        try:
            probe.close()
        except Exception:
            pass

    console.print(
        f"[cyan]V0 出口检测: 浏览器 {actual_ip or '未知'} / 代理 {proxy_ip or '未知'}[/cyan]"
    )
    if proxy_ip and actual_ip and actual_ip != proxy_ip:
        raise RuntimeError(
            "V0 出口检查失败：浏览器没有走本地 7890 代理，"
            f"浏览器出口 {actual_ip}，代理出口 {proxy_ip}。"
        )

    expected_prefix = str((cfg.get("v0") or {}).get("expected_egress_prefix") or "").strip()
    if expected_prefix and actual_ip and not actual_ip.startswith(expected_prefix):
        raise RuntimeError(
            "V0 出口检查失败：出口节点与期望不符，"
            f"实际出口 {actual_ip}，期望 {expected_prefix}*。"
        )


def _resolve_mail_browser_id(cfg: dict) -> str:
    """解析固定邮箱窗口 ID，并从本地文件记住首次确认的窗口。"""
    bit_cfg = ((cfg.get("browser") or {}).get("bitbrowser") or {})
    for value in (
        os.getenv("MAIL_BROWSER_ID"),
        bit_cfg.get("mail_browser_id"),
        bit_cfg.get("browser_id"),
    ):
        value = str(value or "").strip()
        if value:
            return value

    storage = cfg.get("storage") or {}
    remembered_name = bit_cfg.get("mail_browser_id_file") or storage.get(
        "mail_browser_id_file"
    ) or "data/mail_browser_id.txt"
    remembered = ROOT / str(remembered_name)
    try:
        value = remembered.read_text(encoding="utf-8").strip()
        if value:
            return value.splitlines()[0].strip()
    except OSError:
        pass
    return ""


def _mail_browser_config(cfg: dict) -> dict:
    """构造固定邮箱窗口配置：保留 Cookie，不参与手机号窗口销毁。"""
    mail_cfg = copy.deepcopy(cfg)
    # 邮箱窗口始终用比特浏览器固定窗口（保留 GitHub 登录态），
    # 不受 browser.v0_mode 切换影响。
    mail_cfg.setdefault("browser", {})["mode"] = "bitbrowser"
    bit_cfg = mail_cfg.setdefault("browser", {}).setdefault("bitbrowser", {})
    fixed_id = _resolve_mail_browser_id(cfg)
    bit_cfg["browser_id"] = fixed_id
    bit_cfg["auto_select_existing"] = bool(bit_cfg.get("mail_auto_select_existing", True))
    bit_cfg["profile_name_hint"] = str(bit_cfg.get("mail_profile_name_hint") or "mail")
    bit_cfg["select_running_as_fixed"] = not bool(fixed_id)
    bit_cfg["new_if_busy"] = False
    bit_cfg["close_existing_before_start"] = False
    bit_cfg["clean_before_start"] = False
    bit_cfg["clean_after_finish"] = False
    bit_cfg["close_after"] = False
    bit_cfg["delete_after"] = False
    bit_cfg["recreate_on_phone_verification"] = False
    bit_cfg["force_new_once"] = False
    bit_cfg["require_existing"] = True
    bit_cfg["preserve_window"] = True
    bit_cfg["incognito"] = False
    bit_cfg["open_args"] = []
    bit_cfg["clean_before_start"] = False
    # 固定邮箱绝不 about:blank / 清缓存（否则 CF 重验要人手点）
    bit_cfg["skip_blank_dwell"] = True
    # 固定邮箱窗口沿用 BitBrowser 窗口自身已配置好的代理，
    # 避免启动脚本时被全局 7890 配置覆盖。
    bit_cfg["proxy"] = {"enabled": False}
    return mail_cfg


def _v0_browser_config(cfg: dict) -> dict:
    """构造每轮独立的 v0 窗口配置。邮箱窗口 ID 不会带入 v0。"""
    v0_cfg = copy.deepcopy(cfg)
    browser_cfg = v0_cfg.setdefault("browser", {})
    # v0 注册窗口可单独指定模式（edge = 本机 Edge，全新临时目录等效无痕）。
    v0_mode = str(browser_cfg.get("v0_mode") or "").strip().lower()
    if v0_mode:
        browser_cfg["mode"] = v0_mode
    bit_cfg = browser_cfg.setdefault("bitbrowser", {})
    bit_cfg["browser_id"] = str(bit_cfg.get("v0_browser_id") or "").strip()
    bit_cfg["auto_select_existing"] = False
    bit_cfg["force_new_once"] = False
    bit_cfg["new_if_busy"] = True
    bit_cfg["close_existing_before_start"] = True
    bit_cfg["profile_name_hint"] = "v0"
    # v0 每轮都使用一次性窗口：启动前清理，结束后清理并删除配置，
    # 避免注册状态、Cookie 和历史窗口影响下一轮，也避免持续占用窗口配额。
    bit_cfg["clean_before_start"] = True
    bit_cfg["clean_after_finish"] = True
    bit_cfg["close_after"] = True
    bit_cfg["delete_after"] = True
    bit_cfg["preserve_window"] = False
    # 每个 V0 注册窗口都以全新的无痕浏览器上下文启动，避免复用历史登录/风控状态。
    bit_cfg["incognito"] = bool(bit_cfg.get("v0_incognito", True))
    bit_cfg["open_args"] = []
    # 无论成功、普通失败还是手机号验证，v0 窗口都在本轮结束时删除；
    # 失败堆栈和账号记录仍保留在 logs/data 中供排查。
    bit_cfg["recreate_on_phone_verification"] = True
    return v0_cfg


def _remember_mail_browser_id(cfg: dict, browser_id: str) -> None:
    if not browser_id:
        return
    storage = cfg.get("storage") or {}
    bit_cfg = ((cfg.get("browser") or {}).get("bitbrowser") or {})
    path_name = bit_cfg.get("mail_browser_id_file") or storage.get(
        "mail_browser_id_file"
    ) or "data/mail_browser_id.txt"
    path = ROOT / str(path_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{browser_id}\n", encoding="utf-8")
    except OSError as exc:
        console.print(f"[yellow]固定邮箱窗口 ID 保存失败: {exc}[/yellow]")


def _open_fixed_mail_session(
    cfg: dict, playwright=None
) -> tuple[BrowserSession, UnsnowMail]:
    """启动并完成一次固定邮箱窗口登录；该会话贯穿全部注册轮次。"""
    session = BrowserSession(_mail_browser_config(cfg), playwright=playwright)
    try:
        session.start()
        if session.page is None:
            raise RuntimeError("固定邮箱窗口没有可用页面")
        _remember_mail_browser_id(cfg, session.bit_id or "")
        mail = UnsnowMail(session.page, cfg)
        mail.open_and_pass_cf()
        mail.wait_until_authenticated()
        return session, mail
    except Exception:
        session.stop()
        raise


def apply_env_overrides(cfg: dict) -> dict:
    """用环境变量覆盖部分配置。"""
    mode = os.getenv("BROWSER_MODE")
    if mode:
        cfg.setdefault("browser", {})["mode"] = mode

    headless = os.getenv("HEADLESS")
    if headless is not None:
        cfg.setdefault("browser", {})["headless"] = headless.lower() in ("1", "true", "yes")

    bit_api = os.getenv("BITBROWSER_API")
    if bit_api:
        cfg.setdefault("browser", {}).setdefault("bitbrowser", {})["api_url"] = bit_api

    bit_id = os.getenv("BITBROWSER_ID")
    if bit_id:
        cfg.setdefault("browser", {}).setdefault("bitbrowser", {})["browser_id"] = bit_id

    force_new = os.getenv("FORCE_NEW_BROWSER")
    if force_new is not None:
        cfg.setdefault("browser", {}).setdefault("bitbrowser", {})["force_new_once"] = (
            force_new.lower() in ("1", "true", "yes", "on")
        )

    local = os.getenv("MAIL_LOCAL_PART")
    if local:
        cfg.setdefault("mail", {})["local_part"] = local

    count = os.getenv("RUN_COUNT")
    if count and count.isdigit():
        cfg.setdefault("run", {})["count"] = int(count)
    return cfg


def run_once(
    cfg: dict,
    email: str | None = None,
    mail_runtime: tuple[BrowserSession, UnsnowMail] | None = None,
    playwright=None,
) -> dict:
    """使用固定邮箱会话完成一轮，并为 v0 单独创建一个浏览器窗口。"""
    requested_email = email.strip() if email else None
    result = {
        "email": None,
        "api_key": None,
        "account_path": None,
        "key_path": None,
        "ok": False,
        "error": None,
    }
    current_email: str | None = None
    stage = "登录邮箱并生成地址"
    v0_session: BrowserSession | None = None
    registrar: V0Registrar | None = None
    owned_mail_session = mail_runtime is None
    mail_session: BrowserSession | None = None
    mail: UnsnowMail | None = None
    owns_playwright = playwright is None
    if playwright is None:
        _sync_pw, _backend = get_playwright_api()
        console.print(f"[cyan]Playwright 后端: {_backend}[/cyan]")
        shared_playwright = _sync_pw().start()
    else:
        shared_playwright = playwright

    try:
        if mail_runtime is None:
            mail_session, mail = _open_fixed_mail_session(cfg, shared_playwright)
        else:
            mail_session, mail = mail_runtime
        if mail_session.page is None or mail is None:
            raise RuntimeError("固定邮箱窗口没有可用页面")

        # 1. 固定邮箱窗口只负责生成地址和轮询验证码，GitHub 登录态始终留在这里。
        current_email = mail.create_or_set_address(requested_email=requested_email)
        result["email"] = current_email
        record_email_event(cfg, current_email, "generated", "邮箱站点生成")
        mail.prepare_for_verification()

        console.print(
            Panel.fit(
                f"[bold]开始注册[/bold]\n邮箱: {current_email}",
                border_style="cyan",
            )
        )

        # 2. 每轮独立创建 v0 窗口；此窗口和固定邮箱窗口完全不同。
        stage = "提交邮箱并获取验证码"
        v0_cfg = _v0_browser_config(cfg)
        with browser_session(v0_cfg, playwright=shared_playwright) as current_v0_session:
            v0_session = current_v0_session
            _verify_v0_egress(current_v0_session, cfg)
            v0_page = current_v0_session.page or current_v0_session.new_page()
            registrar = V0Registrar(v0_page, cfg)
            try:
                def wait_code() -> str:
                    mail.page.bring_to_front()
                    try:
                        console.print("[cyan]已进入验证码页面，切回固定邮箱窗口轮询验证码...[/cyan]")
                        return mail.wait_code(status_check=registrar._raise_for_auth_error)
                    finally:
                        v0_page.bring_to_front()

                registrar.register_with_email(current_email, wait_code_fn=wait_code)

                # 3. 创建 API Key。账号级 settings/keys 页面不依赖项目 slug。
                stage = "创建 API Key"
                api_key = registrar.create_api_key()
                result["api_key"] = api_key

                stage = "保存注册结果"
                account_path, key_path = save_account(
                    cfg,
                    email=current_email,
                    api_key=api_key,
                    extra={
                        "source": "v0-auto",
                        "status": "success",
                        "keys_page": v0_page.url,
                        "v0_browser_id": current_v0_session.bit_id,
                        "mail_browser_id": mail_session.bit_id,
                    },
                )
                result["account_path"] = str(account_path)
                result["key_path"] = str(key_path) if key_path else None
                result["ok"] = True
                record_email_event(cfg, current_email, "success", str(key_path or ""))

                console.print(
                    Panel.fit(
                        f"[bold green]完成[/bold green]\n"
                        f"邮箱: {current_email}\n"
                        f"API Key: {api_key[:10]}...{api_key[-6:]}\n"
                        f"账号文件: {account_path}\n"
                        f"密钥文件: {key_path}",
                        border_style="green",
                    )
                )
            except Exception as exc:
                # 只有 v0 页面触发手机号验证时删除 v0 窗口，固定邮箱窗口保持原样。
                if _requires_fresh_v0_window(exc):
                    reason = (
                        "v0 检测到手机号验证"
                        if _is_phone_verification_error(exc)
                        else "v0 注册页要求更换全新窗口"
                    )
                    current_v0_session.discard(reason)
                raise
            finally:
                # 仅在本轮成功创建密钥后再 logout；失败时保留登录态便于排查/续跑
                if (
                    result.get("ok")
                    and registrar
                    and (cfg.get("run") or {}).get("logout_after_finish", True)
                ):
                    registrar.logout()
        return result
    except Exception as exc:
        result["error"] = f"[{stage}] {exc}"
        log_path = ROOT / cfg["storage"]["logs_dir"] / f"failed_{ts_name()}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            if current_email:
                account_path, _ = save_account(
                    cfg,
                    email=current_email,
                    api_key=result.get("api_key"),
                    extra={
                        "source": "v0-auto",
                        "status": "failed",
                        "failed_stage": stage,
                        "error": str(exc),
                        "log_path": str(log_path),
                        "v0_browser_id": v0_session.bit_id if v0_session else None,
                        "mail_browser_id": mail_session.bit_id if mail_session else None,
                    },
                )
                result["account_path"] = str(account_path)
                record_email_event(cfg, current_email, "failed", result["error"])
        except Exception as save_exc:
            result["error"] += f"；保存失败记录时又发生错误: {save_exc}"
        console.print(
            Panel.fit(
                f"[bold red]本次任务失败[/bold red]\n"
                f"邮箱: {current_email or '尚未生成'}\n阶段: {stage}\n错误: {exc}\n日志: {log_path}",
                border_style="red",
            )
        )
        return result
    finally:
        # 只有 run_once 单独调用时才负责关闭固定邮箱会话；主循环复用的固定会话
        # 由 main 在全部轮次结束后统一停止，且默认保持 BitBrowser 窗口和登录态。
        if owned_mail_session and mail_session:
            if (cfg.get("run") or {}).get("mail_logout_after_finish", False) and mail:
                mail.logout()
            mail_session.stop()
        if owns_playwright:
            shared_playwright.stop()


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="v0 自动注册 + 创建 API Key")
    parser.add_argument("-c", "--config", default=str(ROOT / "config.yaml"), help="配置文件路径")
    parser.add_argument("-e", "--email", default="", help="已废弃：邮箱由登录后的邮箱站点生成")
    parser.add_argument(
        "-n",
        "--count",
        "--attempts",
        dest="count",
        type=int,
        default=0,
        help="总尝试次数，成功和失败都会继续，覆盖配置",
    )
    parser.add_argument(
        "--mode",
        choices=["playwright", "bitbrowser"],
        default="",
        help="浏览器模式",
    )
    parser.add_argument("--headless", action="store_true", help="无头模式（Cloudflare 时不建议）")
    parser.add_argument("--local-part", default="", help="已废弃：邮箱由登录后的邮箱站点生成")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg = apply_env_overrides(cfg)
    if args.email or args.local_part:
        parser.error("当前邮箱地址由登录后的邮箱站点生成，请不要使用 --email 或 --local-part")
    # 兼容旧 config.yaml，但不再把本地配置的字符串当作真实邮箱地址。
    if (cfg.get("mail") or {}).get("local_part"):
        console.print("[yellow]已忽略 mail.local_part：邮箱将由邮箱站点登录后生成[/yellow]")
        cfg.setdefault("mail", {})["local_part"] = ""
    ensure_dirs(cfg)

    if args.mode:
        cfg.setdefault("browser", {})["mode"] = args.mode
    if args.headless:
        cfg.setdefault("browser", {})["headless"] = True
    count = args.count or int((cfg.get("run") or {}).get("count") or 1)
    if count <= 0:
        parser.error("--count 必须大于 0")
    interval = float((cfg.get("run") or {}).get("interval") or 5)

    console.print(
        Panel.fit(
            f"[bold]v0 Auto Register[/bold]\n"
            f"mode={cfg.get('browser', {}).get('mode')}\n"
            f"count={count}\n"
            f"mail_domain={cfg.get('mail', {}).get('domain')}\n"
            f"keys_dir={ROOT / cfg['storage']['keys_dir']}",
            border_style="blue",
        )
    )

    ok_n = 0
    results = []
    continue_on_failure = bool((cfg.get("run") or {}).get("continue_on_failure", True))
    mail_runtime: tuple[BrowserSession, UnsnowMail] | None = None
    shared_playwright=None
    fatal_error: Exception | None = None
    try:
        # 固定邮箱和每轮 v0 窗口共享同一个 Playwright 管理器，避免在同一线程
        # 重复启动 Sync API 事件循环；两个 BitBrowser profile 仍然完全独立。
        _sync_pw, _backend = get_playwright_api()
        console.print(f"[cyan]Playwright 后端: {_backend}[/cyan]")
        shared_playwright = _sync_pw().start()
        mail_runtime = _open_fixed_mail_session(cfg, shared_playwright)
        console.print(
            f"[green]固定邮箱窗口已就绪: {mail_runtime[0].bit_id or '当前会话'}；后续轮次复用此窗口[/green]"
        )
        for i in range(count):
            console.rule(f"[bold]任务 {i + 1}/{count}")
            try:
                r = run_once(
                    cfg,
                    mail_runtime=mail_runtime,
                    playwright=shared_playwright,
                )
            except Exception as exc:
                # run_once 已经有兜底，这里再保护一层，确保单轮异常不打断后续轮次。
                r = {
                    "email": None,
                    "api_key": None,
                    "account_path": None,
                    "key_path": None,
                    "ok": False,
                    "error": f"[任务 {i + 1}] {exc}",
                }
            results.append(r)
            if r.get("ok"):
                ok_n += 1
            elif not continue_on_failure:
                console.print("[yellow]配置要求失败后停止后续轮次[/yellow]")
                break
            if i < count - 1:
                sleep(interval)
    except Exception as exc:
        fatal_error = exc
    finally:
        if mail_runtime:
            fixed_session, fixed_mail = mail_runtime
            if (cfg.get("run") or {}).get("mail_logout_after_finish", False):
                fixed_mail.logout()
            fixed_session.stop()
        if shared_playwright:
            shared_playwright.stop()

    if fatal_error:
        console.print(
            Panel.fit(
                f"[bold red]固定邮箱会话启动失败[/bold red]\n错误: {fatal_error}\n"
                "请检查 mail_browser_id 是否指向已登录 GitHub 的固定 BitBrowser 窗口。",
                border_style="red",
            )
        )
        return 1

    console.rule("[bold]汇总")
    console.print(f"成功 {ok_n}/{count}")
    console.print(f"已保存 API Key 数量: {ok_n}")
    for r in results:
        if r.get("ok"):
            console.print(f"  [OK] {r.get('email')} -> {r.get('key_path')}")
        else:
            console.print(f"  [FAIL] {r.get('email') or '-'} {r.get('error') or ''}")
    require_all = bool((cfg.get("run") or {}).get("require_all_success", False))
    return 0 if (ok_n == count if require_all else ok_n > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
