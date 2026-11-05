"""Reviewed per-build identity and project selection; never a client endpoint option."""
import json
import os
import re
import sys
from pathlib import Path

DEVELOPMENT_PROJECT = 'aibelmhucizupzvvhdvf'
DEFAULT = {'schemaVersion': 1, 'channel': 'beta', 'version': '3.0.0-beta.14',
           'environment': 'development', 'projectRef': DEVELOPMENT_PROJECT, 'preview': False}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError('Invalid build configuration fields')
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or type(value['preview']) is not bool:
        raise ValueError('Invalid build configuration version')
    channel, version, environment, project = (value[k] for k in ('channel', 'version', 'environment', 'projectRef'))
    if channel not in ('beta', 'stable') or environment not in ('development', 'production'):
        raise ValueError('Explicit channel and environment required')
    core = r'3\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
    pattern = core + r'-beta\.[1-9]\d*' if channel == 'beta' else core
    if not isinstance(version, str) or not re.fullmatch(pattern, version):
        raise ValueError('Version and channel disagree')
    if not isinstance(project, str) or not re.fullmatch(r'[a-z]{20}', project):
        raise ValueError('Expected a Supabase project reference, never a URL or credential')
    if environment == 'development' and project != DEVELOPMENT_PROJECT:
        raise ValueError('Development project mismatch')
    if environment == 'production' and (channel != 'stable' or project == DEVELOPMENT_PROJECT):
        raise ValueError('Production needs a distinct explicitly selected stable project')
    if channel == 'stable' and environment != 'production' and not value['preview']:
        raise ValueError('Stable Development builds must be marked preview')
    return dict(value)


def _read(path):
    if not path.is_file() or path.stat().st_size > 4096:
        raise RuntimeError('Build configuration missing or invalid')
    return validate(json.loads(path.read_text(encoding='utf-8')))


def source_path():
    """The build identity a source launch explicitly selected, if any."""
    if getattr(sys, 'frozen', False):
        return None
    value = os.environ.get('CAD_BUILD_CONFIGURATION')
    return Path(value) if value else None


def load():
    if getattr(sys, 'frozen', False):
        return _read(Path(__file__).with_name('build-configuration.json'))
    # A packaged build cannot be repointed. A source launch may name a whole build
    # identity, validated like a package's; endpoint variables alone never repoint it.
    path = source_path()
    return _read(path) if path else validate(DEFAULT)


CONFIG = load()
# A packaged or explicitly selected identity owns its endpoint end to end.
CONFIGURED = bool(getattr(sys, 'frozen', False) or source_path())
CHANNEL = CONFIG['channel']
VERSION = CONFIG['version']
ENVIRONMENT = CONFIG['environment']
PROJECT_REF = CONFIG['projectRef']
APPLICATION_ID = 'CivitaiDiscoveryBeta' if CHANNEL == 'beta' else 'CivitaiArtistDiscovery'
APP_NAME = 'Civitai Discovery 3.0' + (' Beta' if CHANNEL == 'beta' else '')
if CHANNEL == 'stable' and CONFIG['preview'] and ENVIRONMENT == 'development':
    APP_NAME += ' (Development Preview)'
ENDPOINT = f'https://{PROJECT_REF}.supabase.co/functions/v1/community-v1'
