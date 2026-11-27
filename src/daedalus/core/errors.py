"""Errors raised by the write path (proposals, the queue, apply, logs).

Policy refusals are PolicyError, in daedalus.core.policy. Everything here is a
ProposalError. daedalus.core.queue re-exports these names.
"""

from __future__ import annotations


class ProposalError(Exception):
    """A proposal, apply, reject, or log write was refused."""


class ProposalTooLarge(ProposalError):
    pass


class DraftInvalid(ProposalError):
    def __init__(self, target: str, errors: list[str]) -> None:
        self.target = target
        self.errors = list(errors)
        super().__init__(f"{target}: draft refused:\n  " + "\n  ".join(self.errors))


class NotPending(ProposalError):
    pass


class StaleProposal(ProposalError):
    pass


class CommitFailed(ProposalError):
    def __init__(self, message: str, output: str) -> None:
        self.output = output
        super().__init__(f"{message}: {output}")


class LockBusy(ProposalError):
    pass


class UnknownProposal(ProposalError, KeyError):
    pass


class PlanApprovalNotYet(ProposalError):
    """A plan proposal can't go through the generic apply: approving it must also write its decisions."""
