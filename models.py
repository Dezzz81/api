from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[str | None] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    comment: Mapped[str | None] = mapped_column(String(255))
    client_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sub_id: Mapped[str | None] = mapped_column(String(64), index=True)
    vless_url: Mapped[str] = mapped_column(Text)
    inbound_id: Mapped[int] = mapped_column(Integer)
    server_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PaymentEvent(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[str | None] = mapped_column(String(64), index=True)
    client_uuid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
