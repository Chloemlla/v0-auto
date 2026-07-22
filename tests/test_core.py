from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import utils
from src.browser import BitBrowserAPI
from src.github_login import GitHubAutoLogin
from src.mail_client import UnsnowMail
from src.v0_register import V0Registrar


def make_config() -> dict:
    return {
        "mail": {
            "base_url": "https://mail.unsnow.org",
            "inbox_url": "https://mail.unsnow.org/app#inbox",
            "domain": "mail.unsnow.org",
            "code_timeout": 10,
            "code_poll_interval": 0.01,
            "code_length": 6,
        },
        "browser": {"mode": "bitbrowser"},
        "storage": {
            "keys_dir": "data/keys",
            "accounts_dir": "data/accounts",
            "emails_dir": "data/emails",
            "logs_dir": "logs",
        },
    }


class UtilsTests(unittest.TestCase):
    def test_normalize_email_accepts_configured_domain(self) -> None:
        value = utils.normalize_email(" Demo_01@MAIL.UNSNOW.ORG ", "mail.unsnow.org")
        self.assertEqual(value, "demo_01@mail.unsnow.org")

    def test_normalize_email_rejects_wrong_domain(self) -> None:
        with self.assertRaisesRegex(ValueError, "不一致"):
            utils.normalize_email("demo@example.com", "mail.unsnow.org")

    def test_generated_local_part_has_expected_shape(self) -> None:
        local = utils.random_local_part(15)
        self.assertEqual(len(local), 15)
        self.assertRegex(local, r"^[a-z0-9]+$")

    def test_verification_code_requires_context_when_requested(self) -> None:
        text = "Order 123456 was created. Your Vercel verification code is 654321."
        self.assertEqual(
            utils.extract_verification_code(text, 6, require_context=True),
            "654321",
        )
        self.assertIsNone(
            utils.extract_verification_code("Mailbox id: 123456", 6, require_context=True)
        )
        self.assertEqual(
            utils.extract_verification_code(
                "812866 is your Vercel sign up code", 6, require_context=True
            ),
            "812866",
        )

    def test_success_and_failure_records_are_saved(self) -> None:
        cfg = make_config()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            utils, "ROOT", Path(temp_dir)
        ):
            event_path = utils.record_email_event(
                cfg, "demo@mail.unsnow.org", "generated", "unit test"
            )
            account_path, key_path = utils.save_account(
                cfg,
                "demo@mail.unsnow.org",
                "v0_abcdefghijklmnopqrstuvwxyz123456",
                {"status": "success"},
            )
            self.assertTrue(event_path.is_file())
            self.assertTrue(account_path.is_file())
            self.assertTrue(key_path and key_path.is_file())
            self.assertIn("mail.unsnow.org", account_path.read_text(encoding="utf-8"))
            self.assertTrue(
                (Path(temp_dir) / "data/accounts/all_accounts.txt").is_file()
            )


class MailParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mail = UnsnowMail(Mock(), make_config())

    def test_nested_network_payload_is_ingested(self) -> None:
        self.mail._ingest_payload(
            {
                "data": {
                    "messages": [
                        {
                            "id": "new-message",
                            "subject": "Vercel verification code",
                            "from": "login@vercel.com",
                            "html": "<b>Your verification code is 654321</b>",
                        }
                    ]
                }
            }
        )
        self.assertEqual(len(self.mail.emails), 1)
        self.assertEqual(
            self.mail._scan_emails_for_code(["vercel", "verification"], 6),
            "654321",
        )

    def test_baseline_message_is_not_reused(self) -> None:
        old_mail = {
            "id": "old-message",
            "subject": "Vercel verification code",
            "from": "login@vercel.com",
            "text": "Your verification code is 111111",
        }
        self.mail._ingest_payload({"messages": [old_mail]})
        self.mail._baseline_signatures = {
            self.mail._mail_signature(self.mail.emails[0])
        }
        self.assertIsNone(
            self.mail._scan_emails_for_code(["vercel", "verification"], 6)
        )

    def test_auth_detection_requires_mailbox_state(self) -> None:
        self.assertFalse(UnsnowMail._looks_authenticated("Skip to content"))
        self.assertFalse(
            UnsnowMail._looks_authenticated("Continue with GitHub")
        )
        self.assertTrue(
            UnsnowMail._looks_authenticated("Inbox Create mailbox Sign out")
        )
        self.mail.page.url = "https://mail.unsnow.org/app#inbox"
        self.assertTrue(self.mail._is_inbox_url("Inbox"))
        self.assertFalse(self.mail._is_inbox_url("Skip to content"))

    def test_active_mailbox_text_is_detected_for_replacement(self) -> None:
        self.mail._read_mail_text = Mock(
            return_value=(
                "Inbox Replace mailbox Current address "
                "oldbox@mail.unsnow.org Listening Expires"
            )
        )
        self.assertTrue(self.mail._has_active_mailbox())


