"""
tech_fingerprint.py — OmniBase tech-stack & marketing-tool detector

Scans crawled page source (HTML/markdown text) for known fingerprints of
field-service software and marketing/tracking tools. Runs on content the
crawler already fetched — no extra network requests.

Output is a comma-joined string per category (or "" if nothing matched),
ready to drop straight into the companies.csv `tech_stack` / `marketing_tools`
fields.

Detection is substring/regex matching against lowercased source. It catches
what is exposed on the public site — strong signal, not exhaustive. Extend the
dictionaries below as you discover new fingerprints.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Fingerprint dictionaries
# ---------------------------------------------------------------------------
# Each tool maps to a list of fingerprint patterns. A pattern is a plain
# lowercase substring UNLESS it contains regex metacharacters, in which case it
# is treated as a regex. Keep patterns specific to avoid false positives.

FIELD_SERVICE_SOFTWARE: dict[str, list[str]] = {
    "ServiceTitan":    ["servicetitan", "goservicetitan", "st-scheduler", "servicetitan.com"],
    "Housecall Pro":   ["housecallpro", "housecall-pro", "housecall pro", "hcpapi", "online-booking.housecallpro"],
    "Jobber":          ["getjobber", "jobber.com", "jobber-widget", "clienthub.getjobber"],
    "Workiz":          ["workiz", "workiz.com"],
    "ServiceFusion":   ["servicefusion", "service fusion"],
    "FieldEdge":       ["fieldedge", "field edge"],
    "ServiceM8":       ["servicem8", "service m8"],
    "Kickserv":        ["kickserv"],
    "ThumbTack":       ["thumbtack.com/profile", "thumbtack widget"],
    "Podium":          ["podium.com", "widget.podium"],
    "Service Autopilot": ["serviceautopilot", "service autopilot"],
    "Acuity Scheduling": ["acuityscheduling", "acuity scheduling"],
    "Calendly":        ["calendly.com", "calendly-widget"],
}

MARKETING_TECH: dict[str, list[str]] = {
    "Scorpion":           ["scorpion", "scorpioncms", "scorpion.co", "cdn.scorpioncms"],
    "Google Analytics":   ["google-analytics.com", r"\bua-\d", r"\bg-[a-z0-9]{6,}", "gtag("],
    "Google Ads":         ["googleadservices", r"\baw-\d", "conversion_async", "googleads.g.doubleclick"],
    "Google Tag Manager": ["googletagmanager.com", r"\bgtm-[a-z0-9]+"],
    "Meta Pixel":         ["connect.facebook.net", "fbq(", "facebook pixel"],
    "HubSpot":            ["js.hs-scripts.com", "hubspot", "hs-analytics"],
    "Yelp":               ["yelp.com/biz", "yelp widget"],
    "WordPress":          ["wp-content", "wp-includes", "/wp-json"],
    "Wix":                ["wix.com", "wixstatic.com", "_wix"],
    "Squarespace":        ["squarespace.com", "static1.squarespace", "squarespace-cdn"],
    "Duda":               ["dudamobile", "duda.co", "_dm_"],
    "GoDaddy Website Builder": ["godaddy", "websitebuilder.com"],
    "CallRail":           ["callrail", "cdn.callrail"],
    "Hotjar":             ["hotjar.com", "hj("],
}


def _match_any(source_lc: str, patterns: list[str]) -> bool:
    """Return True if any pattern (substring or regex) is found in source_lc."""
    for pat in patterns:
        # Treat as regex if it contains regex metacharacters we use intentionally.
        if any(ch in pat for ch in r"\[](){}+*?^$|"):
            try:
                if re.search(pat, source_lc):
                    return True
            except re.error:
                # Malformed pattern — fall back to literal containment.
                if pat in source_lc:
                    return True
        else:
            if pat in source_lc:
                return True
    return False


def _scan(source_lc: str, table: dict[str, list[str]]) -> list[str]:
    """Return sorted list of tool names whose fingerprints appear in source."""
    return [name for name, pats in table.items() if _match_any(source_lc, pats)]


def detect_field_service(source: str) -> list[str]:
    """Detect field-service / booking software present in the page source."""
    if not source:
        return []
    return _scan(source.lower(), FIELD_SERVICE_SOFTWARE)


def detect_marketing(source: str) -> list[str]:
    """Detect marketing / tracking / CMS tools present in the page source."""
    if not source:
        return []
    return _scan(source.lower(), MARKETING_TECH)


def fingerprint(source: str) -> dict[str, str]:
    """
    Convenience wrapper. Given raw page source, returns:
        {"tech_stack": "ServiceTitan, CallRail",
         "marketing_tools": "Google Analytics, Scorpion"}
    Empty string for a category with no matches.
    """
    return {
        "tech_stack":      ", ".join(detect_field_service(source)),
        "marketing_tools": ", ".join(detect_marketing(source)),
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    samples = {
        "ST + GA + Scorpion": """
            <script src="https://cdn.scorpioncms.com/app.js"></script>
            <script>gtag('config','G-ABC123XYZ');</script>
            <a href="https://book.servicetitan.com/widget">Book now</a>
        """,
        "Housecall + GTM": """
            <iframe src="https://online-booking.housecallpro.com/x"></iframe>
            <script>(function(w,d){})('GTM-ABCD12');</script>
        """,
        "Nothing": "<html><body>Plain site, call us at 555-1234</body></html>",
    }
    for label, html in samples.items():
        fp = fingerprint(html)
        print(f"[{label}]")
        print(f"   tech_stack      = {fp['tech_stack']!r}")
        print(f"   marketing_tools = {fp['marketing_tools']!r}")
