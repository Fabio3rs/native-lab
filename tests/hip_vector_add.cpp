#include <hip/hip_runtime.h>
#include <cstdio>
#include <cstdlib>

#define CHECK(call) do { hipError_t e = (call); if (e != hipSuccess) { \
    std::fprintf(stderr, "%s: %s\n", #call, hipGetErrorString(e)); return 1; } } while (0)

__global__ void fill(int *values) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < 1024) values[i] = i * 3 + 7;
}

int main() {
    int count = 0;
    CHECK(hipGetDeviceCount(&count));
    if (count < 1) return 1;
    for (int device = 0; device < count; ++device) {
        CHECK(hipSetDevice(device));
        hipDeviceProp_t properties{};
        CHECK(hipGetDeviceProperties(&properties, device));
        int *buffer = nullptr;
        CHECK(hipMalloc(&buffer, 1024 * sizeof(int)));
        fill<<<4, 256>>>(buffer);
        CHECK(hipGetLastError());
        CHECK(hipDeviceSynchronize());
        int result[1024];
        CHECK(hipMemcpy(result, buffer, sizeof(result), hipMemcpyDeviceToHost));
        CHECK(hipFree(buffer));
        for (int i = 0; i < 1024; ++i) if (result[i] != i * 3 + 7) return 1;
        std::printf("HIP kernel PASS: %s (%s), 1024 validated values\n", properties.name, properties.gcnArchName);
    }
    return 0;
}
