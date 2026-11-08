"""Export the real mask pixel/particle renderer; no desktop capture or microphone.

Optional developer preview, using PyAV already installed with faster-whisper.
The MP4 backdrop is for video only. The desktop overlay remains transparent.
"""
from pathlib import Path
import math

import av
import numpy as np
from PIL import Image, ImageDraw

from jarvis_mask_overlay import (MASK_ASSET, NANO_LOCK_SECONDS, _remove_baked_shadow,
    _reveal_thresholds, _eye_pixels, _render_mask_pixels, _nanobot_color,
    _surface_particle_position, _display_eye_intensity)


def main():
    output = Path(__file__).resolve().parent / "previews" / "jarvis-mask-wake.mp4"
    if output.exists():
        raise FileExistsError("Preview exists; back it up before rendering again")
    source = _remove_baked_shadow(np.asarray(Image.open(MASK_ASSET).convert("RGBA")))
    height, width = source.shape[:2]
    thresholds = _reveal_thresholds(width, height)
    eyes = _eye_pixels(source)
    opaque = np.argwhere(source[:, :, 3] > 150)
    rng = np.random.default_rng(50)
    selected = opaque[rng.choice(len(opaque), size=min(3000, len(opaque)), replace=False)]
    targets = [(int(x), int(y), float(thresholds[y, x]), _nanobot_color(i, bool(eyes[y, x])))
               for i, (y, x) in enumerate(selected)]
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with av.open(str(output), "w") as container:
            video = container.add_stream("libx264", rate=20)
            video.width, video.height, video.pix_fmt = 640, 960, "yuv420p"
            video.options = {"crf":"20", "preset":"medium"}
            for index in range(156):
                seconds = index / 20
                status = "listening" if seconds < NANO_LOCK_SECONDS + 0.5 else "speaking"
                voice_level = 0.5 + 0.5 * math.sin(seconds * 9) if status == "speaking" else None
                intensity = _display_eye_intensity(status, seconds, voice_level)
                pixels = _render_mask_pixels(source, thresholds, eyes, seconds, eye_intensity=intensity)
                plate = Image.fromarray(pixels, "RGBA")
                draw = ImageDraw.Draw(plate)
                for i, (x, y, arrival, color) in enumerate(targets):
                    position = _surface_particle_position(x, y, arrival, seconds, i)
                    if position is None:
                        continue
                    px, py = round(position[0]), round(position[1])
                    if not (0 <= px < width and 0 <= py < height) or source[py, px, 3] <= 100:
                        continue
                    draw.rectangle((px-1, py-1, px+1, py+1), fill=color)
                plate.thumbnail((560, 820), Image.Resampling.LANCZOS)
                frame = Image.new("RGB", (640, 960), (5, 14, 24))
                frame.paste(plate, ((640-plate.width)//2, (960-plate.height)//2), plate)
                for packet in video.encode(av.VideoFrame.from_image(frame)):
                    container.mux(packet)
            for packet in video.encode():
                container.mux(packet)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    print(f"Rendered {output.name}: 156 frames, 20 fps, 7.8 seconds")


if __name__ == "__main__":
    main()
