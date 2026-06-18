"""Status constants shared across Guardian."""


class CheckinStatus:
    PENDING = "pending"        # scheduled, attempts in progress
    ANSWERED = "answered"      # Mom picked up and confirmed she's okay (pressed 1)
    NEEDS_DARCEE = "needs_darcee"  # Mom pressed 2 — wants Darcee to call her (terminal, NOT a failure)
    MISSED = "missed"          # all attempts failed, escalation handled
    ESCALATED = "escalated"    # urgent alert sent after max attempts


class AttemptStatus:
    PLACED = "placed"
    ANSWERED = "answered"
    MISSED = "missed"
    FAILED = "failed"          # provider/technical error


class Outcome:
    """Wellness-check outcomes (ANGEL-05 two-choice menu).

    Press 1 = okay → CONFIRMED_OK (the only wellness pass).
    Press 2 = needs Darcee → NEEDS_DARCEE (terminal, NOT a failure; pings Darcee).
    Everything else is a miss/technical state and advances the retry ladder.
    """
    CONFIRMED_OK = "confirmed_ok"            # pressed 1 — she's okay
    NEEDS_DARCEE = "needs_darcee"            # pressed 2 — she wants Darcee to call her
    ANSWERED_UNCONFIRMED = "answered_unconfirmed"  # connected, no input (treat as a miss)
    MISSED = "missed"                        # no answer / busy
    FAILED = "failed"                        # technical/system error (NOT "Mom missed")


class TargetType:
    EXTENSION = "extension"    # Mom's 3CX extension
    CELL = "cell"             # Mom's cell phone backup
    FAMILY = "family"         # future: sibling/family contact
