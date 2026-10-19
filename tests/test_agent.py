import json
from pathlib import Path
import shutil
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from agent.export import export_project
from agent.media import detect, parse_detection, probe, run, suggest_effects
from agent.models import Caption, Cut, Edit, Effect, Settings
from agent.timeline import kept_ranges, map_time, remap_captions, srt
from install_project import install, replace_paths


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.fail("FFmpeg and FFprobe are required for media integration tests")
    root = tmp_path_factory.mktemp("media")
    path = root / "sample.mp4"
    run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=size=320x180:rate=20:duration=6", "-f", "lavfi", "-i",
         r"aevalsrc=if(between(t\,2\,4)\,0\,0.2*sin(2*PI*440*t)):s=48000:d=8",
         "-vf", "tpad=stop_mode=clone:stop_duration=2", "-c:v", "libx264", "-threads", "2",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])
    return path


def test_detection_and_eof():
    cuts = parse_detection("silence_start: 1.0\nsilence_end: 2.0\nsilence_start: 4.0", "silence", 5, .1)
    assert [(c.start, c.end) for c in cuts] == [(1.1, 1.9), (4.1, 4.9)]


def test_union_and_caption_mapping():
    cuts = [Cut(id="a", kind="silence", start=2, end=5), Cut(id="b", kind="freeze", start=4, end=6)]
    kept = kept_ranges(10, cuts)
    assert kept == [(0, 2), (6, 10)]
    assert map_time(4, kept) is None
    assert map_time(6, kept) == 2
    captions = remap_captions([Caption(start=1, end=7, text="경계 자막")], kept)
    assert [(c.start, c.end) for c in captions] == [(1, 2), (2, 3)]
    assert "00:00:02,000 --> 00:00:03,000" in srt(captions)
    with pytest.raises(ValueError):
        kept_ranges(10, [Cut(id="all", kind="silence", start=0, end=10)])


def test_effect_rules_and_spacing():
    captions = [Caption(start=0, end=1, text="우와!"), Caption(start=1, end=2, text="성공"),
                Caption(start=4, end=5, text="완성입니다")]
    effects = suggest_effects(captions)
    assert [(e.time, e.kind) for e in effects] == [(0, "pop"), (4, "ding")]


def test_real_detectors_export_render_and_portability(media, tmp_path):
    meta = probe(media)
    cuts = detect(media, meta, Settings(transcribe=False))
    silent = next(c for c in cuts if c.kind == "silence")
    frozen = next(c for c in cuts if c.kind == "freeze")
    assert silent.start == pytest.approx(2.15, abs=.08)
    assert silent.end == pytest.approx(3.85, abs=.08)
    assert frozen.start == pytest.approx(6.1, abs=.1)
    assert not frozen.selected  # Static slides are preserved by default.
    frozen.selected = True
    edit = Edit(cuts=cuts, captions=[Caption(start=4.5, end=5.5, text="한국어 자막")],
                effects=[Effect(time=4.7, kind="ding", reason="성공"),
                         Effect(time=3, kind="pop", reason="삭제되는 구간")])
    output = tmp_path / "한글 export"
    result = export_project(media, meta, edit, output)
    assert result["duration"] == pytest.approx(4.55, abs=.12)
    assert result["captions"][0]["start"] == pytest.approx(2.8, abs=.08)
    assert len(result["effects"]) == 1
    content = json.loads((output / "project/draft_content.json").read_text())
    assert sorted(t["type"] for t in content["tracks"]) == ["audio", "text", "video"]
    assert len(next(t for t in content["tracks"] if t["type"] == "video")["segments"]) == 3
    assert content["materials"]["videos"][0]["path"] == "__CAPCUT_ASSETS__/source.mp4"
    preview_meta = probe(output / "preview.mp4")
    assert preview_meta["audio"]
    assert preview_meta["duration"] == pytest.approx(result["duration"], abs=.15)
    with zipfile.ZipFile(output / "capcut-project.zip") as archive:
        assert {"install_project.py", "INSTALL.txt", "subtitles.srt", "project/assets/source.mp4"} <= set(archive.namelist())
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "existing.txt").write_text("preserve")
    installed = install(output / "project", drafts)
    installed_again = install(output / "project", drafts)
    assert installed != installed_again
    assert (drafts / "existing.txt").read_text() == "preserve"
    patched = json.loads((installed / "draft_content.json").read_text())
    installed_meta = json.loads((installed / "draft_meta_info.json").read_text())
    second_content = json.loads((installed_again / "draft_content.json").read_text())
    assert patched["id"] == installed_meta["draft_id"]
    assert patched["id"] != second_content["id"]
    path = patched["materials"]["videos"][0]["path"]
    assert Path(path).is_file()
    assert "__CAPCUT_ASSETS__" not in json.dumps(patched)
    assert (installed / "assets/source.mp4").read_bytes() == media.read_bytes()


