"""Notify configured recipients only after a complete JSON release is published."""

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


REPOSITORY = "jmvictoriom/KoraThemes"
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
RECIPIENT_POLICY_MARKER = "<!-- kora-release-recipients:v1 -->"
EMAIL = re.compile(
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


class NotificationError(Exception):
    pass


def addresses(value):
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise NotificationError("Email addresses must not contain line breaks.")
    result = [item.strip().lower() for item in value.split(",")]
    if not result or any(
        not item.isascii() or len(item) > 254 or EMAIL.fullmatch(item) is None
        or len(item.split("@", 1)[0]) > 64 or item.startswith(".")
        or item.split("@", 1)[0].endswith(".") or ".." in item.split("@", 1)[0]
        for item in result
    ):
        raise NotificationError("Configure valid, comma-separated email addresses.")
    return list(dict.fromkeys(result))


def release_details(event):
    repository = event.get("repository") if isinstance(event, dict) else None
    if not isinstance(repository, dict) or repository.get("full_name") != REPOSITORY:
        raise NotificationError("The event does not belong to KoraThemes.")
    release = event.get("release")
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise NotificationError("Only published stable releases can be notified.")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or VERSION.fullmatch(tag) is None or not release.get("published_at"):
        raise NotificationError("A published vMAJOR.MINOR.PATCH version is required.")
    url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    if release.get("html_url") != url:
        raise NotificationError("The release URL does not match its version.")
    changelog = release.get("body")
    if not isinstance(changelog, str) or not changelog.strip():
        raise NotificationError("The release changelog is missing.")
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 4 or any(not isinstance(a, dict) for a in assets):
        raise NotificationError("Expected light, dark, fonts and CHANGELOG.md assets.")
    names = [asset.get("name") for asset in assets]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != 4:
        raise NotificationError("Asset names are missing or duplicated.")
    light = [name for name in names if name.endswith("_light.json")]
    if len(light) != 1:
        raise NotificationError("A light JSON asset is required.")
    prefix = light[0][:-len("_light.json")]
    expected = [prefix + suffix for suffix in ("_light.json", "_dark.json", "_fonts.json")] + ["CHANGELOG.md"]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", prefix) is None or set(names) != set(expected):
        raise NotificationError("All three JSON files must have the same name prefix.")
    by_name = {asset["name"]: asset for asset in assets}
    downloads = []
    for name in expected:
        asset = by_name[name]
        download = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        if (
            asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int
            or asset["size"] <= 0
            or asset.get("browser_download_url") != download
        ):
            raise NotificationError("An asset is incomplete or its download URL has the wrong version.")
        downloads.append(f"- {name}: {download}")
    return release, prefix, downloads


def release_message(event, sender, recipients):
    release, prefix, downloads = release_details(event)
    tag, url, changelog = release["tag_name"], release["html_url"], release["body"]
    message = EmailMessage()
    message["Subject"] = f"KoraThemes {tag} - {prefix}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(
        f"KoraThemes {tag}\n\nRelease: {url}\n\nDownloads:\n"
        + "\n".join(downloads)
        + f"\n\nChangelog:\n\n{changelog.strip()}\n"
    )
    return message


