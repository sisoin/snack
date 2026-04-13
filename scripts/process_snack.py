#!/usr/bin/env python3
"""
간식 칼로리 기여 처리 스크립트
contributions/*.txt 파일을 읽어 OpenAI로 칼로리를 계산하고
data/snacks.json, assets/histogram.svg, README.md를 업데이트합니다.
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

def parse_contribution_file(filepath: Path) -> tuple[str | None, list[tuple[str, int, str]]]:
    """
    txt 파일을 읽어 (표시이름, [(간식명, 개수, 단위)]) 를 반환합니다.

    파일 첫 줄에 '# 이름: 홍길동' 형식으로 한글 표시 이름을 지정할 수 있습니다.
    없으면 파일명(GitHub ID)을 그대로 사용합니다.

    지원 형식:
      # 이름: 홍길동       → 표시 이름 "홍길동"
      오예스 12개          → ("오예스", 12, "개")
      초코파이 1박스       → ("초코파이", 1, "박스")
      콜라 3캔             → ("콜라", 3, "캔")
    """
    display_name: str | None = None
    snacks = []
    name_pattern = re.compile(r"^#\s*이름\s*[:：]\s*(.+)")
    snack_pattern = re.compile(r"^(.+?)\s+(\d+)\s*([가-힣a-zA-Z]*)")

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 이름 지시어 파싱
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
                snacks.append((snack_name, quantity, unit))
            else:
                print(f"  ⚠️  파싱 실패 (무시됨): '{line}'", file=sys.stderr)

    return display_name, snacks


# ---------------------------------------------------------------------------
# OpenAI 칼로리 조회
# ---------------------------------------------------------------------------

def get_calorie_info(snack_name: str, unit: str, cache: dict) -> dict:
    """
    OpenAI를 통해 간식의 단위당 칼로리 상세 정보를 조회합니다.

    반환 구조:
      calories_per_unit   : 사용자가 명시한 단위(unit) 1개당 총 칼로리
      unit                : 사용자가 명시한 단위 (박스/봉지/개/캔 등)
      count_per_unit      : 해당 단위 안에 낱개가 몇 개 들어있는지 (개 단위면 1)
      individual_unit     : 낱개 단위 이름 (개, 조각, 캔 등)
      calories_per_individual : 낱개 1개당 칼로리

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
        # 기본값 보호
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
        fallback = {
            "calories_per_unit": 0, "unit": unit,
            "count_per_unit": 1, "individual_unit": "개",
            "calories_per_individual": 0,
        }
        cache[cache_key] = fallback
        return fallback


# ---------------------------------------------------------------------------
# SVG 히스토그램 생성 (세로형 스택 막대 차트)
# ---------------------------------------------------------------------------

# 간식별 색상 팔레트
SNACK_PALETTE = [
    "#FF6B6B", "#FFD93D", "#6BCB77", "#4D96FF", "#A57BDB",
    "#FF9F43", "#38D9A9", "#F06595", "#74B9FF", "#FDCB6E",
    "#E17055", "#00CEC9", "#6C5CE7", "#FD79A8", "#B2BEC3",
]

RANK_ICONS = ["🥇", "🥈", "🥉"]
FONT = "system-ui,-apple-system,'Segoe UI',sans-serif"


def _esc(text: str) -> str:
    """SVG/XML 특수문자 이스케이프"""
    return html.escape(str(text))


