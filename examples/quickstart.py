"""Generate a synthetic video and decode unsorted, repeated target frames."""
import numpy as np
import video_loader

height, width, count = 48, 64, 33
y, x = np.mgrid[:height, :width]
frames = np.empty((count, height, width, 3), dtype=np.uint8)
for i in range(count):
    frames[i] = np.stack(((x * 3 + i * 7) % 256,
                          (y * 5 + i * 11) % 256,
                          ((x + y) * 2 + i * 13) % 256), axis=-1)
payload = video_loader.transcode(frames, fps=30, mode="fast264")
selected, metrics = video_loader.decode(payload, [17, 2, 17], return_info=True)
assert selected.shape == (3, height, width, 3)
np.testing.assert_array_equal(selected[0], selected[2])
print("version:", video_loader.__version__)
print("decoded shape:", selected.shape)
print("closure:", metrics["closure_mode"])
print("reconstructed:", metrics["frames_reconstructed"])
