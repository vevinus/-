from .models import Caption, Cut


def kept_ranges(duration: float, cuts: list[Cut]) -> list[tuple[float, float]]:
    removed = sorted((max(0, c.start), min(duration, c.end)) for c in cuts if c.selected and c.start < duration)
    merged: list[list[float]] = []
    for start, end in removed:
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    kept, cursor = [], 0.0
    for start, end in merged:
        if start - cursor >= 0.001:
            kept.append((cursor, start))
        cursor = end
    if duration - cursor >= 0.001:
        kept.append((cursor, duration))
    if not kept:
        raise ValueError("전체 영상을 삭제할 수 없습니다. 컷을 하나 이상 해제하세요.")
    return kept


def map_time(time: float, kept: list[tuple[float, float]]) -> float | None:
    cursor = 0.0
    for start, end in kept:
        if start <= time < end:
            return cursor + time - start
        cursor += end - start
    return None


def remap_captions(captions: list[Caption], kept: list[tuple[float, float]]) -> list[Caption]:
    result = []
    for caption in captions:
        cursor = 0.0
        for start, end in kept:
            left, right = max(start, caption.start), min(end, caption.end)
            if right - left >= 0.04 and caption.text.strip():
                result.append(caption.model_copy(update={"start": cursor + left - start, "end": cursor + right - start}))
            cursor += end - start
    result.sort(key=lambda c: c.start)
    # One editable subtitle track cannot contain overlapping segments.
    for i in range(len(result) - 1):
        result[i].end = min(result[i].end, result[i + 1].start)
    return [c for c in result if c.end - c.start >= 0.04]


def srt(captions: list[Caption]) -> str:
    def stamp(t):
        ms = round(t * 1000)
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        seconds, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"
    return "\n\n".join(f"{i}\n{stamp(c.start)} --> {stamp(c.end)}\n{c.text}"
                         for i, c in enumerate(captions, 1)) + "\n"