def recipients_for_release(release, env):
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise NotificationError("A valid release ID is required to select recipients.")
    raw = env.get("RELEASE_EMAIL_SETTINGS")
    try:
        variables = {} if raw is None else json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise NotificationError("Release recipient settings are not valid JSON.") from error
    if not isinstance(variables, dict):
        raise NotificationError("Release recipient settings must be a JSON object.")
    name = f"RELEASE_EMAIL_TO_{release_id}"
    if name in variables:
        try:
            selection = json.loads(variables[name])
        except (TypeError, json.JSONDecodeError) as error:
            raise NotificationError("This release's recipient selection is not valid JSON.") from error
        if (
            not isinstance(selection, dict) or set(selection) != {"schema", "tag", "recipients"}
            or type(selection["schema"]) is not int or selection["schema"] != 1
            or selection["tag"] != release["tag_name"] or not isinstance(selection["recipients"], list)
            or any(not isinstance(item, str) for item in selection["recipients"])
        ):
            raise NotificationError("The saved recipient selection does not match this release.")
        if not selection["recipients"]:
            return []
        # Validate each entry separately: an entry must not hide additional addresses.
        result = []
        for item in selection["recipients"]:
            if "," in item:
                raise NotificationError("Each selected recipient must be one email address.")
            parsed = addresses(item)
            if len(parsed) != 1:
                raise NotificationError("Each selected recipient must be one email address.")
            result.extend(parsed)
        return list(dict.fromkeys(result))
    if RECIPIENT_POLICY_MARKER in release.get("body", ""):
        raise NotificationError("This release's saved recipient selection is missing; no fallback email was sent.")
    return addresses(variables.get("RELEASE_EMAIL_TO", env.get("RELEASE_EMAIL_TO", "")))


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    security: str
    username: str
    password: str = field(repr=False)
    sender: str
    recipients: list

    @classmethod
    def load(cls, env):
        required = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "RELEASE_EMAIL_TO")
        missing = [key for key in required if not env.get(key, "").strip()]
        if missing:
            raise NotificationError("Missing repository secrets/variables: " + ", ".join(missing))
        security = env.get("SMTP_SECURITY", "starttls")
        if security not in ("starttls", "ssl"):
            raise NotificationError("SMTP_SECURITY must be starttls or ssl; plaintext SMTP is not supported.")
        port = env.get("SMTP_PORT", "587")
        if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise NotificationError("SMTP_PORT must be between 1 and 65535.")
        sender = addresses(env["SMTP_FROM"])
        if len(sender) != 1:
            raise NotificationError("SMTP_FROM must contain exactly one sender address.")
        return cls(env["SMTP_HOST"], int(port), security, env["SMTP_USERNAME"], env["SMTP_PASSWORD"],
                   sender[0], addresses(env["RELEASE_EMAIL_TO"]))


def send_notification(event, env):
    release, _, _ = release_details(event)
    recipients = recipients_for_release(release, env)
    if not recipients:
        print("No recipients selected for this release; email intentionally skipped.")
        return
    settings = SMTPSettings.load(dict(env, RELEASE_EMAIL_TO=",".join(recipients)))
    message = release_message(event, settings.sender, settings.recipients)
    context = ssl.create_default_context()
    if settings.security == "ssl":
        connection = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(settings.host, settings.port, timeout=30)
    with connection as server:
        server.ehlo()
        if settings.security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        server.login(settings.username, settings.password)
        refused = server.send_message(message, from_addr=settings.sender, to_addrs=settings.recipients)
        if refused:
            raise NotificationError("SMTP rejected one or more recipients; check the mail service.")
    print(f"SMTP accepted the notification for {len(settings.recipients)} recipient(s).")


def load_event(env):
    tag = env.get("RELEASE_TAG")
    if not tag:
        path = env.get("GITHUB_EVENT_PATH")
        if not path:
            raise NotificationError("GITHUB_EVENT_PATH or a published RELEASE_TAG is required.")
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if VERSION.fullmatch(tag) is None:
        raise NotificationError("The retry tag must be a stable vMAJOR.MINOR.PATCH version.")
    if not env.get("GH_TOKEN"):
        raise NotificationError("GH_TOKEN is required to look up the release for a retry.")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}",
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "KoraThemes-release-email"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    return {"repository": {"full_name": REPOSITORY}, "release": release}


def main():
    try:
        send_notification(load_event(os.environ), os.environ)
    except NotificationError as error:
        print(f"Release notification failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, smtplib.SMTPException, urllib.error.URLError) as error:
        print(f"Release notification failed ({type(error).__name__}). Check SMTP/network configuration.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
