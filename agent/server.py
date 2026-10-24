import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
from threading import Lock
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .export import export_project
from .alignment import align_script
from .media import detect, probe, suggest_effects, transcribe
from .models import Edit, Settings
from .timeline import kept_ranges
from .style_profiles import compare_videos, read_project, summarize_project


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("CAPCUT_AGENT_DATA", ROOT / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-agent")
lock = Lock()
jobs: dict[str, dict] = {}
active_styles: set[str] = set()
app = FastAPI(title="CapCut Agent", docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])


@app.middleware("http")
async def local_requests(request, call_next):
    # Block cross-site form submissions to this local application.
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        return JSONResponse({"detail": "다른 사이트에서 보낸 요청은 허용하지 않습니다."}, status_code=403)
    return await call_next(request)


def folder(job_id: str) -> Path:
    try:
        if uuid.UUID(job_id).hex != job_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return DATA / job_id


def get_job(job_id: str) -> dict:
    directory = folder(job_id)
    with lock:
        if job_id not in jobs:
            try:
                jobs[job_id] = json.loads((directory / "job.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise HTTPException(404, "작업을 찾을 수 없습니다.")
            if jobs[job_id]["status"] in ("queued", "analyzing", "exporting"):
                jobs[job_id].update(status="error", message="실행이 중단되었습니다. 영상을 다시 선택해 주세요.")
        return json.loads(json.dumps(jobs[job_id]))


def update(job_id: str, **values):
    with lock:
        jobs[job_id].update(values)
        path = folder(job_id) / "job.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(jobs[job_id], ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


def analyze(job_id: str, settings: Settings):
    directory = folder(job_id)
    try:
        job = get_job(job_id)
        source = directory / job["source"]
        update(job_id, status="analyzing", message="영상 정보와 무음·정지 구간 분석 중")
        meta = probe(source)
        cuts = detect(source, meta, settings)
        captions, warnings, alignment = [], [], None
        if settings.transcribe and meta["audio"]:
            update(job_id, message="음성에서 자막 생성 중 · 첫 실행은 모델을 내려받습니다")
            try:
                if settings.script:
                    words = []
                    transcribe(source, directory, settings, words_out=words)
                    update(job_id, message="대본 문구를 음성 시간에 맞추는 중")
                    captions, alignment = align_script(settings.script, words, meta["duration"])
                    pending = sum(line["status"] == "review" for line in alignment["lines"])
                    missing = sum(line["status"] == "unmatched" for line in alignment["lines"])
                    if pending or missing:
                        warnings.append(f"대본 정렬: 시간 확인이 필요한 자막 {pending}개, 음성에서 찾지 못한 문장 {missing}개. 자막 탭에서 확인하세요.")
                else:
                    captions = transcribe(source, directory, settings)
                if not captions:
                    warnings.append("대본을 음성과 연결하지 못했습니다. 실제 발화와 대본·언어 설정을 확인하세요." if settings.script
                                    else "인식된 음성이 없습니다. 언어 설정과 원본 소리를 확인하세요.")
            except Exception as exc:
                warnings.append(f"자동 자막 생성 실패. 컷 검토와 수동 자막 입력은 가능합니다. 원인: {str(exc)[:400]}")
        if not meta["audio"]:
            warnings.append("오디오 트랙이 없어 무음 분석과 자동 자막을 생략했습니다.")
        effects = suggest_effects(captions)
        appearance = {}
        style_summary = None
        if settings.style_id:
            profile = get_style(settings.style_id)
            learned = profile['analysis']
            appearance = learned['caption_appearance'] or {}
            if profile['kind'] in ('commentary', 'twist'):
                for cut in cuts:
                    cut.selected = False
                warnings.append("선택한 스타일은 반전 맥락 보존을 위해 컷을 기본 미선택합니다. 삭제할 구간을 직접 확인하세요.")
            for effect in effects:
                effect.volume = learned['suggested_effect_volume']
            style_summary = {'id': profile['id'], 'name': profile['name'], 'kind': profile['kind']}
            warnings.append("예시의 자막 크기·색상·위치와 분할 목표를 적용했습니다. 원본의 대사·제목·컷 시점·음원을 새 영상에 복사하지 않습니다. 장면 의미 기반 자동 편집은 아직 지원하지 않습니다.")
        # Offer restrained transition effects at the end of removed intervals.
        for cut in cuts:
            if cut.selected and cut.end < meta["duration"] - .4 and all(abs(e.time-cut.end) >= 3 for e in effects):
                from .models import Effect
                effects.append(Effect(time=cut.end, kind="whoosh", reason="컷 전환", selected=False))
        edit = Edit(cuts=cuts, captions=captions, effects=sorted(effects, key=lambda e: e.time), caption_appearance=appearance)
        update(job_id, status="analyzed", message="분석 완료 · 컷과 자막을 확인하세요", meta=meta,
               edit=edit.model_dump(), warnings=warnings, script_alignment=alignment, applied_style=style_summary)
    except Exception as exc:
        update(job_id, status="error", message=str(exc)[:2000])


def export(job_id: str, edit: Edit):
    job = get_job(job_id)
    directory = folder(job_id)
    name = f"export-{uuid.uuid4().hex[:12]}"
    output = directory / name
    try:
        update(job_id, message="캡컷 프로젝트와 편집 미리보기 생성 중 · 영상 길이에 따라 시간이 걸립니다")
        timeline = export_project(directory / job["source"], job["meta"], edit, output)
        update(job_id, status="exported", message="프로젝트 준비 완료", export=name, timeline=timeline,
               edit=edit.model_dump())
    except Exception as exc:
        if output.exists():
            shutil.rmtree(output)
        update(job_id, status="analyzed", message=f"내보내기 실패: {str(exc)[:1500]}")


@app.get("/api/health")
def health():
    return {"ok": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}


@app.post("/api/jobs", status_code=202)
async def create_job(file: UploadFile = File(...), settings: str = Form("{}")):
    try:
        options = Settings.model_validate_json(settings)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if options.style_id:
        profile = get_style(options.style_id)
        if profile['status'] != 'ready':
            raise HTTPException(409, "분석이 완료된 스타일을 선택하세요.")
        targets = profile['analysis']['caption_targets']
        if targets:
            options.caption_characters = targets['characters']
            options.caption_seconds = targets['seconds']
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise HTTPException(422, "MP4, MOV, MKV, WebM, AVI, M4V 영상을 선택하세요.")
    job_id = uuid.uuid4().hex
    directory = folder(job_id)
    directory.mkdir()
    source = "source" + suffix
    size = 0
    try:
        with (directory / source).open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 4 * 1024**3:
                    raise HTTPException(413, "4GB 이하 파일을 선택하세요.")
                output.write(chunk)
        if not size:
            raise HTTPException(422, "빈 파일입니다.")
    except Exception:
        shutil.rmtree(directory)
        raise
    finally:
        await file.close()
    with lock:
        jobs[job_id] = {"id": job_id, "source": source, "filename": Path(file.filename or source).name,
                        "status": "queued", "message": "분석 대기 중", "warnings": [], "settings": options.model_dump()}
    update(job_id)
    pool.submit(analyze, job_id, options)
    return get_job(job_id)


@app.get("/api/jobs/{job_id}")
def status(job_id: str):
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/export", status_code=202)
def create_export(job_id: str, edit: Edit):
    job = get_job(job_id)
    if job["status"] not in ("analyzed", "exported"):
        raise HTTPException(409, "분석이 완료된 후 내보낼 수 있습니다.")
    duration = job["meta"]["duration"]
    if any(c.end > duration + .02 for c in edit.cuts + edit.captions) or any(e.time >= duration for e in edit.effects):
        raise HTTPException(422, "편집 시간이 원본 영상 범위를 벗어났습니다.")
    try:
        kept_ranges(duration, edit.cuts)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    update(job_id, status="exporting", message="프로젝트 생성 대기 중")
    pool.submit(export, job_id, edit)
    return {"id": job_id, "status": "exporting"}


@app.get("/api/jobs/{job_id}/media/{kind}")
def media(job_id: str, kind: str):
    job = get_job(job_id)
    directory = folder(job_id)
    if kind == "original":
        return FileResponse(directory / job["source"])
    filenames = {"preview": "preview.mp4", "project": "capcut-project.zip", "subtitles": "subtitles.srt"}
    if kind not in filenames or job["status"] != "exported":
        raise HTTPException(404, "완료된 결과물이 없습니다.")
    path = directory / job["export"] / filenames[kind]
    return FileResponse(path, filename=None if kind == "preview" else filenames[kind])


def style_folder(style_id: str) -> Path:
    return DATA / 'styles' / folder(style_id).name


def get_style(style_id: str) -> dict:
    path = style_folder(style_id) / 'profile.json'
    try:
        profile = json.loads(path.read_text(encoding='utf-8'))
        if profile['status'] in ('queued', 'analyzing') and style_id not in active_styles:
            profile.update(status='error', message='서버 재시작으로 예시 분석이 중단되었습니다. 파일을 다시 등록하세요.')
            save_style(profile)
        return profile
    except FileNotFoundError:
        raise HTTPException(404, "스타일을 찾을 수 없습니다.")


def save_style(profile: dict):
    path = style_folder(profile['id']) / 'profile.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(profile, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def analyze_style(profile: dict):
    directory = style_folder(profile['id'])
    try:
        profile.update(status='analyzing', message='프로젝트 구조와 영상 정보를 분석하는 중')
        save_style(profile)
        analysis = summarize_project(read_project(directory / 'project.zip'))
        original = probe(directory / profile['original'])
        edited = probe(directory / profile['edited'])
        warnings = []
        difference = abs(analysis['duration'] - edited['duration'])
        if difference > .15:
            warnings.append(f"프로젝트 {analysis['duration']:.2f}초와 편집본 {edited['duration']:.2f}초의 길이가 다릅니다. 다른 버전이거나 내보내기 범위가 다를 수 있어 정확한 구간 대응으로 간주하지 않습니다.")
        try:
            profile.update(message='원본과 편집본의 화면 유사도 후보를 비교하는 중')
            save_style(profile)
            comparison = compare_videos(directory / profile['original'], directory / profile['edited'], original['duration'], edited['duration'])
        except Exception:
            comparison = None
            warnings.append('화면 유사도 비교를 완료하지 못했습니다. 프로젝트에서 추출한 스타일만 저장했습니다.')
        warnings.append('분석은 편집 규칙 추출이며 AI 모델 재학습이 아닙니다. 효과음·폰트 파일과 의미 기반 컷·해설 자동 작성은 복원하지 않습니다.')
        profile.update(status='ready', message='편집 예시와 스타일 저장 완료', analysis=analysis,
                       comparison=comparison, original_meta=original, edited_meta=edited, warnings=warnings)
    except Exception as exc:
        profile.update(status='error', message=str(exc)[:1000])
    save_style(profile)
    active_styles.discard(profile['id'])


async def store_example(upload: UploadFile, destination: Path, limit: int):
    size = 0
    try:
        with destination.open('wb') as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, '영상은 파일당 4GB, 프로젝트 ZIP은 64MB 이하로 선택하세요.')
                output.write(chunk)
        if not size:
            raise HTTPException(422, '빈 파일입니다.')
    finally:
        await upload.close()


@app.post('/api/styles', status_code=202)
async def create_style(name: str = Form(..., min_length=1, max_length=80), kind: str = Form('commentary'),
                       project: UploadFile = File(...), original: UploadFile = File(...), edited: UploadFile = File(...)):
    if kind not in ('commentary', 'twist', 'information') or not name.strip():
        raise HTTPException(422, '스타일 이름과 종류를 확인하세요.')
    suffixes = [Path(file.filename or '').suffix.lower() for file in (original, edited)]
    if Path(project.filename or '').suffix.lower() != '.zip' or any(suffix not in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'} for suffix in suffixes):
        raise HTTPException(422, '프로젝트 ZIP과 원본·편집본 영상 파일을 선택하세요.')
    style_id = uuid.uuid4().hex
    directory = style_folder(style_id)
    directory.mkdir(parents=True)
    try:
        await store_example(project, directory / 'project.zip', 64 * 1024**2)
        await store_example(original, directory / ('original'+suffixes[0]), 4 * 1024**3)
        await store_example(edited, directory / ('edited'+suffixes[1]), 4 * 1024**3)
    except Exception:
        shutil.rmtree(directory)
        raise
    profile = {'id': style_id, 'name': name.strip(), 'kind': kind, 'status': 'queued', 'message': '예시 분석 대기 중',
               'original': 'original'+suffixes[0], 'edited': 'edited'+suffixes[1]}
    active_styles.add(style_id)
    save_style(profile)
    pool.submit(analyze_style, profile)
    return {'id': style_id, 'status': 'queued'}


@app.get('/api/styles')
def list_styles():
    root = DATA / 'styles'
    result = []
    for path in sorted(root.glob('*/profile.json')):
        try:
            profile = get_style(path.parent.name)
            result.append({key: profile[key] for key in ('id', 'name', 'kind', 'status')})
        except (ValueError, KeyError):
            continue
    return result


@app.get('/api/styles/{style_id}')
def style_status(style_id: str):
    return get_style(style_id)


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
