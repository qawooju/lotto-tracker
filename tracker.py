"""로또 번호 추천 — 역대 당첨 빈도 기반 분석 + Slack 발송."""

import json
import os
import random
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 경로 / 환경변수 ─────────────────────────────────────────────────────
ROOT = Path(__file__).parent
STATE_PATH = ROOT / "states" / "lotto_state.json"

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0AK0SZ2KK5")

KST = timezone(timedelta(hours=9))

# 추천 게임 수
NUM_GAMES = 5


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def api_get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  API 오류: {e}")
        return None


def slack_post(blocks, thread_ts=None):
    payload = {
        "channel": SLACK_CHANNEL,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"  Slack API 오류: {body.get('error')}")
                return None
            return body.get("ts")
    except urllib.error.HTTPError as e:
        print(f"  Slack 오류: {e.code} {e.reason}")
        return None


# ── 로또 당첨 데이터 수집 ─────────────────────────────────────────────────
# smok95/lotto GitHub Pages — 빠르고 안정적인 로또 데이터 소스
LOTTO_ALL_URL = "https://smok95.github.io/lotto/results/all.json"
LOTTO_LATEST_URL = "https://smok95.github.io/lotto/results/latest.json"


def fetch_lotto_history(state):
    """역대 당첨 번호 수집 (GitHub Pages → 전체 데이터 한 번에)"""
    cached = state.get("history", {})

    # all.json으로 전체 데이터 한 번에 가져오기
    print("  smok95/lotto 데이터 조회...")
    all_data = api_get(LOTTO_ALL_URL, timeout=30)

    if all_data and isinstance(all_data, list):
        for item in all_data:
            rnd = item.get("draw_no")
            nums = item.get("numbers")
            bonus = item.get("bonus_no")
            date_str = item.get("date", "")
            if rnd and nums:
                cached[str(rnd)] = {
                    "numbers": sorted(nums),
                    "bonus": bonus,
                    "date": date_str[:10] if date_str else "",
                }
        print(f"  전체 {len(cached)}회차 로드 완료")
    else:
        # fallback: latest만 가져오기
        print("  all.json 실패, latest.json 시도...")
        latest = api_get(LOTTO_LATEST_URL, timeout=30)
        if latest:
            rnd = latest.get("draw_no")
            nums = latest.get("numbers")
            cached[str(rnd)] = {
                "numbers": sorted(nums),
                "bonus": latest.get("bonus_no"),
                "date": latest.get("date", "")[:10],
            }
        if not cached:
            print("  데이터 수집 실패!")

    # 최근 100회차만 유지
    if len(cached) > 100:
        sorted_keys = sorted(cached.keys(), key=int)
        for k in sorted_keys[:-100]:
            del cached[k]

    print(f"  분석 대상: {len(cached)}회차")
    return cached


# ── 분석 & 추천 ───────────────────────────────────────────────────────────
def analyze_frequency(history):
    """번호별 출현 빈도 분석"""
    counter = Counter()
    pair_counter = Counter()

    for rnd_data in history.values():
        nums = rnd_data["numbers"]
        for n in nums:
            counter[n] += 1
        # 번호 쌍 빈도
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                pair_counter[(nums[i], nums[j])] += 1

    return counter, pair_counter


# 다양성 제약 — 매주 같은 번호만 반복되지 않게
MAX_GAMES_PER_NUMBER = 2   # 한 주 5게임 안에서 한 번호가 들어갈 수 있는 게임 수
MAX_OVERLAP_IN_WEEK = 2    # 같은 주 게임끼리 겹쳐도 되는 번호 수
MAX_OVERLAP_RECENT = 3     # 최근 추천 조합과 겹쳐도 되는 번호 수 (4개 이상 겹치면 다시 뽑음)
RECENT_WEEKS = 4           # 최근 몇 주 추천을 기억할지
HOT_POOL_SIZE = 20         # 핫넘버 풀 (20위와 동률까지 포함)


def _weighted_sample(population, weights, k):
    """가중치 비복원 추출"""
    pop, w = list(population), list(weights)
    picked = []
    for _ in range(min(k, len(pop))):
        i = random.choices(range(len(pop)), weights=w, k=1)[0]
        picked.append(pop.pop(i))
        w.pop(i)
    return picked


def _is_diverse(nums, games, usage, recent, strict=True):
    """주간 다양성·최근 반복 제약 확인"""
    if any(usage[n] >= MAX_GAMES_PER_NUMBER for n in nums):
        return False
    if any(len(set(nums) & set(g)) > MAX_OVERLAP_IN_WEEK for g in games):
        return False
    if strict and any(len(set(nums) & set(r)) > MAX_OVERLAP_RECENT for r in recent):
        return False
    return True


