"""Code patch table for the generator: guest instructions replaced in the lifted C, switchable at run time.

Lifted code cannot be byte-patched when the game runs, so a patch is applied when the C is generated. A patch file
(gen.py --patches) has two kinds of lines; `#` starts a comment, fields are separated by whitespace:

  set  <name> <env> <default>               a patch set: a run-time switch <env>=0/1, <default> when it is unset
  site <set> <module> <address> <original> <replacement> [<replacement> ...]

  spin <module> <head> <back-edge> [<back-edge> ...]

<address> is a module offset (hex), <original> the instruction word the module has there (checked; a mismatch stops
the generator), each <replacement> an instruction word (hex), `@page:x<n>`, which is `adrp x<n>, <patch page>`
encoded for the site's address, or `@pagebl:<hex offset>`, which is `bl <patch page + offset>` (a host function the
runtime registers there). The patch page is the first 4 KiB page after main's image (layout.py leaves the rest
of the 2 MiB-aligned module slot free); the runtime commits it and owns its contents. Words in use, by byte offset:
+0..15 the frame-rate step (runtime/frame_rate.c), +16..55 the output, swapchain and scene sizes
(runtime/resolution.c res_init), +56..59 the camera's aspect ratio (runtime/resolution.c res_init, set aspect), +60..63
the field-of-view factor (runtime/resolution.c res_fov_live, set fov), +64..71 Violet's frame time in ns
(runtime/frame_dt.c, set fps_dt from tools/fps_patches.py), +72..75 Violet's LOD bias float (runtime/lod.c, set
lod_bias); host function entries: +0xfd0 Violet's Oodle decoder wrapper (runtime/data_watch.c, set data_watch),
+0xfe0 and +0xff0 TotK's zstd decoder (runtime/ov_zstd.c). A new
set takes words outside these.

The replacement words are lifted at the site's address in place of the original instruction, and the generated C reads
`if (rt_patch_on[<set>]) { replacement } else { original }`: one byte load and a branch per executed site. Replacement
words must not branch except a final `bl` (lifted with the site's return address); the original must not be a branch
other than `bl`. So the function's control flow, labels and jump tables are the original's. The generator writes
patches.h (set names, switches, the page address) next to layout.h for the runtime.

Exefs plugins (recomp/plugins.py, tools/plugin_hooks.py) add, in their own table loaded after the base one:

  site <set> <module> <address> <original> <word> ... @jump:<module>+<hex offset>   (or a final @ret)
  entry <module> <address>                   an extra function start (an address a plugin jumps back to)
  code <module> <address> <original> <word>  a word lifted in place of the module's, with no switch: code a plugin
                                             writes into its own image at start-up (its hook trampolines)

A final `@jump:` replacement is a tail jump to that module address, `@ret` a return: a plugin's hook ends its
alternative that way. Several tables may patch the same site with different sets; the generated C then tries the sets
in the reverse order of their tables (a plugin's before the base table's), then the original.

A `spin` line names a spin wait found by tools/spin_waits.py: a loop with head <head> that only another thread can end,
and the direct branches back to it (module offsets). Each back-edge (checked: a b, b.cond, cbz / cbnz or tbz / tbnz to
<head>) is lifted as `rt_spin_gate(c, <head>); goto <head>`, which keeps every register and only adds time
(runtime/kernel.c kern_spin_gate).
"""
from pathlib import Path

PAGE = 0x1000


class PatchError(ValueError):
    pass


def adrp(pc, target, rd):
    imm = (target >> 12) - (pc >> 12)
    if not -(1 << 20) <= imm < (1 << 20):
        raise PatchError(f'{pc:#x}: patch page {target:#x} out of ADRP range')
    imm &= 0x1FFFFF
    return 0x90000000 | (imm & 3) << 29 | (imm >> 2) << 5 | rd


def bl(pc, target):
    imm = (target - pc) >> 2
    if not -(1 << 25) <= imm < (1 << 25):
        raise PatchError(f'{pc:#x}: {target:#x} out of BL range')
    return 0x94000000 | (imm & 0x3FFFFFF)


