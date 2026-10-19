"""Align supplied wording to recognized word timestamps, leaving unsupported lines untimed."""
from difflib import SequenceMatcher
import re
import textwrap
import unicodedata

from .models import Caption, SpeechWord


def normalize(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def script_lines(script: str) -> list[str]:
    lines = []
    for paragraph in script.splitlines():
        for sentence in re.split(r"(?<=[.!?。！？])\s+", paragraph.strip()):
            lines.extend(textwrap.wrap(sentence, width=38, break_long_words=True, break_on_hyphens=False))
    return [line for line in lines if normalize(line)]


def character_matches(lines: list[str], spoken: str) -> dict[int, int]:
    """Lock exact sentence anchors before fuzzy gaps so omitted lines cannot steal their suffixes."""
    expected = "".join(lines)
    anchors = []
    script_cursor, spoken_cursor = 0, 0
    for line in lines:
        position = spoken.find(line, spoken_cursor) if len(line) >= 3 else -1
        if position >= 0:
            anchors.append((script_cursor, position, len(line)))
            spoken_cursor = position + len(line)
        script_cursor += len(line)
    anchors.append((len(expected), len(spoken), 0))
    matching = {}
    left_script, left_spoken = 0, 0
    for script_start, spoken_start, size in anchors:
        matcher = SequenceMatcher(None, expected[left_script:script_start], spoken[left_spoken:spoken_start], autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                matching[left_script + block.a + offset] = left_spoken + block.b + offset
        for offset in range(size):
            matching[script_start + offset] = spoken_start + offset
        left_script, left_spoken = script_start + size, spoken_start + size
    return matching


def align_script(script: str, words: list[SpeechWord], duration: float) -> tuple[list[Caption], dict]:
    lines = script_lines(script)
    normalized = [normalize(line) for line in lines]
    expected = "".join(normalized)
    if len(expected) > 8000:
        raise ValueError("대본은 8,000자 이하로 나누어 입력하세요.")
    spoken, times = [], []
    for word in words:
        text = normalize(word.text)
        start = max(0, word.start, times[-1][1] if times else 0)
        end = min(duration, word.end)
        if not text or end <= start:
            continue
        for i, char in enumerate(text):
            spoken.append(char)
            times.append((start + (end-start)*i/len(text), start + (end-start)*(i+1)/len(text)))
    # Bound the global comparison, including highly repetitive transcripts.
    if len(spoken) > 40000:
        raise ValueError("대본 시간 맞추기는 인식 문자 40,000자 이하 영상을 지원합니다. 영상을 나누어 분석하세요.")
    matching = character_matches(normalized, "".join(spoken))
    captions, report = [], []
    cursor, matched_chars = 0, 0
    for index, (line, text) in enumerate(zip(lines, normalized), 1):
        pairs = [(i, matching[cursor+i]) for i in range(len(text)) if cursor+i in matching]
        cursor += len(text)
        coverage = len(pairs) / len(text)
        matched_chars += len(pairs)
        item = {"line": index, "text": line, "score": round(coverage, 3), "status": "unmatched"}
        # Do not guess timing for unspoken lines or for a few coincidentally matching letters.
        if coverage >= .6 and len(pairs) >= min(3, len(text)):
            first, last = pairs[0], pairs[-1]
            start, end = times[first[1]][0], times[last[1]][1]
            density = len(pairs) / (last[1] - first[1] + 1)
            review = coverage < .98 or density < .8 or end-start > 8 or len(text) < 3
            if end > start:
                captions.append(Caption(start=round(start, 6), end=round(end, 6), text=line,
                                        needs_review=review, alignment_score=round(coverage, 3)))
                item.update(status="review" if review else "matched", start=round(start, 6), end=round(end, 6))
        report.append(item)
    return captions, {"method": "speech-word-matching", "coverage": round(matched_chars/max(1, len(expected)), 3),
                      "lines": report}