def generate_recommendations(counter, pair_counter, num_games=5, recent=None):
    """빈도 기반 가중치 추천 번호 생성 (주간 다양성 + 최근 반복 회피)"""
    recent = recent or []
    games = []
    usage = Counter()
    all_numbers = list(range(1, 46))

    # 빈도를 가중치로 변환
    weights = [counter.get(n, 0) + 1 for n in all_numbers]

    # 핫넘버: 상위 HOT_POOL_SIZE 위와 동률까지 포함 (순위 경계가 동률이라 고정 15개는 의미 없음)
    ranked = counter.most_common()
    cut = ranked[min(HOT_POOL_SIZE, len(ranked)) - 1][1]
    hot_numbers = [n for n, c in ranked if c >= cut]
    hot_weights = [counter[n] for n in hot_numbers]
    # 콜드넘버: 하위 10개
    cold_numbers = [n for n, _ in ranked[-10:]]
    cold_weights = [1 / (counter[n] + 1) for n in cold_numbers]
    # 상위 쌍 30개 (빈도 비례로 뽑음)
    top_pairs = pair_counter.most_common(30)

    def draw(game_idx):
        if game_idx == 0:
            # 게임 1: 핫넘버 중심 (빈도 비례)
            return _weighted_sample(hot_numbers, hot_weights, 6)
        if game_idx == 1:
            # 게임 2: 핫 4개 + 콜드 2개
            hot_pick = _weighted_sample(hot_numbers, hot_weights, 4)
            cold = [n for n in cold_numbers if n not in hot_pick]
            cold_pick = _weighted_sample(cold, [cold_weights[cold_numbers.index(n)] for n in cold], 2)
            return hot_pick + cold_pick
        if game_idx == 2:
            # 게임 3: 자주 같이 나온 쌍 3개를 빈도 비례로 뽑아 조합
            picked = []
            pairs = [p for p, _ in top_pairs]
            pw = [c for _, c in top_pairs]
            for a, b in _weighted_sample(pairs, pw, len(pairs)):
                if len(picked) >= 6:
                    break
                if a not in picked and b not in picked and len(picked) <= 4:
                    picked += [a, b]
            while len(picked) < 6:
                n = random.choices(hot_numbers, weights=hot_weights, k=1)[0]
                if n not in picked:
                    picked.append(n)
            return picked
        # 게임 4, 5: 전체 번호 빈도 가중치 랜덤
        return _weighted_sample(all_numbers, weights, 6)

    for game_idx in range(num_games):
        nums = None
        # 엄격 조건으로 먼저 시도, 안 되면 최근 반복 조건만 풀고, 그래도 안 되면 그냥 뽑음
        for strict in (True, False):
            for _ in range(500):
                cand = sorted(draw(game_idx))
                if len(set(cand)) == 6 and _is_diverse(cand, games, usage, recent, strict):
                    nums = cand
                    break
            if nums:
                break
        if not nums:
            nums = sorted(draw(game_idx))
        usage.update(nums)
        games.append(nums)

    return games


def get_strategy_name(idx):
    """추천 전략명"""
    names = [
        "최다 출현 번호 조합",
        "다빈도 + 저빈도 혼합",
        "자주 함께 당첨된 번호 조합",
        "가중치 랜덤 A",
        "가중치 랜덤 B",
    ]
    return names[idx] if idx < len(names) else f"추천 {idx + 1}"


# ── 메시지 빌드 ───────────────────────────────────────────────────────────
def format_numbers(nums):
    """번호를 포맷"""
    return " \u2003".join(f"`{n}`" for n in nums)


