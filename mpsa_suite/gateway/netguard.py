"""Outbound URL safety (pure Python): keep webhooks from reaching the ERP's own network."""

import ipaddress
import socket
from urllib.parse import urlparse


def _blocked(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )


def is_public_url(url: str, resolve=socket.getaddrinfo) -> bool:
    """True only if every address the host resolves to is publicly routable."""
    host = urlparse(url or "").hostname
    if not host:
        return False
    try:
        return not _blocked(host)  # literal IP
    except ValueError:
        pass
    try:
        infos = resolve(host, None)
    except OSError:
        return False
    ips = {info[4][0] for info in infos}
    return bool(ips) and not any(_blocked(ip) for ip in ips)
