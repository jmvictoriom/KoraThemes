# KoraThemes

Versioned theme and fonts JSON files distributed through GitHub Releases.

## Publishing

In KoraThemeConverter, generate both modes and fonts, then choose **Create release**
from the Export menu or Quick start. The converter generates a changelog from the final
JSON compared with this theme's previous published files, including nested components,
colors, dimensions and fonts. It ignores formatting-only changes and never compares
different brands as if they were the same theme.

Both the changelog and suggested version are editable. The first suggestion is `1.0.0`;
later suggestions increment the highest stable version in the repository: removals suggest
a major increment, additions minor and value changes patch. Regenerating the changelog
preserves a manually edited version. Notes summarize all change counts with up to 20 paths
per change type and file; the full JSON files remain attached. A release contains one
snapshot of the selected theme:

- `<name>_light.json`
- `<name>_dark.json`
- `<name>_fonts.json`
- `CHANGELOG.md`, also used as the release notes, including the JSON SHA-256 checksums.

All three JSON files share the `vMAJOR.MINOR.PATCH` release tag. The converter validates
the edited JSON, uploads to a draft and only publishes after checking the whole set.
Published versions are not overwritten. Raw Figma exports and credentials are not uploaded.
An interrupted upload stays in drafts and must be reviewed before reusing its version.
The version is in the release tag, not injected into the native JSON contract.

Before publishing, paste recipient addresses and manage contacts or groups in the converter's
recipients section. Contacts, group membership and selection persist locally on your Mac.
Select people, groups or both; overlapping selections produce only one notification per address.
With nobody selected, the release is still published and email is intentionally skipped.
Publishing requires GitHub Contents: write and Variables: write permissions.

Use pinned URLs to integrate a specific version:

```text
https://github.com/jmvictoriom/KoraThemes/releases/download/v1.0.0/imagin_light.json
https://github.com/jmvictoriom/KoraThemes/releases/download/v1.0.0/imagin_dark.json
https://github.com/jmvictoriom/KoraThemes/releases/download/v1.0.0/imagin_fonts.json
```

`releases/latest/download/<filename>` follows the latest stable release, not the latest
release of each brand. A release only contains its selected theme's set; a different
brand's latest release will not contain these filenames.

## Release email

The **Release email** workflow runs when a release is **published**, not while its files
are being uploaded. It checks the version, complete asset set, download URLs and changelog,
then emails the version-pinned release URL, all downloads and the changelog.

Configure these **Actions secrets** in the repository settings:

| Secret | Value |
| --- | --- |
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_USERNAME` | SMTP authentication user |
| `SMTP_PASSWORD` | SMTP password or provider-specific app password |
| `SMTP_FROM` | Authorized sender email address |

Configure these **Actions variables**:

| Variable | Value |
| --- | --- |
| `RELEASE_EMAIL_TO_<releaseID>` | Per-release recipient selection, saved and verified by the converter before publishing |
| `RELEASE_EMAIL_TO` | Comma-separated fallback for older releases without a saved-selection policy |
| `SMTP_PORT` | Optional; defaults to `587` |
| `SMTP_SECURITY` | Optional; `starttls` (default) or `ssl` for implicit TLS, typically port `465` |

For example, release ID `42` tagged `v1.2.3` uses the private Actions variable
`RELEASE_EMAIL_TO_42`, whose string value is:

```json
{"schema":1,"tag":"v1.2.3","recipients":["developer@example.com"]}
```

The release ID, rather than just the tag or a shared mutable mailing list, selects the
configuration. The saved tag must match. `recipients: []` disables email for that release,
even if a global fallback exists, and does not require SMTP configuration.
The JSON assets and public release notes never include the recipient addresses.
Converter notes include `<!-- kora-release-recipients:v1 -->`: if a release has this marker
but no saved selection, notification fails explicitly instead of emailing the fallback list.
Malformed settings also fail; they are not treated as an empty selection.

Keep each published release's selection variable for retries. Changes to local contacts,
groups or the global fallback do not change its saved recipients. The caller passes private
recipient settings as a secret to `send-release-email.yml`, not as plain logged environment
values; notification output reports counts rather than addresses.

Use a mail service that supports authenticated SMTP for automation and authorizes the
sender. This workflow does not provision an SMTP account or bypass corporate mail policy.
TLS certificate validation is mandatory; there is no plaintext fallback. Missing settings,
authentication failures and rejected recipients fail the workflow explicitly.

After fixing configuration, retry the failed job or use **Actions → Release email →
Run workflow**, entering the published tag. Retrying may deliver a duplicate message if a
previous attempt was accepted by the SMTP service; delivery to an inbox is controlled by
that service. Never paste SMTP credentials into release notes, issues or commits.

Releases created by the converter use the user's GitHub.com credentials, so they trigger
the `release.published` event normally. If another Actions workflow creates releases using
`GITHUB_TOKEN`, GitHub does not emit downstream workflow runs; that publisher must invoke
this workflow explicitly with `workflow_dispatch` after publishing.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

Tests use fake events and mocked SMTP connections; they do not send email.
