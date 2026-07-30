"""Grounded Boardroom: per-member live web research feeding a synthesized
panel discussion, instead of one model call inventing 17 opinions.

Cost control: the full researched run only happens on Mondays or when the
composite signal is already AMBER/RED (spend the money when the water is
choppy). Calm weekdays fall back to the cheap single-call board in
undertow.py. Member research uses Haiku (cheap), synthesis uses Sonnet.
The five deceased members get no web searches - searching for "recent
comments" from dead men wastes money and invites invention. Their entries
are explicitly labeled as historical-framework-only.
"""

from __future__ import annotations

import os
import re
import json
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

RESEARCH_MODEL = "claude-haiku-4-5-20251001"
SYNTHESIS_MODEL = "claude-sonnet-4-6"

# Roster rebalance (2026-07-30): the original room leaned entirely toward
# crash-callers, which pre-loads the panel bearish regardless of the data.
# Jim Rogers (commodities, not an equity risk-on/off voice) -> Peter Lynch
# (stay-invested bottom-up optimist). Lee Robinson (low public footprint,
# searches rarely find anything) -> David Tepper (documented aggressive
# buyer *during* panics - a real counterweight, not a cheerleader).
LIVING_MEMBERS = [
    ("Warren Buffett", "long-term value, fear/greed cycles"),
    ("Michael Burry", "contrarian, hidden systemic risk"),
    ("Ray Dalio", "macro cycles, debt dynamics"),
    ("Stanley Druckenmiller", "macro momentum, asymmetric bets"),
    ("Howard Marks", "risk assessment, market psychology"),
    ("Paul Tudor Jones", "technical macro, crisis anticipation"),
    ("Jeffrey Gundlach", "fixed income, macro flows"),
    ("David Tepper", "buying panics, aggressive risk-on at extremes"),
    ("Nassim Taleb", "tail risk, fragility, black swans"),
    ("Peter Lynch", "bottom-up stock picking, stay-invested optimism"),
    ("George Soros", "reflexivity, currency macro bets"),
    ("Jim Simons", "quantitative pattern detection"),
]

GHOST_MEMBERS = [
    ("Jesse Livermore", "tape reading, market psychology"),
    ("Benjamin Graham", "margin of safety, intrinsic value"),
    ("Sir John Templeton", "contrarian global value"),
    ("Charlie Munger", "mental models, concentrated bets"),
    ("André Kostolany", "European macro, sentiment cycles"),
]


def should_run_full_research(signal: str) -> tuple[bool, str]:
    """Full researched board on Mondays, or any day the composite signal
    is already elevated. BOARDROOM_MODE env var (full/cheap) overrides."""
    mode = os.environ.get("BOARDROOM_MODE", "auto").lower()
    if mode == "full":
        return True, "forced by BOARDROOM_MODE=full"
    if mode == "cheap":
        return False, "forced by BOARDROOM_MODE=cheap"
    if signal in ("AMBER", "RED"):
        return True, f"composite signal is {signal} - elevated risk warrants fresh research"
    if datetime.datetime.now().weekday() == 0:
        return True, "Monday - weekly scheduled deep run"
    return False, "calm GREEN weekday - using cheap board to hold costs down"


def _call_anthropic(api_key, model, prompt, max_tokens, use_search, timeout):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    data = response.json()
    if "content" not in data:
        raise RuntimeError(f"API error: {json.dumps(data)[:300]}")
    return "\n".join(b["text"] for b in data["content"] if b.get("type") == "text")


