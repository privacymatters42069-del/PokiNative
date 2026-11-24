"""Install a mod with an exefs plugin (exlaunch-style subsdk1..subsdk9) so the plugin runs natively in karpur.exe.

    python tools/mod_plugin.py "path/to/Some_Mod" --name somemod

Steps (each can run alone with --step; the default runs them all):

  setup    checks the mod (an exefs plugin NSO, its main.npdm for this game's title, no replaced game module), copies
           the plugin to build/game/mods/<name>/exefs/, makes the RomFS overlay build/game/mods/<name>/romfs with
           tools/mod_overlay.py (kept when it already exists; --romfs-refresh makes it again) and records the plugin
           in build/plugins.json (recomp/plugins.py)
  lift     lifts the plugin alone (recomp/gen.py --modules <plugin>) and builds karpur.exe (tools/build_game.py)
  capture  runs karpur.exe up to the end of the module inits with TOTK_PLUGIN_CAPTURE (runtime/plugin.c): the
           plugin's own start-up runs and every word it writes into code is recorded
  hooks    turns the capture into build/plugins/<name>/hooks.txt (tools/plugin_hooks.py; stops on a plugin built for
           another game version) and adds it to build/plugins.json
  relift   lifts main and the plugin again with the hooks (10-40 min for main) and builds karpur.exe

The capture is cached: hooks.txt records the SHA-256 of the plugin and of main it came from, and the capture and
relift steps are skipped when both are unchanged (--force runs them anyway). Everything derived from the plugin
stays under build/ and generated/.

The game is run with `karpur.exe --mod <name>`. When build/game/karpur.exe is running (locked), the build links
build/game/karpur-new.exe instead and the capture runs that.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'recomp'))
import plugins  # noqa: E402

EXEFS = ROOT / 'game' / 'extracted' / 'update' / 'exefs'
GAME = ROOT / 'build' / 'game'
GEN = ROOT / 'generated'
GAME_MODULES = {'rtld', 'main', 'subsdk0', 'sdk'}


class ModError(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(8 << 20), b''):
            h.update(b)
    return h.hexdigest()


def program_id(npdm):
    """The program id of an NPDM's ACI0 section."""
    b = Path(npdm).read_bytes()
    if b[:4] != b'META' or len(b) < 0x80:
        raise ModError(f'{npdm} is not an NPDM')
    off, size = struct.unpack_from('<II', b, 0x70)
    if b[off:off + 4] != b'ACI0' or size < 0x18:
        raise ModError(f'{npdm} has no ACI0 section')
    return struct.unpack_from('<Q', b, off + 0x10)[0]


def check_mod(mod):
    """[(module name, path)] of the mod's plugins; raises ModError for a mod this build cannot run."""
    exefs = mod / 'exefs'
    if not exefs.is_dir():
        raise ModError(f'{mod} has no exefs folder: a RomFS-only mod needs tools/mod_overlay.py, not this tool')
    found = []
    for p in sorted(exefs.iterdir()):
        if p.name in GAME_MODULES:
            raise ModError(f'the mod replaces the game module {p.name}: not supported (only added subsdk1..9 plugins)')
        if re.fullmatch(r'subsdk[1-9]', p.name):
            if p.read_bytes()[:4] != b'NSO0':
                raise ModError(f'{p} is not an NSO')
            found.append((p.name, p))
    if not found:
        raise ModError(f'{exefs} has no subsdk1..subsdk9 plugin')
    npdm = exefs / 'main.npdm'
    if not npdm.is_file():
        raise ModError(f'{exefs} has no main.npdm, so nothing says which game the plugin was built for')
    want, have = program_id(EXEFS / 'main.npdm'), program_id(npdm)
    if have != want:
        raise ModError(f'the plugin was built for title {have:016x}, the installed game is {want:016x}: it does not '
                       'belong to this game')
    for name, _ in found:
        if (EXEFS / name).exists():
            raise ModError(f'the installed game has its own {name}; the plugin cannot take its slot')
    return found


def locked(exe):
    if not exe.is_file():
        return False
    try:
        with open(exe, 'r+b'):
            return False
    except PermissionError:
        return True


def run(cmd, **kw):
    print('>', ' '.join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=ROOT, **kw)
    if r.returncode:
        raise ModError(f'{Path(str(cmd[1] if len(cmd) > 1 else cmd[0])).name} failed with exit code {r.returncode}')
    return r


