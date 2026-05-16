from __future__ import annotations

from sqlalchemy import select

from database import Session
from models import TeamKey


TEAM_OPTIONS = ("AQUA", "TSUM", "NUR")


def normalize_team(team_name: str) -> str:
    t = (team_name or "").strip().upper()
    return t


async def get_team_api_key(session: Session, team_name: str) -> str | None:
    t = normalize_team(team_name)
    if t not in TEAM_OPTIONS:
        return None
    res = await session.execute(select(TeamKey).where(TeamKey.team_name == t))
    row = res.scalar_one_or_none()
    return row.team_api_key if row else None


async def set_team_api_key(session: Session, team_name: str, team_api_key: str | None) -> None:
    t = normalize_team(team_name)
    if t not in TEAM_OPTIONS:
        return
    res = await session.execute(select(TeamKey).where(TeamKey.team_name == t))
    row = res.scalar_one_or_none()
    if row is None:
        row = TeamKey(team_name=t, team_api_key=team_api_key)
        session.add(row)
    else:
        row.team_api_key = team_api_key
    await session.commit()
