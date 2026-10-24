import io
import json
from pathlib import Path
import time
import zipfile

from fastapi.testclient import TestClient
import pytest

from agent.media import run
from agent.models import CaptionAppearance
from agent.style_profiles import compare_videos, read_project, summarize_project


def example_project(duration=6):
    def span(start, duration):
        return {'start': round(start*1e6), 'duration': round(duration*1e6)}
    def text(identifier, content, size):
        return {'id': identifier, 'content': json.dumps({'text': content, 'styles': [
            {'size': size, 'fill': {'content': {'solid': {'color': [1, 1, 1]}}},
             'font': {'path': 'C:/private/user/font.ttf'}}]})}
    return {'duration': round(duration*1e6), 'canvas_config': {'width': 1080, 'height': 1920},
            'tracks': [
                {'type': 'video', 'segments': [{'material_id': 'v', 'target_timerange': span(0, duration),
                    'source_timerange': span(0, duration), 'clip': {'scale': {'x': 1.1, 'y': 1.1}}}]},
                {'type': 'text', 'segments': [
                    {'material_id': 'title', 'target_timerange': span(0, duration), 'clip': {'transform': {'y': .7}}},
                    {'material_id': 'caption', 'target_timerange': span(1, 1), 'clip': {'transform': {'y': -.4}}},
                    {'material_id': 'comment', 'target_timerange': span(2, .5), 'clip': {'transform': {'y': .2}}}]},
                {'type': 'audio', 'segments': [{'material_id': 'a', 'target_timerange': span(1, .5), 'volume': .25}]}],
            'materials': {'videos': [{'id': 'v', 'type': 'video', 'path': 'C:/private/user/original.mp4'}],
                          'texts': [text('title', '참고 제목', 24), text('caption', '이 영상의 설명', 13), text('comment', '(깜짝)', 8)],
                          'audios': [{'id': 'a', 'type': 'sound', 'name': 'Test pop', 'path': 'C:/private/sfx.wav'}]}}


def archive(project):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as zipped:
        zipped.writestr('Example/draft_content.json', json.dumps(project))
        zipped.writestr('Example/Timelines/id/draft_content.json', json.dumps(project))
    return stream.getvalue()


@pytest.fixture(scope='module')
def style_media(tmp_path_factory):
    source = tmp_path_factory.mktemp('style-media')/'source.mp4'
    run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
         'testsrc2=size=160x90:rate=10:duration=6', '-f', 'lavfi', '-i',
         r'aevalsrc=if(between(t\,2\,4)\,0\,0.2*sin(2*PI*440*t)):s=48000:d=6',
         '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', '-shortest', str(source)])
    return source


def test_zip_parser_and_sanitized_style_summary(tmp_path):
    path = tmp_path/'project.zip'
    path.write_bytes(archive(example_project()))
    result = summarize_project(read_project(path))
    assert result['counts']['text_segments'] == 3  # Do not double count the Timeline copy.
    assert [text['role'] for text in result['texts']] == ['title', 'narration', 'comment']
    assert result['caption_appearance']['size'] == 13
    assert result['caption_appearance']['y'] == -.4
    assert result['suggested_effect_volume'] == .25
    assert result['video'][0]['scale']['x'] == 1.1
    assert 'C:/private' not in json.dumps(result)


@pytest.mark.parametrize('filename,content', [('empty.txt', '{}'), ('draft_content.json', 'encrypted-data'),
                                              ('../draft_content.json', '{}')])
def test_unsupported_and_unsafe_archives(tmp_path, filename, content):
    path = tmp_path/'bad.zip'
    with zipfile.ZipFile(path, 'w') as zipped:
        zipped.writestr(filename, content)
    with pytest.raises(ValueError):
        read_project(path)
    assert not (tmp_path.parent/'draft_content.json').exists()


