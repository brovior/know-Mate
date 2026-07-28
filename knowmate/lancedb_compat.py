"""배포된 lancedb 버전이 실측 검증 버전(requirements.txt)과 일치하는지 앱 시작 시 확인한다.

배포는 PyInstaller onedir(빌드 시점에 의존성이 exe에 번들)라 사용자가 임의로 lancedb
버전을 바꿀 수 없다 — 즉 이 검사는 "런타임에 나쁜 버전이 설치될 위험"을 막는 것이 아니라,
빌드 환경에서 검증되지 않은 lancedb가 실수로 번들됐을 때 조기에(설정 패널까지 가지 않고
시작 로그·트레이 알림만으로) 드러내는 것이 목적이다(설계 리뷰 19차 M-2).

**정확히 한 버전만 허용한다**(범위가 아님, 설계 리뷰 20차 M-1): projection 계약
(`table.search().select([...]).to_arrow()`의 결과 스키마·전건 반환)은 0.34.0에서만 실측했다.
`>=0.34.0,<0.35`처럼 범위를 허용하면 실측하지 않은 0.34.x가 빌드에 선택될 수 있고, 그러면
이 검사를 통과하면서도 동작이 다를 수 있어 "검증된 버전을 번들한다"는 목적 자체가
무너진다. 상위 버전으로 올릴 때는 projection 실측을 다시 하고 `SUPPORTED_VERSION`과
requirements.txt를 함께 갱신한다.

purge의 `"unsupported"` 판정(knowmate/collector/purge_meta.py)은 이와 별개로 유지한다 —
그쪽은 실제 API 호출 실패를 감지하는 안전망이고, 이 모듈은 그 실패가 나기 전에 미리
알려주는 진단 신호일 뿐이다.
"""
from __future__ import annotations

# requirements.txt의 `lancedb==` 값과 반드시 일치시킨다.
SUPPORTED_VERSION = (0, 34, 0)


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
    """requirements.txt에 고정한 실측 검증 버전(`SUPPORTED_VERSION`)과 정확히 같은지 확인한다.

    파싱 실패(알 수 없는 버전 문자열)는 **비호환으로 간주하지 않는다** — 검증되지
    않았다는 신호일 뿐 실제 비호환 증거는 아니므로, 오탐으로 불필요한 경고를 띄우지
    않도록 통과(True)로 폴백한다. 이 검사의 목적은 "빌드 실수 조기 발견"이지 미지
    버전을 막는 관문이 아니다.
    """
    parsed = _parse_version_tuple(version_str)
    if parsed is None:
        return True
    return parsed == SUPPORTED_VERSION


def check_lancedb_version() -> str | None:
    """설치된 lancedb 버전을 확인한다. 검증 버전과 다르면 사용자에게 보여줄 경고 문구를,
    같거나 버전을 확인할 수 없으면 None을 반환한다.

    lancedb import 자체가 실패하는 경우(정상 설치라면 발생하지 않음)는 이 함수의
    책임 범위가 아니다 — 호출부가 이미 lancedb가 필요한 다른 초기화를 거친 뒤에나
    의미 있는 검사이므로, import 실패는 그쪽에서 먼저 드러난다.
    """
    import lancedb

    version_str = str(getattr(lancedb, "__version__", "unknown"))
    if is_supported_lancedb_version(version_str):
        return None
    return (
        f"설치된 lancedb 버전({version_str})이 검증된 버전"
        f"({'.'.join(map(str, SUPPORTED_VERSION))})과 다릅니다. "
        "폴더 정리(purge) 기능이 예상대로 동작하지 않을 수 있습니다."
    )
