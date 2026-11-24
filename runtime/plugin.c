// KarPur: what an exefs plugin's start-up writes into code.
//
// An exlaunch-style plugin (a mod's own subsdkN NSO; recomp/plugins.py, loader.c) hooks the game by writing branches
// into main's .text through a read-write alias it maps with MapProcessMemory, and assembles its hook trampolines the
// same way into pools inside its own .text. Here the lifted C runs, not guest memory, so those writes do nothing by
// themselves: they are captured once (TOTK_PLUGIN_CAPTURE=<file>: the run stops after the module inits and writes
// the file), tools/plugin_hooks.py turns the capture into a patch table, and the generator lifts main and the plugin
// again with it (tools/mod_plugin.py runs all of it).
//
// MapProcessMemory here copies: Windows gives no second view of memory this process already committed as one block,
// so the alias is fresh read-write memory with a copy of the source, and the words the guest changed in it go back to
// the source when it is unmapped and when a plugin's init returns (plugin_init_done; .text and .rodata are briefly
// unsealed for it). A plugin that keeps writing through an alias after its init is not captured.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "host.h"
#include "kernel.h"
#include "platform/platform.h"

#define PAGE 0x1000ull
#define MAX_MAPS 16
#define ST_SHARED_CODE 0xF   // Horizon MemoryState of a MapProcessMemory alias
#define PERM_RW 3

typedef struct { uint64_t dst, src, size; } Map;
typedef struct { uint64_t addr; uint32_t orig, now; } Write;

static PlatMutex lock = PLAT_MUTEX_INIT;
static Map maps[MAX_MAPS];
static int nmaps;
static Write *writes;
static size_t nwrites, capw;
static int plugins_loaded;

static const RtLoaded *module_at(uint64_t a) {
    for (int i = 0; i < ldr_count(); i++) {
        const RtLoaded *m = ldr_module(i);
        if (a >= m->base && a < m->base + m->size) return m;
    }
    return NULL;
}

static int is_game(const RtLoaded *m) {
    for (int i = 0; i < rt_plugin_count; i++)
        if (rt_plugins[i].module && !strcmp(rt_plugins[i].module, m->name)) return 0;
    return 1;
}

static Write *write_of(uint64_t a) {
    for (size_t i = 0; i < nwrites; i++)
        if (writes[i].addr == a) return &writes[i];
    return NULL;
}

// One word of the source a guest changed in an alias: recorded (first original kept) and stored (holding lock).
static void store(uint64_t a, uint32_t now) {
    uint32_t *p = (uint32_t *)(uintptr_t)a;
    Write *w = write_of(a);
    if (!w) {
        if (nwrites == capw) {
            capw = capw ? capw * 2 : 1024;
            writes = realloc(writes, capw * sizeof *writes);
            if (!writes) rt_fatal("plugin: write log allocation");
        }
        w = &writes[nwrites++];
        *w = (Write){a, *p, now};
    }
    w->now = now;
    const RtLoaded *m = module_at(a);
    int sealed = m && a - m->base < m->data_off;
    uint64_t page = a & ~(PAGE - 1);
    if (sealed && !plat_mem_protect(page, PAGE, PLAT_PROT_RW)) rt_fatal("plugin: cannot unseal %#llx", (unsigned long long)page);
    *p = now;
    // every alias of the word shows it too (on hardware they are views of the same memory): a stale alias synced
    // later must not write the old word back
    for (int i = 0; i < nmaps; i++)
        if (a - maps[i].src < maps[i].size) *(uint32_t *)(uintptr_t)(maps[i].dst + (a - maps[i].src)) = now;
    if (sealed && !plat_mem_protect(page, PAGE, PLAT_PROT_READ)) rt_fatal("plugin: cannot reseal %#llx", (unsigned long long)page);
}

// The words of an alias that differ from its source go to the source (holding lock). Returns how many.
static size_t sync(const Map *mp) {
    const uint32_t *d = (const uint32_t *)(uintptr_t)mp->dst, *s = (const uint32_t *)(uintptr_t)mp->src;
    size_t n = 0;
    for (uint64_t i = 0; i < mp->size / 4; i++)
        if (d[i] != s[i]) {
            store(mp->src + 4 * i, d[i]);
            n++;
        }
    return n;
}

