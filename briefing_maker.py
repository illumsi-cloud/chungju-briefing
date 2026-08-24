import urllib.request
import urllib.parse
import json
import ssl
import re
import os
import socket
import http.cookiejar
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import time

# 관공서 사이트가 연결만 받아두고 응답하지 않으면 urlopen이 무한 대기하면서
# 수집 전체가 멈춘다(실제로 GitHub Actions 실행이 40분 넘게 걸린 적이 있다).
# 소켓 기본 타임아웃을 걸어두면 응답 없는 사이트는 예외로 떨어지고,
# 각 수집 함수의 try/except가 이를 잡아 나머지 수집은 계속된다.
SOCKET_TIMEOUT_SEC = 20
socket.setdefaulttimeout(SOCKET_TIMEOUT_SEC)

# 1. 네이버 API 키
# 로컬 실행 시에는 아래 기본값을 쓰고, GitHub Actions에서는 저장소 Secrets(NAVER_CLIENT_ID/SECRET)로
# 덮어써서 키가 코드/공개 저장소에 그대로 노출되지 않도록 한다.
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "5cMwFIqXQNuG9rj4Ckeb")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "W6_J7EAKCx")

# 공공기관 방화벽 환경 대비 SSL 우회 설정
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').strip()

def parse_naver_date(pub_date_str):
    """네이버 뉴스 API의 pubDate(RFC822)를 YYYY-MM-DD로 변환. 카페글 API는 날짜를 주지 않으므로 빈 문자열 반환."""
    if not pub_date_str:
        return ""
    try:
        return parsedate_to_datetime(pub_date_str).strftime("%Y-%m-%d")
    except Exception:
        return ""

NEWS_RECENCY_DAYS = 365

def is_recent_or_undated(date_str):
    """오래된(1년 초과) 뉴스를 걸러내기 위한 판정. 날짜가 없으면(카페글) 통과시킨다."""
    if not date_str:
        return True
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (datetime.now() - d).days <= NEWS_RECENCY_DAYS
    except Exception:
        return True

# 공공 게시판(공고/고시/입법예고 등)에서 개발·부동산·학교·교육 관련 항목만 남기기 위한 키워드
TOPIC_KEYWORDS = [
    "개발", "부동산", "아파트", "주택", "택지", "지구단위", "도시계획", "정비구역",
    "재개발", "재건축", "산업단지", "산단", "분양", "공동주택", "임대주택",
    "학교", "교육", "학생", "통학", "학군", "신설", "폐교", "증축", "개교",
    "유치원", "어린이집", "학급", "교실", "이통반", "이·통·반"
]

def matches_topic(text):
    return any(kw in text for kw in TOPIC_KEYWORDS)

# 충주시청 게시판(공지사항/고시공고 등) 자체 검색에 사용할 대표 키워드.
# 게시판이 활발해서(하루 수건) "최신 페이지 1건"만 가져오면 몇 주 전 글이 이미
# 밀려나 버리므로, 반드시 게시판 자체 검색으로 찾아야 한다.
SEARCH_KEYWORDS = ["개발", "지구단위", "도시계획", "아파트", "주택", "택지", "학교", "교육", "학생", "산업단지", "이통반", "이·통·반"]

# 충주시청 '공지사항'과 '공고/고시/입찰' 게시판에서 관심 부서 글만 남기기 위한 부서 목록.
NOTICE_DEPT_WHITELIST = {
    "자치행정과", "투자유치과", "교통정책과", "도시계획과", "도로과",
    "여성청소년과", "토지정보과", "평생학습과", "정원도시과", "균형개발과",
}

# 실행 회차 간 "신규 항목" 판정을 위한 이력 파일.
# {"링크": ["처음 본 날짜", "그다음 본 날짜", ...]} 형태로, 링크가 목격된 날짜(중복 없이)만 누적한다.
HISTORY_FILE = "item_history.json"
HISTORY_PRUNE_DAYS = 60  # 이만큼 오래 안 보인 항목은 이력 파일에서 정리

def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

def apply_history(all_collected_data, now_str):
    """각 항목에 '신규'(사상 최초 목격 여부)와 '발견일수'(목격된 서로 다른 날짜 수)를 채워 넣는다."""
    history = load_history()
    is_first_run = len(history) == 0
    today = now_str.split(" ")[0]

    for item in all_collected_data:
        link = item["링크"]
        prev_dates = history.get(link, [])
        item["신규"] = (len(prev_dates) == 0) and not is_first_run
        if today not in prev_dates:
            prev_dates = prev_dates + [today]
        history[link] = prev_dates
        item["발견일수"] = len(prev_dates)

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=HISTORY_PRUNE_DAYS)).strftime("%Y-%m-%d")
    history = {link: dates for link, dates in history.items() if max(dates) >= cutoff}

    save_history(history)

def fetch_elis(query_params, category, now_str):
    """자치법규정보시스템(elis.go.kr) 입법예고 목록 수집. query_params는 URL 쿼리스트링."""
    url = f"https://www.elis.go.kr/lgsltntc/lgsltNtcList?curPage=1&pageSize=50&{query_params}"
    pattern = re.compile(
        r'<td>(?P<org>[^<]*)</td>\s*'
        r'<td class="a-l"><a class="a-link ellipsis" href="javascript:void\(0\)" '
        r'onclick="fnSrchDtls\(\'(?P<sn>\d+)\',\'(?P<ctpv>\d+)\',\'(?P<sgg>\d+)\'\); return false;">'
        r'(?P<title>.*?)</a></td>\s*'
        r'<td>(?P<pbanno>[^<]*)</td>\s*'
        r'<td>\s*(?P<date>[\d-]+)\s*</td>',
        re.S
    )
    results = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        response = urllib.request.urlopen(req, context=ctx)
        html = response.read().decode('utf-8')
        for m in pattern.finditer(html):
            title_clean = clean_html(m.group('title'))
            if not matches_topic(title_clean):
                continue
            detail_url = (
                "https://www.elis.go.kr/lgsltntc/lgsltNtcDtl?"
                f"pbancSn={m.group('sn')}&srchCtpvCd={m.group('ctpv')}&srchSggCd={m.group('sgg')}"
            )
            results.append({
                "수집일시": now_str,
                "분류": category,
                "출처": "공식(elis.go.kr)",
                "제목": title_clean,
                "요약": f"{m.group('org').strip()} · {m.group('pbanno').strip()} · 공고일 {m.group('date').strip()}",
                "작성일": m.group('date').strip(),
                "링크": detail_url
            })
    except Exception as e:
        print(f" [오류] {category} 수집 실패: {e}")
    return results

