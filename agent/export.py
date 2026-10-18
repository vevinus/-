import json
import math
from pathlib import Path
import shutil
import time
import uuid
import wave

import numpy as np
import pycapcut as cc

from .media import run
from .models import Edit
from .timeline import kept_ranges, map_time, remap_captions, srt


RATE = 24000


def portable_paths(value, paths: dict[str, str]):
    if isinstance(value, str):
        return paths.get(value, value)
    if isinstance(value, dict):
        return {key: portable_paths(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [portable_paths(item, paths) for item in value]
    return value


def synth(kind: str) -> np.ndarray:
    duration = {"pop": 0.18, "ding": 0.6, "whoosh": 0.3}[kind]
    t = np.arange(round(RATE * duration)) / RATE
    if kind == "pop":
        signal = np.sin(2 * math.pi * (700 * t - 1100 * t * t)) * np.exp(-t * 24)
    elif kind == "ding":
        signal = (np.sin(2 * math.pi * 880 * t) + .35 * np.sin(2 * math.pi * 1760 * t)) * np.exp(-t * 7)
    else:
        noise = np.random.default_rng(42).normal(0, .5, len(t))
        signal = np.convolve(noise, np.ones(8) / 8, mode="same") * np.sin(math.pi * t / duration) ** 2
    signal *= np.minimum(t / .006, 1) * np.minimum((duration - t) / .015, 1)
    return (signal * .6).astype(np.float32)


def wav(path: Path, samples: np.ndarray):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def mix_effects(path: Path, events: list[dict], duration: float):
    # Write in bounded blocks; a long video must not allocate a full-length float array.
    clips = [(round(e["time"] * RATE), synth(e["kind"]) * e["volume"]) for e in events]
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        for offset in range(0, round(duration * RATE), RATE * 10):
            block = np.zeros(min(RATE * 10, round(duration * RATE) - offset), dtype=np.float32)
            for start, samples in clips:
                left, right = max(offset, start), min(offset + len(block), start + len(samples))
                if left < right:
                    block[left-offset:right-offset] += samples[left-start:right-start]
            stream.writeframes((np.clip(block, -1, 1) * 32767).astype("<i2").tobytes())


def render_preview(source: Path, meta: dict, kept: list, events: list, folder: Path):
    duration = sum(end - start for start, end in kept)
    mix_effects(folder / "effects.wav", events, duration)
    # Encode each retained part independently, limiting graph size and memory use on long videos.
    parts = folder / "preview-parts"
    parts.mkdir()
    try:
        for i, (start, end) in enumerate(kept):
            args = ["ffmpeg", "-y", "-nostdin", "-v", "error", "-ss", str(start), "-i", str(source)]
            if not meta["audio"]:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            args += ["-t", str(end-start), "-map", "0:v:0", "-map", "0:a:0" if meta["audio"] else "1:a:0",
                     "-vf", "scale=w='min(960,iw)':h=-2,setsar=1", "-r", str(meta["fps"]),
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p",
                     "-threads", "2", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(parts / f"{i}.mp4")]
            run(args)
        (parts / "list.txt").write_text("\n".join(f"file '{i}.mp4'" for i in range(len(kept))), encoding="utf-8")
        run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(parts / "list.txt"),
             "-i", str(folder / "effects.wav"), "-filter_complex",
             "[0:a][1:a]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
             "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-t", str(duration),
             "-movflags", "+faststart", str(folder / "preview.mp4")])
    finally:
        shutil.rmtree(parts)
        (folder / "effects.wav").unlink(missing_ok=True)


