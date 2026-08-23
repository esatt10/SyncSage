"""Egress host/IP filtering + a redirect-validating URL opener.

(Security audit finding C2, 2026-08-23 — see ``docs/security-audit-2026-08-23.md``.)

Before this module, the only egress guard anywhere in pheasant was a scheme
allowlist (``http``/``https`` only, to keep a ``file://`` "web collection"
from reading the host filesystem). Nothing checked *where* an http/https URL
actually pointed, so a source's remote endpoint — or a redirect an upstream
server issued — could name a cloud metadata endpoint
(``169.254.169.254``), an admin surface bound to loopback, or an internal
service on an RFC1918 address, and the fetched (and, for connector fetches,
subsequently indexed and served-back-out-of-search) response would say so.

:func:`check_fetchable` is the primitive: scheme must be http/https, and —
unless the caller opts in with ``allow_private`` — every address the host
resolves to must be a public, non-reserved unicast address. Checking *every*
resolved address, not just the first, is what stops a DNS answer mixing a
public and a private IP from passing the check on the public one while the
actual connection could still land on the private one.

:func:`open_url` wraps ``urllib.request.urlopen`` with a redirect handler
that re-runs :func:`check_fetchable` on every hop — closing the bypass where
an initially-clean URL 301s to a scheme or host the check would have
refused, which plain ``urlopen`` follows without ever consulting the
original guard again.

Known limitations, not attempted here:

- This checks the resolved address at *validation* time, then lets the
  normal connection machinery resolve again at *connect* time. A DNS answer
  that changes between those two lookups (full "DNS rebinding") is not
  defended against — doing so requires pinning the connection to the
  address actually validated, a larger change than this pass makes.
- A hostname that fails to resolve at check time is treated as *passing*
  the check, not failing it — see :func:`check_fetchable`'s docstring for
  why coupling the check itself to DNS availability is a worse trade than
  it looks. A literal IP in the URL (the shape essentially every real SSRF
  payload uses) needs no resolution and is always range-checked.

What this closes is the SSRF and scheme/redirect-bypass findings the audit
named; full rebinding-proof pinning and resolution-dependent hostname
blocking are documented gaps, not silently claimed as covered.

Every caller threads ``security.allow_private_egress`` (default False, one
flag, one meaning) down to this module rather than getting a per-surface
default: the connector fetch paths (``sync/connectors.py`` —
``WebCollectionConnector``/``APIConnector``) are reachable by an
unauthenticated ``POST /sources`` + ``POST /sync``, so the flag stays off
there by default, but it is honored rather than ignored, because a
loopback or in-cluster test/staging endpoint is a real thing an operator
legitimately points a "web collection"/"api" source at (the connector test
suite does exactly this against a local HTTP server) — the sandboxed-guest
``host_fetch`` path is the one exception that never sees the flag at all
(see ``sync/connectors.py::is_fetchable_url``), because untrusted
third-party plugin code reaching an operator's internal network is exactly
what that sandbox exists to deny regardless of the operator's general
egress policy. The four operator-configured integration points (assistant
chat, embeddings, IdP sync, the Synapse router webhook) honor the same flag
for the same reason: a self-hosted LLM/embedding gateway on an internal
network (a Kubernetes ClusterIP service, an on-prem Ollama/vLLM host) is a
common deployment shape, and — after this same audit round locks
``base_url``/``api_key_env`` on those four to operator-only config, not
settable over unauthenticated HTTP (the C1 remediation) — pointing one at a
private address is a deliberate operator choice, not an attacker-reachable
one.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Schemes any egress-checked fetch may use. `urlopen` also speaks `file://`
#: and `ftp://`; leaving those reachable turns a remote-fetch feature into a
#: local-file reader (`file:///proc/self/environ`, private keys) or an
#: unauthenticated-by-construction transfer.
FETCHABLE_SCHEMES = frozenset({"http", "https"})


class EgressBlocked(RuntimeError):
    """A URL (or a redirect target) failed the scheme/host egress check."""


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_fetchable(url: str, *, allow_private: bool = False) -> bool:
    """``True`` iff :func:`check_fetchable` would not raise for ``url``."""
    try:
        check_fetchable(url, allow_private=allow_private)
    except EgressBlocked:
        return False
    return True


#: Hostnames blocked outright, with no DNS lookup required — the check
#: below still resolves and range-checks every other hostname when it can,
#: but these are common enough SSRF targets, and cheap enough to catch
#: without a network round trip, that they are worth a literal-string check
#: before resolution is ever attempted.
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})


def check_fetchable(url: str, *, allow_private: bool = False) -> str:
    """Raise :class:`EgressBlocked` unless ``url`` is safe to fetch. Returns ``url``.

    Two address checks, neither of which requires DNS to be reachable for
    the *check itself* to run:

    - A host that is already a literal IP (``http://169.254.169.254/...``,
      the shape essentially every real SSRF payload actually uses) is
      range-checked directly — no resolution involved.
    - A hostname is resolved and every returned address range-checked
      *when resolution succeeds*. When it does not — DNS unreachable, the
      name does not exist, this process has no network at all — the
      resolution failure is **not** treated as a security denial: it is
      logged and the check passes. The alternative (fail closed on any
      resolution failure) would make every egress-checked call dependent on
      DNS being up at the moment of the check, which is a worse failure
      mode than the SSRF class this closes, and would make this module's
      own behavior differ under test (no network, by design — see
      ``CLAUDE.md`` pillar 3) from production. What DNS resolution *does*
      catch, when available, is exactly the case this trade-off gives up
      nothing real on: a hostname resolving to a private/reserved range,
      which the connection this check is guarding will fail to reach
      anyway if DNS is down. A handful of common SSRF-relevant hostnames
      (``localhost``, ``metadata.google.internal``) are still blocked with
      no resolution at all, since those cost nothing to check as literal
      strings.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in FETCHABLE_SCHEMES:
        raise EgressBlocked(
            f"refusing to fetch {url!r}: only "
            f"{'/'.join(sorted(FETCHABLE_SCHEMES))} URLs may be fetched "
            f"(got scheme {scheme or 'none'!r})"
        )
    host = parsed.hostname
    if not host:
        raise EgressBlocked(f"refusing to fetch {url!r}: no host in URL")
    if allow_private:
        return url
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise EgressBlocked(
            f"refusing to fetch {url!r}: host {host!r} is a well-known local/internal "
            "name. Set security.allow_private_egress: true if this destination is "
            "intentional."
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_disallowed_ip(ip):
            raise EgressBlocked(
                f"refusing to fetch {url!r}: {ip} is a private/loopback/link-local/"
                "reserved address. Set security.allow_private_egress: true if this "
                "destination is intentional (e.g. a self-hosted gateway on an "
                "internal network)."
            )
        return url
    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError as exc:
        logger.debug(
            "egress check could not resolve host %r for %r (%s); "
            "proceeding — resolution failure is not treated as a security denial",
            host,
            url,
            exc,
        )
        return url
    for info in resolved:
        raw_ip = info[4][0].split("%", 1)[0]  # strip an IPv6 zone id, e.g. "fe80::1%eth0"
        try:
            resolved_ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue  # an address this module doesn't understand blocks nothing
        if _is_disallowed_ip(resolved_ip):
            raise EgressBlocked(
                f"refusing to fetch {url!r}: host {host!r} resolves to {resolved_ip}, a "
                "private/loopback/link-local/reserved address. Set "
                "security.allow_private_egress: true if this destination is "
                "intentional (e.g. a self-hosted gateway on an internal network)."
            )
    return url


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-checks every redirect hop before following it.

    `urllib`'s default redirect handling follows a 301/302/303 to whatever
    the response names, with no re-consultation of any guard the *original*
    URL passed — installing this handler (via `build_opener`, which replaces
    the default handler of the same base class) is what makes a redirect to
    a blocked scheme or host raise instead of being followed transparently.
    """

    def __init__(self, allow_private: bool) -> None:
        self._allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803
        check_fetchable(newurl, allow_private=self._allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    allow_private: bool = False,
) -> Any:
    """``urllib.request.urlopen``, but every redirect hop is egress-checked.

    Validates the request's own URL first (so a caller gets the same
    `EgressBlocked` failure whether the bad URL is the initial one or a
    redirect target), then opens through an opener whose redirect handler
    re-validates each subsequent hop.
    """
    check_fetchable(request.full_url, allow_private=allow_private)
    opener = urllib.request.build_opener(_ValidatingRedirectHandler(allow_private))
    return opener.open(request, timeout=timeout)
