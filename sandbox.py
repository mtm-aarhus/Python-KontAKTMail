"""Kør robotten i hånden - uden trigger, uden kø.

For the first end-to-end test, and for reproducing a failure without waiting for the
scheduler. Set MODE below and run it.

Needs the same two environment variables the other robots' sandboxes use:

    OpenOrchestratorSQL   the ODBC connection string
    OpenOrchestratorKey   the crypto key

Everything else - the KontAKT URL, the API key, the DMZ URL, the push key - is read
from the OO credentials, exactly as a real run does. So if this works, the
credentials are right; if it fails on a credential, so would the scheduler.
"""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement, QueueStatus

from robot_framework.process import process
from robot_framework import config
from robot_framework import reset
import os
import json
from typing import Optional


def make_queue_element_with_payload(
    payload: dict | list,
    queue_name: str,
    reference: Optional[str] = None,
    created_by: Optional[str] = None,
    status: QueueStatus = QueueStatus.NEW,
) -> QueueElement:
    # Validate & serialize
    data_str = json.dumps(payload, ensure_ascii=False)
    if len(data_str) > 2000:
        raise ValueError("data exceeds 2000 chars (column limit)")

    return QueueElement(
        queue_name=queue_name,
        status=status,
        data=data_str,
        reference=reference,
        created_by=created_by,
    )


orchestrator_connection = OrchestratorConnection(
    "KontAKTDeliveryPush",
    os.getenv("OpenOrchestratorSQL"),
    os.getenv("OpenOrchestratorKey"),
    None,
    None,
    None
)

client = reset.reset(orchestrator_connection)

# "queue"      drain whatever KontAKT has queued, exactly as a real run does
# "reconcile"  the nightly pass: what should be out there vs what is, plus the
#              event log's trip home. Safe to run at any time - it deletes only
#              what KontAKT's full plan does not mention.
# "push"       one specific delivery link, by share_id. Fill in SHARE below.
MODE = "reconcile"

# For MODE = "push": read these off the case_shares row you want to test with.
CASE_ID = 35
SHARE_ID = 1


if MODE == "queue":
    task_count = 0
    while task_count < config.MAX_TASK_COUNT:
        task_count += 1
        queue_element = orchestrator_connection.get_next_queue_element(config.QUEUE_NAME)

        if not queue_element:
            orchestrator_connection.log_info("Queue empty.")
            break

        process(orchestrator_connection, queue_element, client)
        orchestrator_connection.set_queue_element_status(queue_element.id, QueueStatus.DONE)

elif MODE == "reconcile":
    qe = make_queue_element_with_payload(
        payload={"mode": "reconcile"},
        queue_name="KontAKTDeliveryPush",
        reference="Sandbox",
        status=QueueStatus.NEW,
    )
    process(orchestrator_connection, qe, client)

elif MODE == "push":
    qe = make_queue_element_with_payload(
        payload={
            "mode": "push_share",
            "kontakt_case_id": CASE_ID,
            "share_id": SHARE_ID,
        },
        queue_name="KontAKTDeliveryPush",
        reference="Sandbox",
        status=QueueStatus.NEW,
    )
    process(orchestrator_connection, qe, client)

else:
    raise ValueError(f"Unknown MODE: {MODE!r}")
