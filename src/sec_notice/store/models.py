"""SQLAlchemy ORM models for the filing store.

Phase 1 uses companies / filings / documents. The `analyses` and
`notifications` tables from CONCEPT.md arrive in later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(255))
    ticker: Mapped[str | None] = mapped_column(String(32))
    sic: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    filings: Mapped[list["Filing"]] = relationship(back_populates="company")


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("accession_no", name="uq_filings_accession"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    accession_no: Mapped[str] = mapped_column(String(32), index=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik"), index=True)
    form_type: Mapped[str | None] = mapped_column(String(32))
    filing_date: Mapped[str | None] = mapped_column(String(16))
    period_of_report: Mapped[str | None] = mapped_column(String(16))
    primary_doc_url: Mapped[str | None] = mapped_column(Text)
    # new -> fetched -> analysed -> notified  (drives idempotent pipeline)
    status: Mapped[str] = mapped_column(String(16), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    company: Mapped["Company"] = relationship(back_populates="filings")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="filing", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("filing_id", "filename", name="uq_documents_filing_filename"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    doc_type: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    filing: Mapped["Filing"] = relationship(back_populates="documents")
