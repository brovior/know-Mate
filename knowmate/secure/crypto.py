"""AES-256-GCM 암호화 + Windows DPAPI 키 보호 (CLAUDE.md 5장 4번).

사외 환경(win32crypt 없음) → CryptoUnavailableError.
fake 모드 → FakeCryptoManager (암·복호화 없이 평문 그대로).
"""
import base64
import logging
import os
import secrets
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# DPAPI가 없는 환경임을 나타내는 예외
class CryptoUnavailableError(RuntimeError):
    """win32crypt(DPAPI)를 사용할 수 없는 환경에서 발생한다."""


class FakeCryptoManager:
    """사외 테스트용 — 암·복호화 없이 입력 문자열을 그대로 반환한다."""

    def encrypt(self, plaintext: str) -> str:
        """평문을 그대로 반환한다 (fake 모드용)."""
        return plaintext

    def decrypt(self, ciphertext_b64: str) -> str:
        """입력을 그대로 반환한다 (fake 모드용)."""
        return ciphertext_b64


class CryptoManager:
    """AES-256-GCM 암호화 + Windows DPAPI 키 보호 관리자.

    key_file: DPAPI로 보호된 AES 키가 저장된 파일 경로.
    파일이 없으면 새 키를 생성해 DPAPI로 암호화 저장한다.
    """

    def __init__(self, key_file: Path) -> None:
        """CryptoManager를 초기화한다. win32crypt 없으면 CryptoUnavailableError."""
        self._key: bytes = self._load_or_create_key(key_file)

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: str) -> str:
        """AES-256-GCM으로 암호화하고 base64 문자열을 반환한다.

        포맷: base64(nonce(12B) + ciphertext + tag(16B))
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        aesgcm = AESGCM(self._key)
        ct_with_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        # AESGCM.encrypt()는 ciphertext + tag(16B)를 붙여서 반환
        payload = nonce + ct_with_tag
        return base64.b64encode(payload).decode("ascii")

    def decrypt(self, ciphertext_b64: str) -> str:
        """base64 암호문을 AES-256-GCM으로 복호화해 평문 문자열을 반환한다.

        복호화 평문은 메모리에서만 사용하며 파일·로그에 기록하지 않는다.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        payload = base64.b64decode(ciphertext_b64.encode("ascii"))
        nonce = payload[:12]
        ct_with_tag = payload[12:]
        aesgcm = AESGCM(self._key)
        plaintext_bytes = aesgcm.decrypt(nonce, ct_with_tag, None)
        return plaintext_bytes.decode("utf-8")

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _load_or_create_key(self, key_file: Path) -> bytes:
        """키 파일에서 DPAPI 복호화로 AES 키를 로드한다. 없으면 생성 후 저장한다."""
        try:
            import win32crypt  # type: ignore
        except ImportError as exc:
            raise CryptoUnavailableError(
                "win32crypt를 import할 수 없습니다. "
                "Windows 환경에서 pywin32를 설치하거나 fake 모드를 사용하세요."
            ) from exc

        if key_file.exists():
            encrypted = key_file.read_bytes()
            _, aes_key = win32crypt.CryptUnprotectData(encrypted)
            logger.info("DPAPI 키 로드 완료: %s", key_file)
            return aes_key
        else:
            aes_key = secrets.token_bytes(32)  # 256-bit
            encrypted = win32crypt.CryptProtectData(aes_key)
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(encrypted)
            logger.info("DPAPI 키 생성·저장 완료: %s", key_file)
            return aes_key


# ------------------------------------------------------------------
# 팩토리
# ------------------------------------------------------------------

def get_crypto_manager(cfg: dict) -> Union[CryptoManager, FakeCryptoManager]:
    """config dict를 받아 적합한 CryptoManager를 반환한다.

    extractor=fake이면 FakeCryptoManager, 그 외 CryptoManager.
    """
    if cfg.get("extractor", "fake") == "fake":
        return FakeCryptoManager()

    key_file = Path(os.environ.get("APPDATA", ".")) / "KnowMate" / "km.key"
    return CryptoManager(key_file)
