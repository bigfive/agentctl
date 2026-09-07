"""Shared child-adapter contract.

A child adapter answers four questions for one harness:

1. what argv starts a fresh non-interactive run
2. what argv sends a follow-up turn into an existing native session
3. how the native session id is learned
4. how a terminal outcome is read out of the run

Every adapter runs its harness fully permissive. agentctl does not implement
approval or permission handling; an approval prompt is a failure, not a
negotiation.

The prompt is always written to the child's stdin. No prompt text is ever
placed on the command line, so a prompt beginning with ``-`` cannot be read as
a flag.
"""


class ChildAdapter(object):
    harness = None

    def preallocate_session(self, cwd, model):
        """Return a native session id to use before the child starts.

        Adapters that cannot know the id up front return ``None`` and learn it
        from the child's event stream instead.
        """
        return None

    def start_command(self, session_id, cwd, model, result_file):
        raise NotImplementedError

    def followup_command(self, session_id, cwd, model, result_file):
        raise NotImplementedError

    def read_event(self, event):
        """Translate one parsed stream event into an outcome fragment.

        Recognised keys in the returned dict:

        ``native_session_id``  the harness's own session id
        ``result``             final assistant text
        ``failed``             the harness reported a failed turn
        ``error``              a human-readable failure message
        """
        return {}
