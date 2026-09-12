import gzip
import json
import os
import re
import threading
import time
from collections import Counter
# pyrefly: ignore [missing-import]
from flask import Flask, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

# 정적 파일은 Vercel이 CDN에서 직접 서빙하도록 public/ 아래에 둔다 (주소는 /static/... 그대로)
app = Flask(__name__, static_folder="public/static", static_url_path="/static")
app.secret_key = "pingpong_rank_secret_key_for_flash"
# Render 프록시 뒤에서도 https 절대 URL(링크 미리보기용 og:image 등)을 만들 수 있도록 설정
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

SHEET_NAME = "탁우회_명단"
# 구글 시트 캐시 유지 시간(초). 요청마다 API를 호출하면 느리고 분당 호출 한도도 금방 소진된다.
SHEET_CACHE_TTL = int(os.environ.get("SHEET_CACHE_TTL", "60"))
SHEET_RETRY_AFTER = 10  # 시트 연동 실패 후 재시도까지 대기(초)
SHEET_TIMEOUT = 10  # 구글 API 응답 대기 한도(초)

SEARCH_FIELDS = ("이름", "부수", "라켓")
RACKET_ORDER = ("쉐이크", "펜홀더")
UNASSIGNED_DIVISION = "미지정"

# 더미 데이터 (구글 시트 로드 실패 시 대체)
DUMMY_DATA = [
    {"이름": "홍길동", "부수": "3부", "라켓": "쉐이크"},
    {"이름": "김철수", "부수": "1부", "라켓": "펜홀더"},
    {"이름": "이영희", "부수": "2부", "라켓": "쉐이크"},
    {"이름": "박민수", "부수": "4부", "라켓": "쉐이크"},
    {"이름": "최성우", "부수": "3부", "라켓": "펜홀더"},
    {"이름": "한경민", "부수": "1부", "라켓": "쉐이크"},
    {"이름": "정아름", "부수": "5부", "라켓": "쉐이크"},
    {"이름": "강태풍", "부수": "2부", "라켓": "펜홀더"},
    {"이름": "유재석", "부수": "3부", "라켓": "쉐이크"},
    {"이름": "하동훈", "부수": "4부", "라켓": "펜홀더"},
]

def normalize_name(name):
    if name is None:
        return ""
    # 모든 공백 제거 및 문자열화
    return "".join(str(name).split())

def normalize_division(div):
    if div is None:
        return ""
    div_str = str(div).strip().replace(" ", "")
    # 정수만 입력했거나 'X부' 형태인 경우 (예: '-1', '0', '3', '03부') -> 'X부'로 통일
    match = re.fullmatch(r'(-?\d+)부?', div_str)
    if match:
        return f"{int(match.group(1))}부"
    # 그 외 포맷(예: '선수부')은 그대로 반환
    return div_str

def normalize_racket(racket):
    if racket is None:
        return ""
    racket_str = str(racket).strip().lower()
    compact = racket_str.replace(" ", "")
    if any(k in compact for k in ('쉐이크', '세이크', '셰이크', 'shake')):
        return '쉐이크'
    if any(k in compact for k in ('펜', '팬', 'pen')):
        return '펜홀더'
    return racket_str.capitalize()  # 원래 값 유지하되 첫글자 대문자화

def division_number(div):
    # '3부' -> 3, '-1부' -> -1, 숫자가 없으면 None
    match = re.search(r'-?\d+', div)
    return int(match.group()) if match else None

def division_sort_key(div):
    # -1부, 0부, 1부 ... 순서, 숫자가 없는 부수(선수부 등)는 그 뒤, 미입력은 맨 뒤
    num = division_number(div)
    if num is not None:
        return (0, num, div)
    return (1 if div else 2, 0, div)

def racket_sort_key(racket):
    # 쉐이크, 펜홀더, 그 외 전형 순서
    return (RACKET_ORDER.index(racket) if racket in RACKET_ORDER else len(RACKET_ORDER), racket)

def member_sort_key(member):
    return (division_sort_key(member["부수"]), member["이름"])

