"""Isolated, time-limited local media decode/ASR child. No network input is accepted.

The parent kills this process on timeout. Output is private JSON, never stdout/logs.
PyAV bundles the decoder; faster-whisper is an optional, pinned voice dependency.
"""

import argparse
import json
from pathlib import Path


def transcribe(path, model_name, max_seconds):
    import av
    import numpy as np
    from faster_whisper import WhisperModel

    frames, samples = [], 0
    # Read frames incrementally, rejecting video and overlong decoded audio before ASR.
    with av.open(str(path), mode="r") as container:
        container_name = container.format.name
        if len(container.streams.audio) != 1 or container.streams.video:
            raise ValueError("invalid_audio")
        if container.format.name not in {
            "wav",
            "ogg",
            "mp3",
            "mov,mp4,m4a,3gp,3g2,mj2",
            "matroska,webm",
        }:
            raise ValueError("invalid_audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                samples += converted.samples
                if samples > max_seconds * 16000:
                    raise ValueError("invalid_audio")
                frames.append(converted.to_ndarray().flatten())
        for converted in resampler.resample(None):
            samples += converted.samples
            if samples > max_seconds * 16000:
                raise ValueError("invalid_audio")
            frames.append(converted.to_ndarray().flatten())
    if samples < 1600:
        raise ValueError("invalid_audio")
    audio = np.concatenate(frames).astype(np.float32) / 32768.0
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2)
    segments, info = model.transcribe(
        audio, beam_size=1, vad_filter=True, condition_on_previous_text=False
    )
    parts, length = [], 0
    for segment in segments:
        length += len(segment.text)
        if length > 20000:
            raise ValueError("invalid_audio")
        parts.append(segment.text)
    text = "".join(parts).strip()
    if not text or "\x00" in text:
        raise ValueError("no_speech")
    media_type = {
        "wav": "audio/wav", "ogg": "audio/ogg", "mp3": "audio/mpeg",
        "mov,mp4,m4a,3gp,3g2,mj2": "audio/mp4", "matroska,webm": "audio/webm",
    }[container_name]
    return {"transcript": text, "duration_seconds": samples / 16000,
            "language": info.language, "media_type": media_type}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument(
        "--model",
        choices=["tiny", "tiny.en", "base", "base.en", "small", "small.en"],
        required=True,
    )
    parser.add_argument("--max-seconds", type=int, required=True)
    args = parser.parse_args()
    try:
        result = transcribe(Path(args.input), args.model, args.max_seconds)
    except (ValueError, ImportError) as error:
        result = {
            "error_code": "speech_unavailable"
            if isinstance(error, ImportError)
            else "invalid_audio"
        }
    except Exception:
        result = {"error_code": "speech_unavailable"}
    Path(args.output).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
