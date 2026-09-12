import os
import subprocess
import main as automation


def safe_normalize_video(input_path):
    source_duration = automation.probe_duration(input_path)
    print(f"Source duration detected: {source_duration:.2f} seconds")

    if source_duration < 2.0:
        raise RuntimeError(
            f"Source video duration is only {source_duration:.2f}s. Refusing to upload a broken/partial file."
        )

    # A valid MP4 has already passed full FFmpeg decode validation in download_video().
    # Re-encoding some reels with unusual timestamps was shrinking an 8s clip to ~1s,
    # so preserve the original MP4 timing instead of changing it.
    if os.path.splitext(input_path)[1].lower() == ".mp4":
        print("Valid MP4 detected; preserving original video timing without re-encoding.")
        return input_path

    output_path = os.path.join(os.path.dirname(input_path), "youtube_normalized.mp4")
    command = [
        "ffmpeg", "-y", "-i", input_path,
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]

    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0:
        print(process.stderr)
        raise RuntimeError("FFmpeg normalization failed.")

    normalized_duration = automation.probe_duration(output_path)
    print(f"Normalized duration: {normalized_duration:.2f} seconds")

    if normalized_duration < 2.0 or abs(normalized_duration - source_duration) > max(2.0, source_duration * 0.20):
        print("Normalization changed duration unexpectedly; using the original validated video instead.")
        try:
            os.remove(output_path)
        except Exception:
            pass
        return input_path

    return output_path


automation.normalize_video = safe_normalize_video

if __name__ == "__main__":
    automation.main()
