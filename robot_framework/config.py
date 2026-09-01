"""This module contains configuration constants used across the framework"""

# The number of times the robot retries on an error before terminating.
MAX_RETRY_COUNT = 3

# Whether the robot should be marked as failed if MAX_RETRY_COUNT is reached.
FAIL_ROBOT_ON_TOO_MANY_ERRORS = False

# Error screenshot config
SMTP_SERVER = "smtp.adm.aarhuskommune.dk"
SMTP_PORT = 25
SCREENSHOT_SENDER = "mtmkontaktmail@aarhus.dk"

# Constant/Credential names
ERROR_EMAIL = "Error Email"


# Queue specific configs
# ----------------------

# The name of the job queue (if any)
QUEUE_NAME = "KontAKTMail"

# The limit on how many queue elements to process
MAX_TASK_COUNT = 50

# Number of attempts per queue_element (1 is no retry, 2 is 2 total attempts and so on).
#
# ONE ATTEMPT, DELIBERATELY. Everything else in KontAKT retries, because rebuilding
# a bundle or re-journalising a document lands in the same place twice. Sending
# a mail does not: a second attempt after a send that actually worked puts a second
# copy in the applicant's inbox, and no amount of care here can take it back.
#
# The failure is recorded on the message instead (case_emails.send_error), and the
# caseworker sees it in the thread and decides whether to send again.
QUEUE_ATTEMPTS = 1

# ----------------------

# The shared mailbox. Neither secret nor changing, so it is not a credential.
MAILBOX = "mtmkontakt@mtm.aarhus.dk"

# Folders the robot files handled mail into, so the inbox only ever holds what has
# not been dealt with. Created on first use.
FOLDER_HANDLED = "KontAKT - på sag"
FOLDER_REJECTED = "KontAKT - afvist"
FOLDER_IGNORED = "KontAKT - maskinpost"

# How many messages one polling pass takes. A backlog is drained over several
# passes rather than in one long run that holds a robot machine for an hour.
POLL_BATCH = 50
