"""Read-only CapCut example analysis. Never extract ZIPs or access embedded local paths."""
from collections import Counter
import json
import math
from pathlib import Path, PurePosixPath
from statistics import median
import subprocess
import zipfile

import numpy as np
from PIL import Image


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def read_project(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(f.file_size for f in entries) > 128 * 1024**2:
                raise ValueError("프로젝트 ZIP의 압축 해제 크기가 너무 큽니다. 미디어를 제외한 프로젝트만 넣으세요.")
            for entry in entries:
                name = PurePosixPath(entry.filename.replace('\\', '/'))
                if name.is_absolute() or '..' in name.parts:
                    raise ValueError("프로젝트 ZIP에 잘못된 경로가 있습니다.")
            candidates = sorted((f for f in entries if PurePosixPath(f.filename).name == 'draft_content.json'),
                                key=lambda f: (len(PurePosixPath(f.filename).parts), f.filename))
            if not candidates:
                raise ValueError("ZIP에서 draft_content.json을 찾지 못했습니다.")
            file = candidates[0]
            if file.file_size > 10 * 1024**2 or file.flag_bits & 1:
                raise ValueError("너무 크거나 암호화된 프로젝트 JSON입니다.")
            try:
                content = json.loads(archive.read(file).decode('utf-8-sig'))
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("프로젝트 JSON을 읽을 수 없습니다. 이 캡컷 버전의 암호화·변경된 형식은 지원하지 않습니다.")
            if not isinstance(content, dict) or not isinstance(content.get('tracks'), list) or not isinstance(content.get('materials'), dict):
                raise ValueError("지원하는 캡컷 타임라인 구조가 아닙니다.")
            return content
    except zipfile.BadZipFile:
        raise ValueError("정상적인 ZIP 파일이 아닙니다.")


def summarize_project(project: dict) -> dict:
    duration = number(project.get('duration')) / 1e6
    if duration <= 0:
        raise ValueError("프로젝트 길이가 유효하지 않습니다.")
    if sum(len(track.get('segments') or []) for track in project['tracks'] if isinstance(track, dict)) > 20000:
        raise ValueError("프로젝트의 구간 수가 너무 많습니다. 필요한 타임라인만 별도 프로젝트로 저장하세요.")
    materials = {m['id']: m for group in project['materials'].values() if isinstance(group, list)
                 for m in group if isinstance(m, dict) and isinstance(m.get('id'), str)}
    texts, audio, videos = [], [], []
    for track in project['tracks']:
        if not isinstance(track, dict):
            continue
        for segment in track.get('segments', []):
            if not isinstance(segment, dict):
                continue
            material = materials.get(segment.get('material_id'), {})
            timerange = segment.get('target_timerange') or {}
            start, length = number(timerange.get('start'))/1e6, number(timerange.get('duration'))/1e6
            if length <= 0 or start < 0:
                continue
            clip = segment.get('clip') or {}
            common = {'start': round(start, 6), 'end': round(start+length, 6), 'duration': round(length, 6)}
            if track.get('type') == 'text':
                try:
                    content = json.loads(material.get('content') or '{}')
                    text = content.get('text', '')
                    if not isinstance(text, str):
                        continue
                    styles = content.get('styles') or [{}]
                    style = styles[0]
                except (ValueError, TypeError, AttributeError, IndexError):
                    continue
                y = number((clip.get('transform') or {}).get('y'))
                size = number(style.get('size'), 8)
                color = ((style.get('fill') or {}).get('content') or {}).get('solid', {}).get('color')
                color = color if isinstance(color, list) and len(color) == 3 and all(isinstance(c, (float, int)) and 0 <= c <= 1 for c in color) else None
                if length >= .7 * duration and y > .4:
                    role = 'title'
                elif length >= .7 * duration:
                    role = 'persistent'
                elif text.strip().startswith(('(', '[', '（')) or (len(text) <= 3 and not any(c.isalnum() for c in text)):
                    role = 'comment'
                elif y < -.15:
                    role = 'narration'
                else:
                    role = 'emphasis'
                texts.append({**common, 'text': text[:2000], 'role': role, 'size': size, 'y': y, 'color': color,
                              'italic': bool(style.get('italic')), 'bold': bool(style.get('bold')),
                              'animated': bool(segment.get('common_keyframes')) or bool(segment.get('extra_material_refs'))})
            elif track.get('type') == 'audio':
                audio.append({**common, 'kind': material.get('type', 'unknown'),
                              'name': str(material.get('name') or material.get('material_name') or '이름 없음').replace('\\', '/').split('/')[-1][:200],
                              'volume': max(0, number(segment.get('volume'), 1)), 'speed': number(segment.get('speed'), 1)})
            elif track.get('type') == 'video':
                source = segment.get('source_timerange') or {}
                videos.append({**common, 'kind': material.get('type', 'unknown'),
                               'source_start': number(source.get('start'))/1e6,
                               'source_duration': number(source.get('duration'))/1e6,
                               'speed': number(segment.get('speed'), 1),
                               'scale': {axis: number((clip.get('scale') or {}).get(axis), 1) for axis in ('x', 'y')},
                               'keyframed': bool(segment.get('common_keyframes'))})
    narration = [t for t in texts if t['role'] == 'narration' and t['text'].strip()]
    colors = Counter(tuple(round(c, 3) for c in t['color']) for t in narration if t['color'] is not None)
    appearance = None
    targets = None
    if narration:
        appearance = {'size': max(4, min(40, median(t['size'] for t in narration))),
                      'y': max(-1, min(1, median(t['y'] for t in narration))),
                      'color': list(colors.most_common(1)[0][0]) if colors else [1, 1, 1],
                      'italic': sum(t['italic'] for t in narration) > len(narration)/2,
                      'bold': sum(t['bold'] for t in narration) > len(narration)/2}
        targets = {'characters': max(6, min(48, round(median(len(t['text']) for t in narration)))),
                   'seconds': max(.5, min(5, round(median(t['duration'] for t in narration), 2)))}
    sounds = [a for a in audio if a['kind'] == 'sound']
    canvas = project.get('canvas_config') or {}
    versions = {key: str((project.get(key) or {}).get('app_version') or '')[:64]
                for key in ('platform', 'last_modified_platform')}
    return {'duration': duration, 'application_versions': versions,
            'canvas': {k: canvas.get(k) for k in ('width', 'height', 'ratio')},
            'counts': {'video_segments': len(videos), 'still_segments': sum(v['kind'] == 'photo' for v in videos),
                       'text_segments': len(texts), 'audio_segments': len(audio),
                       'sound_segments': len(sounds), 'video_keyframed_segments': sum(v['keyframed'] for v in videos)},
            'texts': texts, 'audio': audio, 'video': videos, 'caption_appearance': appearance, 'caption_targets': targets,
            'suggested_effect_volume': max(.05, min(.35, median(a['volume'] for a in sounds))) if sounds else .22}


def sample_frames(path: Path, duration: float) -> tuple[np.ndarray, float]:
    rate = min(1.0, 120/duration)
    result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-an', '-vf',
                             f'fps={rate},scale=96:160', '-frames:v', '120', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                            capture_output=True, timeout=300)
    if result.returncode or not result.stdout:
        raise ValueError("영상 비교용 프레임을 추출하지 못했습니다.")
    frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 160, 96)
    return frames, rate


