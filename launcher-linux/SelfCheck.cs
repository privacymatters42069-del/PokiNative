using System.IO.Compression;
using System.Reflection;
using KarPurLauncher;

namespace KarPurLinux;

static class SelfCheck
{
    public static int Run(bool requirePayload = false)
    {
        string root = Path.Combine(Path.GetTempPath(), "karpur-linux-check-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        int checks = 0;
        void Check(bool condition, string name)
        {
            if (!condition) throw new InvalidOperationException(name);
            checks++; Console.WriteLine("PASS " + name);
        }
        try
        {
            string? payload = null;
            try { payload = BundledFiles.Payload(); }
            catch (FileNotFoundException) { if (requirePayload) throw; }
            if (payload != null)
            {
                using var archive = ZipFile.OpenRead(payload);
                Check(archive.GetEntry("tools/setup_build.py") != null && archive.GetEntry("runtime/platform/linux.c") != null,
                    "packaged Linux build sources");
                foreach (string helper in new[] { "toolchain/python/bin/python3", "toolchain/hactool/hactool", "build/xlate/totk_xlate",
                    "toolchain/zig/zig", "toolchain/portable/bin/ninja" })
                {
                    var entry = archive.GetEntry(helper) ?? throw new InvalidDataException("Missing packaged tool: " + helper);
                    using var input = entry.Open();
                    byte[] elf = new byte[4];
                    input.ReadExactly(elf);
                    Check(elf.SequenceEqual(new byte[] { 0x7f, (byte)'E', (byte)'L', (byte)'F' }) &&
                        (entry.ExternalAttributes >> 16 & 0x49) != 0, "native executable in payload " + helper);
                }
                var compiler = archive.GetEntry("toolchain/portable/bin/clang") ?? throw new InvalidDataException("Portable compiler wrapper missing");
                using (var input = compiler.Open())
                {
                    byte[] shebang = new byte[2];
                    input.ReadExactly(shebang);
                    Check(shebang.SequenceEqual(new byte[] { (byte)'#', (byte)'!' }), "portable compiler wrapper in payload");
                }
                Check(new[] { "toolchain/portable/bin/llvm-ar", "toolchain/deps-linux/include/SDL3/SDL.h",
                    "toolchain/deps-linux/include/vulkan/vulkan.h", "toolchain/deps-linux/include/vma/vk_mem_alloc.h",
                    "toolchain/deps-linux/include/opus/opus.h", "toolchain/deps-linux/include/vpx/vpx_decoder.h",
                    "toolchain/deps-linux/lib/libopus.a", "toolchain/deps-linux/lib/libvpx.a", "toolchain/deps-linux/lib/libzstd.a",
                    "toolchain/deps-linux/lib/libclang_rt.builtins-x86_64.a", "toolchain/deps-linux/lib/libvulkan.so",
                    "toolchain/deps-linux/lib/libSDL3.so.0" }.All(name => archive.GetEntry(name) != null),
                    "portable Linux build headers and libraries in payload");
                Check(File.Exists(Path.Combine(Path.GetDirectoryName(payload)!, "recomplauncher.json")) ||
                    archive.GetEntry("recomplauncher.json") != null, "RecompLauncher manifest in Linux package");
                Check(!archive.Entries.Any(e => e.FullName.StartsWith("game/", StringComparison.Ordinal) ||
                    e.FullName.StartsWith("generated/", StringComparison.Ordinal) || e.FullName.EndsWith(".keys", StringComparison.OrdinalIgnoreCase)),
                    "payload excludes game data and keys");
            }
            if (Environment.GetEnvironmentVariable("APPDIR") is { Length: > 0 } appDir)
                Check(File.Exists(Path.Combine(appDir, "recomplauncher.json")),
                    "RecompLauncher manifest at AppImage root");
            Check(Updates.IsSupportedAppImageAsset($"PokiNative-{Program.Version}-linux-x86_64.AppImage"),
                "current PokiNative Linux AppImage release assets are recognized");
            Check(Updates.IsSupportedAppImageAsset("PokiNative-0.4.6.1-linux-x86_64.AppImage"),
                "previous PokiNative Linux AppImage release assets remain recognized");
            Check(Updates.IsSupportedAppImageAsset("KarPur-Linux-Launcher-0.4.2.AppImage"),
                "legacy KarPur Linux AppImage assets remain recognized");
            Check(!Updates.IsSupportedAppImageAsset($"PokiNative-{Program.Version}-Windows-x64.zip"),
                "non-AppImage release assets are ignored");
            Check(Program.Version == typeof(Program).Assembly.GetName().Version?.ToString() &&
                Program.BuildRevision == Program.Version,
                "Linux launcher version follows the project assembly version");
            string? buildLabel = typeof(Program).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>()
                .FirstOrDefault(attribute => attribute.Key == "KarPurBuildLabel")?.Value;
            Check(Program.DisplayVersion == (string.IsNullOrWhiteSpace(buildLabel)
                    ? Program.Version : $"{Program.Version} ({buildLabel})"),
                "Linux launcher displays its build label separately from its numeric version");
            Check(MainWindow.IssuesUrl == "https://github.com/privacymatters42069-del/PokiNative/issues/new" &&
                MainWindow.PullRequestsUrl == "https://github.com/privacymatters42069-del/PokiNative/pulls",
                "Linux launcher GitHub links target PokiNative");
            if (OperatingSystem.IsLinux() && requirePayload)
                Check(File.Exists(Path.Combine(AppContext.BaseDirectory, "libSDL3.so")) &&
                    File.Exists(Path.Combine(AppContext.BaseDirectory, "libSDL3.so.0")),
                    "SDL3 gamepad runtime is next to the Linux launcher");
            Check(LinuxGamepad.NewlyPressed(LinuxGamepad.ConfirmButton, LinuxGamepad.ConfirmButton) == 0 &&
                LinuxGamepad.NewlyPressed(LinuxGamepad.ConfirmButton, 0) == LinuxGamepad.ConfirmButton,
                "controller buttons activate once per press");
            Check(LinuxGamepad.Directions(0, -16001) == LinuxGamepad.UpButton &&
                LinuxGamepad.Directions(16000, 0) == 0,
                "left-stick navigation uses a dead zone");
            Check(Install.Game(root).EndsWith(Path.Combine("build", "game", "karpur")), "native executable path");
            Check(Install.StartInfo(root).WorkingDirectory == root && Install.StartInfo(root).FileName == Install.Game(root), "native launch path and working directory");
            var start = Install.StartInfo(root);
            Check(start.ArgumentList.SequenceEqual(new[] { "--exefs", Path.Combine(root, "game", "extracted", "update", "exefs"),
                "--romfs", Path.Combine(root, "game", "romfs"), "--profile", Install.Profile(root) }),
                "native launch uses transferable game paths");
            Check(Install.StartInfo(root, true).Environment["KARPUR_DECK_CONTROLS"] == "1",
                "Steam Deck launch enables the controller preset");
            Install.MarkPrepared(root);
            Check(File.Exists(Install.PreparedMarker(root)), "prepared marker written for RecompLauncher");
            Check(!start.Environment.ContainsKey("TOTK_MEMORY") && !start.Environment.ContainsKey("TOTK_XLATE_DEBUG") &&
                !start.Environment.ContainsKey("TOTK_SUBMIT_SYNC"),
                "Linux launch leaves the removed memory, shader and timing experiments unset");
            string? previousAppImage = Environment.GetEnvironmentVariable("APPIMAGE");
            string? previousConfig = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
            string? previousData = Environment.GetEnvironmentVariable("XDG_DATA_HOME");
            string? previousSteamDeck = Environment.GetEnvironmentVariable("SteamDeck");
            try
            {
                string source = Path.Combine(root, "KarPur-Deck.AppImage");
                Environment.SetEnvironmentVariable("APPIMAGE", source);
                Environment.SetEnvironmentVariable("XDG_CONFIG_HOME", Path.Combine(root, "settings"));
                Environment.SetEnvironmentVariable("XDG_DATA_HOME", Path.Combine(root, "data"));
                Environment.SetEnvironmentVariable("SteamDeck", "1");
                Directory.CreateDirectory(UserState.DirectoryPath);
                string stateFile = Path.Combine(UserState.DirectoryPath, "launcher.json");
                File.WriteAllText(stateFile, "{\"DeckGpuSync\":true,\"DeckMemoryImport\":true,\"DeckCullingSafe\":true}");
                var oldState = UserState.Load();
                Check(!Install.StartInfo(root, oldState.DeckControls).Environment.ContainsKey("TOTK_MEMORY") &&
                    !Install.StartInfo(root, oldState.DeckControls).Environment.ContainsKey("TOTK_XLATE_DEBUG"),
                    "old saved experiment values cannot affect a new game launch");
                oldState.Save();
                Check(!File.ReadAllText(stateFile).Contains("DeckMemoryImport") &&
                    !File.ReadAllText(stateFile).Contains("DeckCullingSafe") &&
                    !File.ReadAllText(stateFile).Contains("DeckGpuSync"),
                    "removed experiment values disappear from saved launcher settings");
                Directory.CreateDirectory(Path.GetDirectoryName(Install.Game(root))!);
                File.WriteAllText(Install.Game(root), "synthetic executable");
                Check(UserState.Load().Target == root, "launcher detects a ready game beside the AppImage");
                File.WriteAllText(source, "synthetic AppImage");
                string pinned = Path.Combine(root, "pinned launcher");
                string installed = Install.PinLauncher(pinned, [1, 2, 3])!;
                Check(File.ReadAllText(installed) == "synthetic AppImage" &&
                    File.Exists(Path.Combine(root, "data", "applications", "KarPur-Linux-Launcher.desktop")),
                    "installed launcher and desktop entry created");
                File.WriteAllText(installed, "obsolete 0.3.8.10 launcher");
                File.WriteAllText(Path.Combine(pinned, ".karpur-linux-launcher-version"), "0.3.8.10");
                Install.PinLauncher(pinned, [1, 2, 3]);
                Check(File.ReadAllText(installed) == "synthetic AppImage" &&
                    File.ReadAllText(Path.Combine(pinned, ".karpur-linux-launcher-version")) == Program.BuildRevision,
                    "current release replaces the obsolete 0.3.8.10 Deck launcher");
                File.WriteAllText(installed, "newer launcher");
                File.WriteAllText(Path.Combine(pinned, ".karpur-linux-launcher-version"), "99.0.0");
                Install.PinLauncher(pinned, [1, 2, 3]);
                Check(File.ReadAllText(installed) == "newer launcher", "older launcher does not replace an update");
            }
            finally
            {
                Environment.SetEnvironmentVariable("APPIMAGE", previousAppImage);
                Environment.SetEnvironmentVariable("XDG_CONFIG_HOME", previousConfig);
                Environment.SetEnvironmentVariable("XDG_DATA_HOME", previousData);
                Environment.SetEnvironmentVariable("SteamDeck", previousSteamDeck);
            }
            string zipPath = Path.Combine(root, "sources.zip");
            using (var zip = ZipFile.Open(zipPath, ZipArchiveMode.Create))
            {
                using (var writer = new StreamWriter(zip.CreateEntry("tools/hello.txt").Open())) writer.Write("synthetic source");
                using (var writer = new StreamWriter(zip.CreateEntry("runtime/current.c").Open())) writer.Write("current source");
            }
            string install = Path.Combine(root, "installation with spaces");
            string runtime = Path.Combine(install, "runtime");
            Directory.CreateDirectory(runtime);
            File.WriteAllText(Path.Combine(runtime, "compass.c"), "obsolete source");
            Install.Unpack(zipPath, install, CancellationToken.None);
            Check(File.ReadAllText(Path.Combine(install, "tools", "hello.txt")) == "synthetic source" &&
                File.Exists(Path.Combine(runtime, "current.c")) && !File.Exists(Path.Combine(runtime, "compass.c")),
                "source payload extraction removes obsolete runtime C files");
            DateTime modified = File.GetLastWriteTimeUtc(Path.Combine(install, "tools", "hello.txt"));
            Install.Unpack(zipPath, install, CancellationToken.None);
            Check(File.GetLastWriteTimeUtc(Path.Combine(install, "tools", "hello.txt")) == modified, "unchanged sources retain incremental build dates");
            string bad = Path.Combine(root, "bad.zip");
            using (var zip = ZipFile.Open(bad, ZipArchiveMode.Create)) zip.CreateEntry("../escape.txt");
            bool rejected = false;
            try { Install.Unpack(bad, install, CancellationToken.None); } catch (InvalidDataException) { rejected = true; }
            Check(rejected && !File.Exists(Path.Combine(root, "escape.txt")), "archive traversal rejected");
            Directory.CreateDirectory(Path.GetDirectoryName(Install.Game(install))!);
            File.WriteAllText(Install.Game(install), "synthetic");
            string iniPath = Path.Combine(install, "build", "game", "karpur.ini");
            File.WriteAllText(iniPath, "# keep this\n[display]\nvolume = 75\n");
            File.WriteAllText(Path.Combine(install, "build", "svhost_supported.txt"), "1\n");
            Install.SaveSettings(install, "60", "1280x800", true, "fsr", "fifo", "fps",
                gpu: "Synthetic GPU", safeSubmit: true, keepHotMemory: true, outputRes: "800x450");
            var ini = new Ini(iniPath);
            Check(ini.Get("operation_mode") == "handheld" && ini.Get("vk_present") == "fifo" &&
                ini.Get("perf_overlay") == "fps" && ini.Get("gpu_name") == "Synthetic GPU" &&
                ini.Get("submit_defer") == "0" && ini.Get("hot_cool") == "0" &&
                ini.Get("readback_guard") == "1" && ini.Get("output_res") == "800x450" &&
                ini.Get("svhost_test") == "1" && ini.Get("volume") == "75" &&
                File.ReadAllText(iniPath).Contains("# keep this"), "settings preserve existing values and comments");
            Install.SaveSettings(install, "60", "1280x800", true, "fsr", "fifo", "fps",
                gpu: "Synthetic GPU", safeSubmit: true, keepHotMemory: false);
            var optOut = new Ini(iniPath);
            optOut.Set("performance", "readback_guard", "0");
            optOut.Save();
            Install.SaveSettings(install, "60", "1280x800", true, "fsr", "fifo", "fps",
                gpu: "Synthetic GPU", safeSubmit: true, keepHotMemory: false);
            var saved = new Ini(iniPath);
            Check(saved.Get("hot_cool") == "300" && saved.Get("readback_guard") == "0",
                "hot memory reset and readback guard opt-out are preserved");
            string profile = Install.Profile(install);
            Directory.CreateDirectory(profile);
            File.WriteAllBytes(Path.Combine(profile, "account.uid"), Enumerable.Range(0, 16).Select(i => (byte)i).ToArray());
            string save = Install.SaveDirectory(install);
            Check(Path.GetFileName(save) == "0f0e0d0c0b0a09080706050403020100", "binary account identifier uses runtime byte order");
            Directory.CreateDirectory(save);
            File.WriteAllText(Path.Combine(save, "main"), "first synthetic save");
            string export = Path.Combine(root, "save.zip");
            Install.ExportSave(install, export);
            File.WriteAllText(Path.Combine(save, "main"), "second synthetic save");
            Install.ImportSave(install, export);
            Check(File.ReadAllText(Path.Combine(save, "main")) == "first synthetic save", "save import round trip");
            Check(Directory.GetFiles(Path.Combine(profile, "save-backup"), "main", SearchOption.AllDirectories).Any(f => File.ReadAllText(f) == "second synthetic save"), "prior save preserved in backup");
            Console.WriteLine($"Linux launcher checks passed: {checks}");
            return 0;
        }
        catch (Exception e) { Console.Error.WriteLine(e); return 1; }
        finally { Directory.Delete(root, true); }
    }
}
