import pytest

from agent.alignment import align_script, normalize, script_lines
from agent.models import Caption, Cut, Settings, SpeechWord
from agent.timeline import kept_ranges, remap_captions


def words(*items):
    return [SpeechWord(start=start, end=end, text=text) for start, end, text in items]


def test_korean_script_uses_word_times_and_preserves_script_spelling():
    speech = words((1, 2, '안녕하세요'), (4, 4.5, '오늘은'), (4.5, 5, '자동'),
                   (5, 5.6, '자막을'), (5.6, 6.5, '만듭니다'))
    captions, report = align_script('안녕하세요!\n오늘은 자동자막을 만듭니다.', speech, 10)
    assert [(c.start, c.end, c.text) for c in captions] == [(1, 2, '안녕하세요!'), (4, 6.5, '오늘은 자동자막을 만듭니다.')]
    assert report['coverage'] == 1
    assert all(line['status'] == 'matched' for line in report['lines'])


def test_unspoken_line_does_not_steal_exact_neighbor_suffix():
    captions, report = align_script('시작합니다.\n절대로발화하지않는내용입니다.\n마무리합니다.',
                                    words((1, 2, '시작합니다'), (8, 9, '마무리합니다')), 10)
    assert [(c.start, c.end, c.needs_review) for c in captions] == [(1, 2, False), (8, 9, False)]
    missing = report['lines'][1]
    assert missing['status'] == 'unmatched'
    assert 'start' not in missing and 'end' not in missing


def test_partial_mismatch_is_flagged_without_replacing_script_wording():
    captions, report = align_script('오늘은 멋진 영상을 만들어요.',
        words((1, 1.5, '오늘은'), (1.5, 2, '멋진'), (2, 2.5, '영상을'), (2.5, 3, '만듭니다')), 4)
    assert captions[0].text == '오늘은 멋진 영상을 만들어요.'
    assert captions[0].needs_review
    assert .6 <= captions[0].alignment_score < 1
    assert report['lines'][0]['status'] == 'review'


def test_unrelated_text_and_empty_audio_do_not_get_invented_times():
    for speech in ([], words((1, 2, '자동차가'), (2, 3, '출발했습니다'))):
        captions, report = align_script('나는 바나나를 먹었다.', speech, 5)
        assert captions == []
        assert report['lines'][0]['status'] == 'unmatched'


def test_repeated_script_needs_repeated_speech():
    captions, report = align_script('다시 시작합니다.\n다시 시작합니다.', words((1, 2, '다시시작합니다')), 5)
    assert len(captions) == 1
    assert [line['status'] for line in report['lines']] == ['matched', 'unmatched']


def test_extra_speech_and_long_pauses_require_review():
    captions, report = align_script('오늘 시작합니다.',
        words((1, 2, '오늘'), (2, 5, '추가설명아주길게'), (10, 11, '시작합니다')), 12)
    assert captions[0].needs_review
    assert report['lines'][0]['score'] == 1
    assert captions[0].start == 1 and captions[0].end == 11


def test_duration_clipping_overlapping_words_and_cut_remap():
    captions, _ = align_script('안녕하세요.\n자동 자막입니다.',
        words((1, 2.2, '안녕하세요'), (2, 3, '자동'), (3, 5, '자막입니다')), 4)
    assert captions[0].end <= captions[1].start
    assert captions[-1].end <= 4
    captions[1].needs_review = True
    mapped = remap_captions(captions, kept_ranges(4, [Cut(id='cut', kind='silence', start=0, end=.5)]))
    assert mapped[1].start == pytest.approx(captions[1].start-.5)
    assert mapped[1].needs_review
    assert mapped[1].alignment_score == captions[1].alignment_score


def test_script_limits_normalization_and_subtitle_wrapping():
    assert normalize('ＡＢＣ １２３, 안녕!') == 'abc123안녕'
    lines = script_lines('첫 문장입니다. 둘째 문장입니다.\n' + '아주 긴 문장 ' * 20)
    assert lines[:2] == ['첫 문장입니다.', '둘째 문장입니다.']
    assert all(len(line) <= 38 for line in lines)
    with pytest.raises(ValueError):
        Settings(script='대본', transcribe=False)
    with pytest.raises(ValueError):
        Settings(script='!!!')
    with pytest.raises(ValueError):
        Settings(script='가' * 8001)
