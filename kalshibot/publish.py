"""Writes the results page to site/index.html and, when configured, uploads
it to a GitHub repository served by GitHub Pages.

.env settings (both needed to publish; otherwise the page is local only):
  RESULTS_REPO=yourname/kalshi-bot-results
  GITHUB_TOKEN=github_pat_...   fine-grained token, "Contents: read and write"
                                on that one repository only

Uploads at most every PUBLISH_EVERY_S seconds, or right away when a bet is
placed or settles, so GitHub Pages stays under its build limit.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger("kalshibot.publish")

GITHUB_API = "https://api.github.com"
PUBLISH_EVERY_S = 600


class SitePublisher:
    def __init__(self, path: str | Path = "site/index.html", repo: str | None = None,
                 token: str | None = None, every_s: int = PUBLISH_EVERY_S):
        self._path = Path(path)
        self._repo = (repo if repo is not None else os.environ.get("RESULTS_REPO", "")).strip()
        self._token = (token if token is not None else os.environ.get("GITHUB_TOKEN", "")).strip()
        self._every_s = every_s
        self._last_upload = 0.0
        self._sha: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._repo and self._token)

    @property
    def url(self) -> str | None:
        if not self._repo or "/" not in self._repo:
            return None
        owner, name = self._repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/"

    def update(self, html: str, force: bool = False) -> bool:
        """Save locally every time; upload when due. Never raises. True if
        it was uploaded this call."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(html, encoding="utf-8")
        except OSError as e:
            logger.warning("Couldn't write %s: %s", self._path, e)
        if not self.enabled or (not force and time.time() - self._last_upload < self._every_s):
            return False
        try:
            self._upload(html)
            self._last_upload = time.time()
            return True
        except Exception as e:
            # Keep the token out of logs: only the status and GitHub's message.
            logger.warning("Results page upload failed: %s", e)
            return False

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _current_sha(self) -> str | None:
        resp = requests.get(f"{GITHUB_API}/repos/{self._repo}/contents/index.html",
                            headers=self._headers(), timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("sha")

    def _upload(self, html: str) -> None:
        if self._sha is None:
            self._sha = self._current_sha()
        for attempt in (1, 2):
            body = {"message": "Update results", "content": base64.b64encode(html.encode("utf-8")).decode()}
            if self._sha:
                body["sha"] = self._sha
            resp = requests.put(f"{GITHUB_API}/repos/{self._repo}/contents/index.html",
                                headers=self._headers(), json=body, timeout=30)
            if resp.status_code in (409, 422) and attempt == 1:
                self._sha = self._current_sha()  # changed elsewhere; retry with the new version
                continue
            if not resp.ok:
                raise RuntimeError(f"GitHub HTTP {resp.status_code}: {resp.text[:200]}")
            self._sha = resp.json().get("content", {}).get("sha")
            logger.info("Results page published: %s", self.url)
            return
