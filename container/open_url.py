#!/usr/bin/env python3
"""Open a URL in the host desktop's browser from inside the container.

Python's ``webbrowser`` runs the command named by ``$BROWSER``, which the image points at
this script. It asks the desktop's OpenURI portal over the shared D-Bus session, using
jeepney, which is already installed as a dependency of ``keyring``. The URL is always
printed too, so it can be opened by hand if the portal is missing or declines.
"""

from __future__ import annotations

import sys

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection


PORTAL = DBusAddress("/org/freedesktop/portal/desktop",
                     bus_name="org.freedesktop.portal.Desktop",
                     interface="org.freedesktop.portal.OpenURI")


def main() -> int:
    url = sys.argv[1]
    print(f"[container] Open in your browser: {url}", file=sys.stderr)
    try:
        with open_dbus_connection(bus="SESSION") as connection:
            reply = connection.send_and_get_reply(
                new_method_call(PORTAL, "OpenURI", "ssa{sv}", ("", url, {})), timeout=10)
    except Exception as error:
        print(f"[container] Could not reach the desktop portal: {error}", file=sys.stderr)
        return 1
    if reply.header.message_type == MessageType.error:
        print(f"[container] The desktop portal refused to open the URL: {reply.body}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
