"""Refusals the map's composition raises.

Subclasses of :class:`app.ai.exceptions.AgentError` rather than a parallel
hierarchy, for the reason Target Management gives: the message-safety contract
is the one the agent already keeps, so ``user_message`` is shown verbatim and
therefore never carries SQL, a stack trace or an internal identifier.

Every one is a *state* refusal — the request was well-formed and the data says
no — which is why the API answers 409 rather than 400 or 500, except the
not-found ones, which are 404 because "there is no such design" is a different
problem from "that design says no". The reason is the whole value to the
person editing a design, so it is shown rather than masked.
"""

from __future__ import annotations

from ..ai.exceptions import AgentError


class MapError(AgentError):
    """Base class for every refusal in this package."""

    code = "MAP_ERROR"
    user_message = "That map action could not be completed."


class DesignNotFound(MapError):
    code = "MAP_DESIGN_NOT_FOUND"

    def __init__(self, design_id: int) -> None:
        super().__init__(
            f"map design {design_id} not found",
            user_message="There is no map design with that id.",
            details={"design_id": design_id},
        )


class DesignNameTaken(MapError):
    """Two designs with one name would be two answers to "which map is this".

    Compared without regard to case, which is stricter than the unique
    constraint: "Business overview" beside "Business Overview" is a trap for
    the reader picking from the selector, not a second design.
    """

    code = "MAP_DESIGN_NAME_TAKEN"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"map design name {name!r} already used",
            user_message=f"A map design named \"{name}\" already exists. "
                         f"Choose another name.",
            details={"name": name},
        )


class DesignProtected(MapError):
    """The system default cannot be deleted or deactivated.

    The page has to open with *something*, and the seeded design is what
    guarantees that. Every other design is ordinary data.
    """

    code = "MAP_DESIGN_PROTECTED"

    def __init__(self, name: str, action: str) -> None:
        super().__init__(
            f"map design {name!r} is the system default and cannot be {action}",
            user_message=(
                f"\"{name}\" is the system default design and cannot be "
                f"{action}. Duplicate it and change the copy instead."
            ),
            details={"name": name, "action": action},
        )


class DesignInactive(MapError):
    """An inactive design cannot be the one the page opens with."""

    code = "MAP_DESIGN_INACTIVE"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"map design {name!r} is inactive",
            user_message=f"\"{name}\" is inactive. Activate it before making "
                         f"it the default.",
            details={"name": name},
        )


class InvalidDesign(MapError):
    """A design-level field the map cannot honour: basemap, metric, no layers."""

    code = "MAP_DESIGN_INVALID"

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid map design: {reason}", user_message=reason)


class InvalidLayer(MapError):
    """A layer the map cannot draw as described, named by level and field.

    One class for every layer refusal, because what the editor needs is the
    same each time: which layer, which field, and what would be accepted.
    """

    code = "MAP_LAYER_INVALID"

    def __init__(self, level: str, field: str, reason: str) -> None:
        super().__init__(
            f"invalid map layer {level!r}.{field}: {reason}",
            user_message=reason,
            details={"level": level, "field": field},
        )


__all__ = [
    "DesignInactive",
    "DesignNameTaken",
    "DesignNotFound",
    "DesignProtected",
    "InvalidDesign",
    "InvalidLayer",
    "MapError",
]
