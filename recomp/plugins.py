"""The exefs plugins of installed mods, as tools/mod_plugin.py records them in build/plugins.json.

A plugin is a mod's own exlaunch-style subsdkN NSO (the user's download; nothing of it is in this repository). The
generator lifts it like a game module and places it after the game's modules (layout.py); the runtime loads it only
when its mod pack runs (runtime/loader.c). build/plugins.json:

  {"plugins": [{"mod": "<mod pack folder>", "module": "subsdk9", "nso": "<path>", "sha256": "<hex>",
                "set": "plugin_<mod>", "hooks": "<patch table, recomp/patches.py>"}]}

Paths are relative to the repository root or absolute. "hooks" is written by tools/plugin_hooks.py from a capture run
and is optional: a plugin is first lifted without it, run once to capture what it writes into code, then lifted again.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'build' / 'plugins.json'


def _path(p):
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def load(path=DEFAULT):
    """[dict] with 'nso' and 'hooks' as Paths; [] when the file does not exist."""
    path = Path(path)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    out, names = [], set()
    for e in data.get('plugins', []):
        e = dict(e)
        for k in ('mod', 'module', 'nso', 'sha256', 'set'):
            if not e.get(k):
                raise ValueError(f'{path}: a plugin entry has no "{k}"')
        if e['module'] in names:
            raise ValueError(f'{path}: two plugins use module name {e["module"]} (one plugin per subsdk slot)')
        names.add(e['module'])
        e['nso'] = _path(e['nso'])
        e['hooks'] = _path(e['hooks']) if e.get('hooks') else None
        out.append(e)
    return out


def save(entries, path=DEFAULT):
    def rel(p):
        p = Path(p).resolve()
        return p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p.as_posix()
    data = {'plugins': [{**e, 'nso': rel(e['nso']), **({'hooks': rel(e['hooks'])} if e.get('hooks') else {})}
                        for e in entries]}
    for e in data['plugins']:
        if e.get('hooks') is None:
            e.pop('hooks', None)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')


def layout_entries(entries):
    """[(module name, nso path)] for layout.layout()."""
    return [(e['module'], e['nso']) for e in entries]
