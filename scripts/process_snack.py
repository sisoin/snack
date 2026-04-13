#!/usr/bin/env python3
"""
간식 칼로리 기여 처리 스크립트
contributions/*.txt 파일을 읽어 OpenAI로 칼로리를 계산하고
data/snacks.json, assets/histogram.svg, assets/consumed.svg,
assets/inventory.svg, README.md를 업데이트합니다.
"""

import os
import json
import math
import re
import sys
import html
from pathlib import Path
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "snacks.json"
CONTRIBUTIONS_DIR = ROOT / "contributions"
CHART_FILE = ROOT / "assets" / "histogram.svg"
CONSUMED_FILE = ROOT / "assets" / "consumed.svg"
INVENTORY_FILE = ROOT / "assets" / "inventory.svg"
README_FILE = ROOT / "README.md"


# ---------------------------------------------------------------------------
# 데이터 입출력
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"contributors": {}, "calorie_cache": {}}


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 파일 파싱
# ---------------------------------------------------------------------------

def parse_contribution_file(
    filepath: Path,
) -> tuple[str | None, list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """
    txt 파일을 읽어 (표시이름, 기여간식목록, 섭취간식목록) 를 반환합니다.

    ## 소비 섹션 이후의 줄은 섭취한 간식으로 파싱됩니다.
    그 이전은 기여(가져온) 간식입니다.

    지원 형식:
      # 이름: 홍길동       → 표시 이름 "홍길동"
      오예스 12개          → 기여 간식 ("오예스", 12, "개")
      초코파이 1박스       → 기여 간식 ("초코파이", 1, "박스")

      ## 소비              → 이후 줄은 섭취 간식으로 파싱
      새우깡 3봉지         → 섭취 간식 ("새우깡", 3, "봉지")
    """
    display_name: str | None = None
    mode = "contribution"
    raw_contributions: list[tuple[str, int, str]] = []
    raw_consumed: list[tuple[str, int, str]] = []
    name_pattern = re.compile(r"^#\s*이름\s*[:：]\s*(.+)")
    snack_pattern = re.compile(r"^(.+?)\s+(\d+)\s*([가-힣a-zA-Z]*)")

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 섹션 헤더
            if line.startswith("##"):
                section = line.lstrip("#").strip()
                mode = "consumed" if section == "소비" else "contribution"
                continue
            # 이름 지시어 파싱 (기여 섹션에서만)
            if mode == "contribution":
                name_match = name_pattern.match(line)
                if name_match:
                    display_name = name_match.group(1).strip()
                    continue
            # 일반 주석 무시
            if line.startswith("#"):
                continue
            # 간식 파싱
            snack_match = snack_pattern.match(line)
            if snack_match:
                snack_name = snack_match.group(1).strip()
                quantity = int(snack_match.group(2))
                unit = snack_match.group(3).strip() or "개"
                if mode == "consumed":
                    raw_consumed.append((snack_name, quantity, unit))
                else:
                    raw_contributions.append((snack_name, quantity, unit))
            else:
                print(f"  ⚠️  파싱 실패 (무시됨): '{line}'", file=sys.stderr)

    def merge(items: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
        merged: dict[str, tuple[str, int, str]] = {}
        for snack_name, quantity, unit in items:
            key = f"{snack_name}_{unit}"
            if key in merged:
                orig_name, orig_qty, orig_unit = merged[key]
                merged[key] = (orig_name, orig_qty + quantity, orig_unit)
                print(f"  🔀 병합: {snack_name} {quantity}{unit} → 합계 {orig_qty + quantity}{unit}")
            else:
                merged[key] = (snack_name, quantity, unit)
        return list(merged.values())

    return display_name, merge(raw_contributions), merge(raw_consumed)


# ---------------------------------------------------------------------------
# OpenAI 칼로리 조회
# ---------------------------------------------------------------------------

def get_calorie_info(snack_name: str, unit: str, cache: dict) -> dict:
    """
    OpenAI를 통해 간식의 단위당 칼로리 상세 정보를 조회합니다.
    캐시 키는 "간식명_단위" 로 저장하여 박스/봉지/개를 구분합니다.
    """
    cache_key = f"{snack_name.strip().lower()}_{unit.strip().lower()}"
    if cache_key in cache:
        hit = cache[cache_key]
        print(
            f"  💾 캐시: {snack_name} {unit} → "
            f"{hit['calories_per_unit']} kcal/{hit['unit']} "
            f"({hit['count_per_unit']}{hit['individual_unit']}입)"
        )
        return hit

    print(f"  🤖 OpenAI 조회 중: {snack_name} ({unit})...")
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "당신은 한국 식품 영양 전문가입니다.\n"
                        "사용자가 '간식명 + 단위'를 알려주면 해당 단위 기준으로 칼로리 정보를 JSON으로 답하세요.\n\n"
                        "핵심 규칙:\n"
                        "1. 박스·팩·묶음·세트처럼 여러 개가 들어있는 단위는 몇 개짜리인지 파악하세요.\n"
                        "   예) 초코파이 1박스 = 12개, 오레오 1팩 = 6개, 포카칩 1박스 = 6봉지\n"
                        "2. 개·캔·병 처럼 낱개 단위면 count_per_unit = 1 로 하세요.\n"
                        "3. 정확한 데이터가 없으면 한국 시중 판매 기준으로 합리적으로 추정하세요.\n\n"
                        "반드시 아래 JSON 형식만 출력하세요 (설명 없이):\n"
                        "{\n"
                        '  "calories_per_unit": <사용자가 말한 단위 1개당 총 칼로리(kcal)>,\n'
                        '  "unit": "<사용자가 말한 단위 (박스/봉지/개/캔 등)>",\n'
                        '  "count_per_unit": <그 단위 안에 낱개가 몇 개인지 (낱개면 1)>,\n'
                        '  "individual_unit": "<낱개 단위명 (개/조각/봉지/캔 등)>",\n'
                        '  "calories_per_individual": <낱개 1개당 칼로리(kcal)>\n'
                        "}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"'{snack_name}' {unit}의 칼로리 정보를 알려주세요.\n"
                        f"이 {unit}에 낱개가 몇 개 들어있는지, "
                        f"낱개당 칼로리와 {unit}당 총 칼로리를 계산해주세요."
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        result = json.loads(response.choices[0].message.content)
        result.setdefault("calories_per_unit", 0)
        result.setdefault("unit", unit)
        result.setdefault("count_per_unit", 1)
        result.setdefault("individual_unit", "개")
        result.setdefault("calories_per_individual", result["calories_per_unit"])

        cache[cache_key] = result
        print(
            f"  ✅ {snack_name} {unit} → "
            f"{result['calories_per_unit']} kcal "
            f"({result['count_per_unit']}{result['individual_unit']}입 × "
            f"{result['calories_per_individual']} kcal)"
        )
        return result

    except Exception as e:
        print(f"  ❌ OpenAI 오류 ({snack_name} {unit}): {e}", file=sys.stderr)
        return {
            "calories_per_unit": 0, "unit": unit,
            "count_per_unit": 1, "individual_unit": "개",
            "calories_per_individual": 0,
        }


# ---------------------------------------------------------------------------
# 공통 SVG 유틸
# ---------------------------------------------------------------------------

SNACK_PALETTE = [
    "#FF6B6B", "#FFD93D", "#6BCB77", "#4D96FF", "#A57BDB",
    "#FF9F43", "#38D9A9", "#F06595", "#74B9FF", "#FDCB6E",
    "#E17055", "#00CEC9", "#6C5CE7", "#FD79A8", "#B2BEC3",
]

RANK_ICONS = ["🥇", "🥈", "🥉"]
FONT = "system-ui,-apple-system,'Segoe UI',sans-serif"


def _esc(text: str) -> str:
    return html.escape(str(text))


_DARK_STYLE = (
    '<style>\n'
    '  @media (prefers-color-scheme: dark) {\n'
    '    .bg { fill: #1A1A2E !important; }\n'
    '    .border { stroke: #333 !important; }\n'
    '    .title { fill: #E8E8E8 !important; }\n'
    '    .subtitle { fill: #888 !important; }\n'
    '    .grid { stroke: #333 !important; }\n'
    '    .grid-base { stroke: #444 !important; }\n'
    '    .axis-label { fill: #666 !important; }\n'
    '    .bar-bg { fill: #2A2A3E !important; }\n'
    '    .rank-label { fill: #CCC !important; }\n'
    '    .cal-label { fill: #DDD !important; }\n'
    '    .name-label { fill: #E8E8E8 !important; }\n'
    '    .name-sub { fill: #666 !important; }\n'
    '    .legend-text { fill: #AAA !important; }\n'
    '    .inv-row-alt { fill: #16213E !important; }\n'
    '    .inv-name { fill: #E8E8E8 !important; }\n'
    '    .inv-sub { fill: #666 !important; }\n'
    '    .inv-bar-bg { fill: #333 !important; }\n'
    '    .inv-pct { fill: #888 !important; }\n'
    '  }\n'
    '</style>'
)


# ---------------------------------------------------------------------------
# SVG 히스토그램 생성 (기여도 / 섭취 랭킹 공용)
# ---------------------------------------------------------------------------

def generate_ranking_svg(
    *,
    title_xml: str,
    subtitle: str,
    sorted_list: list[tuple[str, dict]],
    cal_key: str,
    snacks_key: str,
) -> str:
    """
    세로형 스택 막대 차트 SVG를 생성합니다.
    title_xml  : SVG에 들어갈 제목 (XML 특수문자 이스케이프 적용 필요)
    cal_key    : 총 칼로리 키 ('total_calories' 또는 'total_consumed_calories')
    snacks_key : 간식 목록 키 ('snacks' 또는 'consumed')
    """
    if not sorted_list:
        return ""

    n = len(sorted_list)

    # 간식별 색상 할당
    all_snack_names: list[str] = []
    for _, info in sorted_list:
        for s in info.get(snacks_key, []):
            if s["name"] not in all_snack_names:
                all_snack_names.append(s["name"])
    snack_color = {
        name: SNACK_PALETTE[i % len(SNACK_PALETTE)]
        for i, name in enumerate(all_snack_names)
    }

    # 레이아웃
    if n <= 3:
        BAR_W, GAP = 90, 36
    elif n <= 6:
        BAR_W, GAP = 72, 28
    elif n <= 10:
        BAR_W, GAP = 56, 20
    else:
        BAR_W, GAP = 44, 14

    LEFT = 58
    RIGHT = 20
    TOP = 80
    CHART_H = 280
    NAME_H = 50
    LEGEND_COLS = min(3, len(all_snack_names)) if all_snack_names else 1
    LEGEND_ROWS = math.ceil(len(all_snack_names) / LEGEND_COLS) if all_snack_names else 0
    LEGEND_H = LEGEND_ROWS * 22 + 20 if all_snack_names else 0
    BOTTOM = NAME_H + LEGEND_H + 16

    W = max(LEFT + n * (BAR_W + GAP) - GAP + RIGHT, 480)
    H = TOP + CHART_H + BOTTOM

    bar_base_y = TOP + CHART_H
    max_cal = sorted_list[0][1].get(cal_key, 0) if sorted_list else 1
    if max_cal == 0:
        max_cal = 1
    total_cal = sum(v.get(cal_key, 0) for _, v in sorted_list)

    lines: list[str] = []

    lines.append(f'<rect class="bg" width="{W}" height="{H}" fill="#FFFFFF"/>')
    lines.append(
        f'<rect class="border" width="{W}" height="{H}" rx="12" '
        f'fill="none" stroke="#E8E8E8" stroke-width="1"/>'
    )
    lines.append(
        f'<text class="title" x="{W // 2}" y="30" text-anchor="middle" '
        f'font-family="{FONT}" font-size="17" font-weight="700" fill="#1A1A2E">'
        f'{title_xml}</text>'
    )
    lines.append(
        f'<text class="subtitle" x="{W // 2}" y="52" text-anchor="middle" '
        f'font-family="{FONT}" font-size="12" fill="#999">'
        f'{_esc(subtitle)}</text>'
    )

    # Y축 격자선
    for i in range(6):
        cal_val = round(max_cal * i / 5)
        gy = bar_base_y - round(CHART_H * i / 5)
        grid_cls = "grid" if i > 0 else "grid-base"
        stroke = "#EFEFEF" if i > 0 else "#DDDDDD"
        lines.append(
            f'<line class="{grid_cls}" x1="{LEFT - 4}" y1="{gy}" x2="{W - RIGHT}" y2="{gy}" '
            f'stroke="{stroke}" stroke-width="1"/>'
        )
        if cal_val >= 10000:
            label = f"{cal_val // 1000}천"
        elif cal_val >= 1000:
            label = f"{cal_val / 1000:.1f}천"
        else:
            label = str(cal_val)
        lines.append(
            f'<text class="axis-label" x="{LEFT - 8}" y="{gy + 4}" text-anchor="end" '
            f'font-family="{FONT}" font-size="10" fill="#BBBBBB">{_esc(label)}</text>'
        )

    # 막대
    for idx, (username, info) in enumerate(sorted_list):
        x = LEFT + idx * (BAR_W + GAP)
        person_cal = info.get(cal_key, 0)
        total_h = max(round((person_cal / max_cal) * CHART_H), 2)
        bar_top_y = bar_base_y - total_h
        rank_label = RANK_ICONS[idx] if idx < 3 else f"#{idx + 1}"

        clip_id = f"c{idx}"
        lines.append(
            f'<clipPath id="{clip_id}">'
            f'<rect x="{x}" y="{bar_top_y}" width="{BAR_W}" height="{total_h}" rx="5"/>'
            f'</clipPath>'
        )
        lines.append(
            f'<rect class="bar-bg" x="{x}" y="{bar_top_y}" width="{BAR_W}" '
            f'height="{total_h}" rx="5" fill="#F5F5F5"/>'
        )
        lines.append(f'<g clip-path="url(#{clip_id})">')
        current_y = bar_base_y
        for s in reversed(info.get(snacks_key, [])):
            seg_h = max(round((s["total_calories"] / max_cal) * CHART_H), 2)
            current_y -= seg_h
            color = snack_color.get(s["name"], "#CCC")
            lines.append(
                f'  <rect x="{x}" y="{current_y}" width="{BAR_W}" '
                f'height="{seg_h}" fill="{color}"/>'
            )
        lines.append('</g>')

        lines.append(
            f'<text class="rank-label" x="{x + BAR_W // 2}" y="{bar_top_y - 20}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="16" fill="#555">{_esc(rank_label)}</text>'
        )
        lines.append(
            f'<text class="cal-label" x="{x + BAR_W // 2}" y="{bar_top_y - 6}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="10" font-weight="600" fill="#444">'
            f'{person_cal:,}</text>'
        )
        display = info.get("display_name") or username
        name_label = display if display != username else f"@{username}"
        lines.append(
            f'<text class="name-label" x="{x + BAR_W // 2}" y="{bar_base_y + 18}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="12" font-weight="700" fill="#333">'
            f'{_esc(name_label)}</text>'
        )
        if display != username:
            lines.append(
                f'<text class="name-sub" x="{x + BAR_W // 2}" y="{bar_base_y + 32}" text-anchor="middle" '
                f'font-family="{FONT}" font-size="10" fill="#BBBBBB">'
                f'@{_esc(username)}</text>'
            )

    # 범례
    if all_snack_names:
        legend_top = TOP + CHART_H + NAME_H + 4
        col_w = (W - LEFT - RIGHT) // LEGEND_COLS
        for i, name in enumerate(all_snack_names):
            col = i % LEGEND_COLS
            row = i // LEGEND_COLS
            lx = LEFT + col * col_w
            ly = legend_top + row * 22
            color = snack_color[name]
            lines.append(f'<rect x="{lx}" y="{ly}" width="11" height="11" rx="3" fill="{color}"/>')
            lines.append(
                f'<text class="legend-text" x="{lx + 15}" y="{ly + 9}" '
                f'font-family="{FONT}" font-size="11" fill="#666">{_esc(name)}</text>'
            )

    inner = "\n  ".join(lines)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n'
        f'  {_DARK_STYLE}\n'
        f'  {inner}\n'
        f'</svg>\n'
    )


def generate_svg(data: dict) -> str:
    """간식 기여도 랭킹 SVG"""
    contributors: dict = data.get("contributors", {})
    if not contributors:
        return ""
    sorted_list = sorted(
        contributors.items(),
        key=lambda x: x[1]["total_calories"],
        reverse=True,
    )
    total_cal = sum(v["total_calories"] for v in contributors.values())
    total_count = sum(
        s["quantity"] * s.get("count_per_unit", 1)
        for info in contributors.values()
        for s in info["snacks"]
    )
    return generate_ranking_svg(
        title_xml="&#x1F37F; 간식 기여도",
        subtitle=f"총 칼로리 {total_cal:,} kcal · 참여자 {len(contributors)}명 · 총 간식 {total_count:,}개",
        sorted_list=sorted_list,
        cal_key="total_calories",
        snacks_key="snacks",
    )


def generate_consumed_svg(data: dict) -> str:
    """섭취 칼로리 랭킹 SVG"""
    contributors: dict = data.get("contributors", {})
    ranked = [
        (u, info) for u, info in contributors.items()
        if info.get("total_consumed_calories", 0) > 0
    ]
    if not ranked:
        return ""
    sorted_list = sorted(ranked, key=lambda x: x[1]["total_consumed_calories"], reverse=True)
    total_consumed = sum(v.get("total_consumed_calories", 0) for v in contributors.values())
    return generate_ranking_svg(
        title_xml="&#x1F374; 섭취 칼로리 랭킹",
        subtitle=f"총 섭취 칼로리 {total_consumed:,} kcal · 참여자 {len(sorted_list)}명",
        sorted_list=sorted_list,
        cal_key="total_consumed_calories",
        snacks_key="consumed",
    )


# ---------------------------------------------------------------------------
# SVG 인벤토리 카드 생성 (탕비실 현황)
# ---------------------------------------------------------------------------

def generate_inventory_svg(inventory: dict) -> str:
    """탕비실 남은 간식 현황 카드 SVG"""
    if not inventory:
        return ""

    items = sorted(inventory.values(), key=lambda x: x["remaining"], reverse=True)

    W = 480
    PAD_X = 24
    ROW_H = 54
    HEADER_H = 72
    H = HEADER_H + len(items) * ROW_H + 16

    BAR_X = 200
    BAR_W = W - BAR_X - PAD_X - 40  # 40: percentage text area
    BAR_H = 12

    total_remaining = sum(v["remaining"] for v in items)
    total_contributed = sum(v["contributed"] for v in items)

    lines: list[str] = []

    lines.append(f'<rect class="bg" width="{W}" height="{H}" fill="#FFFFFF"/>')
    lines.append(
        f'<rect class="border" width="{W}" height="{H}" rx="12" '
        f'fill="none" stroke="#E8E8E8" stroke-width="1"/>'
    )
    lines.append(
        f'<text class="title" x="{W // 2}" y="30" text-anchor="middle" '
        f'font-family="{FONT}" font-size="17" font-weight="700" fill="#1A1A2E">'
        f'&#x1F3EA; 탕비실 현황</text>'
    )
    lines.append(
        f'<text class="subtitle" x="{W // 2}" y="52" text-anchor="middle" '
        f'font-family="{FONT}" font-size="12" fill="#999">'
        f'총 {total_remaining}개 남음 &#160;·&#160; 총 {total_contributed}개 기여</text>'
    )

    for i, item in enumerate(items):
        y = HEADER_H + i * ROW_H
        contributed = item["contributed"]
        consumed = item["consumed"]
        remaining = item["remaining"]
        ratio = remaining / contributed if contributed > 0 else 0
        pct = round(ratio * 100)

        if ratio > 0.66:
            bar_color = "#6BCB77"
        elif ratio > 0.33:
            bar_color = "#FFD93D"
        else:
            bar_color = "#FF6B6B"

        # 짝수 행 배경
        if i % 2 == 1:
            lines.append(
                f'<rect class="inv-row-alt" x="0" y="{y}" width="{W}" height="{ROW_H}" fill="#F9F9F9"/>'
            )

        # 간식 이름
        lines.append(
            f'<text class="inv-name" x="{PAD_X}" y="{y + 23}" '
            f'font-family="{FONT}" font-size="14" font-weight="700" fill="#222">'
            f'{_esc(item["name"])}</text>'
        )
        # 수량 정보
        lines.append(
            f'<text class="inv-sub" x="{PAD_X}" y="{y + 40}" '
            f'font-family="{FONT}" font-size="11" fill="#AAAAAA">'
            f'{remaining}/{contributed}{_esc(item["unit"])} &#160;·&#160; {consumed}개 섭취</text>'
        )

        # 프로그레스 바 배경
        bar_y = y + 20
        lines.append(
            f'<rect class="inv-bar-bg" x="{BAR_X}" y="{bar_y}" '
            f'width="{BAR_W}" height="{BAR_H}" rx="6" fill="#EEEEEE"/>'
        )
        # 프로그레스 바 채우기
        fill_w = max(round(ratio * BAR_W), 0 if remaining == 0 else 4)
        if fill_w > 0:
            lines.append(
                f'<rect x="{BAR_X}" y="{bar_y}" '
                f'width="{fill_w}" height="{BAR_H}" rx="6" fill="{bar_color}"/>'
            )
        # 퍼센트 텍스트
        lines.append(
            f'<text class="inv-pct" x="{BAR_X + BAR_W + 6}" y="{bar_y + 10}" '
            f'font-family="{FONT}" font-size="11" fill="#999">{pct}%</text>'
        )

    inner = "\n  ".join(lines)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n'
        f'  {_DARK_STYLE}\n'
        f'  {inner}\n'
        f'</svg>\n'
    )


