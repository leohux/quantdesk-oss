# -*- coding: utf-8 -*-
"""SEC EDGAR real-time filings → NewsInbox (free official feed).

- Form 8-K Atom + item codes (domestic issuers)
- Form 6-K Atom + free-text catalyst heuristics (FPI / China ADRs)
- Round-robin submissions JSON for TECH_UNIVERSE CIKs (both forms)

User-Agent must include a contact email (SEC fair-access policy).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .news_inbox import INBOX
from .universe import (
    FPI_TICKERS,
    TECH_SET,
    TECH_UNIVERSE,
    canonical_symbol,
)

UA = os.environ.get(
    "SEC_USER_AGENT",
    "QuantDesk NewsTrader quantdesk@example.com",
)
ATOM_8K_URL = os.environ.get(
    "NEWS_TRADER_SEC_ATOM_8K_URL",
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&owner=include&count=100&output=atom",
)
ATOM_6K_URL = os.environ.get(
    "NEWS_TRADER_SEC_ATOM_6K_URL",
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=6-K&owner=include&count=100&output=atom",
)
POLL_SEC = float(os.environ.get("NEWS_TRADER_SEC_POLL_SEC", "15"))
MAX_AGE_HOURS = float(os.environ.get("NEWS_TRADER_SEC_MAX_AGE_HOURS", "18"))


def _default_tickers_cache() -> Path:
    env = os.environ.get("NEWS_TRADER_SEC_TICKERS_CACHE")
    if env:
        return Path(env)
    for p in (
        Path("/app/data/cache/sec_company_tickers.json"),
        Path(__file__).resolve().parents[1] / "data" / "cache" / "sec_company_tickers.json",
    ):
        if p.parent.exists() or p.exists():
            return p
    return Path("data/cache/sec_company_tickers.json")


TICKERS_CACHE = _default_tickers_cache()

HIGH_SIGNAL_ITEMS = {
    "1.01",
    "1.02",
    "1.03",
    "1.05",
    "2.01",
    "2.02",
    "2.03",
    "2.04",
    "2.05",
    "2.06",
    "3.01",
    "4.01",
    "4.02",
    "5.01",
    "5.02",
    "5.03",
}
ITEM_CATALYST = {
    "2.02": "earnings",
    "2.01": "contract",
    "1.01": "contract",
    "1.02": "contract",
    "5.02": "management",
    "5.01": "management",
    "1.05": "cyber",
    "3.01": "other",
    "4.02": "other",
    "2.05": "other",
    "2.06": "other",
}
# 6-K has no standard item codes — keyword heuristics on title/summary/exhibits
_6K_CATALYST_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "earnings",
        (
            "earnings",
            "results of operations",
            "financial results",
            "interim report",
            "unaudited",
            "annual report",
            "half year",
            "half-year",
            "quarterly",
            "q1 ",
            "q2 ",
            "q3 ",
            "q4 ",
            "fy20",
            "fy21",
            "fy22",
            "fy23",
            "fy24",
            "fy25",
            "fy26",
        ),
    ),
    (
        "contract",
        (
            "agreement",
            "acquisition",
            "merger",
            "share purchase",
            "investment agreement",
            "strategic cooperation",
            "partnership",
            "joint venture",
        ),
    ),
    (
        "management",
        (
            "director",
            "officer",
            "chief executive",
            "chief financial",
            "appoint",
            "resignation",
            "resign",
            "board change",
        ),
    ),
    (
        "product",
        ("product launch", "new model", "unveil", "technology"),
    ),
]

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_log: Callable[[str], None] = print
_thread: threading.Thread | None = None
_stop = threading.Event()
_cik_to_ticker: dict[str, str] = {}
_ticker_to_cik: dict[str, str] = {}
_unmapped: list[str] = []


def _http_bytes(url: str, *, timeout: float = 45.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/atom+xml,application/json,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_json(url: str) -> Any:
    return json.loads(_http_bytes(url).decode("utf-8"))


def load_ticker_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Return (cik10→preferred ticker, ticker→cik10). Logs gaps vs TECH_UNIVERSE."""
    global _cik_to_ticker, _ticker_to_cik, _unmapped
    if _cik_to_ticker and _ticker_to_cik:
        return _cik_to_ticker, _ticker_to_cik
    data: Any
    if TICKERS_CACHE.exists():
        data = json.loads(TICKERS_CACHE.read_text(encoding="utf-8"))
    else:
        data = _http_json("https://www.sec.gov/files/company_tickers.json")
        try:
            TICKERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TICKERS_CACHE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    sec_ticker_to_cik: dict[str, str] = {}
    for row in (data.values() if isinstance(data, dict) else data):
        t = str(row.get("ticker") or "").upper()
        if not t:
            continue
        sec_ticker_to_cik[t] = str(int(row["cik_str"])).zfill(10)

    # Aliases: SQ→XYZ etc. Resolve universe symbols to a CIK.
    tick_to: dict[str, str] = {}
    unmapped: list[str] = []
    for sym in TECH_UNIVERSE:
        canon = canonical_symbol(sym)
        cik = sec_ticker_to_cik.get(canon) or sec_ticker_to_cik.get(sym)
        if cik:
            tick_to[sym] = cik
            tick_to[canon] = cik
        else:
            unmapped.append(sym)

    # Prefer earlier TECH_UNIVERSE listing when multiple tickers share a CIK
    # (e.g. GOOGL before GOOG).
    prefer = {canonical_symbol(s): i for i, s in enumerate(TECH_UNIVERSE)}
    cik_to: dict[str, str] = {}
    for sym, cik in tick_to.items():
        if sym not in TECH_SET and canonical_symbol(sym) not in TECH_SET:
            continue
        cur = cik_to.get(cik)
        if cur is None or prefer.get(sym, 10**6) < prefer.get(cur, 10**6):
            cik_to[cik] = sym

    _cik_to_ticker, _ticker_to_cik, _unmapped = cik_to, tick_to, unmapped
    return cik_to, tick_to