def generate_svg(data: dict) -> str:
    """
    세로형 스택 막대 차트 SVG를 생성합니다.
    - 각 막대는 간식별로 색상을 달리하여 쌓아올립니다.
    - 배경 흰색, 범례 하단 표시.
    """
    contributors: dict = data.get("contributors", {})
    if not contributors:
        return ""

    sorted_list = sorted(
        contributors.items(),
        key=lambda x: x[1]["total_calories"],
        reverse=True,
    )
    n = len(sorted_list)

    # ── 간식별 색상 할당 (모든 기여자에서 동일 색상 유지) ────────────
    all_snack_names: list[str] = []
    for _, info in sorted_list:
        for s in info["snacks"]:
            if s["name"] not in all_snack_names:
                all_snack_names.append(s["name"])
    snack_color = {
        name: SNACK_PALETTE[i % len(SNACK_PALETTE)]
        for i, name in enumerate(all_snack_names)
    }

    # ── 레이아웃 ─────────────────────────────────────────────────
    if n <= 3:
        BAR_W, GAP = 90, 36
    elif n <= 6:
        BAR_W, GAP = 72, 28
    elif n <= 10:
        BAR_W, GAP = 56, 20
    else:
        BAR_W, GAP = 44, 14

    LEFT      = 58    # y축 레이블
    RIGHT     = 20
    TOP       = 80    # 제목 영역
    CHART_H   = 280   # 막대 최대 높이
    NAME_H    = 50    # 기여자 이름 영역
    LEGEND_COLS = min(3, len(all_snack_names)) if all_snack_names else 1
    LEGEND_ROWS = math.ceil(len(all_snack_names) / LEGEND_COLS) if all_snack_names else 0
    LEGEND_H  = LEGEND_ROWS * 22 + 20 if all_snack_names else 0
    BOTTOM    = NAME_H + LEGEND_H + 16

    W = max(LEFT + n * (BAR_W + GAP) - GAP + RIGHT, 480)
    H = TOP + CHART_H + BOTTOM

    bar_base_y = TOP + CHART_H  # 막대 하단 기준선 y
    max_cal   = sorted_list[0][1]["total_calories"] if sorted_list else 1
    total_cal = sum(v["total_calories"] for v in contributors.values())
    total_snack_count = sum(
        s["quantity"] * s.get("count_per_unit", 1)
        for info in contributors.values()
        for s in info["snacks"]
    )

    lines: list[str] = []

    # ── 배경 & 테두리 ─────────────────────────────────────────────
    lines.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
    lines.append(
        f'<rect width="{W}" height="{H}" rx="12" '
        f'fill="none" stroke="#E8E8E8" stroke-width="1"/>'
    )

    # ── 제목 ──────────────────────────────────────────────────────
    lines.append(
        f'<text x="{W // 2}" y="30" text-anchor="middle" '
        f'font-family="{FONT}" font-size="17" font-weight="700" fill="#1A1A2E">'
        f'&#x1F37F; Snack Calorie Rankings</text>'
    )
    lines.append(
        f'<text x="{W // 2}" y="52" text-anchor="middle" '
        f'font-family="{FONT}" font-size="12" fill="#999">'
        f'Total {total_cal:,} kcal &#160;·&#160; {n} contributors '
        f'&#160;·&#160; {total_snack_count:,} snacks</text>'
    )

    # ── Y축 격자선 + 레이블 ───────────────────────────────────────
    grid_steps = 5
    for i in range(grid_steps + 1):
        cal_val = round(max_cal * i / grid_steps)
        gy = bar_base_y - round(CHART_H * i / grid_steps)
        stroke = "#EFEFEF" if i > 0 else "#DDDDDD"
        lines.append(
            f'<line x1="{LEFT - 4}" y1="{gy}" x2="{W - RIGHT}" y2="{gy}" '
            f'stroke="{stroke}" stroke-width="1"/>'
        )
        label = f"{cal_val // 1000}k" if cal_val >= 1000 else str(cal_val)
        lines.append(
            f'<text x="{LEFT - 8}" y="{gy + 4}" text-anchor="end" '
            f'font-family="{FONT}" font-size="10" fill="#BBBBBB">{_esc(label)}</text>'
        )

    # ── 막대 (스택형) ─────────────────────────────────────────────
    for idx, (username, info) in enumerate(sorted_list):
        x = LEFT + idx * (BAR_W + GAP)
        total_h = max(round((info["total_calories"] / max_cal) * CHART_H), 2) if max_cal > 0 else 2
        bar_top_y = bar_base_y - total_h
        rank_label = RANK_ICONS[idx] if idx < 3 else f"#{idx + 1}"

        # 클립패스로 둥근 모서리 적용
        clip_id = f"c{idx}"
        lines.append(
            f'<clipPath id="{clip_id}">'
            f'<rect x="{x}" y="{bar_top_y}" width="{BAR_W}" height="{total_h}" rx="5"/>'
            f'</clipPath>'
        )

        # 막대 배경
        lines.append(
            f'<rect x="{x}" y="{bar_top_y}" width="{BAR_W}" '
            f'height="{total_h}" rx="5" fill="#F5F5F5"/>'
        )

        # 간식 세그먼트 (아래→위)
        lines.append(f'<g clip-path="url(#{clip_id})">')
        current_y = bar_base_y
        for s in reversed(info["snacks"]):
            seg_h = max(round((s["total_calories"] / max_cal) * CHART_H), 2) if max_cal > 0 else 2
            current_y -= seg_h
            color = snack_color.get(s["name"], "#CCC")
            lines.append(
                f'  <rect x="{x}" y="{current_y}" width="{BAR_W}" '
                f'height="{seg_h}" fill="{color}"/>'
            )
        lines.append('</g>')

        # 순위 이모지 (막대 위)
        lines.append(
            f'<text x="{x + BAR_W // 2}" y="{bar_top_y - 20}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="16" fill="#555">{_esc(rank_label)}</text>'
        )
        # 칼로리 수치 (막대 위, 순위 아래)
        lines.append(
            f'<text x="{x + BAR_W // 2}" y="{bar_top_y - 6}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="10" font-weight="600" fill="#444">'
            f'{info["total_calories"]:,}</text>'
        )

        # 기여자 이름 (막대 아래)
        display = info.get("display_name") or username
        name_label = display if display != username else f"@{username}"
        lines.append(
            f'<text x="{x + BAR_W // 2}" y="{bar_base_y + 18}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="12" font-weight="700" fill="#333">'
            f'{_esc(name_label)}</text>'
        )
        if display != username:
            lines.append(
                f'<text x="{x + BAR_W // 2}" y="{bar_base_y + 32}" text-anchor="middle" '
                f'font-family="{FONT}" font-size="10" fill="#BBBBBB">'
                f'@{_esc(username)}</text>'
            )

    # ── 범례 ─────────────────────────────────────────────────────
    if all_snack_names:
        legend_top = TOP + CHART_H + NAME_H + 4
        col_w = (W - LEFT - RIGHT) // LEGEND_COLS
        for i, name in enumerate(all_snack_names):
            col = i % LEGEND_COLS
            row = i // LEGEND_COLS
            lx = LEFT + col * col_w
            ly = legend_top + row * 22
            color = snack_color[name]
            lines.append(
                f'<rect x="{lx}" y="{ly}" width="11" height="11" rx="3" fill="{color}"/>'
            )
            lines.append(
                f'<text x="{lx + 15}" y="{ly + 9}" '
                f'font-family="{FONT}" font-size="11" fill="#666">{_esc(name)}</text>'
            )

    inner = "\n  ".join(lines)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n'
        f'  {inner}\n'
        f'</svg>\n'
    )


