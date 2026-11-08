"""HTML part of the scout email, neo-brutalist. Tables and inline styles only, so Gmail renders it.

Plain text stays the canonical body; compose() runs this page through the same redaction gate
and drops it (text-only mail) if anything credential-shaped survives."""
import re
from html import escape
from string import Template

INK, CREAM, WHITE, RED, YELLOW, VIOLET = "#000000", "#FFFDF5", "#FFFFFF", "#FF6B6B", "#FFD93D", "#C4B5FD"
FONT = "'Space Grotesk',Helvetica,Arial,sans-serif"
LABEL = f"font-family:{FONT};font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:{INK}"
BODY = f"font-family:{FONT};font-size:16px;line-height:1.5;font-weight:500;color:{INK}"
MONO = "font-family:Menlo,Consolas,monospace;font-size:13px;line-height:1.5;color:#000000"
CODE = (f"font-family:Menlo,Consolas,monospace;font-size:0.9em;background:{YELLOW};"
        f"border:1px solid {INK};border-radius:3px;padding:1px 5px;color:{INK}")

PAGE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>$subject</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&amp;display=swap" rel="stylesheet">
</head><body style="margin:0;padding:0;background:#FFFDF5">
<div style="display:none;max-height:0;overflow:hidden">$preheader</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#FFFDF5;background-image:radial-gradient(#000000 1.5px,transparent 1.5px);background-size:20px 20px">
<tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#FFFDF5;border:4px solid #000000;box-shadow:12px 12px 0 #000000">
<tr><td style="padding:20px 24px;border-bottom:4px solid #000000">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
  <td><span style="display:inline-block;background:#FFD93D;border:4px solid #000000;padding:6px 12px;font-family:$font;font-size:20px;font-weight:700;text-transform:uppercase;color:#000000">issue-scout</span></td>
  <td align="right"><span style="display:inline-block;background:$badge_bg;border:4px solid #000000;border-radius:999px;padding:6px 14px;$label">$badge</span></td>
 </tr></table>
</td></tr>
<tr><td style="background:#000000;padding:32px 24px;border-bottom:4px solid #000000">
 <div style="$label;color:#FFD93D">$ref</div>
 <div style="font-family:$font;font-size:30px;line-height:1.1;font-weight:700;color:#FFFFFF;padding:10px 0 14px">$title</div>
 <div style="font-family:$font;font-size:16px;line-height:1.5;font-weight:700;color:#FFFFFF;padding-bottom:22px">Why this one: $why</div>
 <a href="$url" style="display:inline-block;background:#FF6B6B;border:4px solid #FFFFFF;padding:14px 22px;font-family:$font;font-size:15px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#000000;text-decoration:none">Claim it now &rarr;</a>
</td></tr>
<tr><td style="border-bottom:4px solid #000000"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>$stats</tr></table></td></tr>
<tr><td style="padding:28px 0 4px">$sections</td></tr>
<tr><td style="background:#FFD93D;border-top:4px solid #000000;padding:24px">
 <div style="$label">$footer_note</div>
 <div style="$body;font-size:14px;padding-top:10px">Nothing is pushed or commented on GitHub until you do it yourself. Full validation logs are in the plain-text part and the saved report.</div>
</td></tr>
</table></td></tr></table></body></html>
""")


def _t(value):
    """Escape for HTML, keep line breaks, render `function names` as inline-code chips."""
    html = escape(str(value or "")).replace("\n", "<br>")
    return re.sub(r"`([^`]+)`", lambda m: f'<code style="{CODE}">{m.group(1)}</code>', html)


def _section(title, inner, head=YELLOW):
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:0 24px 24px">'
            f'<tr><td style="border:4px solid {INK};background:{WHITE}">'
            f'<div style="background:{head};border-bottom:4px solid {INK};padding:10px 14px;{LABEL}">{escape(title)}</div>'
            f'<div style="padding:14px;{BODY}">{inner}</div></td></tr></table>')


def _items(items):
    return "".join(f"&#9733;&nbsp;{_t(i)}<br>" for i in items or []) or "&mdash;"


def _stat(label, value, bg, last=False):
    edge = "" if last else f"border-right:4px solid {INK};"
    return (f'<td width="25%" valign="top" style="{edge}background:{bg};padding:14px 12px">'
            f'<div style="font-family:{FONT};font-size:22px;font-weight:700;color:{INK}">{_t(value)}</div>'
            f'<div style="{LABEL};font-size:11px;letter-spacing:1px;padding-top:4px">{escape(label)}</div></td>')


def render(state, score):
    """-> the HTML email for one picked issue. score is passed in to keep this module import-free."""
    issue, r = state["issue"], state["result"]
    kind, language = issue.get("work_type", "code"), issue.get("language", "python")
    pol = issue["policy"]
    url = issue["html_url"] if str(issue["html_url"]).startswith("https://") else "#"
    failed = "error" in r
    if failed:
        stats = (_stat("Fix", "none", RED) + _stat("Score", score, YELLOW) + _stat("Type", kind, VIOLET)
                 + _stat("Language", language, WHITE, last=True))
        sections = _section("No fix this time", _t(r["error"]), head=RED)
    else:
        b = r["brief"]
        check = "Checks" if kind == "documentation" else "Tests"
        proof = "n/a" if kind == "documentation" else ("yes" if r.get("reproduces") else "not shown")
        cost = r.get("cost")
        cost_text = f"${cost:.2f}" if isinstance(cost, (int, float)) else "n/a"
        stats = (_stat(check, "pass" if r["passed"] else "FAIL", YELLOW if r["passed"] else RED)
                 + _stat("Fails unfixed", proof, VIOLET) + _stat("Score", score, WHITE)
                 + _stat("Usage est.", cost_text, CREAM, last=True))
        policy = f"AI policy: {pol['status']}" + (f' &mdash; &ldquo;{_t(pol["quote"])}&rdquo;' if pol.get("quote") else "")
        sections = "".join([
            _section("1) Claim it: comment on the issue", f'<div style="{MONO}">{_t(b["claim_comment"])}</div>', head=RED),
            _section("Root cause", _t(b["root_cause"]), head=VIOLET),
            _section("Approach", _t(b["approach"]), head=VIOLET),
            _section("Risks", _t(b["risks"])),
            _section("Not verified", _t(b["not_verified"])),
            _section("Questions a maintainer may ask", _items(b["maintainer_questions"])),
            _section("Before opening the PR", f"{policy}<br><br>{_items(b['checklist'])}", head=RED),
            _section("Commit & PR", f'<div style="{LABEL}">Commit message</div><div style="{MONO};padding:6px 0 14px">{_t(b["commit_message"])}</div>'
                     f'<div style="{LABEL}">PR title</div><div style="padding:6px 0 14px;font-weight:700">{_t(b["pr_title"])}</div>'
                     f'<div style="{LABEL}">PR body</div><div style="{MONO};padding-top:6px">{_t(b["pr_body"])}</div>'),
        ])
    return PAGE.substitute(
        subject=_t(f"[scout] {issue['repo']}#{issue['number']}: {issue['title']}"),
        preheader=_t(f"No fix this time for {issue['repo']}#{issue['number']}" if failed
                     else f"Fix drafted for {issue['repo']}#{issue['number']}. Claim it first."),
        font=FONT, label=LABEL, body=BODY,
        badge_bg=RED if failed else YELLOW, badge=_t(f"{kind} · {language}"),
        ref=_t(f"{issue['repo']}#{issue['number']}"), title=_t(issue["title"]), why=_t(state.get("why")),
        url=escape(url), stats=stats, sections=sections,
        footer_note="No patch attached" if failed else "Patch attached",
    )
