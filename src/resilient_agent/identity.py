import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException, status

SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    principal_id: str


def authenticated_identity(
    tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    principal_id: Annotated[str, Header(alias="X-Principal-Id")],
) -> Identity:
    if not SAFE_ID.fullmatch(tenant_id) or not SAFE_ID.fullmatch(principal_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid identity")
    return Identity(tenant_id=tenant_id, principal_id=principal_id)
