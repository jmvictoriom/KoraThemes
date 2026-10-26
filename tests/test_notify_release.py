import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.notify_release import (
    NotificationError, RECIPIENT_POLICY_MARKER, SMTPSettings, addresses, load_event, recipients_for_release,
    release_message, send_notification,
)


def event():
    tag = "v1.2.3"
    return {
        "repository": {"full_name": "jmvictoriom/KoraThemes"},
        "release": {
            "id": 42,
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

    def test_email_validation_matches_the_converters_address_contract(self):
        for invalid in (
            "a@localhost", "a@-example.com", "a@example..com", ".a@example.com", "a..b@example.com",
            "a.@example.com", "Name <a@example.com>", "a@exámple.com",
            "a" * 65 + "@example.com", "a@" + "b" * 64 + ".com",
            "a@" + ".".join(["b" * 63] * 4),
        ):
            with self.subTest(address=invalid), self.assertRaises(NotificationError):
                addresses(invalid)
        self.assertEqual(addresses("A@EXAMPLE.COM,a@example.com"), ["a@example.com"])
        for valid in ("developer+qa@example.com", "a_b@example-domain.org", "o'hara@example.com"):
            self.assertEqual(addresses(valid), [valid])

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


class RecipientSelectionTests(unittest.TestCase):
    def selected_settings(self, recipients):
        variables = {
            "RELEASE_EMAIL_TO": "default@example.com",
            "RELEASE_EMAIL_TO_42": json.dumps({"schema": 1, "tag": "v1.2.3", "recipients": recipients}),
            "RELEASE_EMAIL_TO_99": json.dumps({"schema": 1, "tag": "v9.0.0", "recipients": ["other@example.com"]}),
        }
        return dict(settings(), RELEASE_EMAIL_SETTINGS=json.dumps(variables))

    def test_only_the_releases_saved_recipients_are_selected_and_duplicates_are_normalized(self):
        env = self.selected_settings(["IOS@EXAMPLE.COM", "android@example.com", "ios@example.com"])
        self.assertEqual(recipients_for_release(event()["release"], env),
                         ["ios@example.com", "android@example.com"])

    @patch("scripts.notify_release.smtplib.SMTP")
    def test_the_actual_smtp_envelope_uses_the_selected_addresses_not_the_global_default(self, smtp):
        server = smtp.return_value.__enter__.return_value
        server.send_message.return_value = {}
        output = io.StringIO()
        with redirect_stdout(output):
            send_notification(event(), self.selected_settings(["IOS@EXAMPLE.COM", "android@example.com", "ios@example.com"]))
        self.assertEqual(server.send_message.call_args.kwargs["to_addrs"], ["ios@example.com", "android@example.com"])
        self.assertNotIn("@example.com", output.getvalue())
        self.assertIn("2 recipient(s)", output.getvalue())

    def test_manual_retries_keep_the_same_selection_even_if_the_global_default_changes(self):
        env = self.selected_settings(["original@example.com"])
        variables = json.loads(env["RELEASE_EMAIL_SETTINGS"])
        variables["RELEASE_EMAIL_TO"] = "new-default@example.com"
        env["RELEASE_EMAIL_SETTINGS"] = json.dumps(variables)
        self.assertEqual(recipients_for_release(event()["release"], env), ["original@example.com"])

    @patch("scripts.notify_release.smtplib.SMTP_SSL")
    @patch("scripts.notify_release.smtplib.SMTP")
    def test_an_explicit_empty_selection_skips_email_without_requiring_smtp_configuration(self, smtp, smtp_ssl):
        env = {"RELEASE_EMAIL_SETTINGS": self.selected_settings([])["RELEASE_EMAIL_SETTINGS"]}
        output = io.StringIO()
        with redirect_stdout(output):
            send_notification(event(), env)
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()
        self.assertIn("intentionally skipped", output.getvalue())
        self.assertNotIn("@example.com", output.getvalue())

    @patch("scripts.notify_release.smtplib.SMTP")
    def test_an_empty_selection_does_not_hide_an_invalid_release(self, smtp):
        payload = event()
        payload["release"]["assets"].pop()
        with self.assertRaises(NotificationError):
            send_notification(payload, self.selected_settings([]))
        smtp.assert_not_called()

    def test_new_releases_with_missing_selection_never_fall_back_to_the_global_list(self):
        release = event()["release"]
        release["body"] += "\n" + RECIPIENT_POLICY_MARKER
        with self.assertRaisesRegex(NotificationError, "missing"):
            recipients_for_release(release, settings())
        release["id"] = 43
        with self.assertRaisesRegex(NotificationError, "missing"):
            recipients_for_release(release, self.selected_settings(["a@example.com"]))

    def test_older_releases_keep_the_existing_repository_default(self):
        env = dict(settings(), RELEASE_EMAIL_SETTINGS=json.dumps({"RELEASE_EMAIL_TO": "repository@example.com"}))
        self.assertEqual(recipients_for_release(event()["release"], env), ["repository@example.com"])
        self.assertEqual(recipients_for_release(event()["release"], settings()), ["developer@example.com"])

    def test_malformed_or_mismatched_selections_are_errors_not_fallbacks(self):
        invalid = [
            "not JSON", "[]", "null",
            json.dumps({"schema": 2, "tag": "v1.2.3", "recipients": ["a@example.com"]}),
            json.dumps({"schema": True, "tag": "v1.2.3", "recipients": ["a@example.com"]}),
            json.dumps({"schema": 1, "tag": "v1.2.4", "recipients": ["a@example.com"]}),
            json.dumps({"schema": 1, "tag": "v1.2.3", "recipients": "a@example.com"}),
            json.dumps({"schema": 1, "tag": "v1.2.3", "recipients": [None]}),
        ]
        for value in invalid:
            env = dict(settings(), RELEASE_EMAIL_SETTINGS=json.dumps({"RELEASE_EMAIL_TO_42": value}))
            with self.subTest(selection=value), self.assertRaises(NotificationError):
                recipients_for_release(event()["release"], env)
        for value in ("not JSON", "[]", "null"):
            with self.subTest(settings=value), self.assertRaises(NotificationError):
                recipients_for_release(event()["release"], dict(settings(), RELEASE_EMAIL_SETTINGS=value))

    def test_recipient_entries_cannot_inject_headers_or_add_hidden_addresses(self):
        for address in ("a@example.com\nBcc:other@example.com", "a@example.com,b@example.com",
                        "a@example.com,a@example.com", "Name <a@example.com>", "not-an-email"):
            with self.subTest(address=address), self.assertRaises(NotificationError):
                recipients_for_release(event()["release"], self.selected_settings([address]))

    def test_release_ids_are_strict_positive_integers(self):
        for value in (None, "42", True, 0, -1):
            release = dict(event()["release"], id=value)
            with self.subTest(release_id=value), self.assertRaises(NotificationError):
                recipients_for_release(release, self.selected_settings(["a@example.com"]))


if __name__ == "__main__":
    unittest.main()
