"""Status constants shared across Guardian."""


class CheckinStatus:
    PENDING = "pending"        # scheduled, attempts in progress
    ANSWERED = "answered"      # Mom picked up
    MISSED = "missed"          # all attempts failed, escalation handled
    ESCALATED = "escalated"    # urgent alert sent after max attempts


class AttemptStatus:
    PLACED = "placed"
    ANSWERED = "answered"
    MISSED = "missed"
    FAILED = "failed"          # provider/technical error


class Outcome:
    """Phase 2.5 wellness-check outcomes. Only CONFIRMED_OK is a real pass."""
    CONFIRMED_OK = "confirmed_ok"            # she actively pressed the confirm digit
    ANSWERED_UNCONFIRMED = "answered_unconfirmed"  # connected, no confirmation (treat as a miss)
    MISSED = "missed"                        # no answer / busy
    FAILED = "failed"                        # technical/system error (NOT "Mom missed")


class TargetType:
    EXTENSION = "extension"    # Mom's 3CX extension
    CELL = "cell"             # Mom's cell phone backup
    FAMILY = "family"         # future: sibling/family contact