# ---------------------------------------------------------------------------
# 검색 규칙 (templates/index.html의 즉시 검색 스크립트도 같은 규칙을 사용)
# ---------------------------------------------------------------------------

# 한글 자모 분해 테이블 (초성 19 · 중성 21 · 종성 28).
# 겹모음/겹받침은 낱자로 풀어서 '고'가 '과'의, '달'이 '닭'의 앞부분이 되도록 한다.
CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNGSUNG = ("ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅗㅏ", "ㅗㅐ", "ㅗㅣ", "ㅛ", "ㅜ",
            "ㅜㅓ", "ㅜㅔ", "ㅜㅣ", "ㅠ", "ㅡ", "ㅡㅣ", "ㅣ")
JONGSUNG = ("", "ㄱ", "ㄲ", "ㄱㅅ", "ㄴ", "ㄴㅈ", "ㄴㅎ", "ㄷ", "ㄹ", "ㄹㄱ", "ㄹㅁ", "ㄹㅂ", "ㄹㅅ", "ㄹㅌ",
            "ㄹㅍ", "ㄹㅎ", "ㅁ", "ㅂ", "ㅂㅅ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ")
COMPOUND_JAMO = {"ㄳ": "ㄱㅅ", "ㄵ": "ㄴㅈ", "ㄶ": "ㄴㅎ", "ㄺ": "ㄹㄱ", "ㄻ": "ㄹㅁ", "ㄼ": "ㄹㅂ", "ㄽ": "ㄹㅅ",
                 "ㄾ": "ㄹㅌ", "ㄿ": "ㄹㅍ", "ㅀ": "ㄹㅎ", "ㅄ": "ㅂㅅ", "ㅘ": "ㅗㅏ", "ㅙ": "ㅗㅐ", "ㅚ": "ㅗㅣ",
                 "ㅝ": "ㅜㅓ", "ㅞ": "ㅜㅔ", "ㅟ": "ㅜㅣ", "ㅢ": "ㅡㅣ"}

def _syllable_index(ch):
    code = ord(ch) - 0xAC00
    return code if 0 <= code < 11172 else -1