def fetch_chungju_bbs(key, bbs_no, category, now_str):
    """충주시청 selectBbsNttList.do 형태 게시판을 개발/부동산/학교/교육 키워드로 검색해 수집.
    게시판 자체 검색(searchKrwd)을 써야 최신 페이지 밖으로 밀려난 글도 찾을 수 있다."""
    pattern = re.compile(
        r'<td class="first">\d+</td>\s*'
        r'<td[^>]*>(?P<dept>[^<]*)</td>\s*'
        r'<td class="subject">\s*.*?'
        rf'<a href="\./selectBbsNttView\.do\?key={key}&amp;bbsNo={bbs_no}&amp;nttNo=(?P<nttno>\d+)&amp;[^"]*">'
        r'(?P<title>[^<]*)</a>.*?'
        r'<td[^>]*>(?P<date>[\d-]+)</td>',
        re.S
    )
    results = []
    seen_links = set()
    for kw in SEARCH_KEYWORDS:
        url = (
            f"https://www.chungju.go.kr/www/selectBbsNttList.do?bbsNo={bbs_no}&key={key}"
            f"&searchCtgry=&searchCnd=&searchKrwd={urllib.parse.quote(kw)}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            response = urllib.request.urlopen(req, context=ctx)
            html = response.read().decode('utf-8')
            for m in pattern.finditer(html):
                detail_url = f"https://www.chungju.go.kr/www/selectBbsNttView.do?key={key}&bbsNo={bbs_no}&nttNo={m.group('nttno')}"
                if detail_url in seen_links:
                    continue
                seen_links.add(detail_url)
                results.append({
                    "수집일시": now_str,
                    "분류": category,
                    "출처": "공식(충주시청)",
                    "제목": clean_html(m.group('title')),
                    "요약": f"담당부서: {m.group('dept').strip()} · 작성일 {m.group('date').strip()}",
                    "작성일": m.group('date').strip(),
                    "담당부서": m.group('dept').strip(),
                    "링크": detail_url
                })
        except Exception as e:
            print(f" [오류] {category} 수집 실패 ({kw}): {e}")
    return results

def fetch_chungju_eminwon(key, ancmt_se_code, category, now_str):
    """충주시청 selectEminwonList.do 형태 고시공고 게시판을 개발/부동산/학교/교육 키워드로 검색해 수집.
    게시판 자체 검색(ancmt_sj)을 써야 최신 페이지 밖으로 밀려난 글도 찾을 수 있다."""
    pattern = re.compile(
        r'<td class="first">\d+</td>\s*'
        r'<td>\s*(?P<pbanno>[^<]*)</td>\s*'
        r'<td class="subject"><a href="(?P<href>\./selectEminwonView\.do\?[^"]*)">'
        r'(?P<title>[^<]*)</a></td>\s*'
        r'<td>(?P<dept>[^<]*)</td>\s*'
        r'<td>(?P<date>[\d-]+)</td>',
        re.S
    )
    results = []
    seen_links = set()
    for kw in SEARCH_KEYWORDS:
        url = (
            f"https://www.chungju.go.kr/www/selectEminwonList.do?key={key}&ofr_pageSize=10"
            f"&ancmt_se_code={ancmt_se_code}&pageIndex=1&ancmt_sj={urllib.parse.quote(kw)}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            response = urllib.request.urlopen(req, context=ctx)
            html = response.read().decode('utf-8')
            for m in pattern.finditer(html):
                # href에는 검색에 쓰인 키워드(ancmt_sj)가 그대로 echo되어 있어
                # 검색어마다 링크 문자열이 달라진다. 게시물 고유번호(ancmt_mgt_no)만
                # 뽑아 링크를 재구성해야 중복 수집도, 이후 신규글 판정도 안정적이다.
                mgtno_m = re.search(r'ancmt_mgt_no=(\d+)', m.group('href'))
                if not mgtno_m:
                    continue
                detail_url = f"https://www.chungju.go.kr/www/selectEminwonView.do?key={key}&ancmt_mgt_no={mgtno_m.group(1)}"
                if detail_url in seen_links:
                    continue
                seen_links.add(detail_url)
                results.append({
                    "수집일시": now_str,
                    "분류": category,
                    "출처": "공식(충주시청)",
                    "제목": clean_html(m.group('title')),
                    "요약": f"{m.group('pbanno').strip()} · 담당부서: {m.group('dept').strip()} · 등록일 {m.group('date').strip()}",
                    "작성일": m.group('date').strip(),
                    "담당부서": m.group('dept').strip(),
                    "링크": detail_url
                })
        except Exception as e:
            print(f" [오류] {category} 수집 실패 ({kw}): {e}")
    return results

def fetch_hug(keyword, category, now_str):
    """HUG(주택도시보증공사) 문의/민원 게시판 검색.
    응답 charset이 euc-kr이고 세션 쿠키가 있어야 검색 폼이 정상 동작한다."""
    list_url = "https://www.khug.or.kr/hug/web/cs/el/csel000181.jsp"
    pattern = re.compile(
        r'href="csel000182\.jsp\?hmpg_sno=(?P<sno>\d+)&amp;[^"]*"\s*'
        r'title="(?P<title>[^"]*)">[^<]*</a><img[^>]*>\s*'
        r'<span>(?P<open>[^<]*)</span>\s*<span>(?P<date>[\d-]+)</span>\s*'
        r'<span>(?P<views>\d+)</span>',
        re.S
    )
    results = []
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=ctx)
        )
        opener.open(urllib.request.Request(list_url, headers={"User-Agent": "Mozilla/5.0"})).read()

        data = urllib.parse.urlencode(
            {"searchField1": "ottp_yn", "searchField": "TITL", "search_Word": keyword, "cur_page": ""},
            encoding="euc-kr"
        ).encode("ascii")
        req = urllib.request.Request(list_url, data=data, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        html = opener.open(req).read().decode("euc-kr", errors="replace")

        for m in pattern.finditer(html):
            detail_url = f"https://www.khug.or.kr/hug/web/cs/el/csel000182.jsp?hmpg_sno={m.group('sno')}&hmpg_cvap_dcd=04"
            results.append({
                "수집일시": now_str,
                "분류": category,
                "출처": "공식(HUG)",
                "제목": clean_html(m.group('title')),
                "요약": f"공개여부: {m.group('open').strip()} · 조회수 {m.group('views').strip()}",
                "작성일": m.group('date').strip(),
                "링크": detail_url
            })
    except Exception as e:
        print(f" [오류] {category} 수집 실패: {e}")
    return results

# '지역여론' 카테고리에서 감시할 지역 카페 목록. 네이버 카페글 검색 API는 특정 카페로
# 검색 범위를 제한할 수 없으므로, 부동산/개발 관련 키워드로 넓게 검색한 뒤
# 응답의 cafeurl이 아래 목록에 해당하는 글만 걸러낸다.
TARGET_CAFES = [
    "https://cafe.naver.com/cjyeonsu",
    "https://cafe.naver.com/westcj2030",
    "https://cafe.naver.com/naver1st1",
    "https://cafe.naver.com/chungjuground",
    "https://cafe.naver.com/songmi1982",
]

CAFE_QUERIES = [
    "충주 아파트", "충주 분양", "충주 부동산", "서충주", "충주 재개발",
    "충주 재건축", "충주 택지", "충주 이주", "충주 신도시", "충주 전세",
    "충주 매매", "충주 입주",
]

def fetch_target_cafes(cafe_urls, queries, category, now_str):
    """네이버 카페글 검색 API로 지정된 지역 카페(cafe_urls)의 글만 필터링해 수집."""
    results = []
    seen_links = set()
    for query in queries:
        enc_query = urllib.parse.quote(query)
        url = f"https://openapi.naver.com/v1/search/cafearticle.json?query={enc_query}&display=30&sort=date"
        req = urllib.request.Request(url)
        req.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
        req.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)
        try:
            response = urllib.request.urlopen(req, context=ctx)
            data = json.loads(response.read().decode('utf-8'))
            for item in data.get('items', []):
                if item.get('cafeurl', '') not in cafe_urls:
                    continue
                link = item.get('link', '')
                if link in seen_links:
                    continue
                seen_links.add(link)
                cafename = item.get('cafename', '').strip()
                results.append({
                    "수집일시": now_str,
                    "분류": category,
                    "출처": "카페",
                    "제목": clean_html(item.get('title', '')),
                    "요약": f"[{cafename}] {clean_html(item.get('description', ''))}",
                    "작성일": "",
                    "링크": link
                })
        except Exception as e:
            print(f" [오류] {category} 수집 실패 ({query}): {e}")
    return results

# 대시보드 4열 레이아웃에서 각 분류(카테고리)가 어느 열에 속하는지 지정.
# 매핑에 없는 분류는 자동으로 '기타' 열로 들어간다.
CATEGORY_COLUMN = {
    "🚨 [위기징후] 건설사·아파트 리스크": "news",
    "🏢 [일반동향] 충주 부동산": "news",
    "🏙️ [도시계획고시] 토지이음 (충주)": "notice",
    "📜 [자치법규] 입법예고 (충주시)": "notice",
    "📜 [자치법규] 입법예고 (충청북도 도단위)": "notice",
    "📢 [공지사항] 충주시청": "notice",
    "📜 [입법예고] 충주시청": "notice",
    "📋 [공고/고시/입찰] 충주시청": "notice",
    "🏫 [학교/교육] 학생배치 동향": "school",
    "🏠 [임대보증] HUG 보증사고·반환지연 민원": "school",
    "🗣️ [지역여론] 커뮤니티": "etc",
}

def assign_columns(all_collected_data):
    for item in all_collected_data:
        item["컬럼"] = CATEGORY_COLUMN.get(item["분류"], "etc")

