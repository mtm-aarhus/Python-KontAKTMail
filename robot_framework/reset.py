"""This module handles resetting the state of the computer so the robot can work with a clean slate.

For this robot the "state" is one HTTP session into KontAKT plus a Graph token for
the shared mailbox. ``reset`` opens them and returns the client the queue framework
hands to every element, so a run that sends twenty mails authenticates once instead
of twenty times.

Reopening on a retry is the point: the likeliest reason a call failed is a
connection that went stale or a token that expired mid-run, and both are fixed by
building the client again.
"""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection

from robot_framework import process


def reset(orchestrator_connection: OrchestratorConnection) -> process.Client:
    """Reset the state of the computer so the robot can work with a clean slate."""
    orchestrator_connection.log_trace("Resetting.")
    clean_up(orchestrator_connection)
    close_all(orchestrator_connection)
    return open_all(orchestrator_connection)


def clean_up(orchestrator_connection: OrchestratorConnection) -> None:
    """Clean up after the robot.

    Nothing is written to disk: mail bodies and attachments are held in memory for
    the moment it takes to hand them to KontAKT, and attachments on their way out
    are streamed from KontAKT straight into the Graph draft. So there are no
    temporary files to remove - and no copies of an applicant's documents left on
    a robot machine.
    """
    orchestrator_connection.log_trace("Cleaning up.")


def open_all(orchestrator_connection: OrchestratorConnection) -> process.Client:
    """Open the connections this robot needs, once per run."""
    orchestrator_connection.log_trace("Opening applications.")
    return process.create_client(orchestrator_connection)


def close_all(orchestrator_connection: OrchestratorConnection) -> None:
    """Close all applications.

    The sessions are plain HTTP and are garbage collected with the client; there is
    no application to shut down and no window to close.
    """
    orchestrator_connection.log_trace("Closing applications.")


def kill_all(orchestrator_connection: OrchestratorConnection) -> None:
    """Forcefully close all applications used by the robot.

    Empty for the same reason as close_all - this robot opens no application, only
    HTTP sessions. It has to exist all the same: queue_framework calls it in its
    final teardown, and a missing name there fails the whole run AFTER the work is
    done, which reads as a broken robot when nothing broke.
    """
    orchestrator_connection.log_trace("Killing all applications.")
