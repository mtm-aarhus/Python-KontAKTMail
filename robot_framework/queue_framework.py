"""This module is the primary module of the robot framework. It collects the functionality of the rest of the framework."""

# This module is not meant to exist next to linear_framework.py in production:
# pylint: disable=duplicate-code

import json
import sys

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueStatus

from robot_framework import initialize
from robot_framework import reset
from robot_framework.exceptions import handle_error, BusinessError, CaseDeleted, log_exception
from robot_framework import process
from robot_framework import config

# The payload the scheduled pass runs with. It is not a queue element: sending is
# queue-driven, and a schedule has nothing to put in front of the robot — so the
# trigger asks for the mailbox to be read through its process arguments instead.
POLL = '{"mode": "poll"}'


class _TriggerElement:
    """Stands in for a queue element when the work came from a trigger argument.

    ``process`` reads only ``.data``. Nothing sets a status on this one — there is no
    queue row to set it on, and the trigger's own job record is what the run shows up
    as in OpenOrchestrator.
    """

    id = None

    def __init__(self, data: str):
        self.data = data


def _poll_requested(orchestrator_connection) -> bool:
    """Did the trigger ask for the mailbox to be read?

    Accepts both the JSON the queue payloads use and the bare word, because that
    field is typed by hand into the trigger and a stray brace should not quietly
    stop the mailbox being read for months. Nobody is watching that mailbox, so a
    silently disabled poll is the failure that would take longest to notice.
    """
    raw = (getattr(orchestrator_connection, "process_arguments", None) or "").strip()
    if not raw:
        return False
    try:
        return (json.loads(raw) or {}).get("mode") == "poll"
    except (ValueError, AttributeError):
        # Not valid JSON - a hand-typed word, or a brace that got lost. Only the
        # unparseable case lands here, so looking for the word is safe: anything that
        # DID parse was already answered above, and this field never carries another
        # mode. 'send' comes from the queue, never from a trigger.
        return "poll" in raw.lower()


def main():
    """The entry point for the framework. Should be called as the first thing when running the robot."""
    orchestrator_connection = OrchestratorConnection.create_connection_from_args()
    sys.excepthook = log_exception(orchestrator_connection)

    orchestrator_connection.log_trace("Robot Framework started.")
    initialize.initialize(orchestrator_connection)

    queue_element = None
    error_count = 0
    task_count = 0
    # Retry loop
    for _ in range(config.MAX_RETRY_COUNT):
        try:
            # reset() cleans the slate and (re)opens the shared connections
            # (GO/Nova + KontAKT, cached credentials), returning them as a
            # client that's reused across every queue element instead of
            # reconnecting per document. Re-run on each outer retry.
            client = reset.reset(orchestrator_connection)

            # Read the mailbox first, then send. That order matters on a busy run:
            # an applicant's reply that arrives while a caseworker is writing gets
            # onto the case before the answer goes out, so the thread reads in the
            # order things actually happened.
            #
            # A failure here falls to the outer handler and is retried like any
            # other. Safe to repeat: every mail that was filed has been moved out
            # of the inbox, and KontAKT recognises one it has already stored.
            if _poll_requested(orchestrator_connection):
                orchestrator_connection.log_info("Læser fællespostkassen (fra triggerens argument).")
                process.process(orchestrator_connection, _TriggerElement(POLL), client)

            # Queue loop
            while task_count < config.MAX_TASK_COUNT:
                task_count += 1
                queue_element = orchestrator_connection.get_next_queue_element(config.QUEUE_NAME)

                if not queue_element:
                    orchestrator_connection.log_info("Queue empty.")
                    break  # Break queue loop

                try:
                    # Per-element attempts: a transient failure (dropped session,
                    # expired token, flaky upload) reconnects via reset() and
                    # retries the same element. A BusinessError is never retried.
                    for attempt in range(1, config.QUEUE_ATTEMPTS + 1):
                        try:
                            process.process(orchestrator_connection, queue_element, client)
                            break
                        except CaseDeleted as exc:
                            # Deleted in KontAKT while this element waited. DONE,
                            # not FAILED — retrying can't bring it back, and this
                            # is not something an operator needs to look at.
                            orchestrator_connection.log_info(f"Skipping queue element: {exc}")
                            break
                        except BusinessError:
                            raise
                        # pylint: disable-next=broad-exception-caught
                        except Exception as exc:
                            orchestrator_connection.log_info(
                                f"Attempt {attempt}/{config.QUEUE_ATTEMPTS} failed: {exc!r}"
                            )
                            if attempt < config.QUEUE_ATTEMPTS:
                                orchestrator_connection.log_info("Reconnecting and retrying queue element.")
                                client = reset.reset(orchestrator_connection)
                            else:
                                raise

                    orchestrator_connection.set_queue_element_status(queue_element.id, QueueStatus.DONE)

                except BusinessError as error:
                    handle_error("Business Error", error, queue_element, orchestrator_connection)

            break  # Break retry loop

        # We actually want to catch all exceptions possible here.
        # pylint: disable-next = broad-exception-caught
        except Exception as error:
            error_count += 1
            handle_error(f"Process Error #{error_count}", error, queue_element, orchestrator_connection)

    reset.clean_up(orchestrator_connection)
    reset.close_all(orchestrator_connection)
    reset.kill_all(orchestrator_connection)

    if config.FAIL_ROBOT_ON_TOO_MANY_ERRORS and error_count == config.MAX_RETRY_COUNT:
        raise RuntimeError("Process failed too many times.")