// The code blocks QueryMemory shows (what an exlaunch plugin walks to find the modules), logged at the first map.
static void log_code_blocks(void) {
    uint64_t a = 0;
    int n = 0;
    while (a < RT_ADDRESS_LIMIT && n < 64) {
        RtMemoryInfo mi;
        if (mem_query(a, &mi) || !mi.size) break;
        if (mi.state == 3 || mi.state == 4) {
            char at[160];
            rt_log("plugin: QueryMemory block %#llx size %#llx state %u perm %u (%s)", (unsigned long long)mi.base,
                   (unsigned long long)mi.size, mi.state, mi.perm, ldr_describe(mi.base, at, sizeof at));
            n++;
        }
        if (mi.base + mi.size <= a) break;
        a = mi.base + mi.size;
    }
}

int plugin_svc(Cpu *c, uint32_t imm) {
    uint64_t *x = c->x;
    uint64_t dst = x[0], src = x[2], size = x[3];
    uint32_t proc = (uint32_t)x[1];
    if (imm != 0x74 && imm != 0x75) return 0;
    if (!kern_is_process_handle(proc)) {
        x[0] = KERN_R_BAD_HANDLE;
        return 1;
    }
    if ((dst | src | size) & (PAGE - 1) || !size || src + size < src) {
        x[0] = KERN_R_INVALID_STATE_MEM;
        return 1;
    }
    const RtLoaded *m = module_at(src);
    plat_mutex_lock(&lock);
    static int walked;
    if (!walked++) log_code_blocks();
    if (imm == 0x74) {   // MapProcessMemory(x0 dst, w1 process, x2 src, x3 size): only module images are mapped
        const char *why = !m ? "source outside the modules" : src + size > m->base + m->size ? "source past its module"
                        : nmaps == MAX_MAPS ? "too many aliases" : !mem_commit_free(dst, size) ? "destination not free"
                        : NULL;
        if (why) {
            plat_mutex_unlock(&lock);
            RtMemoryInfo mi;
            mem_query(dst, &mi);
            rt_log("plugin: MapProcessMemory %#llx <- %#llx size %#llx refused: %s (destination block %#llx size %#llx "
                   "state %u)", (unsigned long long)dst, (unsigned long long)src, (unsigned long long)size, why,
                   (unsigned long long)mi.base, (unsigned long long)mi.size, (unsigned)mi.state);
            x[0] = KERN_R_INVALID_STATE_MEM;
            return 1;
        }
        memcpy((void *)(uintptr_t)dst, (const void *)(uintptr_t)src, size);
        mem_extra_add(dst, size, ST_SHARED_CODE, PERM_RW);
        maps[nmaps++] = (Map){dst, src, size};
        rt_log("plugin: MapProcessMemory %#llx <- %s+%#llx size %#llx", (unsigned long long)dst, m->name,
               (unsigned long long)(src - m->base), (unsigned long long)size);
        x[0] = 0;
    } else {             // UnmapProcessMemory(x0 dst, w1 process, x2 src, x3 size)
        int i = 0;
        while (i < nmaps && !(maps[i].dst == dst && maps[i].src == src && maps[i].size == size)) i++;
        if (i == nmaps) {
            plat_mutex_unlock(&lock);
            x[0] = KERN_R_INVALID_STATE_MEM;
            return 1;
        }
        size_t n = sync(&maps[i]);
        mem_extra_remove(dst);
        mem_decommit_free(dst, size);
        maps[i] = maps[--nmaps];
        rt_log("plugin: UnmapProcessMemory %#llx: %zu words written back", (unsigned long long)dst, n);
        x[0] = 0;
    }
    plat_mutex_unlock(&lock);
    return 1;
}

// Words written into a module's .text, counted as runs of adjacent words.
static void count_text(const RtLoaded *m, size_t *words, size_t *runs) {
    *words = *runs = 0;
    uint64_t last = 0;
    for (size_t i = 0; i < nwrites; i++) {
        uint64_t a = writes[i].addr;
        if (a < m->base || a >= m->base + m->text_end || writes[i].orig == writes[i].now) continue;
        (*words)++;
        if (a != last + 4) (*runs)++;
        last = a;
    }
}