def research_living_member(api_key, name, lens, signal, data_text, flags_text):
    date_str = datetime.datetime.now().strftime("%d %B %Y")
    prompt = f"""Today is {date_str}. You are a research assistant. Use web search to find what {name} ({lens}) has ACTUALLY said publicly in roughly the last 60 days - interviews, shareholder letters, public statements, notable filings or disclosed positioning.

Then output EXACTLY this format, nothing else:
FOUND: yes or no
TAKE: 2-4 sentences. If FOUND is yes, summarize their current actual stance grounded ONLY in what the search returned plus the market data below - never invent quotes or positions. If FOUND is no, the TAKE must begin with "No recent public comment found." and then apply their well-documented {lens} framework to the data below, clearly framed as framework-only, not as something they said.
VOTE: CONFIRM or UPGRADE or DOWNGRADE (relative to the current Undertow signal {signal}; UPGRADE means conditions warrant a more severe signal, DOWNGRADE less severe)
SOURCES: semicolon-separated URLs, or the word none

Current real Undertow market data:
{data_text}

Active stress flags:
{flags_text}

If search results are older than ~90 days, ambiguous, or about someone else, treat them as not found. Being honest about a gap is required; filling it with a plausible guess is forbidden."""

    text = _call_anthropic(api_key, RESEARCH_MODEL, prompt, 700, use_search=True, timeout=90)

    found_m = re.search(r"FOUND:\s*(yes|no)", text, re.IGNORECASE)
    take_m = re.search(r"TAKE:\s*(.+?)(?=\nVOTE:)", text, re.IGNORECASE | re.DOTALL)
    vote_m = re.search(r"VOTE:\s*(CONFIRM|UPGRADE|DOWNGRADE)", text, re.IGNORECASE)
    sources_m = re.search(r"SOURCES:\s*(.+)", text, re.IGNORECASE)

    return {
        "name": name,
        "lens": lens,
        "found": bool(found_m) and found_m.group(1).lower() == "yes",
        "take": take_m.group(1).strip() if take_m else text.strip()[:600],
        "vote": vote_m.group(1).upper() if vote_m else None,
        "sources": sources_m.group(1).strip() if sources_m else "none",
    }


def run_member_research(api_key, signal, data_text, flags_text, max_workers=4):
    """Researches all living members concurrently. A member whose research
    call fails outright is reported as an explicit failure entry - never
    silently dropped, and never replaced with an invented opinion."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(research_living_member, api_key, name, lens, signal, data_text, flags_text): name
            for name, lens in LIVING_MEMBERS
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                lens = dict(LIVING_MEMBERS)[name]
                results[name] = {
                    "name": name, "lens": lens, "found": False,
                    "take": f"RESEARCH FAILED ({e}) - no grounded view available for this member today.",
                    "vote": None, "sources": "none",
                }
                print(f"  ⚠️  Boardroom research failed for {name}: {e}", flush=True)
    # Preserve roster order
    return [results[name] for name, _ in LIVING_MEMBERS]


def run_full_boardroom(api_key, score_data, data_text, flags_text, research):
    """Synthesis pass: one Sonnet call that takes the 12 researched takes
    plus real data and produces an actual cross-discussion, the 5 ghost
    entries (framework-only, labeled as such), the verdict, and the
    machine-readable tally undertow.py already knows how to parse."""
    date_str = datetime.datetime.now().strftime("%d %B %Y")

    research_blocks = []
    for i, r in enumerate(research, start=1):
        vote = r["vote"] or "NO VOTE (research failed)"
        found = "recent public commentary FOUND" if r["found"] else "NO recent public commentary found"
        research_blocks.append(
            f"{i}. {r['name']} ({r['lens']}) - {found}\n   Researched take: {r['take']}\n   Researched vote: {vote}\n   Sources: {r['sources']}"
        )
    research_text = "\n\n".join(research_blocks)

    ghosts_text = "\n".join(
        f"{i}. {name} - {lens}" for i, (name, lens) in enumerate(GHOST_MEMBERS, start=len(LIVING_MEMBERS) + 1)
    )

    prompt = f"""Today is {date_str}. You are moderating The Boardroom - a council of investors reviewing the Undertow Index.

Current Undertow reading:
- Composite score: {score_data['score']}/{score_data['max']}
- Signal: {score_data['signal']}
- Summary: {score_data['summary']}

Live market data:
{data_text}

Active stress flags:
{flags_text}

LIVING MEMBERS (1-12): each has already been independently researched today. Their takes below are grounded in real, current search results (or explicitly note that nothing recent was found). You MUST base each living member's entry on their researched take verbatim in substance - do not add positions or opinions beyond it - and you MUST keep their researched vote exactly as given. Where a member's research FAILED, their entry must say so plainly and cast no vote counted in the tally; state this explicitly.

{research_text}