def export_project(source: Path, meta: dict, edit: Edit, folder: Path, preview: bool = True) -> dict:
    folder.mkdir()
    kept = kept_ranges(meta["duration"], edit.cuts)
    captions = remap_captions(edit.captions, kept)
    duration = sum(end - start for start, end in kept)
    events = []
    for effect in edit.effects:
        target = map_time(effect.time, kept)
        if effect.selected and target is not None:
            events.append({**effect.model_dump(), "time": target})
    events.sort(key=lambda e: e["time"])
    # Per-track overlap is forbidden; stagger or omit crowded recommendations.
    spaced = []
    last_end = -1
    for event in events:
        length = min(len(synth(event["kind"])) / RATE, duration - event["time"])
        if event["time"] >= last_end and length >= .04:
            spaced.append(event)
            last_end = event["time"] + length
    events = spaced
    script = cc.DraftFolder(str(folder)).create_draft("project", meta["width"], meta["height"], meta["fps"])
    draft_id = str(uuid.uuid4()).upper()
    script.content["id"] = draft_id
    assets = folder / "project" / "assets"
    assets.mkdir()
    original = assets / f"source{source.suffix.lower()}"
    shutil.copy2(source, original)
    video = cc.VideoMaterial(str(original))
    script.add_track(cc.TrackType.video, "영상")
    cursor = 0
    for start, end in kept:
        start_us, end_us = round(start * 1e6), round(end * 1e6)
        end_us = min(end_us, video.duration)
        length = end_us - start_us
        if length <= 0:
            continue
        script.add_segment(cc.VideoSegment(video, cc.Timerange(cursor, length),
                                            source_timerange=cc.Timerange(start_us, length)), "영상")
        cursor += length
    if captions:
        script.add_track(cc.TrackType.text, "자막")
        for caption in captions:
            segment = cc.TextSegment(caption.text, cc.Timerange(round(caption.start * 1e6), round((caption.end-caption.start) * 1e6)),
                                     style=cc.TextStyle(size=8, bold=True, align=1, auto_wrapping=True),
                                     border=cc.TextBorder(width=20), clip_settings=cc.ClipSettings(transform_y=-.78))
            script.add_segment(segment, "자막")
    if events:
        script.add_track(cc.TrackType.audio, "효과음")
        for kind in {event["kind"] for event in events}:
            wav(assets / f"{kind}.wav", synth(kind))
        for event in events:
            material = cc.AudioMaterial(str(assets / f"{event['kind']}.wav"))
            length = min(material.duration, round((duration - event["time"]) * 1e6))
            script.add_segment(cc.AudioSegment(material, cc.Timerange(round(event["time"] * 1e6), length),
                                                volume=event["volume"]), "효과음")
    script.save()
    content_path = folder / "project" / "draft_content.json"
    # Placeholder paths are resolved by the installer after copying to the Windows PC.
    paths = {}
    for file in assets.iterdir():
        for path in (str(file.resolve()), file.resolve().as_posix()):
            paths[path] = f"__CAPCUT_ASSETS__/{file.name}"
    content = portable_paths(json.loads(content_path.read_text(encoding="utf-8")), paths)
    content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    meta_path = folder / "project" / "draft_meta_info.json"
    draft_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    draft_meta.update(draft_name="CapCut Agent 자동 편집", draft_id=draft_id,
                      tm_duration=cursor, tm_draft_create=int(time.time() * 1e6), tm_draft_modified=int(time.time() * 1e6))
    meta_path.write_text(json.dumps(draft_meta, ensure_ascii=False), encoding="utf-8")
    (folder / "subtitles.srt").write_text(srt(captions), encoding="utf-8-sig")
    timeline = {"duration": duration, "kept": kept, "captions": [c.model_dump() for c in captions], "effects": events}
    (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(Path(__file__).resolve().parents[1] / "install_project.py", folder / "install_project.py")
    (folder / "INSTALL.txt").write_text(
        "CapCut을 완전히 종료하세요.\nWindows Python 3.11 이상에서 터미널로 실행:\n"
        'py install_project.py --drafts "캡컷 설정에 표시된 초안 저장 폴더"\n'
        "기존 프로젝트는 덮어쓰지 않습니다. 설치 후 CapCut을 다시 여세요.\n"
        "캡컷 버전에 따라 프로젝트 형식/목록 인식이 다릅니다. 실제 앱 열기는 별도 확인이 필요합니다.\n"
        "자막은 텍스트 트랙, 효과음은 오디오 트랙에서 수정할 수 있습니다.\n", encoding="utf-8-sig")
    # Keep the review render out of the portable project ZIP.
    shutil.make_archive(str(folder / "capcut-project"), "zip", folder, "project")
    import zipfile
    with zipfile.ZipFile(folder / "capcut-project.zip", "a") as archive:
        for name in ("install_project.py", "INSTALL.txt", "subtitles.srt", "timeline.json"):
            archive.write(folder / name, name)
    if preview:
        render_preview(source, meta, kept, events, folder)
    return timeline
