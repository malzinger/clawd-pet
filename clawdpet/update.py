"""Update check against GitHub Releases (read-only, best effort)."""
import json
import urllib.error
import urllib.request

from PyQt5.QtCore import QThread, pyqtSignal

from .config import APP_VERSION, GITHUB_LATEST_API, GITHUB_REPO, RELEASES_URL

def parse_version(tag: str) -> tuple:
    """'v1.2.0' / '1.2' -> (1, 2, 0); non-numeric chunks count as 0."""
    if not tag:
        return ()
    core = tag.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for chunk in core.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def version_is_newer(latest: str, current: str) -> bool:
    """True if the release tag `latest` is a strictly newer version."""
    lv, cv = parse_version(latest), parse_version(current)
    if not lv:
        return False
    n = max(len(lv), len(cv))
    lv += (0,) * (n - len(lv))
    cv += (0,) * (n - len(cv))
    return lv > cv


class UpdateThread(QThread):
    """Fetch the latest GitHub release tag off the GUI thread (best effort)."""
    result = pyqtSignal(str, str)   # (tag_name, html_url); empty tag on failure

    def run(self):
        try:
            req = urllib.request.Request(
                GITHUB_LATEST_API,
                headers={"User-Agent": f"ClawdPet/{APP_VERSION}",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            tag = str(data.get("tag_name") or "")
            url = str(data.get("html_url") or RELEASES_URL)
            self.result.emit(tag, url)
        except (urllib.error.URLError, OSError, ValueError):
            self.result.emit("", "")


def is_trusted_update_url(url: str) -> bool:
    """Only ever open this repo's own release pages in the browser.

    The URL comes from the GitHub API response; a bad or manipulated answer
    must not be able to send the user's browser to an arbitrary site."""
    return url.startswith(f"https://github.com/{GITHUB_REPO}/")
