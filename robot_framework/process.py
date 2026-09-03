"""KontAKT mailrobot - fællespostkassen mtmkontakt@mtm.aarhus.dk.

All correspondence with an applicant goes through one shared mailbox, over Graph,
authenticated with a certificate. Nobody reads that mailbox; this robot does.
Mode is set by ``mode`` in the payload:

* ``send``  - queue-driven. One message a caseworker wrote in KontAKT. Sent as the
              shared mailbox, and the Message-ID Exchange assigned is reported
              back, because the applicant's reply will carry exactly that id and it
              is what ties the answer to the case.
* ``poll``  - the scheduled pass. Reads the inbox, asks KontAKT where each mail
              belongs, and files it. Fired from a trigger's process arguments,
              since a schedule has no queue element to put in front of the robot.

WHY THE ROBOT DECIDES NOTHING
    It can read headers; only KontAKT knows which Message-ID belongs to which case,
    because only KontAKT has case_emails. So this robot classifies (graph_mail),
    posts the candidates, and does what it is told: file the mail on a case, send
    the standard "this mailbox does not take new requests" answer, or drop it.
    Deciding to write to a citizen is never taken here.

WHY IT IS SAFE TO ANSWER STRANGERS AT ALL
    Two ceilings, and they are independent. graph_mail.should_ignore refuses to
    process anything written by a machine - autoreplies, bounces, mailing lists,
    our own mail coming back - so an automatic answer can only ever reach a person.
    KontAKT then allows one answer per address per day. The first stops a loop; the
    second stops a person being buried.
"""
import json
from datetime import datetime, timezone

import requests
from OpenOrchestrator.database.queues import QueueElement
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection

from robot_framework import config
from robot_framework.exceptions import BusinessError
from robot_framework.graph_mail import Config, Mailbox, classify

POLL = '{"mode": "poll"}'
TIMEOUT = 120


# ----- forbindelser ----------------------------------------------------------


class Client:
    """One set of connections per run.

    Built once and handed to every queue element, so a run that sends twenty mails
    authenticates once. A failed element rebuilds them before retrying, because the
    likeliest reason a call failed is a connection that went stale.
    """

    def __init__(self, oc: OrchestratorConnection):
        api = oc.get_credential("KontAKTAPI")
        self.kontakt_base = api.username.rstrip("/")
        self.kontakt = requests.Session()
        self.kontakt.headers.update({"X-API-Key": api.password})
        self.box = Mailbox(Config.from_orchestrator(oc, config.MAILBOX),
                           log=oc.log_trace)


def create_client(oc: OrchestratorConnection) -> Client:
    return Client(oc)