def toolchain_env():
    """KARPUR_UCRT for tools/build_game.py: the toolchain the existing build.ninja compiles with (another path would
    change every compile command and rebuild every object), else the repository's toolchain/msys2/ucrt64."""
    env = dict(os.environ)
    if env.get('KARPUR_UCRT'):
        return env
    ucrt = ROOT / 'toolchain' / 'msys2' / 'ucrt64'
    nf = GAME / 'build.ninja'
    if nf.is_file():
        m = re.search(r'^cc = (.+)/bin/clang\.exe$', nf.read_text(errors='replace'), re.M)
        if m and Path(m.group(1)).is_dir():
            ucrt = Path(m.group(1))
    env['KARPUR_UCRT'] = str(ucrt)
    return env


def below_normal():
    return {'creationflags': 0x4000} if os.name == 'nt' else {}   # BELOW_NORMAL_PRIORITY_CLASS


def patch_table():
    host, base = ROOT / 'build' / 'sv_host_targets.txt', ROOT / 'build' / 'fps_patches.txt'
    supported = ROOT / 'build' / 'svhost_supported.txt'
    if supported.is_file() and supported.read_text().strip() == '1' and host.is_file():
        return host
    return base if base.is_file() else None


def lift(modules):
    table = patch_table()
    cmd = [sys.executable, ROOT / 'recomp' / 'gen.py', '--exefs', EXEFS, '--out', GEN, '--modules', ','.join(modules)]
    if table:
        cmd += ['--patches', table]
    run(cmd, **below_normal())


def included_sources():
    """runtime/*.c files another runtime file #includes: compiled there, so build_game.py must leave them out."""
    out = set()
    for p in (ROOT / 'runtime').glob('*.c'):
        out.update(re.findall(r'^#include "([A-Za-z0-9_]+\.c)"', p.read_text(errors='replace'), re.M))
    return ['--skip-runtime', ','.join(sorted(out))] if out else []


