"""Neutral errors shared by research tool and persistence boundaries."""


class ToolError(ValueError):
    """A safe validation or scope error suitable for returning to a model."""

    tool_budget: dict[str, object]

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"error": self.code, "message": self.message}
        if hasattr(self, "tool_budget"):
            result["_tool_budget"] = dict(self.tool_budget)
        return result
