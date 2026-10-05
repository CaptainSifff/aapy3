"""Interpreter-owned Work identity, separate from source IDs and recipe values."""


class WorkIdentity(object):
    """Entry loader supplies the historical top_recipe spelling explicitly.

    Relative names use the active logical cwd, as in Port.port_makesum.
    Includes and deferred-body source IDs never set or replace this identity.
    This object grants no filesystem access by itself.
    """
    def __init__(self, top_recipe=None):
        self.top_recipe = top_recipe
