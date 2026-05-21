from __future__ import annotations

from copilot_agent.credentials.schema import CredentialBinding
from copilot_agent.credentials.store import InMemoryCredentialStore
from copilot_agent.scenario.schema import ScenarioResourcesConfig
from copilot_agent.tools.sanitize import redact_cookie_header


class CredentialManager:
    """M14 facade: Scenario binding metadata + in-memory secret store (single-process MVP)."""

    def __init__(
        self,
        *,
        binding: CredentialBinding,
        store: InMemoryCredentialStore,
        cookie_header_name: str = "",
    ) -> None:
        self._binding = binding
        self._store = store
        self._cookie_header_name = (cookie_header_name or "").strip()

    @property
    def binding(self) -> CredentialBinding:
        return self._binding

    @property
    def binding_id(self) -> str:
        return self._binding.binding_id

    @classmethod
    def from_scenario_resources(
        cls,
        resources: ScenarioResourcesConfig,
        *,
        ttl_seconds: int,
    ) -> CredentialManager:
        binding_id = (resources.credential_binding or "default").strip() or "default"
        provider = (resources.credential_provider or binding_id).strip() or binding_id
        scopes = list(resources.credential_scopes or ["http:read", "http:write"])
        binding = CredentialBinding(
            binding_id=binding_id,
            provider=provider,
            credential_type="cookie",
            scopes=scopes,
            storage="memory",
        )
        store = InMemoryCredentialStore(ttl_seconds=ttl_seconds)
        return cls(
            binding=binding,
            store=store,
            cookie_header_name=resources.credential_cookie_name,
        )

    def authorize_scopes(self, required_scopes: tuple[str, ...] | list[str]) -> bool:
        if not required_scopes:
            return True
        granted = set(self._binding.scopes)
        return all(scope in granted for scope in required_scopes)

    def get_cookie(
        self,
        thread_id: str,
        *,
        required_scopes: tuple[str, ...] | list[str] = ("http:read",),
    ) -> str | None:
        if not self.authorize_scopes(required_scopes):
            return None
        return self._store.get(thread_id)

    def set_cookie(self, thread_id: str, *, user_id: str, cookie_header: str) -> None:
        self._store.set(thread_id, user_id=user_id, secret=cookie_header)
        self._binding = self._binding.model_copy(
            update={"thread_id": thread_id, "user_id": user_id or self._binding.user_id}
        )

    def redact(self, cookie_header: str | None) -> str:
        return redact_cookie_header(cookie_header, cookie_name=self._cookie_header_name)

    def audit_ref(self, *, thread_id: str | None = None, user_id: str | None = None) -> dict[str, object]:
        payload = self._binding.audit_payload()
        if thread_id:
            payload["thread_id"] = thread_id
        if user_id:
            payload["user_id"] = user_id
        return payload
