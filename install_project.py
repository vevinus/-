"""Install an exported portable draft. Standard library only; safe to copy into exports."""
import argparse
import json
import os
from pathlib import Path
import shutil
import time
import uuid


TOKEN = "__CAPCUT_ASSETS__/"


def replace_paths(value, assets: Path):
    if isinstance(value, str) and value.startswith(TOKEN):
        relative = Path(value[len(TOKEN):])
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
            raise ValueError("Invalid asset path")
        target = assets / relative
        if not target.is_file():
            raise FileNotFoundError(target)
        return target.as_posix()
    if isinstance(value, dict):
        return {k: replace_paths(v, assets) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_paths(v, assets) for v in value]
    return value


def install(project: Path, drafts: Path) -> Path:
    if not (project / "draft_content.json").is_file():
        raise ValueError("draft_content.json이 있는 프로젝트 폴더를 선택하세요.")
    if not drafts.is_dir():
        raise ValueError("캡컷 설정에 표시된 기존 초안 저장 폴더를 지정하세요.")
    # Always create a new project; existing CapCut projects and global indexes are untouched.
    destination = drafts / f"CapCutAgent-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(project, destination)
        content_path = destination / "draft_content.json"
        content = replace_paths(json.loads(content_path.read_text(encoding="utf-8")), destination / "assets")
        draft_id = str(uuid.uuid4()).upper()
        content["id"] = draft_id
        content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        meta_path = destination / "draft_meta_info.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(draft_fold_path=destination.as_posix(), draft_root_path=drafts.as_posix(), draft_id=draft_id)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description="캡컷을 종료한 다음 새 프로젝트를 설치합니다. 기존 프로젝트는 보존합니다.")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent / "project")
    parser.add_argument("--drafts", type=Path)
    args = parser.parse_args()
    drafts = args.drafts
    if drafts is None:
        default = Path(os.environ.get("LOCALAPPDATA", "")) / "CapCut/User Data/Projects/com.lveditor.draft"
        entered = input(f"캡컷 초안 폴더 [{default}]: ").strip().strip('"')
        drafts = Path(entered) if entered else default
    print("새 프로젝트 생성:", install(args.project.resolve(), drafts.resolve()))
    print("캡컷을 다시 실행해 프로젝트를 확인하세요. 버전에 따라 목록 인식이 다를 수 있습니다.")


if __name__ == "__main__":
    main()