def _get(client: Client, path: str) -> dict:
    r = client.kontakt.get(f"{client.kontakt_base}{path}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(client: Client, path: str, body: dict) -> dict:
    r = client.kontakt.post(f"{client.kontakt_base}{path}", json=body,
                            timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ----- indgangen -------------------------------------------------------------


def process(orchestrator_connection: OrchestratorConnection,
            queue_element: QueueElement | None = None,
            client: Client | None = None) -> None:
    oc = orchestrator_connection
    payload = json.loads(queue_element.data) if queue_element and queue_element.data else {}
    mode = payload.get("mode") or "poll"

    if client is None:
        client = create_client(oc)

    if mode == "send":
        _send_one(oc, client, payload)
    elif mode == "poll":
        _poll(oc, client)
    else:
        raise BusinessError(f"Ukendt mode: {mode!r}")


# ----- udgående --------------------------------------------------------------


def _send_one(oc, client: Client, payload: dict) -> None:
    """Send one message a caseworker wrote, and report how it went.

    Every failure path ends in a callback. A message whose robot died in silence
    would sit as "sendes" in the thread forever, and a caseworker would have no way
    to tell that from one that is simply still in the queue.
    """
    email_id = int(payload["email_id"])
    path = f"/api/v1/mail/outbound/{email_id}"

    # RESERVÉR FØRST. Der kan findes to køelementer for den samme besked - en
    # sagsbehandler trykker "Send igen", og nogen genkører samtidig det gamle
    # element i OpenOrchestrator. KontAKT afgør med én atomisk UPDATE, hvem der
    # har beskeden; den anden får "skip" og rører den ikke. Uden det ville
    # ansøgeren få den samme mail to gange, og det kan ikke gøres om.
    try:
        item = _post(client, f"{path}/claim", {})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            oc.log_info(f"Mail {email_id} findes ikke længere - beskeden er slettet")
            return
        raise

    if item.get("already_sent"):
        oc.log_info(f"Mail {email_id} er allerede sendt - springer over")
        return
    if item.get("skip"):
        oc.log_info(f"Mail {email_id}: {item.get('reason')} - springer over")
        return

    attachments = []
    for att in item.get("attachments") or []:
        r = client.kontakt.get(
            f"{client.kontakt_base}{path}/attachment/{att['id']}", timeout=600)
        if r.status_code == 410:
            # The staged bytes are gone. Sending the mail without the file the
            # caseworker attached would be worse than not sending it.
            _post(client, f"{path}/sent",
                  {"ok": False, "note": f"Vedhæftningen {att['name']!r} kunne ikke "
                                        f"findes længere og mailen blev ikke sendt."})
            return
        r.raise_for_status()
        attachments.append({"name": att["name"], "bytes": r.content,
                            "content_type": att.get("content_type"),
                            "content_id": att.get("content_id"),
                            "is_inline": att.get("is_inline")})

    oc.log_info(f"Sender mail {email_id} til {item.get('to')} "
                f"({len(attachments)} vedhæftninger)")
    try:
        message_id = client.box.send(
            to=item["to"], cc=item.get("cc"),
            subject=item.get("subject") or "",
            body=item.get("body_html") or item.get("body_text") or "",
            html=bool(item.get("body_html")),
            in_reply_to=item.get("in_reply_to"),
            references=item.get("references"),
            headers={"X-KontAKT-Case-Id": str(item.get("case_id") or "")},
            attachments=attachments,
        )
    except Exception as exc:  # pylint: disable=broad-except
        # Reported, not retried: config.QUEUE_ATTEMPTS is 1 because a retry after a
        # send that actually worked puts a second copy in the applicant's inbox.
        _post(client, f"{path}/sent", {"ok": False, "note": str(exc)[:400]})
        raise

    _post(client, f"{path}/sent", {"ok": True, "message_id": message_id})
    oc.log_info(f"Mail {email_id} sendt, Message-ID {message_id}")


# ----- indgående -------------------------------------------------------------


def _body_of(msg: dict) -> tuple[str, str]:
    body = msg.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        return "", content
    return content, ""


def _poll(oc, client: Client) -> None:
    """Read the inbox once and file everything in it.

    What is left in the inbox afterwards is the alarm: a mail nobody could place
    and nobody could answer. Everything else has been moved into one of the three
    KontAKT folders, so "inbox is empty" means "nothing needs a human".
    """
    box = client.box
    handled = box.ensure_folder(config.FOLDER_HANDLED)
    rejected = box.ensure_folder(config.FOLDER_REJECTED)
    ignored = box.ensure_folder(config.FOLDER_IGNORED)

    messages = box.messages(folder="Inbox", top=config.POLL_BATCH)
    oc.log_info(f"{len(messages)} mails i indbakken")
    counts = {"paa_sag": 0, "afvist": 0, "ignoreret": 0, "sprunget_over": 0}

    for head in messages:
        gid = head["id"]
        match = classify(head, config.MAILBOX)

        if match.strategy == "ignorer":
            oc.log_info(f"Ignorerer: {match.reason}")
            box.move(gid, ignored)
            counts["ignoreret"] += 1
            continue

        # Only now is the full message worth fetching - the body is the expensive
        # part, and most of what a shared mailbox receives never needs it.
        msg = box.message(gid)
        text, html = _body_of(msg)
        # Indlejrede billeder tages MED. De taelles ikke som vedhaeftninger, men
        # uden dem staar der et brudt billede midt i ansoegerens tekst, hver gang
        # nogen aabner sagen - typisk et signaturlogo eller et skaermbillede, der
        # var hele pointen med mailen.
        atts = box.attachments(gid)

        answer = _post(client, "/api/v1/mail/inbound", {
            "message_id": (msg.get("internetMessageId") or "")[:255],
            "message_ids": match.message_ids,
            "case_id": match.case_id,
            "conversation_id": msg.get("conversationId"),
            "in_reply_to": (match.message_ids or [None])[0],
            "from_address": _address(msg.get("from")),
            "to_addresses": _addresses(msg.get("toRecipients")) or config.MAILBOX,
            "cc_addresses": _addresses(msg.get("ccRecipients")),
            "subject": msg.get("subject"),
            "body_text": text,
            "body_html": html,
            "received_at": msg.get("receivedDateTime"),
            "attachments": [{"name": a.get("name"),
                             "content_type": a.get("contentType"),
                             "size_bytes": a.get("size"),
                             "content_id": a.get("contentId"),
                             "is_inline": bool(a.get("isInline")),
                             "graph_id": a.get("id")} for a in atts],
        })

        action = answer.get("action")
        if action == "stored":
            oc.log_info(f"På sag {answer.get('case_id')}: {answer.get('reason')}")
            if not answer.get("duplicate"):
                _upload_attachments(oc, client, box, gid, answer)
            _notify(oc, answer)
            box.move(gid, handled)
            counts["paa_sag"] += 1
        elif action == "autoreply":
            oc.log_info(f"Afviser og svarer {answer.get('to')}: {answer.get('reason')}")
            _autoreply(oc, client, answer, msg)
            box.move(gid, rejected)
            counts["afvist"] += 1
        elif action == "drop":
            oc.log_info(f"Afviser uden svar: {answer.get('note')}")
            box.move(gid, rejected)
            counts["afvist"] += 1
        else:
            # An answer we do not understand. Leave the mail where it is: the
            # inbox is the alarm, and a mail we cannot account for belongs in it.
            oc.log_info(f"Uventet svar fra KontAKT: {answer!r}")
            counts["sprunget_over"] += 1

    oc.log_info("Postkassen gennemgået: "
                + ", ".join(f"{k}={v}" for k, v in counts.items()))


def _upload_attachments(oc, client: Client, box, gid: str, answer: dict) -> None:
    """Hent hver vedhæftnings bytes ned og aflever dem til KontAKT.

    Metadata kom med, da mailen blev afleveret; filerne kommer bagefter, én ad
    gangen. Uden det her skridt kender KontAKT navnet paa det, ansoegeren sendte,
    og har ikke filen - hvilket er vaerre end ingenting, fordi det ser ud som om
    dokumentet er der.

    En fil, der fejler, stopper ikke resten: mailen selv er allerede paa sagen,
    og en enkelt vedhaeftning der mangler er bedre end en mail der ikke kom ind.
    """
    case_id = answer.get("case_id")
    email_id = answer.get("email_id")
    for att in (answer.get("attachments") or []):
        graph_id, kontakt_id = att.get("graph_id"), att.get("id")
        if not graph_id or not kontakt_id:
            continue
        try:
            data = box.attachment_bytes(gid, graph_id)
            if not data:
                oc.log_info(f"Vedhæftning {kontakt_id} var tom - springer over")
                continue
            r = client.kontakt.post(
                f"{client.kontakt_base}/api/v1/cases/{case_id}/emails/{email_id}"
                f"/attachments/{kontakt_id}/file",
                data=data, timeout=600,
                headers={"Content-Type": "application/octet-stream"})
            r.raise_for_status()
        except Exception as exc:                                        # noqa: BLE001
            oc.log_info(f"Kunne ikke hente vedhæftning {kontakt_id}: {exc!r}")


def _autoreply(oc, client: Client, answer: dict, original: dict) -> None:
    """Tell a stranger where their request actually belongs.

    Threaded onto their own mail on purpose, so it lands in their conversation and
    reads as an answer rather than as unrelated mail from an address they have
    never written to. Auto-Submitted marks it as machine-written, which is what
    keeps a well-behaved autoresponder at the other end from answering us back.
    """
    # Only X- headers survive: Graph rejects any other name outright, so
    # Auto-Submitted (RFC 3834) cannot be set from here however much we would
    # like to. X-Auto-Response-Suppress is the one Exchange itself reads, and it
    # is what stops an out-of-office bouncing back at us from a mailbox on this
    # tenant.
    #
    # For everyone else the protection is the incoming side, not the outgoing
    # one: their autoresponder's answer arrives carrying its OWN Auto-Submitted,
    # and should_ignore drops it. So a loop still cannot get going - we simply
    # cannot ask the other end to be polite in advance.
    client.box.send(
        to=answer["to"],
        subject=answer["subject"],
        body=answer["body"],
        in_reply_to=original.get("internetMessageId"),
        headers={"X-Auto-Response-Suppress": "All"},
    )


def _notify(oc, answer: dict) -> None:
    """Nothing to do here yet - KontAKT sends the notification itself.

    Kept as the one place that would change if notifications ever move out to the
    robot, so the decision is visible rather than absent.
    """
    recipients = answer.get("notify") or []
    if recipients:
        oc.log_info(f"KontAKT orienterer {', '.join(recipients)}")
    else:
        oc.log_info("Ingen at orientere - sagen har hverken sagsbehandler "
                    "eller teampostkasse")


def _address(person: dict | None) -> str:
    return (((person or {}).get("emailAddress") or {}).get("address") or "").lower()


def _addresses(people: list | None) -> str:
    return ", ".join(_address(p) for p in (people or []) if _address(p))