def save_chart(data: dict) -> bool:
    svg = generate_svg(data)
    if not svg:
        return False
    CHART_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHART_FILE.write_text(svg, encoding="utf-8")
    return True


def save_consumed_chart(data: dict) -> bool:
    svg = generate_consumed_svg(data)
    if not svg:
        return False
    CONSUMED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONSUMED_FILE.write_text(svg, encoding="utf-8")
    return True


def save_inventory_chart(inventory: dict) -> bool:
    svg = generate_inventory_svg(inventory)
    if not svg:
        return False
    INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_FILE.write_text(svg, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# 업적/뱃지 시스템
# ---------------------------------------------------------------------------

BADGES = [
    (50000, "🏆", "간식 제왕",    "5만 kcal 돌파"),
    (30000, "💎", "간식 마스터",  "3만 kcal 돌파"),
    (20000, "🔥", "간식 매니아",  "2만 kcal 돌파"),
    (10000, "⭐", "간식 러버",    "1만 kcal 돌파"),
    (5000,  "🌟", "간식 입문자",  "5천 kcal 돌파"),
    (1000,  "🍪", "첫 기여자",    "1천 kcal 돌파"),
]


def get_badges(total_calories: int) -> list[tuple[str, str, str]]:
    return [
        (icon, name, desc)
        for threshold, icon, name, desc in BADGES
        if total_calories >= threshold
    ]


def get_next_badge(total_calories: int) -> tuple[int, str, str] | None:
    for threshold, icon, name, _ in reversed(BADGES):
        if total_calories < threshold:
            return (threshold - total_calories, icon, name)
    return None


# ---------------------------------------------------------------------------
# 칼로리 등가 환산 (Fun Stats)
# ---------------------------------------------------------------------------

CALORIE_EQUIVALENTS = [
    (505, "🍜", "신라면"),
    (775, "🍕", "피자 한 판(1/8)"),
    (250, "🍚", "공깃밥"),
    (300, "🏃", "30분 달리기"),
    (200, "🚶", "1시간 걷기"),
    (35,  "🧁", "마카롱"),
]


def get_fun_stats(total_calories: int) -> list[str]:
    stats = []
    for cal, icon, name in CALORIE_EQUIVALENTS:
        count = total_calories / cal
        if count >= 1:
            stats.append(f"{icon} {name} **{count:.1f}**개 분량")
    return stats


# ---------------------------------------------------------------------------
# README 생성
# ---------------------------------------------------------------------------

def _svg_or_path(svg_file: Path, fallback_path: str) -> str:
    """GitHub Actions 환경이면 SVG 인라인, 아니면 상대경로"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo and svg_file.exists():
        return svg_file.read_text(encoding="utf-8").strip()
    return f"![{svg_file.stem}]({fallback_path})"


def generate_readme(data: dict) -> str:
    contributors: dict = data.get("contributors", {})
    inventory: dict = data.get("inventory", {})

    how_to = """
## 🙋 참여 방법

1. 이 저장소를 **Fork** 하세요
2. `contributions/` 폴더에 `{본인_GitHub_ID}.txt` 파일을 만드세요
3. 파일에 이름과 기여한 간식, 먹은 간식을 입력하세요:
   ```
   # 이름: 홍길동
   오예스 12개
   초코파이 1박스
   콜라 3캔

   ## 소비
   오예스 2개
   콜라 1캔
   ```
   > `# 이름:` 줄은 선택사항입니다. 없으면 GitHub ID가 표시됩니다.
   > `## 소비` 섹션 이후는 실제로 먹은 간식으로 처리됩니다.
   > 묶음 단위(`1박스`, `2팩` 등)도 자동으로 낱개 수와 칼로리를 계산합니다.
4. **Pull Request** 를 열면 자동으로 칼로리가 계산되어 랭킹에 반영됩니다 🎉

> 파일을 수정해서 PR을 보내면 기존 기여가 대체됩니다.

---
*⚡ Powered by OpenAI · 자동 업데이트*
"""

    if not contributors:
        return f"""# 🍿 간식 칼로리 기여 현황

아직 기여자가 없습니다. 첫 번째 간식을 추가해보세요!
{how_to}"""

    sorted_list = sorted(
        contributors.items(),
        key=lambda x: x[1]["total_calories"],
        reverse=True,
    )
    total_cal = sum(v["total_calories"] for v in contributors.values())
    total_snack_count = sum(
        s["quantity"] * s.get("count_per_unit", 1)
        for info in contributors.values()
        for s in info["snacks"]
    )

    fun_stats = get_fun_stats(total_cal)
    fun_section = ""
    if fun_stats:
        fun_lines = " &nbsp;|&nbsp; ".join(fun_stats[:4])
        fun_section = f"\n> 🎯 {fun_lines}\n"

    # 상세 내역
    detail_blocks = []
    medals = ["🥇", "🥈", "🥉"]
    for rank, (username, info) in enumerate(sorted_list):
        medal = medals[rank] if rank < 3 else f"#{rank + 1}"
        display = info.get("display_name") or username
        name_label = f"{display} (@{username})" if display != username else f"@{username}"

        earned_badges = get_badges(info["total_calories"])
        badge_str = " ".join(f"{icon}" for icon, _, _ in earned_badges)
        next_badge = get_next_badge(info["total_calories"])
        progress_str = ""
        if next_badge:
            remaining, next_icon, next_name = next_badge
            progress_str = f"\n\n> 다음 업적: {next_icon} **{next_name}** — {remaining:,} kcal 남음"

        lines = [
            "<details>",
            f"<summary>{medal} <b>{name_label}</b> — {info['total_calories']:,} kcal {badge_str}</summary>",
            progress_str,
            "",
            "| 간식 | 수량 | 낱개 구성 | 낱개 칼로리 | 합계 |",
            "|------|-----:|:---------:|------------:|-----:|",
        ]
        for s in info["snacks"]:
            count_per = s.get("count_per_unit", 1)
            ind_unit = s.get("individual_unit", "개")
            cal_ind = s.get("calories_per_individual", s["calories_per_unit"])
            composition = f"{count_per}{ind_unit}입" if count_per > 1 else f"1{ind_unit}"
            total_count = s["quantity"] * count_per
            lines.append(
                f"| {s['name']} | {s['quantity']}{s['unit']} "
                f"| {composition} (총 {total_count}{ind_unit}) "
                f"| {cal_ind:,} kcal/{ind_unit} "
                f"| **{s['total_calories']:,} kcal** |"
            )

        # 섭취 정보
        consumed_items = info.get("consumed", [])
        consumed_cal = info.get("total_consumed_calories", 0)
        if consumed_items:
            lines.append("")
            lines.append(f"**섭취한 간식** — {consumed_cal:,} kcal")
            lines.append("")
            lines.append("| 간식 | 수량 | 칼로리 |")
            lines.append("|------|-----:|-------:|")
            for s in consumed_items:
                lines.append(
                    f"| {s['name']} | {s['quantity']}{s['unit']} "
                    f"| {s['total_calories']:,} kcal |"
                )

        lines.append("")
        lines.append("</details>")
        detail_blocks.append("\n".join(lines))

    details = "\n\n".join(detail_blocks)

    # SVG 섹션
    chart_section = _svg_or_path(CHART_FILE, "assets/histogram.svg")

    has_consumed = any(v.get("total_consumed_calories", 0) > 0 for v in contributors.values())
    consumed_section = ""
    if has_consumed:
        consumed_svg = _svg_or_path(CONSUMED_FILE, "assets/consumed.svg")
        consumed_section = f"\n## 🍽️ 섭취 칼로리 랭킹\n\n{consumed_svg}\n"

    inventory_section = ""
    if inventory:
        inventory_svg = _svg_or_path(INVENTORY_FILE, "assets/inventory.svg")
        inventory_section = f"\n## 🏪 탕비실 현황\n\n{inventory_svg}\n"

    return f"""# 🍿 간식 칼로리 기여 현황

> 🍬 총 간식: **{total_snack_count:,}개** &nbsp;|&nbsp; 🔥 총 칼로리: **{total_cal:,} kcal** &nbsp;|&nbsp; 👥 참여자: **{len(contributors)}명**
{fun_section}
## 🏆 랭킹

{chart_section}
{consumed_section}{inventory_section}
## 📋 상세 내역

{details}
{how_to}"""


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== 🍿 간식 칼로리 처리 시작 ===\n")

    data = load_data()

    # 0칼로리 캐시 항목 제거
    cache: dict = {
        k: v for k, v in data.get("calorie_cache", {}).items()
        if v.get("calories_per_unit", 0) > 0
    }
    removed = len(data.get("calorie_cache", {})) - len(cache)
    if removed:
        print(f"🗑️  유효하지 않은 캐시 {removed}개 제거 (재시도 대상)\n")

    new_contributors: dict = {}

    txt_files = sorted(CONTRIBUTIONS_DIR.glob("*.txt"))
    if not txt_files:
        print("contributions/ 폴더에 txt 파일이 없습니다.")

    for txt_file in txt_files:
        username = txt_file.stem
        if username.startswith(".") or username == "example":
            continue

        display_name, contributions, consumed_items = parse_contribution_file(txt_file)
        label = display_name or username
        print(f"\n👤 {label} (@{username})")

        if not contributions and not consumed_items:
            print("  (항목 없음)")
            continue

        contributor: dict = {
            "total_calories": 0,
            "total_consumed_calories": 0,
            "display_name": label,
            "snacks": [],
            "consumed": [],
        }

        # 기여 간식 처리
        for snack_name, quantity, unit in contributions:
            info = get_calorie_info(snack_name, unit, cache)
            cal_per_unit = info.get("calories_per_unit", 0)
            if cal_per_unit == 0:
                print(f"  ⚠️  칼로리 0 — 건너뜀: {snack_name} {unit}", file=sys.stderr)
                continue
            total = cal_per_unit * quantity
            contributor["snacks"].append({
                "name": snack_name,
                "quantity": quantity,
                "unit": info.get("unit", unit),
                "count_per_unit": info.get("count_per_unit", 1),
                "individual_unit": info.get("individual_unit", "개"),
                "calories_per_unit": cal_per_unit,
                "calories_per_individual": info.get("calories_per_individual", cal_per_unit),
                "total_calories": total,
            })
            contributor["total_calories"] += total

        # 섭취 간식 처리
        if consumed_items:
            print(f"  🍽️  섭취 간식 처리 중...")
        for snack_name, quantity, unit in consumed_items:
            info = get_calorie_info(snack_name, unit, cache)
            cal_per_unit = info.get("calories_per_unit", 0)
            total = cal_per_unit * quantity
            contributor["consumed"].append({
                "name": snack_name,
                "quantity": quantity,
                "unit": info.get("unit", unit),
                "count_per_unit": info.get("count_per_unit", 1),
                "individual_unit": info.get("individual_unit", "개"),
                "calories_per_unit": cal_per_unit,
                "calories_per_individual": info.get("calories_per_individual", cal_per_unit),
                "total_calories": total,
            })
            contributor["total_consumed_calories"] += total

        new_contributors[username] = contributor
        print(f"  💪 기여: {contributor['total_calories']:,} kcal  |  🍽️ 섭취: {contributor['total_consumed_calories']:,} kcal")

    # 인벤토리 계산
    inventory: dict = {}
    for username, info in new_contributors.items():
        for s in info["snacks"]:
            key = f"{s['name']}_{s['unit']}"
            if key not in inventory:
                inventory[key] = {
                    "name": s["name"],
                    "unit": s["unit"],
                    "individual_unit": s["individual_unit"],
                    "count_per_unit": s["count_per_unit"],
                    "contributed": 0,
                    "consumed": 0,
                }
            inventory[key]["contributed"] += s["quantity"]
        for s in info.get("consumed", []):
            key = f"{s['name']}_{s['unit']}"
            if key not in inventory:
                inventory[key] = {
                    "name": s["name"],
                    "unit": s["unit"],
                    "individual_unit": s.get("individual_unit", "개"),
                    "count_per_unit": s.get("count_per_unit", 1),
                    "contributed": 0,
                    "consumed": 0,
                }
            inventory[key]["consumed"] += s["quantity"]

    for key in inventory:
        inventory[key]["remaining"] = max(0, inventory[key]["contributed"] - inventory[key]["consumed"])

    data["contributors"] = new_contributors
    data["calorie_cache"] = cache
    data["inventory"] = inventory

    save_data(data)
    print(f"\n💾 데이터 저장: {DATA_FILE}")

    save_chart(data)
    print(f"📊 기여 차트 저장: {CHART_FILE}")

    if save_consumed_chart(data):
        print(f"📊 섭취 차트 저장: {CONSUMED_FILE}")

    if save_inventory_chart(inventory):
        print(f"📊 인벤토리 저장: {INVENTORY_FILE}")

    readme = generate_readme(data)
    README_FILE.write_text(readme, encoding="utf-8")
    print(f"📄 README 업데이트: {README_FILE}")

    total = sum(v["total_calories"] for v in new_contributors.values())
    total_consumed = sum(v.get("total_consumed_calories", 0) for v in new_contributors.values())
    print(f"\n=== ✅ 완료 | 참여자 {len(new_contributors)}명 | 기여 {total:,} kcal | 섭취 {total_consumed:,} kcal ===")


if __name__ == "__main__":
    main()
