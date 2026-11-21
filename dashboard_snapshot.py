"""Bounded, display-only replica of Jarvis research for the Render dashboard.

The GitHub Actions scanner and its authoritative JSON state are never modified.
Only the replica sent to Render is projected. Full per-scan evidence remains in
paper_tracker.json in GitHub and in GitHub Actions research exports.
"""

from __future__ import annotations

# The full per-scan observations dominate tracker size. These are preserved in
# GitHub but are not required to render the overview, cards or paper portfolio.
RECENT_SIGNAL_SNAPSHOTS_CURRENT = 3
RECENT_SIGNAL_SNAPSHOTS_ARCHIVED = 1
RECENT_ENTRY_OBSERVATIONS = 4
RECENT_PRICE_OBSERVATIONS = 2
RECENT_BUY_GATE_EVENTS_CURRENT = 5
RECENT_BUY_GATE_EVENTS_ARCHIVED = 1


def compact_paper_tracker(paper: dict) -> dict:
    """Return a new dictionary without modifying scanner-owned observations."""
    result = {}
    for key, record in paper.items():
        if not isinstance(record, dict):
            result[key] = record
            continue
        current = record.get("currently_surfaced") is True
        display = record.copy()
        # The large immutable research ledger stays in GitHub. Only recent
        # snapshots are needed for limited on-screen diagnostics.
        for field, count in (
            ("signal_history", RECENT_SIGNAL_SNAPSHOTS_CURRENT if current else RECENT_SIGNAL_SNAPSHOTS_ARCHIVED),
            ("paired_entry_history", RECENT_ENTRY_OBSERVATIONS if current else 0),
            ("price_history", RECENT_PRICE_OBSERVATIONS if current else 0),
            ("buy_gate_history", RECENT_BUY_GATE_EVENTS_CURRENT if current else RECENT_BUY_GATE_EVENTS_ARCHIVED),
        ):
            history = record.get(field)
            if isinstance(history, list):
                display[field] = history[-count:] if count else []
        result[key] = display
    return result


def build_render_snapshot(paper, lifetime, now, *, buy_signals=None,
                          scan_metadata=None, paper_account=None):
    """Project only the Render replica; preserve exact trading account/state."""
    scan_metadata = scan_metadata or {}
    # Compute full-fidelity signal and BUY-gate summaries in GitHub Actions
    # before projecting the historical per-candidate arrays. Render displays
    # these authoritative aggregates without holding the full event ledger.
    from runner_intelligence import build_runner_intelligence_report
    full_intelligence = build_runner_intelligence_report(list(paper.values()), buy_signals or [])
    return {
        "synced_at": now.isoformat(),
        "last_scan_completed_at": scan_metadata.get("last_scan_completed_at"),
        "scan_run_id": scan_metadata.get("run_id"),
        "paper_tracker": compact_paper_tracker(paper),
        "lifetime": lifetime,
        "buy_signals": buy_signals or [],
        "paper_trading": paper_account or {},
        "full_runner_intelligence": full_intelligence,
        "scan_diagnostics": scan_metadata.get("scan_diagnostics") or {},
        "render_compact_snapshot": True,
        "full_research_location": "GitHub: paper_tracker.json / GitHub Actions audit artifacts",
    }
