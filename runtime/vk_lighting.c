// Optional screen-space contact occlusion and indirect-light approximation for
// Violet's tone-mapped scene. Runs after the tone-map draw, before the HUD and
// before DLAA/FSR 2. Images are allocated by VMA outside the game's pools.
#include "vk_lighting.h"

#include <math.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "lighting_spv.h"
#include "lighting_half_spv.h"
#include "lighting_apply_spv.h"
#include "lighting_depth_spv.h"
#include "nvn_api.h"
#include "vk.h"

static struct {
    int checked, ao, gi, init, ready, have_depth, applied;
    const VkTexImage *depth;
    VkFormat depth_format;
    VkImage depth_image;
    uint32_t dw, dh, w, h;
    float proj[4];
    float cam[2];                        // 1 / p00, 1 / p11: view x and y per NDC unit at distance 1
    VkImage image[2];
    VmaAllocation alloc[2];
    VkImageView view[2];
    VkSampler sampler;
    VkDescriptorSetLayout dsl;
    VkPipelineLayout layout;
    VkPipeline pipeline;
    int half;                            // TOTK_LIGHTING_HALF: measure at half size, then apply along depth edges
    VkPipeline half_pipe, apply_pipe, depth_pipe;
    VkImage himg, dimg;                  // half-size light (rgba16f) and view distance (r32f)
    VmaAllocation halloc, dalloc;
    VkImageView hview, dview;
    uint32_t hw, hh;
    PFN_vkCmdPushDescriptorSetKHR push;
    uint64_t frames;
    int timing;                          // TOTK_LIGHTING_TIME: GPU timestamps around the pass, average logged
    VkQueryPool qp;
    double t_sum, t_half;
    uint32_t t_n;
} L;

int vk_lighting_on(void) {
    if (!L.checked) {
        L.checked = 1;
        L.ao = cfg_int("TOTK_AO");
        L.gi = cfg_int("TOTK_INDIRECT_LIGHT");
        L.half = cfg_int("TOTK_LIGHTING_HALF") > 0;
        L.timing = rt_knob("LIGHTING_TIME", "0") > 0;
        if (L.ao < 0 || L.ao > 3) L.ao = 0;
        if (L.gi < 0 || L.gi > 3) L.gi = 0;
        if (L.ao || L.gi)
            rt_log("lighting: experimental screen-space AO %d, indirect light %d; host images only, game pools unchanged",
                   L.ao, L.gi);
    }
    return (L.ao || L.gi) && (!L.init || L.ready);
}


int vk_lighting_need_scene(void) { return vk_lighting_on() && !L.have_depth; }

void vk_lighting_note_scene(const VkExec *ex, const float proj[16]) {
    if (!vk_lighting_need_scene() || !ex->depth.image || !ex->depth.img) return;
    if (!(ex->depth.img->usage & VK_IMAGE_USAGE_SAMPLED_BIT)) return;
    for (int i = 0; i < 16; ++i) if (!isfinite(proj[i])) return;
    if (fabsf(proj[11]) < 1e-6f || fabsf(proj[14]) < 1e-6f) return;
    if (fabsf(proj[0]) < 1e-6f || fabsf(proj[5]) < 1e-6f) return;
    L.depth = ex->depth.img;
    L.depth_image = ex->depth.image;
    L.depth_format = ex->depth.format;
    L.dw = ex->depth.width, L.dh = ex->depth.height;
    L.proj[0] = proj[10], L.proj[1] = proj[14], L.proj[2] = proj[11];
    L.proj[3] = nvn_depth_minus_w() ? 1.0f : 0.0f;
    L.cam[0] = 1.0f / fabsf(proj[0]), L.cam[1] = 1.0f / fabsf(proj[5]);
    L.have_depth = 1;
}

