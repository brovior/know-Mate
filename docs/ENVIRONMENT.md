# ENVIRONMENT.md — 개발·배포 환경

> CLAUDE.md에서 분리. 환경 전제가 바뀌면 이 파일을 갱신한다.

- **OS**: Windows 10/11 · **Python**: 3.11
- **임베딩 운영 모드**: 반드시 `mode: api` (config 기본값도 api). `local` 모드는 폐쇄망에서 모델 다운로드 불가로 무한 대기 발생.
- **패키지**: PyQt6, PyQt6-WebEngine, lancedb, pyarrow, pywin32, cryptography, PyMuPDF, python-docx, openpyxl, xlrd, python-pptx, pytest, pyinstaller(빌드).
  **버전 고정 2개**: `lancedb==0.34.0`(purge projection 실측), `pyinstaller==6.21.0`(번들 구성이 버전마다 달라짐 — 특히 QtWebEngine 리소스 누락은 증상이 '흰 화면'). 둘 다 범위가 아니라 정확히 고정하며, 올릴 때는 실측·클린빌드 검증 후 갱신한다.
- **배포**: `build.bat`(클린, 배포용) / `build.bat fast`(캐시 재사용, 배포 금지) → `dist/AegisDesk/`(exe + `_internal` 폴더)를 통째로 zip 배포. exe만 단독 배포 불가.
  빌드 직후 `AegisDesk.exe --selftest`가 자동 실행돼 번들 누락(리소스·WebEngine 프로세스·지연 import·lancedb 버전·로그 폴더)을 검사하고, 실패 시 배포를 중단한다 — 다만 '파일 존재'만 보므로 흰 화면 여부는 사람이 한 번 띄워 확인해야 한다.
- **네트워크 드라이브**: 일반 SMB(K: 등)는 정상. EFSS2 DRM 드라이브(M: 등)는 화이트리스트 프로세스만 접근 가능해 인덱싱 불가. 나스카 DRM 문서는 SSO 로그인 유지 중에만 복호화.
