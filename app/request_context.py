import ipaddress

from fastapi import Request


def client_ip(request: Request) -> str | None:
    """The caller's IP: the first X-Forwarded-For entry if valid, else the connection's address.

    Honoring the first entry is what lets the README's test IPs be sent via X-Forwarded-For.
    Trade-off: a client can set that header itself; behind a known proxy you would trust only
    the entry that proxy appends.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        try:
            return str(ipaddress.ip_address(first))
        except ValueError:
            pass  # garbage header: fall back to the connection address
    return request.client.host if request.client else None
