import json
import re
from pathlib import Path
from sqlalchemy.orm import Session
from ..config import settings
from ..models.schema import Company

def _normalize_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"[.,'\"()\-/&]", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return " ".join(n.split())

def seed_companies(db: Session, seed_file: Path = settings.COMPANIES_SEED_PATH) -> int:
    if not seed_file.exists():
        return 0

    with open(seed_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    existing_tickers = {c.ticker for c in db.query(Company.ticker).filter(Company.ticker.isnot(None)).all()}
    existing_ciks = {c.cik for c in db.query(Company.cik).filter(Company.cik.isnot(None)).all()}
    existing_names = {_normalize_name(c.name) for c in db.query(Company.name).all()}

    count = 0
    for item in data:
        raw_ticker = item.get("ticker")
        raw_cik = item.get("cik")
        ticker = raw_ticker.upper().strip() if raw_ticker else None
        cik = str(raw_cik).zfill(10) if raw_cik else None
        norm_name = _normalize_name(item["name"])

        if ticker and ticker in existing_tickers:
            continue
        if cik and cik in existing_ciks:
            continue
        if not ticker and not cik and norm_name in existing_names:
            continue

        company = Company(
            name=item["name"],
            ticker=ticker,
            cik=cik,
            industry=item.get("industry", "General"),
            sector=item.get("sector", "General"),
            naics=item.get("naics"),
            aliases=item.get("aliases", []),
            description=item.get("description", "")
        )
        db.add(company)
        count += 1
        if ticker:
            existing_tickers.add(ticker)
        if cik:
            existing_ciks.add(cik)
        existing_names.add(norm_name)

    db.commit()
    return count
