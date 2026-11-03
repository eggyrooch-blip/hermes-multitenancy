"""Opaque identity proof issued only after a channel authenticates its caller."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_SEAL = object()
_OPAQUE_SUBJECT = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


@dataclass(frozen=True, slots=True)
class TrustedRuntimePrincipal:
    channel: str
    profile_name: str
    actor_subject: str
    credential_subject: str
    _seal: object = field(repr=False, compare=False, default=None)

    def is_authentic(self) -> bool:
        return self._seal is _SEAL


def issue_webui_principal(
    *, profile_name: str, actor_subject: str, credential_subject: str
) -> TrustedRuntimePrincipal:
    profile = str(profile_name or "").strip()
    actor = str(actor_subject or "").strip()
    credential = str(credential_subject or "").strip()
    if (
        not profile
        or not _OPAQUE_SUBJECT.fullmatch(actor)
        or credential != actor
    ):
        raise ValueError("trusted WebUI principal is incomplete or inconsistent")
    return TrustedRuntimePrincipal(
        channel="webui",
        profile_name=profile,
        actor_subject=actor,
        credential_subject=credential,
        _seal=_SEAL,
    )
