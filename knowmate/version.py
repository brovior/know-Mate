"""Aegis Desk 버전 상수. 릴리스마다 이 값만 갱신한다."""

__version__ = "0.9.0-beta4"


def get_version_label() -> str:
    """앱 버전과 빌드 커밋을 함께 표시한다."""
    from knowmate.build_info import version_label

    return version_label(__version__)