def resolve(a, page, w):
    """Replacement word w for the site at a: a plain word, ('page', reg), ('pagebl', offset), or a final
    ('jump', absolute target) / ('ret',), which stay tuples for the generator."""
    if not isinstance(w, tuple):
        return w
    if w[0] in ('jump', 'ret'):
        return w
    return adrp(a, page, w[1]) if w[0] == 'page' else bl(a, page + w[1])


class Table:
    """sets: [(name, env, default)]; sites: {module: {offset: [(set index, [word | ('page', reg) | ...], original)]}}
    with the alternatives in the order the generated C tries them; page; spins; entries {module: {offset}};
    code {module: {offset: (original, word)}}."""

    def __init__(self, sets=(), sites=None, page=0, spins=None, entries=None, code=None):
        self.sets = list(sets)
        self.sites = sites or {}
        self.page = page
        self.spins = spins or {}   # {module: {back-edge offset: head offset}}
        self.entries = entries or {}
        self.code = code or {}

    def spins_for(self, name, base):
        """{absolute back-edge address: absolute head address}."""
        return {base + b: base + h for b, h in self.spins.get(name, {}).items()}

    def for_module(self, name, base):
        """{absolute address: [(set index, [words])]} with @page resolved."""
        out = {}
        for off, alts in self.sites.get(name, {}).items():
            a = base + off
            out[a] = [(k, [resolve(a, self.page, w) for w in repl]) for k, repl, _ in alts]
        return out

    def entries_for(self, name, base):
        return sorted(base + o for o in self.entries.get(name, ()))

    def code_for(self, name):
        """{module offset: word} to lift in place of the module's words."""
        return {o: w for o, (_, w) in self.code.get(name, {}).items()}


def _target(where, w, bases):
    mod, _, off = w.partition('+')
    if mod not in bases or not off:
        raise PatchError(f'{where}: {w!r} does not name <module>+<offset> of the layout')
    return bases[mod][0] + int(off, 16)


