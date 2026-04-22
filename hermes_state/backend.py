"""
StateBackend Protocol — all storage backends (SQLite, Postgres, …) must
implement every method defined here.  Parameter signatures are identical to
the original SessionDB class so all existing callers need zero changes.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class StateBackend(Protocol):
    # ── Session lifecycle ────────────────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> str: ...

    def end_session(self, session_id: str, end_reason: str) -> None: ...

    def reopen_session(self, session_id: str) -> None: ...

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None: ...

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        absolute: bool = False,
    ) -> None: ...

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
    ) -> None: ...

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]: ...

    def set_session_title(self, session_id: str, title: str) -> bool: ...

    def get_session_title(self, session_id: str) -> Optional[str]: ...

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]: ...

    def resolve_session_by_title(self, title: str) -> Optional[str]: ...

    def get_next_title_in_lineage(self, base_title: str) -> str: ...

    def get_compression_tip(self, session_id: str) -> Optional[str]: ...

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
    ) -> List[Dict[str, Any]]: ...

    # ── Message storage ──────────────────────────────────────────────────────

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
    ) -> int: ...

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]: ...

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]: ...

    # ── Search ───────────────────────────────────────────────────────────────

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]: ...

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]: ...

    # ── Utility ──────────────────────────────────────────────────────────────

    def session_count(self, source: str = None) -> int: ...

    def message_count(self, session_id: str = None) -> int: ...

    # ── Export & cleanup ─────────────────────────────────────────────────────

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    def export_all(self, source: str = None) -> List[Dict[str, Any]]: ...

    def clear_messages(self, session_id: str) -> None: ...

    def delete_session(self, session_id: str) -> bool: ...

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int: ...

    def close(self) -> None: ...
