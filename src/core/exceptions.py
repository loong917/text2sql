class Text2SQLError(Exception):
    pass


class ConfigurationError(Text2SQLError):
    pass


class AgentError(Text2SQLError):
    pass


class DatabaseError(Text2SQLError):
    pass


class TrainingError(Text2SQLError):
    pass
