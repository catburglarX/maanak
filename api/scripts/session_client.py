"""An HTTP client for the verification suites that renews an expired session.

Access tokens live 15 minutes. The browser application handles this in
``web/js/api.js``: on a 401 it calls the refresh endpoint once and retries the
original request. The verification suites did not, so a suite whose flow includes a
long OCR wait could burn most of the token lifetime and then fail on a later step
with ``session_expired``. The failure moved between steps from run to run, which is
what a clock-driven problem looks like.

Using this client fixes that and has a second benefit: the suites now exercise the
refresh-and-retry path that real sessions depend on, instead of only ever using a
freshly minted token.

``verify_auth_flow.py`` deliberately asserts on expiry and revocation semantics and
must keep using a plain client.
"""

from __future__ import annotations

from typing import Any

import httpx

REFRESH_PATH = "/api/v1/auth/refresh"

#: Renewing a session in response to one of these would either recurse or mask the
#: very thing the caller is testing.
NO_RETRY = ("/auth/sign-in", "/auth/refresh", "/auth/sign-out", "/auth/bootstrap")


class RefreshingClient(httpx.Client):
    """Retries a request once after renewing the session, mirroring the browser."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._renewing = False

    def _csrf_headers(self) -> dict[str, str]:
        token = self.cookies.get("maanak_csrf")
        return {"X-CSRF-Token": token} if token else {}

    def _renew(self) -> bool:
        self._renewing = True
        try:
            response = super().request("POST", REFRESH_PATH, headers=self._csrf_headers())
        except httpx.HTTPError:
            return False
        else:
            return response.status_code == 200
        finally:
            self._renewing = False

    def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        response = super().request(method, url, **kwargs)
        if response.status_code != 401 or self._renewing:
            return response
        if any(fragment in str(url) for fragment in NO_RETRY):
            return response
        if not self._renew():
            return response

        # Refresh rotates the CSRF cookie, so a retried unsafe request has to carry
        # the new token rather than the one it was built with.
        headers = kwargs.get("headers")
        if headers:
            merged = dict(headers)
            if "X-CSRF-Token" in merged:
                merged.update(self._csrf_headers())
                kwargs["headers"] = merged
        return super().request(method, url, **kwargs)