def content_band(frames: np.ndarray) -> tuple[int, int]:
    # Find the longest bright image band, excluding dark title/letterbox regions.
    rows = np.median(frames.mean(axis=2), axis=0) > 22
    runs, start = [], None
    for index, active in enumerate([*rows, False]):
        if active and start is None:
            start = index
        if not active and start is not None:
            runs.append((start, index)); start = None
    top, bottom = max(runs, key=lambda pair: pair[1]-pair[0]) if runs else (0, 160)
    if bottom-top < 40:
        top, bottom = 0, 160
    return top, bottom


def descriptors(frames: np.ndarray, band: tuple[int, int]) -> np.ndarray:
    top, bottom = band
    result = []
    for frame in frames:
        data = np.asarray(Image.fromarray(frame[top:bottom]).resize((32, 32)), dtype=float).ravel()
        data -= data.mean()
        result.append(data/max(np.linalg.norm(data), 1e-6))
    return np.asarray(result)


def compare_videos(original: Path, edited: Path, original_duration: float, edited_duration: float) -> dict:
    source, source_rate = sample_frames(original, original_duration)
    target, target_rate = sample_frames(edited, edited_duration)
    source_band, target_band = content_band(source), content_band(target)
    common = (max(source_band[0], target_band[0]), min(source_band[1], target_band[1]))
    modes = [('resized-content', descriptors(target, target_band) @ descriptors(source, source_band).T)]
    if common[1]-common[0] >= 40:
        modes.append(('same-position-crop', descriptors(target, common) @ descriptors(source, common).T))
    mode, similarities = max(modes, key=lambda pair: float(np.median(pair[1].max(axis=1))))
    points = []
    for i, row in enumerate(similarities):
        index = int(row.argmax())
        margin = float(row[index] - np.partition(row, -2)[-2]) if len(row) > 1 else 1.0
        points.append({'edited_time': round((i+.5)/target_rate, 3), 'original_time': round((index+.5)/source_rate, 3),
                       'similarity': round(float(row[index]), 3), 'margin': round(margin, 4),
                       'candidate': bool(row[index] >= .85 and margin >= .005)})
    return {'method': 'sampled-grayscale-correlation', 'crop_mode': mode, 'interval_seconds': round(1/target_rate, 3),
            'points': points, 'matched_samples': sum(p['candidate'] for p in points), 'samples': len(points)}
