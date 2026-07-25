"""Office Resiliency 마커(안전 모드 유발 표식) 정리 — 강제 종료 ↔ 세이프모드 무한 루프 차단.

**문제**: COM으로 띄운 Office가 모달 프롬프트에 걸려 멈추면 워치독이 강제 종료한다
(`office_guard.terminate_stuck_office`). 그런데 Office는 비정상 종료를 겪으면
`HKCU\\Software\\Microsoft\\Office\\<ver>\\<App>\\Resiliency` 아래에 표식을 남기고,
다음 기동 때 그 표식을 보고 **"안전 모드로 시작할까요?" 프롬프트**를 띄운다. 이
프롬프트는 `Dispatch()`가 반환하기도 전에 뜨므로 `DisplayAlerts=False` 같은 앱 수준
설정으로는 억제할 수 없다 → 또 멈추고, 또 강제 종료되고, 표식이 다시 쌓이는 무한 루프.

**해결**: Office를 띄우기 직전과 워치독이 강제 종료한 직후에 이 표식을 지운다.

**지우는 것 / 지우지 않는 것 (중요)**:
  - `DisabledItems`, `StartupItems` → **지운다**. 세이프모드 프롬프트를 유발하는 주범이고,
    자동화가 만든 부산물이다.
  - `DocumentRecovery` → **절대 지우지 않는다**. 이건 *사용자 본인이* 저장하지 못하고
    잃은 문서의 복구 목록이다. 인덱서가 청소한다고 이걸 지우면 사용자가 다음에 Office를
    열 때 자기 작업을 복구받지 못한다(실제 업무 데이터 손실). 게다가 Document Recovery
    창은 비모달 작업창이라 COM 자동화를 막지도 않으므로 지울 이유 자체가 없다.

Windows 전용. 비Windows(사외 테스트)에서는 조용히 아무것도 하지 않는다.
CLAUDE.md 원칙3(보안·Office 의존 코드는 secure/ 안에 격리) 준수 — 레지스트리 접근은
이 모듈 밖으로 나가지 않는다.
"""
import logging
import sys

logger = logging.getLogger(__name__)

# Office 실행 파일 → Resiliency 레지스트리 키의 앱 이름
_EXE_TO_APP_KEY = {
    "WINWORD.EXE": "Word",
    "EXCEL.EXE": "Excel",
    "POWERPNT.EXE": "PowerPoint",
}

# 탐색할 Office 버전 키 (16.0=2016/2019/2021/365, 15.0=2013, 14.0=2010).
# 설치되지 않은 버전은 키가 없어 조용히 건너뛴다.
_OFFICE_VERSIONS = ("16.0", "15.0", "14.0")

# 세이프모드 프롬프트를 유발하는 하위 키만 대상으로 한다.
# DocumentRecovery는 사용자 데이터라 의도적으로 제외한다(모듈 docstring 참조).
_TARGET_SUBKEYS = ("DisabledItems", "StartupItems")


def _delete_key_tree(winreg, root, path: str) -> bool:
    """레지스트리 키와 그 하위 트리를 삭제한다. 삭제했으면 True, 없으면 False.

    `DisabledItems`는 보통 값만 갖지만, 버전에 따라 하위 키가 있을 수 있어
    재귀적으로 지운다(`DeleteKey`는 하위 키가 있으면 실패한다).
    """
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return False

    try:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break  # 더 이상 하위 키 없음
            _delete_key_tree(winreg, root, f"{path}\\{child}")
    finally:
        key.Close()

    winreg.DeleteKey(root, path)
    return True


def clear_resiliency_markers(exe_name: str) -> int:
    """해당 Office 앱의 세이프모드 유발 표식을 지운다. 삭제한 키 수를 반환한다.

    exe_name: "EXCEL.EXE" 같은 실행 파일명(대소문자 무관). 알 수 없는 이름이면 0.

    실패(권한 없음·키 부재·비Windows)는 모두 조용히 무시한다 — 이 정리는
    best-effort 예방책이라, 실패했다고 인덱싱을 막아서는 안 된다. 최악의 경우
    기존과 동일하게 세이프모드 프롬프트 → 워치독 강제 종료로 이어질 뿐이다.
    """
    if sys.platform != "win32":
        return 0

    app_key = _EXE_TO_APP_KEY.get(exe_name.upper())
    if app_key is None:
        return 0

    try:
        import winreg  # type: ignore
    except ImportError:
        return 0

    deleted = 0
    for version in _OFFICE_VERSIONS:
        for subkey in _TARGET_SUBKEYS:
            path = f"Software\\Microsoft\\Office\\{version}\\{app_key}\\Resiliency\\{subkey}"
            try:
                if _delete_key_tree(winreg, winreg.HKEY_CURRENT_USER, path):
                    deleted += 1
            except OSError as exc:
                # 권한 부족(정책으로 HKCU 쓰기 제한 등)·경합 삭제 → 무시하고 계속
                logger.debug("[resiliency] 키 삭제 실패(무시): %s (%s)", path, exc)

    if deleted:
        logger.info("[resiliency] %s 세이프모드 표식 %d건 정리", app_key, deleted)
    return deleted
