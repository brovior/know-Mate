# vendored from ai-dev-workflow v0.1.4 — scripts/gpt_review.py
# 직접 수정 금지: 개선은 ai-dev-workflow 정본에 하고 init_project.py --update로 동기화한다.
"""GPT 독립 아키텍처 리뷰 브릿지 (채널 A/B 공용 실행체, 프로젝트 무관).

실행한 위치의 git 저장소를 자동 감지해, 커밋된 설계 문서를 명시 경로로 받아
역할 계약(`docs/ai-workflow/prompts/gpt-architect-reviewer.md`)과 함께 OpenAI API에
보내고, 리뷰를 `docs/ai-workflow/reviews/REVIEW-<날짜>-<slug>.md`로 저장한다.

보안 가드 — 외부 리뷰어가 볼 수 있는 것은 Git에 커밋된 것뿐:
  모든 입력 경로는 ① git 추적 파일이고 ② .gitignore 대상이 아니며 ③ 금지 패턴이
  아니어야 한다. 금지 패턴 = 공통 기본(비밀류) + 프로젝트 선언
  (`docs/ai-workflow/forbidden-patterns.txt`, 한 줄당 패턴 하나, `#` 주석).
  위반 시 즉시 거부(exit 2).
  ★ 반출 내용은 작업 트리가 아니라 **HEAD 커밋의 blob**(`git show HEAD:<path>`)에서만
  읽는다 — 커밋 안 된 수정·심볼릭 링크 우회가 새는 것을 원천 차단('커밋된 것만' 엄격 보장).

실행 표면:
  - PC 로컬(채널 A): OPENAI_API_KEY 환경변수 필요. Windows/WSL 무관(pathlib).
  - GitHub Action 러너(채널 B): 같은 코드가 repo Secret으로 실행된다.
  - 채널 C(--emit): API 없이 ChatGPT에 붙여넣을 프롬프트만 파일로 뽑는다(정액 구독용).
  - claude.ai 원격 컨테이너: 기본 네트워크 정책이 OpenAI를 차단(403)하므로
    네트워크 오류 시 채널 B 안내와 함께 exit 3.

옵션:
  - --source <파일|디렉터리…>: 소스 코드를 리뷰에 함께 넣는다(디렉터리는 git 추적 파일로 확장).
  - --emit: 채널 C. --context/--source/manifest 모두 반영된 프롬프트를 붙여넣기용으로 저장.

종료 코드: 0=성공, 2=입력/설정 오류(보안 가드 포함), 3=네트워크/API 오류.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

#: 어떤 프로젝트에서도 외부로 내보내지 않는 공통 금지 패턴 (비밀류)
BASE_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*secret*",
    "*credential*",
    "id_rsa*",
)

#: 컨텍스트 총량 상한(문자 수). 초과 시 거부 — 실수로 거대 파일을 태우는 것 방지.
MAX_CONTEXT_CHARS: int = 400_000

#: 기본 리뷰 모델 (환경변수 GPT_REVIEW_MODEL로 재정의)
DEFAULT_MODEL: str = "gpt-5.6-sol"


def _die(code: int, message: str) -> None:
    """오류 메시지를 stderr에 출력하고 지정 코드로 종료한다."""
    print(f"[gpt_review] 오류: {message}", file=sys.stderr)
    sys.exit(code)


def find_repo_root() -> Path:
    """현재 작업 디렉터리가 속한 git 저장소 루트를 찾는다. 없으면 exit 2."""
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        _die(2, "git 저장소 안에서 실행해야 한다 (저장소 루트 자동 감지 실패).")
    return Path(proc.stdout.strip()).resolve()


REPO_ROOT: Path = find_repo_root()

#: 역할 계약(시스템 프롬프트) — 프로젝트에 vendored된 사본
PROMPT_CONTRACT: Path = REPO_ROOT / "docs" / "ai-workflow" / "prompts" / "gpt-architect-reviewer.md"

#: 프로젝트별 금지 패턴 선언 파일
PROJECT_FORBIDDEN_FILE: Path = REPO_ROOT / "docs" / "ai-workflow" / "forbidden-patterns.txt"

#: 프로젝트별 '항상 배경으로 넣을 문서' 선언 파일.
#: 파일 이름은 프로젝트마다 다르므로 프레임워크는 이름을 하드코딩하지 않는다 —
#: 각 프로젝트가 이 파일에 자기 저장소의 소개/명세 문서 경로를 직접 선언한다.
PROJECT_CONTEXT_MANIFEST: Path = REPO_ROOT / "docs" / "ai-workflow" / "context-manifest.txt"

#: 리뷰 산출물 기본 디렉터리
DEFAULT_OUT_DIR: Path = REPO_ROOT / "docs" / "ai-workflow" / "reviews"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """저장소 루트에서 git 하위 명령을 실행한다(출력 캡처, 검사 없음)."""
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
    )


def load_forbidden_patterns() -> tuple[str, ...]:
    """공통 기본 + 프로젝트 선언 금지 패턴을 합쳐 돌려준다."""
    patterns = list(BASE_FORBIDDEN_PATTERNS)
    if PROJECT_FORBIDDEN_FILE.is_file():
        for line in PROJECT_FORBIDDEN_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return tuple(patterns)


def load_context_manifest() -> list[str]:
    """프로젝트가 선언한 '항상 배경으로 넣을 문서' 목록을 읽는다(없으면 빈 목록).

    존재하지 않는 경로는 경고 후 건너뛴다(선택적 배경 문서이므로 하드 실패 아님).
    존재하지만 보안 가드를 못 넘는 경로는 이후 build 단계에서 하드 거부된다.
    """
    if not PROJECT_CONTEXT_MANIFEST.is_file():
        return []
    files: list[str] = []
    for line in PROJECT_CONTEXT_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if (REPO_ROOT / line).is_file():
            files.append(line)
        else:
            print(f"[gpt_review] 알림: 배경 목록의 파일이 없어 건너뜀: {line}", file=sys.stderr)
    return files


def expand_source_paths(raw_paths: list[str]) -> list[str]:
    """--source 인자(파일 또는 디렉터리)를 git 추적 파일 목록으로 펼친다.

    디렉터리는 `git ls-files`로 추적 파일만 수집한다(비-gitignore 보장). 개별 파일별
    보안 가드(금지 패턴 등)는 이후 build 단계에서 적용되고, 총량은 MAX_CONTEXT_CHARS로 막힌다.
    """
    files: list[str] = []
    for raw in raw_paths:
        rel = _to_repo_relative(raw)
        abs_path = REPO_ROOT / Path(str(rel))
        if abs_path.is_dir():
            names = [n for n in _git("ls-files", "-z", "--", str(rel)).stdout.split("\0") if n.strip()]
            if not names:
                print(f"[gpt_review] 알림: --source 디렉터리에 추적 파일이 없어 건너뜀: {rel}", file=sys.stderr)
            files.extend(names)
        elif abs_path.is_file():
            files.append(str(rel))
        else:
            print(f"[gpt_review] 알림: --source 경로가 없어 건너뜀: {rel}", file=sys.stderr)
    return files


def _to_repo_relative(raw: str) -> PurePosixPath:
    """입력 경로를 저장소 상대 POSIX 경로로 정규화한다. 저장소 밖이면 거부한다."""
    p = Path(raw)
    resolved = (p if p.is_absolute() else Path.cwd() / p).resolve()
    try:
        rel = resolved.relative_to(REPO_ROOT)
    except ValueError:
        _die(2, f"저장소 밖 경로는 허용되지 않는다: {raw}")
    return PurePosixPath(rel.as_posix())


def guard_path(rel: PurePosixPath, forbidden: tuple[str, ...]) -> None:
    """경로 보안 가드 3종. 위반 시 exit 2 — 이 함수가 곧 반출 화이트리스트다.

    ① 금지 패턴 검사 ② git 추적 여부 ③ .gitignore 여부.
    (내용 존재/무결성은 read_committed_blob의 HEAD blob 읽기가 최종 판정한다.)
    """
    rel_str = str(rel)
    for pattern in forbidden:
        # 디렉터리 패턴(cache/*)은 하위 전체, 파일 패턴(*.sql)은 어느 깊이든 파일명 매칭
        if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(rel.name, pattern):
            _die(2, f"금지 패턴({pattern})에 걸린 경로다 — 외부 반출 불가: {rel_str}")

    if _git("ls-files", "--error-unmatch", rel_str).returncode != 0:
        _die(2, f"git 추적 파일이 아니다(커밋된 것만 반출 가능): {rel_str}")

    if _git("check-ignore", "-q", rel_str).returncode == 0:
        _die(2, f".gitignore 대상이다 — 외부 반출 불가: {rel_str}")


def read_committed_blob(rel: PurePosixPath) -> str:
    """HEAD 커밋의 blob 내용을 읽는다(작업 트리 아님).

    작업 트리 파일을 직접 읽으면 커밋 안 된 수정·심볼릭 링크 타깃 우회가 반출될 수 있으므로,
    내용은 항상 `git show HEAD:<path>`(커밋된 blob)에서만 가져온다. HEAD에 없으면(미커밋/삭제)
    거부한다. 작업 트리가 HEAD와 다르면(dirty) 커밋본을 쓴다는 사실을 알린다.
    """
    rel_str = str(rel)
    proc = _git("show", f"HEAD:{rel_str}")
    if proc.returncode != 0:
        _die(2, f"HEAD 커밋에 없다(미커밋/삭제) — 커밋 후 리뷰하라: {rel_str}")
    if _git("diff", "--quiet", "HEAD", "--", rel_str).returncode != 0:
        print(
            f"[gpt_review] 알림: {rel_str}에 커밋 안 된 변경이 있어 HEAD(커밋된) 내용으로 리뷰합니다.",
            file=sys.stderr,
        )
    return proc.stdout


def build_context(
    targets: list[str], source_files: list[str], extra_context: list[str]
) -> tuple[str, list[str]]:
    """리뷰 대상·소스 코드·참조 문서를 읽어 사용자 프롬프트 본문을 조립한다.

    반환: (프롬프트 본문, 가드를 통과한 상대 경로 목록 — 대상 → 소스 → 참조 순).
    대상이 항상 맨 앞이라 호출자는 passed[:len(targets)]로 대상만 슬라이스할 수 있다.
    """
    forbidden = load_forbidden_patterns()
    sections: list[str] = []
    passed: list[str] = []
    seen: set[str] = set()

    def _append(raw: str, kind: str) -> None:
        rel = _to_repo_relative(raw)
        if str(rel) in seen:  # 대상·소스·배경으로 중복 지정된 파일은 한 번만
            return
        guard_path(rel, forbidden)  # 경로 검증(금지/추적/무시)
        text = read_committed_blob(rel)  # 내용은 HEAD 커밋 blob에서만
        sections.append(f"\n---\n### [{kind}] `{rel}`\n\n{text}\n")
        passed.append(str(rel))
        seen.add(str(rel))

    for raw in targets:
        _append(raw, "리뷰 대상")
    for raw in source_files:
        _append(raw, "리뷰 대상 — 소스 코드")
    for raw in extra_context:
        _append(raw, "참조 컨텍스트 (리뷰 대상 아님)")

    body = (
        "다음 설계 문서와 (있다면) 소스 코드를 역할 계약에 따라 검증하라. '참조 컨텍스트'는 "
        "배경 이해용이며, 지적 대상은 '리뷰 대상' 문서와 소스 코드다.\n" + "".join(sections)
    )
    if len(body) > MAX_CONTEXT_CHARS:
        _die(2, f"컨텍스트가 상한({MAX_CONTEXT_CHARS:,}자)을 초과했다({len(body):,}자). 대상/소스를 줄여라.")
    return body, passed


def make_slug(targets: list[str]) -> str:
    """산출물 파일명용 slug를 리뷰 대상 파일명들로 만든다."""
    stems = [PurePosixPath(t).stem.lower() for t in targets]
    slug = "-".join(stems)[:60]
    return re.sub(r"[^a-z0-9\-_]", "-", slug) or "review"


def call_openai(system_prompt: str, user_prompt: str, model: str) -> str:
    """OpenAI API를 호출해 리뷰 본문을 받는다. 네트워크/정책 차단이면 exit 3."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        _die(
            2,
            "OPENAI_API_KEY가 없다. PC: 환경변수로 설정 / 모바일·웹 세션: 채널 B(설계 PR을 열면 "
            "GitHub Action이 리뷰)로 우회하라 — ai-dev-workflow README §3 참조.",
        )

    from openai import OpenAI  # 지연 임포트: --dry-run은 openai 없이도 동작

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:  # noqa: BLE001 — 네트워크/정책/API 오류를 채널 안내로 수렴
        print(
            "[gpt_review] OpenAI 호출 실패. claude.ai 원격 컨테이너라면 네트워크 정책이 "
            "api.openai.com을 차단(403)하는 환경일 수 있다 — 채널 B(설계 PR + GitHub Action)로 "
            "우회하라: ai-dev-workflow README §3.",
            file=sys.stderr,
        )
        _die(3, f"{type(exc).__name__}: {exc}")
    content = resp.choices[0].message.content
    if not content or not content.strip():
        _die(3, "모델 응답이 비어 있다.")
    return content.strip()


