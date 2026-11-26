// The software keyboard's front end for SDL windows (swkbd.h): an in-game ImGui modal with SDL text input.
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "swkbd.h"
#include "ui/menu.h"

int text_entry_run(const SwkbdRequest *req, uint16_t *out, size_t cap, size_t *n) {
    const char *test = cfg_get("TOTK_SWKBD_TEST");
    if (test && !strcmp(test, "cancel")) { *n = 0; return 0; }
    if (test && !strcmp(test, "ok")) {
        size_t k = 0;
        while (k < cap && req->initial[k]) k++;
        memcpy(out, req->initial, k * 2);
        *n = k;
        return k > 0;
    }
    return menu_text_entry_run(req, out, cap, n);
}
