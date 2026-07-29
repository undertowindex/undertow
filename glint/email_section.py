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
        for reason in r.value_reasons:
            lines.append(f"      • {reason}")
        lines.append("")

    return "\n".join(lines).rstrip()
