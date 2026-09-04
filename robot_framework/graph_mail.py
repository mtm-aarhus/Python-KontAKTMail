"""Graph-klienten til fællespostkassen - og logikken der kobler en mail til en sag.

App-only access to one shared mailbox, authenticated with a certificate. Written to
be moved into ``robot_framework/`` once the mail robot exists; nothing here imports
KontAKT, and nothing here writes to a database.

OO configuration (mirrors the old SharePointAPI / SharePointCert pair - four values,
two credentials, two fields each):

    Credential KontAKTGraph   username = tenant id,   password = client id
    Credential KontAKTCert    username = thumbprint,  password = path to the .pem

The mailbox address is neither secret nor changing, so it is a plain constant.

DEN HER FIL KOPIERES UD I ROBOTTEN
    En OO-robot installeres som sit eget lille repo paa robotmaskinen og kan
    ikke importere fra KontAKT, saa `robot_framework/graph_mail.py` er en KOPI
    af den her fil. Retter du noget her, skal den kopieres ud igen - ellers
    kalder robotten noget, dens egen kopi ikke kender.

    Det er sket: 2026-09-03 fik `send()` argumenterne `content_id` og `inline`,
    kopien blev glemt, og hver eneste afsendelse fejlede med TypeError - mens
    signaturlogoet gik ud som en almindelig vedhaeftning. `tests/test_copies.py`
    fanger det nu.

WHY THE MATCHING IS BUILT THE WAY IT IS
    An e-mail has to land on the right aktindsigt, and the same person can easily
    have several running at once - so the sender's address is worthless on its own.
    The strategies below are tried strongest first:

      1. References / In-Reply-To    A reply carries the Message-ID of the mail it
                                     replies to, and KontAKT stores the id of every
                                     mail it sends in case_emails.message_id. So the
                                     ids are looked up there, and one that matches
                                     names exactly one case. Invisible to the
                                     applicant, survives quoting, cannot be typed
                                     wrong - and it has nothing to do with the
                                     addresses involved: everything goes to and from
                                     the one shared mailbox, from whatever address
                                     the applicant happens to use.
      2. Subject token               [KontAKT-35]. Visible and therefore editable
                                     and deletable, but it is the only thing that
                                     survives an applicant who starts a fresh mail
                                     instead of replying.
      3. conversationId              Exchange's own threading. Good within one
                                     mailbox, but it can merge unrelated threads
                                     that share a subject, so it never overrules
                                     the two above.
      4. Nothing matched             A new case - or a queue for a human, which is
                                     a decision for KontAKT, not for this module.

    A custom header on outbound mail (X-KontAKT-Case) is deliberately NOT a strategy:
    almost no mail client echoes unknown headers back on a reply, so it would look
    like it worked in testing with Outlook and quietly fail for everyone else.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]

# The domain KontAKT stamps into every Message-ID it sends (app/email_send.py).
# An inbound reference carrying it is provably an answer to one of our own mails.
OWN_ID_DOMAIN = "kontakt.aarhus.dk"

# The case token KontAKT already puts in every outgoing subject. The canonical
# form is written by app/cases/sanitize.add_case_tag and looks like "[KontAKT #35]"
# - keep the two in step, or the visible fallback silently stops working.
#
# Read loosely on purpose. This token exists precisely FOR the cases where the
# invisible machinery failed: an old client that dropped the headers, someone who
# started a fresh mail and pasted the subject, a phone that turned the space into
# a non-breaking one. So "#", "-" or nothing between, any spacing, any case.
SUBJECT_TOKEN = re.compile(r"\[\s*KontAKT\s*[#\-]?\s*(\d{1,10})\s*\]", re.IGNORECASE)

# Everything worth having when deciding which case a mail belongs to. Asked for
# explicitly because Graph returns none of the interesting parts by default.
SELECT = ",".join([
    "id", "subject", "from", "toRecipients", "ccRecipients", "replyTo",
    "receivedDateTime", "sentDateTime", "isRead", "hasAttachments",
    "internetMessageId", "conversationId", "conversationIndex",
    "bodyPreview", "webLink", "parentFolderId",
    "internetMessageHeaders",
])


# ---------------------------------------------------------------------------
# Adgang
# ---------------------------------------------------------------------------


@dataclass
class Config:
    tenant_id: str
    client_id: str
    thumbprint: str
    cert_path: str
    mailbox: str

    @classmethod
    def from_orchestrator(cls, oc, mailbox: str) -> "Config":
        """The four values as they are stored in OpenOrchestrator."""
        graph = oc.get_credential("KontAKTGraph")
        cert = oc.get_credential("KontAKTCert")
        return cls(tenant_id=graph.username, client_id=graph.password,
                   thumbprint=cert.username, cert_path=cert.password,
                   mailbox=mailbox)

    def private_key(self) -> str:
        path = Path(self.cert_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Fandt ikke den private nøgle på {path}. Stien kommer fra "
                f"KontAKTCert-credentialen, og filen skal ligge samme sted på hver "
                f"robotmaskine.")
        return path.read_text(encoding="utf-8")


class Mailbox:
    """One shared mailbox, over Graph, as the application itself.

    The token is cached until shortly before it expires: a run that reads fifty
    messages should authenticate once, not fifty times.
    """

    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self._token = None
        self._expires = 0.0
        self.session = requests.Session()

    # -- token ------------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() < self._expires - 120:
            return self._token
        import msal

        app = msal.ConfidentialClientApplication(
            client_id=self.cfg.client_id,
            authority=f"https://login.microsoftonline.com/{self.cfg.tenant_id}",
            client_credential={"private_key": self.cfg.private_key(),
                               "thumbprint": self.cfg.thumbprint},
        )
        result = app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            # These two fields are what actually says what went wrong - the
            # exception text alone is usually "invalid_client" and nothing else.
            raise RuntimeError(
                f"Kunne ikke hente token: {result.get('error')} - "
                f"{result.get('error_description', '')[:400]}")
        self._token = result["access_token"]
        self._expires = time.time() + int(result.get("expires_in", 3600))
        return self._token

    def token_claims(self) -> dict:
        """The claims inside the access token, for diagnostics.

        ``roles`` is the useful one: it lists the application permissions the token
        actually carries, which is the fastest way to tell a consent problem from a
        code problem.
        """
        payload = self.token().split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))

    # -- kald -------------------------------------------------------------
    def _call(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else f"{GRAPH}{path}"
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token()}"
        r = self.session.request(method, url, headers=headers, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Graph {method} {path} -> {r.status_code}: "
                               f"{r.text[:500]}")
        return r.json() if r.content else {}

    def _user(self, path: str) -> str:
        return f"/users/{self.cfg.mailbox}{path}"

    # -- læsning ----------------------------------------------------------
    def folders(self) -> list[dict]:
        return self._call("GET", self._user("/mailFolders?$top=50")).get("value", [])

    def messages(self, folder: str = "Inbox", top: int = 10,
                 unread_only: bool = False) -> list[dict]:
        query = f"?$top={int(top)}&$orderby=receivedDateTime desc&$select={SELECT}"
        if unread_only:
            query += "&$filter=isRead eq false"
        return self._call("GET", self._user(
            f"/mailFolders/{folder}/messages{query}")).get("value", [])

    def message(self, message_id: str) -> dict:
        """One message including its body.

        The list view deliberately does not ask for ``body`` - fifty full bodies
        is a lot of bytes to move in order to decide which case they belong to.
        The body is fetched once, for the mails that turned out to matter.
        """
        return self._call("GET", self._user(
            f"/messages/{message_id}?$select={SELECT},body"))

    def attachments(self, message_id: str) -> list[dict]:
        """Filerne paa en mail - med contentId, som er hele pointen.

        INTET ``$select`` HER, OG DET ER MED VILJE. ``contentId`` findes ikke paa
        basistypen ``microsoft.graph.attachment``, saa Graph afviser hele kaldet
        med 400 "Could not find a property named 'contentId'" hvis man beder om
        det - og udelader man det bare fra listen, kommer feltet ikke med, og saa
        er der ingenting at koble ``cid:``-henvisningerne i broedteksten til.
        Resultatet var et brudt billede midt i hver mail med et indlejret logo.

        Uden ``$select`` kommer alle felter, contentId iberegnet. Det koster
        ``contentBytes`` med i svaret, saa listen hentes kun én gang pr. mail -
        og bytes hentes alligevel bagefter for hver fil, der skal gemmes.
        """
        return self._call("GET", self._user(
            f"/messages/{message_id}/attachments")).get("value", [])

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        att = self._call("GET", self._user(
            f"/messages/{message_id}/attachments/{attachment_id}"))
        raw = att.get("contentBytes")
        return base64.b64decode(raw) if raw else b""

    def ensure_folder(self, display_name: str) -> str:
        """The id of a mail folder, created if it is not there yet.

        Handled mail is moved out of the inbox, so what is left in the inbox is
        exactly what has not been dealt with. That doubles as the alarm: a mail
        sitting in there means the robot could not place it AND could not answer
        it, which is the one case a person needs to look at.
        """
        for f in self.folders():
            if (f.get("displayName") or "").lower() == display_name.lower():
                return f["id"]
        created = self._call("POST", self._user("/mailFolders"),
                             json={"displayName": display_name})
        return created["id"]

    # -- skrivning --------------------------------------------------------
    def mark_read(self, message_id: str) -> None:
        """What stops the same mail being processed forever. The reason the app
        needs Mail.ReadWrite and not just Mail.Read."""
        self._call("PATCH", self._user(f"/messages/{message_id}"),
                   json={"isRead": True})

    def move(self, message_id: str, folder_id: str) -> dict:
        return self._call("POST", self._user(f"/messages/{message_id}/move"),
                          json={"destinationId": folder_id})

    def send(self, to: str, subject: str, body: str, *, html: bool = False,
             headers: Optional[dict] = None, cc: Optional[str] = None,
             in_reply_to: Optional[str] = None, references: Optional[str] = None,
             attachments: Optional[list] = None) -> str:
        """Send as the shared mailbox, and return the Message-ID it went out with.

        THE RETURN VALUE IS THE POINT. An applicant's reply carries the id of the
        mail it answers, in In-Reply-To - so the id we sent with is the only thing
        that will later tie that reply back to a case. It has to be stored in
        case_emails.message_id, or the match falls back to conversationId, which
        merges unrelated threads that share a subject.

        Which is why this does NOT use /sendMail: that endpoint answers 202 with an
        empty body, so the id Exchange assigned is never revealed and is lost the
        moment the mail leaves. Instead the mail is created as a draft - Exchange
        stamps internetMessageId at creation - and then sent. Two calls instead of
        one, in exchange for knowing what we sent.

        (Under SMTP, app/email_send.py mints the id itself and this problem does
        not arise. That is also why looks_like_ours() is only a hint: mail sent
        through Graph gets an Exchange id on outlook.com, not one on
        kontakt.aarhus.dk, and it is no less ours for that.)

        ``headers`` become internetMessageHeaders. Graph only allows custom headers
        whose name starts with ``X-``, and note that they are for OUR use when the
        message comes back to us in the Sent folder - a reply from an applicant will
        not carry them back. Threading is what carries across, not headers.
        """
        def _people(value):
            return [{"emailAddress": {"address": a.strip()}}
                    for a in (value or "").replace(";", ",").split(",") if a.strip()]

        message = {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": _people(to),
        }
        if cc:
            message["ccRecipients"] = _people(cc)

        # Graph refuses any internetMessageHeader whose name does not start with
        # "x-": setting In-Reply-To that way answers 400 InvalidInternetMessageHeader
        # and nothing is sent. So threading is done the only way Graph allows -
        # by asking it to build a reply to the original message, which makes
        # Exchange write In-Reply-To, References and the conversation id itself.
        #
        # If we cannot find the message being answered (it was deleted, or it was
        # sent over SMTP before the mailbox existed), the mail still goes out as a
        # fresh one. It is worth being clear about what that costs and what it does
        # not: the applicant's client may show it outside the thread, but OUR
        # matching is unaffected - their reply carries the Message-ID of whatever
        # we sent, and that is the id we store and look up.
        wire = {k: v for k, v in (headers or {}).items() if k.lower().startswith("x-")}
        draft = None
        if in_reply_to:
            draft = self._reply_draft(in_reply_to, message, wire)
        if draft is None:
            if wire:
                message["internetMessageHeaders"] = [
                    {"name": k, "value": v} for k, v in wire.items()]
            draft = self._call("POST", self._user("/messages"), json=message)
        message_id = draft.get("internetMessageId") or ""

        # Attachments go on the draft, one call each. Graph caps a simple upload at
        # 3 MB; anything larger needs an upload session, and KontAKT already refuses
        # attachments above 25 MB in the browser, so the session path is the one
        # that carries the rest.
        for att in (attachments or []):
            self._attach(draft["id"], att["name"], att["bytes"],
                         att.get("content_type"),
                         content_id=att.get("content_id"),
                         inline=bool(att.get("is_inline")))

        self._call("POST", self._user(f"/messages/{draft['id']}/send"))
        return message_id

    def find_by_message_id(self, message_id: str) -> Optional[dict]:
        """The mailbox item carrying this Message-ID, anywhere in the mailbox.

        Searched across all folders rather than just the inbox: the mail we are
        answering has usually been filed into "KontAKT - på sag" by the time a
        caseworker gets round to replying.
        """
        mid = (message_id or "").strip()
        if not mid:
            return None
        quoted = mid.replace("'", "''")
        try:
            found = self._call("GET", self._user(
                f"/messages?$filter=internetMessageId eq '{quoted}'"
                f"&$select=id,conversationId&$top=1")).get("value") or []
        except RuntimeError:
            return None
        return found[0] if found else None

    def _reply_draft(self, in_reply_to: str, message: dict,
                     extra_headers: dict) -> Optional[dict]:
        """A draft Exchange itself threaded onto the original, or None.

        createReply gives us the threading headers we are not allowed to write,
        and then the draft is overwritten with our own body and recipients (not
        the subject - see below) - the quoted original it prefills is not
        wanted, because KontAKT already shows the whole conversation on the
        case. Nor are the FILES it prefills: see _drop_prefilled.
        """
        original = self.find_by_message_id(in_reply_to)
        if original is None:
            self.log(f"Fandt ikke {in_reply_to} i postkassen - sender uden tråd")
            return None
        try:
            draft = self._call("POST", self._user(
                f"/messages/{original['id']}/createReply"))
            # Only what a draft actually accepts on a PATCH.
            # internetMessageHeaders is NOT in that set - Graph answers
            # "ErrorInvalidPropertySet" - because headers can only be written
            # when the message is created. No loss: our own X- header was never
            # a matching strategy, since almost no client echoes an unknown
            # header back on a reply. The threading, which is what we came for,
            # is already on the draft.
            #
            # AND NOT THE SUBJECT. Exchange derives the conversation from the
            # subject, so overwriting it puts the reply in a new thread - which
            # is the one thing this whole path exists to avoid. The subject
            # createReply chose is "RE: <the original>", and since every mail we
            # send carries [KontAKT #35], the tag is already in it.
            patch = {k: v for k, v in message.items()
                     if k in ("body", "toRecipients", "ccRecipients")}
            patched = self._call("PATCH", self._user(f"/messages/{draft['id']}"),
                                 json=patch)
        except RuntimeError as exc:
            self.log(f"Kunne ikke svare i tråden ({exc}) - sender uden tråd")
            return None
        # Efter brødteksten er skiftet, og ikke før: se _drop_prefilled.
        self._drop_prefilled(draft["id"])
        return patched

    def _drop_prefilled(self, draft_id: str) -> None:
        """Ryd de filer, createReply har arvet fra den mail, vi svarer på.

        DET HER ER GRUNDEN: createReply kopierer den oprindelige mails
        INDLEJREDE filer med over på kladden, og vores eget logo har et fast
        content-id. Efter det første svar i en tråd ligger der derfor to filer
        med cid ``aak-logo``: den arvede fra sidst og den, vi selv lægger på.
        Mailklienten viser den FØRSTE - altså den gamle. I én tråd her overlevede
        en logofil fra dagen før to udskiftninger af filen på disken, og for
        hvert svar kom der en kopi mere med.

        Det er sikkert at rydde dem ALLE: PATCH'en lige ovenfor har netop
        erstattet kladdens brødtekst med vores egen, så det citerede svar, der
        henviste til filerne, findes ikke længere. Ingenting peger på dem
        bagefter - de ville kun ligge og fylde i hver mail, vi sender.

        ``$select=id,name`` er med vilje: uden det svarer Graph med contentBytes
        for hver fil, og et indlejret skærmbillede fra en ansøger kan være
        megabytes. ``contentId`` må IKKE staa der - det findes ikke på
        basistypen, og hele kaldet ville fejle - men det skal det heller ikke,
        for her ryddes alt.

        Går det galt, sendes mailen alligevel. Et forkert logo kan man leve med;
        en besked, der ikke kommer af sted, kan man ikke.
        """
        try:
            arvet = self._call("GET", self._user(
                f"/messages/{draft_id}/attachments?$select=id,name")).get("value", [])
        except RuntimeError as exc:
            self.log(f"Kunne ikke se kladdens arvede filer ({exc}) - sender alligevel")
            return
        for att in arvet:
            try:
                self._call("DELETE", self._user(
                    f"/messages/{draft_id}/attachments/{att['id']}"))
                self.log(f"Fjernede arvet fil {att.get('name')!r} fra svarkladden")
            except RuntimeError as exc:
                self.log(f"Kunne ikke fjerne {att.get('name')!r} ({exc}) "
                         "- sender alligevel")

    def _attach(self, draft_id: str, name: str, data: bytes,
                content_type: Optional[str] = None, *,
                content_id: Optional[str] = None, inline: bool = False) -> None:
        """En fil paa kladden. ``inline`` er signaturlogoet og lignende: det
        vises inde i brødteksten via ``cid:<content_id>`` og skal ikke staa som
        en vedhaeftning, modtageren kan hente."""
        if len(data) <= 3 * 1024 * 1024:
            body = {"@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": content_type or "application/octet-stream",
                    "contentBytes": base64.b64encode(data).decode()}
            if content_id:
                body["contentId"] = content_id
            if inline:
                body["isInline"] = True
            self._call("POST", self._user(f"/messages/{draft_id}/attachments"),
                       json=body)
            return

        session = self._call(
            "POST", self._user(f"/messages/{draft_id}/attachments/createUploadSession"),
            json={"AttachmentItem": {"attachmentType": "file", "name": name,
                                     "size": len(data),
                                     "contentType": content_type
                                     or "application/octet-stream"}})
        url = session["uploadUrl"]
        chunk = 4 * 1024 * 1024
        for start in range(0, len(data), chunk):
            piece = data[start:start + chunk]
            end = start + len(piece) - 1
            # The upload URL carries its own authorisation, so no bearer token
            # here - sending one makes Graph reject the whole chunk.
            r = self.session.put(url, data=piece, timeout=300, headers={
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
                "Content-Length": str(len(piece))})
            if r.status_code >= 400:
                raise RuntimeError(f"Vedhæftning {name!r} fejlede ved {start}: "
                                   f"{r.status_code} {r.text[:300]}")


# ---------------------------------------------------------------------------
# Hvad en mail siger om, hvor den hører til
# ---------------------------------------------------------------------------


def headers_of(msg: dict) -> dict:
    """``internetMessageHeaders`` as a case-insensitive dict.

    Headers can repeat (Received, and References in some clients); the values are
    joined rather than dropped, because a lost reference is a lost match.
    """
    out: dict[str, str] = {}
    for h in msg.get("internetMessageHeaders") or []:
        key = (h.get("name") or "").lower()
        value = h.get("value") or ""
        out[key] = f"{out[key]} {value}".strip() if key in out else value
    return out


def message_ids_in(value: str) -> list[str]:
    """Every ``<id@host>`` in a header value, in order.

    References is a space-separated chain oldest-first, so the LAST one is the
    immediate parent - but any of them may be ours, and the oldest of ours is the
    one that started the thread.
    """
    return re.findall(r"<[^<>\s]+>", value or "")


def looks_like_ours(message_id: str) -> bool:
    """A cheap hint, not a rule.

    KontAKT's own Message-IDs currently carry OWN_ID_DOMAIN, because
    app/email_send.py mints them. But that only holds while KontAKT sends over
    SMTP - Exchange assigns its own id when a mail is sent through Graph. So this
    is used to sort the candidates, never to discard one.
    """
    return OWN_ID_DOMAIN in (message_id or "")


def referenced_ids(msg: dict) -> list[str]:
    """Every Message-ID this mail refers back to, best candidate first.

    Both In-Reply-To and References are read: a client that sets only one of them
    is common enough that relying on either alone loses matches.

    Nothing is filtered out. Which of these belongs to a case is a question only
    KontAKT can answer - it has the case_emails table - and answering it here by
    guessing at the domain would quietly stop working the day sending moves to
    Graph. Ours-looking ids are simply tried first.
    """
    h = headers_of(msg)
    ids: list[str] = []
    for header in ("in-reply-to", "references"):
        for mid in message_ids_in(h.get(header, "")):
            if mid not in ids:
                ids.append(mid)
    # Stable sort: the ones that look like ours move to the front, the rest keep
    # the order the client sent them in (References is oldest-first, so the last
    # is the immediate parent).
    return sorted(ids, key=lambda m: 0 if looks_like_ours(m) else 1)


def subject_case_id(msg: dict) -> Optional[int]:
    m = SUBJECT_TOKEN.search(msg.get("subject") or "")
    return int(m.group(1)) if m else None


def sender(msg: dict) -> str:
    return (((msg.get("from") or {}).get("emailAddress") or {})
            .get("address") or "").lower()


# Addresses that are never a person writing to us.
_ROBOT_LOCALPARTS = {"postmaster", "mailer-daemon", "mailerdaemon", "noreply",
                     "no-reply", "donotreply", "do-not-reply", "ikke-svar"}

# Precedence values that mean "bulk mail, do not answer" by long convention.
_BULK = {"bulk", "list", "junk", "auto_reply"}


def should_ignore(msg: dict, mailbox: str = "") -> Optional[str]:
    """Why this mail must be passed over in silence - or None to handle it.

    THIS IS THE MAIL-LOOP GUARD, and it is the reason KontAKT is allowed to answer
    strangers at all. An unmatched mail gets an automatic "write to post@mtm
    instead" reply; if the thing we answered was itself a machine, its answer comes
    back, and two robots can bounce a message between them until someone notices.
    Every rule below exists to make sure the automatic reply only ever reaches a
    human being.

    So the bar is deliberately low: anything that even smells automated is dropped.
    A false positive costs one ignored newsletter. A false negative costs a loop.
    """
    h = headers_of(msg)
    frm = sender(msg)

    # Our own mail, coming back to us. A copy of something we sent, or someone
    # testing by mailing the mailbox from itself. Answering it would be answering
    # ourselves - the shortest loop there is.
    if mailbox and frm == mailbox.lower():
        return "mailen er fra postkassen selv"

    local = frm.split("@")[0] if "@" in frm else ""
    if local in _ROBOT_LOCALPARTS:
        return f"afsenderen er en maskineadresse ({frm})"

    # RFC 3834. Any value other than an explicit "no" means auto-generated.
    auto = (h.get("auto-submitted") or "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"

    # An empty return path is the bounce convention: nowhere to send failures,
    # because this IS the failure notice.
    if (h.get("return-path") or "").strip() in ("<>", ""):
        if "return-path" in h:
            return "tom Return-Path (afvisningsbesked)"

    precedence = (h.get("precedence") or "").strip().lower()
    if precedence in _BULK:
        return f"Precedence: {precedence}"

    if h.get("list-id") or h.get("list-unsubscribe"):
        return "mailen kommer fra en mailingliste"

    # Out-of-office from Exchange and most others.
    if (h.get("x-auto-response-suppress") or "").strip():
        return "X-Auto-Response-Suppress (autosvar)"
    if (h.get("x-autoreply") or h.get("x-autorespond")):
        return "afsenderen markerer selv mailen som autosvar"

    # Delivery status notifications carry their own content type.
    ctype = (h.get("content-type") or "").lower()
    if "report-type=delivery-status" in ctype.replace(" ", ""):
        return "leveringskvittering/afvisning"

    return None


@dataclass
class Match:
    """How a mail should be routed, and why.

    ``reason`` exists so a caseworker looking at a wrongly-placed mail can be told
    what the machine went on - and so this decision can be argued with.
    """
    strategy: str
    reason: str
    message_ids: list[str] = field(default_factory=list)
    case_id: Optional[int] = None
    conversation_id: Optional[str] = None


def classify(msg: dict, mailbox: str = "") -> Match:
    """What this mail can be matched on. Does NOT look anything up.

    Deliberately free of database access: the robot hands the result to KontAKT,
    which owns the question of which case a Message-ID belongs to. That keeps this
    testable against a real mailbox with no database in sight.

    Four outcomes, and only four:

      ignorer     a machine wrote it - drop it without a word (see should_ignore)
      reference   it answers a mail; the ids go to KontAKT to be looked up
      emne        [KontAKT #35] in the subject
      samtale     same Exchange conversation as something we have seen
      afvis       a human, but nothing ties it to a case

    ``afvis`` does NOT create a case. This mailbox is for correspondence on cases
    that already exist; new requests belong on the self-service form or at
    post@mtm.aarhus.dk, and the robot answers with exactly that. Nobody reads this
    mailbox, so a mail we cannot place must not be left lying there in silence.
    """
    why = should_ignore(msg, mailbox)
    if why:
        return Match("ignorer", why, conversation_id=msg.get("conversationId"))

    refs = referenced_ids(msg)
    if refs:
        mine = [m for m in refs if looks_like_ours(m)]
        why = ("svar på vores egen mail" if mine
               else "et svar - slå id'erne op, før det bliver til en ny sag")
        return Match("reference", f"{why} ({refs[0]})",
                     message_ids=refs, conversation_id=msg.get("conversationId"))

    case_id = subject_case_id(msg)
    if case_id is not None:
        return Match("emne", f"sagsnummer i emnefeltet ([KontAKT-{case_id}])",
                     case_id=case_id, conversation_id=msg.get("conversationId"))

    # Exchange stamps a conversationId on EVERY mail, including the first one in a
    # brand new thread - so this on its own is not evidence of anything. It is a
    # candidate to look up, and KontAKT turns it into "afvis" when the id is one it
    # has never seen. Saying more than that here would be the same overclaim as
    # calling every reply "a reply to our own mail".
    conv = msg.get("conversationId")
    if conv:
        return Match("samtale", "kender vi den Exchange-samtale? ellers afvises den",
                     conversation_id=conv)

    return Match("afvis", "et menneske, men intet der peger på en sag")