static int cmp_write(const void *a, const void *b) {
    uint64_t x = ((const Write *)a)->addr, y = ((const Write *)b)->addr;
    return x < y ? -1 : x > y;
}

void plugin_init_done(const RtLoaded *m) {
    plat_mutex_lock(&lock);
    size_t n = 0;
    for (int i = 0; i < nmaps; i++) n += sync(&maps[i]);
    qsort(writes, nwrites, sizeof *writes, cmp_write);
    rt_log("plugin %s: init ran (%d aliases still mapped, %zu words synced now)", m->name, nmaps, n);
    for (int i = 0; i < ldr_count(); i++) {
        const RtLoaded *t = ldr_module(i);
        size_t words, runs;
        count_text(t, &words, &runs);
        if (words) rt_log("plugin %s: %zu words written into %s .text in %zu runs (hook sites)", m->name, words, t->name, runs);
    }
    plat_mutex_unlock(&lock);
}

// Capture file (tools/plugin_hooks.py reads it):
//   module <name> <base> <size> <text end> <data off> game|plugin
//   write <module> <offset> <original word> <word>      a .text word an init changed
//   ptr <module> <offset> <target module> <target offset>   a qword of a plugin's data or bss pointing at written code
static void capture(const char *path) {
    FILE *f = fopen(path, "w");
    if (!f) rt_fatal("plugin: cannot write the capture %s", path);
    fprintf(f, "# KARPUR_" "GENERATED_FROM_GAME_CODE: exefs plugin capture of runtime/plugin.c. Do not edit or commit.\n");
    for (int i = 0; i < ldr_count(); i++) {
        const RtLoaded *m = ldr_module(i);
        fprintf(f, "module %s %llx %llx %llx %llx %s\n", m->name, (unsigned long long)m->base,
                (unsigned long long)m->size, (unsigned long long)m->text_end, (unsigned long long)m->data_off,
                is_game(m) ? "game" : "plugin");
    }
    size_t nw = 0, np = 0;
    for (size_t i = 0; i < nwrites; i++) {
        const Write *w = &writes[i];
        const RtLoaded *m = module_at(w->addr);
        if (!m || w->addr - m->base >= m->text_end || w->orig == w->now) continue;
        fprintf(f, "write %s %llx %08x %08x\n", m->name, (unsigned long long)(w->addr - m->base), w->orig, w->now);
        nw++;
    }
    // Pointers to written code: trampolines a hook calls to run the original (a plugin keeps them in its data).
    for (int i = 0; i < ldr_count(); i++) {
        const RtLoaded *m = ldr_module(i);
        if (is_game(m)) continue;
        for (uint64_t a = m->base + m->data_off; a + 8 <= m->base + m->size; a += 8) {
            uint64_t v;
            memcpy(&v, (const void *)(uintptr_t)a, 8);
            const RtLoaded *t = module_at(v);
            if (!t || v & 3 || v - t->base >= t->text_end) continue;
            if (!write_of(v)) continue;
            fprintf(f, "ptr %s %llx %s %llx\n", m->name, (unsigned long long)(a - m->base), t->name,
                    (unsigned long long)(v - t->base));
            np++;
        }
    }
    fclose(f);
    rt_log("plugin capture: %zu code words, %zu pointers to written code -> %s", nw, np, path);
}

void plugin_inits_done(void) {
    for (int i = 0; i < ldr_count(); i++)
        if (!is_game(ldr_module(i))) plugins_loaded++;
    if (!plugins_loaded) return;
    const char *path = getenv("TOTK_PLUGIN_CAPTURE");
    if (path && *path) {
        plat_mutex_lock(&lock);
        capture(path);
        plat_mutex_unlock(&lock);
        fflush(NULL);
        plat_exit(0);
    }
}

void rt_plugin_hook_hit(uint64_t site) {
    char at[160];
    rt_log("plugin hook: site %s ran its hook", ldr_describe(site, at, sizeof at));
}