def test_video_comparison_with_masked_title_regions(style_media, tmp_path):
    edited = tmp_path/'edited.mp4'
    run(['ffmpeg', '-v', 'error', '-y', '-nostdin', '-i', str(style_media), '-vf',
         'drawbox=x=0:y=0:w=iw:h=15:color=black:t=fill,drawbox=x=0:y=75:w=iw:h=15:color=black:t=fill',
         '-c:v', 'libx264', '-threads', '2', '-an', str(edited)])
    result = compare_videos(style_media, edited, 6, 6)
    assert result['samples'] == 6
    assert result['crop_mode'] == 'same-position-crop'
    assert result['matched_samples'] >= 3
    assert result['points'][0]['original_time'] == .5


def wait_style(client, style_id):
    end = time.monotonic()+30
    while time.monotonic() < end:
        profile = client.get(f'/api/styles/{style_id}').json()
        if profile['status'] not in ('queued', 'analyzing'):
            return profile
        time.sleep(.05)
    pytest.fail('Style analysis did not finish')


def test_register_persist_apply_and_export_style(style_media, tmp_path, monkeypatch):
    from agent import server
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'jobs', {})
    monkeypatch.setattr(server, 'active_styles', set())
    with TestClient(server.app) as client:
        video = style_media.read_bytes()
        response = client.post('/api/styles', data={'name': '반전 예시', 'kind': 'twist'}, files={
            'project': ('project.zip', archive(example_project(duration=7))),
            'original': ('original.mp4', video), 'edited': ('edited.mp4', video)})
        assert response.status_code == 202
        style_id = response.json()['id']
        profile = wait_style(client, style_id)
        assert profile['status'] == 'ready', profile
        assert any('길이가 다릅니다' in warning for warning in profile['warnings'])
        assert client.get('/api/styles').json()[0]['id'] == style_id
        assert json.loads((tmp_path/'styles'/style_id/'profile.json').read_text())['status'] == 'ready'
        # Styles contain observations, not copies of the original example's captions for reuse.
        job = client.post('/api/jobs', data={'settings': json.dumps({'transcribe': False, 'style_id': style_id})},
                          files={'file': ('source.mp4', video)}).json()
        end = time.monotonic()+30
        while time.monotonic() < end:
            job = client.get(f"/api/jobs/{job['id']}").json()
            if job['status'] not in ('queued', 'analyzing'):
                break
            time.sleep(.05)
        assert job['status'] == 'analyzed', job
        assert job['edit']['caption_appearance']['size'] == 13
        assert job['edit']['captions'] == []
        assert job['edit']['cuts'] and all(not cut['selected'] for cut in job['edit']['cuts'])
        edit = job['edit']
        edit['captions'] = [{'start': .2, 'end': 1, 'text': '새 영상의 자막'}]
        assert client.post(f"/api/jobs/{job['id']}/export", json=edit).status_code == 202
        while time.monotonic() < end:
            job = client.get(f"/api/jobs/{job['id']}").json()
            if job['status'] != 'exporting':
                break
            time.sleep(.05)
        assert job['status'] == 'exported', job
        data = client.get(f"/api/jobs/{job['id']}/media/project").content
        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            content = json.loads(zipped.read('project/draft_content.json'))
            text = json.loads(content['materials']['texts'][0]['content'])
            assert text['text'] == '새 영상의 자막'
            assert text['styles'][0]['size'] == 13


def test_invalid_zip_and_interrupted_style_report_errors(style_media, tmp_path, monkeypatch):
    from agent import server
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'active_styles', set())
    with TestClient(server.app) as client:
        video = style_media.read_bytes()
        result = client.post('/api/styles', data={'name': 'bad'}, files={'project': ('x.zip', b'bad'),
            'original': ('x.mp4', video), 'edited': ('x.mp4', video)}).json()
        assert wait_style(client, result['id'])['status'] == 'error'
        style_id = 'a'*32
        path = tmp_path/'styles'/style_id
        path.mkdir()
        (path/'profile.json').write_text(json.dumps({'id': style_id, 'status': 'analyzing'}))
        assert client.get(f'/api/styles/{style_id}').json()['status'] == 'error'
        assert client.get('/api/styles/not-valid').status_code == 404
    with pytest.raises(ValueError):
        CaptionAppearance(color=(1, 2, 0))