def test_video_without_audio(tmp_path):
    source = tmp_path / "noaudio.mp4"
    run(["ffmpeg", "-y", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=size=160x90:rate=10:duration=1", "-an", "-c:v", "libx264", "-threads", "2", str(source)])
    meta = probe(source)
    assert not meta["audio"]
    output = tmp_path / "noaudio-export"
    export_project(source, meta, Edit(), output)
    assert probe(output / "preview.mp4")["duration"] == pytest.approx(1, abs=.15)


def wait_job(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] not in ("queued", "analyzing", "exporting"):
            return result
        time.sleep(.05)
    pytest.fail("Media worker did not finish within 30s")


def test_http_workflow_and_validation(media, tmp_path, monkeypatch):
    from agent import server
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "jobs", {})
    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["ok"]
        assert client.post("/api/jobs", files={"file": ("x.txt", b"x")}).status_code == 422
        assert client.post("/api/jobs", headers={"Origin": "https://other.example"}).status_code == 403
        assert client.get("/api/jobs/not-a-job").status_code == 404
        response = client.post("/api/jobs", files={"file": ("test.mp4", media.read_bytes(), "video/mp4")},
                               data={"settings": '{"transcribe":false}'})
        assert response.status_code == 202
        job_id = response.json()["id"]
        analyzed = wait_job(client, job_id)
        assert analyzed["status"] == "analyzed", analyzed
        bad = {"cuts": [{"id": "all", "kind": "silence", "start": 0, "end": 8, "selected": True}]}
        assert client.post(f"/api/jobs/{job_id}/export", json=bad).status_code == 422
        edit = analyzed["edit"]
        edit["captions"] = [{"start": .2, "end": 1, "text": "실제 API 검증"}]
        assert client.post(f"/api/jobs/{job_id}/export", json=edit).status_code == 202
        exported = wait_job(client, job_id)
        assert exported["status"] == "exported", exported
        assert client.get(f"/api/jobs/{job_id}/media/project").content[:2] == b"PK"
        assert "실제 API 검증" in client.get(f"/api/jobs/{job_id}/media/subtitles").content.decode("utf-8-sig")
        assert client.get(f"/api/jobs/{job_id}/media/preview").status_code == 200
        # Invalid media errors are reported by the background job, not silently treated as success.
        invalid = client.post("/api/jobs", files={"file": ("broken.mp4", b"broken")}, data={"settings": '{"transcribe":false}'}).json()
        assert wait_job(client, invalid["id"])["status"] == "error"


def test_installer_rejects_asset_traversal(tmp_path):
    with pytest.raises(ValueError):
        replace_paths("__CAPCUT_ASSETS__/../outside", tmp_path)


def test_script_api_alignment_and_export(media, tmp_path, monkeypatch):
    from agent import server
    from agent.models import SpeechWord
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "jobs", {})
    def recognized(path, folder, settings, *, words_out=None):
        assert settings.script
        words_out.extend([SpeechWord(start=.2, end=.6, text='우와'),
                          SpeechWord(start=.6, end=1, text='자동'),
                          SpeechWord(start=1, end=1.4, text='편집을'),
                          SpeechWord(start=1.4, end=2, text='시작합니다'),
                          SpeechWord(start=4.2, end=4.6, text='그리고'),
                          SpeechWord(start=4.6, end=5.2, text='결과를'),
                          SpeechWord(start=5.2, end=5.8, text='확인합니다')])
        return []
    monkeypatch.setattr(server, 'transcribe', recognized)
    script = '우와! 자동 편집을 시작합니다.\n그리고 결과를 확인합니다.\n촬영하지 않은 문장입니다.'
    with TestClient(server.app) as client:
        response = client.post('/api/jobs', files={'file': ('test.mp4', media.read_bytes())},
                               data={'settings': json.dumps({'script': script})})
        assert response.status_code == 202
        job_id = response.json()['id']
        job = wait_job(client, job_id)
        assert job['status'] == 'analyzed', job
        assert job['script_alignment']['lines'][-1]['status'] == 'unmatched'
        assert any('찾지 못한 문장 1개' in message for message in job['warnings'])
        assert [c['text'] for c in job['edit']['captions']] == ['우와!', '자동 편집을 시작합니다.', '그리고 결과를 확인합니다.']
        assert client.post(f'/api/jobs/{job_id}/export', json=job['edit']).status_code == 202
        exported = wait_job(client, job_id)
        assert exported['status'] == 'exported', exported
        text = client.get(f'/api/jobs/{job_id}/media/subtitles').content.decode('utf-8-sig')
        assert '그리고 결과를 확인합니다.' in text and '촬영하지' not in text
        assert '00:00:02,500' in text  # 4.2s original minus the ~1.7s silence cut.
        assert exported['script_alignment'] == job['script_alignment']


def test_script_asr_failure_remains_explicit(media, tmp_path, monkeypatch):
    from agent import server
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'jobs', {})
    def failed(*args, **kwargs):
        raise RuntimeError('모델 다운로드 불가')
    monkeypatch.setattr(server, 'transcribe', failed)
    with TestClient(server.app) as client:
        response = client.post('/api/jobs', files={'file': ('test.mp4', media.read_bytes())},
                               data={'settings': json.dumps({'script': '대본 내용입니다.'})})
        job = wait_job(client, response.json()['id'])
        assert job['edit']['captions'] == []
        assert job['script_alignment'] is None
        assert any('실패' in warning for warning in job['warnings'])
