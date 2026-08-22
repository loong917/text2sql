"""Application-specific exception types."""


class Text2SQLError(Exception):
    pass


class ConfigurationError(Text2SQLError):
    pass


class DatabaseConnectionError(Text2SQLError):
    pass
