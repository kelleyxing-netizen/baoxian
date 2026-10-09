"""审计留痕。

需要留痕的四类动作（对应需求的监管关注点）：
- 制度变更（谁改的、改前改后、何时生效）；
- 单据状态查询（谁查了谁的单据、依据什么授权）；
- 越权尝试（被拒绝也必须记录）；
- 由智能助手发起的写操作（发起报销单）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import AuditEvent

# 结果取值
ALLOWED = "allowed"
DENIED = "denied"


class AuditLog:
    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else None
        self._events: list[AuditEvent] = []
        if self.path and self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        actor: str,
        action: str,
        target: str,
        result: str = ALLOWED,
        detail: str = "",
    ) -> AuditEvent:
        event = AuditEvent(
            ts=datetime.now().isoformat(timespec="seconds"),
            actor=actor,
            action=action,
            target=target,
            result=result,
            detail=detail,
        )
        self._events.append(event)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return event

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def denied(self) -> list[AuditEvent]:
        return [e for e in self._events if e.result == DENIED]

    def find(self, action: str) -> list[AuditEvent]:
        return [e for e in self._events if e.action == action]
