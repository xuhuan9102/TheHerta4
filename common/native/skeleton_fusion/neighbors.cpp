#include <cstddef>

extern "C" __declspec(dllexport) int herta_skeleton_distances_v1(
    const float* points, int count, float* distances
) noexcept {
    if (!points || !distances || count < 4 || count > 256) {
        return 0;
    }
    for (int row = 0; row < count; ++row) {
        const float* a = points + 3 * row;
        float* row_distances = distances + row * count;
        row_distances[row] = 0.0f;
        for (int col = row + 1; col < count; ++col) {
            const float* b = points + 3 * col;
            const float x = a[0] - b[0];
            const float y = a[1] - b[1];
            const float z = a[2] - b[2];
            // NumPy adds the three squared float32 coordinates in this order.
            const float squared = (x * x + y * y) + z * z;
            row_distances[col] = squared;
            distances[col * count + row] = squared;
        }
    }
    return 1;
}
