import os
import re
from collections import Counter
# pyrefly: ignore [missing-import]
from flask import Flask, render_template, request

app = Flask(__name__)
app.secret_key = "pingpong_rank_secret_key_for_flash"

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
    if not name:
        return ""
    # 공백 제거 및 문자열화
    return str(name).strip().replace(" ", "")

def normalize_division(div):
    if not div:
        return ""
    div_str = str(div).strip().replace(" ", "")
    # 숫자만 입력된 경우 (예: '1', '2') -> '1부', '2부'로 변경
    if div_str.isdigit():
        return f"{div_str}부"
    # 만약 '1부'와 같은 패턴이면 그대로 반환
    match = re.match(r'^(\d+)부$', div_str)
    if match:
        return div_str
    # 그 외 포맷은 일단 그대로 반환하되, 끝에 '부'를 붙이거나 적절히 보정
    return div_str

def normalize_racket(racket):
    if not racket:
        return ""
    racket_str = str(racket).strip().lower()
    if '세이크' in racket_str or '쉐이크' in racket_str or 'shake' in racket_str:
        return '쉐이크'
    if '펜홀더' in racket_str or '펜' in racket_str or 'pen' in racket_str:
        return '펜홀더'
    return racket_str.capitalize()  # 원래 값 유지하되 첫글자 대문자화

def get_sheet_data():
    # 1. 환경변수 확인 또는 로컬 credentials.json 확인
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        if os.path.exists("credentials.json"):
            cred_path = "credentials.json"
            
    if not cred_path or not os.path.exists(cred_path):
        # 환경 변수 및 파일이 없으면 더미 데이터 반환
        return DUMMY_DATA, True
        
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        
        # 구글 API 인증 및 시트 오픈
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(cred_path, scope)
        client = gspread.authorize(creds)
        
        # '탁우회_명단' 스프레드시트 열기
        spreadsheet = client.open("탁우회_명단")
        sheet = spreadsheet.sheet1
        
        # 전체 데이터 가져오기
        records = sheet.get_all_records()
        
        # 데이터 정규화 및 파싱
        cleaned_data = []
        for row in records:
            name_val = row.get("이름") or row.get("name") or ""
            div_val = row.get("부수") or row.get("division") or ""
            racket_val = row.get("라켓") or row.get("racket") or ""
            
            # 데이터가 모두 빈 행은 건너뜀
            if not str(name_val).strip() and not str(div_val).strip() and not str(racket_val).strip():
                continue
                
            cleaned_data.append({
                "이름": normalize_name(name_val),
                "부수": normalize_division(div_val),
                "라켓": normalize_racket(racket_val)
            })
        return cleaned_data, False
    except Exception as e:
        print(f"[ERROR] 구글 시트 연동 오류 발생: {e}. 더미 데이터를 사용합니다.")
        return DUMMY_DATA, True

@app.route("/", methods=["GET"])
def index():
    search_filter = request.args.get("filter", "이름")  # 기본값: 이름
    search_query = request.args.get("query", "").strip()
    
    # 데이터 로드
    members, is_dummy = get_sheet_data()
    
    # 검색 적용 (부분 매칭)
    filtered_members = []
    is_initial = True
    
    if search_query:
        is_initial = False
        for m in members:
            val_to_compare = m.get(search_filter, "")
            # 대소문자 무관 및 부분 일치 비교
            if search_query.lower() in str(val_to_compare).lower():
                filtered_members.append(m)
    else:
        filtered_members = []  # 첫 진입 시 빈 목록 반환 (TMI 방지)
        
    return render_template(
        "index.html",
        members=filtered_members,
        filter=search_filter,
        query=search_query,
        is_dummy=is_dummy,
        is_initial=is_initial
    )

@app.route("/stats", methods=["GET"])
def stats():
    # 데이터 로드
    members, is_dummy = get_sheet_data()
    
    # 1. 부수별 인원 분포 계산
    divisions = [m["부수"] for m in members if m["부수"]]
    total_count = len(divisions)
    
    # 부수별 집계
    div_counts = Counter(divisions)
    
    # 부수 정렬 기준 (예: 1부, 2부, ... 순)
    def get_div_num(div_name):
        match = re.search(r'\d+', div_name)
        return int(match.group()) if match else 999
        
    sorted_divs = sorted(div_counts.keys(), key=get_div_num)
    
    # 차트용 데이터 가공 (부수명, 인원수, 비율)
    div_stats = []
    for div in sorted_divs:
        count = div_counts[div]
        ratio = round((count / total_count) * 100, 1) if total_count > 0 else 0
        div_stats.append({
            "division": div,
            "count": count,
            "ratio": ratio
        })
        
    # 2. 부수별 그룹화 목록
    grouped_members = {}
    for div in sorted_divs:
        members_in_div = [m for m in members if m["부수"] == div]
        members_in_div = sorted(members_in_div, key=lambda x: x["이름"])
        grouped_members[div] = members_in_div
        
    # 3. 추가 통계: 전형별(라켓) 인원 분포
    rackets = [m["라켓"] for m in members if m["라켓"]]
    racket_counts = Counter(rackets)
    racket_stats = []
    total_rackets = len(rackets)
    for racket, count in racket_counts.items():
        ratio = round((count / total_rackets) * 100, 1) if total_rackets > 0 else 0
        racket_stats.append({
            "racket": racket,
            "count": count,
            "ratio": ratio
        })
        
    return render_template(
        "stats.html",
        div_stats=div_stats,
        grouped_members=grouped_members,
        racket_stats=racket_stats,
        total_count=total_count,
        is_dummy=is_dummy
    )

@app.route("/proposal", methods=["GET"])
def proposal():
    return render_template("proposal.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
