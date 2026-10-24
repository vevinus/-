import json
import os
import re
import subprocess
from pathlib import Path

from .models import Caption, Cut, Effect, Settings, SpeechWord


def run(args: list[str], timeout: int = 7200) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"{args[0]} 실행 실패: {proc.stderr[-1800:]}")
    return proc.stdout + "\n" + proc.stderr


def probe(path: Path) -> dict:
    raw = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    info = json.loads(raw)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("영상 트랙이 있는 파일을 선택하세요.")
    duration = float(info["format"].get("duration", video.get("duration", 0)))
    if not 0 < duration <= 14400:
        raise ValueError("0초 초과, 4시간 이하 영상을 지원합니다.")
    rate = video.get("avg_frame_rate", "30/1").split("/")
    fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 30
    return {"duration": duration, "width": video["width"], "height": video["height"],
            "fps": max(1, min(120, round(fps))),
            "audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def parse_detection(log: str, kind: str, duration: float, padding: float) -> list[Cut]:
    prefix = "silence" if kind == "silence" else "freeze"
    pending = None
    found = []
    # FFmpeg may leave an open interval at EOF. Finalize it at media duration.
    for match in re.finditer(rf"{prefix}_(start|end):\s*([\d.eE+\-]+)", log):
        event, value = match.group(1), float(match.group(2))
        if event == "start":
            pending = value
        elif pending is not None:
            found.append((pending, value))
            pending = None
    if pending is not None:
        found.append((pending, duration))
    result = []
    for start, end in found:
        # Keep a small handle at each boundary, including freezes.
        start, end = max(0, start + padding), min(duration, end - padding)
        if end - start >= 0.08:
            result.append(Cut(id=f"{kind}-{len(result)}", kind=kind, start=round(start, 6),
                              end=round(end, 6), selected=kind == "silence"))
    return result


def detect(path: Path, meta: dict, settings: Settings) -> list[Cut]:
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-threads", "2", "-i", str(path),
            "-vf", f"scale=320:-2,freezedetect=n=-50dB:d={settings.freeze_duration}"]
    if meta["audio"]:
        args += ["-af", f"silencedetect=noise={settings.silence_db}dB:d={settings.silence_duration}"]
    args += ["-f", "null", "-"]
    log = run(args)
    cuts = parse_detection(log, "freeze", meta["duration"], settings.padding)
    if meta["audio"]:
        cuts += parse_detection(log, "silence", meta["duration"], settings.padding)
    return sorted(cuts, key=lambda c: (c.start, c.kind))


def transcribe(path: Path, folder: Path, settings: Settings, *, words_out: list[SpeechWord] | None = None) -> list[Caption]:
    os.environ.setdefault("HF_HOME", str(folder.parent / "huggingface"))
    from faster_whisper import WhisperModel

    audio = folder / "speech.wav"
    run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", "16000", str(audio)])
    # Local model inference; downloaded models are retained in the application data directory.
    model = WhisperModel(settings.model, device="cpu", compute_type="int8", cpu_threads=2,
                         download_root=str(folder.parent / "models"))
    segments, _ = model.transcribe(str(audio), language=None if settings.language == "auto" else settings.language,
                                   beam_size=3, vad_filter=True, word_timestamps=True)
    captions = []
    for segment in segments:
        # Short groups improve review and avoid long subtitles spanning cuts.
        words = segment.words or []
        if not words:
            if segment.text.strip() and segment.end > segment.start:
                captions.append(Caption(start=segment.start, end=segment.end, text=segment.text.strip()))
            continue
        if words_out is not None:
            words_out.extend(SpeechWord(start=word.start, end=word.end, text=word.word)
                             for word in words if word.end > word.start and word.word.strip())
        group = []
        for word in words:
            group.append(word)
            text = "".join(w.word for w in group).strip()
            if len(text) >= settings.caption_characters or word.end - group[0].start >= settings.caption_seconds or word.word.rstrip().endswith((".", "?", "!")):
                if word.end > group[0].start and text:
                    captions.append(Caption(start=group[0].start, end=word.end, text=text))
                group = []
        if group and group[-1].end > group[0].start:
            captions.append(Caption(start=group[0].start, end=group[-1].end,
                                    text="".join(w.word for w in group).strip()))
    return captions


def suggest_effects(captions: list[Caption]) -> list[Effect]:
    rules = [(('성공', '완성', '축하', '정답', 'success', 'congrat'), 'ding', '성공·완성 표현'),
             (('대박', '놀라', '깜짝', '우와', '와!', 'wow', 'surprise'), 'pop', '감탄·놀람 표현'),
             (('왜', '어떻게', '?', 'why', 'how'), 'pop', '질문 표현')]
    effects = []
    last = -10.0
    for caption in captions:
        if caption.start - last < 3:
            continue
        for keywords, kind, reason in rules:
            if any(k in caption.text.lower() for k in keywords):
                effects.append(Effect(time=caption.start, kind=kind, reason=reason))
                last = caption.start
                break
    return effects