static void free_images(VkCtx *c) {
    for (int i = 0; i < 2; ++i) {
        if (L.view[i]) vkDestroyImageView(c->device, L.view[i], NULL);
        if (L.image[i]) vmaDestroyImage(c->vma, L.image[i], L.alloc[i]);
        L.view[i] = VK_NULL_HANDLE, L.image[i] = VK_NULL_HANDLE;
    }
    if (L.hview) vkDestroyImageView(c->device, L.hview, NULL);
    if (L.himg) vmaDestroyImage(c->vma, L.himg, L.halloc);
    L.hview = VK_NULL_HANDLE, L.himg = VK_NULL_HANDLE, L.hw = L.hh = 0;
    if (L.dview) vkDestroyImageView(c->device, L.dview, NULL);
    if (L.dimg) vmaDestroyImage(c->vma, L.dimg, L.dalloc);
    L.dview = VK_NULL_HANDLE, L.dimg = VK_NULL_HANDLE;
    L.w = L.h = 0;
}

static int make_image(VkCtx *c, int i, uint32_t w, uint32_t h) {
    VkImageCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType = VK_IMAGE_TYPE_2D, .format = VK_FORMAT_R8G8B8A8_UNORM,
        .extent = {w, h, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT |
                 VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE};
    if (vmaCreateImage(c->vma, &ci, &ai, &L.image[i], &L.alloc[i], NULL) != VK_SUCCESS) return 0;
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
        .image = L.image[i], .viewType = VK_IMAGE_VIEW_TYPE_2D,
        .format = VK_FORMAT_R8G8B8A8_UNORM,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    return vkCreateImageView(c->device, &vi, NULL, &L.view[i]) == VK_SUCCESS;
}

static int make_one(VkCtx *c, VkFormat f, uint32_t w, uint32_t h, VkImage *img, VmaAllocation *al, VkImageView *view) {
    VkImageCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType = VK_IMAGE_TYPE_2D, .format = f,
        .extent = {w, h, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE};
    if (vmaCreateImage(c->vma, &ci, &ai, img, al, NULL) != VK_SUCCESS) return 0;
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
        .image = *img, .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = f,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    return vkCreateImageView(c->device, &vi, NULL, view) == VK_SUCCESS;
}

static int make_half(VkCtx *c, uint32_t w, uint32_t h) {
    if (!make_one(c, VK_FORMAT_R16G16B16A16_SFLOAT, w, h, &L.himg, &L.halloc, &L.hview) ||
        !make_one(c, VK_FORMAT_R32_SFLOAT, w, h, &L.dimg, &L.dalloc, &L.dview)) return 0;
    L.hw = w, L.hh = h;
    return 1;
}

static VkPipeline make_pipe(VkCtx *c, const uint32_t *code, size_t size) {
    VkShaderModuleCreateInfo mi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = size, .pCode = code};
    VkShaderModule shader;
    if (vkCreateShaderModule(c->device, &mi, NULL, &shader) != VK_SUCCESS) return VK_NULL_HANDLE;
    VkComputePipelineCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                  .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = shader, .pName = "main"}, .layout = L.layout};
    VkPipeline pipe = VK_NULL_HANDLE;
    if (vkCreateComputePipelines(c->device, VK_NULL_HANDLE, 1, &pi, NULL, &pipe) != VK_SUCCESS) pipe = VK_NULL_HANDLE;
    vkDestroyShaderModule(c->device, shader, NULL);
    return pipe;
}

static int ensure_images(VkCtx *c, uint32_t w, uint32_t h, int *fresh) {
    *fresh = 0;
    if (L.image[0] && L.w == w && L.h == h) return 1;
    if (L.image[0]) {
        vk_queue_lock();
        vkDeviceWaitIdle(c->device);
        vk_queue_unlock();
        free_images(c);
    }
    if (!make_image(c, 0, w, h) || !make_image(c, 1, w, h) || (L.half && !make_half(c, (w + 1) / 2, (h + 1) / 2))) {
        free_images(c);
        rt_log("lighting: host image allocation failed at %ux%u; effects off", w, h);
        L.ready = 0;
        return 0;
    }
    L.w = w, L.h = h, *fresh = 1;
    return 1;
}

