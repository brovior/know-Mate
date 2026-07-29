"""임베딩 API 호출 지연이 '어디서' 발생하는지 단계별로 분해하는 진단 스크립트.

배경: 사내 실측에서 청크 수와 무관하게 파일마다 약 20초의 고정 비용이 관측됐다
(3청크 21.93초 / 69청크 23.48초 / 161청크 31.88초 — 배치 하나 처리는 1~3초인데
첫 호출에만 20초대가 붙는다). 이 20초가 **TCP 연결 수립**에서 나가는지 **서버 응답
대기**에서 나가는지에 따라 대응이 완전히 달라진다(전자는 우리 코드, 후자는 인프라).

이 스크립트는 EmbeddingClient를 쓰지 않고 http.client를 직접 다뤄, 한 번의 호출을
네 구간으로 쪼개 잰다:

    1. DNS/주소 해석      resolve
    2. TCP 연결 수립      connect      ← 여기가 20초면 우리 쪽(연결 재시도·경로) 문제
    3. 요청~응답 첫 바이트 ttfb        ← 여기가 20초면 서버 처리 시간
    4. 본문 수신          read

또한 **연결을 재사용할 때 vs 새로 맺을 때**, **유휴 후 재사용할 때**를 나눠 측정해
"COM 추출로 수십 초 노는 동안 연결이 죽어서 매번 새로 맺는가"를 직접 확인한다.

사용법 (사내 PC):
    PYTHONUTF8=1 .venv\\Scripts\\python.exe scripts\\diag_embed_latency.py
    PYTHONUTF8=1 .venv\\Scripts\\python.exe scripts\\diag_embed_latency.py --idle 90

config.yaml(%APPDATA%/AegisDesk/config.yaml)의 embedding 설정을 그대로 읽으므로
앱과 동일한 주소·헤더·키로 호출한다. 문서 내용은 보내지 않고 짧은 더미 문장만
쓴다(원칙7 — 로그·외부 전송에 업무 내용 남기지 않음).
"""
import argparse
import http.client
import json
import os
import socket
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowmate.config import get_config
from knowmate.rag.embedding import EMBEDDING_MODEL, _DUMMY_API_KEY

# 진단용 더미 입력 — 업무 문서 내용을 절대 쓰지 않는다.
_SAMPLE_TEXTS = ["진단용 문장입니다.", "임베딩 지연 측정.", "세 번째 문장."]


def _load_endpoint():
    """config에서 임베딩 접속 정보를 읽어 (scheme, host, port, path_prefix, headers)를 반환한다."""
    cfg = get_config()
    embed_cfg = cfg.get("embedding", {})
    base_url = embed_cfg.get("base_url", "http://localhost")
    host_header = embed_cfg.get("host_header", "embed.internal")
    api_key = embed_cfg.get("api_key", "") or os.environ.get("EMBED_API_KEY", "") or _DUMMY_API_KEY

    parsed = urlparse(base_url.rstrip("/"))
    headers = {
        "Content-Type": "application/json",
        "Host": host_header,
        "Authorization": f"Bearer {api_key}",
    }
    return (
        parsed.scheme or "http",
        parsed.hostname,
        parsed.port,
        parsed.path.rstrip("/"),
        headers,
    )


