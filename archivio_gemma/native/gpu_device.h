#pragma once
#include "ggml-backend.h"

// llama.cpp distinguishes dedicated GPUs from integrated GPUs using host memory.
// Both execute Vulkan workloads; an integrated Radeon must not be rejected.
constexpr bool gemmadesk_gpu_type(enum ggml_backend_dev_type type) {
    return type == GGML_BACKEND_DEVICE_TYPE_GPU ||
           type == GGML_BACKEND_DEVICE_TYPE_IGPU;
}