static int init(VkCtx *c) {
    L.init = 1;
    L.push = (PFN_vkCmdPushDescriptorSetKHR)vkGetDeviceProcAddr(c->device, "vkCmdPushDescriptorSetKHR");
    if (!L.push) return 0;
    VkDescriptorSetLayoutBinding b[5];
    for (int i = 0; i < 5; ++i)
        b[i] = (VkDescriptorSetLayoutBinding){.binding = (uint32_t)i,
            .descriptorType = i == 2 ? VK_DESCRIPTOR_TYPE_STORAGE_IMAGE : VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
            .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT};
    VkDescriptorSetLayoutCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .flags = VK_DESCRIPTOR_SET_LAYOUT_CREATE_PUSH_DESCRIPTOR_BIT_KHR, .bindingCount = 5, .pBindings = b};
    if (vkCreateDescriptorSetLayout(c->device, &di, NULL, &L.dsl) != VK_SUCCESS) return 0;
    VkPushConstantRange pr = {VK_SHADER_STAGE_COMPUTE_BIT, 0, 48};
    VkPipelineLayoutCreateInfo li = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1, .pSetLayouts = &L.dsl, .pushConstantRangeCount = 1, .pPushConstantRanges = &pr};
    if (vkCreatePipelineLayout(c->device, &li, NULL, &L.layout) != VK_SUCCESS) return 0;
    VkSamplerCreateInfo si = {.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
        .magFilter = VK_FILTER_NEAREST, .minFilter = VK_FILTER_NEAREST,
        .mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST,
        .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, .maxLod = 0};
    if (vkCreateSampler(c->device, &si, NULL, &L.sampler) != VK_SUCCESS) return 0;
    VkShaderModuleCreateInfo mi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = sizeof lighting_spv, .pCode = lighting_spv};
    VkShaderModule shader;
    if (vkCreateShaderModule(c->device, &mi, NULL, &shader) != VK_SUCCESS) return 0;
    VkComputePipelineCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                  .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = shader, .pName = "main"}, .layout = L.layout};
    VkResult r = vkCreateComputePipelines(c->device, VK_NULL_HANDLE, 1, &pi, NULL, &L.pipeline);
    vkDestroyShaderModule(c->device, shader, NULL);
    if (r != VK_SUCCESS) return 0;
    if (L.half) {
        L.half_pipe = make_pipe(c, lighting_half_spv, sizeof lighting_half_spv);
        L.apply_pipe = make_pipe(c, lighting_apply_spv, sizeof lighting_apply_spv);
        L.depth_pipe = make_pipe(c, lighting_depth_spv, sizeof lighting_depth_spv);
        if (!L.half_pipe || !L.apply_pipe || !L.depth_pipe) L.half = 0, rt_log("lighting: half-size pipelines failed; full size used");
        else rt_log("lighting: measured at half size (TOTK_LIGHTING_HALF), applied along depth edges");
    }
    return L.ready = 1;
}

static void barrier(VkCommandBuffer cb) {
    VkMemoryBarrier2 mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,
        .srcStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
        .srcAccessMask = VK_ACCESS_2_MEMORY_WRITE_BIT | VK_ACCESS_2_MEMORY_READ_BIT,
        .dstStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
        .dstAccessMask = VK_ACCESS_2_MEMORY_WRITE_BIT | VK_ACCESS_2_MEMORY_READ_BIT};
    VkDependencyInfo d = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO,
        .memoryBarrierCount = 1, .pMemoryBarriers = &mb};
    vkCmdPipelineBarrier2(cb, &d);
}

