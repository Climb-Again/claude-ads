"""
generate_countdown.py — Create a 60-second countdown timer video.

Full-screen background cycles through 12 colors every 5 seconds.
A beep and white flash mark each 5-second transition.
Layout: logo large centered in top half, "Wall Crawls" title below logo,
        big countdown number in bottom half.

Usage:
    python generate_countdown.py [--output countdown_60s.mp4] [--width 1920] [--height 1080]
                                 [--logo assets/logo_white.png] [--title "Wall Crawls"]

Output:
    JSON summary on stdout, video file at --output path.
"""

import argparse
import json
import os
import sys
import numpy as np

try:
    try:
        from moviepy.editor import (
            ColorClip, TextClip, CompositeVideoClip, AudioArrayClip, ImageClip,
        )
    except ModuleNotFoundError:
        from moviepy import (
            ColorClip, TextClip, CompositeVideoClip, AudioArrayClip, ImageClip,
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
    """Short sine-wave beep with a fade-out to avoid clicks."""
    sample_rate = 44100
    n = int(duration * sample_rate)
    t = np.linspace(0, duration, n, endpoint=False)
    wave = np.sin(2 * np.pi * freq * t)
    fade_samples = int(fade * sample_rate)
    wave[-fade_samples:] *= np.linspace(1, 0, fade_samples)
    stereo = np.column_stack([wave, wave]).astype(np.float32)
    return AudioArrayClip(stereo, fps=sample_rate).with_duration(duration)


def make_logo_clip(logo_path: str, width: int, height: int, duration: int) -> ImageClip:
    """Logo scaled to fill ~40% of frame width, centered in top 45% of frame."""
    from PIL import Image
    img = Image.open(logo_path).convert("RGBA")
    target_w = int(width * 0.42)
    target_h = int(target_w * img.height / img.width)
    img = img.resize((target_w, target_h), Image.LANCZOS)
    arr = np.array(img)
    # Center horizontally; vertically centered in top 45% zone
    zone_h = int(height * 0.45)
    x = (width - target_w) // 2
    y = (zone_h - target_h) // 2
    return (
        ImageClip(arr, is_mask=False)
        .with_duration(duration)
        .with_position((x, y))
    )


def make_title_clip(title: str, width: int, height: int, duration: int) -> TextClip:
    """Title text centered just below the logo zone."""
    font_size = int(height * 0.07)
    canvas_w = int(width * 0.8)
    canvas_h = int(font_size * 1.5)
    y = int(height * 0.47)  # just below logo zone
    return (
        TextClip(font=FONT, text=title, font_size=font_size,
                 color="white", size=(canvas_w, canvas_h),
                 method="caption", text_align="center",
                 horizontal_align="center", vertical_align="center")
        .with_duration(duration)
        .with_position(("center", y))
    )


def build_countdown(width: int, height: int,
                    logo_path: str | None, title: str | None) -> CompositeVideoClip:
    clips = []
    beep = make_beep()

    # Number sits in bottom 45% of frame, centered in that zone
    num_zone_top = int(height * 0.55)
    num_zone_h = height - num_zone_top

    for countdown in range(DURATION, 0, -1):
        start = DURATION - countdown
        color_index = min(start // 5, len(COLORS) - 1)
        bg_rgb = hex_to_rgb(COLORS[color_index])
        is_transition = (countdown % 5 == 0)

        bg = (
            ColorClip(size=(width, height), color=bg_rgb)
            .with_duration(1)
            .with_start(start)
        )

        font_size = int(num_zone_h * (0.92 if is_transition else 0.82))
        canvas = (int(width * 0.7), int(font_size * 1.3))
        num_y = num_zone_top + (num_zone_h - int(font_size * 1.3)) // 2
        txt = (
            TextClip(font=FONT, text=str(countdown), font_size=font_size,
                     color="white", size=canvas,
                     method="caption", text_align="center",
                     horizontal_align="center", vertical_align="center")
            .with_position(("center", max(num_zone_top, num_y)))
            .with_duration(1)
            .with_start(start)
        )

        clips.append(bg)
        clips.append(txt)

        if is_transition:
            flash = (
                ColorClip(size=(width, height), color=(255, 255, 255))
                .with_opacity(0.25)
                .with_duration(0.12)
                .with_start(start)
            )
            clips.append(flash)
            txt = txt.with_audio(beep.with_start(start))
            clips[-2] = txt  # replace txt (flash is at -1)

    if logo_path and os.path.exists(logo_path):
        clips.append(make_logo_clip(logo_path, width, height, DURATION))
    if title:
        clips.append(make_title_clip(title, width, height, DURATION))

    return CompositeVideoClip(clips, size=(width, height))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 60-second countdown video.")
    parser.add_argument("--output", default="countdown_60s.mp4")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--logo", default=None, help="Path to logo PNG (white-on-transparent)")
    parser.add_argument("--title", default=None, help="Title text shown below logo")
    args = parser.parse_args()

    print(json.dumps({"status": "building", "output": args.output,
                      "resolution": f"{args.width}x{args.height}",
                      "logo": args.logo, "title": args.title}))

    video = build_countdown(args.width, args.height, args.logo, args.title)
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