class V0FlowTests(unittest.TestCase):
    def test_signup_retry_page_is_treated_as_transient(self) -> None:
        page = Mock()
        page.inner_text.return_value = (
            "Please try again or try a different sign up method. "
            "Please complete the account recovery form."
        )
        registrar = V0Registrar(page, {"mail": {"code_length": 6}})
        self.assertTrue(registrar._is_signup_retry_page())
        registrar._raise_for_auth_error()

    def test_phone_verification_is_classified_as_failure(self) -> None:
        page = Mock()
        page.inner_text.return_value = "Please verify your phone number"
        registrar = V0Registrar(page, {"mail": {"code_length": 6}})
        registrar._abort_phone_verification = Mock()
        with self.assertRaisesRegex(RuntimeError, "手机号验证"):
            registrar._raise_for_auth_error()
        registrar._abort_phone_verification.assert_called_once()

    def test_phone_verification_is_not_a_code_page(self) -> None:
        page = Mock()
        page.inner_text.return_value = "Enter your phone number to verify"
        page.locator.return_value.count.return_value = 0
        registrar = V0Registrar(page, {"mail": {"code_length": 6}})
        self.assertFalse(registrar._is_code_page())

    def test_v0_digits_input_is_recognized_as_code_page(self) -> None:
        page = Mock()
        page.inner_text.return_value = "If you are new to Vercel, we sent a code"
        page.locator.return_value.count.return_value = 0
        page.locator.side_effect = lambda selector: Mock(
            count=Mock(return_value=1 if "name=\"digits\"" in selector else 0)
        )
        registrar = V0Registrar(page, {"mail": {"code_length": 6}})
        self.assertTrue(registrar._is_code_page())

    def test_six_otp_boxes_receive_pasted_code(self) -> None:
        # 当前实现默认走 paste 模式：把整串验证码粘贴到首格，站点自动分发到各格，
        # 不再逐格 fill()。这里断言验证码被整串粘贴到第一个输入框。
        page = Mock()
        page.inner_text.return_value = ""
        boxes = Mock()
        boxes.count.return_value = 6
        cells = [Mock() for _ in range(6)]
        # 模拟粘贴后每格已正确分发一位，避免触发「未分发→慢速补填」分支
        for cell, digit in zip(cells, "123456"):
            cell.input_value.return_value = digit
        boxes.nth.side_effect = cells.__getitem__
        page.locator.return_value = boxes
        page.get_by_role.return_value.count.return_value = 0
        registrar = V0Registrar(page, {"mail": {"code_length": 6}})
        with (
            patch("src.v0_register.paste_otp") as paste_mock,
            patch("src.v0_register.human_mouse_wander"),
            patch("src.v0_register.human_sleep"),
        ):
            registrar._fill_verification_code("123456")
        paste_mock.assert_called_once()
        call_args = paste_mock.call_args.args
        self.assertIs(call_args[0], cells[0])
        self.assertEqual(call_args[2], "123456")