# 여러 매체가 같은 전국 단위 보도자료(예: "삼성전자 히트펌프 생산" 기사가 충주를
# 시험 지역 중 하나로 스치듯 언급)를 유사한 제목/문구로 동시 보도하는 경우가 있어,
# 뉴스 계열 카테고리에 한해 유사 기사를 대표 1건(최신순)으로 접어 보여준다.
NEWS_DEDUPE_CATEGORIES = {
    "🚨 [위기징후] 건설사·아파트 리스크",
    "🏫 [학교/교육] 학생배치 동향",
    "🏢 [일반동향] 충주 부동산",
}

def _normalize_title(text):
    """비교용 정규화: 대괄호 말머리·기호·공백을 제거해 한글/영숫자만 남긴다."""
    text = re.sub(r'\[[^\]]*\]|<[^>]*>', ' ', text)
    return re.sub(r'[^0-9A-Za-z가-힣]+', '', text)

def _bigrams(text):
    text = _normalize_title(text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}

def _text_similarity(a, b):
    """문자 bigram 중복도(overlap coefficient).
    한국어 기사 제목은 어미·조사가 매체마다 달라 SequenceMatcher로는 같은 사건을
    잡아내지 못하므로, 길이 차이에 관대한 bigram 중복도를 쓴다."""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))

# 실측 보정값: 같은 사건 기사끼리는 최소 0.385, 서로 다른 기사끼리는 최대 0.133이라
# 그 사이인 0.32를 임계값으로 둔다.
TITLE_SIMILARITY_THRESHOLD = 0.32

def dedupe_similar_news(items, title_threshold=TITLE_SIMILARITY_THRESHOLD):
    groups = []
    for item in items:
        placed_group = None
        for group in groups:
            # 대표 1건이 아니라 그룹 내 모든 기사와 비교(single-linkage)해야
            # 제목이 조금씩 달라지며 이어지는 같은 사건 기사들이 한 덩어리로 묶인다.
            if any(_text_similarity(item["제목"], member["제목"]) >= title_threshold
                   for member in group):
                placed_group = group
                break
        if placed_group is not None:
            placed_group.append(item)
        else:
            groups.append([item])

    result = []
    for group in groups:
        group.sort(key=lambda x: x.get("작성일") or "", reverse=True)
        rep = dict(group[0])
        rep["관련보도수"] = len(group)
        if len(group) > 1:
            rep["관련보도목록"] = [
                {"제목": g["제목"], "링크": g["링크"], "작성일": g.get("작성일", "")}
                for g in group[1:]
            ]
        result.append(rep)
    return result