def save_chart(data: dict) -> bool:
    svg = generate_svg(data)
    if not svg:
        return False
    CHART_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHART_FILE, "w", encoding="utf-8") as f:
        f.write(svg)
    return True


# ---------------------------------------------------------------------------
# README 생성
# ---------------------------------------------------------------------------

def generate_readme(data: dict) -> str:
    contributors: dict = data.get("contributors", {})

    how_to = """
## 🙋 참여 방법

1. 이 저장소를 **Fork** 하세요
2. `contributions/` 폴더에 `{본인_GitHub_ID}.txt` 파일을 만드세요
3. 파일 **첫 줄**에 본인 이름을 추가하고, 이후 줄에 간식명과 개수를 입력하세요:
   ```
   # 이름: 홍길동
   오예스 12개
   초코파이 1박스
   콜라 3캔
   ```
   > `# 이름:` 줄은 선택사항입니다. 없으면 GitHub ID가 표시됩니다.
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

    # ── 상세 내역 (접을 수 있는 섹션) ──
    detail_blocks = []
    medals = ["🥇", "🥈", "🥉"]
    for rank, (username, info) in enumerate(sorted_list):
        badge = medals[rank] if rank < 3 else f"#{rank + 1}"
        display = info.get("display_name") or username
        name_label = f"{display} (@{username})" if display != username else f"@{username}"
        lines = [
            "<details>",
            f"<summary>{badge} <b>{name_label}</b> — {info['total_calories']:,} kcal</summary>",
            "",
            "| 간식 | 수량 | 낱개 구성 | 낱개 칼로리 | 합계 |",
            "|------|-----:|:---------:|------------:|-----:|",
        ]
        for s in info["snacks"]:
            count_per = s.get("count_per_unit", 1)
            ind_unit  = s.get("individual_unit", "개")
            cal_ind   = s.get("calories_per_individual", s["calories_per_unit"])
            # 묶음 단위일 때만 구성 표시 (count_per > 1)
            composition = (
                f"{count_per}{ind_unit}입"
                if count_per > 1
                else f"1{ind_unit}"
            )
            total_count = s["quantity"] * count_per
            lines.append(
                f"| {s['name']} | {s['quantity']}{s['unit']} "
                f"| {composition} (총 {total_count}{ind_unit}) "
                f"| {cal_ind:,} kcal/{ind_unit} "
                f"| **{s['total_calories']:,} kcal** |"
            )
        lines.append("")
        lines.append("</details>")
        detail_blocks.append("\n".join(lines))

    details = "\n\n".join(detail_blocks)

    # GitHub Actions 환경이면 raw URL, 로컬이면 상대경로
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "master")
    chart_url = (
        f"https://raw.githubusercontent.com/{repo}/{branch}/assets/histogram.svg"
        if repo else "assets/histogram.svg"
    )

    return f"""# 🍿 간식 칼로리 기여 현황

