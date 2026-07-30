def build_glint_section(results_with_fundamentals):
    """Formats Glint's screen results into a plain text block, matching
    the style used for the IBKR portfolio section of the email."""
    candidates = [(r, f) for r, f in results_with_fundamentals if r.is_candidate]
    candidates.sort(key=lambda rf: rf[0].value_score, reverse=True)

    if not candidates:
        return "💎 No candidates cleared the screen today."

    lines = [f"💎 {len(candidates)} CANDIDATE(S) FOUND", ""]

    for r, f in candidates:
        price = f"${f.price:,.2f}" if f.price is not None else "n/a"
        lines.append(f"  {r.ticker:<6} {(f.sector or 'Sector unknown'):<20} {price:>12}  score {r.value_score}/3")
        if f.fifty_two_week_low is not None and f.fifty_two_week_high is not None:
            pct = f.price_position_pct
            pct_str = f"{pct:.0f}% up from the low" if pct is not None else "n/a"
            lines.append(f"      52-wk range ${f.fifty_two_week_low:,.2f} – ${f.fifty_two_week_high:,.2f} ({pct_str})")
        for reason in r.value_reasons:
            lines.append(f"      • {reason}")
        lines.append("")

    return "\n".join(lines).rstrip()
