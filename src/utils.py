from __future__ import annotations

import json
import re
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

console = Console()

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 兼容旧配置：新增的邮箱审计目录和 OTP 长度使用安全默认值。
    mail_cfg = cfg.get("mail") or {}
    storage_cfg = cfg.get("storage") or {}
    cfg["mail"] = mail_cfg
    cfg["storage"] = storage_cfg
    mail_cfg.setdefault("code_length", 6)
    mail_cfg.setdefault("address_verify_timeout", 20)
    storage_cfg.setdefault("emails_dir", "data/emails")
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    """尽早报告配置错误，避免运行到注册中途才因 KeyError 失败。"""
    mail = cfg.get("mail") or {}
    storage = cfg.get("storage") or {}
    browser = cfg.get("browser") or {}

    for key in ("base_url", "domain"):
        if not str(mail.get(key) or "").strip():
            raise ValueError(f"config.yaml 缺少 mail.{key}")
    for key in ("keys_dir", "accounts_dir", "emails_dir", "logs_dir"):
        if not str(storage.get(key) or "").strip():
            raise ValueError(f"config.yaml 缺少 storage.{key}")
    allowed_modes = {
        "bitbrowser",
        "playwright",
        "edge",
        "msedge",
        "edge_cdp",
        "cdp_edge",
        "native_edge",
        "edge_private",
        "edge_inprivate",
        "private_edge",
    }
    mode = str(browser.get("mode", "bitbrowser") or "bitbrowser").lower()
    v0_mode = str(browser.get("v0_mode") or "").lower()
    if mode not in allowed_modes:
        raise ValueError(f"browser.mode 无效: {mode}")
    if v0_mode and v0_mode not in allowed_modes | {""}:
        raise ValueError(f"browser.v0_mode 无效: {v0_mode}")
    if int(mail.get("code_timeout", 180)) <= 0:
        raise ValueError("mail.code_timeout 必须大于 0")


def ensure_dirs(cfg: dict[str, Any]) -> None:
    for key in ("keys_dir", "accounts_dir", "emails_dir", "logs_dir"):
        p = ROOT / cfg["storage"][key]
        p.mkdir(parents=True, exist_ok=True)


def random_local_part(length: int = 15) -> str:
    if not 3 <= length <= 64:
        raise ValueError("邮箱本地名随机长度必须在 3 到 64 之间")
    alphabet = string.ascii_lowercase + string.digits
    # 邮箱地址属于账号凭据的一部分，使用 secrets 避免可预测的伪随机序列。
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_email(email: str, expected_domain: str | None = None) -> str:
    """校验并规范邮箱，确保页面创建地址与注册时使用的是同一个域名。"""
    value = str(email or "").strip().lower()
    if value.count("@") != 1:
        raise ValueError(f"邮箱格式错误: {email!r}")
    local, domain = value.rsplit("@", 1)
    if not 1 <= len(local) <= 64:
        raise ValueError("邮箱本地名长度必须在 1 到 64 之间")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*[a-z0-9]|[a-z0-9]", local):
        raise ValueError("邮箱本地名只能包含小写字母、数字、点、下划线和连字符，且首尾须为字母或数字")
    if ".." in local:
        raise ValueError("邮箱本地名不能包含连续的点")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", domain):
        raise ValueError(f"邮箱域名格式错误: {domain!r}")
    if expected_domain and domain != str(expected_domain).strip().lower():
        raise ValueError(
            f"邮箱域名 {domain} 与配置 mail.domain={expected_domain} 不一致；"
            "当前邮箱页面只会轮询配置域名"
        )
    return f"{local}@{domain}"


def build_email(cfg: dict[str, Any]) -> str:
    local = (cfg.get("mail") or {}).get("local_part") or ""
    local = str(local).strip()
    if not local:
        local = random_local_part()
    domain = str(cfg["mail"]["domain"]).strip().lower()
    return normalize_email(f"{local}@{domain}", expected_domain=domain)


def extract_verification_code(
    text: str,
    expected_length: int = 6,
    require_context: bool = False,
) -> str | None:
    if not text:
        return None
    if not 4 <= expected_length <= 8:
        raise ValueError("验证码长度必须在 4 到 8 之间")
    digits = rf"(\d{{{expected_length}}})"
    context = (
        r"verification|verify|one[- ]time|security|login|sign[- ]?in|"
        r"sign[- ]?up|signup|code|OTP|验证码|校验码|动态码"
    )
    patterns = [
        rf"(?:{context})[^\d]{{0,80}}{digits}",
        rf"{digits}[^\d]{{0,80}}(?:{context})",
    ]
    if not require_context:
        patterns.append(rf"(?<!\d){digits}(?!\d)")
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return None


def ts_name() -> str:
    # 微秒避免同一邮箱在一秒内重试时覆盖旧记录。
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def record_email_event(
    cfg: dict[str, Any],
    email: str,
    status: str,
    detail: str = "",
) -> Path:
    """追加保存邮箱、域名及运行状态，失败的地址也可追溯。"""
    ensure_dirs(cfg)
    normalized = normalize_email(email, expected_domain=cfg["mail"]["domain"])
    domain = normalized.rsplit("@", 1)[1]
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    path = ROOT / cfg["storage"]["emails_dir"] / "all_emails.txt"
    safe_detail = re.sub(r"[\r\n\t]+", " ", str(detail)).strip()
    append_line(path, f"{now}\t{normalized}\t{domain}\t{status}\t{safe_detail}")
    return path


def save_account(
    cfg: dict[str, Any],
    email: str,
    api_key: str | None,
    extra: dict[str, Any] | None = None,
) -> tuple[Path, Path | None]:
    ensure_dirs(cfg)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    safe = email.replace("@", "_at_").replace(".", "_")
    stamp = ts_name()
    account = {
        "email": email,
        "domain": email.rsplit("@", 1)[1],
        "api_key": api_key,
        "created_at": now,
        **(extra or {}),
    }
    account_path = ROOT / cfg["storage"]["accounts_dir"] / f"{safe}_{stamp}.json"
    save_json(account_path, account)

    key_path = None
    if api_key:
        key_path = ROOT / cfg["storage"]["keys_dir"] / f"{safe}_{stamp}.txt"
        key_path.write_text(
            f"email={email}\napi_key={api_key}\ncreated_at={now}\n",
            encoding="utf-8",
        )
        # 汇总文件，方便批量读取
        all_keys = ROOT / cfg["storage"]["keys_dir"] / "all_keys.txt"
        append_line(all_keys, f"{email}\t{api_key}\t{now}")

    # 成功和失败账号都写入汇总；状态位便于筛选，不会把失败记录当成可用账号。
    status = str(account.get("status") or ("success" if api_key else "failed"))
    error = re.sub(r"[\r\n\t]+", " ", str(account.get("error") or "")).strip()
    all_accounts = ROOT / cfg["storage"]["accounts_dir"] / "all_accounts.txt"
    append_line(
        all_accounts,
        f"{email}\t{account['domain']}\t{status}\t{api_key or ''}\t{now}\t{error}",
    )
    return account_path, key_path


def sleep(sec: float) -> None:
    time.sleep(sec)
