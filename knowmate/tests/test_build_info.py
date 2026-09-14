"""배포 빌드 출처와 사용자 표시 버전 테스트."""
from __future__ import annotations

import sys
import types

from knowmate import build_info


def test_source_run_without_generated_module_uses_base_version(monkeypatch):
    """생성 모듈이 없는 소스 실행에서는 기존 버전만 표시한다."""
    monkeypatch.delitem(sys.modules, "aegisdesk_build_info", raising=False)
    assert build_info.version_label("1.2.3") == "1.2.3"


def test_bundled_commit_is_included_in_version_label(monkeypatch):
    """배포 빌드는 짧은 커밋으로 서로 다른 exe를 구분한다."""
    generated = types.SimpleNamespace(BUILD_INFO={
        "commit": "a" * 40,
        "short_commit": "a1b2c3d",
        "branch": "main",
        "dirty": False,
    })
    monkeypatch.setitem(sys.modules, "aegisdesk_build_info", generated)
    assert build_info.version_label("1.2.3") == "1.2.3 (a1b2c3d)"


def test_dirty_build_is_visible_in_version_label(monkeypatch):
    """커밋되지 않은 변경이 포함된 빌드는 화면에서도 식별된다."""
    generated = types.SimpleNamespace(BUILD_INFO={
        "commit": "b" * 40,
        "short_commit": "b1c2d3e",
        "branch": "main",
        "dirty": True,
        "source_fingerprint": "12ab34cd",
    })
    monkeypatch.setitem(sys.modules, "aegisdesk_build_info", generated)
    assert build_info.version_label("1.2.3") == "1.2.3 (b1c2d3e-dirty.12ab34cd)"
