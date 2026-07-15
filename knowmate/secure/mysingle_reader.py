"""Knox .mysingle 파일 파서 (RFC822 + MIME multipart)."""
from __future__ import annotations

import email
import email.policy
import html.parser
import json
import logging
from email.header import decode_header
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 내부 유틸
# ---------------------------------------------------------------------------

def _decode_header_str(raw: str | None) -> str:
    """=?UTF-8?B?...?= 인코딩된 헤더값을 유니코드 문자열로 변환한다."""
    if not raw:
        return ""
    parts: list[str] = []
    for fragment, enc in decode_header(raw):
        if isinstance(fragment, bytes):
            # enc가 없거나 인식 불가('unknown-8bit' 등, MIME 인코딩 안 된 raw UTF-8)면 utf-8 fallback
            charset = enc or "utf-8"
            try:
                parts.append(fragment.decode(charset, errors="replace"))
            except LookupError:
                parts.append(fragment.decode("utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts)


class _HTMLStripper(html.parser.HTMLParser):
    """HTML 태그를 제거하고 텍스트만 수집하는 파서."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._parts)


def html_to_text(html_str: str) -> str:
    """HTML 마크업을 평문 텍스트로 변환한다. 표준 html.parser 사용 (외부 의존성 없음)."""
    stripper = _HTMLStripper()
    try:
        stripper.feed(html_str)
        return stripper.get_text()
    except Exception as exc:
        logger.warning("HTML 파싱 실패, 원본 반환: %s", exc)
        return html_str


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def parse_mysingle(path: str) -> dict:
    """`.mysingle` 파일을 파싱한다(parse_mail_file 래퍼, 하위 호환용)."""
    return parse_mail_file(path)


def parse_mail_file(path: str) -> dict:
    """
    RFC822 메일 파일(.mysingle / .eml)을 파싱해 메타데이터 + 본문 텍스트 dict를 반환한다.

    확장자로 source_type을 판별한다: .mysingle → 'knox', 그 외(.eml 등) → 'eml'.

    반환 키:
        mail_uid      - '{source_type}:{고유ID}' (knox: unique_id/Message-ID, eml: Message-ID)
        source_type   - 'knox' | 'eml'
        message_id    - RFC Message-ID 원문
        subject       - 제목 (헤더 디코딩 완료)
        sender        - 발신자
        recipients    - 수신자 (쉼표 구분 문자열)
        mail_date     - 날짜 문자열
        thread_ref    - References 헤더 (스레드 연결용)
        body_text     - HTML→평문 변환된 본문
        source_file   - 원본 절대 경로 문자열
        source_meta   - JSON 문자열 (Knox 고유 헤더 봉투)

    본문을 추출할 수 없으면 ValueError를 발생시킨다.
    """
    path_str = str(Path(path).resolve())
    source_type = "knox" if Path(path).suffix.lower() == ".mysingle" else "eml"

    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.compat32)

    # --- 헤더 디코딩 ---
    subject = _decode_header_str(msg.get("Subject"))
    sender = _decode_header_str(msg.get("From", ""))
    recipients = _decode_header_str(msg.get("To", ""))
    mail_date = msg.get("Date", "")
    message_id = msg.get("Message-ID", "").strip()
    thread_ref = msg.get("References", "").strip()

    # Knox 고유 헤더 (.eml에는 없음)
    unique_id = msg.get("X-Desktop-Msg-UniqueID", "").strip()
    cms_root = msg.get("X-CMS-RootMailID", "").strip()

    # mail_uid: Knox는 고유ID 우선, eml은 Message-ID → 없으면 경로
    if source_type == "knox":
        uid_src = unique_id or message_id or path_str
    else:
        uid_src = message_id or path_str
    mail_uid = f"{source_type}:{uid_src}"

    source_meta = json.dumps(
        {"x_desktop_msg_unique_id": unique_id, "x_cms_rootmailid": cms_root},
        ensure_ascii=False,
    )

    # --- 본문 추출 (text/html 우선, text/plain fallback) ---
    body_text = ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                html_str = payload.decode(charset, errors="replace")
                body_text = html_to_text(html_str)
                break
        if ct == "text/plain" and not body_text:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                body_text = payload.decode(charset, errors="replace").strip()

    if not body_text:
        raise ValueError(f"본문이 없는 메일 파일: {path_str}")

    logger.debug(
        "[mail] 파싱 완료 type=%s uid=%s subject=%s body_len=%d",
        source_type, mail_uid, subject[:30] if subject else "", len(body_text),
    )

    return {
        "mail_uid": mail_uid,
        "source_type": source_type,
        "message_id": message_id,
        "subject": subject,
        "sender": sender,
        "recipients": recipients,
        "mail_date": mail_date,
        "thread_ref": thread_ref,
        "body_text": body_text,
        "source_file": path_str,
        "source_meta": source_meta,
    }