def to_jamo(text):
    # '홍길동' -> 'ㅎㅗㅇㄱㅣㄹㄷㅗㅇ'
    parts = []
    for ch in text:
        code = _syllable_index(ch)
        if code < 0:
            parts.append(COMPOUND_JAMO.get(ch, ch))
        else:
            parts.append(CHOSUNG[code // 588] + JUNGSUNG[code % 588 // 28] + JONGSUNG[code % 28])
    return "".join(parts).lower()

def to_chosung(text):
    # '홍길동' -> 'ㅎㄱㄷ'
    parts = []
    for ch in text:
        code = _syllable_index(ch)
        parts.append(ch if code < 0 else CHOSUNG[code // 588])
    return "".join(parts)

def match_name(name, query):
    # 이름 부분 일치. 초성만 입력하면 초성으로 비교하고('ㅎㄱㄷ' -> 홍길동),
    # 마지막 글자는 자모 단위로 비교해 입력 중인 글자도 찾아준다('호', '홍ㄱ' -> 홍길동).
    query = normalize_name(query).lower()
    if not query:
        return False
    name = name.lower()
    query_jamo = to_jamo(query)
    if all(ch in CHOSUNG for ch in query_jamo):
        return query_jamo in to_chosung(name)
    head, last = query[:-1], to_jamo(query[-1])
    return any(
        name.startswith(head, i) and to_jamo(name[i + len(head):]).startswith(last)
        for i in range(len(name) - len(head) + 1)
    )

def match_division(division, query):
    # 숫자 부수는 정확히 일치 ('1', '1부' -> 1부만, -1부나 11부는 제외). 그 외는 부분 일치
    query = normalize_division(query)
    if re.fullmatch(r'-?\d+부', query):
        return division == query
    return bool(query) and query in division

def match_racket(racket, query):
    # 오타/동의어 보정 후 부분 일치 ('세이크', 'shake' -> 쉐이크, '펜' -> 펜홀더)
    query = normalize_racket(query).lower()
    return bool(query) and query in racket.lower()

MATCHERS = {"이름": match_name, "부수": match_division, "라켓": match_racket}
NORMALIZERS = {"이름": normalize_name, "부수": normalize_division, "라켓": normalize_racket}

# ---------------------------------------------------------------------------
# 구글 시트 연동
# ---------------------------------------------------------------------------

def get_credentials_path():
    # 환경변수 GOOGLE_APPLICATION_CREDENTIALS 또는 로컬 credentials.json (없으면 None)
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "credentials.json"
    return cred_path if os.path.exists(cred_path) else None

def has_credentials():
    # 파일을 둘 수 없는 서버리스 환경(Vercel)에서는 환경변수 GOOGLE_CREDENTIALS_JSON에 키 JSON 본문을 넣는다
    return bool(os.environ.get("GOOGLE_CREDENTIALS_JSON")) or get_credentials_path() is not None

def create_sheet_client():
    import gspread

    # 서비스 계정 키(JSON)로 인증 (Sheets + Drive 권한)
    key_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if key_json:
        client = gspread.service_account_from_dict(json.loads(key_json))
    else:
        client = gspread.service_account(filename=get_credentials_path())
    client.set_timeout(SHEET_TIMEOUT)
    return client

def find_column(headers, *keywords):
    # 키워드와 같은 헤더를 우선 찾고, 없으면 키워드를 포함하는 헤더를 찾는다 (예: '이름', 'Name')
    lowered = [str(h).strip().lower() for h in headers]
    for i, header in enumerate(lowered):
        if header in keywords:
            return i
    for i, header in enumerate(lowered):
        if any(k in header for k in keywords):
            return i
    return None

def parse_sheet_rows(rows):
    # 시트 값(첫 행은 헤더)을 정규화된 부원 목록으로 변환
    if not rows:
        return []
    headers = rows[0]
    columns = {
        "이름": find_column(headers, "이름", "name"),
        "부수": find_column(headers, "부수", "division"),
        "라켓": find_column(headers, "라켓", "racket"),
    }
    if columns["이름"] is None:
        raise ValueError(f"시트에 '이름' 열이 없습니다. (감지된 헤더: {headers})")

    members = []
    for row in rows[1:]:
        values = {key: row[i] if i is not None and i < len(row) else "" for key, i in columns.items()}
        # 데이터가 모두 빈 행은 건너뜀
        if not any(str(v).strip() for v in values.values()):
            continue
        members.append({
            "이름": normalize_name(values["이름"]),
            "부수": normalize_division(values["부수"]),
            "라켓": normalize_racket(values["라켓"]),
        })
    return members

_sheet_lock = threading.Lock()
_sheet_client = None
_sheet_cache = {"members": None, "expires_at": 0.0}

def fetch_sheet_members():
    global _sheet_client
    if _sheet_client is None:
        _sheet_client = create_sheet_client()
    # get_all_records()는 '0'을 숫자 0으로 바꾸고 헤더가 중복되면 실패하므로 원본 문자열로 받는다
    rows = _sheet_client.open(SHEET_NAME).sheet1.get_all_values()
    return parse_sheet_rows(rows)

def get_sheet_data():
    # (부원 목록, 더미 데이터 여부)를 반환한다.
    # 시트는 SHEET_CACHE_TTL초 동안 캐시하고, 연동 오류가 나면 마지막으로 불러온 데이터를 유지한다.
    if not has_credentials():
        # 환경 변수 및 파일이 없으면 더미 데이터 반환
        return DUMMY_DATA, True

    with _sheet_lock:
        now = time.monotonic()
        if now >= _sheet_cache["expires_at"]:
            try:
                _sheet_cache["members"] = fetch_sheet_members()
                _sheet_cache["expires_at"] = now + SHEET_CACHE_TTL
            except Exception as e:
                app.logger.error("구글 시트 연동 오류 발생: %s", e)
                _sheet_cache["expires_at"] = now + SHEET_RETRY_AFTER
        members = _sheet_cache["members"]

    if members is None:
        # 한 번도 불러오지 못했다면 더미 데이터 사용
        return DUMMY_DATA, True
    return members, False

@app.after_request
def compress_html(response):
    # 검색 페이지는 전체 부원 카드를 담고 있어 HTML이 크므로 gzip으로 압축 (약 1/10 크기)
    if (
        response.mimetype == "text/html"
        and response.status_code == 200
        and not response.direct_passthrough
        and "Content-Encoding" not in response.headers
        and "gzip" in request.headers.get("Accept-Encoding", "")
    ):
        response.set_data(gzip.compress(response.get_data(), compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
    return response

@app.route("/", methods=["GET"])
def index():
    search_filter = request.args.get("filter", "이름")  # 기본값: 이름
    if search_filter not in SEARCH_FIELDS:
        search_filter = "이름"
    search_query = request.args.get("query", "").strip()

    # 데이터 로드 (부수 오름차순 -> 이름순)
    members, is_dummy = get_sheet_data()
    members = sorted(members, key=member_sort_key)

    # 카드는 전부 렌더링하되 검색어와 일치하는 부원만 노출한다.
    # 첫 진입 시에는 아무도 노출하지 않음 (TMI 방지). 브라우저에서는 입력 즉시 JS가 다시 필터링한다.
    matcher = MATCHERS[search_filter]
    cards = [(m, bool(search_query) and matcher(m[search_filter], search_query)) for m in members]

    return render_template(
        "index.html",
        cards=cards,
        match_count=sum(1 for _, matched in cards if matched),
        total_count=len(members),
        divisions=sorted({m["부수"] for m in members if m["부수"]}, key=division_sort_key),
        rackets=sorted({m["라켓"] for m in members if m["라켓"]}, key=racket_sort_key),
        filter=search_filter,
        query=search_query,
        active_value=NORMALIZERS[search_filter](search_query) if search_query else "",
        is_dummy=is_dummy,
        is_initial=not search_query
    )

@app.route("/stats", methods=["GET"])
def stats():
    # 데이터 로드
    members, is_dummy = get_sheet_data()
    total_count = len(members)

    # 1. 부수별 그룹화 (부수 오름차순 및 이름순, 부수 미입력 부원은 '미지정' 그룹으로 맨 뒤)
    grouped = {}
    for m in sorted(members, key=member_sort_key):
        grouped.setdefault(m["부수"] or UNASSIGNED_DIVISION, []).append(m)

    # 2. 부수별 인원 분포 + 부수별 라켓 구성 (막대 길이는 인원이 가장 많은 부수 기준)
    max_count = max((len(ms) for ms in grouped.values()), default=0)
    groups = []
    for i, (division, div_members) in enumerate(grouped.items(), start=1):
        count = len(div_members)
        racket_counts = Counter(m["라켓"] if m["라켓"] in RACKET_ORDER else "기타" for m in div_members)
        groups.append({
            "division": division,
            "anchor": f"group-{i}",
            "members": div_members,
            "count": count,
            "ratio": round((count / total_count) * 100, 1),
            "width": round((count / max_count) * 100, 1),
            "rackets": [(r, racket_counts[r]) for r in (*RACKET_ORDER, "기타") if racket_counts[r]],
        })
    present = {name for g in groups for name, _ in g["rackets"]}
    legend = [r for r in (*RACKET_ORDER, "기타") if r in present]

    # 3. 추가 통계: 전형별(라켓) 인원 분포 및 평균 부수
    racket_members = {}
    for m in members:
        if m["라켓"]:
            racket_members.setdefault(m["라켓"], []).append(m)
    total_rackets = sum(len(ms) for ms in racket_members.values())
    racket_stats = []
    for racket in sorted(racket_members, key=racket_sort_key):
        ms = racket_members[racket]
        nums = [n for n in (division_number(m["부수"]) for m in ms) if n is not None]
        racket_stats.append({
            "racket": racket,
            "count": len(ms),
            "ratio": round((len(ms) / total_rackets) * 100, 1),
            "avg_division": round(sum(nums) / len(nums), 1) if nums else None,
        })

    return render_template(
        "stats.html",
        groups=groups,
        legend=legend,
        racket_stats=racket_stats,
        total_count=total_count,
        is_dummy=is_dummy
    )

@app.route("/proposal", methods=["GET"])
def proposal():
    return render_template("proposal.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