void vk_lighting_after_draw(VkExec *ex, uint32_t fs) {
    if (!vk_lighting_on() || !L.have_depth || L.applied || fs != 0x333e928au) return;
    const VkExecTarget *ct = &ex->color[0];
    uint32_t w = ct->width, h = ct->height;
    if (!ct->image || !ct->img || ex->depth.image || w != L.dw || h != L.dh ||
        !(ct->img->usage & VK_IMAGE_USAGE_TRANSFER_SRC_BIT) ||
        !(ct->img->usage & VK_IMAGE_USAGE_TRANSFER_DST_BIT)) return;
    VkCtx *c = vk_ctx();
    if (!L.init && !init(c)) { rt_log("lighting: compute setup failed; effects off"); return; }
    if (!L.ready) return;
    int fresh;
    if (!ensure_images(c, w, h, &fresh)) return;
    VkImageView depth = vk_exec_target_view(L.depth, L.depth_format, VK_IMAGE_VIEW_TYPE_2D,
                                            VK_IMAGE_ASPECT_DEPTH_BIT, 0, 0, 1, 0);
    if (!depth) return;
    if (!L.frames)
        rt_log("lighting: input colour format %d, depth format %d; sampled depth %ux%u, output %ux%u",
               (int)ct->format, (int)L.depth_format, L.dw, L.dh, w, h);
    int reopen;
    VkCommandBuffer cb = vk_exec_inline_begin(&reopen);
    if (!cb) return;
    if (fresh) {
        VkImageMemoryBarrier2 b[4];
        int nb = L.himg ? 4 : 2;
        for (int i = 0; i < nb; ++i)
            b[i] = (VkImageMemoryBarrier2){.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2,
                .dstStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
                .dstAccessMask = VK_ACCESS_2_MEMORY_READ_BIT | VK_ACCESS_2_MEMORY_WRITE_BIT,
                .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED, .newLayout = VK_IMAGE_LAYOUT_GENERAL,
                .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
                .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = i < 2 ? L.image[i] : i == 2 ? L.himg : L.dimg,
                .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
        VkDependencyInfo d = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO,
            .imageMemoryBarrierCount = (uint32_t)nb, .pImageMemoryBarriers = b};
        vkCmdPipelineBarrier2(cb, &d);
    }
    uint32_t slot = (uint32_t)(L.frames % 4);
    if (L.timing && !L.qp) {
        VkQueryPoolCreateInfo qi = {.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO,
            .queryType = VK_QUERY_TYPE_TIMESTAMP, .queryCount = 12};
        if (vkCreateQueryPool(c->device, &qi, NULL, &L.qp) != VK_SUCCESS) L.timing = 0;
    }
    if (L.timing && L.qp) {
        uint64_t ts[3];
        double ms = c->props.limits.timestampPeriod * 1e-6;
        if (L.frames >= 4 && vkGetQueryPoolResults(c->device, L.qp, slot * 3, 3, sizeof ts, ts, sizeof ts[0],
                                                   VK_QUERY_RESULT_64_BIT) == VK_SUCCESS && ts[2] > ts[0]) {
            L.t_sum += (double)(ts[2] - ts[0]) * ms, L.t_n++;
            if (ts[1] > ts[0]) L.t_half += (double)(ts[1] - ts[0]) * ms;
            if (L.t_n == 600) {
                rt_log("lighting: GPU time %.3f ms per frame, %.3f of it up to the measuring pass (%s, AO %d, indirect %d, %ux%u)",
                       L.t_sum / L.t_n, L.t_half / L.t_n, L.half ? "half size" : "full size", L.ao, L.gi, w, h);
                L.t_sum = L.t_half = 0, L.t_n = 0;
            }
        }
        vkCmdResetQueryPool(cb, L.qp, slot * 3, 3);
        vkCmdWriteTimestamp2(cb, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, L.qp, slot * 3);
    }
    barrier(cb);
    VkImageCopy copy = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
        .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .extent = {w, h, 1}};
    vkCmdCopyImage(cb, ct->image, VK_IMAGE_LAYOUT_GENERAL, L.image[0], VK_IMAGE_LAYOUT_GENERAL, 1, &copy);
    barrier(cb);
    VkDescriptorImageInfo info[3] = {
        {L.sampler, depth, VK_IMAGE_LAYOUT_GENERAL},
        {L.sampler, L.view[0], VK_IMAGE_LAYOUT_GENERAL},
        {VK_NULL_HANDLE, L.view[1], VK_IMAGE_LAYOUT_GENERAL}};
    VkWriteDescriptorSet wr[3];
    for (int i = 0; i < 3; ++i)
        wr[i] = (VkWriteDescriptorSet){.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstBinding = (uint32_t)i, .descriptorCount = 1,
            .descriptorType = i == 2 ? VK_DESCRIPTOR_TYPE_STORAGE_IMAGE : VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
            .pImageInfo = &info[i]};
    float pc[12] = {L.proj[0], L.proj[1], L.proj[2], L.proj[3], (float)w, (float)h, (float)L.ao, (float)L.gi,
                    L.cam[0], L.cam[1], (float)(L.frames % 64), 0.0f};
    if (L.half && L.himg) {
        VkDescriptorImageInfo hout = {VK_NULL_HANDLE, L.hview, VK_IMAGE_LAYOUT_GENERAL};
        VkDescriptorImageInfo hin = {L.sampler, L.hview, VK_IMAGE_LAYOUT_GENERAL};
        VkDescriptorImageInfo dout = {VK_NULL_HANDLE, L.dview, VK_IMAGE_LAYOUT_GENERAL};
        VkDescriptorImageInfo din = {L.sampler, L.dview, VK_IMAGE_LAYOUT_GENERAL};
        VkWriteDescriptorSet wd[2] = {wr[0], wr[2]};
        wd[1].pImageInfo = &dout;
        vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.depth_pipe);
        rec_push(L.push, cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.layout, 0, 2, wd);
        vkCmdPushConstants(cb, L.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof pc, pc);
        vkCmdDispatch(cb, (L.hw + 7) / 8, (L.hh + 7) / 8, 1);
        barrier(cb);
        VkWriteDescriptorSet wh[3] = {wr[0], wr[1], wr[2]};
        wh[0].pImageInfo = &din;
        wh[2].pImageInfo = &hout;
        vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.half_pipe);
        rec_push(L.push, cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.layout, 0, 3, wh);
        vkCmdPushConstants(cb, L.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof pc, pc);
        vkCmdDispatch(cb, (L.hw + 7) / 8, (L.hh + 7) / 8, 1);
        barrier(cb);
        if (L.timing && L.qp) vkCmdWriteTimestamp2(cb, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, L.qp, slot * 3 + 1);
        VkWriteDescriptorSet wa[5] = {wr[0], wr[1], wr[2], wr[1], wr[1]};
        wa[3].dstBinding = 3, wa[3].pImageInfo = &hin;
        wa[4].dstBinding = 4, wa[4].pImageInfo = &din;
        vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.apply_pipe);
        rec_push(L.push, cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.layout, 0, 5, wa);
        vkCmdPushConstants(cb, L.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof pc, pc);
        vkCmdDispatch(cb, (w + 7) / 8, (h + 7) / 8, 1);
    } else {
        vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.pipeline);
        rec_push(L.push, cb, VK_PIPELINE_BIND_POINT_COMPUTE, L.layout, 0, 3, wr);
        vkCmdPushConstants(cb, L.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof pc, pc);
        vkCmdDispatch(cb, (w + 7) / 8, (h + 7) / 8, 1);
        barrier(cb);
        if (L.timing && L.qp) vkCmdWriteTimestamp2(cb, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, L.qp, slot * 3 + 1);
    }
    barrier(cb);
    vkCmdCopyImage(cb, L.image[1], VK_IMAGE_LAYOUT_GENERAL, ct->image, VK_IMAGE_LAYOUT_GENERAL, 1, &copy);
    barrier(cb);
    if (L.timing && L.qp) vkCmdWriteTimestamp2(cb, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, L.qp, slot * 3 + 2);
    vk_exec_draw_new_cb();
    vk_exec_inline_end(reopen);
    L.applied = 1, L.frames++;
    if (L.frames == 1 || L.frames % 3600 == 0)
        rt_log("lighting: %llu frames processed at %ux%u (AO %d, indirect %d)",
               (unsigned long long)L.frames, w, h, L.ao, L.gi);
}

void vk_lighting_frame(void) { L.have_depth = L.applied = 0; }