def generate_ai_picks(counter, history):
    """사주팔자 + 오늘의 운세 기반 AI 추천 (1978년 음력 5월 2일생 남성, 戊午年)"""
    games = []
    all_numbers = list(range(1, 46))
    weights = [counter.get(n, 0) + 1 for n in all_numbers]
    today = datetime.now(KST)

    # ── 사주팔자 기반 행운 번호 ──
    # 戊午年(무오년) = 천간 戊(토) + 지지 午(화)
    # 오행 숫자: 화(火)=2,7 / 토(土)=5,10 / 금(金)=4,9 / 수(水)=1,6 / 목(木)=3,8
    # 본명 오행: 토+화 → 재물운 금(4,9), 식상운 금(4,9), 관운 목(3,8)
    SAJU_CORE = [2, 5, 7, 10]         # 본명 오행 (화+토 계열)
    SAJU_WEALTH = [4, 9, 14, 19]      # 재물운 (금 계열)
    SAJU_CAREER = [3, 8, 13, 18]      # 관운/성취운 (목 계열)

    # 생년월일 파생 행운번호 (78, 5, 2 → 7,8,5,2 + 합산)
    BIRTH_NUMS = [2, 5, 7, 8, 15, 25, 35, 42]  # 생년월일 숫자 조합

    # 차/여행 취미 → 이동수 (驛馬) = 午의 역마: 申(8월=8계열)
    HOBBY_NUMS = [8, 18, 28, 38]  # 역마살(이동운) 기반

    # 오늘의 운세 시드: 날짜 기반 행운 오프셋
    day_seed = today.year * 10000 + today.month * 100 + today.day
    daily_offset = day_seed % 45 + 1  # 1~45

    # 역대 당첨 합계 범위
    sums = [sum(d["numbers"]) for d in history.values()]
    avg_sum = sum(sums) / len(sums)
    min_sum = int(avg_sum - 30)
    max_sum = int(avg_sum + 30)

    strategies = [
        "사주팔자 (戊午년 토+화 오행 조합)",
        "재물운 (금 오행 + 생년월일 행운수)",
        "역마운 (이동·여행·차 기운 반영)",
        "오늘의 운세 (일진 행운 오프셋)",
        "사주 종합 (오행 밸런스 + 합계 최적화)",
    ]

    for idx in range(5):
        attempts = 0
        while attempts < 200:
            attempts += 1

            if idx == 0:
                # 사주팔자: 본명 오행(화+토) 숫자 우선
                saju_w = weights[:]
                for n in all_numbers:
                    if n in SAJU_CORE or n % 10 in [2, 5, 7, 0]:
                        saju_w[n - 1] *= 3
                    if n in BIRTH_NUMS:
                        saju_w[n - 1] *= 2
                nums = sorted(random.choices(all_numbers, weights=saju_w, k=6))
                if len(set(nums)) != 6:
                    continue
                nums = sorted(set(nums))[:6]

            elif idx == 1:
                # 재물운: 금 오행(4,9계열) + 생년월일 수
                pool = list(set(SAJU_WEALTH + BIRTH_NUMS))
                pool = [n for n in pool if 1 <= n <= 45]
                if len(pool) < 4:
                    pool = SAJU_WEALTH[:]
                picks = random.sample(pool, min(4, len(pool)))
                remaining = [n for n in all_numbers if n not in picks]
                rem_w = [weights[n - 1] for n in remaining]
                extras = random.choices(remaining, weights=rem_w, k=6 - len(picks))
                nums = sorted(set(picks + extras))
                if len(nums) != 6:
                    continue

            elif idx == 2:
                # 역마운(이동/여행/차): 역마 숫자(8계열) + 오행 조합
                hobby_w = weights[:]
                for n in all_numbers:
                    if n in HOBBY_NUMS:
                        hobby_w[n - 1] *= 4
                    if n in SAJU_CORE:
                        hobby_w[n - 1] *= 2
                # 반드시 역마 숫자 1개 이상 포함
                must = random.choice(HOBBY_NUMS)
                picks = [must]
                remaining = [n for n in all_numbers if n != must]
                rem_w = [hobby_w[n - 1] for n in remaining]
                extras = random.choices(remaining, weights=rem_w, k=5)
                nums = sorted(set(picks + extras))
                if len(nums) != 6:
                    continue

            elif idx == 3:
                # 오늘의 운세: 일진 기반 오프셋 적용
                lucky_base = [daily_offset]
                for delta in [7, 14, 21, 28, 35]:
                    n = (daily_offset + delta - 1) % 45 + 1
                    lucky_base.append(n)
                # 행운 번호에 가중치 부여
                daily_w = weights[:]
                for n in lucky_base:
                    daily_w[n - 1] *= 3
                for n in BIRTH_NUMS:
                    if 1 <= n <= 45:
                        daily_w[n - 1] *= 2
                nums = sorted(random.choices(all_numbers, weights=daily_w, k=6))
                if len(set(nums)) != 6:
                    continue
                nums = sorted(set(nums))[:6]

            elif idx == 4:
                # 사주 종합: 오행 밸런스 + 합계 최적화
                balanced_w = weights[:]
                for n in all_numbers:
                    if n in SAJU_CORE:
                        balanced_w[n - 1] *= 2
                    if n in SAJU_WEALTH:
                        balanced_w[n - 1] *= 2
                    if n in BIRTH_NUMS:
                        balanced_w[n - 1] *= 2
                    if n in HOBBY_NUMS:
                        balanced_w[n - 1] *= 1.5
                nums = sorted(random.choices(all_numbers, weights=balanced_w, k=6))
                if len(set(nums)) != 6:
                    continue
                nums = sorted(set(nums))[:6]
                if not (min_sum <= sum(nums) <= max_sum):
                    continue

            games.append(nums)
            break

    return games, strategies