def mapping_report() -> dict[str, Any]:
    cik_to, tick_to = load_ticker_maps()
    mapped_syms = sorted({canonical_symbol(s) for s in TECH_UNIVERSE if s in tick_to})
    return {
        "universe": len(TECH_UNIVERSE),
        "unique_ciks": len(cik_to),
        "mapped_symbols": len(mapped_syms),
        "unmapped": list(_unmapped),
        "fpi": sorted(FPI_TICKERS & TECH_SET),
        "dual_class_ciks": sorted(
            {
                c
                for c, t in cik_to.items()
                if sum(1 for s, c2 in _ticker_to_cik.items() if c2 == c and s in TECH_UNIVERSE)
                > 1
            }
        ),
    }


def _parse_items(summary_html: str) -> list[tuple[str, str]]:
    text = (
        summary_html.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("<br>", "\n")
        .replace("<br/>", "\n")
    )
    text = re.sub(r"<[^>]+>", " ", text)
    out: list[tuple[str, str]] = []
    for m in re.finditer(
        r"Item\s+(\d+\.\d+)\s*:\s*([^\n\r]+)",
        text,
        flags=re.I,
    ):
        out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def _strip_html(summary_html: str) -> str:
    text = (
        summary_html.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("<br>", "\n")
        .replace("<br/>", "\n")
    )
    return re.sub(r"<[^>]+>", " ", text)


def _catalyst_from_items(codes: list[str]) -> str:
    for c in codes:
        if c in ITEM_CATALYST:
            return ITEM_CATALYST[c]
    return "other"


def _catalyst_from_6k_text(text: str) -> str | None:
    low = (text or "").lower()
    for cat, keys in _6K_CATALYST_RULES:
        if any(k in low for k in keys):
            return cat
    return None


