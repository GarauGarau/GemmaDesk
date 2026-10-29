#include "../native/gpu_device.h"
// Regression: Radeon 890M is reported as IGPU, even though its name is Vulkan0.
static_assert(gemmadesk_gpu_type(GGML_BACKEND_DEVICE_TYPE_IGPU), "Radeon 890M must be accepted");
static_assert(gemmadesk_gpu_type(GGML_BACKEND_DEVICE_TYPE_GPU), "Dedicated GPU must be accepted");
static_assert(!gemmadesk_gpu_type(GGML_BACKEND_DEVICE_TYPE_CPU), "CPU is not a Vulkan GPU");
static_assert(!gemmadesk_gpu_type(GGML_BACKEND_DEVICE_TYPE_ACCEL), "CPU accelerator is not a Vulkan GPU");
int main() { return 0; }
