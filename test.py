"""
AI Research News Agent
- Gemini 2.0 Flash API (Google Search Grounding) 로 최신 뉴스 수집 및 요약
- Microsoft Teams 전용 발송 (Workflows 웹훅 / Adaptive Card)
- 하루 3회 cron 실행 (08:00 / 13:00 / 18:00)
"""

import os
import re
import requests
import yaml
from google import genai
from google.genai import types
from datetime import datetime
from zoneinfo import ZoneInfo

# ── config 로드 ───────────────────────────────────────────────────────────────
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], raw)
    return yaml.safe_load(raw)

CONFIG = load_config()

KST = ZoneInfo("Asia/Seoul")

# ── 시간대별 타이틀 ───────────────────────────────────────────────────────────
def get_edition_label() -> str:
    hour = datetime.now(KST).hour
    if hour < 11:
        return "🌅 모닝 브리핑"
    elif hour < 16:
        return "☀️ 오후 업데이트"
    else:
        return "🌙 이브닝 리캡"


def fetch_news_summary() -> str:
    client = genai.Client(api_key=CONFIG["gemini_api_key"])
    today = datetime.now(KST).strftime("%Y년 %m월 %d일")

    query_list = "\n".join(
        f"- [{cat}] {q}"
        for cat, queries in CONFIG["search_queries"].items()
        for q in queries
    )

    # ── Step 1: 검색 (thinking 비활성화) ─────────────────────────────────────
    search_prompt = (
        f"오늘({today}) 기준으로 아래 키워드를 검색해서 "
        f"MICCAI, NeurIPS, CVPR, ICLR, ICML에서 accept된 논문 5편을 찾아줘.\n\n"
        f"반드시 지킬 것:\n"
        f"- MICCAI, NeurIPS, CVPR, ICLR, ICML accept 논문만 (MIDL, ISBI, ECCV 등 제외)\n"
        f"- arXiv preprint 절대 포함 금지\n"
        f"- URL은 출력하지 마\n"
        f"- 추론 과정, 메모, 내부 판단을 출력하지 마\n"
        f"- 아래 형식으로만 출력:\n"
        f"  제목 / 저자 (소속) / 학회 연도 / 핵심 기여 한 줄\n\n"
        f"검색 키워드:\n{query_list}"
    )

    search_resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=search_prompt,
        config=types.GenerateContentConfig(
            # ✅ thinking 끄기 — 추론 내용이 response.text에 노출되는 문제 해결
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    # ── Step 2: grounding_chunks에서 실제 URL 추출 ────────────────────────────
    # chunk.web.uri는 vertexaisearch 리다이렉트 래퍼 → grounding_supports로 실제 URI 확보
    verified_sources: list[dict] = []
    candidate = search_resp.candidates[0]
    meta = candidate.grounding_metadata

    if meta:
        # grounding_supports: 각 텍스트 구간이 어떤 chunk를 참조하는지 담고 있음
        # chunk.web.uri가 실제 URL (리다이렉트 아님)
        chunks = meta.grounding_chunks or []
        seen = set()
        for chunk in chunks:
            if chunk.web and chunk.web.uri:
                uri = chunk.web.uri
                # vertexaisearch 리다이렉트 래퍼 제거
                if "vertexaisearch.cloud.google.com" in uri:
                    continue
                if uri not in seen:
                    seen.add(uri)
                    verified_sources.append({
                        "title": chunk.web.title or uri,
                        "url":   uri,
                    })

    sources_block = "\n".join(
        f"- {s['title']}: {s['url']}" for s in verified_sources
    ) or "검색 확인된 출처 없음"

    # ── Step 3: 요약 (search OFF, thinking OFF) ───────────────────────────────
    summary_prompt = (
        f"{CONFIG['system_prompt']}\n\n"
        f"[논문 목록]\n{search_resp.text}\n\n"
        f"[검색으로 확인된 실제 URL]\n{sources_block}\n\n"
        f"위 논문 목록을 형식에 맞게 요약해줘.\n"
        f"필수 규칙:\n"
        f"- URL은 위 목록에 있는 것만 사용, 없으면 '(URL 확인 불가)' 표기\n"
        f"- 추론 과정, 내부 판단, 메모를 절대 출력하지 마\n"
        f"- MICCAI, NeurIPS, CVPR, ICLR, ICML 외 학회 논문이 목록에 있으면 제외하고 다른 논문으로 대체\n"
        f"- 최종 출력만 작성할 것"
    )

    summary_resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=summary_prompt,
        config=types.GenerateContentConfig(
            # ✅ 요약 단계도 thinking 끄기
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    result = summary_resp.text.strip()

    if verified_sources:
        footer = "\n\n---\n✅ 검색 확인된 출처\n" + "\n".join(
            f"{i+1}. [{s['title']}]({s['url']})"
            for i, s in enumerate(verified_sources)
        )
        result += footer

    return result

# ── Teams 발송 (Adaptive Card) ────────────────────────────────────────────────
def send_teams(title: str, body: str):
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": title,
                        "weight": "bolder",
                        "size": "medium",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": body,
                        "wrap": True,
                        "spacing": "medium",
                    },
                ],
            },
        }],
    }
    r = requests.post(CONFIG["teams_webhook_url"], json=payload, timeout=10)
    r.raise_for_status()
    print("[Teams] ✅ 발송 완료")


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    now      = datetime.now(KST)
    edition  = get_edition_label()
    date_str = now.strftime("%Y-%m-%d %H:%M")
    title    = f"[AI Daily] {edition}  |  {date_str} KST"

    print(f"[{date_str}] 뉴스 수집 시작...")
    summary = fetch_news_summary()
    print(summary)
    send_teams(title, summary)
    print("✅ 완료")


if __name__ == "__main__":
    # from google import genai

    # client = genai.Client(api_key=CONFIG["gemini_api_key"])

    # for model in client.models.list():
    #     print(model.name)
    main()