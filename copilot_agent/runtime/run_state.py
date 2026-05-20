from __future__ import annotations

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_APPROVAL = "waiting_approval"
RUN_STATUS_CANCELLING = "cancelling"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

TERMINAL_RUN_STATUSES = {RUN_STATUS_CANCELLED, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED}
NON_TERMINAL_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_CANCELLING,
}
RUN_STATUSES = TERMINAL_RUN_STATUSES | NON_TERMINAL_RUN_STATUSES

ALLOWED_RUN_TRANSITIONS = {
    RUN_STATUS_QUEUED: {RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING, RUN_STATUS_FAILED},
    RUN_STATUS_RUNNING: {
        RUN_STATUS_WAITING_APPROVAL,
        RUN_STATUS_CANCELLING,
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
    },
    RUN_STATUS_WAITING_APPROVAL: {RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING, RUN_STATUS_COMPLETED},
    RUN_STATUS_CANCELLING: {RUN_STATUS_CANCELLED, RUN_STATUS_FAILED},
    RUN_STATUS_CANCELLED: set(),
    RUN_STATUS_COMPLETED: set(),
    RUN_STATUS_FAILED: set(),
}


class InvalidRunStatusError(ValueError):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"unknown run status: {status}")


class InvalidRunTransitionError(ValueError):
    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"invalid run transition: {from_status} -> {to_status}")


def validate_run_status(status: str) -> None:
    if status not in RUN_STATUSES:
        raise InvalidRunStatusError(status)


def validate_run_transition(from_status: str, to_status: str) -> None:
    validate_run_status(from_status)
    validate_run_status(to_status)
    if from_status == to_status:
        return
    if to_status not in ALLOWED_RUN_TRANSITIONS[from_status]:
        raise InvalidRunTransitionError(from_status, to_status)
