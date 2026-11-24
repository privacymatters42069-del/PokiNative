"""Turn a Scarlet/Violet RomFS mod (Trinity layout: <mod>/romfs/<path>) into a RomFS overlay folder (runtime/romfs.c).

Mods such as Pokemon Compass ship their own arc/data.trpfd: the archive's table of contents without the files they
change or add, so the game opens those as loose files (data:/<path>) instead of reading them from arc/data.trpfs.
The runtime serves loose files from an overlay folder: every file there is listed in its manifest.tsv with its size
and SHA-256 and, when it replaces a file of the RomFS (arc/data.trpfd, the Wwise .pck, event.trevt, ...), the
original's SHA-256; a file the RomFS lacks gets "-" and is added.

The mod's files are copied into the folder (hard links when it is on the same drive), so the mod folder stays as it
is. The default folder is romfs-mods next to romfs-index.bin (the runtime loads it when TOTK_ROMFS_MODS is unset);
TOTK_ROMFS_MODS=<folder> points elsewhere, TOTK_ROMFS_MODS=0 turns it off. exefs code is never copied: a Switch
plugin cannot run in the recompiled game; it is reported.

    python -I tools/mod_overlay.py "path/to/Pokemon_Compass" --romfs game/romfs --out build/game/romfs-mods

Several mods: later ones win for a file two of them change. Standard library only.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

CHUNK = 8 << 20


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                return h.hexdigest()
            h.update(b)


def sha256_range(path, offset, size):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        f.seek(offset)
        while size:
            b = f.read(min(CHUNK, size))
            if not b:
                raise SystemExit(f'{path}: ends inside a file')
            h.update(b)
            size -= len(b)
    return h.hexdigest()


def mod_romfs(mod):
    r = mod / 'romfs'
    return r if r.is_dir() else mod


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('mod', type=Path, nargs='+', help='mod folders (with romfs/ inside, or the romfs folder itself)')
    ap.add_argument('--romfs', type=Path, required=True, help='the extracted RomFS folder (index.json, romfs.bin)')
    ap.add_argument('--out', type=Path, required=True, help='overlay folder to write (replaced as a whole)')
    a = ap.parse_args()

    idx = json.loads((a.romfs / 'index.json').read_text(encoding='utf-8'))
    image = a.romfs / 'romfs.bin'
    known = {f['path']: f for f in idx['files']}

    files = {}   # RomFS path -> source file; later mods win
    for mod in a.mod:
        root = mod_romfs(mod)
        if not root.is_dir():
            raise SystemExit(f'{mod}: no such folder')
        for p in sorted(root.rglob('*')):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if rel == 'manifest.tsv':
                    continue
                files[rel] = p
        exefs = mod / 'exefs'
        if exefs.is_dir():
            names = sorted(x.name for x in exefs.iterdir() if x.is_file())
            print(f'{mod.name}: exefs not loaded (Switch code cannot run in the recompiled game): {", ".join(names)}')

    out = a.out
    if out.exists():
        if not (out / 'manifest.tsv').is_file():
            raise SystemExit(f'{out} exists and is not an overlay folder (no manifest.tsv); not replacing it')
        shutil.rmtree(out)
    out.mkdir(parents=True)
    lines, replaced, added, linked = [], 0, 0, 0
    for rel in sorted(files, key=lambda s: s.encode('utf-8')):
        src = files[rel]
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
            linked += 1
        except OSError:
            shutil.copy2(src, dst)
        size = dst.stat().st_size
        orig = '-'
        if rel in known:
            k = known[rel]
            orig = sha256_range(image, k['offset'], k['size'])
            replaced += 1
        else:
            added += 1
        lines.append(f'{rel}\t{size}\t{sha256_file(dst)}\t{orig}\n')
    with open(out / 'manifest.tsv', 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'# RomFS overlay from {", ".join(m.name for m in a.mod)} (tools/mod_overlay.py)\n')
        f.writelines(lines)
    print(f'{out}: {len(lines)} files, {replaced} replace RomFS files, {added} added '
          f'({linked} hard-linked, {len(lines) - linked} copied)')
    for rel in sorted(files):
        if rel in known:
            print(f'  replaces {rel}')


if __name__ == '__main__':
    sys.exit(main())
