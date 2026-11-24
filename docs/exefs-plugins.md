# Exefs plugins in mod packs

Some Switch mods ship native code next to their RomFS files: an exlaunch-style plugin, `exefs/subsdkN` (N = 1 to 9)
plus `exefs/main.npdm`. On a Switch the plugin's start-up writes branches into the game's code (its hooks) and builds
its own trampolines. In the recompiled game those writes would do nothing, because the game runs as lifted C, not as
guest memory.

`tools/mod_plugin.py` handles this on your own machine, with your own copy of the mod:

1. It lifts the plugin like a game module (`recomp/gen.py`). The plugin sits in the layout after every game module,
   so the game's own addresses never move.
2. It runs the game once up to the end of the module inits (`TOTK_PLUGIN_CAPTURE`, `runtime/plugin.c`). The plugin's
   init runs, and every word it writes into main and into its own code is recorded.
3. `tools/plugin_hooks.py` turns that capture into a patch set: each hooked site in main jumps to the plugin's code,
   and the plugin's written trampolines are lifted in place of its file's words.
4. It lifts main and the plugin again with that patch set and builds the game.

Everything derived from the mod (the copied plugin, the capture, the patch set) stays under `build/` and
`generated/`. Nothing from it belongs in the repository.

## Use

```
python tools/mod_plugin.py "path/to/Some_Mod" --name somemod
```

Then start the game with `--mod somemod` (or `TOTK_MOD_PACK=somemod`). The plugin loads only with its mod pack;
without `--mod` the game runs unchanged. The mod's `romfs/` folder, if it has one, becomes the pack's RomFS overlay
(`tools/mod_overlay.py`). `--step setup|lift|capture|hooks|relift` runs one step; `--force` captures again.

The capture is cached by the SHA-256 of the plugin and of main. The loader checks the plugin file against the
recorded hash on every start and stops if it changed, so run the tool again after you update the mod.

## Version check

A plugin hardcodes offsets for the game version it was built for. Setup refuses a `main.npdm` for another title. The
hooks step stops when fewer than half of the hooked sites in main are function starts of the installed main
(`--min-starts`), since on the wrong version an entry hook lands in the middle of a function.

## Limits

- Code writes the plugin makes through its alias after its init returns are not captured.
- One plugin per mod. A mod that replaces a game module (`main`, `sdk`, `subsdk0`) is not supported.
- An instruction in the plugin that the lifter does not handle traps when it runs, as it does in game code.
