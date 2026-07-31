import socket


def create_udp_listener_socket(
    listen_ip: str,
    *,
    reuse_address: bool = False,
) -> socket.socket:
    """Create an unbound IPv4-only or IPv6-only UDP ingress socket."""

    family = socket.AF_INET6 if ":" in listen_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)

    try:
        if family == socket.AF_INET6:
            sock.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_V6ONLY,
                1,
            )
        if reuse_address:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )
    except BaseException:
        sock.close()
        raise

    return sock