def _timed_call(scheme, host, port, path_prefix, headers, texts, conn=None, timeout=60.0):
    """한 번의 임베딩 호출을 구간별로 계측한다.

    conn=None이면 새 연결을 맺고(연결 시간 포함), 아니면 주어진 연결을 재사용한다.
    반환: (구간별 초 dict, 사용한 connection, 오류 문자열 또는 None)
    """
    marks = {"resolve": 0.0, "connect": 0.0, "ttfb": 0.0, "read": 0.0, "total": 0.0}
    t_start = time.perf_counter()
    error = None

    try:
        if conn is None:
            # 주소 해석만 따로 잰다 — base_url이 IP면 0에 가깝게 나온다.
            t0 = time.perf_counter()
            try:
                socket.getaddrinfo(host, port or (443 if scheme == "https" else 80))
            except Exception:
                pass
            marks["resolve"] = time.perf_counter() - t0

            conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=timeout)
            t0 = time.perf_counter()
            conn.connect()          # 명시적으로 연결만 수행 — 이 구간이 TCP 수립 시간
            marks["connect"] = time.perf_counter() - t0

        payload = json.dumps({"model": EMBEDDING_MODEL, "input": texts}).encode("utf-8")
        t0 = time.perf_counter()
        conn.request("POST", f"{path_prefix}/v1/embeddings", body=payload, headers=headers)
        resp = conn.getresponse()   # 첫 응답 헤더까지 = 서버 처리 시간
        marks["ttfb"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        body = resp.read()
        marks["read"] = time.perf_counter() - t0

        if resp.status >= 400:
            error = f"HTTP {resp.status}: {body[:120]!r}"
        else:
            parsed = json.loads(body.decode("utf-8"))
            got = len(parsed.get("data", []))
            if got != len(texts):
                error = f"응답 벡터 수 불일치: 요청 {len(texts)} / 응답 {got}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        conn = None

    marks["total"] = time.perf_counter() - t_start
    return marks, conn, error


def _print_row(label, marks, error):
    line = (
        f"{label:<24} total={marks['total']:6.2f}s  "
        f"resolve={marks['resolve']:5.2f}  connect={marks['connect']:5.2f}  "
        f"ttfb={marks['ttfb']:6.2f}  read={marks['read']:5.2f}"
    )
    if error:
        line += f"   [오류] {error}"
    print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="임베딩 API 지연 구간 분해 진단")
    ap.add_argument("--idle", type=float, default=90.0,
                    help="유휴 재사용 테스트에서 대기할 초 (기본 90 — COM 추출 시간 모사)")
    ap.add_argument("--batch", type=int, default=32,
                    help="대량 배치 테스트에 쓸 텍스트 수 (기본 32 = 앱 batch_size)")
    args = ap.parse_args()

    scheme, host, port, path_prefix, headers = _load_endpoint()
    print(f"대상: {scheme}://{host}:{port or '(기본)'}{path_prefix}/v1/embeddings")
    print(f"Host 헤더: {headers['Host']}\n")

    print("구간 의미: connect=TCP 수립(우리 쪽 문제) / ttfb=서버 처리(인프라 문제)\n")

    # 1) 새 연결 — 앱이 파일마다 재연결하고 있다면 이 값이 실측 20초대와 비슷할 것
    marks, conn, err = _timed_call(scheme, host, port, path_prefix, headers, _SAMPLE_TEXTS)
    _print_row("① 새 연결 + 3청크", marks, err)

    # 2) 같은 연결 재사용 — keep-alive가 살아있을 때의 순수 처리 시간
    if conn is not None:
        marks, conn, err = _timed_call(scheme, host, port, path_prefix, headers, _SAMPLE_TEXTS, conn=conn)
        _print_row("② 연결 재사용 + 3청크", marks, err)

    # 3) 큰 배치 — 청크 수가 시간에 얼마나 기여하는지
    if conn is not None:
        big = [f"진단 문장 {i}." for i in range(args.batch)]
        marks, conn, err = _timed_call(scheme, host, port, path_prefix, headers, big, conn=conn)
        _print_row(f"③ 연결 재사용 + {args.batch}청크", marks, err)

    # 4) 유휴 후 재사용 — COM 추출로 수십 초 노는 상황 모사(핵심 가설 검증)
    if conn is not None and args.idle > 0:
        print(f"\n... {args.idle:.0f}초 유휴 대기 (COM 추출 중 연결이 끊기는지 확인) ...\n")
        time.sleep(args.idle)
        marks, conn, err = _timed_call(scheme, host, port, path_prefix, headers, _SAMPLE_TEXTS, conn=conn)
        _print_row(f"④ {args.idle:.0f}초 유휴 후 재사용", marks, err)

        # 5) 유휴 후 새 연결 — ④가 실패했을 때 재연결 비용이 얼마인지
        marks, conn, err = _timed_call(scheme, host, port, path_prefix, headers, _SAMPLE_TEXTS)
        _print_row("⑤ 유휴 후 새 연결", marks, err)

    print("\n판정 가이드:")
    print("  · ①의 connect 가 20초대   → TCP 연결 수립이 원인. 우리 코드에서 고칠 수 있음")
    print("  · ①의 ttfb 가 20초대      → 서버 처리 시간. 인프라팀 확인 필요(코드로 못 고침)")
    print("  · ②가 빠른데 ④가 느림     → 유휴 중 연결이 끊김. 선제 재연결로 해결 가능")
    print("  · ②③ 모두 20초대          → 요청마다 서버가 느린 것")
    return 0


if __name__ == "__main__":
    sys.exit(main())