class BitBrowserApiTests(unittest.TestCase):
    def test_mail_and_v0_browser_configs_are_separate(self) -> None:
        from src.main import _mail_browser_config, _v0_browser_config

        cfg = {
            "browser": {
                "mode": "bitbrowser",
                "bitbrowser": {
                    "mail_browser_id": "mail-fixed",
                    "v0_browser_id": "",
                    "delete_after": False,
                },
            },
            "storage": {"mail_browser_id_file": "data/mail_browser_id.txt"},
        }
        mail = _mail_browser_config(cfg)["browser"]["bitbrowser"]
        v0 = _v0_browser_config(cfg)["browser"]["bitbrowser"]
        self.assertEqual(mail["browser_id"], "mail-fixed")
        self.assertTrue(mail["require_existing"])
        self.assertTrue(mail["preserve_window"])
        self.assertFalse(mail["delete_after"])
        self.assertEqual(v0["browser_id"], "")
        self.assertFalse(v0["auto_select_existing"])
        self.assertTrue(v0["new_if_busy"])
        self.assertTrue(v0["clean_before_start"])
        self.assertTrue(v0["clean_after_finish"])
        self.assertTrue(v0["close_after"])
        self.assertTrue(v0["delete_after"])
        self.assertFalse(v0["preserve_window"])
        self.assertTrue(v0["recreate_on_phone_verification"])
        self.assertTrue(v0["incognito"])
        self.assertEqual(mail["open_args"], [])
        self.assertEqual(v0["open_args"], [])

    def test_open_browser_passes_incognito_startup_argument(self) -> None:
        api = BitBrowserAPI("http://127.0.0.1:54345")
        with patch.object(
            api,
            "_post",
            return_value={"success": True, "data": {"ws": "http://127.0.0.1:9222"}},
        ) as post:
            info = api.open_browser("browser-1", args=["--incognito"])
        try:
            self.assertEqual(info["endpoint"], "http://127.0.0.1:9222")
            self.assertEqual(
                post.call_args.args[1],
                {"id": "browser-1", "loadExtensions": False, "args": ["--incognito"]},
            )
        finally:
            api.close()

    def test_ping_uses_local_post_and_requires_business_success(self) -> None:
        api = BitBrowserAPI("http://127.0.0.1:54345")
        response = Mock()
        response.is_success = True
        response.json.return_value = {"success": True, "data": "ok"}
        api.client.post = Mock(return_value=response)
        try:
            self.assertTrue(api.ping())
            api.client.post.assert_called_once_with(
                "http://127.0.0.1:54345/health", json={}
            )
        finally:
            api.close()

    def test_create_profile_enables_clean_start(self) -> None:
        api = BitBrowserAPI("http://127.0.0.1:54345")
        with patch.object(
            api, "_post", return_value={"success": True, "data": {"id": "browser-1"}}
        ) as post:
            browser_id = api.create_browser(
                proxy={"enabled": False}, clean_before_launch=True
            )
        try:
            self.assertEqual(browser_id, "browser-1")
            payload = post.call_args.args[1]
            self.assertTrue(payload["clearCacheFilesBeforeLaunch"])
            self.assertTrue(payload["clearCookiesBeforeLaunch"])
            self.assertFalse(payload["syncCookies"])
            self.assertFalse(payload["syncLocalStorage"])
        finally:
            api.close()

    def test_list_profiles_reads_bitbrowser_data_list(self) -> None:
        api = BitBrowserAPI("http://127.0.0.1:54345")
        with patch.object(
            api,
            "_post",
            return_value={
                "success": True,
                "data": {"list": [{"id": "browser-1", "name": "v0-key-test"}]},
            },
        ):
            self.assertEqual(api.list_browsers(), [{"id": "browser-1", "name": "v0-key-test"}])
        api.close()

    def test_auto_select_skips_running_profiles(self) -> None:
        from src.browser import BrowserSession

        session = BrowserSession({"browser": {"mode": "bitbrowser"}})
        session.bit_api = Mock()
        session.bit_api.list_browsers.return_value = [
            {"id": "running", "name": "v0-key-running", "createdTime": "2026-07-20 22:00:00"},
            {"id": "closed", "name": "v0-key-closed", "createdTime": "2026-07-20 21:00:00"},
        ]
        session.bit_api.running_browser_ids.return_value = {"running"}
        self.assertEqual(session._auto_select_bitbrowser_id({"auto_select_existing": True}), "closed")

    def test_force_new_once_skips_profile_reuse(self) -> None:
        from src.browser import BrowserSession

        session = BrowserSession({"browser": {"mode": "bitbrowser"}})
        session.bit_api = Mock()
        self.assertIsNone(
            session._auto_select_bitbrowser_id(
                {"auto_select_existing": True, "force_new_once": True}
            )
        )
        session.bit_api.list_browsers.assert_not_called()

    def test_discard_marks_window_for_delete_and_next_round_new_window(self) -> None:
        from src.browser import BrowserSession

        cfg = {
            "browser": {
                "mode": "bitbrowser",
                "bitbrowser": {"recreate_on_phone_verification": True},
            }
        }
        session = BrowserSession(cfg)
        session.bit_id = "old-window"
        session.discard("手机号验证")
        self.assertTrue(session._delete_bit_on_stop)
        self.assertTrue(cfg["browser"]["bitbrowser"]["force_new_once"])

    def test_discard_stop_closes_and_deletes_existing_window(self) -> None:
        from src.browser import BrowserSession

        cfg = {
            "browser": {
                "mode": "bitbrowser",
                "bitbrowser": {"close_after": False, "delete_after": False},
            }
        }
        session = BrowserSession(cfg)
        session.bit_id = "old-window"
        session.bit_api = Mock()
        session.discard("手机号验证")
        session.stop()
        session.bit_api.close_browser.assert_called_once_with("old-window")
        session.bit_api.wait_until_closed.assert_called_once_with("old-window")
        session.bit_api.delete_browser.assert_called_once_with("old-window")


class GitHubCredentialTests(unittest.TestCase):
    def test_plaintext_credentials_use_first_two_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "github_credentials.txt"
            path.write_text("account@example.com\nsecret-password\n", encoding="utf-8")
            helper = GitHubAutoLogin(
                {
                    "github": {
                        "enabled": True,
                        "credentials_file": str(path),
                    }
                }
            )
            self.assertEqual(
                helper._load_credentials(),
                {"username": "account@example.com", "password": "secret-password"},
            )


if __name__ == "__main__":
    unittest.main()