def dedupe_by_link(all_collected_data):
    """같은 카테고리 안에서 링크가 겹치는 항목 제거.
    '지역여론' 카테고리는 지정카페 수집과 일반 카페 검색 두 경로로 채워지므로
    동일 게시글이 두 번 들어올 수 있다."""
    seen = set()
    result = []
    for item in all_collected_data:
        key = (item["분류"], item["링크"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result

def dedupe_similar_news_in_categories(all_collected_data):
    by_category = {}
    for item in all_collected_data:
        by_category.setdefault(item["분류"], []).append(item)

    result = []
    for category, items in by_category.items():
        if category in NEWS_DEDUPE_CATEGORIES:
            items = dedupe_similar_news(items)
        result.extend(items)
    return result

def fetch_eum(keyword, category, now_str):
    """토지이음(eum.go.kr) 도시계획 고시정보 검색.
    응답 charset이 euc-kr이므로 폼도 euc-kr로 인코딩해야 한다(세션 쿠키는 불필요)."""
    url = "https://www.eum.go.kr/web/gs/gv/gvGosiList.jsp"
    pattern = re.compile(
        r'<td class="mb">(?P<date>[\d-]+)</td>\s*'
        r'<td class="left mb"[^>]*>\s*(?P<gosino>[^<]*?)\s*</td>\s*'
        r'<td class="left">\s*<a href=\'gvGosiDet\.jsp\?seq=(?P<seq>\d+)[^\']*\' title=\'(?P<title>[^\']*)\'\s*>',
        re.S
    )
    results = []
    try:
        data = urllib.parse.urlencode(
            {"zonenm": keyword, "listSize": "50"},
            encoding="euc-kr"
        ).encode("ascii")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        html = urllib.request.urlopen(req, context=ctx).read().decode("euc-kr", errors="replace")

        for m in pattern.finditer(html):
            detail_url = f"https://www.eum.go.kr/web/gs/gv/gvGosiDet.jsp?seq={m.group('seq')}"
            results.append({
                "수집일시": now_str,
                "분류": category,
                "출처": "공식(토지이음)",
                "제목": clean_html(m.group('title')),
                "요약": m.group('gosino').strip(),
                "작성일": m.group('date').strip(),
                "링크": detail_url
            })
    except Exception as e:
        print(f" [오류] {category} 수집 실패: {e}")
    return results

def run_briefing():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n[{now_str}] 데이터 수집을 시작합니다...")
    all_collected_data = []

    # 1. 네이버 API 수집 (위기 징후 및 여론)
    # 네이버 검색 API는 "OR" 같은 불리언 연산자를 지원하지 않으므로
    # 키워드별로 각각 호출한 뒤 결과를 합친다 (링크 기준 중복 제거).
    search_targets = [
        {
            "category": "🚨 [위기징후] 건설사·아파트 리스크",
            "type": "news",
            "queries": [
                "충주 아파트 시공사 부도", "충주 아파트 건설사 회생", "충주 아파트 공사중단",
                "충주 아파트 시행사 법정관리", "충주 아파트 허그 보증금",
                "충주 건설사", "충주 아파트 시공사", "서충주 아파트 건설",
                "삼일건설", "삼일파라뷰"
            ],
            # 네이버 뉴스 검색이 단어를 느슨하게 매칭해서(돌봄센터 소식 등) 무관한 기사가 섞이므로,
            # 제목/요약에 건설·아파트 관련 단어가 실제로 있는 것만 통과시킨다.
            "must_include": [
                "시공사", "시행사", "회생절차", "회생 절차", "법정관리", "법정 관리",
                "공사중단", "공사 중단", "부도", "미분양", "재건축", "재개발", "정비사업",
                "입주 지연", "보증금 반환", "부실시공", "하자", "PF", "준공"
            ]
        },
        {"category": "🗣️ [지역여론] 커뮤니티", "type": "cafearticle", "queries": ["충주 분양 전환", "충주 허그 보증금", "충주 분양 지연", "충주 아파트 취소"]},
        {"category": "🏫 [학교/교육] 학생배치 동향", "type": "news", "queries": ["충주 초등학교 신설", "충주 중학교 배치", "서충주 과밀학급"]},
        {"category": "🏢 [일반동향] 충주 부동산", "type": "news", "queries": ["충주 아파트 분양", "서충주 신도시", "충주 공동주택"]}
    ]

    for target in search_targets:
        seen_links = set()
        found_count = 0
        for query in target["queries"]:
            enc_query = urllib.parse.quote(query)
            url = f"https://openapi.naver.com/v1/search/{target['type']}.json?query={enc_query}&display=10&sort=date"

            req = urllib.request.Request(url)
            req.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
            req.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)

            try:
                response = urllib.request.urlopen(req, context=ctx)
                res_code = response.getcode()
                if res_code == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    items = data.get('items', [])

                    for item in items:
                        link = item.get("link", "")
                        if link in seen_links:
                            continue
                        seen_links.add(link)
                        date_val = parse_naver_date(item.get("pubDate", ""))
                        if not is_recent_or_undated(date_val):
                            continue
                        title_clean = clean_html(item.get("title", ""))
                        desc_clean = clean_html(item.get("description", ""))
                        must_include = target.get("must_include")
                        if must_include and not any(kw in title_clean or kw in desc_clean for kw in must_include):
                            continue
                        found_count += 1
                        all_collected_data.append({
                            "수집일시": now_str,
                            "분류": target["category"],
                            "출처": "뉴스" if target["type"] == "news" else "카페",
                            "제목": title_clean,
                            "요약": desc_clean,
                            "작성일": date_val,
                            "링크": link
                        })
            except Exception as e:
                print(f" [오류] 네이버 수집 실패 ({target['category']} / {query}): {e}")

        print(f" - {target['category']}: {found_count}건 발견")

    # 1-0. 지정 지역 카페 5곳의 부동산/개발 관련 글.
    # 네이버 카페 검색으로 모은 '지역여론' 항목과 같은 카테고리로 합쳐서 보여준다.
    cafe_category = "🗣️ [지역여론] 커뮤니티"
    cafe_items = fetch_target_cafes(TARGET_CAFES, CAFE_QUERIES, cafe_category, now_str)
    all_collected_data.extend(cafe_items)
    print(f" - {cafe_category}(지정카페): {len(cafe_items)}건 발견")

    # 1-1. HUG(주택도시보증공사) 문의/민원 게시판 - 보증사고·반환지연 등 1차 민원 데이터
    hug_category = "🏠 [임대보증] HUG 보증사고·반환지연 민원"
    hug_items = fetch_hug("충주", hug_category, now_str)
    all_collected_data.extend(hug_items)
    print(f" - {hug_category}: {len(hug_items)}건 발견")

    # 1-2. 토지이음(eum.go.kr) 도시계획 고시정보 - 국토교통부 통합 시스템
    eum_category = "🏙️ [도시계획고시] 토지이음 (충주)"
    eum_items = fetch_eum("충주", eum_category, now_str)
    all_collected_data.extend(eum_items)
    print(f" - {eum_category}: {len(eum_items)}건 발견")

    # 2. 자치법규정보시스템 (elis.go.kr) 입법예고
    elis_boards = [
        {"params": "ctpvCd=43&sggCd=130", "category": "📜 [자치법규] 입법예고 (충주시)"},
        {"params": "ctpvCd=43&sggCd=000", "category": "📜 [자치법규] 입법예고 (충청북도 도단위)"},
    ]
    for board in elis_boards:
        items = fetch_elis(board["params"], board["category"], now_str)
        all_collected_data.extend(items)
        print(f" - {board['category']}: {len(items)}건 발견")

    # 4. 충주시청 공지사항 / 입법예고 / 공고·고시·입찰
    # '공지사항'과 '공고/고시/입찰'은 지정된 부서 목록에 해당하는 글만 남긴다.
    chungju_boards = [
        {"fn": fetch_chungju_bbs, "args": (506, 5), "category": "📢 [공지사항] 충주시청", "dept_filter": True},
        {"fn": fetch_chungju_eminwon, "args": (509, "03"), "category": "📜 [입법예고] 충주시청"},
        {"fn": fetch_chungju_eminwon, "args": (510, "01,02,04,05"), "category": "📋 [공고/고시/입찰] 충주시청", "dept_filter": True},
    ]
    for board in chungju_boards:
        items = board["fn"](*board["args"], board["category"], now_str)
        if board.get("dept_filter"):
            items = [i for i in items if i.get("담당부서") in NOTICE_DEPT_WHITELIST]
        all_collected_data.extend(items)
        print(f" - {board['category']}: {len(items)}건 발견")

    # 5. 지난 실행 대비 신규 항목 판정
    all_collected_data = dedupe_by_link(all_collected_data)
    all_collected_data = dedupe_similar_news_in_categories(all_collected_data)
    assign_columns(all_collected_data)
    apply_history(all_collected_data, now_str)
    new_count = sum(1 for item in all_collected_data if item["신규"])
    print(f" - 🆕 신규 항목: {new_count}건")

    # 6. HTML 생성
    json_data_str = json.dumps(all_collected_data, ensure_ascii=False)
    
    html_template = f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>충주 동향 브리핑</title>
      <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%99%EF%B8%8F%3C/text%3E%3C/svg%3E">
      <meta name="description" content="충주시 개발·부동산·학교 동향을 자동 수집하는 대시보드 (GitHub Actions로 하루 3회 자동 갱신)">
      <style>
        :root {{
          --bg-page: #f2f2f0;
          --bg: #ffffff;
          --bg-subtle: #fafaf9;
          --border: #e2e2df;
          --border-strong: #c9c9c5;
          --text-primary: #1a1a1a;
          --text-secondary: #595955;
          --text-dim: #8c8c87;
          --accent: #d9531e;
          --accent-dark: #b84415;
          --accent-green: #0a7a3d;
          --accent-red: #c81e3a;
          --accent-blue: #1a5fb4;
          --mono: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
          --sans: 'Segoe UI', 'Pretendard', 'Malgun Gothic', -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
        }}

        * {{ box-sizing: border-box; }}

        body {{
          margin: 0;
          font-family: var(--sans);
          background: var(--bg-page);
          color: var(--text-primary);
          padding: 0 0 40px 0;
          min-height: 100vh;
        }}

        .topbar {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 16px 28px;
          border-bottom: 2px solid var(--text-primary);
          background: var(--bg);
          position: sticky;
          top: 0;
          z-index: 10;
        }}

        .brand {{ display: flex; align-items: center; gap: 12px; }}

        .brand-mark {{
          width: 38px; height: 38px;
          border-radius: 4px;
          display: flex; align-items: center; justify-content: center;
          font-family: var(--sans);
          font-weight: 700;
          font-size: 0.95em;
          background: var(--accent);
          color: #ffffff;
          letter-spacing: 0.5px;
        }}

        .brand h1 {{
          margin: 0;
          font-size: 1.2em;
          font-weight: 700;
          letter-spacing: -0.2px;
          color: var(--text-primary);
        }}

        .brand .sub {{
          font-family: var(--mono);
          font-size: 0.7em;
          color: var(--text-dim);
          letter-spacing: 0.5px;
          margin-top: 2px;
        }}

        .live-indicator {{
          display: flex; align-items: center; gap: 7px;
          font-family: var(--mono);
          font-size: 0.76em;
          font-weight: 700;
          color: var(--accent-green);
          border: 1px solid var(--accent-green);
          padding: 5px 11px;
          border-radius: 3px;
        }}

        .live-indicator .dot {{
          width: 6px; height: 6px; border-radius: 50%;
          background: var(--accent-green);
          animation: pulse 2s ease-in-out infinite;
        }}

        @keyframes pulse {{
          0%, 100% {{ opacity: 1; }}
          50% {{ opacity: 0.35; }}
        }}

        .stats-row {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
          gap: 1px;
          background: var(--border);
          border: 1px solid var(--border);
          margin: 22px 28px 0 28px;
        }}

        .stat-tile {{
          background: var(--bg);
          padding: 14px 18px;
        }}

        .stat-tile .label {{
          font-size: 0.7em;
          color: var(--text-dim);
          letter-spacing: 0.4px;
          text-transform: uppercase;
          margin-bottom: 8px;
        }}

        .stat-tile .value {{
          font-family: var(--sans);
          font-size: 1.5em;
          font-weight: 700;
          color: var(--text-primary);
        }}

        .stat-tile.risk .value {{ color: var(--accent-red); }}
        .stat-tile.new .value {{ color: var(--accent-green); }}
        .stat-tile.clock .value {{ font-family: var(--mono); color: var(--accent); font-size: 1.3em; }}

        .columns {{
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 18px;
          padding: 22px 28px;
          align-items: start;
        }}

        .column-head {{
          font-size: 0.98em;
          font-weight: 700;
          color: var(--text-primary);
          padding-bottom: 8px;
          margin-bottom: 14px;
          border-bottom: 3px solid var(--accent);
        }}

        .column-body {{
          display: flex;
          flex-direction: column;
          gap: 16px;
        }}

        .card {{
          background: var(--bg);
          border: 1px solid var(--border);
        }}

        .card-header {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 11px 14px;
          border-bottom: 1px solid var(--border);
          background: var(--bg-subtle);
        }}

        .card-header h2 {{
          margin: 0;
          font-size: 0.86em;
          font-weight: 700;
          color: var(--text-primary);
        }}

        .card-count {{
          font-family: var(--mono);
          font-size: 0.74em;
          color: var(--text-secondary);
          border: 1px solid var(--border-strong);
          border-radius: 3px;
          padding: 1px 7px;
        }}

        .card-body {{
          max-height: 460px;
          overflow-y: auto;
          padding: 4px 14px 2px 14px;
        }}

        .card-body::-webkit-scrollbar {{ width: 7px; }}
        .card-body::-webkit-scrollbar-track {{ background: transparent; }}
        .card-body::-webkit-scrollbar-thumb {{
          background: var(--border-strong);
          border-radius: 10px;
        }}
        .card-body::-webkit-scrollbar-thumb:hover {{ background: var(--accent); }}

        .item {{
          padding: 11px 0;
          border-bottom: 1px solid var(--border);
        }}

        .item:last-child {{ border-bottom: none; }}

        .item-top {{
          display: flex;
          align-items: center;
          gap: 6px;
          margin-bottom: 6px;
        }}

        .badge {{
          display: inline-block;
          padding: 1px 7px;
          font-size: 0.66em;
          font-family: var(--mono);
          border-radius: 3px;
          font-weight: 700;
          letter-spacing: 0.2px;
          border: 1px solid transparent;
        }}

        .badge-news {{ background: #eaf1fb; color: var(--accent-blue); border-color: #c3d7f0; }}
        .badge-cafe {{ background: #e8f6ee; color: var(--accent-green); border-color: #b9e3ca; }}
        .badge-official {{ background: #fdf1e8; color: var(--accent); border-color: #f2cdb0; }}
        .badge-warning {{
          background: #fbe9ec;
          color: var(--accent-red);
          border-color: #f0bcc4;
        }}

        .badge-new {{
          background: #e8f6ee;
          color: var(--accent-green);
          border-color: #b9e3ca;
        }}

        .badge-streak {{
          background: var(--bg-subtle);
          color: var(--text-secondary);
          border-color: var(--border-strong);
        }}

        .badge-dup {{
          background: var(--bg-subtle);
          color: var(--text-dim);
          border-color: var(--border);
          cursor: pointer;
          user-select: none;
        }}
        .badge-dup:hover {{ border-color: var(--text-secondary); color: var(--text-secondary); }}

        .dup-list {{
          margin-top: 8px;
          padding-left: 10px;
          border-left: 2px solid var(--border);
          display: flex;
          flex-direction: column;
          gap: 6px;
        }}

        .dup-list-item {{
          font-size: 0.8em;
          color: var(--text-secondary);
          text-decoration: none;
          display: flex;
          justify-content: space-between;
          gap: 8px;
        }}

        .dup-list-item:hover {{ color: var(--accent); text-decoration: underline; }}

        .dup-list-date {{
          font-family: var(--mono);
          font-size: 0.85em;
          color: var(--text-dim);
          white-space: nowrap;
        }}

        .settings-btn {{
          font-family: var(--sans);
          font-size: 0.78em;
          font-weight: 700;
          color: var(--text-secondary);
          background: var(--bg);
          border: 1px solid var(--border-strong);
          border-radius: 3px;
          padding: 6px 12px;
          cursor: pointer;
        }}

        .settings-btn:hover {{ color: var(--accent); border-color: var(--accent); }}

        .topbar-right {{
          display: flex;
          align-items: center;
          gap: 10px;
        }}

        .modal-overlay {{
          display: none;
          position: fixed;
          inset: 0;
          background: rgba(20, 18, 14, 0.5);
          z-index: 100;
          align-items: center;
          justify-content: center;
          padding: 20px;
        }}

        .modal-overlay.open {{ display: flex; }}

        .modal-panel {{
          background: var(--bg);
          border-radius: 6px;
          max-width: 560px;
          width: 100%;
          max-height: 80vh;
          display: flex;
          flex-direction: column;
          box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
        }}

        .modal-header {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 16px 20px;
          border-bottom: 1px solid var(--border);
        }}

        .modal-header h3 {{ margin: 0; font-size: 1em; }}

        .modal-close-btn {{
          background: none;
          border: none;
          cursor: pointer;
          font-size: 1.1em;
          color: var(--text-dim);
          line-height: 1;
        }}

        .modal-close-btn:hover {{ color: var(--text-primary); }}

        .modal-desc {{
          padding: 12px 20px 0 20px;
          font-size: 0.82em;
          color: var(--text-secondary);
          line-height: 1.5;
          margin: 0;
        }}

        .rule-list {{
          overflow-y: auto;
          padding: 12px 20px;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }}

        .rule-row {{
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 8px 10px;
          border: 1px solid var(--border);
          border-radius: 4px;
          font-size: 0.85em;
        }}

        .rule-row.rule-off {{ opacity: 0.5; }}

        .rule-toggle {{
          display: flex;
          align-items: center;
          gap: 8px;
          flex: 1;
          cursor: pointer;
        }}

        .rule-text {{ flex: 1; }}

        .rule-tag {{
          font-family: var(--mono);
          font-size: 0.7em;
          color: var(--text-dim);
          border: 1px solid var(--border-strong);
          border-radius: 3px;
          padding: 1px 6px;
          white-space: nowrap;
        }}

        .rule-tag-custom {{ color: var(--accent-green); border-color: #b9e3ca; }}

        .rule-remove-btn {{
          font-size: 0.78em;
          color: var(--accent-red);
          background: none;
          border: 1px solid transparent;
          cursor: pointer;
          padding: 3px 8px;
          border-radius: 3px;
        }}

        .rule-remove-btn:hover {{ border-color: var(--accent-red); }}

        .rule-add-row {{
          display: flex;
          gap: 8px;
          padding: 12px 20px;
          border-top: 1px solid var(--border);
        }}

        .rule-add-row input {{
          flex: 1;
          border: 1px solid var(--border-strong);
          border-radius: 4px;
          padding: 8px 10px;
          font-size: 0.85em;
          outline: none;
          color: var(--text-primary);
          background: var(--bg);
          font-family: var(--sans);
        }}

        .rule-add-row input:focus {{ border-color: var(--accent); }}

        .modal-footer-note {{
          padding: 0 20px 14px 20px;
          font-size: 0.72em;
          color: var(--text-dim);
        }}

        .item-title {{
          font-weight: 600;
          color: var(--text-primary);
          text-decoration: none;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
          line-height: 1.45;
          font-size: 0.92em;
        }}

        .item-title:hover {{ color: var(--accent); text-decoration: underline; }}

        .item-desc {{
          font-size: 0.81em;
          color: var(--text-secondary);
          line-height: 1.55;
          margin-top: 6px;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }}

        .highlight-red {{
          color: var(--accent-red);
          font-weight: 700;
          background: #fbe9ec;
          padding: 0 3px;
          border-radius: 2px;
        }}

        .highlight-search {{
          color: #1a1a1a;
          font-weight: 700;
          background: #ffe3a3;
          padding: 0 3px;
          border-radius: 2px;
        }}

        .filter-bar {{
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
          padding: 16px 28px 0 28px;
        }}

        .filter-input-wrap {{
          display: flex;
          align-items: center;
          gap: 8px;
          background: var(--bg);
          border: 1px solid var(--border-strong);
          border-radius: 4px;
          padding: 8px 12px;
          flex: 1;
          min-width: 240px;
        }}

        .filter-input-wrap:focus-within {{
          border-color: var(--accent);
          box-shadow: 0 0 0 1px var(--accent);
        }}

        .filter-prompt {{
          font-family: var(--mono);
          color: var(--accent);
          font-size: 0.9em;
        }}

        #filterInput {{
          flex: 1;
          background: transparent;
          border: none;
          outline: none;
          color: var(--text-primary);
          font-family: var(--sans);
          font-size: 0.9em;
        }}

        #filterInput::placeholder {{ color: var(--text-dim); }}

        .filter-btn {{
          font-family: var(--sans);
          font-size: 0.82em;
          font-weight: 700;
          letter-spacing: 0.2px;
          border-radius: 4px;
          padding: 9px 16px;
          cursor: pointer;
          border: 1px solid transparent;
          transition: filter 0.15s ease, transform 0.1s ease;
        }}

        .filter-btn:active {{ transform: scale(0.97); }}

        .filter-btn-search {{
          background: var(--accent);
          color: #ffffff;
          border-color: var(--accent);
        }}

        .filter-btn-search:hover {{ background: var(--accent-dark); }}

        .filter-btn-reset {{
          background: transparent;
          color: var(--text-secondary);
          border-color: var(--border-strong);
        }}

        .filter-btn-reset:hover {{ color: var(--text-primary); border-color: var(--text-secondary); }}

        .filter-status {{
          padding: 10px 28px 0 28px;
          font-family: var(--mono);
          font-size: 0.78em;
          color: var(--accent-dark);
          display: none;
        }}

        .filter-status.active {{ display: block; }}

        .filter-status .clear-link {{
          color: var(--accent-red);
          cursor: pointer;
          text-decoration: underline;
          margin-left: 8px;
        }}

        .item-date-tag {{
          font-family: var(--mono);
          font-size: 0.68em;
          color: var(--text-dim);
          margin-left: auto;
        }}

        .bucket-header {{
          display: flex;
          align-items: center;
          gap: 8px;
          font-family: var(--mono);
          font-size: 0.68em;
          letter-spacing: 0.5px;
          color: var(--accent-dark);
          text-transform: uppercase;
          padding: 13px 0 6px 0;
          border-top: 1px solid var(--border);
          margin-top: 4px;
        }}

        .bucket-header:first-child {{
          border-top: none;
          margin-top: 0;
          padding-top: 4px;
        }}

        .bucket-count {{
          font-family: var(--mono);
          font-size: 0.85em;
          color: var(--text-dim);
        }}

        .empty-state {{
          padding: 40px 14px;
          text-align: center;
          color: var(--text-dim);
          font-family: var(--mono);
          font-size: 0.85em;
        }}

        footer {{
          text-align: center;
          padding: 26px 20px 22px 20px;
          color: var(--text-dim);
          font-family: var(--sans);
          font-size: 0.75em;
          letter-spacing: 0.2px;
          border-top: 1px solid var(--border);
          margin-top: 12px;
        }}

        footer .auto-badge {{
          display: inline-flex;
          align-items: center;
          gap: 6px;
          color: var(--accent-green);
          border: 1px solid #b9e3ca;
          background: #e8f6ee;
          padding: 5px 12px;
          border-radius: 3px;
          margin-bottom: 12px;
          font-weight: 700;
        }}

        footer .sources {{
          color: var(--text-secondary);
          line-height: 1.8;
          max-width: 720px;
          margin: 0 auto;
        }}

        @media (max-width: 1100px) {{
          .columns {{ grid-template-columns: repeat(2, 1fr); }}
        }}

        @media (max-width: 640px) {{
          .stats-row, .columns, .filter-bar, .filter-status {{ padding-left: 14px; padding-right: 14px; margin-left: 14px; margin-right: 14px; }}
          .topbar {{ padding: 14px; flex-wrap: wrap; gap: 10px; }}
          .brand h1 {{ font-size: 1.05em; }}
          .columns {{ grid-template-columns: 1fr; padding-left: 14px; padding-right: 14px; margin: 0; }}
          .stats-row {{ margin-left: 14px; margin-right: 14px; }}
        }}
      </style>
    </head>
    <body>
      <div class="topbar">
        <div class="brand">
          <div class="brand-mark">CJ</div>
          <div>
            <h1>충주 동향 브리핑</h1>
            <div class="sub">CHUNGJU REGIONAL BRIEFING</div>
          </div>
        </div>
        <div class="topbar-right">
          <button type="button" class="settings-btn" id="warningSettingsBtn">⚙ 주의어 설정</button>
          <div class="live-indicator"><span class="dot"></span>자동 갱신 중</div>
        </div>
      </div>

      <div class="stats-row" id="statsRow"></div>

      <div class="filter-bar">
        <div class="filter-input-wrap">
          <span class="filter-prompt">&gt;</span>
          <input type="text" id="filterInput" placeholder="검색어 입력 (예: 삼일파라뷰, 삼일건설) 후 Enter">
        </div>
        <button type="button" class="filter-btn filter-btn-search" id="filterSearchBtn">검색</button>
        <button type="button" class="filter-btn filter-btn-reset" id="filterResetBtn">초기화</button>
      </div>
      <div class="filter-status" id="filterStatus"></div>

      <div id="dashboardWrap">
        <div class="columns" id="dashboard"></div>
      </div>

      <div class="modal-overlay" id="warningSettingsOverlay">
        <div class="modal-panel">
          <div class="modal-header">
            <h3>🚨 주의 표시 키워드 관리</h3>
            <button type="button" class="modal-close-btn" id="warningSettingsCloseBtn">✕</button>
          </div>
          <p class="modal-desc">
            아래 규칙 중 하나라도 만족하면(같은 규칙 안 단어는 모두 포함되어야 함, 규칙끼리는 OR) 제목/요약에 🚨 주의 배지가 표시됩니다.
            변경 사항은 이 브라우저에만 저장되며, 다음 자동 갱신에도 유지됩니다.
          </p>
          <div class="rule-list" id="warningRuleList"></div>
          <div class="rule-add-row">
            <input type="text" id="newRuleInput" placeholder="새 키워드 추가 (AND 조건은 + 로 구분, 예: 한화포레나+충주호암)">
            <button type="button" class="filter-btn filter-btn-search" id="addRuleBtn">추가</button>
          </div>
          <div class="modal-footer-note">기본 규칙은 삭제 대신 체크 해제로 끌 수 있고, 언제든 다시 켤 수 있습니다.</div>
        </div>
      </div>

      <footer>
        <div class="auto-badge">⚙️ GitHub Actions로 매일 10:00 · 14:00 · 17:40 자동 갱신</div>
        <div class="sources">
          충주시 개발·부동산·학교 관련 공공데이터를 자동 수집하는 대시보드입니다.<br>
          출처: 네이버 뉴스·카페(cjyeonsu · westcj2030 · naver1st1 · chungjuground · songmi1982) ·
          충주시청(공지사항·지구단위계획·고시공고) · 자치법규정보시스템(elis.go.kr) ·
          주택도시보증공사(HUG) · 토지이음(eum.go.kr)<br>
          담당자 확인용 참고 자료이며, 법적 효력이 있는 공식 공고는 각 기관 원문을 반드시 확인하세요.
        </div>
      </footer>

      <script>
        const rawData = {json_data_str};

        // 🚨 주의 배지 기본 규칙 (규칙 내 항목은 모두 AND, 규칙 간에는 OR).
        // 사용자가 '주의어 설정'에서 켜고 끄거나 새 규칙을 추가할 수 있으며, 그 변경은
        // localStorage에 저장되어 이 브라우저에서 계속 유지된다.
        const defaultWarningRules = [
          ['삼일건설'],
          ['삼일파라뷰'],
          ['한화포레나 충주호암'],
          ['서충주', '아파트'],
          ['통학구역'],
          ['학구'],
          ['건설사 회생'],
          ['미분양', '충주'],
          ['도시계획', '조례'],
          ['이통반'],
          ['이·통·반'],
          ['학교', '조례'],
          ['충주', '유치원'],
          ['충주', '초등학교'],
          ['충주', '중학교'],
          ['충주', '고등학교'],
        ];
        // 하이라이트 시에는 '충주'/'아파트'/'학교'/'조례'처럼 너무 흔한 단어는 제외한다.
        const HIGHLIGHT_STOPWORDS = new Set(['충주', '아파트', '학교', '조례']);
        const WARNING_RULE_STORAGE_KEY = 'cj_briefing_warning_rules_v1';

        function ruleKey(rule) {{ return rule.join('+'); }}

        function loadWarningRuleState() {{
          try {{
            const raw = JSON.parse(localStorage.getItem(WARNING_RULE_STORAGE_KEY) || '{{}}');
            return {{
              disabled: Array.isArray(raw.disabled) ? raw.disabled : [],
              custom: Array.isArray(raw.custom) ? raw.custom : [],
            }};
          }} catch (e) {{
            return {{ disabled: [], custom: [] }};
          }}
        }}

        function saveWarningRuleState(state) {{
          localStorage.setItem(WARNING_RULE_STORAGE_KEY, JSON.stringify(state));
        }}

        let warningRuleState = loadWarningRuleState();
        let warningRules = [];
        let warningHighlightTerms = [];

        function recomputeWarningRules() {{
          const disabledSet = new Set(warningRuleState.disabled);
          const active = defaultWarningRules.filter(rule => !disabledSet.has(ruleKey(rule)));
          warningRules = [...active, ...warningRuleState.custom];
          const terms = new Set();
          warningRules.forEach(rule => rule.forEach(term => {{
            if (!HIGHLIGHT_STOPWORDS.has(term)) terms.add(term);
          }}));
          warningHighlightTerms = [...terms];
        }}
        recomputeWarningRules();

        const columnDefs = [
          {{ key: 'news', title: '① 뉴스' }},
          {{ key: 'notice', title: '② 공시 · 충주시청 알림' }},
          {{ key: 'school', title: '③ 학교 / 공동주택' }},
          {{ key: 'etc', title: '④ 기타' }},
        ];

        function hasWarning(item) {{
          const text = item['제목'] + ' ' + item['요약'];
          return warningRules.some(rule => rule.every(kw => text.includes(kw)));
        }}

        function highlightWarning(text) {{
          let resultText = text;
          warningHighlightTerms.forEach(keyword => {{
            const regex = new RegExp(keyword, 'g');
            resultText = resultText.replace(regex, `<span class="highlight-red">${{keyword}}</span>`);
          }});
          return resultText;
        }}

        function escapeHtml(text) {{
          const div = document.createElement('div');
          div.textContent = text;
          return div.innerHTML;
        }}

        function escapeRegExp(text) {{
          const bs = String.fromCharCode(92);
          const specials = [bs, '.', '*', '+', '?', '^', '$', '(', ')', '|', '[', ']'];
          let result = text;
          specials.forEach(ch => {{
            result = result.split(ch).join(bs + ch);
          }});
          return result;
        }}

        function highlightSearch(text, term) {{
          if (!term) return text;
          const regex = new RegExp(escapeRegExp(term), 'gi');
          return text.replace(regex, m => `<span class="highlight-search">${{m}}</span>`);
        }}

        const bucketOrder = ['최근 3개월', '최근 6개월', '최근 1년', '1년 이상', '날짜 미상'];

        function bucketFor(dateStr) {{
          if (!dateStr) return '날짜 미상';
          const d = new Date(dateStr);
          if (isNaN(d.getTime())) return '날짜 미상';
          const diffDays = (Date.now() - d.getTime()) / 86400000;
          if (diffDays < 0) return '최근 3개월';
          if (diffDays <= 90) return '최근 3개월';
          if (diffDays <= 180) return '최근 6개월';
          if (diffDays <= 365) return '최근 1년';
          return '1년 이상';
        }}

        let dupIdCounter = 0;

        function renderItem(item, filterTerm) {{
          const badgeClass = item['출처'] === '뉴스' ? 'badge-news' : (item['출처'] === '카페' ? 'badge-cafe' : 'badge-official');
          const warningBadge = hasWarning(item) ? `<span class="badge badge-warning">🚨 주의</span>` : '';
          const newBadge = item['신규'] ? `<span class="badge badge-new">🆕 NEW</span>` : '';
          const streakBadge = (!item['신규'] && item['발견일수'] >= 2)
            ? `<span class="badge badge-streak">🔁 ${{item['발견일수']}}일째</span>`
            : '';
          const dupList = item['관련보도목록'] || [];
          const dupId = `dup-${{dupIdCounter++}}`;
          const dupBadge = dupList.length > 0
            ? `<span class="badge badge-dup" data-target="${{dupId}}" data-count="${{item['관련보도수']}}">유사보도 ${{item['관련보도수']}}건 보기</span>`
            : '';
          let displayTitle = highlightWarning(escapeHtml(item['제목']));
          let displayDesc = highlightWarning(escapeHtml(item['요약']));
          if (filterTerm) {{
            displayTitle = highlightSearch(displayTitle, filterTerm);
            displayDesc = highlightSearch(displayDesc, filterTerm);
          }}
          const dateTag = item['작성일'] ? `<span class="item-date-tag">${{item['작성일']}}</span>` : '';
          const dupListHtml = dupList.length > 0
            ? `<div class="dup-list" id="${{dupId}}" style="display:none">${{dupList.map(d => `
                <a href="${{d['링크']}}" target="_blank" class="dup-list-item">
                  <span>${{escapeHtml(d['제목'])}}</span>
                  ${{d['작성일'] ? `<span class="dup-list-date">${{d['작성일']}}</span>` : ''}}
                </a>
              `).join('')}}</div>`
            : '';

          return `
            <div class="item">
              <div class="item-top">
                <span class="badge ${{badgeClass}}">${{item['출처']}}</span>${{warningBadge}}${{newBadge}}${{streakBadge}}${{dupBadge}}${{dateTag}}
              </div>
              <a href="${{item['링크']}}" target="_blank" class="item-title">${{displayTitle}}</a>
              <div class="item-desc">${{displayDesc}}</div>
              ${{dupListHtml}}
            </div>
          `;
        }}

        function toggleDupList(badgeEl) {{
          const id = badgeEl.dataset.target;
          const list = document.getElementById(id);
          if (!list) return;
          const show = list.style.display === 'none';
          list.style.display = show ? 'flex' : 'none';
          badgeEl.textContent = show
            ? `유사보도 ${{badgeEl.dataset.count}}건 접기`
            : `유사보도 ${{badgeEl.dataset.count}}건 보기`;
        }}

        function renderCardBody(category, items, filterTerm) {{
          const isRisk = category.indexOf('위기징후') !== -1;
          if (isRisk) {{
            return items.map(item => renderItem(item, filterTerm)).join('');
          }}

          const buckets = {{}};
          items.forEach(item => {{
            const b = bucketFor(item['작성일']);
            if (!buckets[b]) buckets[b] = [];
            buckets[b].push(item);
          }});

          let html = '';
          bucketOrder.forEach(b => {{
            if (buckets[b] && buckets[b].length > 0) {{
              html += `<div class="bucket-header">${{b}}<span class="bucket-count">${{buckets[b].length}}</span></div>`;
              html += buckets[b].map(item => renderItem(item, filterTerm)).join('');
            }}
          }});
          return html;
        }}

        function renderStats() {{
          const statsRow = document.getElementById('statsRow');
          const total = rawData.length;
          const riskCount = rawData.filter(hasWarning).length;
          const newCount = rawData.filter(i => i['신규']).length;
          const categories = new Set(rawData.map(i => i['분류'])).size;
          const lastUpdate = total > 0 ? rawData[0]['수집일시'] : '-';

          statsRow.innerHTML = `
            <div class="stat-tile"><div class="label">총 수집 건수</div><div class="value">${{total}}</div></div>
            <div class="stat-tile risk"><div class="label">🚨 주의 신호</div><div class="value">${{riskCount}}</div></div>
            <div class="stat-tile new"><div class="label">🆕 신규 항목</div><div class="value">${{newCount}}</div></div>
            <div class="stat-tile"><div class="label">모니터링 카테고리</div><div class="value">${{categories}}</div></div>
            <div class="stat-tile"><div class="label">마지막 수집 일시</div><div class="value" style="font-size:1.05em;">${{lastUpdate}}</div></div>
            <div class="stat-tile clock"><div class="label">현재 시각</div><div class="value" id="liveClockValue">--:--:--</div></div>
          `;
        }}

        function tickClock() {{
          const el = document.getElementById('liveClockValue');
          if (!el) return;
          const now = new Date();
          const pad = n => String(n).padStart(2, '0');
          el.textContent = `${{pad(now.getHours())}}:${{pad(now.getMinutes())}}:${{pad(now.getSeconds())}}`;
        }}

        function renderDashboard(filterTerm) {{
          const term = (filterTerm || '').trim();
          const dashboard = document.getElementById('dashboard');
          const status = document.getElementById('filterStatus');

          if (rawData.length === 0) {{
            dashboard.innerHTML = '<div class="empty-state">// 수집된 브리핑 데이터가 없습니다 //</div>';
            status.classList.remove('active');
            return;
          }}

          const filteredData = term
            ? rawData.filter(item => {{
                const t = item['제목'].toLowerCase();
                const d = item['요약'].toLowerCase();
                const q = term.toLowerCase();
                return t.includes(q) || d.includes(q);
              }})
            : rawData;

          // 작성일(실제 게시일) 기준 최신순 정렬. 날짜가 없는 항목(카페글 등)은 맨 뒤로.
          const sourceData = [...filteredData].sort((a, b) => {{
            const da = a['작성일'] || '';
            const db = b['작성일'] || '';
            if (!da && !db) return 0;
            if (!da) return 1;
            if (!db) return -1;
            return db.localeCompare(da);
          }});

          if (term) {{
            status.classList.add('active');
            status.innerHTML = `🔍 '${{escapeHtml(term)}}' 필터 적용 중 — ${{sourceData.length}}건 표시 (전체 ${{rawData.length}}건 중)<span class="clear-link" id="filterClearLink">초기화</span>`;
            const clearLink = document.getElementById('filterClearLink');
            if (clearLink) clearLink.addEventListener('click', resetFilter);
          }} else {{
            status.classList.remove('active');
            status.innerHTML = '';
          }}

          if (sourceData.length === 0) {{
            dashboard.innerHTML = `<div class="empty-state">// '${{escapeHtml(term)}}' 검색 결과가 없습니다 //</div>`;
            return;
          }}

          const byColumn = {{}};
          sourceData.forEach(item => {{
            const col = item['컬럼'] || 'etc';
            if (!byColumn[col]) byColumn[col] = [];
            byColumn[col].push(item);
          }});

          dashboard.innerHTML = '';

          columnDefs.forEach(colDef => {{
            const colItems = byColumn[colDef.key] || [];

            const groupedData = colItems.reduce((acc, item) => {{
              if (!acc[item['분류']]) acc[item['분류']] = [];
              acc[item['분류']].push(item);
              return acc;
            }}, {{}});

            const section = document.createElement('section');
            section.className = 'column';

            let bodyHtml = '';
            if (colItems.length === 0) {{
              bodyHtml = '<div class="empty-state">// 해당 없음 //</div>';
            }} else {{
              for (const [category, items] of Object.entries(groupedData)) {{
                const cardBody = renderCardBody(category, items, term);
                bodyHtml += `
                  <div class="card">
                    <div class="card-header">
                      <h2>${{category}}</h2>
                      <span class="card-count">${{items.length}}</span>
                    </div>
                    <div class="card-body">${{cardBody}}</div>
                  </div>
                `;
              }}
            }}

            section.innerHTML = `
              <div class="column-head">${{colDef.title}}</div>
              <div class="column-body">${{bodyHtml}}</div>
            `;
            dashboard.appendChild(section);
          }});
        }}

        function applyFilter() {{
          const input = document.getElementById('filterInput');
          renderDashboard(input.value);
        }}

        function resetFilter() {{
          const input = document.getElementById('filterInput');
          input.value = '';
          renderDashboard('');
        }}

        function refreshAll() {{
          renderStats();
          const input = document.getElementById('filterInput');
          renderDashboard(input ? input.value : '');
        }}

        // --- 주의어 설정 모달 ---

        function openWarningSettings() {{
          renderWarningSettingsModal();
          document.getElementById('warningSettingsOverlay').classList.add('open');
        }}

        function closeWarningSettings() {{
          document.getElementById('warningSettingsOverlay').classList.remove('open');
        }}

        function renderWarningSettingsModal() {{
          const disabledSet = new Set(warningRuleState.disabled);
          let html = '';
          defaultWarningRules.forEach(rule => {{
            const key = ruleKey(rule);
            const isOff = disabledSet.has(key);
            html += `
              <div class="rule-row ${{isOff ? 'rule-off' : ''}}">
                <label class="rule-toggle">
                  <input type="checkbox" ${{isOff ? '' : 'checked'}} data-action="toggle-default" data-key="${{escapeHtml(key)}}">
                  <span>${{escapeHtml(rule.join(' + '))}}</span>
                </label>
                <span class="rule-tag">기본</span>
              </div>
            `;
          }});
          warningRuleState.custom.forEach((rule, idx) => {{
            html += `
              <div class="rule-row">
                <span class="rule-text">${{escapeHtml(rule.join(' + '))}}</span>
                <span class="rule-tag rule-tag-custom">추가됨</span>
                <button type="button" class="rule-remove-btn" data-action="remove-custom" data-idx="${{idx}}">삭제</button>
              </div>
            `;
          }});
          document.getElementById('warningRuleList').innerHTML = html || '<div class="empty-state">등록된 규칙이 없습니다</div>';
        }}

        function addCustomWarningRule() {{
          const input = document.getElementById('newRuleInput');
          const raw = input.value.trim();
          if (!raw) return;
          const rule = raw.split('+').map(s => s.trim()).filter(Boolean);
          if (rule.length === 0) return;
          warningRuleState.custom.push(rule);
          saveWarningRuleState(warningRuleState);
          recomputeWarningRules();
          input.value = '';
          renderWarningSettingsModal();
          refreshAll();
        }}

        function handleWarningModalClick(e) {{
          const toggleEl = e.target.closest('[data-action="toggle-default"]');
          if (toggleEl) {{
            const key = toggleEl.dataset.key;
            const disabledSet = new Set(warningRuleState.disabled);
            if (toggleEl.checked) {{ disabledSet.delete(key); }} else {{ disabledSet.add(key); }}
            warningRuleState.disabled = [...disabledSet];
            saveWarningRuleState(warningRuleState);
            recomputeWarningRules();
            renderWarningSettingsModal();
            refreshAll();
            return;
          }}
          const removeEl = e.target.closest('[data-action="remove-custom"]');
          if (removeEl) {{
            const idx = parseInt(removeEl.dataset.idx, 10);
            warningRuleState.custom.splice(idx, 1);
            saveWarningRuleState(warningRuleState);
            recomputeWarningRules();
            renderWarningSettingsModal();
            refreshAll();
          }}
        }}

        function renderBriefing() {{
          refreshAll();
          tickClock();
          setInterval(tickClock, 1000);

          document.getElementById('filterSearchBtn').addEventListener('click', applyFilter);
          document.getElementById('filterResetBtn').addEventListener('click', resetFilter);
          document.getElementById('filterInput').addEventListener('keydown', e => {{
            if (e.key === 'Enter') applyFilter();
          }});

          document.getElementById('dashboard').addEventListener('click', e => {{
            const badge = e.target.closest('.badge-dup');
            if (badge) toggleDupList(badge);
          }});

          document.getElementById('warningSettingsBtn').addEventListener('click', openWarningSettings);
          document.getElementById('warningSettingsCloseBtn').addEventListener('click', closeWarningSettings);
          document.getElementById('warningSettingsOverlay').addEventListener('click', e => {{
            if (e.target.id === 'warningSettingsOverlay') closeWarningSettings();
          }});
          document.getElementById('warningRuleList').addEventListener('click', handleWarningModalClick);
          document.getElementById('addRuleBtn').addEventListener('click', addCustomWarningRule);
          document.getElementById('newRuleInput').addEventListener('keydown', e => {{
            if (e.key === 'Enter') addCustomWarningRule();
          }});
        }}

        window.onload = renderBriefing;
      </script>
    </body>
    </html>
    """

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)

    print(f"완료: 총 {len(all_collected_data)}건의 데이터로 'index.html'이 갱신되었습니다.")
    print("-" * 50)


if os.environ.get("GITHUB_ACTIONS") == "true":
    # GitHub Actions에서는 워크플로우의 cron이 반복 실행을 담당하므로
    # 스크립트 자신은 1회만 수집하고 종료한다.
    run_briefing()
else:
    # 로컬 실행: 지정된 시각마다 반복 수집하는 상시 스케줄러로 동작.
    target_times = ["10:00", "14:00", "17:40"]

    print("==================================================")
    print(f"자동 수집 스케줄러가 시작되었습니다. (종료하려면 Ctrl+C)")
    print(f"설정된 수집 시간: {', '.join(target_times)}")
    print("==================================================")

    # 최초 1회 즉시 실행 (상태 확인용)
    run_briefing()

    # 설정된 시간에 도달하면 실행하는 무한 루프
    while True:
        current_time = datetime.now().strftime("%H:%M")

        if current_time in target_times:
            run_briefing()
            time.sleep(61) # 중복 실행을 막기 위해 1분 대기

        time.sleep(10) # 10초마다 시간 확인