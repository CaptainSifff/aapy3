"""Diagnostics shared by lowering, conversions and interpretation."""
from aap_frontend import FrontendError


class SemanticError(FrontendError):
    def __init__(self, origin, reason):
        super(SemanticError, self).__init__(origin.source,
                                            origin.span.start.offset, reason)
        self.span = origin.span


class Unsupported(SemanticError):
    pass


class UndefinedName(SemanticError):
    pass
