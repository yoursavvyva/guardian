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


class TargetType:
    EXTENSION = "extension"    # Mom's 3CX extension
    CELL = "cell"             # Mom's cell phone backup
    FAMILY = "family"         # future: sibling/family contact
