import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.notify_release import (
    NotificationError, SMTPSettings, addresses, load_event, release_message, send_notification,
)


def event():
    tag = "v1.2.3"
    return {
        "repository": {"full_name": "jmvictoriom/KoraThemes"},
        "release": {
            "tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-10-26T12:00:00Z",
            "html_url": f"https://github.com/jmvictoriom/KoraThemes/releases/tag/{tag}",
            "body": "# v1.2.3\n\n- Updated colors and font styles.\n",
            "assets": [
                {"name": name, "state": "uploaded", "size": 10,
                 "browser_download_url": f"https://github.com/jmvictoriom/KoraThemes/releases/download/{tag}/{name}"}
                for name in ("imagin_light.json", "imagin_dark.json", "imagin_fonts.json", "CHANGELOG.md")
            ],
        },
    }


def settings():
    return {
        "SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "mailer", "SMTP_PASSWORD": "test-only-password",
        "SMTP_FROM": "releases@example.com", "RELEASE_EMAIL_TO": "developer@example.com",
    }


class ReleaseMessageTests(unittest.TestCase):
    def test_contains_the_version_pinned_urls_all_files_and_exact_changelog(self):
        payload = event()
        message = release_message(payload, "releases@example.com", ["developer@example.com"])
        self.assertEqual(message["Subject"], "KoraThemes v1.2.3 - imagin")
        body = message.get_content()
        self.assertIn(payload["release"]["html_url"], body)
        self.assertIn(payload["release"]["body"].strip(), body)
        for asset in payload["release"]["assets"]:
            self.assertIn(asset["browser_download_url"], body)
        self.assertNotIn("/latest/", body)

    def test_incomplete_or_mixed_sets_are_never_notified(self):
        for index in range(4):
            payload = event()
            del payload["release"]["assets"][index]
            with self.subTest(index=index), self.assertRaises(NotificationError):
                release_message(payload, "sender@example.com", ["developer@example.com"])
        for name in ("other_dark.json", "../imagin_dark.json", "imagin_light.json"):
            payload = event()
            payload["release"]["assets"][1]["name"] = name
            with self.subTest(name=name), self.assertRaises(NotificationError):
                release_message(payload, "sender@example.com", ["developer@example.com"])

    def test_drafts_prereleases_missing_changelogs_and_invalid_versions_are_rejected(self):
        invalid = [
            ("draft", True), ("prerelease", True), ("published_at", None),
            ("tag_name", "v01.2.3"), ("tag_name", "v1.2.3\n"), ("tag_name", "v1.2.3-rc.1"),
            ("body", " \n"), ("body", None), ("html_url", "https://example.com/release"),
        ]
        for key, value in invalid:
            payload = event()
            payload["release"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(NotificationError):
                release_message(payload, "sender@example.com", ["developer@example.com"])

    def test_foreign_repositories_are_rejected(self):
        payload = event()
        payload["repository"]["full_name"] = "someone/another-repo"
        with self.assertRaises(NotificationError):
            release_message(payload, "sender@example.com", ["developer@example.com"])

    def test_upload_states_sizes_and_urls_are_checked(self):
        for key, value in [
            ("state", "starter"), ("size", 0), ("size", True),
            ("browser_download_url", "https://example.com/file"),
            ("browser_download_url", "https://github.com/jmvictoriom/KoraThemes/releases/latest/download/imagin_light.json"),
        ]:
            payload = event()
            payload["release"]["assets"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(NotificationError):
                release_message(payload, "sender@example.com", ["developer@example.com"])


class SMTPTests(unittest.TestCase):
    def test_missing_configuration_fails_explicitly(self):
        for key in settings():
            env = settings()
            del env[key]
            with self.subTest(key=key), self.assertRaisesRegex(NotificationError, key):
                SMTPSettings.load(env)

    def test_plaintext_invalid_ports_and_header_injection_are_rejected(self):
        for key, value in [
            ("SMTP_SECURITY", "none"), ("SMTP_PORT", "0"), ("SMTP_PORT", "65536"),
            ("SMTP_PORT", "abc"), ("SMTP_PORT", "587\n"),
            ("SMTP_FROM", "sender@example.com\nBcc: other@example.com"),
            ("RELEASE_EMAIL_TO", "not-an-email"), ("RELEASE_EMAIL_TO", "a@example.com,"),
        ]:
            env = settings()
            env[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(NotificationError):
                SMTPSettings.load(env)

    def test_multiple_recipients_are_supported_without_duplicates(self):
        self.assertEqual(addresses("a@example.com, b@example.com,a@example.com"),
                         ["a@example.com", "b@example.com"])

    def test_password_is_not_in_settings_representation(self):
        self.assertNotIn(settings()["SMTP_PASSWORD"], repr(SMTPSettings.load(settings())))

    @patch("scripts.notify_release.smtplib.SMTP")
    def test_starttls_precedes_login_and_all_recipients_receive_the_message(self, smtp):
        server = smtp.return_value.__enter__.return_value
        server.send_message.return_value = {}
        env = settings()
        env["RELEASE_EMAIL_TO"] += ",other@example.com"
        with redirect_stdout(io.StringIO()):
            send_notification(event(), env)
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
        calls = [call[0] for call in server.method_calls]
        self.assertEqual(calls, ["ehlo", "starttls", "ehlo", "login", "send_message"])
        self.assertEqual(server.send_message.call_args.kwargs["to_addrs"],
                         ["developer@example.com", "other@example.com"])

    @patch("scripts.notify_release.smtplib.SMTP_SSL")
    def test_implicit_tls_is_supported(self, smtp):
        server = smtp.return_value.__enter__.return_value
        server.send_message.return_value = {}
        env = dict(settings(), SMTP_SECURITY="ssl", SMTP_PORT="465")
        with redirect_stdout(io.StringIO()):
            send_notification(event(), env)
        self.assertEqual(smtp.call_args.args, ("smtp.example.com", 465))
        server.starttls.assert_not_called()
        server.login.assert_called_once()

    @patch("scripts.notify_release.smtplib.SMTP")
    def test_partial_recipient_rejection_is_not_reported_as_success(self, smtp):
        server = smtp.return_value.__enter__.return_value
        server.send_message.return_value = {"developer@example.com": (550, b"rejected")}
        with self.assertRaises(NotificationError):
            send_notification(event(), settings())

    @patch("scripts.notify_release.smtplib.SMTP")
    def test_invalid_release_does_not_open_an_smtp_connection(self, smtp):
        payload = event()
        payload["release"]["assets"].pop()
        with self.assertRaises(NotificationError):
            send_notification(payload, settings())
        smtp.assert_not_called()


class RetryTests(unittest.TestCase):
    @patch("scripts.notify_release.urllib.request.urlopen")
    def test_manual_retry_fetches_the_requested_immutable_version(self, open_url):
        open_url.return_value.__enter__.return_value = io.BytesIO(json.dumps(event()["release"]).encode())
        result = load_event({"RELEASE_TAG": "v1.2.3", "GH_TOKEN": "test-only-token"})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/jmvictoriom/KoraThemes/releases/tags/v1.2.3")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-token")
        self.assertEqual(result, event())

    @patch("scripts.notify_release.urllib.request.urlopen")
    def test_invalid_retry_tags_never_make_a_request(self, open_url):
        for tag in ("../secrets", "v1.2.3?x=1", "v1.2.3\n", "latest"):
            with self.subTest(tag=tag), self.assertRaises(NotificationError):
                load_event({"RELEASE_TAG": tag, "GH_TOKEN": "test-only-token"})
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
