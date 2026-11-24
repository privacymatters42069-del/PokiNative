"""Turn an exefs plugin capture (runtime/plugin.c, TOTK_PLUGIN_CAPTURE) into a patch table (recomp/patches.py format).

The capture lists every .text word the plugin's start-up changed, in the game's modules (its hooks) and in its own
image (the trampolines and hook stubs it assembles at run time), and the qwords of its data that point at changed
code. This writes, under build/ only (the words are the plugin's and the game's):

  set <set> <env> 0                one switch for the plugin; runtime/patches.c turns it on when the plugin loaded
  site <set> <game module> ...     a hook: the first word of each changed run of a game module becomes a site whose
                                   replacement sets the registers the run sets (movz/movk) and ends with @jump to
                                   where the run branches (b, or br through a register loaded from a literal, a
                                   movz/movk chain or adrp + add) or @ret; other changed words are plain sites
  code <plugin> <offset> ...       each word the plugin wrote into its own .text
  entry <module> <offset>          every address control reaches from the written code: hook targets, the return
                                   addresses of trampolines (branches and code pointers in the written words), and the
                                   pointers to written code in the plugin's data (the trampolines its hooks call)

Version check: the plugin hardcodes offsets of the game version it was built for. A hook (a b at a game function's
first word, the usual exlaunch "replace" hook) on another version lands mid-function, so the tool stops when under
--min-starts of the game-module hook sites are function starts of the installed main (its .eh_frame / symbols).
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'recomp'))
from check_content import GENERATED_MARKER  # noqa: E402

MASK64 = (1 << 64) - 1


class HookError(Exception):
    pass


def sext(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def movz(rd, imm, hw):
    return 0xd2800000 | hw << 21 | imm << 5 | rd


def movk(rd, imm, hw):
    return 0xf2800000 | hw << 21 | imm << 5 | rd


def materialize(rd, v):
    words = [movz(rd, v & 0xffff, 0)]
    for hw in range(1, 4):
        part = (v >> (16 * hw)) & 0xffff
        if part:
            words.append(movk(rd, part, hw))
    return words


def read_capture(path):
    mods, writes, ptrs = {}, defaultdict(dict), []
    for no, line in enumerate(Path(path).read_text().splitlines(), 1):
        f = line.split('#', 1)[0].split()
        if not f:
            continue
        if f[0] == 'module':
            mods[f[1]] = dict(base=int(f[2], 16), size=int(f[3], 16), text=int(f[4], 16), data=int(f[5], 16),
                              game=f[6] == 'game')
        elif f[0] == 'write':
            writes[f[1]][int(f[2], 16)] = (int(f[3], 16), int(f[4], 16))
        elif f[0] == 'ptr':
            ptrs.append((f[1], int(f[2], 16), f[3], int(f[4], 16)))
        else:
            raise HookError(f'{path}:{no}: cannot parse {line!r}')
    return mods, writes, ptrs


def runs(offs):
    out, cur = [], []
    for o in sorted(offs):
        if cur and o != cur[-1] + 4:
            out.append(cur)
            cur = []
        cur.append(o)
    if cur:
        out.append(cur)
    return out


class Space:
    def __init__(self, mods):
        self.mods = mods

    def where(self, a):
        """(module, offset) of an address inside some module's .text, else None."""
        for n, m in self.mods.items():
            if m['base'] <= a < m['base'] + m['text']:
                return n, a - m['base']
        return None


def follow(words, start, base):
    """Symbolic run of a hook's words from module offset start: (registers set {reg: value}, words consumed,
    ('jump', absolute) | ('ret',)), or None when the run does not branch away in a form this tool knows."""
    regs = {}
    o = start
    while o in words:
        w = words[o]
        pc = base + o
        if w & 0xfc000000 == 0x14000000:            # b
            return regs, o + 4 - start, ('jump', pc + 4 * sext(w & 0x3ffffff, 26))
        if w & 0xfffffc1f == 0xd65f0000:            # ret
            return regs, o + 4 - start, ('ret',)
        if w & 0xfffffc1f == 0xd61f0000:            # br xn
            n = (w >> 5) & 31
            if n not in regs:
                return None
            return regs, o + 4 - start, ('jump', regs[n])
        if w == 0xd503201f:                         # nop
            pass
        elif w & 0xff000000 == 0x58000000:          # ldr xt, literal
            lit = o + 4 * sext((w >> 5) & 0x7ffff, 19)
            if lit not in words or lit + 4 not in words:
                return None
            regs[w & 31] = words[lit] | words[lit + 4] << 32
        elif w & 0xff800000 == 0xd2800000:          # movz x
            hw = (w >> 21) & 3
            regs[w & 31] = ((w >> 5) & 0xffff) << (16 * hw)
        elif w & 0xff800000 == 0xf2800000:          # movk x
            hw, r = (w >> 21) & 3, w & 31
            if r not in regs:
                return None
            regs[r] = (regs[r] & ~(0xffff << (16 * hw)) & MASK64) | ((w >> 5) & 0xffff) << (16 * hw)
        elif w & 0x9f000000 == 0x90000000:          # adrp
            imm = sext(((w >> 5) & 0x7ffff) << 2 | (w >> 29) & 3, 21) << 12
            regs[w & 31] = ((pc & ~0xfff) + imm) & MASK64
        elif w & 0xff800000 == 0x91000000:          # add x, x, #imm
            rn, rd = (w >> 5) & 31, w & 31
            if rn not in regs:
                return None
            imm = (w >> 10) & 0xfff
            regs[rd] = (regs[rn] + (imm << 12 if w >> 22 & 1 else imm)) & MASK64
        else:
            return None
        o += 4
    return None