def write_review(
    body: str, targets: list[str], model: str, out_dir: Path, channel: str
) -> Path:
    """리뷰 본문을 메타데이터 헤더와 함께 reviews/ 파일로 저장한다."""
    today = _dt.date.today().strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"REVIEW-{today}-{make_slug(targets)}"
    out_path = out_dir / f"{base}.md"
    seq = 2
    while out_path.exists():  # 같은 날 재리뷰는 -2, -3…으로 이어 붙인다(이력 보존)
        out_path = out_dir / f"{base}-{seq}.md"
        seq += 1

    header = (
        f"# {out_path.stem}\n\n"
        f"| 생성일 | 모델 | 채널 | 리뷰 대상 |\n|---|---|---|---|\n"
        f"| {_dt.date.today().isoformat()} | {model} | {channel} | {', '.join(f'`{t}`' for t in targets)} |\n\n"
        f"> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.\n\n"
        f"---\n\n"
    )
    out_path.write_text(header + body + "\n", encoding="utf-8", newline="\n")
    return out_path


def emit_prompt(
    system_prompt: str, user_prompt: str, targets: list[str], out_dir: Path
) -> Path:
    """채널 C — API 호출 없이 ChatGPT에 붙여넣을 프롬프트 묶음을 파일로 뽑는다.

    정액 구독(ChatGPT Plus 등)만 있고 API 결제가 없을 때, 같은 역할 계약·문서 묶음을
    사람이 복사해 ChatGPT 창에 붙여넣도록 스크래치 파일로 만든다(reviews/_pending/).
    """
    today = _dt.date.today().strftime("%Y%m%d")
    pend = out_dir / "_pending"
    pend.mkdir(parents=True, exist_ok=True)
    # 스크래치 프롬프트가 git에 섞이지 않도록 _pending 전체를 무시(.gitignore 자신만 예외).
    gitignore = pend / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n!.gitignore\n", encoding="utf-8", newline="\n")
    base = f"REVIEW-{today}-{make_slug(targets)}-PROMPT"
    path = pend / f"{base}.md"
    seq = 2
    while path.exists():
        path = pend / f"{base}-{seq}.md"
        seq += 1

    marker = "===8<=== 여기서부터 파일 끝까지 전체를 복사해 ChatGPT 창에 붙여넣으세요 ===8<==="
    guide = (
        "# 채널 C — ChatGPT(정액 구독)에 붙여넣을 리뷰 프롬프트\n\n"
        "1. 아래 마커 다음의 **전체**를 복사해 ChatGPT 창에 붙여넣으세요.\n"
        f"2. 받은 리뷰를 `{out_dir.name}/REVIEW-{today}-<이름>.md`로 저장하세요"
        " (또는 Claude에게 붙여주면 저장·커밋해 드립니다).\n"
        "3. 이 `_pending` 파일은 스크래치이므로 커밋할 필요 없습니다.\n\n"
        f"{marker}\n\n"
    )
    body = f"{system_prompt}\n\n---\n\n{user_prompt}\n"
    path.write_text(guide + body, encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점."""
    parser = argparse.ArgumentParser(
        description="GPT 독립 아키텍처 리뷰 브릿지 — 커밋된 문서만 명시 경로로 반출한다.",
    )
    parser.add_argument("targets", nargs="+", help="리뷰 대상 문서 경로(저장소 상대, 1개 이상)")
    parser.add_argument(
        "--context", nargs="*", default=[], help="배경 이해용 참조 문서(리뷰 대상 아님, 배경 목록에 추가)"
    )
    parser.add_argument(
        "--source",
        nargs="*",
        default=[],
        help="리뷰에 함께 넣을 소스 파일/디렉터리(디렉터리는 git 추적 파일로 자동 확장)",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="context-manifest.txt(프로젝트 배경 목록)를 무시한다(대상+CLI --context만)",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="채널 C: API 호출 없이 ChatGPT에 붙여넣을 프롬프트를 파일로 뽑는다(정액 구독용)",
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT_DIR), help="리뷰 저장 디렉터리(기본: docs/ai-workflow/reviews)"
    )
    parser.add_argument(
        "--channel", default="A(local)", help="메타데이터에 기록할 채널 라벨(Action은 B(action) 전달)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="API 호출 없이 반출 예정 파일·컨텍스트 크기만 출력(반출 사전 검토)",
    )
    args = parser.parse_args(argv)

    if not PROMPT_CONTRACT.is_file():
        _die(2, f"역할 계약 파일이 없다: {PROMPT_CONTRACT} — init_project.py로 먼저 채택하라.")
    system_prompt = PROMPT_CONTRACT.read_text(encoding="utf-8")

    # 배경 문서 = 프로젝트 선언 목록(context-manifest.txt) + CLI --context.
    # 프레임워크는 이름을 모른다 — 목록은 프로젝트가 소유한다.
    manifest_files = [] if args.no_manifest else load_context_manifest()
    context_files = manifest_files + args.context
    source_files = expand_source_paths(args.source) if args.source else []
    user_prompt, passed = build_context(args.targets, source_files, context_files)
    model = os.environ.get("GPT_REVIEW_MODEL", DEFAULT_MODEL)

    if args.dry_run:
        print("[gpt_review] dry-run — 실제 호출 없음")
        print(f"  저장소 루트    : {REPO_ROOT}")
        print(f"  모델           : {model}")
        print(f"  반출 파일      : {len(passed)}개 (전부 보안 가드 통과)")
        for rel in passed:
            print(f"    - {rel}")
        print(f"  컨텍스트 크기  : {len(user_prompt):,}자 (상한 {MAX_CONTEXT_CHARS:,})")
        print(f"  산출 위치      : {Path(args.out)}")
        return 0

    if args.emit:  # 채널 C — API 없이 붙여넣기용 프롬프트만 생성
        out_path = emit_prompt(system_prompt, user_prompt, passed[: len(args.targets)], Path(args.out))
        print(f"[gpt_review] 채널 C 프롬프트 생성: {out_path.relative_to(REPO_ROOT)}")
        print("  → 이 파일 내용을 복사해 ChatGPT에 붙여넣고, 받은 리뷰를 reviews/에 저장하세요.")
        return 0

    review_body = call_openai(system_prompt, user_prompt, model)
    out_path = write_review(review_body, passed[: len(args.targets)], model, Path(args.out), args.channel)
    print(f"[gpt_review] 저장 완료: {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
