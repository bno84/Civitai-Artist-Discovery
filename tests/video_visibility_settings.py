"""Autoplay visibility persists independently and rejects invalid percentages."""
import test_safety
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery.settings import AppSettings

with tempfile.TemporaryDirectory(prefix='cad3-video-visibility-') as temporary:
    path = Path(temporary) / 'settings.json'
    store = AppSettings(path)
    assert store.load()['videoVisibilityPercent'] == 40
    baseline = store.update(browsing_levels_value=[1, 4], detail_video_sound_value=False)
    for percent in (1, 40, 67, 100):
        saved = store.update(video_visibility_percent_value=percent)
        assert AppSettings(path).load() == saved
        assert {k: v for k, v in saved.items() if k != 'videoVisibilityPercent'} == {
            k: v for k, v in baseline.items() if k != 'videoVisibilityPercent'}
    before = path.read_bytes()
    for invalid in (0, 101, -1, True, False, 40.5, '40', [], {}):
        try:
            store.update(video_visibility_percent_value=invalid)
            raise AssertionError(f'Accepted invalid percentage: {invalid!r}')
        except ValueError:
            pass
        assert path.read_bytes() == before
        path.write_text(json.dumps({'videoVisibilityPercent': invalid}), encoding='utf-8')
        assert store.load()['videoVisibilityPercent'] == 40
        path.write_bytes(before)
    # An older file has no new key, and merely reading it must not rewrite it.
    legacy = b'{"dimSeenCards":false,"detailVideoSound":false}'
    path.write_bytes(legacy)
    loaded = store.load()
    assert loaded['videoVisibilityPercent'] == 40 and not loaded['dimSeenCards'] and not loaded['detailVideoSound']
    assert path.read_bytes() == legacy
print('PASS: autoplay percentage defaults, persistence, validation, independent preferences and legacy read preservation')