def build():
    """Compiles what changed and links karpur.exe, or karpur-new.exe when karpur.exe is running. Returns the exe."""
    exe = GAME / 'karpur.exe'
    if not locked(exe):
        run([sys.executable, ROOT / 'tools' / 'build_game.py', *included_sources()], env=toolchain_env(), **below_normal())
        return exe
    # karpur.exe is in use: compile only, then the same link to karpur-new.exe (only the output and map names change)
    run([sys.executable, ROOT / 'tools' / 'build_game.py', '--compile-only', *included_sources()], env=toolchain_env(), **below_normal())
    env = toolchain_env()
    env['PATH'] = str(Path(env['KARPUR_UCRT']) / 'bin') + os.pathsep + env.get('PATH', '')
    ninja = shutil.which('ninja', path=env['PATH']) or 'ninja'
    cmds = subprocess.run([ninja, '-f', str(GAME / 'build.ninja'), '-t', 'commands', str(GAME / 'karpur.exe')],
                          cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.splitlines()
    link = cmds[-1]
    if '-o build/game/karpur.exe' not in link or 'karpur.map' not in link:
        raise ModError('unexpected link command: ' + link[:200])
    link = link.replace('-o build/game/karpur.exe', '-o build/game/karpur-new.exe').replace('karpur.map', 'karpur-new.map')
    print('> link build/game/karpur-new.exe (build/game/karpur.exe is running)', flush=True)
    r = subprocess.run(link, cwd=ROOT, env=env, shell=True, **below_normal())
    if r.returncode:
        raise ModError(f'link failed with exit code {r.returncode}')
    return GAME / 'karpur-new.exe'


def entry_of(name):
    for e in plugins.load():
        if e['mod'] == name:
            return e
    return None


def save_entry(entry):
    entries = [e for e in plugins.load() if e['mod'] != entry['mod'] and e['module'] != entry['module']]
    plugins.save(entries + [entry])


def setup(mod, name, refresh):
    found = check_mod(mod)
    if len(found) > 1:
        raise ModError(f'the mod has {len(found)} plugins; one per mod is supported')
    module, src = found[0]
    pack = GAME / 'mods' / name
    (pack / 'exefs').mkdir(parents=True, exist_ok=True)
    dst = pack / 'exefs' / module
    shutil.copyfile(src, dst)
    digest = sha256(dst)
    print(f'plugin {module}: {dst} (SHA-256 {digest[:16]}...)')
    if (mod / 'romfs').is_dir():
        manifest = pack / 'romfs' / 'manifest.tsv'
        if refresh or not manifest.is_file():
            run([sys.executable, '-I', ROOT / 'tools' / 'mod_overlay.py', mod, '--romfs', ROOT / 'game' / 'romfs',
                 '--out', pack / 'romfs'])
        else:
            print(f'RomFS overlay kept: {manifest} (--romfs-refresh makes it again)')
    old = entry_of(name)
    entry = {'mod': name, 'module': module, 'nso': dst, 'sha256': digest, 'set': f'plugin_{name}'}
    if old and old.get('hooks') and old['sha256'] == digest:
        entry['hooks'] = old['hooks']
    save_entry(entry)
    return entry


def capture(exe, entry, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    profile = out.parent / 'capture-profile'   # never the player's profile
    profile.mkdir(exist_ok=True)
    env = dict(os.environ, TOTK_PLUGIN_CAPTURE=str(out), TOTK_GPU='null')
    env.pop('TOTK_MOD_PACK', None)
    log = out.parent / 'capture.log'
    cmd = [exe, '--profile', profile, '--mod', entry['mod'], '--boot']
    print('>', ' '.join(str(c) for c in cmd), f'2> {log}', flush=True)
    with open(log, 'wb') as f:
        try:
            r = subprocess.run([str(c) for c in cmd], cwd=exe.parent, env=env, stdout=f, stderr=subprocess.STDOUT,
                               timeout=300, **below_normal())
        except subprocess.TimeoutExpired:
            raise ModError(f'the capture run did not finish in 5 minutes; see {log}')
    if r.returncode or not out.is_file():
        tail = log.read_text(errors='replace').splitlines()[-15:]
        raise ModError(f'the capture run failed (exit {r.returncode}); end of {log}:\n  ' + '\n  '.join(tail))
    for line in log.read_text(errors='replace').splitlines():
        if 'plugin' in line:
            print('  ' + line)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('mod', type=Path, help='the mod folder (exefs/ with the plugin and main.npdm, optional romfs/)')
    ap.add_argument('--name', help='mod pack name (karpur.exe --mod NAME; default: the folder name in lower case)')
    ap.add_argument('--step', choices=['all', 'setup', 'lift', 'capture', 'hooks', 'relift'], default='all')
    ap.add_argument('--romfs-refresh', action='store_true', help='make the RomFS overlay again')
    ap.add_argument('--force', action='store_true', help='capture and lift again even when the cache matches')
    a = ap.parse_args()
    name = a.name or re.sub(r'[^a-z0-9_]+', '_', a.mod.name.lower()).strip('_')
    if not re.fullmatch(r'[a-z0-9_]+', name):
        sys.exit(f'mod_plugin: --name {name!r}: use a-z, 0-9 and _')
    steps = ['setup', 'lift', 'capture', 'hooks', 'relift'] if a.step == 'all' else [a.step]
    work = ROOT / 'build' / 'plugins' / name
    cap, hooks, stamp = work / 'capture.txt', work / 'hooks.txt', work / 'hooks.json'
    main_sha = sha256(EXEFS / 'main')
    try:
        entry = setup(a.mod, name, a.romfs_refresh) if 'setup' in steps else entry_of(name)
        if not entry:
            raise ModError(f'mod {name} is not set up: run --step setup first')
        key = {'plugin': entry['sha256'], 'main': main_sha}
        cached = not a.force and hooks.is_file() and stamp.is_file() and json.loads(stamp.read_text()) == key
        if a.step == 'all' and cached and entry.get('hooks'):
            print(f'hooks cached for this plugin and main: {hooks}; building only')
            build()
            return
        exe = None
        if 'lift' in steps:
            if entry.get('hooks'):   # the first lift is without hooks: capture needs the plain plugin
                entry.pop('hooks')
                save_entry(entry)
            lift([entry['module']])
            exe = build()
        if 'capture' in steps:
            exe = exe or (GAME / 'karpur-new.exe' if locked(GAME / 'karpur.exe') else GAME / 'karpur.exe')
            capture(exe, entry, cap)
        if 'hooks' in steps:
            run([sys.executable, ROOT / 'tools' / 'plugin_hooks.py', cap, '--set', entry['set'],
                 '--env', f'TOTK_PLUGIN_{name.upper()}', '--out', hooks, '--exefs', EXEFS])
            stamp.write_text(json.dumps(key) + '\n')
            entry['hooks'] = hooks
            save_entry(entry)
        if 'relift' in steps:
            if not entry.get('hooks'):
                raise ModError('no hooks recorded: run --step hooks first')
            lift(['main', entry['module']])
            exe = build()
            print(f'done: {exe.name} --mod {name}')
    except ModError as e:
        sys.exit(f'mod_plugin: {e}')


if __name__ == '__main__':
    main()
