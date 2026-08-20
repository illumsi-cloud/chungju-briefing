import urllib.request
import urllib.parse
import json
import ssl
import re
import os
import http.cookiejar
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import time

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
    "유치원", "어린이집", "학급", "교실"
]

def matches_topic(text):
    return any(kw in text for kw in TOPIC_KEYWORDS)

# 충주시청 게시판(공지사항/고시공고 등) 자체 검색에 사용할 대표 키워드.
# 게시판이 활발해서(하루 수건) "최신 페이지 1건"만 가져오면 몇 주 전 글이 이미
# 밀려나 버리므로, 반드시 게시판 자체 검색으로 찾아야 한다.
SEARCH_KEYWORDS = ["개발", "지구단위", "도시계획", "아파트", "주택", "택지", "학교", "교육", "학생", "산업단지"]

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
        {"category": "🚨 [위기징후] 건설사·아파트 리스크", "type": "news", "queries": ["충주 아파트 시공사 부도", "충주 아파트 건설사 회생", "충주 아파트 공사중단", "충주 아파트 시행사 법정관리", "충주 아파트 허그 보증금"]},
        {"category": "🗣️ [지역여론] 커뮤니티 (분양/갈등)", "type": "cafearticle", "queries": ["충주 분양 전환", "충주 허그 보증금", "충주 분양 지연", "충주 아파트 취소"]},
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
                        found_count += 1
                        all_collected_data.append({
                            "수집일시": now_str,
                            "분류": target["category"],
                            "출처": "뉴스" if target["type"] == "news" else "카페",
                            "제목": clean_html(item.get("title", "")),
                            "요약": clean_html(item.get("description", "")),
                            "작성일": date_val,
                            "링크": link
                        })
            except Exception as e:
                print(f" [오류] 네이버 수집 실패 ({target['category']} / {query}): {e}")

        print(f" - {target['category']}: {found_count}건 발견")

    # 1-1. HUG(주택도시보증공사) 문의/민원 게시판 - 보증사고·반환지연 등 1차 민원 데이터
    hug_category = "🚨 [위기징후] HUG 보증사고·반환지연 민원"
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

    # 3. 충주시청 지구단위계획 고시자료 게시판
    chungju_url = "https://www.chungju.go.kr/www/selectBbsNttList.do?bbsNo=289&key=3267"
    chungju_row_pattern = re.compile(
        r'<td class="first">(?P<no>\d+)</td>\s*'
        r'<td class="subject">\s*.*?'
        r'<a href="\./selectBbsNttView\.do\?key=3267&amp;bbsNo=289&amp;nttNo=(?P<nttno>\d+)&amp;[^"]*">'
        r'(?P<title>[^<]*)</a>.*?'
        r'<td[^>]*>(?P<views>\d+)</td>\s*'
        r'<td class="last">(?P<date>[\d-]+)</td>',
        re.S
    )
    try:
        req = urllib.request.Request(chungju_url, headers={"User-Agent": "Mozilla/5.0"})
        response = urllib.request.urlopen(req, context=ctx)
        html = response.read().decode('utf-8')

        chungju_count = 0
        for m in chungju_row_pattern.finditer(html):
            detail_url = (
                "https://www.chungju.go.kr/www/selectBbsNttView.do?"
                f"key=3267&bbsNo=289&nttNo={m.group('nttno')}"
            )
            chungju_count += 1
            all_collected_data.append({
                "수집일시": now_str,
                "분류": "📐 [지구단위계획] 고시자료 (충주시청)",
                "출처": "공식(충주시청)",
                "제목": clean_html(m.group('title')),
                "요약": f"조회수 {m.group('views').strip()} · 작성일 {m.group('date').strip()}",
                "작성일": m.group('date').strip(),
                "링크": detail_url
            })
        print(f" - 📐 [지구단위계획] 고시자료: {chungju_count}건 발견")
    except Exception as e:
        print(f" [오류] 충주시청 게시판 수집 실패: {e}")

    # 4. 충주시청 공지사항 / 유관기관 공지사항 / 입법예고 / 공고·고시·입찰
    chungju_boards = [
        {"fn": fetch_chungju_bbs, "args": (506, 5), "category": "📢 [공지사항] 충주시청"},
        {"fn": fetch_chungju_bbs, "args": (507, 10), "category": "📢 [유관기관] 공지사항"},
        {"fn": fetch_chungju_eminwon, "args": (509, "03"), "category": "📜 [입법예고] 충주시청"},
        {"fn": fetch_chungju_eminwon, "args": (510, "01,02,04,05"), "category": "📋 [공고/고시/입찰] 충주시청"},
    ]
    for board in chungju_boards:
        items = board["fn"](*board["args"], board["category"], now_str)
        all_collected_data.extend(items)
        print(f" - {board['category']}: {len(items)}건 발견")

    # 5. 지난 실행 대비 신규 항목 판정
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
      <title>충주 동향 인텔리전스 터미널</title>
      <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%99%EF%B8%8F%3C/text%3E%3C/svg%3E">
      <meta name="description" content="충주시 개발·부동산·학교 동향을 자동 수집하는 대시보드 (GitHub Actions로 하루 3회 자동 갱신)">
      <style>
        :root {{
          --bg: #060a13;
          --bg-elevated: #0d1526;
          --bg-elevated-2: #101b30;
          --border: rgba(148, 178, 224, 0.14);
          --border-strong: rgba(148, 178, 224, 0.28);
          --text-primary: #e7edf8;
          --text-secondary: #8a97ad;
          --text-dim: #5b6478;
          --accent-cyan: #22d3ee;
          --accent-green: #34d399;
          --accent-red: #fb4b6b;
          --accent-amber: #fbbf24;
          --accent-purple: #a78bfa;
          --accent-blue: #60a5fa;
          --mono: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
          --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Pretendard', 'Malgun Gothic', Roboto, sans-serif;
        }}

        * {{ box-sizing: border-box; }}

        body {{
          margin: 0;
          font-family: var(--sans);
          background:
            radial-gradient(circle at 15% 0%, rgba(34, 211, 238, 0.07), transparent 45%),
            radial-gradient(circle at 85% 15%, rgba(167, 139, 250, 0.06), transparent 40%),
            var(--bg);
          color: var(--text-primary);
          padding: 0 0 40px 0;
          min-height: 100vh;
        }}

        .topbar {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 18px 28px;
          border-bottom: 1px solid var(--border);
          background: rgba(9, 14, 26, 0.85);
          position: sticky;
          top: 0;
          z-index: 10;
          backdrop-filter: blur(10px);
        }}

        .brand {{ display: flex; align-items: center; gap: 14px; }}

        .brand-mark {{
          width: 42px; height: 42px;
          border-radius: 10px;
          display: flex; align-items: center; justify-content: center;
          font-family: var(--mono);
          font-weight: 700;
          font-size: 0.95em;
          background: linear-gradient(135deg, rgba(34,211,238,0.18), rgba(167,139,250,0.18));
          border: 1px solid var(--border-strong);
          color: var(--accent-cyan);
          letter-spacing: 0.5px;
        }}

        .brand h1 {{
          margin: 0;
          font-size: 1.15em;
          font-weight: 700;
          letter-spacing: -0.2px;
        }}

        .brand .sub {{
          font-family: var(--mono);
          font-size: 0.72em;
          color: var(--text-dim);
          letter-spacing: 0.5px;
          margin-top: 2px;
        }}

        .live-indicator {{
          display: flex; align-items: center; gap: 8px;
          font-family: var(--mono);
          font-size: 0.78em;
          color: var(--accent-green);
          border: 1px solid rgba(52, 211, 153, 0.3);
          background: rgba(52, 211, 153, 0.08);
          padding: 6px 12px;
          border-radius: 999px;
        }}

        .live-indicator .dot {{
          width: 7px; height: 7px; border-radius: 50%;
          background: var(--accent-green);
          box-shadow: 0 0 8px var(--accent-green);
          animation: pulse 1.6s ease-in-out infinite;
        }}

        @keyframes pulse {{
          0%, 100% {{ opacity: 1; transform: scale(1); }}
          50% {{ opacity: 0.4; transform: scale(0.8); }}
        }}

        .ticker-wrap {{
          border-bottom: 1px solid var(--border);
          background: var(--bg-elevated);
          overflow: hidden;
          white-space: nowrap;
          padding: 10px 0;
        }}

        .ticker-track {{
          display: inline-block;
          white-space: nowrap;
          font-family: var(--mono);
          font-size: 0.82em;
          color: var(--text-secondary);
          animation: ticker-scroll 90s linear infinite;
          padding-left: 100%;
        }}

        .ticker-track .seg {{ margin-right: 40px; }}
        .ticker-track .seg.warn {{ color: var(--accent-red); font-weight: 600; }}

        @keyframes ticker-scroll {{
          0% {{ transform: translateX(0); }}
          100% {{ transform: translateX(-100%); }}
        }}

        .stats-row {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 14px;
          padding: 22px 28px 0 28px;
        }}

        .stat-tile {{
          background: var(--bg-elevated);
          border: 1px solid var(--border);
          border-radius: 12px;
          padding: 16px 18px;
        }}

        .stat-tile .label {{
          font-size: 0.72em;
          color: var(--text-dim);
          letter-spacing: 0.5px;
          text-transform: uppercase;
          margin-bottom: 8px;
        }}

        .stat-tile .value {{
          font-family: var(--mono);
          font-size: 1.6em;
          font-weight: 700;
          color: var(--text-primary);
        }}

        .stat-tile.risk .value {{ color: var(--accent-red); text-shadow: 0 0 16px rgba(251, 75, 107, 0.35); }}
        .stat-tile.new .value {{ color: var(--accent-green); text-shadow: 0 0 16px rgba(52, 211, 153, 0.35); }}
        .stat-tile.clock .value {{ color: var(--accent-cyan); font-size: 1.4em; }}

        .grid {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
          gap: 18px;
          padding: 22px 28px;
          align-items: start;
        }}

        .card {{
          background: var(--bg-elevated);
          border: 1px solid var(--border);
          border-radius: 14px;
          overflow: hidden;
          transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
        }}

        .card:hover {{
          border-color: var(--card-accent, var(--accent-cyan));
          box-shadow: 0 0 0 1px var(--card-accent, var(--accent-cyan)) inset, 0 12px 32px -16px var(--card-accent, var(--accent-cyan));
          transform: translateY(-2px);
        }}

        .card::before {{
          content: '';
          display: block;
          height: 3px;
          background: var(--card-accent, var(--accent-cyan));
          box-shadow: 0 0 12px var(--card-accent, var(--accent-cyan));
        }}

        .card-header {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 14px 18px;
          border-bottom: 1px solid var(--border);
        }}

        .card-header h2 {{
          margin: 0;
          font-size: 0.98em;
          font-weight: 700;
          color: var(--text-primary);
        }}

        .card-count {{
          font-family: var(--mono);
          font-size: 0.75em;
          color: var(--card-accent, var(--accent-cyan));
          background: color-mix(in srgb, var(--card-accent, var(--accent-cyan)) 14%, transparent);
          border: 1px solid var(--card-accent, var(--accent-cyan));
          border-radius: 999px;
          padding: 2px 9px;
        }}

        .card-body {{
          max-height: 460px;
          overflow-y: auto;
          padding: 6px 18px 4px 18px;
        }}

        .card-body::-webkit-scrollbar {{ width: 7px; }}
        .card-body::-webkit-scrollbar-track {{ background: transparent; }}
        .card-body::-webkit-scrollbar-thumb {{
          background: var(--border-strong);
          border-radius: 10px;
        }}
        .card-body::-webkit-scrollbar-thumb:hover {{ background: var(--card-accent, var(--accent-cyan)); }}

        .item {{
          padding: 12px 0;
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
          padding: 2px 8px;
          font-size: 0.68em;
          font-family: var(--mono);
          border-radius: 5px;
          font-weight: 600;
          letter-spacing: 0.3px;
          border: 1px solid transparent;
        }}

        .badge-news {{ background: rgba(96, 165, 250, 0.12); color: var(--accent-blue); border-color: rgba(96, 165, 250, 0.3); }}
        .badge-cafe {{ background: rgba(251, 191, 36, 0.12); color: var(--accent-amber); border-color: rgba(251, 191, 36, 0.3); }}
        .badge-warning {{
          background: rgba(251, 75, 107, 0.14);
          color: var(--accent-red);
          border-color: rgba(251, 75, 107, 0.4);
          text-shadow: 0 0 8px rgba(251, 75, 107, 0.4);
        }}

        .badge-new {{
          background: rgba(52, 211, 153, 0.14);
          color: var(--accent-green);
          border-color: rgba(52, 211, 153, 0.45);
          text-shadow: 0 0 8px rgba(52, 211, 153, 0.5);
          animation: new-pulse 1.8s ease-in-out infinite;
        }}

        @keyframes new-pulse {{
          0%, 100% {{ box-shadow: 0 0 0 rgba(52, 211, 153, 0); }}
          50% {{ box-shadow: 0 0 10px rgba(52, 211, 153, 0.55); }}
        }}

        .badge-streak {{
          background: rgba(167, 139, 250, 0.12);
          color: var(--accent-purple);
          border-color: rgba(167, 139, 250, 0.35);
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
          font-size: 0.93em;
        }}

        .item-title:hover {{ color: var(--accent-cyan); }}

        .item-desc {{
          font-size: 0.82em;
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
          background: rgba(251, 75, 107, 0.14);
          padding: 0 3px;
          border-radius: 3px;
          text-shadow: 0 0 6px rgba(251, 75, 107, 0.35);
        }}

        .highlight-search {{
          color: #0b1220;
          font-weight: 700;
          background: var(--accent-cyan);
          padding: 0 3px;
          border-radius: 3px;
          box-shadow: 0 0 10px rgba(34, 211, 238, 0.55);
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
          background: var(--bg-elevated);
          border: 1px solid var(--border);
          border-radius: 10px;
          padding: 8px 12px;
          flex: 1;
          min-width: 240px;
        }}

        .filter-input-wrap:focus-within {{
          border-color: var(--accent-cyan);
          box-shadow: 0 0 0 1px var(--accent-cyan);
        }}

        .filter-prompt {{
          font-family: var(--mono);
          color: var(--accent-cyan);
          font-size: 0.9em;
        }}

        #filterInput {{
          flex: 1;
          background: transparent;
          border: none;
          outline: none;
          color: var(--text-primary);
          font-family: var(--mono);
          font-size: 0.9em;
        }}

        #filterInput::placeholder {{ color: var(--text-dim); }}

        .filter-btn {{
          font-family: var(--mono);
          font-size: 0.8em;
          font-weight: 700;
          letter-spacing: 0.3px;
          border-radius: 8px;
          padding: 10px 16px;
          cursor: pointer;
          border: 1px solid transparent;
          transition: filter 0.15s ease, transform 0.1s ease;
        }}

        .filter-btn:active {{ transform: scale(0.97); }}

        .filter-btn-search {{
          background: var(--accent-cyan);
          color: #05131a;
          border-color: var(--accent-cyan);
        }}

        .filter-btn-search:hover {{ filter: brightness(1.1); }}

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
          color: var(--accent-cyan);
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
          font-size: 0.7em;
          letter-spacing: 0.6px;
          color: var(--card-accent, var(--accent-cyan));
          text-transform: uppercase;
          padding: 14px 0 6px 0;
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
          padding: 60px 28px;
          text-align: center;
          color: var(--text-dim);
          font-family: var(--mono);
        }}

        footer {{
          text-align: center;
          padding: 28px 20px 24px 20px;
          color: var(--text-dim);
          font-family: var(--mono);
          font-size: 0.72em;
          letter-spacing: 0.4px;
          border-top: 1px solid var(--border);
          margin-top: 12px;
        }}

        footer .auto-badge {{
          display: inline-flex;
          align-items: center;
          gap: 6px;
          color: var(--accent-green);
          border: 1px solid rgba(52, 211, 153, 0.3);
          background: rgba(52, 211, 153, 0.08);
          padding: 5px 12px;
          border-radius: 999px;
          margin-bottom: 12px;
        }}

        footer .sources {{
          color: var(--text-secondary);
          line-height: 1.8;
          max-width: 720px;
          margin: 0 auto;
        }}

        @media (max-width: 640px) {{
          .stats-row, .grid, .filter-bar, .filter-status {{ padding-left: 14px; padding-right: 14px; }}
          .topbar {{ padding: 14px; flex-wrap: wrap; gap: 10px; }}
          .brand h1 {{ font-size: 1em; }}
          .grid {{ grid-template-columns: 1fr; }}
        }}
      </style>
    </head>
    <body>
      <div class="topbar">
        <div class="brand">
          <div class="brand-mark">CJ</div>
          <div>
            <h1>충주 동향 인텔리전스</h1>
            <div class="sub">CHUNGJU REGIONAL INTELLIGENCE TERMINAL</div>
          </div>
        </div>
        <div class="live-indicator"><span class="dot"></span>LIVE</div>
      </div>

      <div class="ticker-wrap">
        <div class="ticker-track" id="ticker"></div>
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
        <div class="grid" id="dashboard"></div>
      </div>

      <footer>
        <div class="auto-badge">⚙️ GitHub Actions로 매일 10:00 · 14:00 · 17:40 자동 갱신</div>
        <div class="sources">
          충주시 개발·부동산·학교 관련 공공데이터를 자동 수집하는 대시보드입니다.<br>
          출처: 네이버 뉴스·카페 · 충주시청(공지사항·지구단위계획·고시공고) · 자치법규정보시스템(elis.go.kr) ·
          주택도시보증공사(HUG) · 토지이음(eum.go.kr)<br>
          담당자 확인용 참고 자료이며, 법적 효력이 있는 공식 공고는 각 기관 원문을 반드시 확인하세요.
        </div>
      </footer>

      <script>
        const rawData = {json_data_str};
        const warningKeywords = ['반대', '민원', '지연', '갈등', '우려', '취소', '투기', '하락', '논란', '피해'];
        const palette = ['#fb4b6b', '#fbbf24', '#60a5fa', '#a78bfa', '#22d3ee', '#34d399', '#f472b6', '#38bdf8', '#facc15', '#c084fc', '#4ade80'];
        const categoryColor = {{}};

        function colorFor(category) {{
          if (!(category in categoryColor)) {{
            categoryColor[category] = palette[Object.keys(categoryColor).length % palette.length];
          }}
          return categoryColor[category];
        }}

        function hasWarning(item) {{
          return warningKeywords.some(kw => item['제목'].includes(kw) || item['요약'].includes(kw));
        }}

        function highlightWarning(text) {{
          let resultText = text;
          warningKeywords.forEach(keyword => {{
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

        function renderItem(item, filterTerm) {{
          const badgeClass = item['출처'] === '뉴스' ? 'badge-news' : 'badge-cafe';
          const warningBadge = hasWarning(item) ? `<span class="badge badge-warning">🚨 주의</span>` : '';
          const newBadge = item['신규'] ? `<span class="badge badge-new">🆕 NEW</span>` : '';
          const streakBadge = (!item['신규'] && item['발견일수'] >= 2)
            ? `<span class="badge badge-streak">🔁 ${{item['발견일수']}}일째</span>`
            : '';
          let displayTitle = highlightWarning(escapeHtml(item['제목']));
          let displayDesc = highlightWarning(escapeHtml(item['요약']));
          if (filterTerm) {{
            displayTitle = highlightSearch(displayTitle, filterTerm);
            displayDesc = highlightSearch(displayDesc, filterTerm);
          }}
          const dateTag = item['작성일'] ? `<span class="item-date-tag">${{item['작성일']}}</span>` : '';

          return `
            <div class="item">
              <div class="item-top">
                <span class="badge ${{badgeClass}}">${{item['출처']}}</span>${{warningBadge}}${{newBadge}}${{streakBadge}}${{dateTag}}
              </div>
              <a href="${{item['링크']}}" target="_blank" class="item-title">${{displayTitle}}</a>
              <div class="item-desc">${{displayDesc}}</div>
            </div>
          `;
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

        function renderTicker() {{
          const ticker = document.getElementById('ticker');
          if (rawData.length === 0) {{ ticker.innerHTML = '<span class="seg">수집된 데이터가 없습니다</span>'; return; }}

          const warnings = rawData.filter(hasWarning).slice(0, 15);
          const source = (warnings.length > 0 ? warnings : rawData).slice(0, 20);
          const segs = source.map(item => {{
            const cls = hasWarning(item) ? 'seg warn' : 'seg';
            const mark = hasWarning(item) ? '🔴 ' : '● ';
            return `<span class="${{cls}}">${{mark}}${{escapeHtml(item['제목'])}}</span>`;
          }}).join('');
          ticker.innerHTML = segs + segs;
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

          const sourceData = term
            ? rawData.filter(item => {{
                const t = item['제목'].toLowerCase();
                const d = item['요약'].toLowerCase();
                const q = term.toLowerCase();
                return t.includes(q) || d.includes(q);
              }})
            : rawData;

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

          const groupedData = sourceData.reduce((acc, item) => {{
            if (!acc[item['분류']]) acc[item['분류']] = [];
            acc[item['분류']].push(item);
            return acc;
          }}, {{}});

          dashboard.innerHTML = '';

          for (const [category, items] of Object.entries(groupedData)) {{
            const accent = colorFor(category);
            const card = document.createElement('div');
            card.className = 'card';
            card.style.setProperty('--card-accent', accent);

            const bodyHtml = renderCardBody(category, items, term);

            card.innerHTML = `
              <div class="card-header">
                <h2>${{category}}</h2>
                <span class="card-count">${{items.length}}</span>
              </div>
              <div class="card-body">${{bodyHtml}}</div>
            `;
            dashboard.appendChild(card);
          }}
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

        function renderBriefing() {{
          renderTicker();
          renderStats();
          tickClock();
          setInterval(tickClock, 1000);

          renderDashboard('');

          document.getElementById('filterSearchBtn').addEventListener('click', applyFilter);
          document.getElementById('filterResetBtn').addEventListener('click', resetFilter);
          document.getElementById('filterInput').addEventListener('keydown', e => {{
            if (e.key === 'Enter') applyFilter();
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