def code_targets(words, base, space):
    """Absolute addresses the plugin's written words reach: b/bl targets and 8-byte literals that point into a
    module's .text."""
    out = set()
    for o, w in words.items():
        pc = base + o
        if w & 0x7c000000 == 0x14000000:            # b / bl
            out.add(pc + 4 * sext(w & 0x3ffffff, 26))
        if o % 8 == 0 and o + 4 in words:
            v = w | words[o + 4] << 32
            if v % 4 == 0 and space.where(v):
                out.add(v)
    return {a for a in out if space.where(a)}


def function_starts(exefs, mods, name):
    from module import Module
    m = Module(name, Path(exefs) / name, mods[name]['base'])
    return set(m.seeds())


def convert(capture, set_name, env, exefs=None, min_starts=0.5):
    mods, writes, ptrs = read_capture(capture)
    space = Space(mods)
    lines = [f'# {GENERATED_MARKER}: exefs plugin hooks by tools/plugin_hooks.py from {Path(capture).name}. '
             'Do not edit or commit.', f'set {set_name} {env} 0']
    entries = set()
    stats = dict(sites=0, hooks=0, plain=0, code=0, entries=0, starts=0, game_hooks=0)
    by_module = {}
    for name, ws in sorted(writes.items()):
        m = mods.get(name)
        if not m:
            raise HookError(f'capture writes module {name}, which it does not list')
        now = {o: w for o, (_, w) in ws.items()}
        if m['game']:
            hooked = []
            for run in runs(ws):
                o, end = run[0], run[-1] + 4
                while o < end:
                    orig = ws[o][0]
                    f = follow(now, o, m['base'])
                    if f:
                        regs, used, how = f
                        repl = [w for r in sorted(regs) for w in materialize(r, regs[r])]
                        if how[0] == 'jump':
                            t = space.where(how[1])
                            if not t:
                                raise HookError(f'{name}+{o:#x}: hook jumps to {how[1]:#x}, outside every module')
                            entries.add(t)
                            tail = f'@jump:{t[0]}+{t[1]:x}'
                        else:
                            tail = '@ret'
                        lines.append(f'site {set_name} {name} {o:x} {orig:08x} '
                                     + ' '.join(f'{w:08x}' for w in repl) + (' ' if repl else '') + tail)
                        hooked.append(o)
                        stats['hooks'] += 1
                        # words after the branch that the hook also wrote (its literals) are never executed
                        lits = {q + d for q in literal_slots(now, o, o + used) for d in (0, 4)}
                        o += used
                        while o < end and o in lits:
                            o += 4
                        continue
                    w = now[o]
                    if w & 0xfc000000 == 0x14000000 or w & 0xfffffc1f in (0xd61f0000, 0xd63f0000):
                        raise HookError(f'{name}+{o:#x}: a hook in a form this tool does not follow')
                    lines.append(f'site {set_name} {name} {o:x} {orig:08x} {w:08x}')
                    stats['plain'] += 1
                    o += 4
            stats['sites'] += len(hooked)
            by_module[name] = hooked
        else:
            for o, (orig, w) in sorted(ws.items()):
                lines.append(f'code {name} {o:x} {orig:08x} {w:08x}')
                stats['code'] += 1
            entries.update(space.where(a) for a in code_targets(now, m['base'], space))
    for src, _, tmod, toff in ptrs:
        entries.add((tmod, toff))
    for name, off in sorted(entries):
        lines.append(f'entry {name} {off:x}')
    stats['entries'] = len(entries)
    stats['sites'] += stats['plain']
    if exefs and 'main' in by_module and by_module['main']:
        starts = function_starts(exefs, mods, 'main')
        base = mods['main']['base']
        direct = [o for o in by_module['main'] if (writes['main'][o][1] & 0xfc000000) == 0x14000000]
        stats['game_hooks'] = len(direct)
        stats['starts'] = sum(base + o in starts for o in direct)
        if direct and stats['starts'] < min_starts * len(direct):
            raise HookError(f'only {stats["starts"]} of {len(direct)} branch hooks of the plugin are at function starts '
                            'of the installed main: the plugin was built for another game version')
    return '\n'.join(lines) + '\n', stats


def literal_slots(words, lo, hi):
    """Offsets of literals that ldr-literal words in [lo, hi) load (both halves are covered by the caller)."""
    out = []
    for o in range(lo, hi, 4):
        w = words.get(o)
        if w is not None and w & 0xff000000 == 0x58000000:
            out.append(o + 4 * sext((w >> 5) & 0x7ffff, 19))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('capture', type=Path)
    ap.add_argument('--set', required=True, help='patch set name, e.g. plugin_<mod>')
    ap.add_argument('--env', required=True, help='its switch variable, e.g. TOTK_PLUGIN_<MOD>')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--exefs', type=Path, default=ROOT / 'game' / 'extracted' / 'update' / 'exefs')
    ap.add_argument('--min-starts', type=float, default=0.5)
    a = ap.parse_args()
    try:
        text, st = convert(a.capture, a.set, a.env, a.exefs, a.min_starts)
    except HookError as e:
        sys.exit(f'plugin_hooks: {e}')
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text)
    print(f'plugin_hooks: {st["sites"]} sites ({st["hooks"]} hooks, {st["plain"]} plain words), {st["code"]} code '
          f'words, {st["entries"]} entries; {st["starts"]} of {st["game_hooks"]} branch hooks at main function '
          f'starts -> {a.out}')


if __name__ == '__main__':
    main()