def load(paths, mods):
    """Table from one patch file or a list of them (later files' sites are tried first); mods is layout() output
    [(name, base, size)]."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    sets, index, sites, spins, entries, code = [], {}, {}, {}, {}, {}
    bases = {n: (b, s) for n, b, s in mods}
    for path in paths:
        own = set()   # sites of this file: one file patches an address once
        for no, raw in enumerate(Path(path).read_text().splitlines(), 1):
            f = raw.split('#', 1)[0].split()
            if not f:
                continue
            where = f'{path}:{no}'
            if f[0] == 'set' and len(f) == 4:
                if f[1] in index:
                    raise PatchError(f'{where}: set {f[1]} defined twice')
                index[f[1]] = len(sets)
                sets.append((f[1], f[2], int(f[3])))
            elif f[0] == 'site' and len(f) >= 6:
                if f[1] not in index:
                    raise PatchError(f'{where}: unknown set {f[1]}')
                if f[2] not in bases:
                    raise PatchError(f'{where}: module {f[2]} not in the layout')
                off, orig = int(f[3], 16), int(f[4], 16)
                repl = []
                for j, w in enumerate(f[5:], 5):
                    final = j == len(f) - 1
                    if w.startswith('@page:x'):
                        repl.append(('page', int(w[7:])))
                    elif w.startswith('@pagebl:'):
                        if not final:
                            raise PatchError(f'{where}: @pagebl must be the last replacement word')
                        repl.append(('pagebl', int(w[8:], 16)))
                    elif w.startswith('@jump:'):
                        if not final:
                            raise PatchError(f'{where}: @jump must be the last replacement word')
                        repl.append(('jump', _target(where, w[6:], bases)))
                    elif w == '@ret':
                        if not final:
                            raise PatchError(f'{where}: @ret must be the last replacement word')
                        repl.append(('ret',))
                    else:
                        repl.append(int(w, 16))
                if (f[2], off) in own:
                    raise PatchError(f'{where}: {f[2]}+{off:#x} patched twice')
                own.add((f[2], off))
                alts = sites.setdefault(f[2], {}).setdefault(off, [])
                if any(a[2] != orig for a in alts):
                    raise PatchError(f'{where}: {f[2]}+{off:#x} has another original word in an earlier table')
                alts.insert(0, (index[f[1]], repl, orig))
            elif f[0] == 'spin' and len(f) >= 4:
                if f[1] not in bases:
                    raise PatchError(f'{where}: module {f[1]} not in the layout')
                mod, head = spins.setdefault(f[1], {}), int(f[2], 16)
                for b in f[3:]:
                    b = int(b, 16)
                    if b in mod:
                        raise PatchError(f'{where}: {f[1]}+{b:#x} is a back-edge twice')
                    mod[b] = head
            elif f[0] == 'entry' and len(f) == 3:
                if f[1] not in bases:
                    raise PatchError(f'{where}: module {f[1]} not in the layout')
                entries.setdefault(f[1], set()).add(int(f[2], 16))
            elif f[0] == 'code' and len(f) == 5:
                if f[1] not in bases:
                    raise PatchError(f'{where}: module {f[1]} not in the layout')
                mod, off = code.setdefault(f[1], {}), int(f[2], 16)
                if off in mod:
                    raise PatchError(f'{where}: {f[1]}+{off:#x} has two code words')
                mod[off] = (int(f[3], 16), int(f[4], 16))
            else:
                raise PatchError(f'{where}: cannot parse {raw!r}')
    for name, offs in code.items():
        clash = set(offs) & set(sites.get(name, {}))
        if clash:
            raise PatchError(f'{name}+{min(clash):#x}: both a code word and a patch site')
    base, size = bases['main'][0], bases['main'][1]
    page = (base + size + PAGE - 1) & ~(PAGE - 1)
    nxt = min((b for _, b, _ in mods if b > base), default=None)
    if nxt is not None and page + PAGE > nxt:
        raise PatchError(f'no room for the patch page after main ({page:#x})')
    return Table(sets, sites, page, spins, entries, code)


def check(table, mod, name):
    """Stops on a site (or code word) whose original word differs from the module's file. mod must be the module
    without its code words applied (module.Module with code=None)."""
    want_at = {off: alts[0][2] for off, alts in table.sites.get(name, {}).items()}
    want_at.update({off: orig for off, (orig, _) in table.code.get(name, {}).items()})
    for off, want in want_at.items():
        a = mod.base + off
        if not mod.in_text(a):
            raise PatchError(f'{name}+{off:#x}: patch site outside .text')
        have = mod.word(a)
        if have != want:
            raise PatchError(f'{name}+{off:#x}: module has {have:08x}, the patch expects {want:08x}')
    for off in table.entries.get(name, ()):
        if not mod.in_text(mod.base + off):
            raise PatchError(f'{name}+{off:#x}: entry outside .text')
    for b, head in table.spins.get(name, {}).items():
        a = mod.base + b
        if not mod.in_text(a) or branch_target(mod.word(a), a) != mod.base + head:
            raise PatchError(f'{name}+{b:#x}: not a direct branch to the spin wait head {name}+{head:#x}')


def branch_target(w, a):
    """Target of the direct branch w at a (b, b.cond, cbz / cbnz, tbz / tbnz), or None."""
    if w >> 26 == 0x05:
        return a + ((((w & 0x3FFFFFF) ^ 0x2000000) - 0x2000000) << 2)
    if w & 0xFF000010 == 0x54000000 or w & 0x7E000000 == 0x34000000:
        return a + ((((w >> 5) & 0x7FFFF) ^ 0x40000) - 0x40000 << 2)
    if w & 0x7E000000 == 0x36000000:
        return a + ((((w >> 5) & 0x3FFF) ^ 0x2000) - 0x2000 << 2)
    return None


def header_text(table, marker):
    lines = [f'// {marker}: code patch sets, generated by recomp/gen.py --patches. Do not edit or commit.',
             '#pragma once', '', f'#define RT_PATCH_COUNT {len(table.sets)}',
             f'#define RT_PATCH_PAGE {table.page:#x}ull',
             '// X(index, name, env switch, default)',
             '#define RT_PATCH_SETS(X) ' + ' '.join(f'X({i}, {n}, "{e}", {d})' for i, (n, e, d) in
                                                    enumerate(table.sets)), '']
    return '\n'.join(lines)