def _to_utc_iso(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _age_ok(created_iso: str) -> bool:
    try:
        age_h = (
            datetime.now(timezone.utc) - datetime.fromisoformat(created_iso)
        ).total_seconds() / 3600.0
        return age_h <= MAX_AGE_HOURS
    except Exception:
        return False


def _accession_from_entry(entry: ET.Element, summary: str) -> str | None:
    id_text = entry.findtext("a:id", default="", namespaces=_ATOM_NS) or ""
    am = re.search(r"accession-number=([0-9\-]+)", id_text)
    if am:
        return am.group(1)
    sm = re.search(r"AccNo:\s*</b>\s*([0-9\-]+)", summary, flags=re.I)
    if not sm:
        sm = re.search(r"AccNo:\s*([0-9\-]+)", summary, flags=re.I)
    return sm.group(1) if sm else None


def _entry_to_item(
    entry: ET.Element,
    cik_to: dict[str, str],
    *,
    form_hint: str,
) -> dict[str, Any] | None:
    title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
    m = re.search(r"\((\d{7,10})\)", title)
    if not m:
        return None
    cik = m.group(1).zfill(10)
    ticker = cik_to.get(cik)
    if not ticker:
        return None
    ticker = canonical_symbol(ticker)
    summary = entry.findtext("a:summary", default="", namespaces=_ATOM_NS) or ""
    plain = _strip_html(summary)
    updated = entry.findtext("a:updated", default="", namespaces=_ATOM_NS)
    created = _to_utc_iso(updated)
    if not _age_ok(created):
        return None
    acc = _accession_from_entry(entry, summary)
    if not acc:
        return None
    link_el = entry.find("a:link", namespaces=_ATOM_NS)
    href = link_el.get("href") if link_el is not None else None

    form = form_hint
    cat_el = entry.find("a:category", namespaces=_ATOM_NS)
    if cat_el is not None and cat_el.get("term"):
        form = cat_el.get("term") or form_hint

    codes: list[str] = []
    cat: str | None = None
    if str(form).upper().startswith("8-K"):
        items = _parse_items(summary)
        codes = [c for c, _ in items]
        if not any(c in HIGH_SIGNAL_ITEMS for c in codes):
            return None
        cat = _catalyst_from_items(codes)
        labels = "; ".join(f"{c} {lab}" for c, lab in items[:6])
        headline = f"SEC 8-K {ticker}: Items {','.join(codes)}"
        body = labels or f"Form 8-K filed for {ticker}"
    elif str(form).upper().startswith("6-K"):
        # FPI path — no item codes; require keyword hit to avoid monthly-return spam
        blob = f"{title}\n{plain}"
        cat = _catalyst_from_6k_text(blob)
        if cat is None:
            return None
        # Prefer mapping CIK→FPI ticker; still accept if domestic somehow files 6-K
        headline = f"SEC 6-K {ticker}: {cat}"
        # Keep a readable slice of the atom summary / exhibit hints
        body = re.sub(r"\s+", " ", plain).strip()[:500] or f"Form 6-K filed for {ticker}"
    else:
        return None

    return {
        "id": f"sec:{acc}",
        "headline": headline,
        "summary": body,
        "symbols": [ticker],
        "created_at": created,
        "url": href,
        "source": "sec_edgar",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "feed": "sec_edgar",
        "sec_form": str(form),
        "sec_accession": acc,
        "sec_cik": cik,
        "sec_items": codes,
        "catalyst_type": cat,
    }


def _poll_atom(url: str, *, form_hint: str) -> int:
    cik_to, _ = load_ticker_maps()
    if not cik_to:
        return 0
    raw = _http_bytes(url)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("iso-8859-1", errors="replace")
    root = ET.fromstring(text)
    n = 0
    for entry in root.findall("a:entry", _ATOM_NS):
        item = _entry_to_item(entry, cik_to, form_hint=form_hint)
        if item is None:
            continue
        if INBOX.push_raw(item, feed="sec_edgar"):
            n += 1
            _log(
                f"sec-edgar + {item['symbols'][0]} form={item.get('sec_form')} "
                f"items={item.get('sec_items') or '-'} cat={item.get('catalyst_type')}"
            )
    return n


def poll_atom_once() -> int:
    """Poll 8-K and 6-K current feeds."""
    n8 = _poll_atom(ATOM_8K_URL, form_hint="8-K")
    n6 = _poll_atom(ATOM_6K_URL, form_hint="6-K")
    return n8 + n6


def poll_submissions_round_robin(state: dict[str, Any]) -> int:
    """Slow CIK walk via submissions API (8-K + 6-K, age-gated)."""
    _, tick_to = load_ticker_maps()
    tickers = [t for t in TECH_UNIVERSE if t in tick_to]
    if not tickers:
        return 0
    idx = int(state.get("rr_idx") or 0) % len(tickers)
    batch = 3
    added = 0
    for j in range(batch):
        t = tickers[(idx + j) % len(tickers)]
        cik = tick_to[t]
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            data = _http_json(url)
        except Exception as exc:
            _log(f"sec-edgar submissions {t}: {exc}")
            time.sleep(0.2)
            continue
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        items_arr = recent.get("items") or []
        accs = recent.get("accessionNumber") or []
        accepts = recent.get("acceptanceDateTime") or []
        primary = recent.get("primaryDocDescription") or []
        for i, form in enumerate(forms[:20]):
            fu = str(form).upper()
            is_8k = fu.startswith("8-K")
            is_6k = fu.startswith("6-K")
            if not is_8k and not is_6k:
                continue
            acc = accs[i] if i < len(accs) else None
            if not acc:
                continue
            accepted = accepts[i] if i < len(accepts) else None
            created = _to_utc_iso(
                str(accepted).replace("Z", "+00:00") if accepted else None
            )
            if not _age_ok(created):
                continue
            codes: list[str] = []
            cat: str | None = None
            desc = str(primary[i] if i < len(primary) else "")
            if is_8k:
                items_str = str(items_arr[i] if i < len(items_arr) else "")
                codes = [c.strip() for c in items_str.split(",") if c.strip()]
                if not any(c in HIGH_SIGNAL_ITEMS for c in codes):
                    continue
                cat = _catalyst_from_items(codes)
                headline = f"SEC 8-K {t}: Items {','.join(codes)}"
                body = f"Form {form} items {items_str}"
            else:
                cat = _catalyst_from_6k_text(f"{desc} {form}")
                if cat is None:
                    # submissions often lack exhibit text — keep FPI 6-K with weak tag
                    if canonical_symbol(t) not in FPI_TICKERS and t not in FPI_TICKERS:
                        continue
                    cat = "other"
                headline = f"SEC 6-K {t}: {cat}"
                body = desc or f"Form {form} filed for {t}"
            raw = {
                "id": f"sec:{acc}",
                "headline": headline,
                "summary": body,
                "symbols": [canonical_symbol(t)],
                "created_at": created,
                "url": (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm"
                ),
                "source": "sec_edgar",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "sec_form": str(form),
                "sec_accession": acc,
                "sec_cik": cik,
                "sec_items": codes,
                "catalyst_type": cat,
            }
            if INBOX.push_raw(raw, feed="sec_edgar"):
                added += 1
        time.sleep(0.15)
    state["rr_idx"] = (idx + batch) % len(tickers)
    return added


def _thread_main() -> None:
    rr_state: dict[str, Any] = {"rr_idx": 0}
    try:
        rep = mapping_report()
        _log(
            f"sec-edgar maps ready universe={rep['universe']} "
            f"unique_ciks={rep['unique_ciks']} mapped_symbols={rep['mapped_symbols']} "
            f"unmapped={rep['unmapped'] or '[]'} "
            f"fpi={rep['fpi']} poll={POLL_SEC}s"
        )
        if rep["unmapped"]:
            _log(
                f"sec-edgar UNMAPPED tickers (no CIK): {', '.join(rep['unmapped'])} "
                f"— check aliases / company_tickers refresh"
            )
    except Exception as exc:
        _log(f"sec-edgar ticker map fail: {exc}")
    while not _stop.is_set():
        try:
            n_atom = poll_atom_once()
            n_rr = poll_submissions_round_robin(rr_state)
            if n_atom or n_rr:
                _log(f"sec-edgar cycle atom+{n_atom} submissions+{n_rr}")
        except Exception as exc:
            _log(f"sec-edgar poll fail: {exc}")
        _stop.wait(POLL_SEC)


def start_sec_edgar(log: Callable[[str], None] | None = None) -> None:
    global _thread, _log
    if log is not None:
        _log = log
    if os.environ.get("NEWS_TRADER_SEC_EDGAR", "1") != "1":
        _log("sec-edgar disabled (NEWS_TRADER_SEC_EDGAR!=1)")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_thread_main, name="sec-edgar", daemon=True)
    _thread.start()
    _log("sec-edgar thread started")


def stop_sec_edgar() -> None:
    _stop.set()
