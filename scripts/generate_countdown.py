"""
generate_countdown.py — Create a 60-second countdown timer video.

Full-screen background cycles through 12 colors every 5 seconds.
A beep and white flash mark each 5-second transition.
Bold centered numerals with strong contrast throughout.

Usage:
    python generate_countdown.py [--output countdown_60s.mp4] [--width 1080] [--height 1920]

Output:
    JSON summary on stdout, video file at --output path.
"""

import argparse
import json
import sys
import numpy as np

try:
    try:
        from moviepy.editor import (
            ColorClip, TextClip, CompositeVideoClip, AudioArrayClip,
        )
    except ModuleNotFoundError:
        # moviepy 2.x removed the .editor shim
        from moviepy import (
            ColorClip, TextClip, CompositeVideoClip, AudioArrayClip,
        )
except ImportError:
    print(json.dumps({"error": "moviepy not installed. Run: pip install moviepy"}))
    sys.exit(1)


DURATION = 60
COLORS = [
    "#1e293b", "#166534", "#1e40af", "#991b1b",
    "#5b21b6", "#134e4a", "#9a3412", "#1e1b4b",
    "#7f1d1d", "#854d0e", "#334155", "#1a1a2e",
]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def make_beep(duration: float = 0.12, freq: float = 880, fade: float = 0.02) -> AudioArrayClip:
    """Short sine-wave beep with a short fade-out to avoid clicks."""
    sample_rate = 44100
    n = int(duration * sample_rate)
    t = np.linspace(0, duration, n, endpoint=False)
    wave = np.sin(2 * np.pi * freq * t)
    # Apply fade-out
    fade_samples = int(fade * sample_rate)
    wave[-fade_samples:] *= np.linspace(1, 0, fade_samples)
    stereo = np.column_stack([wave, wave]).astype(np.float32)
    return AudioArrayClip(stereo, fps=sample_rate).with_duration(duration)


def build_countdown(width: int, height: int) -> CompositeVideoClip:
    clips = []
    beep = make_beep()

    for countdown in range(DURATION, 0, -1):
        start = DURATION - countdown            # timeline position (seconds)
        color_index = min(start // 5, len(COLORS) - 1)
        bg_rgb = hex_to_rgb(COLORS[color_index])
        is_transition = (countdown % 5 == 0)

        # Background
        bg = (
            ColorClip(size=(width, height), color=bg_rgb)
            .with_duration(1)
            .with_start(start)
        )

        # Number label — larger on transition seconds for a "pulse" feel.
        # Use a fixed canvas size (never smaller than the glyph) to prevent
        # tight cropping that clips ascenders/descenders.
        font_size = 560 if is_transition else 500
        canvas = (int(width * 0.9), int(font_size * 1.4))
        txt = (
            TextClip(font=FONT, text=str(countdown), font_size=font_size,
                     color="white", size=canvas,
                     method="caption", text_align="center",
                     horizontal_align="center", vertical_align="center")
            .with_position("center")
            .with_duration(1)
            .with_start(start)
        )

        clips.append(bg)
        clips.append(txt)

        if is_transition:
            # Brief white flash overlay (0.12 s)
            flash = (
                ColorClip(size=(width, height), color=(255, 255, 255))
                .with_opacity(0.25)
                .with_duration(0.12)
                .with_start(start)
            )
            clips.append(flash)

            # Beep attached to the text clip at this second
            txt = txt.with_audio(beep.with_start(start))
            clips[-2] = txt   # replace the txt (index -2; flash is -1)

    return CompositeVideoClip(clips, size=(width, height))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 60-second countdown video.")
    parser.add_argument("--output", default="countdown_60s.mp4")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    args = parser.parse_args()

    print(json.dumps({"status": "building", "output": args.output,
                      "resolution": f"{args.width}x{args.height}"}))

    video = build_countdown(args.width, args.height)
    video.write_videofile(
        args.output,
        fps=30,
        codec="libx264",
        audio_codec="aac",
        ffmpeg_params=["-movflags", "+faststart"],
        logger="bar",
    )

    print(json.dumps({"status": "done", "output": args.output,
                      "duration": DURATION, "colors": len(COLORS)}))


if __name__ == "__main__":
    main()