def build_header(counter, history, next_round):
    _w = ["월", "화", "수", "목", "금", "토", "일"]
    _t = datetime.now(KST)
    _d = f"{_t.strftime('%m-%d')} ({_w[_t.weekday()]})"

    # 최근 당첨 번호
    latest_rnd = max(history.keys(), key=int)
    latest_data = history[latest_rnd]
    latest_nums = latest_data["numbers"]
    latest_date = latest_data["date"]

    # 상위 10 핫넘버
    top10 = counter.most_common(10)
    hot_str = ", ".join(f"*{n}*({c}회)" for n, c in top10)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"로또 번호 추천 (제{next_round}회) | {_d}"}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*최근 당첨번호* (제{latest_rnd}회, {latest_date})\n"
                    f"{format_numbers(latest_nums)} + `{latest_data['bonus']}`\n\n"
                    f"*최다 출현 TOP 10* ({len(history)}회차 분석)\n"
                    f"{hot_str}"
                ),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "상세 추천은 스레드를 확인해주세요"}]},
    ]
    return blocks


def build_games_thread(title, games, strategies):
    """추천 게임 스레드 블록 — 게임별 개별 블록"""
    blocks = []
    if title:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}})
    for i, nums in enumerate(games):
        if i < len(strategies) and strategies[i]:
            text = f"*{i + 1}.* {strategies[i]}\n  {format_numbers(nums)}"
        else:
            text = f"*{i + 1}.*  {format_numbers(nums)}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return blocks


# ── 메인 ─────────────────────────────────────────────────────────────────
def main():
    now_kst = datetime.now(KST)

    if not SLACK_BOT_TOKEN:
        print("SLACK_BOT_TOKEN 미설정.")
        sys.exit(1)

    print(f"로또 번호 추천 시작 ({now_kst.strftime('%Y-%m-%d %H:%M')} KST)...")

    # 상태 로드
    state = load_state()

    # 당첨 데이터 수집
    print("\n[당첨 데이터 수집]")
    history = fetch_lotto_history(state)

    if len(history) < 10:
        print("충분한 데이터 없음. 종료.")
        return

    # 빈도 분석
    print("\n[빈도 분석]")
    counter, pair_counter = analyze_frequency(history)

    # 다음 회차 (데이터에서 최신 회차 + 1)
    next_round = max(int(k) for k in history.keys()) + 1

    # 추천 번호 생성
    print("\n[빈도 기반 추천]")
    recent_weeks = state.get("recent_picks", [])
    recent = [g for week in recent_weeks for g in week]
    freq_games = generate_recommendations(counter, pair_counter, NUM_GAMES, recent)
    freq_strategies = [get_strategy_name(i) for i in range(NUM_GAMES)]
    for i, nums in enumerate(freq_games):
        print(f"  빈도 {i + 1}: {nums}")

    print("\n[AI 추천]")
    ai_games, ai_strategies = generate_ai_picks(counter, history)
    for i, nums in enumerate(ai_games):
        print(f"  AI {i + 1}: {nums}")

    # 1) 메인 메시지
    header_blocks = build_header(counter, history, next_round)
    ts = slack_post(header_blocks)
    if not ts:
        print("\n메인 메시지 전송 실패. 종료.")
        return
    print(f"\n메인 메시지 전송 (ts={ts})")

    # 2) 빈도 기반 추천 스레드
    freq_blocks = build_games_thread("빈도 기반 추천 (통계 분석)", freq_games, freq_strategies)
    slack_post(freq_blocks, thread_ts=ts)
    print("  스레드: 빈도 기반 추천")

    # 3) AI 추천 스레드
    ai_blocks = build_games_thread("오늘 추천 번호", ai_games, [])
    slack_post([{"type": "divider"}] + ai_blocks, thread_ts=ts)
    print("  스레드: AI 추천")

    # 상태 저장
    state["history"] = history
    state["recent_picks"] = (recent_weeks + [freq_games])[-RECENT_WEEKS:]
    state["last_run"] = now_kst.isoformat()
    save_state(state)
    print("상태 저장 완료")
    print("완료.")


if __name__ == "__main__":
    main()
