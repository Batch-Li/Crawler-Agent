from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WechatKeywordSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=500)
    account_names: list[str] = Field(min_length=1, max_length=3, alias="accountNames")
    limit: int = Field(default=10, ge=1, le=20)
    freshness: Literal["cache_only", "prefer_cached", "prefer_fresh"] = "prefer_fresh"

    def normalized_accounts(self) -> list[str]:
        accounts = [name.strip() for name in self.account_names]
        if any(not name for name in accounts):
            raise ValueError("accountNames must not contain empty names")
        if len(set(accounts)) != len(accounts):
            raise ValueError("accountNames must be unique")
        return accounts
