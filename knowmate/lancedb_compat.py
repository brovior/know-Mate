"""배포된 lancedb 버전이 실측 검증 범위(requirements.txt)와 일치하는지 앱 시작 시 확인한다.

배포는 PyInstaller onedir(빌드 시점에 의존성이 exe에 번들)라 사용자가 임의로 lancedb
버전을 바꿀 수 없다 — 즉 이 검사는 "런타임에 나쁜 버전이 설치될 위험"을 막는 것이 아니라,
빌드 환경에서 requirements.txt 상한을 넘는 lancedb가 실수로 번들됐을 때 조기에(설정
패널까지 가지 않고 시작 로그·트레이 알림만으로) 드러내는 것이 목적이다(설계 리뷰 19차 M-2).

purge의 `"unsupported"` 판정(knowmate/collector/purge_meta.py)은 이와 별개로 유지한다 —
그쪽은 실제 API 호출 실패를 감지하는 안전망이고, 이 모듈은 그 실패가 나기 전에 미리
알려주는 진단 신호일 뿐이다.
"""
from __future__ import annotations

MIN_SUPPORTED_VERSION = (0, 34, 0)
MAX_SUPPORTED_VERSION_EXCLUSIVE = (0, 35, 0)


def _parse_version_tuple(version_str: str) -> tuple[int, int, int] | None:
    """"0.34.0" 같은 버전 문자열을 (major, minor, patch) 정수 튜플로 파싱한다.

    각 구간의 선행 숫자만 취한다("1rc1" → 1). 숫자를 전혀 찾을 수 없으면 None
    (알 수 없는 형식 — 개발 빌드 등).
    """
    parts: list[int] = []
    for chunk in version_str.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return None
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def is_supported_lancedb_version(version_str: str) -> bool:
    """requirements.txt의 lancedb 버전 범위(`[0.34.0, 0.35.0)`)와 일치하는지 확인한다.

    파싱 실패(알 수 없는 버전 문자열)는 **비호환으로 간주하지 않는다** — 검증되지
    않았다는 신호일 뿐 실제 비호환 증거는 아니므로, 오탐으로 불필요한 경고를 띄우지
    않도록 통과(True)로 폴백한다. 이 검사의 목적은 "빌드 실수 조기 발견"이지 미지
    버전을 막는 관문이 아니다.
    """
    parsed = _parse_version_tuple(version_str)
    if parsed is None:
        return True
    return MIN_SUPPORTED_VERSION <= parsed < MAX_SUPPORTED_VERSION_EXCLUSIVE


def check_lancedb_version() -> str | None:
    """설치된 lancedb 버전을 확인한다. 검증 범위 밖이면 사용자에게 보여줄 경고 문구를,
    범위 안이거나 버전을 확인할 수 없으면 None을 반환한다.

    lancedb import 자체가 실패하는 경우(정상 설치라면 발생하지 않음)는 이 함수의
    책임 범위가 아니다 — 호출부가 이미 lancedb가 필요한 다른 초기화를 거친 뒤에나
    의미 있는 검사이므로, import 실패는 그쪽에서 먼저 드러난다.
    """
    import lancedb

    version_str = str(getattr(lancedb, "__version__", "unknown"))
    if is_supported_lancedb_version(version_str):
        return None
    return (
        f"설치된 lancedb 버전({version_str})이 검증된 범위"
        f"({'.'.join(map(str, MIN_SUPPORTED_VERSION))} 이상 "
        f"{'.'.join(map(str, MAX_SUPPORTED_VERSION_EXCLUSIVE))} 미만)를 벗어났습니다. "
        "폴더 정리(purge) 기능이 예상대로 동작하지 않을 수 있습니다."
    )
