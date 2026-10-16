"""Bounded local diagnostics for server errors and browser artwork events."""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit, urlunsplit


LOG_LOCK = threading.Lock()
LOG_BYTES = 5 * 1024 * 1024
EVENTS = {'card_removed', 'image_load_failed', 'image_load_recovered'}


def append_log(root: Path, message: str) -> None:
    try:
        with LOG_LOCK:
            root.mkdir(parents=True, exist_ok=True)
            path = root / 'error.log'
            if path.exists() and path.stat().st_size + len(message.encode('utf-8')) > LOG_BYTES:
                path.replace(root / 'error.log.1')
            with path.open('a', encoding='utf-8') as output:
                output.write(message)
    except OSError:
        pass


def log_browser_events(root: Path, events: object) -> None:
    if not isinstance(events, list) or not 1 <= len(events) <= 10:
        raise ValueError('Provide 1 to 10 diagnostic events')
    records = []
    for event in events:
        if not isinstance(event, dict) or event.get('event') not in EVENTS:
            raise ValueError('Unknown diagnostic event')
        record = {'event': event['event']}
        for name, limit in (('reason', 100), ('username', 80), ('date', 10),
                            ('segment', 10), ('surface', 30)):
            if isinstance(event.get(name), str):
                record[name] = event[name][:limit]
        image_id = event.get('imageId')
        if str(image_id).isdigit() and 0 < int(image_id) < 2 ** 63:
            record['imageId'] = int(image_id)
        try:
            url = urlsplit(str(event.get('url') or ''))
            if url.scheme == 'https' and url.hostname == 'image.civitai.com':
                record['url'] = urlunsplit(('https', url.hostname, url.path[:400], '', ''))
        except ValueError:
            pass
        stamp = datetime.now(timezone.utc).isoformat()
        records.append(f'[{stamp}] Browser diagnostic: {json.dumps(record, ensure_ascii=False)}\n')
    append_log(root, ''.join(records))