HISTORICAL MEMBERS (13-17): these investors are deceased. There is NO live commentary for them and you must not pretend otherwise. Each of their entries must open with "(historical framework - no live commentary)" and then apply their documented approach to today's data above. They each cast a vote based on that framework application.

{ghosts_text}

Write the session as a genuine discussion: members should react to the same shared data AND to each other's researched positions (e.g. if Tepper's researched stance conflicts with Burry's, have them engage on it) - not 17 disconnected monologues.

For every member, end their entry with a vote line formatted exactly like: "Vote: 🟡 UPGRADE" - emoji is the signal level the vote implies: 🟢 GREEN, 🟡 AMBER, 🔴 RED. CONFIRM implies the current signal ({score_data['signal']}); UPGRADE one level more severe; DOWNGRADE one level less severe; at an extreme, reuse the current signal's color.

Then a BOARDROOM VERDICT:
- Final consensus signal as emoji + word: 🟢 GREEN, 🟡 AMBER, or 🔴 RED
- 2-3 sentence synthesis
- Confidence (Low / Medium / High)
- A one-line note of how many living members had real recent commentary found vs not

CRITICAL: members appear in strict order 1-17, each exactly once, names bolded. Votes counted in the tally come only from members who actually cast one; if all 17 voted the counts MUST sum to 17, and if any research-failed members cast no vote, state the reduced total explicitly.

CRITICAL - MACHINE-READABLE TALLY: the very last line of your output must be exactly this format with real integer counts (nothing else on the line):
TALLY: CONFIRM=<n> UPGRADE=<n> DOWNGRADE=<n>"""

    return _call_anthropic(api_key, SYNTHESIS_MODEL, prompt, 5000, use_search=False, timeout=180)


def run_glint_review(api_key, research, glint_results, score_data):
    """The Boardroom's grounded view on Glint's current candidates - its
    own labeled section, separate from the Undertow signal."""
    candidates = [(r, f) for r, f in glint_results if r.is_candidate]
    if not candidates:
        return "No Glint candidates today - nothing for the Boardroom to review."

    lines = []
    for r, f in sorted(candidates, key=lambda rf: rf[0].value_score, reverse=True):
        pos = f.price_position_pct
        pos_str = f", {pos:.0f}% up from its 52-week low" if pos is not None else ""
        lines.append(f"- {f.ticker} ({f.sector or 'sector unknown'}): value score {r.value_score}/3, price {f.price}{pos_str}; passed: {'; '.join(r.value_reasons)}")
    candidates_text = "\n".join(lines)

    research_text = "\n".join(
        f"- {r['name']}: {'[researched]' if r['found'] else '[no recent commentary]'} {r['take']}"
        for r in research
    )

    prompt = f"""The Undertow Boardroom has just reviewed today's markets (current signal: {score_data['signal']}, composite {score_data['score']}/{score_data['max']}). The panel's researched member views today:

{research_text}

Glint (a quality-value screener) surfaced these candidates today:

{candidates_text}

For EACH candidate, give the panel's grounded view in 1-2 sentences: do today's actual conditions and the panel's researched sentiment support or caution against it right now? Ground every claim in the member views or candidate data above - if the panel's research says nothing relevant to a particular stock or its sector, say exactly that ("panel research today contains nothing specific to this name") rather than inventing a view. End each candidate with "Panel lean: SUPPORT / NEUTRAL / CAUTION".

Close with one sentence reminding that this is screening commentary, not financial advice."""

    return _call_anthropic(api_key, SYNTHESIS_MODEL, prompt, 1500, use_search=False, timeout=90)


def log_run(mode, reason, research, tally, final_signal_data, score_data):
    """Structured stdout log per run so drift/bias is visible over time in
    Railway's logs: which members had real commentary, tally, direction."""
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "boardroom_mode": mode,
        "mode_reason": reason,
        "composite_score": score_data["score"],
        "composite_signal": score_data["signal"],
        "final_signal": final_signal_data["signal"],
        "overridden": final_signal_data["overridden"],
        "tally": tally,
        "members_with_recent_commentary": [r["name"] for r in research if r["found"]] if research else None,
        "members_without": [r["name"] for r in research if not r["found"]] if research else None,
    }
    print(f"BOARDROOM_RUN_LOG: {json.dumps(entry)}", flush=True)