> 🍬 총 간식: **{total_snack_count:,}개** &nbsp;|&nbsp; 🔥 총 칼로리: **{total_cal:,} kcal** &nbsp;|&nbsp; 👥 참여자: **{len(contributors)}명**

## 🏆 랭킹

![histogram]({chart_url})

## 📋 상세 내역

{details}
{how_to}"""


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== 🍿 간식 칼로리 처리 시작 ===\n")

    data = load_data()
    cache: dict = data.get("calorie_cache", {})

    # 매번 전체를 재계산 (idempotent)
    new_contributors: dict = {}

    txt_files = sorted(CONTRIBUTIONS_DIR.glob("*.txt"))
    if not txt_files:
        print("contributions/ 폴더에 txt 파일이 없습니다.")

    for txt_file in txt_files:
        username = txt_file.stem
        if username.startswith(".") or username == "example":
            continue

        display_name, snacks = parse_contribution_file(txt_file)
        label = display_name or username
        print(f"\n👤 {label} (@{username})")
        if not snacks:
            print("  (항목 없음)")
            continue

        contributor: dict = {"total_calories": 0, "display_name": label, "snacks": []}
        for snack_name, quantity, unit in snacks:
            info = get_calorie_info(snack_name, unit, cache)
            cal_per_unit = info.get("calories_per_unit", 0)
            total = cal_per_unit * quantity
            contributor["snacks"].append(
                {
                    "name": snack_name,
                    "quantity": quantity,
                    "unit": info.get("unit", unit),
                    "count_per_unit": info.get("count_per_unit", 1),
                    "individual_unit": info.get("individual_unit", "개"),
                    "calories_per_unit": cal_per_unit,
                    "calories_per_individual": info.get("calories_per_individual", cal_per_unit),
                    "total_calories": total,
                }
            )
            contributor["total_calories"] += total

        new_contributors[username] = contributor
        print(f"  💪 소계: {contributor['total_calories']:,} kcal")

    data["contributors"] = new_contributors
    data["calorie_cache"] = cache

    save_data(data)
    print(f"\n💾 데이터 저장: {DATA_FILE}")

    save_chart(data)
    print(f"📊 차트 저장: {CHART_FILE}")

    readme = generate_readme(data)
    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"📄 README 업데이트: {README_FILE}")

    total = sum(v["total_calories"] for v in new_contributors.values())
    print(f"\n=== ✅ 완료 | 참여자 {len(new_contributors)}명 | 총 {total:,} kcal ===")


if __name__ == "__main__":
    main()
