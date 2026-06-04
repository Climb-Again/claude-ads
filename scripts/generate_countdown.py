"""
generate_countdown.py — Create a 60-second countdown timer video.

Full-screen background cycles through 12 colors every 5 seconds.
A beep and white flash mark each 5-second transition.
Bold centered numerals with strong contrast throughout.
Optional logo (top-left) and title (top-center) overlays.

Usage:
    python generate_countdown.py [--output countdown_60s.mp4] [--width 1920] [--height 1080]
                                 [--logo assets/logo.png] [--title "Wall Crawls"]

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
MARGIN = 40  # px from edges for logo / title


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


def make_logo_clip(logo_path: str, height: int, duration: int) -> ImageClip:
    """Load logo, scale to logo_height, pin to top-left with margin."""
    logo_h = int(height * 0.10)  # 10% of frame height
    from PIL import Image
    img = Image.open(logo_path).convert("RGBA")
    aspect = img.width / img.height
    img = img.resize((int(logo_h * aspect), logo_h), Image.LANCZOS)

    # Composite onto transparent background to preserve alpha
    arr = np.array(img)
    clip = (
        ImageClip(arr, is_mask=False)
        .with_duration(duration)
        .with_position((MARGIN, MARGIN))
    )
    return clip


def make_title_clip(title: str, width: int, height: int, duration: int) -> TextClip:
    """Render title text, centered horizontally at the top."""
    font_size = max(40, int(height * 0.055))
    canvas_w = int(width * 0.6)
    canvas_h = int(font_size * 1.6)
    clip = (
        TextClip(font=FONT, text=title, font_size=font_size,
                 color="white", size=(canvas_w, canvas_h),
                 method="caption", text_align="center",
                 horizontal_align="center", vertical_align="center")
        .with_duration(duration)
        .with_position(("center", MARGIN))
    )
    return clip


def build_countdown(width: int, height: int,
                    logo_path: str | None, title: str | None) -> CompositeVideoClip:
    clips = []
    beep = make_beep()

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

        font_size = 560 if is_transition else 500
        # Scale number font to frame height so it works at any resolution
        font_size = int(height * (font_size / 1080))
        canvas = (int(width * 0.7), int(font_size * 1.4))
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
            flash = (
                ColorClip(size=(width, height), color=(255, 255, 255))
                .with_opacity(0.25)
                .with_duration(0.12)
                .with_start(start)
            )
            clips.append(flash)
            txt = txt.with_audio(beep.with_start(start))
            clips[-2] = txt  # replace txt (flash is at -1)

    # Logo and title sit on top of everything, spanning full duration
    if logo_path and os.path.exists(logo_path):
        clips.append(make_logo_clip(logo_path, height, DURATION))
    if title:
        clips.append(make_title_clip(title, width, height, DURATION))

    return CompositeVideoClip(clips, size=(width, height))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 60-second countdown video.")
    parser.add_argument("--output", default="countdown_60s.mp4")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--logo", default=None, help="Path to logo PNG (transparent background)")
    parser.add_argument("--title", default=None, help="Title text shown at top-center")
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
